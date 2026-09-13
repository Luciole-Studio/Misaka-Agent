"""Writing a deck: fourteen ops, applied in order to one file.

Ported from FrontierAgent's ``plugins/tools/_writer_pptx.py`` (audit D131-D148). Addressing
is a 1-based slide number plus a placeholder role, or anchor text for a find across the
whole deck.

Three fossils worth naming, because each is a silent-loss bug that a success receipt hides:

* ``_placeholder`` looks a placeholder up by its **semantic type**, not by index. Body and
  subtitle both live at index 1, so an index lookup makes writing both on one slide
  overwrite one with the other and report success for both;
* a font set on CJK text has to be written to ``a:ea`` as well. ``font.name`` writes only
  ``a:latin``, so ``"font": "SimSun"`` on Chinese text does nothing at all and the deck
  comes out in the theme font;
* ``duplicate_slide`` copies the source slide's relationships and rewrites the copied
  ``r:embed`` ids. Copying shape XML alone leaves every image pointing at a relationship
  the new slide does not have -- PowerPoint calls the file corrupt.
"""
from __future__ import annotations

import os

from misaka.core.tools._office._receipt import result
from misaka.core.tools._office._runs import norm_runs

SUFFIXES = frozenset({".pptx", ".pptm"})

LAYOUTS = {"title": 0, "title_and_content": 1, "section_header": 2,
           "two_content": 3, "title_only": 5, "blank": 6}

# Fallback placeholder index per role, used only when the semantic lookup finds nothing.
_ROLE_INDEX = {"title": 0, "body": 1, "subtitle": 1, "content": 1}

SHAPES = {"rectangle": "RECTANGLE", "rounded_rectangle": "ROUNDED_RECTANGLE",
          "oval": "OVAL", "ellipse": "OVAL", "diamond": "DIAMOND",
          "triangle": "ISOCELES_TRIANGLE", "right_arrow": "RIGHT_ARROW",
          "left_arrow": "LEFT_ARROW", "up_arrow": "UP_ARROW", "down_arrow": "DOWN_ARROW",
          "pentagon": "PENTAGON", "chevron": "CHEVRON", "star": "STAR_5_POINT",
          "cloud": "CLOUD", "heart": "HEART"}

CHARTS = {"bar": "BAR_CLUSTERED", "column": "COLUMN_CLUSTERED", "line": "LINE",
          "line_markers": "LINE_MARKERS", "pie": "PIE", "doughnut": "DOUGHNUT",
          "area": "AREA", "radar": "RADAR"}

SLIDE_SIZES = {"16:9": (13.333, 7.5), "widescreen": (13.333, 7.5),
               "4:3": (10, 7.5), "standard": (10, 7.5), "16:10": (10, 6.25)}

_RELATIONSHIPS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

OPS = ("create", "add_slide", "set_text", "add_textbox", "add_table", "add_image",
       "set_notes", "replace_text", "add_shape", "add_chart", "format_text",
       "duplicate_slide", "delete_slide", "set_slide_size")


def _rgb(color):
    from pptx.dml.color import RGBColor
    return RGBColor.from_string(str(color).lstrip("#").upper())


def _slide(presentation, number):
    """A 1-based slide number as a slide object."""
    slides = list(presentation.slides)
    index = int(number) - 1
    if index < 0 or index >= len(slides):
        raise ValueError(f"slide {number} out of range (this deck has {len(slides)})")
    return slides[index]


def _align(paragraph, value):
    if not value:
        return
    from pptx.enum.text import PP_ALIGN
    name = {"left": "LEFT", "center": "CENTER", "centre": "CENTER",
            "right": "RIGHT", "justify": "JUSTIFY"}.get(str(value).lower())
    if name:
        paragraph.alignment = getattr(PP_ALIGN, name)


def _east_asian(run, font):
    """Write the font to ``a:ea`` and ``a:cs`` as well as ``a:latin``.

    Without this a font named for Chinese, Japanese or Korean text has no effect whatsoever:
    those characters take their typeface from ``a:ea``, and python-pptx's ``font.name``
    never touches it.
    """
    from pptx.oxml.ns import qn
    properties = run._r.get_or_add_rPr()
    for tag in ("a:ea", "a:cs"):
        element = properties.find(qn(tag))
        if element is None:
            element = properties.makeelement(qn(tag), {})
            properties.append(element)
        element.set("typeface", font)


