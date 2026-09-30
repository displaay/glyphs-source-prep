"""Repairs a designspace needs before varLib or Instantiator will read it.

Failure modes produced by glyphsLib from a source Glyphs.app is perfectly
happy with, all of them surfacing far from their cause:

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

**Masters past the end of an axis.** glyphsLib builds a non-identity axis map
from the instances that are switched on for export, and then
``fontTools.designspaceLib.split.splitInterpolable`` drops every master whose
user location falls outside that map. Switching the wide instances off leaves
the wide masters in the file and takes them out of the interpolation, so a
"Standard" instance comes out as wide as the condensed master.

**An axis map that decreases.** glyphsLib merges per-instance user locations
last-write-wins, so two conflicting instances can emit a crossed map.
Instantiator requires the design locations of minimum, default and maximum to
be non-decreasing and raises when they are not.
:func:`repair_inverted_axis_maps` keeps the longest non-decreasing run, and
:func:`extend_axis_maps_to_masters` runs that repair before it adds points.

**A width axis relabelled in width classes.** Not a failure but a convention:
without an ``Axis Location`` glyphsLib labels each width with its OS/2 width
class, so a design drawn at 130 ships as ``wdth`` 125.
:func:`reset_axis_maps_to_design` makes the axis 1:1 with the design
coordinates, which also leaves the two repairs above nothing to find on it.

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


# OS/2 width class -> user location (percent of normal). Copied from
# glyphsLib.builder.constants.WIDTH_CLASS_TO_VALUE so this package does not
# import glyphsLib. A width *class* of 7 is user 125, not user 7.
_WIDTH_CLASS_TO_USER: dict[int, float] = {
    1: 50,
    2: 62.5,
    3: 75,
    4: 87.5,
    5: 100,
    6: 112.5,
    7: 125,
    8: 150,
    9: 200,
}

# Glyphs UI names -> OS/2 class. Keys match glyphsLib's tables, including the
# inconsistent spacing ("SemiCondensed" vs "Semi Expanded"): lookup tries the
# string as written, then with the spaces removed.
_WIDTH_NAME_TO_CLASS: dict[str, int] = {
    "Ultra Condensed": 1,
    "Extra Condensed": 2,
    "Condensed": 3,
    "SemiCondensed": 4,
    "Medium (normal)": 5,
    "Semi Expanded": 6,
    "Expanded": 7,
    "Extra Expanded": 8,
    "Ultra Expanded": 9,
}
_WEIGHT_NAME_TO_CLASS: dict[str, int] = {
    "Thin": 100,
    "ExtraLight": 200,
    "UltraLight": 200,
    "Light": 300,
    "Normal": 400,
    "Regular": 400,
    "Medium": 500,
    "DemiBold": 600,
    "SemiBold": 600,
    "Bold": 700,
    "UltraBold": 800,
    "ExtraBold": 800,
    "Black": 900,
    "Heavy": 900,
}

#: glyphsLib's ``InstanceType.VARIABLE``. A variable-font setting is not a
#: static instance and does not contribute a user location.
_VARIABLE_INSTANCE_TYPE = 1


@dataclass
class AxisRangeResult:
    """What :func:`extend_axis_maps_to_masters` changed."""

    #: Axes whose map or range was extended to reach a master.
    axes: list[str] = field(default_factory=list)
    #: Map points added, as ``(axis name, user location, design location)``.
    points: list[tuple[str, float, float]] = field(default_factory=list)
    #: Axes where no instance supplied a missing point, so it was extrapolated.
    extrapolated: list[str] = field(default_factory=list)
    #: The subset of :attr:`points` that was extrapolated rather than read from
    #: an inactive instance. The other points on the same axis are the
    #: designer's.
    extrapolated_points: list[tuple[str, float, float]] = field(default_factory=list)
    #: Instances that could not contribute a point, and why.
    skipped: list[str] = field(default_factory=list)

    @property
    def repaired(self) -> int:
        return len(self.axes)

    def summary(self) -> str:
        """A single line for a processing log."""
        if not self.axes:
            return "every master is already inside the axis range"
        parts: list[str] = []
        for name in self.axes:
            added = ", ".join(
                f"{point[1]:g}->{point[2]:g}"
                + (" (extrapolated)" if point in self.extrapolated_points else "")
                for point in self.points
                if point[0] == name
            )
            parts.append(f"{name} {added}")
        return (
            "extended axis map to reach masters outside the active-instance range: "
            + "; ".join(parts)
        )


def _custom_parameter(owner: Any, name: str) -> Any:
    """Read one Glyphs custom parameter, or None when it is absent.

    ``GSCustomParameter`` lookups return None for a missing name; a stand-in
    object in a test may raise ``KeyError`` instead. Both mean "not set".
    """
    params = getattr(owner, "customParameters", None)
    if params is None:
        return None
    try:
        return params[name]
    except (KeyError, TypeError):
        return None


def _axis_label(axis: AxisDescriptor) -> str:
    return axis.name or axis.tag or "?"


def _map_decreases(pairs: list[tuple[float, float]]) -> bool:
    ordered = sorted(pairs)
    return any(ordered[i][1] < ordered[i - 1][1] - EPSILON for i in range(1, len(ordered)))


def _is_identity(pairs: list[tuple[float, float]]) -> bool:
    return all(abs(user - design) < EPSILON for (user, design) in pairs)


def _covered_interval(
    axis: AxisDescriptor, pairs: list[tuple[float, float]]
) -> tuple[float, float, float, float]:
    """User and design intervals the axis can already address.

    A mapped axis covers the designs its map outputs, including the designs
    ``minimum`` and ``maximum`` map to. An unmapped axis is an identity, so
    the two intervals are the same.
    """
    if not pairs:
        low = float(axis.minimum)
        high = float(axis.maximum)
        return low, high, low, high
    users = [user for (user, _) in pairs]
    designs = [design for (_, design) in pairs]
    designs.append(float(axis.map_forward(axis.minimum)))
    designs.append(float(axis.map_forward(axis.maximum)))
    user_lo = min(float(axis.minimum), min(users))
    user_hi = max(float(axis.maximum), max(users))
    return user_lo, user_hi, min(designs), max(designs)


def _master_designs(designspace: DesignSpaceDocument, axis_name: str) -> list[float]:
    values: list[float] = []
    for source in master_sources(designspace):
        location = source.location or {}
        if axis_name not in location:
            continue
        values.append(float(location[axis_name]))
    return values


def _outside(value: float, low: float, high: float) -> bool:
    return value < low - EPSILON or value > high + EPSILON


def _font_axis_index(font: Any, axis: AxisDescriptor) -> int | None:
    """Index of ``axis`` in the source's axis list, which ``instance.axes`` follows."""
    font_axes = list(getattr(font, "axes", None) or [])
    by_tag: int | None = None
    by_name: int | None = None
    for index, font_axis in enumerate(font_axes):
        tag = getattr(font_axis, "axisTag", None) or getattr(font_axis, "tag", None)
        name = getattr(font_axis, "name", None)
        if by_tag is None and axis.tag and tag == axis.tag:
            by_tag = index
        if by_name is None and axis.name and name == axis.name:
            by_name = index
    if by_tag is not None:
        return by_tag
    return by_name


