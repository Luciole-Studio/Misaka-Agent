"""Every module of the package imports.

A module that is only imported lazily -- from inside a function, on the path that needs it -- fails
nowhere else, and no other test notices. Found 2026-09-29: the move of timeouts into settings.json
turned ``misaka.core.mcp.CALL_TIMEOUT`` and ``INIT_TIMEOUT`` into functions, the network MCP
transport and the MCP resource tools still imported the constants, and every HTTP/SSE MCP server
and every resource read would have failed at first use. The walk runs in a child process so that
importing everything leaves no state behind for the other tests."""
import subprocess
import sys

WALK = r"""
import importlib, pkgutil, sys
import misaka
# The vendored LCM upstream is checked against its own pins; its modules are not misaka's.
SKIP = ("misaka.extensions.misaka_lcm.vendor", "misaka.extensions.misaka_lcm.native")
# Windows-only modules, imported only there.
WINDOWS_ONLY = {
    "misaka.env.daemon.system.windows",
    "misaka.ui.panel.conpty",
    "misaka.utils.libuv_windows",
    "misaka.utils.win_clipboard",
    "misaka.utils.win_console",
}
failed = []
for info in pkgutil.walk_packages(misaka.__path__, "misaka.", onerror=lambda name: failed.append(name)):
    name = info.name
    if name.startswith(SKIP) or ".tests" in name or ".assets" in name or name.endswith(".__main__"):
        continue
    if name in WINDOWS_ONLY and sys.platform != "win32":
        continue
    try:
        importlib.import_module(name)
    except BaseException as error:
        failed.append(f"{name}: {type(error).__name__}: {error}")
print("\n".join(failed))
sys.exit(1 if failed else 0)
"""


def test_every_module_of_the_package_imports():
    result = subprocess.run([sys.executable, "-c", WALK], capture_output=True, text=True, timeout=300, check=False)
    assert result.returncode == 0, result.stdout + result.stderr[-2000:]
