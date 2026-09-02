"""Utility exports for the coding-agent package."""

from misaka.utils.ansi import strip_ansi, stripAnsi
from misaka.utils.mime import (
    IMAGE_TYPE_SNIFF_BYTES,
    PNG_SIGNATURE,
    detect_supported_image_mime_type,
    detect_supported_image_mime_type_from_file,
    detectSupportedImageMimeType,
    detectSupportedImageMimeTypeFromFile,
)
from misaka.utils.paths import (
    canonicalize_path,
    canonicalizePath,
    format_path_relative_to_cwd_or_absolute,
    formatPathRelativeToCwdOrAbsolute,
    get_cwd_relative_path,
    getCwdRelativePath,
    is_local_path,
    isLocalPath,
    mark_path_ignored_by_cloud_sync,
    markPathIgnoredByCloudSync,
    normalize_path,
    normalizePath,
    resolve_path,
    resolvePath,
)
from misaka.utils.shell import (
    get_powershell_config,
    getPowerShellConfig,
    sanitize_binary_output,
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
    "format_dimension_note": "format_dimension_note",
    "formatDimensionNote": "formatDimensionNote",
    "resize_image": "resize_image",
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
    "canonicalize_path",
    "detectSupportedImageMimeType",
    "detectSupportedImageMimeTypeFromFile",
    "detect_supported_image_mime_type",
    "detect_supported_image_mime_type_from_file",
    "formatDimensionNote",
    "formatPathRelativeToCwdOrAbsolute",
    "format_dimension_note",
    "format_path_relative_to_cwd_or_absolute",
    "getCwdRelativePath",
    "getPowerShellConfig",
    "get_cwd_relative_path",
    "get_powershell_config",
    "isLocalPath",
    "is_local_path",
    "markPathIgnoredByCloudSync",
    "mark_path_ignored_by_cloud_sync",
    "normalizePath",
    "normalize_path",
    "resizeImage",
    "resize_image",
    "resolvePath",
    "resolve_path",
    "sanitizeBinaryOutput",
    "sanitize_binary_output",
    "stripAnsi",
    "strip_ansi",
]