def _mapping_pins_axis(font: Any, axis: AxisDescriptor) -> bool:
    """True when the source sets this axis's map explicitly.

    An ``Axis Mappings`` custom parameter is the designer's map. glyphsLib
    uses it as-is and does not derive one from instances, so extending it
    here would overwrite a deliberate choice.
    """
    mappings = _custom_parameter(font, "Axis Mappings")
    if mappings is None:
        return False
    try:
        keys = {str(key) for key in mappings}
    except TypeError:
        return False
    return bool((axis.tag and axis.tag in keys) or (axis.name and axis.name in keys))


def _masters_declare_axis_locations(font: Any) -> bool:
    """glyphsLib's ``font_uses_axis_locations``.

    When every master carries an Axis Location, glyphsLib reads instance user
    locations only from that parameter and ignores the width and weight class.
    """
    masters = list(getattr(font, "masters", None) or [])
    axes = list(getattr(font, "axes", None) or [])
    if not masters or not axes:
        return False
    return all(_custom_parameter(master, "Axis Location") for master in masters)


def _is_inactive_instance(instance: Any) -> bool:
    """glyphsLib's ``is_instance_active`` inverted.

    Glyphs treats either ``exports=0`` or ``active=0`` as switched off.
    """
    exports = getattr(instance, "exports", True)
    active = getattr(instance, "active", True)
    return not bool(exports) or not bool(active)


