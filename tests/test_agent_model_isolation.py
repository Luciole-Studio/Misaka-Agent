"""Offline regressions for per-agent model defaults, without live profiles or sessions."""

import errno
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from misaka.config import profiles
from misaka.core.agent_session import AgentSession
from misaka.core.settings_manager import InMemorySettingsStorage, SettingsManager


@pytest.fixture
def registry():
    models = [
        SimpleNamespace(provider=provider, id=model_id, reasoning=False)
        for provider, model_id in [
            ("global-provider", "base"),
            ("vendor-a", "shared"),
            ("vendor-b", "shared"),
            ("vendor-a", "unique"),
            ("vendor-b", "org/nested"),
        ]
    ]
    return SimpleNamespace(
        getAll=lambda: list(models),
        getAvailable=lambda: list(models),
        find=lambda provider, model_id: next(
            (model for model in models if (model.provider, model.id) == (provider, model_id)),
            None,
        ),
        hasConfiguredAuth=lambda _model: True,
        checkConfiguredAuth=AsyncMock(return_value=True),
    )


@pytest.fixture
def shared_storage():
    storage = InMemorySettingsStorage()
    storage.global_value = json.dumps({
        "defaultProvider": "global-provider",
        "defaultModel": "base",
        "theme": "existing-theme",
    })
    return storage


def make_role(tmp_path, name, model):
    """A role directory whose settings.json pins ``provider/model`` (and holds something else)."""
    role = tmp_path / name
    role.mkdir()
    provider, _, model_id = model.partition("/")
    (role / "settings.json").write_text(json.dumps(
        {"defaultProvider": provider, "defaultModel": model_id, "custom": {"keep": True}}))
    return role


def pin_of(role):
    data = json.loads((role / "settings.json").read_text())
    return f"{data['defaultProvider']}/{data['defaultModel']}"


def bind(storage, role, registry):
    settings = SettingsManager.fromStorage(storage)
    settings.bindModelProfile(str(role), registry)
    return settings


def default_pair(settings):
    return settings.getDefaultProvider(), settings.getDefaultModel()


def test_pair_getter_reads_role_file_at_bind_not_per_call(tmp_path, registry, shared_storage):
    role = make_role(tmp_path, "10032", "vendor-a/shared")
    settings = bind(shared_storage, role, registry)
    (role / "settings.json").unlink()
    assert settings.getDefaultModelPair() == ("vendor-a", "shared")


async def test_two_roles_save_independent_full_model_identity(tmp_path, monkeypatch, registry, shared_storage):
    first_role = make_role(tmp_path, "last_order", "vendor-a/shared")
    second_role = make_role(tmp_path, "10032", "vendor-b/shared")
    first = bind(shared_storage, first_role, registry)
    second = bind(shared_storage, second_role, registry)
    global_before = shared_storage.global_value
    second_before = (second_role / "settings.json").read_bytes()

    # A process-global environment value must not decide which live instance saves.
    monkeypatch.setenv("MISAKA_PROFILE_DIR", str(second_role))
    assert default_pair(first) == ("vendor-a", "shared")
    assert default_pair(second) == ("vendor-b", "shared")
    first.setDefaultModelAndProvider("vendor-b", "org/nested")
    await first.flush()

    assert json.loads((first_role / "settings.json").read_text()) == {
        "defaultProvider": "vendor-b", "defaultModel": "org/nested", "custom": {"keep": True},
    }
    assert (second_role / "settings.json").read_bytes() == second_before
    assert shared_storage.global_value == global_before
    assert default_pair(first) == ("vendor-b", "org/nested")
    assert default_pair(second) == ("vendor-b", "shared")
    await first.reload()
    await second.reload()
    assert default_pair(first) == ("vendor-b", "org/nested")
    assert default_pair(second) == ("vendor-b", "shared")
    assert default_pair(bind(shared_storage, first_role, registry)) == ("vendor-b", "org/nested")


async def test_other_settings_save_does_not_copy_role_model_to_global(tmp_path, registry, shared_storage):
    role = make_role(tmp_path, "10032", "vendor-a/shared")
    settings = bind(shared_storage, role, registry)
    settings.setDefaultModelAndProvider("vendor-b", "org/nested")
    settings.setTheme("new-theme")
    await settings.flush()
    assert json.loads(shared_storage.global_value) == {
        "defaultProvider": "global-provider", "defaultModel": "base", "theme": "new-theme",
    }
    assert default_pair(settings) == ("vendor-b", "org/nested")


