"""A Word document read at the XML level: heading levels, run marks, lists, tables in body
order, footnotes and endnotes.

FrontierAgent reads .docx by shelling out to pandoc (``plugins/tools/_reader_docx.py``
``_docx_to_md``) and falls back to python-docx when pandoc is absent. Owner decision D3
rules out bundling an external program, so this has to reach pandoc's output surface on its
own -- and FA's own fallback is the proof that python-docx's easy path is not enough:
``doc.paragraphs`` skips every table, ``doc.tables`` collects them at the end in the wrong
place, run-level bold/italic is gone, and there is no footnote API at all. A footnote is
where a paper puts the qualification on its own claim, so losing it loses exactly the
sentence a researcher needed to cite.

So the walk is over ``w:body`` itself: ``w:p`` and ``w:tbl`` arrive interleaved in document
order, runs keep their marks, and ``word/footnotes.xml`` is read out of the package.

Conventions shared with the rest of this package: a backtick span is parser-added and not
file content.

**Nothing the parser adds is ever inserted inside a line.** Footnote markers, hyperlink
targets and formatting notes go after the line's own text; tables are tab-separated rather
than markdown pipe tables. All of it is one rule with one reason: a quotation is checked
against the page with every whitespace character folded away
(``index.normalize_for_quote_match``), and anything wedged between two words of a sentence
puts a character in the page that the quotation does not have. Pandoc writes
``Revenue grew[^1] in Q3`` and FrontierAgent inherits it; that page folds to
"Revenuegrew[^1]inQ3" while the model's quotation folds to "RevenuegrewinQ3", so the
sentence can never be locked to the page it is on. Same for ``|`` in a pipe table, which
the fold keeps while dropping the spaces around it. A corpus that exists to verify
quotations cannot spend them on typography.

Inline emphasis follows from the same rule and from what misaka already does elsewhere: the
HTML and EPUB extractor (``htmltext.readable``) drops ``<b>`` and ``<i>`` and keeps block
structure, so the same sentence in an EPUB and in a .docx has to fold the same way. Bold
covering a *whole* line is kept -- markers at the two ends cost a within-line quotation
nothing -- and emphasis on a span inside a line is moved to a trailing note that says which
words carried it, so the signal survives without splitting the sentence.

Not rendered, deliberately: headers and footers. They repeat on every page, so indexing
them buries ``doc_find`` under N copies of a running title -- FA does not render them
either.
"""
from __future__ import annotations

import os
import re
import zipfile

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
DC = "{http://purl.org/dc/elements/1.1/}"

# A style whose name is one of these is a top-level heading. Word ships localised style
# names and a document authored in Chinese Word carries "标题 1" where an English one
# carries "Heading 1"; both are the same style and both have to become "#".
_HEADING = re.compile(r"^(?:heading|标题|titre|überschrift|encabezado)\s*(\d+)$", re.IGNORECASE)
_TITLE_STYLES = frozenset({"title", "标题", "subtitle", "副标题"})

# Footnote and endnote ids 0 and 1 are the separator rules Word draws above the notes, not
# notes. They carry ``w:type``; a real note does not.
_NOT_A_NOTE = frozenset({"separator", "continuationSeparator", "continuationNotice"})

MAX_ALT_CHARS = 120

# Beyond this many characters of echoed emphasis on one line, the trailing note names the
# marks instead of repeating the words. A document typeset entirely in bold runs would
# otherwise double the page.
MAX_EMPHASIS_ECHO = 200

_PLAIN = (False, False, False, False)

# Footnotes and endnotes number independently in Word, so both would print "[^1]" and a
# reader could not tell which section to look the marker up in. The endnote takes a prefix.
_MARKER = {"footnote": "", "endnote": "e"}

# In the order ``_marks`` returns them.
_MARK_NAMES = ("bold", "italic", "strikethrough", "underline")


