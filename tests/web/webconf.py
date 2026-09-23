"""How a test stores a web configuration the way the product keeps it now."""
from __future__ import annotations

import json
from pathlib import Path

from misaka.config import env as env_file
from misaka.config import home


def write_web(values: dict, profile: str | Path | None = None) -> None:
    """Store a web configuration the way the product keeps it now.

    ``env`` entries that are credentials go to the layer's ``.env`` (the home's, or the
    role's); everything else is the ``web`` section of ``settings.json`` -- the home's, or the
    role's when ``profile`` names a role directory. Replaces what that layer held before.
    """
    from misaka.core.web.config import is_credential_var

    values = json.loads(json.dumps(values))          # the caller's literal, untouched
    env = values.get("env") if isinstance(values.get("env"), dict) else {}
    secrets = {name: value for name, value in env.items() if is_credential_var(name)}
    target = env_file.path(profile)
    target.unlink(missing_ok=True)                    # the layer held these before; not any more
    if secrets:
        env_file.write(secrets, profile)
    rest = {name: value for name, value in env.items() if not is_credential_var(name)}
    if rest:
        values["env"] = rest
    else:
        values.pop("env", None)
    settings = Path(profile) / "settings.json" if profile is not None else home.path("settings")
    settings.parent.mkdir(parents=True, exist_ok=True)
    try:
        document = json.loads(settings.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        document = {}
    if values:
        document["web"] = values
    else:
        document.pop("web", None)
    settings.write_text(json.dumps(document), encoding="utf-8")


def read_web(profile: str | Path | None = None) -> dict:
    """What one layer's ``web`` section holds, with that layer's credentials folded back in."""
    from misaka.core.web.config import is_credential_var

    settings = Path(profile) / "settings.json" if profile is not None else home.path("settings")
    try:
        section = json.loads(settings.read_text(encoding="utf-8")).get("web", {})
    except (OSError, ValueError):
        section = {}
    try:
        credentials = {name: value for name, value in env_file.read(profile).items() if is_credential_var(name)}
    except env_file.EnvFileError:
        credentials = {}
    if credentials:
        section["env"] = {**section.get("env", {}), **credentials}
    return section
