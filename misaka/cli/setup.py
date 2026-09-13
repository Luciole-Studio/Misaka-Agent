"""``misaka setup``: the first-run wizard, section by section.

Modelled on Hermes's setup wizard: one command, the logo, a fixed list of sections that can
also be run one at a time (``misaka setup model``), a ✓/✗ summary at the end that says what
works and what to run next. Every prompt shows the current value and Enter keeps it, so
running it again on a configured install is a review, not a reset.

What each section touches, and nothing else:

- environment: reads the system (python, git, ripgrep, fd, pdftotext, ocrmypdf, the panel's
  terminal library) and prints what is missing, with the install command for this OS.
- model: ``~/.misaka/agent/auth.json`` (the credential) and ``settings.json``
  (``defaultProvider`` / ``defaultModel``), through the same registry every session uses;
  then one tiny real request, because a stored key that does not work is the failure that
  otherwise shows up an hour later inside a research run.
- sisters: ``~/.misaka/profiles/sisters/<id>/`` through ``roster.create_sister``. Without at
  least one Sister a research run has nobody to hand cards to.
- documents: the optional PDF outline extra and the office libraries, checked by import.
- web: ``~/.misaka/web.json`` -- a pinned search backend and its key, or the keyless ring.
- project: ``misaka init`` on a folder.

Bare ``misaka`` runs the wizard by itself when no credential is configured anywhere.
"""
from __future__ import annotations

import asyncio
import os
import platform
import shutil
import subprocess
import sys
from types import SimpleNamespace

from misaka.cli import setup_ui as ui
from misaka.cli.setup_ui import (
    SetupCancelled,
    SetupGoBack,
    prompt,
    prompt_choice,
    prompt_yes_no,
)

SECTIONS = ("environment", "model", "sisters", "documents", "web", "project")

# The providers a fresh install is most likely to want, in the order they are offered; every
# other provider the registry knows is one menu entry further ("Another provider...").
FEATURED_PROVIDERS = ("anthropic", "openai", "google", "mistral", "amazon-bedrock", "openrouter", "ollama")

# Web search backends that take a key, and the variable the backend reads it from.
WEB_BACKENDS = (
    ("tavily", "TAVILY_API_KEY"), ("brave-free", "BRAVE_SEARCH_API_KEY"), ("exa", "EXA_API_KEY"),
    ("firecrawl", "FIRECRAWL_API_KEY"), ("perplexity", "PERPLEXITY_API_KEY"), ("parallel", "PARALLEL_API_KEY"),
    ("keenable", "KEENABLE_API_KEY"), ("xai", "XAI_API_KEY"), ("searxng", "SEARXNG_URL"),
)

PAGEINDEX_PACKAGES = ("PyPDF2==3.0.1", "pypdfium2==4.30.0", "regex>=2024.0.0", "sortedcontainers==2.4.0")


def _install_command(binary: str) -> str:
    """The package manager line for a missing binary on this OS."""
    brew = {"git": "xcode-select --install", "rg": "brew install ripgrep", "fd": "brew install fd",
            "pdftotext": "brew install poppler", "ocrmypdf": "brew install ocrmypdf tesseract-lang"}
    apt = {"git": "sudo apt install git", "rg": "sudo apt install ripgrep", "fd": "sudo apt install fd-find",
           "pdftotext": "sudo apt install poppler-utils", "ocrmypdf": "sudo apt install ocrmypdf tesseract-ocr"}
    table = brew if sys.platform == "darwin" else apt
    return table.get(binary, f"install {binary}")


