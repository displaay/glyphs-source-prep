"""Bake Glyphs 4 higher-order interpolation (HOI) into intermediate layers.

Glyphs 4 lets a node follow a curve instead of a straight line when the font
interpolates along an axis. The curve is stored on the node itself, as a
``hoi`` attribute keyed by axis tag, on the *lower* node of the span (the
GlyphsSDK Glyphs 4 file format spec, ``nodeAttr.hoi``). Nothing downstream
knows the attribute: glyphsLib reads it into the node's ``userData`` and
interpolates in straight lines, so the outlines come out wrong with no
warning - on Gellix Futt by up to 50 units half way along the axis.

What Glyphs computes
--------------------

Nothing below is documented by Glyphs; it was reverse-engineered from
unrounded interpolations Glyphs 4.2 (build 4205) produced, and reproduces
them to better than 1e-6 units at 231 locations in six test sources covering
every HOI kind.

The axis carrying the HOI is cut into *columns*: the sources of a glyph that
share every other coordinate (a master pair, or a brace layer pair), one at
each end of the axis. In a column every node runs along one cubic Bezier
``P0 -> C1 -> C2 -> P1`` in ``t = (s - s_lo) / (s_hi - s_lo)``, with ``t``
used directly as the curve parameter. ``s`` is the design coordinate - the
one the layers, the instances and the variable font's deltas all live in - so
an axis mapping (Axis Location), which only relates it to the user
coordinate, does not enter here. ``P0`` is the node on the lower layer,
``P1`` on the upper one, ``d = P1 - P0`` and ``R(v) = (v.y, -v.x)``:

* no ``hoi``: ``C1 = P0 + d/3``, ``C2 = P1 - d/3`` - a straight line;
* intermediate point ``ip``: the quadratic through ``ip`` at ``t = 0.5``,
  raised to a cubic - ``Q = 2 ip - (P0 + P1)/2``,
  ``C1 = P0 + 2/3 (Q - P0)``, ``C2 = P1 + 2/3 (Q - P1)``;
* Bezier ``tl, tu, al, au`` (defaults 1/3, 1/3, 0, 0):
  ``C1 = P0 + tl d + al R(d)``, ``C2 = P1 - tu d + au R(-d)``;
* rotation ``rc`` (a point, or ``"auto"``: the mean of the centres of the
  tight bounds of the node's path on the lower and the upper layer) and
  ``rd`` (``cw`` / ``ccw``, else the shorter way round): the standard
  circular-arc cubic, ``k = 4/3 tan(theta/4)``, with the radius interpolated
  linearly across the four control points.

Across the other axes the *control points* interpolate linearly between the
columns, like any point - not the HOI parameters. Advance widths, anchors and
component transforms do not take part in HOI at all.

How it is baked
---------------

A variable font can only interpolate in straight lines between masters, so
the curve is approximated by intermediate (brace) layers along the HOI axis,
synthesized in every column of the glyph:

* at every instance location on the axis, so the static fonts come out
  exactly as Glyphs draws them - the control points are linear along the
  other axes, so a full grid of layers makes the interpolation exact at those
  locations for every weight in between, too;
* then wherever the straight-line approximation is still further than
  ``tolerance`` units from the curve, halving the worst span until it is not.

Glyphs' own variable font export encodes the cubic exactly with three copies
of the axis under the same tag. That was considered and rejected: it breaks
the STAT spec, fontTools' instancer silently linearises it, and every
application that addresses axes by index draws it wrong.

Upstream: nothing yet. googlefonts/glyphsLib#1189 (Glyphs 4 sources) is open
and the maintainers want Glyphs 4 support in fontc rather than glyphsLib; but
Glyphs 4 also writes ``hoi`` into format 3 files, which glyphsLib reads today.
The module is written as a pure function on a glyphsLib ``GSFont`` with no
other dependency on this package, so that it can move to glyphsLib's
``builder/transformations`` as it is.
"""

from __future__ import annotations

import copy
import logging
import math
import uuid
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from glyphsLib.classes import GSFont, GSGlyph

LOGGER = logging.getLogger(__name__)

