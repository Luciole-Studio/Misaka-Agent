"""Login-backend contract + registry for the browser credential vault.

A ``LoginBackend`` lists login metadata (never secrets) and resolves ONE
password at fill time. External managers (1Password, Bitwarden) additionally
need a per-session unlock; ``resolve_password`` raises ``UnlockRequired``
while locked so the tool can ask the surface to prompt. Handles are
namespaced by ``prefix`` so ``backend_for_handle`` needs no lookup table.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from misaka.core.web.browser.vault.store import VaultItemMeta


class UnlockRequired(Exception):
    """The backend is locked for this session; the surface must prompt for the master password."""

    def __init__(self, backend: LoginBackend):
        super().__init__(f"{backend.display_name} is locked")
        self.backend = backend


class LoginBackend(ABC):
    name: str                # config key: local | onepassword | bitwarden
    display_name: str        # user-facing
    prefix: str              # handle prefix ("vault_", "op:", "bw:")
    needs_unlock: bool = False

    def owns(self, handle: str) -> bool:
        return handle.startswith(self.prefix)

    def is_unlocked(self) -> bool:
        return True

    @abstractmethod
    def list_items(self) -> list[VaultItemMeta]:
        """Metadata only. Locked external backends return [] (the agent sees a lock hint instead)."""

    @abstractmethod
    def get_meta(self, handle: str) -> VaultItemMeta | None: ...

    @abstractmethod
    def resolve_password(self, handle: str) -> str:
        """Server-side only; raises ``UnlockRequired`` when locked."""

    def resolve_otp(self, handle: str) -> str | None:
        """Current one-time code for a login that stores a TOTP seed, else None (the user is asked).
        Server-side only, like resolve_password."""
        return None

    def resolve_secret(self, handle: str) -> dict[str, str]:
        """Full payload of a payment/address item (server-side only). External managers list only
        logins, so the base returns the password-only shape."""
        return {"password": self.resolve_password(handle)}


def run_with_stdin_secret(argv, *, env, secret, timeout, label):
    from misaka.core.web.browser.vault.host import run_cli
    return run_cli(argv, env=env, timeout=timeout, label=label,
                   timeout_message=f"{label} unlock timed out", input_bytes=(secret + "\n").encode())



def run_with_secret_env(argv, *, env, secret_env, secret, timeout, label):
    from misaka.core.web.browser.vault.host import run_cli
    return run_cli(argv, env={**env, secret_env: secret}, timeout=timeout, label=label,
                   timeout_message=f"{label} unlock timed out")



def _cfg() -> dict:
    from misaka.core.web.config import web_config as load_config_readonly
    cfg = load_config_readonly().get("vault") or {}
    return cfg if isinstance(cfg, dict) else {}


def external_backend_classes():
    from misaka.core.web.browser.vault.backends.bitwarden import BitwardenLoginBackend
    from misaka.core.web.browser.vault.backends.onepassword import (
        OnePasswordLoginBackend,
    )
    return (OnePasswordLoginBackend, BitwardenLoginBackend)


def is_installed(name: str) -> bool:
    """Is the manager CLI reachable — honouring a configured ``binary_path`` over PATH."""
    import shutil
    section = _cfg().get(name) or {}
    explicit = str(section.get("binary_path") or "") if isinstance(section, dict) else ""
    if explicit:
        return Path(explicit).is_file()
    if name == "onepassword":
        from misaka.core.web.browser.vault.host import find_op
        return find_op() is not None
    return shutil.which("bw") is not None


def is_enabled(name: str) -> bool:
    """An installed manager is a login source unless the user opted out (``vault.<name>.enabled: false``).
    Zero-config on purpose: a user with ``bw``/``op`` on PATH should never have to discover a toggle."""
    section = _cfg().get(name) or {}
    if isinstance(section, dict) and section.get("enabled") is False:
        return False
    return is_installed(name)


def enabled_backends() -> list[LoginBackend]:
    """Local first (always on), then every detected external manager the user has not turned off."""
    from misaka.core.web.browser.vault.backends.local import LocalLoginBackend

    cfg = _cfg()
    out: list[LoginBackend] = [LocalLoginBackend()]
    for cls in external_backend_classes():
        if is_enabled(cls.name):
            section = cfg.get(cls.name) or {}
            out.append(cls(section if isinstance(section, dict) else {}))
    return out


def backend_for_handle(handle: str) -> LoginBackend | None:
    return next((b for b in enabled_backends() if b.owns(handle)), None)
