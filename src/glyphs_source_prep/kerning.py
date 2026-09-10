"""Give a master with no kerning the kerning of the master beside it.

A Glyphs source usually holds one set of kerning pairs, drawn on the
proportional masters, and leaves the mono masters empty - in the editor the
mono design does not need it, because every glyph is the same width. What that
means for a build is less obvious: the mono masters interpolate to *no*
kerning, so a static Mono instance compiles with an empty GPOS and a variable
font's kerning fades out towards the mono end of the axis.

Glyphs.app does not show this, because it never interpolates the kerning the
way varLib does. The fix is to give an empty master the pairs of the sibling
that matches it on every axis except the mono one, preferring the sibling at
mono 0 - the proportional design the mono one was drawn from.

Only a master with *no* pairs at all is touched. A master with some kerning is
a design decision, and topping it up would be guessing at which pairs the
designer meant to leave out.

The axis is recognised by its tag or its name: Glyphs sources spell it
``MONO`` either way. There is no flag in the format that marks an axis as the
one whose ends share a kerning set.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

from fontTools.designspaceLib import DesignSpaceDocument, SourceDescriptor

from .designspace import EPSILON, master_sources

LOGGER = logging.getLogger(__name__)

#: How many master names the summary lists before it stops.
MAX_REPORTED_MASTERS = 10


@dataclass
class KerningInheritResult:
    """What :func:`inherit_empty_master_kerning_document` copied."""

    #: Names of the masters that were given a donor's kerning.
    masters: list[str] = field(default_factory=list)
    #: Masters that hold no kerning and have no sibling to take it from, so
    #: they still interpolate to none. Named rather than repaired: there is no
    #: donor to be right about.
    without_donor: list[str] = field(default_factory=list)

    @property
    def copied(self) -> int:
        return len(self.masters)

    def summary(self) -> str:
        """A single line for a processing log."""
        parts = []
        if self.masters:
            names = ", ".join(self.masters[:MAX_REPORTED_MASTERS])
            text = (
                f"{len(self.masters)} master(s) held no kerning and inherited it "
                f"from the matching proportional master: {names}"
            )
            if len(self.masters) > MAX_REPORTED_MASTERS:
                text += f" and {len(self.masters) - MAX_REPORTED_MASTERS} more"
            parts.append(text)
        if self.without_donor:
            names = ", ".join(self.without_donor[:MAX_REPORTED_MASTERS])
            text = (
                f"{len(self.without_donor)} master(s) hold no kerning and have no "
                f"sibling to inherit it from, so they interpolate to none: {names}"
            )
            if len(self.without_donor) > MAX_REPORTED_MASTERS:
                text += f" and {len(self.without_donor) - MAX_REPORTED_MASTERS} more"
            parts.append(text)
        if not parts:
            return "every master carries its own kerning"
        return "; ".join(parts)


def is_mono_axis(tag_or_name: str) -> bool:
    """Whether an axis tag or name is the mono axis.

    Both, because the same question is asked of a designspace, whose locations
    are keyed by axis *name*, and of an ``fvar`` table, whose axes are keyed by
    *tag*. Glyphs sources spell it ``MONO`` either way, and there is no flag in
    the format that marks an axis as the one whose ends share a kerning set.
    """
    return (tag_or_name or "").strip().upper() == "MONO"


def _locations_match_except(
    left: dict[str, float],
    right: dict[str, float],
    except_axes: set[str],
) -> bool:
    """Whether two locations agree on every axis outside ``except_axes``."""
    for key in set(left) | set(right):
        if key in except_axes:
            continue
        if abs(float(left.get(key, 0.0)) - float(right.get(key, 0.0))) > EPSILON:
            return False
    return True


def _location(source: SourceDescriptor) -> dict[str, float]:
    return {name: float(value) for (name, value) in (source.location or {}).items()}


def _source_name(source: SourceDescriptor) -> str:
    return source.name or source.filename or source.styleName or "?"


def inherit_empty_master_kerning_document(
    designspace: DesignSpaceDocument,
) -> KerningInheritResult:
    """Copy kerning onto empty masters, in place, from the loaded UFOs.

    Every master source is expected to carry its UFO on ``source.font``, which
    is how a document built in memory by glyphsLib arrives. Sources without one
    are skipped rather than opened: a caller holding a document has not
    necessarily written the UFOs anywhere yet. See
    :func:`inherit_empty_master_kerning` for the file-backed form.

    :param designspace: The document whose masters to repair.
    :returns: A :class:`KerningInheritResult`.
    """
    result = KerningInheritResult()
    masters = [
        source
        for source in master_sources(designspace)
        if getattr(source, "font", None) is not None
    ]
    if len(masters) < 2:
        return result

    mono_axes = {
        axis_name
        for source in masters
        for axis_name in (source.location or {})
        if is_mono_axis(axis_name)
    }
    if not mono_axes:
        # Without a mono axis there is no pair of masters that share a design
        # for kerning purposes, and an empty master is simply empty.
        return result

    for source in masters:
        if source.font.kerning:
            continue
        location = _location(source)
        candidates: list[tuple[float, SourceDescriptor]] = []
        for other in masters:
            if other is source or not other.font.kerning:
                continue
            if not _locations_match_except(location, _location(other), mono_axes):
                continue
            # The sibling closest to mono 0 is the proportional design this
            # master was drawn from.
            mono_value = sum(_location(other).get(axis, 0.0) for axis in mono_axes)
            candidates.append((mono_value, other))
        if not candidates:
            result.without_donor.append(_source_name(source))
            continue
        candidates.sort(key=lambda item: item[0])
        donor = candidates[0][1]
        source.font.kerning.clear()
        source.font.kerning.update(donor.font.kerning)
        result.masters.append(_source_name(source))

    if result.masters:
        LOGGER.warning(
            "%d master(s) inherited kerning from a proportional sibling: %s",
            len(result.masters),
            result.masters,
        )
    return result


def inherit_empty_master_kerning(
    designspace_path: str | os.PathLike[str],
) -> KerningInheritResult:
    """Copy kerning onto the empty masters of a designspace on disk.

    The UFOs are opened, repaired and written back; only the ones that actually
    inherited anything are saved. The designspace file itself is untouched -
    kerning lives in the UFOs.

    :param designspace_path: Path to the .designspace file.
    :returns: A :class:`KerningInheritResult`.
    """
    import ufoLib2

    path = os.fspath(designspace_path)
    directory = os.path.dirname(os.path.abspath(path))
    designspace = DesignSpaceDocument.fromfile(path)

    opened: dict[int, str] = {}
    for source in master_sources(designspace):
        ufo_path = source.path or (
            os.path.join(directory, source.filename) if source.filename else None
        )
        if not ufo_path or not os.path.exists(ufo_path):
            continue
        source.font = ufoLib2.Font.open(ufo_path)
        opened[id(source)] = ufo_path

    result = inherit_empty_master_kerning_document(designspace)
    if result.masters:
        repaired = set(result.masters)
        for source in master_sources(designspace):
            if id(source) in opened and _source_name(source) in repaired:
                source.font.save(opened[id(source)], overwrite=True)
    return result


__all__ = [
    "KerningInheritResult",
    "inherit_empty_master_kerning",
    "inherit_empty_master_kerning_document",
    "is_mono_axis",
]
