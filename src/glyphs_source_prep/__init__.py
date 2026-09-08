"""glyphs-source-prep -- fix the things Glyphs.app tolerates in a .glyphs
source and glyphsLib does not.

Glyphs.app is forgiving about a handful of source states that are harmless in
the editor: a background layer referencing a glyph that was later deleted, or
brace layers at the same coordinates hung off different masters in different
glyphs. glyphsLib is stricter, so the same source that opens and exports fine
in Glyphs fails the build - sometimes deep in varLib, with a message that says
nothing about the source.

Each fix here is small, targeted and reversible in intent: it changes what the
*editor* recorded, never what the font will look like. Every one is also an
open issue upstream, so each module says which one and when it can be deleted.

    from glyphs_source_prep import (
        align_brace_layers_to_variable_origin,
        deduplicate_designspace_sources,
        drop_dangling_background_components,
    )

    report = drop_dangling_background_components(font)
    if report.dropped:
        log.warning(report.summary())

Every entry point returns a small result object with a ``summary()`` for a
processing log, rather than mutating silently: a source the build had to
repair is something its author should hear about.

glyphsLib is deliberately **not** a runtime dependency. The GSFont-based fixes
duck-type what they touch and import glyphsLib only for type checking, so this
package adds nothing to an environment that already has one.
"""

from __future__ import annotations

from .backgrounds import (
    BackgroundCleanupResult,
    drop_dangling_background_components,
)
from .brace_layers import (
    BraceLayerAlignResult,
    align_brace_layers_to_variable_origin,
    brace_coordinates,
    conflicting_locations,
    effective_location,
    is_brace_layer,
    variable_font_origin_master_id,
)
from .designspace import (
    DeduplicateResult,
    deduplicate_designspace_document,
    deduplicate_designspace_sources,
)
from .path_order import (
    PathOrderResult,
    layer_point_counts,
    reorder_master_paths_to_variable_origin,
    unambiguous_order,
)

__version__ = "0.3.1"

__all__ = [
    "BackgroundCleanupResult",
    "BraceLayerAlignResult",
    "DeduplicateResult",
    "PathOrderResult",
    "__version__",
    "align_brace_layers_to_variable_origin",
    "brace_coordinates",
    "conflicting_locations",
    "effective_location",
    "deduplicate_designspace_document",
    "deduplicate_designspace_sources",
    "drop_dangling_background_components",
    "is_brace_layer",
    "layer_point_counts",
    "reorder_master_paths_to_variable_origin",
    "unambiguous_order",
    "variable_font_origin_master_id",
]
