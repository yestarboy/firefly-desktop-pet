# -*- coding: utf-8 -*-
"""
Firefly 语音模块
优先使用 GPT-SoVITS API（api_v2.py, FastAPI /tts）
"""

import requests
import json
import io
import os
import sys
import asyncio
from pathlib import Path
from typing import Optional
import threading
import queue
import subprocess
import time
import unicodedata
import tempfile
import winsound

# 兼容中文 Windows 控制台（默认可能是 GBK，打印 emoji 会炸）
try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def _sanitize_text_for_tts(text: str) -> str:
    """
    给“朗读”用的文本清洗：
    - 去掉波浪号/装饰符
    - 去掉大部分 emoji/不可见字符
    - 避免系统 TTS 念出“波浪号/表情”
    """
    if text is None:
        return ""
    t = str(text)
    # 常见装饰符
    for ch in ("~", "～", "〜", "﹏", "…"):
        t = t.replace(ch, " ")
    # 去掉代理对范围字符（大部分 emoji 在这里）
    t = "".join(c for c in t if not (0xD800 <= ord(c) <= 0xDFFF))
    # 去掉变体选择符（U+FE00~U+FE0F、U+E0100~U+E01EF）及零宽字符
    t = "".join(c for c in t if not (0xFE00 <= ord(c) <= 0xFE0F)
                and not (0xE0100 <= ord(c) <= 0xE01EF)
                and ord(c) not in (0x200B, 0x200C, 0x200D, 0xFEFF))
    # 去掉符号类字符（含很多装饰符/emoji），保留中英文与常见标点
    t = "".join(c for c in t if not unicodedata.category(c).startswith("S"))
    # 去掉其它不可打印控制字符
    t = "".join(c for c in t if (c == "\n" or c == "\t" or ord(c) >= 32))
    # 压缩空白
    return " ".join(t.split()).strip()


def _log_tts(msg: str):
    """
    在 pythonw/隐藏启动时，stdout 往往看不到；这里把关键 TTS 状态写入 logs/pet_start.log。
    """
    try:
        log_dir = WORKSPACE_ROOT / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "pet_start.log"
        if (not log_path.exists()) or log_path.stat().st_size == 0:
            with log_path.open("w", encoding="utf-8-sig", errors="replace") as f:
                f.write("")
        with log_path.open("a", encoding="utf-8-sig", errors="replace") as f:
            f.write(str(msg).rstrip() + "\n")
    except Exception:
        pass


# ---- 工作区根目录：必须在任何用到它的模块级代码之前定义 ----
from firefly_app.paths import ROOT as WORKSPACE_ROOT

VOICES_DIR = WORKSPACE_ROOT / "data" / "voices"


def _load_ref_pairs():
    """扫描 data/voices/*.wav：文件名就是台词，天生配对，绝不会错位"""
    pairs = []
    for p in sorted(VOICES_DIR.glob("*.wav")):
        text = p.stem.strip()
        if text:
            pairs.append({"path": str(p), "text": text, "lang": "zh"})
    return pairs


_REF_PAIRS = _load_ref_pairs()


def _pick_ref(emotion: str = "中立"):
    """按配置挑参考音频：配置里指定的优先，否则用第一条"""
    want = ""
    try:
        from firefly_app.backend import llm

        want = str(llm.load_config().get("tts_ref_wav") or "").strip()
    except Exception:
        want = ""

    if want:
        for p in _REF_PAIRS:
            if os.path.basename(p["path"]) == want:
                return p
    return _REF_PAIRS[0] if _REF_PAIRS else None


# GPT-SoVITS API 配置（由启动器自动拉起）
GPT_SOVITS_API_URL = "http://127.0.0.1:9880"

