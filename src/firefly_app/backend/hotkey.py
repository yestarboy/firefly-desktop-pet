# -*- coding: utf-8 -*-
"""
全局键盘热键（按住说话用）。

为什么自己写而不用第三方库：
- 需要的是 **keydown + keyup 配对**，Windows 的 RegisterHotKey 只给按下、没有松开
- 必须全局生效：桌宠窗口没有焦点时也要能收到
- 用 WH_KEYBOARD_LL 低级钩子，纯 ctypes，不引入新依赖

钩子回调跑在独立的钩子线程里，所以 on_down / on_up 必须**立刻返回**，
不要在回调里做网络请求或 sleep —— 否则整个系统的键盘都会卡。
"""

import ctypes
import threading
from ctypes import wintypes

WH_KEYBOARD_LL = 13
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
WM_QUIT = 0x0012

# 热键名 -> Windows 虚拟键码
VK_MAP = {
    "F1": 0x70, "F2": 0x71, "F3": 0x72, "F4": 0x73,
    "F5": 0x74, "F6": 0x75, "F7": 0x76, "F8": 0x77,
    "F9": 0x78, "F10": 0x79, "F11": 0x7A, "F12": 0x7B,
    "LeftCtrl": 0xA2, "RightCtrl": 0xA3,
    "LeftShift": 0xA0, "RightShift": 0xA1,
    "LeftAlt": 0xA4, "RightAlt": 0xA5,
    "Space": 0x20, "Tab": 0x09, "CapsLock": 0x14,
    "Backquote": 0xC0, "ScrollLock": 0x91, "Pause": 0x13,
    "Insert": 0x2D, "Delete": 0x2E, "Home": 0x24, "End": 0x23,
    "PageUp": 0x21, "PageDown": 0x22,
}

# 反查：虚拟键码 -> 名字（给界面显示用）
VK_NAMES = {v: k for k, v in VK_MAP.items()}


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG)),
    ]


class HotkeyHook:
    """
    全局键盘钩子。用法：
        hook = HotkeyHook(on_down=fn, on_up=fn)
        hook.start()
    回调收到的是虚拟键码（int）。
    """

    def __init__(self, on_down=None, on_up=None):
        self.on_down = on_down or (lambda vk: None)
        self.on_up = on_up or (lambda vk: None)
        self._thread = None
        self._thread_id = None
        self._ready = threading.Event()
        self._ok = False

    # ------------------------------------------------------------ 生命周期

    def start(self, timeout=3.0):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._ready.wait(timeout)
        return self._ok

    def stop(self):
        if self._thread_id:
            try:
                ctypes.windll.user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
            except Exception:
                pass

    @property
    def ok(self):
        return self._ok

    # ------------------------------------------------------------ 内部

    def _run(self):
        user32 = ctypes.WinDLL("user32", use_last_error=True)

        HOOKPROC = ctypes.WINFUNCTYPE(
            ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM
        )
        user32.SetWindowsHookExW.restype = ctypes.c_void_p
        user32.SetWindowsHookExW.argtypes = [
            ctypes.c_int, HOOKPROC, ctypes.c_void_p, wintypes.DWORD
        ]
        user32.CallNextHookEx.restype = ctypes.c_ssize_t
        user32.CallNextHookEx.argtypes = [
            ctypes.c_void_p, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM
        ]

        def _proc(n_code, w_param, l_param):
            if n_code == 0:
                try:
                    kb = ctypes.cast(
                        l_param, ctypes.POINTER(KBDLLHOOKSTRUCT)
                    ).contents
                    if w_param in (WM_KEYDOWN, WM_SYSKEYDOWN):
                        self.on_down(int(kb.vkCode))
                    elif w_param in (WM_KEYUP, WM_SYSKEYUP):
                        self.on_up(int(kb.vkCode))
                except Exception:
                    pass
            # 永远放行，不能吞键，否则会影响系统输入
            return user32.CallNextHookEx(None, n_code, w_param, l_param)

        self._proc = HOOKPROC(_proc)          # 保持引用，否则会被 GC 掉
        hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._proc, None, 0)
        if not hook:
            self._ok = False
            self._ready.set()
            return

        self._ok = True
        self._thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
        self._ready.set()

        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

        user32.UnhookWindowsHookEx(hook)
