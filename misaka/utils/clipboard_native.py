"""Optional native clipboard bridge."""

from __future__ import annotations

import asyncio
import os
import sys
from io import BytesIO
from typing import Any, Protocol


class ClipboardModule(Protocol):
    async def set_text(self, text: str) -> None: ...

    def has_image(self) -> bool: ...

    async def get_image_binary(self) -> list[int] | None: ...


def _has_display(env: dict[str, str], platform_name: str) -> bool:
    return platform_name != "linux" or bool(env.get("DISPLAY") or env.get("WAYLAND_DISPLAY"))


class _NativeClipboardImpl:
    def __init__(self, pyperclip_module: Any, image_grab_module: Any) -> None:
        self._pyperclip = pyperclip_module
        self._image_grab = image_grab_module

    async def set_text(self, text: str) -> None:
        await asyncio.to_thread(self._pyperclip.copy, text)

    def has_image(self) -> bool:
        try:
            image = self._image_grab.grabclipboard()
        except Exception:
            return False
        return hasattr(image, "save")

    async def get_image_binary(self) -> list[int] | None:
        return await asyncio.to_thread(self._get_image_binary_sync)

    def _get_image_binary_sync(self) -> list[int] | None:
        try:
            image = self._image_grab.grabclipboard()
        except Exception:
            return None
        if not hasattr(image, "save"):
            return None

        buffer = BytesIO()
        image.save(buffer, format="PNG")
        close = getattr(image, "close", None)
        if callable(close):
            close()
        return list(buffer.getvalue())


def _load_clipboard(
    *,
    env: dict[str, str] | None = None,
    platform_name: str | None = None,
    pyperclip_module: Any = None,
    image_grab_module: Any = None,
) -> ClipboardModule | None:
    resolved_env = env if env is not None else os.environ
    resolved_platform = platform_name or sys.platform

    if resolved_env.get("TERMUX_VERSION") or not _has_display(resolved_env, resolved_platform):
        return None

    try:
        pyperclip = pyperclip_module
        if pyperclip is None:
            import pyperclip as pyperclip  # type: ignore[no-redef]

        image_grab = image_grab_module
        if image_grab is None:
            from PIL import ImageGrab as image_grab  # type: ignore[no-redef]
    except Exception:
        return None

    return _NativeClipboardImpl(pyperclip, image_grab)


clipboard = _load_clipboard()

__all__ = ["clipboard"]
