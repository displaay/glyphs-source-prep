"""The axis repairs a designspace needs before varLib or Instantiator reads it."""

from __future__ import annotations

from fontTools.designspaceLib import (
    AxisDescriptor,
    DesignSpaceDocument,
    SourceDescriptor,
)

from glyphs_source_prep import (
    ensure_default_master_document,
    ensure_designspace_default_master,
    master_sources,
    repair_collapsing_axis_maps,
    repair_collapsing_axis_maps_document,
)


def make_axis(name="Weight", tag="wght", minimum=100, default=400, maximum=900, mapping=None):
    axis = AxisDescriptor()
    axis.name = name
    axis.tag = tag
    axis.minimum = minimum
    axis.default = default
    axis.maximum = maximum
    axis.map = mapping or []
    return axis


def make_source(name, location, *, layer=None, copy_info=False, filename=None):
    source = SourceDescriptor()
    source.name = name
    source.filename = filename or f"{name}.ufo"
    source.location = location
    source.layerName = layer
    source.copyInfo = copy_info
    return source


def make_doc(axes, sources):
    doc = DesignSpaceDocument()
    for axis in axes:
        doc.addAxis(axis)
    for source in sources:
        doc.addSource(source)
    return doc


class TestMasterSources:
    def test_a_layer_source_is_not_a_master(self):
        doc = make_doc(
            [make_axis()],
            [
                make_source("Regular", {"Weight": 400}),
                make_source("Brace", {"Weight": 500}, layer="{500}"),
            ],
        )
        assert [s.name for s in master_sources(doc)] == ["Regular"]


class TestEnsureDefaultMaster:
    def test_a_document_that_already_has_a_master_default_is_untouched(self):
        doc = make_doc(
            [make_axis()],
            [
                make_source("Regular", {"Weight": 400}),
                make_source("Bold", {"Weight": 700}),
            ],
        )
        result = ensure_default_master_document(doc)
        assert not result.changed
        assert result.axes == []
        assert doc.axes[0].default == 400
        assert result.summary() == "designspace default already sits on a master"

    def test_a_default_with_no_master_is_rebased_onto_one(self):
        # Thin is the reference master, but the axis default sits at 400 where
        # no master is: Instantiator has nothing to interpolate from.
        doc = make_doc(
            [make_axis()],
            [
                make_source("Thin", {"Weight": 100}, copy_info=True),
                make_source("Bold", {"Weight": 700}),
            ],
        )
        doc.axes[0].default = 400
        result = ensure_default_master_document(doc)
        assert result.changed
        assert result.axes == ["Weight"]
        assert doc.axes[0].default == 100
        assert doc.findDefault() is not None
        assert doc.findDefault().name == "Thin"

    def test_a_brace_layer_cannot_serve_as_the_default(self):
        # A layer source at the default location holds only the glyphs that
        # differ there, so interpolating from it would drop the rest.
        doc = make_doc(
            [make_axis()],
            [
                make_source("Thin", {"Weight": 100}, copy_info=True),
                make_source("Brace", {"Weight": 400}, layer="{400}"),
                make_source("Bold", {"Weight": 700}),
            ],
        )
        result = ensure_default_master_document(doc)
        assert result.changed
        assert doc.findDefault().name == "Thin"

    def test_a_collapsed_map_gains_an_entry_for_every_master(self):
        # The map only names the two instances that export, so the master in
        # between is unreachable through it, and the axis default sits at 200
        # where there is no master at all.
        doc = make_doc(
            [make_axis(mapping=[(100, 100), (900, 900)])],
            [
                make_source("Thin", {"Weight": 100}, copy_info=True),
                make_source("Regular", {"Weight": 400}),
                make_source("Bold", {"Weight": 900}),
            ],
        )
        doc.axes[0].default = 200
        result = ensure_default_master_document(doc)
        assert result.changed
        assert (400.0, 400.0) in doc.axes[0].map
        assert doc.axes[0].default == 100
        assert doc.findDefault().name == "Thin"

    def test_an_empty_document_is_left_alone(self):
        doc = make_doc([make_axis()], [])
        result = ensure_default_master_document(doc)
        assert not result.changed

    def test_the_file_form_only_writes_when_something_changed(self, tmp_path):
        doc = make_doc(
            [make_axis()],
            [
                make_source("Regular", {"Weight": 400}),
                make_source("Bold", {"Weight": 700}),
            ],
        )
        path = tmp_path / "test.designspace"
        doc.write(str(path))
        before = path.read_bytes()
        result = ensure_designspace_default_master(path)
        assert not result.changed
        assert path.read_bytes() == before

    def test_the_file_form_writes_the_rebase(self, tmp_path):
        doc = make_doc(
            [make_axis()],
            [
                make_source("Thin", {"Weight": 100}, copy_info=True),
                make_source("Bold", {"Weight": 700}),
            ],
        )
        doc.axes[0].default = 400
        path = tmp_path / "test.designspace"
        doc.write(str(path))
        result = ensure_designspace_default_master(path)
        assert result.changed
        assert DesignSpaceDocument.fromfile(str(path)).axes[0].default == 100

    def test_a_document_with_no_full_master_is_left_untouched(self):
        # Only layer sources: there is nothing to rebase onto, so the axes must
        # come out exactly as they went in. A caller holding the document has
        # no file to reload from, and a half-rebased design space is worse than
        # the state that failed the build.
        #
        # This covers the early return. The rollback further down, for a rebase
        # that runs and then does not land, is defensive - no document could be
        # constructed that reaches it.
        doc = make_doc(
            [make_axis(mapping=[(100, 100), (900, 900)])],
            [
                make_source("Brace A", {"Weight": 100}, layer="{100}"),
                make_source("Brace B", {"Weight": 900}, layer="{900}"),
            ],
        )
        doc.axes[0].default = 200
        before = (
            list(doc.axes[0].map),
            doc.axes[0].minimum,
            doc.axes[0].maximum,
            doc.axes[0].default,
        )
        result = ensure_default_master_document(doc)
        assert not result.changed
        assert result.axes == []
        assert (
            list(doc.axes[0].map),
            doc.axes[0].minimum,
            doc.axes[0].maximum,
            doc.axes[0].default,
        ) == before