async def test_unbound_settings_keep_explicit_global_configuration(shared_storage):
    settings = SettingsManager.fromStorage(shared_storage)
    settings.setDefaultModelAndProvider("vendor-a", "unique")
    await settings.flush()
    assert json.loads(shared_storage.global_value)["defaultProvider"] == "vendor-a"
    assert json.loads(shared_storage.global_value)["defaultModel"] == "unique"


@pytest.mark.parametrize("method,args", [
    ("setDefaultProvider", ("vendor-b",)),
    ("setDefaultModel", ("unique",)),
    ("setDefaultModelAndProvider", ("vendor-a", "unique")),
])
@pytest.mark.parametrize("has_role", [False, True])
def test_child_default_writes_require_definition_editor(tmp_path, registry, shared_storage, method, args, has_role):
    role = make_role(tmp_path, "10032", "vendor-a/shared")
    settings = bind(shared_storage, role, registry) if has_role else SettingsManager.fromStorage(shared_storage)
    settings.restrictModelDefaults()
    before = (role / "settings.json").read_bytes()
    global_before = shared_storage.global_value
    with pytest.raises(ValueError, match="/agents"):
        getattr(settings, method)(*args)
    assert (role / "settings.json").read_bytes() == before
    assert shared_storage.global_value == global_before


async def test_child_can_still_change_session_model_without_saving(registry, shared_storage):
    settings = SettingsManager.fromStorage(shared_storage)
    settings.restrictModelDefaults()
    global_before = shared_storage.global_value
    session = session_stub(settings, registry)
    target = registry.find("vendor-b", "shared")
    await session.setModel(target, persist=False)
    assert session.model is target
    assert shared_storage.global_value == global_before


def test_unpinned_role_inherits_default_without_creating_configuration(tmp_path, registry, shared_storage):
    role = tmp_path / "unpinned"
    role.mkdir()
    settings = bind(shared_storage, role, registry)
    assert default_pair(settings) == ("global-provider", "base")
    assert not (role / "settings.json").exists()


def test_role_pin_takes_precedence_over_project_default(tmp_path, registry, shared_storage):
    shared_storage.project_value = json.dumps({"defaultProvider": "vendor-b", "defaultModel": "org/nested"})
    role = make_role(tmp_path, "10032", "vendor-a/shared")
    settings = bind(shared_storage, role, registry)
    assert default_pair(settings) == ("vendor-a", "shared")


@pytest.mark.parametrize("stored", [
    {"defaultProvider": "vendor-missing", "defaultModel": "shared"},   # no such provider
    {"defaultModel": "unique"},                                         # half a pin: no provider
    {"defaultProvider": "vendor-a"},                                    # half a pin: no model
])
def test_invalid_role_pin_does_not_silently_become_global_default(tmp_path, registry, shared_storage, stored):
    role = tmp_path / "10032"
    role.mkdir()
    (role / "settings.json").write_text(json.dumps(stored))
    before = (role / "settings.json").read_bytes()
    settings = bind(shared_storage, role, registry)
    with pytest.raises(ValueError):
        default_pair(settings)
    assert (role / "settings.json").read_bytes() == before              # never rewritten behind the user's back


@pytest.mark.parametrize("reference,provider,expected", [
    ("vendor-a/shared", None, "vendor-a/shared"),
    ("unique", None, "vendor-a/unique"),
    ("vendor-b/org/nested", None, "vendor-b/org/nested"),
    ("org/nested", None, "vendor-b/org/nested"),
    ("shared", "vendor-b", "vendor-b/shared"),
])
def test_model_reference_resolution_is_exact(registry, reference, provider, expected):
    assert profiles.resolve_model_reference(reference, registry, fallback_provider=provider) == expected


@pytest.mark.parametrize("reference", ["shared", "unknown/model", "unknowable", ""])
def test_model_reference_rejects_ambiguous_or_unknown_names(registry, reference):
    with pytest.raises(ValueError):
        profiles.resolve_model_reference(reference, registry)


