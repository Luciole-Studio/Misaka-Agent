"""Web search/extract and direct fetch/download, assembled once as a session Part.

Provider protocols and schemas come from Hermes; MISAKA owns cancellation, HTTP
pools and workspace evidence. WebPart uses the existing Moments lifecycle rather
than registering shutdown handlers on the tool-only collector.

X search, native/remote browsers and managed gateway share this same owner and
permission ceiling. Optional browser executables are never installed by discovery.
"""

from __future__ import annotations

# Bare Last Order planning calls still receive explicitly permitted material tools.
# Beast sessions supply their own tool set; the final session ceiling remains authoritative.
SESSION_KINDS = {"foreground", "dm", "card", "child", "bare"}


class WebPart:
    """Web tools and their awaited, session-scoped resource owner."""

    def __init__(self, spec):
        from misaka.core.tools.download_file import create_download_file_tool_definition
        from misaka.core.tools.web_fetch import create_web_fetch_tool_definition
        from misaka.core.web.extract import register as register_extract
        from misaka.core.web.runtime import WebRuntime
        from misaka.core.web.scope import WebScope
        from misaka.core.web.tool import register as register_search
        from misaka.core.web.x_search import register as register_x_search

        self.scope = WebScope(spec.profile_dir)
        self.runtime = WebRuntime(self.scope)
        self._restart_on_configure = False
        self.tools = []
        register_search(self)
        register_extract(self, spec.workspace)
        register_x_search(self, spec.workspace)
        from misaka.core.web.browser.tools import register as register_browser
        register_browser(self, spec.workspace)
        from misaka.core.web.browser.vault.register import register as register_vault
        register_vault(self, spec.workspace)
        self.registerTool(create_web_fetch_tool_definition(spec.workspace))
        self.registerTool(create_download_file_tool_definition(spec.workspace))
        self._definitions = tuple(self.tools)
        self.configure_tools([])

    def configure_tools(self, extensions):
        from misaka.core.web.registry import (
            ensure_backends_registered,
            replace_extension_providers,
            web_search_available,
        )

        with self.scope.activate():
            ensure_backends_registered()
            replace_extension_providers(extensions)
            self.scope.browser_providers = {name: provider for extension in extensions
                                            for name, provider in getattr(extension, "browserProviders", {}).items()}
        # One config read for the entire registration gate, not a file read per probe.
        with self.scope.activate(snapshot=True):
            ready = web_search_available()
            from misaka.core.web.backends.xai import XAIWebSearchProvider

            x_ready = XAIWebSearchProvider().is_available()
            from misaka.core.web.browser.settings import available_tools
            try:
                # Keep fallback definitions in the registry; the final active-tool
                # ceiling selects exec or base tools, never exposing both to the model.
                browser_tools = available_tools(include_fallback=True)
                from misaka.core.web.browser.settings import config as browser_config
                local_exec = bool(browser_config().get("use_real_profile"))
            except (ValueError, OSError):
                browser_tools, local_exec = set(), False
        import copy
        from dataclasses import replace
        definitions = list(self._definitions)
        if local_exec:
            for index, definition in enumerate(definitions):
                if definition.name == "browser_exec":
                    parameters = copy.deepcopy(definition.parameters)
                    parameters["properties"]["local"] = {"type": "boolean", "default": False,
                        "description": "Use the explicitly configured isolated copy of a real Chromium profile."}
                    definitions[index] = replace(definition, parameters=parameters)
        if self._restart_on_configure:
            from misaka.core.web.runtime import WebRuntime

            self.runtime = WebRuntime(self.scope)
            self._restart_on_configure = False
        self.tools = [definition for definition in definitions
                      if (ready or definition.name not in {"web_search", "web_extract"})
                      and (x_ready or definition.name != "x_search")
                      and (not definition.name.startswith("browser_") or definition.name in browser_tools)]

    def registerTool(self, definition):
        from dataclasses import replace

        from misaka.utils.values import read_field

        async def execute(*args, **kwargs):
            result = await self.runtime.run(definition.execute, *args, _tool_name=definition.name, _tool_call=True, **kwargs)
            # Pi marks thrown errors, not an isError field on a normal tool return.
            if read_field(result, "isError", False) or read_field(read_field(result, "details"), "isError", False):
                raise RuntimeError("\n".join(read_field(block, "text", "") for block in read_field(result, "content", [])
                                             if read_field(block, "type") == "text") or "Web tool failed")
            return result

        self.tools.append(replace(definition, execute=execute))

    async def session_start(self, event, ctx):
        if self.runtime.closed:
            # A start racing teardown must not expose a new pool before the old
            # owner's work has actually stopped.
            await self.runtime.close()
            from misaka.core.web.runtime import WebRuntime
            self.runtime = WebRuntime(self.scope)
        self._restart_on_configure = False

    async def session_shutdown(self, event, ctx):
        from misaka.core.web.negative_cache import clear
        from misaka.core.web.registry import replace_extension_providers

        self._restart_on_configure = False
        try:
            await self.runtime.close()
        finally:
            with self.scope.activate():
                replace_extension_providers([])
                self.scope.browser_providers = {}
                clear()
        # Reopen only after the loader has completed and the tool registry is rebuilt.
        # That path runs without a UI; an interrupted reload leaves this owner closed.
        self._restart_on_configure = event.get("reason") == "reload"


def part(spec):
    return WebPart(spec)
