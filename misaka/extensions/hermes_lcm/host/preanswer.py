"""Upstream's pre-answer evidence hook, on the last seam before the provider.

Upstream runs this from ``pre_llm_call``: given the turn's question it compiles a small,
bounded brief of *exact quotes with citations* out of the store and appends it to the
context the model is about to answer from. The whole point is that the brief is derived
deterministically -- no auxiliary model in the default path -- so a question about
something said forty compactions ago is answered from the row rather than from a summary
of a summary.

misaka's equivalent seam is the extension ``context`` event (``transformContext``), the
same one ``host/externalize.py`` uses: it is the last thing to touch the message list
before it is sent, and the only place a message can be added that the session never
stores. Upstream returns ``{"context": policy + "\\n\\n" + brief}`` because its host owns
a system-context string; misaka's seam owns a message list, so the brief arrives as one
appended user message and nothing else in the list moves.

Three things this module does *not* copy from ``~/.hermes/plugins/hermes-lcm/__init__.py``:

* the recall-policy bytes. Upstream's hook always returns the policy blob and adds the
  brief to it, so "off" still rewrites the context. There is no policy blob on this seam,
  so "off" here returns ``None`` and the message list is the same object it arrived as.
* ``enabled_toolsets``. That is a Hermes payload key naming which tool families the turn
  may use; misaka gates tools per card through its own vocabulary, and the engine is
  either registered for this session or it is not.
* ``_engine_bound_session_id`` / ``_ensure_engine_bound_to_session``. ``host/ingest`` and
  ``host/context_engine`` already own binding; this module asks for it the same way
  ``externalize.stub_replay`` does.

**Cost.** ``preanswer_evidence_enabled`` is off by default and the very first thing read,
so a default install runs no query and no model call on this seam. With it on, the
deterministic path stays deterministic: the baseline is one ``lcm_recall``, and
``requirements_compiler`` / ``selective_recall`` are pure Python over the rows it
returned. The one auxiliary-model call in the family is the selective *compiler*'s
selector, behind its own default-off ``LCM_SELECTIVE_COMPILER_ENABLED``, and it is
reached only in ``legacy_selective`` mode after the router has already said this question
needs more than its baseline.

**Research ledger (not this phase).** misaka's research flow keeps its own ledger of
sources and claims, and it is the obvious first consumer of a brief like this: the two
answer the same question (what do we actually have on file for this) from different
stores. Integrating them means deciding whose citation format wins and whether a research
claim should become an LCM assertion -- neither of which is a seam question, so the port
plan holds it back. The join would land here: ``_brief`` is the one place that produces
the text, so a ledger section would be a second producer appended beside it, and
``_baseline`` is the one place that decides what the compiler gets to see.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

from misaka.ai.types import TextContent, UserMessage
from misaka.core.platform.prompt_guard import untrusted
from misaka.utils.values import read_field

from . import context_engine, fence, ingest

logger = logging.getLogger(__name__)

# Upstream's three, and the rule that an unrecognised value means off rather than a
# guess: `_effective_preanswer_mode`. An empty value with the switch on means the mode
# upstream shipped first.
_MODES = frozenset({"off", "legacy_selective", "requirements_v1"})
_DEFAULT_MODE = "legacy_selective"

# `_answer_ready_baseline`, verbatim in its arguments: exact quotes rather than snippets,
# a fixed ceiling, no scope bias, and occurrence time carried so the compiler can tell
# when something happened from when it was said.
_BASELINE_RECALL = {
    "include": "verbatim",
    "detail": "answer_ready",
    "limit": 25,
    "scope_bias": 0.0,
    "include_occurrence_time": True,
}


def _mode(config) -> str:
    """Which pre-answer mode this configuration asks for, or ``"off"``."""
    if not bool(getattr(config, "preanswer_evidence_enabled", False)):
        return "off"
    raw = str(getattr(config, "preanswer_evidence_mode", "") or "").strip().casefold()
    if not raw:
        return _DEFAULT_MODE
    return raw if raw in _MODES else "off"


def _anchor(messages) -> tuple[str, str | None]:
    """The turn's question, and the calendar date "yesterday" is counted back from.

    Upstream reads ``question_date``/``question_as_of`` off its hook payload and falls
    back to the timestamp of the last user message in ``conversation_history``. misaka's
    ``context`` event carries the message list and nothing else, so only that fallback
    exists here -- and it is the better half anyway: an explicit anchor on the payload is
    a benchmark harness feeding a frozen date, not something a live turn produces.

    UTC, because upstream's own fallback is ``datetime.fromtimestamp(..., timezone.utc)``
    and ``answer_contract`` refuses a date whose timezone is ambiguous. A brief compiled
    against a date the contract rejected is no brief at all, so the two have to agree.
    """
    for message in reversed(ingest.upstream_messages(messages)):
        if message.get("role") != "user":
            continue
        question = str(message.get("content") or "").strip()
        stamp = message.get("timestamp")
        if not isinstance(stamp, (int, float)) or isinstance(stamp, bool):
            return question, None
        try:
            return question, datetime.fromtimestamp(float(stamp), tz=UTC).date().isoformat()
        except (OSError, OverflowError, ValueError):
            return question, None
    return "", None


def _baseline(built, question: str) -> tuple[dict, ...]:
    """The bounded set of exact refs the compilers are allowed to reason over.

    Upstream's ``_answer_ready_baseline``: its own host does not put answer-ready refs on
    the hook payload either, so it creates them with one ``lcm_recall``. Deterministic --
    full-text or hybrid retrieval over the store, no model.
    """
    raw = built.handle_tool_call("lcm_recall", {"query": question, **_BASELINE_RECALL})
    recalled = json.loads(raw) if isinstance(raw, str) else raw
    hits = recalled.get("hits") if isinstance(recalled, dict) else None
    candidates = []
    for hit in hits if isinstance(hits, list) else []:
        if not isinstance(hit, dict):
            continue
        quote = str(hit.get("content") or hit.get("snippet") or "")
        if not quote:
            continue
        exact_ref = str(hit.get("exact_ref") or "").strip()
        if exact_ref:
            candidates.append({"exact_ref": exact_ref, "quote": quote})
        elif hit.get("store_id") is not None:
            candidates.append({
                "store_id": hit.get("store_id"),
                "content_offset": hit.get("content_offset", 0),
                "content": quote,
            })
    return tuple(candidates)


def _requirements_brief(built, question: str, question_date: str | None) -> str:
    """``requirements_v1``: one deterministic answer brief, or nothing."""
    from ..vendor.answer_contract import compile_answer_contract
    from ..vendor.evidence_compiler import compile_preanswer_evidence

    # The contract is what decides whether this question has a shape the compiler can
    # close at all. Asking it first is what keeps an ordinary turn free of the recall
    # below -- upstream's own order, and the reason the baseline is not built eagerly.
    if compile_answer_contract(question, question_date).status != "planned":
        return ""
    result = compile_preanswer_evidence(
        question,
        engine=built,
        baseline_refs=_baseline(built, question),
        question_as_of=question_date,
        retrieve=lambda recall_args: built.handle_tool_call("lcm_recall", recall_args),
        enabled=True,
        # True because the baseline above is ours: upstream renders the baseline fact
        # into the brief only when it built the refs itself, since a caller that supplied
        # them already has them in front of the model.
        render_baseline_context=True,
    )
    return str(result.get("context") or "") if isinstance(result, dict) else ""


def _selective_brief(built, config, question: str, question_date: str | None) -> str:
    """``legacy_selective``: the session bundle, and the compiler's brief beside it."""
    from ..vendor.selective_recall import (
        build_selective_session_bundle,
        route_selective_recall,
    )

    # The router is a regex over the question and nothing else. When it says "ordinary"
    # upstream performs no recall, no session load and no model call, and the context
    # bytes stay identical -- so this early return is a contract, not an optimisation.
    if route_selective_recall(question, question_date)["route"] == "ordinary":
        return ""
    baseline = _baseline(built, question)
    bundle = build_selective_session_bundle(
        question, engine=built, baseline_refs=baseline,
        question_date=question_date, enabled=True,
    )
    briefs = [str(bundle.get("context") or "")] if isinstance(bundle, dict) else []
    briefs.append(_compiled_brief(built, config, question, question_date, baseline, bundle))
    return "\n\n".join(brief for brief in briefs if brief)


