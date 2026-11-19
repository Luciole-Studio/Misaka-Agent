"""The vendored PageIndex tree is upstream's bytes.

It was once edited in place under a lint pass, 72 of 79 files, and from then on no upstream fix
could be taken without a hand merge: a crash upstream had fixed in one line stayed in for
months. MISAKA's own code lives beside the tree, in ``pageindex/__init__.py``."""
import hashlib
from pathlib import Path

from misaka.core.documents import pageindex

# VectifyAI/PageIndex at 340b8535a219ea448c4c0f5bdd354761449b55b3: pageindex/flash without
# README.md and assets/, and pageindex/naming.py.
DIGEST = "4ecf1c0eb2532da3fff7e5d66051c62f2a941e3e189bc4933af576aa3d29a558"


def vendored_digest():
    root = Path(pageindex.__file__).parent
    files = sorted(path for path in [*(root / "flash").rglob("*"), root / "naming.py"]
                   if path.is_file() and "__pycache__" not in path.parts)
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.relative_to(root).as_posix().encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


def test_the_vendored_tree_is_upstreams_bytes():
    assert vendored_digest() == DIGEST, (
        "misaka/core/documents/pageindex/flash or naming.py differs from the upstream commit named "
        "above. Put MISAKA's change in pageindex/__init__.py instead. After copying a newer upstream "
        "over them, set DIGEST to the value on the left and name the new commit.")