def _local(element):
    tag = element.tag
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _marks(run):
    """``(bold, italic, strike, underline)`` for one ``w:r``.

    A toggle property is on unless it carries ``w:val`` set to a false value: ``<w:b/>`` is
    bold and ``<w:b w:val="0"/>`` is not, which is how Word turns a style's bold back off
    for one run.
    """
    properties = run.find(f"{W}rPr")
    if properties is None:
        return (False, False, False, False)

    def on(name):
        node = properties.find(f"{W}{name}")
        if node is None:
            return False
        value = node.get(f"{W}val")
        return value not in ("0", "false", "off", "none")

    return (on("b"), on("i"), on("strike") or on("dstrike"), on("u"))


def _run_text(run):
    """The text of one ``w:r``, with the layout elements Word stores beside it.

    ``w:tab`` and ``w:br`` are content -- a tabbed column of figures and a line break inside
    a paragraph both read as text -- but a tab inside a table cell would break the
    tab-separated row, so ``_cell_text`` flattens them there.
    """
    out = []
    for node in run.iter():
        name = _local(node)
        if name == "t":
            out.append(node.text or "")
        elif name == "tab":
            out.append("\t")
        elif name in ("br", "cr"):
            out.append("\n")
        elif name == "noBreakHyphen":
            out.append("-")
        elif name == "softHyphen":
            out.append("")
    return "".join(out)


def _wrap(text, marks):
    """Apply run marks to text, keeping the surrounding whitespace outside the markers.

    ``**text **`` does not render as bold in any markdown parser, so the padding has to sit
    outside: lead + ``**text**`` + trail.
    """
    if not text:
        return ""
    core = text.strip()
    if not core:
        return text
    lead = text[: len(text) - len(text.lstrip())]
    trail = text[len(text.rstrip()):]
    bold, italic, strike, underline = marks
    if bold and italic:
        core = f"***{core}***"
    elif bold:
        core = f"**{core}**"
    elif italic:
        core = f"*{core}*"
    if strike:
        core = f"~~{core}~~"
    if underline:
        core = f"<u>{core}</u>"
    return lead + core + trail


def _image_alt(drawing):
    """The alt text Word stores on an inline image, clipped.

    ``@name`` is deliberately not a fallback: Word fills it in as "Picture 3" whether or not
    anyone described the image, so using it would put a caption on every undescribed
    picture that says nothing and reads as if it did.
    """
    for node in drawing.iter():
        if _local(node) == "docPr":
            alt = (node.get("descr") or node.get("title") or "").strip()
            return alt[:MAX_ALT_CHARS]
    return ""


def _emphasis_note(pieces):
    """The trailing note for a line whose emphasis covers only part of it.

    Names the marks and echoes the words that carried them, so "which term was bold" is not
    lost -- it simply moves off the sentence, where it would have broken the quotation.
    """
    by_mark = {}
    for marks, text in pieces:
        if not any(marks) or not text.strip():
            continue
        for name, on in zip(_MARK_NAMES, marks, strict=True):
            if on:
                by_mark.setdefault(name, []).append(text.strip())
    if not by_mark:
        return ""
    echoed = sum(len(word) for words in by_mark.values() for word in words)
    if echoed > MAX_EMPHASIS_ECHO:
        return "`emphasis: " + ", ".join(by_mark) + "`"
    parts = [f'{name} "{" / ".join(words)}"' for name, words in by_mark.items()]
    return "`" + "; ".join(parts) + "`"


def _line(text, trailing):
    """The line's own text with the parser's additions after it, single-spaced.

    Runs of spaces are squeezed: a footnote reference is a run of its own carrying no text,
    so dropping it leaves the space before it next to the space after it. The fold that
    checks quotations removes whitespace anyway, so this is only what the line looks like.
    """
    joined = " ".join([text.strip(), *trailing]).strip() if trailing else text.strip()
    return re.sub(r"[ ]{2,}", " ", joined)


