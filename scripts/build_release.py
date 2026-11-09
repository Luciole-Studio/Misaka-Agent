"""Build a MISAKA release archive for the machine it is run on.

The archive is a relocatable tree, one per architecture the panel ships
(``misaka/ui/panel/lib/README.md``):

    misaka-<version>-<os>-<arch>/
      bin/misaka            launcher (scripts/toolwrap.c)
      python/               CPython from python-build-standalone
      tools/uv              uv
      tools/rg              ripgrep
      tools/fd              fd
      tools/git             git, with GIT_EXEC_PATH pointed at the bundle
      tools/pdftotext       poppler
      opt/                  libraries those tools need
      LICENSE NOTICE THIRD_PARTY_NOTICES.md BUNDLED.txt

``bin/misaka`` runs the bundled interpreter as ``python -m misaka`` and puts
``tools/`` on PATH. Child processes keep using ``sys.executable -m misaka``,
which a frozen one-file binary cannot do. uv, ripgrep and fd are the upstream
single-file builds. git and poppler come from conda-forge, except Windows on
ARM, where conda-forge has git and not poppler: poppler then comes from the
MSYS2 clangarm64 repository.

Run ``python scripts/build_release.py`` on the target machine. The release
workflow runs it once per architecture.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import tarfile
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, assert_never

ROOT = Path(__file__).resolve().parents[1]
GITHUB_REPO = os.environ.get("MISAKA_RELEASE_REPO", "Luciole-Studio/Misaka-Agent")
USER_AGENT = "misaka-release"
ZIG_VERSION = "0.15.2"
HELLO_PDF = b"""%PDF-1.4
1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj
2 0 obj<</Type/Pages/Count 1/Kids[3 0 R]>>endobj
3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>endobj
4 0 obj<</Length 44>>stream
BT /F1 24 Tf 20 100 Td (Hello) Tj ET
endstream
endobj
5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj
trailer<</Root 1 0 R>>
%%EOF
"""

ArchiveKind = Literal["tar.gz", "zip"]
PopplerSource = Literal["conda", "msys2"]


@dataclass(frozen=True)
class Target:
    """One architecture the panel ships a ghostty library for."""

    name: str
    runner: str
    python_triple: str
    tool_triple: str
    conda_subdir: str
    archive: ArchiveKind
    poppler: PopplerSource


# Order matches the table in misaka/ui/panel/lib/README.md.
TARGETS: dict[str, Target] = {
    target.name: target
    for target in (
        Target("darwin-arm64", "macos-15", "aarch64-apple-darwin", "aarch64-apple-darwin", "osx-arm64", "tar.gz", "conda"),
        Target("darwin-x86_64", "macos-15-intel", "x86_64-apple-darwin", "x86_64-apple-darwin", "osx-64", "tar.gz", "conda"),
        Target("linux-x86_64", "ubuntu-24.04", "x86_64-unknown-linux-gnu", "x86_64-unknown-linux-musl", "linux-64", "tar.gz", "conda"),
        Target("linux-arm64", "ubuntu-24.04-arm", "aarch64-unknown-linux-gnu", "aarch64-unknown-linux-musl", "linux-aarch64", "tar.gz", "conda"),
        Target("windows-x86_64", "windows-2025", "x86_64-pc-windows-msvc", "x86_64-pc-windows-msvc", "win-64", "zip", "conda"),
        Target("windows-arm64", "windows-11-arm", "aarch64-pc-windows-msvc", "aarch64-pc-windows-msvc", "win-arm64", "zip", "msys2"),
    )
}

# Binaries the running app looks up. ripgrep and poppler keep these names.
BUNDLED_BINARIES = ("uv", "git", "rg", "fd", "pdftotext")


def project_version(root: Path = ROOT) -> str:
    with (root / "pyproject.toml").open("rb") as handle:
        data = tomllib.load(handle)
    version = data["project"]["version"]
    if not isinstance(version, str) or not version:
        raise SystemExit("pyproject.toml has no project.version")
    return version


def tool_versions(root: Path = ROOT) -> dict[str, str]:
    path = root / "release" / "tool-versions.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise SystemExit(f"{path} is not an object")
    return {str(key): str(value) for key, value in data.items()}


def host_target() -> str:
    """The bundle this interpreter can produce. Wheels follow the interpreter, not an emulated CPU."""
    machine = platform.machine().lower()
    arch = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "x86_64", "amd64": "x86_64"}.get(machine)
    if arch is None:
        raise SystemExit(f"no MISAKA build for architecture {platform.machine()}")
    if sys.platform == "darwin":
        name = f"darwin-{arch}"
    elif sys.platform == "win32":
        name = f"windows-{arch}"
    elif sys.platform == "linux":
        name = f"linux-{arch}"
    else:
        raise SystemExit(f"no MISAKA build for {sys.platform}")
    if name not in TARGETS:
        raise SystemExit(f"no MISAKA build for {name}")
    return name


def _download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_file() and dest.stat().st_size > 0:
        return
    print(f"download {url}", flush=True)
    partial = dest.with_suffix(dest.suffix + ".part")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    last_error: Exception | None = None
    for _attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=180) as response, partial.open("wb") as handle:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    handle.write(chunk)
            partial.replace(dest)
            return
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            last_error = error
            partial.unlink(missing_ok=True)
    raise SystemExit(f"could not download {url}: {last_error}")


def _extract(archive: Path, dest: Path) -> None:
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    if archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(dest)
        return
    if archive.name.endswith(".tar.xz"):
        mode = "r:xz"
    elif archive.name.endswith(".tar.bz2"):
        mode = "r:bz2"
    elif archive.name.endswith(".tar.gz") or archive.name.endswith(".tgz"):
        mode = "r:gz"
    else:
        mode = "r:*"
    with tarfile.open(archive, mode) as bundle:
        bundle.extractall(dest, filter="data")


def _find_named(root: Path, names: set[str]) -> Path:
    hits = [path for path in root.rglob("*") if path.is_file() and path.name in names]
    if not hits:
        raise SystemExit(f"none of {sorted(names)} found under {root}")
    hits.sort(key=lambda path: (len(path.parts), str(path)))
    return hits[0]


def _copy_licenses(tree: Path, dest: Path, label: str) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for path in tree.rglob("*"):
        if not path.is_file():
            continue
        lowered = path.name.lower()
        if lowered.startswith(("license", "copying", "unlicense")):
            shutil.copy2(path, dest / f"{label}-{path.name}")


def _exe(name: str) -> str:
    return f"{name}.exe" if sys.platform == "win32" else name


def _rel(from_dir: Path, path: Path) -> str:
    return Path(os.path.relpath(path, from_dir)).as_posix()


def python_executable(stage: Path) -> Path:
    candidates = (
        stage / "python" / "python.exe",
        stage / "python" / "bin" / "python3",
        stage / "python" / "bin" / "python",
    )
    for path in candidates:
        if path.is_file():
            return path
    raise SystemExit(f"standalone Python was not where the install_only archive puts it, under {stage / 'python'}")


def _break_hardlinks(root: Path) -> None:
    """conda hardlinks into its package cache. Copy those files so the archive stands alone."""
    for path in root.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        info = path.stat()
        if info.st_nlink <= 1:
            continue
        data = path.read_bytes()
        path.unlink()
        path.write_bytes(data)
        path.chmod(info.st_mode)


def _slim_prefix(prefix: Path) -> None:
    for relative in ("include", "share/man", "share/doc", "share/info", "man"):
        shutil.rmtree(prefix / relative, ignore_errors=True)
    for path in list(prefix.rglob("*")):
        if path.is_file() and path.suffix in {".a", ".la"}:
            path.unlink()


def _conda_version(prefix: Path, name: str) -> str:
    meta = prefix / "conda-meta"
    for path in meta.glob("*.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("name") == name:
            version = data.get("version")
            if isinstance(version, str):
                return version
    raise SystemExit(f"{name} is not installed in {prefix}")


def _find_program(prefixes: list[Path], stem: str) -> Path:
    names = {stem, f"{stem}.exe"}
    hits: list[Path] = []
    for prefix in prefixes:
        for path in prefix.rglob("*"):
            if not path.is_file() or path.name not in names:
                continue
            if "git-core" in path.parts:
                continue
            hits.append(path)
    if not hits:
        raise SystemExit(f"could not find {stem} under {prefixes}")
    hits.sort(key=lambda path: (0 if "bin" in path.parts else 1, len(path.parts), str(path)))
    return hits[0]


def _find_git_exec(prefix: Path) -> Path:
    hits = [path for path in prefix.rglob("git-core") if path.is_dir() and any(path.glob("git-*"))]
    if not hits:
        raise SystemExit(f"git's libexec directory is missing under {prefix}")
    hits.sort(key=lambda path: len(path.parts))
    return hits[0]


def _find_git_templates(prefix: Path) -> Path:
    hits = [
        path
        for path in prefix.rglob("templates")
        if path.is_dir() and (path / "hooks").is_dir() and "git-core" in path.parts
    ]
    if not hits:
        raise SystemExit(f"git templates are missing under {prefix}")
    return hits[0]


def _find_poppler_data(prefixes: list[Path]) -> Path | None:
    for prefix in prefixes:
        for path in prefix.rglob("poppler"):
            if path.is_dir() and path.parent.name == "share":
                return path
    return None


def poppler_tool_roots(conda_prefix: Path, msys2_prefix: Path | None) -> list[Path]:
    """Where the poppler CLIs live.

    conda-forge git for win-arm64 vendors Xpdf 4.06 as
    ``Library/clangarm64/bin/pdftotext.exe``. That file is on the archive
    prefix next to the MSYS2 poppler build. Searching the git prefix picks
    Xpdf, whose ``-v`` exits 99. The MSYS2 tree is the poppler the README asks
    for, and its ``bin`` directory also holds the DLLs ``pdftotext.exe`` loads.
    """
    if msys2_prefix is not None:
        return [msys2_prefix]
    return [conda_prefix]


def _poppler_programs(prefixes: list[Path]) -> list[Path]:
    found: list[Path] = []
    for prefix in prefixes:
        for path in prefix.rglob("*"):
            if not path.is_file() or "bin" not in path.parts:
                continue
            stem = path.name[:-4] if path.name.lower().endswith(".exe") else path.name
            if stem.startswith("pdf"):
                found.append(path)
    found.sort(key=lambda path: path.name)
    unique: list[Path] = []
    seen: set[str] = set()
    for path in found:
        if path.name in seen:
            continue
        seen.add(path.name)
        unique.append(path)
    return unique


def _pacman_fields(text: str) -> dict[str, list[str]]:
    fields: dict[str, list[str]] = {}
    current: str | None = None
    values: list[str] = []

    def flush() -> None:
        nonlocal values
        if current is not None:
            fields[current] = [line for line in values if line]
        values = []

    for line in text.splitlines():
        if len(line) >= 2 and line.startswith("%") and line.endswith("%"):
            flush()
            current = line[1:-1]
            continue
        values.append(line)
    flush()
    return fields


def parse_pacman_db(blob: bytes) -> dict[str, dict[str, object]]:
    """Index an MSYS2/pacman database blob by package name."""
    packages: dict[str, dict[str, object]] = {}
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:") as archive:
        for member in archive.getmembers():
            if not member.name.endswith("/desc"):
                continue
            handle = archive.extractfile(member)
            if handle is None:
                continue
            fields = _pacman_fields(handle.read().decode("utf-8", "replace"))
            name = (fields.get("NAME") or [""])[0]
            if not name:
                continue
            packages[name] = {
                "filename": (fields.get("FILENAME") or [""])[0],
                "version": (fields.get("VERSION") or [""])[0],
                "depends": tuple(fields.get("DEPENDS") or []),
                "provides": tuple(fields.get("PROVIDES") or []),
            }
    return packages


def _dep_name(spec: str) -> str:
    for operator in (">=", "<=", "=", ">", "<"):
        if operator in spec:
            return spec.split(operator, 1)[0]
    return spec


def dependency_closure(packages: dict[str, dict[str, object]], roots: tuple[str, ...]) -> list[str]:
    """Runtime DEPENDS of ``roots``, dependencies before the package that needs them.

    A dependency that is not itself a package is installed via the single package
    that PROVIDES that name. ``mingw-w64-clang-aarch64-cc-libs`` is that kind of
    name: libc++ provides it, and poppler's libraries are linked against it.
    """
    provided_by: dict[str, list[str]] = {}
    for package_name, package in packages.items():
        provides = package.get("provides", ())
        if not isinstance(provides, tuple):
            raise SystemExit(f"{package_name} has no provides list")
        for provided in provides:
            provided_by.setdefault(_dep_name(str(provided)), []).append(package_name)

    def resolve(name: str) -> str:
        if name in packages:
            return name
        choices = provided_by.get(name, [])
        if len(choices) == 1:
            return choices[0]
        if not choices:
            raise SystemExit(f"MSYS2 package {name} is not in the database")
        listed = ", ".join(sorted(choices))
        raise SystemExit(f"MSYS2 package {name} is provided by more than one package: {listed}")

    order: list[str] = []
    seen: set[str] = set()

    def visit(spec: str) -> None:
        name = _dep_name(spec)
        if name in seen:
            return
        seen.add(name)
        real = resolve(name)
        if real != name:
            if real in seen:
                return
            seen.add(real)
        package = packages[real]
        depends = package["depends"]
        if not isinstance(depends, tuple):
            raise SystemExit(f"{real} has no dependency list")
        for dependency in depends:
            visit(str(dependency))
        order.append(real)

    for root_name in roots:
        visit(root_name)
    return order


def _stage_python(target: Target, stage: Path, cache: Path, versions: dict[str, str]) -> None:
    name = (
        f"cpython-{versions['python']}+{versions['python_release']}-"
        f"{target.python_triple}-install_only_stripped.tar.gz"
    )
    url = (
        "https://github.com/astral-sh/python-build-standalone/releases/download/"
        f"{versions['python_release']}/{name}"
    )
    archive = cache / name
    _download(url, archive)
    _extract(archive, cache / f"python-{target.name}")
    extracted = cache / f"python-{target.name}" / "python"
    if not extracted.is_dir():
        raise SystemExit(f"python-build-standalone archive did not contain python/: {archive}")
    shutil.copytree(extracted, stage / "python")


def _stage_static_tools(target: Target, tools: Path, cache: Path, versions: dict[str, str]) -> None:
    extension = ".zip" if target.archive == "zip" else ".tar.gz"
    specs = (
        (
            "uv",
            (
                f"https://github.com/astral-sh/uv/releases/download/{versions['uv']}/"
                f"uv-{target.tool_triple}{extension}"
            ),
            {"uv", "uv.exe", "uvx", "uvx.exe"},
        ),
        (
            "ripgrep",
            (
                f"https://github.com/BurntSushi/ripgrep/releases/download/{versions['ripgrep']}/"
                f"ripgrep-{versions['ripgrep']}-{target.tool_triple}{extension}"
            ),
            {"rg", "rg.exe"},
        ),
        (
            "fd",
            (
                f"https://github.com/sharkdp/fd/releases/download/v{versions['fd']}/"
                f"fd-v{versions['fd']}-{target.tool_triple}{extension}"
            ),
            {"fd", "fd.exe"},
        ),
    )
    licenses = tools / "LICENSES"
    for label, url, names in specs:
        archive = cache / Path(urllib.parse.urlparse(url).path).name
        _download(url, archive)
        tree = cache / f"{label}-{target.name}"
        _extract(archive, tree)
        _copy_licenses(tree, licenses, label)
        copied = False
        for name in sorted(names):
            hits = [path for path in tree.rglob(name) if path.is_file()]
            if not hits:
                continue
            hits.sort(key=lambda path: len(path.parts))
            dest = tools / hits[0].name
            shutil.copy2(hits[0], dest)
            dest.chmod(dest.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            if name in {label, f"{label}.exe"} or name in {"uv", "uv.exe", "rg", "rg.exe", "fd", "fd.exe"}:
                copied = True
        if not copied:
            raise SystemExit(f"{label} binary was not in {archive}")


def _micromamba(target: Target, cache: Path) -> Path:
    archive = cache / f"micromamba-{target.conda_subdir}.tar.bz2"
    url = f"https://micro.mamba.pm/api/micromamba/{target.conda_subdir}/latest"
    _download(url, archive)
    tree = cache / f"micromamba-{target.conda_subdir}"
    _extract(archive, tree)
    return _find_named(tree, {"micromamba", "micromamba.exe"})


def _conda_create(micromamba: Path, prefix: Path, packages: list[str], cache: Path) -> None:
    if prefix.exists():
        shutil.rmtree(prefix)
    env = os.environ.copy()
    env["MAMBA_ROOT_PREFIX"] = str(cache / "mamba-root")
    env["CONDA_PKGS_DIRS"] = str(cache / "conda-pkgs")
    print(f"conda-forge {' '.join(packages)}", flush=True)
    subprocess.check_call(
        [str(micromamba), "create", "-y", "-p", str(prefix), "-c", "conda-forge", "--override-channels", *packages],
        env=env,
    )


def windows_vcvars_arch(machine: str) -> str | None:
    """MSVC target name for a Windows CPU. ARM64 stays ``arm64``."""
    return {"arm64": "arm64", "aarch64": "arm64", "amd64": "x64", "x86_64": "x64"}.get(machine.lower())


def _host_windows_arch() -> str | None:
    if sys.platform != "win32":
        return None
    return windows_vcvars_arch(platform.machine())


# COFF machine field. An x64 CRT object is 0x8664; an ARM64 one is 0xAA64.
_PE_MACHINE = {"arm64": 0xAA64, "x64": 0x8664}


def _pe_machine(path: Path) -> int | None:
    data = path.read_bytes()
    if len(data) < 0x40 or data[:2] != b"MZ":
        return None
    offset = int.from_bytes(data[0x3C:0x40], "little")
    if offset < 0 or offset + 6 > len(data) or data[offset : offset + 4] != b"PE\0\0":
        return None
    return int.from_bytes(data[offset + 4 : offset + 6], "little")


def _compiler_can_target(compiler: str, arch: str) -> bool:
    """``cl`` follows the active vcvars prompt. gcc/clang must already target ``arch``."""
    name = Path(compiler).name.lower()
    if name in {"cl", "cl.exe"}:
        return True
    completed = subprocess.run(
        [compiler, "-dumpmachine"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    machine = completed.stdout.strip().lower()
    if completed.returncode != 0 or not machine:
        return True
    if arch == "arm64":
        return "aarch64" in machine or "arm64" in machine
    if arch == "x64":
        return "x86_64" in machine or "amd64" in machine
    return True


def _compile_command(compiler: str, source: Path, dest: Path, arch: str | None) -> list[str]:
    name = Path(compiler).name.lower()
    if name in {"cl", "cl.exe"}:
        return [compiler, "/nologo", "/O2", "/W4", "/std:c11", str(source), f"/Fe:{dest}"]
    command = [compiler]
    if "clang" in name and arch == "arm64":
        command.append("--target=aarch64-pc-windows-msvc")
    elif "clang" in name and arch == "x64":
        command.append("--target=x86_64-pc-windows-msvc")
    command.extend(["-std=c11", "-O2", "-Wall", "-Wextra", "-o", str(dest), str(source)])
    return command


def _accept_launcher(dest: Path, arch: str | None) -> bool:
    expected = _PE_MACHINE.get(arch) if arch is not None else None
    if expected is None:
        return True
    actual = _pe_machine(dest)
    if actual == expected:
        return True
    got = "missing" if actual is None else f"{actual:#x}"
    print(f"launcher machine {got} is not {arch} ({expected:#x})", flush=True)
    dest.unlink(missing_ok=True)
    return False


def _compile_launcher(cache: Path) -> Path:
    source = ROOT / "scripts" / "toolwrap.c"
    dest_dir = cache / "toolwrap"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / _exe("toolwrap")
    if dest.exists():
        dest.unlink()
    arch = _host_windows_arch()
    compilers: list[str] = []
    for name in ("cc", "gcc", "clang", "cl"):
        found = shutil.which(name)
        if found:
            compilers.append(found)
    for compiler in compilers:
        if arch is not None and not _compiler_can_target(compiler, arch):
            print(f"skip {compiler}: it does not target {arch}", flush=True)
            continue
        if dest.exists():
            dest.unlink()
        completed = subprocess.run(
            _compile_command(compiler, source, dest, arch),
            cwd=dest_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
        if completed.returncode == 0 and dest.is_file() and _accept_launcher(dest, arch):
            dest.chmod(dest.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            return dest
        print(completed.stdout, flush=True)
    zig = _ensure_zig(cache)
    subprocess.check_call([str(zig), "cc", "-std=c11", "-O2", "-o", str(dest), str(source)])
    if not _accept_launcher(dest, arch):
        raise SystemExit(f"zig cc produced a launcher for the wrong architecture ({arch})")
    dest.chmod(dest.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return dest


def _zig_host() -> tuple[str, str]:
    machine = platform.machine().lower()
    arch = {"arm64": "aarch64", "aarch64": "aarch64", "x86_64": "x86_64", "amd64": "x86_64"}.get(machine)
    if arch is None:
        raise SystemExit(f"no zig build for {platform.machine()}")
    if sys.platform == "win32":
        return f"{arch}-windows", ".zip"
    if sys.platform == "darwin":
        return f"{arch}-macos", ".tar.xz"
    if sys.platform == "linux":
        return f"{arch}-linux", ".tar.xz"
    raise SystemExit(f"no zig build for {sys.platform}")


def _ensure_zig(cache: Path) -> Path:
    zig_target, extension = _zig_host()
    name = f"zig-{zig_target}-{ZIG_VERSION}{extension}"
    archive = cache / name
    _download(f"https://ziglang.org/download/{ZIG_VERSION}/{name}", archive)
    tree = cache / f"zig-{zig_target}"
    if not tree.exists():
        _extract(archive, tree)
    return _find_named(tree, {"zig", "zig.exe"})


def _place_wrapper(launcher: Path, dest: Path, lines: list[str]) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(launcher, dest)
    dest.chmod(dest.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    dest.with_name(dest.stem + ".wrap").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _wrap(exec_path: str, args: list[str], env: list[tuple[str, str]], prepend: list[str]) -> list[str]:
    lines = [f"exec={exec_path}"]
    lines.extend(f"arg={arg}" for arg in args)
    lines.extend(f"env={key}={value}" for key, value in env)
    lines.extend(f"path_prepend={path}" for path in prepend)
    return lines


# cryptography stopped publishing win_arm64 wheels after 46.0.3. That wheel is
# cp311-abi3, so the bundled CPython 3.13 loads it, and google-auth / pyjwt still
# accept it. tiktoken has never published a win_arm64 wheel; the cp313 build in
# release/wheels is the one this installer passes via --find-links.
WIN_ARM64_CRYPTOGRAPHY = "46.0.3"
VENDORED_WHEELS = ROOT / "release" / "wheels"


def windows_arm64_wheel_requirements(text: str) -> str:
    """Pin cryptography to the last release that has a win_arm64 wheel.

    Other lines, including tiktoken==0.14.0, stay at the locked version. A
    hashed cryptography block from ``uv export`` lists 50.0.0 files only, so
    those continuation lines are dropped with the pin.
    """
    lines = text.splitlines(keepends=True)
    rewritten: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        body = line.split("#", 1)[0].strip()
        name = body.rstrip("\\").strip()
        if name.startswith("cryptography=="):
            newline = "\n" if line.endswith("\n") or body.endswith("\\") else ""
            rewritten.append(f"cryptography=={WIN_ARM64_CRYPTOGRAPHY}{newline}")
            index += 1
            if body.endswith("\\"):
                while index < len(lines):
                    continuation = lines[index].split("#", 1)[0].strip()
                    index += 1
                    if not continuation.endswith("\\"):
                        break
            continue
        rewritten.append(line)
        index += 1
    return "".join(rewritten)


def pip_install_command(uv: Path, python: Path, windows_arch: str | None) -> list[str]:
    """Install command for the bundled interpreter.

    On Windows ARM64, refuse sdists. The index has no win_arm64 wheel for the
    locked cryptography or tiktoken, and building them wants OpenSSL or a Rust
    toolchain. Published and vendored wheels are the only inputs.
    """
    command = [str(uv), "pip", "install", "--python", str(python), "--link-mode", "copy"]
    if windows_arch == "arm64":
        command.extend(["--only-binary", ":all:", "--find-links", str(VENDORED_WHEELS)])
    return command


def _install_project(uv: Path, python: Path) -> None:
    env = os.environ.copy()
    env["PYTHONNOUSERSITE"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["UV_PYTHON_DOWNLOADS"] = "never"
    # setuptools reads VSCMD_ARG_TGT_ARCH before the interpreter. The x64 developer
    # prompt on an ARM64 runner otherwise builds wheels as win-amd64, and
    # setuptools-rust then passes --target x86_64-pc-windows-msvc.
    windows_arch = _host_windows_arch()
    if windows_arch is not None:
        env["VSCMD_ARG_TGT_ARCH"] = windows_arch
    requirements = ROOT / "dist" / "cache" / "requirements-providers.txt"
    export = [
        str(uv),
        "export",
        "--extra",
        "providers",
        "--no-dev",
        "--frozen",
        "--no-emit-project",
        "-o",
        str(requirements),
    ]
    if windows_arch == "arm64":
        # Lock hashes name cryptography 50.0.0 and the tiktoken sdist. Neither
        # matches the win_arm64 wheels this install uses.
        export.append("--no-hashes")
    exported = subprocess.run(
        export,
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    install = [str(uv), "pip", "install", "--python", str(python), "--link-mode", "copy"]
    # --only-binary applies to third-party requirements. The local project is
    # installed afterwards with --no-deps and is not a published wheel.
    dependencies = pip_install_command(uv, python, windows_arch)
    if exported.returncode == 0:
        if windows_arch == "arm64":
            rewritten = windows_arm64_wheel_requirements(requirements.read_text(encoding="utf-8"))
            pin = f"cryptography=={WIN_ARM64_CRYPTOGRAPHY}"
            if pin not in rewritten:
                raise SystemExit(f"windows-arm64 requirements did not pin {pin}")
            requirements.write_text(rewritten, encoding="utf-8")
            print(f"windows-arm64: {pin} wheel and vendored tiktoken win_arm64 wheel", flush=True)
        subprocess.check_call([*dependencies, "-r", str(requirements)], env=env)
        subprocess.check_call([*install, "--no-deps", str(ROOT)], cwd=ROOT, env=env)
        return
    print(exported.stdout, flush=True)
    print("uv export could not use the lock; installing misaka[providers] from the index", flush=True)
    if windows_arch == "arm64":
        constraint = requirements.with_name("windows-arm64-wheels.txt")
        constraint.write_text(f"cryptography=={WIN_ARM64_CRYPTOGRAPHY}\n", encoding="utf-8")
        subprocess.check_call([*dependencies, "-c", str(constraint), ".[providers]"], cwd=ROOT, env=env)
        return
    subprocess.check_call([*install, ".[providers]"], cwd=ROOT, env=env)


def _write_bundled(
    stage: Path,
    version: str,
    target: Target,
    versions: dict[str, str],
    git_version: str,
    poppler_version: str,
    poppler_origin: str,
) -> None:
    text = "\n".join(
        (
            f"MISAKA {version}",
            f"target: {target.name}",
            f"python: {versions['python']} (astral-sh/python-build-standalone {versions['python_release']})",
            "",
            "Tools required by the README, in tools/, on PATH when bin/misaka runs:",
            f"uv         {versions['uv']}     https://github.com/astral-sh/uv",
            f"git        {git_version}     https://git-scm.com (conda-forge, GPL-2.0)",
            f"rg         {versions['ripgrep']}     ripgrep, https://github.com/BurntSushi/ripgrep",
            f"fd         {versions['fd']}     https://github.com/sharkdp/fd",
            f"pdftotext  {poppler_version}     poppler, https://poppler.freedesktop.org ({poppler_origin}, GPL-2.0-or-later)",
            "",
            "Other poppler utilities from that package (pdfinfo, pdftoppm, and the rest) are on PATH too.",
            "ocrmypdf, DjVuLibre and LibreOffice stay optional and are not in this archive.",
            "Third-party licences for the bundled tools are under tools/LICENSES/.",
            "",
        )
    )
    (stage / "BUNDLED.txt").write_text(text, encoding="utf-8")


def _smoke(stage: Path, version: str) -> None:
    home = stage.parent / "smoke-home"
    if home.exists():
        shutil.rmtree(home)
    home.mkdir()
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    env["MISAKA_HOME"] = str(home / "misaka")
    env["GIT_CONFIG_GLOBAL"] = str(home / "gitconfig")
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env.pop("GIT_EXEC_PATH", None)
    env.pop("GIT_TEMPLATE_DIR", None)
    tools = stage / "tools"
    launcher = stage / "bin" / _exe("misaka")

    def run(command: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        print("+", " ".join(command), flush=True)
        completed = subprocess.run(
            command,
            cwd=cwd,
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=180,
            check=False,
        )
        if completed.returncode != 0:
            raise SystemExit(
                f"{command} exited {completed.returncode}\n{completed.stdout}\n{completed.stderr}"
            )
        return completed

    version_out = run([str(launcher), "--version"])
    if version not in version_out.stdout:
        raise SystemExit(f"misaka --version did not report {version}: {version_out.stdout!r}")
    for name in ("uv", "rg", "fd", "git"):
        run([str(tools / _exe(name)), "--version"])
    poppler = run([str(tools / _exe("pdftotext")), "-v"])
    if "poppler" not in (poppler.stdout + poppler.stderr).lower():
        raise SystemExit(poppler.stdout + poppler.stderr)
    project = home / "project"
    project.mkdir()
    init = run([str(tools / _exe("git")), "init"], cwd=project)
    if "templates not found" in init.stderr:
        raise SystemExit(init.stderr)
    exec_path = Path(run([str(tools / _exe("git")), "--exec-path"]).stdout.strip()).resolve()
    if not str(exec_path).startswith(str(stage.resolve())):
        raise SystemExit(f"git --exec-path left the bundle: {exec_path}")
    pdf_path = home / "hello.pdf"
    pdf_path.write_bytes(HELLO_PDF)
    text = run([str(tools / _exe("pdftotext")), str(pdf_path), "-"])
    if "Hello" not in text.stdout:
        raise SystemExit(f"pdftotext did not read the sample PDF:\n{text.stdout}\n{text.stderr}")


def build(target_name: str) -> Path:
    if target_name != host_target():
        raise SystemExit(
            f"this machine builds {host_target()}, not {target_name}. "
            "Each archive is built on its own architecture."
        )
    target = TARGETS[target_name]
    versions = tool_versions()
    version = project_version()
    folder = f"misaka-{version}-{target.name}"
    cache = ROOT / "dist" / "cache"
    stage = ROOT / "dist" / "work" / folder
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)
    (stage / "bin").mkdir()
    (stage / "tools").mkdir()
    print(f"building {folder}", flush=True)
    _stage_python(target, stage, cache, versions)
    _stage_static_tools(target, stage / "tools", cache, versions)
    prefix = stage / "opt" / "conda"
    packages = ["git"] if target.poppler == "msys2" else ["git", "poppler"]
    _conda_create(_micromamba(target, cache), prefix, packages, cache)
    _break_hardlinks(prefix)
    _slim_prefix(prefix)
    prefixes = [prefix]
    poppler_version = ""
    poppler_origin = "conda-forge"
    msys2_poppler: Path | None = None
    if target.poppler == "msys2":
        msys2_poppler = stage / "opt" / "poppler"
        subprocess.check_call([sys.executable, "-m", "pip", "install", "zstandard"])
        subprocess.check_call(
            [sys.executable, str(ROOT / "scripts" / "msys2_poppler.py"), str(msys2_poppler), str(cache)]
        )
        poppler_version = (msys2_poppler / "VERSION").read_text(encoding="utf-8").strip()
        poppler_origin = "MSYS2 clangarm64"
    else:
        poppler_version = _conda_version(prefix, "poppler")
    poppler_roots = poppler_tool_roots(prefix, msys2_poppler)
    git_version = _conda_version(prefix, "git")
    bin_dir = stage / "bin"
    tools = stage / "tools"
    launcher = _compile_launcher(cache)
    git_exec = _find_git_exec(prefix)
    git_templates = _find_git_templates(prefix)
    poppler_data = _find_poppler_data(poppler_roots)
    shared_env = [
        ("GIT_EXEC_PATH", _rel(bin_dir, git_exec)),
        ("GIT_TEMPLATE_DIR", _rel(bin_dir, git_templates)),
    ]
    if poppler_data is not None:
        shared_env.append(("POPPLER_DATADIR", _rel(bin_dir, poppler_data)))
    _place_wrapper(
        launcher,
        bin_dir / _exe("misaka"),
        _wrap(
            _rel(bin_dir, python_executable(stage)),
            ["-m", "misaka"],
            # ``-m`` puts the working directory first on sys.path, so a folder holding a
            # ``misaka`` package (a source checkout) would be run instead of the archive's own.
            [*shared_env, ("PYTHONNOUSERSITE", "1"), ("PYTHONSAFEPATH", "1")],
            ["../tools"],
        ),
    )
    tool_env = [
        ("GIT_EXEC_PATH", _rel(tools, git_exec)),
        ("GIT_TEMPLATE_DIR", _rel(tools, git_templates)),
    ]
    if poppler_data is not None:
        tool_env.append(("POPPLER_DATADIR", _rel(tools, poppler_data)))
    _place_wrapper(
        launcher,
        tools / _exe("git"),
        _wrap(_rel(tools, _find_program(prefixes, "git")), [], tool_env, []),
    )
    programs = _poppler_programs(poppler_roots)
    if not any(path.name.lower().removesuffix(".exe") == "pdftotext" for path in programs):
        raise SystemExit(f"poppler pdftotext was not staged under {poppler_roots}")
    for program in programs:
        stem = program.name[:-4] if program.name.lower().endswith(".exe") else program.name
        if stem == "pdftotext":
            print(f"pdftotext {program}", flush=True)
        program_env = tool_env if stem == "pdftotext" or poppler_data is not None else []
        _place_wrapper(
            launcher,
            tools / _exe(stem),
            _wrap(_rel(tools, program), [], program_env, []),
        )
    for name in ("LICENSE", "NOTICE", "THIRD_PARTY_NOTICES.md"):
        shutil.copy2(ROOT / name, stage / name)
    shutil.copytree(ROOT / "LICENSES", stage / "LICENSES", dirs_exist_ok=True)
    _write_bundled(stage, version, target, versions, git_version, poppler_version, poppler_origin)
    _install_project(tools / _exe("uv"), python_executable(stage))
    _smoke(stage, version)
    archive_path = _archive(stage, target.archive)
    print(f"wrote {archive_path}", flush=True)
    return archive_path


def _archive(stage: Path, kind: ArchiveKind) -> Path:
    out = ROOT / "dist"
    out.mkdir(parents=True, exist_ok=True)
    match kind:
        case "tar.gz":
            dest = out / f"{stage.name}.tar.gz"
            if dest.exists():
                dest.unlink()
            with tarfile.open(dest, "w:gz") as archive:
                archive.add(stage, arcname=stage.name)
        case "zip":
            dest = out / f"{stage.name}.zip"
            if dest.exists():
                dest.unlink()
            with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for path in sorted(stage.rglob("*")):
                    archive.write(path, path.relative_to(stage.parent).as_posix())
        case other:
            assert_never(other)
    return dest


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_archive_name(name: str) -> tuple[str, str]:
    """Return ``(version, target)`` for ``misaka-<version>-<target>.tar.gz`` or ``.zip``."""
    for target_name, target in TARGETS.items():
        suffix = f"-{target_name}{'.tar.gz' if target.archive == 'tar.gz' else '.zip'}"
        prefix = "misaka-"
        if name.startswith(prefix) and name.endswith(suffix):
            return name[len(prefix) : -len(suffix)], target_name
    raise SystemExit(f"not a MISAKA release archive name: {name}")


def _release_url(version: str, filename: str) -> str:
    return f"https://github.com/{GITHUB_REPO}/releases/download/v{version}/{filename}"


def render_homebrew(version: str, hashes: dict[str, str]) -> str:
    """A formula that installs the CLI archive. Hashes may be 64 zeros before the first release."""

    def block(target_name: str) -> str:
        filename = f"misaka-{version}-{target_name}.tar.gz"
        digest = hashes[target_name]
        return f'      url "{_release_url(version, filename)}"\n      sha256 "{digest}"'

    return f"""# sha256 values of 64 zeros are placeholders. The release workflow rewrites them
