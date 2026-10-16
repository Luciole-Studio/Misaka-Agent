"""Forking a turn, taking back the last turn and read-only side questions, without model calls."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from misaka.agent.agent import Agent
from misaka.agent.types import AgentTool, AgentToolResult
from misaka.ai.types import (
    AssistantMessage,
    DoneEvent,
    Model,
    ModelCost,
    TextContent,
    ToolCall,
    Usage,
)
from misaka.ai.utils.event_stream import AssistantMessageEventStream
from misaka.core.session_manager import SessionManager
from misaka.ui.gui.branching import fork_file, turn_end
from misaka.ui.gui.chat_runner import SIDE_NOTE, ChatHost, _settled_messages

USAGE = {"input": 1, "output": 1, "cacheRead": 0, "cacheWrite": 0, "totalTokens": 2,
         "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0}}


def user(text, ts):
    return {"role": "user", "content": [{"type": "text", "text": text}], "timestamp": ts}


def assistant(text, ts, calls=()):
    content = [{"type": "text", "text": text}, *({"type": "toolCall", "id": c, "name": "read", "arguments": {}} for c in calls)]
    return {"role": "assistant", "content": content, "api": "test", "provider": "test", "model": "m",
            "usage": USAGE, "stopReason": "toolUse" if calls else "stop", "timestamp": ts}


def result(call, ts):
    return {"role": "toolResult", "toolCallId": call, "toolName": "read",
            "content": [{"type": "text", "text": "ok"}], "isError": False, "timestamp": ts}


def entry(id, kind="message", **fields):
    return {"type": kind, "id": id, **fields}


class TurnEndTests(unittest.TestCase):
    def test_turn_runs_to_the_next_visible_row(self):
        branch = [entry("u1", message=user("q", 1)), entry("a1", message=assistant("", 2, ["c"])),
                  entry("r1", message=result("c", 3)), entry("a2", message=assistant("done", 4)),
                  entry("m1", "model_change"), entry("h1", "custom_message", display=False),
                  entry("u2", message=user("next", 5)), entry("a3", message=assistant("second", 6)),
                  entry("n1", "custom_message", display=True), entry("a4", message=assistant("after note", 7))]
        self.assertEqual(turn_end(branch, 2), "h1", "any reply in the turn names the whole turn")
        self.assertEqual(turn_end(branch, 4), "h1")
        self.assertEqual(turn_end(branch, 6), "a3", "a displayed notification opens a new row")
        self.assertEqual(turn_end(branch, 7), "a4")
        with self.assertRaisesRegex(ValueError, "找不到"):
            turn_end(branch, 99)
        with self.assertRaisesRegex(ValueError, "无效"):
            turn_end(branch, True)


class Session:
    """The parts of AgentSession the branch ops touch, over a real session file."""

    def __init__(self, manager):
        self.sessionManager = manager
        self.isStreaming = False
        self.aborted = 0

    @property
    def sessionFile(self):
        return self.sessionManager.getSessionFile()

    @property
    def messages(self):
        return self.sessionManager.buildSessionContext().messages

    async def navigateTree(self, target):
        entry = self.sessionManager.getEntry(target)
        if entry["type"] == "message" and entry["message"]["role"] == "user":
            self.sessionManager.branch(entry["parentId"])
            return {"cancelled": False, "editorText": entry["message"]["content"][0]["text"]}
        self.sessionManager.branch(target)
        return {"cancelled": False}

    async def abort(self):
        self.aborted += 1
        self.isStreaming = False


class BranchOpsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dir = tempfile.mkdtemp()
        self.manager = SessionManager.create(self.dir, self.dir)
        self.manager.appendSessionInfo("研究计划")
        for message in (user("第一问", 10), assistant("第一答", 11), user("第二问", 20),
                        assistant("查一下", 21, ["c1"]), result("c1", 22), assistant("第二答", 23)):
            self.manager.appendMessage(message)
        self.host = ChatHost({"workspace": self.dir})
        self.host.session = Session(self.manager)
        self.host._create_input_queue()
        self.addCleanup(self.host.input_queue.clear)
        self.emitted = []
        emitter = patch("misaka.ui.gui.chat_runner.emit", side_effect=self.emitted.append)
        emitter.start()
        self.addCleanup(emitter.stop)

    def read(self, path):
        return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines()]

    async def test_fork_copies_up_to_the_turn_and_leaves_the_source_alone(self):
        source = self.manager.getSessionFile()
        before = Path(source).read_bytes()
        forked = self.host.op_fork({"timestamp": 11, "name": "研究计划 · 分支"})["session_path"]
        self.assertEqual(Path(source).read_bytes(), before)
        entries = self.read(forked)
        self.assertEqual(entries[0]["parentSession"], source)
        texts = [e["message"]["content"][0]["text"] for e in entries if e["type"] == "message"]
        self.assertEqual(texts, ["第一问", "第一答"])
        self.assertEqual(entries[-1], {**entries[-1], "type": "session_info", "name": "研究计划 · 分支"})
        whole = self.read(self.host.op_fork({"timestamp": 21})["session_path"])
        self.assertEqual(whole[-1]["message"]["content"][0]["text"], "第二答", "tool calls and results stay with their turn")

    async def test_fork_refuses_a_turn_still_streaming(self):
        self.host.session.isStreaming = True
        with self.assertRaisesRegex(ValueError, "还没有结束"):
            self.host.op_fork({"timestamp": 23})
        self.assertTrue(os.path.isfile(self.host.op_fork({"timestamp": 11})["session_path"]),
                        "an earlier, finished turn forks while the newest one streams")

    async def test_saved_session_forks_from_its_file(self):
        forked = fork_file(self.manager.getSessionFile(), timestamp=23)
        texts = [e["message"]["content"][0]["text"] for e in self.read(forked) if e["type"] == "message"]
        self.assertEqual(texts[-1], "第二答")

    async def test_rewind_returns_the_prompt_and_restore_brings_the_turn_back(self):
        leaf = self.manager.getLeafId()
        with self.assertRaisesRegex(ValueError, "已经更新"):
            await self.host.op_rewind({"timestamp": 10})
        undone = await self.host.op_rewind({"timestamp": 20})
        self.assertEqual(undone["text"], "第二问")
        self.assertEqual(undone["leaf_id"], leaf)
        self.assertEqual([m["content"][0]["text"] for m in self.host.session.messages], ["第一问", "第一答"])
        self.assertEqual(self.emitted[-1]["type"], "history")
        self.assertEqual(len(self.emitted[-1]["messages"]), 2)
        def reopened():
            return [m["content"][0]["text"] for m in SessionManager.open(self.manager.getSessionFile()).buildSessionContext().messages]
        self.assertEqual(reopened(), ["第一问", "第一答"], "the rewind survives closing the chat")
        await self.host.op_restore({"leaf_id": undone["leaf_id"]})
        self.assertEqual(self.manager.getBranch()[-2]["id"], leaf)
        self.assertEqual(len(self.emitted[-1]["messages"]), 6)
        self.assertEqual(reopened()[-1], "第二答")
        self.assertEqual(SessionManager.open(self.manager.getSessionFile()).getSessionName(), "研究计划")
        self.assertEqual(self.host.session.aborted, 0, "an idle restore stops nothing")

    async def test_restore_stops_a_reply_started_after_the_rewind(self):
        undone = await self.host.op_rewind({})
        self.manager.appendMessage(user("改过的第二问", 30))
        self.host.session.isStreaming = True
        await self.host.op_restore({"leaf_id": undone["leaf_id"]})
        self.assertEqual(self.host.session.aborted, 1)
        self.assertTrue(self.host.input_queue.paused, "queued follow-ups wait instead of running on the restored turn")
        self.assertEqual(self.host.session.messages[-1]["content"][0]["text"], "第二答")
        with self.assertRaisesRegex(ValueError, "找不到"):
            await self.host.op_restore({"leaf_id": "missing"})

    async def test_rewind_waits_for_the_run_and_queue(self):
        self.host.session.isStreaming = True
        with self.assertRaisesRegex(ValueError, "请等"):
            await self.host.op_rewind({})
        self.host.session.isStreaming = False
        self.host.input_queue.pause()
        self.host.input_queue.submit({"text": "排队", "message_id": "q"})
        with self.assertRaisesRegex(ValueError, "请等"):
            await self.host.op_rewind({})


MODEL = Model(id="m", name="M", api="test", provider="test", baseUrl="", reasoning=False, input=["text"],
              cost=ModelCost(input=0, output=0, cacheRead=0, cacheWrite=0), contextWindow=1000, maxTokens=100)


def reply(*content, stop="stop"):
    return AssistantMessage(content=list(content), api="test", provider="test", model="m",
                            usage=Usage(**USAGE), stopReason=stop, timestamp=1)


class SideTests(unittest.IsolatedAsyncioTestCase):
    async def test_side_question_reads_but_never_writes_and_leaves_the_main_context(self):
        executed, contexts = [], []

        def tool(name):
            async def execute(call_id, args, signal=None, update=None):
                executed.append(name)
                return AgentToolResult(content=[TextContent(text=name + " ran")], details=None)
            return AgentTool(name=name, label=name, description=name, execute=execute,
                             parameters={"type": "object", "properties": {}})

        answers = [reply(ToolCall(id="w", name="write", arguments={}), stop="toolUse"),
                   reply(ToolCall(id="r", name="read", arguments={}), stop="toolUse"),
                   reply(TextContent(text="侧边回答"))]

        async def stream_fn(model, context, options=None):
            contexts.append(context)
            stream = AssistantMessageEventStream()
            message = answers.pop(0)
            stream.push(DoneEvent(reason=message.stopReason, message=message))
            stream.end()
            return stream

        history = [user("主问题", 1), assistant("主回答", 2), assistant("", 3, ["running"])]
        main = Agent({"initialState": {"systemPrompt": "sys", "model": MODEL, "tools": [tool("read"), tool("write")],
                                       "messages": history}, "streamFn": stream_fn})
        before = list(main.state.messages)
        host = ChatHost({"workspace": "."})
        host.session = SimpleNamespace(agent=main, sessionId="s")
        emitted = []
        with patch("misaka.ui.gui.chat_runner.emit", side_effect=emitted.append):
            side_id = host.op_side({"text": "顺带问一句"})["side_id"]
            await host.sides[side_id]["task"]
            host._history, host._session_status = list, dict
            snapshot = host.op_snapshot({})
            host.op_side_close({"side_id": side_id})
        self.assertEqual(executed, ["read"], "write is refused before it runs")
        self.assertEqual(main.state.messages, before, "the main conversation is untouched")
        first = [m.role for m in contexts[0].messages]
        self.assertEqual(first[-2:], ["assistant", "user"], "the unfinished tool call is left out")
        self.assertTrue(contexts[0].messages[-1].content[0].text.startswith(SIDE_NOTE))
        self.assertEqual(contexts[0].messages[-1].content[0].text.removeprefix(SIDE_NOTE), "顺带问一句")
        events = [e for e in emitted if e["type"] == "side_event"]
        starts = [e["event"]["message"] for e in events if e["event"]["type"] == "message_start"]
        self.assertEqual(starts[0]["content"][0]["text"], "顺带问一句", "the browser sees the question as typed")
        side = snapshot["sides"][0]
        self.assertEqual(side["id"], side_id)
        self.assertEqual(side["messages"][0]["content"][0]["text"], "顺带问一句")
        self.assertEqual(side["messages"][-1]["content"][0]["text"], "侧边回答")
        self.assertIn("侧边对话只读", json.dumps(side["messages"], ensure_ascii=False))
        self.assertEqual(emitted[-1], {"type": "side_closed", "side": side_id})
        self.assertEqual(host.sides, {})

    def test_settled_messages_drop_an_unanswered_call(self):
        history = [user("q", 1), assistant("", 2, ["a"]), result("a", 3), assistant("", 4, ["b"])]
        self.assertEqual(len(_settled_messages(history)), 3)


if __name__ == "__main__":
    unittest.main()
