"""Align brace layers so the designspace has no duplicate source locations.

Glyphs.app tolerates brace (intermediate) layers at the same location being
attached to different masters in different glyphs. glyphsLib turns each
association into a separate designspace source, so varLib then fails with
"Locations must be unique" and fontmake gives up.

Georg Seifert put it plainly in googlefonts/glyphsLib#925: *"A brace layer is
what is called a sparse master in ufo/designspace. So no connection to the
master it is attached to. It just has to be somewhere."*

Upstream: googlefonts/glyphsLib#995, open since 2024. anthrotype: *"ideally we
should [ignore the putative master for an intermediate layer]. PR most
welcome."* schriftgestalt: *"If someone could put that into a test case, I
could fix this in the Glyphs3 branch."* When that lands, this module can go.

What "the same location" means
------------------------------

Not the coordinates written in the layer name. ``builder/sources.py`` fills a
short coordinate list up from the **associated master**:

    if len(master_coordinates) < len(designspace.axes):
        master_locations = [master.location[a.name] for a in designspace.axes]
        master_coordinates = brace_coordinates + master_locations[len(...):]

So on a wght/wdth font a ``{80}`` layer on a Width 100 master and a ``{80}``
layer on a Width 50 master are two *different* sources, and conversely
``{80, 100}`` on one master collides with ``{80}`` on a master whose Width is
100. Comparing the raw coordinates gets both cases wrong, so
:func:`effective_location` reproduces the padding.

That padding also constrains the repair: for a layer whose coordinates are
*short*, the associated master is what supplies the rest of the location, so
reassigning it would move the sparse master through the design space and
change what the font interpolates. Only layers whose coordinates cover every
axis can be reassigned safely; a short layer in conflict is reported for a
human to resolve, never moved.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from glyphsLib.classes import GSFont, GSLayer

LOGGER = logging.getLogger(__name__)

#: How many names the summary lists before it stops.
MAX_REPORTED_GLYPHS = 10

#: A design-space location: one value per font axis.
Location = tuple[float, ...]


@dataclass
class BraceLayerAlignResult:
    """What :func:`align_brace_layers_to_variable_origin` did."""

    moved: int = 0
    target_master_id: str = ""
    glyphs: list[str] = field(default_factory=list)
    #: Locations claimed by more than one master. Zero means the source was
    #: already in the state varLib wants.
    conflicting_locations: int = 0
    #: Glyphs carrying a conflicting layer that could NOT be moved, because its
    #: coordinates are shorter than the axis count and the associated master
    #: therefore supplies part of its location. Moving those would shift the
    #: sparse master; they need a decision this code cannot make.
    unresolved_glyphs: list[str] = field(default_factory=list)

    def summary(self) -> str:
        """A single line for a processing log."""
        parts = []
        if self.moved:
            names = ", ".join(self.glyphs[:MAX_REPORTED_GLYPHS])
            text = (
                f"{self.moved} brace layer(s) on {len(self.glyphs)} glyph(s) "
                f"reassigned to master {self.target_master_id}: {names}"
            )
            if len(self.glyphs) > MAX_REPORTED_GLYPHS:
                text += f" and {len(self.glyphs) - MAX_REPORTED_GLYPHS} more"
            parts.append(text)
        if self.unresolved_glyphs:
            names = ", ".join(self.unresolved_glyphs[:MAX_REPORTED_GLYPHS])
            text = (
                f"{len(self.unresolved_glyphs)} glyph(s) still share a brace "
                f"layer location through different masters and cannot be "
                f"realigned automatically, because their coordinates do not "
                f"cover every axis: {names}"
            )
            if len(self.unresolved_glyphs) > MAX_REPORTED_GLYPHS:
                text += f" and {len(self.unresolved_glyphs) - MAX_REPORTED_GLYPHS} more"
            parts.append(text)
        if not parts:
            return "no brace layers needed realigning"
        return "; ".join(parts)


def variable_font_origin_master_id(font: GSFont) -> str:
    """The master id Glyphs uses as the variable font origin.

    Falls back to the first master, which is what Glyphs itself does when no
    origin is set.
    """
    origin = font.customParameters.get("Variable Font Origin")
    if origin:
        return str(origin)

    legacy = font.customParameters.get("Variation Font Origin")
    if legacy:
        for master in font.masters:
            if master.name == legacy:
                master_id: Any = master.id
                return str(master_id)

    first_master_id: Any = font.masters[0].id
    return str(first_master_id)


def is_brace_layer(layer: GSLayer) -> bool:
    """Whether a layer is a brace/intermediate layer.

    ``GSLayer._is_brace_layer()`` is private and its implementation differs by
    format version - it reads ``self.attributes`` on Glyphs 3 and parses the
    layer name on Glyphs 2, both through ``self.parent.parent`` - so it is
    called defensively here. A layer we cannot classify is left alone.
    """
    checker: Any = getattr(layer, "_is_brace_layer", None)
    if not callable(checker):
        return False
    try:
        return bool(checker())
    except Exception:  # noqa: BLE001 - private API, any shape change is fine to ignore
        LOGGER.debug("Could not classify layer %r as a brace layer", layer, exc_info=True)
        return False


def brace_coordinates(layer: GSLayer) -> Location | None:
    """The coordinates written on a brace layer, or None if unreadable.

    These are the raw values; they may be shorter than the axis count. Use
    :func:`effective_location` for anything that compares locations.
    """
    reader: Any = getattr(layer, "_brace_coordinates", None)
    if not callable(reader):
        return None
    try:
        coordinates = reader()
    except Exception:  # noqa: BLE001 - private API
        LOGGER.debug("Could not read the coordinates of %r", layer, exc_info=True)
        return None
    if coordinates is None:
        return None
    try:
        return tuple(float(value) for value in coordinates)
    except (TypeError, ValueError):
        return None


def _master_locations(font: GSFont) -> dict[str, Location]:
    locations: dict[str, Location] = {}
    for master in font.masters:
        try:
            locations[str(master.id)] = tuple(float(value) for value in master.axes)
        except (TypeError, ValueError):
            continue
    return locations


def effective_location(
    coordinates: Location, master_location: Location, axis_count: int
) -> Location:
    """Where a brace layer actually lands in the design space.

    Reproduces ``builder/sources.py``: coordinates shorter than the axis count
    are filled up from the associated master, longer ones are truncated (which
    glyphsLib warns about and then does anyway).
    """
    if len(coordinates) < axis_count:
        return tuple(coordinates) + tuple(master_location[len(coordinates) : axis_count])
    return tuple(coordinates[:axis_count])


def conflicting_locations(font: GSFont) -> dict[Location, set[str]]:
    """Design-space locations that more than one master lays claim to.

    This is the state varLib rejects. A location used by many glyphs through
    one master is fine, and so is one master carrying many distinct locations -
    neither produces a duplicate designspace source.

    :returns: ``{effective location: {master id, ...}}``, only for locations
        with more than one master.
    """
    axis_count = len(font.axes)
    master_locations = _master_locations(font)

    by_location: dict[Location, set[str]] = {}
    for glyph in font.glyphs:
        for layer in glyph.layers:
            if not is_brace_layer(layer):
                continue
            coordinates = brace_coordinates(layer)
            if coordinates is None:
                continue
            master_id = str(layer.associatedMasterId)
            master_location = master_locations.get(master_id)
            if master_location is None:
                continue
            location = effective_location(coordinates, master_location, axis_count)
            by_location.setdefault(location, set()).add(master_id)
    return {
        location: masters for (location, masters) in by_location.items() if len(masters) > 1
    }


def align_brace_layers_to_variable_origin(
    font: GSFont, *, only_conflicts: bool = True
) -> BraceLayerAlignResult:
    """Reassign brace layers to the variable font origin master.

    By default only the layers actually in conflict are moved - those whose
    design-space location is claimed by more than one master, which is the
    state varLib rejects. A source where every location belongs to a single
    master is left completely alone, however those masters are spread out:
    measured across eight retail families, six had brace layers on several
    masters and *none* had a conflicting location, so moving them all would
    have rewritten up to a hundred layers per family for nothing.

    A conflicting layer whose coordinates are shorter than the axis count is
    **not** moved either, because the associated master supplies the rest of
    its location and reassigning it would shift the sparse master. Those are
    counted in ``unresolved_glyphs`` for a human to sort out.

    :param font: A glyphsLib GSFont, modified in place.
    :param only_conflicts: Pass False to reassign every brace layer whose
        coordinates cover all axes, which is what a Glyphs user does by hand
        when tidying a source.
    :returns: A :class:`BraceLayerAlignResult`.
    """
    target_master_id = variable_font_origin_master_id(font)
    result = BraceLayerAlignResult(target_master_id=target_master_id)
    axis_count = len(font.axes)
    master_locations = _master_locations(font)

    conflicts: dict[Location, set[str]] = {}
    if only_conflicts:
        conflicts = conflicting_locations(font)
        result.conflicting_locations = len(conflicts)
        if not conflicts:
            return result

    for glyph in font.glyphs:
        moved_here = 0
        unresolved_here = False
        for layer in glyph.layers:
            if not is_brace_layer(layer):
                continue
            coordinates = brace_coordinates(layer)
            if coordinates is None:
                continue
            master_id = str(layer.associatedMasterId)
            master_location = master_locations.get(master_id)
            if master_location is None:
                continue
            location = effective_location(coordinates, master_location, axis_count)
            if only_conflicts and location not in conflicts:
                continue
            if len(coordinates) < axis_count:
                # the associated master supplies part of this location, so
                # moving the layer would move the sparse master
                unresolved_here = True
                continue
            if master_id == target_master_id:
                continue
            layer.associatedMasterId = target_master_id
            moved_here += 1
        if moved_here:
            result.moved += moved_here
            result.glyphs.append(glyph.name)
        if unresolved_here:
            result.unresolved_glyphs.append(glyph.name)

    if result.moved:
        LOGGER.info(
            "Reassigned %d brace layer(s) on %d glyph(s) to master %s",
            result.moved,
            len(result.glyphs),
            target_master_id,
        )
    if result.unresolved_glyphs:
        LOGGER.warning(
            "%d glyph(s) share a brace layer location through different masters "
            "and cannot be realigned automatically: %s",
            len(result.unresolved_glyphs),
            result.unresolved_glyphs[:MAX_REPORTED_GLYPHS],
        )
    return result


__all__ = [
    "MAX_REPORTED_GLYPHS",
    "BraceLayerAlignResult",
    "Location",
    "align_brace_layers_to_variable_origin",
    "brace_coordinates",
    "conflicting_locations",
    "effective_location",
    "is_brace_layer",
    "variable_font_origin_master_id",
]
