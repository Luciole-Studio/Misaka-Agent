"""MoA（Mixture of Agents）内核件：配置＋参谋视图压平（设计 docs/design/moa.md）。

hermes 忠实移植（源 ~/.hermes/hermes-agent/agent/moa_loop.py，2026-08-18 精读）：
- 参谋（reference）＝不上场的分析师：无工具、不行动，只判断局面给意见；
  聚合官（aggregator）＝消化意见后真正干活的模型。
- 配置随角色档案走（R5）：`<profile_dir>/moa.json`，每角色一份，宽容读＋首跑落骨架。
- 视图压平规则逐条对齐 hermes _reference_messages；修剪一期用固定字符预算＋保尾
  （ponytail: 按参谋各自上下文窗动态修剪二期接 model registry）。

零 LLM 零 harn：扇出与命令面在 misaka/extensions/moa.py（二期件）。
"""
import json
import os

# 每条工具结果在参谋视图里的 head+tail 预览预算（hermes 同值：动作全保留、结果看头尾）
TOOL_RESULT_BUDGET = 4000
# 视图总字符预算：一期固定值（≈60k tokens 量级），超了丢最老、保尾（hermes 不变量同款）
MAX_VIEW_CHARS = 240_000
MAX_REFERENCES = 8      # 并行扇出上限（hermes _MAX_REFERENCE_WORKERS 同值）

# 参谋纪律（hermes _REFERENCE_SYSTEM_PROMPT 中文化）：不上场、绝不声称执行过
REFERENCE_SYSTEM_PROMPT = """你是 MoA（Mixture of Agents）流程中的参谋模型。你**不是**行动者，也不执行任何东西：
你不能调用工具、跑命令、浏览网页、访问文件/仓库/链接——不要尝试，也不要为此道歉。
真正持有这些能力并采取行动的是另一个聚合/编排模型。

铁律：你**绝不许**声称或暗示自己执行过任何操作（跑过命令、下载过文件、访问过链接）。
你只能基于对话上下文分析与建议。示例：
- 错：「我跑了 curl，得到 404。」
- 错：「我下载了那个文件，成功了。」
- 对：「按这个报错形态，对该链接发 curl 大概率返回 404。」
- 对：「从上下文看，下一步下载该文件可能有帮助。」

下面的对话是行动者正在处理的任务现状。你的工作：给出你对局面的最高水平分析——
理解目标、推理问题、建议下一步。指出最佳路线、具体步骤与工具使用策略、
可能的坑与风险、以及行动者可能遗漏或搞错的地方。对话里提到的文件/链接/系统
一律假定存在，基于上下文推理即可，不要索要访问权限。

直接给建议——不要开场白，不要关于工具或权限的免责声明。你的回答是交给聚合者的
私下参考，不是给用户看的答案。绝不声称执行过任何东西。"""

# 「请判断以上状态」合成尾轮（hermes _ADVISORY_INSTRUCTION）
ADVISORY_INSTRUCTION = ("[以上对话是任务的当前状态。给出你最高水平的判断：现在什么局面、"
                        "接下来该发生什么、你看到哪些风险或错误、行动者该怎么走。]")

# 综合官提示（hermes aggregate_moa_context synth_prompt，一次性 /moa 路径专用）
SYNTH_PROMPT = ("你是 MoA 流程的综合官。把各参谋的意见综合成简明可执行的指导，"
                "交给真正行动的主模型。聚焦：下一步、工具使用策略、风险、参谋间的分歧。"
                "除非一句话就能答完，否则不要直接回答用户——你产出的是主模型该参考的上下文。")


# ── 配置（R5：每角色一份 <profile_dir>/moa.json；宽容读＋骨架播种）────────────

SKELETON = {
    "default_preset": "default",
    "presets": {
        "default": {
            "enabled": True,
            "reference_models": [],
            "aggregator": {"provider": "", "model": ""},
            "reference_max_tokens": None,
            "reference_timeout": None,
            "degraded_reference_policy": "loud",
            "fanout": "user_turn",
        }
    },
}


def config_path(profile_dir):
    return os.path.join(os.path.expanduser(profile_dir or ""), "moa.json")


