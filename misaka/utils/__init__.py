"""Utility exports for the coding-agent package."""

from misaka.utils.ansi import stripAnsi
from misaka.utils.mime import (
    IMAGE_TYPE_SNIFF_BYTES,
    PNG_SIGNATURE,
    detectSupportedImageMimeType,
    detectSupportedImageMimeTypeFromFile,
)
from misaka.utils.paths import (
    canonicalizePath,
    formatPathRelativeToCwdOrAbsolute,
    getCwdRelativePath,
    isLocalPath,
    markPathIgnoredByCloudSync,
    normalizePath,
    resolvePath,
)
from misaka.utils.shell import (
    getPowerShellConfig,
    sanitizeBinaryOutput,
)

_IMAGE_RESIZE_EXPORTS = {
    "IMAGE_RESIZE_DEFAULT_MAX_BYTES": "DEFAULT_MAX_BYTES",
    "ImageResizeOptions": "ImageResizeOptions",
    "ResizedImage": "ResizedImage",
    "formatDimensionNote": "formatDimensionNote",
    "resizeImage": "resizeImage",
}


def __getattr__(name):
    source = _IMAGE_RESIZE_EXPORTS.get(name)
    if source is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from misaka.utils import image_resize
    value = getattr(image_resize, source)
    globals()[name] = value
    return value


__all__ = [
    "IMAGE_RESIZE_DEFAULT_MAX_BYTES",
    "IMAGE_TYPE_SNIFF_BYTES",
    "PNG_SIGNATURE",
    "ImageResizeOptions",
    "ResizedImage",
    "canonicalizePath",
    "detectSupportedImageMimeType",
    "detectSupportedImageMimeTypeFromFile",
    "formatDimensionNote",
    "formatPathRelativeToCwdOrAbsolute",
    "getCwdRelativePath",
    "getPowerShellConfig",
    "isLocalPath",
    "markPathIgnoredByCloudSync",
    "normalizePath",
    "resizeImage",
    "resolvePath",
    "sanitizeBinaryOutput",
    "stripAnsi",
]
