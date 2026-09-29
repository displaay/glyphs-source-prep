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

**An axis map that stops short of a master.** glyphsLib builds a non-identity
map only from instances switched on for export. Wide or extended instances that
are off for export still carry the designer's user locations, but their masters
then fall outside the map and varLib's ``splitInterpolable`` drops them.

Each repair comes in two forms: one that takes a
:class:`~fontTools.designspaceLib.DesignSpaceDocument` and edits it in place,
for a caller that built the document in memory and never writes it out, and one
that takes a path and rewrites the file, for a caller handing it to fontmake.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any

from fontTools.designspaceLib import (
    AxisDescriptor,
    DesignSpaceDocument,
    SourceDescriptor,
)
from fontTools.varLib.models import piecewiseLinearMap

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


#: glyphsLib ``WIDTH_CLASS_TO_VALUE`` (builder/constants.py), duplicated so
#: this package does not depend on glyphsLib at runtime.
_WIDTH_CLASS_TO_USER: dict[int, float] = {
    1: 50.0,
    2: 62.5,
    3: 75.0,
    4: 87.5,
    5: 100.0,
    6: 112.5,
    7: 125.0,
    8: 150.0,
    9: 200.0,
}


@dataclass
class AxisRangeResult:
    """What :func:`extend_axis_maps_to_masters` changed."""

    #: Axes whose map, minimum or maximum was extended.
    axes: list[str] = field(default_factory=list)
    #: ``(axis_name, user, design)`` triples added to the map.
    points: list[tuple[str, float, float]] = field(default_factory=list)
    #: Axes where at least one added point used extrapolated user coordinates.
    extrapolated: list[str] = field(default_factory=list)
    #: Axes left alone because extending would break user→design monotonicity.
    skipped: list[str] = field(default_factory=list)

    def summary(self) -> str:
        """A single line for a processing log."""
        if not self.axes and not self.skipped:
            return "every axis map already covers its masters"
        parts: list[str] = []
        if self.axes:
            detail = []
            for axis_name, user, design in self.points:
                extrap = axis_name in self.extrapolated
                detail.append(f"{axis_name} {user:g}→{design:g}" + ("*" if extrap else ""))
            names = ", ".join(self.axes)
            text = f"extended axis map(s) on {names}"
            if detail:
                text += ": " + ", ".join(detail)
            if self.extrapolated:
                extrap_names = ", ".join(dict.fromkeys(self.extrapolated))
                text += f" (*extrapolated user location on {extrap_names})"
            parts.append(text)
        if self.skipped:
            parts.append(
                "left axis map(s) unchanged (would break monotonicity): "
                + ", ".join(self.skipped)
            )
        return "; ".join(parts)


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


#: What :func:`_axis_state` snapshots: the map, and the three bounds a
#: rebase moves. Named because two signatures spell it.
AxisState = tuple[list[tuple[float, float]], float, float, float]


def _axis_state(axis: AxisDescriptor) -> AxisState:
    """What :func:`ensure_default_master_document` may change about an axis."""
    return (list(axis.map or []), axis.minimum, axis.maximum, axis.default)


def _restore_axis(axis: AxisDescriptor, state: AxisState) -> None:
    """Put an axis back the way :func:`_axis_state` found it."""
    (axis.map, axis.minimum, axis.maximum, axis.default) = state


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
    # The rebase is only known to have worked once every axis has been walked,
    # and it edits the axes as it goes. Keep what they were so that a rebase
    # that turns out not to land can put them back: a caller holding the
    # document has no file to fall back on, and a half-rebased axis is worse
    # than the state that failed.
    before = [_axis_state(axis) for axis in designspace.axes]

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
        # The rebase did not land on a master after all. Put the axes back and
        # report nothing: a change that did not fix what it was for is not one
        # a caller should be told about, and certainly not one to leave behind
        # in a document the caller cannot reload from a file.
        #
        # Defensive: no document could be constructed that reaches here, since
        # the loop rebases each axis onto the preferred master's own design
        # location and findDefault() then matches it. It is kept because the
        # cost is a list of four-tuples and the alternative is a half-rebased
        # design space that the result object says nothing about.
        for axis, state in zip(designspace.axes, before, strict=True):
            _restore_axis(axis, state)
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