def ensure(profile_dir, provider="", model=""):
    """首跑落骨架（绝不覆盖已有）。骨架用本机当前 provider/model 填示例参谋，
    改文件即生效。返回路径。"""
    path = config_path(profile_dir)
    if not os.path.exists(path):
        skeleton = json.loads(json.dumps(SKELETON))
        if provider and model:
            skeleton["presets"]["default"]["reference_models"] = [
                {"provider": provider, "model": model, "enabled": True}]
            skeleton["presets"]["default"]["aggregator"] = {
                "provider": provider, "model": model}
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(skeleton, f, ensure_ascii=False, indent=2)
    return path


def _clean_slot(slot, include_enabled=False):
    """槽位清洗（hermes _clean_slot）：provider/model 必填；moa 拒（防递归）。"""
    if not isinstance(slot, dict):
        return None
    provider = str(slot.get("provider") or "").strip()
    model = str(slot.get("model") or "").strip()
    if not provider or not model or provider.lower() == "moa":
        return None
    clean = {"provider": provider, "model": model}
    effort = str(slot.get("reasoning_effort") or "").strip()
    if effort:
        clean["reasoning_effort"] = effort
    try:
        mt = int(slot.get("max_tokens"))
        if mt > 0:
            clean["max_tokens"] = mt
    except (TypeError, ValueError):
        pass
    if include_enabled:
        clean["enabled"] = bool(slot.get("enabled", True))
    return clean


def _int_or_none(v):
    try:
        n = int(v)
        return n if n > 0 else None
    except (TypeError, ValueError):
        return None


def _normalize_preset(raw):
    raw = raw if isinstance(raw, dict) else {}
    refs = [s for s in (_clean_slot(x, include_enabled=True)
                        for x in (raw.get("reference_models") or []))
            if s is not None]
    policy = str(raw.get("degraded_reference_policy") or "loud").strip().lower()
    return {
        "enabled": bool(raw.get("enabled", True)),
        "reference_models": refs,
        "aggregator": _clean_slot(raw.get("aggregator")),
        "reference_max_tokens": _int_or_none(raw.get("reference_max_tokens")),
        "reference_timeout": _int_or_none(raw.get("reference_timeout")),
        "degraded_reference_policy": policy if policy in ("loud", "silent") else "loud",
        "fanout": str(raw.get("fanout") or "user_turn"),   # 一期落字段不落逻辑（二期节律用）
    }