# from the built archives and publishes this file to the Homebrew tap.
class Misaka < Formula
  desc "A research team of AI agents for the humanities and social sciences"
  homepage "https://github.com/{GITHUB_REPO}"
  version "{version}"
  license "Apache-2.0"

  on_macos do
    on_arm do
{block("darwin-arm64")}
    end

    on_intel do
{block("darwin-x86_64")}
    end
  end

  on_linux do
    on_arm do
{block("linux-arm64")}
    end

    on_intel do
{block("linux-x86_64")}
    end
  end

  def install
    libexec.install Dir["*"]
    bin.install_symlink libexec/"bin/misaka"
  end

  def caveats
    <<~EOS
      uv, git, ripgrep (rg), fd and poppler (pdftotext) ship inside the bundle.
      Running misaka puts them on PATH for that process. They are not linked into Homebrew's bin.
    EOS
  end

  test do
    assert_match version.to_s, shell_output("#{{bin}}/misaka --version")
  end
end
"""


def render_winget(version: str, hashes: dict[str, str]) -> dict[str, str]:
    """The three winget manifests, keyed by filename."""
    identifier = "Luciole-Studio.Misaka"

    def installer(target_name: str, architecture: str, minimum: str) -> str:
        filename = f"misaka-{version}-{target_name}.zip"
        relative = f"misaka-{version}-{target_name}/bin/misaka.exe"
        return f"""  - Architecture: {architecture}
    MinimumOSVersion: {minimum}
    InstallerUrl: {_release_url(version, filename)}
    InstallerSha256: {hashes[target_name]}
    NestedInstallerFiles:
      - RelativeFilePath: {relative}
        PortableCommandAlias: misaka
