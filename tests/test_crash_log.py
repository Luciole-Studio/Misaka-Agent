"""Translated from pi's ``coding-agent/test/crash-log.test.ts`` (v1.1.0).

MISAKA fork: stacks are Python tracebacks (``File "<path>", line N, in <name>`` frames) and extensions are Python
files, so the fixtures use those forms; pi's last case (a URI-encoded frame path) has no Python counterpart, since
traceback paths are never encoded.
"""

from __future__ import annotations

from types import SimpleNamespace

from misaka.core.crash_log import findExtensionStackMatches


def packageExtension(source: str, baseDir: str, entry: str = "extensions/index.py"):
    normalizedBaseDir = baseDir.replace("\\", "/")
    resolvedPath = f"{normalizedBaseDir}/{entry}"
    return SimpleNamespace(
        path=resolvedPath,
        resolvedPath=resolvedPath,
        sourceInfo=SimpleNamespace(path=resolvedPath, source=source, scope="user", origin="package", baseDir=baseDir),
    )


def test_matches_stack_frames_beneath_loaded_package_roots():
    memory = packageExtension("npm:pi-observational-memory", "/home/fedora/.misaka/npm/pi-observational-memory")
    unrelated = packageExtension("npm:unrelated", "/home/fedora/.misaka/npm/unrelated")
    stack = (
        "Traceback (most recent call last):\n"
        '  File "/usr/lib/python3/site-packages/misaka/ai/stream.py", line 1093, in streamSimple\n'
        "    raise TypeError\n"
        '  File "/home/fedora/.misaka/npm/pi-observational-memory/src/agents/worker_stream.py", line 43, in run\n'
        "TypeError: Cannot read 'runtime'"
    )

    assert findExtensionStackMatches(stack, [memory, unrelated]) == ["npm:pi-observational-memory"]


def test_normalizes_windows_paths_and_deduplicates_package_extensions():
    root = "C:\\Users\\reporter\\.misaka\\npm\\@scope\\memory"
    first = packageExtension("npm:@scope/memory", root, "extensions/first.py")
    second = packageExtension("npm:@scope/memory", root, "extensions/second.py")
    stack = (
        "Traceback (most recent call last):\n"
        '  File "c:\\users\\reporter\\.misaka\\npm\\@scope\\memory\\src\\worker.py", line 4, in run\n'
        "RuntimeError: broken"
    )

    assert findExtensionStackMatches(stack, [first, second]) == ["npm:@scope/memory"]


def test_does_not_attribute_sibling_single_file_packages():
    def extension(name: str):
        return SimpleNamespace(
            path=f"/plugins/{name}.py",
            resolvedPath=f"/plugins/{name}.py",
            sourceInfo=SimpleNamespace(
                path=f"/plugins/{name}.py", source=f"/plugins/{name}.py", scope="user", origin="package", baseDir="/plugins"
            ),
        )

    stack = 'Traceback (most recent call last):\n  File "/plugins/b.py", line 4, in run\nRuntimeError: broken'

    assert findExtensionStackMatches(stack, [extension("a"), extension("b")]) == ["/plugins/b.py"]


def test_ignores_extension_paths_in_the_error_message():
    extension = packageExtension("npm:memory", "/tmp/npm/memory")
    stack = (
        f"Traceback (most recent call last):\n"
        f'  File "/opt/misaka/core.py", line 4, in run\n'
        f"OSError: Failed to read {extension.resolvedPath}"
    )

    assert findExtensionStackMatches(stack, [extension]) == []


def test_a_directory_extension_matches_any_file_in_its_directory():
    extension = SimpleNamespace(
        path="/Users/reporter/.misaka/extensions/local memory/index.py",
        resolvedPath="/Users/reporter/.misaka/extensions/local memory/index.py",
        sourceInfo=SimpleNamespace(
            path="/Users/reporter/.misaka/extensions/local memory/index.py",
            source="local",
            scope="user",
            origin="top-level",
            baseDir="/Users/reporter/.misaka/extensions",
        ),
    )
    stack = (
        "Traceback (most recent call last):\n"
        '  File "/Users/reporter/.misaka/extensions/local memory/worker.py", line 4, in run\n'
        "RuntimeError: progress 100%"
    )

    assert findExtensionStackMatches(stack, [extension]) == [extension.path]