@pytest.mark.parametrize("method,argument,expected", [
    ("setDefaultProvider", "vendor-b", "vendor-b/shared"),
    ("setDefaultModel", "unique", "vendor-a/unique"),
])
async def test_single_field_setters_are_also_role_scoped(tmp_path, registry, shared_storage, method, argument, expected):
    role = make_role(tmp_path, "10032", "vendor-a/shared")
    settings = bind(shared_storage, role, registry)
    global_before = shared_storage.global_value
    getattr(settings, method)(argument)
    await settings.flush()
    assert pin_of(role) == expected
    assert shared_storage.global_value == global_before


def test_provider_switch_rejects_model_missing_on_new_provider(tmp_path, registry, shared_storage):
    role = make_role(tmp_path, "10032", "vendor-a/unique")
    settings = bind(shared_storage, role, registry)
    before = (role / "settings.json").read_bytes()
    global_before = shared_storage.global_value
    with pytest.raises(ValueError):
        settings.setDefaultProvider("vendor-b")
    assert (role / "settings.json").read_bytes() == before
    assert shared_storage.global_value == global_before


@pytest.mark.parametrize("broken", ["{broken json", "[]", '"not an object"'])
def test_role_configuration_read_error_is_not_a_silent_global_fallback(tmp_path, registry, shared_storage, broken):
    role = make_role(tmp_path, "10032", "vendor-a/unique")
    (role / "settings.json").write_text(broken)
    settings = bind(shared_storage, role, registry)
    with pytest.raises((ValueError, TypeError)):
        default_pair(settings)
    assert (role / "settings.json").read_text() == broken


@pytest.mark.parametrize("broken", ["{broken json", "[]", '"not an object"'])
def test_strict_role_write_rejects_corruption_without_destroying_it(tmp_path, broken):
    config = tmp_path / "settings.json"
    config.write_text(broken)
    with pytest.raises((ValueError, TypeError)):
        profiles.persist_role_default_model(str(tmp_path), "vendor-a/unique", strict=True)
    assert config.read_text() == broken
    assert profiles.persist_role_default_model(str(tmp_path), "vendor-a/unique") is False


def test_role_write_error_is_reported_without_global_fallback(tmp_path, monkeypatch, registry, shared_storage):
    role = make_role(tmp_path, "10032", "vendor-a/shared")
    settings = bind(shared_storage, role, registry)
    before = (role / "settings.json").read_bytes()
    global_before = shared_storage.global_value

    def disk_full(*_args, **_kwargs):
        raise OSError(errno.ENOSPC, "fixture disk full")

    monkeypatch.setattr(profiles.atomic, "write_text", disk_full)
    with pytest.raises(OSError, match="fixture disk full"):
        settings.setDefaultModelAndProvider("vendor-b", "shared")
    assert (role / "settings.json").read_bytes() == before
    assert shared_storage.global_value == global_before
    assert default_pair(settings) == ("vendor-a", "shared")


def session_stub(settings, registry):
    session = object.__new__(AgentSession)
    session.settingsManager = settings
    session.agent = SimpleNamespace(state=SimpleNamespace(
        model=registry.find("vendor-a", "shared"), thinkingLevel="off",
    ))
    session.sessionManager = SimpleNamespace(appendModelChange=Mock())
    session._modelRegistry = registry
    session._scopedModels = []
    session._get_thinking_level_for_model_switch = lambda *_args: "off"
    session.setThinkingLevel = Mock()
    session._extensionRunner = SimpleNamespace(emit=AsyncMock())
    return session


@pytest.mark.parametrize("persist", [False, True])
@pytest.mark.parametrize("operation", ["set", "cycle", "cycle-scoped"])
async def test_session_model_operations_respect_persistence_scope(tmp_path, registry, shared_storage, persist, operation):
    role = make_role(tmp_path, "10032", "vendor-a/shared")
    settings = bind(shared_storage, role, registry)
    before = (role / "settings.json").read_bytes()
    global_before = shared_storage.global_value
    session = session_stub(settings, registry)
    target = registry.find("vendor-b", "shared")
    if operation == "set":
        await session.setModel(target, persist=persist)
    else:
        if operation == "cycle-scoped":
            session._scopedModels = [{"model": session.model}, {"model": target}]
        await session.cycleModel(persist=persist)
    await settings.flush()
    assert session.model is target
    assert shared_storage.global_value == global_before
    if persist:
        assert pin_of(role) == "vendor-b/shared"
        assert default_pair(settings) == ("vendor-b", "shared")
    else:
        assert (role / "settings.json").read_bytes() == before
        assert default_pair(settings) == ("vendor-a", "shared")


