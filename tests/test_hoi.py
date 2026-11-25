"""
Unit tests of baking Glyphs 4 higher-order interpolation into intermediate layers.

The expected positions are computed by hand from the geometry, not by the
module's own formulas: an intermediate point is where the curve passes at
t = 0.5, a quarter arc's midpoint lies on the circle, and so on. The formulas
themselves were validated against Glyphs 4.2 output to 1e-6 units, see the
module docstring.
"""
import math

import glyphsLib
import openstep_plist
import pytest

from glyphs_source_prep import HOIError, bake_hoi, find_hoi, is_brace_layer

AXES = [{"tag": "wght", "name": "Weight"}, {"tag": "slnt", "name": "Slant"}]
MASTERS = [
    ("m01", "Thin Open", [100, 0]),
    ("m02", "Black Open", [900, 0]),
    ("m03", "Thin Closed", [100, 50]),
    ("m04", "Black Closed", [900, 50]),
]
LOWER = ("m01", "m02")


def make_font(glyphs, instances=((100, 25),), masters=MASTERS):
    source = {
        ".appVersion": "4205",
        ".formatVersion": 3,
        "familyName": "Test",
        "axes": AXES,
        "fontMaster": [
            {"id": master_id, "name": name, "axesValues": values}
            for (master_id, name, values) in masters
        ],
        "glyphs": [
            {"glyphname": name, "layers": layers} for (name, layers) in glyphs.items()
        ],
        "instances": [
            {"name": f"I{i}", "axesValues": list(values)} for i, values in enumerate(instances)
        ],
        "unitsPerEm": 1000,
    }
    return glyphsLib.loads(openstep_plist.dumps(source, sort_keys=False))


def node(x, y, hoi=None, kind="l"):
    return [x, y, kind, {"hoi": {"slnt": hoi}}] if hoi else [x, y, kind]


def layer(master_id, nodes, width=500, anchors=None, extra_shapes=()):
    result = {
        "layerId": master_id,
        "width": width,
        "shapes": [{"closed": 1, "nodes": nodes}, *extra_shapes],
    }
    if anchors:
        result["anchors"] = [{"name": n, "pos": list(p)} for n, p in anchors.items()]
    return result


def brace(master_id, coordinates, nodes, width=500):
    return {
        "layerId": f"brace-{master_id}-{coordinates}",
        "associatedMasterId": master_id,
        "attr": {"coordinates": list(coordinates)},
        "name": "{" + ", ".join(str(c) for c in coordinates) + "}",
        "width": width,
        "shapes": [{"closed": 1, "nodes": nodes}],
    }


def square(hoi=None, dx=0):
    """A closed three-node path; the first node may carry HOI."""
    return [node(100 + dx, 0, hoi), node(200 + dx, 0), node(200 + dx, 100)]


def glyph_with(lower_hoi, upper_dx=0, **kwargs):
    """A glyph whose first node moves from (100, 0) to (100 + upper_dx, 0)."""
    return [
        layer("m01", square(lower_hoi), **kwargs),
        layer("m02", square(lower_hoi), **kwargs),
        layer("m03", square(dx=upper_dx), **kwargs),
        layer("m04", square(dx=upper_dx), **kwargs),
    ]


def synthesized(font, glyph_name, coordinates):
    for candidate in font.glyphs[glyph_name].layers:
        if is_brace_layer(candidate) and [
            float(v) for v in candidate.attributes["coordinates"]
        ] == [float(v) for v in coordinates]:
            return candidate
    raise AssertionError(f"no layer at {coordinates} in {glyph_name}")


def first_node(layer_):
    position = layer_.paths[0].nodes[0].position
    return (position.x, position.y)


# ------------------------------------------------------------------ detection


def test_find_hoi_names_the_glyphs_and_axes():
    font = make_font({"a": glyph_with({"ip": [150, 50]}, upper_dx=100), "b": glyph_with(None)})
    assert find_hoi(font) == {"a": {"slnt"}}


def test_a_font_without_hoi_is_left_alone():
    font = make_font({"a": glyph_with(None)})
    result = bake_hoi(font)
    assert result.layers_added == 0
    assert len(font.glyphs["a"].layers) == 4
    assert "no higher-order" in result.summary()


# ------------------------------------------------------------------ the curves


def test_an_intermediate_point_is_where_the_node_is_half_way():
    font = make_font({"a": glyph_with({"ip": [150, 50]}, upper_dx=100)})
    bake_hoi(font)
    assert first_node(synthesized(font, "a", [100, 25])) == pytest.approx((150, 50))
    assert first_node(synthesized(font, "a", [900, 25])) == pytest.approx((150, 50))