def _axis_map_pairs(axis: AxisDescriptor) -> list[tuple[float, float]]:
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


def _axis_map_is_identity(axis: AxisDescriptor) -> bool:
    if not axis.map:
        return True
    return all(abs(float(user) - float(design)) < EPSILON for user, design in axis.map)


def _mapping_design_outputs(mapping: dict[float, float]) -> set[float]:
    return {float(design) for design in mapping.values()}


def _map_is_monotonic(pairs: list[tuple[float, float]]) -> bool:
    if len(pairs) < 2:
        return True
    sorted_pairs = sorted((float(user), float(design)) for user, design in pairs)
    for index in range(1, len(sorted_pairs)):
        if sorted_pairs[index][1] + EPSILON < sorted_pairs[index - 1][1]:
            return False
    return True


def _custom_parameters_get(parameters: Any, name: str) -> Any:
    if parameters is None:
        return None
    if isinstance(parameters, dict):
        return parameters.get(name)
    try:
        return parameters[name]
    except (KeyError, TypeError):
        return None


def _instance_is_exporting(instance: Any) -> bool:
    return bool(getattr(instance, "exports", True)) and bool(
        getattr(instance, "active", True)
    )


def _glyphs_axis_tag(axis: Any) -> str | None:
    return getattr(axis, "axisTag", None) or getattr(axis, "tag", None)


def _glyphs_axis_defs(font: Any) -> list[tuple[str, str, int, str | None, str | None]]:
    """Return ``(tag, name, index, user_loc_key, user_loc_param)`` per font axis."""
    axes_param = _custom_parameters_get(getattr(font, "customParameters", None), "Axes")
    factory_index = -1

    def next_axis(
        tag: str, name: str, user_loc_key: str | None, user_loc_param: str | None
    ) -> tuple[str, str, int, str | None, str | None]:
        nonlocal factory_index
        factory_index += 1
        return (tag, name, factory_index, user_loc_key, user_loc_param)

    if axes_param:
        result: list[tuple[str, str, int, str | None, str | None]] = []
        for entry in axes_param:
            tag = entry.get("Tag") or "XXXX"
            name = entry["Name"]
            if tag == "wght":
                user_key, user_param = "weight", "weightClass"
            elif tag == "wdth":
                user_key, user_param = "width", "widthClass"
            else:
                user_key, user_param = None, None
            result.append(next_axis(tag, name, user_key, user_param))
        return result

    font_axes = getattr(font, "axes", None) or []
    if font_axes:
        result = []
        for axis in font_axes:
            tag = _glyphs_axis_tag(axis) or "XXXX"
            name = getattr(axis, "name", "Custom")
            if tag == "wght":
                user_key, user_param = "weight", "weightClass"
            elif tag == "wdth":
                user_key, user_param = "width", "widthClass"
            else:
                user_key, user_param = None, None
            result.append(next_axis(tag, name, user_key, user_param))
        return result

    return [
        next_axis("wght", "Weight", "weight", "weightClass"),
        next_axis("wdth", "Width", "width", "widthClass"),
    ]


def _glyphs_design_location(
    master_or_instance: Any,
    axis_index: int,
    axis_tag: str,
    axis_name: str,
) -> float:
    if hasattr(master_or_instance, "_get_axis_value"):
        return float(master_or_instance._get_axis_value(axis_index))
    axes_values = getattr(master_or_instance, "axes", None)
    if axes_values is not None and axis_index < len(axes_values):
        return float(axes_values[axis_index])
    axes_values = getattr(master_or_instance, "axesValues", None)
    if axes_values is not None and axis_index < len(axes_values):
        return float(axes_values[axis_index])
    if axis_tag == "wght":
        return float(getattr(master_or_instance, "weightValue", 400.0))
    if axis_tag == "wdth":
        return float(getattr(master_or_instance, "widthValue", 100.0))
    raise ValueError(f"cannot read design location for axis {axis_name}")


