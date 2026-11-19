"""Whether the panel can run is decided by loading its terminal library, not by finding the file."""
from misaka.ui.panel import ghostty


def test_a_build_this_system_cannot_load_counts_as_unavailable(tmp_path, monkeypatch):
    build = tmp_path / "libghostty-vt.so"
    build.write_bytes(b"not a shared library")
    monkeypatch.setenv("MISAKA_GHOSTTY_VT", str(build))
    monkeypatch.setattr(ghostty, "_LIB", None)
    reason = ghostty.unavailable()
    assert reason and "libghostty-vt is not available" in reason


def test_the_shipped_build_loads_on_this_platform(monkeypatch):
    monkeypatch.delenv("MISAKA_GHOSTTY_VT", raising=False)
    monkeypatch.setattr(ghostty, "_LIB", None)
    assert ghostty.unavailable() is None


def test_windows_takes_the_build_for_the_interpreter_not_the_cpu(monkeypatch):
    monkeypatch.delenv("MISAKA_GHOSTTY_VT", raising=False)
    monkeypatch.setattr(ghostty.sys, "platform", "win32")
    monkeypatch.setattr(ghostty.platform, "machine", lambda: "ARM64")
    monkeypatch.setattr(ghostty.sysconfig, "get_platform", lambda: "win-amd64")
    assert ghostty.library_path().endswith("libghostty-vt-windows-x86_64.dll")
    monkeypatch.setattr(ghostty.sysconfig, "get_platform", lambda: "win-arm64")
    assert ghostty.library_path().endswith("libghostty-vt-windows-arm64.dll")
