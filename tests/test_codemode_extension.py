"""pi coding-agent's ``suite/agent-session-codemode.test.ts``, ``codemode-renderer.test.ts`` and the
"codemode description catalog" cases of ``tool-search.test.ts``."""

from __future__ import annotations

import base64
import json
import os
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from misaka.agent.types import AgentToolResult
from misaka.ai.types import (
    AssistantImages,
    ClassifierResult,
    ImageContent,
    TextContent,
    ToolCall,
    Usage,
)
from misaka.ai.utils.transcript import getCurrentSystemPrompt, getCurrentTools
from misaka.core.extensions.types import ToolDefinition
from misaka.extensions.codemode import createCodemodeExtension
from misaka.extensions.codemode.execute import readCodemodeStore
from misaka.extensions.codemode.tool import (
    CODEMODE_DOCS_PATH,
    CODEMODE_STORE_ENTRY_TYPE,
    createCodemodeDescription,
    createCodemodeTool,
)
from misaka.utils.values import read_field
from tests.test_tool_search import _assistant, _scripted, session_with

TINY_PNG_BASE64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFBQIAX8jx0gAAAABJRU5ErkJggg=="


async def _echo(_id, params, *_args):
    return AgentToolResult(content=[TextContent(text=f"echo: {params['text']}")], details={})


async def _stats(*_args):
    return AgentToolResult(content=[TextContent(text="2 files")], details={}, structuredContent={"files": 2, "names": ["a", "b"]})


async def _screenshot(*_args):
    return AgentToolResult(
        content=[TextContent(text="captured"), ImageContent(data=TINY_PNG_BASE64, mimeType="image/png")], details={}
    )


echoTool = ToolDefinition(
    name="echo", label="Echo", description="Echo text back.\n\nSecond paragraph.",
    parameters={"type": "object", "properties": {"text": {"type": "string", "description": "Text to echo"}}, "required": ["text"]},
    execute=_echo,
)
statsTool = ToolDefinition(
    name="stats", label="Stats", description="Return structured stats", parameters={"type": "object", "properties": {}},
    outputSchema={"type": "object", "properties": {"files": {"type": "number"}, "names": {"type": "array", "items": {"type": "string"}}}, "required": ["files", "names"]},
    execute=_stats,
)
screenshotTool = ToolDefinition(
    name="screenshot", label="Screenshot", description="Return a screenshot", parameters={"type": "object", "properties": {}},
    execute=_screenshot,
)


