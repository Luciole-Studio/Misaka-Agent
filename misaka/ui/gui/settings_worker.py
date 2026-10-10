"""Settings for the GUI, done the way ``misaka setup`` and ``misaka web`` do them, minus the terminal.

Each call is one short-lived process (``python -m misaka.ui.gui.settings_worker``): a JSON
request on stdin, one marked JSON line back. A process per call keeps the model registry,
the web scope and the extension discovery these use out of the long-lived GUI server, the
same reason chats run in their own processes. The writes go through the same functions the
wizard calls -- credential store, SettingsManager, role pins, roster, the web config
writer -- so the GUI and the terminal can never disagree about where a setting lives.

``login`` is the one conversational call: it streams events (the browser link, a device
code, a question) as marked lines and reads answers back on stdin until the sign-in ends.
"""
from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import re
import shlex
import sys
import threading
from types import SimpleNamespace

MARK = "@@misaka-gui@@"
_OUT = sys.stdout


def send(payload: dict) -> None:
    _OUT.write(MARK + json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    _OUT.flush()


# ---- environment and overview ---------------------------------------------------------

TOOLS = (("git", "项目仓库与结果提交", True), ("rg", "文本检索（ripgrep）", True),
         ("fd", "文件查找", True), ("pdftotext", "读取 PDF 文本（poppler）", True),
         ("ocrmypdf", "扫描版 PDF 的文字识别（只有图片型 PDF 才需要）", False))


def _extras_command(extras: list[str]) -> dict:
    from misaka.cli import update
    try:
        install = update.describe()
        command = update.adding_extras(install, extras)
    except Exception:  # noqa: BLE001 - a guess at the install command is not worth failing for
        command, install = None, None
    if command is None:
        from misaka.cli.update import REPO_URL
        return {"command": f"pip install 'misaka[{','.join(extras)}] @ git+{REPO_URL}'", "after_exit": True}
    return {"command": shlex.join(command), "after_exit": getattr(install, "installer", "") in ("uv tool", "pipx")}


def op_overview(_params: dict) -> dict:
    if _params.get("section") == "skills":
        # Older GUI servers can invoke this fresh, read-only worker while their
        # existing chats stay alive, before they gain composer_skills themselves.
        from misaka.ui.gui.composer import skill_catalogue
        return skill_catalogue(os.getcwd(), _params.get("role") or None)
    import platform

    from misaka.cli.setup import Wizard, _install_command
    from misaka.config import CFG, VERSION, current_config, profiles
    from misaka.config import env as env_file
    from misaka.config.product import setting
    from misaka.core.documents.pageindex import available as pageindex_available
    from misaka.core.research import runs
    from misaka.core.skills import layers as skill_layers
    from misaka.extensions import coverage
    from misaka.utils.tools_manager import find_tool

    tools = []
    for binary, purpose, required in TOOLS:
        present = find_tool(binary) is not None
        tools.append({"name": binary, "purpose": purpose, "required": required, "present": present,
                      "install": "" if present else _install_command(binary)})
    cfg = current_config()
    try:
        stored = env_file.read()
    except Exception:  # noqa: BLE001
        stored = {}
    roles_root = os.path.expanduser(CFG["roles_root"])
    external = os.path.expanduser("~/.agents/skills")
    external_names = sorted(e.name for e in os.scandir(external) if e.is_dir() and not e.name.startswith(".")) \
        if os.path.isdir(external) else None
    has_outline = pageindex_available()
    return {
        "version": VERSION, "python": platform.python_version(), "python_ok": sys.version_info >= (3, 12),
        "platform": sys.platform, "tools": tools,
        "office": Wizard._importable("docx", "openpyxl", "pptx"),
        "pageindex": has_outline, "pageindex_install": None if has_outline else _extras_command(["pageindex"]),
        "provider": cfg.get("provider", ""), "model": cfg.get("default_model", ""),
        "plan_approval": bool(setting("research", "plan_approval", True, bool)),
        "limits": dict(runs.DEFAULT_LIMITS),
        "openalex": {"name": coverage.KEY_ENV, "stored": bool(stored.get(coverage.KEY_ENV)),
                     "shell": bool(os.environ.get(coverage.KEY_ENV)), "url": "https://openalex.org/rest-api",
                     "file": str(env_file.path())},
        "paths": {"soul": profiles.shared_soul(), "roles": roles_root,
                  "shared_skills": str(skill_layers.shared_skills_dir()), "external_skills": external,
                  "external_names": external_names},
    }


# ---- models and credentials -----------------------------------------------------------

def _runtime(read_only=True):
    from misaka.cli.auth import _create_runtime
    return _create_runtime(read_only=read_only)


def _profile_for(target):
    from misaka.config import current_config
    cfg = current_config()
    if not target:
        return None
    if target == "last_order":
        return os.path.join(cfg["roles_root"], "last_order")
    from misaka.core.network import roster
    if target not in roster.roster_names(root=cfg["profiles_root"]):
        raise ValueError("没有这位 Sister")
    return os.path.join(cfg["profiles_root"], target)


def op_models_overview(_params: dict) -> dict:
    from misaka.cli.auth import _missing_sdk_extras
    from misaka.cli.setup import FEATURED_PROVIDERS
    from misaka.config import current_config, profiles
    from misaka.core.network import roster

    cfg = current_config()
    registry = _runtime().registry
    custom = _custom_catalog()
    load_error = registry.getError()
    if load_error and not custom.get("error"):
        custom = {**custom, "error": load_error[:500]}
    custom_ids = {item["id"] for item in custom["services"]}
    known = sorted({model.provider for model in registry.getAll()})
    oauth_ids = {p.id for p in registry.getOAuthProviders()}
    providers = []
    for provider in [*[p for p in FEATURED_PROVIDERS if p in known], *[p for p in known if p not in FEATURED_PROVIDERS]]:
        status = registry.getProviderAuthStatus(provider)
        try:
            extras = _missing_sdk_extras(registry, provider)
        except Exception:  # noqa: BLE001
            extras = []
        configured = bool(status.configured or status.source)
        providers.append({"id": provider, "name": registry.getProviderDisplayName(provider),
                          "oauth": provider in oauth_ids, "featured": provider in FEATURED_PROVIDERS,
                          "configured": configured,
                          "source": "自定义服务" if provider in custom_ids else (status.source or ""),
                          "extras": extras, "extras_install": _extras_command(extras) if extras else None,
                          "custom": provider in custom_ids})
    from misaka.ui.gui.model_preferences import preferences, initial_refs, selection
    stored, refs = preferences(), initial_refs()
    for item in providers:
        chosen = selection(item["id"], registry.getAll(), stored=stored, refs=refs)
        item.update(enabled=chosen["enabled"], selected_count=len(chosen["models"]))
    targets = [{"key": None, "label": "全局默认", "hint": "没有单独设置模型的角色都使用它", "pinned": ""},
               {"key": "last_order", "label": "Last Order", "hint": "研究协调者",
                "pinned": profiles.pinned_model(os.path.join(cfg["roles_root"], "last_order")) or ""}]
    for sid in roster.roster_names(root=cfg["profiles_root"]):
        targets.append({"key": sid, "label": f"Sister {sid}", "hint": "研究助手",
                        "pinned": profiles.pinned_model(os.path.join(cfg["profiles_root"], sid)) or ""})
    return {"providers": providers, "targets": targets, "custom": custom,
            "global": {"provider": cfg.get("provider", ""), "model": cfg.get("default_model", "")}}


def op_models(params: dict) -> dict:
    from misaka.cli.setup import Wizard
    from misaka.config import current_config
    provider = str(params.get("provider") or "")
    registry = _runtime().registry
    cfg = current_config()
    current = cfg["default_model"] if cfg["provider"] == provider else None
    models = Wizard._model_choices([m for m in registry.getAll() if m.provider == provider], current)
    if params.get("configured_only"):
        models = [m for m in registry.getAll() if registry.hasConfiguredAuth(m)]
    if params.get("configured_only") or params.get("enabled_only"):
        from misaka.ui.gui.model_preferences import visible_models
        if params.get("enabled_only"):
            models = [m for m in registry.getAll() if m.provider == provider]
        models = visible_models(models)
    return {"models": [{"provider": m.provider, "providerName": registry.getProviderDisplayName(m.provider), "id": m.id, "name": m.name or m.id,
                        "reasoning": bool(getattr(m, "reasoning", False)),
                        "contextWindow": getattr(m, "contextWindow", None)} for m in models]}


def op_set_key(params: dict) -> dict:
    provider, key = str(params.get("provider") or ""), str(params.get("key") or "").strip()
    if not provider or not key:
        raise ValueError("请选择服务商并填写 API Key")
    runtime = _runtime(read_only=False)
    if provider not in {m.provider for m in runtime.registry.getAll()}:
        raise ValueError("未知的服务商")
    runtime.storage.set(provider, {"type": "api_key", "key": key})
    return {"message": f"已保存 {provider} 的 API Key"}


def op_logout(params: dict) -> dict:
    provider = str(params.get("provider") or "")
    runtime = _runtime(read_only=False)
    runtime.storage.remove(provider)
    return {"message": f"已移除 {provider} 保存的登录信息（环境变量中的密钥不受影响）"}


def op_set_default(params: dict) -> dict:
    from misaka.config import profiles
    from misaka.core.settings_manager import SettingsManager
    provider, model = str(params.get("provider") or ""), str(params.get("model") or "")
    target = params.get("target") or None
    profile = _profile_for(target)
    if not provider and not model and profile:
        _clear_pin(profile)
        return {"message": "已改为跟随全局默认模型"}
    if not provider or not model:
        raise ValueError("请选择模型")
    if _runtime().registry.find(provider, model) is None:
        raise ValueError(f"{provider}/{model} 不在模型目录中")
    if profile:
        profiles.persist_role_default_model(profile, f"{provider}/{model}", strict=True)
    else:
        SettingsManager.create(params.get("workspace") or os.getcwd()).setDefaultModelAndProvider(provider, model)
    return {"message": f"默认模型已保存：{provider} / {model}"}


def _clear_pin(profile: str) -> None:
    from filelock import FileLock

    from misaka.config import profiles
    from misaka.utils import atomic
    path = profiles.settings_path(profile)
    if not os.path.exists(path):
        return
    with FileLock(path + ".lock"):
        data = profiles.role_settings(profile, strict=True)
        data.pop("defaultProvider", None)
        data.pop("defaultModel", None)
        atomic.write_text(path, json.dumps(data, ensure_ascii=False, indent=2))


def op_verify(params: dict) -> dict:
    from misaka.ai.stream import complete_simple
    from misaka.ai.types import Context, SimpleStreamOptions, UserMessage
    provider, model_id = str(params.get("provider") or ""), str(params.get("model") or "")
    registry = _runtime().registry

    async def ping() -> str:
        model = registry.find(provider, model_id)
        if model is None:
            raise RuntimeError(f"{provider}/{model_id} 不在模型目录中")
        auth = await registry.getApiKeyAndHeaders(model)
        if not auth.get("ok"):
            raise RuntimeError(auth.get("error") or "没有找到可用的凭证")
        reply = await complete_simple(
            model, Context(messages=[UserMessage(content="Reply with the single word OK.", timestamp=0)]),
            SimpleStreamOptions(apiKey=auth.get("apiKey"), headers=auth.get("headers"), maxTokens=16))
        if reply.stopReason == "error":
            raise RuntimeError(reply.errorMessage or "请求失败")
        return "".join(getattr(part, "text", "") for part in reply.content)
    text = asyncio.run(ping())
    return {"message": f"{provider} 已回复：{text.strip()[:40] or '（空回复）'}"}


# ---- provider catalogue and the GUI shortlist -------------------------------------------


def _provider_catalog(registry, provider: str) -> list[dict]:
    from misaka.config.product import setting
    saved = setting("gui", "modelCatalog", {})
    cached = saved.get(provider, []) if isinstance(saved, dict) else []
    catalog = {m.id: {"id": m.id, "name": m.name or m.id, "contextWindow": m.contextWindow,
                      "maxTokens": m.maxTokens, "reasoning": bool(m.reasoning), "input": list(m.input)}
               for m in registry.getAll() if m.provider == provider}
    if isinstance(cached, list) and cached:
        from misaka.ui.gui.model_preferences import selection
        selected = set(selection(provider, registry.getAll())["models"])
        catalog = {key: value for key, value in catalog.items() if key in selected}
    for item in cached if isinstance(cached, list) else []:
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            catalog[item["id"]] = {**catalog.get(item["id"], {}), **item}
    return sorted(catalog.values(), key=lambda m: (m.get("name") or m["id"]).casefold())


def op_provider_models(params: dict) -> dict:
    from misaka.ui.gui.model_preferences import selection
    provider = str(params.get("provider") or "")
    registry = _runtime().registry
    if provider not in {m.provider for m in registry.getAll()}:
        raise ValueError("未知的服务商")
    chosen = selection(provider, registry.getAll())
    return {"models": _provider_catalog(registry, provider), "selected": chosen["models"], "enabled": chosen["enabled"],
            "message": "已保存的目录；点击获取模型列表可更新。"}


def _provider_auth(provider: str, model_id: str = ""):
    registry = _runtime(read_only=False).registry
    models = [m for m in registry.getAll() if m.provider == provider]
    if not models:
        raise ValueError("未知的服务商")
    if not model_id:
        from misaka.ui.gui.model_preferences import selection
        model_id = next(iter(selection(provider, models)["models"]), "")
    model = next((m for m in models if m.id == model_id), models[0])
    auth = asyncio.run(registry.getApiKeyAndHeaders(model))
    if not auth.get("ok"):
        raise ValueError(auth.get("error") or "请先登录或填写 API Key")
    return registry, model, auth


def op_fetch_provider_models(params: dict) -> dict:
    from misaka.core.settings_manager import SettingsManager
    provider = str(params.get("provider") or "")
    registry, model, auth = _provider_auth(provider)
    # Subscription bridges do not necessarily expose /models. Never query an
    # unrelated public API with a subscription token.
    if model.api not in _CUSTOM_API_IDS:
        return {**op_provider_models(params),
                "message": "这个登录方式没有模型列表接口，已读取内置模型目录。"}
    base = _check_base_url(model.baseUrl)
    key = auth.get("apiKey") or ""
    headers = {**_catalog_headers(model.api, key), **(auth.get("headers") or {})}
    catalog = None
    from urllib.parse import urlencode
    for url in _catalog_urls(model.api, base):
        gathered, seen = {}, set()
        while url and len(gathered) < 2000:
            if url in seen or len(seen) >= 20:
                raise ValueError("模型列表分页过多，请稍后重试")
            seen.add(url)
            status, payload = _fetch_one(url, headers, key)
            if status in {404, 405} and not gathered:
                break
            if status in {401, 403}:
                raise ValueError(f"地址可达，但凭证被拒绝（HTTP {status}）")
            if not 200 <= status < 300 or payload is None:
                raise ValueError(f"获取模型列表失败（HTTP {status}）")
            page, capped = _parse_model_catalog(payload, 2001)
            gathered.update((m["id"], m) for m in page)
            if capped or len(gathered) > 2000:
                raise ValueError("目录超过 2000 个模型，请使用更小的目录")
            url = ""
            if isinstance(payload, dict):
                if payload.get("nextPageToken"):
                    url = _catalog_urls(model.api, base)[0] + "?" + urlencode({"pageToken": payload["nextPageToken"]})
                elif payload.get("has_more") and page:
                    url = _catalog_urls(model.api, base)[0] + "?" + urlencode({"after_id": page[-1]["id"], "limit": 1000})
        if url:
            raise ValueError("目录超过 2000 个模型，请使用更小的目录")
        if gathered:
            catalog = list(gathered.values())
            break
    if not catalog:
        raise ValueError("服务没有返回可用于对话的模型列表；可以使用已保存的目录。")
    def mutate(section):
        all_catalogs = dict(section.get("modelCatalog") or {})
        all_catalogs[provider] = catalog
        section["modelCatalog"] = all_catalogs
    SettingsManager.forRole(None).updateSection("gui", mutate)
    chosen = op_provider_models(params)
    # A successful refresh is authoritative for available choices. Keep already
    # selected entries visible so users can explicitly remove retired models.
    from misaka.ui.gui.model_preferences import selection
    selection_data = selection(provider, registry.getAll())
    selected_ids = set(selection_data["models"])
    missing = [m for m in chosen["models"] if m["id"] in selected_ids and m["id"] not in {c["id"] for c in catalog}]
    return {"models": sorted(catalog + missing, key=lambda m: (m.get("name") or m["id"]).casefold()),
            "selected": selection_data["models"], "enabled": selection_data["enabled"], "message": f"已从服务获取 {len(catalog)} 个模型，勾选后保存即可用于对话。"}


def op_test_provider(params: dict) -> dict:
    provider = str(params.get("provider") or "")
    _registry, model, auth = _provider_auth(provider, str(params.get("model") or ""))
    if provider == "openrouter":
        key = auth.get("apiKey") or ""
        status, payload = _fetch_one(model.baseUrl.rstrip("/") + "/key",
                                     _catalog_headers("openai-completions", key), key)
        if not 200 <= status < 300 or not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
            raise ValueError(f"OpenRouter 凭证测试失败（HTTP {status}）")
        return {"message": "OpenRouter 连接正常，凭证有效。"}
    try:
        return op_verify({"provider": provider, "model": model.id})
    except Exception as error:
        raise ValueError(_scrub(str(error), auth.get("apiKey") or "")) from None


def op_save_model_selection(params: dict) -> dict:
    from misaka.core.settings_manager import SettingsManager
    provider = str(params.get("provider") or "")
    registry = _runtime().registry
    if provider not in {m.provider for m in registry.getAll()}:
        raise ValueError("未知的服务商")
    ids = params.get("models")
    if not isinstance(ids, list) or len(ids) > 2000 or any(not isinstance(v, str) for v in ids):
        raise ValueError("请选择有效的模型")
    ids = list(dict.fromkeys(ids))
    catalog = {m["id"]: m for m in _provider_catalog(registry, provider)}
    if any(v not in catalog for v in ids):
        raise ValueError("选择中有未知模型，请重新获取模型列表")
    # Newly discovered models need to be resolvable by actual chat/Sister runtimes.
    if not isinstance(params.get("enabled", True), bool):
        raise ValueError("启用状态必须是布尔值")
    additions = [catalog[v] for v in ids if registry.find(provider, v) is None]
    if additions:
        def import_models(data):
            block = data.setdefault("providers", {}).setdefault(provider, {})
            existing = {m["id"]: m for m in block.get("models", [])}
            existing.update((m["id"], m) for m in additions)
            block["models"] = list(existing.values())
        _update_models_document(import_models)
    def mutate(section):
        choices = dict(section.get("modelSelection") or {})
        choices[provider] = {"enabled": params.get("enabled", True), "models": ids}
        section["modelSelection"] = choices
    SettingsManager.forRole(None).updateSection("gui", mutate)
    return {"message": f"已保存 {provider} 的 {len(ids)} 个模型"}


# ---- a service the built-in catalog does not know ---------------------------------------

# What models.json calls `api`. Each one is a wire protocol a gateway can speak;
# the list endpoint is how the form checks the address and learns model ids.
CUSTOM_PROTOCOLS = (
    {"id": "openai-completions", "label": "OpenAI 兼容（Chat Completions）",
     "hint": "Ollama、LM Studio、vLLM、OneAPI 等。地址一般以 /v1 结尾。",
     "placeholder": "http://127.0.0.1:11434/v1"},
    {"id": "openai-responses", "label": "OpenAI Responses",
     "hint": "使用 OpenAI Responses 接口的服务。地址一般以 /v1 结尾。",
     "placeholder": "https://api.openai.com/v1"},
    {"id": "anthropic-messages", "label": "Anthropic 兼容（Messages）",
     "hint": "Claude 官方或兼容网关。官方地址不带 /v1。",
     "placeholder": "https://api.anthropic.com"},
    {"id": "google-generative-ai", "label": "Google Gemini 兼容",
     "hint": "Gemini 官方或兼容网关。",
     "placeholder": "https://generativelanguage.googleapis.com/v1beta"},
)
_CUSTOM_API_IDS = {item["id"] for item in CUSTOM_PROTOCOLS}
_PROVIDER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_ENV_KEY = re.compile(r"\$(?:[A-Za-z_][A-Za-z0-9_]*|\{[A-Za-z_][A-Za-z0-9_]*\})")
_CATALOG_LIMIT = 400


def _models_file() -> str:
    from misaka.config.engine import get_models_path
    return get_models_path()


def _read_models_document() -> dict:
    path = _models_file()
    if not os.path.isfile(path):
        return {"providers": {}}
    from misaka.core.model_registry import _strip_json_comments
    with open(path, encoding="utf-8-sig") as handle:
        text = handle.read()
    try:
        parsed = json.loads(_strip_json_comments(text) or "{}")
    except json.JSONDecodeError as error:
        raise ValueError(f"models.json 无法解析：{error.msg}（第 {error.lineno} 行）") from error
    if not isinstance(parsed, dict):
        raise ValueError("models.json 的顶层必须是对象")
    providers = parsed.get("providers", {})
    if not isinstance(providers, dict):
        raise ValueError("models.json 的 providers 必须是对象")
    parsed["providers"] = providers
    return parsed


def _builtin_provider_ids() -> set[str]:
    from misaka.ai.models import get_providers
    return set(get_providers())


def _is_custom_block(provider_id: str, block: object, builtin: set[str]) -> bool:
    if provider_id in builtin or not isinstance(block, dict) or not _PROVIDER_ID.fullmatch(provider_id):
        return False
    return bool(block.get("baseUrl")) and bool(block.get("api") or block.get("models") or block.get("apiKey"))


def _key_hint(api_key: str) -> str:
    if not api_key:
        return ""
    if api_key.startswith("!"):
        return "由本机命令提供"
    if _ENV_KEY.fullmatch(api_key):
        return api_key
    return "已保存在本机"


def _public_model(item: dict) -> dict:
    published = {"id": item["id"]}
    for key in ("name", "contextWindow", "maxTokens", "reasoning"):
        if item.get(key) not in (None, ""):
            published[key] = item[key]
    return published


def _custom_catalog() -> dict:
    protocols = [dict(item) for item in CUSTOM_PROTOCOLS]
    try:
        document = _read_models_document()
    except ValueError as error:
        return {"protocols": protocols, "services": [], "error": str(error)}
    builtin = _builtin_provider_ids()
    services = []
    for provider_id, block in document["providers"].items():
        if not _is_custom_block(provider_id, block, builtin):
            continue
        models = []
        for item in block.get("models") or []:
            if isinstance(item, dict) and item.get("id"):
                models.append(_public_model(item))
        services.append({
            "id": provider_id, "name": block.get("name") or provider_id,
            "api": block.get("api") or "", "base_url": block.get("baseUrl") or "",
            "has_key": bool(block.get("apiKey")), "key_hint": _key_hint(str(block.get("apiKey") or "")),
            "models": models,
        })
    services.sort(key=lambda item: item["id"])
    return {"protocols": protocols, "services": services}


def _check_base_url(url: object) -> str:
    from urllib.parse import urlsplit
    text = str(url or "").strip()
    if not text or len(text) > 2048:
        raise ValueError("请填写服务地址")
    parsed = urlsplit(text)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("地址必须是 http 或 https，并包含主机名")
    if parsed.username or parsed.password:
        raise ValueError("不要把密钥写进地址，请填到 API Key")
    return text.rstrip("/")


def _require_protocol(api: object) -> str:
    text = str(api or "").strip()
    if text not in _CUSTOM_API_IDS:
        raise ValueError("请选择协议")
    return text


def _clean_key(value: object) -> str:
    key = str(value or "")
    if any(char in key for char in "\r\n\0"):
        raise ValueError("API Key 不能包含换行")
    key = key.strip()
    if len(key) > 4096:
        raise ValueError("API Key 过长")
    return key


def _scrub(message: str, key: str) -> str:
    if key and len(key) >= 8:
        message = message.replace(key, "******")
    return message[:500]


def _resolved_key(stored: str) -> str:
    if not stored:
        return ""
    from misaka.core.resolve_config_value import resolve_config_value
    resolved = resolve_config_value(stored)
    if resolved is None:
        raise ValueError("API Key 没有解析出来。$NAME 需要环境变量已设置，!命令 需要命令有输出")
    return resolved


def _stored_api_key(provider_id: str) -> str:
    if not _PROVIDER_ID.fullmatch(provider_id):
        return ""
    try:
        block = _read_models_document()["providers"].get(provider_id)
    except ValueError:
        return ""
    if not isinstance(block, dict):
        return ""
    return str(block.get("apiKey") or "")


def _key_for_request(params: dict, *, required: bool) -> str:
    typed = _clean_key(params.get("api_key"))
    stored = typed or _stored_api_key(str(params.get("provider") or "").strip())
    resolved = _resolved_key(stored)
    if required and not resolved:
        raise ValueError("请填写 API Key。本地服务可以填任意文字，例如 local")
    return resolved


def _catalog_urls(api: str, base: str) -> list[str]:
    if api in {"openai-completions", "openai-responses"}:
        return [base if base.endswith("/models") else base + "/models"]
    if api == "anthropic-messages":
        if base.endswith("/models"):
            return [base]
        if base.endswith("/v1"):
            return [base + "/models"]
        return [base + "/v1/models", base + "/models"]
    if api == "google-generative-ai":
        return [base if base.endswith("/models") else base + "/models"]
    raise ValueError("请选择协议")


def _catalog_headers(api: str, key: str) -> dict[str, str]:
    from misaka.ai.utils.user_agent import get_misaka_user_agent
    headers = {"Accept": "application/json", "User-Agent": get_misaka_user_agent()}
    if not key:
        return headers
    if api == "anthropic-messages":
        headers["x-api-key"] = key
        headers["anthropic-version"] = "2023-06-01"
    elif api == "google-generative-ai":
        headers["x-goog-api-key"] = key
    else:
        headers["Authorization"] = "Bearer " + key
    return headers


def _parse_model_catalog(payload: object, limit: int = _CATALOG_LIMIT) -> tuple[list[dict], bool]:
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        items = payload.get("data")
        if not isinstance(items, list):
            items = payload.get("models")
        if not isinstance(items, list):
            items = []
    else:
        items = []
    found: list[dict] = []
    seen: set[str] = set()
    capped = False
    for item in items:
        if isinstance(item, str):
            model_id, name = item.strip(), item.strip()
        elif isinstance(item, dict):
            raw = str(item.get("id") or item.get("name") or "").strip()
            model_id = raw.split("/", 1)[1] if raw.startswith("models/") else raw
            name = str(item.get("display_name") or item.get("displayName") or (item.get("name") if item.get("id") else None) or model_id).strip()
            if name.startswith("models/"):
                name = model_id
        else:
            continue
        if not model_id or model_id in seen or len(model_id) > 256 or any(char in model_id for char in "\r\n\0"):
            continue
        seen.add(model_id)
        record = {"id": model_id, "name": name or model_id}
        if isinstance(item, dict):
            architecture = item.get("architecture") or {}
            if architecture.get("output_modalities") and "text" not in architecture["output_modalities"]:
                continue
            methods = item.get("supportedGenerationMethods")
            if isinstance(methods, list) and "generateContent" not in methods:
                continue
            for key, raw in (("contextWindow", item.get("context_length") or item.get("inputTokenLimit")),
                             ("maxTokens", (item.get("top_provider") or {}).get("max_completion_tokens") or item.get("outputTokenLimit"))):
                if isinstance(raw, int) and raw > 0:
                    record[key] = raw
            if "input_modalities" in architecture:
                inputs = [v for v in architecture["input_modalities"] if v in {"text", "image"}]
                record["input"] = inputs or ["text"]
            if "supported_parameters" in item:
                record["reasoning"] = "reasoning" in (item.get("supported_parameters") or [])
            pricing = item.get("pricing")
            if isinstance(pricing, dict):
                try:
                    record["cost"] = {"input": max(0, float(pricing.get("prompt") or 0) * 1_000_000),
                                      "output": max(0, float(pricing.get("completion") or 0) * 1_000_000),
                                      "cacheRead": 0, "cacheWrite": 0}
                except (TypeError, ValueError):
                    pass
        found.append(record)
        if len(found) >= limit:
            capped = True
            break
    return found, capped


def _fetch_one(url: str, headers: dict[str, str], key: str) -> tuple[int, object]:
    import httpx
    from urllib.parse import urljoin, urlsplit
    host = (urlsplit(url).hostname or "").lower()
    loopback = host in {"localhost", "127.0.0.1", "::1"}
    try:
        with httpx.Client(timeout=httpx.Timeout(15.0), follow_redirects=False, trust_env=not loopback) as client:
            current = url
            origin = urlsplit(current)
            for _ in range(4):
                response = client.get(current, headers=headers)
                if response.status_code in {301, 302, 303, 307, 308}:
                    nxt = urljoin(current, response.headers.get("location") or "")
                    _check_base_url(nxt)
                    target = urlsplit(nxt)
                    # A catalogue request carries the key. Leaving the host, or leaving
                    # https, would hand that key to somewhere the user did not name.
                    if (target.hostname or "").lower() != (origin.hostname or "").lower():
                        raise ValueError("服务把请求重定向到了另一个主机，已中止")
                    if origin.scheme == "https" and target.scheme != "https":
                        raise ValueError("服务把请求从 https 重定向到了不安全的地址，已中止")
                    current = nxt.rstrip("/")
                    continue
                body = response.content
                if len(body) > 8_000_000:
                    raise ValueError("模型列表过大")
                parsed = None
                if body:
                    try:
                        parsed = json.loads(body.decode("utf-8-sig"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        parsed = None
                return response.status_code, parsed
            raise ValueError("重定向次数过多")
    except httpx.HTTPError as error:
        raise ValueError("无法连接这个地址：" + _scrub(str(error), key)) from None


def _fetch_model_catalog(api: str, base: str, key: str) -> dict:
    missing = False
    for url in _catalog_urls(api, base):
        status, parsed = _fetch_one(url, _catalog_headers(api, key), key)
        if status in {401, 403}:
            raise ValueError(f"地址可达，但密钥被拒绝（HTTP {status}）")
        if status in {404, 405}:
            missing = True
            continue
        if status < 200 or status >= 300:
            raise ValueError(f"服务返回 HTTP {status}")
        if parsed is None:
            return {"message": "已连通，但响应不是模型列表。可以在下面手动填写模型 ID。", "models": []}
        models, capped = _parse_model_catalog(parsed)
        if models:
            message = f"已连通，获取到 {len(models)} 个模型"
            if capped:
                message += "。只列出前 400 个，其余请手动填写 ID"
            return {"message": message, "models": models}
        return {"message": "已连通，但响应里没有模型。可以在下面手动填写模型 ID。", "models": []}
    if missing:
        return {"message": "地址可达，但没有模型列表接口。可以在下面手动填写模型 ID。", "models": []}
    raise ValueError("没有拿到模型列表")


def op_probe_custom(params: dict) -> dict:
    return _fetch_model_catalog(
        _require_protocol(params.get("api")),
        _check_base_url(params.get("base_url")),
        _key_for_request(params, required=False),
    )


def op_ping_custom(params: dict) -> dict:
    api = _require_protocol(params.get("api"))
    base = _check_base_url(params.get("base_url"))
    key = _key_for_request(params, required=True)
    model_id = str(params.get("model") or "").strip()
    if not model_id or len(model_id) > 256 or any(char in model_id for char in "\r\n\0"):
        raise ValueError("请选择一个模型")
    from misaka.ai.stream import complete_simple
    from misaka.ai.types import Context, Model, ModelCost, SimpleStreamOptions, UserMessage
    model = Model(
        id=model_id, name=model_id, api=api, provider="custom", baseUrl=base, reasoning=False,
        input=["text"], cost=ModelCost(input=0, output=0, cacheRead=0, cacheWrite=0),
        contextWindow=128000, maxTokens=32,
    )

    async def ping() -> str:
        reply = await complete_simple(
            model, Context(messages=[UserMessage(content="Reply with the single word OK.", timestamp=0)]),
            SimpleStreamOptions(apiKey=key, maxTokens=32, timeoutMs=30_000),
        )
        if reply.stopReason == "error":
            raise RuntimeError(reply.errorMessage or "请求失败")
        return "".join(getattr(part, "text", "") for part in reply.content)

    try:
        text = asyncio.run(ping())
    except Exception as error:  # noqa: BLE001 - the form shows one line, never the key
        raise ValueError(_scrub(str(error) or "请求失败", key)) from None
    return {"message": f"模型已回复：{text.strip()[:40] or '（空回复）'}"}


def _clean_models(value: object) -> list[dict]:
    if not isinstance(value, list):
        raise ValueError("请至少选择或填写一个模型")
    models = []
    seen: set[str] = set()
    for item in value:
        if isinstance(item, str):
            item = {"id": item}
        if not isinstance(item, dict):
            raise ValueError("模型格式不正确")
        model_id = str(item.get("id") or "").strip()
        if not model_id or len(model_id) > 256 or any(char in model_id for char in "\r\n\0"):
            raise ValueError("模型 ID 不能为空，且不能包含换行")
        if model_id in seen:
            continue
        seen.add(model_id)
        cleaned = {"id": model_id}
        name = str(item.get("name") or "").strip()
        if name and name != model_id:
            if len(name) > 256:
                raise ValueError("模型名称过长")
            cleaned["name"] = name
        for key in ("contextWindow", "maxTokens"):
            if item.get(key) in (None, ""):
                continue
            try:
                number = int(item[key])
            except (TypeError, ValueError):
                raise ValueError(f"{model_id} 的 {key} 不是正整数") from None
            if number <= 0:
                raise ValueError(f"{model_id} 的 {key} 必须大于 0")
            cleaned[key] = number
        if item.get("reasoning") not in (None, ""):
            cleaned["reasoning"] = bool(item["reasoning"])
        models.append(cleaned)
        if len(models) > _CATALOG_LIMIT:
            raise ValueError("一次最多保存 400 个模型")
    if not models:
        raise ValueError("请至少选择或填写一个模型")
    return models


def _ordered_block(block: dict) -> dict:
    ordered = {}
    for key in ("name", "baseUrl", "api", "apiKey", "headers", "compat", "authHeader", "oauth", "models", "modelOverrides"):
        if key in block:
            ordered[key] = block[key]
    for key, value in block.items():
        if key not in ordered:
            ordered[key] = value
    return ordered


def _update_models_document(mutate):
    from filelock import FileLock

    from misaka.utils import atomic
    path = _models_file()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with FileLock(path + ".lock", timeout=10):
        document = _read_models_document()
        result = mutate(document)
        atomic.write_text(path, json.dumps(document, ensure_ascii=False, indent=2) + "\n", mode=0o600)
        return result


def op_save_custom(params: dict) -> dict:
    provider_id = str(params.get("provider") or "").strip()
    if not _PROVIDER_ID.fullmatch(provider_id):
        raise ValueError("服务 ID 只能使用字母、数字、点、下划线和短横线，并以字母或数字开头")
    api = _require_protocol(params.get("api"))
    base = _check_base_url(params.get("base_url"))
    typed_key = _clean_key(params.get("api_key"))
    models = _clean_models(params.get("models"))
    name = str(params.get("name") or "").strip()
    if name and (len(name) > 80 or any(char in name for char in "\r\n\0")):
        raise ValueError("显示名称过长或含有换行")
    builtin = _builtin_provider_ids()

    def mutate(document: dict) -> dict:
        existing = document["providers"].get(provider_id)
        if existing is not None and not isinstance(existing, dict):
            raise ValueError("models.json 里这个服务的配置不是对象")
        if existing is not None and not _is_custom_block(provider_id, existing, builtin):
            raise ValueError("这个名称是内置服务商，请换一个，例如 ollama 或 my-gateway")
        if existing is None and provider_id in builtin:
            raise ValueError("这个名称是内置服务商，请换一个，例如 ollama 或 my-gateway")
        block = dict(existing or {})
        if name:
            block["name"] = name
        else:
            block.pop("name", None)
        block["baseUrl"] = base
        block["api"] = api
        if typed_key:
            block["apiKey"] = typed_key
        elif not block.get("apiKey"):
            raise ValueError("请填写 API Key。本地服务可以填任意文字，例如 local")
        block["models"] = models
        document["providers"][provider_id] = _ordered_block(block)
        return {"message": f"已保存自定义服务 {provider_id}（{len(models)} 个模型）"}

    return _update_models_document(mutate)


def op_remove_custom(params: dict) -> dict:
    provider_id = str(params.get("provider") or "").strip()
    builtin = _builtin_provider_ids()

    def mutate(document: dict) -> dict:
        block = document["providers"].get(provider_id)
        if not _is_custom_block(provider_id, block, builtin):
            raise ValueError("没有这个自定义服务")
        document["providers"].pop(provider_id, None)
        return {"message": f"已移除自定义服务 {provider_id}。如果它是某个角色的默认模型，请另外再选一个"}

    return _update_models_document(mutate)


# ---- browser and device sign-in ---------------------------------------------------------

def op_login(params: dict) -> dict:
    """The wizard's ``_oauth``, with the browser as the terminal.

    Answers arrive on stdin from a daemon thread, never through ``to_thread``: a login
    whose callback server wins the race would otherwise leave a worker blocked on stdin
    that ``asyncio.run`` then waits for forever (see the wizard's docstring).
    """
    from misaka.ai.auth.oauth_bridge import callbacks_for_interaction
    from misaka.cli.setup import _open_browser

    provider = str(params.get("provider") or "")
    registry = _runtime(read_only=False).registry
    if provider not in {p.id for p in registry.getOAuthProviders()}:
        raise ValueError("这个服务商不支持浏览器登录")

    async def run() -> None:
        loop = asyncio.get_running_loop()
        waiting: dict[str, asyncio.Future] = {}

        def reader() -> None:
            for line in sys.stdin:
                try:
                    answer = json.loads(line)
                except ValueError:
                    continue
                future = waiting.get(str(answer.get("id")))
                if future is not None:
                    loop.call_soon_threadsafe(lambda f=future, v=answer.get("value"): f.done() or f.set_result(v))
        threading.Thread(target=reader, daemon=True).start()
        counter = iter(range(1, 10_000))

        async def ask(request) -> str:
            key = f"q{next(counter)}"
            kind = getattr(request, "type", "text")
            options = [{"id": o.id, "label": o.label} for o in (getattr(request, "options", None) or [])]
            future = loop.create_future()
            waiting[key] = future
            send({"event": "prompt", "id": key, "kind": kind, "message": str(getattr(request, "message", "")),
                  "options": options})
            try:
                value = await future
            finally:
                waiting.pop(key, None)
                send({"event": "prompt_done", "id": key})
            if value is None:
                raise RuntimeError("已取消登录")
            return str(value)

        def notify(event) -> None:
            kind = getattr(event, "type", "")
            if kind == "auth_url":
                opened = _open_browser(str(event.url))
                send({"event": "auth_url", "url": str(event.url), "opened": opened,
                      "instructions": str(getattr(event, "instructions", "") or "")})
            elif kind == "device_code":
                send({"event": "device_code", "url": str(event.verificationUri), "code": str(event.userCode)})
            else:
                send({"event": "message", "message": str(getattr(event, "message", ""))})

        interaction = SimpleNamespace(signal=None, prompt=ask, notify=notify)
        owned = (registry.getRegisteredProviderConfig(provider) is not None
                 or registry.getRegisteredNativeProvider(provider) is not None)
        if owned:
            await registry.login(provider, "oauth", interaction)
        else:
            await registry.authStorage.login(provider, callbacks_for_interaction(interaction))

    asyncio.run(run())
    return {"message": f"{provider} 已登录"}


# ---- roles ------------------------------------------------------------------------------

ROLE_FILES = ("DESCRIBE.md", "SOUL.md")


def op_sisters(_params: dict) -> dict:
    from misaka.config import CFG, profiles
    from misaka.core.network import roster
    root = CFG["profiles_root"]
    items = []
    for sid in roster.roster_names(root=root):
        desc, _body = roster.describe(sid, root)
        try:
            counts = roster.card_counts(sid)
        except Exception:  # noqa: BLE001 - a missing board is zero cards
            counts = {}
        items.append({"id": sid, "description": desc or "", "model": profiles.pinned_model(os.path.join(root, sid)) or "",
                      "cards": counts, "path": os.path.join(root, sid)})
    next_id = 10032
    while any(str(next_id) == item["id"] for item in items):
        next_id += 1
    return {"sisters": items, "next_id": str(next_id), "roles_root": os.path.expanduser(CFG["roles_root"])}


def op_create_sister(params: dict) -> dict:
    from misaka.core.network import roster
    sid = str(params.get("id") or "").strip()
    ok, message = roster.create_sister(sid, specialty=(params.get("specialty") or None),
                                       model=(params.get("model") or None))
    if not ok:
        raise ValueError(message)
    return {"message": f"已添加 Sister {sid}"}


def op_remove_sister(params: dict) -> dict:
    from misaka.core.network import roster
    ok, message = roster.remove_sister(str(params.get("id") or ""))
    if not ok:
        raise ValueError(message)
    return {"message": message}


def _role_file(params: dict) -> str:
    from misaka.config import CFG, profiles
    name = params.get("file")
    if name not in ROLE_FILES and name != "SHARED_SOUL":
        raise ValueError("只能编辑 DESCRIBE.md、SOUL.md 或共享身份")
    if name == "SHARED_SOUL":
        return profiles.shared_soul()
    role = str(params.get("role") or "")
    if role == "last_order":
        if name != "SOUL.md":
            raise ValueError("Last Order 只有 SOUL.md")
        return os.path.join(os.path.expanduser(CFG["roles_root"]), "last_order", "SOUL.md")
    _profile_for(role)
    return os.path.join(CFG["profiles_root"], role, name)


def op_read_role_file(params: dict) -> dict:
    path = _role_file(params)
    try:
        with open(path, encoding="utf-8-sig") as f:
            return {"path": path, "content": f.read()}
    except FileNotFoundError:
        return {"path": path, "content": ""}


def op_write_role_file(params: dict) -> dict:
    from misaka.utils import atomic
    path = _role_file(params)
    content = params.get("content")
    if not isinstance(content, str) or len(content) > 200_000:
        raise ValueError("内容为空或过长")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    atomic.write_text(path, content)
    return {"message": f"已保存 {os.path.basename(path)}"}


# ---- research and project ---------------------------------------------------------------

def op_terminal(_params: dict) -> dict:
    from misaka.ui.gui.terminals import terminal_settings
    return terminal_settings()


def op_set_terminal(params: dict) -> dict:
    from misaka.core.settings_manager import SettingsManager
    from misaka.ui.gui.terminals import validate_terminal
    value = validate_terminal(params.get("terminal"))

    def mutate(section: dict) -> None:
        section["terminal"] = value
    SettingsManager.forRole(None).updateSection("gui", mutate)
    return {"message": "终端类型已保存", **op_terminal({})}


def op_set_research(params: dict) -> dict:
    from misaka.config import env as env_file
    from misaka.core.settings_manager import SettingsManager
    from misaka.extensions import coverage
    messages = []
    if "plan_approval" in params:
        value = bool(params["plan_approval"])

        def mutate(section: dict) -> None:
            section["plan_approval"] = value
        SettingsManager.forRole(None).updateSection("research", mutate)
        messages.append("计划审批：" + ("需要确认" if value else "自动执行"))
    if params.get("openalex_key"):
        env_file.write({coverage.KEY_ENV: str(params["openalex_key"]).strip()})
        messages.append("已保存 OpenAlex Key")
    if params.get("openalex_remove"):
        env_file.write({}, remove=(coverage.KEY_ENV,))
        messages.append("已移除 OpenAlex Key")
    return {"message": "；".join(messages) or "没有改动"}


def op_init_project(params: dict) -> dict:
    from misaka.core.platform import cards
    folder = os.path.abspath(os.path.expanduser(str(params.get("folder") or "")))
    if not params.get("folder"):
        raise ValueError("请填写项目文件夹")
    if not os.path.isdir(folder):
        if not params.get("create"):
            raise ValueError("文件夹不存在")
        os.makedirs(folder, exist_ok=True)
    lines = [str(line) for line in cards.init_project(folder)]
    return {"message": f"项目已就绪：{folder}", "lines": lines, "folder": folder}


# ---- web tools --------------------------------------------------------------------------

@contextlib.contextmanager
def _web_scope():
    from misaka.cli import web as web_cli
    from misaka.core.web import registry
    from misaka.core.web.scope import WebScope, current_scope
    with WebScope(None).activate():
        result = asyncio.run(web_cli._discover([]))
        try:
            registry.ensure_backends_registered()
            registry.replace_extension_providers(result.extensions)
            current_scope().browser_providers = {name: provider for extension in result.extensions
                                                 for name, provider in getattr(extension, "browserProviders", {}).items()}
            yield
        finally:
            result.runtime.invalidate()


def _kind(key: str) -> dict:
    from misaka.cli.web_setup import CHOICES
    from misaka.core.web import config
    if key in config._BOOL_KEYS or tuple(key.split(".")) in config._NESTED_BOOL_KEYS or key == "vault.enabled":
        return {"type": "bool"}
    if key in CHOICES:
        return {"type": "choice", "choices": list(CHOICES[key])}
    if key.startswith("provider_tier."):
        return {"type": "choice", "choices": ["auto", "free", "paid"]}
    if key in config._LIST_KEYS or tuple(key.split(".")) in config._NESTED_LIST_KEYS:
        return {"type": "list"}
    if key in {"vault.onepassword", "vault.bitwarden", "browser.controller_command", "browser.controller_capabilities"} \
            or key.startswith("http_timeout."):
        return {"type": "json"}
    return {"type": "text"}


def _shown(key):
    from misaka.cli.web_setup import _sensitive, _value
    from misaka.core.web import config
    value = _value(key)
    if value is None:
        return None, False
    if _sensitive(key):
        return "已设置（隐藏）", True
    return config.redact_secrets(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)), False


def op_web_overview(_params: dict) -> dict:
    from misaka.cli import web as web_cli
    from misaka.cli import web_setup
    from misaka.core.web import config, dispatch, registry
    with _web_scope():
        providers = []
        for p in registry.list_providers(include_disabled=True):
            rows = []
            for row in web_cli._provider_rows(p):
                rows.append({"name": row.get("name", p.display_name), "tag": row.get("tag", ""), "badge": row.get("badge", ""),
                             "web_tier": row.get("web_tier"), "post_setup": row.get("post_setup"),
                             "env_vars": [{"key": v["key"], "prompt": v.get("prompt", v["key"]), "url": v.get("url", "")}
                                          for v in row.get("env_vars", [])]})
            providers.append({"name": p.name, "display": p.display_name, "search": bool(p.supports_search()),
                              "extract": bool(p.supports_extract()), "disabled": config.provider_disabled(p.name),
                              "ready": bool(registry.provider_is_ready(p)), "tier": config.provider_tier(p.name), "rows": rows})
        resolved = {}
        for label, resolve in (("search", dispatch.resolve_provider), ("extract", dispatch.resolve_extractor)):
            provider, backend, error = resolve()
            resolved[label] = {"backend": backend or "", "error": str(error or ""),
                               "ready": bool(provider is not None and registry.provider_is_ready(provider))}
        own = config.own_section()
        browser = config.web_config().get("browser") or {}
        from misaka.core.web.browser.providers import providers as browser_providers
        services = {}
        for name, provider in browser_providers().items():
            services[name] = [{"name": r.get("name", name), "tag": r.get("tag", ""),
                               "env_vars": [{"key": v["key"], "prompt": v.get("prompt", v["key"])} for v in r.get("env_vars", [])]}
                              for r in web_cli._provider_rows(provider)]
        services["camofox"] = [{"name": "Camofox", "tag": "", "env_vars": [{"key": k, "prompt": k} for k in ("CAMOFOX_URL", "CAMOFOX_API_KEY", "CAMOFOX_USER_ID")]}]
        groups = []
        for title, keys in web_setup.groups().items():
            fields = []
            for key in keys:
                shown, hidden = _shown(key)
                fields.append({"key": key, "value": shown, "hidden": hidden, **_kind(key)})
            groups.append({"title": title, "fields": fields})
        credentials = [{"name": n, "set": s, "source": src} for n, s, src in config.credential_status()]
        buffer = io.StringIO()
        try:
            with contextlib.redirect_stdout(buffer):
                web_cli._status()
        except Exception as error:  # noqa: BLE001 - a broken config is shown, not raised
            buffer.write(f"\n{config.redact_secrets(str(error))}")
        return {"providers": providers, "resolved": resolved,
                "selected": {k: own.get(k) for k in ("backend", "search_backend", "extract_backend")},
                "keyless": bool(config.keyless_tier_enabled()),
                "browser": {"enabled": browser.get("enabled"), "cloud_provider": browser.get("cloud_provider"),
                            "engine": browser.get("engine"), "lightpanda_path": browser.get("lightpanda_path") or "",
                            "cdp": bool(browser.get("cdp_url"))},
                "browser_services": services, "groups": groups, "credentials": credentials,
                "status": config.redact_secrets(buffer.getvalue()), "path": config.config_label()}


def _web_commit(changes: dict, remove=()) -> dict:
    """``web_setup.save`` without its confirmation prompt: validate the merged layers, then write."""
    from misaka.cli.web_setup import _sensitive, _validate
    from misaka.core.settings_manager import deep_merge_settings
    from misaka.core.web import config, dispatch, registry
    from misaka.core.web.scope import current_scope
    from dataclasses import replace
    changes = {str(k): str(v) for k, v in changes.items()}
    for key, value in changes.items():
        if _sensitive(key) and value:
            config.remember_secret(value)
    own = config.own_section()
    for key in remove:
        parts = key.split(".")
        parent = own if len(parts) == 1 else own.get(parts[0], {})
        if isinstance(parent, dict):
            parent.pop(parts[-1], None)
    for key, value in changes.items():
        config._set_value(own, key, value)
    shared = config.load_config() if current_scope().profile_dir else {}
    prospective = deep_merge_settings(shared, own)
    _validate(prospective)
    warnings = []
    with replace(current_scope(), config=prospective, config_error=None).activate():
        for label, resolve in (("搜索", dispatch.resolve_provider), ("网页提取", dispatch.resolve_extractor)):
            provider, backend, error = resolve()
            if error or not registry.provider_is_ready(provider):
                warnings.append(f"{label}：{backend or '未选择'} 在本机尚未就绪，可能缺少凭证或安装。")
    config.update_config(changes, remove=tuple(remove))
    return {"message": "已保存。账号可用性与网络连通性没有测试。", "warnings": warnings}


def op_web_save(params: dict) -> dict:
    changes = params.get("changes") or {}
    remove = params.get("remove") or []
    if not isinstance(changes, dict) or not isinstance(remove, list):
        raise ValueError("参数格式不正确")
    with _web_scope():
        return _web_commit(changes, tuple(str(k) for k in remove))


def op_web_provider(params: dict) -> dict:
    """``misaka web setup <provider>`` as one form: capabilities, tier row, its credentials."""
    from misaka.cli import web as web_cli
    from misaka.core.web import config, registry
    name = str(params.get("name") or "")
    capability = params.get("capability") or "both"
    with _web_scope():
        if not name:
            caps = ("search", "extract") if capability == "both" else (capability,)
            changes = {f"{cap}_backend": "" for cap in caps}
            if capability == "both":
                changes["backend"] = ""
            return _web_commit(changes)
        provider = registry.get_provider(name, include_disabled=True)
        if provider is None:
            raise ValueError("未知的联网服务")
        if config.provider_disabled(name):
            raise ValueError("这个服务已停用，请先启用")
        rows = web_cli._provider_rows(provider)
        index = int(params.get("row") or 0)
        row = rows[index] if 0 <= index < len(rows) else rows[0]
        supported = [cap for cap in ("search", "extract") if getattr(provider, f"supports_{cap}")()]
        selected = supported if capability == "both" else [capability]
        if not selected or any(cap not in supported for cap in selected):
            raise ValueError(f"{name} 只支持：{'、'.join(supported) or '无'}")
        changes = {f"{cap}_backend": name for cap in selected}
        tier = params.get("tier") or row.get("web_tier")
        if tier:
            changes[f"provider_tier.{name}"] = tier
        if tier == "free":
            changes["keyless_fallback"] = "true"
        values = params.get("env") or {}
        for variable in row.get("env_vars", []):
            value = str(values.get(variable["key"]) or "").strip()
            if value:
                changes["env." + variable["key"]] = value
        result = _web_commit(changes)
        if row.get("post_setup") == "ddgs" and not registry.provider_is_ready(provider):
            result["warnings"].append("ddgs 尚未安装：可在「联网 → 安装可选工具」中安装。")
        return result


def op_web_enable(params: dict) -> dict:
    from misaka.core.web import config, registry
    name = str(params.get("name") or "")
    with _web_scope():
        if registry.get_provider(name, include_disabled=True) is None:
            raise ValueError("未知的联网服务")
        config.set_provider_enabled(name, bool(params.get("enabled")))
    return {"message": ("已启用 " if params.get("enabled") else "已停用 ") + name}


def op_web_browser(params: dict) -> dict:
    """The wizard's browser-connection menu as one form."""
    from urllib.parse import urlsplit

    from misaka.core.web import config
    mode = params.get("mode")
    with _web_scope():
        if mode == "off":
            return _web_commit({"browser.enabled": "false"})
        changes = {"browser.enabled": "true", "browser.controller_command": "[]",
                   "browser.cdp_url": "", "env.BROWSER_CDP_URL": "", "browser.cloud_provider": mode}
        if mode != "cdp" and os.environ.get("BROWSER_CDP_URL"):
            raise ValueError("环境变量 BROWSER_CDP_URL 会覆盖这里的设置，请先在启动环境中取消它。")
        if mode == "local":
            engine = params.get("engine") or "auto"
            if engine not in ("auto", "chrome", "lightpanda"):
                raise ValueError("未知的浏览器引擎")
            changes["browser.engine"] = engine
            if engine == "lightpanda":
                path = params.get("lightpanda_path") or (config.web_config().get("browser") or {}).get("lightpanda_path")
                if not path:
                    raise ValueError("Lightpanda 需要可执行文件路径")
                changes.update({"browser.lightpanda_path": path, "browser.headed": "false", "browser.use_real_profile": "false"})
        elif mode == "cdp":
            endpoint = str(params.get("cdp_url") or "")
            if not endpoint:
                raise ValueError("CDP 需要填写地址")
            if urlsplit(endpoint).scheme not in {"http", "https", "ws", "wss"} or not urlsplit(endpoint).hostname:
                raise ValueError("CDP 地址必须是 http(s) 或 ws(s)")
            changes["browser.cdp_url"] = endpoint
        else:
            for key, value in (params.get("env") or {}).items():
                if str(value).strip():
                    changes["env." + str(key)] = str(value).strip()
        return _web_commit(changes)


OPS = {name[3:]: fn for name, fn in globals().items() if name.startswith("op_") and callable(fn)}


def main() -> int:
    try:
        for stream in (sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", newline="\n")
        line = sys.stdin.readline()
        request = json.loads(line or "{}")
        op = OPS.get(request.get("op"))
        if op is None:
            raise ValueError("未知设置操作")
        params = request.get("params") or {}
        if request.get("workspace") and os.path.isdir(request["workspace"]):
            os.chdir(request["workspace"])
            params.setdefault("workspace", request["workspace"])
        # Whatever a library prints is not the reply; only marked lines are.
        with contextlib.redirect_stdout(sys.stderr):
            data = op(params)
        send({"ok": True, "data": data})
    except BaseException as error:  # noqa: BLE001 - every failure reaches the GUI as text
        if isinstance(error, KeyboardInterrupt):
            raise
        message = str(error) or type(error).__name__
        try:
            from misaka.core.web import config
            message = config.redact_secrets(message)
        except Exception:  # noqa: BLE001
            pass
        send({"ok": False, "error": message})
    sys.stdout.flush()
    os._exit(0)   # a login's stdin reader thread must not hold the exit


if __name__ == "__main__":
    raise SystemExit(main())
