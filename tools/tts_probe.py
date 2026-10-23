# -*- coding: utf-8 -*-
"""
TTS 参数体检工具（一次性诊断用）

用同一句话，在不同参数下各合成一遍，把 wav 存到 tools/tts_probe/ 下。
你逐个听，看哪个开头是完整的——用耳朵投票，比猜参数快得多。

用法（API 必须先启动）：
    .\\.venv\\Scripts\\python.exe tools\\tts_probe.py

可选：只测某几条
    .\\.venv\\Scripts\\python.exe tools\\tts_probe.py B D
"""

import sys
import time
import wave
from pathlib import Path

import requests

API = "http://127.0.0.1:9880/tts"
ROOT = Path(__file__).resolve().parents[1]
OUT = Path(__file__).resolve().parent / "tts_probe"

# 故意用你实际遇到过的那句
TEXT = "我是流萤，你的桌面小助手，有什么可以帮你的吗？"

# 名字 -> (切分方式, 种子)
CASES = {
    "A": ("cut5", 1234),   # 旧配置：外层切句 + API 再切一次
    "B": ("cut0", 1234),   # 新配置：不再二次切分
    "C": ("cut0", -1),     # 不固定种子
    "D": ("cut0", 42),     # 换一个种子
}


def wav_seconds(data: bytes) -> float:
    p = OUT / "_tmp.wav"
    p.write_bytes(data)
    try:
        with wave.open(str(p), "rb") as w:
            return round(w.getnframes() / float(w.getframerate()), 2)
    except Exception:
        return -1.0
    finally:
        p.unlink(missing_ok=True)


def compare_prefix(ref: Path, prompt: str) -> int:
    """
    客观判定"开头有没有被吃掉"：
    分别合成「我是流萤，…」和「流萤，…」，比较时长。
    如果两者时长几乎一样，说明"我是"确实没念出来。
    """
    full = "我是流萤，你的桌面小助手，有什么可以帮你的吗？"
    cut = "流萤，你的桌面小助手，有什么可以帮你的吗？"

    print("客观对比：完整句 vs 去掉开头的句子")
    print("-" * 66)

    results = {}
    for label, text in (("完整句", full), ("去开头", cut)):
        payload = {
            "text": text, "text_lang": "zh",
            "ref_audio_path": str(ref), "aux_ref_audio_paths": [],
            "prompt_text": prompt, "prompt_lang": "zh",
            "text_split_method": "cut0", "batch_size": 1,
            "speed_factor": 1.0, "media_type": "wav",
            "streaming_mode": False, "seed": 1234,
        }
        r = requests.post(API, json=payload, timeout=180)
        if r.status_code != 200 or not r.content:
            print(f"{label}: HTTP {r.status_code}")
            return 1
        secs = wav_seconds(r.content)
        results[label] = secs
        (OUT / f"cmp_{label}.wav").write_bytes(r.content)
        print(f"{label:<6} {len(text):>2} 字  {secs:>5.2f}s")

    diff = results["完整句"] - results["去开头"]
    print("-" * 66)
    if diff < 0.25:
        print(f"差 {diff:.2f}s —— 两个前缀只差 2 个字却几乎等长：**开头大概率被吃掉了**")
    else:
        print(f"差 {diff:.2f}s —— 时长有正常差距，开头应该是念出来的")
    print(f"人工复核：听 {OUT}\\cmp_完整句.wav 的头两个字")
    return 0


def main() -> int:
    voice_dir = ROOT / "data" / "voices"
    refs = sorted(voice_dir.glob("*.wav"))
    if not refs:
        print(f"找不到参考音频：{voice_dir}")
        return 1
    ref = refs[0]
    prompt = ref.stem

    OUT.mkdir(parents=True, exist_ok=True)

    # 先确认 API 活着
    try:
        requests.get("http://127.0.0.1:9880/docs", timeout=3)
    except Exception as e:
        print(f"连不上 GPT-SoVITS API（{e}）\n先跑 start_all.bat，等出现 Application startup complete")
        return 1

    if "--compare" in sys.argv:
        return compare_prefix(ref, prompt)

    picks = [a.upper() for a in sys.argv[1:] if a.upper() in CASES] or list(CASES)

    print(f"参考音频: {ref.name}")
    print(f"测试文本: {TEXT}")
    print("-" * 66)

    for name in picks:
        split, seed = CASES[name]
        payload = {
            "text": TEXT,
            "text_lang": "zh",
            "ref_audio_path": str(ref),
            "aux_ref_audio_paths": [],
            "prompt_text": prompt,
            "prompt_lang": "zh",
            "text_split_method": split,
            "batch_size": 1,
            "speed_factor": 1.0,
            "media_type": "wav",
            "streaming_mode": False,
            "seed": seed,
        }
        t0 = time.time()
        try:
            r = requests.post(API, json=payload, timeout=180)
        except Exception as e:
            print(f"{name}: 请求失败 {e}")
            continue

        if r.status_code != 200 or not r.content:
            print(f"{name}: HTTP {r.status_code} {r.text[:120]}")
            continue

        dst = OUT / f"{name}_{split}_seed{seed}.wav"
        dst.write_bytes(r.content)
        secs = wav_seconds(r.content)
        print(f"{name}: {split:<5} seed={seed:<6} {secs:>5.2f}s  "
              f"{time.time()-t0:>5.2f}s  -> {dst.name}")

    print("-" * 66)
    print(f"文件都在：{OUT}")
    print("逐个播放，重点听开头两个字：‘我是’ 有没有被吃掉。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