def _is_variable_instance(instance: Any) -> bool:
    type_value = getattr(instance, "type", 0)
    if isinstance(type_value, str):
        return type_value.strip().lower() == "variable"
    try:
        return int(type_value) == _VARIABLE_INSTANCE_TYPE
    except (TypeError, ValueError):
        return False


def _instance_design(instance: Any, index: int) -> float | None:
    axes = getattr(instance, "axes", None)
    if axes is None:
        return None
    try:
        values = list(axes)
    except TypeError:
        return None
    if index >= len(values) or values[index] is None:
        return None
    try:
        return float(values[index])
    except (TypeError, ValueError):
        return None


def _lookup_class_name(table: dict[str, int], text: str) -> int | None:
    if text in table:
        return table[text]
    compact = "".join(text.split())
    for key, value in table.items():
        if "".join(key.split()) == compact:
            return value
    return None


def _class_to_user(tag: str, raw: Any) -> float | None:
    """Turn a Glyphs width/weight class into the user location glyphsLib uses.

    A bare number is the OS/2 class. Width class 7 is the user location 125;
    weight class 700 is already the user location.
    """
    if isinstance(raw, bool) or raw is None:
        return None
    class_value: int | None
    if isinstance(raw, (int, float)):
        class_value = int(raw)
    elif isinstance(raw, str):
        text = raw.strip()
        try:
            class_value = int(float(text))
        except ValueError:
            table = _WEIGHT_NAME_TO_CLASS if tag == "wght" else _WIDTH_NAME_TO_CLASS
            class_value = _lookup_class_name(table, text)
    else:
        return None
    if class_value is None:
        return None
    if tag == "wght":
        return float(class_value)
    mapped = _WIDTH_CLASS_TO_USER.get(class_value)
    if mapped is None:
        return None
    return float(mapped)


def _axis_location_user(owner: Any, axis_name: str) -> float | None:
    locations = _custom_parameter(owner, "Axis Location")
    if not locations:
        return None
    try:
        entries = list(locations)
    except TypeError:
        return None
    for entry in entries:
        try:
            if entry.get("Axis") != axis_name:
                continue
            return float(entry["Location"])
        except (AttributeError, KeyError, TypeError, ValueError):
            continue
    return None


def _instance_user_location(
    instance: Any,
    axis: AxisDescriptor,
    design: float,
    *,
    use_class: bool,
) -> float | None:
    """The user location glyphsLib would record for this instance on ``axis``.

    ``Axis Location`` wins over the class. The class is only consulted when
    the masters do not all declare Axis Location (glyphsLib's ``cp_only``
    path). The weight axis has no design-location fallback: inventing one
    would place the point at the stem width instead of the usWeightClass.
    """
    located = _axis_location_user(instance, axis.name or "")
    if located is not None:
        return located
    if not use_class:
        return None
    tag = axis.tag or ""
    if tag == "wght":
        raw = _custom_parameter(instance, "weightClass")
        if raw is None:
            raw = getattr(instance, "weight", None)
        if raw is None:
            return None
        return _class_to_user("wght", raw)
    if tag == "wdth":
        raw = _custom_parameter(instance, "widthClass")
        if raw is None:
            raw = getattr(instance, "width", None)
        if raw is None:
            return design
        user = _class_to_user("wdth", raw)
        return design if user is None else user
    return design


