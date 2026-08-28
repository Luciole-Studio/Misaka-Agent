"""pi's current auth stack, translated module-for-module from ``packages/ai/src/auth/``.

misaka's OAuth flow implementations live in ``ai/utils/oauth/`` and keep the callback
record (``onAuth``/``onDeviceCode``/``onPrompt``/``onSelect``/...) that pi now carries in
``compat/extension-oauth-types.ts``, re-exported from the type-only entry point
``packages/ai/src/oauth.ts`` "for coding-agent extension OAuth declarations". The two
stacks coexist: ``oauth_bridge.py`` wraps those flows in this package's ``OAuthAuth``
instead of rewriting them.
"""
