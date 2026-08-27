"""The web tool extension: a pluggable search backend layer, ported from Hermes.

Layout, and why it is split this way -- each module is the misaka equivalent of one
Hermes file, so a future re-sync can be done file by file:

    provider.py   agent/web_search_provider.py   the ABC and its response-shape contract
    registry.py   agent/web_search_registry.py + tools/web_tools.py:171-450
                  registration, backend selection, availability
    keyless.py    plugins/web/keyless_mcp.py     the no-key vendor ring
    dispatch.py   tools/web_tools.py:362-460     resolve -> search -> one-shot rescue
    config.py     hermes_cli.config              backend selection and credentials
    backends/     plugins/web/<vendor>/          one module per vendor
    cache.py      tools/web_result_cache.py      the TTL memo (search half)
    tool.py       tools/web_tools.py:838-1048    the tool: schema, memo, untrusted fence

The tool body (schema, rendering, caching) calls
:func:`misaka.extensions.web.dispatch.web_search`; nothing above the dispatch layer is
allowed to reshape what a provider returned, because that shape is the contract.
"""

from __future__ import annotations

# Which sessions actually receive the tool. ``beast`` is left out because a beast session
# is started with ``-t <subagent tools>`` or ``-nt`` (misaka/network/worker.py), so
# declaring it would only produce a tool the ceiling then hides -- the reasoning todo.py
# already states. ``bare`` is IN, unlike documents.py and skills.py, because a bare
# session is not tool-less: ``run_llm_json`` builds ``-t <explicit list>`` out of its
# ``tools=`` argument, which is exactly how coverage.py's ``coverage_scan`` reaches Last
# Order's planning calls. Leaving ``bare`` out would silently undo the research-flow
# whitelist entries in research/planner.py and research/report.py.
SESSION_KINDS = {"foreground", "dm", "card", "child", "bare"}


def activate(spec):
    """Register the three web tools: search, fetch, and controlled download.

    ``web_search`` is conditional -- Hermes' ``check_fn=check_web_api_key`` on the
    registry entry, moved to the point MISAKA makes the same decision. Config and
    environment only, never a network call: it runs while a session is being assembled.
    On a machine with no credentials anywhere it still returns True, because the keyless
    vendor ring can serve, which is the whole reason the ring was ported.

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
    from misaka.extensions.web.registry import web_search_available
    from misaka.extensions.web.tool import register as register_search

    searchable = web_search_available()

    def register(harn):
        if searchable:
            register_search(harn)
        harn.registerTool(create_web_fetch_tool_definition(spec.workspace))
        harn.registerTool(create_download_file_tool_definition(spec.workspace))

    return register
