"""角色身份与职责段（hermes agent/system_prompt.py 的 stable_parts 结构严格对齐）。

hermes 原文（agent/system_prompt.py:377-397）的段序与语义：

    _soul = load_soul_md(...)
    if _soul: stable_parts.append(_soul); _soul_loaded = True
    if not _soul_loaded: stable_parts.append(DEFAULT_AGENT_IDENTITY)  # 空了兜底
    stable_parts.append(HERMES_AGENT_HELP_GUIDANCE)                   # 无条件
    if task_completion_guidance: stable_parts.append(TASK_COMPLETION_GUIDANCE)
    if tools_loaded: stable_parts.append(PARALLEL_TOOL_CALL_GUIDANCE) # 跟工具走

misaka 的对应（2026-08-20 用户裁定「完整对齐 hermes」）：

  ① 身份槽   SOUL.md 有内容就用它，**替换**默认身份；空/缺失则用 ROLE_IDENTITY
             ——所以 SOUL.md 是纯用户自定义槽，放空也不影响使用
  ② 职责段   ROLE_CHARTER 无条件注入（对应 HERMES_AGENT_HELP_GUIDANCE 的位置）。
             宪法级职责（②只发令不下场、③花钱须点头、⑥验收必经红队）不放身份槽，
             因为身份槽会被用户的 SOUL.md 整段替换掉
  ③ 共同魂   ~/.misaka/profiles/MISAKA.md（profiles.shared_soul，用户可编辑、可空）
  ④ 工具纪律 promptGuidelines，随工具注册贡献（misaka 已有此机制＝hermes 的
             「跟工具走」段）；工具清单由 promptSnippet 自动生成，任何角色档里都
             不该再手抄一份（2026-08-20 实测：LO 手抄的清单已漏掉 7 个工具）
"""
import os

# 通用兜底身份（对应 hermes DEFAULT_AGENT_IDENTITY）：角色没有专属默认身份时用它。
DEFAULT_IDENTITY = (
    "你是御坂网络（MISAKA）的一名 agent——面向人文社科的多 agent 研究系统。"
    "你直接、诚实、不铺陈：说你真做过的，查不到就明说查不到。"
)

# 各角色的默认身份（SOUL.md 为空时生效；用户写了 SOUL.md 就整段替换这里）。
ROLE_IDENTITY = {
    "last_order": (
        "你是 Last Order（最终指令）——御坂网络的编排官。"
        "用户跟你说话，妹妹们（Sisters）替你干活。"
        "成规模的活一律外包给妹妹，不要自己埋头做完。"
    ),
    "sisters": (
        "你是御坂网络的一名 Sister——领卡干活的研究员。"
        "你的产出是落盘的文件，不是对话里的结论。"
    ),
    "redteam": (
        "你是御坂网络的红队验收官。你只验收，不生产；拿不准算不过。"
    ),
    "synthesizer": (
        "你是御坂网络的综合器。你把已验收的产物合成一份报告，不引入外部新事实。"
    ),
    "harvester": (
        "你是御坂网络的收割官。你从已验收产物里抽取发现与缺口，只做抽取不做发挥。"
    ),
    "hypothesizer": (
        "你是御坂网络的假说综合官。你从高权重发现里溯因出可检验的假说。"
    ),
}

# 无条件段：宪法级职责与工作方式。用户的 SOUL.md 替换不掉这里（对齐 hermes 把
# HELP_GUIDANCE 放在身份段之后无条件 append 的做法）。
ROLE_CHARTER = {
    "last_order": """\
# 编排官职责（系统契约，不可由人格档覆盖）

你的 misaka_* 工具是**在册 Sister 的专用控制面**，不是通用 sub-agent：只许操作已经
上板、带验收合同的卡。你**没有** `Agent / TaskOutput / SendMessage / TaskStop` 这套
子代理工具——**Sisters 就是你的子代理**，要把活分出去只有建卡这一条道（带验收合同、
经红队），不能私开一个没人验收的分身。

工作方式：
1. **先弄清要什么再动手。** 用户说"研究 X"时，不清楚的地方就问：要多深、覆盖哪些面、
   有没有必须回答的具体问题。一次只问最要紧的一两个，别甩问卷。
2. **拆解前先下注**（赌注协议）：这份研究赌哪个反直觉判断成立？"赌主流归因把因果搞反了"
   胜过"赌能查清事实"。下注可以豪赌，兑现必须诚实——红队可以降低结论的证据等级，
   不许削你的赌注。赌注要能被证伪。
3. **建完卡停下来。** 把计划摊给用户：赌注是什么、拆几张卡、每张管什么、边界在哪。
   等他点头再开工——**开工要花真钱，用户没说开工就别跑。**
4. 派活工具只负责后台启动，**别把「已启动」说成「已完成」**。收到 `<sister-notification>`
   后先报告真实状态与 summary，再说下一步能干嘛（收割／看饱和读数／补缺口／综合），
   但同样等指令。开工后不用轮询；任务结束后追问会沿用原 task ID、工作区和会话，
   但会再花一次额度，同样先等用户明确点头。
5. **不替用户拍板**：开不开工、验收争议、要不要继续挖，都是他的决定。
""",
}


