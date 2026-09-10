"""Decoding a .glyphs source that is not valid UTF-8."""

from __future__ import annotations

import pytest

from glyphs_source_prep import (
    SourceDecodeResult,
    decode_source_bytes,
    decode_source_text,
)


class TestValidUtf8:
    """The common case has to stay exact and cheap."""

    def test_ascii_is_returned_unchanged(self):
        result = decode_source_bytes(b"{ .formatVersion = 3; }")
        assert result.text == "{ .formatVersion = 3; }"
        assert result.rewritten is False
        assert result.legacy_bytes == 0

    def test_multi_byte_utf8_is_not_mistaken_for_legacy_bytes(self):
        # a source with a Czech designer name, saved by a current Glyphs.app
        source = "designer = Tomáš Ňuňátko; note = 私;"
        result = decode_source_bytes(source.encode("utf-8"))
        assert result.text == source
        assert result.rewritten is False

    def test_summary_says_nothing_happened(self):
        assert decode_source_bytes(b"x").summary() == "source is valid UTF-8"


class TestLegacyBytes:
    """Glyphs.app falls back per byte, so the file is usually mixed."""

    def test_single_macroman_byte(self):
        result = decode_source_bytes("copyright © Displaay".encode("mac_roman"))
        assert result.text == "copyright © Displaay"
        assert result.rewritten is True
        assert result.legacy_bytes == 1
        assert result.characters == ["©"]

    def test_utf8_around_a_legacy_byte_survives(self):
        # the whole point: decoding the file as MacRoman would turn every
        # genuine multi-byte character into mojibake
        data = "Tomáš ".encode("utf-8") + b"\xa9" + " Ňuňátko".encode("utf-8")
        result = decode_source_bytes(data)
        assert result.text == "Tomáš © Ňuňátko"
        assert result.rewritten is True
        assert result.legacy_bytes == 1

    def test_several_legacy_bytes(self):
        data = b"a\xa9b\xd5c"          # (c) and a right single quote
        result = decode_source_bytes(data)
        assert result.text == "a©b’c"
        assert result.legacy_bytes == 2
        assert result.characters == ["©", "’"]

    def test_legacy_byte_at_the_very_end(self):
        result = decode_source_bytes(b"trailing \xa9")
        assert result.text == "trailing ©"
        assert result.legacy_bytes == 1

    def test_legacy_byte_at_the_very_start(self):
        result = decode_source_bytes(b"\xa9 leading")
        assert result.text == "© leading"
        assert result.legacy_bytes == 1

    def test_only_legacy_bytes(self):
        result = decode_source_bytes(b"\xa9\xd5")
        assert result.text == "©’"
        assert result.legacy_bytes == 2

    def test_empty_input(self):
        result = decode_source_bytes(b"")
        assert result.text == ""
        assert result.rewritten is False


class TestSummary:
    """The summary is what reaches a processing log."""

    def test_names_the_characters(self):
        summary = decode_source_bytes(b"a\xa9b").summary()
        assert "1 byte(s)" in summary
        assert "MacRoman" in summary
        assert "©" in summary

    def test_long_runs_are_truncated(self):
        result = decode_source_bytes(b"\xa9" * 25)
        summary = result.summary()
        assert result.legacy_bytes == 25
        assert "and 15 more" in summary


class TestDecodeSourceText:
    """Callers reach a source as bytes or as an already-decoded string."""

    def test_str_passes_through(self):
        result = decode_source_text("already text")
        assert result.text == "already text"
        assert result.rewritten is False

    def test_bytes_are_decoded(self):
        result = decode_source_text("©".encode("mac_roman"))
        assert result.text == "©"
        assert result.rewritten is True

    @pytest.mark.parametrize("value", ["", b""])
    def test_empty_either_way(self, value):
        assert decode_source_text(value).text == ""


def test_result_defaults_do_not_share_a_list():
    """A mutable default would leak characters between two decodes."""
    first, second = SourceDecodeResult(), SourceDecodeResult()
    first.characters.append("©")
    assert second.characters == []