def _compiled_brief(built, config, question, question_date, baseline, bundle) -> str:
    """The selective compiler's brief. **The one auxiliary-model call on this seam.**

    Off unless ``LCM_SELECTIVE_COMPILER_ENABLED`` says otherwise, and even then upstream
    only calls the selector when ``prepare_selective_compiler`` reports it is needed --
    so a turn whose refs already close the question costs nothing. Fails open: a selector
    that timed out leaves the session bundle to stand on its own.
    """
    if not bool(getattr(config, "selective_compiler_enabled", False)):
        return ""
    try:
        from ..vendor.selective_compiler import (
            call_selective_auxiliary_selector,
            compile_selective_evidence,
            prepare_selective_compiler,
        )

        refs = []
        for raw in [*baseline, *((bundle.get("evidence") if isinstance(bundle, dict) else None) or [])]:
            if not isinstance(raw, dict):
                continue
            exact_ref = str(raw.get("exact_ref") or "").strip()
            quote = str(raw.get("quote") or raw.get("content") or "")
            if exact_ref and quote:
                refs.append({"exact_ref": exact_ref, "quote": quote, "date": raw.get("date")})
        prepared = prepare_selective_compiler(
            question, baseline_refs=refs, question_date=question_date, engine=built,
        )
        if prepared["status"] != "selector_required":
            return ""
        proposal, _usage = call_selective_auxiliary_selector(
            prepared,
            model=str(getattr(config, "selective_compiler_model", "") or ""),
            timeout_seconds=8.0,
        )
        compiled = compile_selective_evidence(
            question, engine=built, compiler_refs=prepared["compiler_refs"],
            selector_proposal=proposal, question_date=question_date, enabled=True,
        )
    # Upstream fails this open too: the brief is an addition, and losing it costs the
    # turn nothing the baseline was not already carrying.
    except Exception:
        logger.warning("LCM selective compiler failed; the turn keeps its baseline.", exc_info=True)
        return ""
    return str(compiled.get("context") or "") if isinstance(compiled, dict) else ""