def _normalize(role):
    return (role or "").strip().casefold().replace("-", "_").replace(" ", "_")


def _role_key(role):
    """角色名 → 常量表键。`sisters/10032` 归到 `sisters`；纯编号（`10032`）同样——
    profiles.role_of 在路径不含 `profiles/` 时会退化成 basename，那时只剩编号。"""
    normalized = _normalize(role)
    if "/" in normalized:
        return normalized.split("/", 1)[0]
    return "sisters" if normalized.isdigit() else normalized


def read_soul(profile_dir):
    """角色 SOUL.md 的内容（纯用户自定义槽）。空文件/只有空白/不存在都算「没写」。"""
    path = os.path.join(profile_dir or "", "SOUL.md")
    if not profile_dir or not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8-sig") as f:
            content = f.read().strip()
    except OSError:
        return None
    return content or None


def prompt_sections(profile_dir, role=None):
    """身份槽＋职责段（hermes stable_parts 同序）。返回可直接喂
    `--append-system-prompt` 的文本列表（该旗文本与路径都吃）。"""
    key = _role_key(role if role is not None else os.path.basename(profile_dir or ""))
    soul = read_soul(profile_dir)
    identity = soul or ROLE_IDENTITY.get(key) or DEFAULT_IDENTITY
    sections = [identity]
    charter = ROLE_CHARTER.get(key)
    if charter:
        sections.append(charter)
    return sections


if __name__ == "__main__":
    import tempfile

    tmp = tempfile.mkdtemp()
    lo = os.path.join(tmp, "profiles", "last_order")
    os.makedirs(lo)

    # ① SOUL 缺失＝用角色默认身份，职责段照常在
    sections = prompt_sections(lo, "last_order")
    assert sections[0] == ROLE_IDENTITY["last_order"], sections[0][:40]
    assert "不可由人格档覆盖" in sections[1] and "没有" in sections[1]

    # ② 空文件＝等同没写（用户要的「放空也不影响使用」）
    soul_path = os.path.join(lo, "SOUL.md")
    open(soul_path, "w", encoding="utf-8").write("   \n\n")
    assert prompt_sections(lo, "last_order")[0] == ROLE_IDENTITY["last_order"], "空档要兜底"

    # ③ 写了就替换身份（hermes 语义：SOUL 是 identity 不是 append）
    open(soul_path, "w", encoding="utf-8").write("我是御坂，说话像猫。")
    got = prompt_sections(lo, "last_order")
    assert got[0] == "我是御坂，说话像猫。" and ROLE_IDENTITY["last_order"] not in got[0]
    assert "开工要花真钱" in got[1], "职责段替换不掉——宪法③不能被人格档抹掉"

    # ④ 带编号的 sister 归到 sisters 键；未知角色走通用兜底
    sis = os.path.join(tmp, "profiles", "sisters", "10032")
    os.makedirs(sis)
    assert prompt_sections(sis, "sisters/10032")[0] == ROLE_IDENTITY["sisters"]
    assert len(prompt_sections(sis, "sisters/10032")) == 1, "sister 无职责段（纪律跟工具走）"
    # role_of 在非标准路径下只给 basename（纯编号）——同样要认出是妹妹
    assert prompt_sections(sis, "10032")[0] == ROLE_IDENTITY["sisters"], "裸编号也归 sisters"
    unknown = os.path.join(tmp, "profiles", "nobody")
    os.makedirs(unknown)
    assert prompt_sections(unknown, "nobody") == [DEFAULT_IDENTITY]

    # ⑤ BOM 档不该被当成「写了」的乱码（utf-8-sig）
    open(soul_path, "w", encoding="utf-8-sig").write("带 BOM 的人格")
    assert prompt_sections(lo, "last_order")[0] == "带 BOM 的人格"

    print("identity selfcheck ok — 兜底/空档/替换语义/职责不可覆盖/角色键/BOM")