def _identity_endpoints(axis: AxisDescriptor) -> list[tuple[float, float]]:
    low = float(axis.minimum)
    high = float(axis.maximum)
    if abs(high - low) < EPSILON:
        return [(low, low)]
    return [(low, low), (high, high)]


def _extrapolated_user(pairs: list[tuple[float, float]], target: float, *, high: bool) -> float:
    """User coordinate for ``target``, continuing the end segment.

    A flat end segment has no slope to continue, so the user coordinate moves
    one-for-one with the design coordinate.
    """
    ordered = sorted(pairs)
    if high:
        end_user, end_design = ordered[-1]
        prev_user, prev_design = ordered[-2] if len(ordered) >= 2 else (end_user, end_design)
    else:
        end_user, end_design = ordered[0]
        prev_user, prev_design = ordered[1] if len(ordered) >= 2 else (end_user, end_design)
    span = end_user - prev_user
    rise = end_design - prev_design
    slope = rise / span if abs(span) > EPSILON else 0.0
    if abs(slope) < EPSILON:
        slope = 1.0
    return end_user + (target - end_design) / slope


#: User ranges the OpenType axis registry allows. An extrapolated user
#: coordinate is invented, so it must not leave them: a ``wght`` of 1080 is
#: not a weight any consumer accepts. ``None`` is an open end.
_REGISTERED_USER_RANGE: dict[str, tuple[float | None, float | None]] = {
    "wght": (1.0, 1000.0),
    "ital": (0.0, 1.0),
    "slnt": (-90.0, 90.0),
}


def _clamped_to_registered_range(tag: str | None, user: float) -> float:
    low, high = _REGISTERED_USER_RANGE.get(tag or "", (None, None))
    if low is not None and user < low:
        return low
    if high is not None and user > high:
        return high
    return user


def _axis_snapshot(
    designspace: DesignSpaceDocument,
) -> tuple[tuple[object, ...], ...]:
    """Axis maps and bounds, so a path repair can see a change it must write."""

    def _bound(value: float | None) -> float | None:
        return None if value is None else float(value)

    return tuple(
        (
            axis.name,
            tuple((float(user), float(design)) for user, design in (axis.map or [])),
            _bound(axis.minimum),
            _bound(axis.maximum),
            _bound(axis.default),
        )
        for axis in designspace.axes
    )


def _apply_map(
    axis: AxisDescriptor,
    pairs: list[tuple[float, float]],
    *,
    had_map: bool,
) -> None:
    ordered = sorted(pairs)
    # A declared bound can lie beyond the explicit map points (designspaceLib
    # extrapolates the map there). Extending must never shrink the axis.
    axis.minimum = min([user for (user, _) in ordered] + [float(axis.minimum)])
    axis.maximum = max([user for (user, _) in ordered] + [float(axis.maximum)])
    if not had_map and _is_identity(ordered):
        # The axis was an identity and still is: minimum/maximum are the map.
        axis.map = []
        return
    axis.map = ordered


