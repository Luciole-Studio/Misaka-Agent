"""Lazy Pillow-backed photon adapter."""

from __future__ import annotations

from io import BytesIO
from types import SimpleNamespace
from typing import Any


class _PhotonImage:
    def __init__(self, data: bytes | bytearray | memoryview, width: int, height: int) -> None:
        from PIL import Image

        self._image = Image.frombytes("RGBA", (width, height), bytes(data))

    @classmethod
    def new_from_byteslice(cls, data: bytes | bytearray | memoryview) -> _PhotonImage:
        from PIL import Image

        image = Image.open(BytesIO(bytes(data)))
        image.load()
        try:
            rgba = image.convert("RGBA")
        finally:
            image.close()
        instance = cls.__new__(cls)
        instance._image = rgba
        return instance

    def get_width(self) -> int:
        return int(self._image.width)

    def get_height(self) -> int:
        return int(self._image.height)

    def get_raw_pixels(self) -> bytes:
        return self._image.convert("RGBA").tobytes()

    def get_bytes(self) -> bytes:
        buffer = BytesIO()
        self._image.save(buffer, format="PNG")
        return buffer.getvalue()

    def get_bytes_jpeg(self, quality: int) -> bytes:
        from PIL import Image

        image = self._image
        if "A" in image.getbands():
            background = Image.new("RGB", image.size, (255, 255, 255))
            background.paste(image, mask=image.getchannel("A"))
            jpeg_ready = background
        else:
            jpeg_ready = image.convert("RGB")

        buffer = BytesIO()
        jpeg_ready.save(buffer, format="JPEG", quality=quality)
        if jpeg_ready is not image:
            jpeg_ready.close()
        return buffer.getvalue()

    def free(self) -> None:
        self._image.close()


class _PhotonModule:
    PhotonImage = _PhotonImage
    SamplingFilter = SimpleNamespace(Lanczos3="Lanczos3")

    @staticmethod
    def resize(image: _PhotonImage, width: int, height: int, _sampling_filter: Any) -> _PhotonImage:
        from PIL import Image

        resized = image._image.resize((width, height), Image.Resampling.LANCZOS)
        instance = _PhotonImage.__new__(_PhotonImage)
        instance._image = resized
        return instance

    @staticmethod
    def fliph(image: _PhotonImage) -> None:
        from PIL import Image

        previous = image._image
        image._image = previous.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
        previous.close()

    @staticmethod
    def flipv(image: _PhotonImage) -> None:
        from PIL import Image

        previous = image._image
        image._image = previous.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
        previous.close()


_photon_module: _PhotonModule | None = None
_load_attempted = False


async def load_photon() -> _PhotonModule | None:
    global _load_attempted, _photon_module
    if _photon_module is not None:
        return _photon_module
    if _load_attempted:
        return None

    _load_attempted = True
    try:
        from PIL import Image  # noqa: F401
    except Exception:
        _photon_module = None
        return _photon_module

    _photon_module = _PhotonModule()
    return _photon_module


loadPhoton = load_photon

__all__ = ["loadPhoton"]
