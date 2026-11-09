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

# image_resize re-exports are lazy (PEP 562): the module imports misaka.ai.types, whose
# package __init__ boots every provider SDK. In pi (TypeScript) importing utils/paths
# never runs a package index, so this chain does not exist there; eager Python __init__s
# made the panel and daemon -- processes that never call a model -- pay ~0.8s of AI SDK
# imports at startup. Provider registration is untouched: every engine process imports
# misaka.ai.* directly (core/agent_session.py imports register_builtins itself).
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
