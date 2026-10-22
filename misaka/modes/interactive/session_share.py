"""Translated from pi's ``modes/interactive/session-share.ts`` (v1.3.0).

MISAKA forks: the gist result shows the gist URL only. pi also prints a share-viewer link on pi.dev
(``getShareViewerUrl``), which MISAKA does not use. The Radius artifact is titled "MISAKA session".
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from typing import Any

import httpx

from misaka.ai.auth.resolve import AuthResolutionOverrides
from misaka.ai.providers.radius_config import DEFAULT_RADIUS_GATEWAY
from misaka.cli.auth_command import getAuthCredential
from misaka.core.session_export import exportSessionToJsonl
from misaka.ui.tui import hyperlink
from misaka.utils.abort import raceWithAbortSignal
from misaka.utils.js import errorMessage, jsTrim
from misaka.utils.values import read_field

from .components.bordered_loader import BorderedLoader
from .theme.theme import theme


@dataclass(slots=True)
class SessionShareContext:
    session: Any
    ui: Any
    editorContainer: Any
    editor: Any
    showStatus: Any
    showError: Any


def createShareTrailingEntries(session: Any, parentId: str | None, timestamp: str) -> list[dict[str, Any]]:
    """Trailing `pi.share` entry carrying the system prompt and tool schemas for the session viewer."""
    return [
        {
            "type": "custom",
            "customType": "pi.share",
            "id": str(uuid.uuid4())[:8],
            "parentId": parentId,
            "timestamp": timestamp,
            "data": {
                "systemPrompt": read_field(session.state, "systemPrompt"),
                "tools": [
                    {
                        "name": read_field(tool, "name"),
                        "description": read_field(tool, "description"),
                        "parameters": read_field(tool, "parameters"),
                    }
                    for tool in read_field(session.state, "tools", [])
                ],
            },
        }
    ]


def exportSessionForShare(filePath: str, session: Any) -> None:
    """Export the current branch with presentation metadata for Radius."""
    exportSessionToJsonl(
        session.sessionManager,
        filePath,
        lambda parentId, timestamp: createShareTrailingEntries(session, parentId, timestamp),
    )


async def shareSession(context: SessionShareContext) -> None:
    """Share the current session through Radius, falling back to a private gist."""
    tempDir = tempfile.mkdtemp(prefix="pi-share-")
    jsonlFile = os.path.join(tempDir, "session.jsonl")
    htmlFile = os.path.join(tempDir, "session.html")

    try:
        try:
            exportSessionForShare(jsonlFile, context.session)
        except Exception as error:  # noqa: BLE001 - upstream `catch (error)` reports any export failure
            context.showError(f"Failed to export session: {errorMessage(error)}")
            return
        destination = context.session.settingsManager.getSessionShareDestination()
        if destination != "gist":
            if await tryShareViaRadius(jsonlFile, context):
                return
            if destination == "radius":
                context.showError("Radius authentication is not configured. Run '/login radius' first.")
                return

        try:
            authResult = subprocess.run(  # noqa: ASYNC221 - upstream spawnSync
                ["gh", "auth", "status"], check=False, capture_output=True, encoding="utf-8", errors="replace"
            )
            if authResult.returncode != 0:
                context.showError("GitHub CLI is not logged in. Run 'gh auth login' first.")
                return
        except OSError:
            context.showError("GitHub CLI (gh) is not installed. Install it from https://cli.github.com/")
            return

        try:
            await context.session.exportToHtml(htmlFile, {"themeName": theme.name})
        except Exception as error:  # noqa: BLE001 - upstream `catch (error)` reports any export failure
            context.showError(f"Failed to export session: {errorMessage(error)}")
            return
        await shareViaGist(htmlFile, context)
    finally:
        # Ignore cleanup errors
        shutil.rmtree(tempDir, ignore_errors=True)


async def tryShareViaRadius(tmpFile: str, context: SessionShareContext) -> bool:
    provider = context.session.modelRuntime.getProvider("radius")
    if not provider:
        return False

    token = getAuthCredential(
        await context.session.modelRuntime.getAuth("radius", AuthResolutionOverrides(minOAuthValidityMs=5 * 60_000))
    )
    if not token:
        return False

    loader = BorderedLoader(context.ui, theme, "Uploading to Radius...")
    context.editorContainer.clear()
    context.editorContainer.addChild(loader)
    context.ui.setFocus(loader)
    context.ui.requestRender()

    def onAbort() -> None:
        restoreEditor(loader, context)
        context.showStatus("Share cancelled")

    loader.onAbort = onAbort

    try:
        with open(tmpFile, "rb") as file:  # noqa: ASYNC230 - upstream readFileSync
            body = file.read()
        url = httpx.URL(DEFAULT_RADIUS_GATEWAY).join("/v1/artifacts").copy_merge_params(
            # MISAKA fork: the artifact title names MISAKA.
            {"visibility": "organization", "title": "MISAKA session"}
        )
        async with httpx.AsyncClient() as client:
            response = await raceWithAbortSignal(
                client.post(
                    url,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/x-ndjson",
                        "Content-Length": str(len(body)),
                    },
                    content=body,
                ),
                loader.signal,
            )
        if loader.signal.aborted:
            return True
        try:
            json: Any = response.json()
        except ValueError:
            json = None
        if loader.signal.aborted:
            return True
        restoreEditor(loader, context)
        artifact = read_field(json, "artifact")
        if not response.is_success or not artifact:
            detail = read_field(json, "error") or response.reason_phrase or response.status_code
            context.showError(f"Failed to upload Radius artifact: {detail}")
            return True
        shareUrl = read_field(artifact, "canonical_url")
        context.showStatus(f"Share URL: {hyperlink(shareUrl, shareUrl)}")
        return True
    except Exception as error:  # noqa: BLE001 - upstream `catch (error)` reports any upload failure
        if not loader.signal.aborted:
            restoreEditor(loader, context)
            context.showError(f"Failed to upload Radius artifact: {errorMessage(error)}")
        return True


async def shareViaGist(tmpFile: str, context: SessionShareContext) -> None:
    loader = BorderedLoader(context.ui, theme, "Creating gist...")
    context.editorContainer.clear()
    context.editorContainer.addChild(loader)
    context.ui.setFocus(loader)
    context.ui.requestRender()

    proc: asyncio.subprocess.Process | None = None

    def onAbort() -> None:
        if proc is not None and proc.returncode is None:
            proc.kill()
        restoreEditor(loader, context)
        context.showStatus("Share cancelled")

    loader.onAbort = onAbort

    try:
        proc = await asyncio.create_subprocess_exec(
            "gh",
            "gist",
            "create",
            "--public=false",
            tmpFile,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdoutBytes, stderrBytes = await proc.communicate()
        stdout = stdoutBytes.decode("utf-8", errors="replace")
        stderr = stderrBytes.decode("utf-8", errors="replace")

        if loader.signal.aborted:
            return
        restoreEditor(loader, context)

        if proc.returncode != 0:
            context.showError(f"Failed to create gist: {jsTrim(stderr) or 'Unknown error'}")
            return

        gistUrl = jsTrim(stdout)
        gistId = gistUrl.split("/")[-1] if gistUrl else ""
        if not gistId:
            context.showError("Failed to parse gist ID from gh output")
            return

        # MISAKA fork: no pi.dev share-viewer link (`getShareViewerUrl`), only the gist.
        context.showStatus(f"Gist: {hyperlink(gistUrl, gistUrl)}")
    except Exception as error:  # noqa: BLE001 - upstream `catch (error)` reports any gist failure
        if not loader.signal.aborted:
            restoreEditor(loader, context)
            context.showError(f"Failed to create gist: {errorMessage(error)}")


def restoreEditor(loader: BorderedLoader, context: SessionShareContext) -> None:
    loader.dispose()
    context.editorContainer.clear()
    context.editorContainer.addChild(context.editor)
    context.ui.setFocus(context.editor)


__all__ = [
    "SessionShareContext",
    "createShareTrailingEntries",
    "exportSessionForShare",
    "shareSession",
]