def test_bezier_handles_bow_to_the_side_the_amplitude_says():
    # P0 (100, 0) -> P1 (200, 0); al = 0.3 puts C1 30 units right of the
    # direction of travel (below), au = -0.3 puts C2 on the same side, so
    # B(0.5) = (P0 + 3 C1 + 3 C2 + P1) / 8 = (150, -22.5)
    hoi = {"al": 0.3, "au": -0.3, "tl": 1 / 3, "tu": 1 / 3}
    font = make_font({"a": glyph_with(hoi, upper_dx=100)})
    bake_hoi(font)
    assert first_node(synthesized(font, "a", [100, 25])) == pytest.approx((150, -22.5))


def test_values_glyphs_wrote_as_strings_are_read():
    hoi = {"al": "0.3", "au": "-0.3", "tl": "0.33333333", "tu": "0.33333333"}
    font = make_font({"a": glyph_with(hoi, upper_dx=100)})
    bake_hoi(font)
    assert first_node(synthesized(font, "a", [100, 25])) == pytest.approx((150, -22.5), abs=1e-4)


def test_a_rotation_around_an_explicit_centre_stays_on_the_circle():
    # (100, 0) -> (0, 100) round (0, 0): the arc cubic is exact at its middle
    lower = [node(100, 0, {"rc": [0, 0]}), node(200, 0), node(200, 100)]
    upper = [node(0, 100), node(200, 0), node(200, 100)]
    font = make_font(
        {
            "a": [
                layer("m01", lower),
                layer("m02", lower),
                layer("m03", upper),
                layer("m04", upper),
            ]
        }
    )
    bake_hoi(font)
    r = 100 / math.sqrt(2)
    assert first_node(synthesized(font, "a", [100, 25])) == pytest.approx((r, r))


def test_rotation_direction_can_force_the_long_way_round():
    lower = [node(100, 0, {"rc": [0, 0], "rd": "cw"}), node(200, 0), node(200, 100)]
    upper = [node(0, 100), node(200, 0), node(200, 100)]
    font = make_font(
        {"a": [layer("m01", lower), layer("m02", lower), layer("m03", upper), layer("m04", upper)]}
    )
    bake_hoi(font, max_intermediate=64)
    x, y = first_node(synthesized(font, "a", [100, 25]))
    # clockwise from 0 to 90 degrees passes through 225 degrees half way
    assert x < 0 and y < 0


def test_the_automatic_rotation_centre_is_the_middle_of_the_path_bounds():
    # the path spans (0..200, 0..100) on both layers, so "auto" is (100, 50);
    # the node runs from (0, 50) to (200, 50) - half a turn, ambiguous by
    # direction, so it is pinned with rd and must pass (100, 150) at t = 0.5
    lower = [node(0, 50, {"rc": "auto", "rd": "cw"}), node(0, 100), node(200, 0)]
    upper = [node(200, 50), node(0, 100), node(0, 0)]
    font = make_font(
        {"a": [layer("m01", lower), layer("m02", lower), layer("m03", upper), layer("m04", upper)]}
    )
    bake_hoi(font, max_intermediate=64)
    assert first_node(synthesized(font, "a", [100, 25])) == pytest.approx((100, 150), abs=0.5)


def test_widths_and_anchors_interpolate_in_straight_lines():
    font = make_font(
        {
            "a": [
                layer("m01", square({"ip": [150, 50]}), width=400, anchors={"top": (0, 0)}),
                layer("m02", square({"ip": [150, 50]}), width=400, anchors={"top": (0, 0)}),
                layer("m03", square(dx=100), width=600, anchors={"top": (100, 40)}),
                layer("m04", square(dx=100), width=600, anchors={"top": (100, 40)}),
            ]
        }
    )
    bake_hoi(font)
    baked = synthesized(font, "a", [100, 25])
    assert baked.width == 500
    assert type(baked.width) is int  # a fractional advance stalls the CFF compiler
    top = baked.anchors["top"].position
    assert (top.x, top.y) == pytest.approx((50, 20))


# ------------------------------------------------------------------ the layers


def test_every_column_gets_layers_and_the_tolerance_holds():
    font = make_font({"a": glyph_with({"ip": [150, 90]}, upper_dx=100)})
    result = bake_hoi(font, tolerance=1.0)
    assert result.max_error <= 1.0
    assert result.glyphs == ["a"]
    assert 25 in result.locations
    weights = {
        float(candidate.attributes["coordinates"][0])
        for candidate in font.glyphs["a"].layers
        if is_brace_layer(candidate)
    }
    assert weights == {100.0, 900.0}
    assert result.layers_added == 2 * len(result.locations)