#: The node attribute Glyphs 4 stores the curve in.
HOI_KEY = "hoi"

#: How many names the summary lists before it stops.
MAX_REPORTED_GLYPHS = 10

#: Default maximum distance, in font units, between the baked outline and the
#: curve Glyphs draws, between two synthesized layers.
DEFAULT_TOLERANCE = 1.0

#: Default cap on the intermediate layers synthesized per column. Gellix Futt
#: needs 7 for 1 unit; a curve that needs more than this is extreme (a node
#: swinging almost a full turn round its centre) and is refused rather than
#: baked into hundreds of masters.
DEFAULT_MAX_INTERMEDIATE = 16

#: Points sampled inside each span to measure the approximation error.
_ERROR_SAMPLES = 8

Point = tuple[float, float]
Cubic = tuple[Point, Point, Point, Point]


class HOIError(ValueError):
    """A source uses HOI in a way this module does not know how to bake.

    Raised instead of falling back to straight lines: that fallback is the
    silent wrong outline this module exists to prevent. The message names
    every glyph and reason.
    """


@dataclass
class HOIBakeResult:
    """What :func:`bake_hoi` did."""

    #: The tag of the axis the HOI runs along, or None when there was none.
    axis_tag: str | None = None
    #: Glyphs that had HOI and got intermediate layers.
    glyphs: list[str] = field(default_factory=list)
    #: How many intermediate layers were synthesized in total.
    layers_added: int = 0
    #: Every axis value an intermediate layer was synthesized at.
    locations: list[float] = field(default_factory=list)
    #: The largest remaining distance between the baked outline and the curve.
    max_error: float = 0.0
    tolerance: float = DEFAULT_TOLERANCE

    def summary(self) -> str:
        """A single line for a processing log."""
        if not self.glyphs:
            return "no higher-order interpolation (HOI) to bake"
        names = ", ".join(self.glyphs[:MAX_REPORTED_GLYPHS])
        if len(self.glyphs) > MAX_REPORTED_GLYPHS:
            names += f" and {len(self.glyphs) - MAX_REPORTED_GLYPHS} more"
        values = ", ".join(_format_number(v) for v in self.locations)
        return (
            f"HOI on {self.axis_tag!r} baked into {self.layers_added} intermediate "
            f"layer(s) on {len(self.glyphs)} glyph(s) at {self.axis_tag} = {values} "
            f"(max deviation {self.max_error:.2f}, tolerance {self.tolerance:g}): {names}"
        )


# ------------------------------------------------------------------ detection


def node_hoi(node: Any) -> dict[str, Any] | None:
    """The ``hoi`` attribute of a node, or None.

    glyphsLib keeps the fourth element of a node in ``userData``; ``_userData``
    is read directly because the proxy is created per access and this runs over
    every node of a source.
    """
    data = getattr(node, "_userData", None)
    if not data:
        return None
    try:
        hoi = data.get(HOI_KEY)
    except AttributeError:
        return None
    return hoi if hoi else None


def _strip_node_hoi(node: Any) -> bool:
    data = getattr(node, "_userData", None)
    if data and HOI_KEY in data:
        del data[HOI_KEY]
        return True
    return False


def _layer_paths(layer: Any) -> list[Any]:
    try:
        return list(layer.paths)
    except Exception:  # noqa: BLE001 - a layer without paths has no HOI either
        return []


def find_hoi(font: GSFont) -> dict[str, set[str]]:
    """Which glyphs carry HOI, and along which axes.

    For a consumer that does not bake: a non-empty result means the font will
    not come out as Glyphs draws it. Only the layers that are built count -
    masters, intermediate and alternate layers; a stale ``hoi`` on a backup
    layer does not.

    :returns: ``{glyph name: {axis tag, ...}}``, only glyphs with HOI.
    """
    found: dict[str, set[str]] = {}
    master_ids = {str(m.id) for m in font.masters}
    for glyph in font.glyphs:
        for layer in glyph.layers:
            if not _is_source_layer(layer, master_ids):
                continue
            for path in _layer_paths(layer):
                for node in path.nodes:
                    hoi = node_hoi(node)
                    if hoi:
                        found.setdefault(str(glyph.name), set()).update(str(k) for k in hoi)
    return found


