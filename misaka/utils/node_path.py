"""Node's ``path`` module (``lib/path.js``, Node 24): ``normalize``, ``join``, ``dirname``, ``basename``, ``resolve`` and
``isAbsolute`` for posix and win32, ported function by function so that code translated from pi treats paths as Node does.

Python's ``os.path`` differs in ways a translated caller notices: ``join`` restarts at an absolute segment and does not
normalize, ``basename("src/")`` is ``""``, ``dirname("x")`` is ``""``. The module-level functions follow the running
platform, as ``require("path")`` does. ``basename`` takes no ``suffix`` (no caller passes one).
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from types import SimpleNamespace

CHAR_FORWARD_SLASH = "/"
CHAR_BACKWARD_SLASH = "\\"
CHAR_DOT = "."
CHAR_COLON = ":"

WINDOWS_RESERVED_NAMES = [
    "CON",
    "PRN",
    "AUX",
    "NUL",
    "COM1",
    "COM2",
    "COM3",
    "COM4",
    "COM5",
    "COM6",
    "COM7",
    "COM8",
    "COM9",
    "LPT1",
    "LPT2",
    "LPT3",
    "LPT4",
    "LPT5",
    "LPT6",
    "LPT7",
    "LPT8",
    "LPT9",
    "COM\xb9",
    "COM\xb2",
    "COM\xb3",
    "LPT\xb9",
    "LPT\xb2",
    "LPT\xb3",
]


def isPathSeparator(code: str) -> bool:
    return code in (CHAR_FORWARD_SLASH, CHAR_BACKWARD_SLASH)


def isPosixPathSeparator(code: str) -> bool:
    return code == CHAR_FORWARD_SLASH


def isWindowsDeviceRoot(code: str) -> bool:
    return ("A" <= code <= "Z") or ("a" <= code <= "z")


def isWindowsReservedName(path: str, colonIndex: int) -> bool:
    devicePart = path[:colonIndex].upper()
    return devicePart in WINDOWS_RESERVED_NAMES


def _charAt(path: str, index: int) -> str:
    """``StringPrototypeCharCodeAt`` compared with a character: ``""`` (NaN) outside the string."""
    return path[index] if 0 <= index < len(path) else ""


def normalizeString(path: str, allowAboveRoot: bool, separator: str, isPathSeparator: Callable[[str], bool]) -> str:
    """Resolves . and .. elements in a path with directory names"""
    res = ""
    lastSegmentLength = 0
    lastSlash = -1
    dots = 0
    code = ""
    i = 0
    while i <= len(path):
        if i < len(path):
            code = path[i]
        elif isPathSeparator(code):
            break
        else:
            code = CHAR_FORWARD_SLASH

        if isPathSeparator(code):
            if lastSlash == i - 1 or dots == 1:
                # NOOP
                pass
            elif dots == 2:
                if len(res) < 2 or lastSegmentLength != 2 or res[-1] != CHAR_DOT or res[-2] != CHAR_DOT:
                    if len(res) > 2:
                        lastSlashIndex = res.rfind(separator)
                        if lastSlashIndex == -1:
                            res = ""
                            lastSegmentLength = 0
                        else:
                            res = res[:lastSlashIndex]
                            lastSegmentLength = len(res) - 1 - res.rfind(separator)
                        lastSlash = i
                        dots = 0
                        i += 1
                        continue
                    elif len(res) != 0:
                        res = ""
                        lastSegmentLength = 0
                        lastSlash = i
                        dots = 0
                        i += 1
                        continue
                if allowAboveRoot:
                    res += f"{separator}.." if len(res) > 0 else ".."
                    lastSegmentLength = 2
            else:
                if len(res) > 0:
                    res += f"{separator}{path[lastSlash + 1 : i]}"
                else:
                    res = path[lastSlash + 1 : i]
                lastSegmentLength = i - lastSlash - 1
            lastSlash = i
            dots = 0
        elif code == CHAR_DOT and dots != -1:
            dots += 1
        else:
            dots = -1
        i += 1
    return res


# --- posix ----------------------------------------------------------------------------------------


def posixNormalize(path: str) -> str:
    if len(path) == 0:
        return "."

    isAbsolute = path[0] == CHAR_FORWARD_SLASH
    trailingSeparator = path[-1] == CHAR_FORWARD_SLASH

    # Normalize the path
    path = normalizeString(path, not isAbsolute, "/", isPosixPathSeparator)

    if len(path) == 0:
        if isAbsolute:
            return "/"
        return "./" if trailingSeparator else "."
    if trailingSeparator:
        path += "/"

    return f"/{path}" if isAbsolute else path


def posixJoin(*args: str) -> str:
    if len(args) == 0:
        return "."

    path = [arg for arg in args if len(arg) > 0]

    if len(path) == 0:
        return "."

    return posixNormalize("/".join(path))


def posixDirname(path: str) -> str:
    if len(path) == 0:
        return "."
    hasRoot = path[0] == CHAR_FORWARD_SLASH
    end = -1
    matchedSlash = True
    for i in range(len(path) - 1, 0, -1):
        if path[i] == CHAR_FORWARD_SLASH:
            if not matchedSlash:
                end = i
                break
        else:
            # We saw the first non-path separator
            matchedSlash = False

    if end == -1:
        return "/" if hasRoot else "."
    if hasRoot and end == 1:
        return "//"
    return path[:end]


def posixBasename(path: str) -> str:
    start = 0
    end = -1
    matchedSlash = True

    for i in range(len(path) - 1, -1, -1):
        if path[i] == CHAR_FORWARD_SLASH:
            # If we reached a path separator that was not part of a set of path
            # separators at the end of the string, stop now
            if not matchedSlash:
                start = i + 1
                break
        elif end == -1:
            # We saw the first non-path separator, mark this as the end of our
            # path component
            matchedSlash = False
            end = i + 1

    if end == -1:
        return ""
    return path[start:end]


def posixCwd() -> str:
    """``process.cwd()`` in POSIX form: on Windows, backslashes become slashes and the drive is cut off."""
    if sys.platform == "win32":
        cwd = os.getcwd().replace("\\", "/")
        return cwd[cwd.find("/") :] if "/" in cwd else cwd[-1:]
    return os.getcwd()


def posixResolve(*args: str) -> str:
    if len(args) == 0 or (len(args) == 1 and (args[0] == "" or args[0] == ".")):
        cwd = posixCwd()
        if _charAt(cwd, 0) == CHAR_FORWARD_SLASH:
            return cwd
    resolvedPath = ""
    resolvedAbsolute = False

    i = len(args) - 1
    while i >= 0 and not resolvedAbsolute:
        path = args[i]
        i -= 1

        # Skip empty entries
        if len(path) == 0:
            continue

        resolvedPath = f"{path}/{resolvedPath}"
        resolvedAbsolute = _charAt(path, 0) == CHAR_FORWARD_SLASH

    if not resolvedAbsolute:
        cwd = posixCwd()
        resolvedPath = f"{cwd}/{resolvedPath}"
        resolvedAbsolute = _charAt(cwd, 0) == CHAR_FORWARD_SLASH

    # At this point the path should be resolved to a full absolute path, but
    # handle relative paths to be safe (might happen when process.cwd() fails)

    # Normalize the path
    resolvedPath = normalizeString(resolvedPath, not resolvedAbsolute, "/", isPosixPathSeparator)

    if resolvedAbsolute:
        return f"/{resolvedPath}"
    return resolvedPath if len(resolvedPath) > 0 else "."


def posixIsAbsolute(path: str) -> bool:
    return len(path) > 0 and path[0] == CHAR_FORWARD_SLASH


def posixRelative(from_: str, to: str) -> str:
    if from_ == to:
        return ""

    # Trim leading forward slashes.
    from_ = posixResolve(from_)
    to = posixResolve(to)

    if from_ == to:
        return ""

    fromStart = 1
    fromEnd = len(from_)
    fromLen = fromEnd - fromStart
    toStart = 1
    toLen = len(to) - toStart

    # Compare paths to find the longest common path from root
    length = min(toLen, fromLen)
    lastCommonSep = -1
    i = 0
    while i < length:
        fromCode = from_[fromStart + i]
        if fromCode != to[toStart + i]:
            break
        elif fromCode == CHAR_FORWARD_SLASH:
            lastCommonSep = i
        i += 1
    if i == length:
        if toLen > length:
            if to[toStart + i] == CHAR_FORWARD_SLASH:
                # We get here if `from` is the exact base path for `to`.
                # For example: from='/foo/bar'; to='/foo/bar/baz'
                return to[toStart + i + 1 :]
            if i == 0:
                # We get here if `from` is the root
                # For example: from='/'; to='/foo'
                return to[toStart + i :]
        elif fromLen > length:
            if from_[fromStart + i] == CHAR_FORWARD_SLASH:
                # We get here if `to` is the exact base path for `from`.
                # For example: from='/foo/bar/baz'; to='/foo/bar'
                lastCommonSep = i
            elif i == 0:
                # We get here if `to` is the root.
                # For example: from='/foo/bar'; to='/'
                lastCommonSep = 0

    out = ""
    # Generate the relative path based on the path difference between `to`
    # and `from`.
    i = fromStart + lastCommonSep + 1
    while i <= fromEnd:
        if i == fromEnd or from_[i] == CHAR_FORWARD_SLASH:
            out += ".." if len(out) == 0 else "/.."
        i += 1

    # Lastly, append the rest of the destination (`to`) path that comes after
    # the common path parts.
    return f"{out}{to[toStart + lastCommonSep :]}"


# --- win32 ----------------------------------------------------------------------------------------


def win32Normalize(path: str) -> str:
    length = len(path)
    if length == 0:
        return "."
    rootEnd = 0
    device: str | None = None
    isAbsolute = False
    code = path[0]

    # Try to match a root
    if length == 1:
        # `path` contains just a single char, exit early to avoid
        # unnecessary work
        return "\\" if isPosixPathSeparator(code) else path
    if isPathSeparator(code):
        # Possible UNC root

        # If we started with a separator, we know we at least have an absolute
        # path of some kind (UNC or otherwise)
        isAbsolute = True

        if isPathSeparator(path[1]):
            # Matched double path separator at beginning
            j = 2
            last = j
            # Match 1 or more non-path separators
            while j < length and not isPathSeparator(path[j]):
                j += 1
            if j < length and j != last:
                firstPart = path[last:j]
                # Matched!
                last = j
                # Match 1 or more path separators
                while j < length and isPathSeparator(path[j]):
                    j += 1
                if j < length and j != last:
                    # Matched!
                    last = j
                    # Match 1 or more non-path separators
                    while j < length and not isPathSeparator(path[j]):
                        j += 1
                    if j == length or j != last:
                        if firstPart in (".", "?"):
                            # We matched a device root (e.g. \\\\.\\PHYSICALDRIVE0)
                            device = f"\\\\{firstPart}"
                            rootEnd = 4
                            colonIndex = path.find(":")
                            # Special case: handle \\?\COM1: or similar reserved device paths
                            possibleDevice = path[4 : colonIndex + 1]
                            if isWindowsReservedName(possibleDevice, len(possibleDevice) - 1):
                                device = f"\\\\?\\{possibleDevice}"
                                rootEnd = 4 + len(possibleDevice)
                        elif j == length:
                            # We matched a UNC root only
                            # Return the normalized version of the UNC root since there
                            # is nothing left to process
                            return f"\\\\{firstPart}\\{path[last:]}\\"
                        else:
                            # We matched a UNC root with leftovers
                            device = f"\\\\{firstPart}\\{path[last:j]}"
                            rootEnd = j
        else:
            rootEnd = 1
    else:
        colonIndex = path.find(":")
        if colonIndex > 0:
            if isWindowsDeviceRoot(code) and colonIndex == 1:
                device = path[:2]
                rootEnd = 2
                if length > 2 and isPathSeparator(path[2]):
                    isAbsolute = True
                    rootEnd = 3
            elif isWindowsReservedName(path, colonIndex):
                device = path[: colonIndex + 1]
                rootEnd = colonIndex + 1

    tail = normalizeString(path[rootEnd:], not isAbsolute, "\\", isPathSeparator) if rootEnd < length else ""
    if len(tail) == 0 and not isAbsolute:
        tail = "."
    if len(tail) > 0 and isPathSeparator(path[length - 1]):
        tail += "\\"
    if not isAbsolute and device is None and ":" in path:
        # If the original path was not absolute and if we have not been able to
        # resolve it relative to a particular device, we need to ensure that the
        # `tail` has not become something that Windows might interpret as an
        # absolute path. See CVE-2024-36139.
        if len(tail) >= 2 and isWindowsDeviceRoot(tail[0]) and tail[1] == CHAR_COLON:
            return f".\\{tail}"
        index = path.find(":")

        while True:
            if index == length - 1 or isPathSeparator(_charAt(path, index + 1)):
                return f".\\{tail}"
            index = path.find(":", index + 1)
            if index == -1:
                break
    colonIndex = path.find(":")
    if isWindowsReservedName(path, colonIndex):
        return f".\\{device if device is not None else ''}{tail}"
    if device is None:
        return f"\\{tail}" if isAbsolute else tail
    return f"{device}\\{tail}" if isAbsolute else f"{device}{tail}"


def win32Join(*args: str) -> str:
    if len(args) == 0:
        return "."

    path = [arg for arg in args if len(arg) > 0]

    if len(path) == 0:
        return "."

    firstPart = path[0]
    joined = "\\".join(path)

    # Make sure that the joined path doesn't start with two slashes, because
    # normalize() will mistake it for a UNC path then.
    #
    # This step is skipped when it is very clear that the user actually
    # intended to point at a UNC path. This is assumed when the first
    # non-empty string arguments starts with exactly two slashes followed by
    # at least one more non-slash character.
    #
    # Note that for normalize() to treat a path as a UNC path it needs to
    # have at least 2 components, so we don't filter for that here.
    # This means that the user can use join to construct UNC paths from
    # a server name and a share name; for example:
    #   path.join('//server', 'share') -> '\\\\server\\share\\')
    needsReplace = True
    slashCount = 0
    if isPathSeparator(firstPart[0]):
        slashCount += 1
        firstLen = len(firstPart)
        if firstLen > 1 and isPathSeparator(firstPart[1]):
            slashCount += 1
            if firstLen > 2:
                if isPathSeparator(firstPart[2]):
                    slashCount += 1
                else:
                    # We matched a UNC path in the first part
                    needsReplace = False
    if needsReplace:
        # Find any more consecutive slashes we need to replace
        while slashCount < len(joined) and isPathSeparator(joined[slashCount]):
            slashCount += 1

        # Replace the slashes if needed
        if slashCount >= 2:
            joined = f"\\{joined[slashCount:]}"

    # Skip normalization when reserved device names are present
    parts: list[str] = []
    part = ""

    i = 0
    while i < len(joined):
        if joined[i] == "\\":
            if part:
                parts.append(part)
            part = ""
            # Skip consecutive backslashes
            while i + 1 < len(joined) and joined[i + 1] == "\\":
                i += 1
        else:
            part += joined[i]
        i += 1
    # Add the final part if any
    if part:
        parts.append(part)

    # Check if any part has a Windows reserved name
    if any(p.find(":") != -1 and isWindowsReservedName(p, p.find(":")) for p in parts):
        # Replace forward slashes with backslashes
        return joined.replace("/", "\\")

    return win32Normalize(joined)


def win32Dirname(path: str) -> str:
    length = len(path)
    if length == 0:
        return "."
    rootEnd = -1
    offset = 0
    code = path[0]

    if length == 1:
        # `path` contains just a path separator, exit early to avoid
        # unnecessary work or a dot.
        return path if isPathSeparator(code) else "."

    # Try to match a root
    if isPathSeparator(code):
        # Possible UNC root

        rootEnd = offset = 1

        if isPathSeparator(path[1]):
            # Matched double path separator at beginning
            j = 2
            last = j
            # Match 1 or more non-path separators
            while j < length and not isPathSeparator(path[j]):
                j += 1
            if j < length and j != last:
                # Matched!
                last = j
                # Match 1 or more path separators
                while j < length and isPathSeparator(path[j]):
                    j += 1
                if j < length and j != last:
                    # Matched!
                    last = j
                    # Match 1 or more non-path separators
                    while j < length and not isPathSeparator(path[j]):
                        j += 1
                    if j == length:
                        # We matched a UNC root only
                        return path
                    if j != last:
                        # We matched a UNC root with leftovers

                        # Offset by 1 to include the separator after the UNC root to
                        # treat it as a "normal root" on top of a (UNC) root
                        rootEnd = offset = j + 1
    # Possible device root
    elif isWindowsDeviceRoot(code) and path[1] == CHAR_COLON:
        rootEnd = 3 if length > 2 and isPathSeparator(path[2]) else 2
        offset = rootEnd

    end = -1
    matchedSlash = True
    for i in range(length - 1, offset - 1, -1):
        if isPathSeparator(path[i]):
            if not matchedSlash:
                end = i
                break
        else:
            # We saw the first non-path separator
            matchedSlash = False

    if end == -1:
        if rootEnd == -1:
            return "."

        end = rootEnd
    return path[:end]


def win32Basename(path: str) -> str:
    start = 0
    end = -1
    matchedSlash = True

    # Check for a drive letter prefix so as not to mistake the following
    # path separator as an extra separator at the end of the path that can be
    # disregarded
    if len(path) >= 2 and isWindowsDeviceRoot(path[0]) and path[1] == CHAR_COLON:
        start = 2

    for i in range(len(path) - 1, start - 1, -1):
        if isPathSeparator(path[i]):
            # If we reached a path separator that was not part of a set of path
            # separators at the end of the string, stop now
            if not matchedSlash:
                start = i + 1
                break
        elif end == -1:
            # We saw the first non-path separator, mark this as the end of our
            # path component
            matchedSlash = False
            end = i + 1

    if end == -1:
        return ""
    return path[start:end]


def _processEnv(name: str) -> str | None:
    """``process.env[name]``. Windows keeps the working directory of each drive in a variable named like ``=C:``, which
    Python's ``os.environ`` leaves out."""
    if sys.platform == "win32" and name.startswith("="):
        import ctypes

        getVariable = ctypes.windll.kernel32.GetEnvironmentVariableW  # type: ignore[attr-defined]
        size = getVariable(name, None, 0)
        if size == 0:
            return None
        buffer = ctypes.create_unicode_buffer(size)
        getVariable(name, buffer, size)
        return buffer.value
    return os.environ.get(name)


