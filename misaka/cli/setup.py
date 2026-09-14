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
- skills: installs ``DEFAULT_SKILLS`` from the optional catalog that ships in the package,
  into the shared layer every role reads. Nothing else is installed by default.
- documents: the optional PDF outline extra and the office libraries, checked by import.
- web: ``~/.misaka/web.json`` -- a pinned search backend and its key, or the keyless ring.
- research: nothing; it explains the shape of a run and the approval gate, which is the one
  behaviour a first run meets without warning.
- project: ``misaka init`` on a folder, and optionally a first pass of ``misaka doc`` over a
  folder of sources.

Bare ``misaka`` runs the wizard by itself when no credential is configured anywhere.
"""
from __future__ import annotations

import asyncio
import os
import platform
import shutil
import subprocess
import sys
import time
from types import SimpleNamespace

from misaka.cli import setup_ui as ui
from misaka.cli.setup_ui import (
    SetupCancelled,
    SetupGoBack,
    prompt,
    prompt_choice,
    prompt_yes_no,
)

SECTIONS = ("environment", "model", "sisters", "skills", "documents", "web", "research", "project")

# The providers a fresh install is most likely to want, in the order they are offered; every
# other provider the registry knows is one menu entry further ("Another provider...").
#
# Ordered so the ones that sign in through a browser come first. A subscription is the path
# that needs no API key at all, and it used to be the buried one: `openai` was featured and
# takes only a key, while ChatGPT Plus/Pro lives under a different provider id
# (`openai-codex`) that only the second menu reached -- so the person with a subscription had
# further to walk than the person with a credit card.
FEATURED_PROVIDERS = ("anthropic", "openai-codex", "github-copilot", "xai", "openrouter",
                      "openai", "google", "mistral", "amazon-bedrock", "ollama")

# Web search backends that take a key, and the variable the backend reads it from.
WEB_BACKENDS = (
    ("tavily", "TAVILY_API_KEY"), ("brave-free", "BRAVE_SEARCH_API_KEY"), ("exa", "EXA_API_KEY"),
    ("firecrawl", "FIRECRAWL_API_KEY"), ("perplexity", "PERPLEXITY_API_KEY"), ("parallel", "PARALLEL_API_KEY"),
    ("keenable", "KEENABLE_API_KEY"), ("xai", "XAI_API_KEY"), ("searxng", "SEARXNG_URL"),
)

# The skills a research install wants out of the box, from the optional catalog under
# ``core/skills/assets/optional/``. Everything else in that catalog (140 skills across 24
# categories) ships too and stays uninstalled until somebody asks for it by name.
DEFAULT_SKILLS = ("coverage-maps",)

PAGEINDEX_PACKAGES = ("PyPDF2==3.0.1", "pypdfium2==4.30.0", "regex>=2024.0.0", "sortedcontainers==2.4.0")

# PyPI's ``misaka`` is an unrelated package, so a ``misaka[extra]`` requirement only resolves
# from a checkout or straight from the repository -- the address the README installs from.
REPO_URL = "git+https://github.com/Luciole-Studio/Misaka-Agent.git"


def _requirement(package: str) -> str:
    """``misaka[anthropic]`` as something pip can actually resolve; anything else unchanged."""
    return f"{package} @ {REPO_URL}" if package == "misaka" or package.startswith("misaka[") else package


def _open_browser(url: str) -> bool:
    """Hand the URL to the desktop, the way the chat's login dialog does.

    Printing it and waiting is what made the browser logins look like a hang: the flow says
    "a browser window should open" because whoever drives it is expected to open one.
    """
    if sys.platform == "darwin":
        command = ["open", url]
    elif sys.platform == "win32":
        # Never `cmd /c start`: cmd re-parses &, | and ^ before `start` runs, so a
        # provider-supplied URL could execute. rundll32 takes the target unparsed.
        command = ["rundll32", "url.dll,FileProtocolHandler", url]
    else:
        command = ["xdg-open", url]
    try:
        with open(os.devnull, "wb") as sink:
            subprocess.Popen(command, stdout=sink, stderr=sink, start_new_session=True)
    except OSError:
        return False
    return True


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
                       "no build for this platform; `misaka` opens plain chat, everything else works")
        if not has_panel:
            ui.print_info(f"    looked in {library}",
                          "    macOS and Linux on x86_64 and arm64 ship a build; point MISAKA_GHOSTTY_VT",
                          "    at your own build of ghostty's libghostty-vt to use the panel elsewhere.")
        self.state["missing"] = missing
        self.state["panel"] = has_panel
        if "git" in missing:
            ui.print_warning("Without git the project step below cannot create a repository. Install it and run "
                             "`misaka setup project` afterwards.")
        if missing:
            ui.print_info("", "Install what is missing in another terminal; the wizard goes on regardless.")
        # Named, not checked: a network call here would make the wizard hang on a bad
        # connection, and nothing about MISAKA polls for updates on its own.
        ui.print_info("", ui.color("  `misaka update` says whether this install is behind the repository.", ui.DIM))

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
        oauth_ids = {p.id for p in registry.getOAuthProviders()}
        labels = [*self._provider_labels(registry, featured, oauth_ids), "Another provider..."]
        default = featured.index(current) if current in featured else 0
        picked = prompt_choice("Which provider should Last Order and the Sisters use by default?", labels, default,
                               "A browser sign-in uses a subscription you already pay for; an API key is billed per token.")
        if picked == len(featured):
            others = [p for p in known if p not in featured]
            provider = others[prompt_choice("Provider", self._provider_labels(registry, others, oauth_ids))]
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
    def _provider_labels(registry, providers: list[str], oauth_ids: set[str]) -> list[str]:
        """The menu rows: who they are, how you sign in, and whether you already have.

        The name column is measured, not guessed: "ChatGPT Plus/Pro (Codex Subscription)" is
        half again as long as "OpenAI", and a fixed width either wraps it or wastes a third
        of the row on everything else.
        """
        names = {p: registry.getProviderDisplayName(p) for p in providers}
        width = max((len(name) for name in names.values()), default=0)
        rows = []
        for provider in providers:
            status = registry.getProviderAuthStatus(provider)
            how = "browser sign-in" if provider in oauth_ids else "API key"
            where = status.source or ("configured" if status.configured else "")
            rows.append(f"{names[provider]:<{width}}  {how:<16}"
                        + (f"({where})" if where else "(not set up yet)"))
        return rows

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
        """The same login the chat's /login runs, drawn as terminal lines.

        Every prompt runs on a worker thread. Browser logins (Anthropic, ChatGPT Codex,
        OpenRouter) start a local callback server and race it against a paste-the-code
        prompt, both on the event loop; a prompt that reads the terminal directly blocks that
        loop, so the redirect the browser sends can never be accepted and the login hangs
        with the URL on screen. The chat does not hit this because its dialogs are already
        asynchronous.

        When the server wins, the paste prompt is still waiting on a keystroke, so it has to
        be cancellable -- otherwise the thread holds stdin and answers the wizard's next
        question.
        """
        import threading
        cancel = threading.Event()

        async def ask(request) -> str:
            kind = getattr(request, "type", "text")
            message = str(getattr(request, "message", ""))
            if kind == "select":
                options = list(getattr(request, "options", []) or [])
                chosen = await asyncio.to_thread(prompt_choice, message, [o.label for o in options])
                return options[chosen].id
            if kind == "manual_code":
                return await asyncio.to_thread(ui.prompt_cancellable, message, cancel)
            return await asyncio.to_thread(prompt, message, None, password=(kind == "secret"))

        def notify(event) -> None:
            kind = getattr(event, "type", "")
            if kind == "auth_url":
                ui.print_info(f"Opening your browser: {event.url}")
                if getattr(event, "instructions", None):
                    ui.print_info(str(event.instructions))
                if not _open_browser(str(event.url)):
                    ui.print_warning("The browser could not be opened; copy the URL above.")
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
        finally:
            # Release a paste prompt the callback server beat, and give its thread the moment
            # it needs to restore the terminal before the next question is asked.
            cancel.set()
            time.sleep(0.25)

    def _pick_model(self, registry, provider: str, cfg: dict) -> str:
        models = self._model_choices([m for m in registry.getAll() if m.provider == provider],
                                     cfg["default_model"] if cfg["provider"] == provider else None)
        ids = [model.id for model in models]
        if not ids:
            return prompt("Model id", cfg["default_model"])
        current = cfg["default_model"] if cfg["provider"] == provider else None
        default = ids.index(current) if current in ids else 0
        labels = []
        for index, model in enumerate(models):
            name = f"  — {model.name}" if model.name and model.name != model.id else ""
            labels.append(model.id + name + ("   (this install's default)" if index == default else ""))
        return ids[prompt_choice(
            f"Default model for {provider}", labels, default,
            "Last Order plans and writes with this; the Sisters use it too unless you pin theirs below.")]

    @staticmethod
    def _model_choices(models: list, current: str | None) -> list:
        """The catalog with dated aliases folded away. Anthropic alone lists both
        ``claude-sonnet-4-5`` and ``claude-sonnet-4-5-20250929``; a first run should not have to
        work out that those are one model. A dated id survives only when its undated form is
        absent from the catalog, or when it is the one already configured."""
        known = {model.id for model in models}

        def dated_alias(model_id: str) -> bool:
            head, _, tail = model_id.rpartition("-")
            return len(tail) == 8 and tail.isdigit() and head in known

        kept = [model for model in models if model.id == current or not dated_alias(model.id)]
        return kept or list(models)

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
        from misaka.config import CFG, profiles
        from misaka.core.network import roster
        ui.print_header("Roles")
        roles_root = os.path.expanduser(CFG["roles_root"])
        # The identity files shape every run and are seeded silently on first start, so a
        # wizard that never names them leaves the most useful edit undiscoverable.
        ui.print_info("Two roles do the work, and both read plain files you can edit:",
                      f"  {ui.tilde(profiles.shared_soul())}",
                      "      the shared identity every role loads first",
                      f"  {ui.tilde(os.path.join(roles_root, 'last_order'))}/",
                      "      Last Order, the coordinator: config.yaml for MCP servers, skills/ for her skills",
                      f"  {ui.tilde(os.path.join(roles_root, 'sisters'))}/<id>/",
                      "      one folder per Sister: DESCRIBE.md routes work to her, SOUL.md is her voice", "")
        existing = roster.roster_names()
        if existing:
            ui.print_success(f"Registered Sisters: {', '.join(existing)}")
        else:
            ui.print_info("No Sister yet. Last Order hands research cards to Sisters; without at least one,",
                          "a research run has nobody to send out. Two is a good start (one can red-team the other).")
        if existing and not prompt_yes_no("Add another Sister?", False):
            return
        model = self._sister_model()
        next_id = 10032
        while any(str(next_id) == name for name in existing):
            next_id += 1
        while True:
            sid = prompt("Sister ID", str(next_id))
            specialty = prompt("Her specialty, for Last Order's routing (optional)", "")
            ok, message = roster.create_sister(sid, specialty=specialty or None, model=model)
            (ui.print_success if ok else ui.print_error)(message)
            if ok:
                existing.append(sid)
                next_id = int(sid) + 1 if sid.isdigit() else next_id + 1
            if not prompt_yes_no("Add another?", len(existing) < 2):
                break
        self.state["sisters"] = existing

    def _sister_model(self) -> str | None:
        """A model pinned into each new Sister's ``config.json``, or None to follow the default.

        Sisters do the reading and the legwork while Last Order plans and writes the
        conclusion, so running them on a cheaper model is the common shape of a real install;
        ``create_sister`` has always taken the argument, and nothing offered it."""
        from misaka.cli.auth import _create_runtime
        from misaka.config import current_config
        cfg = current_config()
        default_label = f"The same as Last Order ({cfg['provider']} / {cfg['default_model']})"
        if prompt_choice("Which model should the Sisters run?", [default_label, "Pin a different one"], 0,
                         "Sisters read and gather; Last Order plans and writes. A cheaper model here is normal.") == 0:
            return None
        try:
            registry = _create_runtime(read_only=True).registry
        except Exception as error:  # noqa: BLE001 - a registry that will not open is not worth failing the section for
            ui.print_warning(f"The model catalog could not be read ({error}); the Sisters follow the default.")
            return None
        models = self._model_choices([m for m in registry.getAll() if m.provider == cfg["provider"]], None)
        if not models:
            return prompt("Model id for the Sisters", "") or None
        labels = [m.id + (f"  — {m.name}" if m.name and m.name != m.id else "") for m in models]
        return models[prompt_choice(f"Model for the Sisters ({cfg['provider']})", labels, 0)].id

    # -- 4. skills ----------------------------------------------------------------------------

    def skills(self) -> None:
        from misaka.config import CFG
        from misaka.core.skills import layers as skill_layers
        ui.print_header("Skills")
        roles_root = os.path.expanduser(CFG["roles_root"])
        shared = os.path.join(roles_root, "skills")
        shown = ((f"{ui.tilde(roles_root)}/<role>/skills/", "that role alone"),
                 (ui.tilde(shared), "every role; the wizard installs here"),
                 ("~/.agents/skills", "shared with your other agent tools, read-only"))
        column = max(len(path) for path, _purpose in shown)
        ui.print_info("A skill is a folder with a SKILL.md a role reads when the work calls for it.",
                      "Three layers, in the order a role sees them:",
                      *[f"  {path:<{column}}   {purpose}" for path, purpose in shown], "")
        external = os.path.expanduser("~/.agents/skills")
        if os.path.isdir(external):
            names = sorted(entry.name for entry in os.scandir(external)
                           if entry.is_dir() and not entry.name.startswith("."))
            ui.print_success(f"~/.agents/skills is mounted: {', '.join(names) if names else 'empty'}")
        else:
            ui.print_info(ui.color("  ~/.agents/skills is absent; it appears by itself once another "
                                   "agent tool creates it.", ui.DIM))
        installed = self._installed_skills(skill_layers, shared)
        wanted = [name for name in DEFAULT_SKILLS if name not in installed]
        if not wanted:
            ui.print_success(f"Already installed: {', '.join(DEFAULT_SKILLS)}")
        elif prompt_yes_no(f"Install {', '.join(wanted)}? (maps of a field, scanned before planning "
                           f"and again when red-teaming)", True):
            for name in wanted:
                self._install_skill(name)
        ui.print_info("", "The package also carries an optional catalog nothing installs by itself:",
                      "  misaka skills optional-list                 what ships, by category",
                      "  misaka skills hub-install <name>            install one",
                      "  misaka skills pending / approve <id>        the review step each install goes through")

    @staticmethod
    def _installed_skills(skill_layers, shared: str) -> set[str]:
        """The skill names already present in the shared layer, category folders included."""
        try:
            return {path.parent.name for path in skill_layers.iter_skill_files(shared)}
        except (OSError, ValueError):
            return set()

    def _install_skill(self, name: str) -> None:
        """Install one catalog skill into the shared layer, then approve it.

        Two calls because that is the design: a distribution write is scanned and staged, and
        a person approves it (``misaka skills pending`` / ``approve``). The person is standing
        at this prompt and just said yes, so the wizard completes the round trip instead of
        leaving a pending item behind -- and only ever for a skill that ships inside the
        package, never something fetched from a hub.

        The shared layer is addressed by passing the roles root as the profile: a scope's skill
        directory is ``<profile>/skills``, which for the roles root is the shared layer itself.
        """
        from misaka.config import CFG
        from misaka.core.skills import manage as skill_manage
        from misaka.core.skills import write as skill_write
        from misaka.core.skills.operations import execute
        roles_root = os.path.expanduser(CFG["roles_root"])
        try:
            result = execute("hub-install", name, profile_dir=roles_root, workspace=os.getcwd(),
                             source="official", category="", force=False, restore=False, repo="",
                             dry_run=False, bundled_root=None, optional_root=None, expected_digest=None)
        except Exception as error:  # noqa: BLE001 - one skill is not worth failing the wizard for
            ui.print_error(f"{name}: {error}")
            return
        pending = result.get("pending_id")
        if result.get("success") and not pending:
            ui.print_success(f"{name} installed.")
            return
        if not pending:
            ui.print_error(f"{name}: {result.get('error') or 'the install did not complete'}")
            if "skill_write_mode" in str(result.get("error") or ""):
                ui.print_info("Skill writing is off; `misaka skills mode ask` turns the review queue back on.")
            return
        record = skill_write.get_pending(pending)
        if record is None:
            ui.print_error(f"{name}: the staged write could not be read back.")
            return
        applied = skill_manage.apply_pending(record)
        if not applied.get("success"):
            ui.print_error(f"{name}: approval failed: {applied.get('error')}")
            ui.print_info(f"It is still queued: `misaka skills pending` and `misaka skills approve {pending}`.")
            return
        skill_write.discard_pending(record.get("_pending_file_id") or record["id"])
        verdict = (applied.get("scan") or {}).get("verdict")
        ui.print_success(f"{name} installed into the shared layer"
                         + (f" (scanned: {verdict})" if verdict else "")
                         + f" -- {ui.tilde(str(applied.get('path') or ''))}")
        self.state.setdefault("skills", []).append(name)

    # -- 5. documents -------------------------------------------------------------------------

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

    # -- 6. web -------------------------------------------------------------------------------

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
        if config.has_env(env):
            ui.print_success(f"{env} is already set; Enter keeps it.")
        value = prompt(label, password=(env != "SEARXNG_URL"))
        if not value and not config.has_env(env):
            # Pinning without a credential is worse than not pinning: the resolver takes an
            # explicit backend "ignoring availability", so every later search fails on the
            # missing key instead of falling back. A zero-config install that worked would
            # come out of this wizard broken, under a line that said it had been saved.
            ui.print_warning(f"No {env} given, so {name} was not pinned: the keyless ring stays in charge.")
            ui.print_info(f"Add it later with `misaka web set env.{env} <value>` and `misaka web set backend {name}`.")
            self.state["web"] = "keyless ring"
            return
        changes = {"backend": name}
        if value:
            changes[f"env.{env}"] = value
        path = config.update_config(changes)
        ui.print_success(f"{name} saved in {path}")
        self.state["web"] = name

    # -- 7. research --------------------------------------------------------------------------

    def research(self) -> None:
        """Nothing to configure, everything to say. A run's limits are per-run flags with
        defaults in code, and the approval gate is an environment variable, so this section
        reports rather than writes -- but a first run walks into the gate within minutes, and
        a wizard that never mentions it is where the "it just stopped" reports come from."""
        from misaka.config import CFG
        from misaka.core.research import runs
        ui.print_header("How a research run behaves")
        limits = runs.DEFAULT_LIMITS
        ui.print_info("Last Order turns your question into a plan, the plan into cards, and hands the",
                      "cards to the Sisters. Per run, unless you pass the flags:", "")
        ui.print_check(True, "parallel cards", f"{limits['parallel']}    (`--parallel`)")
        ui.print_check(True, "follow-up rounds", f"{limits['max_followups']}    extra rounds a node may run before concluding (`--followups`)")
        ui.print_check(True, "sub-question depth", f"{limits['max_depth']}    (`--depth`)")
        gate = bool(CFG.get("research_plan_approval", True))
        ui.print_check(gate, "plan approval",
                       "every node's plan waits for you, in a conversation with Last Order; she starts the "
                       "run herself once you agree" if gate else
                       "off (MISAKA_RESEARCH_PLAN_APPROVAL=0): plans run unattended")
        if gate:
            ui.print_info("", "So a run pauses and talks to you before it spends anything. There is no approve",
                          "command and no keyword: you discuss the plan and she goes when you are satisfied.",
                          "Set MISAKA_RESEARCH_PLAN_APPROVAL=0 for unattended runs.")
        self.state["approval"] = gate

    # -- 8. project ---------------------------------------------------------------------------

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
            return
        self._index_sources(folder)

    def _index_sources(self, folder: str) -> None:
        """`misaka doc add` is step two of the quickstart and the whole point of the document
        stack; the wizard used to check that the libraries imported and stop there."""
        ui.print_info("", "Research reads an indexed corpus: PDFs, Office files and text you have already",
                      "collected. Indexing extracts their text (and, with the PageIndex extra, an outline)",
                      "so a Sister can cite a page instead of guessing.")
        if not prompt_yes_no("Index a folder of sources into this project now?", False):
            ui.print_info(f"Later: cd {folder} && misaka doc scan <folder>")
            return
        source = os.path.expanduser(prompt("Folder to index", os.path.join(folder, "sources")))
        if not os.path.isdir(source):
            ui.print_error(f"{source} is not a folder; skipping. Run `misaka doc scan <folder>` when it exists.")
            return
        from misaka.core.documents import index as corpus
        ui.print_info(f"Indexing {source}... (this reads every file; large corpora take a while)")
        try:
            ingested, skipped = corpus.scan(source, workspace=folder)
        except Exception as error:  # noqa: BLE001 - one unreadable corpus does not fail the wizard
            ui.print_error(f"Indexing failed: {error}")
            return
        for path, reason in skipped[:5]:
            ui.print_info(ui.color(f"    skipped {os.path.basename(path)}: {reason}", ui.DIM))
        if len(skipped) > 5:
            ui.print_info(ui.color(f"    ... and {len(skipped) - 5} more skipped", ui.DIM))
        (ui.print_success if ingested else ui.print_warning)(
            f"{len(ingested)} document(s) indexed, {len(skipped)} skipped."
            if ingested else f"Nothing was indexed; all {len(skipped)} candidate(s) were skipped.")
        self.state["documents"] = len(ingested)

    # -- summary ------------------------------------------------------------------------------

    def summary(self) -> None:
        from misaka.core.network import roster
        state = self.state
        ui.print_banner("✓ Setup complete")
        # `verified` is None when the check was declined and False when the request failed.
        # Ticking on "a provider was chosen" reported a broken key as a finished step, which is
        # the one line of this screen a person actually reads.
        verified = state.get("verified")
        ui.print_check(bool(state.get("provider")) and verified is not False, "model",
                       f"{state.get('provider')} / {state.get('model')}"
                       + ("  (the test request failed; the key is stored but does not work)" if verified is False else
                          "  (not tested)" if verified is None else "")
                       if state.get("provider") else "run `misaka setup model`")
        names = roster.roster_names()
        ui.print_check(bool(names), "sisters", ", ".join(names) if names else "none: run `misaka setup sisters`")
        for binary in ("git", "rg", "fd", "pdftotext"):
            ui.print_check(shutil.which(binary) is not None, binary, "" if shutil.which(binary) else _install_command(binary))
        ui.print_check(state.get("pageindex", None), "PDF outlines", "" if state.get("pageindex") else "optional")
        # Web search works with no configuration at all, so this row is never a failure: the
        # only question is whether a backend got pinned on top of the keyless ring.
        ui.print_check(True, "web search", str(state.get("web") or "keyless ring"))
        skills = state.get("skills")
        ui.print_check(bool(skills) if skills is not None else None, "skills",
                       ", ".join(skills) if skills else "none added: `misaka skills optional-list`")
        indexed = state.get("documents")
        ui.print_check(bool(indexed) if indexed is not None else None, "documents",
                       f"{indexed} indexed" if indexed else "none indexed yet: `misaka doc scan <folder>`")
        ui.print_check(bool(state.get("project")), "project", state.get("project") or "run `misaka init` in a folder")
        project = state.get("project") or "<project>"
        ui.print_info("", "Next:",
                      f"  cd {project} && misaka                       the panel (Last Order, the Sisters, the board)",
                      "  misaka doc scan <folder>                     index sources; research reads what is indexed",
                      '  misaka research "your question"              a run: plan, your go-ahead, cards, red team',
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
        """Install into this interpreter, or say what to run by hand when it cannot.

        ``misaka[...]`` extras become ``.[extra]`` inside a checkout; everywhere else they are
        printed against the repository URL, because telling someone who installed from git to
        run ``pip install '.[anthropic]'`` names a directory they do not have."""
        import importlib.util
        if from_checkout and os.path.isfile("pyproject.toml"):
            packages = [package.replace("misaka[", ".[") for package in packages]
        elif from_checkout:
            ui.print_info("Install it with:", *[f"  pip install '{_requirement(p)}'" for p in packages])
            return
        if importlib.util.find_spec("pip") is None:
            # Normal for `uv tool install` and pipx: the tool environment is managed, and
            # pip-installing into it is either impossible or undone by the next upgrade.
            ui.print_warning("This interpreter has no pip, which is how `uv tool` and pipx installs look.")
            ui.print_info("Add it through the tool that installed misaka, for example:",
                          *[f"  uv tool install --force '{_requirement(p)}'" for p in packages])
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
    steps = [("Environment", wizard.environment), ("Model & Provider", wizard.model), ("Roles", wizard.sisters),
             ("Skills", wizard.skills), ("Documents", wizard.documents), ("Web search", wizard.web),
             ("Research", wizard.research), ("Project", wizard.project)]
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
        ui.print_logo("Setup", "Configure this install: model, roles, documents, web, project.",
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