"""

    version_yaml = f"""PackageIdentifier: {identifier}
PackageVersion: {version}
DefaultLocale: en-US
ManifestType: version
ManifestVersion: 1.10.0
"""
    installer_yaml = f"""PackageIdentifier: {identifier}
PackageVersion: {version}
InstallerType: zip
NestedInstallerType: portable
ArchiveBinariesDependOnPath: true
Installers:
{installer("windows-x86_64", "x64", "10.0.17763.0")}
{installer("windows-arm64", "arm64", "10.0.22000.0")}
ManifestType: installer
ManifestVersion: 1.10.0
"""
    locale_yaml = f"""PackageIdentifier: {identifier}
PackageVersion: {version}
PackageLocale: en-US
Publisher: Luciole Studio
PublisherUrl: https://github.com/Luciole-Studio
PackageName: MISAKA
PackageUrl: https://github.com/{GITHUB_REPO}
License: Apache-2.0
LicenseUrl: https://github.com/{GITHUB_REPO}/blob/main/LICENSE
ShortDescription: A research team of AI agents for the humanities and social sciences
Description: >-
  MISAKA is a research team of AI agents for the humanities and social sciences.
  The Windows archive bundles uv, git, ripgrep (rg), fd and poppler (pdftotext)
  and puts them on PATH when misaka runs.
