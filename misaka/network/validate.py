"""Small schema validators for task cards and reports."""
import json


def extract_json(text):
    """Extract the first decodable JSON object or array from model output."""
    dec = json.JSONDecoder()
    for i, ch in enumerate(text or ""):
        if ch in "[{":
            try:
                obj, _ = dec.raw_decode(text[i:])
                return obj
            except ValueError:
                continue
    return None


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
        if not (isinstance(body, str) and '## acceptance criteria' in body):
            errors.append(f"card {i}: body must contain a testable `## acceptance criteria` section")
        if assignee not in sisters:
            errors.append(f"card {i}: assignee={assignee} not in the roster {sorted(sisters)}")
        cards.append({
            "title": (title or "").strip(), "body": body or "", "assignee": assignee,
            "model": c.get("model"), "priority": int(c.get("priority") or 0),
            "timeout": min(max(int(c.get("timeout") or 900), 60), 3600),
        })
    return cards, errors


def validate_verdict(obj):
    """Return validation errors for a reviewer verdict; empty means valid."""
    if not isinstance(obj, dict):
        return ["verdict must be an object"]
    errors = []
    if not isinstance(obj.get("pass"), bool):
        errors.append("pass must be a boolean")
    for k in ("reasons", "must_fix"):
        if not (isinstance(obj.get(k), list) and all(isinstance(x, str) for x in obj[k])):
            errors.append(f"{k} must be an array of strings")
    if obj.get("pass") is False and not obj.get("must_fix"):
        errors.append("must_fix cannot be empty when pass is false")
    return errors
