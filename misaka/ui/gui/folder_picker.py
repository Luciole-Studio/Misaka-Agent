"""OS folder picker, run in its own process so HTTP worker threads never own UI."""
from __future__ import annotations

import json
import os
import sys


def windows_folder(initial):
    """Windows' modern IFileOpenDialog in folder mode (no Tk dependency)."""
    import ctypes as c
    from ctypes import wintypes as w
    import uuid

    def guid(value):
        return (c.c_byte * 16).from_buffer_copy(uuid.UUID(value).bytes_le)

    def call(obj, slot, *types):
        table = c.cast(obj, c.POINTER(c.POINTER(c.c_void_p))).contents
        return c.WINFUNCTYPE(c.c_long, c.c_void_p, *types)(table[slot])

    def check(result):
        if result < 0:
            raise OSError(f"系统文件夹选择失败 (0x{result & 0xffffffff:08x})")

    ole = c.OleDLL("ole32")
    ole.CoInitializeEx(None, 2)
    user = c.WinDLL("user32", use_last_error=True)
    user.GetForegroundWindow.restype = w.HWND
    user.GetWindowRect.argtypes = [w.HWND, c.POINTER(w.RECT)]
    user.CreateWindowExW.argtypes = [w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD,
                                   c.c_int, c.c_int, c.c_int, c.c_int,
                                   w.HWND, w.HMENU, w.HINSTANCE, c.c_void_p]
    user.CreateWindowExW.restype = w.HWND
    user.DestroyWindow.argtypes = [w.HWND]
    bounds = w.RECT()
    foreground = user.GetForegroundWindow()
    user.GetWindowRect(foreground, c.byref(bounds))
    # A temporary topmost tool owner keeps the picker above its initiating
    # browser. It has no taskbar entry and never disables an unrelated app.
    owner = user.CreateWindowExW(0x8 | 0x80, "STATIC", "Misaka 文件夹选择", 0x80000000,
                                 bounds.left, bounds.top, max(1, bounds.right - bounds.left),
                                 max(1, bounds.bottom - bounds.top), None, None, None, None)
    if not owner:
        ole.CoUninitialize()
        raise c.WinError(c.get_last_error())
    dialog, folder, chosen, name = c.c_void_p(), c.c_void_p(), c.c_void_p(), c.c_void_p()
    shell_id = guid("43826d1e-e718-42ee-bc55-a1e261c37bfe")
    try:
        ole.CoCreateInstance(guid("dc1c5a9c-e88a-4dde-a5a1-60f82a20aef7"), None, 1,
                             guid("d57c7288-d4ad-4768-be02-9d969532d960"), c.byref(dialog))
        options = w.DWORD()
        check(call(dialog, 10, c.POINTER(w.DWORD))(dialog, c.byref(options)))
        check(call(dialog, 9, w.DWORD)(dialog, options.value | 0x20 | 0x40 | 0x800 | 0x8))
        check(call(dialog, 17, w.LPCWSTR)(dialog, "选择项目文件夹"))
        check(call(dialog, 18, w.LPCWSTR)(dialog, "添加项目"))
        if initial and os.path.isdir(initial):
            c.OleDLL("shell32").SHCreateItemFromParsingName(c.c_wchar_p(initial), None, shell_id, c.byref(folder))
            check(call(dialog, 12, c.c_void_p)(dialog, folder))
        result = call(dialog, 3, w.HWND)(dialog, owner)
        if result & 0xffffffff == 0x800704c7:
            return None
        check(result)
        check(call(dialog, 20, c.POINTER(c.c_void_p))(dialog, c.byref(chosen)))
        check(call(chosen, 5, w.DWORD, c.POINTER(c.c_void_p))(chosen, 0x80058000, c.byref(name)))
        return c.wstring_at(name)
    finally:
        if name:
            ole.CoTaskMemFree(name)
        for obj in (chosen, folder, dialog):
            if obj:
                call(obj, 2)(obj)
        user.DestroyWindow(owner)
        ole.CoUninitialize()


def choose(initial):
    if os.name == "nt":
        return windows_folder(initial)
    from tkinter import Tk, filedialog
    root = Tk()
    root.withdraw()
    try:
        return filedialog.askdirectory(parent=root, title="选择项目文件夹", initialdir=initial, mustexist=True) or None
    finally:
        root.destroy()


if __name__ == "__main__":
    try:
        print(json.dumps({"path": choose(sys.argv[1] if len(sys.argv) > 1 else "")}, ensure_ascii=False))
    except Exception as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False))
        sys.exit(1)