def _inline(container, notes, rels, state, *, plain=False):
    """The run-level children of a paragraph as ``(text, trailing bits)``.

    Everything the parser adds lands in ``trailing``, to be written after the line: note
    markers, hyperlink targets, the emphasis note. The line's own text stays contiguous;
    see the module docstring for why that is not negotiable.

    Adjacent runs carrying the same marks are merged first. Word splits a sentence into a
    new run at every spell-check boundary, revision mark and language switch, so a bold
    phrase routinely arrives as four runs -- treating each as its own emphasis span
    produces ``bold "Total" / " reven" / "ue"`` where the document has one bold phrase.

    ``plain=True`` renders nested content (a hyperlink's runs, a table cell) with no
    emphasis wrapping of its own: the decision belongs to the line that will hold it.
    """
    pieces, trailing = [], []
    for child in container:
        name = _local(child)
        if name == "r":
            marks = _marks(child)
            text = _run_text(child)
            if any(_local(node) == "drawing" for node in child.iter()):
                alt = _image_alt(child)
                text += f"![{alt or f'image {state.next_image()}'}]"
            if text:
                if pieces and pieces[-1][0] == marks:
                    pieces[-1][1] += text
                else:
                    pieces.append([marks, text])
            for node in child.iter():
                reference = _local(node)
                if reference not in ("footnoteReference", "endnoteReference"):
                    continue
                kind = "footnote" if reference == "footnoteReference" else "endnote"
                ordinal = notes.mark(kind, node.get(f"{W}id"))
                if ordinal:
                    trailing.append(f"[^{_MARKER[kind]}{ordinal}]")
        elif name == "hyperlink":
            inner, inner_trailing = _inline(child, notes, rels, state, plain=True)
            if inner:
                pieces.append([_PLAIN, inner])
            trailing.extend(inner_trailing)
            target = rels.get(child.get(f"{R}id", ""), "")
            if target and target not in inner:
                # Autolink form, after the line: ``[text](url)`` would put "](" inside the
                # sentence. A link whose text already is the URL needs nothing.
                trailing.append(f"<{target}>")
        elif name in ("ins", "smartTag", "sdt", "sdtContent", "bdo", "dir", "fldSimple"):
            # Preserve stored field results too; reading does not evaluate field instructions.
            inner, inner_trailing = _inline(child, notes, rels, state, plain=True)
            if inner:
                pieces.append([_PLAIN, inner])
            trailing.extend(inner_trailing)
        elif name == "del":
            continue  # a tracked deletion is text the author removed; it is not the document
    text = "".join(part for _, part in pieces)
    if plain:
        return text, trailing
    marked = {marks for marks, part in pieces if part.strip()}
    if len(marked) == 1 and any(next(iter(marked))):
        # Uniform across the whole line: the markers sit at the two ends, where they cost a
        # quotation of anything inside the line nothing.
        text = _wrap(text, next(iter(marked)))
    else:
        note = _emphasis_note(pieces)
        if note:
            trailing.append(note)
    return text, trailing


class _State:
    """Counters that run the length of one document: unnamed images, list numbering."""

    def __init__(self):
        self.images = 0
        self.counters = {}

    def next_image(self):
        self.images += 1
        return self.images

    def number(self, num_id, level):
        """The next ordinal for one numbered list at one level; deeper levels restart."""
        key = (num_id, level)
        value = self.counters.get(key, 0) + 1
        self.counters[key] = value
        for (other_id, other_level) in list(self.counters):
            if other_id == num_id and other_level > level:
                del self.counters[(other_id, other_level)]
        return value


