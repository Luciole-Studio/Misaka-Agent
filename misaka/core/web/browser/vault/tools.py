"""Vault-backed model-blind browser autofill tools.

Five model-facing tools, gated on a configured CDP-capable browser and the
optional crypto dependency. The native registration layer owns availability.

- ``browser_vault_list``  → handles + metadata (for logins this includes the
  identifier — it is NOT a secret; the agent types it itself). Passwords are
  never returned.
- ``browser_vault_fill``  → server-side fill of the CURRENT page from a vault
  handle: the password field for logins, card fields for payment items (after
  the user confirms), address fields for address items. The secret is
  resolved locally, the page origin must EXACTLY match the item's bound
  origin (pre-checked AND re-asserted synchronously inside the fill script),
  the field is chosen by the ported login-control classifier, injection runs
  exclusively over the supervisor CDP WebSocket (never argv), and the tool
  result reports only ``{filled_fields, kind, origin, success}`` — the
  password never appears in tool results, logs, or the session DB, and its
  exact bytes are registered with the browser-result redaction boundary so
  no later browser tool call can echo them back to the model.

Ported design from Merit-Systems/OpenInstinct (MIT): opaque-handle vault
autofill (kernel-login-autofill.ts / fill_from_vault.ts).
"""

from __future__ import annotations

import json
import logging
import secrets
from typing import Any
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Availability check
# ---------------------------------------------------------------------------

def _check_vault_available() -> bool:
    from misaka.core.web.browser.settings import VAULT_TOOLS, available_tools
    return bool(set(VAULT_TOOLS) & available_tools())



# ---------------------------------------------------------------------------
# JS evaluation plumbing (server-side; results never carry secret values)
# ---------------------------------------------------------------------------

def _eval_js(task_id: str, expression: str) -> dict[str, Any]:
    from misaka.core.web.browser.vault.host import evaluate
    return evaluate(expression)



def _ensure_supervisor(task_id: str):
    from misaka.core.web.browser.vault.host import current_bridge
    return current_bridge()



def _eval_js_secret(task_id: str, expression: str) -> dict[str, Any]:
    # Both evaluations use the owned CDP socket. There is no CLI argv fallback.
    from misaka.core.web.browser.vault.host import evaluate
    return evaluate(expression)



def _parse_json_result(raw: Any) -> Any:
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return raw
    return raw


def _current_page_origin(task_id: str) -> str | None:
    res = _eval_js(task_id, "window.location.href")
    if not res.get("success"):
        return None
    href = str(res.get("result") or "").strip().strip('"').strip("'")
    from misaka.core.web.browser.vault.store import VaultError, normalize_origin

    try:
        if urlsplit(href).scheme not in {"http", "https"}:
            return None
        return normalize_origin(href)
    except (ValueError, VaultError):
        return None


# Per kind: a JS probe that is truthy on a tab holding the form this kind fills.
_TAB_PROBES = {
    "login": "!!document.querySelector('input[type=password]')",
    "payment": "!!document.querySelector('input[autocomplete^=cc-], [name*=card i], [placeholder*=card i], [name*=cvc i], [name*=cvv i]')",
    "address": "!!document.querySelector('input[autocomplete^=address-], [autocomplete=postal-code], [name*=address i], [name*=zip i], [name*=postal i]')",
}


def _focus_bound_origin(task_id: str, origin: str, kind: str) -> str | None:
    from misaka.core.web.browser.vault.host import focus_page
    return focus_page(origin, _TAB_PROBES.get(kind))



# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

