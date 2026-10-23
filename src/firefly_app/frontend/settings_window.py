# -*- coding: utf-8 -*-
"""
可视化设置面板（独立窗口）。

它和桌宠共用同一个本地静态服务器，页面通过 QWebChannel 直接调用这里的槽函数，
不额外起一套 HTTP 接口：

    panel/index.html --QWebChannel--> _Bridge --读写--> firefly_config.json

保存后立刻调用 llm.load_config(force=True) 让新配置当场生效，不用重启桌宠。
"""

import json
import threading
import time
import wave
from pathlib import Path

from PyQt6.QtCore import QObject, QUrl, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QVBoxLayout, QWidget
from PyQt6.QtWebChannel import QWebChannel
from PyQt6.QtWebEngineCore import QWebEngineSettings
from PyQt6.QtWebEngineWidgets import QWebEngineView

from firefly_app.backend import llm, ui_bus
from firefly_app.paths import ICONS_DIR, LOGS_DIR, ROOT, VOICES_DIR

WORKSPACE_ROOT = ROOT
CONFIG_PATH = WORKSPACE_ROOT / "firefly_config.json"
VOICE_DIR = VOICES_DIR
LOG_NAMES = ("tts", "stt", "pet")

# 允许在界面上编辑的字段（其余字段原样保留）
EDITABLE_KEYS = [
    "provider", "api_key", "base_url", "model",
    "temperature", "max_tokens", "timeout",
    "system_prompt", "tts_ref_wav", "tts_speed",
    "stt_mic", "voice_mode", "voice_hotkey",
]


