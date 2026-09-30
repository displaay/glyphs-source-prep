"""The axis repairs a designspace needs before varLib or Instantiator reads it."""

from __future__ import annotations

import pytest
from fontTools.designspaceLib import (
    AxisDescriptor,
    AxisLabelDescriptor,
    DesignSpaceDocument,
    DiscreteAxisDescriptor,
    InstanceDescriptor,
    LocationLabelDescriptor,
    RangeAxisSubsetDescriptor,
    SourceDescriptor,
    VariableFontDescriptor,
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
    reset_axis_maps_to_design,
    reset_axis_maps_to_design_document,
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


def make_italic_doc():
    """A designspace 5 document whose only axis is discrete."""
    italic = DiscreteAxisDescriptor()
    italic.name = "Italic"
    italic.tag = "ital"
    italic.values = [0, 1]
    italic.default = 0
    return make_doc(
        [italic],
        [make_source("Roman", {"Italic": 0}), make_source("Italic", {"Italic": 1})],
    )


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
        # (400->80), (600->220), master at design 290. Slope 0.7 continues to
        # user 600 + (290 - 220) / 0.7, not the constant offset 670.
        doc = make_doc(
            [
                make_axis(
                    name="Weight",
                    tag="wght",
                    minimum=400,
                    default=400,
                    maximum=600,
                    mapping=[(400, 80), (600, 220)],
                )
            ],
            [make_source("Black", {"Weight": 290})],
        )
        result = extend_axis_maps_to_masters_document(doc, font=None)
        assert result.axes == ["Weight"]
        assert result.extrapolated == ["Weight"]
        user, design = result.points[0][1], result.points[0][2]
        assert design == 290.0
        assert user == pytest.approx(700.0)
        assert float(doc.axes[0].maximum) == pytest.approx(700.0)
        assert "(extrapolated)" in result.summary()

    def test_an_extrapolated_weight_stays_inside_the_registered_range(self):
        # The slope would put the master at user 1542; wght ends at 1000.
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
        assert result.points == [("Weight", 1000.0, 400.0)]
        assert float(doc.axes[0].maximum) == 1000.0

    def test_a_weight_map_that_already_ends_at_1000_is_not_extended(self):
        doc = make_doc(
            [
                make_axis(
                    name="Weight",
                    tag="wght",
                    minimum=400,
                    default=400,
                    maximum=1000,
                    mapping=[(400, 80), (1000, 220)],
                )
            ],
            [make_source("Black", {"Weight": 400})],
        )
        before = list(doc.axes[0].map)
        result = extend_axis_maps_to_masters_document(doc, font=None)
        assert result.axes == []
        assert list(doc.axes[0].map) == before
        assert "leaves the valid wght range" in result.skipped[0]

    def test_only_the_extrapolated_point_is_marked(self):
        # Wide comes from an inactive instance, Extended from the slope.
        doc = _width_doc(
            [(75, 75), (87.5, 89), (100, 100)],
            [
                make_source("Condensed", {"Width": 75}, copy_info=True),
                make_source("Wide", {"Width": 115}),
                make_source("Extended", {"Width": 130}),
            ],
        )
        result = extend_axis_maps_to_masters_document(
            doc, _Font([_Instance(115, 6, name="Wide")])
        )
        assert result.points[0] == ("Width", 112.5, 115.0)
        assert result.extrapolated_points == [result.points[1]]
        assert result.summary().count("(extrapolated)") == 1
        assert "112.5->115," in result.summary()

    def test_the_summary_is_ascii(self):
        doc = _width_doc(
            [(75, 75), (87.5, 89), (100, 100)],
            [make_source("Extended", {"Width": 130})],
        )
        result = extend_axis_maps_to_masters_document(doc, font=None)
        assert result.axes == ["Width"]
        result.summary().encode("ascii")
        for line in result.skipped:
            line.encode("ascii")

    def test_declared_bounds_outside_the_map_keys_are_preserved(self):
        # minimum 50 lies below the first map key: extending must not shrink it.
        doc = _width_doc(
            [(75, 75), (100, 100)],
            [
                make_source("Condensed", {"Width": 75}, copy_info=True),
                make_source("Extended", {"Width": 130}),
            ],
            minimum=50,
        )
        result = extend_axis_maps_to_masters_document(
            doc, _Font([_Instance(130, 7, name="Extended")])
        )
        assert result.points == [("Width", 125.0, 130.0)]
        assert float(doc.axes[0].minimum) == 50.0
        assert float(doc.axes[0].maximum) == 125.0

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
        assert result.inverted == ["Width"]

    def test_the_document_form_reports_an_inverted_repair_that_adds_no_point(self):
        doc = make_doc(
            [_reckless_width()],
            [
                make_source("Condensed", {"Width": 50}, copy_info=True),
                make_source("Wide", {"Width": 150}),
            ],
        )
        result = extend_axis_maps_to_masters_document(doc, font=None)
        assert result.axes == []
        assert result.inverted == ["Width"]
        assert result.repaired == 1
        assert "Width" in result.summary()
        result.summary().encode("ascii")

    def test_an_unmapped_axis_stays_an_identity(self):
        # Stem units on an axis with no map: a weight class of 700 would
        # squeeze user 160..700 into twenty design units.
        doc = make_doc(
            [make_axis(minimum=100, default=100, maximum=160)],
            [
                make_source("Light", {"Weight": 100}, copy_info=True),
                make_source("Bold", {"Weight": 180}),
            ],
        )
        font = _Font(
            [_Instance(180, weight="Bold", name="Bold")],
            axes=[_Axis("Weight", "wght")],
        )
        result = extend_axis_maps_to_masters_document(doc, font)
        weight = doc.axes[0]
        assert result.points == [("Weight", 180.0, 180.0)]
        assert result.extrapolated == []
        assert result.extrapolated_points == []
        assert list(weight.map) == []
        assert float(weight.maximum) == 180.0

    def test_an_instance_past_the_outermost_master_does_not_end_the_axis(self):
        doc = _width_doc(
            [(75, 75), (100, 100)],
            [
                make_source("Condensed", {"Width": 75}, copy_info=True),
                make_source("Extended", {"Width": 130}),
            ],
        )
        font = _Font(
            [_Instance(130, 7, name="Extended"), _Instance(150, 9, name="Ultra")]
        )
        result = extend_axis_maps_to_masters_document(doc, font)
        assert result.points == [("Width", 125.0, 130.0)]
        assert float(doc.axes[0].maximum) == 125.0
        assert any("Ultra" in line and "outermost master" in line for line in result.skipped)

    def test_a_source_without_the_axis_is_reported_once(self):
        doc = _width_doc(
            [(75, 75), (100, 100)],
            [make_source("Extended", {"Width": 130})],
        )
        font = _Font(
            [_Instance(130, 7, name="Extended"), _Instance(115, 6, name="Wide")],
            axes=[_Axis("Optical Size", "opsz")],
        )
        result = extend_axis_maps_to_masters_document(doc, font)
        assert result.extrapolated == ["Width"]
        assert len(result.skipped) == 1
        assert "not in the source" in result.skipped[0]

    def test_a_discrete_axis_is_skipped(self):
        doc = make_italic_doc()
        assert extend_axis_maps_to_masters_document(doc, font=None).axes == []


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

    def test_a_fully_decreasing_map_keeps_the_default_on_its_master(self):
        doc = make_doc(
            [make_axis(minimum=100, default=100, maximum=900, mapping=[(100, 200), (900, 40)])],
            [
                make_source("Thin", {"Weight": 40}),
                make_source("Black", {"Weight": 200}, copy_info=True),
            ],
        )
        axis = doc.axes[0]
        assert repair_inverted_axis_maps_document(doc).axes == ["Weight"]
        assert list(axis.map) == [(40.0, 40.0), (200.0, 200.0)]
        assert float(axis.map_forward(axis.default)) == 200.0
        assert doc.findDefault().name == "Black"

    def test_the_default_keeps_its_design_location_over_a_longer_run(self):
        # The three entries after the default are the longest run, and they
        # all sit below it: keeping them would move the default to design 50.
        doc = make_doc(
            [
                make_axis(
                    minimum=100,
                    default=100,
                    maximum=400,
                    mapping=[(100, 100), (200, 50), (300, 60), (400, 70)],
                )
            ],
            [
                make_source("A", {"Weight": 50}),
                make_source("B", {"Weight": 60}),
                make_source("C", {"Weight": 70}),
                make_source("Default", {"Weight": 100}, copy_info=True),
            ],
        )
        axis = doc.axes[0]
        assert repair_inverted_axis_maps_document(doc).axes == ["Weight"]
        assert float(axis.map_forward(axis.default)) == 100.0
        assert doc.findDefault().name == "Default"
        assert not any(
            later[1] < earlier[1]
            for earlier, later in zip(axis.map, axis.map[1:], strict=False)
        )

    def test_a_declared_bound_past_a_kept_end_entry_is_preserved(self):
        # 50..200 reaches past the map keys 75..125. The entry at 75 is the
        # conflicting one, so the minimum follows the map; 125 survives, and
        # the maximum beyond it is the document's own and stays.
        doc = make_doc(
            [
                make_axis(
                    name="Width",
                    tag="wdth",
                    minimum=50,
                    default=100,
                    maximum=200,
                    mapping=[(75, 150), (100, 100), (125, 150)],
                )
            ],
            [
                make_source("Standard", {"Width": 100}, copy_info=True),
                make_source("Wide", {"Width": 150}),
            ],
        )
        axis = doc.axes[0]
        assert repair_inverted_axis_maps_document(doc).axes == ["Width"]
        assert list(axis.map) == [(100.0, 100.0), (125.0, 150.0)]
        assert float(axis.minimum) == 100.0
        assert float(axis.maximum) == 200.0

    def test_float_noise_is_not_a_decrease(self):
        mapping = [(100, 100.0000001), (200, 100.0), (300, 100.0)]
        doc = make_doc(
            [make_axis(minimum=100, default=200, maximum=300, mapping=mapping)],
            [make_source("Only", {"Weight": 100})],
        )
        assert repair_inverted_axis_maps_document(doc).axes == []
        assert list(doc.axes[0].map) == mapping

    def test_a_discrete_axis_is_skipped(self):
        doc = make_italic_doc()
        doc.axes[0].map = [(0, 1), (1, 0)]
        assert repair_inverted_axis_maps_document(doc).axes == []

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


def _instance_at(name, location):
    instance = InstanceDescriptor()
    instance.name = name
    instance.designLocation = location
    return instance


class TestResetAxisMapsToDesign:
    def test_a_width_class_map_gives_way_to_the_design_coordinates(self):
        # Greed: every instance exports, glyphsLib labels 89 as 87.5 and 130
        # as 125. The designer's numbers are 75..130.
        doc = _width_doc(
            [(75, 75), (87.5, 89), (100, 100), (112.5, 115), (125, 130)],
            [
                make_source("Condensed", {"Width": 75}, copy_info=True),
                make_source("Extended", {"Width": 130}),
            ],
            maximum=125,
        )
        result = reset_axis_maps_to_design_document(doc)
        width = doc.axes[0]
        assert result.axes == ["Width"]
        assert result.ranges == [("Width", 75.0, 75.0, 130.0)]
        assert list(width.map) == []
        assert (width.minimum, width.default, width.maximum) == (75.0, 75.0, 130.0)
        result.summary().encode("ascii")

    def test_a_truncated_map_reaches_the_masters_without_the_source(self):
        # The wide instances are switched off: the map stops at 100 and the
        # Extended master is outside it. No font is needed to put it back.
        doc = _width_doc(
            [(75, 75), (87.5, 89), (100, 100)],
            [
                make_source("Condensed", {"Width": 75}, copy_info=True),
                make_source("Extended", {"Width": 130}),
            ],
        )
        reset_axis_maps_to_design_document(doc)
        assert float(doc.axes[0].maximum) == 130.0
        assert extend_axis_maps_to_masters_document(doc, font=None).axes == []

    def test_the_default_keeps_its_design_location(self):
        doc = _width_doc(
            [(75, 75), (100, 103), (125, 130)],
            [
                make_source("Condensed", {"Width": 75}),
                make_source("Normal", {"Width": 103}, copy_info=True),
                make_source("Extended", {"Width": 130}),
            ],
            default=100,
            maximum=125,
        )
        reset_axis_maps_to_design_document(doc)
        assert float(doc.axes[0].default) == 103.0

    def test_an_inverted_width_map_is_replaced_not_pruned(self):
        doc = make_doc(
            [_reckless_width()],
            [
                make_source("Condensed", {"Width": 50}, copy_info=True),
                make_source("Standard", {"Width": 100}),
                make_source("Wide", {"Width": 150}),
            ],
        )
        designs = [50.0, 150.0]
        result = reset_axis_maps_to_design_document(doc)
        width = doc.axes[0]
        assert result.axes == ["Width"]
        assert list(width.map) == []
        assert (float(width.minimum), float(width.maximum)) == (designs[0], designs[-1])
        assert repair_inverted_axis_maps_document(doc).axes == []

    def test_an_instance_past_the_masters_stays_inside_the_axis(self):
        doc = _width_doc(
            [(75, 75), (100, 100)],
            [
                make_source("Condensed", {"Width": 75}, copy_info=True),
                make_source("Standard", {"Width": 100}),
            ],
        )
        doc.addInstance(_instance_at("Wide", {"Width": 110}))
        reset_axis_maps_to_design_document(doc)
        assert float(doc.axes[0].maximum) == 110.0

    def test_an_instance_user_location_keeps_its_design(self):
        # User 125 is design 130 through the map. Once the axis is 1:1 the
        # same number would name design 125, so the instance is restated.
        doc = _width_doc(
            [(75, 75), (100, 100), (125, 130)],
            [
                make_source("Condensed", {"Width": 75}, copy_info=True),
                make_source("Standard", {"Width": 100}),
            ],
            maximum=125,
        )
        instance = InstanceDescriptor()
        instance.name = "Extended"
        instance.userLocation = {"Width": 125}
        doc.addInstance(instance)
        assert instance.getFullDesignLocation(doc) == {"Width": 130}

        reset_axis_maps_to_design_document(doc)
        assert instance.getFullDesignLocation(doc) == {"Width": 130.0}
        assert "Width" not in instance.userLocation
        assert float(doc.axes[0].maximum) == 130.0

    def test_labels_and_subsets_follow_the_axis(self):
        doc = _width_doc(
            [(75, 75), (100, 100), (125, 130)],
            [
                make_source("Condensed", {"Width": 75}, copy_info=True),
                make_source("Extended", {"Width": 130}),
            ],
            maximum=125,
        )
        width = doc.axes[0]
        width.axisLabels = [
            AxisLabelDescriptor(
                name="Extended", userValue=125, userMinimum=112.5, userMaximum=125
            ),
            AxisLabelDescriptor(name="Condensed", userValue=75, linkedUserValue=125),
        ]
        doc.locationLabels = [
            LocationLabelDescriptor(name="Extended", userLocation={"Width": 125})
        ]
        doc.variableFonts = [
            VariableFontDescriptor(
                name="VF",
                axisSubsets=[RangeAxisSubsetDescriptor(name="Width", userMaximum=125)],
            )
        ]
        reset_axis_maps_to_design_document(doc)
        extended, condensed = width.axisLabels
        assert (extended.userMinimum, extended.userValue, extended.userMaximum) == (
            115.0,
            130.0,
            130.0,
        )
        assert condensed.linkedUserValue == 130.0
        assert doc.locationLabels[0].userLocation == {"Width": 130.0}
        subset = doc.variableFonts[0].axisSubsets[0]
        assert subset.userMaximum == 130.0
        assert subset.userMinimum == float("-inf")
        assert subset.userDefault is None

    def test_a_discrete_axis_is_skipped(self):
        doc = make_italic_doc()
        assert reset_axis_maps_to_design_document(doc, tags=("ital",)).axes == []

    def test_a_brace_layer_does_not_set_the_range(self):
        doc = _width_doc(
            [(75, 75), (100, 100)],
            [
                make_source("Condensed", {"Width": 75}, copy_info=True),
                make_source("Standard", {"Width": 100}),
                make_source("Brace", {"Width": 140}, layer="{140}"),
            ],
        )
        reset_axis_maps_to_design_document(doc)
        assert float(doc.axes[0].maximum) == 100.0

    def test_weight_is_left_alone_by_default(self):
        weight = make_axis(mapping=[(100, 40), (400, 90), (900, 220)])
        doc = make_doc(
            [weight],
            [make_source("Thin", {"Weight": 40}), make_source("Black", {"Weight": 220})],
        )
        assert reset_axis_maps_to_design_document(doc).axes == []
        assert list(doc.axes[0].map) == [(100, 40), (400, 90), (900, 220)]

    def test_a_named_tag_is_reset(self):
        weight = make_axis(mapping=[(100, 40), (400, 90), (900, 220)])
        doc = make_doc(
            [weight],
            [make_source("Thin", {"Weight": 40}), make_source("Black", {"Weight": 220})],
        )
        result = reset_axis_maps_to_design_document(doc, tags=("wght",))
        assert result.ranges == [("Weight", 40.0, 90.0, 220.0)]

    def test_an_axis_that_is_already_one_to_one_is_untouched(self):
        doc = _width_doc(
            None,
            [
                make_source("Condensed", {"Width": 75}, copy_info=True),
                make_source("Standard", {"Width": 100}),
            ],
        )
        result = reset_axis_maps_to_design_document(doc)
        assert result.axes == []
        assert result.summary() == "no axis map to reset to design coordinates"

    def test_the_file_form_writes_only_a_reset(self, tmp_path):
        path = tmp_path / "Family.designspace"
        doc = _width_doc(
            [(75, 75), (125, 130)],
            [
                make_source("Condensed", {"Width": 75}, copy_info=True),
                make_source("Extended", {"Width": 130}),
            ],
            maximum=125,
        )
        doc.write(path)
        assert reset_axis_maps_to_design(path).axes == ["Width"]
        written = DesignSpaceDocument.fromfile(path)
        assert list(written.axes[0].map) == []
        assert float(written.axes[0].maximum) == 130.0
        stamp = path.stat().st_mtime_ns
        assert reset_axis_maps_to_design(path).axes == []
        assert path.stat().st_mtime_ns == stamp
