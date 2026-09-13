"""LibreOffice, when the machine has it: recalculation, legacy formats, PDF export.

Owner decision D3 rules out bundling an external program, so every capability here is
optional and each one says so plainly when LibreOffice is absent. Never pretend: a formula
whose value was not computed reads back as ``uncached``, a .doc that cannot be converted is
refused by name with the command that would fix it, and an export that cannot run does not
report a PDF nobody wrote.

The shape follows ``index._ocr_pages`` (audit: the same optional-binary problem, solved once
already): probe with ``shutil.which``, run under a timeout in a temp directory, write the
failure into ``meta`` so the refusal can quote it, and return ``None`` so the caller decides
what that means.

Every call gets its own ``-env:UserInstallation`` profile. LibreOffice keeps one shared
profile per user and locks it, so two concurrent conversions -- which is the normal case
here, since research cards run N-way parallel -- would deadlock on each other. Borrowed from
FrontierAgent ``plugins/tools/_writer_core.py:126 _export_pdf``.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile

BINARY = "soffice"

# macOS installs the app bundle without putting anything on PATH, which is the default
# outcome of ``brew install --cask libreoffice``.
_MACOS_PATH = "/Applications/LibreOffice.app/Contents/MacOS/soffice"

# One conversion of a large workbook, with the profile creation of a first run inside it.
TIMEOUT = 300

# What each legacy format converts to.
LEGACY = {".doc": "docx", ".xls": "xlsx", ".ppt": "pptx"}

# Named once so a refusal and its test cannot drift apart. Only the macOS command: guiding
# other platforms is the installer wizard's job (owner decision D4).
INSTALL_HINT = "install LibreOffice (brew install --cask libreoffice)"

_ERROR_CHARS = 200


def binary():
    """The LibreOffice executable, or ``None``.

    Not cached: a user who installs LibreOffice mid-session should not have to restart to
    get the capability, and the probe is a PATH lookup.
    """
    found = shutil.which(BINARY)
    if found:
        return found
    return _MACOS_PATH if os.path.exists(_MACOS_PATH) else None


def _note(meta, key, value):
    if meta is not None:
        meta[key] = value


def _run(arguments, *, timeout, meta, directory):
    """Run one LibreOffice command in a private profile. True when it exited cleanly."""
    executable = binary()
    if executable is None:
        return False
    with tempfile.TemporaryDirectory(prefix=".soffice-", dir=directory) as profile:
        try:
            completed = subprocess.run(
                [executable, "--headless", "--norestore",
                 f"-env:UserInstallation=file://{profile}/profile", *arguments],
                capture_output=True, text=True, timeout=timeout, check=False)
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            _note(meta, "soffice_error", str(error)[:_ERROR_CHARS])
            return False
        if completed.returncode != 0:
            _note(meta, "soffice_error",
                  " ".join((completed.stderr or "").split())[-_ERROR_CHARS:]
                  or f"exit {completed.returncode}")
            return False
    return True


def _converted(source, target_suffix, into):
    """Where ``--convert-to`` puts its output: the source's stem, the new suffix."""
    stem = os.path.splitext(os.path.basename(str(source)))[0]
    return os.path.join(into, f"{stem}.{target_suffix}")


def convert(source, target_suffix, *, into, timeout=TIMEOUT, meta=None):
    """Convert one file into ``into``. Returns the new path, or ``None``.

    ``into`` is the caller's temp directory rather than one made here, so the result
    outlives this call -- a directory made and cleaned up inside would delete the file it
    just produced.
    """
    if binary() is None:
        return None
    try:
        os.makedirs(into, exist_ok=True)
    except OSError as error:
        _note(meta, "soffice_error", str(error)[:_ERROR_CHARS])
        return None
    if not _run(["--convert-to", target_suffix, "--outdir", str(into),
                 os.path.abspath(str(source))], timeout=timeout, meta=meta, directory=into):
        return None
    produced = _converted(source, target_suffix, str(into))
    if os.path.exists(produced):
        return produced
    _note(meta, "soffice_error", f"no {target_suffix} produced from "
                                 f"{os.path.basename(str(source))}")
    return None


def recalc(path, *, into, timeout=TIMEOUT, meta=None):
    """A copy of a workbook with its formula results filled in, or ``None``.

    openpyxl stores a formula string and cannot store its result, so a workbook misaka
    writes carries formulas whose cached values are empty; converting it through
    LibreOffice recomputes them on the way out.

    The **copy** is the point on the read side: the corpus identifies a document by the
    sha256 of the file on disk, so recalculating in place would change a document's
    identity as a side effect of reading it. The caller renders the copy and keeps the
    original untouched.
    """
    if binary() is None:
        return None
    staging = os.path.join(str(into), os.path.basename(str(path)))
    if os.path.abspath(staging) != os.path.abspath(str(path)):
        try:
            shutil.copy2(str(path), staging)
        except OSError as error:
            _note(meta, "soffice_error", str(error)[:_ERROR_CHARS])
            return None
    # Converting a .xlsx to .xlsx re-saves it, and LibreOffice recalculates on the way.
    # FrontierAgent drives a Basic macro (``calculateAll`` then ``store``) and falls back
    # to this when the macro silently does nothing; the fallback is the whole mechanism
    # here, because it needs no macro framework, no writable HOME and no first-run profile
    # -- three things that fail differently on every machine.
    return convert(staging, "xlsx", into=into, timeout=timeout, meta=meta)


def export_pdf(source, out, *, timeout=TIMEOUT, meta=None):
    """Render a document to PDF at ``out``. True when the file is there."""
    if binary() is None:
        return False
    destination = os.path.dirname(os.path.abspath(str(out))) or "."
    os.makedirs(destination, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".pdf-", dir=destination) as staging:
        produced = convert(source, "pdf", into=staging, timeout=timeout, meta=meta)
        if produced is None:
            return False
        try:
            shutil.move(produced, str(out))
        except OSError as error:
            _note(meta, "soffice_error", str(error)[:_ERROR_CHARS])
            return False
    return True
