# -*- coding: utf-8 -*-
"""测量 TTS 冷启动耗时：进程启动 → 端口就绪 → 首次合成完成"""

import subprocess
import sys
import time
from pathlib import Path

import requests

ROOT = Path(r"D:\AI")
GPT_DIR = ROOT / "v4" / "GPT-SoVITS-v4-20250422fix" / "GPT-SoVITS-v4-20250422-nvidia50"
GPT_PY = GPT_DIR / "runtime" / "python.exe"


def port_ok(url: str, timeout=1.0) -> bool:
    try:
        requests.get(url, timeout=timeout)
        return True
    except Exception:
        return False


def main():
    if port_ok("http://127.0.0.1:9880/docs"):
        print("TTS 正在运行，请先停掉再测（或改用 --force-kill）")
        if "--force-kill" in sys.argv:
            subprocess.run(
                "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
                "Where-Object { $_.CommandLine -match 'api_v2' } | "
                "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }",
                shell=True,
                executable="powershell",
            )
            time.sleep(3)
        else:
            return 1

    print("启动 TTS…")
    t0 = time.time()
    log = open(ROOT / "logs" / "tts_cold.log", "wb")
    subprocess.Popen(
        [str(GPT_PY), "api_v2.py", "-a", "127.0.0.1", "-p", "9880",
         "-c", "GPT_SoVITS/configs/tts_infer.yaml"],
        cwd=str(GPT_DIR),
        stdout=log,
        stderr=subprocess.STDOUT,
    )

    t_ready = None
    for _ in range(180):
        if port_ok("http://127.0.0.1:9880/docs"):
            t_ready = time.time() - t0
            break
        time.sleep(1)

    if t_ready is None:
        print("180 秒内没起来")
        return 1
    print(f"端口就绪: {t_ready:.1f} 秒")

    # 首次合成（会触发模型加载）
    try:
        t1 = time.time()
        r = requests.post(
            "http://127.0.0.1:9880/tts",
            json={
                "text": "你好",
                "text_lang": "zh",
                "ref_audio_path": "",
                "prompt_lang": "zh",
                "prompt_text": "",
                "text_split_method": "cut5",
                "media_type": "wav",
                "streaming_mode": False,
            },
            timeout=180,
        )
        t_synth = time.time() - t1
        print(f"首次合成: {t_synth:.1f} 秒 (HTTP {r.status_code}, {len(r.content)} 字节)")
    except Exception as e:
        print("首次合成失败:", str(e)[:120])
        t_synth = -1

    print(f"\n冷启动总耗时（能出声）: {t_ready + max(0, t_synth):.1f} 秒")
    return 0


if __name__ == "__main__":
    sys.exit(main())
