# -*- coding: utf-8 -*-
"""
资源模式：按场景开关 TTS / STT 这两个大内存服务。

背景（实测）：
- TTS（GPT-SoVITS）常驻约 3.0 GB，冷启动约 24 秒
- STT（FunASR） 常驻约 2.4 GB，冷启动约 4 秒
- 桌宠本体 + 2D 渲染约 0.6 GB

所以给三档：
    normal  正常：全都开着（体验最好，约 6 GB）
    saver   省内存：只留 TTS，STT 停掉；唤醒暂停，改用热键说话（约 3.6 GB）
    game    游戏：两个都停，只留桌宠（约 0.8 GB），退出后自动拉回来
"""

from __future__ import annotations

import json
import socket
import subprocess
import time
from pathlib import Path
from typing import Dict, Optional

ROOT = Path(r"D:\AI")
GPT_DIR = ROOT / "v4" / "GPT-SoVITS-v4-20250422fix" / "GPT-SoVITS-v4-20250422-nvidia50"
GPT_PY = GPT_DIR / "runtime" / "python.exe"

MODES = {
    "normal": {"tts": True, "stt": True, "label": "正常"},
    "saver": {"tts": True, "stt": False, "label": "省内存"},
    "game": {"tts": False, "stt": False, "label": "游戏"},
}


def _port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.6) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def _procs(match: str) -> list:
    """按命令行关键字找进程"""
    ps = (
        "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
        f"Where-Object {{ $_.CommandLine -match '{match}' }} | "
        "Select-Object -ExpandProperty ProcessId | ConvertTo-Json -Compress"
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True,
            text=True,
            timeout=20,
        ).stdout.strip()
        if not out:
            return []
        data = json.loads(out)
        return data if isinstance(data, list) else [data]
    except Exception:
        return []


def _kill(pids: list) -> int:
    n = 0
    for pid in pids:
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/F"],
                capture_output=True,
                timeout=15,
            )
            n += 1
        except Exception:
            pass
    return n


def stop_tts() -> bool:
    pids = _procs("api_v2")
    if not pids:
        return True
    _kill(pids)
    for _ in range(20):
        if not _port_open(9880):
            return True
        time.sleep(0.5)
    return not _port_open(9880)


def stop_stt() -> bool:
    pids = _procs("stt_server")
    if not pids:
        return True
    _kill(pids)
    for _ in range(20):
        if not _port_open(9881):
            return True
        time.sleep(0.5)
    return not _port_open(9881)


def start_tts(wait: float = 60.0) -> bool:
    if _port_open(9880):
        return True
    if not GPT_PY.exists():
        return False
    log = open(ROOT / "logs" / "tts.log", "ab")
    subprocess.Popen(
        [
            str(GPT_PY),
            "api_v2.py",
            "-a",
            "127.0.0.1",
            "-p",
            "9880",
            "-c",
            "GPT_SoVITS/configs/tts_infer.yaml",
        ],
        cwd=str(GPT_DIR),
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    t0 = time.time()
    while time.time() - t0 < wait:
        if _port_open(9880):
            return True
        time.sleep(1)
    return False


def start_stt(wait: float = 60.0) -> bool:
    if _port_open(9881):
        return True
    if not GPT_PY.exists():
        return False
    log = open(ROOT / "logs" / "stt.log", "ab")
    subprocess.Popen(
        [str(GPT_PY), "tools/stt_server.py"],
        cwd=str(ROOT),
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    t0 = time.time()
    while time.time() - t0 < wait:
        if _port_open(9881):
            return True
        time.sleep(1)
    return False


def current_mode() -> str:
    try:
        from firefly_app.backend import llm

        m = str(llm.load_config().get("power_mode") or "normal")
        return m if m in MODES else "normal"
    except Exception:
        return "normal"


def set_mode(mode: str, wait: bool = True) -> Dict:
    """切换资源模式；返回 {ok, mode, actions, status}"""
    if mode not in MODES:
        return {"ok": False, "error": f"未知模式: {mode}"}

    cfg = MODES[mode]
    actions = []

    if cfg["tts"]:
        if not _port_open(9880):
            ok = start_tts(wait=60 if wait else 0.1)
            actions.append(("启动 TTS", ok))
    else:
        if _port_open(9880):
            actions.append(("停止 TTS", stop_tts()))

    if cfg["stt"]:
        if not _port_open(9881):
            ok = start_stt(wait=60 if wait else 0.1)
            actions.append(("启动 STT", ok))
    else:
        if _port_open(9881):
            actions.append(("停止 STT", stop_stt()))

    # 记到配置里：唤醒监听会看它，非 normal 模式就暂停收音
    try:
        from firefly_app.frontend import settings_window as sw
        from firefly_app.backend import llm

        raw = sw._read_raw()
        raw["power_mode"] = mode
        sw.write_raw(raw)
        llm.load_config(force=True)
    except Exception as e:
        actions.append(("写入配置", False))

    return {
        "ok": all(ok for _, ok in actions) if actions else True,
        "mode": mode,
        "label": cfg["label"],
        "actions": [{"what": w, "ok": ok} for w, ok in actions],
        "status": status(),
    }


def status() -> Dict:
    try:
        from firefly_app.backend import llm

        cfg = llm.load_config()
        mode = str(cfg.get("power_mode") or "normal")
    except Exception:
        mode = "unknown"

    tts = _port_open(9880)
    stt = _port_open(9881)
    return {
        "mode": mode if mode in MODES else "unknown",
        "tts": tts,
        "stt": stt,
        "estimate_mb": 600 + (3000 if tts else 0) + (2400 if stt else 0),
    }