# ------------------------------------------------------------------ geometry


def _number(value: Any) -> float:
    if isinstance(value, str):
        value = value.strip().strip('"')
    return float(value)


def _point(value: Any) -> Point:
    """An ``ip`` / ``rc`` value: a two-item list, or its string form."""
    if isinstance(value, str):
        parts = value.strip().strip("()[]{}").replace(";", ",").split(",")
        values = [p for p in (part.strip() for part in parts) if p]
    else:
        values = list(value)
    if len(values) != 2:
        raise ValueError(f"expected an (x, y) pair, got {value!r}")
    return (_number(values[0]), _number(values[1]))


def _add(a: Point, b: Point) -> Point:
    return (a[0] + b[0], a[1] + b[1])


def _sub(a: Point, b: Point) -> Point:
    return (a[0] - b[0], a[1] - b[1])


def _mul(a: Point, f: float) -> Point:
    return (a[0] * f, a[1] * f)


def _lerp(a: Point, b: Point, t: float) -> Point:
    return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)


def _rot_cw(v: Point) -> Point:
    """``v`` rotated 90 degrees clockwise (y up)."""
    return (v[1], -v[0])


def _perp(v: Point) -> Point:
    """``v`` rotated 90 degrees counter-clockwise (y up)."""
    return (-v[1], v[0])


def _linear(p0: Point, p1: Point) -> Cubic:
    d = _sub(p1, p0)
    return (p0, _add(p0, _mul(d, 1 / 3)), _sub(p1, _mul(d, 1 / 3)), p1)


def _rotation(p0: Point, p1: Point, center: Point, direction: str | None) -> Cubic:
    v0 = _sub(p0, center)
    v1 = _sub(p1, center)
    r0 = math.hypot(*v0)
    r1 = math.hypot(*v1)
    if r0 == 0 or r1 == 0:
        return _linear(p0, p1)
    u0 = _mul(v0, 1 / r0)
    u1 = _mul(v1, 1 / r1)
    theta = (math.atan2(u1[1], u1[0]) - math.atan2(u0[1], u0[0])) % (2 * math.pi)
    if direction == "cw":
        if theta > 0:
            theta -= 2 * math.pi
    elif direction != "ccw" and theta > math.pi:
        theta -= 2 * math.pi
    k = 4 / 3 * math.tan(theta / 4)
    c1 = _add(center, _mul(_add(u0, _mul(_perp(u0), k)), (2 * r0 + r1) / 3))
    c2 = _add(center, _mul(_sub(u1, _mul(_perp(u1), k)), (r0 + 2 * r1) / 3))
    return (p0, c1, c2, p1)


def node_cubic(p0: Point, p1: Point, hoi: dict[str, Any] | None, center: Point | None) -> Cubic:
    """The curve one node follows between the lower and the upper layer.

    :param hoi: The node's HOI description for this axis, or None.
    :param center: The rotation centre ``"auto"`` resolves to for this node's
        path; only read for a rotation.
    """
    if not hoi:
        return _linear(p0, p1)
    if "ip" in hoi:
        ip = _point(hoi["ip"])
        q = _sub(_mul(ip, 2), _mul(_add(p0, p1), 0.5))
        return (p0, _lerp(p0, q, 2 / 3), _lerp(p1, q, 2 / 3), p1)
    if "rc" in hoi:
        rc = hoi["rc"]
        if isinstance(rc, str) and rc.strip().strip('"') == "auto":
            if center is None:
                raise ValueError("rotation centre 'auto' without a path to take it from")
            rotation_center = center
        else:
            rotation_center = _point(rc)
        direction = hoi.get("rd")
        if direction is not None:
            direction = str(direction).strip().strip('"')
            if direction not in ("cw", "ccw"):
                raise ValueError(f"unknown rotation direction {direction!r}")
        return _rotation(p0, p1, rotation_center, direction)
    if any(key in hoi for key in ("tl", "tu", "al", "au")):
        d = _sub(p1, p0)
        tl = _number(hoi.get("tl", 1 / 3))
        tu = _number(hoi.get("tu", 1 / 3))
        al = _number(hoi.get("al", 0))
        au = _number(hoi.get("au", 0))
        c1 = _add(_add(p0, _mul(d, tl)), _mul(_rot_cw(d), al))
        c2 = _add(_sub(p1, _mul(d, tu)), _mul(_rot_cw(_mul(d, -1)), au))
        return (p0, c1, c2, p1)
    raise ValueError(f"unknown HOI kind: {sorted(hoi)}")


