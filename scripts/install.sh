#!/bin/sh

set -eu

REPO="${MISAKA_REPO:-Luciole-Studio/Misaka-Agent}"
PREFIX="${MISAKA_PREFIX:-$HOME/.local/share/misaka}"
BIN_DIR="${MISAKA_BIN:-$HOME/.local/bin}"

os=$(uname -s)
mach=$(uname -m)
case "$os:$mach" in
  Darwin:arm64) target=darwin-arm64 ;;
  Darwin:x86_64) target=darwin-x86_64 ;;
  Linux:x86_64) target=linux-x86_64 ;;
  Linux:aarch64|Linux:arm64) target=linux-arm64 ;;
  MINGW*|MSYS*|CYGWIN*)
    echo "On Windows, run scripts/install.ps1 in PowerShell:" >&2
    echo "  irm https://raw.githubusercontent.com/${REPO}/main/scripts/install.ps1 | iex" >&2
    exit 1
    ;;
  *)
    echo "No MISAKA build for ${os} ${mach}." >&2
    echo "Published architectures: darwin-arm64, darwin-x86_64, linux-x86_64, linux-arm64, windows-x86_64, windows-arm64." >&2
    exit 1
    ;;
esac

version="${1:-${MISAKA_VERSION:-}}"
version="${version#v}"
if [ -z "$version" ]; then
  tag=$(curl -fsSL "https://api.github.com/repos/${REPO}/releases/latest" \
    | sed -n 's/.*"tag_name"[[:space:]]*:[[:space:]]*"v\{0,1\}\([^"]*\)".*/\1/p' \
    | head -n 1)
  version="${tag#v}"
fi
if [ -z "$version" ]; then
  echo "Could not tell which release to install. Pass a version, or set MISAKA_VERSION." >&2
  exit 1
fi

link="${BIN_DIR}/misaka"
if [ -e "$link" ] || [ -L "$link" ]; then
  current=$(readlink "$link" 2>/dev/null || echo "$link")
  case "$current" in
    "${PREFIX}"/misaka-*/bin/misaka) ;;
    *)
      if [ "${MISAKA_REPLACE:-}" != "1" ]; then
        echo "${link} is another MISAKA install (${current})." >&2
        echo "Remove that one first (uv tool uninstall misaka, or pipx uninstall misaka), set" >&2
        echo "MISAKA_REPLACE=1 to replace its command with this one, or set MISAKA_BIN to another folder." >&2
        exit 1
      fi
      ;;
  esac
fi

name="misaka-${version}-${target}.tar.gz"
url="https://github.com/${REPO}/releases/download/v${version}/${name}"
sums_url="https://github.com/${REPO}/releases/download/v${version}/SHA256SUMS"
workdir=$(mktemp -d)
trap 'rm -rf "$workdir"' EXIT

echo "Downloading ${name}"
curl -fL --retry 3 -o "${workdir}/${name}" "$url"
curl -fL --retry 3 -o "${workdir}/SHA256SUMS" "$sums_url"
expected=$(awk -v file="$name" '$2 == file { print $1 }' "${workdir}/SHA256SUMS")
if [ -z "$expected" ]; then
  echo "SHA256SUMS has no entry for ${name}." >&2
  exit 1
fi
if command -v sha256sum >/dev/null 2>&1; then
  actual=$(sha256sum "${workdir}/${name}" | awk '{ print $1 }')
else
  actual=$(shasum -a 256 "${workdir}/${name}" | awk '{ print $1 }')
fi
if [ "$expected" != "$actual" ]; then
  echo "Checksum mismatch for ${name}." >&2
  echo "expected ${expected}" >&2
  echo "actual   ${actual}" >&2
  exit 1
fi

mkdir -p "$PREFIX" "$BIN_DIR"
rm -rf "${PREFIX}/misaka-${version}-${target}"
tar -xzf "${workdir}/${name}" -C "$PREFIX"
dest="${PREFIX}/misaka-${version}-${target}"
if [ "$os" = "Darwin" ]; then
  xattr -dr com.apple.quarantine "$dest" 2>/dev/null || true
fi
ln -sfn "$dest/bin/misaka" "$BIN_DIR/misaka"

echo "Installed ${dest}"
echo "Command: ${BIN_DIR}/misaka"
case ":${PATH:-}:" in
  *":${BIN_DIR}:"*) ;;
  *) echo "Add ${BIN_DIR} to PATH, then open a new terminal." ;;
esac
echo "Then: mkdir ~/Documents/my-research && cd ~/Documents/my-research && misaka setup"
