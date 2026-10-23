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
        try:
            from firefly_app.backend.voice import set_tts_callbacks
            set_tts_callbacks(
                on_start=lambda: self.send_to_js("tts_start", {}),
                on_end=lambda: self.send_to_js("tts_end", {}),
            )
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
        """打开可视化设置窗口（懒加载，只创建一次）"""
        try:
            if getattr(self, "_settings_win", None) is None:
                from firefly_app.frontend.settings_window import SettingsWindow

                self._settings_win = SettingsWindow(self.server, chat_bridge=self.bridge)
            self._settings_win.show()
            self._settings_win.raise_()
            self._settings_win.activateWindow()
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


class Bridge(QObject):
    def __init__(self, win: PetWindow):
        super().__init__()
        self.win = win
        self._seq = 0          # 每问一次 +1，用来丢掉上一轮的过期流
        self._ptt_active = False   # 热键是否正处于"按住录音"状态
        self._ptt_fail_at = 0.0    # 上次启动失败的时间（冷却用）

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
        """点 🎤：录一句 → 识别 → 直接提问（全程麦模式）"""
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

        try:
            started = False
            for piece in llm.chat_stream(q, extra_system=mem_block):
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

        # 全局窗口/任务栏图标（托盘图标另有 find_tray_icon）
        try:
            _icon = find_tray_icon()
            if not _icon.isNull():
                app.setWindowIcon(_icon)
        except Exception:
            pass

        win = PetWindow()
        win.show()

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

        sys.exit(app.exec())
    except Exception as e:
        _log_line(f"[pet] fatal: {e}")
        _log_line(traceback.format_exc())
        raise


if __name__ == "__main__":
    main()