def cubic_point(cubic: Cubic, t: float) -> Point:
    p0, c1, c2, p1 = cubic
    u = 1 - t
    a, b, c, d = u * u * u, 3 * t * u * u, 3 * t * t * u, t * t * t
    return (
        a * p0[0] + b * c1[0] + c * c2[0] + d * p1[0],
        a * p0[1] + b * c1[1] + c * c2[1] + d * p1[1],
    )


_SEGMENT_TYPES = {"line": "line", "curve": "curve", "qcurve": "qcurve", "offcurve": None}


def _path_center(path: Any) -> Point:
    """Centre of the tight bounds (curve extremes) of a path."""
    from fontTools.pens.boundsPen import BoundsPen
    from fontTools.pens.pointPen import PointToSegmentPen

    bounds_pen = BoundsPen(None)
    pen = PointToSegmentPen(bounds_pen)
    pen.beginPath()
    closed = bool(getattr(path, "closed", True))
    for index, node in enumerate(path.nodes):
        segment_type = _SEGMENT_TYPES.get(str(node.type), "line")
        if index == 0 and not closed:
            segment_type = "move"
        pen.addPoint((float(node.position.x), float(node.position.y)), segment_type)
    pen.endPath()
    if bounds_pen.bounds is None:
        raise ValueError("a path without bounds")
    x0, y0, x1, y1 = bounds_pen.bounds
    return ((x0 + x1) / 2, (y0 + y1) / 2)


# ------------------------------------------------------------------ the font


def _format_number(value: float) -> str:
    return str(_canonical(value))


def _canonical(value: float) -> int | float:
    """One spelling per value: glyphsLib names a brace layer from ``str(v)``,
    so ``25`` and ``25.0`` would become two designspace sources."""
    rounded = round(float(value), 6)
    return int(rounded) if rounded == int(rounded) else rounded


def _hoi_axis_index(font: GSFont, tag: str) -> int:
    for index, axis in enumerate(font.axes):
        if str(getattr(axis, "axisTag", "")) == tag:
            return index
    raise HOIError(f"HOI along axis {tag!r}, which the font does not have")


def _master_locations(font: GSFont) -> dict[str, tuple[float, ...]]:
    return {str(m.id): tuple(float(v) for v in m.axes) for m in font.masters}


def _is_master_layer(layer: Any, master_ids: Iterable[str]) -> bool:
    return str(layer.layerId) in master_ids and str(layer.associatedMasterId) == str(
        layer.layerId
    )


def _is_brace(layer: Any) -> bool:
    try:
        return bool(layer._is_brace_layer())
    except Exception:  # noqa: BLE001 - private API
        return False


def _is_bracket(layer: Any) -> bool:
    try:
        return bool(layer._is_bracket_layer())
    except Exception:  # noqa: BLE001 - private API
        return False


def _is_source_layer(layer: Any, master_ids: Iterable[str]) -> bool:
    """A layer that is built: a master, an intermediate or an alternate one."""
    return _is_master_layer(layer, master_ids) or _is_brace(layer) or _is_bracket(layer)


def _raw_brace_coordinates(layer: Any) -> list[Any] | None:
    """The coordinates as written, without the float conversion of
    ``_brace_coordinates()``: glyphsLib names the source after them."""
    try:
        coordinates = layer.attributes.get("coordinates")
    except AttributeError:
        return None
    return list(coordinates) if coordinates else None