def _extend_one_axis(
    designspace: DesignSpaceDocument,
    axis: AxisDescriptor,
    font: Any,
    *,
    use_class: bool,
    result: AxisRangeResult,
) -> None:
    label = _axis_label(axis)
    had_map = bool(axis.map)
    pairs = _axis_map_pairs(axis)
    if pairs and _map_decreases(pairs):
        # repair_inverted_axis_maps_document has already run. A map that is
        # still decreasing has no slope this function can extend.
        return
    if font is not None and _mapping_pins_axis(font, axis):
        return

    user_lo, user_hi, design_lo, design_hi = _covered_interval(axis, pairs)
    missing = [
        design
        for design in _master_designs(designspace, axis.name or "")
        if _outside(design, design_lo, design_hi)
    ]
    if not missing:
        return

    base = list(pairs) if pairs else _identity_endpoints(axis)
    accepted: list[tuple[float, float]] = []
    if font is not None:
        index = _font_axis_index(font, axis)
        instances = list(getattr(font, "instances", None) or [])
        for instance in instances:
            if not _is_inactive_instance(instance) or _is_variable_instance(instance):
                continue
            if index is None:
                continue
            design = _instance_design(instance, index)
            if design is None or not _outside(design, design_lo, design_hi):
                continue
            user = _instance_user_location(instance, axis, design, use_class=use_class)
            name = str(getattr(instance, "name", None) or "?")
            if user is None:
                result.skipped.append(f"{label}: {name} has no user location")
                continue
            if not _outside(user, user_lo, user_hi):
                result.skipped.append(
                    f"{label}: {name} user {user:g} sits inside the axis range"
                )
                continue
            if any(abs(user - existing) < EPSILON for (existing, _) in base + accepted):
                result.skipped.append(
                    f"{label}: {name} user {user:g} is already on the map"
                )
                continue
            trial = base + accepted + [(user, design)]
            if _map_decreases(trial):
                result.skipped.append(
                    f"{label}: {name} user {user:g}->{design:g} would make the map decrease"
                )
                continue
            accepted.append((user, design))

    merged = base + accepted
    invented: list[tuple[float, float]] = []
    covered_designs = [design for (_, design) in merged]
    ends: list[tuple[float, bool]] = []
    still_low = [design for design in missing if design < min(covered_designs) - EPSILON]
    still_high = [design for design in missing if design > max(covered_designs) + EPSILON]
    if still_low:
        ends.append((min(still_low), False))
    if still_high:
        ends.append((max(still_high), True))
    for target, high in ends:
        user = _clamped_to_registered_range(
            axis.tag, _extrapolated_user(merged, target, high=high)
        )
        if any(abs(user - existing) < EPSILON for (existing, _) in merged):
            # Clamped onto a point the map already has: there is no user
            # coordinate left inside the registered range for this master.
            result.skipped.append(
                f"{label}: extrapolated {user:g}->{target:g} leaves the valid "
                f"{axis.tag} range"
            )
            continue
        trial = merged + [(user, target)]
        if _map_decreases(trial):
            result.skipped.append(
                f"{label}: extrapolated {user:g}->{target:g} would make the map decrease"
            )
            continue
        merged = trial
        accepted.append((user, target))
        invented.append((user, target))

    if not accepted:
        return
    _apply_map(axis, merged, had_map=had_map)
    result.axes.append(label)
    for user, design in accepted:
        result.points.append((label, user, design))
    if invented:
        result.extrapolated.append(label)
        result.extrapolated_points.extend((label, user, design) for user, design in invented)


def extend_axis_maps_to_masters_document(
    designspace: DesignSpaceDocument,
    font: Any = None,
) -> AxisRangeResult:
    """Extend each axis so every full master falls inside it, in place.

    glyphsLib's axis map stops at the last instance that is switched on for
    export. ``splitInterpolable`` then drops a master whose design location
    maps outside that range, and every instance collapses onto the masters
    that remain. Inactive instances still carry the designer's user location
    (a width class of 7 is user 125, not an invented coordinate), so those
    points are added first. A master that no instance accounts for is reached
    by continuing the end segment of the map.

    A decreasing map is repaired first, by
    :func:`repair_inverted_axis_maps_document`, so a crossed map is reduced to
    its longest non-decreasing run before any point is added. This result
    names only the points added here; the inverted repair's own result names
    the axes it changed.

    The axis default and the instance locations are left alone: instances are
    already in design space, and they interpolate correctly once the far
    master is inside the axis.

    :param designspace: The document to repair. Not written to disk - see
        :func:`extend_axis_maps_to_masters` for that.
    :param font: The Glyphs source the designspace was built from, duck-typed
        (masters, instances, axes, custom parameters). Without it the missing
        points are extrapolated, because the user locations live only in the
        source.
    :returns: An :class:`AxisRangeResult`.
    """
    repair_inverted_axis_maps_document(designspace)
    result = AxisRangeResult()
    use_class = font is not None and not _masters_declare_axis_locations(font)
    for axis in designspace.axes:
        _extend_one_axis(designspace, axis, font, use_class=use_class, result=result)
    if result.axes:
        LOGGER.warning(
            "Extended axis map(s) to reach masters outside the active-instance range: %s",
            result.axes,
        )
    return result


