"""Images for a model that cannot see them, read to it by the one vision model the user named.

Pi keeps every image in the session as it arrived and decides per request what a model is sent:
``transformMessages`` puts "(image omitted: model does not support images)" in an image's place
for a model whose ``input`` has no "image". This extension runs before that, on the request-time
``context`` event, and for such a model puts what the vision model (``vision.model``, see
:mod:`misaka.core.web.vision`) reads off the image in its place instead. Every image a request
carries is covered the same way -- one the user pasted, a page from doc_page_image, an image
file from read, a screenshot, a picture an MCP tool returned -- so no tool reads images to a
model on its own. A model that can see is sent the image itself, untouched; the session keeps
the pixels either way, so a switch to a model that sees brings them back. Nothing in Pi's kernel
is changed for it.

An image is read once: what the vision model read is kept by the image's hash (the home's
``vision_cache``), and every later request that carries the same image takes it from there.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from types import SimpleNamespace
from typing import Any

from misaka.ai.types import TextContent
from misaka.config import home
from misaka.core.platform.prompt_guard import untrusted
from misaka.core.wiring import KINDS
from misaka.utils import atomic
from misaka.utils.values import read_field

logger = logging.getLogger(__name__)

# `builtin:vision`, like every bundled extension: hidden from the startup list, and disabled with
# `-builtin:vision` in the `extensions` setting.
BUILTIN = True
# Every session, a tool-less one included: an image the user pastes there is read the same way.
SESSION_KINDS = KINDS

# Bumped when the question changes, so a reading made under another one is not reused.
# 2: readings kept before 0.18.10 may be empty or cut short, with nothing to tell them by.
READING_VERSION = 2
# Images read at once, as FrontierAgent's batch reader does (READDOC_VISION_CONCURRENCY).
CONCURRENCY = 4
CONTEXT_CHARS = 800
_kept: dict[str, str] = {}


def _omitted_note():
    """The line Pi's read tool appends to its text for a model without vision -- "the image will be
    omitted" -- which this extension makes untrue. Asked of the tool rather than copied."""
    try:
        from misaka.core.tools.read import getNonVisionImageNote
        return getNonVisionImageNote(SimpleNamespace(input=["text"]))
    except Exception:  # noqa: BLE001 - only a courtesy: the note is left in place
        return None


def _key(data: str, selected: str) -> str:
    return hashlib.sha256(f"{READING_VERSION}\n{selected}\n{data}".encode()).hexdigest()


def _cached(key: str) -> str | None:
    if key in _kept:
        return _kept[key]
    try:
        with open(os.path.join(home.path("vision_cache"), f"{key}.md"), encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return None
    if not text.strip():
        return None                         # no reading: the image is read again
    _kept[key] = text
    return text


def _keep(key: str, text: str) -> None:
    _kept[key] = text
    folder = home.path("vision_cache")
    os.makedirs(folder, exist_ok=True)
    atomic.write_text(os.path.join(folder, f"{key}.md"), text)


def _where_from(message: Any) -> str:
    """What the vision model is told about where an image came from: orientation, not content."""
    if read_field(message, "role") == "toolResult":
        said = " ".join(read_field(block, "text") or "" for block in read_field(message, "content") or []
                        if read_field(block, "type") == "text")
        said = " ".join(said.split())[:CONTEXT_CHARS]
        return f"The image came back from the tool `{read_field(message, 'toolName')}`, which said: {said}"
    return "The user attached the image."


async def read_images(event: dict, ctx: Any) -> dict | None:
    """The ``context`` handler: the request's images read into words for a model without vision."""
    from misaka.core.web import vision
    from misaka.core.web.vision import TRANSCRIBE, describe, selected_model

    model = read_field(ctx, "model")
    if model is None or "image" in (read_field(model, "input") or []):
        return None
    selected, knob = selected_model()
    if not selected:
        return None                         # Pi's placeholder stands, and the tool notes say how to set one
    found = []
    for message in event["messages"]:
        content = read_field(message, "content")
        if read_field(message, "role") in ("user", "toolResult") and isinstance(content, list):
            found += [(message, i) for i, block in enumerate(content) if read_field(block, "type") == "image"]
    if not found:
        return None
    gate = asyncio.Semaphore(CONCURRENCY)
    omitted = _omitted_note()
    # One reading per distinct image: the same page or screenshot twice in a request is read once.
    first = {}
    for message, index in found:
        block = read_field(message, "content")[index]
        first.setdefault(_key(read_field(block, "data") or "", selected), (message, block))

    async def read_one(key: str, message: Any, block: Any) -> str:
        text = await asyncio.to_thread(_cached, key)
        if text is None:
            image = {"type": "image", "data": read_field(block, "data") or "", "mimeType": read_field(block, "mimeType")}
            question = f"{TRANSCRIBE}\n\nFor orientation only, not to transcribe: {_where_from(message)}"
            async with gate:
                try:
                    text = await describe(image, question, ctx, selected, setting=knob,
                                          purpose="vision_bridge", max_tokens=4096)
                    if not text.strip() or text.strip() == vision.CUT_OFF:
                        raise ValueError("it returned no text")
                except Exception as error:  # noqa: BLE001 - one unreadable image must not stop the turn
                    logger.warning("vision: %s could not read an image: %s", selected, error)
                    return (f"[An image that {selected} ({knob}) could not read for this model, which "
                            f"cannot see images: {error}]")
            # A reading cut short is shown, never kept: the next turn reads the image again (a
            # reasoning model that spent its output thinking used to leave such a reading for good).
            if not text.endswith(vision.CUT_OFF):
                await asyncio.to_thread(_keep, key, text)
        return (f"[An image, read to this model by {selected} ({knob}) because "
                f"{read_field(model, 'id') or 'it'} cannot see images. What follows is that model's "
                f"transcription, not the image itself: a quotation located in it is a reading.]\n"
                + untrusted(f"image {key[:12]}", text))

    read = dict(zip(first, await asyncio.gather(*(read_one(key, *where) for key, where in first.items())),
                    strict=True))
    readings = [read[_key(read_field(read_field(message, "content")[index], "data") or "", selected)]
                for message, index in found]
    for (message, index), reading in zip(found, readings, strict=True):
        content = read_field(message, "content")
        block = content[index]
        content[index] = TextContent(text=reading) if not isinstance(block, dict) else {"type": "text", "text": reading}
    if omitted:
        for message in {id(message): message for message, _ in found}.values():
            content = read_field(message, "content")
            for i, block in enumerate(content):
                text = read_field(block, "type") == "text" and read_field(block, "text") or ""
                if omitted in text:
                    text = text.replace("\n" + omitted, "").replace(omitted, "")
                    content[i] = TextContent(text=text) if not isinstance(block, dict) else {**block, "text": text}
    return {"messages": event["messages"]}


def register(harn: Any) -> None:
    harn.on("context", read_images)


def activate(_spec: Any):
    return register
