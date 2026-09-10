"""Decode a .glyphs source that is not valid UTF-8.

Glyphs.app has written UTF-8 for many years, but a source that started life in
an older version keeps whatever single bytes were typed into it then - a
designer's name, a copyright line, a note on a glyph - encoded as MacRoman.
The editor still opens such a file: it falls back per byte. Python does not.
``data.decode("utf-8")`` raises ``UnicodeDecodeError`` and the build fails
before glyphsLib is ever reached, with a message about a byte offset that says
nothing about which source or which string.

The file is usually *mixed*: mostly UTF-8, with a handful of legacy bytes in
the middle. Decoding the whole thing as MacRoman would therefore be wrong -
every genuine multi-byte UTF-8 character would turn into mojibake. This
decodes as UTF-8 as far as it can, takes the single offending byte as
MacRoman, and resumes; only the bytes that cannot be UTF-8 are treated as
legacy.

Unlike the other fixes here this one is not a glyphsLib bug and has no
upstream issue: by the time glyphsLib sees a source it is already ``str``.
It lives here because it is the same class of problem - what Glyphs.app
tolerates in a source and the Python toolchain does not - and both Displaay
tools need it at the same point, before the source is parsed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

LOGGER = logging.getLogger(__name__)

#: How many decoded legacy characters the summary names before it stops.
MAX_REPORTED_CHARS = 10


@dataclass
class SourceDecodeResult:
    """How :func:`decode_source_bytes` read the file."""

    text: str = ""
    rewritten: bool = False
    legacy_bytes: int = 0
    characters: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.characters is None:
            self.characters = []

    def summary(self) -> str:
        """A single line for a processing log."""
        if not self.rewritten:
            return "source is valid UTF-8"
        shown = self.characters[:MAX_REPORTED_CHARS]
        chars = ", ".join(repr(c) for c in shown)
        text = (
            f"{self.legacy_bytes} byte(s) that are not valid UTF-8 read as "
            f"MacRoman: {chars}"
        )
        if len(self.characters) > MAX_REPORTED_CHARS:
            text += f", and {len(self.characters) - MAX_REPORTED_CHARS} more"
        return text


def decode_source_bytes(data: bytes) -> SourceDecodeResult:
    """Decode ``.glyphs`` file bytes to text, falling back to MacRoman per byte.

    :param data: The raw bytes of a ``.glyphs`` file.
    :returns: A :class:`SourceDecodeResult`. ``rewritten`` is False and the
        result is exact when the file was already valid UTF-8, which is the
        overwhelmingly common case and costs one decode attempt.
    """
    try:
        return SourceDecodeResult(text=data.decode("utf-8"), rewritten=False)
    except UnicodeDecodeError:
        pass

    out: list[str] = []
    characters: list[str] = []
    index = 0
    length = len(data)
    while index < length:
        try:
            out.append(data[index:].decode("utf-8"))
            break
        except UnicodeDecodeError as exc:
            if exc.start:
                # everything up to the bad byte is genuine UTF-8
                out.append(data[index : index + exc.start].decode("utf-8"))
                index += exc.start
            character = bytes([data[index]]).decode("mac_roman")
            out.append(character)
            characters.append(character)
            index += 1

    result = SourceDecodeResult(
        text="".join(out),
        rewritten=True,
        legacy_bytes=len(characters),
        characters=characters,
    )
    LOGGER.warning("%s", result.summary())
    return result


def decode_source_text(data: bytes | str) -> SourceDecodeResult:
    """Accept bytes or an already-decoded string.

    The two Displaay tools reach a source differently - one reads a file, the
    other is handed the body of a request that may already have been decoded -
    and neither should have to care which it holds.
    """
    if isinstance(data, str):
        return SourceDecodeResult(text=data, rewritten=False)
    return decode_source_bytes(data)
