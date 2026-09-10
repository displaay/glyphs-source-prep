"""Repairs a designspace needs before varLib or Instantiator will read it.

Three failure modes, all of them produced by glyphsLib from a source Glyphs.app
is perfectly happy with, and all of them surfacing far from their cause:

**Duplicate source locations.** The last line of defence behind
:mod:`glyphs_source_prep.brace_layers`: even after the brace layers have been
aligned, a designspace can still carry two sources at the same location - from
a source that was not built by glyphsLib, or from a case the alignment does not
reach. varLib requires each master to define a unique location and fails
otherwise. Where two sources collide, the full master wins over a sparse
(layer) source, because dropping the full one would remove a real master from
the design space. Between two of a kind the order is stable but arbitrary;
there is no signal in the file that says which the author meant. Upstream:
googlefonts/glyphsLib#925 fixed the layer-naming half of this in 6.2.4/6.2.5.
The half that remains - intermediate layers at the same location associated
with different masters - is #995.

**No master at the axis default.** glyphsLib emits a collapsed axis map when
only a sparse set of instances export and those instances use Axis Location
remapping. ``axis.default`` then maps to a design location no master sits at,
and ufo2ft's Instantiator fails because it has no base to interpolate from.

**An axis map that collapses an endpoint onto the default.** A map like wdth
``(50->50), (75->50)`` puts the axis minimum and the axis default at the same
design location. ``fontTools.varLib._add_avar`` asserts, because normalized
``-1`` has to map to ``-1`` and here it maps to ``0``.

Each repair comes in two forms: one that takes a
:class:`~fontTools.designspaceLib.DesignSpaceDocument` and edits it in place,
for a caller that built the document in memory and never writes it out, and one
that takes a path and rewrites the file, for a caller handing it to fontmake.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

from fontTools.designspaceLib import DesignSpaceDocument, SourceDescriptor

LOGGER = logging.getLogger(__name__)

#: Axis coordinates are floats that came through a file and a mapping, so two
#: that describe the same location can differ in the last bits.
EPSILON = 1e-6


@dataclass
class DefaultMasterResult:
    """What :func:`ensure_designspace_default_master` changed."""

    #: Axes whose minimum, maximum, default or map was rewritten.
    axes: list[str] = field(default_factory=list)
    #: True when a real master now sits at the default location. False means
    #: the document was left as it was - either it was already fine, or there
    #: is no master to rebase onto and the caller should let the build fail
    #: with its own message rather than have this invent a default.
    changed: bool = False

    def summary(self) -> str:
        """A single line for a processing log."""
        if not self.changed:
            return "designspace default already sits on a master"
        names = ", ".join(self.axes)
        return f"axis default rebased onto a real master on: {names}"


@dataclass
class AxisMapResult:
    """What :func:`repair_collapsing_axis_maps` changed."""

    #: Axes whose default was moved onto the collapsed endpoint.
    axes: list[str] = field(default_factory=list)

    @property
    def repaired(self) -> int:
        return len(self.axes)

    def summary(self) -> str:
        """A single line for a processing log."""
        if not self.axes:
            return "no axis map collapses an endpoint onto the default"
        names = ", ".join(self.axes)
        return (
            f"{len(self.axes)} axis map(s) mapped an endpoint and the default to "
            f"one design location; the default was moved onto the endpoint: {names}"
        )


@dataclass
class DeduplicateResult:
    """What :func:`deduplicate_designspace_sources` removed."""

    removed: int = 0
    locations: list[str] = field(default_factory=list)

    def summary(self) -> str:
        """A single line for a processing log."""
        if not self.removed:
            return "no duplicate designspace sources"
        locations = "; ".join(self.locations)
        return f"{self.removed} duplicate designspace source(s) dropped at: {locations}"


def _normalized_location(source: SourceDescriptor) -> tuple[tuple[str, float], ...]:
    """A hashable location, ignoring axes left at zero."""
    return tuple(
        sorted(
            (name, float(value))
            for (name, value) in source.location.items()
            if float(value) != 0.0
        )
    )


def _source_rank(source: SourceDescriptor) -> tuple[int, str, str]:
    """Sort key deciding which of two sources at one location is kept.

    A full master (no layer name) outranks a sparse layer source.
    """
    layer_name = source.layerName or ""
    return (0 if not layer_name else 1, layer_name, source.name or "")


def deduplicate_designspace_document(designspace: DesignSpaceDocument) -> DeduplicateResult:
    """Drop sources that share an axis location, in place.

    :param designspace: The document to prune. Not written to disk - see
        :func:`deduplicate_designspace_sources` for that.
    :returns: A :class:`DeduplicateResult`.
    """
    result = DeduplicateResult()
    keep: list[SourceDescriptor] = []
    seen: dict[tuple[tuple[str, float], ...], SourceDescriptor] = {}

    for source in designspace.sources:
        location = _normalized_location(source)
        if not location:
            # the default location; leave it alone rather than guess
            keep.append(source)
            continue
        existing = seen.get(location)
        if existing is None:
            seen[location] = source
            keep.append(source)
            continue
        result.removed += 1
        result.locations.append(str(dict(location)))
        if _source_rank(source) < _source_rank(existing):
            keep.remove(existing)
            seen[location] = source
            keep.append(source)

    if result.removed:
        designspace.sources = keep
        LOGGER.warning(
            "Dropped %d duplicate designspace source(s) at %s",
            result.removed,
            result.locations,
        )
    return result


def deduplicate_designspace_sources(
    designspace_path: str | os.PathLike[str],
) -> DeduplicateResult:
    """Drop duplicate sources from a designspace file and rewrite it.

    The file is only written when something was actually removed.

    :param designspace_path: Path to the .designspace file.
    :returns: A :class:`DeduplicateResult`.
    """
    path = os.fspath(designspace_path)
    designspace = DesignSpaceDocument.fromfile(path)
    result = deduplicate_designspace_document(designspace)
    if result.removed:
        designspace.write(path)
    return result


def master_sources(designspace: DesignSpaceDocument) -> list[SourceDescriptor]:
    """The full masters of a document.

    A source with a layer name is a sparse layer (brace/intermediate) source:
    its UFO layer holds only the glyphs that differ at that location, so it is
    a master for interpolation but not something a repair can read a complete
    design off.
    """
    return [source for source in designspace.sources if not source.layerName]


def _preferred_default_source(
    designspace: DesignSpaceDocument,
) -> SourceDescriptor | None:
    """The master the default should sit on.

    The one the source marked as carrying the family-wide info, if there is
    one; glyphsLib sets those flags on the master Glyphs itself treats as the
    reference. Otherwise the first, which is at least stable.
    """
    masters = master_sources(designspace)
    if not masters:
        return None
    for source in masters:
        if source.copyInfo or source.copyLib or source.copyGroups or source.copyFeatures:
            return source
    return masters[0]


def _mapping_has_design_output(mapping: dict[float, float], design_val: float) -> bool:
    return any(abs(output - design_val) < EPSILON for output in mapping.values())


def _user_loc_for_design(mapping: dict[float, float], design_val: float) -> float | None:
    for user_loc, output in mapping.items():
        if abs(output - design_val) < EPSILON:
            return user_loc
    return None


def ensure_default_master_document(
    designspace: DesignSpaceDocument,
) -> DefaultMasterResult:
    """Rebase each axis default onto a real master, in place.

    A brace/intermediate layer source cannot serve as the base either: its UFO
    layer only holds the glyphs that differ at that location, so interpolating
    from it would drop every glyph that does not.

    Nothing is changed unless a master can actually be found to rebase onto.
    Inventing a default where there is no master would only move the build's
    failure somewhere less informative.

    :param designspace: The document to repair. Not written to disk - see
        :func:`ensure_designspace_default_master` for that.
    :returns: A :class:`DefaultMasterResult`.
    """
    result = DefaultMasterResult()
    existing_default = designspace.findDefault()
    if existing_default is not None and not existing_default.layerName:
        return result

    preferred = _preferred_default_source(designspace)
    if preferred is None:
        return result

    default_design = {
        name: float(value)
        for (name, value) in preferred.getFullDesignLocation(designspace).items()
    }
    masters = master_sources(designspace)

    for axis in designspace.axes:
        design_val = float(default_design.get(axis.name, 0.0))
        mapping = {float(user): float(design) for (user, design) in (axis.map or [])}
        changed = False

        if mapping:
            # An axis with a map: every master's design location has to be
            # reachable through it, or that master is unaddressable.
            for source in masters:
                source_design = float((source.location or {}).get(axis.name, design_val))
                if _mapping_has_design_output(mapping, source_design):
                    continue
                if source_design not in mapping:
                    mapping[source_design] = source_design
                    changed = True

            user_default = _user_loc_for_design(mapping, design_val)
            if user_default is None:
                mapping[design_val] = design_val
                user_default = design_val
                changed = True

            new_map = sorted(mapping.items())
            new_minimum = min(mapping)
            new_maximum = max(mapping)
            if (
                list(axis.map or []) != new_map
                or float(axis.minimum) != new_minimum
                or float(axis.maximum) != new_maximum
                or float(axis.default) != float(user_default)
            ):
                axis.map = new_map
                axis.minimum = new_minimum
                axis.maximum = new_maximum
                axis.default = float(user_default)
                changed = True
        else:
            # No map: user and design coordinates are the same thing, so the
            # range only has to cover the masters.
            source_vals = [
                float(source.location[axis.name])
                for source in masters
                if axis.name in (source.location or {})
            ]
            if source_vals:
                new_minimum = min(source_vals)
                new_maximum = max(source_vals)
                if axis.minimum is None or float(axis.minimum) > new_minimum:
                    axis.minimum = new_minimum
                    changed = True
                if axis.maximum is None or float(axis.maximum) < new_maximum:
                    axis.maximum = new_maximum
                    changed = True
            if axis.default is None or abs(float(axis.default) - design_val) > EPSILON:
                axis.default = design_val
                changed = True

        if changed:
            result.axes.append(axis.name or axis.tag or "?")

    if designspace.findDefault() is None:
        # The rebase did not land on a master after all. Report nothing rather
        # than a change that did not fix what it was for.
        return DefaultMasterResult()

    result.changed = bool(result.axes)
    if result.changed:
        LOGGER.warning("Rebased the designspace default onto a master: %s", result.axes)
    return result


def ensure_designspace_default_master(
    designspace_path: str | os.PathLike[str],
) -> DefaultMasterResult:
    """Rebase the axis defaults in a designspace file onto a real master.

    The file is only written when something was actually changed.

    :param designspace_path: Path to the .designspace file.
    :returns: A :class:`DefaultMasterResult`.
    """
    path = os.fspath(designspace_path)
    designspace = DesignSpaceDocument.fromfile(path)
    result = ensure_default_master_document(designspace)
    if result.changed:
        designspace.write(path)
    return result


def _axis_map_pairs(axis) -> list[tuple[float, float]]:
    return [(float(user), float(design)) for (user, design) in (axis.map or [])]


def repair_collapsing_axis_maps_document(
    designspace: DesignSpaceDocument,
) -> AxisMapResult:
    """Move a default that shares a design location with an endpoint, in place.

    The design location is what varLib normalizes, and it needs the minimum at
    ``-1``, the default at ``0`` and the maximum at ``+1``. When the map sends
    two of those to the same design coordinate there is no such normalization,
    and the assertion inside ``_add_avar`` is the first anyone hears of it.

    The repair moves the *user* default onto the endpoint it collapsed against
    and drops the now-redundant remap entry, which leaves every master where it
    was: only the label on the default location changes.

    :param designspace: The document to repair. Not written to disk - see
        :func:`repair_collapsing_axis_maps` for that.
    :returns: An :class:`AxisMapResult`.
    """
    result = AxisMapResult()

    for axis in designspace.axes:
        if not axis.map:
            continue
        axis_min = float(axis.minimum)
        axis_default = float(axis.default)
        axis_max = float(axis.maximum)
        design_min = float(axis.map_forward(axis_min))
        design_default = float(axis.map_forward(axis_default))
        design_max = float(axis.map_forward(axis_max))
        mapping = _axis_map_pairs(axis)
        changed = False

        if (
            abs(design_min - design_default) < EPSILON
            and abs(axis_min - axis_default) > EPSILON
        ):
            mapping = [
                (user, design)
                for (user, design) in mapping
                if abs(user - axis_default) > EPSILON
            ]
            axis.default = axis_min
            changed = True
        elif (
            abs(design_max - design_default) < EPSILON
            and abs(axis_max - axis_default) > EPSILON
        ):
            mapping = [
                (user, design)
                for (user, design) in mapping
                if abs(user - axis_default) > EPSILON
            ]
            axis.default = axis_max
            changed = True

        if changed:
            # A stable, unique, ascending input map is what avar validation
            # expects, and the filtering above can leave neither.
            by_user: dict[float, float] = {}
            for (user, design) in sorted(mapping):
                by_user[user] = design
            axis.map = sorted(by_user.items())
            result.axes.append(axis.name or axis.tag or "?")

    if result.axes:
        LOGGER.warning("Repaired collapsing axis map(s) on: %s", result.axes)
    return result


def repair_collapsing_axis_maps(
    designspace_path: str | os.PathLike[str],
) -> AxisMapResult:
    """Repair collapsing axis maps in a designspace file and rewrite it.

    The file is only written when something was actually changed.

    :param designspace_path: Path to the .designspace file.
    :returns: An :class:`AxisMapResult`.
    """
    path = os.fspath(designspace_path)
    designspace = DesignSpaceDocument.fromfile(path)
    result = repair_collapsing_axis_maps_document(designspace)
    if result.axes:
        designspace.write(path)
    return result


__all__ = [
    "AxisMapResult",
    "EPSILON",
    "DeduplicateResult",
    "DefaultMasterResult",
    "deduplicate_designspace_document",
    "deduplicate_designspace_sources",
    "ensure_default_master_document",
    "ensure_designspace_default_master",
    "master_sources",
    "repair_collapsing_axis_maps",
    "repair_collapsing_axis_maps_document",
]