def _apply_runs(paragraph, text):
    from pptx.util import Pt
    for spec in norm_runs(text):
        run = paragraph.add_run()
        run.text = spec.get("text", "")
        font = run.font
        for field in ("bold", "italic", "underline"):
            if spec.get(field) is not None:
                setattr(font, field, bool(spec[field]))
        if spec.get("size"):
            font.size = Pt(float(spec["size"]))
        if spec.get("color"):
            font.color.rgb = _rgb(spec["color"])
        if spec.get("font"):
            font.name = spec["font"]
            _east_asian(run, spec["font"])
        if spec.get("link"):
            run.hyperlink.address = spec["link"]


def _no_bullet(paragraph):
    """Turn a paragraph's bullet off, for content that is already numbered or needs no dot."""
    from pptx.oxml.ns import qn
    properties = paragraph._p.get_or_add_pPr()
    for tag in ("a:buChar", "a:buAutoNum", "a:buNone"):
        for element in properties.findall(qn(tag)):
            properties.remove(element)
    properties.append(properties.makeelement(qn("a:buNone"), {}))


def _autofit(frame, mode):
    if not mode:
        return
    from pptx.enum.text import MSO_AUTO_SIZE
    frame.word_wrap = True
    wanted = str(mode).lower()
    if wanted in ("shrink", "shrink_text"):
        frame.auto_size = MSO_AUTO_SIZE.TEXT_TO_FIT_SHAPE
    elif wanted in ("resize", "resize_shape"):
        frame.auto_size = MSO_AUTO_SIZE.SHAPE_TO_FIT_TEXT
    elif wanted == "none":
        frame.auto_size = MSO_AUTO_SIZE.NONE


def _placeholder(slide, role):
    """The placeholder for a role, found by semantic type.

    Body and subtitle share index 1, so an index lookup returns the same shape for both:
    writing a subtitle and a body onto one slide then silently leaves only whichever ran
    last. The index is still the fallback, but it refuses to take a shape that belongs to
    another role.
    """
    from pptx.enum.shapes import PP_PLACEHOLDER
    wanted = {"title": {PP_PLACEHOLDER.TITLE, PP_PLACEHOLDER.CENTER_TITLE},
              "subtitle": {PP_PLACEHOLDER.SUBTITLE},
              "body": {PP_PLACEHOLDER.BODY, PP_PLACEHOLDER.OBJECT},
              "content": {PP_PLACEHOLDER.BODY, PP_PLACEHOLDER.OBJECT}}.get(role)
    avoid = {"body": {PP_PLACEHOLDER.SUBTITLE, PP_PLACEHOLDER.TITLE,
                      PP_PLACEHOLDER.CENTER_TITLE},
             "content": {PP_PLACEHOLDER.SUBTITLE, PP_PLACEHOLDER.TITLE,
                         PP_PLACEHOLDER.CENTER_TITLE},
             "subtitle": {PP_PLACEHOLDER.BODY, PP_PLACEHOLDER.OBJECT,
                          PP_PLACEHOLDER.TITLE, PP_PLACEHOLDER.CENTER_TITLE}}.get(role, set())
    shapes = list(slide.placeholders)
    if wanted:
        for shape in shapes:
            if shape.placeholder_format.type in wanted:
                return shape
    index = _ROLE_INDEX.get(role, 1)
    for shape in shapes:
        if (shape.placeholder_format.idx == index
                and shape.placeholder_format.type not in avoid):
            return shape
    return None


def _set_placeholder(slide, role, text):
    """Write rich text into a placeholder, falling back to a text box when there is none."""
    from pptx.util import Inches
    frame = None
    if role == "title" and slide.shapes.title is not None:
        frame = slide.shapes.title.text_frame
    else:
        shape = _placeholder(slide, role)
        if shape is not None:
            frame = shape.text_frame
    if frame is None:
        frame = slide.shapes.add_textbox(
            Inches(0.8), Inches(1.6), Inches(8), Inches(1)).text_frame
    frame.clear()
    _apply_runs(frame.paragraphs[0], text)


