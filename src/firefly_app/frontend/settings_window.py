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
from PyQt6.QtWebEngineCore import QWebEnginePage, QWebEngineSettings
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
    "show_hint", "wake_enabled", "wake_word",
]


class _Bridge(QObject):
    """暴露给网页的接口"""

    sig_js = pyqtSignal(str)

    def __init__(self, win: "SettingsWindow"):
        super().__init__()
        self.win = win
        self.sig_js.connect(win.run_js)
        self._ws_busy = False

    # ------------------------------------------------ 工作区

    @pyqtSlot(result=str)
    def wsRoot(self) -> str:
        try:
            from firefly_app.backend import workspace

            return json.dumps(
                {"ok": True, "root": workspace._root().as_posix()}, ensure_ascii=False
            )
        except Exception as e:
            return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)

    @pyqtSlot(str, result=str)
    def wsList(self, rel: str) -> str:
        try:
            from firefly_app.backend import workspace

            return json.dumps(workspace.list_dir(rel), ensure_ascii=False)
        except Exception as e:
            return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)

    @pyqtSlot(str, result=str)
    def wsRead(self, rel: str) -> str:
        try:
            from firefly_app.backend import workspace

            return json.dumps(workspace.read_file(rel), ensure_ascii=False)
        except Exception as e:
            return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)

    @pyqtSlot(str, str, result=str)
    def wsWrite(self, rel: str, content: str) -> str:
        try:
            from firefly_app.backend import workspace

            return json.dumps(workspace.write_file(rel, content), ensure_ascii=False)
        except Exception as e:
            return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)

    @pyqtSlot(str, result=str)
    def wsRun(self, command: str) -> str:
        try:
            from firefly_app.backend import workspace

            return json.dumps(workspace.run_command(command), ensure_ascii=False)
        except Exception as e:
            return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)

    @pyqtSlot(str)
    def evolveStart(self, goal: str):
        """自主迭代：开分支 → 自己改 → 验证 → 成功留分支 / 失败回滚"""
        if getattr(self, "_evolving", False):
            return
        self._evolving = True

        def _run():
            from firefly_app.backend import evolve, ui_bus

            def emit(kind, text):
                ui_bus.broadcast("evolve_event", {"kind": kind, "text": text})

            try:
                result = evolve.run_iteration(goal or "", emit=emit)
                if result.get("ok"):
                    ui_bus.broadcast(
                        "evolve_event",
                        {
                            "kind": "done",
                            "text": result.get("summary", ""),
                            "branch": result.get("branch", ""),
                            "diff": result.get("diff", ""),
                            "report": result.get("report", ""),
                        },
                    )
                else:
                    ui_bus.broadcast(
                        "evolve_event",
                        {"kind": "fail", "text": result.get("error", "未知错误")},
                    )
            except Exception as e:
                ui_bus.broadcast("evolve_event", {"kind": "fail", "text": str(e)})
            finally:
                self._evolving = False

        import threading

        threading.Thread(target=_run, daemon=True).start()

    @pyqtSlot(result=str)
    def evolveState(self) -> str:
        try:
            from firefly_app.backend import evolve

            return json.dumps(evolve.status(), ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": str(e)}, ensure_ascii=False)

    @pyqtSlot(str)
    def wsAsk(self, text: str):
        """跑一轮 AI 工具循环（后台线程，过程通过事件推到前端）"""
        if self._ws_busy:
            return
        q = (text or "").strip()
        if not q:
            return
        self._ws_busy = True

        def _run():
            from firefly_app.backend import ui_bus, workspace

            def emit(kind, msg):
                ui_bus.broadcast("ws_event", {"kind": kind, "text": msg})

            ui_bus.broadcast("ws_event", {"kind": "start", "text": q})
            try:
                result = workspace.agent_ask(q, emit=emit)
                if result.get("ok"):
                    ui_bus.broadcast(
                        "ws_event", {"kind": "reply", "text": result.get("answer", "")}
                    )
                else:
                    ui_bus.broadcast(
                        "ws_event",
                        {"kind": "error", "text": result.get("error", "未知错误")},
                    )
            except Exception as e:
                ui_bus.broadcast("ws_event", {"kind": "error", "text": str(e)})
            finally:
                ui_bus.broadcast("ws_event", {"kind": "done", "text": ""})
                self._ws_busy = False

        import threading

        threading.Thread(target=_run, daemon=True).start()

    @pyqtSlot(result=bool)
    def startVoice(self) -> bool:
        """面板里的 🎤：复用桌宠那条录音→识别→对话的流程"""
        try:
            pb = getattr(self.win, "chat_bridge", None)
            if pb is not None and hasattr(pb, "startVoice"):
                pb.startVoice()
                return True
        except Exception as e:
            print(f"[panel] 语音输入失败: {e}", flush=True)
        return False

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

    @pyqtSlot(result=str)
    def motion3dPath(self) -> str:
        """返回 assets/models3d/motions 里第一个 .vmd 文件名（没有就空串）"""
        d = WORKSPACE_ROOT / "assets" / "models3d" / "motions"
        try:
            names = sorted(p.name for p in d.glob("*.vmd"))
        except Exception:
            names = []
        return names[0] if names else ""

    @pyqtSlot(result=str)
    def model3dPath(self) -> str:
        """返回 assets/models3d 里第一个可用的 .pmx 文件名（不带目录）"""
        d = WORKSPACE_ROOT / "assets" / "models3d"
        try:
            names = [p.name for p in d.glob("*.pmx")]
        except Exception:
            names = []
        if not names:
            return ""
        # 优先主模型，避免加载到 武器.pmx 这类附属文件
        for kw in ("流萤", "firefly", "Firefly"):
            for n in names:
                if kw in n:
                    return n
        return sorted(names)[0]

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


class _PanelPage(QWebEnginePage):
    """把面板页的 JS 控制台输出到 stdout，便于排查"""

    def javaScriptConsoleMessage(self, level, message, lineNumber, sourceID):
        try:
            src = (sourceID or "").rsplit("/", 1)[-1]
            print(f"[panel] {message} ({src}:{lineNumber})", flush=True)
        except Exception:
            pass


class SettingsWindow(QWidget):
    # 跨线程安全的 JS 入口：工作线程只能 emit 信号，实际执行由主线程完成
    sig_js = pyqtSignal(str)

    def __init__(self, server, chat_bridge=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Firefly 控制台")
        # FIREFLY_PANEL_SIZE=706x1000 可以指定窗口尺寸（调试自适应用）
        _w, _h = 940, 780
        try:
            import os as _os

            _sz = _os.environ.get("FIREFLY_PANEL_SIZE", "")
            if "x" in _sz:
                _a, _b = _sz.lower().split("x", 1)
                _w, _h = int(_a), int(_b)
        except Exception:
            pass
        self.resize(_w, _h)

        # 窗口/任务栏图标（优先多尺寸的 firefly.ico）
        try:
            icon_path = ICONS_DIR / "firefly.ico"
            if not icon_path.exists():
                icon_path = ICONS_DIR / "bitbug_favicon.ico"
            if icon_path.exists():
                self.setWindowIcon(QIcon(str(icon_path)))
        except Exception:
            pass

        # 任务栏身份：微软商店版 Python 在进程级锁死了 AppUserModelID，
        # 只能在窗口级覆盖，否则任务栏一直显示 Python 图标。
        self._taskbar_fixed = False

        # 桌宠那边的 Bridge，面板的聊天借它走同一套流程
        self.chat_bridge = chat_bridge

        # 必须先把信号接到主线程槽上，否则工作线程 emit 后没人收
        self.sig_js.connect(self._apply_js)

        self.view = QWebEngineView(self)
        # 用自定义 Page 把面板的 JS 控制台转发到 stdout（最终落到 logs/pet.log），
        # 否则 panel 里的报错完全看不到，像 3D 加载失败这种只能靠猜。
        self.view.setPage(_PanelPage(self.view))
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

        # 调试/截图用：FIREFLY_PANEL_TAB=setting 打开时自动切页签，
        # FIREFLY_PANEL_FOCUS="#card-stt" 再滚动到指定区块
        try:
            import os as _os

            _tab = _os.environ.get("FIREFLY_PANEL_TAB", "")
            _focus = _os.environ.get("FIREFLY_PANEL_FOCUS", "")
            _sub = _os.environ.get("FIREFLY_PANEL_SUB", "")
            _extra = _os.environ.get("FIREFLY_PANEL_JS", "")
            if _tab or _focus or _sub or _extra:
                from PyQt6.QtCore import QTimer as _QTimer

                _js = []
                if _tab:
                    _js.append(
                        'document.querySelector(".tab[data-tab=%s]")?.click();' % _tab
                    )
                if _sub:
                    # 对话页里的子切换（chat / work）
                    _js.append(
                        'document.querySelector(".chat-subnav .sub-tab[data-sub=%s]")?.click();'
                        % _sub
                    )
                if _focus:
                    _js.append(
                        "document.querySelector(%s)?.scrollIntoView({block:'center'});"
                        % repr(_focus)
                    )
                if _extra:
                    # 调试用：直接执行传入的 JS（例如自动点开某个文件）
                    _js.append(_extra)
                _QTimer.singleShot(4000, lambda: self.view.page().runJavaScript("".join(_js)))
        except Exception:
            pass

        # 注册到广播总线：桌宠那边产生的事件（llm_delta 等）这里也能收到
        ui_bus.register(self)

        # 任务栏身份必须在窗口"显示之前"打上：
        # 先强制创建原生窗口句柄（winId()），设置 AUMID，再让窗口出现。
        # 放到 showEvent 里就晚了，任务栏已经按进程身份建好按钮。
        try:
            from firefly_app.frontend import win_taskbar

            _icon = ICONS_DIR / "firefly.ico"
            if not _icon.exists():
                _icon = ICONS_DIR / "bitbug_favicon.ico"

            _aumid = "Firefly.DesktopPet.1"
            if _icon.exists():
                win_taskbar.register_aumid_icon(_aumid, "Firefly 控制台", str(_icon))

            _hwnd = int(self.winId())
            self._taskbar_fixed = win_taskbar.set_window_aumid(_hwnd, _aumid)
            print(
                f"[panel] 任务栏身份: {'已设置' if self._taskbar_fixed else '设置失败（不影响使用）'}",
                flush=True,
            )
        except Exception as e:
            print(f"[panel] 任务栏身份设置异常: {e}", flush=True)

    def showEvent(self, event):
        super().showEvent(event)
        # 窗口显示后任务栏才会重建按钮，这里再补一次确保生效
        if not getattr(self, "_taskbar_fixed", False):
            try:
                from firefly_app.frontend import win_taskbar

                self._taskbar_fixed = win_taskbar.set_window_aumid(
                    int(self.winId()), "Firefly.DesktopPet.1"
                )
            except Exception:
                pass

    def closeEvent(self, event):
        # 关窗口只是隐藏，不注销总线：重新打开时还能接着收到广播、保留聊天记录
        # 面板打开时藏起来的是桌宠本体，这里把它放回来
        try:
            if getattr(self, "chat_bridge", None) is not None:
                w = getattr(self.chat_bridge, "win", None)
                if w is not None:
                    w.show()
        except Exception:
            pass
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

            # 提示条开关也当场生效
            ui_bus.broadcast("set_hint", {"show": bool(raw.get("show_hint", True))})
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
    # 写入方是"先写 .tmp 再替换"，替换的一瞬间读可能撞上，
    # 短暂重试几次，避免读到空配置把设置覆盖掉
    for _ in range(6):
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
        except OSError:
            time.sleep(0.03)
        except Exception:
            return {}
    return {}


def write_raw(raw: dict) -> None:
    """写配置文件：先写 .tmp 再替换（原子），失败时短暂重试。

    Windows 上 os.replace 要删除目标文件，如果这一刻别处正开着读句柄，
    会抛 WinError 5 拒绝访问。读取都很快，重试几乎总能成功。
    """
    tmp = CONFIG_PATH.with_name(CONFIG_PATH.name + ".tmp")
    tmp.write_text(
        json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    last = None
    for _ in range(12):
        try:
            tmp.replace(CONFIG_PATH)
            return
        except PermissionError as e:
            last = e
            time.sleep(0.05)

    # 兜底：直接覆盖写（失去原子性，但至少能保存成功）
    try:
        CONFIG_PATH.write_text(tmp.read_text(encoding="utf-8"), encoding="utf-8")
        tmp.unlink(missing_ok=True)
        print("[config] 原子替换失败，已退回直接覆盖写入", flush=True)
    except Exception:
        raise last if last else RuntimeError("写入配置失败")


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