def _layer_location(
    layer: Any, master_locations: dict[str, tuple[float, ...]], axis_count: int
) -> tuple[float, ...] | None:
    """Where a master or brace layer sits, padded the way glyphsLib does."""
    master_location = master_locations.get(str(layer.associatedMasterId))
    if master_location is None:
        return None
    if str(layer.layerId) in master_locations:
        return master_location
    coordinates = [float(v) for v in layer._brace_coordinates()]
    if len(coordinates) < axis_count:
        coordinates += list(master_location[len(coordinates) : axis_count])
    return tuple(coordinates[:axis_count])


@dataclass
class _Column:
    key: tuple[float, ...]
    lower: Any
    upper: Any
    #: per path, per node: the curve the node follows
    cubics: list[list[Cubic]]
    #: the curves of the nodes that have HOI - the only ones a straight line
    #: can miss, so the only ones the error is measured on
    curved: list[Cubic] = field(default_factory=list)


def _source_layers(
    glyph: GSGlyph, master_locations: dict[str, tuple[float, ...]], axis_count: int
) -> Iterator[tuple[Any, tuple[float, ...]]]:
    for layer in glyph.layers:
        if _is_bracket(layer):
            continue
        if _is_master_layer(layer, master_locations) or _is_brace(layer):
            location = _layer_location(layer, master_locations, axis_count)
            if location is not None:
                yield layer, location


def _shapes_signature(layer: Any) -> list[tuple[Any, ...]]:
    signature: list[tuple[Any, ...]] = []
    for shape in layer.shapes:
        if hasattr(shape, "nodes"):
            signature.append(
                ("path", bool(shape.closed), tuple(str(n.type) for n in shape.nodes))
            )
        else:
            signature.append(("component", str(shape.componentName)))
    return signature


def _glyph_columns(
    glyph: GSGlyph,
    axis_index: int,
    axis_tag: str,
    master_locations: dict[str, tuple[float, ...]],
    axis_count: int,
    problems: list[str],
) -> tuple[list[_Column], tuple[float, float]] | None:
    """Pair the glyph's sources into columns and compute every node's curve."""
    name = str(glyph.name)
    for layer in glyph.layers:
        if _is_bracket(layer) and any(
            node_hoi(n) for p in _layer_paths(layer) for n in p.nodes
        ):
            problems.append(f"{name}: HOI on an alternate (bracket) layer")
            return None

    by_key: dict[tuple[float, ...], dict[float, Any]] = {}
    for layer, location in _source_layers(glyph, master_locations, axis_count):
        key = location[:axis_index] + location[axis_index + 1 :]
        column = by_key.setdefault(key, {})
        if location[axis_index] in column:
            problems.append(
                f"{name}: two layers at {[_canonical(v) for v in location]} "
                f"({column[location[axis_index]].name!r} / {layer.name!r})"
            )
            return None
        column[location[axis_index]] = layer

    spans = {tuple(sorted(column)) for column in by_key.values()}
    if len(spans) != 1 or len(next(iter(spans))) != 2:
        problems.append(
            f"{name}: HOI needs every source paired at both ends of {axis_tag!r}, "
            f"found {axis_tag} = {sorted({v for s in spans for v in s})} "
            f"in {len(by_key)} column(s)"
        )
        return None
    lo, hi = next(iter(spans))

    columns: list[_Column] = []
    for key in sorted(by_key):
        lower, upper = by_key[key][lo], by_key[key][hi]
        if _shapes_signature(lower) != _shapes_signature(upper):
            problems.append(
                f"{name}: the layers at {axis_tag} = {lo:g} and {hi:g} are not "
                f"compatible ({lower.name!r} / {upper.name!r})"
            )
            return None
        if any(node_hoi(n) for p in _layer_paths(upper) for n in p.nodes):
            problems.append(
                f"{name}: HOI on the upper layer {upper.name!r} - Glyphs stores it on "
                "the lower one"
            )
            return None
        cubics: list[list[Cubic]] = []
        curved: list[Cubic] = []
        lower_paths = _layer_paths(lower)
        upper_paths = _layer_paths(upper)
        for lower_path, upper_path in zip(lower_paths, upper_paths, strict=True):
            center: Point | None = None
            node_cubics: list[Cubic] = []
            for lower_node, upper_node in zip(lower_path.nodes, upper_path.nodes, strict=True):
                hoi_all = node_hoi(lower_node) or {}
                unknown = set(hoi_all) - {axis_tag}
                if unknown:
                    problems.append(f"{name}: HOI along more than one axis {sorted(hoi_all)}")
                    return None
                hoi = hoi_all.get(axis_tag)
                if hoi and "rc" in hoi and center is None:
                    lower_center = _path_center(lower_path)
                    upper_center = _path_center(upper_path)
                    center = _lerp(lower_center, upper_center, 0.5)
                p0 = (float(lower_node.position.x), float(lower_node.position.y))
                p1 = (float(upper_node.position.x), float(upper_node.position.y))
                try:
                    cubic = node_cubic(p0, p1, hoi, center)
                except (ValueError, TypeError) as error:
                    problems.append(f"{name}: unreadable HOI {hoi!r}: {error}")
                    return None
                node_cubics.append(cubic)
                if hoi:
                    curved.append(cubic)
            cubics.append(node_cubics)
        columns.append(_Column(key=key, lower=lower, upper=upper, cubics=cubics, curved=curved))
    return columns, (lo, hi)


