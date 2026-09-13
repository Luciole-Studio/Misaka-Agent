"""Independent xAI X Search; citations are response facts, never a prose verdict."""

from __future__ import annotations

import json
import math
import re
from datetime import UTC, date, datetime

from misaka.core.extensions.types import ToolDefinition
from misaka.core.tools._common import run_with_abort
from misaka.core.tools._web.evidence import save_page
from misaka.core.web.backends.xai import post_responses
from misaka.core.web.config import redact_secrets, web_config
from misaka.core.web.tool import untrusted
from misaka.utils.async_lifecycle import run_in_thread

_PROPERTIES = {
    "query": {"type": "string", "minLength": 1, "description": "What to look up on X."},
    **{key: {"type": "array", "items": {"type": "string"}, "maxItems": 10}
       for key in ("allowed_x_handles", "excluded_x_handles")},
    **{key: {"type": "string", "description": "YYYY-MM-DD"}
       for key in ("from_date", "to_date")},
    **{key: {"type": "boolean", "default": False}
       for key in ("enable_image_understanding", "enable_video_understanding")},
}


def tool_parameters(args):
    tool = {"type": "x_search"}
    for key in ("allowed_x_handles", "excluded_x_handles"):
        values = args.get(key)
        if values is None:
            continue
        if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
            raise ValueError(f"{key} must be a list of handles")
        values = [value.strip().lstrip("@") for value in values if value.strip().lstrip("@")]
        if len(values) > 10:
            raise ValueError(f"{key} accepts at most 10 handles")
        if values:
            tool[key] = values
    if "allowed_x_handles" in tool and "excluded_x_handles" in tool:
        raise ValueError("Choose allowed_x_handles or excluded_x_handles, not both")
    dates = {}
    for key in ("from_date", "to_date"):
        raw = args.get(key, "")
        if not isinstance(raw, str):
            raise ValueError(f"{key} requires YYYY-MM-DD")  # noqa: TRY004 - surface protocol/configuration failure
        if raw.strip():
            value = raw.strip()
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                raise ValueError(f"{key} requires YYYY-MM-DD")
            dates[key] = date.fromisoformat(value)
            tool[key] = value
    if "from_date" in dates and dates["from_date"] > datetime.now(UTC).date():
        raise ValueError("from_date must not be in the future")
    if len(dates) == 2 and dates["from_date"] > dates["to_date"]:
        raise ValueError("from_date must not be later than to_date")
    for key in ("enable_image_understanding", "enable_video_understanding"):
        value = args.get(key, False)
        if not isinstance(value, bool):
            raise ValueError(f"{key} must be true or false")  # noqa: TRY004 - surface protocol/configuration failure
        # False means the API default, not a truthy string or an enabled feature.
        if value:
            tool[key] = True
    return tool


async def search(args):
    query = args.get("query", "")
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query is required for x_search")
    tool = tool_parameters(args)
    cfg = web_config(strict=True).get("x_search", {})
    if not isinstance(cfg, dict):
        raise ValueError("x_search configuration must be an object")  # noqa: TRY004 - surface protocol/configuration failure
    model = str(cfg.get("model") or "").strip() or "grok-4.5"
    effort = str(cfg.get("reasoning_effort") or "").strip().lower()
    if effort and effort not in {"low", "medium", "high", "xhigh"}:
        raise ValueError("x_search.reasoning_effort takes low, medium, high or xhigh")
    timeout, retries = cfg.get("timeout_seconds", 180), cfg.get("retries", 2)
    if type(timeout) not in {int, float} or not math.isfinite(timeout) or timeout < 30:
        raise ValueError("x_search.timeout_seconds must be a finite number >= 30")
    if type(retries) is not int or not 0 <= retries <= 10:
        raise ValueError("x_search.retries must be an integer from 0 to 10")
    payload = {"model": model, "input": [{"role": "user", "content": query.strip()}],
               "tools": [tool], "store": False}
    if effort:
        payload["reasoning"] = {"effort": effort}
    response, source = await post_responses(payload, query, operation="x_search", timeout=timeout,
                                            retries=retries, prefer_api_key=True)
    data = response.json()
    if not isinstance(data, dict) or data.get("error"):
        raise ValueError("xAI returned an invalid/error X Search envelope")
    if data.get("output_text") is not None and not isinstance(data["output_text"], str):
        raise ValueError("xAI output_text must be text")
    chunks, inline = [], []
    for item in data.get("output") or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for chunk in item.get("content") or []:
            if not isinstance(chunk, dict):
                continue
            if chunk.get("type") in {"text", "output_text"} and isinstance(chunk.get("text"), str):
                chunks.append(chunk["text"])
            for annotation in chunk.get("annotations") or []:
                if isinstance(annotation, dict) and annotation.get("type") == "url_citation":
                    inline.append({key: annotation.get(key) for key in ("url", "title", "start_index", "end_index")})
    citations = data.get("citations") or []
    if not isinstance(citations, list):
        raise ValueError("xAI returned invalid citations metadata")  # noqa: TRY004 - surface protocol/configuration failure
    return {"success": True, "provider": "xai", "credential_source": source, "tool": "x_search",
            "model": model, "query": query.strip(), "answer": data.get("output_text") or "\n\n".join(chunks),
            "citations": citations, "inline_citations": inline,
            "citations_missing": not citations and not inline,
            "active_filters": [key for key in tool if key.endswith(("_handles", "_date"))]}


def register(harn, cwd):
    async def execute(tool_call_id, raw, signal, on_update, ctx):
        try:
            result, _ = await run_with_abort(search(raw), signal)
            rendered = redact_secrets(json.dumps(result, ensure_ascii=False))
            saved = await run_in_thread(save_page, cwd, {"provider": "xai", "content_kind": "search_answer"}, rendered)
            if len(rendered) > 95_000:
                storage = ({"saved_path": saved, "read": {"path": saved, "offset": 1}} if saved else
                           {"storage_error": "Full X search result could not be saved; omitted content is unavailable."})
                rendered = json.dumps({"success": True, "truncated": True, **storage,
                                       "preview": rendered[:40_000]},
                                      ensure_ascii=False)
            return {"content": [{"type": "text", "text": untrusted("x-search", rendered)}],
                    "details": {"saved_path": saved}}
        except Exception as error:  # noqa: BLE001 - tool or transport boundary reports the failure
            return {"content": [{"type": "text", "text": redact_secrets(str(error))[:2048]}],
                    "details": {}, "isError": True}

    harn.registerTool(ToolDefinition(
        name="x_search", label="Search X",
        description="Search public X posts, profiles and threads with xAI X Search. Read-only discovery; "
                    "supports account/date filters and optional image/video understanding. Not a posting or account tool.",
        parameters={"type": "object", "properties": _PROPERTIES, "required": ["query"]},
        execute=execute, promptSnippet="Search current public discussion on X",
    ))