def test_a_glyph_without_hoi_gets_no_layers():
    font = make_font({"a": glyph_with({"ip": [150, 50]}, upper_dx=100), "b": glyph_with(None)})
    bake_hoi(font)
    assert len(font.glyphs["b"].layers) == 4


def test_brace_coordinates_are_spelled_one_way():
    """glyphsLib names the source after str(v): 25 and 25.0 would be two."""
    font = make_font({"a": glyph_with({"ip": [150, 50]}, upper_dx=100)})
    bake_hoi(font)
    baked = synthesized(font, "a", [100, 25])
    assert baked.attributes["coordinates"] == [100, 25]
    assert all(type(v) is int for v in baked.attributes["coordinates"])


def test_an_existing_source_location_keeps_its_master():
    """Another glyph already has {100, 25} on m02: joining it there avoids a
    second designspace source at one location."""
    other = glyph_with(None) + [brace("m02", [100, 25], square())]
    font = make_font({"a": glyph_with({"ip": [150, 50]}, upper_dx=100), "b": other})
    bake_hoi(font)
    assert synthesized(font, "a", [100, 25]).associatedMasterId == "m02"
    assert synthesized(font, "a", [900, 25]).associatedMasterId == "m01"


def test_hoi_is_removed_once_baked_so_a_second_run_does_nothing():
    font = make_font({"a": glyph_with({"ip": [150, 50]}, upper_dx=100)})
    first = bake_hoi(font)
    count = len(font.glyphs["a"].layers)
    second = bake_hoi(font)
    assert first.layers_added > 0
    assert second.layers_added == 0
    assert find_hoi(font) == {}
    assert len(font.glyphs["a"].layers) == count


def test_extra_locations_are_exact_too():
    font = make_font({"a": glyph_with({"ip": [150, 50]}, upper_dx=100)}, instances=())
    result = bake_hoi(font, extra_locations=[10])
    assert 10 in result.locations


def test_brace_columns_are_baked_like_master_columns():
    brace_lower = [node(100, 0, {"ip": [150, 70]}), node(200, 0), node(200, 100)]
    brace_upper = [node(200, 0), node(200, 0), node(200, 100)]
    font = make_font(
        {
            "a": glyph_with({"ip": [150, 50]}, upper_dx=100)
            + [brace("m01", [400, 0], brace_lower), brace("m01", [400, 50], brace_upper)]
        }
    )
    bake_hoi(font)
    assert first_node(synthesized(font, "a", [400, 25])) == pytest.approx((150, 70))


# ------------------------------------------------------------------ refusals


def refused(font, **kwargs):
    before = {g.name: len(g.layers) for g in font.glyphs}
    with pytest.raises(HOIError) as info:
        bake_hoi(font, **kwargs)
    # nothing changed: no layer added, the HOI still there
    assert {g.name: len(g.layers) for g in font.glyphs} == before
    assert find_hoi(font)
    return str(info.value)


def test_hoi_on_the_upper_layer_is_refused():
    upper = [node(200, 0, {"ip": [1, 2]}), node(200, 0), node(200, 100)]
    glyph = glyph_with({"ip": [150, 50]}, upper_dx=100)
    glyph[2] = layer("m03", upper)
    assert "upper layer" in refused(make_font({"a": glyph}))


def test_incompatible_layers_are_refused():
    glyph = glyph_with({"ip": [150, 50]}, upper_dx=100)
    glyph[3] = layer("m04", [node(200, 0), node(200, 100)])
    assert "not compatible" in refused(make_font({"a": glyph}))


def test_a_column_without_a_pair_is_refused():
    glyph = glyph_with({"ip": [150, 50]}, upper_dx=100) + [brace("m01", [400, 0], square())]
    assert "paired" in refused(make_font({"a": glyph}))


def test_hoi_along_two_axes_is_refused():
    glyph = glyph_with({"ip": [150, 50]}, upper_dx=100)
    glyph[0]["shapes"][0]["nodes"][1] = [200, 0, "l", {"hoi": {"wght": {"ip": [1, 2]}}}]
    assert "more than one axis" in refused(make_font({"a": glyph}))


def test_a_curve_needing_too_many_layers_is_refused():
    font = make_font({"a": glyph_with({"ip": [150, 900]}, upper_dx=100)})
    message = refused(font, tolerance=0.01, max_intermediate=2)
    assert "a: the HOI curve" in message