def _user_loc_from_axis_location_cp(
    master_or_instance: Any, axis_name: str
) -> float | None:
    loc_param = _custom_parameters_get(
        getattr(master_or_instance, "customParameters", None), "Axis Location"
    )
    if not loc_param:
        return None
    try:
        for location in loc_param:
            if location.get("Axis") == axis_name:
                return float(location["Location"])
    except (TypeError, KeyError, ValueError):
        return None
    return None


def _user_loc_from_width_weight_class(
    master_or_instance: Any, axis_tag: str, user_loc_param: str | None
) -> float | None:
    if user_loc_param is None:
        return None
    class_ = _custom_parameters_get(
        getattr(master_or_instance, "customParameters", None), user_loc_param
    )
    if class_ is None:
        return None
    if axis_tag == "wght":
        return float(class_)
    if axis_tag == "wdth":
        return _WIDTH_CLASS_TO_USER.get(int(class_))
    return None


def _user_loc_from_instance_key(
    master_or_instance: Any, axis_tag: str, user_loc_key: str | None
) -> float | None:
    if user_loc_key is None or not hasattr(master_or_instance, user_loc_key):
        return None
    raw = getattr(master_or_instance, user_loc_key)
    if raw is None:
        return None
    if axis_tag == "wght" and isinstance(raw, (int, float)):
        return float(raw)
    if axis_tag == "wdth" and isinstance(raw, int):
        return _WIDTH_CLASS_TO_USER.get(raw)
    return None


def _glyphs_user_location(
    master_or_instance: Any,
    axis_tag: str,
    axis_name: str,
    axis_index: int,
    user_loc_key: str | None,
    user_loc_param: str | None,
) -> float:
    if axis_tag == "wght":
        user_loc = 400.0
    else:
        user_loc = _glyphs_design_location(
            master_or_instance, axis_index, axis_tag, axis_name
        )

    from_key = _user_loc_from_instance_key(master_or_instance, axis_tag, user_loc_key)
    if from_key is not None:
        user_loc = from_key

    from_class = _user_loc_from_width_weight_class(
        master_or_instance, axis_tag, user_loc_param
    )
    if from_class is not None:
        user_loc = from_class

    from_cp = _user_loc_from_axis_location_cp(master_or_instance, axis_name)
    if from_cp is not None:
        user_loc = from_cp

    return user_loc


def _match_font_axis(
    axis: AxisDescriptor, font_axis_defs: list[tuple[str, str, int, str | None, str | None]]
) -> tuple[str, str, int, str | None, str | None] | None:
    for tag, name, index, user_key, user_param in font_axis_defs:
        if axis.tag and tag == axis.tag:
            return (tag, name, index, user_key, user_param)
        if axis.name and name == axis.name:
            return (tag, name, index, user_key, user_param)
    return None


def _inactive_instance_user_for_design(
    font: Any,
    axis: AxisDescriptor,
    axis_def: tuple[str, str, int, str | None, str | None],
    design_val: float,
) -> float | None:
    tag, name, index, user_key, user_param = axis_def
    for instance in getattr(font, "instances", []) or []:
        if _instance_is_exporting(instance):
            continue
        try:
            instance_design = _glyphs_design_location(instance, index, tag, name)
        except ValueError:
            continue
        if abs(instance_design - design_val) > EPSILON:
            continue
        return _glyphs_user_location(instance, tag, name, index, user_key, user_param)
    return None


def _extrapolate_user_for_design(mapping: dict[float, float], design_val: float) -> float:
    reverse = {float(design): float(user) for user, design in sorted(mapping.items())}
    return float(piecewiseLinearMap(float(design_val), reverse))


