"""Refuse an Office package before a parser is handed it.

An OOXML file is a zip of XML, and both of the risks that carries have to be answered
*before* the library opens it rather than inside it. ``python-docx`` and ``python-pptx``
read a whole package into lxml in one call, so unlike ``index.py``'s EPUB reader -- which
counts markup as it decodes it, chapter by chapter, and can stop halfway -- there is no
point inside them where a growing decompression can be counted. The bound is therefore
taken off the central directory, which is cheap and happens before a byte is inflated.

The declared sizes are attacker-written, so they are a *ceiling* rather than a
measurement: a package that admits to being large is turned away on its own word, and one
that lies about being small still cannot inflate past what the parser itself will read.
The same reasoning ``index.py:_zip_read`` states for EPUB members.

The entity scan is the second half. Expat expands internal entities, so a
``word/document.xml`` declaring a dozen nested ones is a megabyte of memory and thirty is
the machine -- and a .docx now arrives by download and is indexed on arrival without
anybody looking at it first. No Office package has ever needed a declared entity.
"""
from __future__ import annotations

import os
import zipfile

# Borrowed from FrontierAgent's judgement about document size, expressed here the way
# ``index.py`` already expresses it for EPUB (``_EPUB_MEMBER_BYTES`` / ``_EPUB_BOOK_CHARS``):
# one bound on a part and one on the whole. 16 MiB is a sheet of several million cells or a
# slide deck's largest media part; 64 MiB is a document, and a "document" that does not fit
# is a dataset somebody renamed.
MEMBER_BYTES = 16 * 1024 * 1024
PACKAGE_BYTES = 64 * 1024 * 1024

# How much of each markup member is read looking for a declaration. A DOCTYPE is legal XML
# only before the root element, so it is always in the head of the file; the bound is what
# keeps the scan from inflating a whole package to prove a negative.
ENTITY_SCAN_BYTES = 64 * 1024

# Every OOXML package carries this at its root. Its absence means the zip is something
# else -- and saying so beats letting python-docx raise ``PackageNotFoundError`` at the
# model, which names neither the file nor what was wrong with it.
CONTENT_TYPES = "[Content_Types].xml"

# The members whose bytes are parsed as XML. An embedded image or font is not markup, and
# scanning it would refuse a package because a PNG's compressed bytes happened to spell a
# token.
_MARKUP_SUFFIXES = (".xml", ".rels")


def _refuse(path, why):
    return ValueError(f"Refused {os.path.basename(path)}: {why}")


def precheck(path):
    """Raise ``ValueError`` when this file must not be handed to an Office parser.

    Returns ``None`` for a package that may be opened. Every refusal names the file, so the
    string is usable as ``doc_add``'s whole answer.
    """
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            if CONTENT_TYPES not in names:
                raise _refuse(path, f"it is a zip but carries no {CONTENT_TYPES}, so it is "
                                    "not an Office package. Check the file is what its name says.")
            total = 0
            markup = []
            for info in archive.infolist():
                if info.is_dir():
                    continue
                if info.file_size > MEMBER_BYTES:
                    raise _refuse(path, f"{info.filename} declares "
                                        f"{info.file_size // (1024 * 1024)} MiB uncompressed, "
                                        f"larger than any part of a document "
                                        f"(limit {MEMBER_BYTES // (1024 * 1024)} MiB)")
                total += info.file_size
                if total > PACKAGE_BYTES:
                    raise _refuse(path, f"its parts sum past "
                                        f"{PACKAGE_BYTES // (1024 * 1024)} MiB uncompressed, "
                                        f"larger than any document")
                if info.filename.lower().endswith(_MARKUP_SUFFIXES):
                    markup.append(info.filename)
            for name in markup:
                try:
                    with archive.open(name) as member:
                        head = member.read(ENTITY_SCAN_BYTES)
                except (OSError, zipfile.BadZipFile, RuntimeError, EOFError):
                    # A member that will not decompress is the parser's to report: it can say
                    # which part of the package is broken, and this scan cannot.
                    continue
                if b"<!ENTITY" in head:
                    raise _refuse(path, f"{name} declares XML entities. Expat expands them, "
                                        "so a document that does this is not one this corpus "
                                        "will parse.")
    except zipfile.BadZipFile as error:
        raise _refuse(path, f"the file is not a zip archive ({error}). An .docx/.xlsx/.pptx "
                            "is a zip; a legacy .doc/.xls/.ppt is not, and needs LibreOffice "
                            "to convert it first.") from error
