"""Decoding and parsing a .glyphs source in one step, in the right order."""

from __future__ import annotations

import pytest

from glyphs_source_prep import load_source

pytest.importorskip("glyphs4to3")

MINIMAL_V3 = """{
.formatVersion = 3;
familyName = Test;
fontMaster = (
{
id = m01;
name = Regular;
}
);
glyphs = (
{
glyphname = A;
layers = (
{
layerId = m01;
width = 500;
}
);
}
);
unitsPerEm = 1000;
versionMajor = 1;
versionMinor = 0;
}
"""

#: A designer's name with a MacRoman byte in it, the way a source saved by an
#: older Glyphs.app keeps it: 0xD5 is a right single quote there, and is not
#: valid UTF-8 on its own.
MACROMAN_BYTE = b"\xd5"


class TestLoadSource:
    def test_text_is_parsed(self):
        result = load_source(MINIMAL_V3)
        assert result.font.familyName == "Test"
        assert not result.decode.rewritten
        assert not result.converted
        assert result.summary_lines() == []

    def test_bytes_are_decoded_before_they_are_parsed(self):
        # A bare data.decode("utf-8") raises here, and the caller never gets
        # far enough to find out which source or which string.
        source = MINIMAL_V3.replace(
            "familyName = Test;", 'familyName = Test;\ndesigner = "OD5Brien";'
        ).encode("utf-8").replace(b"OD5Brien", b"O" + MACROMAN_BYTE + b"Brien")
        result = load_source(source)
        assert result.decode.rewritten
        assert result.font.familyName == "Test"
        assert "MacRoman" in result.summary_lines()[0]

    def test_valid_utf8_bytes_are_left_as_they_are(self):
        result = load_source(MINIMAL_V3.encode("utf-8"))
        assert not result.decode.rewritten
        assert result.font.familyName == "Test"

    def test_the_origin_is_recorded_the_way_the_constructor_would(self, tmp_path):
        path = tmp_path / "Test.glyphs"
        result = load_source(MINIMAL_V3, origin=path)
        assert result.font.filepath == str(path)

    def test_without_an_origin_nothing_is_invented(self):
        result = load_source(MINIMAL_V3)
        assert not result.font.filepath

    def test_a_format_3_source_is_not_reported_as_converted(self):
        result = load_source(MINIMAL_V3)
        assert result.conversion is not None
        assert not result.converted