class TestRepairCollapsingAxisMaps:
    def test_a_map_that_collapses_the_minimum_onto_the_default(self):
        # wdth 50 and 75 both land on design 50: varLib needs the minimum at
        # normalized -1 and gets 0.
        axis = make_axis(
            name="Width", tag="wdth", minimum=50, default=75, maximum=100,
            mapping=[(50, 50), (75, 50), (100, 100)],
        )
        doc = make_doc([axis], [make_source("Regular", {"Width": 50})])
        result = repair_collapsing_axis_maps_document(doc)
        assert result.axes == ["Width"]
        assert doc.axes[0].default == 50
        assert doc.axes[0].map == [(50.0, 50.0), (100.0, 100.0)]

    def test_a_map_that_collapses_the_maximum_onto_the_default(self):
        axis = make_axis(
            name="Width", tag="wdth", minimum=50, default=75, maximum=100,
            mapping=[(50, 50), (75, 100), (100, 100)],
        )
        doc = make_doc([axis], [make_source("Regular", {"Width": 100})])
        result = repair_collapsing_axis_maps_document(doc)
        assert result.axes == ["Width"]
        assert doc.axes[0].default == 100
        assert doc.axes[0].map == [(50.0, 50.0), (100.0, 100.0)]

    def test_a_healthy_map_is_left_alone(self):
        axis = make_axis(mapping=[(100, 100), (400, 400), (900, 900)])
        doc = make_doc([axis], [make_source("Regular", {"Weight": 400})])
        result = repair_collapsing_axis_maps_document(doc)
        assert result.axes == []
        assert result.repaired == 0
        assert doc.axes[0].default == 400
        assert result.summary() == (
            "no axis map collapses an endpoint onto the default"
        )

    def test_an_axis_with_no_map_is_left_alone(self):
        doc = make_doc([make_axis()], [make_source("Regular", {"Weight": 400})])
        assert repair_collapsing_axis_maps_document(doc).axes == []

    def test_the_default_already_at_the_minimum_is_not_a_collapse(self):
        axis = make_axis(
            name="Width", tag="wdth", minimum=50, default=50, maximum=100,
            mapping=[(50, 50), (100, 100)],
        )
        doc = make_doc([axis], [make_source("Regular", {"Width": 50})])
        assert repair_collapsing_axis_maps_document(doc).axes == []

    def test_the_summary_names_the_axes(self):
        axis = make_axis(
            name="Width", tag="wdth", minimum=50, default=75, maximum=100,
            mapping=[(50, 50), (75, 50), (100, 100)],
        )
        doc = make_doc([axis], [make_source("Regular", {"Width": 50})])
        result = repair_collapsing_axis_maps_document(doc)
        assert "Width" in result.summary()
        assert "moved onto the endpoint" in result.summary()

    def test_the_file_form_writes_only_a_repair(self, tmp_path):
        axis = make_axis(mapping=[(100, 100), (400, 400), (900, 900)])
        doc = make_doc([axis], [make_source("Regular", {"Weight": 400})])
        path = tmp_path / "test.designspace"
        doc.write(str(path))
        before = path.read_bytes()
        assert repair_collapsing_axis_maps(path).axes == []
        assert path.read_bytes() == before
