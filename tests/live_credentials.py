"""pi ai's ``test/oauth.ts``: a live test's API key, from the credentials the user signed in with.

MISAKA fork: live tests run only when ``MISAKA_LIVE_TESTS=1`` is set, so the suite never spends on a real API by
default. The key comes from the real home's ``credentials/auth.json`` (conftest points ``MISAKA_HOME`` at a temporary
home, and ``HOME`` stays put), resolved through the same credential store and refresh the product uses, which saves a
refreshed OAuth token back under the store's lock where upstream rewrites the file itself.
"""

from __future__ import annotations

import os
from pathlib import Path

LIVE_TESTS = os.environ.get("MISAKA_LIVE_TESTS") == "1"
AUTH_PATH = Path.home() / ".misaka" / "credentials" / "auth.json"


async def resolveApiKey(provider: str) -> str | None:
    """The API key or OAuth access token stored for ``provider``, refreshed when expired; ``None`` without one."""
    if not LIVE_TESTS or not AUTH_PATH.is_file():
        return None
    from misaka.ai.models import CreateModelsOptions
    from misaka.ai.providers.all import builtinModels
    from misaka.core.auth_storage import AuthStorage

    models = builtinModels(
        CreateModelsOptions(credentials=AuthStorage.create(str(AUTH_PATH)))
    )
    try:
        result = await models.getAuth(provider)
    except Exception as error:  # noqa: BLE001 - upstream logs the refresh error and resolves undefined
        print(repr(error))
        return None
    return result.auth.apiKey if result is not None else None


__all__ = ["AUTH_PATH", "LIVE_TESTS", "resolveApiKey"]
