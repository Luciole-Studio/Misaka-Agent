"""Web profile state and a consistent view for each logical tool call.

The existing WebRuntime owns I/O. This object owns only configuration/registration
identity; it neither starts threads nor keeps another global table of sessions.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from pathlib import Path


@dataclass
class WebScope:
    profile_dir: str | None = None
    providers: dict = field(default_factory=dict)
    browser_providers: dict = field(default_factory=dict)
    builtins: dict = field(default_factory=dict)
    extensions: dict = field(default_factory=dict)
    owners: dict = field(default_factory=dict)
    builtins_registered: bool = False
    registration_id: str = ""
    # Shared by snapshots, not copied: a request advances its owner's cursor once.
    cursor: list[int] = field(default_factory=lambda: [secrets.randbelow(4)])
    negative_bans: dict = field(default_factory=dict, repr=False)
    rejected_credentials: dict = field(default_factory=dict, repr=False)
    runtime_secrets: dict = field(default_factory=dict, repr=False)
    vault_secrets: set[str] = field(default_factory=set, repr=False)
    gateway_accounts: dict = field(default_factory=dict, repr=False)
    lock: object = field(default_factory=threading.RLock, repr=False)
    config: dict | None = field(default=None, repr=False)
    environment: dict | None = field(default=None, repr=False)
    config_error: Exception | None = field(default=None, repr=False)
    namespace: str | None = None

    def __post_init__(self):
        if self.profile_dir is not None:
            self.profile_dir = str(Path(self.profile_dir).expanduser().resolve())

    @contextmanager
    def activate(self, *, snapshot=False):
        view = self
        if snapshot:
            from misaka.core.web.config import load_config

            with self.lock:
                view = replace(self, providers=dict(self.providers), builtins=dict(self.builtins),
                               extensions=dict(self.extensions), owners=dict(self.owners),
                               browser_providers=dict(self.browser_providers), namespace=None)
            view.environment = dict(os.environ)
            try:
                view.config = load_config(self.profile_dir)
                view.config_error = None
            except (OSError, ValueError) as error:
                view.config, view.config_error = {}, error
        token = _current.set(view)
        try:
            yield view
        finally:
            _current.reset(token)


_default = WebScope()
_current: ContextVar[WebScope] = ContextVar("web_scope", default=_default)


def current_scope() -> WebScope:
    return _current.get()


def auth_identity():
    """Atomic auth-file replacement invalidates both search and extract account caches."""
    from misaka.config import get_auth_path
    scope = current_scope()
    try:
        path = Path(scope.profile_dir) / 'auth.json' if scope.profile_dir else Path(get_auth_path())
    except (OSError, RuntimeError):
        return None  # A home-less local process has no OAuth account to cache.
    try:
        stat = path.stat()
        return str(path), stat.st_ino, stat.st_mtime_ns, stat.st_size
    except OSError:
        return str(path), None


def cache_namespace() -> str:
    """No plaintext credentials in cache paths, memo keys or flight diagnostics."""
    from misaka.core.web.config import (
        _config_path,
        provider_env,
        provider_variables,
        web_config,
    )

    scope = current_scope()
    if scope.namespace is not None:
        return scope.namespace
    # Include plugin-declared environment credentials/endpoints, not just bundled keys.
    names = provider_variables()
    # These controls are checked on every hit, not vendor request parameters. Including
    # policy here would refetch a canonical URL before checking its already-known source.
    # ponytail: one routing namespace per scope; split by vendor if unrelated key rotations
    # become a measured source of cache misses, rather than maintaining two key builders.
    routing = {key: value for key, value in web_config().items() if key not in {
        "website_blocklist", "allow_private_urls", "cache_enabled", "cache_ttl_minutes", "cache_exempt_hosts",
        "http_timeout", "operation_timeout", "extract_timeout", "debug_enabled", "proxy_dns", "trusted_private_hosts",
    }}
    if isinstance(routing.get('env'), dict):
        from misaka.core.web.network import PROXY_VARIABLES, TLS_VARIABLES

        routing['env'] = {key: value for key, value in routing['env'].items()
                          if key.upper() not in PROXY_VARIABLES + TLS_VARIABLES}
        if not routing['env']:
            routing.pop('env')
    # xAI's upstream-compatible timeout setting is also policy, unlike its model/filters.
    if isinstance(routing.get("xai"), dict):
        routing["xai"] = {key: value for key, value in routing["xai"].items() if key != "timeout"}
        if not routing["xai"]:
            routing.pop("xai")
    value = (_config_path(), routing, {name: provider_env(name) for name in sorted(names)},
             scope.registration_id, auth_identity())
    digest = hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
    if scope.config is not None:
        scope.namespace = digest
    return digest
