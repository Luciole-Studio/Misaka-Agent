"""Translated from pi's ``packages/coding-agent/test/extensions-discovery.test.ts`` (v1.1.0).

pi discovers TypeScript/JavaScript extensions (``*.ts``, ``*.js``, ``index.ts``); MISAKA discovers Python ones
(``*.py``, ``index.py``), so the cases below use those names. MISAKA fork: not translated -- the ``.js`` file and
``index.js`` cases (including ``index.ts`` over ``index.js``), the jiti import cases (the coding-agent entrypoint,
the pi-ai OAuth barrel, package ownership from ancestor ``node_modules`` manifests, #9863) and the extension with its
own ``node_modules`` dependencies (``examples/extensions/with-deps``).
"""

from __future__ import annotations

import json
import os
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

from misaka.core.extensions.loader import discoverAndLoadExtensions, loadExtensions

extensionCode = """
async def _handler(*_args):
    pass


def default(pi):
    pi.registerCommand("test", {"handler": _handler})
"""


def extensionCodeWithTool(toolName: str) -> str:
    return textwrap.dedent(f"""
        from misaka.agent.types import AgentToolResult
        from misaka.ai.types import TextContent
        from misaka.core.extensions.types import ToolDefinition


        async def _ok(*_args, **_kwargs):
            return AgentToolResult(content=[TextContent(text="ok")])


        def default(pi):
            pi.registerTool(ToolDefinition(
                name="{toolName}",
                label="{toolName}",
                description="Test tool",
                parameters={{"type": "object", "properties": {{}}}},
                execute=_ok,
            ))
    """)


@pytest.fixture
def dirs(tmp_path: Path) -> SimpleNamespace:
    extensionsDir = tmp_path / "extensions"
    extensionsDir.mkdir()
    return SimpleNamespace(tempDir=str(tmp_path), extensionsDir=extensionsDir)


async def discover(dirs: SimpleNamespace, configured: list[str] | None = None):
    return await discoverAndLoadExtensions(configured or [], dirs.tempDir, dirs.tempDir)


def write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


async def test_discovers_direct_py_files_in_extensions(dirs: SimpleNamespace):
    write(dirs.extensionsDir / "foo.py", extensionCode)
    write(dirs.extensionsDir / "bar.py", extensionCode)

    result = await discover(dirs)

    assert result.errors == []
    assert sorted(os.path.basename(extension.path) for extension in result.extensions) == ["bar.py", "foo.py"]


async def test_discovers_subdirectory_with_index_py(dirs: SimpleNamespace):
    write(dirs.extensionsDir / "my-extension" / "index.py", extensionCode)

    result = await discover(dirs)

    assert result.errors == []
    assert len(result.extensions) == 1
    assert "my-extension" in result.extensions[0].path
    assert "index.py" in result.extensions[0].path


async def test_discovers_subdirectory_with_package_json_pi_field(dirs: SimpleNamespace):
    subdir = dirs.extensionsDir / "my-package"
    write(subdir / "src" / "main.py", extensionCode)
    write(subdir / "package.json", json.dumps({"name": "my-package", "pi": {"extensions": ["./src/main.py"]}}))

    result = await discover(dirs)

    assert result.errors == []
    assert len(result.extensions) == 1
    assert "src" in result.extensions[0].path
    assert "main.py" in result.extensions[0].path


async def test_keeps_package_json_pi_extension_entries_with_leading_tilde_package_relative(dirs: SimpleNamespace):
    subdir = dirs.extensionsDir / "tilde-package"
    directExtensionPath = write(subdir / "~entry.py", extensionCode)
    slashExtensionPath = write(subdir / "~" / "entry.py", extensionCode)
    write(subdir / "package.json", json.dumps({"name": "tilde-package", "pi": {"extensions": ["~entry.py", "~/entry.py"]}}))

    result = await discover(dirs)

    assert result.errors == []
    assert sorted(os.path.normcase(extension.path) for extension in result.extensions) == sorted(
        os.path.normcase(str(path)) for path in (directExtensionPath, slashExtensionPath)
    )


async def test_package_json_can_declare_multiple_extensions(dirs: SimpleNamespace):
    subdir = dirs.extensionsDir / "my-package"
    write(subdir / "ext1.py", extensionCode)
    write(subdir / "ext2.py", extensionCode)
    write(subdir / "package.json", json.dumps({"name": "my-package", "pi": {"extensions": ["./ext1.py", "./ext2.py"]}}))

    result = await discover(dirs)

    assert result.errors == []
    assert len(result.extensions) == 2


