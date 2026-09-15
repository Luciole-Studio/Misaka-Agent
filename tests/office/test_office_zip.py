"""An Office package is refused before a parser is handed it.

``python-docx`` and ``python-pptx`` read a whole package into lxml in one call, so there is
no point inside them at which a growing decompression can be counted and stopped -- unlike
``documents/index.py``'s EPUB reader, which counts markup as it decodes it chapter by
chapter. The bound therefore has to be taken before the library is called at all, off the
central directory, and the ENTITY scan with it: a ``word/document.xml`` that declares thirty
nested entities is a megabyte of memory per level, and an .docx arrives by download and is
indexed on arrival without anybody looking at it first.

Every refusal names the file and what is wrong with it, because the string reaches the model
as ``doc_add``'s answer and "cannot index" alone sends it back with the same file.
"""
from __future__ import annotations

import zipfile

import pytest

from misaka.core.documents.office import _zip


def _package(path, members, *, content_types=True):
    """A minimal OOXML package: whatever members are asked for, plus the type map."""
    with zipfile.ZipFile(path, "w") as archive:
        if content_types:
            archive.writestr(_zip.CONTENT_TYPES, "<?xml version='1.0'?><Types/>")
        for name, data in members.items():
            archive.writestr(name, data)
    return str(path)


def test_a_well_formed_package_passes(tmp_path):
    path = _package(tmp_path / "ok.docx", {"word/document.xml": "<?xml version='1.0'?><document/>"})
    assert _zip.precheck(path) is None


def test_a_file_that_is_not_a_zip_says_so(tmp_path):
    path = tmp_path / "fake.docx"
    path.write_bytes(b"not a zip at all, just some bytes\n")
    with pytest.raises(ValueError) as caught:
        _zip.precheck(str(path))
    assert "not a zip archive" in str(caught.value)
    assert "fake.docx" in str(caught.value)


def test_a_zip_without_the_type_map_is_not_an_office_package(tmp_path):
    path = _package(tmp_path / "plain.docx", {"a.txt": "hello"}, content_types=False)
    with pytest.raises(ValueError) as caught:
        _zip.precheck(path)
    assert "not an Office package" in str(caught.value)


def test_one_oversized_member_is_refused_by_its_declared_size(tmp_path):
    """The central directory is read; the member itself is never decompressed."""
    path = tmp_path / "bomb.xlsx"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(_zip.CONTENT_TYPES, "<?xml version='1.0'?><Types/>")
        archive.writestr("xl/worksheets/sheet1.xml", b"\0" * (_zip.MEMBER_BYTES + 1))
    with pytest.raises(ValueError) as caught:
        _zip.precheck(str(path))
    message = str(caught.value)
    assert "bomb.xlsx" in message
    assert "sheet1.xml" in message


def test_many_members_that_sum_past_the_package_bound_are_refused(tmp_path):
    """No single member is oversized; together they are. This is the shape the member
    bound alone cannot see."""
    path = tmp_path / "many.pptx"
    each = _zip.MEMBER_BYTES // 2
    count = _zip.PACKAGE_BYTES // each + 1
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(_zip.CONTENT_TYPES, "<?xml version='1.0'?><Types/>")
        for n in range(count):
            archive.writestr(f"ppt/slides/slide{n}.xml", b"\0" * each)
    with pytest.raises(ValueError) as caught:
        _zip.precheck(str(path))
    assert "many.pptx" in str(caught.value)
    assert "uncompressed" in str(caught.value)


@pytest.mark.parametrize("member", ["word/document.xml", "word/_rels/document.xml.rels"])
def test_a_declared_entity_in_any_markup_member_is_refused(tmp_path, member):
    path = _package(tmp_path / "entity.docx", {
        member: "<?xml version='1.0'?><!DOCTYPE d [<!ENTITY a 'aaaa'>]><d>&a;</d>",
    })
    with pytest.raises(ValueError) as caught:
        _zip.precheck(path)
    assert "XML entities" in str(caught.value)


def test_an_entity_outside_the_scanned_head_is_still_caught(tmp_path):
    """The scan reads a bounded head of each member, so the head has to be big enough for
    the declaration to be in it -- a DOCTYPE that is legal XML comes before the root
    element, and a member whose first bytes are padding is not a document."""
    padded = "<?xml version='1.0'?>" + (" " * (_zip.ENTITY_SCAN_BYTES - 64))
    path = _package(tmp_path / "late.docx", {
        "word/document.xml": padded + "<!DOCTYPE d [<!ENTITY a 'aaaa'>]><d/>",
    })
    with pytest.raises(ValueError) as caught:
        _zip.precheck(path)
    assert "XML entities" in str(caught.value)


def test_a_non_markup_member_is_not_scanned_for_entities(tmp_path):
    """An embedded image whose bytes happen to spell the token is not a declaration."""
    path = _package(tmp_path / "image.docx", {
        "word/media/image1.png": b"\x89PNG\r\n\x1a\n<!ENTITY not really>",
        "word/document.xml": "<?xml version='1.0'?><d/>",
    })
    assert _zip.precheck(path) is None


def test_a_damaged_member_does_not_crash_the_scan(tmp_path):
    """A member the archive cannot decompress is not evidence of an entity, and the
    parser is the thing that gets to report a broken package."""
    source = tmp_path / "torn.docx"
    _package(source, {"word/document.xml": "<?xml version='1.0'?><d/>"})
    raw = bytearray(source.read_bytes())
    # Corrupt a member's stored bytes without touching the central directory, so the sizes
    # still read cleanly and only the decompression fails.
    raw[40:48] = b"\xff" * 8
    torn = tmp_path / "torn2.docx"
    torn.write_bytes(bytes(raw))
    try:
        _zip.precheck(str(torn))
    except ValueError as error:
        assert "torn2.docx" in str(error)