async def test_saving_new_role_default_does_not_expand_global_enabled_models(tmp_path, registry, shared_storage):
    values = json.loads(shared_storage.global_value)
    values["enabledModels"] = ["vendor-a/shared"]
    shared_storage.global_value = json.dumps(values)
    role = make_role(tmp_path, "10032", "vendor-a/shared")
    settings = bind(shared_storage, role, registry)
    global_before = shared_storage.global_value
    session = session_stub(settings, registry)
    session._scopedModels = [{"model": session.model}]
    target = registry.find("vendor-b", "shared")
    await session.setModel(target, persist=True)
    await settings.flush()
    assert shared_storage.global_value == global_before
    assert target in [item["model"] for item in session._scopedModels]


@pytest.mark.parametrize("explicit_scope", [False, True])
def test_shared_enabled_models_do_not_override_role_pin_but_explicit_scope_can(tmp_path, registry, shared_storage, explicit_scope):
    from misaka.cli.args import Args
    from misaka.cli.engine import build_session_options
    from misaka.core.model_resolver import ScopedModel

    role = make_role(tmp_path, "10032", "vendor-a/shared")
    settings = bind(shared_storage, role, registry)
    pinned = registry.find("vendor-a", "shared")
    scoped = registry.find("vendor-b", "shared")
    args = Args(models=["vendor-b/shared"] if explicit_scope else None)
    result = build_session_options(args, [ScopedModel(scoped)], False, registry, settings)
    if explicit_scope:
        assert result.options["model"] is scoped
    else:
        # Leaving model unset is also correct: the SDK then applies the role pin.
        assert result.options.get("model", pinned) is pinned


@pytest.fixture
def sdk_host(tmp_path, monkeypatch, registry, shared_storage):
    """Exercise SDK selection with in-memory transcript and inert session construction."""
    from misaka.core import sdk
    from misaka.core.session_manager import SessionManager

    monkeypatch.setattr(sdk, "Agent", lambda options: SimpleNamespace(
        state=SimpleNamespace(model=options["initialState"]["model"], messages=[]),
    ))
    monkeypatch.setattr(sdk, "AgentSession", lambda config: SimpleNamespace(
        model=config["agent"].state.model, settingsManager=config["settingsManager"],
    ))
    monkeypatch.setattr(sdk, "applyHttpProxySettings", lambda _settings: None)
    return {
        "cwd": str(tmp_path),
        "agentDir": str(tmp_path / "engine"),
        "authStorage": object(),
        "resourceLoader": SimpleNamespace(getExtensions=lambda: None),
        "settingsManager": SettingsManager.fromStorage(shared_storage),
        "sessionManager": SessionManager.inMemory(str(tmp_path)),
        "modelRegistry": registry,
        "tools": [],
    }


@pytest.mark.parametrize("state", ["new", "resume", "explicit"])
async def test_sdk_default_never_overrides_restored_or_explicit_model(tmp_path, registry, sdk_host, state):
    from misaka.core.sdk import create_agent_session

    role = make_role(tmp_path, "10032", "vendor-a/unique")
    sdk_host["modelProfile"] = str(role)
    expected = registry.find("vendor-a", "unique")
    if state != "new":
        sdk_host["sessionManager"].appendModelChange("vendor-b", "shared")
        sdk_host["sessionManager"].appendMessage({"role": "user", "content": "fixture", "timestamp": 0})
        expected = registry.find("vendor-b", "shared")
    if state == "explicit":
        expected = sdk_host["model"] = registry.find("vendor-b", "org/nested")
    result = await create_agent_session(sdk_host)
    assert result["session"].model is expected
    assert result["session"].settingsManager.getModelProfile() == str(role)
    assert default_pair(result["session"].settingsManager) == ("vendor-a", "unique")


