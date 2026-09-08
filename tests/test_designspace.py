"""
Unit tests of the designspace source deduplication.
"""
from fontTools.designspaceLib import (
    AxisDescriptor,
    DesignSpaceDocument,
    SourceDescriptor,
)

from glyphs_source_prep import (
    deduplicate_designspace_document,
    deduplicate_designspace_sources,
)


def document(*sources):
    doc = DesignSpaceDocument()
    axis = AxisDescriptor()
    axis.name = "Weight"
    axis.tag = "wght"
    axis.minimum, axis.default, axis.maximum = (50, 100, 150)
    doc.addAxis(axis)
    for source in sources:
        doc.addSource(source)
    return doc


def source(name, weight, filename="Master.ufo", layer_name=None):
    descriptor = SourceDescriptor()
    descriptor.name = name
    descriptor.filename = filename
    descriptor.location = {"Weight": weight}
    descriptor.layerName = layer_name
    return descriptor


def test_a_duplicate_location_is_dropped():
    doc = document(source("light", 50), source("bold", 150), source("bold-again", 150))

    result = deduplicate_designspace_document(doc)

    assert result.removed == 1
    assert [s.name for s in doc.sources] == ["light", "bold"]
    assert "150" in result.summary()


def test_distinct_locations_are_kept():
    doc = document(source("light", 50), source("bold", 150))

    result = deduplicate_designspace_document(doc)

    assert result.removed == 0
    assert len(doc.sources) == 2
    assert "no duplicate" in result.summary()


def test_a_full_master_wins_over_a_sparse_layer_source():
    """
    Dropping the full master would take a real master out of the design space,
    so the sparse source is the one that goes - whichever order they arrive in.
    """
    doc = document(source("sparse", 150, layer_name="{150}"), source("master", 150))
    deduplicate_designspace_document(doc)
    assert [s.name for s in doc.sources] == ["master"]

    doc = document(source("master", 150), source("sparse", 150, layer_name="{150}"))
    deduplicate_designspace_document(doc)
    assert [s.name for s in doc.sources] == ["master"]


def test_the_default_location_is_left_alone():
    """
    A location that is all zeroes carries no signal, so it is not deduplicated
    on - guessing there would be worse than leaving the duplicate for varLib
    to complain about.
    """
    doc = document(source("a", 0), source("b", 0))

    result = deduplicate_designspace_document(doc)

    assert result.removed == 0
    assert len(doc.sources) == 2


def test_axes_left_at_zero_do_not_change_the_identity():
    doc = document(source("a", 150), source("b", 150))
    doc.sources[0].location = {"Weight": 150, "Width": 0}
    doc.sources[1].location = {"Weight": 150}

    assert deduplicate_designspace_document(doc).removed == 1


def test_the_file_is_rewritten_only_when_something_was_removed(tmp_path):
    path = tmp_path / "Test.designspace"

    document(source("light", 50), source("bold", 150)).write(str(path))
    before = path.read_bytes()
    assert deduplicate_designspace_sources(path).removed == 0
    assert path.read_bytes() == before

    document(source("light", 50), source("bold", 150), source("dup", 150)).write(str(path))
    result = deduplicate_designspace_sources(path)
    assert result.removed == 1
    assert len(DesignSpaceDocument.fromfile(str(path)).sources) == 2
