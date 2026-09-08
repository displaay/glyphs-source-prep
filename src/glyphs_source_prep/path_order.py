"""Match master path order to the variable font origin.

A glyph's contours are stored per layer, in whatever order they were drawn.
Nothing in the source records which contour of one master corresponds to which
of another - the correspondence is positional. Two masters that hold the same
contours in a different order therefore describe a glyph that cannot be
interpolated, and ufo2ft's compatibility check fails the build:

    Fatal error: glyph 'one.tf' has incompatible masters

Seen on a retail source where the proportional masters stored a ``(8, 4)``
point-count sequence and the mono masters ``(4, 8)``: the same two contours,
drawn in the other order.

Reordering contours within a layer cannot change how that master renders - a
glyph is filled from all its contours at once, and each contour keeps its own
direction and points. What it changes is which contour pairs with which across
masters, which is the whole point, and also why this is careful about *when* it
may act.

Only an unambiguous reordering
------------------------------

The correspondence is inferred from point counts, and that inference is only
sound when the counts identify a contour uniquely. A glyph whose counts contain
a duplicate - a colon with two four-point dots, a dieresis, a quotation mark -
offers no evidence about which of the two belongs where, and picking the first
match would silently pair the wrong contours and change what every intermediate
instance draws. Those are reported in ``ambiguous_glyphs`` for a human instead,
never guessed at: a build that fails is better than a font that interpolates
through the wrong shape.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .brace_layers import variable_font_origin_master_id

if TYPE_CHECKING:
    from glyphsLib.classes import GSFont, GSLayer

LOGGER = logging.getLogger(__name__)

#: How many names the summary lists before it stops.
MAX_REPORTED_GLYPHS = 10

#: A layer's contours as their point counts, in stored order.
PointCounts = tuple[int, ...]


@dataclass
class PathOrderResult:
    """What :func:`reorder_master_paths_to_variable_origin` did."""

    reordered: int = 0
    glyphs: list[str] = field(default_factory=list)
    #: Glyphs whose master path order differs but whose point counts contain a
    #: duplicate, so which contour belongs where cannot be established. Left
    #: exactly as they were: the build fails loudly rather than interpolating
    #: through the wrong pairing.
    ambiguous_glyphs: list[str] = field(default_factory=list)

    def summary(self) -> str:
        """A single line for a processing log."""
        parts = []
        if self.reordered:
            names = ", ".join(self.glyphs[:MAX_REPORTED_GLYPHS])
            text = (
                f"{self.reordered} master layer(s) on {len(self.glyphs)} glyph(s) "
                f"reordered to the variable font origin's path order: {names}"
            )
            if len(self.glyphs) > MAX_REPORTED_GLYPHS:
                text += f" and {len(self.glyphs) - MAX_REPORTED_GLYPHS} more"
            parts.append(text)
        if self.ambiguous_glyphs:
            names = ", ".join(self.ambiguous_glyphs[:MAX_REPORTED_GLYPHS])
            text = (
                f"{len(self.ambiguous_glyphs)} glyph(s) store their contours in a "
                f"different order per master and cannot be matched automatically, "
                f"because two contours share a point count: {names}"
            )
            if len(self.ambiguous_glyphs) > MAX_REPORTED_GLYPHS:
                text += (
                    f" and {len(self.ambiguous_glyphs) - MAX_REPORTED_GLYPHS} more"
                )
            parts.append(text)
        if not parts:
            return "no master paths needed reordering"
        return "; ".join(parts)


def layer_point_counts(layer: GSLayer) -> PointCounts:
    """The layer's contours as their point counts, in stored order."""
    paths: Any = getattr(layer, "paths", None) or ()
    try:
        return tuple(len(path.nodes) for path in paths)
    except (AttributeError, TypeError):
        LOGGER.debug("Could not read the paths of %r", layer, exc_info=True)
        return ()


def unambiguous_order(
    reference: PointCounts, current: PointCounts
) -> list[int] | None:
    """Indices into ``current`` that put it in ``reference`` order.

    :returns: The mapping, the identity when the orders already agree, or None
        when the two are not a pure reordering of each other *or* the point
        counts do not identify a contour uniquely.
    """
    if len(reference) != len(current) or sorted(reference) != sorted(current):
        # Not a reordering at all: genuinely incompatible masters, which must
        # keep failing.
        return None
    if reference == current:
        return list(range(len(current)))
    if len(set(reference)) != len(reference):
        # A duplicate point count: no evidence for which contour goes where.
        return None
    at = {count: index for (index, count) in enumerate(current)}
    return [at[count] for count in reference]


def reorder_master_paths_to_variable_origin(font: GSFont) -> PathOrderResult:
    """Reorder each master's contours to match the variable font origin.

    :param font: A glyphsLib GSFont, modified in place.
    :returns: A :class:`PathOrderResult`.
    """
    origin_id = variable_font_origin_master_id(font)
    master_ids = {str(master.id) for master in font.masters}
    result = PathOrderResult()

    for glyph in font.glyphs:
        if not getattr(glyph, "export", True):
            continue
        reference_layer = next(
            (layer for layer in glyph.layers if str(layer.layerId) == origin_id),
            None,
        )
        if reference_layer is None:
            continue
        reference = layer_point_counts(reference_layer)
        if len(reference) < 2:
            # One contour (or none) cannot be out of order.
            continue

        reordered_here = 0
        ambiguous = False
        for layer in glyph.layers:
            layer_id = str(layer.layerId)
            if layer_id not in master_ids or layer_id == origin_id:
                continue
            current = layer_point_counts(layer)
            if current == reference:
                continue
            order = unambiguous_order(reference, current)
            if order is None:
                if sorted(current) == sorted(reference):
                    ambiguous = True
                continue
            layer.paths = [list(layer.paths)[index] for index in order]
            reordered_here += 1

        if reordered_here:
            result.reordered += reordered_here
            result.glyphs.append(glyph.name)
        if ambiguous:
            result.ambiguous_glyphs.append(glyph.name)

    if result.reordered:
        LOGGER.info(
            "Reordered %d master layer(s) on %d glyph(s) to the origin's path order",
            result.reordered,
            len(result.glyphs),
        )
    if result.ambiguous_glyphs:
        LOGGER.warning(
            "%d glyph(s) have a per-master path order that cannot be matched: %s",
            len(result.ambiguous_glyphs),
            result.ambiguous_glyphs,
        )
    return result


__all__ = [
    "MAX_REPORTED_GLYPHS",
    "PathOrderResult",
    "PointCounts",
    "layer_point_counts",
    "reorder_master_paths_to_variable_origin",
    "unambiguous_order",
]