async def test_sdk_pin_missing_auth_reports_error_instead_of_using_another_provider(tmp_path, registry, sdk_host):
    from misaka.core.sdk import create_agent_session

    role = make_role(tmp_path, "10032", "vendor-a/unique")
    sdk_host["modelProfile"] = str(role)
    registry.hasConfiguredAuth = lambda model: model.provider != "vendor-a"
    result = await create_agent_session(sdk_host)
    # TUI can still open /login; the pin is never silently replaced by another endpoint.
    assert result["session"].model is registry.find("vendor-a", "unique")
    assert "vendor-a/unique" in result["modelFallbackMessage"]


async def test_services_keep_bound_scope_through_sdk_creation(tmp_path, registry, sdk_host):
    from misaka.core.agent_session_services import create_agent_session_from_services

    role = make_role(tmp_path, "10032", "vendor-a/unique")
    sdk_host["settingsManager"].bindModelProfile(str(role), registry)
    services = SimpleNamespace(**{key: sdk_host[key] for key in (
        "cwd", "agentDir", "authStorage", "settingsManager", "modelRegistry", "resourceLoader",
    )})
    result = await create_agent_session_from_services({
        "services": services, "sessionManager": sdk_host["sessionManager"], "tools": [],
    })
    assert result["session"].model is registry.find("vendor-a", "unique")
    assert result["session"].settingsManager.getModelProfile() == str(role)


@pytest.mark.parametrize("operation,selection", [
    ("create", "definition"), ("create", "call"), ("create", "environment"),
    ("resume", "definition"), ("resume", "environment"),
])
@pytest.mark.parametrize("authenticated", [False, True])
async def test_generic_child_preserves_cross_provider_identity_before_auth_check(
    tmp_path, monkeypatch, registry, operation, selection, authenticated,
):
    from misaka.core.subagent import runtime
    from misaka.core.subagent.agents import AgentDefinition

    target = "vendor-b/shared"
    registry.hasConfiguredAuth = lambda model: model.provider != "vendor-b" or authenticated
    registry.getAvailable = lambda: [model for model in registry.getAll() if registry.hasConfiguredAuth(model)]
    context = SimpleNamespace(
        cwd=str(tmp_path), model=registry.find("vendor-a", "shared"), modelRegistry=registry,
        settingsManager=SettingsManager.inMemory(), isProjectTrusted=lambda: True,
    )
    definition = AgentDefinition(
        name="fixture-agent", description="Fixture", prompt="Fixture agent instructions.",
        model=target if selection == "definition" else "inherit",
    )
    manager = runtime.SubagentManager(context, runtime.RoleContext(
        role="fixture", profile_dir="", workspace=str(tmp_path), mcp_role="fixture",
        model_override=target if selection == "environment" else None,
    ))
    manager._parent_session_id = "fixture-parent"
    manager._session_dir = lambda _context: tmp_path
    manager._register_task = AsyncMock(side_effect=lambda *args: SimpleNamespace(
        model_provider=args[4], model_id=args[5],
    ))
    manager.resolve_definition = lambda *_args: definition
    monkeypatch.setattr(runtime, "read_resume_transcript", lambda _path: [])
    task = SimpleNamespace(
        worktree=None, cwd=str(tmp_path), trust_from_parent=True, project_trusted=True,
        trust_source_cwd=str(tmp_path), definition=definition, transcript=tmp_path / "unused.jsonl",
        agent_type=definition.name, forked=False, persist=AsyncMock(),
        model_provider="vendor-a", model_id="shared",
    )

    async def invoke():
        if operation == "create":
            return await manager.create_task(
                definition=definition, description="Fixture", prompt="Fixture", model=target if selection == "call" else None,
                background=False, name=None, isolation=None, cwd=None, tool_call_id="fixture", context=context,
            )
        await manager._prepare_resume_state(task, context)
        return task

    if authenticated:
        selected = await invoke()
        assert (selected.model_provider, selected.model_id) == ("vendor-b", "shared")
    else:
        with pytest.raises(ValueError, match="authentication.*vendor-b/shared"):
            await invoke()
        manager._register_task.assert_not_awaited()
        task.persist.assert_not_awaited()
    assert manager._reserved == 0
