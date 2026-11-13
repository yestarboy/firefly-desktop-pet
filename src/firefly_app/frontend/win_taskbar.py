# -*- coding: utf-8 -*-
"""
给单个窗口设置 AppUserModelID（决定任务栏图标与分组）

背景：微软商店版 Python 启动时已经绑定了应用标识
（PythonSoftwareFoundation.Python.3.13_xxx!PythonW），
进程级 SetCurrentProcessExplicitAppUserModelID 改不动它，
任务栏就一直显示 Python 的图标。

Windows 提供了窗口级的覆盖接口：
    SHGetPropertyStoreForWindow(hwnd) -> IPropertyStore
    SetValue(PKEY_AppUserModel_ID, "Firefly.DesktopPet.1")

配合注册表 HKCU\\Software\\Classes\\AppUserModelId\\<id> 里的 IconUri，
任务栏就会显示我们自己的图标。
"""

from __future__ import annotations

import ctypes
from ctypes import POINTER, byref, c_void_p, c_wchar, c_long, wintypes

_ole32 = ctypes.WinDLL("ole32")
_shell32 = ctypes.WinDLL("shell32")
_user32 = ctypes.WinDLL("user32", use_last_error=True)


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class _PROPERTYKEY(ctypes.Structure):
    _fields_ = [("fmtid", _GUID), ("pid", wintypes.DWORD)]


class _PROPVARIANT(ctypes.Structure):
    # 只处理 VT_LPWSTR：前 8 字节是类型/保留位，其后是字符串指针
    _fields_ = [
        ("vt", wintypes.USHORT),
        ("r1", wintypes.WORD),
        ("r2", wintypes.WORD),
        ("r3", wintypes.WORD),
        ("p", c_void_p),
    ]


class _IPropertyStore(ctypes.Structure):
    _fields_ = [("lpVtbl", POINTER(c_void_p))]


_VT_LPWSTR = 31


def _guid(text: str) -> _GUID:
    g = _GUID()
    _ole32.CLSIDFromString(ctypes.c_wchar_p(text), byref(g))
    return g


_PKEY_AUMID = _PROPERTYKEY(_guid("{9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3}"), 5)
_IID_PROPERTY_STORE = _guid("{886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99}")


def _call(store, index, restype, *argtypes):
    vtbl = ctypes.cast(store.contents.lpVtbl, POINTER(c_void_p))
    proto = ctypes.WINFUNCTYPE(restype, c_void_p, *argtypes)
    return proto(vtbl[index])


def set_window_aumid(hwnd: int, aumid: str) -> bool:
    """给窗口设置应用标识；成功返回 True（失败不影响程序运行）"""
    try:
        _ole32.OleInitialize(None)

        store = POINTER(_IPropertyStore)()
        hr = _shell32.SHGetPropertyStoreForWindow(
            wintypes.HWND(hwnd), byref(_IID_PROPERTY_STORE), byref(store)
        )
        if hr != 0 or not store:
            return False

        buf = ctypes.create_unicode_buffer(aumid)
        pv = _PROPVARIANT()
        pv.vt = _VT_LPWSTR
        pv.p = ctypes.cast(buf, c_void_p)

        set_value = _call(
            store, 6, c_long, POINTER(_PROPERTYKEY), POINTER(_PROPVARIANT)
        )
        if set_value(store, byref(_PKEY_AUMID), byref(pv)) != 0:
            return False

        commit = _call(store, 7, c_long)
        return commit(store) == 0
    except Exception:
        return False


def register_aumid_icon(aumid: str, display_name: str, icon_path: str) -> bool:
    """在注册表登记 AUMID 的显示名与图标（任务栏据此取图标）"""
    try:
        import winreg

        key_path = r"Software\Classes\AppUserModelId" + "\\" + aumid
        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_WRITE
        ) as k:
            winreg.SetValueEx(k, "DisplayName", 0, winreg.REG_SZ, display_name)
            winreg.SetValueEx(k, "IconUri", 0, winreg.REG_SZ, icon_path)
        return True
    except Exception:
        return False