class Wizard:
    def __init__(self):
        self.state: dict = {"missing": []}

    # -- 1. environment -----------------------------------------------------------------------

    def environment(self) -> None:
        ui.print_header("Environment")
        version = platform.python_version()
        ui.print_check(sys.version_info >= (3, 12), "Python", f"{version}" + ("" if sys.version_info >= (3, 12) else "  (3.12 or newer needed)"))
        required = {"git": "projects are git repositories; results are committed",
                    "rg": "the grep tool (ripgrep)", "fd": "the find tool",
                    "pdftotext": "reads the text of PDFs (poppler)"}
        optional = {"ocrmypdf": "scanned PDFs (OCR); only needed for image-only documents"}
        missing = []
        for binary, purpose in required.items():
            present = shutil.which(binary) is not None
            ui.print_check(present, binary, purpose if present else f"{purpose}  →  {_install_command(binary)}")
            if not present:
                missing.append(binary)
        for binary, purpose in optional.items():
            present = shutil.which(binary) is not None
            ui.print_check(True if present else None, binary, purpose if present else f"{purpose}  →  {_install_command(binary)}")
        from misaka.ui.panel import ghostty
        library = ghostty.library_path()
        has_panel = os.path.isfile(library)
        ui.print_check(has_panel, "panel terminal",
                       "libghostty-vt: the panel's terminal emulator" if has_panel else
                       f"libghostty-vt not found at {library}; `misaka` opens plain chat until it is built or "
                       "MISAKA_GHOSTTY_VT points at a build")
        self.state["missing"] = missing
        self.state["panel"] = has_panel
        if "git" in missing:
            ui.print_warning("Without git the project step below cannot create a repository. Install it and run "
                             "`misaka setup project` afterwards.")
        if missing:
            ui.print_info("", "Install what is missing in another terminal; the wizard goes on regardless.")

    # -- 2. model & provider ------------------------------------------------------------------

    def model(self) -> None:
        from misaka.cli.auth import _create_runtime, _install_hint, _missing_sdk_extras
        from misaka.config import current_config
        ui.print_header("Model & Provider")
        cfg = current_config()
        runtime = _create_runtime(read_only=False)
        registry = runtime.registry
        known = sorted({model.provider for model in registry.getAll()})
        current = cfg["provider"] if cfg["provider"] in known else None
        ui.print_info(f"Current default: {cfg['provider']} / {cfg['default_model']}"
                      + ("" if current else "  (provider unknown to this install)"))
        featured = [p for p in FEATURED_PROVIDERS if p in known]
        if current and current not in featured:
            featured.insert(0, current)
        labels = [self._provider_label(registry, p) for p in featured] + ["Another provider..."]
        default = featured.index(current) if current in featured else 0
        picked = prompt_choice("Which provider should Last Order and the Sisters use by default?", labels, default)
        if picked == len(featured):
            others = [p for p in known if p not in featured]
            provider = others[prompt_choice("Provider", [self._provider_label(registry, p) for p in others])]
        else:
            provider = featured[picked]
        # The SDK is installed per provider; a missing one is the most common first-run failure.
        extras = _missing_sdk_extras(registry, provider)
        if extras:
            ui.print_warning(f"The SDK for {provider} is not installed: {_install_hint(extras)}")
            if prompt_yes_no("Install it now with this interpreter's pip?", True):
                self._pip_install(["misaka[" + extra + "]" for extra in extras], from_checkout=True)
        self._credential(registry, runtime.storage, provider)
        model_id = self._pick_model(registry, provider, cfg)
        from misaka.core.settings_manager import SettingsManager
        SettingsManager.create(os.getcwd()).setDefaultModelAndProvider(provider, model_id)
        ui.print_success(f"Default model saved: {provider} / {model_id}")
        self.state["provider"], self.state["model"] = provider, model_id
        self._verify(registry, provider, model_id)

    @staticmethod
    def _provider_label(registry, provider: str) -> str:
        status = registry.getProviderAuthStatus(provider)
        name = registry.getProviderDisplayName(provider)
        where = status.source or ("configured" if status.configured else "")
        return f"{name}" + (f"  ({where})" if where else "  (no credential yet)")

    def _credential(self, registry, storage, provider: str) -> None:
        status = registry.getProviderAuthStatus(provider)
        if status.configured or status.source:
            ui.print_success(f"{provider}: credential found ({status.source or 'configured'})")
            if not prompt_yes_no("Keep it?", True):
                self._enter_credential(registry, storage, provider)
            return
        self._enter_credential(registry, storage, provider)

    def _enter_credential(self, registry, storage, provider: str) -> None:
        if provider == "amazon-bedrock":
            ui.print_info("Bedrock uses your AWS credentials: an AWS profile, or AWS_ACCESS_KEY_ID / "
                          "AWS_SECRET_ACCESS_KEY / AWS_REGION in the environment. Set those, then run "
                          "`misaka auth check amazon-bedrock`.")
            return
        oauth_ids = {p.id for p in registry.getOAuthProviders()}
        kinds = ["API key"] + (["Subscription login in the browser (OAuth)"] if provider in oauth_ids else [])
        kind = prompt_choice(f"How do you want to sign in to {provider}?", kinds, 0)
        if kind == 1:
            self._oauth(registry, provider)
            return
        key = prompt("API key", password=True)
        if not key:
            ui.print_warning("No key entered; you can add one later with /login inside the chat.")
            return
        storage.set(provider, {"type": "api_key", "key": key})
        ui.print_success(f"Key stored in {storage.authPath if hasattr(storage, 'authPath') else '~/.misaka/agent/auth.json'}")

    def _oauth(self, registry, provider: str) -> None:
        """The same login the chat's /login runs, with the browser steps printed instead of drawn."""
        async def ask(request) -> str:
            kind = getattr(request, "type", "text")
            if kind == "select":
                options = list(getattr(request, "options", []) or [])
                chosen = prompt_choice(str(getattr(request, "message", "")), [o.label for o in options])
                return options[chosen].id
            return prompt(str(getattr(request, "message", "")), password=(kind == "secret"))

        def notify(event) -> None:
            kind = getattr(event, "type", "")
            if kind == "auth_url":
                ui.print_info(f"Open this URL in your browser: {event.url}")
                if getattr(event, "instructions", None):
                    ui.print_info(str(event.instructions))
            elif kind == "device_code":
                ui.print_info(f"Go to {event.verificationUri} and enter the code {event.userCode}")
            else:
                ui.print_info(str(getattr(event, "message", "")))

        try:
            asyncio.run(registry.login(provider, "oauth", SimpleNamespace(signal=None, prompt=ask, notify=notify)))
            ui.print_success(f"{provider}: signed in")
        except (SetupCancelled, SetupGoBack):
            raise
        except Exception as error:  # noqa: BLE001 - a failed login is reported, the wizard goes on
            ui.print_error(f"Login failed: {error}")

    def _pick_model(self, registry, provider: str, cfg: dict) -> str:
        models = [model for model in registry.getAll() if model.provider == provider]
        ids = [model.id for model in models]
        if not ids:
            return prompt("Model id", cfg["default_model"])
        current = cfg["default_model"] if cfg["provider"] == provider else None
        default = ids.index(current) if current in ids else 0
        labels = [f"{model.id}" + (f"  — {model.name}" if model.name and model.name != model.id else "") for model in models]
        return ids[prompt_choice(f"Default model for {provider}", labels, default)]

    def _verify(self, registry, provider: str, model_id: str) -> None:
        if not prompt_yes_no("Send one tiny request now to confirm the credential works?", True):
            return
        from misaka.ai.stream import complete_simple
        from misaka.ai.types import Context, SimpleStreamOptions, UserMessage

        async def ping() -> str:
            model = registry.find(provider, model_id)
            if model is None:
                raise RuntimeError(f"{provider}/{model_id} is not in the model catalog")
            auth = await registry.getApiKeyAndHeaders(model)
            if not auth.get("ok"):
                raise RuntimeError(auth.get("error") or "no credential resolved")
            reply = await complete_simple(
                model, Context(messages=[UserMessage(content="Reply with the single word OK.", timestamp=0)]),
                SimpleStreamOptions(apiKey=auth.get("apiKey"), headers=auth.get("headers"), maxTokens=16))
            if reply.stopReason == "error":
                raise RuntimeError(reply.errorMessage or "request failed")
            return "".join(getattr(part, "text", "") for part in reply.content)
        try:
            text = asyncio.run(ping())
            ui.print_success(f"{provider} answered: {text.strip()[:40] or '(empty reply)'}")
            self.state["verified"] = True
        except Exception as error:  # noqa: BLE001 - the wizard reports and continues
            ui.print_error(f"The request failed: {error}")
            ui.print_info("Check the key (or `misaka auth check`) and run `misaka setup model` again.")
            self.state["verified"] = False

    # -- 3. sisters ---------------------------------------------------------------------------

    def sisters(self) -> None:
        from misaka.core.network import roster
        ui.print_header("Sisters")
        existing = roster.roster_names()
        if existing:
            ui.print_success(f"Registered: {', '.join(existing)}")
        else:
            ui.print_info("No Sister yet. Last Order hands research cards to Sisters; without at least one,",
                          "a research run has nobody to send out. Two is a good start (one can red-team the other).")
        if existing and not prompt_yes_no("Add another Sister?", False):
            return
        next_id = 10032
        while any(str(next_id) == name for name in existing):
            next_id += 1
        while True:
            sid = prompt("Sister ID", str(next_id))
            specialty = prompt("Her specialty, for Last Order's routing (optional)", "")
            ok, message = roster.create_sister(sid, specialty=specialty or None)
            (ui.print_success if ok else ui.print_error)(message)
            if ok:
                existing.append(sid)
                next_id = int(sid) + 1 if sid.isdigit() else next_id + 1
            if not prompt_yes_no("Add another?", len(existing) < 2):
                break
        self.state["sisters"] = existing

    # -- 4. documents -------------------------------------------------------------------------

    def documents(self) -> None:
        from misaka.core.documents.pageindex import available as pageindex_available
        ui.print_header("Documents")
        office = self._importable("docx", "openpyxl", "pptx")
        ui.print_check(office, "office documents", "python-docx / openpyxl / python-pptx" if office else
                       "python-docx, openpyxl, python-pptx missing: reinstall misaka (they are dependencies)")
        has_outline = pageindex_available()
        ui.print_check(has_outline, "PDF outlines", "PageIndex extra installed" if has_outline else
                       "PageIndex extra not installed: PDFs are indexed page by page, without an outline")
        if not has_outline and prompt_yes_no("Install the PDF outline extra now? (recommended for PDF-heavy research)", True):
            self._pip_install(list(PAGEINDEX_PACKAGES))
            has_outline = pageindex_available()
        ocr = shutil.which("ocrmypdf") is not None
        ui.print_check(True if ocr else None, "OCR", "ocrmypdf present" if ocr else
                       f"scanned PDFs need ocrmypdf: {_install_command('ocrmypdf')}")
        self.state["office"], self.state["pageindex"] = office, has_outline

    # -- 5. web -------------------------------------------------------------------------------

    def web(self) -> None:
        from misaka.core.web import config
        ui.print_header("Web search")
        current = config.web_config()
        backend = current.get("search_backend") or current.get("backend")
        ui.print_info("Web search works with no configuration: a keyless vendor ring answers searches.",
                      "A pinned backend with your own key is steadier and faster.")
        if backend:
            ui.print_success(f"Pinned backend: {backend}")
        names = ["Keep the keyless ring only"] + [name for name, _env in WEB_BACKENDS]
        default = 1 + [n for n, _e in WEB_BACKENDS].index(backend) if backend in dict(WEB_BACKENDS) else 0
        choice = prompt_choice("Search backend", names, default)
        if choice == 0:
            if backend and prompt_yes_no(f"Unpin {backend} and use the ring only?", False):
                config.unset_config("backend")
                config.unset_config("search_backend")
            self.state["web"] = "keyless ring"
            return
        name, env = WEB_BACKENDS[choice - 1]
        label = "SearXNG instance URL" if env == "SEARXNG_URL" else f"{name} API key ({env})"
        value = prompt(label, password=(env != "SEARXNG_URL"))
        changes = {"backend": name}
        if value:
            changes[f"env.{env}"] = value
        path = config.update_config(changes)
        ui.print_success(f"{name} saved in {path}")
        self.state["web"] = name

    # -- 6. project ---------------------------------------------------------------------------

    def project(self) -> None:
        from misaka.core.platform import cards
        ui.print_header("Project folder")
        ui.print_info("A project is a folder MISAKA works in: a git repository with PROJECT.md and cards/.",
                      "Research products land in it, and accepted results are committed there.")
        folder = os.path.expanduser(prompt("Folder to initialize (Enter for the current one)", os.getcwd()))
        if not os.path.isdir(folder):
            if not prompt_yes_no(f"{folder} does not exist. Create it?", True):
                return
            os.makedirs(folder, exist_ok=True)
        try:
            for line in cards.init_project(folder):
                ui.print_info(line)
            ui.print_success(f"Project ready: {folder}")
            self.state["project"] = folder
        except RuntimeError as error:
            ui.print_error(str(error))

    # -- summary ------------------------------------------------------------------------------

    def summary(self) -> None:
        from misaka.core.network import roster
        state = self.state
        ui.print_banner("✓ Setup complete")
        ui.print_check(bool(state.get("provider")), "model",
                       f"{state.get('provider')} / {state.get('model')}" + ("" if state.get("verified", True) else "  (request failed)")
                       if state.get("provider") else "run `misaka setup model`")
        names = roster.roster_names()
        ui.print_check(bool(names), "sisters", ", ".join(names) if names else "none: run `misaka setup sisters`")
        for binary in ("git", "rg", "fd", "pdftotext"):
            ui.print_check(shutil.which(binary) is not None, binary, "" if shutil.which(binary) else _install_command(binary))
        ui.print_check(state.get("pageindex", None), "PDF outlines", "" if state.get("pageindex") else "optional")
        ui.print_check(state.get("web") is not None, "web search", str(state.get("web") or "keyless ring"))
        ui.print_check(bool(state.get("project")), "project", state.get("project") or "run `misaka init` in a folder")
        ui.print_info("", "Next:",
                      f"  cd {state.get('project') or '<project>'} && misaka      the panel (Last Order, the Sisters, the board)",
                      "  misaka chat                                  plain chat with Last Order",
                      "  misaka setup <section>                       revisit one section: " + " | ".join(SECTIONS),
                      "  misaka auth check                            credentials, per provider", "")

    # -- helpers ------------------------------------------------------------------------------

    @staticmethod
    def _importable(*modules: str) -> bool:
        import importlib.util
        try:
            return all(importlib.util.find_spec(module) is not None for module in modules)
        except (ImportError, ValueError):
            return False

    @staticmethod
    def _pip_install(packages: list[str], *, from_checkout: bool = False) -> None:
        """Install into this interpreter. ``misaka[...]`` extras are only meaningful from a
        checkout (PyPI's ``misaka`` is an unrelated package), so those are installed as
        ``.[extra]`` from the current directory when it is one, and printed otherwise."""
        if from_checkout:
            if os.path.isfile("pyproject.toml"):
                packages = [p.replace("misaka[", ".[") for p in packages]
            else:
                ui.print_info("Run from the misaka checkout: " + " ".join(f"pip install '{p.replace('misaka[', '.[')}'" for p in packages))
                return
        command = [sys.executable, "-m", "pip", "install", *packages]
        ui.print_info(color_dim(" ".join(command)))
        try:
            result = subprocess.run(command, check=False)
        except OSError as error:
            ui.print_error(f"pip could not start: {error}")
            return
        (ui.print_success if result.returncode == 0 else ui.print_error)(
            "installed" if result.returncode == 0 else f"pip exited with {result.returncode}")


