"""Owned Skill environment, secret capture and credential transport.

No process environment mutations. A scope is bound only around a concrete read or
activation; sibling tasks get their own owner, not a shared ContextVar default.
Remote adapters consume mount/file manifests through the existing shell operations
interface; no Hermes terminal manager or background uploader is started.
"""
import contextvars
import os
import shutil
import stat
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path

from misaka.utils import atomic

from .layers import load_skills_config
from .manage import lookup_path_error
from .vendor.env_policy import (
    _AWS_SDK_CREDENTIAL_ENV_VARS,
    _STATIC_PROVIDER_ENV_BLOCKLIST,
    _is_hermes_internal_secret,
)

_active = contextvars.ContextVar("misaka_skill_runtime", default=None)


def active_runtime():
    runtime = _active.get()
    if runtime is None or runtime.closed:
        raise RuntimeError("Skill runtime is not bound or has closed.")
    return runtime


@contextmanager
def using_runtime(runtime):
    token = _active.set(runtime)
    try:
        yield runtime
    finally:
        _active.reset(token)


def blocked_env(name):
    from misaka.ai.env_api_keys import _ENV_MAP
    return (name in _STATIC_PROVIDER_ENV_BLOCKLIST or name in _AWS_SDK_CREDENTIAL_ENV_VARS
            or name in _ENV_MAP.values() or _is_hermes_internal_secret(name)
            or name.startswith(("MISAKA_", "_HERMES_FORCE_")))


def validate_within_dir(path, root):
    from .write import _safe_parents
    try:
        path, root = Path(path).absolute(), Path(root).absolute()
        if not path.is_relative_to(root) or lookup_path_error(str(path.relative_to(root))):
            return "Credential path is outside its role directory."
        _safe_parents(path.parent)
        if any(getattr(p, "is_junction", lambda: False)() for p in (path, *path.parents)):
            return "Credential path contains a junction."
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            return "Credential path contains a symlink."
    except (OSError, ValueError):
        return "Credential path is not a contained regular file."
    return None


def credential_read_error(path):
    runtime = active_runtime()
    if error := validate_within_dir(path, runtime.profile):
        return error
    rel = Path(path).relative_to(runtime.profile)
    denied = {".env", "auth.json", "credentials.json", "mcp-tokens", "tokens", "sessions"}
    if any(p in denied for p in rel.parts) or any(p.startswith(".") for p in rel.parts):
        return "Master stores are not Skill credential files."
    return None