class _Bridge(QObject):
    """暴露给网页的接口"""

    sig_js = pyqtSignal(str)

    def __init__(self, win: "SettingsWindow"):
        super().__init__()
        self.win = win
        self.sig_js.connect(win.run_js)

    # ------------------------------------------------ 读写配置

    @pyqtSlot(result=str)
    def loadConfig(self) -> str:
        try:
            return json.dumps(self.win.build_state(), ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": str(e)}, ensure_ascii=False)

    @pyqtSlot(str, result=str)
    def saveConfig(self, payload: str) -> str:
        try:
            data = json.loads(payload)
            self.win.save(data)
            return json.dumps({"ok": True}, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)

    # ------------------------------------------------ 日志

    @pyqtSlot(result=str)
    def listLogs(self) -> str:
        """有哪些日志文件、多大"""
        items = []
        for name in LOG_NAMES:
            p = LOGS_DIR / f"{name}.log"
            items.append({
                "name": name,
                "size": p.stat().st_size if p.exists() else 0,
                "exists": p.exists(),
            })
        return json.dumps(items, ensure_ascii=False)

    @pyqtSlot(str, int, result=str)
    def readLog(self, name: str, lines: int) -> str:
        """读日志尾部若干行"""
        if name not in LOG_NAMES:
            return "非法日志名"
        p = LOGS_DIR / f"{name}.log"
        if not p.exists():
            return f"（还没有 {name}.log）\n用 start_all.vbs 启动后才会生成。"
        return _tail(p, max(20, min(int(lines or 200), 3000)))

    @pyqtSlot(result=str)
    def services(self) -> str:
        """各服务是否在线 + 已记录的 PID"""
        import socket

        def up(port):
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(0.3)
            try:
                return s.connect_ex(("127.0.0.1", port)) == 0
            finally:
                s.close()

        pids = []
        try:
            pid_path = LOGS_DIR / "pids.json"
            if pid_path.exists():
                pids = json.loads(pid_path.read_text(encoding="utf-8"))
        except Exception:
            pids = []

        return json.dumps({"tts": up(9880), "stt": up(9881), "pids": pids},
                          ensure_ascii=False)

    # ------------------------------------------------ 语音输入

    @pyqtSlot(result=str)
    def getSttDevices(self) -> str:
        """从识别服务拉取麦克风列表"""
        try:
            import requests

            r = requests.get("http://127.0.0.1:9881/devices", timeout=4)
            r.raise_for_status()
            return r.text          # 服务端返回的就是 JSON
        except Exception as e:
            return json.dumps(
                {"devices": [], "error": f"识别服务未就绪：{e}"},
                ensure_ascii=False,
            )

    @pyqtSlot()
    def testMic(self):
        """在设置页里试录一句，不发给 LLM"""
        threading.Thread(target=self._run_mic_test, daemon=True).start()

    def _run_mic_test(self):
        t0 = time.time()
        try:
            import requests

            mic = ""
            try:
                mic = str(llm.load_config().get("stt_mic") or "").strip()
            except Exception:
                mic = ""
            url = "http://127.0.0.1:9881/listen" + (f"?mic={mic}" if mic else "")
            r = requests.post(url, timeout=60)
            if r.status_code != 200:
                try:
                    detail = r.json().get("error") or r.text
                except Exception:
                    detail = (r.text or "")[:200]
                self._push("mic", False, f"HTTP {r.status_code}：{detail}",
                           int((time.time() - t0) * 1000))
                return
            data = r.json()
            text = (data.get("text") or "").strip()
            if text:
                self._push("mic", True, text, int((time.time() - t0) * 1000))
            else:
                self._push("mic", False, data.get("reason") or "没听到声音",
                           int((time.time() - t0) * 1000))
        except Exception as e:
            self._push("mic", False, str(e), int((time.time() - t0) * 1000))

    # ------------------------------------------------ 对话

    @pyqtSlot(str)
    def askLLM(self, text: str):
        """面板里的聊天：复用桌宠那边的对话流程，事件会被广播回两个窗口"""
        q = (text or "").strip()
        if not q:
            return
        bridge = getattr(self.win, "chat_bridge", None)
        if bridge is None:
            self._push_chat_error("桌宠窗口尚未就绪，请稍后再试")
            return
        try:
            bridge.askLLM(q)
        except Exception as e:
            self._push_chat_error(str(e))

    def _push_chat_error(self, msg: str):
        payload = json.dumps({"error": msg}, ensure_ascii=False)
        self.sig_js.emit(
            f"window.__pet_on_py_event && "
            f"window.__pet_on_py_event('llm_error', {payload});"
        )

    # ------------------------------------------------ 测试（异步）

    @pyqtSlot(str)
    def testLLM(self, text: str):
        threading.Thread(target=self._run_llm_test, args=(text,), daemon=True).start()

    @pyqtSlot(str)
    def testTTS(self, text: str):
        threading.Thread(target=self._run_tts_test, args=(text,), daemon=True).start()

    def _run_llm_test(self, text: str):
        t0 = time.time()
        try:
            ans = llm.chat_once(text or "你好，用一句话介绍你自己")
            self._push("llm", True, ans or "(空回复)", int((time.time() - t0) * 1000))
        except Exception as e:
            self._push("llm", False, str(e), int((time.time() - t0) * 1000))

    def _run_tts_test(self, text: str):
        t0 = time.time()
        try:
            from firefly_app.backend import voice

            voice.speak(text or "你好呀，我是流萤。", emotion="开心",
                        interrupt=True, async_mode=False)
            self._push("tts", True, "已播放", int((time.time() - t0) * 1000))
        except Exception as e:
            self._push("tts", False, str(e), int((time.time() - t0) * 1000))

    def _push(self, kind: str, ok: bool, text: str, ms: int):
        payload = json.dumps(
            {"kind": kind, "ok": ok, "text": text, "ms": ms}, ensure_ascii=False
        )
        self.sig_js.emit(
            f"window.__onTestResult && window.__onTestResult({payload})"
        )


class SettingsWindow(QWidget):
    # 跨线程安全的 JS 入口：工作线程只能 emit 信号，实际执行由主线程完成
    sig_js = pyqtSignal(str)

    def __init__(self, server, chat_bridge=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Firefly 控制台")
        self.resize(940, 780)

        # 窗口/任务栏图标
        try:
            icon_path = ICONS_DIR / "bitbug_favicon.ico"
            if icon_path.exists():
                self.setWindowIcon(QIcon(str(icon_path)))
        except Exception:
            pass

        # 桌宠那边的 Bridge，面板的聊天借它走同一套流程
        self.chat_bridge = chat_bridge

        # 必须先把信号接到主线程槽上，否则工作线程 emit 后没人收
        self.sig_js.connect(self._apply_js)

        self.view = QWebEngineView(self)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.view)

        s = self.view.settings()
        s.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)
        s.setAttribute(QWebEngineSettings.WebAttribute.JavascriptEnabled, True)

        self.channel = QWebChannel(self.view.page())
        self.bridge = _Bridge(self)
        self.channel.registerObject("cfg", self.bridge)
        self.view.page().setWebChannel(self.channel)

        # 页面拆到 panel/ 目录：panel/index.html + panel/css + panel/js
        self.view.load(QUrl(server.get_url("panel/index.html")))

        # 注册到广播总线：桌宠那边产生的事件（llm_delta 等）这里也能收到
        ui_bus.register(self)

    def closeEvent(self, event):
        # 关窗口只是隐藏，不注销总线：重新打开时还能接着收到广播、保留聊天记录
        super().closeEvent(event)

    # ------------------------------------------------ 线程安全边界

    def run_js(self, js: str):
        """可以被任意线程调用：只发信号，不碰 GUI"""
        self.sig_js.emit(js)

    def _apply_js(self, js: str):
        """只会在主线程执行（信号排队投递）"""
        try:
            self.view.page().runJavaScript(js)
        except Exception as e:
            print(f"[settings] runJavaScript 失败: {e}")

    def build_state(self) -> dict:
        raw = _read_raw()
        cfg = {}
        for k in EDITABLE_KEYS:
            v = raw.get(k)
            if v in (None, ""):
                v = llm.DEFAULTS.get(k)
            cfg[k] = v

        return {
            "config": cfg,
            "voices": self.list_voices(),
            "status": self.status(),
            "config_path": str(CONFIG_PATH),
        }

    def list_voices(self) -> list:
        out = []
        if not VOICE_DIR.exists():
            return out
        for p in sorted(VOICE_DIR.glob("*.wav")):
            out.append({
                "file": p.name,
                "text": p.stem,
                "seconds": _wav_seconds(p),
            })
        return out

    def status(self) -> dict:
        st = {
            "配置文件": str(CONFIG_PATH),
            "音色目录": str(VOICE_DIR),
        }
        try:
            import requests

            r = requests.get("http://127.0.0.1:9880/docs", timeout=1.5)
            st["GPT-SoVITS API"] = f"在线 (HTTP {r.status_code})"
        except Exception:
            st["GPT-SoVITS API"] = "未连接（检查 start_all.bat 里的 API 窗口）"
        return st

    def save(self, data: dict):
        raw = _read_raw()
        for k in EDITABLE_KEYS:
            if k in data:
                raw[k] = data[k]

        # 空值/非法值自愈，避免出现 voice_mode="" 这种状态
        normalize_voice(raw)

        write_raw(raw)

        # 立刻生效：清掉进程内的配置缓存
        llm.load_config(force=True)

        # 通知桌宠窗口：语音模式/热键可能变了
        try:
            from firefly_app.backend import ui_bus

            ui_bus.broadcast("voice_config", {
                "mode": str(raw.get("voice_mode") or "always"),
                "hotkey": str(raw.get("voice_hotkey") or "F9"),
            })
        except Exception:
            pass
        # 参考音色可能变了，让 voice 重新扫描
        try:
            from firefly_app.backend import voice

            voice._REF_PAIRS = voice._load_ref_pairs()
        except Exception:
            pass