class VoiceEngine:
    """语音合成引擎"""

    def __init__(self, api_url: str = GPT_SOVITS_API_URL):
        self.api_url = api_url
        self.is_available = False
        self._check_availability()

    def _check_availability(self):
        """检查 GPT-SoVITS API 是否可用（FastAPI /docs）"""
        try:
            resp = requests.get(f"{self.api_url}/docs", timeout=2)
            self.is_available = True
            print("[TTS] GPT-SoVITS API 已连接")
            _log_tts("[TTS] GPT-SoVITS API 已连接")
        except Exception:
            self.is_available = False
            print("[TTS] GPT-SoVITS API 未启动（将尝试兜底 TTS）")
            _log_tts("[TTS] GPT-SoVITS API 未启动（将尝试兜底 TTS）")

    def synthesize(
        self,
        text: str,
        emotion: str = "中立",
        speed: float = 1.0,
        streaming: bool = False
    ) -> Optional[bytes]:
        """
        合成语音
        :param text: 要合成的文字
        :param emotion: 情感 (中立/开心/一点悲伤/中立谨慎)
        :param speed: 语速 (0.5-2.0)
        :param streaming: 是否流式返回
        :return: WAV 音频数据
        """
        return self._synthesize_api(text, emotion, speed)

    def _synthesize_api(
        self,
        text: str,
        emotion: str = "中立",
        speed: float = 1.0,
        seed: Optional[int] = None,
    ) -> Optional[bytes]:
        """使用 GPT-SoVITS API（api_v2.py /tts）合成语音"""

        # 随机种子：配置里可改；None 表示每次随机（-1）
        if seed is None:
            try:
                from firefly_app.backend import llm

                seed = int(llm.load_config().get("tts_seed", 1234))
            except Exception:
                seed = 1234

        # 选参考音频：音频和 prompt_text 必须来自同一对
        ref_config = _pick_ref(emotion)
        if not ref_config:
            print("[TTS] 没有可用的参考音频")
            _log_tts("[TTS] 没有可用的参考音频")
            return None

        ref_audio_path = ref_config["path"]

        try:
            raw = (text or "").strip()
            if not raw:
                return None

            # API 侧常以 GBK 打印日志，emoji/符号会导致编码异常 → 合成失败
            t = _sanitize_text_for_tts(raw)
            if not t:
                return None

            print(f"[TTS] 合成语音(API): {t[:30]}... (情感: {emotion})")
            payload = {
                "text": t,
                "text_lang": "zh",
                "ref_audio_path": ref_audio_path,
                "aux_ref_audio_paths": [],
                "prompt_text": _sanitize_text_for_tts(ref_config["text"]),
                "prompt_lang": ref_config.get("lang", "zh"),
                # 外面已经按句切好了，这里不要再切：cut5 会把一句话再拆成几段，
                # 短片段单独推理时最容易把开头一两个字"吃掉"
                "text_split_method": "cut0",
                "batch_size": 1,
                "speed_factor": float(speed),
                "media_type": "wav",
                "streaming_mode": False,
                "seed": int(seed),
            }
            r = requests.post(f"{self.api_url}/tts", json=payload, timeout=120)
            if r.status_code == 200 and r.content:
                print("[TTS] 语音合成成功(API)")
                _log_tts("[TTS] 语音合成成功(API)")
                return r.content

            try:
                err = r.json()
            except Exception:
                err = {"status_code": r.status_code, "text": (r.text or "")[:500]}
            print(f"[TTS] 语音合成失败(API): {err}")
            _log_tts(f"[TTS] 语音合成失败(API): {err}")
            return None
        except Exception as e:
            print(f"[TTS] 语音合成错误(API): {e}")
            _log_tts(f"[TTS] 语音合成错误(API): {e}")
            import traceback
            traceback.print_exc()
            return None

    def play_audio(self, audio_data: bytes):
        """播放音频数据（Windows 下可被打断）"""
        try:
            if _on_tts_start:
                _on_tts_start()
        except Exception:
            pass
        try:
            self._play_with_winsound(audio_data)
        finally:
            try:
                if _on_tts_end:
                    _on_tts_end()
            except Exception:
                pass

    def stop_audio(self):
        """打断正在播放的音频（尽力而为）"""
        try:
            winsound.PlaySound(None, winsound.SND_PURGE)
        except Exception:
            pass

    def _play_with_winsound(self, audio_data: bytes):
        """
        使用 winsound 播放 WAV（同步，可被 stop_audio 打断）。
        说明：之前用 PowerShell SoundPlayer 不好打断，且容易多段叠加。
        """
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                f.write(audio_data)
                tmp_path = f.name
            winsound.PlaySound(tmp_path, winsound.SND_FILENAME)
        finally:
            if tmp_path:
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass

    def _play_with_system(self, audio_data: bytes):
        """使用系统方式播放音频"""
        import tempfile
        import subprocess
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            f.write(audio_data)
            tmp_path = f.name
        try:
            creationflags = 0
            startupinfo = None
            if sys.platform == "win32":
                creationflags = subprocess.CREATE_NO_WINDOW
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startupinfo.wShowWindow = 0
            subprocess.Popen(
                [
                    "powershell",
                    "-NoProfile",
                    "-WindowStyle",
                    "Hidden",
                    "-c",
                    f'(New-Object Media.SoundPlayer "{tmp_path}").PlaySync()',
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creationflags,
                startupinfo=startupinfo,
            )
        except Exception as e:
            print(f"❌ 播放失败: {e}")

    def speak(self, text: str, emotion: str = "中立", speed: float = 1.0):
        """合成并播放语音（同步）"""
        audio_data = self.synthesize(text, emotion, speed)
        if audio_data:
            self.play_audio(audio_data)

    def speak_async(self, text: str, emotion: str = "中立", speed: float = 1.0):
        """合成并播放语音（异步，不阻塞）"""
        thread = threading.Thread(
            target=self.speak,
            args=(text, emotion, speed),
            daemon=True
        )
        thread.start()
        return thread


# 全局语音引擎实例
_engine: Optional[VoiceEngine] = None

# TTS 播放事件回调（由外部注册，用于驱动 Live2D 嘴型）
_on_tts_start = None
_on_tts_end = None


def set_tts_callbacks(on_start, on_end):
    """注册 TTS 播放开始/结束回调（供 live2d_pet.py 使用）"""
    global _on_tts_start, _on_tts_end
    _on_tts_start = on_start
    _on_tts_end = on_end


def get_engine() -> VoiceEngine:
    """获取全局语音引擎"""
    global _engine
    if _engine is None:
        _engine = VoiceEngine()
    return _engine


def prewarm(timeout: float = 90.0):
    """
    后台预热：赶在用户开口之前跑一次极短的合成。

    GPT-SoVITS 的第一次推理要编译 CUDA kernel、懒加载 BERT / CNHuBERT，
    通常要 3~8 秒。把这笔成本提前付掉，"第一句很慢"就消失了。
    """
    def run():
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                engine = get_engine()
                if not engine.is_available:
                    engine._check_availability()
                if engine.is_available:
                    t = time.time()
                    audio = engine.synthesize("嗯。", "中立", 1.0)
                    if audio:
                        _log_tts(f"[TTS] 预热完成，耗时 {time.time() - t:.2f}s")
                    return
            except Exception as e:
                _log_tts(f"[TTS] 预热异常: {e}")
            time.sleep(3)

    threading.Thread(target=run, daemon=True).start()


def _infer_emotion(text: str) -> str:
    """
    根据输出内容粗略判断情感，返回 REF_AUDIO_CONFIG 的 key：
    - "开心" / "一点悲伤" / "中立谨慎" / "中立"
    """
    if not text:
        return "中立"

    t = str(text).strip()
    low = t.lower()

    # 优先根据明显情绪词判断
    happy_kw = ["开心", "高兴", "喜欢", "太棒", "真好", "不错", "好耶", "期待", "祝你", "恭喜", "哈哈", "lol"]
    sad_kw = ["难过", "伤心", "失望", "抱歉", "对不起", "遗憾", "可惜", "惭愧", "难受"]
    cautious_kw = ["小心", "注意", "谨慎", "警惕", "危险", "不能", "不要", "必须", "务必", "重要", "严肃"]

    for kw in happy_kw:
        if kw in t or kw in low:
            return "开心"

    for kw in sad_kw:
        if kw in t or kw in low:
            return "一点悲伤"

    for kw in cautious_kw:
        if kw in t or kw in low:
            return "中立谨慎"

    # 根据标点简单区分：感叹号偏开心，多个省略号/叹号可能偏感性
    if "！" in t or "!" in t:
        return "开心"

    return "中立"


def speak(text: str, emotion: str = "开心", speed: Optional[float] = None,
          async_mode: bool = True, interrupt: bool = True):
    """
    让 Firefly 说话
    :param text: 要说的文字
    :param emotion: 情感 (中立/开心/一点悲伤/中立谨慎)，默认开心
    :param speed: 语速；None=读取配置里的 tts_speed
    :param async_mode: 是否异步（不阻塞主线程）
    :param interrupt: True=打断正在播的声音并清空队列（新的一轮对话）；
                      False=排队接着播（同一轮回复的后续句子，必须用 False，
                      否则每一句都会把前一句掐掉，听起来就是"漏字/断句"）
    """
    # 自动情感推断
    emo = emotion
    if emo is None or emo == "" or str(emo).lower() == "auto":
        emo = _infer_emotion(text)

    # 语速：没显式指定就按配置来
    if speed is None:
        try:
            from firefly_app.backend import llm

            speed = float(llm.load_config().get("tts_speed") or 1.0)
        except Exception:
            speed = 1.0

    cleaned_for_api = _sanitize_text_for_tts(text)
    if not cleaned_for_api:
        return

    _TTS_MANAGER.enqueue(
        cleaned_for_api, emo, float(speed),
        interrupt=bool(interrupt), async_mode=async_mode,
    )


class _TTSManager:
    """
    合成与播放分离的双线程流水线：

        文本队列 --[合成线程]--> 音频队列 --[播放线程]--> 声卡

    旧实现是一条线程“合成一句→播完→再合成下一句”，播放期间 GPU 闲着，
    句与句之间必然出现 1 秒左右的空档。拆成两条线程后，
    “播放第 N 句”和“合成第 N+1 句”是并行的，句子之间几乎无缝。

    _gen 是“这一轮对话”的代号：新问题进来时 +1，所有旧代号的任务在
    合成/播放前都会被丢弃，实现“打断”语义而不必真的杀线程。
    """

    def __init__(self):
        self._text_q = queue.Queue()
        self._audio_q = queue.Queue(maxsize=6)   # 最多预合成 6 句，防止无限堆积
        self._lock = threading.Lock()
        self._gen = 0
        threading.Thread(target=self._synth_worker, daemon=True).start()
        threading.Thread(target=self._play_worker, daemon=True).start()

    # ------------------------------------------------------------ 对外

    def enqueue(self, text: str, emotion: str, speed: float, interrupt: bool, async_mode: bool):
        if interrupt:
            self.interrupt()

        with self._lock:
            gen = self._gen
        item = (gen, text, emotion, float(speed))

        if async_mode:
            self._text_q.put(item)
        else:
            self._run_sync(item)

    def interrupt(self):
        """丢掉所有还没播的句子，并掐断正在播的音频"""
        with self._lock:
            self._gen += 1
        self._drain(self._text_q)
        self._drain(self._audio_q)
        try:
            get_engine().stop_audio()
        except Exception:
            pass

    def pending(self) -> int:
        """调试用：还有多少句没播完"""
        return self._text_q.qsize() + self._audio_q.qsize()

    # ------------------------------------------------------------ 内部

    @staticmethod
    def _drain(q: "queue.Queue"):
        try:
            while True:
                q.get_nowait()
                q.task_done()
        except queue.Empty:
            pass

    def _is_stale(self, gen: int) -> bool:
        with self._lock:
            return gen != self._gen

    def _synth_worker(self):
        """只负责合成，合成完就丢进音频队列，不等播放"""
        while True:
            gen, text, emotion, speed = self._text_q.get()
            try:
                if self._is_stale(gen):
                    continue
                audio = self._synthesize(text, emotion, speed)
                if audio is None:
                    continue
                if self._is_stale(gen):
                    continue
                self._audio_q.put((gen, audio))
            except Exception as e:
                _log_tts(f"[TTS] 合成线程异常: {e}")
            finally:
                self._text_q.task_done()

    def _play_worker(self):
        """只负责播放，播完就从队列取下一条"""
        while True:
            gen, audio = self._audio_q.get()
            try:
                if self._is_stale(gen):
                    continue
                get_engine().play_audio(audio)
            except Exception as e:
                _log_tts(f"[TTS] 播放线程异常: {e}")
            finally:
                self._audio_q.task_done()

    def _synthesize(self, text: str, emotion: str, speed: float):
        try:
            engine = get_engine()
            # API 可能是后启动的，每次失败都重新探测一次
            if not engine.is_available:
                engine._check_availability()
            if not engine.is_available:
                return None
            return engine.synthesize(text, emotion, speed)
        except Exception as e:
            _log_tts(f"[TTS] 合成失败: {e}")
            return None

    def _run_sync(self, item):
        """同步模式（自测/命令行用）：合成完直接播，不进队列"""
        gen, text, emotion, speed = item
        audio = self._synthesize(text, emotion, speed)
        if audio is None:
            self._speak_fallback(text)
            return
        if self._is_stale(gen):
            return
        get_engine().play_audio(audio)

    @staticmethod
    def _speak_fallback(text: str):
        try:
            from lightweight_tts import speak as _lw_speak

            _log_tts(f"[TTS] fallback lightweight_tts: {text}")
            _lw_speak(text)
        except Exception as e:
            _log_tts(f"[TTS] 轻量 TTS 也失败: {e}")


# 全局单例：所有 speak() 共享同一套队列和线程
_TTS_MANAGER = _TTSManager()


# 情感映射
EMOTION_MAP = {
    "happy": "开心",
    "sad": "一点悲伤",
    "neutral": "中立",
    "cautious": "中立谨慎",
    "开心": "开心",
    "悲伤": "一点悲伤",
    "中立": "中立",
    "谨慎": "中立谨慎",
}


def emotion_to_ref(emotion: str) -> str:
    """将情感标签转换为参考音频配置名称"""
    return EMOTION_MAP.get(emotion, "中立")


if __name__ == "__main__":
    print("测试 Firefly 语音合成...")
    
    engine = VoiceEngine()

    if engine.is_available:
        print("✅ 使用 GPT-SoVITS API 进行测试...")
        engine.speak("你好，我是Firefly，很高兴认识你。", emotion="中立")
    else:
        print("❌ GPT-SoVITS API 未启动")
        print("请先通过桌宠启动器/快捷键拉起 GPT-SoVITS API（端口 9880）")