def browser_vault_list() -> str:
    """List login handles + metadata across every enabled backend. Passwords are never included.

    A locked external manager contributes no items; instead it is reported under ``locked`` so the
    agent knows to call browser_vault_fill (which prompts the user to unlock) or tell the user.
    """
    from misaka.core.web.browser.vault.backends import enabled_backends
    from misaka.core.web.browser.vault.backends.unlock import can_prompt_here

    items, locked, errors = [], [], []
    for backend in enabled_backends():
        if backend.needs_unlock and not backend.is_unlocked():
            locked.append({"backend": backend.name, "display_name": backend.display_name,
                           "unlock": "browser_vault_unlock" if can_prompt_here() else "unavailable_in_this_session"})
            continue
        try:
            metas = backend.list_items()
        except Exception as exc:  # noqa: BLE001 - isolate backend failures at the metadata boundary
            errors.append({"backend": backend.name, "error": str(exc)[:200]})
            continue
        for meta in metas:
            entry = {"handle": meta.id, "backend": backend.name, "label": meta.label, "kind": meta.kind,
                     "origin": meta.origin, "available": meta.kind == "login" or bool(meta.origin)}
            if meta.has_otp or backend.needs_unlock:
                entry["two_factor"] = "automatic" if meta.has_otp else "automatic if the manager stores a TOTP seed, else the user is asked"
            if meta.identifier:
                entry["identifier"] = meta.identifier
                entry["identifier_type"] = meta.identifier_type
            items.append(entry)
    out: dict[str, Any] = {"success": True, "items": items}
    if not items:
        out["hint"] = ("No saved logins. On a login page, call browser_vault_save_login to ask the user to save one. "
                       "Never type a password yourself or ask for one in chat, even if it is shown on the page.")
    if locked:
        out["locked"] = locked
    if errors:
        out["errors"] = errors
    return json.dumps(out, ensure_ascii=False)


def browser_vault_unlock(backend_name: str) -> str:
    """Ask the user (via the surface's masked prompt) to unlock an external manager for this session."""
    from misaka.core.web.browser.vault.backends import enabled_backends
    from misaka.core.web.browser.vault.backends.unlock import (
        can_prompt_here,
        get_unlock_prompt_callback,
    )

    backend = next((b for b in enabled_backends() if b.name == backend_name and b.needs_unlock), None)
    if backend is None:
        return json.dumps({"success": False, "error": f"No unlockable vault backend named {backend_name!r}."})
    if backend.is_unlocked():
        return json.dumps({"success": True, "backend": backend.name, "already_unlocked": True})
    if not can_prompt_here():
        return json.dumps({"success": False, "error_type": "unlock_unavailable",
                           "error": (f"{backend.display_name} is locked and this session cannot prompt for the "
                                     "master password (headless/cron/API). Unlock it from an interactive MISAKA "
                                     "session first.")})
    prompt = get_unlock_prompt_callback()
    master = prompt(backend.name, backend.display_name) if prompt else ""
    if not master:
        return json.dumps({"success": False, "error_type": "unlock_cancelled",
                           "error": f"The user declined to unlock {backend.display_name}."})
    try:
        backend.unlock(master)  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001 - surface a sanitized manager failure
        return json.dumps({"success": False, "error_type": "unlock_failed", "error": str(exc)[:300]})
    finally:
        del master
    return json.dumps({"success": True, "backend": backend.name})


def browser_vault_save_login(label: str = "", task_id: str | None = None) -> str:
    """Ask the user (masked prompt on their surface) for the login of the CURRENT page, store it in the local
    vault bound to that origin, and fill the password at once. The values never enter the conversation."""
    from misaka.core.web.browser.vault.backends.unlock import (
        can_prompt_here,
        get_save_login_prompt_callback,
    )
    from misaka.core.web.browser.vault.store import get_vault_store

    effective_task_id = task_id or "default"
    # The supervisor's default page session is whatever tab it attached to first (on Browser Use that is
    # the daemon's blank tab); the login form lives in the tab with a password field, so focus that one.
    _focus_bound_origin(effective_task_id, "", "login")
    origin = _current_page_origin(effective_task_id)
    if not origin:
        return json.dumps({"success": False, "error": "Open the site's login page first; the login is saved for that page's origin."})
    prompt = get_save_login_prompt_callback()
    if prompt is None or not can_prompt_here():
        return json.dumps({"success": False, "error_type": "prompt_unavailable",
                           "error": (f"This session cannot ask the user for a login (headless/cron/API). Tell them to run "
                                     f"browser_vault_save_login in an interactive MISAKA session for {origin}.")})
    host = origin.split("://", 1)[-1]
    site = label.strip() or host
    answer = prompt(origin, host)  # the prompt names the site by host: the user recognises URLs, not agent labels
    if not answer or not answer.get("password") or not answer.get("identifier"):
        return json.dumps({"success": False, "error_type": "save_declined",
                           "error": "The user chose not to save a login for this site. Do not ask again this turn."})
    identifier = str(answer["identifier"]).strip()
    id_type = "email" if "@" in identifier else ("phone" if identifier.lstrip("+").isdigit() else "username")
    try:
        meta = get_vault_store().add_item("login", site, {"identifier_type": id_type, "identifier": identifier,
                                                        "password": str(answer["password"])}, origin=origin)
    except Exception as exc:  # noqa: BLE001 - report storage failure without credential values
        return json.dumps({"success": False, "error_type": "save_failed", "error": str(exc)[:200]})
    finally:
        answer.clear()
    filled = json.loads(browser_vault_fill(meta.id, task_id=effective_task_id))
    return json.dumps({"success": True, "handle": meta.id, "origin": origin, "identifier": identifier,
                       "identifier_type": id_type, "fill": filled,
                       "next": "Type the identifier into the username field if the form has one, then submit."},
                      ensure_ascii=False)


