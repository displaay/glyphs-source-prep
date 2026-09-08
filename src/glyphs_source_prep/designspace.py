"""Drop designspace sources that share an axis location.

The last line of defence behind :mod:`glyphs_source_prep.brace_layers`: even
after the brace layers have been aligned, a designspace can still carry two
sources at the same location - from a source that was not built by glyphsLib,
or from a case the alignment does not reach. varLib requires each master to
define a unique location and fails otherwise.

Where two sources collide, the full master wins over a sparse (layer) source,
because dropping the full one would remove a real master from the design
space. Between two of a kind the order is stable but arbitrary; there is no
signal in the file that says which the author meant.

Upstream: googlefonts/glyphsLib#925 fixed the layer-naming half of this in
6.2.4/6.2.5. The half that remains - intermediate layers at the same location
associated with different masters - is #995.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

from fontTools.designspaceLib import DesignSpaceDocument, SourceDescriptor

LOGGER = logging.getLogger(__name__)


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


__all__ = [
    "DeduplicateResult",
    "deduplicate_designspace_document",
    "deduplicate_designspace_sources",
]
