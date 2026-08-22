#!/bin/sh
# Hard-gate example: an artifact must not be an empty file.
# $1 is the workspace. A nonzero exit vetoes acceptance and the card is sent back.
cd "$1" || exit 0
empty=$(python3 - <<'PY'
import json, os
try:
    r = json.load(open(os.environ.get("MISAKA_REPORT", "report.json"), encoding="utf-8"))
except Exception:
    raise SystemExit("")
print(" ".join(a for a in r.get("artifacts", [])
                if os.path.isfile(a) and os.path.getsize(a) == 0))
PY
)
[ -n "$empty" ] && { echo "Empty artifact file: $empty"; exit 2; }
exit 0