def _guarded(built, brief: str, session_id: str) -> str:
    """The brief, fenced when the rows it quotes were fenced.

    This is the family's third exit and the quietest one. A compiled brief is *quotes*,
    not prose -- so the laundering the summariser does is not the risk here -- but the
    quote is a slice out of the middle of a row, where a fence's two sentinels are not,
    and the citation beside it is an ``lcm:<row>:<start>-<end>`` string with no
    ``store_id`` field for ``fence._collect`` to find. So the rows are read out of the
    citations (``fence.cited_rows``) and the lineage check does the rest.
    """
    if fence.is_tainted(built, store_ids=fence.cited_rows(brief)):
        logger.info("LCM pre-answer brief quotes fenced material; handing it over as data.")
        return untrusted(f"lcm:preanswer:{session_id}", brief)
    return brief


def inject(event, ctx) -> dict | None:
    """Serve one ``context`` event: the messages, plus one bounded evidence brief.

    Returns ``None`` when there is nothing to add, which is both the disabled default and
    the common case with the switch on -- the runner then keeps the list it already has,
    byte for byte.

    Synchronous, and off the event loop for the reason ``externalize.stub_replay`` is:
    the recall below is a full-text scan and the selective compiler's selector is a real
    model round trip, neither of which the loop can afford to wait on inline. The two
    share this seam and both read fields off the live config that ``context_engine`` pins
    for the duration of one compaction; ``context_engine.ENGINE_LOCK``, which
    ``extension._off_loop`` holds around both, is what keeps that window invisible.

    Registered *after* ``stub_replay``, and that order is load-bearing: the runner threads
    each handler's messages into the next, and upstream's active-replay stubbing protects
    a fresh tail counted from the end of the list. A brief appended first would shift that
    window by one and push a real message out of the protected region.
    """
    built = context_engine.engine()
    if built is None:
        return None
    config = built._config
    mode = _mode(config)
    if mode == "off":
        return None
    messages = list(read_field(event, "messages") or [])
    question, question_date = _anchor(messages)
    if not question:
        return None
    session_id = ingest.session_id(ctx)
    # One engine serves every session in this process and the recall below answers from
    # whichever one it is bound to, so a card asking about its own history has to be that
    # session first -- the same check `externalize.stub_replay` makes.
    if session_id and built.current_session_id != session_id:
        context_engine.start(ctx)
    if mode == "requirements_v1":
        brief = _requirements_brief(built, question, question_date)
    else:
        brief = _selective_brief(built, config, question, question_date)
    if not brief.strip():
        return None
    logger.info("LCM added a %s pre-answer brief (%d chars) for %s.", mode, len(brief), session_id)
    stamp = read_field(messages[-1], "timestamp") if messages else None
    return {"messages": [*messages, UserMessage(
        content=[TextContent(text=_guarded(built, brief, session_id))],
        timestamp=int(stamp or 0),
    )]}
