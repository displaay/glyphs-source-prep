"""Align brace layers to one master so the designspace has unique locations.

Glyphs.app tolerates brace (intermediate) layers at the same coordinates being
attached to different masters in different glyphs - the association is an
editing convenience, not data. glyphsLib turns each association into a separate
designspace source, so varLib then fails with "Locations must be unique" and
fontmake gives up.

Georg Seifert put it plainly in googlefonts/glyphsLib#925: *"A brace layer is
what is called a sparse master in ufo/designspace. So no connection to the
master it is attached to. It just has to be somewhere."*

Upstream: googlefonts/glyphsLib#995, open since 2024. anthrotype: *"ideally we
should [ignore the putative master for an intermediate layer]. PR most
welcome."* schriftgestalt: *"If someone could put that into a test case, I
could fix this in the Glyphs3 branch."* When that lands, this module can go.

Until then the fix is what Glyphs users do by hand: reassign every brace layer
to the variable font origin master. It changes no outlines - only which master
a sparse source is nominally hung off.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from glyphsLib.classes import GSFont, GSLayer

LOGGER = logging.getLogger(__name__)

#: How many glyph names the summary names before it stops.
MAX_REPORTED_GLYPHS = 10


@dataclass
class BraceLayerAlignResult:
    """What :func:`align_brace_layers_to_variable_origin` moved."""

    moved: int = 0
    target_master_id: str = ""
    glyphs: list[str] = field(default_factory=list)

    def summary(self) -> str:
        """A single line for a processing log."""
        if not self.moved:
            return "no brace layers needed realigning"
        names = ", ".join(self.glyphs[:MAX_REPORTED_GLYPHS])
        text = (
            f"{self.moved} brace layer(s) on {len(self.glyphs)} glyph(s) "
            f"reassigned to master {self.target_master_id}: {names}"
        )
        if len(self.glyphs) > MAX_REPORTED_GLYPHS:
            text += f" and {len(self.glyphs) - MAX_REPORTED_GLYPHS} more"
        return text


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


def align_brace_layers_to_variable_origin(font: GSFont) -> BraceLayerAlignResult:
    """Reassign every brace layer to the variable font origin master.

    :param font: A glyphsLib GSFont, modified in place.
    :returns: A :class:`BraceLayerAlignResult`.
    """
    target_master_id = variable_font_origin_master_id(font)
    result = BraceLayerAlignResult(target_master_id=target_master_id)

    for glyph in font.glyphs:
        moved_here = 0
        for layer in glyph.layers:
            if not is_brace_layer(layer):
                continue
            if layer.associatedMasterId == target_master_id:
                continue
            layer.associatedMasterId = target_master_id
            moved_here += 1
        if moved_here:
            result.moved += moved_here
            result.glyphs.append(glyph.name)

    if result.moved:
        LOGGER.info(
            "Reassigned %d brace layer(s) on %d glyph(s) to master %s",
            result.moved,
            len(result.glyphs),
            target_master_id,
        )
    return result


__all__ = [
    "MAX_REPORTED_GLYPHS",
    "BraceLayerAlignResult",
    "align_brace_layers_to_variable_origin",
    "is_brace_layer",
    "variable_font_origin_master_id",
]
