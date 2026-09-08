"""
Unit tests of the brace layer alignment.
"""
from conftest import brace_layer, make_font, master_layer

from glyphs_source_prep import (
    align_brace_layers_to_variable_origin,
    is_brace_layer,
    variable_font_origin_master_id,
)


def test_a_brace_layer_on_another_master_is_reassigned(two_masters):
    """The case varLib rejects: same location, different associated master."""
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

    result = align_brace_layers_to_variable_origin(font)

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
    assert align_brace_layers_to_variable_origin(font).moved == 1


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