def _body(slide, body):
    """The body placeholder, from a structured list.

    ``body`` is ``{"items": [{"text", "level", "bullet"}], "autofit"}``, or the older plain
    list of strings. Levels and bullets are structure, not characters typed into the text.
    """
    from pptx.util import Inches
    if isinstance(body, dict):
        items, autofit = body.get("items", []), body.get("autofit")
    else:
        items, autofit = list(body or []), None
    shape = _placeholder(slide, "body")
    if shape is None:
        shape = slide.shapes.add_textbox(Inches(0.8), Inches(1.8), Inches(8.5), Inches(4))
    frame = shape.text_frame
    frame.clear()
    for index, item in enumerate(items):
        spec = {"text": item} if isinstance(item, str) else item
        paragraph = frame.paragraphs[0] if index == 0 else frame.add_paragraph()
        paragraph.level = int(spec.get("level", 0))
        _apply_runs(paragraph, spec.get("text", ""))
        if spec.get("bullet") is False:
            _no_bullet(paragraph)
    _autofit(frame, autofit)


def _table(slide, rows, x, y, width, height):
    from pptx.util import Inches
    graphic = slide.shapes.add_table(len(rows), len(rows[0]), Inches(x), Inches(y),
                                     Inches(width), Inches(height))
    for r, row in enumerate(rows):
        for c, value in enumerate(row):
            graphic.table.cell(r, c).text = str(value)
    return graphic


def _build(presentation, spec):
    layout = presentation.slide_layouts[
        LAYOUTS.get(spec.get("layout", "title_and_content"), 1)]
    slide = presentation.slides.add_slide(layout)
    if spec.get("title"):
        _set_placeholder(slide, "title", spec["title"])
    if spec.get("subtitle"):
        _set_placeholder(slide, "subtitle", spec["subtitle"])
    if spec.get("body") is not None:
        _body(slide, spec["body"])
    elif spec.get("bullets"):
        _body(slide, spec["bullets"])
    if spec.get("table"):
        _table(slide, spec["table"], 0.5, 2.0, 9, 3)
    if spec.get("notes"):
        frame = slide.notes_slide.notes_text_frame
        frame.clear()
        _apply_runs(frame.paragraphs[0], spec["notes"])
    return slide


def _duplicate(presentation, source):
    """A deep copy of one slide, appended to the deck, with its relationships rebuilt."""
    import copy
    new = presentation.slides.add_slide(source.slide_layout)
    for shape in list(new.shapes):
        shape._element.getparent().remove(shape._element)
    for shape in source.shapes:
        new.shapes._spTree.append(copy.deepcopy(shape._element))
    # Copying shape XML alone leaves every ``r:embed`` pointing at a relationship the new
    # slide does not have: the images vanish and PowerPoint reports the file as corrupt.
    mapping = {}
    for relation_id, relation in list(source.part.rels.items()):
        if relation.reltype.endswith(("/slideLayout", "/notesSlide")):
            continue
        fresh = (new.part.rels.get_or_add_ext_rel(relation.reltype, relation.target_ref)
                 if relation.is_external
                 else new.part.relate_to(relation.target_part, relation.reltype))
        if fresh != relation_id:
            mapping[relation_id] = fresh
    if mapping:
        for element in new.shapes._spTree.iter():
            for name, value in list(element.attrib.items()):
                if name.startswith("{" + _RELATIONSHIPS + "}") and value in mapping:
                    element.set(name, mapping[value])
    return new