def extend_axis_maps_to_masters(
    designspace_path: str | os.PathLike[str],
    font: Any = None,
) -> AxisRangeResult:
    """Extend axis maps in a designspace file so every master is inside them.

    The file is only written when something was actually changed.

    :param designspace_path: Path to the .designspace file.
    :param font: See :func:`extend_axis_maps_to_masters_document`.
    :returns: An :class:`AxisRangeResult`.
    """
    path = os.fspath(designspace_path)
    designspace = DesignSpaceDocument.fromfile(path)
    before = _axis_snapshot(designspace)
    result = extend_axis_maps_to_masters_document(designspace, font)
    # The inverted-map repair can change the file without adding a point, so
    # the write follows the document, not just ``result.axes``.
    if _axis_snapshot(designspace) != before:
        designspace.write(path)
    return result


@dataclass
class AxisResetResult:
    """What :func:`reset_axis_maps_to_design` changed."""

    #: Axes whose map was dropped or whose range was rewritten.
    axes: list[str] = field(default_factory=list)
    #: The range each of them has now, as ``(axis name, minimum, default,
    #: maximum)`` - design coordinates, which are the user coordinates too.
    ranges: list[tuple[str, float, float, float]] = field(default_factory=list)

    @property
    def repaired(self) -> int:
        return len(self.axes)

    def summary(self) -> str:
        """A single line for a processing log."""
        if not self.axes:
            return "no axis map to reset to design coordinates"
        parts = "; ".join(
            f"{name} {minimum:g}..{maximum:g} (default {default:g})"
            for (name, minimum, default, maximum) in self.ranges
        )
        return f"axis map reset to the design coordinates on: {parts}"


def reset_axis_maps_to_design_document(
    designspace: DesignSpaceDocument,
    tags: tuple[str, ...] = ("wdth",),
) -> AxisResetResult:
    """Make the named axes 1:1 with their design coordinates, in place.

    A Displaay source sets the width axis in the numbers the designer drew at
    - 75, 89, 100, 115, 130 - and those are the numbers the variable font has
    to carry. glyphsLib does not use them: without an ``Axis Location`` it
    derives the user location from the instance's OS/2 width class, which has
    nine steps, so 89 becomes 87.5 and 130 becomes 125. The same source then
    gets a different axis from every tool that does or does not keep that map.

    The repair drops the map and spans the axis over the design locations of
    the full masters and the instances. Nothing moves: sources and instances
    are already in design space, only the label on each location changes.
    The default keeps its design location. No width class and no
    ``Axis Location`` is consulted - a source can get those wrong (a Standard
    and a Wide instance of Reckless Italic both claim 50), and only the
    coordinate the outlines were drawn at is evidence of the width.

    This also takes care of what :func:`repair_inverted_axis_maps_document`
    and :func:`extend_axis_maps_to_masters_document` would find on the same
    axis, so run it before them; they still apply to every other axis.

    :param designspace: The document to repair. Not written to disk - see
        :func:`reset_axis_maps_to_design` for that.
    :param tags: Tags of the axes to reset. Weight is deliberately not in the
        default: a stem of 450 labelled 400 is a map the designer wants.
    :returns: An :class:`AxisResetResult`.
    """
    result = AxisResetResult()
    masters = master_sources(designspace)
    for axis in designspace.axes:
        if axis.tag not in tags or not axis.name:
            continue
        designs = [
            float(source.getFullDesignLocation(designspace)[axis.name]) for source in masters
        ]
        if not designs:
            continue
        designs.extend(
            float(value)
            for instance in designspace.instances
            for value in [(instance.designLocation or {}).get(axis.name)]
            if isinstance(value, (int, float))
        )
        minimum = min(designs)
        maximum = max(designs)
        default = min(max(float(axis.map_forward(axis.default)), minimum), maximum)
        unchanged = (
            not axis.map
            and abs(float(axis.minimum) - minimum) < EPSILON
            and abs(float(axis.maximum) - maximum) < EPSILON
            and abs(float(axis.default) - default) < EPSILON
        )
        if unchanged:
            continue
        axis.map = []
        axis.minimum = minimum
        axis.maximum = maximum
        axis.default = default
        label = _axis_label(axis)
        result.axes.append(label)
        result.ranges.append((label, minimum, default, maximum))
    if result.axes:
        LOGGER.warning("Reset axis map(s) to the design coordinates on: %s", result.axes)
    return result