def extend_axis_maps_document(
    designspace: DesignSpaceDocument,
    font: Any | None = None,
) -> AxisRangeResult:
    """Extend each non-identity axis map so every master design location is covered.

    Inactive instances supply user locations before extrapolation is used.

    :param designspace: The document to repair. Not written to disk - see
        :func:`extend_axis_maps_to_masters` for that.
    :param font: Optional Glyphs source the designspace was built from.
    :returns: An :class:`AxisRangeResult`.
    """
    result = AxisRangeResult()
    masters = master_sources(designspace)
    font_axis_defs = _glyphs_axis_defs(font) if font is not None else []

    for axis in designspace.axes:
        if _axis_map_is_identity(axis):
            continue
        axis_name = axis.name or axis.tag or "?"
        mapping = {float(user): float(design) for user, design in (axis.map or [])}
        design_outputs = _mapping_design_outputs(mapping)
        axis_def = _match_font_axis(axis, font_axis_defs) if font is not None else None

        needed_designs: list[float] = []
        for source in masters:
            full_design = source.getFullDesignLocation(designspace)
            design_val = float(full_design[axis.name])
            if any(abs(design_val - existing) < EPSILON for existing in design_outputs):
                continue
            needed_designs.append(design_val)

        if not needed_designs:
            continue

        proposed = dict(mapping)
        additions: list[tuple[float, float, bool]] = []
        skip_axis = False
        for design_val in sorted(needed_designs):
            user_loc: float | None = None
            extrapolated = False
            if font is not None and axis_def is not None:
                user_loc = _inactive_instance_user_for_design(
                    font, axis, axis_def, design_val
                )
            if user_loc is None:
                user_loc = _extrapolate_user_for_design(mapping, design_val)
                extrapolated = True
            if user_loc in proposed and abs(proposed[user_loc] - design_val) > EPSILON:
                skip_axis = True
                break
            if user_loc not in proposed:
                proposed[user_loc] = design_val
                additions.append((user_loc, design_val, extrapolated))

        if skip_axis:
            result.skipped.append(axis_name)
            LOGGER.warning(
                "Skipped extending axis map on %s: user location already maps elsewhere",
                axis_name,
            )
            continue

        if not _map_is_monotonic(list(proposed.items())):
            result.skipped.append(axis_name)
            LOGGER.warning(
                "Skipped extending axis map on %s: would break monotonicity",
                axis_name,
            )
            continue

        if not additions:
            continue

        for user_loc, design_val, extrapolated in additions:
            result.points.append((axis_name, user_loc, design_val))
            if extrapolated and axis_name not in result.extrapolated:
                result.extrapolated.append(axis_name)

        new_map = sorted(proposed.items())
        map_minimum = min(proposed)
        map_maximum = max(proposed)
        new_minimum = map_minimum
        new_maximum = map_maximum
        if axis.minimum is not None:
            new_minimum = min(float(axis.minimum), map_minimum)
        if axis.maximum is not None:
            new_maximum = max(float(axis.maximum), map_maximum)
        axis.map = new_map
        axis.minimum = new_minimum
        axis.maximum = new_maximum
        result.axes.append(axis_name)

    if result.axes:
        LOGGER.warning("Extended axis map(s) on: %s", result.axes)
    return result


def extend_axis_maps_to_masters(
    designspace_path: str | os.PathLike[str],
    font: Any | None = None,
) -> AxisRangeResult:
    """Extend axis maps in a designspace file so every master is inside the map.

    The file is only written when an axis actually changed.

    :param designspace_path: Path to the .designspace file.
    :param font: Optional Glyphs source for inactive-instance user locations.
    :returns: An :class:`AxisRangeResult`.
    """
    path = os.fspath(designspace_path)
    designspace = DesignSpaceDocument.fromfile(path)
    result = extend_axis_maps_document(designspace, font)
    if result.axes:
        designspace.write(path)
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
    "AxisRangeResult",
    "EPSILON",
    "DeduplicateResult",
    "DefaultMasterResult",
    "deduplicate_designspace_document",
    "deduplicate_designspace_sources",
    "ensure_default_master_document",
    "ensure_designspace_default_master",
    "extend_axis_maps_document",
    "extend_axis_maps_to_masters",
    "master_sources",
    "repair_collapsing_axis_maps",
    "repair_collapsing_axis_maps_document",
]
