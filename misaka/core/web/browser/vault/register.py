"""Native registration only; protocols and handler logic live in the port."""
import copy
import json

from misaka.core.extensions.types import ToolDefinition
from misaka.core.platform.prompt_guard import untrusted
from misaka.core.tools._common import run_with_abort
from misaka.core.web.browser.vault import tools
from misaka.core.web.browser.vault.host import invoke
from misaka.core.web.config import redact_secrets, redact_values
from misaka.core.web.runtime import current_runtime


def register(harn, cwd):
    for suffix in ("list", "unlock", "fill", "save_login", "enter_code"):
        schema = copy.deepcopy(getattr(tools, f"BROWSER_VAULT_{suffix.upper()}_SCHEMA"))
        if suffix in {"fill", "save_login", "enter_code"}:
            schema["parameters"]["properties"]["session"] = {
                "type": "string", "description": "Named MISAKA browser session; omitted selects the default session."}
        handler = getattr(tools, f"_handle_vault_{suffix}")
        async def execute(call_id, args, signal=None, on_update=None, ctx=None, *, handler=handler):
            try:
                runtime = current_runtime()
                if runtime.browser is None:
                    from misaka.core.web.browser import BrowserManager
                    runtime.browser = BrowserManager(cwd)
                result, aborted = await run_with_abort(invoke(handler, args or {}, runtime, ctx), signal)
                if aborted:
                    raise RuntimeError("Operation aborted")
                document = redact_values(json.loads(result))
                rendered = json.dumps(document, ensure_ascii=False)
                # List metadata is user-owned but still potentially large.
                if len(rendered) > 90_000 and isinstance(document.get("items"), list):
                    document["truncated"] = True
                    while document["items"] and len(rendered) > 90_000:
                        document["items"].pop()
                        rendered = json.dumps(document, ensure_ascii=False)
                if len(rendered) > 90_000:
                    raise ValueError("Vault metadata exceeds the tool result budget")
                return {"content": [{"type": "text", "text": untrusted("browser-vault", rendered)}],
                        "details": {}, "isError": document.get("success") is False}
            except Exception as exc:  # noqa: BLE001 - tool boundary reports a sanitized failure
                return {"content": [{"type": "text", "text": redact_secrets(str(exc))[:2048]}],
                        "details": {}, "isError": True}
        harn.registerTool(ToolDefinition(name=schema["name"], label=schema["name"].replace("_", " "),
            description=schema["description"], parameters=schema["parameters"], execute=execute))
