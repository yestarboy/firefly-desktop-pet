# -*- coding: utf-8 -*-
"""
桌宠（稳定启动结构）：
- 透明无边框窗口 + 托盘
- 内置 HTTP 静态服务器（保证 live2d-libs / Firefly-desktop 可被页面加载）
- QWebChannel: pyBridge（兼容旧 bridge 名称）
- LLM：线上 API（DeepSeek），可切回本地 Ollama，见 firefly_app/backend/llm.py
- TTS：优先 GPT-SoVITS（大模型），不可用则降级到 lightweight_tts
"""

import json
import sys
import threading
import socket
import time
import traceback
from pathlib import Path
import webbrowser

# 源码在 src/ 下，运行入口留在项目根，方便静态服务器直接以根为 web 根。
PROJECT_ROOT = Path(__file__).resolve().parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import requests
from PyQt6.QtCore import Qt, QUrl, QObject, pyqtSlot, pyqtSignal
from PyQt6.QtGui import QAction, QIcon
from PyQt6.QtWidgets import QApplication, QWidget, QMenu, QSystemTrayIcon
from PyQt6.QtWebChannel import QWebChannel
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWebEngineCore import QWebEngineSettings, QWebEnginePage

from firefly_app.frontend.live2d_server import Live2DServer
from firefly_app.backend import ui_bus
from firefly_app.paths import ICONS_DIR, LOGS_DIR

# ====== 语音识别服务 ======
# 由 tools/stt_server.py 提供（跑在 GPT-SoVITS 的 runtime Python 里）
STT_URL = "http://127.0.0.1:9881"

# TTS 正在播报时置位：唤醒词监听要暂停，否则会把自己的声音听成指令
_tts_speaking = threading.Event()

# ====== LLM 配置 ======
# 已经迁到 firefly_app/backend/llm.py：
#   - 线上：DeepSeek（OpenAI 兼容 /chat/completions，SSE 流式）
#   - 本地：Ollama
# 切换方式：改 D:\AI\firefly_config.json 里的 "provider"
# 自测：python -m firefly_app.backend.llm "你好"

_single_instance_socket = None


def _log_line(msg: str):
    try:
        try:
            LOGS_DIR.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        log_path = LOGS_DIR / "pet_start.log"
        # 用 utf-8-sig（带 BOM）避免 Windows 记事本打开乱码
        if (not log_path.exists()) or log_path.stat().st_size == 0:
            with log_path.open("w", encoding="utf-8-sig", errors="replace") as f:
                f.write("")
        with log_path.open("a", encoding="utf-8-sig", errors="replace") as f:
            f.write(msg.rstrip() + "\n")
    except Exception:
        pass


def _acquire_single_instance_lock(port: int = 8764) -> bool:
    """
    单实例锁：用本机端口占用实现。
    - 已经有桌宠在跑：绑定失败 → 直接退出（避免重复启动导致端口冲突/崩溃）
    """
    global _single_instance_socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", int(port)))
        s.listen(1)
        _single_instance_socket = s
        _log_line(f"[pet] single-instance lock ok on 127.0.0.1:{port}")
        return True
    except OSError:
        try:
            s.close()
        except Exception:
            pass
        _log_line(f"[pet] another instance is already running (lock port {port} busy)")
        return False


def _pick_speaker():
    # 1) 主方案：GPT-SoVITS（firefly_app.backend.voice，自带队列与流水线）
    try:
        from firefly_app.backend.voice import speak as _s

        return _s
    except Exception:
        pass

    # 2) 兜底：Windows 系统语音
    try:
        from lightweight_tts import speak as _s

        return _s
    except Exception:
        pass

    def _noop(*_args, **_kwargs):
        return None

    return _noop


_speak = _pick_speaker()


def _tts_say(text: str, emotion: str = "中立", interrupt: bool = True):
    """
    兼容不同降级实现：voice.speak 支持 emotion/interrupt，
    而 tts_gpt_sovits / lightweight_tts 的 speak 只收 text。
    """
    try:
        _speak(text, emotion=emotion, interrupt=interrupt)
        return
    except TypeError:
        pass
    try:
        _speak(text, emotion=emotion)
        return
    except TypeError:
        pass
    _speak(text)


class DebugWebEnginePage(QWebEnginePage):
    def javaScriptConsoleMessage(self, level, message, lineNumber, sourceID):
        print(f"[WebEngine] {message} (line {lineNumber})")


def find_tray_icon() -> QIcon:
    root = Path(__file__).resolve().parent

    # 多尺寸的 firefly.ico 优先（托盘/任务栏各种缩放都清晰）
    for p in (ICONS_DIR / "firefly.ico", root / "assets" / "icons" / "firefly.ico"):
        try:
            if p.exists():
                icon = QIcon(str(p))
                if not icon.isNull():
                    return icon
        except Exception:
            pass

    candidates = []
    for d in (ICONS_DIR, root / "assets" / "icons", root):
        try:
            candidates += list(d.glob("*.ico"))
            candidates += list(d.glob("*.png"))
        except Exception:
            pass
    for p in candidates:
        try:
            icon = QIcon(str(p))
            if not icon.isNull():
                return icon
        except Exception:
            pass
    return QIcon()