class _Notes:
    """Footnote and endnote bodies, and the order the document referred to them in.

    The ordinal a reader sees is the position of the reference in the body, not the
    ``w:id`` in the package -- Word's ids start at 2 and survive deletions, so a document
    whose second footnote was deleted would otherwise print ``[^1]`` then ``[^3]``.
    """

    def __init__(self, bodies):
        self.bodies = bodies                    # {(kind, id): rendered text}
        self.order = {"footnote": {}, "endnote": {}}
        self.used = {"footnote": [], "endnote": []}

    def mark(self, kind, raw_id):
        if raw_id is None or (kind, raw_id) not in self.bodies:
            return None
        seen = self.order[kind]
        if raw_id not in seen:
            seen[raw_id] = len(seen) + 1
            self.used[kind].append(raw_id)
        return seen[raw_id]


def _list_of(paragraph, styles):
    """``(num_id, level, numbered)`` when this paragraph is a list item, else ``None``.

    ``w:numPr`` is authoritative; a paragraph carrying only a ``List Bullet`` style is a
    list item too, which is what a document written from Word's ribbon looks like.
    """
    properties = paragraph.find(f"{W}pPr")
    level, num_id = 0, None
    if properties is not None:
        num_pr = properties.find(f"{W}numPr")
        if num_pr is not None:
            level_node = num_pr.find(f"{W}ilvl")
            id_node = num_pr.find(f"{W}numId")
            if level_node is not None:
                level = int(level_node.get(f"{W}val") or 0)
            if id_node is not None:
                num_id = id_node.get(f"{W}val")
    style = _style_name(paragraph, styles).lower()
    if num_id is not None:
        # A ``w:numPr`` says a list, but not which kind; the style name is the only hint
        # python-docx exposes without resolving numbering.xml's abstract definitions.
        return (num_id, level, "number" in style or "numbered" in style)
    if style.startswith("list bullet"):
        return ("style-bullet", level, False)
    if style.startswith("list number"):
        return ("style-number", level, True)
    return None


def _style_name(paragraph, styles):
    properties = paragraph.find(f"{W}pPr")
    if properties is None:
        return ""
    node = properties.find(f"{W}pStyle")
    if node is None:
        return ""
    style_id = node.get(f"{W}val") or ""
    return styles.get(style_id, style_id)


def _heading_level(paragraph, styles):
    """The ``#`` depth for this paragraph, or ``None`` when it is body text."""
    name = _style_name(paragraph, styles).strip()
    if not name:
        return None
    if name.lower() in _TITLE_STYLES:
        return 1
    match = _HEADING.match(name)
    if match:
        return min(int(match.group(1)), 6)
    # Word also writes headings as the bare style id "Heading1" with no space.
    match = re.match(r"^heading(\d+)$", name.replace(" ", ""), re.IGNORECASE)
    return min(int(match.group(1)), 6) if match else None


def _cell_text(cell, notes, rels, state, styles):
    """One table cell as a single line: paragraphs joined, layout characters flattened.

    A tab or newline inside a cell would split the tab-separated row it sits in, so both
    become spaces -- the cell keeps its text and the row keeps its shape.
    """
    parts = []
    for child in cell:
        name = _local(child)
        if name == "p":
            # ``plain``: emphasis markers inside a cell would sit between two cells of the
            # same row once the tabs fold away, and a row is exactly what gets quoted.
            rendered = _line(*_inline(child, notes, rels, state, plain=True))
            if rendered:
                parts.append(rendered)
        elif name == "tbl":
            # A nested table is flattened into its parent cell: its rows would otherwise
            # claim columns of the outer table they do not belong to.
            for row in child.findall(f"{W}tr"):
                inner = [_cell_text(c, notes, rels, state, styles)
                         for c in row.findall(f"{W}tc")]
                joined = " ".join(part for part in inner if part)
                if joined:
                    parts.append(joined)
    return " ".join(parts).replace("\t", " ").replace("\n", " ").strip()


