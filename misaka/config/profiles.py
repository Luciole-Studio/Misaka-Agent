"""角色档案单一落位：~/.misaka/profiles/<角色>/。

照 pi 的处理：人格（SOUL.md、config.json）和技能/MCP（skills/、mcp/、config.yaml）
都是用户态数据，同居一个角色目录——pi 把 SYSTEM.md 与 skills/extensions 一并放
`~/.pi/agent/`，从不放进程序源码目录。内建分身类型是出厂默认，随包走
（misaka/extensions/subagent/agents/），用户可在 ~/.misaka/agent/agents/ 覆盖。

`<角色>` 是相对 profiles/ 的路径，如 `last_order`、`sisters/10032`。
"""
import os


def role_of(profile_dir):
    """profile 目录 → 角色名（相对 profiles/ 的路径）。"""
    p = os.path.abspath(profile_dir or "")
    marker = os.sep + "profiles" + os.sep
    return p.split(marker, 1)[1] if marker in p else os.path.basename(p)


def is_last_order(profile_dir):
    """该 profile 是否属于 Last Order（唯一没有子代理的角色）。"""
    role = role_of(profile_dir).strip().casefold().replace("-", "_").replace(" ", "_")
    return role == "last_order"


def skills(profile_dir):
    """该角色的技能目录清单（一个条目一个技能，可为软链）。"""
    d = os.path.join(profile_dir, "skills")
    if not os.path.isdir(d):
        return []
    return [os.path.join(d, x) for x in sorted(os.listdir(d)) if not x.startswith(".")]


def config_yaml(profile_dir):
    """该角色的 config.yaml 路径（MCP server 定义等）。"""
    return os.path.join(profile_dir, "config.yaml")


SHARED_SOUL_TEMPLATE = """# 御坂网络・共同魂

（Last Order 与全体 Sisters——含她们的分身——共同的开场约定：语气、价值观、纪律。
各角色自己的 SOUL.md 在这之后加载，个性与专长写在那边。改这个文件即全网生效。）

- 文件即真相：结论落盘成产物，不留在对话里。
- 查不到就明写「未能确证」，绝不编造出处。
"""


def shared_soul():
    """共同魂 ~/.misaka/profiles/MISAKA.md：LO 与 Sisters（含分身）共用的开场人格，
    装配序＝共同魂在前、角色 SOUL 在后。首次调用落骨架（绝不覆盖已有）。
    一次性角色（收割官/思辨红队/判官）不读它——审计姿态不受共同人格影响
    （2026-08-18 用户裁定）。返回路径。"""
    from misaka.config import CFG
    path = os.path.join(os.path.expanduser(CFG["roles_root"]), "MISAKA.md")
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(SHARED_SOUL_TEMPLATE)
    return path


if __name__ == "__main__":
    import sys
    import tempfile
    m = sys.modules[__name__]

    assert m.role_of("/x/.misaka/profiles/sisters/10032") == "sisters/10032"
    assert m.role_of("/x/.misaka/profiles/last_order") == "last_order"
    assert m.role_of("/tmp/孤立") == "孤立"           # 不在 profiles/ 下时退化为目录名
    assert m.is_last_order("/x/.misaka/profiles/last_order")
    assert m.is_last_order("/x/.misaka/profiles/Last-Order")
    assert not m.is_last_order("/x/.misaka/profiles/sisters/10032")

    prof = tempfile.mkdtemp()
    sd = os.path.join(prof, "skills")
    os.makedirs(os.path.join(sd, "ponytail"))
    open(os.path.join(sd, ".DS_Store"), "w").close()
    got = m.skills(prof)
    assert len(got) == 1 and got[0].endswith("ponytail"), got   # 点开头的要跳过
    assert m.skills("/x/misaka/profiles/没有的") == []
    assert m.config_yaml(prof) == os.path.join(prof, "config.yaml")

    from misaka.config import CFG
    CFG["roles_root"] = tempfile.mkdtemp()
    p = m.shared_soul()
    assert os.path.isfile(p) and "共同魂" in open(p, encoding="utf-8").read(), \
        "首跑落骨架"
    with open(p, "w", encoding="utf-8") as f:
        f.write("# 我自己写的")
    assert open(m.shared_soul(), encoding="utf-8").read() == "# 我自己写的", \
        "绝不覆盖用户已写的共同魂"
    print("profile_paths selfcheck ok — 角色名解析/技能扫描/点文件跳过/共同魂播种 均正确")