class SkillRuntime:
    def __init__(self, profile_dir, *, env=None, backend="local", remote=None, capture=None, platform="cli"):
        self.profile = Path(profile_dir).expanduser().absolute() if profile_dir else None
        self.env = dict(os.environ if env is None else env)
        self.backend, self.remote, self.capture, self.platform = backend, remote, capture, platform
        self.allowed, self.files = set(), {}
        self.closed = False
        self._tmp = None
        self._lock = threading.RLock()
        self._remote_manifest = []

    def reopen(self):
        self.close()
        return type(self)(self.profile, backend=self.backend, remote=self.remote, capture=self.capture, platform=self.platform)

    def load_env(self):
        """The environment plus the role's own ``.env`` (hermes: the profile's .env), the
        way ``config.env.values`` layers it; ``MISAKA_*`` names in the file are ignored."""
        from misaka.config import env as env_file

        data = dict(self.env)
        if self.profile is not None:
            for name, value in env_file.read(self.profile).items():
                if not name.startswith(env_file.RESERVED_PREFIX):
                    data[name] = value
        return data

    def store_secret(self, name, value):
        from .vendor.readiness import _ENV_VAR_NAME_RE
        if self.profile is None or blocked_env(name) or not _ENV_VAR_NAME_RE.fullmatch(name):
            raise ValueError("This is not a Skill service-secret name.")
        if not isinstance(value, str) or not value or "\x00" in value:
            raise ValueError("Secret must be nonempty text without NUL.")
        from misaka.config import env as env_file

        from .write import _safe_parents
        _safe_parents(self.profile, create=True)
        with self._lock:
            if self.closed:
                raise RuntimeError("Skill runtime is closed.")
            env_file.write({name: value}, self.profile)     # the role's .env, locked and owner-only

    def register_env(self, names):
        from .vendor.readiness import _ENV_VAR_NAME_RE
        for name in names:
            if isinstance(name, str) and _ENV_VAR_NAME_RE.fullmatch(name) and not blocked_env(name):
                self.allowed.add(name)

    def register_files(self, entries):
        if self.profile is None:
            return [str(e.get("path") or e.get("name") or "") if isinstance(e, dict) else e for e in entries if isinstance(e, (str, dict))]
        from .vendor.credential_files import register_credential_files
        with using_runtime(self):
            return register_credential_files(entries)

    def readiness(self, frontmatter, name="", *, capture=True):
        from .vendor.readiness import (
            _REMOTE_ENV_BACKENDS,
            _capture_required_environment_variables,
        )
        from .vendor.readiness_fields import _skill_readiness
        callback = self.capture if capture else None
        hint = ("Secure secret entry requires the local CLI or a secure interactive dialog."
                if self.platform not in ("cli", "tui", "desktop", "acp", "") and callback is None else None)
        fields, extras = _skill_readiness(frontmatter, name, backend=self.backend,
            load_env=self.load_env, _is_env_var_persisted=lambda key, env: bool(env.get(key)),
            _capture_required_environment_variables=lambda skill, entries: _capture_required_environment_variables(
                skill, entries, callback=callback, gateway_hint=hint),
            register_env_passthrough=self.register_env,
            register_credential_files=self.register_files,
            _is_remote_env_backend=lambda backend: backend in _REMOTE_ENV_BACKENDS or self.remote is not None)
        return {**fields, **extras}

    def execution_env(self, base=None, *, scrub=False):
        if self.closed:
            raise RuntimeError("Skill runtime is closed.")
        env = dict(self.env if base is None else base)
        if scrub:
            env = {k: v for k, v in env.items() if not blocked_env(k)}
        cfg = load_skills_config().get("env_passthrough", [])
        self.register_env(cfg if isinstance(cfg, list) else [])
        values = self.load_env()
        for name in self.allowed:
            if values.get(name) is not None:
                env[name] = values[name]
            else:
                env.pop(name, None)
        return env

    def credential_mounts(self):
        """Owned regular-file copies, revalidated per invocation; no live bind mounts."""
        with self._lock, using_runtime(self):
            if self.closed:
                raise RuntimeError("Skill runtime is closed.")
            cfg = load_skills_config().get("credential_files", [])
            if isinstance(cfg, list):
                self.register_files(cfg)
            if self._tmp is None:
                self._tmp = Path(tempfile.mkdtemp(prefix="misaka-skill-credentials-"))
            result = []
            for i, (remote, source) in enumerate(self.files.items()):
                if credential_read_error(source):
                    continue
                try:
                    dirs = []
                    try:
                        if os.open in os.supports_dir_fd:
                            rel = Path(source).relative_to(self.profile)
                            parent = os.open(self.profile, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                            dirs.append(parent)
                            for part in rel.parts[:-1]:
                                parent = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                                dirs.append(parent)
                            fd = os.open(rel.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
                        else:
                            fd = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                    finally:
                        for directory in reversed(dirs):
                            os.close(directory)
                    with os.fdopen(fd, "rb") as stream:
                        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                            continue
                        data = stream.read(8 * 1024 * 1024 + 1)
                    if len(data) > 8 * 1024 * 1024 or credential_read_error(source):
                        continue
                except OSError:
                    continue
                dest = self._tmp / str(i)
                atomic.write_bytes(dest, data, mode=0o600)
                result.append({"host_path": str(dest), "container_path": remote})
            return result

    def prepare_remote(self):
        # Serialize transport completion with cleanup, including partial uploads.
        with self._lock:
            if self.closed:
                raise RuntimeError("Skill runtime is closed.")
            if self.remote is None:
                return []
            mounts = self.credential_mounts()
            # The host transport owns authentication and remote process identity.
            old = {m["container_path"]: m for m in self._remote_manifest}
            current = {m["container_path"]: m for m in mounts}
            self._remote_manifest = list((old | current).values())
            if removed := old.keys() - current.keys():
                self.remote.remove_files(sorted(removed))
            self.remote.sync_files(mounts)
            self._remote_manifest = mounts
            return mounts

    def close(self):
        self.closed = True  # Reject new work while waiting for an in-flight upload.
        with self._lock:
            # A failed remote deletion remains retryable, but the owner is already
            # closed to further execution and local copies/secrets are discarded.
            self.closed = True
            try:
                if self.remote is not None and self._remote_manifest:
                    self.remote.remove_files([m["container_path"] for m in self._remote_manifest])
                    self._remote_manifest = []
            finally:
                if self._tmp is not None:
                    shutil.rmtree(self._tmp)
                    self._tmp = None
                self.allowed.clear()
                self.files.clear()
                self.env.clear()