def reset_axis_maps_to_design(
    designspace_path: str | os.PathLike[str],
    tags: tuple[str, ...] = ("wdth",),
) -> AxisResetResult:
    """Reset axis maps in a designspace file to the design coordinates.

    The file is only written when something was actually changed.

    :param designspace_path: Path to the .designspace file.
    :param tags: See :func:`reset_axis_maps_to_design_document`.
    :returns: An :class:`AxisResetResult`.
    """
    path = os.fspath(designspace_path)
    designspace = DesignSpaceDocument.fromfile(path)
    result = reset_axis_maps_to_design_document(designspace, tags)
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


@dataclass
class InvertedAxisMapResult:
    """What :func:`repair_inverted_axis_maps` changed."""

    #: Axes whose map was reduced to a non-decreasing run.
    axes: list[str] = field(default_factory=list)

    @property
    def repaired(self) -> int:
        return len(self.axes)

    def summary(self) -> str:
        """A single line for a processing log."""
        if not self.axes:
            return "no axis map decreases"
        names = ", ".join(self.axes)
        return (
            f"{len(self.axes)} axis map(s) decreased in design space; "
            f"the longest non-decreasing run was kept: {names}"
        )


def _longest_nondecreasing_run(
    pairs: list[tuple[float, float]], default_user: float | None
) -> list[tuple[float, float]]:
    """Longest user-ordered run whose designs never decrease.

    Ties prefer the run containing the axis default, then the widest design
    span, then the most strictly increasing steps, so a minority conflicting
    entry loses to the majority mapping rather than the reverse.
    """
    n = len(pairs)
    if n <= 1:
        return list(pairs)
    users = [user for user, _ in pairs]
    designs = [design for _, design in pairs]

    def _is_default(index: int) -> bool:
        return default_user is not None and abs(users[index] - default_user) < EPSILON

    # dp[i] is the best run ending at i: (length, includes_default, span,
    # strict_steps, first_design, prev_index).
    dp: list[tuple[int, bool, float, int, float, int | None]] = []
    for i in range(n):
        best: tuple[int, bool, float, int, float, int | None] = (
            1,
            _is_default(i),
            0.0,
            0,
            designs[i],
            None,
        )
        for j in range(i):
            if designs[j] > designs[i]:
                continue
            prev_len, prev_has_default, _, prev_strict, prev_first, _ = dp[j]
            candidate = (
                prev_len + 1,
                prev_has_default or _is_default(i),
                designs[i] - prev_first,
                prev_strict + (1 if designs[j] < designs[i] else 0),
                prev_first,
                j,
            )
            if candidate[:4] > best[:4]:
                best = candidate
        dp.append(best)

    end: int | None = max(range(n), key=lambda i: dp[i][:4])
    kept: list[tuple[float, float]] = []
    while end is not None:
        kept.append(pairs[end])
        end = dp[end][5]
    kept.reverse()
    return kept