_TAB_PROBES["otp"] = ("!!document.querySelector('input[autocomplete=one-time-code], input[name*=otp i], input[name*=code i], "
                      "input[id*=otp i], input[id*=code i], input[name*=totp i], input[aria-label*=code i]')")


def browser_vault_enter_code(handle: str = "", task_id: str | None = None) -> str:
    """Second factor: fill the one-time code the CURRENT page asks for. If the saved login (``handle``) has an
    authenticator seed, the code is minted server-side and nobody is asked; otherwise the user is prompted on
    their surface for the code their phone/email/app shows. The code goes into the page over the supervisor
    socket and never enters the conversation."""
    from misaka.core.web.browser.vault.backends import backend_for_handle
    from misaka.core.web.browser.vault.backends.unlock import (
        can_prompt_here,
        get_code_prompt_callback,
    )
    from misaka.core.web.browser.vault.classifier import (
        LoginControl,
        build_fill_js,
        build_inspection_js,
        build_otp_fills,
        classify_otp_controls,
    )
    from misaka.core.web.browser.vault.host import register_vault_redaction_value

    effective_task_id = task_id or "default"
    _focus_bound_origin(effective_task_id, "", "otp")
    origin = _current_page_origin(effective_task_id)
    if not origin:
        return json.dumps({"success": False, "error": "No page with a code field is open."})
    site = origin.split("://", 1)[-1]

    nonce = secrets.token_hex(8)
    inspect = _eval_js(effective_task_id, build_inspection_js(nonce))
    raw_controls = _parse_json_result(inspect.get("result")) if inspect.get("success") else None
    if isinstance(raw_controls, str):
        raw_controls = _parse_json_result(raw_controls)
    otp_controls = classify_otp_controls([LoginControl.from_dict(r) for r in (raw_controls or []) if isinstance(r, dict)])
    if not otp_controls:
        return json.dumps({"success": False, "error_type": "no_code_field",
                           "error": ("No one-time-code field on the current page. If the site wants a passkey, hardware key or "
                                     "an approval tap in an app, tell the user to complete it on their device and wait for the page to move on.")})

    code: str | None = None
    source = "user"
    backend = backend_for_handle(handle) if handle else None
    if backend is not None:
        meta = backend.get_meta(handle)
        if meta is None or meta.origin != origin:
            return json.dumps({"success": False, "error_type": "origin_mismatch",
                               "error": "The OTP login handle is not bound to the current page origin."})
        try:
            code = backend.resolve_otp(handle)
        except Exception:  # noqa: BLE001 - an unavailable manager OTP falls back to the masked prompt
            code = None
        if code:
            source = backend.name
    if not code:
        prompt = get_code_prompt_callback()
        if prompt is None or not can_prompt_here():
            return json.dumps({"success": False, "error_type": "prompt_unavailable",
                               "error": (f"{site} asks for a one-time code and this session cannot ask the user (headless/cron/API). "
                                         "Save an authenticator key for this login so codes can be generated automatically.")})
        code = (prompt(site, "") or "").strip().replace(" ", "").replace("-", "")
        if not code:
            return json.dumps({"success": False, "error_type": "code_declined",
                               "error": "The user did not enter a code. Do not ask again this turn."})

    register_vault_redaction_value(code)
    fills = build_otp_fills(otp_controls, code)
    result = _eval_js_secret(effective_task_id, build_fill_js(fills, expected_origin=origin, nonce=nonce))
    del code
    if not result.get("success"):
        return json.dumps({"success": False, "error": str(result.get("error") or "fill failed")[:200]})
    parsed = _parse_json_result(result.get("result"))
    if isinstance(parsed, str):
        parsed = _parse_json_result(parsed)
    if isinstance(parsed, dict) and parsed.get("refused") == "origin_changed":
        return json.dumps({"success": False, "error_type": "origin_changed", "error": "The page navigated before the code could be entered. Nothing was written."})
    filled = int(parsed.get("filled", 0)) if isinstance(parsed, dict) else 0
    return json.dumps({"success": bool(filled), "filled_fields": filled, "origin": origin, "source": source,
                       "next": "Submit the form (many sites auto-submit when the last digit lands)."})


