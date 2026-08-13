"""主题体检：misaka 是唯一主题，dark/light 是它黑白终端的两个面孔。
审两个变体色标齐、变量可解析、暗版零冷色残留、亮版白底可读、两变体键集一致。

起因：曾漏网 6 个冷色色标（如紫蓝 #9575cd），肉眼看不全，改成机器查。
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
    """蓝分量明显高于红＝还是冷色调，没换过来。"""
    rgb = _rgb(h)
    return bool(rgb) and rgb[2] > rgb[0] + 15


def luminance(h):
    rgb = _rgb(h)
    return None if rgb is None else (0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]) / 255


def audit(path):
    d = json.load(open(path, encoding="utf-8"))
    vars_, cols = d.get("vars", {}), d["colors"]
    unresolved = [k for k, v in cols.items()
                  if isinstance(v, str) and v and not v.startswith("#")
                  and not v.isdigit() and v not in vars_]
    resolved = {k: resolve(v, vars_) for k, v in cols.items()}
    return {"name": d.get("name"), "cols": cols, "resolved": resolved,
            "blue": [(k, v) for k, v in resolved.items() if bluish(v)],
            "unresolved": unresolved}


if __name__ == "__main__":
    dark, light = audit(DARK), audit(LIGHT)
    assert dark["name"] == "dark" and light["name"] == "light"
    for a in (dark, light):
        assert not a["unresolved"], f"{a['name']} 色标引用了不存在的变量: {a['unresolved']}"
    # 暗版零冷色残留（面板 accent 等直接取这里的值）
    assert not dark["blue"], f"暗版仍有冷色调色标: {dark['blue']}"
    # 两变体键集必须一致——面板 _load_palette 靠 vars 名字取色，缺一个就画错
    assert set(dark["cols"]) == set(light["cols"]), \
        f"两变体色标不一致: {set(dark['cols']) ^ set(light['cols'])}"
    dv = set(json.load(open(DARK, encoding="utf-8"))["vars"])
    lv = set(json.load(open(LIGHT, encoding="utf-8"))["vars"])
    assert dv == lv, f"两变体 vars 名不一致（面板按名取色）: {dv ^ lv}"
    # 亮版文字得在白底上够暗、暗版文字得在黑底上够亮——各自可读
    assert luminance(light["resolved"]["text"]) < 0.5, "亮版正文太浅，白底看不清"
    assert luminance(dark["resolved"]["text"]) > 0.5, "暗版正文太深，黑底看不清"
    print(f"theme selfcheck ok — misaka 暗/亮两变体各 {len(dark['cols'])} 色标齐、"
          f"变量可解析、暗版零冷色、键集一致、各自可读")
