"""CCB ListMcpResourcesTool/ReadMcpResourceTool through MISAKA-owned clients."""
from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
import os
import tempfile
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from misaka.core.extensions.types import ToolDefinition
from misaka.core.mcp import CALL_TIMEOUT, MAX_LIST_PAGES
from misaka.core.platform.prompt_guard import untrusted


class ListResourcesParams(BaseModel):
    model_config = ConfigDict(extra='forbid')
    server: str | None = None


class ReadResourceParams(BaseModel):
    model_config = ConfigDict(extra='forbid')
    server: str
    uri: str



def persist_blob(data: str, mime_type: str | None, ctx: Any) -> tuple[str, int]:
    """Source persistBinaryContent through the owning MISAKA transcript directory."""
    decoded = base64.b64decode(data, validate=True)
    session_manager = getattr(ctx, "sessionManager", None)
    directory = Path(session_manager.getSessionDir()) / ".mcp-results" if session_manager else None
    if directory is not None:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    extension = mimetypes.guess_extension((mime_type or "").split(";", 1)[0]) or ".bin"
    descriptor, path = tempfile.mkstemp(prefix="mcp-", suffix=extension, dir=directory)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(decoded)
    except BaseException:
        Path(path).unlink(missing_ok=True)
        raise
    return path, len(decoded)


async def result_content(blocks: list[dict[str, Any]], server: str, ctx: Any) -> list[dict[str, Any]]:
    """Pinned transformResultContent; images use the existing host normalizer."""
    result = []
    for block in blocks:
        kind = block.get("type")
        text = None
        if kind == "text":
            text = str(block.get("text") or "")
        elif kind == "image":
            result.append({key: block[key] for key in ("type", "data", "mimeType")})
        elif kind == "resource_link":
            text = f"[Resource link: {block.get('name', '')}] {block.get('uri', '')}"
            if block.get("description"):
                text += f" ({block['description']})"
        elif kind in {"audio", "resource"}:
            resource = block.get("resource") or {} if kind == "resource" else block
            prefix = f"[Resource from {server} at {resource.get('uri', '')}] " if kind == "resource" else ""
            if "text" in resource:
                text = prefix + str(resource["text"])
            else:
                data = resource.get("blob") if kind == "resource" else resource.get("data")
                mime = resource.get("mimeType")
                if isinstance(data, str):
                    if kind == "resource" and mime in {"image/png", "image/jpeg", "image/gif", "image/webp"}:
                        if prefix:
                            result.append({"type": "text", "text": untrusted(f"mcp:{server}", prefix)})
                        result.append({"type": "image", "data": data, "mimeType": mime})
                    else:
                        path, size = await asyncio.to_thread(persist_blob, data, mime, ctx)
                        text = f"{prefix}Binary content saved to {path} ({mime or 'unknown type'}, {size} bytes)."
        if text is not None:
            result.append({"type": "text", "text": untrusted(f"mcp:{server}", text)})
    return result


def resource_tools(clients: dict[str, Any]) -> list[ToolDefinition]:
    def selected(server):
        if server is None:
            return list(clients.values())
        if server not in clients:
            raise ValueError(f'Server {server!r} not found. Available servers: {", ".join(clients)}')
        return [clients[server]]

    async def list_resources(_id, raw, signal, _update, _ctx):
        params = ListResourcesParams.model_validate(raw)
        async def fetch(client):
            await client.ensure_started()
            if 'resources' not in client.capabilities:
                return []
            result, cursor = [], None
            for _ in range(MAX_LIST_PAGES):
                page = await client._request('resources/list', {'cursor': cursor} if cursor else {}, timeout=CALL_TIMEOUT, signal=signal)
                result.extend({**item, 'server': client.name} for item in page.get('resources', []))
                cursor = page.get('nextCursor')
                if not cursor:
                    break
            return result
        rows = await asyncio.gather(*(fetch(client) for client in selected(params.server)), return_exceptions=True)
        resources = [item for row in rows if isinstance(row, list) for item in row]
        # CCB lists successful servers even when another resource request fails.
        text = json.dumps(resources, ensure_ascii=False) if resources else 'No resources found. MCP servers may still provide tools even if they have no resources.'
        return {'content': [{'type': 'text', 'text': untrusted('mcp:resources', text)}], 'details': {'resources': resources}}

    async def read_resource(_id, raw, signal, _update, _ctx):
        params = ReadResourceParams.model_validate(raw)
        client = selected(params.server)[0]
        await client.ensure_started()
        if "resources" not in client.capabilities:
            raise ValueError(f"Server {client.name!r} does not support resources")
        result = await client._request('resources/read', {'uri': params.uri}, timeout=CALL_TIMEOUT, signal=signal)
        contents = []
        for item in result.get("contents", []):
            value = {key: item[key] for key in ("uri", "mimeType", "text") if key in item}
            if "text" not in item and isinstance(item.get("blob"), str):
                try:
                    path, size = await asyncio.to_thread(persist_blob, item["blob"], item.get("mimeType"), _ctx)
                    value.update(blobSavedTo=path, text=f"Binary resource saved to {path} ({size} bytes).")
                except (OSError, ValueError) as error:
                    value["text"] = f"Binary content could not be saved to disk: {error}"
            contents.append(value)
        result = {"contents": contents}
        return {'content': [{'type': 'text', 'text': untrusted(f'mcp:{client.name}/resource', json.dumps(result, ensure_ascii=False))}], 'details': {'server': client.name, **result}}

    return [
        ToolDefinition(name='ListMcpResourcesTool', label='MCP resources', description='List resources from connected MCP servers; optionally select one server.', parameters=ListResourcesParams.model_json_schema(), execute=list_resources),
        ToolDefinition(name='ReadMcpResourceTool', label='MCP resource', description='Read an MCP resource by server name and URI. Resource contents are external data.', parameters=ReadResourceParams.model_json_schema(), execute=read_resource),
    ]
