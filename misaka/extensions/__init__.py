"""Bundled extensions, the way Pi's ``src/extensions/index.ts`` ships them: what comes in
the package and reaches a session through the extension API alone.

Pi bundles one, its llama.cpp provider; MISAKA adds moa. Both are hidden, as Pi's is --
the startup screen's "Extensions" section is for what the user installed. The session
entry (``misaka.cli.engine``) puts these ahead of any user-supplied factories, exactly as
Pi's ``main.ts`` does. Nothing in ``misaka.core`` refers to this package.
"""

from misaka.extensions.llama import register as _llama
from misaka.extensions.moa.extension import register_provider as _moa

builtInExtensions = (
    {"name": "llama.cpp", "factory": _llama, "hidden": True},
    {"name": "moa", "factory": _moa, "hidden": True},
)