def _span_error(columns: Sequence[_Column], ta: float, tb: float) -> float:
    """Largest distance between the straight line from ``ta`` to ``tb`` and
    the curve, over every node of every column."""
    worst = 0.0
    fractions = [(i + 1) / (_ERROR_SAMPLES + 1) for i in range(_ERROR_SAMPLES)]
    for column in columns:
        for cubic in column.curved:
            a = cubic_point(cubic, ta)
            b = cubic_point(cubic, tb)
            for f in fractions:
                on_curve = cubic_point(cubic, ta + (tb - ta) * f)
                on_line = _lerp(a, b, f)
                distance = math.hypot(on_curve[0] - on_line[0], on_curve[1] - on_line[1])
                worst = max(worst, distance)
    return worst


def _refine(
    columns: Sequence[_Column],
    required: Sequence[float],
    tolerance: float,
    max_intermediate: int,
    glyph_name: str,
) -> tuple[list[float], float]:
    """The ``t`` values to synthesize layers at, and the error that remains.

    Starts from ``required`` and halves the worst span until every span is
    within tolerance; ``max_intermediate`` caps the halvings, not the
    required values. Halving keeps the values on one shared dyadic lattice
    per required set, so glyphs that need the same refinement share sources.
    """
    ts = sorted({0.0, 1.0, *required})
    fixed = len(ts)
    errors = {(a, b): _span_error(columns, a, b) for a, b in zip(ts, ts[1:], strict=False)}
    while True:
        (a, b), worst = max(errors.items(), key=lambda item: item[1])
        if worst <= tolerance:
            return ts[1:-1], worst
        if len(ts) - fixed >= max_intermediate:
            raise HOIError(
                f"{glyph_name}: the HOI curve is still {worst:.1f} units from straight "
                f"after {max_intermediate} intermediate layers (tolerance {tolerance:g})"
            )
        middle = (a + b) / 2
        del errors[(a, b)]
        errors[(a, middle)] = _span_error(columns, a, middle)
        errors[(middle, b)] = _span_error(columns, middle, b)
        ts = sorted({*ts, middle})


def _existing_brace_layers(
    font: GSFont, master_locations: dict[str, tuple[float, ...]], axis_count: int
) -> dict[tuple[float, ...], tuple[str, list[Any]]]:
    """``{location: (master id, coordinates as written)}`` of the font's brace
    layers, so a synthesized layer at the same location joins the same source."""
    found: dict[tuple[float, ...], tuple[str, list[Any]]] = {}
    for glyph in font.glyphs:
        for layer in glyph.layers:
            if not _is_brace(layer):
                continue
            coordinates = _raw_brace_coordinates(layer)
            try:
                location = _layer_location(layer, master_locations, axis_count)
            except (TypeError, ValueError):
                continue
            if coordinates is None or location is None:
                continue
            # a short spelling is kept as it is, with its master - that master
            # supplies the rest of the location, and the pair is what glyphsLib
            # keys the source on
            location = tuple(float(_canonical(v)) for v in location)
            found.setdefault(location, (str(layer.associatedMasterId), coordinates))
    return found


