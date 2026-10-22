"""The ``write`` tool.

Translated from pi's ``packages/durable/src/tools/write.ts``. The parameters are the JSON schema TypeBox builds for the
upstream ``Type.Object``.
"""

from __future__ import annotations

from typing import Any

from misaka.chord.context import Context

from ..env import getOrThrow
from ..harness.define import defineTool
from .env import requireEnv
from .file_mutation_queue import withFileMutationQueue
from .path_utils import resolveToolPath

writeSchema: dict[str, Any] = {
    "type": "object",
    "required": ["path", "content"],
    "properties": {
        "path": {"type": "string", "description": "Path to the file to write (relative or absolute)"},
        "content": {"type": "string", "description": "Content to write to the file"},
    },
}


def createWriteTool() -> dict[str, Any]:
    async def execute(args: dict[str, Any], api: Any, context: Context) -> dict[str, Any]:
        path = args["path"]
        content = args["content"]
        env = requireEnv(api)
        absolutePath = await resolveToolPath(env, path, context)

        async def write() -> dict[str, Any]:
            signal = context.abortSignal
            if signal is not None and signal.aborted:
                raise RuntimeError("Operation aborted")
            getOrThrow(await env.writeFile(absolutePath, content, context))
            if signal is not None and signal.aborted:
                raise RuntimeError("Operation aborted")
            return {"output": [{"type": "text", "text": f"Successfully wrote to {path}"}]}

        return await withFileMutationQueue(env, absolutePath, write, context)

    return defineTool(
        {
            "name": "write",
            "description": "Write content to a file. Creates the file if it doesn't exist, overwrites if it does. Automatically creates parent directories.",
            "parameters": writeSchema,
            "execute": execute,
        }
    )


__all__ = ["createWriteTool"]
