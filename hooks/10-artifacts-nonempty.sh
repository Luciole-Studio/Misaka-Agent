#!/bin/sh
# 钩子硬闸示例：产物不许是空文件。$1=工作区。非零退出＝否决通过，卡被打回。
cd "$1" || exit 0
empty=$(python3 - <<'PY'
import json, os
try:
    r = json.load(open("report.json", encoding="utf-8"))
except Exception:
    raise SystemExit("")
print(" ".join(a for a in r.get("artifacts", [])
                if os.path.isfile(a) and os.path.getsize(a) == 0))
PY
)
[ -n "$empty" ] && { echo "空产物文件：$empty"; exit 2; }
exit 0
