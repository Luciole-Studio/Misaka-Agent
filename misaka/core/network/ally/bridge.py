"""The card's tools for an ally, as a stdio MCP server: ``misaka ally-bridge``.

The ally's agent starts it from ``session/new``'s ``mcpServers`` with the card's claim, the sender's
session and its panel space in the environment (``card.Attempt._bridge``). Every tool is the
definition a Sister's card session carries -- the card's own (``todo.card_tools``), SendMessage
(``messages.send_as``), a workflow's read-only views and the document index -- run against the board and
the card's folder. Nothing is kept here: ``misaka_card_complete`` records the completion on the
board, where the runner reads it when the turn ends. ACP has no system prompt, so each tool's
prompt guidelines travel in its description.
"""
from __future__ import annotations

import asyncio
import dataclasses
import os
import secrets
from contextlib import closing
from types import SimpleNamespace

from misaka.ai.utils.typebox_helpers import typeboxSchema
from misaka.core.network import messages, todo
from misaka.core.platform import tasks as db

# The document tools a card needs, by name; the rest of their module is not lent. A workflow's own
# read-only tools (``platform.owners``) are lent beside them.
BORROWED = ("doc_list", "doc_outline", "doc_read", "doc_find", "doc_verify", "doc_add")


def _send_message(definition, sender, task_id):
    async def execute(tool_call_id, raw, signal, on_update, ctx):
        args = raw if isinstance(raw, messages.SendMessageParams) else messages.SendMessageParams(**(raw or {}))
        return await asyncio.to_thread(
            messages.send_as, sender, args.to.strip(), args.message, args.summary,
            request_input=args.request_input, card_task=task_id,
            sender_session=os.environ.get("MISAKA_ALLY_SESSION"), space=os.environ.get("MISAKA_NET_SPACE"))
    return dataclasses.replace(definition, execute=execute)


def definitions():
    """The tools, for the card this process was started for."""
    from misaka.core.documents.wiring import documents
    from misaka.core.platform import owners
    from misaka.core.wiring import ToolCollector

    task_id = os.environ["MISAKA_USAGE_TASK_ID"]
    with closing(db.connect(os.environ["MISAKA_USAGE_DB"])) as con:
        row = db.get(con, task_id)
    if row is None:
        raise SystemExit(f"Card not found: {task_id}")
    sender = row["assignee"]
    borrowed = ToolCollector()
    for owner in owners.registered():       # a workflow's read-only views
        owner.borrowed_tools(borrowed)
    documents.register(borrowed)
    lent = {*BORROWED, *owners.read_only_tools()}
    send = messages.MessagesPart(sender=sender, card_task=task_id).tools[0]
    return [*todo.card_tools(task_id, sender), _send_message(send, sender, task_id),
            *(tool for tool in borrowed.tools if tool.name in lent)]


def _schema(parameters):
    return typeboxSchema(parameters) if isinstance(parameters, type) else parameters


def _description(tool):
    guidelines = [*([tool.promptSnippet] if tool.promptSnippet else []), *tool.promptGuidelines]
    return tool.description + "".join(f"\n- {line}" for line in guidelines if line not in tool.description)


async def serve():
    from mcp import types
    from mcp.server.lowlevel import Server
    from mcp.server.stdio import stdio_server

    tools = {tool.name: tool for tool in definitions()}
    # Relative paths in a tool resolve against the card's folder, as in a Sister's session.
    ctx = SimpleNamespace(cwd=os.environ.get("MISAKA_WORKSPACE") or os.getcwd())
    server = Server("misaka")

    @server.list_tools()
    async def list_tools():
        return [types.Tool(name=tool.name, description=_description(tool), inputSchema=_schema(tool.parameters))
                for tool in tools.values()]

    @server.call_tool()
    async def call_tool(name, arguments):
        tool = tools.get(name)
        if tool is None:
            raise ValueError(f"Unknown tool: {name}")
        result = await tool.execute(f"mcp_{secrets.token_hex(6)}", arguments, None, None, ctx)
        return [types.TextContent(type="text", text=str(block.get("text") or ""))
                for block in (result or {}).get("content") or []
                if isinstance(block, dict) and block.get("type") == "text"]

    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def main():
    asyncio.run(serve())