def usage(input: int, cost: float) -> dict:
    return {"input": input, "output": 0, "cacheRead": 0, "cacheWrite": 0, "totalTokens": input,
            "cost": {"input": cost, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": cost}}


def registerTools(pi) -> None:
    # Registered by an extension so they run with the session's tool context, next to the built-in codemode.
    for tool in (echoTool, statsTool, screenshotTool):
        pi.registerTool(tool)


def codemodeResult(session):
    """The latest codemode result, like the harness's ``getToolResult``."""
    return [message for message in session.messages if getattr(message, "role", None) == "toolResult" and message.toolName == "codemode"][-1]


def resultText(message) -> str:
    """The output after the script header, which is checked on the way."""
    header, *items = message.content
    assert header.type == "text" and re.match(r"^Script (completed|failed)\nWall time \d+\.\d seconds\nOutput:\n\Z", header.text)
    return "\n".join(block.text if block.type == "text" else f"<{block.type}>" for block in items)


def codemodeCall(code: str):
    return _assistant([ToolCall(id="call-1", name="codemode", arguments={"code": code})], "toolUse")


async def run(session, code: str):
    session.agent.streamFunction = _scripted([codemodeCall(code), _assistant([TextContent(text="done")])])
    await session.prompt("go")
    await session.waitForIdle()
    return codemodeResult(session)


def description(session, name: str) -> str:
    return next((tool.description for tool in session.agent.state.tools if tool.name == name), "")


# MISAKA fork: `(.+\.png)` rather than `(\S+\.png)`, as a Windows temp path may hold spaces.
TINY_PNG_LABEL = re.compile(r"^\[Image saved to (.+\.png) \(image/png, \d+B\)\]\Z")


def checkSavedImages(text: str) -> str:
    """Replace the ``[Image saved to ...]`` labels in ``text`` with ``<saved>`` after checking that each file
    holds the tiny PNG, and remove the files."""
    lines = []
    for line in text.split("\n"):
        match = TINY_PNG_LABEL.match(line)
        if match is None:
            lines.append(line)
            continue
        try:
            assert base64.b64encode(Path(match.group(1)).read_bytes()).decode() == TINY_PNG_BASE64
        finally:
            Path(match.group(1)).unlink(missing_ok=True)
        lines.append("<saved>")
    return "\n".join(lines)


# -- AgentSession codemode tool -----------------------------------------------------------------


async def test_presents_callable_tools_per_codemode_mode(tmp_path):
    requestTools: list[list[str]] = []
    requestPrompts: list[str] = []

    def record(context):
        requestTools.append([tool.name for tool in getCurrentTools(context.messages)])
        requestPrompts.append(getCurrentSystemPrompt(context.messages))
        return _assistant([TextContent(text="ok")])

    async with session_with(tmp_path, [createCodemodeExtension(), registerTools], settings={"defaultTools": ["codemode"]}) as session:
        # on: declared tools say how scripts call them and are not listed again in codemode.
        session.setActiveToolsByName(["read", "echo", "codemode"])
        assert "Codemode: `tools.echo(args)` resolves to" in description(session, "echo")
        assert "codemode tool declaration:" not in description(session, "echo")
        assert "### `echo`" not in description(session, "codemode")
        session.agent.streamFunction = _scripted([record])
        await session.prompt("on")
        await session.waitForIdle()
        assert {"read", "echo", "codemode"} <= set(requestTools[0])
        assert "\n- read: " in requestPrompts[0]

        # only: codemode lists echo, which stays active but is left out of requests.
        session.settingsManager.applyOverrides({"codemode": {"mode": "only"}})
        session.setActiveToolsByName(["read", "echo", "codemode"])
        assert "Codemode: `tools.echo" not in description(session, "echo")
        assert "### `echo`" in description(session, "codemode")
        assert "### `stats`" not in description(session, "codemode")
        session.agent.streamFunction = _scripted([record])
        await session.prompt("only")
        await session.waitForIdle()
        assert "codemode" in requestTools[1]
        assert "echo" not in requestTools[1]
        assert "read" not in requestTools[1]
        # The prompt's tool list matches the declarations: hidden tools are not listed (#10192).
        assert "\n- read: " not in requestPrompts[1]
        assert "\n- codemode: " in requestPrompts[1]
        assert "\n- read: " not in session.systemPrompt
        # Hidden tools' guidelines move from the rules to their codemode sections (#10343).
        assert "Use read to examine files" not in requestPrompts[1]
        assert "- Use read to examine files instead of cat or sed." in description(session, "codemode")

        # Without codemode, tools keep their plain descriptions.
        session.setActiveToolsByName(["echo"])
        assert description(session, "echo") == "Echo text back.\n\nSecond paragraph."


# #10343
async def test_shows_the_guidelines_of_tools_that_do_not_fit_the_inline_budget_through_describe_tool(tmp_path):
    async with session_with(tmp_path, [createCodemodeExtension(), registerTools], settings={"defaultTools": ["codemode"]}) as session:
        session.settingsManager.applyOverrides({"codemode": {"mode": "only", "inlineBudget": 0}})
        session.setActiveToolsByName(["read", "codemode"])
        assert "### `read`" not in description(session, "codemode")
        result = await run(session, 'text(await describeTool("read"))')
        assert "- Use read to examine files instead of cat or sed." in resultText(result)


async def test_runs_nested_calls_in_parallel_and_returns_only_the_script_result(tmp_path):
    async with session_with(tmp_path, [createCodemodeExtension(), registerTools], settings={"defaultTools": ["codemode"]}) as session:
        result = await run(session, """
            const [a, b, stats] = await Promise.all([
                tools.echo({ text: "one" }),
                tools.echo({ text: "two" }),
                tools.stats({}),
            ]);
            console.log("files", stats.files);
            text(ALL_TOOLS.map((tool) => tool.name).join(","));
            return { a, b, names: stats.names };
        """)
        assert not result.isError
        assert resultText(result) == (
            '==> text 1/2 <==\necho,stats,screenshot\n==> text 2/2 <==\n{"a":"echo: one","b":"echo: two","names":["a","b"]}'
            "\n<console_output>\nfiles 2\n</console_output>"
        )
        details = result.details
        assert [[call["name"], call["status"]] for call in details["calls"]] == [["echo", "ok"], ["echo", "ok"], ["stats", "ok"]]
        assert all(call["id"].startswith(f"{result.toolCallId}/") for call in details["calls"])
        # Nested calls never become transcript tool results; their events carry the parent id.
        assert len([message for message in session.messages if getattr(message, "role", None) == "toolResult"]) == 1


async def test_routes_nested_calls_through_extension_hooks(tmp_path):
    def hooks(pi) -> None:
        def on_call(event, _ctx):
            if event["toolName"] == "echo" and event["input"]["text"] == "forbidden":
                return {"block": True, "reason": "echo of forbidden text is blocked"}
            return None

        def on_result(event, _ctx):
            return {"content": [TextContent(text="redacted")]} if event["toolName"] == "stats" else None

        pi.on("tool_call", on_call)
        pi.on("tool_result", on_result)

    async with session_with(tmp_path, [createCodemodeExtension(), registerTools, hooks], settings={"defaultTools": ["codemode"]}) as session:
        result = await run(session, """
            let blocked;
            try {
                await tools.echo({ text: "forbidden" });
            } catch (error) {
                blocked = error.message;
            }
            const stats = await tools.stats({});
            return { blocked, stats };
        """)
        # Replacing content without replacing structured content drops the structured result.
        assert json.loads(resultText(result)) == {"blocked": "echo of forbidden text is blocked", "stats": "redacted"}
        assert [call["status"] for call in result.details["calls"]] == ["error", "ok"]


async def test_adds_the_usage_of_nested_results_to_the_codemode_result(tmp_path):
    async def billed(*_args):
        return AgentToolResult(content=[TextContent(text="ran")], details={}, usage=usage(100, 0.25))

    def register(pi) -> None:
        pi.registerTool(ToolDefinition(name="billed", label="Billed", description="Run a model",
                                       parameters={"type": "object", "properties": {}}, execute=billed))

    async with session_with(tmp_path, [createCodemodeExtension(), registerTools, register], settings={"defaultTools": ["codemode"]}) as session:
        result = await run(session, 'await tools.billed({}); await tools.billed({}); await tools.echo({ text: "x" });')
        assert result.usage.input == 200 and result.usage.totalTokens == 200 and result.usage.cost.total == 0.5
        # The usage is persisted with the result, so session totals count it.
        persisted = next(
            entry for entry in session.sessionManager.getEntries()
            if entry["type"] == "message" and read_field(entry["message"], "role") == "toolResult"
        )
        assert Usage.model_validate(read_field(persisted["message"], "usage")) == result.usage
        assert session.getSessionStats().cost == 0.5


async def test_keeps_structured_content_that_tool_result_handlers_replace_along_with_the_content(tmp_path):
    def hooks(pi) -> None:
        pi.on("tool_result", lambda event, _ctx: (
            {"content": [TextContent(text="0 files")], "structuredContent": {"files": 0, "names": []}}
            if event["toolName"] == "stats" else None
        ))
        # A later handler that only touches details keeps what the first one set.
        pi.on("tool_result", lambda event, _ctx: {"details": {"audited": True}} if event["toolName"] == "stats" else None)

    async with session_with(tmp_path, [createCodemodeExtension(), registerTools, hooks], settings={"defaultTools": ["codemode"]}) as session:
        result = await run(session, "return await tools.stats({});")
        assert json.loads(resultText(result)) == {"files": 0, "names": []}


# Saved images: https://github.com/earendil-works/pi/issues/10310
async def test_attaches_only_the_images_the_script_passes_to_image_in_output_order_each_after_its_saved_path(tmp_path):
    async with session_with(tmp_path, [createCodemodeExtension(), registerTools], settings={"defaultTools": ["codemode"]}) as session:
        result = await run(session, f"""
            // Tools without an outputSchema resolve to their text; images are not passed on.
            const shot = await tools.screenshot({{}});
            text(shot);
            image("data:image/png;base64,{TINY_PNG_BASE64}");
            image("data:image/png;base64,{TINY_PNG_BASE64}");
            text("after");
        """)
        # The same image shown twice is saved once, so both labels name one file.
        lines = resultText(result).split("\n")
        assert lines == ["==> text 1/2 <==", "captured", lines[2], "<image>", lines[2], "<image>", "==> text 2/2 <==", "after"]
        assert checkSavedImages(lines[2]) == "<saved>"
        assert result.content[2] == ImageContent(data=TINY_PNG_BASE64, mimeType="image/png")


async def test_marks_where_each_text_item_starts_and_puts_console_lines_last_in_one_text_block(tmp_path):
    async with session_with(tmp_path, [createCodemodeExtension(), registerTools], settings={"defaultTools": ["codemode"]}) as session:
        result = await run(session, 'text("one\\ntwo");\nconsole.log("a");\nconsole.log("b");\ntext("three\\n");\nreturn 4;')
        # Providers join adjacent text blocks with nothing or a newline, so the output is one block.
        assert len(result.content) == 2
        assert resultText(result) == (
            "==> text 1/3 <==\none\ntwo\n==> text 2/3 <==\nthree\n==> text 3/3 <==\n4\n<console_output>\na\nb\n</console_output>"
        )


async def test_reports_script_failures_as_results_that_keep_partial_output_and_the_calls_that_ran(tmp_path):
    async with session_with(tmp_path, [createCodemodeExtension(), registerTools], settings={"defaultTools": ["codemode"]}) as session:
        result = await run(
            session, 'text("partial");\nconsole.log("log");\nawait tools.echo({ text: "x" });\nthrow new Error("boom");'
        )
        assert result.isError is True
        assert result.content[0].text.startswith("Script failed\n")
        text = resultText(result)
        assert re.match(r"^partial\n<console_output>\nlog\n</console_output>\nScript error:\nError: boom\n", text)
        assert "codemode.js:4" in text
        assert "Tool calls made before the failure (they are not undone): echo (ok)" in text
        assert [call["name"] for call in result.details["calls"]] == ["echo"]


# -- codemode options and store -----------------------------------------------------------------


def storeEntries(session) -> list:
    return [
        entry["data"] for entry in session.sessionManager.getBranch()
        if entry["type"] == "custom" and entry["customType"] == CODEMODE_STORE_ENTRY_TYPE
    ]


INCREMENT = 'const next = (load("count") ?? 0) + 1;\nstore("count", next);\nreturn next;'


async def test_applies_the_timeout_ms_option_and_rejects_invalid_options(tmp_path):
    async with session_with(tmp_path, [createCodemodeExtension()], settings={"defaultTools": ["codemode"]}) as session:
        timedOut = await run(session, '// @options: {"timeout_ms": 200}\nwhile (true) {}')
        assert timedOut.isError is True
        assert "Script error:\nScript timed out" in resultText(timedOut)

        invalid = await run(session, '// @options: {"yield": 1}\ntext(1)')
        assert invalid.isError is True
        assert invalid.content == [TextContent(text="@options only supports `max_output_tokens` and `timeout_ms`; got `yield`")]

        # #10631: a misplaced options line fails before the script runs instead of running without a deadline.
        misplaced = await run(session, '// count\n// @options: {"timeout_ms": 200}\nwhile (true) {}')
        assert misplaced.isError is True
        assert misplaced.content == [
            TextContent(text="The // @options: line must be the first line of the input; found one on line 2")
        ]


async def test_limits_script_memory_so_runaway_allocations_fail_inside_the_script(tmp_path):
    async with session_with(tmp_path, [createCodemodeExtension()], settings={"defaultTools": ["codemode"]}) as session:
        result = await run(
            session,
            '// @options: {"timeout_ms": 30000}\nlet a = [];\ntry { while (true) a.push("x".repeat(1 << 20) + a.length); }'
            " catch (error) { const n = a.length; a = null; return { n, error: String(error) }; }",
        )
        assert result.isError is False
        outcome = json.loads(resultText(result))
        assert "out of memory" in outcome["error"]
        # Each entry holds at least 1 MiB, so the limit stops the script well before wasm32's 4 GiB.
        assert outcome["n"] < 512


# #10678: joining the text items was quadratic, about 10 s here for the item limit.
async def test_formats_output_at_the_item_limit_in_linear_time(tmp_path):
    import time

    from misaka.extensions.codemode.execute import formatOutput, joinAdjacentText

    async with session_with(tmp_path, [createCodemodeExtension()], settings={"defaultTools": ["codemode"]}) as session:
        result = await run(session, 'for (let i = 0; i < 100000; i++) text("");')
        Path(result.details.get("fullOutputPath") or "").unlink(missing_ok=True)
        assert not result.isError
        assert "==> text 100000/100000 <==" in resultText(result)
    # MISAKA fork: timed apart from the script. Crossing the Python bridge 100,000 times takes about 5 s here by
    # itself (upstream's whole run, V8 bridge included, must stay under 5 s); the formatting is what was quadratic.
    startedAt = time.perf_counter()
    joinAdjacentText(formatOutput([{"type": "text", "text": ""}] * 100_000))
    assert time.perf_counter() - startedAt < 1


def test_joins_adjacent_text_like_upstream():
    from misaka.extensions.codemode.execute import joinAdjacentText

    image = ImageContent(data=TINY_PNG_BASE64, mimeType="image/png")
    joined = joinAdjacentText([
        TextContent(text="a"), TextContent(text=""), TextContent(text="b\n"), TextContent(text="c"),
        image, TextContent(text=""), TextContent(text="d"),
    ])
    # The separator follows the last non-empty part; an image starts a new text item.
    assert joined == [TextContent(text="a\nb\nc"), image, TextContent(text="d")]


async def test_truncates_output_to_the_token_budget_and_spills_the_full_text(tmp_path):
    async with session_with(tmp_path, [createCodemodeExtension()], settings={"defaultTools": ["codemode"]}) as session:
        result = await run(
            session,
            f'// @options: {{"max_output_tokens": 30}}\nfor (let i = 0; i < 100; i++) text("row " + i);\nimage("data:image/png;base64,{TINY_PNG_BASE64}");',
        )
        path = result.details.get("fullOutputPath")
        assert path, "No spill file"
        try:
            text = resultText(result)
            assert text.startswith("Warning: truncated output")
            assert "row 0\n" in text
            assert "tokens truncated" in text
            assert "row 99\n" in text
            assert "row 50\n" not in text
            assert f"[Full output: {path} (read with offset/limit)]" in text
            # Images follow the truncated text, each after the path it was saved to.
            assert result.content[-1] == ImageContent(data=TINY_PNG_BASE64, mimeType="image/png")
            assert checkSavedImages(text.split("\n")[-2]) == "<saved>"
            # Bytes, so that a platform's newline translation cannot hide a difference.
            assert Path(path).read_bytes().decode("utf-8") == "\n".join(f"==> text {i + 1}/100 <==\nrow {i}" for i in range(100))
        finally:
            os.remove(path)

        small = await run(session, "return { ok: true };")
        assert "fullOutputPath" not in small.details


async def test_resolves_bash_calls_to_structured_results_also_for_non_zero_exit_codes(tmp_path):
    async with session_with(tmp_path, [createCodemodeExtension()], settings={"defaultTools": ["codemode", "bash"]}) as session:
        result = await run(
            session,
            'const r = await tools.bash({ command: "echo out; exit 3" });\ntext(JSON.stringify([r.output, r.exit_code, typeof r.wall_time_seconds]));',
        )
        assert not result.isError
        assert resultText(result) == '["out\\n",3,"number"]'


# https://github.com/earendil-works/pi/issues/10251
async def test_resolves_read_calls_to_text_for_text_files_and_to_image_blocks_that_image_shows(tmp_path):
    async with session_with(tmp_path, [createCodemodeExtension()], settings={"defaultTools": ["codemode", "read"]}) as session:
        workspace = tmp_path / "workspace"
        (workspace / "notes.txt").write_text("hello", encoding="utf-8")
        (workspace / "pixel.png").write_bytes(base64.b64decode(TINY_PNG_BASE64))
        result = await run(
            session,
            'text(await tools.read({ path: "notes.txt" }));\nconst shot = await tools.read({ path: "pixel.png" });\ntext(shot.note);\nimage(shot);',
        )
        assert not result.isError
        assert checkSavedImages(resultText(result)) == (
            "==> text 1/2 <==\nhello\n==> text 2/2 <==\nRead image file [image/png]\n<saved>\n<image>"
        )
        assert result.content[-1] == ImageContent(data=TINY_PNG_BASE64, mimeType="image/png")


async def test_persists_store_writes_as_custom_entries_for_later_calls(tmp_path):
    async with session_with(tmp_path, [createCodemodeExtension()], settings={"defaultTools": ["codemode"]}) as session:
        events: list = []
        session.subscribe(events.append)
        assert resultText(await run(session, INCREMENT)) == "1"
        assert resultText(await run(session, INCREMENT)) == "2"
        assert storeEntries(session) == [{"set": {"count": 1}, "delete": []}, {"set": {"count": 2}, "delete": []}]
        # Each write is announced as a session event, so it went through `pi.appendEntry`.
        appended = [
            event for event in events
            if isinstance(event, dict) and event.get("type") == "entry_appended"
            and event["entry"].get("type") == "custom" and event["entry"].get("customType") == CODEMODE_STORE_ENTRY_TYPE
        ]
        assert len(appended) == 2

        assert resultText(await run(session, 'store("count", undefined);\nreturn load("count") === undefined;')) == "true"
        assert storeEntries(session)[-1] == {"set": {}, "delete": ["count"]}


async def test_appends_nothing_for_failed_scripts_or_scripts_without_writes(tmp_path):
    async with session_with(tmp_path, [createCodemodeExtension()], settings={"defaultTools": ["codemode"]}) as session:
        assert (await run(session, 'store("count", 5);\nthrow new Error("boom");')).isError is True
        assert not (await run(session, 'return load("count") ?? "missing";')).isError
        assert storeEntries(session) == []


async def test_runs_without_a_session_starting_from_an_empty_store():
    tool = createCodemodeTool()
    result = await tool.execute("direct", {"code": INCREMENT}, None, None)
    assert result.content[1] == TextContent(text="1")


async def test_loads_the_values_written_on_the_current_branch(tmp_path):
    async with session_with(tmp_path, [createCodemodeExtension()], settings={"defaultTools": ["codemode"]}) as session:
        await run(session, INCREMENT)
        firstPrompt = next(entry for entry in session.sessionManager.getBranch() if entry["type"] == "message")
        assert resultText(await run(session, INCREMENT)) == "2"

        # Branch from the first prompt: the store entries written after it are on another path.
        session.sessionManager.branch(firstPrompt["id"])
        assert resultText(await run(session, INCREMENT)) == "1"


def test_folds_store_entries_from_the_root_ignoring_malformed_data():
    def entry(data, customType=CODEMODE_STORE_ENTRY_TYPE) -> dict:
        return {"type": "custom", "customType": customType, "data": data, "id": "x", "parentId": None, "timestamp": "1970-01-01T00:00:00.000Z"}

    assert readCodemodeStore([
        entry({"set": {"a": 1, "b": {"c": 2}}, "delete": []}),
        entry({"set": {"a": 3}, "delete": ["b"]}),
        entry({"set": {"z": 1}}),
        entry({"set": {"other": 1}, "delete": []}, "other-extension"),
    ]) == {"a": 3}


# -- codemode models -----------------------------------------------------------------------------

COST = {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}
scorerModel = {
    "type": "classifier", "id": "judge", "name": "Judge", "api": "test-classifier", "provider": "scorer",
    "baseUrl": "https://classifier.test/v1", "input": ["text"], "cost": COST, "contextWindow": 1000,
    "headers": {"X-Secret": "hunter2"},
}
painterModel = {
    "type": "image", "id": "painter", "name": "Painter", "api": "test-images", "provider": "scorer",
    "baseUrl": "https://images.test/v1", "input": ["text", "image"], "output": ["text", "image"], "cost": COST,
}
QUESTIONS = '{ approved: { type: "bool", instructions: "Approval?", criteria: { true: "yes", false: "no" } } }'


class _Scorer:
    def __init__(self) -> None:
        self.observed: list = []
        self.imageRequests: list = []
        self.active = 0
        self.maxActive = 0

    async def generateImages(self, model, context, options=None) -> AssistantImages:
        self.imageRequests.append({"baseUrl": model.baseUrl, "apiKey": options.apiKey, "input": [block.model_dump(exclude_none=True) for block in context.input]})
        prompt = next((block.text for block in context.input if block.type == "text"), None)
        base = {"api": model.api, "provider": model.provider, "model": model.id, "timestamp": 0}
        if prompt == "explode":
            return AssistantImages.model_validate({**base, "output": [], "stopReason": "error", "errorMessage": "painter exploded"})
        return AssistantImages.model_validate({
            **base,
            "output": [{"type": "text", "text": f"painted {prompt}"}, {"type": "image", "data": TINY_PNG_BASE64, "mimeType": "image/png"}],
            "usage": usage(100, 0.04),
            "stopReason": "stop",
        })

    async def classify(self, model, context, options=None) -> ClassifierResult:
        import asyncio

        self.active += 1
        self.maxActive = max(self.maxActive, self.active)
        await asyncio.sleep(0.01)
        self.active -= 1
        text = context.state.get("text")
        self.observed.append({"baseUrl": model.baseUrl, "apiKey": options.apiKey, "text": text})
        base = {"api": model.api, "provider": model.provider, "model": model.id, "timestamp": 0}
        if text == "explode":
            return ClassifierResult.model_validate({**base, "answers": {}, "stopReason": "error", "errorMessage": "classifier exploded"})
        return ClassifierResult.model_validate({
            **base,
            "answers": {"approved": {"type": "bool", "probability": 0.9 if text == "good" else 0.1}},
            "usage": usage(300, 0.001),
            "stopReason": "stop",
        })


async def _modelsSession(tmp_path, stack):
    session = await stack.enter_async_context(session_with(tmp_path, [createCodemodeExtension()], settings={"defaultTools": ["codemode"]}))
    scorer = _Scorer()
    session.modelRuntime.registerProvider("scorer", {
        "apiKey": "secret-key",
        "models": [scorerModel, painterModel],
        "images": {"test-images": scorer},
        "classifiers": {"test-classifier": scorer},
    })
    session.setActiveToolsByName(["codemode"])
    return session, scorer


async def test_declares_models_only_for_the_sessions_own_codemode_tool(tmp_path):
    from contextlib import AsyncExitStack

    async with AsyncExitStack() as stack:
        session, _scorer = await _modelsSession(tmp_path, stack)
        codemode = description(session, "codemode")
        # The description names the models globals and points to the docs for the API.
        assert "`models`: classifiers and image generation" in codemode
        assert CODEMODE_DOCS_PATH in codemode

    from tests.suite_harness import createHarness

    async with createHarness(tools=[createCodemodeTool()]) as overridden:
        plain = next(tool for tool in overridden.session.agent.state.tools if tool.name == "codemode")
        assert "`models`" not in plain.description


async def test_lists_models_and_classifies_with_catalog_auth_ignoring_script_supplied_fields(tmp_path):
    from contextlib import AsyncExitStack

    async with AsyncExitStack() as stack:
        session, scorer = await _modelsSession(tmp_path, stack)
        result = await run(session, f"""
            const [model] = await models.getAvailableOfType("classifier", "scorer");
            const listed = await models.getModelsOfType("classifier");
            const same = await models.getModelOfType("classifier", "scorer", "judge");
            const texts = ["good", "bad", "good", "bad", "good", "bad"];
            const results = await Promise.all(
                texts.map((text) => models.classify({{ ...model, baseUrl: "https://evil.test" }}, {{ state: {{ text }}, questions: {QUESTIONS} }})),
            );
            return {{
                id: model.id,
                headers: "headers" in model,
                listed: listed.some((entry) => entry.provider === "scorer" && entry.id === "judge"),
                same: same.id,
                missing: (await models.getModelOfType("classifier", "scorer", "nope")) === undefined,
                probabilities: results.map((r) => r.answers.approved.probability),
                cost: results[0].usage.cost.total,
            }};
        """)
        assert not result.isError
        assert json.loads(resultText(result)) == {
            "id": "judge", "headers": False, "listed": True, "same": "judge", "missing": True,
            "probabilities": [0.9, 0.1, 0.9, 0.1, 0.9, 0.1], "cost": 0.001,
        }
        assert len(scorer.observed) == 6
        assert all(entry["baseUrl"] == "https://classifier.test/v1" and entry["apiKey"] == "secret-key" for entry in scorer.observed)
        # Six classifications with at most four in flight.
        assert scorer.maxActive == 4
        assert [[call["name"], call["args"], call["status"], call["cost"]] for call in result.details["calls"]] == (
            [["models.classify", "scorer/judge", "ok", 0.001]] * 6
        )
        # The classifications' usage becomes the codemode result's usage.
        assert result.usage.input == 1800
        assert result.usage.cost.total == pytest.approx(0.006)
        assert session.getSessionStats().cost == pytest.approx(0.006)


async def test_generates_images_with_catalog_auth_and_attaches_them_through_image(tmp_path):
    from contextlib import AsyncExitStack

    async with AsyncExitStack() as stack:
        session, scorer = await _modelsSession(tmp_path, stack)
        result = await run(session, f"""
            const [model] = await models.getAvailableOfType("image", "scorer");
            const reference = {{ type: "image", data: "{TINY_PNG_BASE64}", mimeType: "image/png" }};
            const generated = await models.generateImages(
                {{ ...model, baseUrl: "https://evil.test" }},
                {{ input: [{{ type: "text", text: "a fox" }}, reference] }},
            );
            for (const block of generated.output) {{
                if (block.type === "image") image(block);
                else text(block.text);
            }}
            const failed = await models.generateImages(model, {{ input: [{{ type: "text", text: "explode" }}] }});
            const attempt = async (fn) => {{ try {{ await fn(); return "ok"; }} catch (error) {{ return error.message; }} }};
            return {{
                id: model.id,
                stopReason: generated.stopReason,
                failed: [failed.stopReason, failed.errorMessage],
                wrongType: await attempt(() => models.generateImages({{ provider: "scorer", id: "judge" }}, {{ input: [] }})),
            }};
        """)
        assert not result.isError
        header, text, *rest = checkSavedImages(resultText(result)).split("\n")
        assert header == "==> text 1/2 <=="
        assert text == "painted a fox"
        assert rest[0] == "<saved>"
        assert rest[1] == "<image>"
        assert rest[2] == "==> text 2/2 <=="
        assert json.loads("\n".join(rest[3:])) == {
            "id": "painter", "stopReason": "stop", "failed": ["error", "painter exploded"],
            "wrongType": '"scorer/judge" is a classifier model, not an image model. List the image models you can use with models.getAvailableOfType("image").',
        }
        assert result.content[2] == ImageContent(data=TINY_PNG_BASE64, mimeType="image/png")
        assert [[request["baseUrl"], request["apiKey"]] for request in scorer.imageRequests] == [
            ["https://images.test/v1", "secret-key"], ["https://images.test/v1", "secret-key"],
        ]
        assert scorer.imageRequests[0]["input"] == [
            {"type": "text", "text": "a fox"}, {"type": "image", "data": TINY_PNG_BASE64, "mimeType": "image/png"},
        ]
        assert [[call["name"], call["args"], call["status"], call.get("cost"), call.get("error")] for call in result.details["calls"]] == [
            ["models.generateImages", "scorer/painter", "ok", 0.04, None],
            ["models.generateImages", "scorer/painter", "error", None, "painter exploded"],
        ]
        assert result.usage.cost.total == pytest.approx(0.04)
        assert session.getSessionStats().cost == pytest.approx(0.04)


async def test_notes_generated_images_that_the_script_did_not_show(tmp_path):
    from contextlib import AsyncExitStack

    async with AsyncExitStack() as stack:
        session, _scorer = await _modelsSession(tmp_path, stack)
        result = await run(session, """
            const [model] = await models.getAvailableOfType("image", "scorer");
            const generated = await models.generateImages(model, { input: [{ type: "text", text: "a fox" }] });
            return generated.stopReason;
        """)
        assert not result.isError
        assert resultText(result) == (
            "stop\nNote: models.generateImages() returned 1 image that the script did not show. Show each image block of result.output with image(block)."
        )


async def test_reports_provider_errors_as_results_and_invalid_arguments_as_exceptions(tmp_path):
    from contextlib import AsyncExitStack

    async with AsyncExitStack() as stack:
        session, _scorer = await _modelsSession(tmp_path, stack)
        result = await run(session, f"""
            const model = await models.getModelOfType("classifier", "scorer", "judge");
            const failed = await models.classify(model, {{ state: {{ text: "explode" }}, questions: {QUESTIONS} }});
            const textOnly = await models.classify(model, {{
                state: {{ text: "good" }},
                images: [{{ type: "image", data: "aW1hZ2U=", mimeType: "image/png" }}],
                questions: {QUESTIONS},
            }});
            const attempt = async (fn) => {{ try {{ await fn(); return "ok"; }} catch (error) {{ return error.message; }} }};
            return {{
                failed: [failed.stopReason, failed.errorMessage],
                textOnly: [textOnly.stopReason, textOnly.errorMessage],
                badClassifierImage: await attempt(() =>
                    models.classify(model, {{ state: {{}}, images: [{{ data: "aW1hZ2U=" }}], questions: {QUESTIONS} }}),
                ),
                badType: await attempt(() => models.getModelsOfType("video")),
                unknown: await attempt(() => models.classify({{ provider: "scorer", id: "nope" }}, {{}})),
                noModel: await attempt(() => models.classify("judge", {{}})),
                undefinedModel: await attempt(() => models.classify(undefined, {{}})),
                noState: await attempt(() => models.classify(model, {{ questions: {QUESTIONS} }})),
                badQuestion: await attempt(() =>
                    models.classify(model, {{ state: {{}}, questions: {{ kind: {{ type: "choice", instructions: "Kind?", criteria: ["a", "b"] }} }} }}),
                ),
                badImage: await attempt(() => models.generateImages({{ provider: "scorer", id: "painter" }}, {{ prompt: "a fox" }})),
                arrayModel: await attempt(() => models.classify(["scorer", "judge"], {{}})),
                arrayContext: await attempt(() => models.classify(model, [])),
                arrayState: await attempt(() => models.classify(model, {{ state: [], questions: {QUESTIONS} }})),
                arrayQuestions: await attempt(() => models.classify(model, {{ state: {{}}, questions: [] }})),
                arrayQuestion: await attempt(() => models.classify(model, {{ state: {{}}, questions: {{ kind: [] }} }})),
                noInstructions: await attempt(() =>
                    models.classify(model, {{ state: {{}}, questions: {{ kind: {{ type: "bool", criteria: {{ true: "y", false: "n" }} }} }} }}),
                ),
                badBool: await attempt(() =>
                    models.classify(model, {{ state: {{}}, questions: {{ kind: {{ type: "bool", instructions: "Ok?", criteria: ["y", "n"] }} }} }}),
                ),
                badKind: await attempt(() =>
                    models.classify(model, {{ state: {{}}, questions: {{ kind: {{ type: "rank", instructions: "Rank?" }} }} }}),
                ),
                arrayImagesContext: await attempt(() => models.generateImages({{ provider: "scorer", id: "painter" }}, [])),
                badImageBlock: await attempt(() =>
                    models.generateImages({{ provider: "scorer", id: "painter" }}, {{ input: [{{ type: "text", text: "a fox" }}, {{ type: "image", data: "aW1hZ2U=" }}] }}),
                ),
                badSplit: await attempt(() => models.getModelOfType("classifier", "scorer/judge")),
            }};
        """)
        assert not result.isError
        value = json.loads(resultText(result))
        assert value["failed"] == ["error", "classifier exploded"]
        assert value["textOnly"] == ["error", "Model scorer/judge does not accept image input"]
        assert "models.classify() context.images[0] must be an image block, got { data }." in value["badClassifierImage"]
        assert 'Unknown model type "video"' in value["badType"]
        assert value["unknown"] == 'Unknown classifier model "scorer/nope". List the classifier models you can use with models.getAvailableOfType("classifier").'
        assert "models.classify() expects a classifier model as its first argument, got a string." in value["noModel"]
        assert "models.getModelOfType() returns undefined for an unknown provider or id." in value["undefinedModel"]
        assert "models.classify() context.state must be an object, got undefined." in value["noState"]
        assert "codemode.md" in value["noState"]
        assert 'context.questions.kind is a "choice" question, so criteria must map each label to its meaning.' in value["badQuestion"]
        assert "models.generateImages() context.input must be a non-empty array of blocks, got undefined." in value["badImage"]
        assert "The provider and the id are separate arguments" in value["badSplit"]
        assert "models.classify() expects a classifier model as its first argument, got an array." in value["arrayModel"]
        assert "models.classify() expects a context object as its second argument, got an empty array." in value["arrayContext"]
        assert "models.classify() context.state must be an object, got an empty array." in value["arrayState"]
        assert "models.classify() context.questions must map question IDs to questions, got an empty array." in value["arrayQuestions"]
        assert "models.classify() context.questions.kind must be a question object, got an empty array." in value["arrayQuestion"]
        assert "models.classify() context.questions.kind.instructions must be a string." in value["noInstructions"]
        assert 'context.questions.kind is a "bool" question, so criteria must be { true: string, false: string }.' in value["badBool"]
        assert 'context.questions.kind.type must be "choice", "score", or "bool", got "rank".' in value["badKind"]
        assert "models.generateImages() expects a context object as its second argument, got an empty array." in value["arrayImagesContext"]
        assert "models.generateImages() context.input[1] must be a text or image block, got { type, data }." in value["badImageBlock"]
        assert [[call["name"], call["status"], call.get("error")] for call in result.details["calls"]] == [
            ["models.classify", "error", "classifier exploded"],
            ["models.classify", "error", "Model scorer/judge does not accept image input"],
        ]
        assert result.usage is None


# -- codemode-renderer.test.ts -------------------------------------------------------------------


def _render(result, isError=False, expanded=True, width=200) -> str:
    from misaka.extensions.codemode.renderer import renderResult
    from misaka.modes.interactive.theme.theme import initTheme, theme
    from misaka.utils.ansi import stripAnsi

    initTheme("dark")
    context = SimpleNamespace(
        args={"code": ""}, toolCallId="call", invalidate=lambda: None, lastComponent=None, state={}, cwd="/",
        executionStarted=True, argsComplete=True, isPartial=False, expanded=expanded, showImages=False, isError=isError,
        durationMs=None, outputPad=1,
    )
    component = renderResult(result, {"expanded": expanded, "isPartial": False}, theme, context)
    return "\n".join(line.rstrip() for line in stripAnsi("\n".join(component.render(width))).split("\n")).strip()


HEADER = {"type": "text", "text": "Script completed\nWall time 0.1 seconds\nOutput:\n"}


# #10708
def test_renderer_shows_the_elapsed_time_while_the_script_runs_and_the_total_once_it_finished(monkeypatch):
    from misaka.core.tools import render_utils
    from misaka.extensions.codemode.tool import createCodemodeToolDefinition
    from misaka.modes.interactive.components.tool_execution import (
        ToolExecutionComponent,
    )
    from misaka.modes.interactive.theme.theme import initTheme
    from misaka.utils.ansi import stripAnsi

    initTheme("dark")
    # `vi.useFakeTimers()` + `vi.setSystemTime(0)`.
    clock = {"now": 0}
    monkeypatch.setattr(render_utils, "_now", lambda: clock["now"])
    tui = SimpleNamespace(requestRender=lambda: None)
    component = ToolExecutionComponent("codemode", "call", {"code": ""}, {}, createCodemodeToolDefinition(), tui, "/")

    def text() -> str:
        return stripAnsi("\n".join(component.render(120)))

    component.markExecutionStarted()
    component.updateResult({"content": [], "details": {"calls": []}, "isError": False}, True)
    clock["now"] += 3_000
    component.invalidate()
    assert "Elapsed 3.0s" in text()

    component.updateResult({"content": [], "details": {"calls": []}, "isError": False, "durationMs": 5_100}, False)
    assert "Took 5.1s" in text()
    assert "Elapsed" not in text()


def test_renderer_hides_the_script_header_and_shows_the_output():
    text = _render({
        "content": [HEADER, {"type": "text", "text": "hello"}],
        "details": {"calls": [{"id": "call/1", "name": "read", "args": '{"path":"a"}', "status": "ok", "durationMs": 5}]},
    })
    assert text == '✓ read {"path":"a"} 5ms\n\nhello'


def test_renderer_shows_the_cost_of_model_calls_and_their_total():
    call = {"name": "models.classify", "args": "scorer/judge", "status": "ok", "durationMs": 5}
    text = _render({
        "content": [HEADER],
        "details": {"calls": [
            {**call, "id": "call/models.classify/1", "cost": 0.000012936},
            {**call, "id": "call/models.classify/2", "cost": 0.02},
            {**call, "id": "call/models.classify/3"},
        ]},
    })
    assert text == "✓ models.classify scorer/judge 5ms $0.000013\n✓ models.classify scorer/judge 5ms $0.02\n✓ models.classify scorer/judge 5ms\nModel calls: $0.02"


def test_renderer_shows_results_without_a_header_such_as_rejected_options():
    text = _render({"content": [{"type": "text", "text": "The @options line must be followed by JavaScript source"}], "details": None}, True)
    assert text == "The @options line must be followed by JavaScript source"


def test_renderer_limits_collapsed_output_to_wrapped_lines_not_logical_lines():
    text = _render(
        {"content": [HEADER, {"type": "text", "text": "x" * 1000}], "details": {"calls": [], "fullOutputPath": "/tmp/out.txt"}},
        False, False, 50,
    )
    lines = text.split("\n")
    assert len(lines) == 7
    assert lines[:5] == ["x" * 50] * 5
    assert re.match(r"^\.\.\. \(15 more lines,", lines[5])
    assert lines[6] == "Full output: /tmp/out.txt"


# -- tool-search.test.ts: codemode description catalog ---------------------------------------------


def tool(name: str, description: str, properties: dict | None = None):
    return SimpleNamespace(name=name, label=name, description=description,
                           parameters={"type": "object", "properties": properties or {}}, outputSchema=None)


plain = tool("read_notes", "Read notes.")
github = [tool(f"mcp__github__{suffix}", f"GitHub {suffix}.") for suffix in ("a", "b", "c")]
docs = [tool("mcp__docs__search", "Search docs."), tool("mcp__docs__long", "Long " * 200)]
ALL = [plain, *github, *docs]
NAMESPACES = {
    **{entry.name: {"name": "mcp__github", "description": "GitHub server"} for entry in github},
    **{entry.name: {"name": "mcp__docs"} for entry in docs},
}


def test_catalog_lists_everything_without_a_budget():
    description = createCodemodeDescription(ALL, {"namespaces": NAMESPACES})
    assert "Nested tools:" in description
    assert "## mcp__github\nGitHub server" in description
    assert "## mcp__docs\n\n### `mcp__docs" in description
    # The search guidance is always there, so tools that appear later do not change it.
    assert "find unlisted tools, such as MCP tools" in description
    # #10555: the lookup helpers are async; without `await` scripts serialize the promise as {}.
    assert "`await searchTools(query, { limit?, namespace? })`" in description
    assert "`await describeTool(name)`" in description
    assert "`await describeNamespace(name)`" in description


def test_catalog_fills_the_budget_round_robin_cheapest_first_and_says_what_is_missing():
    # Each small section costs about 42 tokens: one tool per group, then one more.
    description = createCodemodeDescription(ALL, {"namespaces": NAMESPACES, "inlineBudget": 170})
    assert "### `read_notes`" in description
    assert "## mcp__docs (some tools not listed)" in description
    assert "### `mcp__docs__search`" in description
    assert "### `mcp__docs__long`" not in description
    assert "## mcp__github (some tools not listed)" in description
    assert "find unlisted tools, such as MCP tools" in description
    # Deterministic: the same input gives the same description.
    assert createCodemodeDescription(ALL, {"namespaces": NAMESPACES, "inlineBudget": 170}) == description


def test_catalog_leaves_deferred_tools_and_their_namespaces_out_entirely():
    deferred = {entry.name for entry in github}
    description = createCodemodeDescription(ALL, {"namespaces": NAMESPACES, "deferred": deferred})
    assert "mcp__github" not in description
    # Deferred tools, such as those of a server that connects later, do not change the description.
    assert description == createCodemodeDescription([plain, *docs], {"namespaces": NAMESPACES})


def test_catalog_leaves_namespace_instructions_out():
    description = createCodemodeDescription(github, {
        "namespaces": {entry.name: {"name": "mcp__github", "instructions": "Long usage guide."} for entry in github},
    })
    assert "## mcp__github\n\n### `mcp__github__a`" in description
    assert "Long usage guide." not in description


def test_catalog_lists_only_namespaces_with_a_zero_budget():
    description = createCodemodeDescription(ALL, {"namespaces": NAMESPACES, "inlineBudget": 0})
    assert "## mcp__docs (tools not listed)" in description
    assert "codemode tool declaration:" not in description


# -- research scopes ---------------------------------------------------------------------------------


async def test_a_research_scope_inherits_the_window_s_active_tools_without_adding_codemode(tmp_path):
    """Last Order's research window inherits the tools active in her session, her directly declared MCP tools
    among them; codemode and tool_search are Sisters' tools and are not added."""
    from misaka.core.research import planner

    async def search(*_args):
        return AgentToolResult(content=[TextContent(text="found")], details={})

    def mcpLike(pi) -> None:
        pi.registerTool(ToolDefinition(
            name="mcp__docs__search", label="docs/search", description="Search the docs.",
            parameters={"type": "object", "properties": {"q": {"type": "string"}}}, exposure="direct",
            namespace={"name": "mcp__docs"}, execute=search,
        ))

    async with session_with(tmp_path, [createCodemodeExtension(), mcpLike]) as session:
        names = list(planner.session_tools(SimpleNamespace(session=session)))
        assert "mcp__docs__search" in names
        assert "codemode" not in names and "tool_search" not in names


async def test_shows_null_returned_by_a_script(tmp_path):
    async with session_with(tmp_path, [createCodemodeExtension()], settings={"defaultTools": ["codemode"]}) as session:
        assert resultText(await run(session, "return null;")) == "null"
        assert resultText(await run(session, "return undefined;")) == ""


def test_describe_fields_lists_properties_in_object_entries_order():
    # `Object.entries(properties)`: integer-like names come first in ascending order, the rest as written (upstream's
    # one-line result description, checked against v1.3.0 tool.ts under node).
    from misaka.extensions.codemode.tool import describeFields

    schema = {
        "type": "object",
        "properties": {"b": {}, "1": {"type": "object", "properties": {"z": {}, "0": {}}}},
        "required": ["1"],
    }
    assert describeFields(schema, 1) == "{ 1: { 0?, z? }, b? }"
