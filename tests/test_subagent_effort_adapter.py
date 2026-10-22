"""The sub-agent effort hand-off (``install_effort_adapter``): ``subagents.effort_level``, or an agent definition's
``effort:``, reaches the request pi built for the sub-agent's model.

pi 1.3.0 moved kimi-coding to openai-completions and stopped sending ``reasoning_effort`` to Xiaomi (#10818). The
adapter must never put a second reasoning control next to pi's own (Kimi answers ``thinking`` plus
``reasoning_effort`` with a 400), must send nothing to a model that does not reason, and writes an effort only in the
field, and with the value, pi itself writes for that level on that model.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import json
import re
from types import SimpleNamespace
from typing import Any

import pytest

from misaka.ai.api._common import _compat_value
from misaka.ai.compat import getModel, getModels, getProviders, streamSimple
from misaka.ai.models import clampThinkingLevel
from misaka.ai.types import Context, UserMessage
from misaka.config import product
from misaka.core.subagent.model import EFFORT_LEVELS, install_effort_adapter


def _wire(monkeypatch, model, payload):
    monkeypatch.setattr(product, "setting", lambda *args, **kwargs: None)  # no user setting: the hand-off decides
    agent = SimpleNamespace(onPayload=None)
    install_effort_adapter(agent, {"MISAKA_SUBAGENT_EFFORT": '"low"'})
    return asyncio.run(agent.onPayload(dict(payload), model))


@pytest.mark.parametrize(
    ("provider", "model_id"),
    [("kimi-coding", "kimi-for-coding-highspeed"), ("xiaomi", "mimo-v2.5")],
)
def test_a_model_whose_compat_refuses_reasoning_effort_keeps_only_pis_thinking_control(monkeypatch, provider, model_id):
    model = getModel(provider, model_id)
    assert model is not None and model.api == "openai-completions"
    assert _wire(monkeypatch, model, {"thinking": {"type": "enabled"}}) == {"thinking": {"type": "enabled"}}


def test_a_model_that_takes_reasoning_effort_still_gets_the_hand_off(monkeypatch):
    k3 = getModel("kimi-coding", "k3")
    assert _wire(monkeypatch, k3, {}) == {"reasoning_effort": "low"}
    # pi's own value wins over the hand-off.
    assert _wire(monkeypatch, k3, {"reasoning_effort": "high"}) == {"reasoning_effort": "high"}



# --- every catalog model, every session thinking level, every effort setting ------------------------------------

# Where pi's providers put reasoning, as paths into the request: openai-completions (toggles and efforts by
# `thinkingFormat`), openai-responses/codex (`reasoning`), anthropic-messages (`thinking`, `output_config`), Bedrock
# (`additionalModelRequestFields`), and the ant-only numeric override.
_CONTROLS = (
    ("thinking",), ("reasoning_effort",), ("reasoning",), ("enable_thinking",), ("chat_template_kwargs",),
    ("chat_template_args",), ("output_config",), ("extra_body", "anthropic_internal"),
    ("additionalModelRequestFields", "thinking"), ("additionalModelRequestFields", "output_config"),
    ("additionalModelRequestFields", "reasoning_config"), ("additionalModelRequestFields", "anthropic_internal"),
)
_EFFORTS = (*EFFORT_LEVELS, 3)  # every named effort, and a numeric one
_SESSION_LEVELS = (None, "minimal", "low", "medium", "high", "xhigh", "max")


class _Built(Exception):
    pass


def _at(payload: Any, path: tuple[str, ...]) -> Any:
    for key in path:
        if not isinstance(payload, dict) or key not in payload:
            return None
        payload = payload[key]
    return payload


def _leaves(value: Any, path: tuple[str, ...] = ()) -> dict[tuple[str, ...], Any]:
    if isinstance(value, dict) and value:
        return {leaf: item for key, child in value.items() for leaf, item in _leaves(child, (*path, key)).items()}
    return {path: value}


def _controls(payload: dict[str, Any]) -> dict[tuple[str, ...], Any]:
    """Every reasoning leaf of the request, by path."""
    return {
        leaf: value
        for control in _CONTROLS
        if _at(payload, control) is not None
        for leaf, value in _leaves(_at(payload, control), control).items()
    }


def _jwt() -> str:
    claims = {"https://api.openai.com/auth": {"chatgpt_account_id": "account"}}
    segment = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"e30.{segment}.signature"


async def _pis_request(model: Any, level: str | None) -> dict[str, Any] | None:
    """The request pi builds for ``model`` at the session thinking ``level``, captured at ``onPayload``."""
    captured: dict[str, Any] = {}

    def capture(payload: dict[str, Any], _model: Any) -> None:
        captured["payload"] = copy.deepcopy(payload)
        raise _Built

    options: dict[str, Any] = {
        "apiKey": _jwt() if model.api == "openai-codex-responses" else "test-key",
        "onPayload": capture,
        "env": {"AZURE_OPENAI_BASE_URL": "https://example.openai.azure.com/openai/v1", "AWS_ACCESS_KEY_ID": "test",
                "AWS_SECRET_ACCESS_KEY": "test", "AWS_REGION": "us-east-1"},
    }
    if level is not None:
        options["reasoning"] = level
    await streamSimple(model, Context(messages=[UserMessage(content="hi", timestamp=1)]), options).result()
    return captured.get("payload")


def _adapter(monkeypatch, effort: Any) -> Any:
    monkeypatch.setattr(product, "setting", lambda *args, **kwargs: None)
    agent = SimpleNamespace(onPayload=None)
    install_effort_adapter(agent, {"MISAKA_SUBAGENT_EFFORT": json.dumps(effort), "USER_TYPE": "ant"})
    return agent.onPayload


def _expected_off_effort(model: Any, effort: str, controls: dict[Any, Any], requests: dict[Any, Any]) -> str:
    """The effort pi writes with thinking on at that level, capped at "high" beside `thinking: disabled` on Opus 5 and
    Haiku 5.5, which reject `xhigh`/`max` with thinking off (Anthropic's thinking guide, pi generate-models
    `ANTHROPIC_DISABLED_THINKING_MODELS`)."""
    level = clampThinkingLevel(model, effort)
    if _compat_value(model, "supportsMidConvoEffort") is True:
        # A managed-effort model's request keeps `output_config.effort: "high"` and carries the level in its
        # per-message effort markers: the value pi writes there.
        from misaka.ai.api.anthropic_messages import _mapThinkingLevelToEffort

        value = _mapThinkingLevelToEffort(model, level)
    else:
        value = _at(requests[level], ("output_config", "effort"))
    base = re.sub(r"-(?:[0-9]{8}|batch|fast)$", "", model.id.lower().removeprefix("anthropic/"))
    if controls and value in ("xhigh", "max") and base in ("claude-opus-5", "claude-haiku-5-5", "claude-haiku-5.5"):
        return "high"
    return value


def test_no_catalog_model_gets_a_second_reasoning_control_or_one_pi_would_not_send(monkeypatch):
    adapters = {effort: _adapter(monkeypatch, effort) for effort in _EFFORTS}
    models = [model for provider in getProviders() for model in getModels(provider)]

    async def check() -> tuple[int, list[str]]:
        probes, problems = 0, []
        for model in models:
            requests = {level: await _pis_request(model, level) for level in _SESSION_LEVELS}
            assert requests[None] is not None, f"{model.provider}/{model.id}: pi built no request"
            for level, before in requests.items():
                for effort, adapter in adapters.items():
                    probes += 1
                    name = f"{model.provider}/{model.id} session={level} effort={effort!r}"
                    try:
                        after = await adapter(copy.deepcopy(before), model)
                    except Exception as error:  # noqa: BLE001 - a request the hook breaks is a finding too
                        problems.append(f"{name}: raised {error!r}")
                        continue
                    added = {path: value for path, value in _controls(after).items()
                             if _controls(before).get(path, object()) != value}
                    # pi's adaptive models with thinking off: nothing (they cannot turn it off) gets the sub-agent's
                    # effort; Claude's `thinking: disabled` gets Anthropic's `output_config.effort` beside it, as CCB
                    # sends it (a sub-agent runs with thinking off), and it does not turn thinking on there.
                    adaptive_off = (model.api == "anthropic-messages"
                                    and _compat_value(model, "forceAdaptiveThinking") is True
                                    and (_controls(before) == {} or (
                                        _controls(before) == {("thinking", "type"): "disabled"}
                                        and "claude" in model.id.lower())))
                    if not model.reasoning or (_controls(before) and not adaptive_off):
                        # Nothing for a model that does not reason; nothing next to pi's own control. At thinking off
                        # in particular, pi's off switch is never joined by a control that turns reasoning back on
                        # (Fireworks reads `output_config.effort` as `reasoning_effort`, beside `disabled`).
                        if after != before:
                            what = "turned reasoning back on" if level is None and _controls(before) else "changed"
                            problems.append(f"{name}: {what}: {added or after}")
                        continue
                    if len(added) > 1 or {path for path in _controls(after)} - set(added) - set(_controls(before)):
                        problems.append(f"{name}: added {added}")
                        continue
                    if adaptive_off:
                        expected = (("extra_body", "anthropic_internal", "effort_override"), 3) if effort == 3 else (
                            ("output_config", "effort"), _expected_off_effort(model, effort, _controls(before), requests))
                        if list(added.items()) != [expected]:
                            problems.append(f"{name}: added {added}, expected {expected}")
                        continue
                    if added and effort != 3:
                        # The field and value pi itself writes for that level on this model.
                        ((path, value),) = added.items()
                        reference = requests.get(clampThinkingLevel(model, effort)) or {}
                        if _at(reference, path) != value:
                            problems.append(f"{name}: added {path}={value!r}, pi sends {_controls(reference)}")
        return probes, problems

    probes, problems = asyncio.run(check())
    assert probes > 50_000
    assert problems == [], f"{len(problems)} of {probes}: " + "; ".join(problems[:10])



@pytest.mark.parametrize("provider", ["anthropic", "cloudflare-ai-gateway", "opencode"])
@pytest.mark.parametrize("model_id", ["claude-opus-4-6", "claude-opus-4-7", "claude-sonnet-4-6"])
def test_a_sub_agent_on_adaptive_claude_sends_its_effort_beside_disabled_thinking(monkeypatch, provider, model_id):
    # A sub-agent runs with thinking off, so pi sends `thinking: {type: "disabled"}`; the agent's effort still applies
    # (output_config.effort), as before the port.
    model = getModel(provider, model_id)
    before = asyncio.run(_pis_request(model, None))
    assert before["thinking"] == {"type": "disabled"} and "output_config" not in before
    after = asyncio.run(_adapter(monkeypatch, "low")(copy.deepcopy(before), model))
    assert after == {**before, "output_config": {"effort": "low"}}


@pytest.mark.parametrize(("model_id", "expected"), [("claude-opus-5", "high"), ("claude-opus-4-7", "max")])
def test_effort_beside_disabled_thinking_stops_at_high_where_the_model_needs_it(monkeypatch, model_id, expected):
    # Opus 5 and Haiku 5.5 accept disabled thinking only at `high` effort or below; Opus 4.7 at any effort.
    model = getModel("cloudflare-ai-gateway", model_id)
    before = asyncio.run(_pis_request(model, None))
    after = asyncio.run(_adapter(monkeypatch, "max")(copy.deepcopy(before), model))
    assert after["thinking"] == {"type": "disabled"} and after["output_config"] == {"effort": expected}



def test_an_anthropic_compatible_service_keeps_a_sub_agents_reasoning_off(monkeypatch):
    # Fireworks' Anthropic surface: `thinking: disabled` is `reasoning_effort: "none"` and `output_config.effort` is
    # `reasoning_effort`, so an effort beside pi's `disabled` would switch the reasoning back on.
    models = [model for model in getModels("fireworks")
              if model.api == "anthropic-messages" and _compat_value(model, "forceAdaptiveThinking") is True]
    off = [(model, asyncio.run(_pis_request(model, None))) for model in models]
    off = [(model, request) for model, request in off if request.get("thinking") == {"type": "disabled"}]
    assert len(off) >= 7
    for effort in _EFFORTS:
        adapter = _adapter(monkeypatch, effort)
        for model, request in off:
            assert asyncio.run(adapter(copy.deepcopy(request), model)) == request, (model.id, effort)