def color_dim(text: str) -> str:
    return ui.color(text, ui.DIM)


def configured_anywhere() -> bool:
    """Whether the default provider has a credential: the test bare ``misaka`` runs to decide
    whether to open the wizard first."""
    try:
        from misaka.cli.auth import _create_runtime
        from misaka.config import current_config
        registry = _create_runtime(read_only=True).registry
        status = registry.getProviderAuthStatus(current_config()["provider"])
        return bool(status.configured or status.source)
    except Exception:  # noqa: BLE001 - an unreadable store is not a reason to force the wizard
        return True


def run(section: str | None = None) -> int:
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("misaka setup needs a terminal. Without one, configure by hand:\n"
              "  export ANTHROPIC_API_KEY=...   (or another provider's key)\n"
              "  misaka create 10032           (a Sister)\n"
              "  misaka init                   (inside the project folder)\n"
              "  misaka auth check", file=sys.stderr)
        return 1
    wizard = Wizard()
    steps = [("Environment", wizard.environment), ("Model & Provider", wizard.model), ("Sisters", wizard.sisters),
             ("Documents", wizard.documents), ("Web search", wizard.web), ("Project", wizard.project)]
    by_key = dict(zip(SECTIONS, steps, strict=True))
    try:
        if section:
            if section not in by_key:
                ui.print_error(f"Unknown section {section!r}; one of: {', '.join(SECTIONS)}")
                return 2
            label, action = by_key[section]
            ui.print_logo(f"Setup · {label}")
            ui.run_steps([(label, action)])
            ui.print_success(f"{label} done.")
            return 0
        ui.print_logo("Setup", "Configure this install: model, Sisters, documents, web, project.",
                      "Enter keeps a current value · ← previous section · Esc or Ctrl+C exits.")
        if configured_anywhere():
            ui.print_info("", "A provider is already configured: each prompt shows the current value.")
        ui.run_steps(steps)
        wizard.summary()
        return 0
    except SetupCancelled:
        print()
        ui.print_info("Setup cancelled. Sections already completed were saved; the rest were not changed.")
        return 130
