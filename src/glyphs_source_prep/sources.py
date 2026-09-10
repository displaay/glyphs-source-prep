"""Read a .glyphs source into a ``GSFont``.

Two things stand between the bytes on disk and a font object glyphsLib will
work with, and they have to happen in this order:

1. decode the file, which is UTF-8 with the occasional legacy MacRoman byte
   (:mod:`glyphs_source_prep.encoding`);
2. convert a format 4 source down to format 3, because glyphsLib knows
   versions 2 and 3 only and fails inside its node parser on a format 4 one
   with ``TypeError: expected string or bytes-like object, got 'list'``.

Doing them the other way round cannot work - the converter is handed ``str`` -
and doing only the second is what both tools did before a source with a
designer's name in MacRoman arrived. Neither tool should have to remember the
order, so it lives here.

What stays with each tool is what genuinely differs: the Builder stages a
converted copy on disk for fontmake to read, and the Customizer translates the
conversion error into its own validation error so an order is reported as
"validationFailed" rather than "failed". Both call :func:`load_source` for the
step in the middle.

``glyphs4to3`` is an optional dependency: this module is the only thing here
that needs it, and it is imported when the function runs so that the rest of
the package installs without it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .encoding import SourceDecodeResult, decode_source_bytes

if TYPE_CHECKING:
    from glyphsLib.classes import GSFont

LOGGER = logging.getLogger(__name__)


@dataclass
class SourceLoadResult:
    """A parsed source, with what had to be done to it on the way."""

    #: The parsed source.
    font: GSFont
    #: How the bytes were decoded. ``decode.rewritten`` is True when the file
    #: was not valid UTF-8.
    decode: SourceDecodeResult
    #: The glyphs4to3 normalize report. ``conversion.converted`` is False for a
    #: format 2 or 3 source, which took the path glyphsLib would have taken
    #: anyway.
    conversion: Any

    @property
    def converted(self) -> bool:
        """Whether the source was a format 4 one that had to be converted."""
        return bool(getattr(self.conversion, "converted", False))

    def summary_lines(self) -> list[str]:
        """The lines worth writing to a processing log, or none."""
        lines = []
        if self.decode.rewritten:
            lines.append(self.decode.summary())
        if self.converted:
            lines.append(self.conversion.summary())
        return lines


def load_source(data: bytes | str, *, origin: Any = None) -> SourceLoadResult:
    """Decode and parse a .glyphs source.

    :param data: The file's bytes, or its text when the caller has already
        decoded it. Prefer handing over the bytes: a caller that decodes with
        a bare ``bytes.decode("utf-8")`` raises on the legacy bytes this is
        here to survive.
    :param origin: The path the source came from, recorded on the returned font
        as ``filepath`` the way ``GSFont(path)`` would. Optional - the
        Customizer is handed the body of a request and has no path.
    :returns: :class:`SourceLoadResult`.
    :raises glyphs4to3.Glyphs4Error: The source uses a Glyphs 4 construct with
        no format 3 equivalent, or a format version newer than 4. Callers that
        report to a user are expected to translate this.
    :raises RuntimeError: ``glyphs4to3`` is not installed.
    """
    try:
        from glyphs4to3 import loads_with_report
    except ImportError as exc:  # pragma: no cover - packaging failure
        raise RuntimeError(
            "glyphs4to3 is required to read .glyphs sources; install "
            "glyphs-source-prep[sources]."
        ) from exc

    if isinstance(data, bytes):
        decoded = decode_source_bytes(data)
    else:
        decoded = SourceDecodeResult(text=data, rewritten=False)

    font, report = loads_with_report(decoded.text)
    if origin is not None:
        # GSFont.__init__ records filepath after parsing; the parser itself
        # already wires master.font. Setting it keeps this indistinguishable
        # from the constructor for anything that reads it later.
        font.filepath = str(origin)
    return SourceLoadResult(font=font, decode=decoded, conversion=report)