def browser_vault_fill(handle: str, task_id: str | None = None) -> str:
    """Fill the current page's password field from a vault handle.

    Password-only: the identifier is agent-visible metadata (see
    browser_vault_list) and is typed by the agent via normal input tools.
    The password is resolved server-side and injected via in-page JS over
    the supervisor CDP WebSocket; the result reports only counts/metadata.
    """
    from misaka.core.web.browser.vault.backends import (
        UnlockRequired,
        backend_for_handle,
    )
    from misaka.core.web.browser.vault.classifier import (
        ClassifiedLoginControl,
        LoginControl,
        build_fill_js,
        build_inspection_js,
        classify_checkout_control,
        classify_login_control,
        select_checkout_fills,
        select_password_fill,
    )
    from misaka.core.web.browser.vault.host import register_vault_redaction_value
    from misaka.core.web.browser.vault.store import (
        ADDRESS_FIELDS,
        PAYMENT_FIELDS,
        scrub_secret_from_text,
    )

    effective_task_id = task_id or "default"
    backend = backend_for_handle(handle)
    if backend is not None and backend.needs_unlock and not backend.is_unlocked():
        unlocked = json.loads(browser_vault_unlock(backend.name))
        if not unlocked.get("success"):
            return json.dumps(unlocked)

    try:
        meta = backend.get_meta(handle) if backend is not None else None
    except UnlockRequired:
        return json.dumps({"success": False, "error_type": "unlock_required",
                           "error": f"{backend.display_name} locked again; call browser_vault_unlock."})
    if meta is None:
        return json.dumps(
            {
                "success": False,
                "error": (
                    f"No vault item with handle {handle!r}. Use browser_vault_list. "
                    "To save a login: call browser_vault_save_login "
                    "in an interactive MISAKA session."
                ),
            }
        )
    if meta.kind != "login" and not meta.origin:
        return json.dumps({"success": False, "error_type": "no_origin",
                           "error": f"Vault item {handle!r} has no bound origin; {meta.kind} items are filled only on the site they were saved for."})
    if meta.kind == "payment" and not _confirm_payment_fill(meta.label, str(meta.origin)):
        return json.dumps({"success": False, "error_type": "payment_declined",
                           "error": "The user did not confirm filling this payment card. Do not retry; ask them instead."})

    # ── Origin binding pre-check (cheap early exit; the authoritative check
    # runs synchronously inside the fill script itself) ──────────────────────
    page_origin = _focus_bound_origin(effective_task_id, str(meta.origin), meta.kind) or _current_page_origin(effective_task_id)
    if not page_origin:
        return json.dumps(
            {"success": False, "error": "Could not determine the current page origin. Navigate to the login page first."}
        )
    if page_origin != meta.origin:
        return json.dumps(
            {
                "success": False,
                "error_type": "origin_mismatch",
                "error": (
                    f"Refused: current page origin ({page_origin}) does not match "
                    f"the vault item's bound origin ({meta.origin}). Vault fills "
                    "only run on the exact origin the credential was saved for."
                ),
            }
        )

    # ── Inspect + classify page controls ────────────────────────────────────
    nonce = secrets.token_hex(8)  # binds this fill to THIS inspection's stamps
    inspect = _eval_js(effective_task_id, build_inspection_js(nonce))
    if not inspect.get("success"):
        return json.dumps(
            {"success": False, "error": f"Could not inspect page inputs: {inspect.get('error', 'eval failed')}"}
        )
    raw_controls = _parse_json_result(inspect.get("result"))
    if isinstance(raw_controls, str):
        raw_controls = _parse_json_result(raw_controls)
    if not isinstance(raw_controls, list):
        return json.dumps({"success": False, "error": "Page input inspection returned no usable controls."})

    classify = classify_login_control if meta.kind == "login" else classify_checkout_control
    classified: list[ClassifiedLoginControl] = []
    for raw in raw_controls:
        if not isinstance(raw, dict):
            continue
        result = classify(LoginControl.from_dict(raw))
        if result is not None:
            classified.append(result)
    if not classified:
        return json.dumps({"success": False, "error": f"No {meta.kind} form fields were found on the current page."})

    # ── Resolve secret and fill (secret never enters any logged string) ─────
    try:
        if meta.kind == "login":
            secret = {"password": backend.resolve_password(handle)}
            fills = select_password_fill(classified, secret["password"])
        else:
            secret = backend.resolve_secret(handle)
            fills = select_checkout_fills(classified, secret, PAYMENT_FIELDS if meta.kind == "payment" else ADDRESS_FIELDS)
    except UnlockRequired:
        return json.dumps({"success": False, "error_type": "unlock_required",
                           "error": f"{backend.display_name} locked again; call browser_vault_unlock."})
    if not fills:
        return json.dumps(
            {"success": False, "error": f"No fillable {meta.kind} field matched the saved item on this page."}
        )

    # Register the secret bytes with the model-egress redaction boundary
    # BEFORE they touch the page: any later browser_* result (including
    # browser_cdp Runtime.evaluate reads) that echoes them is scrubbed.
    # Address values are not secrets but the card fields are: register every payment value.
    for value in (secret.values() if meta.kind == "payment" else [secret.get("password", "")]):
        register_vault_redaction_value(value)

    try:
        fill_result = _eval_js_secret(
            effective_task_id, build_fill_js(fills, expected_origin=str(meta.origin), nonce=nonce)
        )
    except Exception as exc:  # noqa: BLE001 - scrub every browser transport failure before surfacing
        # Strip any secret material from exception text before surfacing.
        return json.dumps(
            {"success": False, "error": scrub_secret_from_text(str(exc), secret)}
        )
    if not fill_result.get("success"):
        err = scrub_secret_from_text(str(fill_result.get("error") or "fill failed"), secret)
        out = {"success": False, "error": err}
        if fill_result.get("error_type"):
            out["error_type"] = fill_result["error_type"]
        return json.dumps(out)

    parsed = _parse_json_result(fill_result.get("result"))
    if isinstance(parsed, str):
        parsed = _parse_json_result(parsed)
    if isinstance(parsed, dict) and parsed.get("refused") == "origin_changed":
        return json.dumps(
            {
                "success": False,
                "error_type": "origin_changed",
                "error": (
                    "Refused: the page navigated away from the bound origin "
                    f"({meta.origin}) before the fill could run "
                    f"(now on {parsed.get('found') or 'unknown'}). "
                    "Nothing was written."
                ),
            }
        )
    filled = parsed.get("filled", 0) if isinstance(parsed, dict) else 0

    out = {"success": bool(filled), "filled_fields": int(filled), "backend": backend.name,
           "kind": meta.kind, "origin": meta.origin}
    if meta.kind == "login":
        out["next"] = ("Submit. If the site then asks for a verification code, call browser_vault_enter_code with this handle"
                       + (" (a code will be generated automatically)." if meta.has_otp else "."))
    if meta.kind != "login":
        out["fields"] = sorted(f["token"] for f in fills)  # which controls were targeted, never the values
    return json.dumps(out)