def win32Resolve(*args: str) -> str:
    resolvedDevice = ""
    resolvedTail = ""
    resolvedAbsolute = False

    for i in range(len(args) - 1, -2, -1):
        if i >= 0:
            path = args[i]

            # Skip empty entries
            if len(path) == 0:
                continue
        elif len(resolvedDevice) == 0:
            path = os.getcwd()
            # Fast path for current directory
            if len(args) == 0 or (
                (len(args) == 1 and (args[0] == "" or args[0] == ".")) and isPathSeparator(_charAt(path, 0))
            ):
                if sys.platform != "win32":
                    path = path.replace("/", "\\")
                return path
        else:
            # Windows has the concept of drive-specific current working
            # directories. If we've resolved a drive letter but not yet an
            # absolute path, get cwd for that drive, or the process cwd if
            # the drive cwd is not available. We're sure the device is not
            # a UNC path at this points, because UNC paths are always absolute.
            path = _processEnv(f"={resolvedDevice}") or os.getcwd()

            # Verify that a cwd was found and that it actually points
            # to our drive. If not, default to the drive's root.
            if path[0:2].lower() != resolvedDevice.lower() and _charAt(path, 2) == CHAR_BACKWARD_SLASH:
                path = f"{resolvedDevice}\\"

        length = len(path)
        rootEnd = 0
        device = ""
        isAbsolute = False
        code = _charAt(path, 0)

        # Try to match a root
        if length == 1:
            if isPathSeparator(code):
                # `path` contains just a path separator
                rootEnd = 1
                isAbsolute = True
        elif isPathSeparator(code):
            # Possible UNC root

            # If we started with a separator, we know we at least have an
            # absolute path of some kind (UNC or otherwise)
            isAbsolute = True

            if isPathSeparator(_charAt(path, 1)):
                # Matched double path separator at beginning
                j = 2
                last = j
                # Match 1 or more non-path separators
                while j < length and not isPathSeparator(path[j]):
                    j += 1
                if j < length and j != last:
                    firstPart = path[last:j]
                    # Matched!
                    last = j
                    # Match 1 or more path separators
                    while j < length and isPathSeparator(path[j]):
                        j += 1
                    if j < length and j != last:
                        # Matched!
                        last = j
                        # Match 1 or more non-path separators
                        while j < length and not isPathSeparator(path[j]):
                            j += 1
                        if j == length or j != last:
                            if firstPart != "." and firstPart != "?":
                                # We matched a UNC root
                                device = f"\\\\{firstPart}\\{path[last:j]}"
                                rootEnd = j
                            else:
                                # We matched a device root (e.g. \\\\.\\PHYSICALDRIVE0)
                                device = f"\\\\{firstPart}"
                                rootEnd = 4
            else:
                rootEnd = 1
        elif isWindowsDeviceRoot(code) and _charAt(path, 1) == CHAR_COLON:
            # Possible device root
            device = path[0:2]
            rootEnd = 2
            if length > 2 and isPathSeparator(path[2]):
                # Treat separator following drive name as an absolute path
                # indicator
                isAbsolute = True
                rootEnd = 3

        if len(device) > 0:
            if len(resolvedDevice) > 0:
                if device.lower() != resolvedDevice.lower():
                    # This path points to another device so it is not applicable
                    continue
            else:
                resolvedDevice = device

        if resolvedAbsolute:
            if len(resolvedDevice) > 0:
                break
        else:
            resolvedTail = f"{path[rootEnd:]}\\{resolvedTail}"
            resolvedAbsolute = isAbsolute
            if isAbsolute and len(resolvedDevice) > 0:
                break

    # At this point the path should be resolved to a full absolute path,
    # but handle relative paths to be safe (might happen when process.cwd()
    # fails)

    # Normalize the tail path
    resolvedTail = normalizeString(resolvedTail, not resolvedAbsolute, "\\", isPathSeparator)

    return f"{resolvedDevice}\\{resolvedTail}" if resolvedAbsolute else f"{resolvedDevice}{resolvedTail}" or "."