def _synthesize_layer(column: _Column, t: float, master_id: str, coordinates: list[Any]) -> Any:
    lower, upper = column.lower, column.upper
    layer = type(lower)()
    layer.layerId = str(uuid.uuid4()).upper()
    layer.associatedMasterId = master_id
    layer.name = "{" + ", ".join(str(v) for v in coordinates) + "}"
    layer.attributes["coordinates"] = list(coordinates)
    # whole units, as Glyphs exports them; a fractional advance also costs
    # fontTools' CFF width optimiser seconds per source
    layer.width = round(lower.width + (upper.width - lower.width) * t)
    for metric in ("vertWidth", "vertOrigin"):
        low, up = getattr(lower, metric, None), getattr(upper, metric, None)
        if low is not None:
            setattr(layer, metric, round(low + ((up if up is not None else low) - low) * t))

    path_index = 0
    for lower_shape, upper_shape in zip(lower.shapes, upper.shapes, strict=True):
        if hasattr(lower_shape, "nodes"):
            path = type(lower_shape)()
            path.closed = lower_shape.closed
            cubics = column.cubics[path_index]
            path_index += 1
            for node, cubic in zip(lower_shape.nodes, cubics, strict=True):
                x, y = cubic_point(cubic, t)
                path.nodes.append(type(node)((x, y), type=node.type, smooth=node.smooth))
            layer.shapes.append(path)
        else:
            layer.shapes.append(_interpolated_component(lower_shape, upper_shape, t))

    # hints carry the corner components and caps, which glyphsLib applies per
    # source: a layer without them would not be compatible with its masters.
    # They point at nodes by index, and the paths are the lower layer's.
    for hint in getattr(lower, "hints", None) or []:
        layer.hints.append(copy.copy(hint))

    upper_anchors = {str(anchor.name): anchor for anchor in upper.anchors}
    for anchor in lower.anchors:
        other = upper_anchors.get(str(anchor.name), anchor)
        position = _lerp(
            (float(anchor.position.x), float(anchor.position.y)),
            (float(other.position.x), float(other.position.y)),
            t,
        )
        point = type(anchor.position)(position[0], position[1])
        layer.anchors.append(type(anchor)(name=anchor.name, position=point))
    return layer


def _interpolated_component(lower: Any, upper: Any, t: float) -> Any:
    a = list(lower.transform)
    b = list(upper.transform)
    transform = type(lower.transform)(*(va + (vb - va) * t for va, vb in zip(a, b, strict=True)))
    component = type(lower)(lower.componentName, transform=transform)
    for attribute in ("anchor", "alignment", "locked"):
        value = getattr(lower, attribute, None)
        if value is not None:
            setattr(component, attribute, value)
    low: dict[str, Any] = dict(getattr(lower, "smartComponentValues", None) or {})
    up: dict[str, Any] = dict(getattr(upper, "smartComponentValues", None) or {})
    if low or up:
        values: dict[str, Any] = {}
        for key in low.keys() | up.keys():
            va: Any = low.get(key, up.get(key))
            vb: Any = up.get(key, va)
            try:
                values[key] = float(va) + (float(vb) - float(va)) * t
            except (TypeError, ValueError):
                values[key] = va
        component.smartComponentValues = values
    return component


