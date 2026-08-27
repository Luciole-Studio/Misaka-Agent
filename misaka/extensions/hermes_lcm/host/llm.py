"""Hermes' auxiliary-model seam, served by misaka's provider stack.

Four places in the vendored tree lazily ``from agent.auxiliary_client import call_llm``:
escalation (the three-tier summariser), extraction, the expansion answer in ``tools``,
and assertion extraction. Every one of them is wrapped in a try/except that degrades to
a deterministic fallback, so a host that never installs this seam still works -- it just
summarises by truncation.

The seam is installed the same way the ContextEngine stub is: a module registered under
the name upstream imports, so no vendored byte changes. Unlike that stub this one is
*not* imported by ``vendor/__init__``; it pulls in misaka's session runner, and merely
importing a vendored module must not drag the provider stack in with it. The context
engine installs it when it builds an engine.

Model routing: upstream hands us whatever ``LCM_SUMMARY_MODEL`` (and the per-task model
overrides) contain, after ``model_routing`` has tried and failed to split a Hermes
provider prefix off it. So a ``provider/model`` value arrives whole and is split here
against misaka's own provider names; anything else is a model id for the configured
provider.
"""

from __future__ import annotations

import logging
import os
import sys
from types import ModuleType, SimpleNamespace

from misaka.config import CFG, current_config

logger = logging.getLogger(__name__)

# Same profile directory the pre-port summariser used, so an existing install keeps its
# summariser personality/settings across the switch.
SUMMARIZER_ROLE = "lcm-summarizer"


def _prompt(messages) -> str:
    """Flatten an OpenAI-shaped message list into one turn.

    ``run_text`` runs a single personality-free, tool-free turn and takes one string.
    Every upstream caller sends either one user message or a system+user pair, so the
    system half becomes a leading block rather than being dropped.
    """
    parts = []
    for message in messages or []:
        content = message.get("content")
        text = content if isinstance(content, str) else str(content)
        role = str(message.get("role") or "user")
        parts.append(text if role == "user" else f"[{role}]\n{text}")
    return "\n\n".join(part for part in parts if part.strip())


def _route(model: str, provider: str) -> tuple[str, str]:
    """Resolve the provider/model pair for one auxiliary call.

    ``model`` arrives already routed by upstream's ``model_routing``: it splits a
    ``provider/model`` value only when the *host* can resolve the prefix, and misaka
    names its provider separately in ``MISAKA_LCM_SUMMARY_PROVIDER``, so whatever is
    left here is a model id for that provider -- never a prefix to re-split.
    """
    cfg = current_config()
    provider = provider or str(CFG.get("lcm_summary_provider") or "")
    return provider or cfg["provider"], model or cfg["default_model"]


def call_llm(*, task="", messages=None, temperature=None, max_tokens=None,
             timeout=None, model="", provider="", **_ignored):
    """One auxiliary completion, in the shape ``response.choices[0].message.content``.

    Raises on an empty or failed turn: every caller treats an exception as "escalate to
    the next model, then to the deterministic fallback", which is the behaviour we want
    for a provider that timed out or returned nothing.
    """
    from misaka.platform.session import run_text

    provider, model = _route(str(model or ""), str(provider or ""))
    profile = os.path.join(os.path.expanduser(current_config()["roles_root"]), SUMMARIZER_ROLE)
    os.makedirs(profile, exist_ok=True)
    text = run_text(
        _prompt(messages),
        profile,
        provider,
        model,
        timeout=max(60, int(timeout or 60)),
        max_tokens=int(max_tokens) if max_tokens else None,
    )
    if not (text and text.strip()):
        raise RuntimeError(f"LCM auxiliary call ({task or 'unspecified'}) returned nothing")
    logger.debug("LCM auxiliary call %s served by %s/%s", task or "unspecified", provider, model)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
        usage=None,
    )


def install() -> None:
    """Register the seam under the name the vendored tree imports."""
    if "agent.auxiliary_client" in sys.modules:
        return
    module = ModuleType("agent.auxiliary_client")
    module.call_llm = call_llm
    sys.modules["agent.auxiliary_client"] = module
    agent = sys.modules.get("agent")
    if agent is not None:
        agent.auxiliary_client = module
