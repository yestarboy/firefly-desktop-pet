# -*- coding: utf-8 -*-
"""
给指定窗口单独设置 AppUserModelID（任务栏图标/分组用）

为什么需要它：
微软商店版 Python 启动时带了一个固定的应用标识
（PythonSoftwareFoundation.Python.3.13_xxx!PythonW），
进程级的 SetCurrentProcessExplicitAppUserModelID 改不动它，
任务栏就一直显示 Python 图标。

Windows 提供了"按窗口"设置标识的接口：
    SHGetPropertyStoreForWindow(hwnd) -> IPropertyStore
    store.SetValue(PKEY_AppUserModel_ID, "Firefly.DesktopPet.1")

用法：
    python tools/set_taskbar_icon.py "Firefly 控制台"
"""

import ctypes
import sys
from ctypes import POINTER, byref, c_void_p, c_wchar_p, wintypes

# ---------------------------------------------------------------- COM 基础

ole32 = ctypes.WinDLL("ole32")
shell32 = ctypes.WinDLL("shell32")
propsys = ctypes.WinDLL("propsys")
user32 = ctypes.WinDLL("user32", use_last_error=True)


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class PROPERTYKEY(ctypes.Structure):
    _fields_ = [("fmtid", GUID), ("pid", wintypes.DWORD)]


class PROPVARIANT(ctypes.Structure):
    """只处理 VT_LPWSTR 的情况：前 8 字节是类型和保留位，之后是字符串指针"""

    _fields_ = [
        ("vt", wintypes.USHORT),
        ("r1", wintypes.WORD),
        ("r2", wintypes.WORD),
        ("r3", wintypes.WORD),
        ("p", ctypes.c_void_p),
    ]


VT_LPWSTR = 31


def _guid(s: str) -> GUID:
    g = GUID()
    ole32.CLSIDFromString(c_wchar_p(s), byref(g))
    return g


# PKEY_AppUserModel_ID = {9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3}, pid 5
PKEY_AUMID = PROPERTYKEY(
    _guid("{9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3}"), 5
)

# IID_IPropertyStore = {886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99}
IID_IPropertyStore = _guid("{886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99}")


class IPropertyStore(ctypes.Structure):
    """只声明用得到的前几个 vtable 槽位"""

    _fields_ = [("lpVtbl", POINTER(c_void_p))]


def _vtable_call(store, index, restype, *argtypes):
    """取 vtable 里第 index 个函数并调用"""
    vtbl = ctypes.cast(store.contents.lpVtbl, POINTER(c_void_p))
    func_ptr = vtbl[index]
    proto = ctypes.WINFUNCTYPE(restype, c_void_p, *argtypes)
    return proto(func_ptr)


def set_window_aumid(hwnd: int, aumid: str) -> bool:
    """给窗口设置 AppUserModelID；成功返回 True"""
    ole32.OleInitialize(None)

    store = ctypes.POINTER(IPropertyStore)()
    hr = shell32.SHGetPropertyStoreForWindow(
        wintypes.HWND(hwnd), byref(IID_IPropertyStore), byref(store)
    )
    if hr != 0 or not store:
        print(f"  SHGetPropertyStoreForWindow 失败 hr=0x{hr & 0xFFFFFFFF:08X}")
        return False

    # 手工构造 VT_LPWSTR 的 PROPVARIANT（保持缓冲区存活到 SetValue 之后）
    buf = ctypes.create_unicode_buffer(aumid)
    pv = PROPVARIANT()
    pv.vt = VT_LPWSTR
    pv.p = ctypes.cast(buf, ctypes.c_void_p)

    # IPropertyStore::SetValue = 槽位 6，Commit = 槽位 7
    set_value = _vtable_call(
        store, 6, ctypes.c_long, POINTER(PROPERTYKEY), POINTER(PROPVARIANT)
    )
    hr = set_value(store, byref(PKEY_AUMID), byref(pv))
    if hr != 0:
        print(f"  SetValue 失败 hr=0x{hr & 0xFFFFFFFF:08X}")
        return False

    commit = _vtable_call(store, 7, ctypes.c_long)
    hr = commit(store)
    if hr != 0:
        print(f"  Commit 失败 hr=0x{hr & 0xFFFFFFFF:08X}")
        return False

    return True


def find_window(title: str) -> int:
    EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    found = []

    def _cb(hwnd, _l):
        n = user32.GetWindowTextLengthW(hwnd)
        if n:
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            if buf.value == title:
                found.append(hwnd)
                return False
        return True

    user32.EnumWindows(EnumProc(_cb), 0)
    return found[0] if found else 0


if __name__ == "__main__":
    title = sys.argv[1] if len(sys.argv) > 1 else "Firefly 控制台"
    aumid = sys.argv[2] if len(sys.argv) > 2 else "Firefly.DesktopPet.1"

    hwnd = find_window(title)
    if not hwnd:
        print(f"找不到窗口: {title}")
        sys.exit(1)

    print(f"窗口句柄: {hwnd}  AUMID -> {aumid}")
    ok = set_window_aumid(hwnd, aumid)
    print("结果:", "成功" if ok else "失败")
    sys.exit(0 if ok else 1)
