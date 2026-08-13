"""Utility exports for the coding-agent package."""

from misaka.utils.ansi import strip_ansi, stripAnsi
from misaka.utils.image_resize import (
    DEFAULT_MAX_BYTES as IMAGE_RESIZE_DEFAULT_MAX_BYTES,
)
from misaka.utils.image_resize import (
    ImageResizeOptions,
    ResizedImage,
    format_dimension_note,
    formatDimensionNote,
    resize_image,
    resizeImage,
)
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
from misaka.utils.shell import sanitize_binary_output, sanitizeBinaryOutput

__all__ = [
    "IMAGE_RESIZE_DEFAULT_MAX_BYTES",
    "IMAGE_TYPE_SNIFF_BYTES",
    "ImageResizeOptions",
    "PNG_SIGNATURE",
    "ResizedImage",
    "canonicalizePath",
    "canonicalize_path",
    "detectSupportedImageMimeType",
    "detectSupportedImageMimeTypeFromFile",
    "detect_supported_image_mime_type",
    "detect_supported_image_mime_type_from_file",
    "formatPathRelativeToCwdOrAbsolute",
    "formatDimensionNote",
    "format_dimension_note",
    "format_path_relative_to_cwd_or_absolute",
    "getCwdRelativePath",
    "get_cwd_relative_path",
    "isLocalPath",
    "is_local_path",
    "markPathIgnoredByCloudSync",
    "mark_path_ignored_by_cloud_sync",
    "normalizePath",
    "normalize_path",
    "resolvePath",
    "resizeImage",
    "resize_image",
    "resolve_path",
    "sanitizeBinaryOutput",
    "sanitize_binary_output",
    "stripAnsi",
    "strip_ansi",
]