class PetWindow(QWidget):
    sig_js = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self._really_quit = False

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        # 初始尺寸；网页会通过 resizeWindow 贴合模型
        self.resize(520, 820)
        self.move(80, 80)

        # 静态资源服务器（强引用）
        project_root = Path(__file__).resolve().parent
        self.server = Live2DServer(project_root, port=8765)
        self.server.start()

        # WebEngine
        self.web = QWebEngineView(self)
        self.web.setGeometry(0, 0, self.width(), self.height())
        self.web.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)

        page = DebugWebEnginePage(self.web)
        self.web.setPage(page)
        page.setBackgroundColor(Qt.GlobalColor.transparent)

        s = self.web.settings()
        s.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True)
        s.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)
        s.setAttribute(QWebEngineSettings.WebAttribute.JavascriptEnabled, True)

        # WebChannel（强引用）
        self.channel = QWebChannel(self.web.page())
        self.bridge = Bridge(self)
        self.channel.registerObject("pyBridge", self.bridge)
        self.channel.registerObject("bridge", self.bridge)  # 兼容旧名
        self.web.page().setWebChannel(self.channel)

        self.sig_js.connect(self._run_js)

        url = QUrl(self.server.get_url("live2d_pet.html"))
        print(f"加载 URL: {url.toString()}")
        self.web.load(url)

        self._init_tray()
        print("桌宠启动完成（托盘可操作）")

        # 注册 TTS 嘴型回调
        # 用 broadcast 而不是 send_to_js：桌宠窗口和控制台面板都要收到，
        # 面板里的 3D 模型靠这两个事件做口型同步。
        try:
            from firefly_app.backend.voice import set_tts_callbacks

            def _on_tts_start():
                _tts_speaking.set()     # 说话期间暂停唤醒词监听，避免自听
                ui_bus.broadcast("tts_start", {})

            def _on_tts_end():
                _tts_speaking.clear()
                ui_bus.broadcast("tts_end", {})

            set_tts_callbacks(on_start=_on_tts_start, on_end=_on_tts_end)
        except Exception as e:
            print(f"[pet] TTS 回调注册失败: {e}")

        # 注册到广播总线，面板和桌宠共用同一套事件
        ui_bus.register(self)

    def run_js(self, js: str):
        """ui_bus 的统一入口（经由信号切回 GUI 线程）"""
        self.sig_js.emit(js)

    def _init_tray(self):
        icon = find_tray_icon()
        self.tray = QSystemTrayIcon(icon, self)
        self.tray.setToolTip("Firefly 桌宠")

        menu = QMenu()
        act_toggle = QAction("显示/隐藏", self)
        act_toggle.triggered.connect(self.toggle_show_hide)
        menu.addAction(act_toggle)

        act_reload = QAction("重载页面", self)
        act_reload.triggered.connect(self.web.reload)
        menu.addAction(act_reload)

        menu.addSeparator()

        act_webui = QAction("打开 GPT-SoVITS WebUI", self)
        act_webui.triggered.connect(lambda: webbrowser.open("http://127.0.0.1:9874"))
        menu.addAction(act_webui)

        act_api = QAction("打开 GPT-SoVITS API 文档", self)
        act_api.triggered.connect(lambda: webbrowser.open("http://127.0.0.1:9880/docs"))
        menu.addAction(act_api)

        menu.addSeparator()

        act_voice = QAction("切换语音模式（热键麦 / 全程麦）", self)
        act_voice.triggered.connect(self.toggle_voice_mode)
        menu.addAction(act_voice)

        act_settings = QAction("设置…", self)
        act_settings.triggered.connect(self.open_settings)
        menu.addAction(act_settings)

        # 调试用：不经过 LLM，直接从 Python 触发一个动作，用来单独验证前端
        act_test = QAction("测试动作（猫耳）", self)
        act_test.triggered.connect(lambda: self.send_to_js("pet_motion", {"name": "猫耳"}))
        menu.addAction(act_test)

        menu.addSeparator()

        act_exit = QAction("退出", self)
        act_exit.triggered.connect(self.quit_app)
        menu.addAction(act_exit)

        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.show()

    def toggle_voice_mode(self):
        """托盘一键切换 全程麦 / 热键麦，顺便把空配置补成默认值"""
        try:
            from firefly_app.backend import llm, ui_bus
            from firefly_app.frontend import settings_window as sw

            raw = sw.normalize_voice(sw._read_raw())
            new_mode = "hotkey" if raw["voice_mode"] != "hotkey" else "always"
            raw["voice_mode"] = new_mode

            sw.write_raw(raw)
            llm.load_config(force=True)

            ui_bus.broadcast("voice_config", {
                "mode": new_mode, "hotkey": raw["voice_hotkey"],
            })
            print(f"[hotkey] 语音模式 -> {new_mode}（热键 {raw['voice_hotkey']}）", flush=True)
        except Exception as e:
            print(f"[hotkey] 切换语音模式失败: {e}", flush=True)

    def open_settings(self):
        """打开可视化设置窗口（懒加载，只创建一次）

        打开时把桌宠本体藏起来：控制台里已经有 3D 角色，
        两个窗口叠着既挡视线又白烧一份 GPU。
        """
        try:
            if getattr(self, "_settings_win", None) is None:
                from firefly_app.frontend.settings_window import SettingsWindow

                self._settings_win = SettingsWindow(self.server, chat_bridge=self.bridge)
            self._settings_win.show()
            self._settings_win.raise_()
            self._settings_win.activateWindow()
            self.hide()
        except Exception as e:
            print(f"[settings] 打开失败: {e}", flush=True)

    def _on_tray_activated(self, reason):
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self.toggle_show_hide()

    def _run_js(self, js: str):
        try:
            self.web.page().runJavaScript(js)
        except Exception as e:
            print(f"runJavaScript failed: {e}")

    def resizeEvent(self, event):
        self.web.setGeometry(0, 0, self.width(), self.height())
        super().resizeEvent(event)

    def send_to_js(self, event_name: str, payload: dict):
        js = (
            "window.__pet_on_py_event && "
            f"window.__pet_on_py_event({json.dumps(event_name)}, {json.dumps(payload)});"
        )
        self.sig_js.emit(js)

    def toggle_show_hide(self):
        if self.isVisible():
            self.hide()
        else:
            self.show()
            self.raise_()
            self.activateWindow()

    def quit_app(self):
        self._really_quit = True
        try:
            if hasattr(self, "tray") and self.tray:
                self.tray.hide()
        except Exception:
            pass
        try:
            self.server.stop()
        except Exception:
            pass
        QApplication.quit()

    def closeEvent(self, event):
        if not self._really_quit:
            event.ignore()
            self.hide()
            return
        super().closeEvent(event)


