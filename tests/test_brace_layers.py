"""
Unit tests of the brace layer alignment.
"""
from conftest import TWO_AXES, brace_layer, make_font, master_layer

from glyphs_source_prep import (
    align_brace_layers_to_variable_origin,
    brace_coordinates,
    conflicting_locations,
    effective_location,
    is_brace_layer,
    variable_font_origin_master_id,
)


def test_a_conflicting_brace_layer_is_reassigned(two_masters):
    """The case varLib rejects: one location, two associated masters."""
    font = make_font(
        {
            "a": [master_layer("m01"), master_layer("m02"),
                  brace_layer("m02", (100,))],
            "ae": [master_layer("m01"), master_layer("m02"),
                   brace_layer("m01", (100,))],
        },
        masters=two_masters,
    )

    result = align_brace_layers_to_variable_origin(font)

    assert result.moved == 1
    assert result.glyphs == ["a"]
    assert result.target_master_id == "m01"
    associated = {
        layer.associatedMasterId
        for glyph in font.glyphs
        for layer in glyph.layers
        if is_brace_layer(layer)
    }
    assert associated == {"m01"}
    assert "m01" in result.summary()


def test_brace_layers_already_aligned_are_left_alone(two_masters):
    font = make_font(
        {"a": [master_layer("m01"), master_layer("m02"), brace_layer("m01", (100,))]},
        masters=two_masters,
    )

    result = align_brace_layers_to_variable_origin(font)

    assert result.moved == 0
    assert result.glyphs == []
    assert "no brace layers" in result.summary()


def test_master_layers_are_not_touched(two_masters):
    """Only intermediate layers move; a master layer *is* its master."""
    font = make_font(
        {"a": [master_layer("m01"), master_layer("m02")]}, masters=two_masters
    )

    result = align_brace_layers_to_variable_origin(font)

    assert result.moved == 0
    assert [layer.layerId for layer in font.glyphs[0].layers] == ["m01", "m02"]


def test_the_variable_font_origin_parameter_wins(two_masters):
    font = make_font(
        {"a": [master_layer("m01"), master_layer("m02"), brace_layer("m01", (100,))]},
        masters=two_masters,
        custom_parameters=[{"name": "Variable Font Origin", "value": "m02"}],
    )

    result = align_brace_layers_to_variable_origin(font, only_conflicts=False)

    assert result.target_master_id == "m02"
    assert result.moved == 1


def test_the_legacy_origin_parameter_is_resolved_by_master_name(two_masters):
    """"Variation Font Origin" names the master rather than giving its id."""
    font = make_font(
        {"a": [master_layer("m01"), master_layer("m02"), brace_layer("m01", (100,))]},
        masters=two_masters,
        custom_parameters=[{"name": "Variation Font Origin", "value": "Bold"}],
    )

    assert variable_font_origin_master_id(font) == "m02"
    assert align_brace_layers_to_variable_origin(font, only_conflicts=False).moved == 1


def test_without_an_origin_parameter_the_first_master_wins(two_masters):
    font = make_font({"a": [master_layer("m01")]}, masters=two_masters)
    assert variable_font_origin_master_id(font) == "m01"


def test_a_layer_that_cannot_be_classified_is_left_alone():
    """
    _is_brace_layer() is private and version-dependent, so an exception from
    it must not take the build down - the layer is simply not touched.
    """

    class Exploding:
        associatedMasterId = "m99"

        def _is_brace_layer(self):
            raise RuntimeError("glyphsLib internals moved")

    layer = Exploding()
    assert is_brace_layer(layer) is False
    assert layer.associatedMasterId == "m99"


def test_a_layer_without_the_private_method_is_left_alone():
    assert is_brace_layer(object()) is False


def test_distinct_locations_on_different_masters_are_left_alone(two_masters):
    """
    Brace layers spread across masters are perfectly healthy as long as each
    *location* belongs to one master - that produces no duplicate designspace
    source. Measured across eight retail families, six had brace layers on
    several masters and none had a conflicting location, so aligning them all
    would rewrite up to a hundred layers per family for nothing.
    """
    font = make_font(
        {
            "a": [master_layer("m01"), master_layer("m02"), brace_layer("m01", (80,))],
            "ae": [master_layer("m01"), master_layer("m02"), brace_layer("m02", (120,))],
        },
        masters=two_masters,
    )

    result = align_brace_layers_to_variable_origin(font)

    assert result.moved == 0
    assert result.conflicting_locations == 0
    associated = sorted(
        layer.associatedMasterId
        for glyph in font.glyphs
        for layer in glyph.layers
        if is_brace_layer(layer)
    )
    assert associated == ["m01", "m02"]


