# -*- coding: utf-8 -*-
"""
热键钩子自测：安装钩子 → 用 keybd_event 注入 F9 → 看回调是否收到。

    .\\.venv\\Scripts\\python.exe tools\\hotkey_test.py
"""

import ctypes
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from firefly_app.backend.hotkey import VK_MAP, HotkeyHook  # noqa: E402

VK_F9 = VK_MAP["F9"]
got = []


def on_down(vk):
    got.append(("down", vk))
    print(f"  [收到] DOWN vk={vk}", flush=True)


def on_up(vk):
    got.append(("up", vk))
    print(f"  [收到] UP   vk={vk}", flush=True)


def main() -> int:
    print("安装钩子…")
    hook = HotkeyHook(on_down=on_down, on_up=on_up)
    ok = hook.start()
    print(f"installed = {ok}")
    if not ok:
        print("钩子安装失败：可能被安全软件拦截")
        return 1

    time.sleep(0.4)

    print(f"注入 F9 (vk={VK_F9}) 按下/松开…")
    user32 = ctypes.windll.user32
    user32.keybd_event(VK_F9, 0, 0, 0)      # 按下
    time.sleep(0.2)
    user32.keybd_event(VK_F9, 0, 2, 0)      # 松开 (KEYEVENTF_KEYUP)

    time.sleep(0.6)
    hook.stop()

    print(f"\n共收到 {len(got)} 个事件: {got}")
    if not got:
        print("❌ 钩子没收到任何按键 —— 机制有问题")
        return 1
    if not any(g[0] == "down" and g[1] == VK_F9 for g in got):
        print("❌ 收到了事件但没有 F9")
        return 1
    print("✓ 钩子工作正常")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