async def test_package_json_with_pi_field_takes_precedence_over_index_py(dirs: SimpleNamespace):
    subdir = dirs.extensionsDir / "my-package"
    write(subdir / "index.py", extensionCodeWithTool("from-index"))
    write(subdir / "custom.py", extensionCodeWithTool("from-custom"))
    write(subdir / "package.json", json.dumps({"name": "my-package", "pi": {"extensions": ["./custom.py"]}}))

    result = await discover(dirs)

    assert result.errors == []
    assert len(result.extensions) == 1
    assert "custom.py" in result.extensions[0].path
    # Verify the right tool was registered
    assert "from-custom" in result.extensions[0].tools
    assert "from-index" not in result.extensions[0].tools


async def test_ignores_package_json_without_pi_field_falls_back_to_index_py(dirs: SimpleNamespace):
    subdir = dirs.extensionsDir / "my-package"
    write(subdir / "index.py", extensionCode)
    write(subdir / "package.json", json.dumps({"name": "my-package", "version": "1.0.0"}))

    result = await discover(dirs)

    assert result.errors == []
    assert len(result.extensions) == 1
    assert "index.py" in result.extensions[0].path


async def test_ignores_subdirectory_without_index_or_package_json(dirs: SimpleNamespace):
    subdir = dirs.extensionsDir / "not-an-extension"
    write(subdir / "helper.py", extensionCode)
    write(subdir / "utils.py", extensionCode)

    result = await discover(dirs)

    assert result.errors == []
    assert result.extensions == []


async def test_does_not_recurse_beyond_one_level(dirs: SimpleNamespace):
    # No index.py or package.json in container/
    write(dirs.extensionsDir / "container" / "nested" / "index.py", extensionCode)

    result = await discover(dirs)

    assert result.errors == []
    assert result.extensions == []


async def test_handles_mixed_direct_files_and_subdirectories(dirs: SimpleNamespace):
    # Direct file
    write(dirs.extensionsDir / "direct.py", extensionCode)
    # Subdirectory with index
    write(dirs.extensionsDir / "with-index" / "index.py", extensionCode)
    # Subdirectory with package.json
    write(dirs.extensionsDir / "with-manifest" / "entry.py", extensionCode)
    write(dirs.extensionsDir / "with-manifest" / "package.json", json.dumps({"pi": {"extensions": ["./entry.py"]}}))

    result = await discover(dirs)

    assert result.errors == []
    assert len(result.extensions) == 3


async def test_skips_non_existent_paths_declared_in_package_json(dirs: SimpleNamespace):
    subdir = dirs.extensionsDir / "my-package"
    write(subdir / "exists.py", extensionCode)
    write(subdir / "package.json", json.dumps({"pi": {"extensions": ["./exists.py", "./missing.py"]}}))

    result = await discover(dirs)

    assert result.errors == []
    assert len(result.extensions) == 1
    assert "exists.py" in result.extensions[0].path


async def test_loads_extensions_and_registers_commands(dirs: SimpleNamespace):
    write(dirs.extensionsDir / "with-command.py", extensionCode)

    result = await discover(dirs)

    assert result.errors == []
    assert len(result.extensions) == 1
    assert "test" in result.extensions[0].commands


async def test_loads_extensions_and_registers_tools(dirs: SimpleNamespace):
    write(dirs.extensionsDir / "with-tool.py", extensionCodeWithTool("my-tool"))

    result = await discover(dirs)

    assert result.errors == []
    assert len(result.extensions) == 1
    assert "my-tool" in result.extensions[0].tools


async def test_reports_errors_for_invalid_extension_code(dirs: SimpleNamespace):
    write(dirs.extensionsDir / "invalid.py", "this is not valid python export")

    result = await discover(dirs)

    assert len(result.errors) == 1
    assert "invalid.py" in result.errors[0]["path"]
    assert result.extensions == []


async def test_handles_explicitly_configured_paths(dirs: SimpleNamespace, tmp_path: Path):
    customPath = write(tmp_path / "custom-location" / "my-ext.py", extensionCode)

    result = await discover(dirs, [str(customPath)])

    assert result.errors == []
    assert len(result.extensions) == 1
    assert "my-ext.py" in result.extensions[0].path


