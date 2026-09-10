"""Giving a mono master the kerning it was drawn without."""

from __future__ import annotations

import pytest
from fontTools.designspaceLib import (
    AxisDescriptor,
    DesignSpaceDocument,
    SourceDescriptor,
)

from glyphs_source_prep import (
    inherit_empty_master_kerning,
    inherit_empty_master_kerning_document,
    is_mono_axis,
)

ufoLib2 = pytest.importorskip("ufoLib2")

PAIRS = {("A", "V"): -40, ("T", "o"): -30}


def make_ufo(kerning=None):
    font = ufoLib2.Font()
    if kerning:
        font.kerning.update(kerning)
    return font


def make_source(name, location, kerning=None, *, layer=None):
    source = SourceDescriptor()
    source.name = name
    source.filename = f"{name}.ufo"
    source.location = location
    source.layerName = layer
    source.font = make_ufo(kerning)
    return source


def make_doc(sources, axis_names=("Weight", "MONO")):
    doc = DesignSpaceDocument()
    for index, name in enumerate(axis_names):
        axis = AxisDescriptor()
        axis.name = name
        axis.tag = "wght" if name == "Weight" else "MONO"
        axis.minimum = 0
        axis.default = 0
        axis.maximum = 900 if name == "Weight" else 100
        doc.addAxis(axis)
    for source in sources:
        doc.addSource(source)
    return doc


class TestIsMonoAxis:
    @pytest.mark.parametrize("name", ["MONO", "mono", "Mono", " MONO "])
    def test_the_mono_axis_is_recognised_whatever_its_case(self, name):
        # Asked of a designspace it is an axis name, of an fvar table a tag.
        assert is_mono_axis(name)

    @pytest.mark.parametrize("name", ["Weight", "Width", "wght", "", None])
    def test_nothing_else_is(self, name):
        assert not is_mono_axis(name)


class TestInheritDocument:
    def test_a_mono_master_takes_the_proportional_master_kerning(self):
        doc = make_doc([
            make_source("Regular", {"Weight": 400, "MONO": 0}, PAIRS),
            make_source("Mono", {"Weight": 400, "MONO": 100}),
        ])
        result = inherit_empty_master_kerning_document(doc)
        assert result.copied == 1
        assert result.masters == ["Mono"]
        assert dict(doc.sources[1].font.kerning) == PAIRS

    def test_a_master_that_has_its_own_kerning_is_left_alone(self):
        own = {("A", "V"): -10}
        doc = make_doc([
            make_source("Regular", {"Weight": 400, "MONO": 0}, PAIRS),
            make_source("Mono", {"Weight": 400, "MONO": 100}, own),
        ])
        result = inherit_empty_master_kerning_document(doc)
        assert result.copied == 0
        assert dict(doc.sources[1].font.kerning) == own

    def test_the_donor_has_to_match_on_every_other_axis(self):
        # The Bold mono master must not inherit the Regular's kerning.
        doc = make_doc([
            make_source("Regular", {"Weight": 400, "MONO": 0}, PAIRS),
            make_source("Bold Mono", {"Weight": 900, "MONO": 100}),
        ])
        result = inherit_empty_master_kerning_document(doc)
        assert result.copied == 0
        assert result.without_donor == ["Bold Mono"]
        assert not doc.sources[1].font.kerning

    def test_the_sibling_nearest_mono_zero_wins(self):
        near = {("A", "V"): -40}
        far = {("A", "V"): -99}
        doc = make_doc([
            make_source("Prop", {"Weight": 400, "MONO": 0}, near),
            make_source("Half", {"Weight": 400, "MONO": 50}, far),
            make_source("Mono", {"Weight": 400, "MONO": 100}),
        ])
        result = inherit_empty_master_kerning_document(doc)
        assert result.copied == 1
        assert dict(doc.sources[2].font.kerning) == near

    def test_a_family_with_no_mono_axis_is_left_alone(self):
        doc = make_doc(
            [
                make_source("Regular", {"Weight": 400}, PAIRS),
                make_source("Bold", {"Weight": 900}),
            ],
            axis_names=("Weight",),
        )
        result = inherit_empty_master_kerning_document(doc)
        assert result.copied == 0
        assert result.without_donor == []

    def test_a_layer_source_is_not_a_master(self):
        doc = make_doc([
            make_source("Regular", {"Weight": 400, "MONO": 0}, PAIRS),
            make_source("Brace", {"Weight": 500, "MONO": 0}, layer="{500}"),
        ])
        assert inherit_empty_master_kerning_document(doc).copied == 0

    def test_a_source_with_no_loaded_ufo_is_skipped(self):
        doc = make_doc([
            make_source("Regular", {"Weight": 400, "MONO": 0}, PAIRS),
            make_source("Mono", {"Weight": 400, "MONO": 100}),
        ])
        doc.sources[1].font = None
        assert inherit_empty_master_kerning_document(doc).copied == 0

    def test_the_summary_reads_as_a_log_line(self):
        doc = make_doc([
            make_source("Regular", {"Weight": 400, "MONO": 0}, PAIRS),
            make_source("Mono", {"Weight": 400, "MONO": 100}),
        ])
        result = inherit_empty_master_kerning_document(doc)
        assert "Mono" in result.summary()
        assert "inherited it" in result.summary()

    def test_the_summary_of_nothing_says_so(self):
        doc = make_doc([make_source("Regular", {"Weight": 400, "MONO": 0}, PAIRS)])
        result = inherit_empty_master_kerning_document(doc)
        assert result.summary() == "every master carries its own kerning"

    def test_a_master_with_no_donor_is_named_not_repaired(self):
        doc = make_doc([
            make_source("Mono", {"Weight": 400, "MONO": 100}),
            make_source("Bold Mono", {"Weight": 900, "MONO": 100}),
        ])
        result = inherit_empty_master_kerning_document(doc)
        assert result.copied == 0
        assert sorted(result.without_donor) == ["Bold Mono", "Mono"]
        assert "interpolate to none" in result.summary()


class TestInheritFile:
    def write(self, tmp_path, sources):
        doc = make_doc(sources)
        for source in doc.sources:
            path = tmp_path / source.filename
            source.font.save(str(path), overwrite=True)
            source.font = None
            source.path = str(path)
        designspace = tmp_path / "test.designspace"
        doc.write(str(designspace))
        return designspace

    def test_the_repaired_ufo_is_written_back(self, tmp_path):
        designspace = self.write(tmp_path, [
            make_source("Regular", {"Weight": 400, "MONO": 0}, PAIRS),
            make_source("Mono", {"Weight": 400, "MONO": 100}),
        ])
        result = inherit_empty_master_kerning(designspace)
        assert result.copied == 1
        reopened = ufoLib2.Font.open(str(tmp_path / "Mono.ufo"))
        assert dict(reopened.kerning) == PAIRS

    def test_nothing_is_written_when_nothing_inherited(self, tmp_path):
        designspace = self.write(tmp_path, [
            make_source("Regular", {"Weight": 400, "MONO": 0}, PAIRS),
            make_source("Bold", {"Weight": 900, "MONO": 0}, PAIRS),
        ])
        assert inherit_empty_master_kerning(designspace).copied == 0
