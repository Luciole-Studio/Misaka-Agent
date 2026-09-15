"""Local Fernet vault as a login backend (the always-on default)."""

from __future__ import annotations

from misaka.core.web.browser.vault.backends.base import LoginBackend
from misaka.core.web.browser.vault.store import VaultItemMeta


def _store():
    # Late import: tests and callers patch ``misaka.core.web.browser.vault.store.get_vault_store``; binding it here
    # at call time keeps that facade name the single seam.
    from misaka.core.web.browser.vault.store import get_vault_store
    return get_vault_store()


class LocalLoginBackend(LoginBackend):
    name = "local"
    display_name = "MISAKA vault"
    prefix = "vault_"

    def list_items(self) -> list[VaultItemMeta]:
        return _store().list_items()

    def get_meta(self, handle: str) -> VaultItemMeta | None:
        return _store().get_meta(handle)

    def resolve_password(self, handle: str) -> str:
        return str(_store().resolve_secret(handle).get("password") or "")

    def resolve_otp(self, handle: str) -> str | None:
        from misaka.core.web.browser.vault.store import totp_now
        seed = str(_store().resolve_secret(handle).get("otp_secret") or "")
        return totp_now(seed) if seed else None

    def resolve_secret(self, handle: str) -> dict[str, str]:
        return {k: str(v) for k, v in _store().resolve_secret(handle).items()}