def _format_text(slide, args):
    from pptx.util import Pt
    find = args["find"]
    hits = 0
    for shape in slide.shapes:
        if not shape.has_text_frame:
            continue
        for paragraph in shape.text_frame.paragraphs:
            for run in paragraph.runs:
                if not run.text or find not in run.text:
                    continue
                font = run.font
                for field in ("bold", "italic", "underline"):
                    if args.get(field) is not None:
                        setattr(font, field, bool(args[field]))
                if args.get("strike") is not None:
                    # python-pptx has no font.strike; the attribute lives on rPr.
                    run._r.get_or_add_rPr().set(
                        "strike", "sngStrike" if args["strike"] else "noStrike")
                size = args.get("font_size") or args.get("size")
                if size:
                    font.size = Pt(float(size))
                colour = args.get("font_color") or args.get("color")
                if colour:
                    font.color.rgb = _rgb(colour)
                hits += 1
    return result(f"formatted {hits} run(s) matching {find!r} on slide {args['slide']}",
                  warn=(f"0 runs matched {find!r}" if hits == 0 else None))


def write(path, op, args, *, overwrite=False):
    """Apply one op to a deck. Returns a result dict or an ``[error]`` string."""
    try:
        import pptx as _pptx
        from pptx.util import Inches
    except ImportError:                                             # pragma: no cover
        return "[error] python-pptx is not installed."

    if op == "create":
        if os.path.exists(path) and not (overwrite or args.get("overwrite")):
            return result(f"create refused: {path} already exists — use add_slide or set_text "
                          "to edit it, or pass overwrite:true to rebuild it", ok=False)
        slides = args.get("slides", [])
        if not slides:
            return result(f"create wrote nothing to {path}: no slides given", ok=False)
        presentation = _pptx.Presentation()
        for spec in slides:
            _build(presentation, spec)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        presentation.save(path)
        return result(f"created pptx: {path}", counts={"slide": len(slides)})

    if not os.path.exists(path):
        return f"[error] file not found (this op edits an existing file): {path}"
    presentation = _pptx.Presentation(path)

    def saved(summary):
        presentation.save(path)
        return summary

    if op == "add_slide":
        spec = {key: args[key] for key in
                ("layout", "title", "subtitle", "body", "bullets", "notes", "table")
                if key in args}
        _build(presentation, spec)
        if args.get("index") is not None:
            order = presentation.slides._sldIdLst
            moved = order[-1]
            order.remove(moved)
            order.insert(min(int(args["index"]) - 1, len(order)), moved)
        return saved(f"added slide ({spec.get('layout', 'title_and_content')})")

    if op == "replace_text":
        find = args.get("find")
        if not find:
            return "[error] replace_text needs 'find'."
        try:
            targets = ([_slide(presentation, args["slide"])] if args.get("slide")
                       else list(presentation.slides))
        except ValueError as error:
            return f"[error] {error}"
        done = 0
        for slide in targets:
            for shape in slide.shapes:
                if not shape.has_text_frame:
                    continue
                for paragraph in shape.text_frame.paragraphs:
                    for run in paragraph.runs:
                        if run.text and find in run.text:
                            run.text = run.text.replace(find, args.get("replace", ""))
                            done += 1
        presentation.save(path)
        return result(f"replaced {done} run(s) containing {find!r}",
                      warn=(f"0 matches for {find!r}" if done == 0 else None))

    if op == "set_slide_size":
        preset = str(args.get("preset") or args.get("aspect") or "").lower().replace("x", ":")
        if preset in SLIDE_SIZES:
            width, height = SLIDE_SIZES[preset]
        else:
            width = float(args.get("width_in", 13.333))
            height = float(args.get("height_in", 7.5))
        presentation.slide_width = Inches(width)
        presentation.slide_height = Inches(height)
        return saved(f"set slide size {width}in x {height}in ({preset or 'custom'})")

    if op == "delete_slide":
        order = presentation.slides._sldIdLst
        identifiers = list(order)
        index = int(args["slide"]) - 1
        if index < 0 or index >= len(identifiers):
            return f"[error] slide {args['slide']} out of range."
        order.remove(identifiers[index])
        return saved(f"deleted slide {args['slide']}")

    # Everything below addresses one existing slide. The op name is checked first: a typo
    # would otherwise be reported as a missing 'slide', which sends the model looking for
    # the wrong mistake.
    if op not in OPS:
        return f"[error] unknown pptx op {op!r}: this writes {', '.join(OPS)}."
    try:
        slide = _slide(presentation, args["slide"])
    except (KeyError, ValueError) as error:
        return f"[error] {op} needs a valid 'slide': {error}"

    if op == "set_text":
        role = args.get("placeholder", "body")
        _set_placeholder(slide, role, args["text"])
        return saved(f"set {role} on slide {args['slide']}")

    if op == "add_textbox":
        box = slide.shapes.add_textbox(
            Inches(args.get("x", 1)), Inches(args.get("y", 1)),
            Inches(args.get("w", 8)), Inches(args.get("h", 1)))
        frame = box.text_frame
        frame.clear()
        _apply_runs(frame.paragraphs[0], args.get("text", ""))
        _align(frame.paragraphs[0], args.get("align_h"))
        _autofit(frame, args.get("autofit"))
        return saved(f"added textbox on slide {args['slide']}")

    if op == "add_table":
        rows = args.get("rows")
        if not rows:
            return "[error] add_table needs 'rows'."
        _table(slide, rows, args.get("x", 0.5), args.get("y", 1.5),
               args.get("w", 9), args.get("h", 3))
        return saved(f"added table on slide {args['slide']}")

    if op == "add_image":
        source = args.get("image_path") or args.get("path")
        if not source or not os.path.exists(str(source)):
            return f"[error] add_image: no such image {source!r}"
        options = {}
        if args.get("w"):
            options["width"] = Inches(args["w"])
        if args.get("h"):
            options["height"] = Inches(args["h"])
        slide.shapes.add_picture(str(source), Inches(args.get("x", 1)),
                                 Inches(args.get("y", 1)), **options)
        return saved(f"added image on slide {args['slide']}")

    if op == "set_notes":
        frame = slide.notes_slide.notes_text_frame
        frame.clear()
        _apply_runs(frame.paragraphs[0], args.get("text", ""))
        return saved(f"set notes on slide {args['slide']}")

    if op == "add_shape":
        from pptx.enum.shapes import MSO_SHAPE
        from pptx.util import Pt
        name = str(args.get("shape", "rectangle")).lower()
        shape = slide.shapes.add_shape(
            getattr(MSO_SHAPE, SHAPES.get(name, "RECTANGLE")),
            Inches(args.get("x", 1)), Inches(args.get("y", 1)),
            Inches(args.get("w", 2)), Inches(args.get("h", 1)))
        if args.get("fill_color"):
            shape.fill.solid()
            shape.fill.fore_color.rgb = _rgb(args["fill_color"])
        if args.get("line_color"):
            shape.line.color.rgb = _rgb(args["line_color"])
        if args.get("text"):
            shape.text_frame.text = args["text"]
            run = shape.text_frame.paragraphs[0].runs[0]
            if args.get("font_color"):
                run.font.color.rgb = _rgb(args["font_color"])
            if args.get("font_size"):
                run.font.size = Pt(float(args["font_size"]))
        return saved(f"added {name} on slide {args['slide']}")

    if op == "add_chart":
        from pptx.chart.data import CategoryChartData
        from pptx.enum.chart import XL_CHART_TYPE
        data = CategoryChartData()
        data.categories = args.get("categories") or []
        for name, values in (args.get("series") or {}).items():
            data.add_series(name, [float(value) for value in values])
        graphic = slide.shapes.add_chart(
            getattr(XL_CHART_TYPE,
                    CHARTS.get(str(args.get("chart_type", "column")).lower(),
                               "COLUMN_CLUSTERED")),
            Inches(args.get("x", 1)), Inches(args.get("y", 1.5)),
            Inches(args.get("w", 8)), Inches(args.get("h", 4.5)), data)
        if args.get("title"):
            graphic.chart.has_title = True
            graphic.chart.chart_title.text_frame.text = args["title"]
        return saved(f"added {args.get('chart_type', 'column')} chart on slide {args['slide']}")

    if op == "format_text":
        outcome = _format_text(slide, args)
        presentation.save(path)
        return outcome

    _duplicate(presentation, slide)                  # the last op in OPS reaching here
    return saved(f"duplicated slide {args['slide']} -> "
                 f"slide {len(presentation.slides._sldIdLst)}")
