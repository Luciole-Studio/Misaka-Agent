"""pi codemode's ``sandbox.test.ts``.

Left out: "accepts a worker path string" and "reports a missing worker file as a sandbox error" (the worker is a
thread, not a file).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import math
import re
import time
from typing import ClassVar

import pytest

from misaka.codemode import (
    MAX_OUTPUT_CHARS,
    MAX_OUTPUT_ITEMS,
    CodemodeSandbox,
    CodemodeTool,
    loadQuickJSWasm,
    quickjs_wasi,
)
from misaka.codemode.runtime import host as codemode_host
from misaka.codemode.runtime.prelude_source import PRELUDE_SOURCE
from misaka.utils.js import UNDEFINED, AbortController


@pytest.fixture
async def sandboxes():
    created: list[CodemodeSandbox] = []
    yield created
    await asyncio.gather(*(sandbox.close() for sandbox in created))


def createSandbox(sandboxes, tools=(), timeoutMs=10_000) -> CodemodeSandbox:
    sandbox = CodemodeSandbox({"tools": list(tools), "timeoutMs": timeoutMs})
    sandboxes.append(sandbox)
    return sandbox


def matches(actual, expected) -> bool:
    """``toMatchObject``: every expected key is present and matches, recursively."""
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(key in actual and matches(actual[key], value) for key, value in expected.items())
    if isinstance(expected, list):
        return isinstance(actual, list) and len(actual) == len(expected) and all(matches(a, e) for a, e in zip(actual, expected, strict=True))
    return actual == expected


def assertMatches(actual, expected) -> None:
    assert matches(actual, expected), f"{actual!r} does not match {expected!r}"


echo = CodemodeTool(name="echo", execute=lambda args, _context: args)

# Base64 of the leading bytes of each format. image() only inspects the signature.
PNG = "iVBORw0KGgo="
JPEG = "/9j/4A=="
GIF = "R0lGODlh"
WEBP = "UklGRgAAAABXRUJQ"


# -- embedded sources --------------------------------------------------------------------------


async def test_prelude_parses_as_javascript():
    # MISAKA fork: parsed by QuickJS itself (upstream uses Node's `vm.Script`); the prelude evaluates to a function.
    vm = quickjs_wasi.QuickJS.create(wasm=await loadQuickJSWasm())
    vm.evalCode(PRELUDE_SOURCE, "prelude.js").dispose()


# -- script execution --------------------------------------------------------------------------


async def test_returns_the_scripts_return_value_after_a_json_round_trip(sandboxes):
    sandbox = createSandbox(sandboxes)
    assertMatches(await sandbox.execute("return { a: 1, b: [true, 'x'] }"), {
        "ok": True,
        "value": {"a": 1, "b": [True, "x"]},
        "output": [],
        "calls": [],
    })
    assertMatches(await sandbox.execute("return 'plain'"), {"ok": True, "value": "plain"})
    assertMatches(await sandbox.execute(""), {"ok": True, "value": UNDEFINED})


async def test_supports_top_level_await(sandboxes):
    sandbox = createSandbox(sandboxes)
    assertMatches(await sandbox.execute("const x = await Promise.resolve(41); return x + 1"), {"ok": True, "value": 42})


async def test_collects_text_image_and_console_output_in_order(sandboxes):
    sandbox = createSandbox(sandboxes)
    result = await sandbox.execute(f"""
        console.log("hello", 1, {{ a: 1 }});
        text({{ json: true }});
        text(undefined);
        text(7);
        image("data:image/png;base64,{PNG}");
        image({{ image_url: "data:image/jpeg;base64,{JPEG}" }});
        image({{ type: "image", data: "{GIF}", mimeType: "image/gif" }});
        image("data:image/png;base64,{WEBP}");
        image({{ type: "image", data: "{PNG}" }});
        console.error(new Error("bad"));
        return null;
    """)
    assert result["ok"] is True
    assert result["output"][:-1] == [
        {"type": "text", "text": 'hello 1 {"a":1}', "console": True},
        {"type": "text", "text": '{"json":true}'},
        {"type": "text", "text": "undefined"},
        {"type": "text", "text": "7"},
        {"type": "image", "data": PNG, "mimeType": "image/png"},
        {"type": "image", "data": JPEG, "mimeType": "image/jpeg"},
        {"type": "image", "data": GIF, "mimeType": "image/gif"},
        # The MIME type comes from the data, not from the declared type.
        {"type": "image", "data": WEBP, "mimeType": "image/webp"},
        {"type": "image", "data": PNG, "mimeType": "image/png"},
    ]
    last = result["output"][-1]
    assert last["type"] == "text" and re.match(r"^Error: bad", last["text"]) and last.get("console") is True


async def test_rejects_invalid_text_and_image_arguments(sandboxes):
    sandbox = createSandbox(sandboxes)
    result = await sandbox.execute("""
        const errors = [];
        const circular = {};
        circular.self = circular;
        for (const run of [
            () => text(circular),
            () => image(""),
            () => image("https://example.com/a.png"),
            () => image("data:image/png,raw"),
            () => image({ type: "text", text: "x" }),
            () => image({ type: "image", data: "" }),
            () => image(42),
            () => image("data:image/png;base64,AAAA!"),
            () => image("data:image/png;base64,AAAAA"),
            () => image("data:image/png;base64,AA=A"),
            () => image("data:image/png;base64,"),
            () => image("data:image/png;base64,AAAA\\n[Output truncated]"),
            () => image({ type: "image", data: "AAAA!", mimeType: "image/png" }),
            () => image("data:image/png;base64,AAAA"),
            () => image("data:image/png;base64,QUJD"),
            () => image("data:image/jpeg;base64,/9j/9w=="),
        ]) {
            try { run(); errors.push("no error"); } catch (error) { errors.push(error.name + ": " + error.message); }
        }
        return errors;
    """)
    assert result["ok"] is True
    assert result["output"] == []
    errors = result["value"]
    assert re.match(r"^TypeError: .*circular", errors[0])
    assert errors[1:] == [
        'TypeError: image expects a non-empty image URL string, an object with image_url, or an image block { type: "image", data, mimeType }',
        "TypeError: remote image URLs are not supported in tool outputs. Pass a base64 data URI instead",
        "TypeError: invalid image output. Pass a base64 data URI instead",
        'TypeError: image only accepts image blocks, got "text"',
        "TypeError: image expected image block data",
        'TypeError: image expects a non-empty image URL string, an object with image_url, or an image block { type: "image", data, mimeType }',
        *["TypeError: invalid image output. The image data is not valid base64 (truncated or corrupted?)"] * 6,
        *["TypeError: invalid image output. The image data is not a PNG, JPEG, GIF, or WebP image"] * 3,
    ]


# https://github.com/earendil-works/pi/issues/10215
async def test_accepts_wrapped_and_large_base64_image_data(sandboxes):
    sandbox = createSandbox(sandboxes)
    large = "iVBORw0KGgoA" + "QUJD" * (256 * 1024)
    result = await sandbox.execute(f"""
        image("data:image/png;base64,iVBORw0K\\r\\nGgo=\\n");
        image("data:image/png;base64,{large}");
    """)
    assert result["ok"] is True
    assert result["output"] == [
        {"type": "image", "data": PNG, "mimeType": "image/png"},
        {"type": "image", "data": large, "mimeType": "image/png"},
    ]


async def test_ends_the_script_successfully_on_exit_keeping_output_and_store_writes(sandboxes):
    sandbox = createSandbox(sandboxes, [echo])
    result = await sandbox.execute("""
        text("before");
        store("k", 1);
        await tools.echo(1);
        try { exit(); } catch {}
        text("after");
        return "unreachable";
    """)
    assertMatches(result, {
        "ok": True,
        "value": UNDEFINED,
        "output": [{"type": "text", "text": "before"}],
        "storeWrites": {"set": {"k": 1}, "delete": []},
    })


async def test_keeps_output_produced_before_a_failure(sandboxes):
    sandbox = createSandbox(sandboxes)
    result = await sandbox.execute('text("partial");\nthrow new Error("boom")')
    assertMatches(result, {"ok": False, "output": [{"type": "text", "text": "partial"}]})


async def test_reports_syntax_errors_with_the_scripts_line_number(sandboxes):
    sandbox = createSandbox(sandboxes)
    result = await sandbox.execute("const a = 1;\nconst b = ;\nreturn a")
    assert result["ok"] is False
    assert result["error"]["kind"] == "script"
    assert result["error"]["name"] == "SyntaxError"
    assert re.search(r"codemode\.js:2", result["error"]["stack"])


async def test_reports_thrown_errors_with_the_scripts_line_number(sandboxes):
    sandbox = createSandbox(sandboxes)
    result = await sandbox.execute("const a = 1;\nthrow new TypeError('boom ' + a)")
    assert result["ok"] is False
    assertMatches(result["error"], {"kind": "script", "name": "TypeError", "message": "boom 1"})
    assert re.search(r"codemode\.js:2", result["error"]["stack"])


async def test_formats_stacks_like_v8_without_prelude_frames(sandboxes):
    sandbox = createSandbox(sandboxes)
    result = await sandbox.execute("console.log(new Error('inner'));\nthrow new RangeError('outer')")
    assert result["ok"] is False
    assert re.match(r"^RangeError: outer\n {4}at .*codemode\.js:2", result["error"]["stack"])
    assert "codemode-prelude.js" not in result["error"]["stack"]
    assert result["output"][0]["type"] == "text"
    assert re.match(r"^Error: inner\n {4}at .*codemode\.js:1", result["output"][0]["text"])
    assert "codemode-prelude.js" not in repr(result["output"])


async def test_reports_non_error_throws(sandboxes):
    sandbox = createSandbox(sandboxes)
    result = await sandbox.execute("throw { code: 7 }")
    assertMatches(result, {"ok": False, "error": {"kind": "script", "message": '{"code":7}'}})


async def test_reports_a_non_serializable_return_value_as_a_script_error(sandboxes):
    sandbox = createSandbox(sandboxes)
    result = await sandbox.execute("return 10n")
    assertMatches(result, {"ok": False, "error": {"kind": "script", "name": "TypeError"}})


# -- tools -------------------------------------------------------------------------------------


async def test_exposes_tools_as_async_functions_and_records_calls(sandboxes):
    seen: list = []

    def add(args, _context):
        seen.append(args)
        return {"sum": args["a"] + args["b"]}

    sandbox = createSandbox(sandboxes, [CodemodeTool(name="add", execute=add)])
    result = await sandbox.execute("""
        const first = await tools.add({ a: 1, b: 2 });
        const second = await tools.add({ a: first.sum, b: 10 });
        return second.sum;
    """)
    assertMatches(result, {"ok": True, "value": 13})
    assert seen == [{"a": 1, "b": 2}, {"a": 3, "b": 10}]
    assert [[call["name"], call["status"]] for call in result["calls"]] == [["add", "ok"], ["add", "ok"]]
    assert all(call["durationMs"] >= 0 for call in result["calls"])


async def test_runs_concurrent_calls_and_lists_tool_names(sandboxes):
    async def delay(args, _context):
        await asyncio.sleep(0.02)
        return args

    sandbox = createSandbox(sandboxes, [echo, CodemodeTool(name="delay", execute=delay)])
    result = await sandbox.execute("""
        const [a, b, c] = await Promise.all([tools.delay(1), tools.delay(2), tools.echo(3)]);
        return { values: [a, b, c], names: Object.keys(tools) };
    """)
    assertMatches(result, {"ok": True, "value": {"values": [1, 2, 3], "names": ["echo", "delay"]}})


async def test_exposes_tools_under_normalized_identifiers_and_lists_them_in_all_tools(sandboxes):
    sandbox = createSandbox(sandboxes, [
        CodemodeTool(name="my-tool", description="Dashes", execute=lambda _a, _c: "dash"),
        CodemodeTool(name="my_tool", description="Shadowed", execute=lambda _a, _c: "underscore"),
        CodemodeTool(name="mcp__docs__search", execute=lambda _a, _c: "mcp"),
    ])
    result = await sandbox.execute("""
        try { ALL_TOOLS.push({}); } catch {}
        return {
            all: ALL_TOOLS,
            calls: [await tools.my_tool(), await tools["my-tool"](), await tools.mcp__docs__search()],
        };
    """)
    assertMatches(result, {
        "ok": True,
        "value": {
            "all": [
                {"name": "my_tool", "description": "Dashes"},
                {"name": "mcp__docs__search", "description": ""},
            ],
            "calls": ["dash", "dash", "mcp"],
        },
    })


async def test_passes_undefined_arguments_and_results_through(sandboxes):
    sandbox = createSandbox(sandboxes, [CodemodeTool(name="noop", execute=lambda args, _c: args)])
    result = await sandbox.execute("return [await tools.noop(), await tools.noop(null)]")
    assertMatches(result, {"ok": True, "value": [None, None]})


async def test_turns_tool_errors_into_catchable_errors_in_the_script(sandboxes):
    def fail(_args, _context):
        raise RuntimeError("tool exploded")

    sandbox = createSandbox(sandboxes, [CodemodeTool(name="fail", execute=fail)])
    result = await sandbox.execute("""
        try {
            await tools.fail();
            return "no error";
        } catch (error) {
            return { isError: error instanceof Error, message: error.message };
        }
    """)
    assertMatches(result, {"ok": True, "value": {"isError": True, "message": "tool exploded"}})
    assertMatches(result["calls"], [{"name": "fail", "status": "error"}])


async def test_rejects_calls_to_unknown_tools(sandboxes):
    sandbox = createSandbox(sandboxes)
    result = await sandbox.execute("return await tools.missing()")
    assertMatches(result, {"ok": False, "error": {"kind": "script", "name": "TypeError"}})


async def test_aborts_unawaited_calls_when_the_script_returns(sandboxes):
    aborted = []

    async def slow(_args, context):
        await context["signal"].wait()
        aborted.append(True)
        raise RuntimeError("aborted")

    sandbox = createSandbox(sandboxes, [CodemodeTool(name="slow", execute=slow)])
    result = await sandbox.execute("tools.slow(); return 'early'")
    assertMatches(result, {"ok": True, "value": "early"})
    assertMatches(result["calls"], [{"name": "slow", "status": "cancelled"}])
    await asyncio.sleep(0)
    assert aborted == [True]


async def test_names_close_matches_when_a_script_reads_a_tool_that_does_not_exist(sandboxes):
    sandbox = createSandbox(sandboxes, [echo, CodemodeTool(name="web-search", execute=lambda _a, _c: "")])

    async def attempt(expression: str):
        result = await sandbox.execute(f"return {expression};")
        return result["value"] if result["ok"] else result["error"]["message"]

    assert await attempt("tools.Echo") == (
        'tools.Echo does not exist. Did you mean tools.echo? ALL_TOOLS lists every tool; searchTools(query) finds tools by topic. Check for a member with "Echo" in tools.'
    )
    assert "Did you mean tools.web_search?" in await attempt("tools.websearch")
    assert "Available: echo, web_search." in await attempt("tools.nothing")
    assert await attempt("['echo' in tools, 'nothing' in tools, String(tools.toString), JSON.stringify(tools)]") == [
        True, False, "undefined", "{}",
    ]


async def test_supports_register_and_unregister_between_executions(sandboxes):
    sandbox = createSandbox(sandboxes)
    sandbox.registerTool(echo)
    with pytest.raises(ValueError, match="already registered"):
        sandbox.registerTool(echo)
    assert [tool.name for tool in sandbox.tools] == ["echo"]
    assertMatches(await sandbox.execute("return await tools.echo('a')"), {"ok": True, "value": "a"})
    assert sandbox.unregisterTool("echo") is True
    assertMatches(await sandbox.execute("return 'echo' in tools"), {"ok": True, "value": False})


# -- store and load ----------------------------------------------------------------------------


async def test_reads_the_snapshot_and_reports_writes(sandboxes):
    sandbox = createSandbox(sandboxes)
    result = await sandbox.execute(
        """
        const seen = load("counter");
        store("counter", seen + 1);
        store("list", [1, { a: null }]);
        store("old", undefined);
        return [seen, load("counter"), load("missing"), load("old")];
        """,
        {"store": {"counter": 41, "old": "x"}},
    )
    assertMatches(result, {
        "ok": True,
        # undefined array elements become null in the JSON round trip of the return value.
        "value": [41, 42, None, None],
        "storeWrites": {"set": {"counter": 42, "list": [1, {"a": None}]}, "delete": ["old"]},
    })


async def test_returns_copies_so_mutating_a_loaded_value_does_not_change_the_store(sandboxes):
    sandbox = createSandbox(sandboxes)
    result = await sandbox.execute(
        """const value = load("obj"); value.a = 2; const kept = { b: 1 }; store("kept", kept); kept.b = 2;
        return [load("obj").a, load("kept").b];""",
        {"store": {"obj": {"a": 1}}},
    )
    assertMatches(result, {"ok": True, "value": [1, 1], "storeWrites": {"set": {"kept": {"b": 1}}}})


async def test_rejects_invalid_keys_values_and_oversized_writes_inside_the_script(sandboxes):
    sandbox = createSandbox(sandboxes)
    result = await sandbox.execute("""
        const attempt = (fn) => { try { fn(); return "ok"; } catch (error) { return error.name; } };
        return [
            attempt(() => store(1, "x")),
            attempt(() => load({})),
            attempt(() => store("fn", () => 1)),
            attempt(() => store("big", "x".repeat(300 * 1024))),
            attempt(() => { for (let i = 0; i < 8; i++) store("k" + i, "x".repeat(200 * 1024)); }),
        ];
    """)
    assertMatches(result, {"ok": True, "value": ["TypeError", "TypeError", "TypeError", "RangeError", "RangeError"]})


async def test_explains_oversized_writes(sandboxes):
    sandbox = createSandbox(sandboxes)
    result = await sandbox.execute('store("img", "x".repeat(300 * 1024));')
    assert result["ok"] is False
    assert 'store("img") value has 307202 characters of JSON' in result["error"]["message"]
    assert "Show images with image()" in result["error"]["message"]


def test_reserves_the_store_and_load_names():
    for name in ("store", "load"):
        with pytest.raises(ValueError, match="Invalid global"):
            CodemodeSandbox({"globals": [CodemodeTool(name=name, execute=lambda _a, _c: None)]})


# -- globals -----------------------------------------------------------------------------------


async def test_exposes_globals_as_top_level_functions_without_recording_them_as_calls(sandboxes):
    seen: list = []
    sandbox = CodemodeSandbox({
        "tools": [echo],
        "globals": [CodemodeTool(name="attach", execute=lambda args, _c: seen.append(args))],
    })
    sandboxes.append(sandbox)
    result = await sandbox.execute("""
        await attach({ ref: 1 });
        attach("not awaited");
        return [typeof attach, typeof globalThis.attach, await tools.echo(2)];
    """)
    assertMatches(result, {"ok": True, "value": ["function", "function", 2]})
    assert [call["name"] for call in result["calls"]] == ["echo"]
    # Messages are handled in order, so an unawaited global still runs before the script settles.
    assert seen == [{"ref": 1}, "not awaited"]


async def test_groups_namespaced_globals_and_spreads_arguments_on_request(sandboxes):
    seen: list = []
    sandbox = CodemodeSandbox({
        "globals": [
            CodemodeTool(name="models.list", spread=True, execute=lambda args, _c: seen.append(args)),
            CodemodeTool(name="models.first", execute=lambda args, _c: args),
        ],
    })
    sandboxes.append(sandbox)
    result = await sandbox.execute("""
        await models.list("classifier", undefined, 3);
        await models.list();
        try { models.extra = 1; } catch {}
        return [Object.keys(models), await models.first("a", "ignored"), "extra" in models];
    """)
    assertMatches(result, {"ok": True, "value": [["list", "first"], "a", False]})
    # undefined array elements become null in the JSON round trip.
    assert seen == [["classifier", None, 3], []]


async def test_names_the_members_of_a_namespace_when_a_script_reads_one_that_does_not_exist(sandboxes):
    execute = lambda _a, _c: None
    sandbox = CodemodeSandbox({
        "globals": [
            CodemodeTool(name="models.classify", execute=execute),
            CodemodeTool(name="models.generateImages", execute=execute),
        ],
    })
    sandboxes.append(sandbox)
    result = await sandbox.execute("await models.generateImage();")
    assert result["ok"] is False
    assert result["error"]["message"] == (
        'models.generateImage does not exist. Did you mean models.generateImages? Check for a member with "generateImage" in models.'
    )


def test_rejects_invalid_and_reserved_global_names():
    execute = lambda _a, _c: None
    for name in ["a.b.c", "a.", ".a", "tools.x", "store.x", "a.not-valid"]:
        with pytest.raises(ValueError, match="Invalid global"):
            CodemodeSandbox({"globals": [CodemodeTool(name=name, execute=execute)]})
    with pytest.raises(ValueError, match="conflicts with the namespace"):
        CodemodeSandbox({"globals": [CodemodeTool(name="models", execute=execute), CodemodeTool(name="models.list", execute=execute)]})
    for name in ["not-valid", "tools", "console"]:
        with pytest.raises(ValueError, match="Invalid global"):
            CodemodeSandbox({"globals": [CodemodeTool(name=name, execute=execute)]})


# -- limits and lifetime -----------------------------------------------------------------------


async def test_terminates_a_synchronous_infinite_loop_on_timeout(sandboxes):
    sandbox = createSandbox(sandboxes)
    started = time.perf_counter()
    result = await sandbox.execute("while (true) {}", {"timeoutMs": 200})
    assertMatches(result, {"ok": False, "error": {"kind": "timeout"}})
    assert time.perf_counter() - started < 5


async def test_runs_without_a_deadline_when_timeout_is_infinite(sandboxes):
    async def wait(_args, _context):
        await asyncio.sleep(0.05)
        return "late"

    sandbox = createSandbox(sandboxes, [CodemodeTool(name="wait", execute=wait)])
    result = await sandbox.execute("return await tools.wait()", {"timeoutMs": math.inf})
    assertMatches(result, {"ok": True, "value": "late"})


async def test_fails_a_script_that_waits_on_a_promise_nothing_can_settle(sandboxes):
    sandbox = createSandbox(sandboxes, [CodemodeTool(name="echo", execute=lambda args, _c: args)])
    result = await sandbox.execute("await tools.echo(1); await new Promise(() => {}); return 'never'", {"timeoutMs": math.inf})
    assert result["ok"] is False
    assert result["error"]["kind"] == "script"
    assert "can never settle" in result["error"]["message"]
    # Returning while a call is still pending is not a stall.
    returned = await sandbox.execute("tools.echo(2); return 'early'", {"timeoutMs": math.inf})
    assertMatches(returned, {"ok": True, "value": "early"})


async def test_terminates_a_microtask_spinning_loop_on_timeout(sandboxes):
    sandbox = createSandbox(sandboxes)
    result = await sandbox.execute("while (true) await null", {"timeoutMs": 200})
    assertMatches(result, {"ok": False, "error": {"kind": "timeout"}})


async def test_aborts_via_signal_and_cancels_in_flight_calls(sandboxes):
    controller = AbortController()
    toolSignal: list = []
    called: list[asyncio.Future] = []

    def nextCall() -> asyncio.Future:
        future = asyncio.get_running_loop().create_future()
        called.append(future)
        return future

    async def hang(_args, context):
        toolSignal.append(context["signal"])
        if called:
            called.pop(0).set_result(None)
        await asyncio.get_running_loop().create_future()

    sandbox = createSandbox(sandboxes, [CodemodeTool(name="hang", execute=hang)])
    firstCall = nextCall()
    promise = sandbox.execute("await tools.hang(); return 'never'")
    await firstCall
    controller.abort(RuntimeError("user cancelled"))
    assert toolSignal[-1].aborted is False
    result = await sandbox.execute("await tools.hang()", {"signal": controller.signal})
    assertMatches(result, {"ok": False, "error": {"kind": "aborted", "message": "user cancelled"}})

    second = AbortController()
    secondCall = nextCall()
    pending = sandbox.execute("await tools.hang(); return 'never'", {"signal": second.signal})
    await secondCall
    second.abort()
    aborted = await pending
    assertMatches(aborted, {"ok": False, "error": {"kind": "aborted"}})
    assertMatches(aborted["calls"], [{"name": "hang", "status": "cancelled"}])
    assert toolSignal[-1].aborted is True

    await sandbox.close()
    assertMatches(await promise, {"ok": False, "error": {"kind": "aborted", "message": "Sandbox closed"}})


# #10283: the host keeps all output, so a script that prints in a loop must not grow it without bound.
async def test_fails_a_script_whose_output_passes_the_limits_even_if_it_catches_the_error(sandboxes):
    sandbox = createSandbox(sandboxes)
    for print_ in ("text(s)", "console.log(s)", 'image("data:image/png;base64," + p)'):
        result = await sandbox.execute(f"""
            const s = "x".repeat(1 << 20);
            const p = "iVBORw0KGgoA" + "A".repeat(1 << 20);
            for (;;) {{ try {{ {print_}; }} catch {{}} }}
        """)
        assertMatches(result, {"ok": False, "error": {"kind": "script", "name": "RangeError"}})
        assert "script output exceeded" in result["error"]["message"]
        chars = sum(len(item["text"]) if item["type"] == "text" else len(item["data"]) for item in result["output"])
        assert chars <= MAX_OUTPUT_CHARS
        assert chars > MAX_OUTPUT_CHARS - (2 << 20)

    empty = await sandbox.execute('for (;;) text("");')
    assertMatches(empty, {"ok": False, "error": {"name": "RangeError"}})
    assert len(empty["output"]) == MAX_OUTPUT_ITEMS


async def test_rejects_execute_after_close(sandboxes):
    sandbox = createSandbox(sandboxes)
    await sandbox.close()
    with pytest.raises(RuntimeError, match="closed"):
        await sandbox.execute("return 1")


async def test_runs_executions_in_parallel_without_sharing_state(sandboxes):
    sandbox = createSandbox(sandboxes)
    results = await asyncio.gather(
        sandbox.execute("globalThis.shared = 'a'; await null; return globalThis.shared"),
        sandbox.execute("globalThis.shared = 'b'; await null; return globalThis.shared"),
        sandbox.execute("return typeof globalThis.shared"),
    )
    assert [result["value"] if result["ok"] else result["error"] for result in results] == ["a", "b", "undefined"]


class RawWorker:
    """Stands in for the real worker in host tests (upstream's ``fixtures/raw-worker.ts``): posts the JSON
    message (or array of messages) passed as the script source, so tests can send the host payloads the real
    prelude never produces."""

    def __init__(self, workerData) -> None:
        self._listeners = {}
        self.threadExited = concurrent.futures.Future()
        messages = json.loads(workerData["code"])
        for message in messages if isinstance(messages, list) else [messages]:
            asyncio.get_running_loop().call_soon(lambda message=message: self._emit("message", message))

    def _emit(self, event, payload) -> None:
        for listener in self._listeners.get(event, []):
            listener(payload)

    def on(self, event, listener) -> None:
        self._listeners.setdefault(event, []).append(listener)

    def postMessage(self, _message) -> None:
        pass

    def terminate(self):
        if not self.threadExited.done():
            self.threadExited.set_result(None)


async def test_reports_a_failing_wasm_module_as_a_sandbox_error(sandboxes):
    failed = asyncio.get_running_loop().create_future()
    failed.set_exception(RuntimeError("no wasm"))
    sandbox = CodemodeSandbox({"wasm": failed})
    sandboxes.append(sandbox)
    assertMatches(await sandbox.execute("return 1"), {
        "ok": False,
        "error": {"kind": "sandbox", "message": "Failed to load QuickJS: no wasm"},
    })


# #10444: a malformed payload from the worker must fail the run as a sandbox error instead of
# raising in the host's message listener and never settling.
@pytest.mark.parametrize(
    ("message", "reason"),
    [
        ({"type": "done", "ok": True, "value": "1", "writes": "null"}, "store writes are not an array"),
        ({"type": "done", "ok": True, "value": "1", "writes": "[1]"}, "store writes contain a malformed entry"),
        ({"type": "done", "ok": True, "value": "1", "writes": '[["k", "{"]]'}, 'store value for "k" is not valid JSON'),
        ({"type": "done", "ok": True, "value": "{", "writes": "[]"}, "return value is not valid JSON"),
        ({"type": "done", "ok": False, "error": "5"}, "script error is not an object"),
        ({"type": "done", "ok": False, "error": "{}"}, "script error is malformed"),
        ({"type": "nonsense"}, "unknown message from the worker"),
    ],
)
async def test_reports_a_broken_bridge_as_a_sandbox_error(sandboxes, monkeypatch, message, reason):
    # MISAKA fork: there is no `workerUrl` option; the fixture replaces the worker class.
    monkeypatch.setattr(codemode_host, "Worker", RawWorker)
    sandbox = createSandbox(sandboxes)
    result = await sandbox.execute(json.dumps(message))
    assertMatches(result, {"ok": False, "error": {"kind": "sandbox"}})
    assert f"Sandbox bridge broken: {reason}" in result["error"]["message"]


async def test_turns_deep_recursion_into_a_catchable_range_error(sandboxes):
    sandbox = createSandbox(sandboxes)
    result = await sandbox.execute("""
        let depth = 0;
        function dive() { depth++; dive(); }
        try { dive(); } catch (error) { return [error.name, depth > 1000]; }
    """)
    assertMatches(result, {"ok": True, "value": ["RangeError", True]})


# -- escape hatches ----------------------------------------------------------------------------


async def test_has_no_host_globals(sandboxes):
    sandbox = createSandbox(sandboxes)
    result = await sandbox.execute("""
        return [
            typeof process, typeof require, typeof module, typeof setTimeout, typeof fetch,
            typeof WebAssembly, typeof std, typeof os, typeof globalThis.constructor,
        ]
    """)
    assertMatches(result, {"ok": True, "value": [*["undefined"] * 8, "function"]})


async def test_keeps_eval_and_function_inside_the_vm(sandboxes):
    # Code generation is allowed: it can only produce more code in the same isolate.
    sandbox = createSandbox(sandboxes, [echo])
    result = await sandbox.execute("""
        return [
            eval("typeof process"),
            new Function("return typeof process")(),
            tools.echo.constructor("return typeof require")(),
            (async () => {}).constructor("return typeof setTimeout")() instanceof Promise,
        ];
    """)
    assertMatches(result, {"ok": True, "value": ["undefined", "undefined", "undefined", True]})


async def test_rejects_dynamic_import(sandboxes):
    sandbox = createSandbox(sandboxes)
    result = await sandbox.execute("""
        try { await import("node:fs"); return "imported"; } catch (error) { return error.constructor.name; }
    """)
    assert result["ok"] is True
    assert result["value"] != "imported"


# #10444: the prelude shares the built-ins with the script, so patching them could corrupt what
# the prelude sends to the host.
async def test_ignores_patches_to_built_ins_and_built_in_globals(sandboxes):
    sandbox = createSandbox(sandboxes, [echo])
    result = await sandbox.execute("""
        Array.prototype.toJSON = () => null;
        Object.prototype.toJSON = () => 5;
        Promise.prototype.then = () => {};
        Map.prototype.get = () => undefined;
        globalThis.JSON = { stringify: () => "x", parse: () => "x" };
        store("k", [1]);
        return [await tools.echo([2]), JSON.stringify({ a: 1 })];
    """)
    assertMatches(result, {"ok": True, "value": [[2], '{"a":1}'], "storeWrites": {"set": {"k": [1]}}})


async def test_freezes_intrinsics_that_are_only_reachable_from_instances(sandboxes):
    sandbox = createSandbox(sandboxes)
    result = await sandbox.execute("""
        return [
            Object.getPrototypeOf(function* () {}).prototype,
            Object.getPrototypeOf(async function () {}),
            Object.getPrototypeOf(Int8Array).prototype,
            Object.getPrototypeOf([][Symbol.iterator]()),
            Object.getPrototypeOf(Object.getPrototypeOf([][Symbol.iterator]())),
            Object.getPrototypeOf(new Map()[Symbol.iterator]()),
            Object.getPrototypeOf(/a/[Symbol.matchAll]("")),
        ].every((object) => Object.isFrozen(object));
    """)
    assertMatches(result, {"ok": True, "value": True})


async def test_still_lets_instances_override_properties_of_frozen_prototypes(sandboxes):
    sandbox = createSandbox(sandboxes)
    result = await sandbox.execute("""
        const object = {};
        object.toString = () => "custom";
        function Legacy() {}
        Legacy.prototype = Object.create(Error.prototype);
        Legacy.prototype.constructor = Legacy;
        const bare = new Error();
        bare.message = "set later";
        class MyError extends Error {
            constructor(message) {
                super(message);
                this.name = "MyError";
            }
        }
        let patched = "silent";
        try { Error.prototype.name = "Patched"; } catch (error) { patched = error.constructor.name; }
        return [String(object), new Legacy().constructor === Legacy, bare.message, new MyError("x").name, Error.prototype.name, patched];
    """)
    assertMatches(result, {"ok": True, "value": ["custom", True, "set later", "MyError", "Error", "TypeError"]})


async def test_reports_errors_whose_name_or_message_is_not_a_string(sandboxes):
    sandbox = createSandbox(sandboxes)
    result = await sandbox.execute("const error = new Error('x'); error.message = 42; throw error;")
    assertMatches(result, {"ok": False, "error": {"kind": "script", "name": "Error", "message": "42"}})


async def test_keeps_tools_and_console_frozen(sandboxes):
    sandbox = createSandbox(sandboxes, [echo])
    result = await sandbox.execute("""
        try { tools.echo = () => 'nope'; } catch {}
        try { tools.extra = () => 'nope'; } catch {}
        try { globalThis.tools = null; } catch {}
        return ["extra" in tools, await tools.echo('still')];
    """)
    assertMatches(result, {"ok": True, "value": [False, "still"]})


# -- misaka regressions ------------------------------------------------------------------------


async def test_running_out_of_memory_throws_inside_the_script():
    sandbox = CodemodeSandbox({"memoryLimitBytes": 64 * 1024 * 1024})
    try:
        result = await sandbox.execute(
            'let a = []; try { while (true) a.push("x".repeat(1 << 20) + a.length); } catch (error) { return String(error); }'
        )
    finally:
        await sandbox.close()
    assertMatches(result, {"ok": True, "value": "InternalError: out of memory"})


async def test_ends_a_run_when_the_script_reports_done_and_keeps_spinning(sandboxes):
    # The host terminates the worker on "done"; the interrupt flag stops the spinning VM.
    sandbox = createSandbox(sandboxes)
    started = time.perf_counter()
    result = await asyncio.wait_for(
        sandbox.execute('text("before"); for (;;) { try { exit(); } catch {} }', {"timeoutMs": 5000}), 15
    )
    assertMatches(result, {"ok": True, "output": [{"type": "text", "text": "before"}]})
    assert time.perf_counter() - started < 2


async def test_a_tool_may_return_an_awaitable_or_a_plain_value(sandboxes):
    async def later(args, _context):
        return [args, "later"]

    sandbox = createSandbox(sandboxes, [echo, CodemodeTool(name="later", execute=later)])
    assertMatches(await sandbox.execute("return [await tools.echo(1), await tools.later(2)]"), {"ok": True, "value": [1, [2, "later"]]})


# -- review findings (MISAKA): hangs and null versus undefined ----------------------------------


async def test_a_value_nested_deeper_than_the_json_decoder_allows_settles_instead_of_hanging(sandboxes):
    sandbox = createSandbox(sandboxes)
    # Python's decoder gives up a few thousand levels down (how far depends on the platform); either way
    # the run must settle and the sandbox must close.
    deepStore = 'let list = null; for (let i = 0; i < 4000; i++) list = { i, next: list }; store("list", list); return "stored"'
    stored = await asyncio.wait_for(sandbox.execute(deepStore, {"timeoutMs": 2000}), 15)
    assert stored["ok"] is True or stored["error"]["kind"] == "sandbox"
    deepReturn = "let v = []; for (let i = 0; i < 20000; i++) v = [v]; return v"
    returned = await asyncio.wait_for(sandbox.execute(deepReturn, {"timeoutMs": 5000}), 15)
    assert "ok" in returned
    await asyncio.wait_for(sandbox.close(), 5)


async def test_a_script_cannot_run_code_while_it_is_compiled(sandboxes):
    sandbox = createSandbox(sandboxes)
    # Closes the wrapper function, spins at the top level, then reopens one for the closing text to end.
    code = "}); while (true) {}; ((tools, console) => {"
    result = await asyncio.wait_for(sandbox.execute(code, {"timeoutMs": 500}), 15)
    assertMatches(result, {"ok": False, "error": {"kind": "timeout"}})


class RecordingWorker(codemode_host.Worker):
    """The real worker, remembered: the launcher keeps its worker to itself."""

    created: ClassVar[list[codemode_host.Worker]] = []

    def __init__(self, workerData) -> None:
        super().__init__(workerData)
        RecordingWorker.created.append(self)


async def test_cancelling_the_task_that_awaits_a_run_stops_the_run(sandboxes, monkeypatch):
    monkeypatch.setattr(RecordingWorker, "created", [])
    monkeypatch.setattr(codemode_host, "Worker", RecordingWorker)
    hang = CodemodeTool(name="hang", execute=lambda _args, _context: asyncio.Event().wait())
    sandbox = createSandbox(sandboxes, [hang])

    async def wait():
        return await sandbox.execute("await tools.hang()", {"timeoutMs": math.inf})

    task = asyncio.get_running_loop().create_task(wait())
    await asyncio.sleep(0.3)
    (worker,) = RecordingWorker.created
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.3)
    assert worker is not None and not worker._thread.is_alive()


async def test_keeps_null_and_undefined_apart_for_return_values_arguments_and_results(sandboxes):
    seen = []

    def record(args, _context):
        seen.append(args)
        return None if len(seen) == 1 else UNDEFINED

    sandbox = createSandbox(sandboxes, [CodemodeTool(name="record", execute=record)])
    assertMatches(await sandbox.execute("return null"), {"ok": True, "value": None})
    assertMatches(await sandbox.execute("return undefined"), {"ok": True, "value": UNDEFINED})
    result = await sandbox.execute("return [await tools.record(null), await tools.record(), typeof (await tools.record(undefined))]")
    assertMatches(result, {"ok": True, "value": [None, None, "undefined"]})
    assert seen == [None, UNDEFINED, UNDEFINED]
    # The first call answered null, which is not undefined.
    result = await sandbox.execute("const a = await tools.record(1); return [a === null, a === undefined]")
    assertMatches(result, {"ok": True, "value": [False, True]})


async def test_a_tool_that_ends_in_a_cancellation_rejects_instead_of_leaving_the_script_waiting(sandboxes):
    async def cancelled(_args, _context):
        future = asyncio.get_running_loop().create_future()
        future.cancel()
        await future

    sandbox = createSandbox(sandboxes, [CodemodeTool(name="cancelled", execute=cancelled)])
    result = await asyncio.wait_for(
        sandbox.execute("try { await tools.cancelled(); return 'resolved'; } catch (error) { return error.message; }"), 15
    )
    assertMatches(result, {"ok": True, "value": "Tool call was cancelled"})


async def test_a_lone_surrogate_in_the_code_does_not_fail_the_sandbox(sandboxes):
    sandbox = createSandbox(sandboxes)
    result = await sandbox.execute("// \ud800\nreturn 1")
    assertMatches(result, {"ok": True, "value": 1})


async def test_keeps_the_output_a_script_printed_before_a_timeout_or_an_abort(sandboxes):
    sandbox = createSandbox(sandboxes)
    result = await asyncio.wait_for(sandbox.execute('text("before"); while (true) {}', {"timeoutMs": 500}), 15)
    assertMatches(result, {"ok": False, "error": {"kind": "timeout"}, "output": [{"type": "text", "text": "before"}]})

    controller = AbortController()
    pending = sandbox.execute('text("first"); text("second"); while (true) {}', {"signal": controller.signal})
    await asyncio.sleep(0.5)
    controller.abort()
    result = await asyncio.wait_for(pending, 15)
    assertMatches(result, {
        "ok": False,
        "error": {"kind": "aborted"},
        "output": [{"type": "text", "text": "first"}, {"type": "text", "text": "second"}],
    })


async def test_running_out_of_memory_after_an_await_throws_inside_the_script():
    sandbox = CodemodeSandbox({"memoryLimitBytes": 64 * 1024 * 1024, "tools": [echo]})
    try:
        result = await sandbox.execute(
            'await tools.echo(1); let a = []; try { while (true) a.push("x".repeat(1 << 20) + a.length); }'
            " catch (error) { return String(error); }"
        )
    finally:
        await sandbox.close()
    assertMatches(result, {"ok": True, "value": "InternalError: out of memory"})


@pytest.mark.parametrize(("code", "expected"), [
    ("return btoa('hello') + ' ' + atob('aGVsbG8=') + ' ' + atob(' aGVs bG8 ') + ' ' + atob('aGVsbG8')",
     "aGVsbG8= hello hello hello"),
    ("try { btoa('\u0100'); return 'no' } catch (e) { return e.name + ': ' + e.message + ' ' + (e instanceof DOMException) + ' ' + e.code }",
     "InvalidCharacterError: String contains an invalid character true 5"),
    ("try { atob('a'); return 'no' } catch (e) { return e.name + ': ' + e.message + ' ' + e.code }",
     "InvalidCharacterError: The string to be decoded is not correctly encoded 5"),
    ("return [btoa(), atob(''), btoa(null), btoa(123)].join('|')", "dW5kZWZpbmVk||bnVsbA==|MTIz"),
    ("let order = []; queueMicrotask(() => order.push('micro')); await null; order.push('after'); return order.join(',')",
     "micro,after"),
    ("try { queueMicrotask(1); return 'no' } catch (e) { return e.name + ': ' + e.message }", "TypeError: not a function"),
    ("const e = new DOMException('m', 'AbortError'); return [e.name, e.message, e.code, e instanceof Error, String(e), "
     "Object.prototype.toString.call(e)].join('|')", "AbortError|m|20|true|AbortError: m|[object DOMException]"),
    ("return [typeof InternalError, new InternalError('x') instanceof Error, String(new InternalError('x'))].join('|')",
     "function|true|InternalError: x"),
    ("return typeof performance.now() + ' ' + (performance.now() >= 0) + ' ' + typeof performance.timeOrigin",
     "number true number"),
])
async def test_the_globals_quickjs_adds_behave_as_they_do_upstream(code, expected):
    # Expectations are pi 1.1.0's sandbox output (quickjs-wasi) for the same scripts; they held for the V8 shims
    # misaka had before it ran quickjs-wasi itself.
    sandbox = CodemodeSandbox({})
    try:
        result = await sandbox.execute(code)
    finally:
        await sandbox.close()
    assert result["ok"] is True and result["value"] == expected


# -- pi-alignment fixes (MISAKA) ---------------------------------------------------------------


async def test_a_top_level_throw_after_closing_the_wrapper_is_a_script_error(sandboxes):
    result = await createSandbox(sandboxes).execute("}); throw new Error('x'); (() => {")
    assertMatches(result, {"ok": False, "error": {"kind": "script", "name": "Error", "message": "x"}})


async def test_stacks_hold_no_host_frames(sandboxes):
    result = await createSandbox(sandboxes).execute("throw new Error('boom');")
    assert "codemode-host" not in result["error"]["stack"]


async def test_output_made_before_running_out_of_memory_is_kept(sandboxes):
    sandbox = CodemodeSandbox({"timeoutMs": 20_000, "memoryLimitBytes": 64 * 1024 * 1024})
    sandboxes.append(sandbox)
    result = await sandbox.execute("text('before'); const a = []; while (true) a.push(new Array(100000).fill(1));")
    assertMatches(result, {
        "ok": False,
        "error": {"kind": "script", "name": "InternalError", "message": "out of memory"},
        "output": [{"type": "text", "text": "before"}],
    })


async def test_a_tool_that_aborts_the_execution_records_its_call_as_cancelled(sandboxes):
    controller = AbortController()

    def stop(_args, _context):
        controller.abort()
        return 1

    sandbox = createSandbox(sandboxes, [CodemodeTool(name="stop", execute=stop)])
    result = await sandbox.execute("return await tools.stop();", {"signal": controller.signal})
    assertMatches(result, {"ok": False, "error": {"kind": "aborted"}, "calls": [{"name": "stop", "status": "cancelled"}]})


async def test_closing_after_a_caller_cancelled_its_execution_does_not_raise(sandboxes):
    sandbox = createSandbox(sandboxes)
    pending = sandbox.execute("await new Promise(() => {});")
    pending.cancel()
    await sandbox.close()


async def test_a_script_spinning_after_it_reported_done_past_an_await_is_stopped(sandboxes):
    started = time.monotonic()
    result = await createSandbox(sandboxes, [echo], timeoutMs=30_000).execute(
        "await tools.echo(1); try { exit(); } catch {} for (;;) {}"
    )
    assertMatches(result, {"ok": True, "calls": [{"name": "echo", "status": "ok"}]})
    assert time.monotonic() - started < 5


# -- the QuickJS wasm on wasmtime (MISAKA) -------------------------------------------------------


@pytest.mark.parametrize("recursion", [
    "function dive() { dive(); } dive();",
    "function dive() { [1].map(dive); } dive();",
    "const o = { get x() { return this.x; } }; o.x;",
    "const p = new Proxy({}, { get(target, key, receiver) { return receiver[key]; } }); p.a;",
    "const o = { toString() { return String(o); } }; String(o);",
    "class A { constructor() { new B(); } } class B extends A { constructor() { super(); } } new B();",
])
async def test_recursion_through_native_functions_is_a_catchable_range_error(sandboxes, recursion):
    # The wasm stack (quickjs_wasi.MAX_WASM_STACK) outlasts QuickJS's own guard in each of these; with wasmtime's
    # default 512 KiB all but the first trapped.
    result = await createSandbox(sandboxes).execute(f"try {{ {recursion} }} catch (error) {{ return error.name; }}")
    assertMatches(result, {"ok": True, "value": "RangeError"})


async def test_exhausting_the_wasm_stack_fails_the_run_and_leaves_the_sandbox_working(sandboxes):
    # Parsing nests deeper than any guard: the wasm traps, which ends the run as a sandbox failure.
    sandbox = createSandbox(sandboxes)
    result = await sandbox.execute("return JSON.parse('['.repeat(200000) + ']'.repeat(200000)).length")
    assertMatches(result, {"ok": False, "error": {"kind": "sandbox"}})
    assert "call stack exhausted" in result["error"]["message"]
    assertMatches(await sandbox.execute("return 1"), {"ok": True, "value": 1})


async def test_calls_into_wasm_take_the_unchecked_path():
    # `Func.__call__` is about nine times slower; a wasmtime-py that moved its C API bindings would fall back to it
    # silently, so this pins that the fast path is in use.
    vm = quickjs_wasi.QuickJS.create(wasm=await loadQuickJSWasm())
    assert isinstance(vm._e.qjs_eval, quickjs_wasi._UncheckedCall)
    assert vm.evalCode("[1, 2, 3].map((x) => x * 2).join()").toString() == "2,4,6"


async def test_a_finished_run_releases_its_vm(sandboxes, monkeypatch):
    # A VM's wasm memory is invisible to the garbage collector, which would leave a large one to wait for a cycle
    # collection; the worker disposes it when it ends.
    disposed = []
    dispose = quickjs_wasi.QuickJS.dispose
    monkeypatch.setattr(quickjs_wasi.QuickJS, "dispose", lambda vm: (disposed.append(vm), dispose(vm))[1])
    exited: list[concurrent.futures.Future[None]] = []
    sandbox = CodemodeSandbox({"onWorkerStarted": exited.append})
    sandboxes.append(sandbox)
    assertMatches(await sandbox.execute("return 1"), {"ok": True, "value": 1})
    # The result no longer waits for the worker (#10748): the VM is disposed as its thread ends, which may be after.
    assert len(exited) == 1
    await asyncio.wait_for(asyncio.wrap_future(exited[0]), 10)
    assert len(disposed) == 1 and disposed[0].disposed


async def test_strings_cross_into_and_out_of_the_vm_intact():
    vm = quickjs_wasi.QuickJS.create(wasm=await loadQuickJSWasm())
    seen = []
    echo_ = vm.newFunction("echo", lambda value: (seen.append(value.toString()), vm.undefined)[1])
    text = "h\u00e9llo \ud800 \U0001f600 \x00 end"
    vm.callFunction(vm.evalCode("(echo, text) => echo(text)"), vm.undefined, echo_, vm.newString(text))
    assert seen == [text]
