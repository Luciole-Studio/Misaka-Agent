"""The web tool extension: a pluggable search and extract backend layer, ported from Hermes.

Layout, and why it is split this way -- each module is the misaka equivalent of one
Hermes file, so a future re-sync can be done file by file:

    provider.py   agent/web_search_provider.py   the ABC, both capabilities, the contract
    registry.py   agent/web_search_registry.py + tools/web_tools.py:171-450
                  registration, per-capability backend selection, availability
    keyless.py    plugins/web/keyless_mcp.py     the no-key vendor ring, search and extract
    dispatch.py   tools/web_tools.py:362-460 + 1163-1345
                  resolve -> call -> one-shot rescue, per capability
    config.py     hermes_cli.config              backend selection and credentials
    backends/     plugins/web/<vendor>/          one module per vendor, nine of them
    cache.py      tools/web_result_cache.py      the search memo and the extract disk cache
    tool.py       tools/web_tools.py:838-1048    web_search: schema, memo, untrusted fence
    extract.py    tools/web_tools.py:1048-1490 + 635-800
                  web_extract: schema, gates, budget, evidence, untrusted fence

Three helpers this package leans on live under ``misaka/core/tools/_web/`` rather than
here, because ``web_fetch`` and ``download_file`` need them too: ``url_safety.py`` and
``website_policy.py`` are Hermes' ``tools/url_safety.py`` and ``tools/website_policy.py``,
``screening.py`` is the fixed order they are asked in, and ``evidence.py`` is the writer
that makes a fetched or extracted page quotable.

The tool bodies (schema, rendering, caching) call into
:mod:`misaka.core.web.dispatch`; nothing above the dispatch layer is allowed to
reshape what a provider returned, because that shape is the contract.

**Deliberately not ported from Hermes**, each because the thing it depends on does not
exist here rather than because it was skipped:

* the plugin-scoped registry (``snapshot_registration`` / ``restore_registration``) and
  ``_disabled_web_plugin_for`` -- misaka's registry is one flat process-global table with
  no plugin identity and no enable/disable state, so there is nothing to scope or to
  diagnose. A name that is not registered is a typo, which is what the error says.
* the Nous managed tool-gateway (``NOUS_MANAGED_PROVIDER``, ``_is_tool_gateway_ready``,
  ``web.use_gateway``, ``TOOL_GATEWAY_*``) -- Hermes' subscription product.
* ``WEB_TOOLS_DEBUG`` and its ``DebugSession`` call log -- misaka replaced its debug
  environment switches with ``/debug`` and module loggers.
* the ``requires_env`` / ``emoji`` / ``toolset`` registration metadata --
  :class:`misaka.core.extensions.types.ToolDefinition` has no such fields.
* Hermes' proxy DNS delegation in ``url_safety`` -- it would let a request reach an
  address that was never vetted, which is what ``bounded.py``'s address pinning exists to
  prevent.
"""

from __future__ import annotations

# Which sessions actually receive the tool. ``beast`` is left out because a beast session
# is started with ``-t <subagent tools>`` or ``-nt`` (misaka/core/network/worker.py), so
# declaring it would only produce a tool the ceiling then hides -- the reasoning todo.py
# already states. ``bare`` is IN, unlike documents.py and skills.py, because a bare
# session is not tool-less: ``run_llm_json`` builds ``-t <explicit list>`` out of its
# ``tools=`` argument, which is exactly how coverage.py's ``coverage_scan`` reaches Last
# Order's planning calls. Leaving ``bare`` out would silently undo the research-flow
# whitelist entries in research/planner.py and research/report.py.
SESSION_KINDS = {"foreground", "dm", "card", "child", "bare"}


def activate(spec):
    """Register the four web tools: search, extract, fetch, and controlled download.

    ``web_search`` and ``web_extract`` are conditional on the same gate -- Hermes' single
    ``check_fn=check_web_api_key`` on both registry entries, moved to the point MISAKA
    makes the same decision. Config and environment only, never a network call: it runs
    while a session is being assembled. On a machine with no credentials anywhere it still
    returns True, because the keyless vendor ring can serve, which is the whole reason the
    ring was ported. One gate for two capabilities is Hermes' choice and it is kept: a
    backend that searches but cannot extract is reported by name at call time
    ("X is a search-only backend"), which tells the user what to change, where hiding the
    tool would leave them wondering why the model never reads a page.

    Three tools that fetch, not one, because they answer different questions.
    ``web_extract`` asks a vendor that renders JavaScript and reads PDFs for up to five
    pages at once; ``web_fetch`` dials one page itself, which is what a page the vendor
    cannot see or should not be shown needs; ``download_file`` puts bytes on disk and into
    the corpus. All three leave their evidence in the same place, under ``spec.workspace``.

    ``web_fetch`` and ``download_file`` need no credentials at all, so a session gets
    them either way: reading a page the model already has a URL for does not depend on
    anyone's search subscription. Both are rooted at ``spec.workspace``, which is where
    each leaves what it pulled off the internet -- a downloaded file, or a fetched page's
    complete text -- so a card can register the path and the ledger can quote it.
    ``download_file`` goes one step further for the formats the corpus can read: it indexes
    the file on arrival and hands back a document ID, because the alternative next step the
    model is left with is the read tool, which cannot open a PDF at all. That makes the
    ``doc_*`` tools (registered by ``extensions/documents.py`` for every session kind here
    except ``bare``) the way a downloaded document is navigated and quoted.
    """
    from misaka.core.tools.download_file import create_download_file_tool_definition
    from misaka.core.tools.web_fetch import create_web_fetch_tool_definition
    from misaka.core.web.extract import register as register_extract
    from misaka.core.web.registry import web_search_available
    from misaka.core.web.tool import register as register_search

    searchable = web_search_available()

    def register(harn):
        if searchable:
            register_search(harn)
            register_extract(harn, spec.workspace)
        harn.registerTool(create_web_fetch_tool_definition(spec.workspace))
        harn.registerTool(create_download_file_tool_definition(spec.workspace))

    return register