def win32IsAbsolute(path: str) -> bool:
    length = len(path)
    if length == 0:
        return False

    code = path[0]
    return isPathSeparator(code) or (
        # Possible device root
        length > 2 and isWindowsDeviceRoot(code) and path[1] == CHAR_COLON and isPathSeparator(path[2])
    )


def win32Relative(from_: str, to: str) -> str:
    if from_ == to:
        return ""

    fromOrig = win32Resolve(from_)
    toOrig = win32Resolve(to)

    if fromOrig == toOrig:
        return ""

    from_ = fromOrig.lower()
    to = toOrig.lower()

    if from_ == to:
        return ""

    if len(fromOrig) != len(from_) or len(toOrig) != len(to):
        fromSplit = fromOrig.split("\\")
        toSplit = toOrig.split("\\")
        if fromSplit[-1] == "":
            fromSplit.pop()
        if toSplit[-1] == "":
            toSplit.pop()

        fromLen = len(fromSplit)
        toLen = len(toSplit)
        length = min(toLen, fromLen)

        i = 0
        while i < length:
            if fromSplit[i].lower() != toSplit[i].lower():
                break
            i += 1

        if i == 0:
            return toOrig
        elif i == length:
            if toLen > length:
                return "\\".join(toSplit[i:])
            if fromLen > length:
                return "..\\" * (fromLen - 1 - i) + ".."
            return ""

        return "..\\" * (fromLen - i) + "\\".join(toSplit[i:])

    # Trim any leading backslashes
    fromStart = 0
    while fromStart < len(from_) and from_[fromStart] == CHAR_BACKWARD_SLASH:
        fromStart += 1
    # Trim trailing backslashes (applicable to UNC paths only)
    fromEnd = len(from_)
    while fromEnd - 1 > fromStart and from_[fromEnd - 1] == CHAR_BACKWARD_SLASH:
        fromEnd -= 1
    fromLen = fromEnd - fromStart

    # Trim any leading backslashes
    toStart = 0
    while toStart < len(to) and to[toStart] == CHAR_BACKWARD_SLASH:
        toStart += 1
    # Trim trailing backslashes (applicable to UNC paths only)
    toEnd = len(to)
    while toEnd - 1 > toStart and to[toEnd - 1] == CHAR_BACKWARD_SLASH:
        toEnd -= 1
    toLen = toEnd - toStart

    # Compare paths to find the longest common path from root
    length = min(toLen, fromLen)
    lastCommonSep = -1
    i = 0
    while i < length:
        fromCode = from_[fromStart + i]
        if fromCode != to[toStart + i]:
            break
        elif fromCode == CHAR_BACKWARD_SLASH:
            lastCommonSep = i
        i += 1

    # We found a mismatch before the first common path separator was seen, so
    # return the original `to`.
    if i != length:
        if lastCommonSep == -1:
            return toOrig
    else:
        if toLen > length:
            if to[toStart + i] == CHAR_BACKWARD_SLASH:
                # We get here if `from` is the exact base path for `to`.
                # For example: from='C:\foo\bar'; to='C:\foo\bar\baz'
                return toOrig[toStart + i + 1 :]
            if i == 2:
                # We get here if `from` is the device root.
                # For example: from='C:\'; to='C:\foo'
                return toOrig[toStart + i :]
        if fromLen > length:
            if from_[fromStart + i] == CHAR_BACKWARD_SLASH:
                # We get here if `to` is the exact base path for `from`.
                # For example: from='C:\foo\bar'; to='C:\foo'
                lastCommonSep = i
            elif i == 2:
                # We get here if `to` is the device root.
                # For example: from='C:\foo\bar'; to='C:\'
                lastCommonSep = 3
        if lastCommonSep == -1:
            lastCommonSep = 0

    out = ""
    # Generate the relative path based on the path difference between `to` and
    # `from`
    i = fromStart + lastCommonSep + 1
    while i <= fromEnd:
        if i == fromEnd or from_[i] == CHAR_BACKWARD_SLASH:
            out += ".." if len(out) == 0 else "\\.."
        i += 1

    toStart += lastCommonSep

    # Lastly, append the rest of the destination (`to`) path that comes after
    # the common path parts
    if len(out) > 0:
        return f"{out}{toOrig[toStart:toEnd]}"

    if _charAt(toOrig, toStart) == CHAR_BACKWARD_SLASH:
        toStart += 1
    return toOrig[toStart:toEnd]


posix = SimpleNamespace(
    normalize=posixNormalize,
    join=posixJoin,
    dirname=posixDirname,
    basename=posixBasename,
    resolve=posixResolve,
    isAbsolute=posixIsAbsolute,
    relative=posixRelative,
    sep="/",
)
win32 = SimpleNamespace(
    normalize=win32Normalize,
    join=win32Join,
    dirname=win32Dirname,
    basename=win32Basename,
    resolve=win32Resolve,
    isAbsolute=win32IsAbsolute,
    relative=win32Relative,
    sep="\\",
)

_platform = win32 if sys.platform == "win32" else posix
normalize: Callable[[str], str] = _platform.normalize
join: Callable[..., str] = _platform.join
dirname: Callable[[str], str] = _platform.dirname
basename: Callable[[str], str] = _platform.basename
resolve: Callable[..., str] = _platform.resolve
relative: Callable[[str, str], str] = _platform.relative
isAbsolute: Callable[[str], bool] = _platform.isAbsolute
sep = "\\" if sys.platform == "win32" else "/"

__all__ = ["basename", "dirname", "isAbsolute", "join", "normalize", "posix", "relative", "resolve", "sep", "win32"]
