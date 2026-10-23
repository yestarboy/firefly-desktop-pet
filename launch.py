# -*- coding: utf-8 -*-
"""
Firefly 一键启动器（静默，无终端窗口）。

做三件事：
1. 检查 9880（TTS）/ 9881（STT）端口，没起就拉起来
2. 启动桌宠
3. 所有子进程的 stdout/stderr 都重定向到 logs/*.log，并把 PID 写进 logs/pids.json

为什么要单独一个启动器：
- .bat 启动会闪一个黑窗口，而且没法把子进程输出重定向到文件
- 子进程用 CREATE_NO_WINDOW 启动，用户完全看不到终端
- 输出落到文件后，控制台面板的「日志」页才能读

由 start_all.vbs 调用（pythonw 启动，连启动器自己都没有窗口）。
"""

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOGS = ROOT / "logs"
LOGS.mkdir(exist_ok=True)

GPT_DIR = ROOT / "v4" / "GPT-SoVITS-v4-20250422fix" / "GPT-SoVITS-v4-20250422-nvidia50"
GPT_PY = GPT_DIR / "runtime" / "python.exe"
PET_PY = ROOT / ".venv" / "Scripts" / "pythonw.exe"
PET_PY_FALLBACK = ROOT / ".venv" / "Scripts" / "python.exe"

CREATE_NO_WINDOW = 0x08000000

_open_files = []      # 保持文件句柄，防止被 GC


def port_open(port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.4)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


def spawn(name: str, exe: Path, args, cwd: Path):
    """静默启动子进程，输出写到 logs/<name>.log"""
    log_path = LOGS / f"{name}.log"

    f = open(log_path, "a", encoding="utf-8", errors="replace")
    _open_files.append(f)

    f.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} 启动 {name} =====\n")
    f.flush()

    cmd = [str(exe)] + [str(a) for a in args]

    # 关键：不开 unbuffered 的话，子进程 stdout 是块缓冲，
    # 日志要攒满 8KB 才落盘，面板里会一直看到空文件。
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"

    if str(exe).lower().endswith("python.exe") or str(exe).lower().endswith("pythonw.exe"):
        cmd = [cmd[0], "-u"] + cmd[1:]

    try:
        p = subprocess.Popen(
            cmd,
            cwd=str(cwd),
            stdout=f,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=env,
            creationflags=CREATE_NO_WINDOW,
            close_fds=True,
        )
    except Exception as e:
        f.write(f"启动失败: {e}\n")
        f.flush()
        return None

    return {"pid": p.pid, "name": name, "cmd": " ".join(cmd)}


def main() -> int:
    started = []

    # 1) GPT-SoVITS TTS
    if not GPT_PY.exists():
        print("未找到 GPT-SoVITS 运行时，跳过 TTS（桌宠会降级到系统语音）")
    elif port_open(9880):
        print("TTS 已在运行，跳过")
    else:
        info = spawn(
            "tts",
            GPT_PY,
            ["api_v2.py", "-a", "127.0.0.1", "-p", "9880",
             "-c", "GPT_SoVITS/configs/tts_infer.yaml"],
            GPT_DIR,
        )
        if info:
            started.append(info)

    # 2) 语音识别
    if not GPT_PY.exists():
        print("未找到 GPT-SoVITS 运行时，跳过 STT（语音输入不可用）")
    elif port_open(9881):
        print("STT 已在运行，跳过")
    else:
        info = spawn("stt", GPT_PY, ["tools/stt_server.py"], ROOT)
        if info:
            started.append(info)

    # 3) 桌宠
    pet_exe = PET_PY if PET_PY.exists() else PET_PY_FALLBACK
    info = spawn("pet", pet_exe, ["live2d_pet.py"], ROOT)
    if info:
        started.append(info)

    # 记录 PID，面板的日志页会读它
    pid_file = LOGS / "pids.json"
    try:
        pid_file.write_text(
            json.dumps(started, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as e:
        print(f"写 pids.json 失败: {e}")

    print(f"已启动 {len(started)} 个进程")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    raise SystemExit(main())
