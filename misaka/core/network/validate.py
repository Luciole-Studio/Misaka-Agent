"""Small schema validators for task cards and model JSON."""
import json


def extract_json(text):
    """Extract the first decodable JSON object or array from model output.

    ``strict=False``: a model writing a long Markdown document into a JSON string will,
    now and then, put a real newline or tab inside it instead of ``\\n`` -- Last Order's
    PROJECT.md draft came back with four, and the strict decoder answered "no json in
    output" for a document that was otherwise complete. Non-strict decoding differs from
    strict in exactly that one way (control characters inside strings are accepted); every
    strictly valid document decodes the same.
    """
    dec = json.JSONDecoder(strict=False)
    text = text or ""
    depth, in_string, escaped = 0, False, False
    for i, ch in enumerate(text):
        if depth:
            # Never turn a malformed outer response into a valid nested fragment.
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
            elif ch == '"':
                in_string = True
            elif ch in "[{":
                depth += 1
            elif ch in "]}":
                depth -= 1
        elif ch in "[{":
            try:
                obj, _ = dec.raw_decode(text[i:])
                return obj
            except ValueError:
                pass
            try:
                obj, _ = dec.raw_decode(_escape_prose_quotes(text[i:]))
                return obj
            except ValueError:
                depth = 1
    return None


def _escape_prose_quotes(text):
    """Escape the double quotes a model leaves bare inside JSON string values.

    A research plan came back as one ``{"plan_markdown": "..."}`` whose Markdown quoted a
    phrase -- ``默认接受"埃及成功、印度失败"的通行叙事`` -- with the quotes unescaped. The
    object failed to decode, the scan above moved on *into* it and returned the first
    fragment that did decode, ``"clarifying_questions": []`` -- an empty list -- and the
    planner reported "planning output is not an object" for a plan that was all there.

    The rule is the one a reader applies: inside a string, a quote followed (after
    whitespace) by a structural character -- ``,`` ``}`` ``]`` ``:`` -- or by the end of
    the text closes the string; any other quote is part of the prose and gets escaped.
    A prose quote that happens to sit right before a comma is misread as a terminator,
    and the decode then fails as it did before; nothing valid is ever changed, because a
    valid document's string-closing quotes all satisfy the rule.
    """
    out = []
    in_string = False
    escaped = False
    for index, ch in enumerate(text):
        if not in_string:
            if ch == '"':
                in_string = True
            out.append(ch)
            continue
        if escaped:
            out.append(ch)
            escaped = False
            continue
        if ch == "\\":
            out.append(ch)
            escaped = True
            continue
        if ch == '"':
            after = index + 1
            while after < len(text) and text[after] in " \t\r\n":
                after += 1
            if after >= len(text) or text[after] in ",}]:":
                in_string = False
                out.append(ch)
            else:
                out.append('\\"')
            continue
        out.append(ch)
    return "".join(out)


def validate_cards(obj, sisters):
    """Validate task cards and apply safe defaults."""
    errors = []
    if not isinstance(obj, list) or not 1 <= len(obj) <= 6:
        return [], [f"Expected a JSON array containing 1–6 cards; got {type(obj).__name__}."]
    cards = []
    for i, c in enumerate(obj):
        if not isinstance(c, dict):
            errors.append(f"card {i}: expected an object")
            continue
        title, body, assignee = c.get("title"), c.get("body"), c.get("assignee")
        if not (isinstance(title, str) and title.strip()):
            errors.append(f"card {i}: missing title")
        if not (isinstance(body, str) and '## acceptance criteria' in body.lower()):
            errors.append(f"card {i}: body must contain a testable `## acceptance criteria` section")
        if assignee not in sisters:
            errors.append(f"card {i}: assignee={assignee} not in the roster {sorted(sisters)}")
        cards.append({
            "title": (title or "").strip(), "body": body or "", "assignee": assignee,
            "model": c.get("model"), "priority": int(c.get("priority") or 0),
        })
    return cards, errors