def _wav_seconds(path: Path) -> float:
    """
    读 wav 时长。先用标准库 wave；它读不了 24bit / WAVE_FORMAT_EXTENSIBLE 这类
    扩展格式（会抛错），就退化成自己解析 RIFF 头：
        duration = data_chunk_size / (采样率 × 声道数 × 位深/8)
    """
    try:
        with wave.open(str(path), "rb") as w:
            return round(w.getnframes() / float(w.getframerate()), 1)
    except Exception:
        pass

    try:
        with open(path, "rb") as f:
            if f.read(4) != b"RIFF":
                return 0.0
            f.seek(8)
            if f.read(4) != b"WAVE":
                return 0.0

            channels = rate = bits = 0
            data_size = 0
            while True:
                hdr = f.read(8)
                if len(hdr) < 8:
                    break
                cid = hdr[:4]
                size = int.from_bytes(hdr[4:], "little")
                if cid == b"fmt ":
                    fmt = f.read(size)
                    if len(fmt) >= 16:
                        channels = int.from_bytes(fmt[2:4], "little")
                        rate = int.from_bytes(fmt[4:8], "little")
                        bits = int.from_bytes(fmt[14:16], "little")
                    else:
                        f.seek(size - len(fmt), 1)
                elif cid == b"data":
                    data_size = size
                    break
                else:
                    f.seek(size, 1)

        byte_rate = rate * channels * (bits / 8.0)
        if byte_rate > 0 and data_size > 0:
            return round(data_size / byte_rate, 1)
    except Exception:
        pass
    return 0.0


def _tail(path: Path, lines: int) -> str:
    """高效读文件最后 N 行：从尾部按块倒着读，不整读大文件"""
    try:
        size = path.stat().st_size
        block = 64 * 1024
        data = b""
        pos = size
        with path.open("rb") as f:
            while pos > 0 and data.count(b"\n") <= lines:
                step = min(block, pos)
                pos -= step
                f.seek(pos)
                data = f.read(step) + data
        text = data.decode("utf-8", errors="replace")
        return "\n".join(text.splitlines()[-lines:])
    except Exception as e:
        return f"读取失败: {e}"


def _read_raw() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}


def write_raw(raw: dict) -> None:
    """原子写入配置文件（先 .tmp 再替换）"""
    tmp = CONFIG_PATH.with_name(CONFIG_PATH.name + ".tmp")
    tmp.write_text(
        json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    tmp.replace(CONFIG_PATH)


def normalize_voice(raw: dict) -> dict:
    """语音相关字段自愈：空值/非法值补成默认"""
    mode = str(raw.get("voice_mode") or "").strip()
    if mode not in ("always", "hotkey"):
        mode = "always"
    raw["voice_mode"] = mode

    hotkey = str(raw.get("voice_hotkey") or "").strip()
    if not hotkey:
        hotkey = "F9"
    raw["voice_hotkey"] = hotkey
    return raw