async def test_registers_message_and_entry_renderers(dirs: SimpleNamespace):
    write(
        dirs.extensionsDir / "with-renderer.py",
        textwrap.dedent("""
            def default(pi):
                pi.registerMarkdownTransformer(lambda markdown, *_: markdown)
                # None uses the default rendering
                pi.registerMessageRenderer("my-custom-type", lambda message, options, theme: None)
                pi.registerEntryRenderer("my-entry-type", lambda entry, options, theme: None)
        """),
    )

    result = await discover(dirs)

    assert result.errors == []
    assert len(result.extensions) == 1
    extension = result.extensions[0]
    assert extension.markdownTransformer is not None
    assert "my-custom-type" in extension.messageRenderers
    assert "my-entry-type" in extension.entryRenderers


async def test_reports_error_when_extension_throws_during_initialization(dirs: SimpleNamespace):
    write(dirs.extensionsDir / "throws.py", 'def default(pi):\n    raise RuntimeError("Initialization failed!")\n')

    result = await discover(dirs)

    assert len(result.errors) == 1
    assert "Initialization failed!" in result.errors[0]["error"]
    assert result.extensions == []


async def test_reports_error_when_extension_has_no_default_export(dirs: SimpleNamespace):
    write(
        dirs.extensionsDir / "no-default.py",
        textwrap.dedent("""
            async def _handler(*_args):
                pass


            def notDefault(pi):
                pi.registerCommand("test", {"handler": _handler})
        """),
    )

    result = await discover(dirs)

    assert len(result.errors) == 1
    assert "does not export a valid factory function" in result.errors[0]["error"]
    assert result.extensions == []


async def test_allows_multiple_extensions_to_register_different_tools(dirs: SimpleNamespace):
    write(dirs.extensionsDir / "tool-a.py", extensionCodeWithTool("tool-a"))
    write(dirs.extensionsDir / "tool-b.py", extensionCodeWithTool("tool-b"))

    result = await discover(dirs)

    assert result.errors == []
    assert len(result.extensions) == 2

    allTools = {name for extension in result.extensions for name in extension.tools}
    assert "tool-a" in allTools
    assert "tool-b" in allTools


async def test_loads_extension_with_event_handlers(dirs: SimpleNamespace):
    write(
        dirs.extensionsDir / "with-handlers.py",
        textwrap.dedent("""
            async def _nothing(*_args):
                return None


            def default(pi):
                pi.on("agent_start", _nothing)
                pi.on("tool_call", _nothing)
                pi.on("agent_end", _nothing)
        """),
    )

    result = await discover(dirs)

    assert result.errors == []
    assert len(result.extensions) == 1
    for event in ("agent_start", "tool_call", "agent_end"):
        assert event in result.extensions[0].handlers


async def test_loads_extension_with_shortcuts(dirs: SimpleNamespace):
    write(
        dirs.extensionsDir / "with-shortcut.py",
        textwrap.dedent("""
            async def _handler(ctx):
                pass


            def default(pi):
                pi.registerShortcut("ctrl+t", {"description": "Test shortcut", "handler": _handler})
        """),
    )

    result = await discover(dirs)

    assert result.errors == []
    assert len(result.extensions) == 1
    assert "ctrl+t" in result.extensions[0].shortcuts


async def test_loads_extension_with_flags(dirs: SimpleNamespace):
    write(
        dirs.extensionsDir / "with-flag.py",
        textwrap.dedent("""
            async def _handler(value):
                pass


            def default(pi):
                pi.registerFlag("my-flag", {"description": "My custom flag", "handler": _handler})
        """),
    )

    result = await discover(dirs)

    assert result.errors == []
    assert len(result.extensions) == 1
    assert "my-flag" in result.extensions[0].flags


async def test_load_extensions_only_loads_explicit_paths_without_discovery(dirs: SimpleNamespace, tmp_path: Path):
    # Create discoverable extensions (would be found by discoverAndLoadExtensions)
    write(dirs.extensionsDir / "discovered.py", extensionCodeWithTool("discovered"))

    # Create explicit extension outside discovery path
    explicitPath = write(tmp_path / "explicit.py", extensionCodeWithTool("explicit"))

    # Use loadExtensions directly to skip discovery
    result = await loadExtensions([str(explicitPath)], dirs.tempDir)

    assert result.errors == []
    assert len(result.extensions) == 1
    assert "explicit" in result.extensions[0].tools
    assert "discovered" not in result.extensions[0].tools


async def test_load_extensions_with_no_paths_loads_nothing(dirs: SimpleNamespace):
    # Create discoverable extensions (would be found by discoverAndLoadExtensions)
    write(dirs.extensionsDir / "discovered.py", extensionCode)

    # Use loadExtensions directly with empty paths
    result = await loadExtensions([], dirs.tempDir)

    assert result.errors == []
    assert result.extensions == []
