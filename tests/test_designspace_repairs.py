"""The axis repairs a designspace needs before varLib or Instantiator reads it."""

from __future__ import annotations

import pytest
from fontTools.designspaceLib import (
    AxisDescriptor,
    DesignSpaceDocument,
    SourceDescriptor,
)

from glyphs_source_prep import (
    ensure_default_master_document,
    ensure_designspace_default_master,
    extend_axis_maps_to_masters,
    extend_axis_maps_to_masters_document,
    master_sources,
    repair_collapsing_axis_maps,
    repair_collapsing_axis_maps_document,
    repair_inverted_axis_maps,
    repair_inverted_axis_maps_document,
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


class _Params(dict):
    """A missing custom parameter is absent, matching GSCustomParameter."""

    def __missing__(self, key):
        return None


class _Axis:
    def __init__(self, name, tag):
        self.name = name
        self.axisTag = tag


class _Master:
    def __init__(self, axis_location=None, axis_name="Width"):
        params = _Params()
        if axis_location is not None:
            params["Axis Location"] = [{"Axis": axis_name, "Location": axis_location}]
        self.customParameters = params


class _Instance:
    def __init__(
        self,
        design,
        width=None,
        *,
        weight=None,
        axis_location=None,
        axis_name="Width",
        name="Instance",
        type_=0,
        axes=None,
    ):
        self.axes = [design] if axes is None else axes
        self.width = width
        self.weight = weight
        self.exports = False
        self.active = False
        self.name = name
        self.type = type_
        params = _Params()
        if axis_location is not None:
            params["Axis Location"] = [{"Axis": axis_name, "Location": axis_location}]
        self.customParameters = params


class _Font:
    def __init__(self, instances, *, axes=None, masters=None, axis_mappings=None):
        self.axes = axes if axes is not None else [_Axis("Width", "wdth")]
        self.instances = instances
        self.masters = masters if masters is not None else [_Master(), _Master()]
        params = _Params()
        if axis_mappings is not None:
            params["Axis Mappings"] = axis_mappings
        self.customParameters = params


def _width_doc(mapping, sources, **bounds):
    axis = make_axis(
        name="Width",
        tag="wdth",
        minimum=bounds.get("minimum", 75),
        default=bounds.get("default", 75),
        maximum=bounds.get("maximum", 100),
        mapping=mapping,
    )
    return make_doc([axis], sources)


class TestExtendAxisMapsToMasters:
    def test_truncated_width_map_uses_inactive_width_classes(self):
        doc = _width_doc(
            [(75, 75), (87.5, 89), (100, 100)],
            [
                make_source("Condensed", {"Width": 75}, copy_info=True),
                make_source("Wide", {"Width": 115}),
                make_source("Extended", {"Width": 130}),
            ],
        )
        font = _Font([_Instance(115, 6, name="Wide"), _Instance(130, 7, name="Extended")])
        result = extend_axis_maps_to_masters_document(doc, font)
        width = doc.axes[0]
        assert result.axes == ["Width"]
        assert result.extrapolated == []
        assert result.points == [
            ("Width", 112.5, 115.0),
            ("Width", 125.0, 130.0),
        ]
        assert float(width.maximum) == 125.0
        assert list(width.map) == [
            (75.0, 75.0),
            (87.5, 89.0),
            (100.0, 100.0),
            (112.5, 115.0),
            (125.0, 130.0),
        ]
        assert "Width" in result.summary()
        assert "extrapolated" not in result.summary()

    def test_width_and_weight_names_use_the_glyphs_class_tables(self):
        """GSInstance.width / .weight are names, not OS/2 class numbers."""
        width = _width_doc(
            [(75, 75), (100, 100)],
            [
                make_source("Condensed", {"Width": 75}, copy_info=True),
                make_source("Expanded", {"Width": 130}),
            ],
        )
        result = extend_axis_maps_to_masters_document(
            width, _Font([_Instance(130, "Expanded", name="Expanded")])
        )
        assert result.points == [("Width", 125.0, 130.0)]
        assert result.extrapolated == []

        weight = make_doc(
            [
                make_axis(
                    name="Weight",
                    tag="wght",
                    minimum=400,
                    default=400,
                    maximum=700,
                    mapping=[(400, 80), (700, 180)],
                )
            ],
            [
                make_source("Regular", {"Weight": 80}, copy_info=True),
                make_source("Black", {"Weight": 220}),
            ],
        )
        font = _Font(
            [_Instance(220, weight="Black", name="Black")],
            axes=[_Axis("Weight", "wght")],
        )
        result = extend_axis_maps_to_masters_document(weight, font)
        assert result.points == [("Weight", 900.0, 220.0)]
        assert result.extrapolated == []

    def test_a_spaced_width_name_matches_the_unspaced_table_key(self):
        doc = _width_doc(
            [(75, 75), (100, 100)],
            [make_source("Wide", {"Width": 115})],
        )
        result = extend_axis_maps_to_masters_document(
            doc, _Font([_Instance(115, "Semi Expanded", name="Wide")])
        )
        assert result.points == [("Width", 112.5, 115.0)]

    def test_one_stale_instance_does_not_drop_its_sibling(self):
        """Extended still on width class 5 must not veto Wide at class 6."""
        doc = _width_doc(
            [(75, 75), (87.5, 89), (100, 100)],
            [
                make_source("Condensed", {"Width": 75}, copy_info=True),
                make_source("Wide", {"Width": 115}),
                make_source("Extended", {"Width": 130}),
            ],
        )
        font = _Font(
            [
                _Instance(130, 5, name="Extended"),
                _Instance(115, 6, name="Wide"),
            ]
        )
        result = extend_axis_maps_to_masters_document(doc, font)
        assert ("Width", 112.5, 115.0) in result.points
        assert result.axes == ["Width"]
        assert any("Extended" in line for line in result.skipped)
        assert list(doc.axes[0].map)[-2:] == [(112.5, 115.0), (125.0, 130.0)]

    def test_a_near_duplicate_user_location_is_not_a_second_key(self):
        doc = _width_doc(
            [(75, 75), (100, 100)],
            [make_source("Wide", {"Width": 115})],
        )
        font = _Font(
            [
                _Instance(115, name="Wide", axis_location=112.5),
                _Instance(115, name="Wide again", axis_location=112.5 + 4e-7),
            ]
        )
        result = extend_axis_maps_to_masters_document(doc, font)
        assert result.points == [("Width", 112.5, 115.0)]
        assert any("already on the map" in line for line in result.skipped)
        assert len(doc.axes[0].map) == 3

    def test_a_variable_instance_is_skipped_without_reading_its_axes(self):
        doc = _width_doc(
            [(75, 75), (100, 100)],
            [make_source("Extended", {"Width": 130})],
        )
        font = _Font([_Instance(None, name="VF", type_=1, axes=[None])])
        result = extend_axis_maps_to_masters_document(doc, font)
        assert result.axes == ["Width"]
        assert result.extrapolated == ["Width"]
        assert result.skipped == []

    def test_extrapolates_along_the_end_segment_when_no_font_is_available(self):
        # (400->80), (900->220), master at design 400. Slope 0.28 continues
        # to user 900 + (400 - 220) / 0.28, not the constant offset 1080.
        doc = make_doc(
            [
                make_axis(
                    name="Weight",
                    tag="wght",
                    minimum=400,
                    default=400,
                    maximum=900,
                    mapping=[(400, 80), (900, 220)],
                )
            ],
            [make_source("Black", {"Weight": 400})],
        )
        result = extend_axis_maps_to_masters_document(doc, font=None)
        assert result.axes == ["Weight"]
        assert result.extrapolated == ["Weight"]
        user, design = result.points[0][1], result.points[0][2]
        assert design == 400.0
        assert user == pytest.approx(1542.857142)
        assert float(doc.axes[0].maximum) == pytest.approx(1542.857142)
        assert "(extrapolated)" in result.summary()

    def test_a_map_that_already_covers_masters_is_untouched(self, tmp_path):
        doc = _width_doc(
            [(75, 75), (100, 100), (112.5, 115), (125, 130)],
            [
                make_source("Condensed", {"Width": 75}),
                make_source("Extended", {"Width": 130}),
            ],
            default=100,
            maximum=125,
        )
        path = tmp_path / "test.designspace"
        doc.write(str(path))
        before = path.read_bytes()
        result = extend_axis_maps_to_masters(path, font=None)
        assert result.axes == []
        assert result.points == []
        assert path.read_bytes() == before
        assert result.summary() == "every master is already inside the axis range"

    def test_an_axis_mappings_parameter_is_left_alone(self):
        doc = _width_doc(
            [(75, 75), (100, 100)],
            [make_source("Extended", {"Width": 130})],
        )
        before = list(doc.axes[0].map)
        font = _Font(
            [_Instance(130, 7, name="Extended")],
            axis_mappings={"wdth": []},
        )
        result = extend_axis_maps_to_masters_document(doc, font)
        assert result.axes == []
        assert list(doc.axes[0].map) == before

    def test_a_source_that_omits_the_axis_does_not_invent_a_zero(self):
        weight = make_axis(name="Weight", tag="wght", minimum=100, default=400, maximum=900)
        width = make_axis(
            name="Width",
            tag="wdth",
            minimum=75,
            default=75,
            maximum=100,
            mapping=[(75, 75), (87.5, 89), (100, 100)],
        )
        doc = make_doc(
            [weight, width],
            [
                make_source("Condensed", {"Weight": 400}),
                make_source("Extended", {"Weight": 400, "Width": 130}),
            ],
        )
        result = extend_axis_maps_to_masters_document(doc, _Font([_Instance(130, 7)]))
        assert result.points == [("Width", 125.0, 130.0)]
        assert (0.0, 0.0) not in width.map

    def test_extend_repairs_an_inverted_map_before_extrapolating(self):
        doc = _width_doc(
            [(50, 150), (75, 50), (100, 100), (125, 150)],
            [
                make_source("Condensed", {"Width": 50}, copy_info=True),
                make_source("Wide", {"Width": 150}),
                make_source("Ultra", {"Width": 200}),
            ],
            minimum=50,
            maximum=125,
        )
        result = extend_axis_maps_to_masters_document(doc, font=None)
        assert list(doc.axes[0].map) == [
            (75.0, 50.0),
            (100.0, 100.0),
            (125.0, 150.0),
            (150.0, 200.0),
        ]
        assert result.points == [("Width", 150.0, 200.0)]
        assert result.extrapolated == ["Width"]


def _reckless_width(**overrides):
    values = {
        "minimum": 50,
        "default": 75,
        "maximum": 125,
        "mapping": [(50, 150), (75, 50), (100, 100), (125, 150)],
    }
    values.update(overrides)
    return make_axis(name="Width", tag="wdth", **values)


class TestRepairInvertedAxisMaps:
    def test_reckless_italic_width_keeps_the_increasing_run(self, tmp_path):
        doc = make_doc(
            [_reckless_width()],
            [
                make_source("Condensed", {"Width": 50}, copy_info=True),
                make_source("Wide", {"Width": 150}),
            ],
        )
        axis = doc.axes[0]
        assert float(axis.map_forward(axis.minimum)) > float(axis.map_forward(axis.default))

        result = repair_inverted_axis_maps_document(doc)
        assert result.axes == ["Width"]
        assert "Width" in result.summary()
        design_triple = (
            float(axis.map_forward(axis.minimum)),
            float(axis.map_forward(axis.default)),
            float(axis.map_forward(axis.maximum)),
        )
        assert design_triple == (50.0, 50.0, 150.0)
        assert list(axis.map) == [(75.0, 50.0), (100.0, 100.0), (125.0, 150.0)]
        assert float(axis.minimum) == 75.0
        assert float(axis.default) == 75.0
        assert float(axis.maximum) == 125.0

        path = tmp_path / "Reckless-Italic.designspace"
        doc = make_doc(
            [_reckless_width()],
            [
                make_source("Condensed", {"Width": 50}, copy_info=True),
                make_source("Wide", {"Width": 150}),
            ],
        )
        doc.write(str(path))
        assert repair_inverted_axis_maps(path).axes == ["Width"]
        assert repair_inverted_axis_maps(path).axes == []
        reloaded = DesignSpaceDocument.fromfile(str(path))
        assert list(reloaded.axes[0].map) == [
            (75.0, 50.0),
            (100.0, 100.0),
            (125.0, 150.0),
        ]

    def test_a_monotonic_map_is_left_alone(self, tmp_path):
        doc = make_doc(
            [
                make_axis(
                    name="Width",
                    tag="wdth",
                    minimum=75,
                    default=75,
                    maximum=125,
                    mapping=[(75, 50), (100, 100), (125, 150)],
                )
            ],
            [],
        )
        path = tmp_path / "Reckless.designspace"
        doc.write(str(path))
        before = path.read_bytes()
        assert repair_inverted_axis_maps(path).axes == []
        assert path.read_bytes() == before
        assert repair_inverted_axis_maps_document(doc).summary() == "no axis map decreases"

    def test_a_flat_run_is_left_for_the_collapsing_repair(self):
        doc = make_doc(
            [
                make_axis(
                    name="Width",
                    tag="wdth",
                    minimum=50,
                    default=75,
                    maximum=150,
                    mapping=[
                        (50, 50),
                        (75, 50),
                        (100, 100),
                        (125, 150),
                        (150, 150),
                    ],
                )
            ],
            [make_source("Condensed", {"Width": 50})],
        )
        assert repair_inverted_axis_maps_document(doc).axes == []
        assert repair_collapsing_axis_maps_document(doc).axes == ["Width"]

    def test_a_trailing_decrease_loses_to_the_increasing_run(self):
        doc = make_doc(
            [
                make_axis(
                    name="Width",
                    tag="wdth",
                    minimum=0,
                    default=0,
                    maximum=100,
                    mapping=[(0, 0), (50, 50), (100, 0)],
                )
            ],
            [
                make_source("Condensed", {"Width": 0}, copy_info=True),
                make_source("Wide", {"Width": 50}),
            ],
        )
        result = repair_inverted_axis_maps_document(doc)
        assert result.axes == ["Width"]
        axis = doc.axes[0]
        assert list(axis.map) == [(0.0, 0.0), (50.0, 50.0)]
        assert float(axis.minimum) == 0.0
        assert float(axis.maximum) == 50.0

    def test_extend_persists_the_inverted_repair_when_no_point_is_added(self, tmp_path):
        doc = make_doc(
            [_reckless_width()],
            [
                make_source("Condensed", {"Width": 50}, copy_info=True),
                make_source("Wide", {"Width": 150}),
            ],
        )
        path = tmp_path / "Reckless-Italic.designspace"
        doc.write(str(path))
        result = extend_axis_maps_to_masters(path, font=None)
        assert result.axes == []
        reloaded = DesignSpaceDocument.fromfile(str(path))
        assert list(reloaded.axes[0].map) == [
            (75.0, 50.0),
            (100.0, 100.0),
            (125.0, 150.0),
        ]