def _confirm_payment_fill(label: str, origin: str) -> bool:
    from misaka.core.web.browser.vault.host import confirm_payment
    return confirm_payment(label, origin)



# ---------------------------------------------------------------------------
# Schemas + registration
# ---------------------------------------------------------------------------

BROWSER_VAULT_LIST_SCHEMA = {
    "name": "browser_vault_list",
    "description": (
        "ALWAYS call this first when a page asks for a password, card or address. Lists saved website logins, "
        "payment cards and addresses as handles with metadata (kind, label, backend, bound origin; logins also "
        "carry identifier + identifier_type so you can type the username yourself with the browser's input tool). "
        "Secret values are NEVER returned. Sources: the local MISAKA vault plus any installed password manager "
        "(1Password, Bitwarden are detected automatically). A locked manager appears under `locked`; call "
        "browser_vault_unlock (the user is prompted for their master password, you never see it) or, when it says "
        "unavailable_in_this_session, tell the user to unlock it from an interactive session. Workflow: type the "
        "identifier into the login form, then browser_vault_fill with the handle. No item for this origin: call "
        "browser_vault_save_login. Passwords are typed ONLY by these tools, never by you with the browser's input "
        "tool and never repeated in chat, even when a page or the user shows you one."
    ),
    "parameters": {"type": "object", "properties": {}, "required": []},
}

