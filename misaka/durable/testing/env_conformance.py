"""Runner-independent conformance cases for an ``ExecutionEnv``.

Translated from pi's ``packages/durable/src/testing/env-conformance.ts`` (v1.3.0). Records are plain ``dict`` objects
with the upstream keys; a case is ``{"name", "timeoutMs"?, "run"}``.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import Any

from misaka.chord.context import BACKGROUND_CONTEXT, Context, withAbortSignal
from misaka.utils.js import AbortController, Utf8Decoder

from ..env import getOrThrow
from .types import EnvConformanceCase, EnvConformanceOptions

context = BACKGROUND_CONTEXT

type EnvTest = Callable[[Any], Awaitable[None]]


def decode(data: bytes, ignoreBOM: bool = False) -> str:
    """``new TextDecoder("utf-8", {ignoreBOM}).decode(data)``."""
    return Utf8Decoder(ignoreBOM=ignoreBOM).decode(bytes(data), True)


def encodedLength(text: str) -> int:
    """``new TextEncoder().encode(text).length``."""
    return len(text.encode("utf-8", "surrogatepass"))


def abortedContext() -> Context:
    controller = AbortController()
    controller.abort()
    return withAbortSignal(controller.signal, BACKGROUND_CONTEXT)


def errorCode(result: dict[str, Any]) -> str | None:
    return None if result["ok"] else result["error"].code  # type: ignore[no-any-return]


async def readAll(env: Any, path: str, maxEntries: int) -> dict[str, Any]:
    reader = getOrThrow(await env.openDirReader(path, context))
    pages: list[list[dict[str, Any]]] = []
    try:
        for _ in range(1000):
            next_ = getOrThrow(await reader.next(maxEntries, context))
            pages.append(next_["entries"])
            if next_["done"]:
                return {"pages": pages, "done": True}
        return {"pages": pages, "done": False}
    finally:
        await reader.close(context)


async def listed(env: Any, path: str, options: dict[str, Any] | None = None) -> dict[str, Any]:
    """A listing as ``<kind letter> <path>`` lines (``f``, ``d``, ``l``), with its truncation."""
    if getattr(env, "listFiles", None) is None:
        raise RuntimeError("The environment does not implement listFiles")
    listing = getOrThrow(await env.listFiles(path, options, context))
    letter = {"file": "f", "directory": "d", "symlink": "l"}
    return {
        "entries": [f"{letter[entry['kind']]} {entry['path']}" for entry in listing["entries"]],
        "truncated": listing["truncated"],
    }


def covers(change: dict[str, Any], path: str) -> bool:
    """Whether a change reports ``path``: an overflow, or a reported path at or above it."""
    if "overflow" in change:
        return True
    if "paths" not in change:
        return False
    return any(
        path == reported or path.startswith((f"{reported}/", f"{reported}\\"))
        for reported in change["paths"]
    )


async def watching(env: Any, targets: list[dict[str, Any]], run: Callable[[Any], Awaitable[None]]) -> None:
    """Watch ``targets`` while ``run`` changes files, with a helper that waits for a change reporting a path. ``run``
    receives an object with ``changes``, ``expectChange(path, change)`` (wait up to three seconds for a change, after
    the call, that reports ``path``) and ``absolute(path)``."""
    changes: list[dict[str, Any]] = []
    watcher = getOrThrow(await env.watch(targets, changes.append, context))
    try:

        async def absolute(path: str) -> str:
            return getOrThrow(await env.absolutePath(path, context))  # type: ignore[no-any-return]

        async def expectChange(path: str, change: Callable[[], Awaitable[None]]) -> None:
            target = await absolute(path)
            start = len(changes)
            await change()
            deadline = time.monotonic() + 3
            while not any(covers(entry, target) for entry in changes[start:]):
                if time.monotonic() > deadline:
                    raise RuntimeError(f"No change reported {target}; got {json.dumps(changes[start:])}")
                await asyncio.sleep(0.02)

        await run(SimpleNamespace(changes=changes, expectChange=expectChange, absolute=absolute))
    finally:
        await watcher.close(context)


def createEnvConformance(options: EnvConformanceOptions) -> list[EnvConformanceCase]:
    """Creates runner-independent cases for an ``ExecutionEnv``. ``withEnv`` must call and await its callback exactly
    once per case with an environment whose ``cwd`` is a fresh, empty, writable directory."""
    check = options["assertions"]
    shell: list[str] = options.get("shell") or ["sh", "-c"]
    symlinks: bool = options.get("symlinks") if options.get("symlinks") is not None else True  # type: ignore[assignment]

    def createCase(name: str, test: EnvTest, timeoutMs: int | None = None) -> EnvConformanceCase:
        return {
            "name": name,
            **({} if timeoutMs is None else {"timeoutMs": timeoutMs}),
            "run": lambda: options["withEnv"](test),
        }

    # Watch cases wait up to three seconds per step, longer than test runners allow by default.
    def watchCase(name: str, test: EnvTest) -> EnvConformanceCase:
        return createCase(name, test, 30_000)

    async def execCollect(env: Any, command: str | list[str], cwd: str | None = None) -> dict[str, Any]:
        output = {"stdout": "", "stderr": ""}

        def onOutput(text: str, _context: Any, info: dict[str, Any]) -> None:
            output[info["stream"]] += text

        result = await env.exec(command, {**({} if cwd is None else {"cwd": cwd}), "onOutput": onOutput}, context)
        return {"result": result, **output}

    async def binaryRanges(env: Any) -> None:
        getOrThrow(await env.writeFile("data.txt", "hello world", context))
        reader = getOrThrow(await env.openBinaryReader("data.txt", None, context))
        info = getOrThrow(await reader.info(context))
        check.partialDeepEqual(info, {"name": "data.txt", "kind": "file", "size": 11})
        check.strictEqual(decode(getOrThrow(await reader.read(0, 5, context))), "hello")
        check.strictEqual(decode(getOrThrow(await reader.read(6, 100, context))), "world")
        check.strictEqual(len(getOrThrow(await reader.read(11, 4, context))), 0)
        check.strictEqual(len(getOrThrow(await reader.read(50, 1, context))), 0)
        check.strictEqual(len(getOrThrow(await reader.read(3, 0, context))), 0)
        check.strictEqual(errorCode(await reader.read(-1, 1, context)), "invalid")
        check.strictEqual(errorCode(await reader.read(0, 1.5, context)), "invalid")
        check.strictEqual(errorCode(await reader.read(0, 1, abortedContext())), "aborted")
        await reader.close(context)
        await reader.close(context)
        check.strictEqual(errorCode(await reader.read(0, 1, context)), "invalid")
        check.strictEqual(errorCode(await reader.info(context)), "invalid")

    async def binaryScanLines(env: Any) -> None:
        # A byte-order mark, an invalid sequence before a newline, an empty line, a later U+FEFF, and no final newline.
        data = bytes([0xEF, 0xBB, 0xBF, 0x61, 0x0A, 0xE2, 0x82, 0x0A, 0x0A, 0xEF, 0xBB, 0xBF, 0x62, 0x0A, 0xC3, 0xA9])
        getOrThrow(await env.writeFile("lines.txt", data, context))
        lines = decode(data).split("\n")
        reader = getOrThrow(await env.openBinaryReader("lines.txt", None, context))
        try:
            ranges: list[tuple[int, int | None]] = [(0, None), (0, 1), (1, 3), (2, 3), (3, None), (4, 9)]
            for startLine, endLine in ranges:
                scan = getOrThrow(
                    await reader.scanLines(
                        {"startLine": startLine, **({} if endLine is None else {"endLine": endLine})}, context
                    )
                )
                selected = lines[startLine:endLine]

                def span(start: int, end: int) -> str:
                    return decode(data[start:end], ignoreBOM=start > 0)

                check.strictEqual(scan["newlines"], len(lines) - 1)
                check.strictEqual(span(scan["start"], scan["end"]), "\n".join(selected))
                check.strictEqual(scan["selectedBytes"], encodedLength("\n".join(selected)))
                check.strictEqual(span(scan["start"], scan["firstLineEnd"]), lines[startLine])
                check.strictEqual(scan["firstLineBytes"], encodedLength(lines[startLine]))
            check.partialDeepEqual(
                getOrThrow(await reader.scanLines({"startLine": 9}, context)),
                {"start": len(data), "end": len(data), "selectedBytes": 0},
            )
            check.strictEqual(errorCode(await reader.scanLines({"startLine": 2, "endLine": 2}, context)), "invalid")
        finally:
            await reader.close(context)

    async def binaryAfterRename(env: Any) -> None:
        getOrThrow(await env.writeFile("a.txt", "one", context))
        reader = getOrThrow(await env.openBinaryReader("a.txt", None, context))
        try:
            getOrThrow(await env.renameFile("a.txt", "b.txt", context))
            getOrThrow(await env.writeFile("a.txt", "two", context))
            check.strictEqual(decode(getOrThrow(await reader.read(0, 10, context))), "one")
        finally:
            await reader.close(context)

    async def binaryRefuses(env: Any) -> None:
        getOrThrow(await env.createDir("dir", None, context))
        getOrThrow(await env.writeFile("file.txt", "x", context))
        check.strictEqual(errorCode(await env.openBinaryReader("dir", None, context)), "is_directory")
        check.strictEqual(errorCode(await env.openBinaryReader("missing.txt", None, context)), "not_found")
        check.strictEqual(errorCode(await env.openBinaryReader("file.txt", None, abortedContext())), "aborted")

    async def dirPages(env: Any) -> None:
        names = ["a.txt", "b.txt", "c.txt", "d.txt", "e.txt"]
        for name in names:
            getOrThrow(await env.writeFile(name, name, context))
        getOrThrow(await env.createDir("sub", None, context))
        read = await readAll(env, ".", 2)
        check.ok(read["done"], "directory reader reached the end")
        for page in read["pages"]:
            check.ok(len(page) <= 2, "page within maxEntries")
        entries = [entry for page in read["pages"] for entry in page]
        check.deepEqual(sorted(entry["name"] for entry in entries), sorted([*names, "sub"]))
        check.strictEqual(next((entry for entry in entries if entry["name"] == "sub"), {}).get("kind"), "directory")
        check.partialDeepEqual(
            next((entry for entry in entries if entry["name"] == "a.txt"), None), {"kind": "file", "size": 5}
        )

    async def dirEnd(env: Any) -> None:
        getOrThrow(await env.createDir("empty", None, context))
        reader = getOrThrow(await env.openDirReader("empty", context))
        check.deepEqual(getOrThrow(await reader.next(10, context)), {"entries": [], "done": True})
        check.deepEqual(getOrThrow(await reader.next(10, context)), {"entries": [], "done": True})
        check.strictEqual(errorCode(await reader.next(0, context)), "invalid")
        check.strictEqual(errorCode(await reader.next(1, abortedContext())), "aborted")
        await reader.close(context)
        await reader.close(context)
        check.strictEqual(errorCode(await reader.next(1, context)), "invalid")

    async def dirRefuses(env: Any) -> None:
        getOrThrow(await env.writeFile("file.txt", "x", context))
        check.strictEqual(errorCode(await env.openDirReader("missing", context)), "not_found")
        file = await env.openDirReader("file.txt", context)
        check.strictEqual(errorCode(file), "not_directory")
        check.strictEqual(errorCode(await env.openDirReader(".", abortedContext())), "aborted")

    async def dirRemoved(env: Any) -> None:
        getOrThrow(await env.createDir("dir", None, context))
        for name in ["x", "y", "z"]:
            getOrThrow(await env.writeFile(f"dir/{name}", name, context))
        reader = getOrThrow(await env.openDirReader("dir", context))
        try:
            for name in ["x", "y", "z"]:
                getOrThrow(await env.remove(f"dir/{name}", None, context))
            entries: list[dict[str, Any]] = []
            for _ in range(10):
                next_ = getOrThrow(await reader.next(10, context))
                entries.extend(next_["entries"])
                if next_["done"]:
                    break
            check.deepEqual(entries, [])
        finally:
            await reader.close(context)

    def write(env: Any, path: str, content: str) -> Callable[[], Awaitable[None]]:
        async def change() -> None:
            getOrThrow(await env.writeFile(path, content, context))

        return change

    async def watchMissingFile(env: Any) -> None:
        async def run(helpers: Any) -> None:
            await helpers.expectChange("AGENTS.md", write(env, "AGENTS.md", "one"))
            await helpers.expectChange("AGENTS.md", write(env, "AGENTS.md", "two!"))

            # Editors replace a file by renaming a new one over it.
            async def replace() -> None:
                getOrThrow(await env.writeFile("AGENTS.md.tmp", "three", context))
                getOrThrow(await env.renameFile("AGENTS.md.tmp", "AGENTS.md", context))

            await helpers.expectChange("AGENTS.md", replace)
            await helpers.expectChange("AGENTS.md", write(env, "AGENTS.md", "four"))

            async def remove() -> None:
                getOrThrow(await env.remove("AGENTS.md", None, context))

            await helpers.expectChange("AGENTS.md", remove)

        await watching(env, [{"path": "AGENTS.md"}], run)

    async def watchAncestors(env: Any) -> None:
        async def run(helpers: Any) -> None:
            await helpers.expectChange("a/b/c/AGENTS.md", write(env, "a/b/c/AGENTS.md", "x"))

        await watching(env, [{"path": "a/b/c/AGENTS.md"}], run)

    async def watchCreatedDirectories(env: Any) -> None:
        getOrThrow(await env.createDir("skills", None, context))

        async def run(helpers: Any) -> None:
            # Written before any watcher on the new directories can exist.
            await helpers.expectChange("skills/a/b/SKILL.md", write(env, "skills/a/b/SKILL.md", "one"))
            await helpers.expectChange("skills/a/b/SKILL.md", write(env, "skills/a/b/SKILL.md", "two!"))
            await helpers.expectChange("skills/a/b/c/SKILL.md", write(env, "skills/a/b/c/SKILL.md", "deeper"))

        await watching(env, [{"path": "skills", "recursive": True}], run)

    async def watchRenamedParent(env: Any) -> None:
        getOrThrow(await env.writeFile("proj/.pi/skills/x.md", "x", context))

        async def run(helpers: Any) -> None:
            async def rename() -> None:
                getOrThrow(await env.renameFile("proj/.pi", "proj/old", context))

            await helpers.expectChange("proj/.pi/skills", rename)
            await helpers.expectChange("proj/.pi/skills/y.md", write(env, "proj/.pi/skills/y.md", "y"))
            await helpers.expectChange("proj/.pi/skills/y.md", write(env, "proj/.pi/skills/y.md", "yy"))

        await watching(env, [{"path": "proj/.pi/skills", "recursive": True}], run)

    async def watchExcluded(env: Any) -> None:
        getOrThrow(await env.createDir("skills", None, context))
        targets = [{"path": "skills", "recursive": True, "exclude": {"hidden": True, "names": ["node_modules"]}}]

        async def run(helpers: Any) -> None:
            getOrThrow(await env.writeFile("skills/node_modules/dep/SKILL.md", "dep", context))
            getOrThrow(await env.writeFile("skills/.SKILL.md.tmp", "draft", context))

            async def rename() -> None:
                getOrThrow(await env.renameFile("skills/.SKILL.md.tmp", "skills/SKILL.md", context))

            await helpers.expectChange("skills/SKILL.md", rename)
            hidden = [await helpers.absolute("skills/node_modules"), await helpers.absolute("skills/.SKILL.md.tmp")]
            for change in helpers.changes:
                if "paths" not in change:
                    continue
                for path in change["paths"]:
                    check.ok(
                        not any(path == excluded or path.startswith(excluded) for excluded in hidden),
                        f"excluded {path}",
                    )

        await watching(env, targets, run)

    async def watchOverlap(env: Any) -> None:
        getOrThrow(await env.writeFile("skills/a/one.md", "one", context))
        targets = [{"path": "skills"}, {"path": "skills", "recursive": True}]

        async def run(helpers: Any) -> None:
            await helpers.expectChange("skills/a/two.md", write(env, "skills/a/two.md", "two"))

        await watching(env, targets, run)

    async def watchReplacedDirectory(env: Any) -> None:
        getOrThrow(await env.writeFile("skills/a/x.md", "x", context))

        async def run(helpers: Any) -> None:
            async def replace() -> None:
                getOrThrow(await env.renameFile("skills/a", "skills-old", context))
                getOrThrow(await env.createDir("skills/a", None, context))

            await helpers.expectChange("skills/a", replace)
            await helpers.expectChange("skills/a/y.md", write(env, "skills/a/y.md", "y"))
            await helpers.expectChange("skills/a/y.md", write(env, "skills/a/y.md", "yy"))

        await watching(env, [{"path": "skills", "recursive": True}], run)

    async def watchClosed(env: Any) -> None:
        changes: list[dict[str, Any]] = []
        watcher = getOrThrow(await env.watch([{"path": "file.txt"}], changes.append, context))
        check.ok(watcher.mode in ("native", "polling"), "watcher reports its mode")
        await watcher.close(context)
        await watcher.close(context)
        getOrThrow(await env.writeFile("file.txt", "x", context))
        await asyncio.sleep(0.3)
        check.deepEqual(changes, [])

    async def argvNoShell(env: Any) -> None:
        hostile = "it's $(touch pwned) `touch pwned` *; touch pwned"
        collected = await execCollect(env, [*shell, 'printf "%s|%s" "$1" "$2"', "argv0", hostile, "a b"])
        check.strictEqual(getOrThrow(collected["result"])["exitCode"], 0)
        check.strictEqual(collected["stdout"], f"{hostile}|a b")
        check.strictEqual(getOrThrow(await env.exists("pwned", context)), False)

    async def bothStreams(env: Any) -> None:
        script = "printf out; printf err >&2; printf more"
        argv = await execCollect(env, [*shell, script])
        check.strictEqual(getOrThrow(argv["result"])["exitCode"], 0)
        check.strictEqual(argv["stdout"], "outmore")
        check.strictEqual(argv["stderr"], "err")
        string = await execCollect(env, script)
        check.strictEqual(getOrThrow(string["result"])["exitCode"], 0)
        check.strictEqual(string["stdout"], "outmore")
        check.strictEqual(string["stderr"], "err")

    async def argvCwd(env: Any) -> None:
        getOrThrow(await env.createDir("sub", None, context))
        made = await execCollect(env, [*shell, "printf x > made.txt; exit 3"], "sub")
        check.strictEqual(getOrThrow(made["result"])["exitCode"], 3)
        check.strictEqual(getOrThrow(await env.readTextFile("sub/made.txt", context)), "x")

    async def argvMissing(env: Any) -> None:
        check.strictEqual(
            errorCode(await env.exec(["pi-durable-conformance-missing-program"], None, context)), "spawn_error"
        )
        check.strictEqual(errorCode(await env.exec([], None, context)), "spawn_error")

    async def windowed(env: Any) -> None:
        lines = 2000
        window = {"maxBytes": 200, "maxLines": 5, "minIntervalMs": 0, "bytesPerSecond": 1_000_000_000}
        state = {"bytes": 0, "newlines": 0, "tail": ""}

        def onOutput(text: str, _context: Any, info: dict[str, Any]) -> None:
            skipped = info.get("skipped")
            if skipped is not None:
                state["bytes"] += skipped["bytes"]
                state["newlines"] += skipped["newlines"]
                after = encodedLength(text)
                afterNewlines = text.count("\n")
                check.ok(
                    after > window["maxBytes"] or afterNewlines > window["maxLines"],
                    "a skip is followed by more than the window",
                )
                state["tail"] = ""
            state["bytes"] += encodedLength(text)
            state["newlines"] += text.count("\n")
            state["tail"] += text

        result = await env.exec(
            [*shell, f"i=0; while [ $i -lt {lines} ]; do echo line-$i; i=$((i+1)); done"],
            {"window": window, "onOutput": onOutput},
            context,
        )
        check.strictEqual(getOrThrow(result)["exitCode"], 0)
        expected = [f"line-{index}\n" for index in range(lines)]
        check.strictEqual(state["bytes"], len("".join(expected)))
        check.strictEqual(state["newlines"], lines)
        check.ok(
            state["tail"].endswith("".join(expected[-window["maxLines"] :])), "the delivered output ends with the tail"
        )

    async def timeoutVersusAbort(env: Any) -> None:
        timedOut = await env.exec([*shell, "sleep 2"], {"timeout": 0.1}, context)
        check.strictEqual(errorCode(timedOut), "timeout")
        controller = AbortController()
        running = asyncio.ensure_future(env.exec([*shell, "sleep 2"], None, withAbortSignal(controller.signal, context)))
        asyncio.get_running_loop().call_later(0.1, controller.abort)
        check.strictEqual(errorCode(await running), "aborted")

    cases: list[EnvConformanceCase] = [
        createCase("binary reader reads byte ranges of the opened file", binaryRanges),
        createCase("binary reader scans lines like decoding the whole file", binaryScanLines),
        createCase("binary reader keeps reading the file it opened after a rename", binaryAfterRename),
        createCase("binary reader refuses directories, missing files and aborted opens", binaryRefuses),
        createCase("directory reader pages every entry exactly once", dirPages),
        createCase("directory reader reports the end and refuses use after close", dirEnd),
        createCase("directory reader refuses missing paths and files", dirRefuses),
        createCase("directory reader skips entries removed during enumeration", dirRemoved),
        watchCase("watch reports a missing file's creation, changes, replacement and removal", watchMissingFile),
        watchCase("watch reports a missing target whose ancestors are created", watchAncestors),
        watchCase("watch follows directories created together with their contents", watchCreatedDirectories),
        watchCase("watch keeps watching a path whose parent is renamed and recreated", watchRenamedParent),
        watchCase("watch skips excluded entries and reports a rename out of them", watchExcluded),
        watchCase("watch keeps recursive coverage where a non-recursive target overlaps", watchOverlap),
        watchCase("watch follows a directory replaced at the same path", watchReplacedDirectory),
        watchCase("watch stops reporting once closed", watchClosed),
        createCase("argv exec passes arguments to the program without shell parsing", argvNoShell),
        createCase("exec reports the stream of every chunk in both forms", bothStreams),
        createCase("argv exec honors cwd and exit codes", argvCwd),
        createCase("argv exec reports missing programs and empty argv as spawn errors", argvMissing),
        createCase("windowed exec keeps the exact tail and counts what it skips", windowed),
        createCase("argv exec distinguishes timeout from abort", timeoutVersusAbort),
    ]

    if options.get("listFiles"):

        async def writeAll(env: Any, files: list[str]) -> None:
            for file in files:
                getOrThrow(await env.writeFile(file, "", context))

        async def listOrder(env: Any) -> None:
            await writeAll(env, ["b.txt", "a/z.txt", "a/b/c.txt", ".hidden/x", "A.txt"])
            getOrThrow(await env.createDir("empty", None, context))
            check.deepEqual(
                await listed(env, "."),
                {
                    "entries": [
                        "d .hidden",
                        "f .hidden/x",
                        "f A.txt",
                        "d a",
                        "d a/b",
                        "f a/b/c.txt",
                        "f a/z.txt",
                        "f b.txt",
                        "d empty",
                    ],
                    "truncated": False,
                },
            )
            check.deepEqual(
                (await listed(env, ".", {"hidden": False}))["entries"],
                ["f A.txt", "d a", "d a/b", "f a/b/c.txt", "f a/z.txt", "f b.txt", "d empty"],
            )
            expected = {"entries": ["d b", "f b/c.txt", "f z.txt"], "truncated": False}
            check.deepEqual(await listed(env, "a"), expected)
            check.deepEqual(await listed(env, getOrThrow(await env.absolutePath("a", context))), expected)
            check.deepEqual(await listed(env, "empty"), {"entries": [], "truncated": False})

        async def listIgnored(env: Any) -> None:
            await writeAll(env, ["keep.txt", "out/x.js", "sub/keep.txt", "sub/drop.log", "node_modules/m/i.js"])
            getOrThrow(await env.writeFile(".gitignore", "out/\n*.log\n", context))
            getOrThrow(await env.writeFile("sub/.gitignore", "!keep.txt\n", context))
            # Not a work tree yet: `.gitignore` files have no effect.
            check.ok("f out/x.js" in (await listed(env, "."))["entries"])
            getOrThrow(await env.writeFile(".git/HEAD", "ref: refs/heads/main\n", context))
            getOrThrow(await env.writeFile(".git/info/exclude", "keep.txt\n", context))
            check.deepEqual(
                (await listed(env, ".", {"exclude": ["node_modules"]}))["entries"],
                ["f .gitignore", "d sub", "f sub/.gitignore", "f sub/keep.txt"],
            )
            # A directory inside the work tree applies its parents' rules.
            check.deepEqual((await listed(env, "sub"))["entries"], ["f .gitignore", "f keep.txt"])
            every = (await listed(env, ".", {"gitignore": False}))["entries"]
            check.ok("f out/x.js" in every and "f node_modules/m/i.js" in every and "f keep.txt" in every)
            check.ok(not any(".git/" in entry or entry == "d .git" for entry in every))

        async def listBounds(env: Any) -> None:
            await writeAll(env, ["a", "b", "c"])
            check.deepEqual(await listed(env, ".", {"maxEntries": 2}), {"entries": ["f a", "f b"], "truncated": True})
            check.deepEqual(
                await listed(env, ".", {"maxEntries": 3}), {"entries": ["f a", "f b", "f c"], "truncated": False}
            )
            # Each entry counts its path plus two bytes.
            check.deepEqual(await listed(env, ".", {"maxBytes": 6}), {"entries": ["f a", "f b"], "truncated": True})
            check.deepEqual(await listed(env, ".", {"maxEntries": 0}), {"entries": [], "truncated": True})
            check.strictEqual(errorCode(await env.listFiles(".", {"maxEntries": -1}, context)), "invalid")
            check.strictEqual(errorCode(await env.listFiles(".", {"timeoutMs": 1.5}, context)), "invalid")

        async def listFailures(env: Any) -> None:
            getOrThrow(await env.writeFile("file.txt", "", context))
            check.strictEqual(errorCode(await env.listFiles("missing", None, context)), "not_found")
            check.strictEqual(errorCode(await env.listFiles("file.txt", None, context)), "not_directory")
            check.strictEqual(errorCode(await env.listFiles(".", None, abortedContext())), "aborted")

        cases.extend(
            [
                createCase("listFiles lists files and directories depth first in name order", listOrder),
                createCase("listFiles skips what git ignores in work trees, .git, and excluded names", listIgnored),
                createCase("listFiles stops at its bounds and reports truncated", listBounds),
                createCase("listFiles fails on missing directories, files, and aborts", listFailures),
            ]
        )

    if symlinks and options.get("listFiles"):

        async def listSymlinks(env: Any) -> None:
            getOrThrow(await env.writeFile("dir/inner.txt", "", context))
            linked = await env.exec(
                [*shell, "ln -s dir link && ln -s missing dangling && ln -s .. dir/loop"], None, context
            )
            check.strictEqual(getOrThrow(linked)["exitCode"], 0)
            check.deepEqual(
                (await listed(env, "."))["entries"], ["l dangling", "d dir", "f dir/inner.txt", "l dir/loop", "l link"]
            )
            # The listed directory itself may be a link.
            check.deepEqual((await listed(env, "link"))["entries"], ["f inner.txt", "l loop"])

        cases.append(createCase("listFiles lists symbolic links without following them", listSymlinks))

    if symlinks:

        async def binarySymlinks(env: Any) -> None:
            getOrThrow(await env.writeFile("target.txt", "target", context))
            getOrThrow(await env.createDir("sub", None, context))
            getOrThrow(await env.writeFile("sub/inner.txt", "inner", context))
            linked = await env.exec([*shell, "ln -s target.txt link.txt && ln -s sub dirlink"], None, context)
            check.strictEqual(getOrThrow(linked)["exitCode"], 0)

            followed = getOrThrow(await env.openBinaryReader("link.txt", None, context))
            check.strictEqual(decode(getOrThrow(await followed.read(0, 10, context))), "target")
            await followed.close(context)

            check.strictEqual(errorCode(await env.openBinaryReader("link.txt", {"noFollow": True}, context)), "invalid")

            # Only the final component is refused; earlier symlinked directories still resolve.
            inner = getOrThrow(await env.openBinaryReader("dirlink/inner.txt", {"noFollow": True}, context))
            check.strictEqual(decode(getOrThrow(await inner.read(0, 10, context))), "inner")
            await inner.close(context)

        async def watchSymlink(env: Any) -> None:
            getOrThrow(await env.writeFile("data/real.md", "one", context))
            getOrThrow(await env.createDir("config", None, context))
            linked = await env.exec([*shell, "ln -s ../data/real.md config/AGENTS.md"], None, context)
            check.strictEqual(getOrThrow(linked)["exitCode"], 0)

            async def run(helpers: Any) -> None:
                await helpers.expectChange("config/AGENTS.md", write(env, "data/real.md", "two!"))

            await watching(env, [{"path": "config/AGENTS.md"}], run)

        cases.extend(
            [
                createCase("binary reader follows symlinks unless noFollow refuses the final one", binarySymlinks),
                watchCase("watch reports changes to the file a watched symbolic link points to", watchSymlink),
            ]
        )

    return cases


__all__ = ["createEnvConformance"]
