"""摘要三级逃生梯＋熔断＋花费闸（hermes-lcm escalation.py 骨架移植，MIT）。

L1 保细节（预算=调用方给）→ L2 激进 bullet（预算×l2_ratio）→
**L3 确定性截断（零 LLM）**。熔断器打开或花费闸拉闸时 L1/L2 直接跳过——
失控压缩循环以零花费收敛，这是整条链的安全底。

call_llm 注入：`call_llm(prompt, max_tokens, timeout) -> str|None`（None＝失败）。
misaka 生产侧包 worker.run_llm_json(raw=True, bare=True)；测试打桩。零 misaka 依赖。
"""
import threading
import time

from misaka.orchestration.lcm.tokens import count_tokens

L1_PROMPT = """把下面的对话记录压缩成摘要，保住细节：已做的决定与理由、约束、
进行中的任务、文件路径、命令、具体数值与名称。查不到的不要编。
结尾必须有一行：「展开可见：<被压缩内容的一句话提示>」。

对话记录：
{text}"""

L2_PROMPT = """把下面的对话记录压成极简条目，只留四类：做了什么决定｜改了哪些文件｜
撞了什么错｜当前状态。丢掉全部推理过程与备选方案。
结尾必须有一行：「展开可见：<被压缩内容的一句话提示>」。

对话记录：
{text}"""

TRUNCATION_MARKER = "\n\n[……确定性截断——细节可经回收工具展开……]\n\n"


class SummaryCircuitBreaker:
    """连败熔断：failure_threshold 次连续失败后冷却 cooldown_seconds。"""

    def __init__(self, failure_threshold=2, cooldown_seconds=300.0):
        self._threshold = max(1, int(failure_threshold))
        self._cooldown = float(cooldown_seconds)
        self._failures = 0
        self._open_until = 0.0
        self._lock = threading.Lock()

    def allows(self):
        with self._lock:
            if self._open_until and time.monotonic() >= self._open_until:
                self._open_until = 0.0
                self._failures = 0
            return not self._open_until

    def record_failure(self):
        with self._lock:
            self._failures += 1
            if self._failures >= self._threshold:
                self._open_until = time.monotonic() + self._cooldown

    def record_success(self):
        with self._lock:
            self._failures = 0
            self._open_until = 0.0


class SummarySpendGuard:
    """滑窗花费闸：window 秒内超 max_calls 次即拉闸 backoff 秒。max_calls=0 禁用。"""

    def __init__(self, max_calls=24, window_seconds=600.0, backoff_seconds=1800.0):
        self._max = int(max_calls)
        self._window = float(window_seconds)
        self._backoff = float(backoff_seconds)
        self._calls = []
        self._blocked_until = 0.0
        self._lock = threading.Lock()

    def allows(self):
        if self._max <= 0:
            return True
        with self._lock:
            now = time.monotonic()
            if self._blocked_until:
                if now < self._blocked_until:
                    return False
                self._blocked_until = 0.0
                self._calls = []
            self._calls = [t for t in self._calls if now - t < self._window]
            if len(self._calls) >= self._max:
                self._blocked_until = now + self._backoff
                return False
            return True

    def record_call(self):
        if self._max > 0:
            with self._lock:
                self._calls.append(time.monotonic())


def deterministic_truncate(text, target_tokens):
    """L3：头尾各半＋中缝标记。**二分收敛**——CJK 命门：头（CJK 除数 1.5）＋
    标记（ASCII 除数 4）＋尾分别合格，拼起来仍可能超，估算器不可加。"""
    if count_tokens(text) <= target_tokens:
        return text
    lo, hi, best = 0, len(text) // 2, TRUNCATION_MARKER.strip()
    while lo <= hi:
        half = (lo + hi) // 2
        candidate = text[:half] + TRUNCATION_MARKER + (text[-half:] if half else "")
        if count_tokens(candidate) <= target_tokens:
            best = candidate
            lo = half + 1
        else:
            hi = half - 1
    return best


def summarize_with_escalation(text, *, source_tokens, token_budget, call_llm,
                              l2_budget_ratio=0.5, l3_truncate_tokens=512,
                              timeout=60.0, circuit_breaker=None, spend_guard=None):
    """返回 (summary_text, level)。level ∈ 1/2/3。
    接受条件：结果 token < 源 token（摘要必须真的变小）。"""
    attempts = (
        (1, L1_PROMPT, max(1, int(token_budget))),
        (2, L2_PROMPT, max(1, int(token_budget * l2_budget_ratio))),
    )
    for level, template, budget in attempts:
        if circuit_breaker is not None and not circuit_breaker.allows():
            break
        if spend_guard is not None and not spend_guard.allows():
            break
        if spend_guard is not None:
            spend_guard.record_call()
        try:
            result = call_llm(template.format(text=text), budget * 2, timeout)
        except Exception:  # noqa: BLE001 - 单级失败落下一级，绝不炸压缩
            result = None
        if result and result.strip() and count_tokens(result) < source_tokens:
            if circuit_breaker is not None:
                circuit_breaker.record_success()
            return result.strip(), level
        if circuit_breaker is not None:
            circuit_breaker.record_failure()
    return deterministic_truncate(text, l3_truncate_tokens), 3


if __name__ == "__main__":
    calls = []

    def good_llm(prompt, max_tokens, timeout):
        calls.append(max_tokens)
        return "决定：先查入藏簿。展开可见：搬迁记录细节"

    src = "对话内容 " * 500
    s, lv = summarize_with_escalation(src, source_tokens=count_tokens(src),
                                      token_budget=100, call_llm=good_llm)
    assert lv == 1 and "入藏簿" in s and calls == [200]

    def bloated_then_ok(prompt, max_tokens, timeout):
        calls.append(max_tokens)
        return src + src if len(calls) == 1 else "条目摘要。展开可见：略"

    calls.clear()
    s, lv = summarize_with_escalation(src, source_tokens=count_tokens(src),
                                      token_budget=100, call_llm=bloated_then_ok)
    assert lv == 2 and calls == [200, 100], "L1 没变小→降 L2（预算减半）"

    breaker = SummaryCircuitBreaker(failure_threshold=1, cooldown_seconds=60)
    breaker.record_failure()
    calls.clear()
    s, lv = summarize_with_escalation(src, source_tokens=count_tokens(src),
                                      token_budget=100, call_llm=good_llm,
                                      circuit_breaker=breaker)
    assert lv == 3 and not calls, "熔断开→零 LLM 调用直落 L3"
    assert count_tokens(s) <= 512 and "确定性截断" in s

    guard = SummarySpendGuard(max_calls=1, window_seconds=600, backoff_seconds=600)
    guard.record_call()
    calls.clear()
    _, lv = summarize_with_escalation(src, source_tokens=count_tokens(src),
                                      token_budget=100, call_llm=good_llm,
                                      spend_guard=guard)
    assert lv == 3 and not calls, "花费闸拉→零花费收敛"

    cjk = "档案记录内容详实" * 200 + "x" * 400
    t = deterministic_truncate(cjk, 100)
    assert count_tokens(t) <= 100 and "确定性截断" in t, "L3 二分收敛（CJK 混合）"
    assert deterministic_truncate("短", 100) == "短"

    def raising_llm(prompt, max_tokens, timeout):
        raise RuntimeError("503")

    _, lv = summarize_with_escalation(src, source_tokens=count_tokens(src),
                                      token_budget=100, call_llm=raising_llm)
    assert lv == 3, "调用炸了也要落 L3，绝不炸压缩"
    print("lcm escalation selfcheck ok — 三级梯/熔断/花费闸/L3 二分/异常兜底 全对")
