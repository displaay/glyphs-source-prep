"""
Unit tests of the dangling background component cleanup.
"""
from conftest import PATH, make_font, master_layer

from glyphs_source_prep import drop_dangling_background_components


def font(glyphs):
    return make_font({name: [layer] for (name, layer) in glyphs.items()})


def layer(shapes, background=None):
    return master_layer(shapes=shapes, background=background)


def test_a_dangling_background_component_is_dropped():
    f = font({"A": layer([PATH], background=[{"ref": "A.old"}])})

    result = drop_dangling_background_components(f)

    assert result.dropped == 1
    assert result.glyphs == ["A"]
    assert result.missing_refs == {"A.old": 1}
    assert list(f.glyphs[0].layers[0].background.components) == []
    assert "A.old" in result.summary()
    assert "A" in result.summary()


def test_the_rest_of_the_background_survives():
    """Only the offending component goes, not the background layer."""
    f = font({"A": layer([PATH], background=[{"ref": "A.old"}, PATH])})

    drop_dangling_background_components(f)

    background = f.glyphs[0].layers[0].background
    assert list(background.components) == []
    assert len(background.paths) == 1


def test_a_resolvable_background_component_is_kept():
    f = font({
        "A": layer([PATH], background=[{"ref": "B"}]),
        "B": layer([PATH]),
    })

    result = drop_dangling_background_components(f)

    assert result.dropped == 0
    assert len(f.glyphs[0].layers[0].background.components) == 1


def test_a_dangling_component_in_the_drawing_layer_is_kept():
    """
    That one does change the output, so it must keep failing further down the
    pipeline - masking it would ship a font with a missing glyph.
    """
    f = font({"A": layer([{"ref": "A.old"}])})

    result = drop_dangling_background_components(f)

    assert result.dropped == 0
    assert len(f.glyphs[0].layers[0].components) == 1


def test_a_font_without_backgrounds_is_untouched():
    f = font({"A": layer([PATH])})

    result = drop_dangling_background_components(f)

    assert result.dropped == 0
    assert result.glyphs == []
    # and no empty background layer was created on the way
    assert not f.glyphs[0].layers[0].hasBackground


def test_several_glyphs_are_counted_once_each():
    f = font({
        "A": layer([PATH], background=[{"ref": "A.old"}, {"ref": "A.old"}]),
        "B": layer([PATH], background=[{"ref": "B.old"}]),
    })

    result = drop_dangling_background_components(f)

    assert result.dropped == 3
    assert sorted(result.glyphs) == ["A", "B"]
    assert result.missing_refs == {"A.old": 2, "B.old": 1}