class WakeListener:
    """全程麦 + 唤醒词：一直听，只有听到唤醒词才响应

    实现上就是循环调用 STT 服务的 /listen（它自带 VAD 断句）：
    听到一句 → 转文字 → 含唤醒词就处理剩下的部分，否则丢掉。
    说话期间暂停，避免把自己的 TTS 听成指令。
    """

    def __init__(self, bridge):
        self.bridge = bridge
        self._stop = threading.Event()
        self._thread = None
        # 只喊了唤醒词时，这个时间点之前说的下一句会被当成指令
        self._await_until = 0.0

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        print("[wake] 唤醒词监听已启动", flush=True)

    def stop(self):
        self._stop.set()

    def _cfg(self):
        """每次循环都读一遍配置，改唤醒词/开关立即生效（不做缓存）"""
        try:
            from firefly_app.frontend import settings_window as sw

            raw = sw._read_raw()
            return (
                str(raw.get("wake_word") or "萤宝").strip(),
                raw.get("wake_enabled", True) is not False,
                str(raw.get("voice_mode") or "always"),
            )
        except Exception:
            return "萤宝", True, "always"

    def _loop(self):
        import requests

        armed = None   # 上一次的监听状态，用于打印"开始/暂停"

        while not self._stop.is_set():
            word, enabled, mode = self._cfg()

            now_armed = bool(enabled and mode == "always" and word)
            if now_armed != armed:
                armed = now_armed
                print(
                    f"[wake] {'开始监听（全程麦）' if armed else '暂停监听'} "
                    f"mode={mode} word={word!r}",
                    flush=True,
                )

            # 只在"全程麦"且开启唤醒词时才监听
            if not armed:
                time.sleep(1.0)
                continue

            if _tts_speaking.is_set():
                time.sleep(0.3)
                continue

            try:
                r = requests.post(f"{STT_URL}/listen", timeout=60)
                text = str((r.json() or {}).get("text") or "").strip()
            except Exception:
                time.sleep(1.5)
                continue

            if not text:
                continue

            # 调试用：把听到的每一句都打出来，方便确认唤醒词为什么没命中
            print(f"[wake] 听到: {text!r}", flush=True)

            # 刚说完唤醒词、还没给指令：给 8 秒的"接着说"窗口
            if self._await_until and time.time() < self._await_until:
                if word not in text:
                    self._await_until = 0.0
                    print(f"[wake] 补充指令 {text!r}", flush=True)
                    self.bridge.askLLM(text)
                    continue
                self._await_until = 0.0

            if word not in text:
                continue

            rest = text.split(word, 1)[1].strip(" ，,。.！!？?~～、")
            print(f"[wake] 命中 {text!r} → 指令 {rest!r}", flush=True)

            if rest:
                self.bridge.askLLM(rest)
            else:
                # 只喊了唤醒词：应答一声，并开启"接着说话"窗口
                self._await_until = time.time() + 8.0
                ui_bus.broadcast(
                    "llm_reply",
                    {"answer": "我在。", "emotion": "开心", "skip_motion": True},
                )