def _table_lines(table, notes, rels, state, styles):
    """A ``w:tbl`` as ``table RxC`` plus tab-separated rows.

    Merges: a horizontally merged cell carries ``w:gridSpan`` and occupies that many grid
    columns -- the text goes in the first and the rest stay empty. A vertically merged
    cell carries ``w:vMerge`` without ``restart``; it repeats the origin's text in the
    file, so it is emitted empty and only the origin keeps it. Repeating it would make one
    value look like several.
    """
    grid = table.find(f"{W}tblGrid")
    width = len(grid.findall(f"{W}gridCol")) if grid is not None else 0
    rows = []
    for row in table.findall(f"{W}tr"):
        cells = []
        for cell in row.findall(f"{W}tc"):
            properties = cell.find(f"{W}tcPr")
            span, continued = 1, False
            if properties is not None:
                span_node = properties.find(f"{W}gridSpan")
                if span_node is not None:
                    span = max(1, int(span_node.get(f"{W}val") or 1))
                merge = properties.find(f"{W}vMerge")
                if merge is not None and (merge.get(f"{W}val") or "continue") != "restart":
                    continued = True
            text = "" if continued else _cell_text(cell, notes, rels, state, styles)
            cells.append(text)
            cells.extend([""] * (span - 1))
        rows.append(cells)
    if not rows:
        return []
    width = max([width, *(len(row) for row in rows)])
    lines = [f"`table {len(rows)}×{width}`"]
    for row in rows:
        lines.append("\t".join(row + [""] * (width - len(row))))
    return lines


def _paragraph_line(paragraph, notes, rels, state, styles):
    """``(kind, line)`` for one ``w:p``, or ``None`` when it is empty.

    ``kind`` is "block", "list-bullet" or "list-number": consecutive items of the *same*
    kind are written without a blank line between them, because a blank line makes a
    markdown list loose and every reader of the page then sees a paragraph break inside
    what the document draws as one list. A change of marker does get the blank line -- a
    bullet list running straight into a numbered one is two lists.
    """
    text = _line(*_inline(paragraph, notes, rels, state))
    if not text:
        return None
    level = _heading_level(paragraph, styles)
    if level is not None:
        return ("block", "#" * level + " " + text)
    listing = _list_of(paragraph, styles)
    if listing is not None:
        num_id, depth, numbered = listing
        indent = "  " * depth
        if numbered:
            return ("list-number", f"{indent}{state.number(num_id, depth)}. {text}")
        return ("list-bullet", f"{indent}- {text}")
    return ("block", text)


def _styles(archive):
    """``{styleId: style name}`` from ``word/styles.xml``.

    The paragraph carries a style id and the heading test needs the name: an English Word
    writes ``w:pStyle w:val="Heading1"`` (id) whose name is "heading 1", and a document
    round-tripped through another editor can carry ``val="a3"`` for the same thing.
    """
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(archive.read("word/styles.xml"))
    except (KeyError, ET.ParseError):
        return {}
    out = {}
    for style in root.findall(f"{W}style"):
        style_id = style.get(f"{W}styleId")
        name = style.find(f"{W}name")
        if style_id and name is not None:
            out[style_id] = name.get(f"{W}val") or ""
    return out


def _rels(archive, part="document"):
    """External links for one Word part; relationship IDs are local to that part."""
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(archive.read(f"word/_rels/{part}.xml.rels"))
    except (KeyError, ET.ParseError):
        return {}
    package = "{http://schemas.openxmlformats.org/package/2006/relationships}"
    out = {}
    for rel in root.findall(f"{package}Relationship"):
        rid, target = rel.get("Id"), rel.get("Target")
        if rid and target and rel.get("TargetMode") == "External":
            out[rid] = target
    return out


def _note_bodies(archive):
    """``{(kind, id): [paragraph elements]}`` from footnotes.xml and endnotes.xml.

    python-docx has no API for either, which is why FA's fallback loses them entirely.
    """
    import xml.etree.ElementTree as ET
    out = {}
    for kind, member, tag in (("footnote", "word/footnotes.xml", "footnote"),
                              ("endnote", "word/endnotes.xml", "endnote")):
        try:
            root = ET.fromstring(archive.read(member))
        except (KeyError, ET.ParseError):
            continue
        for note in root.findall(f"{W}{tag}"):
            if (note.get(f"{W}type") or "") in _NOT_A_NOTE:
                continue
            note_id = note.get(f"{W}id")
            if note_id is not None:
                out[(kind, note_id)] = list(note)
    return out