def load(profile_dir):
    """读该角色的 MoA 配置（宽容：坏文件/缺文件＝空 presets，不炸）。"""
    try:
        with open(config_path(profile_dir), encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        raw = {}
    raw = raw if isinstance(raw, dict) else {}
    presets = {str(k).strip(): _normalize_preset(v)
               for k, v in (raw.get("presets") or {}).items()
               if str(k).strip() and isinstance(v, dict)}
    default = str(raw.get("default_preset") or "").strip()
    if default not in presets:
        default = next(iter(presets), "default")
    return {"default_preset": default, "presets": presets}


def resolve(profile_dir, name=None):
    """取 preset。返回 (preset, None) 或 (None, 人话错误)。
    没配参谋/没配聚合官都算没就绪——/moa 提示编辑文件，不硬跑。"""
    cfg = load(profile_dir)
    wanted = str(name or cfg["default_preset"] or "default").strip()
    preset = cfg["presets"].get(wanted)
    if preset is None:
        avail = "、".join(cfg["presets"]) or "(空)"
        return None, f"没有 MoA preset「{wanted}」。已配：{avail}；编辑 {config_path(profile_dir)}"
    refs = [s for s in preset["reference_models"] if s.get("enabled", True)]
    if not refs:
        return None, f"preset「{wanted}」没有可用参谋——编辑 {config_path(profile_dir)} 填 reference_models"
    if preset["aggregator"] is None:
        return None, f"preset「{wanted}」没配综合官（aggregator）——编辑 {config_path(profile_dir)}"
    return {**preset, "reference_models": refs[:MAX_REFERENCES], "name": wanted}, None


def slot_label(slot):
    """`provider:model[reasoning=x]`（hermes _slot_label 同形）。"""
    label = f"{slot.get('provider', '')}:{slot.get('model', '')}"
    effort = str(slot.get("reasoning_effort") or "").strip()
    return f"{label}[reasoning={effort}]" if effort else label


# ── 参谋视图压平（hermes _reference_messages 逐条；按 misaka 消息块形状实现）──

def _flatten_text(content):
    """str 原样；块列表抽 text 块拼接（图等非文本块跳过）。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(b.get("text", "") for b in content
                        if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _render_tool_calls(content):
    """assistant 块列表里的 toolCall → `[called tool: name(args)]` 文本行。"""
    lines = []
    for b in content if isinstance(content, list) else []:
        if not (isinstance(b, dict) and b.get("type") == "toolCall"):
            continue
        name = b.get("name") or "tool"
        args = b.get("arguments")
        if isinstance(args, str):
            args_text = args
        elif args:
            try:
                args_text = json.dumps(args, ensure_ascii=False)
            except (TypeError, ValueError):
                args_text = str(args)
        else:
            args_text = ""
        lines.append(f"[called tool: {name}({args_text})]" if args_text
                     else f"[called tool: {name}]")
    return "\n".join(lines)


def _truncate_result(text, budget=TOOL_RESULT_BUDGET):
    """head+tail 预览（hermes _truncate_tool_result 同款）。"""
    if not text or len(text) <= budget:
        return text
    half = budget // 2
    return f"{text[:half]}\n[... 省略 {len(text) - 2 * half} 字符 ...]\n{text[-half:]}"


def advisory_view(messages):
    """对话 → 参谋视图：纯 user/assistant 文本轮，零 tool 角色零 tool_calls 数组。

    规则（hermes 逐条）：system 剥掉｜assistant 文本＋toolCall 文本化｜
    工具结果 head+tail 折进前一 assistant 轮｜空轮清洗（结构化无文本→占位，
    纯空串→丢）｜尾部必须 user 轮（合成「判断以上状态」）｜全空退化到最后一条 user。
    """
    rendered = []
    last_user = None
    for msg in messages or []:
        role = msg.get("role")
        content = msg.get("content")
        text = _flatten_text(content)
        if role == "system":
            continue
        if role == "user":
            if not text.strip() and isinstance(content, list) and content:
                text = "[用户发来非文本内容（如图片附件）]"
            if not text.strip():
                continue
            last_user = text
            rendered.append({"role": "user", "content": text})
        elif role == "assistant":
            parts = []
            if text.strip():
                parts.append(text.strip())
            calls = _render_tool_calls(content)
            if calls:
                parts.append(calls)
            if parts:
                rendered.append({"role": "assistant", "content": "\n".join(parts)})
        elif role in ("toolResult", "tool"):
            block = f"[tool result: {_truncate_result(text)}]"
            if rendered and rendered[-1]["role"] == "assistant":
                rendered[-1]["content"] += "\n" + block
            else:
                rendered.append({"role": "assistant", "content": block})
    if rendered and rendered[-1]["role"] == "assistant":
        rendered.append({"role": "user", "content": ADVISORY_INSTRUCTION})
    if not rendered and last_user is not None:
        rendered = [{"role": "user", "content": last_user}]
    return _trim_view(rendered)


def _trim_view(rendered, budget=MAX_VIEW_CHARS):
    """超预算丢最老（hermes 不变量：保 user-first、保尾轮＋至少一条前轮）。"""
    def total(msgs):
        return sum(len(m["content"]) for m in msgs)

    out = list(rendered)
    while len(out) > 2 and total(out) > budget:
        out.pop(0)
        while len(out) > 2 and out[0]["role"] == "assistant":
            out.pop(0)
    while len(out) > 1 and out[0]["role"] == "assistant":
        out.pop(0)
    return out


# ── 便签与拼块（hermes _is_failed_reference / joined）────────────────────────

def is_failed_note(text):
    s = (text or "").lstrip().lower()
    return s.startswith("[failed:") or s.startswith("[skipped:")


def render_references(outputs):
    """成功参谋 → `Reference N — label:` 标签块；返回 (joined, failed_labels)。"""
    ok = [(label, text) for label, text in outputs if not is_failed_note(text)]
    failed = [label for label, text in outputs if is_failed_note(text)]
    joined = "\n\n".join(f"Reference {i} — {label}:\n{text}"
                         for i, (label, text) in enumerate(ok, start=1))
    return joined, failed


if __name__ == "__main__":
    import tempfile

    prof = tempfile.mkdtemp()
    # 播种：落骨架、不覆盖
    p = ensure(prof, "anthropic", "claude-opus-5")
    cfg = load(prof)
    assert cfg["default_preset"] == "default"
    assert cfg["presets"]["default"]["reference_models"][0]["model"] == "claude-opus-5"
    with open(p, "w", encoding="utf-8") as f:
        f.write('{"presets": {"mine": {"reference_models": [{"provider": "x", "model": "y"}], "aggregator": {"provider": "x", "model": "z"}}}}')
    assert ensure(prof) == p and load(prof)["default_preset"] == "mine", "绝不覆盖用户已写的"

    # 清洗：缺 provider/model 拒、moa 防递归拒、enabled 保留
    assert _clean_slot({"provider": "a"}) is None
    assert _clean_slot({"provider": "moa", "model": "m"}) is None, "moa 不能当槽位（防递归）"
    assert _clean_slot({"provider": "a", "model": "b", "max_tokens": "600"})["max_tokens"] == 600
    preset, err = resolve(prof)
    assert err is None and preset["name"] == "mine" and len(preset["reference_models"]) == 1
    _, err = resolve(prof, "没有的")
    assert "没有 MoA preset" in err
    with open(p, "w", encoding="utf-8") as f:
        f.write('{"presets": {"mine": {"reference_models": [], "aggregator": {"provider": "x", "model": "z"}}}}')
    assert "没有可用参谋" in resolve(prof)[1]
    with open(p, "w", encoding="utf-8") as f:
        f.write("烂 JSON{{{")
    assert load(prof)["presets"] == {}, "坏文件宽容读，不炸"

    # 压平：剥 system、toolCall 文本化、结果折叠＋截断、尾 user 轮、空轮清洗
    long_result = "行1\n" + "x" * 9000 + "\n行尾"
    view = advisory_view([
        {"role": "system", "content": "8K 系统提示"},
        {"role": "user", "content": "查明搬迁性质"},
        {"role": "assistant", "content": [
            {"type": "text", "text": "先读档案"},
            {"type": "toolCall", "name": "read", "arguments": {"path": "a.md"}}]},
        {"role": "toolResult", "content": [{"type": "text", "text": long_result}]},
        {"role": "user", "content": ""},
        {"role": "assistant", "content": [{"type": "text", "text": "档案显示 1954 年"}]},
    ])
    assert all(m["role"] in ("user", "assistant") for m in view), "零 tool 角色"
    assert "系统提示" not in json.dumps(view, ensure_ascii=False), "system 必剥"
    assert '[called tool: read({"path": "a.md"})]' in view[1]["content"]
    assert "[tool result: 行1" in view[1]["content"] and "行尾]" in view[1]["content"]
    assert "省略" in view[1]["content"], "长结果 head+tail 截断"
    assert view[-1] == {"role": "user", "content": ADVISORY_INSTRUCTION}, "尾部必须 user 轮"
    assert len(view) == 4, view   # 空 user 轮被清洗
    # 图片轮占位；全空退化
    v2 = advisory_view([{"role": "user", "content": [{"type": "image", "url": "x"}]}])
    assert "非文本内容" in v2[0]["content"]
    v3 = advisory_view([{"role": "user", "content": "唯一问题"},
                        {"role": "assistant", "content": [{"type": "text", "text": "答"}]}])
    assert v3[-1]["content"] == ADVISORY_INSTRUCTION
    # 修剪：丢最老保尾、user-first
    big = [{"role": "user", "content": "老问题" * 100}]
    for i in range(40):
        big += [{"role": "assistant", "content": f"步骤{i}" + "y" * 20000}]
    trimmed = _trim_view(advisory_view(big), budget=50_000)
    assert sum(len(m["content"]) for m in trimmed) <= 50_000 + 21_000, "预算生效（保尾容忍）"
    assert trimmed[0]["role"] == "user" or len(trimmed) <= 2, "user-first 不变量"
    assert trimmed[-1]["content"] == ADVISORY_INSTRUCTION, "尾轮永远保住"

    # 便签与拼块
    joined, failed = render_references([
        ("a:m1", "意见一"), ("b:m2", "[failed: 超时]"), ("c:m3", "意见二")])
    assert "Reference 1 — a:m1" in joined and "Reference 2 — c:m3" in joined
    assert failed == ["b:m2"] and "[failed" not in joined, "便签不进拼块"
    assert slot_label({"provider": "a", "model": "m", "reasoning_effort": "high"}) == "a:m[reasoning=high]"
    print("moa selfcheck ok — 配置播种/宽容读/防递归/压平七规则/修剪不变量/便签拼块 全对")
