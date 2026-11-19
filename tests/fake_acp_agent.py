"""A scripted ACP agent for the ally tests: a real process speaking ACP over its stdio.

``fake_acp_agent.py SCRIPT LOG``. SCRIPT is JSON: ``{"load": bool, "replay": [text, ...],
"turns": [[step, ...], ...]}``; each ``session/prompt`` plays the next turn's steps. A step is one of
``{"say": text}``, ``{"write": [path, text]}``, ``{"call": [tool, arguments]}`` (through the MCP
server the client named in ``mcpServers``), ``{"permission": true}``, ``{"sleep": seconds}`` (a
``session/cancel`` ends it), ``{"stderr": text}``, ``{"stop": reason}``, ``{"error": message}``.
Everything the agent is told or does is appended to LOG as JSON lines.
"""
import asyncio
import contextlib
import json
import sys
import time
import uuid
from pathlib import Path

SCRIPT = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
LOG = sys.argv[2]


def log(kind, **data):
    with open(LOG, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"kind": kind, "at": time.time(), **data}) + "\n")


class Agent:
    def __init__(self):
        self.turns = list(SCRIPT.get("turns") or [])
        self.ids = iter(range(1, 10**6))
        self.waiting = {}
        self.cancel = asyncio.Event()
        self.mcp = None
        self.stack = contextlib.AsyncExitStack()
        self.session = None

    def send(self, message):
        sys.stdout.write(json.dumps(message) + "\n")
        sys.stdout.flush()

    def update(self, update):
        self.send({"jsonrpc": "2.0", "method": "session/update",
                   "params": {"sessionId": self.session, "update": update}})

    async def ask(self, method, params):
        request_id = next(self.ids)
        answer = self.waiting[request_id] = asyncio.get_running_loop().create_future()
        self.send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        return await answer

    async def connect(self, servers):
        if not servers:
            return
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        server = servers[0]
        params = StdioServerParameters(command=server["command"], args=server["args"],
                                       env={item["name"]: item["value"] for item in server["env"]})
        read, write = await self.stack.enter_async_context(stdio_client(params))
        self.mcp = await self.stack.enter_async_context(ClientSession(read, write))
        await self.mcp.initialize()
        log("tools", names=[tool.name for tool in (await self.mcp.list_tools()).tools])

    async def turn(self, message):
        text = "".join(block.get("text", "") for block in message["params"]["prompt"])
        log("prompt", text=text)
        self.cancel.clear()
        stop = "end_turn"
        steps = self.turns.pop(0) if self.turns else [{"say": "(no more script)"}]
        for step in steps:
            if "say" in step:
                self.update({"sessionUpdate": "agent_message_chunk",
                             "content": {"type": "text", "text": step["say"]}})
            elif "write" in step:
                path, body = step["write"]
                Path(path).write_text(body, encoding="utf-8")
                self.update({"sessionUpdate": "tool_call", "toolCallId": uuid.uuid4().hex, "title": f"Write {path}",
                             "kind": "edit", "status": "completed", "rawInput": {"path": path},
                             "rawOutput": "written"})
            elif "call" in step:
                name, arguments = step["call"]
                call_id = uuid.uuid4().hex
                self.update({"sessionUpdate": "tool_call", "toolCallId": call_id, "title": name, "kind": "other",
                             "status": "pending", "rawInput": arguments})
                result = await self.mcp.call_tool(name, arguments)
                output = "".join(getattr(block, "text", "") for block in result.content)
                log("tool", name=name, output=output, error=bool(result.isError))
                self.update({"sessionUpdate": "tool_call_update", "toolCallId": call_id,
                             "status": "failed" if result.isError else "completed", "rawOutput": output})
            elif "permission" in step:
                answer = await self.ask("session/request_permission", {
                    "sessionId": self.session, "toolCall": {"toolCallId": "p1", "title": "run a command"},
                    "options": [{"optionId": "nope", "name": "Reject", "kind": "reject_once"},
                                {"optionId": "go", "name": "Allow", "kind": "allow_once"}]})
                log("permission", outcome=answer["outcome"])
            elif "sleep" in step:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self.cancel.wait(), step["sleep"])
                if self.cancel.is_set():
                    log("cancelled")
                    stop = "cancelled"
                    break
            elif "stderr" in step:
                sys.stderr.write(step["stderr"] + "\n")
                sys.stderr.flush()
            elif "stop" in step:
                stop = step["stop"]
            elif "error" in step:
                self.send({"jsonrpc": "2.0", "id": message["id"],
                           "error": {"code": -32603, "message": step["error"]}})
                return
        self.send({"jsonrpc": "2.0", "id": message["id"], "result": {"stopReason": stop}})

    async def handle(self, message):
        method, params = message.get("method"), message.get("params") or {}
        if method is None:
            future = self.waiting.get(message.get("id"))
            if future is not None:
                future.set_result(message.get("result"))
        elif method == "initialize":
            log("initialize", params=params)
            self.send({"jsonrpc": "2.0", "id": message["id"], "result": {
                "protocolVersion": 1, "agentCapabilities": {"loadSession": bool(SCRIPT.get("load"))},
                "authMethods": []}})
        elif method == "session/new":
            self.session = "S-" + uuid.uuid4().hex[:8]
            log("new", session=self.session, cwd=params.get("cwd"))
            await self.connect(params.get("mcpServers"))
            self.send({"jsonrpc": "2.0", "id": message["id"], "result": {"sessionId": self.session}})
        elif method == "session/load":
            self.session = params["sessionId"]
            log("load", session=self.session)
            for text in SCRIPT.get("replay") or []:
                self.update({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}})
            await self.connect(params.get("mcpServers"))
            self.send({"jsonrpc": "2.0", "id": message["id"], "result": None})
        elif method == "session/prompt":
            asyncio.ensure_future(self.turn(message))
        elif method == "session/cancel":
            log("cancel")
            self.cancel.set()
        elif "id" in message:
            self.send({"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32601, "message": method}})


async def main():
    agent = Agent()
    try:
        while line := await asyncio.to_thread(sys.stdin.buffer.readline):
            await agent.handle(json.loads(line))
    finally:
        log("exit")
        await agent.stack.aclose()


asyncio.run(main())
