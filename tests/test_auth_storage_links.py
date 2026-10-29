"""auth.json may be a symlink (a dotfiles repo) or carry a second hard link (a backup tool).

Both used to make every credential unreadable: the store refused anything but a lone regular
file. What protects the credentials is that the file really is a regular file owned by this
user; that is still enforced, on the file the link names.
"""
import json
import os

import pytest

from misaka.ai.auth.types import ApiKeyCredential
from misaka.core.auth_storage import AuthStorage, FileAuthStorageBackend

KEY = {"type": "api_key", "key": "sk-test"}


def _seed(path):
    path.write_text(json.dumps({"anthropic": KEY}), encoding="utf-8")
    os.chmod(path, 0o600)


async def _store(storage, provider, credential):
    async def replace(_current):
        return credential

    await storage.modify(provider, replace)


async def test_a_symlinked_store_is_read_and_written_through_the_link(tmp_path):
    real = tmp_path / "dotfiles" / "auth.json"
    real.parent.mkdir()
    _seed(real)
    link = tmp_path / "home" / "auth.json"
    link.parent.mkdir()
    link.symlink_to(real)

    storage = AuthStorage.create(str(link))
    assert (await storage.read("anthropic")).key == KEY["key"]
    await _store(storage, "openai", ApiKeyCredential(key="sk-other"))

    # Compared resolved: Windows reports an absolute link's target in its `\\?\` form.
    assert link.is_symlink() and link.resolve() == real.resolve()
    assert json.loads(real.read_text(encoding="utf-8"))["openai"]["key"] == "sk-other"
    assert (await AuthStorage.create(str(link)).read("openai")).key == "sk-other"


async def test_a_hard_linked_store_is_read_and_written(tmp_path):
    path = tmp_path / "auth.json"
    _seed(path)
    os.link(path, tmp_path / "auth.json.backup")

    storage = AuthStorage.create(str(path))
    assert (await storage.read("anthropic")).key == KEY["key"]
    await _store(storage, "openai", ApiKeyCredential(key="sk-other"))
    assert json.loads(path.read_text(encoding="utf-8"))["openai"]["key"] == "sk-other"


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs FIFOs")
def test_a_link_to_a_fifo_is_refused_without_blocking(tmp_path):
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    link = tmp_path / "auth.json"
    link.symlink_to(fifo)
    with pytest.raises(RuntimeError, match="regular file"):
        FileAuthStorageBackend(str(link))._read_file()


def test_a_link_to_a_directory_is_refused(tmp_path):
    (tmp_path / "somewhere").mkdir()
    link = tmp_path / "auth.json"
    link.symlink_to(tmp_path / "somewhere")
    with pytest.raises(RuntimeError, match="regular file"):
        FileAuthStorageBackend(str(link))._read_file()


def test_an_empty_store_is_still_refused(tmp_path):
    path = tmp_path / "auth.json"
    path.write_text("", encoding="utf-8")
    with pytest.raises(RuntimeError, match="empty"):
        FileAuthStorageBackend(str(path))._read_file()