ManifestType: defaultLocale
ManifestVersion: 1.10.0
"""
    return {
        f"{identifier}.yaml": version_yaml,
        f"{identifier}.installer.yaml": installer_yaml,
        f"{identifier}.locale.en-US.yaml": locale_yaml,
    }


def render_notes(version: str) -> str:
    rows = "\n".join(
        f"| `{_archive_filename(version, name)}` | `{target.runner}` |" for name, target in TARGETS.items()
    )
    return f"""MISAKA {version}

Each archive is one architecture and includes the tools named in the README: uv, git, ripgrep (`rg`), fd and poppler (`pdftotext`). `bin/misaka` puts `tools/` on PATH for that process.

| Archive | Built on |
|---|---|
{rows}

Install with `scripts/install.sh` (macOS and Linux) or `scripts/install.ps1` (Windows). Homebrew and winget manifests are attached; publishing them needs the tap repository and a winget submission, described in `release/README.md`.
"""


def _archive_filename(version: str, target_name: str) -> str:
    target = TARGETS[target_name]
    match target.archive:
        case "tar.gz":
            return f"misaka-{version}-{target_name}.tar.gz"
        case "zip":
            return f"misaka-{version}-{target_name}.zip"
        case other:
            assert_never(other)


def placeholder_hashes() -> dict[str, str]:
    return {name: "0" * 64 for name in TARGETS}


def render_distribution(archive_dir: Path, publish_dir: Path) -> None:
    """Write SHA256SUMS, the Homebrew formula and the winget manifests for archives in ``archive_dir``."""
    archives = [path for path in archive_dir.iterdir() if path.is_file() and path.name.startswith("misaka-")]
    hashes: dict[str, str] = {}
    version: str | None = None
    for path in archives:
        found_version, target_name = parse_archive_name(path.name)
        if version is None:
            version = found_version
        elif version != found_version:
            raise SystemExit(f"mixed versions in {archive_dir}: {version} and {found_version}")
        hashes[target_name] = _sha256(path)
    if version is None:
        raise SystemExit(f"no release archives in {archive_dir}")
    missing = [name for name in TARGETS if name not in hashes]
    if missing:
        raise SystemExit(f"missing archives for {', '.join(missing)}")
    publish_dir.mkdir(parents=True, exist_ok=True)
    lines = [f"{hashes[name]}  {_archive_filename(version, name)}" for name in TARGETS]
    (publish_dir / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (publish_dir / "misaka.rb").write_text(render_homebrew(version, hashes), encoding="utf-8")
    (publish_dir / "NOTES.md").write_text(render_notes(version), encoding="utf-8")
    winget_root = publish_dir / "winget" / "manifests" / "l" / "Luciole-Studio" / "Misaka" / version
    winget_root.mkdir(parents=True, exist_ok=True)
    for filename, text in render_winget(version, hashes).items():
        (winget_root / filename).write_text(text, encoding="utf-8")
    print(f"wrote distribution files to {publish_dir}", flush=True)


def checked_in_distribution() -> None:
    """Refresh the in-repo formula and winget manifests. Hashes stay placeholders until a release."""
    version = project_version()
    hashes = placeholder_hashes()
    formula = ROOT / "release" / "homebrew" / "misaka.rb"
    formula.parent.mkdir(parents=True, exist_ok=True)
    formula.write_text(render_homebrew(version, hashes), encoding="utf-8")
    winget_root = ROOT / "release" / "winget" / "manifests" / "l" / "Luciole-Studio" / "Misaka" / version
    winget_root.mkdir(parents=True, exist_ok=True)
    for filename, text in render_winget(version, hashes).items():
        (winget_root / filename).write_text(text, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the MISAKA release archive for this machine.")
    parser.add_argument("--target", choices=sorted(TARGETS), help="defaults to this machine")
    parser.add_argument(
        "--render-checksums",
        type=Path,
        help="write SHA256SUMS, the Homebrew formula and winget manifests for archives in this directory",
    )
    parser.add_argument("--publish-dir", type=Path, help="where --render-checksums writes (default dist/publish)")
    parser.add_argument(
        "--write-placeholders",
        action="store_true",
        help="rewrite release/homebrew and release/winget with placeholder sha256 values",
    )
    args = parser.parse_args(argv)
    if args.write_placeholders:
        checked_in_distribution()
        return 0
    if args.render_checksums:
        render_distribution(args.render_checksums, args.publish_dir or (ROOT / "dist" / "publish"))
        return 0
    build(args.target or host_target())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