class Bridge(QObject):
    def __init__(self, win: PetWindow):
        super().__init__()
        self.win = win
        self._seq = 0          # 每问一次 +1，用来丢掉上一轮的过期流
        self._ptt_active = False   # 热键是否正处于"按住录音"状态
        self._ptt_fail_at = 0.0    # 上次启动失败的时间（冷却用）
        # 短期对话记忆（最近几轮），让"它还记得刚才说了什么"
        self._history = []
        self._history_max = 16     # 8 轮往返
        self._history_lock = threading.Lock()

    @pyqtSlot(result=str)
    def usageInfo(self) -> str:
        """当天 token 用量 + 账户余额（鼠标悬停桌宠时显示）"""
        try:
            from firefly_app.backend import llm as _llm

            return json.dumps(_llm.usage_info(), ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": str(e)}, ensure_ascii=False)

    @pyqtSlot(int, int)
    def moveWindowBy(self, dx, dy):
        self.win.move(self.win.x() + int(dx), self.win.y() + int(dy))

    @pyqtSlot(result=bool)
    def startDrag(self) -> bool:
        """
        把拖动交给 Windows 自己处理（QWindow.startSystemMove）。

        旧做法是页面每帧发一次位移、Python 再 win.move()：
        - 每帧一次跨进程 IPC，鼠标快时请求堆积成卡顿
        - win.move() 会让半透明窗口整层重新合成，掉帧
        系统级移动由 DWM 直接搬窗口，不经过 IPC、不重绘，跟手程度和拖原生窗口一样。
        """
        try:
            wh = self.win.windowHandle()
            if wh is not None and wh.startSystemMove():
                return True
        except Exception as e:
            print(f"[drag] startSystemMove 失败: {e}", flush=True)
        return False

    @pyqtSlot(int, int)
    def resizeWindow(self, w, h):
        w = max(260, int(w))
        h = max(260, int(h))
        self.win.resize(w, h)

    @pyqtSlot(bool)
    def setVoiceMode(self, on: bool):
        """
        语音模式开关。

        前端在开启时会隐藏输入框；真正的语音识别（麦克风 → 文字）还没有接入，
        这里先把状态记下来，方便后续挂 STT 引擎。
        """
        print(f"[pet] 语音模式: {'开' if on else '关'}", flush=True)

    @pyqtSlot()
    def startVoice(self):
        """点 🎤 的行为

        - 懒人麦（always + 唤醒词）：麦克风已经在常听，点它没意义，
          只提示"直接叫唤醒词"。
        - 其它情况：录一句 → 识别 → 提问。
        """
        mode, _name, _vk = self._voice_cfg()
        try:
            from firefly_app.frontend import settings_window as sw

            raw = sw._read_raw()
            wake_on = raw.get("wake_enabled", True) is not False
            wake_word = str(raw.get("wake_word") or "萤宝")
        except Exception:
            wake_on, wake_word = True, "萤宝"

        if mode == "always" and wake_on:
            ui_bus.broadcast(
                "voice_state",
                {"state": "hint", "message": f"直接说「{wake_word}」+ 指令就行"},
            )
            return

        threading.Thread(target=self._voice_worker, daemon=True).start()

    @pyqtSlot(result=str)
    def getVoiceConfig(self) -> str:
        """给前端：当前是全程麦还是热键麦、热键叫什么"""
        mode, name, _vk = self._voice_cfg()
        return json.dumps({"mode": mode, "hotkey": name}, ensure_ascii=False)

    # ------------------------------------------------ 热键按住说话

    def _voice_cfg(self):
        try:
            from firefly_app.backend import llm
            from firefly_app.backend.hotkey import VK_MAP

            cfg = llm.load_config()
            mode = str(cfg.get("voice_mode") or "always")
            name = str(cfg.get("voice_hotkey") or "F9")
            return mode, name, VK_MAP.get(name, 0)
        except Exception:
            return "always", "F9", 0x78

    def _mic_query(self) -> str:
        try:
            from firefly_app.backend import llm

            mic = str(llm.load_config().get("stt_mic") or "").strip()
        except Exception:
            mic = ""
        return f"?mic={mic}" if mic else ""

    def on_hotkey_down(self, vk: int):
        """键盘钩子回调 —— 必须立刻返回，所以只起线程"""
        mode, _name, target = self._voice_cfg()
        if mode != "hotkey" or not target or vk != target:
            return
        if self._ptt_active:
            return
        # 上一次启动失败后短暂冷却，避免按住不放时按键重复刷屏
        if time.time() - self._ptt_fail_at < 1.5:
            return
        self._ptt_active = True
        threading.Thread(target=self._ptt_start_worker, daemon=True).start()

    def on_hotkey_up(self, vk: int):
        mode, _name, target = self._voice_cfg()
        if mode != "hotkey" or not target or vk != target:
            return
        if not self._ptt_active:
            return
        self._ptt_active = False
        threading.Thread(target=self._ptt_stop_worker, daemon=True).start()

    def _ptt_start_worker(self):
        from firefly_app.backend import voice as voice_mod

        try:
            voice_mod.stop_audio()      # 别把流萤自己的声音录进去
        except Exception:
            pass

        ui_bus.broadcast("voice_state", {"state": "listening"})
        try:
            r = requests.post(
                f"{STT_URL}/ptt/start{self._mic_query()}", timeout=10
            )
            if r.status_code != 200:
                try:
                    detail = r.json().get("message") or r.text
                except Exception:
                    detail = (r.text or "")[:200]
                raise RuntimeError(detail)
            print("[stt] 热键录音开始", flush=True)
        except Exception as e:
            self._ptt_active = False
            self._ptt_fail_at = time.time()
            print(f"[stt] 开始录音失败: {e}", flush=True)
            ui_bus.broadcast("voice_state", {"state": "error", "message": str(e)})

    def _ptt_stop_worker(self):
        ui_bus.broadcast("voice_state", {"state": "transcribing"})
        try:
            r = requests.post(f"{STT_URL}/ptt/stop", timeout=60)
            if r.status_code != 200:
                try:
                    detail = r.json().get("error") or r.text
                except Exception:
                    detail = (r.text or "")[:200]
                raise RuntimeError(f"HTTP {r.status_code}：{detail}")
            data = r.json()
            text = (data.get("text") or "").strip()
        except Exception as e:
            print(f"[stt] 结束录音失败: {e}", flush=True)
            ui_bus.broadcast("voice_state", {"state": "error", "message": str(e)})
            return

        ui_bus.broadcast("voice_state", {"state": "idle"})
        if not text:
            ui_bus.broadcast("voice_state", {"state": "empty"})
            return

        print(f"[stt] 热键识别到: {text}", flush=True)
        self.askLLM(text)

    def _voice_worker(self):
        from firefly_app.backend import voice as voice_mod

        # 先掐掉正在播的语音，否则麦克风会把流萤自己的声音录进去
        try:
            voice_mod.stop_audio()
        except Exception:
            pass

        ui_bus.broadcast("voice_state", {"state": "listening"})
        try:
            # 麦克风索引来自配置（设置面板可选）
            mic = ""
            try:
                from firefly_app.backend import llm as _llm

                mic = str(_llm.load_config().get("stt_mic") or "").strip()
            except Exception:
                mic = ""

            url = f"{STT_URL}/listen" + (f"?mic={mic}" if mic else "")
            r = requests.post(url, timeout=60)
            if r.status_code != 200:
                try:
                    detail = r.json().get("error") or r.text
                except Exception:
                    detail = (r.text or "")[:200]
                raise RuntimeError(f"HTTP {r.status_code}：{detail}")
            text = (r.json().get("text") or "").strip()
        except Exception as e:
            print(f"[stt] 识别失败: {e}", flush=True)
            ui_bus.broadcast(
                "voice_state", {"state": "error", "message": str(e)}
            )
            return

        ui_bus.broadcast("voice_state", {"state": "idle"})
        if not text:
            ui_bus.broadcast("voice_state", {"state": "empty"})
            return

        print(f"[stt] 识别到: {text}", flush=True)
        self.askLLM(text)

    @pyqtSlot(str)
    def askLLM(self, text: str):
        q = (text or "").strip()
        if not q:
            return

        self._seq += 1
        seq = self._seq

        # 取长期记忆，拼进系统提示；取不到就当没有
        mem_block = ""
        try:
            from firefly_app.backend import memory

            mem_block = memory.prompt_block()
        except Exception as e:
            print(f"[memory] 读取记忆失败: {e}", flush=True)

        if mem_block:
            print(f"[memory] 注入 {mem_block.count(chr(10) + '- ')} 条记忆", flush=True)

        # 让面板也知道用户说了什么（语音和文字共用同一条通路）
        ui_bus.broadcast("user_text", {"text": q})

        ui_bus.broadcast("llm_status", {"status": "thinking"})
        threading.Thread(
            target=self._reply_worker, args=(q, seq, mem_block), daemon=True
        ).start()

    def _history_snapshot(self):
        """最近几轮对话（给 llm 做上下文）"""
        with self._history_lock:
            return list(self._history)

    def _remember(self, q: str, a: str):
        """把一轮问答存进短期上下文，超过上限就丢最旧的"""
        q = (q or "").strip()
        a = (a or "").strip()
        if not q or not a:
            return
        with self._history_lock:
            self._history.append({"role": "user", "content": q})
            self._history.append({"role": "assistant", "content": a})
            while len(self._history) > self._history_max:
                self._history.pop(0)

    def _reply_worker(self, q: str, seq: int, mem_block: str = ""):
        """流式收字 → 解析动作 → 逐句合成播放（第一句一成型就开口）"""
        from firefly_app.backend import llm
        from firefly_app.backend.actions import ActionFilter
        from firefly_app.backend.sentences import SentenceSplitter
        from firefly_app.backend.voice import _infer_emotion

        def stale() -> bool:
            return seq != self._seq

        t0 = time.monotonic()

        motion_filter = ActionFilter()
        splitter = SentenceSplitter()
        shown_parts = []   # 过滤后、用于显示和朗读的文本
        raw_parts = []     # 过滤前的原始文本（调试用）
        pending = []       # 还没推给前端的增量
        last_flush = 0.0
        had_motion = False
        spoke_any = False

        def flush():
            nonlocal last_flush
            if not pending:
                return
            ui_bus.broadcast("llm_delta", {"text": "".join(pending)})
            pending.clear()
            last_flush = time.monotonic()

        def say(text: str):
            """排进 TTS 流水线：本轮第一句打断旧声音，后续句子排队接着播"""
            nonlocal spoke_any
            s = (text or "").strip()
            if not s or stale():
                return
            try:
                emo = _infer_emotion(s)
            except Exception:
                emo = "中立"
            try:
                _tts_say(s, emotion=emo, interrupt=not spoke_any)
            except Exception as e:
                print(f"[TTS] 排入队列失败: {e}", flush=True)
            print(
                f"[tts] +{time.monotonic() - t0:5.2f}s 入队({len(s)}字): {s[:24]}",
                flush=True,
            )
            spoke_any = True

        # 本地指令优先：不花 token、不等模型，说完就执行
        try:
            from firefly_app.backend import skills

            hit = skills.try_handle(q)
        except Exception as e:
            print(f"[skills] 解析失败: {e}", flush=True)
            hit = None

        if hit:
            print(f"[skills] 命中 {hit.get('action')}: {hit.get('text')}", flush=True)
            ui_bus.broadcast("llm_start", {"q": q})
            ui_bus.broadcast(
                "llm_reply",
                {
                    "answer": hit["text"],
                    "emotion": hit.get("emotion", "开心"),
                    "skip_motion": True,
                },
            )
            say(hit["text"])
            self._remember(q, hit["text"])
            return

        # 带上最近几轮对话，这样"它还记得刚才说了什么"
        history = self._history_snapshot()

        try:
            started = False
            for piece in llm.chat_stream(q, history=history, extra_system=mem_block):
                if stale():
                    return
                if not started:
                    started = True
                    ui_bus.broadcast("llm_start", {"q": q})

                raw_parts.append(piece)
                shown, motions = motion_filter.feed(piece)

                for name in motions:
                    had_motion = True
                    print(f"[motion] 解析到动作标签: {name}", flush=True)
                    ui_bus.broadcast("pet_motion", {"name": name})

                if shown:
                    shown_parts.append(shown)
                    pending.append(shown)
                    if time.monotonic() - last_flush >= 0.05:
                        flush()
                    # 关键：一句话说完就送去合成，不等整段回复结束
                    for sent in splitter.feed(shown):
                        say(sent)

            tail = motion_filter.flush()
            if tail:
                shown_parts.append(tail)
                pending.append(tail)
                for sent in splitter.feed(tail):
                    say(sent)

            for sent in splitter.flush():
                say(sent)
            flush()

            ans = "".join(shown_parts).strip() or "(空回复)"
            raw_ans = "".join(raw_parts).strip()
            print(f"[llm] 原始回复: {raw_ans[:120]!r}", flush=True)
            print(f"[llm] 动作标签: {'有' if had_motion else '无'}", flush=True)

            try:
                emo = _infer_emotion(ans)
            except Exception:
                emo = "中立"

            # 本轮已经有明确的动作标签时，别再让情绪兜底随机播一个把它顶掉
            ui_bus.broadcast(
                "llm_reply",
                {"q": q, "answer": ans, "emotion": emo, "skip_motion": had_motion},
            )

            # 记进短期上下文，下一轮就能接上
            self._remember(q, ans)

            # 后台把这一轮提炼成记忆（另起线程，不影响说话）
            try:
                from firefly_app.backend import memory

                memory.learn_async(q, ans)
            except Exception as e:
                print(f"[memory] 提炼启动失败: {e}", flush=True)
        except Exception as e:
            flush()
            ui_bus.broadcast("llm_error", {"error": str(e)})
        finally:
            ui_bus.broadcast("llm_status", {"status": "idle"})


def main():
    try:
        # 原生崩溃（段错误）默认不留任何痕迹；开启后至少能打印出崩溃时的调用栈
        try:
            import faulthandler

            faulthandler.enable()
        except Exception:
            pass

        _log_line("==== live2d_pet start ====")

        # 最底层的那句话：每次启动都念一遍，提醒自己为什么写这个项目
        try:
            from firefly_app.backend import soul

            print(f"[soul] {soul.CREED}", flush=True)
        except Exception:
            pass

        # 任务栏图标：不设置 AppUserModelID 的话，Windows 会把窗口
        # 归到 python.exe 名下、显示 Python 图标，而不是我们的图标。
        # 注意：AUMID 还必须在注册表里登记图标，否则任务栏会回退到 exe 图标。
        try:
            import ctypes as _ctypes

            _aumid = "Firefly.DesktopPet.1"
            _ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(_aumid)

            try:
                import winreg

                _icon = Path(__file__).resolve().parent / "assets" / "icons" / "firefly.ico"
                if not _icon.exists():
                    _icon = Path(__file__).resolve().parent / "assets" / "icons" / "bitbug_favicon.ico"

                _key_path = r"Software\Classes\AppUserModelId" + "\\" + _aumid
                with winreg.CreateKeyEx(
                    winreg.HKEY_CURRENT_USER, _key_path, 0, winreg.KEY_WRITE
                ) as _k:
                    winreg.SetValueEx(_k, "DisplayName", 0, winreg.REG_SZ, "Firefly 控制台")
                    if _icon.exists():
                        winreg.SetValueEx(_k, "IconUri", 0, winreg.REG_SZ, str(_icon))
                print(f"[pet] 任务栏标识已登记: {_aumid}", flush=True)
            except Exception as e:
                print(f"[pet] 登记任务栏图标失败: {e}", flush=True)
        except Exception as e:
            print(f"[pet] 设置 AppUserModelID 失败: {e}", flush=True)

        # 压一下 WebEngine 的内存：限制渲染进程数、限制 V8 老生代，
        # 关掉用不上的磁盘着色器缓存（15G 内存的机器上这点很关键）
        try:
            import os as _os

            flags = _os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "")
            extra = (
                "--renderer-process-limit=1 "
                "--js-flags=--max-old-space-size=256 "
                "--disable-gpu-shader-disk-cache"
            )
            if "--max-old-space-size" not in flags:
                _os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = (flags + " " + extra).strip()
                print("[mem] 已设置 WebEngine 内存限制参数", flush=True)
        except Exception:
            pass

        # 初始化记忆库并清理过期记录
        try:
            from firefly_app.backend import memory

            memory.init_db()
            gone = memory.purge_expired()
            if gone:
                print(f"[memory] 清理过期记忆 {gone} 条", flush=True)
        except Exception as e:
            print(f"[memory] 初始化失败: {e}", flush=True)

        if not _acquire_single_instance_lock():
            return
        # Windows：必须给进程一个独立的 AppUserModelID，
        # 否则任务栏会按宿主 pythonw.exe 来分组、显示 Python 图标，
        # setWindowIcon 也压不过它。必须在 QApplication 之前设置。
        try:
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "Firefly.DesktopPet"
            )
        except Exception as e:
            print(f"[icon] 设置 AppUserModelID 失败: {e}", flush=True)

        app = QApplication(sys.argv)
        app.setQuitOnLastWindowClosed(False)

        # 关掉 WebEngine 的磁盘 HTTP 缓存。
        # 否则改了 panel 的 js/css 后，页面还是加载旧版本（服务器加了 no-store
        # 也救不了已缓存的条目），排查起来极其费劲。
        try:
            from PyQt6.QtWebEngineCore import QWebEngineProfile

            QWebEngineProfile.defaultProfile().setHttpCacheType(
                QWebEngineProfile.HttpCacheType.MemoryHttpCache
            )
        except Exception as e:
            print(f"[pet] 设置 WebEngine 缓存失败: {e}", flush=True)

        # 全局窗口/任务栏图标（托盘图标另有 find_tray_icon）
        try:
            _icon = find_tray_icon()
            if not _icon.isNull():
                app.setWindowIcon(_icon)
        except Exception:
            pass

        win = PetWindow()
        win.show()

        # 开发用：FIREFLY_OPEN_PANEL=1 时自动打开控制台面板。
        # 无界面环境下（我这边）没法点托盘，靠它才能抓到面板的 [panel] 日志。
        try:
            import os

            if os.environ.get("FIREFLY_OPEN_PANEL") == "1":
                from PyQt6.QtCore import QTimer

                QTimer.singleShot(2000, win.open_settings)

                # 再等渲染稳定，自动截一张面板图存到 logs/panel-shot.png
                if os.environ.get("FIREFLY_SHOT") == "1":

                    def _shot(idx=1):
                        try:
                            w = getattr(win, "_settings_win", None)
                            if w is None:
                                return
                            from firefly_app.paths import LOGS_DIR

                            # 窗口若处于隐藏/最小化，grab() 会拿到空白图
                            if not w.isVisible():
                                w.show()
                            w.raise_()
                            w.activateWindow()

                            name = "panel-shot.png" if idx == 1 else f"panel-shot{idx}.png"
                            out = LOGS_DIR / name
                            ok = w.grab().save(str(out), "PNG")
                            print(f"[shot] {'已保存' if ok else '保存失败'} {out}", flush=True)
                        except Exception as e:
                            print(f"[shot] 截图失败: {e}", flush=True)

                    # 连拍两张（间隔 3 秒），用来确认待机动画真的在动
                    QTimer.singleShot(12000, _shot)
                    QTimer.singleShot(15000, lambda: _shot(2))

                    # 抽搐检测用：短时间内连拍多张，由脚本比对相邻帧差异
                    if os.environ.get("FIREFLY_BURST") == "1":

                        def _burst(i):
                            try:
                                w = getattr(win, "_settings_win", None)
                                if w is None:
                                    return
                                from firefly_app.paths import LOGS_DIR

                                w.grab().save(str(LOGS_DIR / f"burst-{i:02d}.png"), "PNG")
                            except Exception:
                                pass

                        for _i in range(10):
                            _start = int(os.environ.get("FIREFLY_BURST_AT", "16000"))
                            QTimer.singleShot(_start + _i * 160, lambda i=_i: _burst(i))
                        print("[shot] 连拍 10 张用于抽搐检测", flush=True)
        except Exception as e:
            print(f"[pet] 自动打开面板失败: {e}", flush=True)

        # 后台预热 TTS，避免"第一句特别慢"
        try:
            from firefly_app.backend import voice

            voice.prewarm()
        except Exception as e:
            print(f"[pet] TTS 预热启动失败: {e}", flush=True)

        # 语音配置自愈：空值补默认，并打印当前模式（方便排查）
        try:
            from firefly_app.backend import llm
            from firefly_app.frontend import settings_window as sw

            raw = sw.normalize_voice(sw._read_raw())
            sw.write_raw(raw)
            llm.load_config(force=True)
            print(
                f"[hotkey] 语音模式={raw['voice_mode']} 热键={raw['voice_hotkey']}",
                flush=True,
            )

            # 底部提示条开关（设置里可关）
            try:
                win.send_to_js("set_hint", {"show": bool(raw.get("show_hint", True))})
            except Exception:
                pass
        except Exception as e:
            print(f"[hotkey] 读取语音配置失败: {e}", flush=True)

        # 全局键盘热键（按住说话）
        try:
            from firefly_app.backend.hotkey import HotkeyHook

            hook = HotkeyHook(
                on_down=win.bridge.on_hotkey_down,
                on_up=win.bridge.on_hotkey_up,
            )
            if hook.start():
                win._hotkey_hook = hook
                print("[hotkey] 全局热键监听已启动", flush=True)
            else:
                print("[hotkey] 安装键盘钩子失败（热键麦不可用）", flush=True)
        except Exception as e:
            print(f"[hotkey] 启动失败: {e}", flush=True)

        # 唤醒词监听：全程麦模式下常驻，听到唤醒词才响应
        try:
            from firefly_app.backend import llm as _llm

            _wcfg = _llm.load_config()
            _wake = WakeListener(win.bridge)
            _wake.start()
            print(
                f"[wake] 唤醒词={_wcfg.get('wake_word')!r} "
                f"开启={_wcfg.get('wake_enabled')} "
                f"模式={_wcfg.get('voice_mode')}",
                flush=True,
            )
        except Exception as e:
            print(f"[wake] 启动失败: {e}", flush=True)

        # 开发用：FIREFLY_TEST_EMOTION=开心 时，启动 12 秒后广播一次情绪，
        # 用来在没法聊天（比如账户欠费）时验证 3D 表情是否生效
        try:
            import os as _os

            _emo = _os.environ.get("FIREFLY_TEST_EMOTION")
            if _emo:
                from PyQt6.QtCore import QTimer as _QTimer

                _QTimer.singleShot(
                    12000, lambda: ui_bus.broadcast("pet_motion", {"name": _emo})
                )
                print(f"[pet] 12 秒后注入测试情绪: {_emo}", flush=True)

            # FIREFLY_TEST_CHAT=1：注入几条假消息，检查聊天区排版
            if _os.environ.get("FIREFLY_TEST_CHAT") == "1":
                from PyQt6.QtCore import QTimer as _QTimer

                def _fake_chat():
                    msgs = [
                        "你喜欢干什么",
                        "喜欢陪开拓者聊天，还在桌面上蹦来蹦去～",
                        "你现在能做些什么",
                        "开着呢，我能陪你聊天、提醒你喝水休息，还能帮你打开 QQ、搜索东西，都可以试试看。",
                    ]
                    for i, m in enumerate(msgs):
                        who = "me" if i % 2 == 0 else "bot"
                        ui_bus.broadcast(
                            "user_text" if who == "me" else "llm_reply",
                            {"text": m, "answer": m, "emotion": "开心", "skip_motion": True},
                        )

                _QTimer.singleShot(9000, _fake_chat)
                print("[pet] 9 秒后注入测试消息", flush=True)

            # FIREFLY_TEST_STAT=1：10 秒后强制显示余额浮层，方便截图核对样式
            if _os.environ.get("FIREFLY_TEST_STAT") == "1":
                from PyQt6.QtCore import QTimer as _QTimer

                _QTimer.singleShot(10000, lambda: win.send_to_js("show_stat", {}))
                print("[pet] 10 秒后显示余额浮层（测试）", flush=True)

            # FIREFLY_SHOT_PET=1：14 秒后截桌宠窗口本身（浮层在这里）
            if _os.environ.get("FIREFLY_SHOT_PET") == "1":

                def _pet_shot():
                    try:
                        from firefly_app.paths import LOGS_DIR as _L

                        win.grab().save(str(_L / "pet-shot.png"), "PNG")
                        print("[shot] 桌宠截图已保存", flush=True)
                    except Exception as e:
                        print(f"[shot] 桌宠截图失败: {e}", flush=True)

                _QTimer.singleShot(14000, _pet_shot)
        except Exception as e:
            print(f"[pet] 测试注入失败: {e}", flush=True)

        sys.exit(app.exec())
    except Exception as e:
        _log_line(f"[pet] fatal: {e}")
        _log_line(traceback.format_exc())
        raise


if __name__ == "__main__":
    main()