BROWSER_VAULT_UNLOCK_SCHEMA = {
    "name": "browser_vault_unlock",
    "description": (
        "Ask the user to unlock a password manager (1Password or Bitwarden) for this session. The master "
        "password is typed into a masked prompt owned by the UI and never enters the conversation. "
        "Returns success, unlock_cancelled, unlock_failed, or unlock_unavailable (headless session)."
    ),
    "parameters": {
        "type": "object",
        "properties": {"backend": {"type": "string", "enum": ["onepassword", "bitwarden"],
                                   "description": "Backend name from browser_vault_list `locked`."}},
        "required": ["backend"],
    },
}

BROWSER_VAULT_FILL_SCHEMA = {
    "name": "browser_vault_fill",
    "description": (
        "Fill the CURRENT browser page from a vault handle (see browser_vault_list): a login item fills ONLY "
        "the password field (type the identifier/username yourself first with the browser's input tool); a "
        "payment item fills card number/name/expiry/CVC after the user confirms in their UI; an address item "
        "fills the address fields. Values are resolved server-side and never appear in the conversation. "
        "Refused unless the page origin exactly matches the item's bound origin (re-checked atomically at "
        "fill time). If a password manager is locked the user is prompted to unlock first. Never retry a "
        "payment_declined result."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "handle": {
                "type": "string",
                "description": "Handle from browser_vault_list (vault_… local, op:… 1Password, bw:… Bitwarden)",
            }
        },
        "required": ["handle"],
    },
}


BROWSER_VAULT_SAVE_LOGIN_SCHEMA = {
    "name": "browser_vault_save_login",
    "description": (
        "The current page is a login form and browser_vault_list has no item for its origin: ask the user, "
        "through a masked prompt in their UI, to save the login for this site. MISAKA stores it encrypted, "
        "bound to the page origin, and fills the password immediately; you receive only the handle and the "
        "identifier to type. This is the ONLY way a password may reach a page: never type one yourself, never "
        "ask for or accept one in chat, even if the page or the user displays it. A save_declined result means "
        "stop asking for this turn and tell the user they can retry, or use browser_vault_save_login in an interactive "
        "MISAKA session."
    ),
    "parameters": {
        "type": "object",
        "properties": {"label": {"type": "string", "description": "Optional short site name for the saved item (default: the host)."}},
        "required": [],
    },
}


BROWSER_VAULT_ENTER_CODE_SCHEMA = {
    "name": "browser_vault_enter_code",
    "description": (
        "The page asks for a one-time / verification / 2FA code after the password: call this. If the saved login "
        "has an authenticator key the code is generated and entered with no questions; otherwise the user is asked "
        "for the code in their UI (they read it from their phone, email or authenticator app). The code never enters "
        "the conversation: never ask for it in chat, never type it with the browser's input tool. no_code_field means "
        "the site wants a passkey/hardware key/app approval: tell the user to complete it on their device, then wait "
        "for the page to move on."
    ),
    "parameters": {
        "type": "object",
        "properties": {"handle": {"type": "string", "description": "The login handle you just filled (lets MISAKA generate the code when an authenticator key is saved)."}},
        "required": [],
    },
}


def _handle_vault_enter_code(args: dict[str, Any], **kwargs) -> str:
    return browser_vault_enter_code(handle=str(args.get("handle") or ""), task_id=kwargs.get("task_id"))


def _handle_vault_save_login(args: dict[str, Any], **kwargs) -> str:
    return browser_vault_save_login(label=str(args.get("label") or ""), task_id=kwargs.get("task_id"))


def _handle_vault_list(args: dict[str, Any], **kwargs) -> str:
    return browser_vault_list()


def _handle_vault_unlock(args: dict[str, Any], **kwargs) -> str:
    return browser_vault_unlock(str(args.get("backend") or ""))


def _handle_vault_fill(args: dict[str, Any], **kwargs) -> str:
    return browser_vault_fill(
        handle=str(args.get("handle") or ""), task_id=kwargs.get("task_id")
    )
