"""The built-in and replaceable extension cases of pi coding-agent's ``resource-loader.test.ts``
(the package-manifest host-dependency warnings are not ported: misaka has no npm packages)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from misaka.config import home
from misaka.core.extensions.runner import ExtensionRunner
from misaka.core.resource_loader import DefaultResourceLoader
from misaka.core.session_manager import SessionManager


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    agent_dir = tmp_path / "agent"
    cwd = tmp_path / "project"
    agent_dir.mkdir()
    cwd.mkdir()
    monkeypatch.setenv(home.ENV_HOME, str(tmp_path / "home"))
    return cwd, agent_dir


async def _noop_handler(*_args):
    return None


def _command(name: str, description: str):
    def factory(pi) -> None:
        pi.registerCommand(name, {"description": description, "handler": _noop_handler})

    return factory


def _write_settings(path: Path, settings: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings), encoding="utf-8")


async def test_leaves_out_replaceable_extensions_whose_names_another_extension_registers(dirs):
    # A third-party MCP extension registering /mcp replaces the built-in one instead of both running.
    cwd, agent_dir = dirs
    global_ext_dir = agent_dir / "extensions"
    global_ext_dir.mkdir()
    (global_ext_dir / "other_mcp.py").write_text(
        "async def _handler(*args):\n    return None\n\n"
        "def default(pi):\n    pi.registerCommand('mcp', {'description': 'other mcp', 'handler': _handler})\n",
        encoding="utf-8",
    )

    loader = DefaultResourceLoader({
        "cwd": str(cwd), "agentDir": str(agent_dir),
        "extensionFactories": [
            {"name": "mcp", "replaceable": True, "factory": _command("mcp", "built-in mcp")},
            {"name": "llama", "replaceable": True, "factory": _command("llama", "built-in llama")},
        ],
    })
    await loader.reload()

    result = loader.getExtensions()
    assert [extension.path for extension in result.extensions] == [
        str(global_ext_dir / "other_mcp.py"), "<inline:llama>",
    ]
    assert result.errors == []

    runner = ExtensionRunner(result.extensions, result.runtime, str(cwd), SessionManager.inMemory(str(cwd)), None)
    assert runner.getCommand("mcp").description == "other mcp"
    assert runner.getCommand("llama").description == "built-in llama"


async def test_skips_built_in_extensions_disabled_in_settings(dirs):
    cwd, agent_dir = dirs
    _write_settings(agent_dir / "settings.json", {"extensions": ["-builtin:mcp"]})
    loaded: list[str] = []
    loader = DefaultResourceLoader({
        "cwd": str(cwd), "agentDir": str(agent_dir),
        "extensionFactories": [
            {"name": "mcp", "builtin": True, "factory": lambda _pi: loaded.append("mcp")},
            {"name": "llama", "builtin": True, "factory": lambda _pi: loaded.append("llama")},
        ],
    })
    await loader.reload()

    extensions = loader.getExtensions().extensions
    assert [extension.path for extension in extensions] == ["builtin:llama"]
    assert (extensions[0].sourceInfo.path, extensions[0].sourceInfo.source) == ("builtin:llama", "builtin")
    assert extensions[0].hidden is True
    assert loaded == ["llama"]


async def test_loads_built_in_extensions_after_file_extensions_with_and_without_trust_resolution(dirs):
    cwd, agent_dir = dirs
    user_ext_dir = agent_dir / "extensions"
    user_ext_dir.mkdir()
    (user_ext_dir / "user.py").write_text("def default(pi):\n    pass\n", encoding="utf-8")
    # A project override gives the built-in project scope, which must not move it ahead.
    _write_settings(cwd / home.DIR_NAME / "settings.json", {"extensions": ["+builtin:mcp"]})
    loader = DefaultResourceLoader({
        "cwd": str(cwd), "agentDir": str(agent_dir),
        "extensionFactories": [{"name": "mcp", "builtin": True, "factory": lambda _pi: None}],
    })
    expected = [str(user_ext_dir / "user.py"), "builtin:mcp"]

    async def trusted(_context):
        return True

    await loader.reload({"resolveProjectTrust": trusted})
    assert [extension.path for extension in loader.getExtensions().extensions] == expected
    await loader.reload()
    assert [extension.path for extension in loader.getExtensions().extensions] == expected


async def test_disables_built_in_extensions_with_no_extensions_unless_loaded_with_e_builtin(dirs):
    cwd, agent_dir = dirs
    loaded: list[str] = []
    loader = DefaultResourceLoader({
        "cwd": str(cwd), "agentDir": str(agent_dir), "noExtensions": True,
        "additionalExtensionPaths": ["builtin:mcp", "builtin:missing"],
        "extensionFactories": [
            {"name": "mcp", "builtin": True, "factory": lambda _pi: loaded.append("mcp")},
            {"name": "llama", "builtin": True, "factory": lambda _pi: loaded.append("llama")},
        ],
    })
    await loader.reload()

    assert [extension.path for extension in loader.getExtensions().extensions] == ["builtin:mcp"]
    assert loader.getExtensions().errors == [
        {"path": "builtin:missing", "error": "Unknown built-in extension: builtin:missing"},
    ]
    assert loaded == ["mcp"]


async def test_applies_project_built_in_extension_overrides_after_trust_resolves(dirs):
    cwd, agent_dir = dirs
    _write_settings(agent_dir / "settings.json", {"extensions": ["-builtin:mcp"]})
    _write_settings(cwd / home.DIR_NAME / "settings.json", {"extensions": ["+builtin:mcp", "-builtin:llama"]})
    loaded: list[str] = []
    loader = DefaultResourceLoader({
        "cwd": str(cwd), "agentDir": str(agent_dir),
        "extensionFactories": [
            {"name": "mcp", "builtin": True, "factory": lambda _pi: loaded.append("mcp")},
            {"name": "plain", "factory": lambda _pi: loaded.append("plain")},
            {"name": "llama", "builtin": True, "factory": lambda _pi: loaded.append("llama")},
        ],
    })

    async def trusted(context):
        # Built-in extensions wait until project settings are known.
        assert [extension.path for extension in context["extensionsResult"].extensions] == ["<inline:plain>"]
        return True

    await loader.reload({"resolveProjectTrust": trusted})

    assert [extension.path for extension in loader.getExtensions().extensions] == ["builtin:mcp", "<inline:plain>"]
    assert loaded == ["plain", "mcp"]


async def test_a_replaced_built_in_extension_is_reported_as_a_warning(dirs):
    cwd, agent_dir = dirs
    loader = DefaultResourceLoader({
        "cwd": str(cwd), "agentDir": str(agent_dir),
        "extensionFactories": [
            {"name": "other", "factory": _command("mcp", "other mcp")},
            {"name": "mcp", "builtin": True, "replaceable": True, "factory": _command("mcp", "built-in mcp")},
        ],
    })
    await loader.reload()

    result = loader.getExtensions()
    assert [extension.path for extension in result.extensions] == ["<inline:other>"]
    [warning] = result.warnings
    assert warning["path"] == "builtin:mcp"
    assert "registers command `/mcp`, so built-in extension `mcp` was not loaded" in warning["warning"]
    assert os.sep not in warning["path"]


async def test_skips_disabled_builtin_extensions_even_when_settings_or_e_enable_them(dirs):
    cwd, agent_dir = dirs
    _write_settings(cwd / home.DIR_NAME / "settings.json", {"extensions": ["+builtin:mcp"]})
    loaded: list[str] = []
    loader = DefaultResourceLoader({
        "cwd": str(cwd), "agentDir": str(agent_dir),
        "disabledBuiltinExtensions": ["mcp"],
        "additionalExtensionPaths": ["builtin:mcp"],
        "extensionFactories": [
            {"name": "mcp", "builtin": True, "factory": lambda _pi: loaded.append("mcp")},
            {"name": "llama", "builtin": True, "factory": lambda _pi: loaded.append("llama")},
        ],
    })

    async def trusted(_context):
        return True

    await loader.reload({"resolveProjectTrust": trusted})

    assert [extension.path for extension in loader.getExtensions().extensions] == ["builtin:llama"]
    assert loader.getExtensions().errors == []
    assert loaded == ["llama"]
