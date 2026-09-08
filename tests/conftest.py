"""Shared fixtures: small .glyphs sources built as plist literals.

Building a GSFont through glyphsLib rather than mocking it keeps the tests
honest about the private API these fixes touch (``layer.hasBackground``,
``layer._is_brace_layer()``, the shapes proxy) - the parts most likely to move
under a glyphsLib bump, which is why CI runs the matrix.
"""

import glyphsLib
import openstep_plist
import pytest

PATH = {"closed": 1, "nodes": [[100, 200, "l"], [300, 400, "l"], [300, 200, "l"]]}


def make_font(glyphs, masters=None, custom_parameters=None):
    """Build a GSFont from a compact description.

    :param glyphs: ``{glyph name: [layer dict, ...]}``.
    :param masters: ``[(id, name, axes values), ...]``; one master by default.
    :param custom_parameters: ``[{"name": ..., "value": ...}, ...]``.
    """
    masters = masters or [("m01", "Regular", [100])]
    source = {
        ".appVersion": "3300",
        ".formatVersion": 3,
        "familyName": "Test",
        "axes": [{"tag": "wght", "name": "Weight"}],
        "fontMaster": [
            {"id": master_id, "name": name, "axesValues": values}
            for (master_id, name, values) in masters
        ],
        "glyphs": [
            {"glyphname": name, "layers": layers} for (name, layers) in glyphs.items()
        ],
        "unitsPerEm": 1000,
    }
    if custom_parameters:
        source["customParameters"] = custom_parameters
    return glyphsLib.loads(openstep_plist.dumps(source, sort_keys=False))


def master_layer(master_id="m01", shapes=None, background=None):
    layer = {"layerId": master_id, "width": 500, "shapes": shapes or [PATH]}
    if background is not None:
        layer["background"] = {"shapes": background}
    return layer


def brace_layer(associated_master_id, coordinates, shapes=None):
    """An intermediate layer at ``coordinates``, hung off a given master."""
    return {
        "layerId": f"brace-{associated_master_id}-{coordinates}",
        "associatedMasterId": associated_master_id,
        "attr": {"coordinates": list(coordinates)},
        "name": "{" + ", ".join(str(c) for c in coordinates) + "}",
        "width": 500,
        "shapes": shapes or [PATH],
    }


@pytest.fixture
def two_masters():
    return [("m01", "Light", [50]), ("m02", "Bold", [150])]
