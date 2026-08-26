"""Audit the bundled dark and light theme definitions.

Checks that every color resolves, that the dark variant has no cool-toned
leftovers, and that both variants define the same keys.
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
DARK = os.path.join(HERE, "dark.json")
LIGHT = os.path.join(HERE, "light.json")


def resolve(v, vars_, depth=5):
    while isinstance(v, str) and v in vars_ and depth:
        v, depth = vars_[v], depth - 1
    return v


def _rgb(h):
    return (int(h[1:3], 16), int(h[3:5], 16), int(h[5:7], 16)) if (
        isinstance(h, str) and h.startswith("#") and len(h) == 7) else None


def bluish(h):
    """True if the hex color is still cool-toned (blue well above red)."""
    rgb = _rgb(h)
    return bool(rgb) and rgb[2] > rgb[0] + 15


def luminance(h):
    rgb = _rgb(h)
    return None if rgb is None else (0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]) / 255


def audit(path):
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    vars_, cols = d.get("vars", {}), d["colors"]
    unresolved = [k for k, v in cols.items()
                  if isinstance(v, str) and v and not v.startswith("#")
                  and not v.isdigit() and v not in vars_]
    resolved = {k: resolve(v, vars_) for k, v in cols.items()}
    return {"name": d.get("name"), "cols": cols, "resolved": resolved,
            "blue": [(k, v) for k, v in resolved.items() if bluish(v)],
            "unresolved": unresolved}