def test_every_problem_is_reported_at_once():
    upper = [node(200, 0, {"ip": [1, 2]}), node(200, 0), node(200, 100)]
    a = glyph_with({"ip": [150, 50]}, upper_dx=100)
    a[2] = layer("m03", upper)
    b = glyph_with({"ip": [150, 50]}, upper_dx=100)
    b[3] = layer("m04", [node(200, 0), node(200, 100)])
    message = refused(make_font({"a": a, "b": b}))
    assert "a:" in message and "b:" in message


# ------------------------------------------------------------------ from the review


def test_variable_and_inactive_instances_are_not_locations():
    font = make_font({"a": glyph_with({"ip": [150, 50]}, upper_dx=100)}, instances=())
    font.instances.extend(_instances(font, [(100, 10, 1, True), (100, 20, 0, False)]))
    result = bake_hoi(font, tolerance=100)
    assert 10 not in result.locations and 20 not in result.locations


def _instances(font, specs):
    instances = []
    for weight, slant, kind, exports in specs:
        instance = glyphsLib.classes.GSInstance()
        instance.name = f"x{slant}"
        instance.axes = [weight, slant]
        instance.type = kind
        instance.exports = exports
        instances.append(instance)
    return instances


def test_a_short_brace_spelling_is_reused_not_duplicated():
    """``{100}`` on a master at slnt 25 already is the source at (100, 25): the
    synthesized layer must join it, not add a second source at that location."""
    masters = MASTERS + [("m05", "Medium Half", [500, 25])]
    b = glyph_with(None) + [layer("m05", square()), brace("m05", [100], square())]
    a = glyph_with({"ip": [150, 50]}, upper_dx=100)
    font = make_font({"a": a, "b": b}, masters=masters)
    bake_hoi(font)
    baked = synthesized_short(font, "a")
    assert baked.associatedMasterId == "m05"
    assert list(baked.attributes["coordinates"]) == [100]

    designspace = glyphsLib.to_designspace(font, minimal=True)
    locations = [tuple(sorted(s.location.items())) for s in designspace.sources]
    assert len(locations) == len(set(locations))


def synthesized_short(font, glyph_name):
    for candidate in font.glyphs[glyph_name].layers:
        if is_brace_layer(candidate) and list(candidate.attributes["coordinates"]) == [100]:
            return candidate
    raise AssertionError("no {100} layer")


def test_a_layer_at_a_duplicate_location_is_refused():
    glyph = glyph_with({"ip": [150, 50]}, upper_dx=100) + [
        brace("m01", [400, 0], square()),
        brace("m02", [400, 0], square()),
    ]
    glyph[-1]["layerId"] = "other"
    assert "two layers" in refused(make_font({"a": glyph}))


def test_hoi_on_a_backup_layer_is_ignored():
    glyph = glyph_with(None)
    backup = layer("m01", square({"ip": [1, 2]}))
    backup["layerId"] = "backup-1"
    backup["associatedMasterId"] = "m01"
    backup["name"] = "old"
    font = make_font({"a": glyph + [backup]})
    assert find_hoi(font) == {}
    assert bake_hoi(font).layers_added == 0


def test_the_cap_counts_only_the_halvings():
    """Instance locations are not refinements: many of them must not trip it."""
    instances = [(100, v) for v in range(2, 50, 2)]
    font = make_font({"a": glyph_with({"ip": [150, 50]}, upper_dx=100)}, instances=instances)
    result = bake_hoi(font, max_intermediate=1)
    assert len(result.locations) >= len(instances)


def test_hints_and_component_properties_are_kept():
    hint = {"type": "Corner", "name": "_corner.x", "origin": [0, 1]}
    component = {"ref": "b", "anchor": "top", "alignment": -1, "pos": [10, 0]}
    upper_component = {"ref": "b", "anchor": "top", "alignment": -1, "pos": [30, 0]}

    def with_extras(layer_dict, comp):
        layer_dict["hints"] = [dict(hint)]
        layer_dict["shapes"].append(dict(comp))
        return layer_dict

    a = [
        with_extras(layer("m01", square({"ip": [150, 50]})), component),
        with_extras(layer("m02", square({"ip": [150, 50]})), component),
        with_extras(layer("m03", square(dx=100)), upper_component),
        with_extras(layer("m04", square(dx=100)), upper_component),
    ]
    font = make_font({"a": a, "b": glyph_with(None)})
    bake_hoi(font)
    baked = synthesized(font, "a", [100, 25])
    assert len(baked.hints) == 1 and baked.hints[0].name == "_corner.x"
    (comp,) = baked.components
    assert comp.anchor == "top"
    assert comp.alignment == -1
    assert comp.position.x == pytest.approx(20)