def bake_hoi(
    font: GSFont,
    *,
    tolerance: float = DEFAULT_TOLERANCE,
    max_intermediate: int = DEFAULT_MAX_INTERMEDIATE,
    extra_locations: Iterable[float] = (),
) -> HOIBakeResult:
    """Replace every node's HOI with intermediate layers, in place.

    Run it on the source model right before it is built into masters - not
    when loading: what a source *says* (validation, the version snapshot)
    should see the HOI, not the layers standing in for it. Running it twice
    does nothing the second time, because the HOI is removed once baked.

    :param font: A glyphsLib GSFont, modified in place.
    :param tolerance: The largest distance, in font units, the baked outline
        may stray from the curve between two synthesized layers.
    :param max_intermediate: The most layers one column may need; a glyph that
        needs more is refused.
    :param extra_locations: Axis values (design coordinates) that must be
        exact besides the font's instances, e.g. custom static styles.
    :raises HOIError: The HOI cannot be baked faithfully; the message names
        every glyph and why. Nothing is changed then.
    """
    result = HOIBakeResult(tolerance=tolerance)
    found = find_hoi(font)
    if not found:
        return result

    tags = set().union(*found.values())
    if len(tags) != 1:
        raise HOIError(f"HOI along more than one axis: {sorted(tags)}")
    axis_tag = tags.pop()
    result.axis_tag = axis_tag
    axis_index = _hoi_axis_index(font, axis_tag)

    axis_count = len(font.axes)
    master_locations = _master_locations(font)
    axis_values = {
        float(instance.axes[axis_index])
        for instance in font.instances
        if _is_exported_static(instance) and len(instance.axes) > axis_index
    }
    axis_values.update(float(v) for v in extra_locations)

    problems: list[str] = []
    plans: list[tuple[GSGlyph, list[_Column], tuple[float, float], list[float], float]] = []
    for glyph in font.glyphs:
        if str(glyph.name) not in found:
            continue
        paired = _glyph_columns(
            glyph, axis_index, axis_tag, master_locations, axis_count, problems
        )
        if paired is None:
            continue
        columns, (lo, hi) = paired
        required = sorted((v - lo) / (hi - lo) for v in axis_values if lo < v < hi)
        try:
            ts, error = _refine(columns, required, tolerance, max_intermediate, str(glyph.name))
        except HOIError as refused:
            problems.append(str(refused))
            continue
        plans.append((glyph, columns, (lo, hi), ts, error))

    if problems:
        raise HOIError(
            "higher-order interpolation (HOI) that cannot be baked: " + "; ".join(problems)
        )

    origin = _origin_master_id(font)
    existing = _existing_brace_layers(font, master_locations, axis_count)
    locations: set[float] = set()
    # built first and added at the end, so that a failure leaves the font as it was
    pending: list[tuple[GSGlyph, Any]] = []
    for glyph, columns, (lo, hi), ts, error in plans:
        for column in columns:
            for t in ts:
                # place the layer where its (canonically spelled) name says,
                # and draw it at exactly that point of the curve
                value = float(_canonical(lo + (hi - lo) * t))
                t = (value - lo) / (hi - lo)
                location = column.key[:axis_index] + (value,) + column.key[axis_index:]
                location = tuple(float(_canonical(v)) for v in location)
                if location in existing:
                    master_id, coordinates = existing[location]
                else:
                    master_id = origin
                    coordinates = [_canonical(v) for v in location]
                    existing[location] = (master_id, coordinates)
                pending.append((glyph, _synthesize_layer(column, t, master_id, coordinates)))
                result.layers_added += 1
                locations.add(float(_canonical(value)))
        result.glyphs.append(str(glyph.name))
        result.max_error = max(result.max_error, error)

    for glyph, layer in pending:
        glyph.layers.append(layer)
    for glyph in font.glyphs:
        if str(glyph.name) in found:
            for layer in glyph.layers:
                for path in _layer_paths(layer):
                    for node in path.nodes:
                        _strip_node_hoi(node)

    result.locations = sorted(locations)
    LOGGER.info(result.summary())
    return result


def _is_exported_static(instance: Any) -> bool:
    """A static instance that is exported; a variable-font setting is not a
    location (glyphsLib gives it default axes values)."""
    if int(getattr(instance, "type", 0) or 0) == 1:  # InstanceType.VARIABLE
        return False
    return bool(getattr(instance, "exports", True))


def _origin_master_id(font: GSFont) -> str:
    """The variable font origin, without importing the sibling module, so this
    file can move to glyphsLib on its own."""
    origin = font.customParameters.get("Variable Font Origin")
    if origin:
        return str(origin)
    legacy = font.customParameters.get("Variation Font Origin")
    if legacy:
        for master in font.masters:
            if master.name == legacy:
                return str(master.id)
    return str(font.masters[0].id)
