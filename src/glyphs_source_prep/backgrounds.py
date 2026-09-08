"""Drop background-layer components that point at a glyph the source lacks.

Glyphs.app tolerates a background layer referencing a glyph that has since
been renamed or deleted - the background is a sketch pad, it is never
compiled. glyphsLib does not: ``to_ufo_glyph_background()`` decomposes
background components like any others and raises ``MissingComponentError``.
A whole build then fails over drawing scaffolding that has no effect on the
output.

Seen on a retail source with 981 background components referencing a deleted
``A.old``, while all 605 glyphs had clean foregrounds.

Upstream: googlefonts/glyphsLib#743, open since 2021. The maintainers are
receptive ("one can make a case for skipping such components by default or
under an option") and fontTools now has the mechanism -
``DecomposingRecordingPen(..., skipMissingComponents=True)`` - but glyphsLib
still does not pass it. When it does, this module can go.

Only the dangling component is dropped, never the layer, and never outside a
background: a dangling component in a *drawing* layer changes the output and
must keep failing.
"""

from __future__ import annotations

import collections
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from glyphsLib.classes import GSFont

LOGGER = logging.getLogger(__name__)

#: How many missing glyph names the summary names before it stops.
MAX_REPORTED_NAMES = 10


@dataclass
class BackgroundCleanupResult:
    """What :func:`drop_dangling_background_components` removed."""

    dropped: int = 0
    glyphs: list[str] = field(default_factory=list)
    missing_refs: collections.Counter[str] = field(default_factory=collections.Counter)

    def summary(self) -> str:
        """A single line for a processing log."""
        if not self.dropped:
            return "no dangling background components"
        refs = ", ".join(
            f"{name} ({count})"
            for (name, count) in self.missing_refs.most_common(MAX_REPORTED_NAMES)
        )
        text = (
            f"{self.dropped} background component(s) referencing a non-existent "
            f"glyph removed from {len(self.glyphs)} glyph(s): {refs}"
        )
        if len(self.missing_refs) > MAX_REPORTED_NAMES:
            text += f" and {len(self.missing_refs) - MAX_REPORTED_NAMES} more"
        return text


def drop_dangling_background_components(font: GSFont) -> BackgroundCleanupResult:
    """Remove components of background layers that reference a missing glyph.

    :param font: A glyphsLib GSFont, modified in place.
    :returns: A :class:`BackgroundCleanupResult`.
    """
    result = BackgroundCleanupResult()
    glyph_names = {glyph.name for glyph in font.glyphs}

    for glyph in font.glyphs:
        dropped_here = 0
        for layer in glyph.layers:
            # hasBackground first: touching .background would create an empty
            # one for every layer of every glyph.
            if not layer.hasBackground:
                continue
            background = layer.background
            for component in list(background.components):
                if component.name in glyph_names:
                    continue
                background.components.remove(component)
                result.missing_refs[component.name] += 1
                dropped_here += 1
        if dropped_here:
            result.dropped += dropped_here
            result.glyphs.append(glyph.name)

    if result.dropped:
        LOGGER.warning(
            "Removed %d dangling background component(s) from %d glyph(s): %s",
            result.dropped,
            len(result.glyphs),
            dict(result.missing_refs),
        )
    return result


__all__ = [
    "MAX_REPORTED_NAMES",
    "BackgroundCleanupResult",
    "drop_dangling_background_components",
]
