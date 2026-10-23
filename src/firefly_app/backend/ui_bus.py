# -*- coding: utf-8 -*-
"""
页面事件总线。

桌宠窗口和控制台面板是两个独立的 QWebEngineView。同一条对话流要同时
出现在两边，所以 Python 侧不直接往某个窗口写 JS，而是广播给所有已注册的
"接收者"。

接收者只需要有一个 run_js(js: str) 方法。
"""

import json
import threading

_sinks = []
_lock = threading.Lock()


def register(sink):
    with _lock:
        if sink not in _sinks:
            _sinks.append(sink)


def unregister(sink):
    with _lock:
        if sink in _sinks:
            _sinks.remove(sink)


def broadcast(event: str, payload: dict = None):
    """把 (事件名, 数据) 发给所有打开的窗口"""
    ev = json.dumps(event, ensure_ascii=False)
    pl = json.dumps(payload or {}, ensure_ascii=False)
    js = f"window.__pet_on_py_event && window.__pet_on_py_event({ev}, {pl});"

    with _lock:
        sinks = list(_sinks)

    for s in sinks:
        try:
            s.run_js(js)
        except Exception:
            pass
