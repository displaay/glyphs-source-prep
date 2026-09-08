"""Master path order, and the cases where it must not be guessed at."""

from conftest import make_font, master_layer

from glyphs_source_prep import (
    reorder_master_paths_to_variable_origin,
    unambiguous_order,
)


def path(node_count):
    """A closed path with ``node_count`` line nodes."""
    return {
        "closed": 1,
        "nodes": [[100 + i * 10, 200, "l"] for i in range(node_count)],
    }


def counts(font, glyph_name, master_id):
    layer = next(
        layer for layer in font.glyphs[glyph_name].layers
        if layer.layerId == master_id
    )
    return tuple(len(p.nodes) for p in layer.paths)


TWO_MASTERS = [("m01", "Regular", [100]), ("m02", "Bold", [700])]


def test_unambiguous_order_maps_a_pure_reordering():
    assert unambiguous_order((8, 4), (4, 8)) == [1, 0]
    assert unambiguous_order((8, 4), (8, 4)) == [0, 1]


def test_unambiguous_order_refuses_a_duplicate_point_count():
    """Which of two four-point contours belongs where is not knowable."""
    assert unambiguous_order((4, 8, 4), (4, 4, 8)) is None


def test_unambiguous_order_refuses_genuinely_different_masters():
    """Not a reordering: the masters really are incompatible."""
    assert unambiguous_order((8, 4), (8, 6)) is None
    assert unambiguous_order((8, 4), (8, 4, 4)) is None


def test_paths_are_reordered_to_the_origin():
    font = make_font(
        {"one.tf": [
            master_layer("m01", shapes=[path(8), path(4)]),
            master_layer("m02", shapes=[path(4), path(8)]),
        ]},
        masters=TWO_MASTERS,
    )
    result = reorder_master_paths_to_variable_origin(font)

    assert result.reordered == 1
    assert result.glyphs == ["one.tf"]
    assert not result.ambiguous_glyphs
    assert counts(font, "one.tf", "m02") == (8, 4)
    # The origin itself is never touched.
    assert counts(font, "one.tf", "m01") == (8, 4)


def test_a_duplicate_point_count_is_reported_not_guessed():
    font = make_font(
        {"colon": [
            master_layer("m01", shapes=[path(4), path(8), path(4)]),
            master_layer("m02", shapes=[path(4), path(4), path(8)]),
        ]},
        masters=TWO_MASTERS,
    )
    result = reorder_master_paths_to_variable_origin(font)

    assert result.reordered == 0
    assert result.ambiguous_glyphs == ["colon"]
    # Untouched: a wrong pairing would change every intermediate instance.
    assert counts(font, "colon", "m02") == (4, 4, 8)
    assert "share a point count" in result.summary()


def test_incompatible_masters_are_left_to_fail():
    """Different contours, not a different order - that must keep failing."""
    font = make_font(
        {"A": [
            master_layer("m01", shapes=[path(8), path(4)]),
            master_layer("m02", shapes=[path(8), path(6)]),
        ]},
        masters=TWO_MASTERS,
    )
    result = reorder_master_paths_to_variable_origin(font)

    assert result.reordered == 0
    assert not result.ambiguous_glyphs
    assert counts(font, "A", "m02") == (8, 6)


def test_matching_order_is_a_no_op():
    font = make_font(
        {"A": [
            master_layer("m01", shapes=[path(8), path(4)]),
            master_layer("m02", shapes=[path(8), path(4)]),
        ]},
        masters=TWO_MASTERS,
    )
    result = reorder_master_paths_to_variable_origin(font)

    assert result.reordered == 0
    assert result.summary() == "no master paths needed reordering"


def test_a_single_contour_cannot_be_out_of_order():
    font = make_font(
        {"period": [
            master_layer("m01", shapes=[path(4)]),
            master_layer("m02", shapes=[path(4)]),
        ]},
        masters=TWO_MASTERS,
    )
    assert reorder_master_paths_to_variable_origin(font).reordered == 0