def test_only_the_conflicting_location_moves(two_masters):
    """A source with one bad location keeps its good ones where they are."""
    font = make_font(
        {
            "a": [master_layer("m01"), master_layer("m02"),
                  brace_layer("m02", (100,)), brace_layer("m02", (80,))],
            "ae": [master_layer("m01"), master_layer("m02"),
                   brace_layer("m01", (100,))],
        },
        masters=two_masters,
    )

    result = align_brace_layers_to_variable_origin(font)

    assert result.conflicting_locations == 1
    assert result.moved == 1                # only the {100} layer on 'a'
    layers = {
        (glyph.name, brace_coordinates(layer)): layer.associatedMasterId
        for glyph in font.glyphs
        for layer in glyph.layers
        if is_brace_layer(layer)
    }
    assert layers[("a", (100.0,))] == "m01"     # moved
    assert layers[("a", (80.0,))] == "m02"      # left alone
    assert layers[("ae", (100.0,))] == "m01"


def test_only_conflicts_false_moves_everything(two_masters):
    """The opt-out is what a Glyphs user does by hand when tidying a source."""
    font = make_font(
        {"a": [master_layer("m01"), master_layer("m02"), brace_layer("m02", (80,))]},
        masters=two_masters,
    )

    assert align_brace_layers_to_variable_origin(font).moved == 0
    assert align_brace_layers_to_variable_origin(font, only_conflicts=False).moved == 1


# -- the padding from the associated master --------------------------------
#
# builder/sources.py fills a short coordinate list up from the associated
# master, so the raw coordinates are NOT the location. Both directions below
# are invisible on a single-axis font, which is why these use two.

def test_short_coordinates_on_different_masters_are_not_a_conflict(two_axis_masters):
    """
    {80} on a Width 100 master and {80} on a Width 50 master are two distinct
    sources. Comparing raw coordinates would call them a conflict and merge
    them, which deletes a sparse master and changes what the font interpolates.
    """
    font = make_font(
        {
            "a": [master_layer("m01"), master_layer("m02"), brace_layer("m01", (80,))],
            "ae": [master_layer("m01"), master_layer("m02"), brace_layer("m02", (80,))],
        },
        masters=two_axis_masters,
        axes=TWO_AXES,
    )

    assert conflicting_locations(font) == {}

    result = align_brace_layers_to_variable_origin(font)

    assert result.moved == 0
    assert result.unresolved_glyphs == []


def test_a_short_and_a_full_coordinate_can_collide(two_axis_masters):
    """
    {80, 50} on m01 and {80} on m02 (whose Width is 50) land on the same
    location. Comparing raw coordinates would miss it and varLib would still
    fail with "Locations must be unique".
    """
    font = make_font(
        {
            "a": [master_layer("m01"), master_layer("m02"),
                  brace_layer("m01", (80, 50))],
            "ae": [master_layer("m01"), master_layer("m02"),
                   brace_layer("m02", (80,))],
        },
        masters=two_axis_masters,
        axes=TWO_AXES,
    )

    conflicts = conflicting_locations(font)

    assert list(conflicts) == [(80.0, 50.0)]
    assert conflicts[(80.0, 50.0)] == {"m01", "m02"}


def test_a_short_conflicting_layer_is_reported_not_moved(two_axis_masters):
    """
    The associated master supplies part of a short layer's location, so
    reassigning it would move the sparse master through the design space.
    That is a decision for a human, not for this code.
    """
    font = make_font(
        {
            "a": [master_layer("m01"), master_layer("m02"),
                  brace_layer("m01", (80, 50))],
            "ae": [master_layer("m01"), master_layer("m02"),
                   brace_layer("m02", (80,))],
        },
        masters=two_axis_masters,
        axes=TWO_AXES,
    )

    result = align_brace_layers_to_variable_origin(font)

    assert result.conflicting_locations == 1
    assert result.moved == 0                       # the m01 one is already there
    assert result.unresolved_glyphs == ["ae"]      # the short one stays put
    assert font.glyphs[1].layers[2].associatedMasterId == "m02"
    assert "cannot be realigned automatically" in result.summary()


def test_effective_location_pads_and_truncates():
    assert effective_location((80.0,), (50.0, 100.0), 2) == (80.0, 100.0)
    assert effective_location((80.0, 30.0), (50.0, 100.0), 2) == (80.0, 30.0)
    # glyphsLib warns and then truncates
    assert effective_location((80.0, 30.0, 10.0), (50.0, 100.0), 2) == (80.0, 30.0)


def test_summary_does_not_mention_conflicts_on_the_opt_out_path(two_masters):
    """only_conflicts=False never counts them, so it must not report a count."""
    font = make_font(
        {"a": [master_layer("m01"), master_layer("m02"), brace_layer("m02", (80,))]},
        masters=two_masters,
    )

    summary = align_brace_layers_to_variable_origin(font, only_conflicts=False).summary()

    assert "reassigned to master m01" in summary
    assert "conflicting" not in summary