def _note_lines(notes, note_rels, state, styles):
    """The footnote and endnote sections, in the order the body referred to them.

    They go in the same page flow as the body rather than into a sidecar, so ``verify_quote``
    can lock a quotation taken from a footnote the same way it locks one from a paragraph.
    """
    lines = []
    for kind, heading in (("footnote", "## Footnotes"), ("endnote", "## Endnotes")):
        used = notes.used[kind]
        if not used:
            continue
        rels = note_rels[kind]
        lines.extend(["", heading, ""])
        for raw_id in used:
            ordinal = notes.order[kind][raw_id]
            body = []
            for child in notes.bodies[(kind, raw_id)]:
                if _local(child) == "p":
                    rendered = _line(*_inline(child, notes, rels, state))
                    if rendered:
                        body.append(rendered)
                elif _local(child) == "tbl":
                    body.extend(_table_lines(child, notes, rels, state, styles))
            lines.append(f"[^{_MARKER[kind]}{ordinal}]: " + (" ".join(body) if body else ""))
    return lines


def title(path):
    """``dc:title`` from ``docProps/core.xml``, or ``""``.

    A downloaded paper is named whatever the URL ended in; the document knows better.
    """
    import xml.etree.ElementTree as ET
    try:
        with zipfile.ZipFile(path) as archive:
            root = ET.fromstring(archive.read("docProps/core.xml"))
    except (KeyError, OSError, ET.ParseError, zipfile.BadZipFile):
        return ""
    node = root.find(f"{DC}title")
    return (node.text or "").strip() if node is not None else ""


def render(path):
    """The whole document as markdown, in body order.

    Raises ``ValueError`` naming the file when python-docx cannot open it -- that string is
    what ``doc_add`` hands the model.
    """
    import xml.etree.ElementTree as ET
    try:
        with zipfile.ZipFile(str(path)) as archive:
            body = ET.fromstring(archive.read("word/document.xml")).find(f"{W}body")
            if body is None:
                raise ValueError("word/document.xml has no body")
            styles = _styles(archive)
            rels = _rels(archive)
            notes = _Notes(_note_bodies(archive))
            note_rels = {kind: _rels(archive, kind + "s") for kind in _MARKER}
    except (OSError, KeyError, ValueError, ET.ParseError, zipfile.BadZipFile) as error:
        raise ValueError(
            f"Cannot read {os.path.basename(str(path))}: {type(error).__name__}: {error}"
        ) from error

    def body_blocks(container):
        for child in container:
            if _local(child) in ("sdt", "sdtContent", "ins"):
                yield from body_blocks(child)
            else:
                yield child

    state = _State()
    lines = [
        ("<!-- docx readout: body order, so a table sits where the document puts it. "
         "A backtick span is parser-added, not file content. Table rows are tab-separated. "
         "Footnotes and endnotes are collected at the end and marked [^n]. "
         "Headers and footers are not rendered: they repeat on every page. -->"),
    ]
    previous = None
    for child in body_blocks(body):
        name = _local(child)
        if name == "p":
            rendered = _paragraph_line(child, notes, rels, state, styles)
            if rendered is None:
                continue
            kind, line = rendered
            if not (kind.startswith("list") and kind == previous):
                lines.append("")
            lines.append(line)
            previous = kind
        elif name == "tbl":
            table = _table_lines(child, notes, rels, state, styles)
            if table:
                lines.append("")
                lines.extend(table)
                previous = "block"
    lines.extend(_note_lines(notes, note_rels, state, styles))
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines) + "\n"