def repair_inverted_axis_maps_document(
    designspace: DesignSpaceDocument,
) -> InvertedAxisMapResult:
    """Drop axis-map entries that break user-to-design monotonicity, in place.

    Instantiator requires ``map_forward(min) <= map_forward(default) <=
    map_forward(max)``. glyphsLib merges per-instance user locations
    last-write-wins, so two conflicting instances can emit a crossed map —
    Reckless Italic Width was ``(50->150), (75->50), (100->100), (125->150)``
    — and interpolation raises ``ValueError`` on the design triple.

    The repair keeps the longest non-decreasing run by design, which drops the
    minority conflicting entries and preserves the majority mapping. Flat runs
    are left for :func:`repair_collapsing_axis_maps_document`.
    :func:`extend_axis_maps_to_masters_document` calls this before it adds
    points.

    :param designspace: The document to repair. Not written to disk - see
        :func:`repair_inverted_axis_maps` for that.
    :returns: An :class:`InvertedAxisMapResult`.
    """
    result = InvertedAxisMapResult()
    masters = master_sources(designspace)
    for axis in designspace.axes:
        if not axis.map:
            continue
        pairs = sorted(_axis_map_pairs(axis))
        has_decrease = any(
            pairs[i][1] < pairs[i - 1][1] - EPSILON for i in range(1, len(pairs))
        )
        design_min = float(axis.map_forward(axis.minimum))
        design_default = float(axis.map_forward(axis.default))
        design_max = float(axis.map_forward(axis.maximum))
        if not has_decrease and design_min <= design_default <= design_max:
            continue

        default_user = float(axis.default) if axis.default is not None else None
        kept = _longest_nondecreasing_run(pairs, default_user)
        source_vals = sorted(
            {
                float(source.location[axis.name])
                for source in masters
                if axis.name in (source.location or {})
            }
        )
        if len(kept) < 2 and source_vals:
            # No monotonic run to keep (for example a fully decreasing map):
            # fall back to an identity map over the master designs so every
            # master is covered.
            axis.map = [(value, value) for value in source_vals]
            axis.minimum = source_vals[0]
            axis.maximum = source_vals[-1]
            preferred = designspace.findDefault()
            default_design = source_vals[0]
            if (
                preferred is not None
                and not preferred.layerName
                and axis.name in (preferred.location or {})
            ):
                default_design = float(preferred.location[axis.name])
            axis.default = min(max(default_design, axis.minimum), axis.maximum)
        else:
            axis.map = kept
            axis.minimum = min(user for user, _ in kept)
            axis.maximum = max(user for user, _ in kept)
            if default_user is not None:
                axis.default = min(max(default_user, axis.minimum), axis.maximum)

        result.axes.append(_axis_label(axis))

    if result.axes:
        LOGGER.warning("Repaired inverted axis map(s) on: %s", result.axes)
    return result


def repair_inverted_axis_maps(
    designspace_path: str | os.PathLike[str],
) -> InvertedAxisMapResult:
    """Drop decreasing axis-map entries from a designspace file and rewrite it.

    The file is only written when something was actually changed.

    :param designspace_path: Path to the .designspace file.
    :returns: An :class:`InvertedAxisMapResult`.
    """
    path = os.fspath(designspace_path)
    designspace = DesignSpaceDocument.fromfile(path)
    result = repair_inverted_axis_maps_document(designspace)
    if result.axes:
        designspace.write(path)
    return result


__all__ = [
    "AxisMapResult",
    "AxisRangeResult",
    "AxisResetResult",
    "EPSILON",
    "DeduplicateResult",
    "DefaultMasterResult",
    "InvertedAxisMapResult",
    "deduplicate_designspace_document",
    "deduplicate_designspace_sources",
    "ensure_default_master_document",
    "ensure_designspace_default_master",
    "extend_axis_maps_to_masters",
    "extend_axis_maps_to_masters_document",
    "master_sources",
    "repair_collapsing_axis_maps",
    "repair_collapsing_axis_maps_document",
    "repair_inverted_axis_maps",
    "repair_inverted_axis_maps_document",
    "reset_axis_maps_to_design",
    "reset_axis_maps_to_design_document",
]
