# -*- coding: utf-8 -*-
"""
Firefly 语音识别服务（独立进程，端口 9881）

为什么单独跑一个进程：
- 识别要 funasr + torch + sounddevice，这些只装在 GPT-SoVITS 的 runtime 里
- 桌宠跑在另一个 Python（3.13 / PyQt6），不该为了识别再装一整套
- 跟 TTS 的 9880 一样用 HTTP 通信，互不影响、可单独重启

必须用 runtime 的 Python 启动：
    .\\runtime\\python.exe tools\\stt_server.py

接口：
    GET  /health                    状态
    GET  /devices                   录音设备列表
    POST /stt                       body 为 wav 字节 → 文字
    POST /listen[?mic=N]            录音（VAD 自动断句）→ 文字
    POST /ptt/start[?mic=N]         开始按住录音
    POST /ptt/stop                  结束录音 → 文字
"""

import io
import json
import os
import re
import sys
import threading
import time
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np
import soundfile as sf

PORT = int(os.environ.get("STT_PORT", "9881"))
SAMPLE_RATE = 16000
MIC_INDEX = os.environ.get("STT_MIC")
DEVICE = os.environ.get("STT_DEVICE", "cpu")

PTT_MAX_SEC = 30.0

MODEL_CANDIDATES = [
    "iic/speech_seaco_paraformer_large_asr_nat-zh-cn-16k-common-vocab8404-pytorch",
    "iic/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-pytorch",
    "paraformer-zh",
]

WORKSPACE = Path(__file__).resolve().parents[1]
CONFIG_PATH = WORKSPACE / "firefly_config.json"

DEFAULT_FIXES = {
    "刘莹": "流萤", "刘颖": "流萤", "流瑩": "流萤", "流莹": "流萤",
    "流影": "流萤", "刘银": "流萤", "流英": "流萤", "刘英": "流萤",
}
DEFAULT_HOTWORDS = "流萤 星穹铁道"

_model = None
_model_name = ""
_model_err = ""
_loaded = threading.Event()

# ---- 占位状态：任何一路录音都要先 claim，再在 finally 里 release ----
_busy_lock = threading.Lock()
_busy = {"who": None, "since": 0.0}
STALE_AFTER = 8.0        # 占位超过这么久且没在录音，视为僵死，自动清理

# 按住说话会话
_ptt = {"active": False, "buf": [], "stream": None, "t0": 0.0}


def log(msg):
    print(f"[stt] {msg}", flush=True)


# ---------------------------------------------------------------- 占位管理

def _claim(who):
    """成功返回 (True, '', 0)；失败返回 (False, 占用者, 已占用秒数)"""
    with _busy_lock:
        now = time.time()
        cur = _busy["who"]
        if cur:
            age = now - _busy["since"]
            # PTT 录音期间是正常占用，不按僵死处理
            if _ptt["active"] or age < STALE_AFTER:
                return False, cur, age
            log(f"清理僵死占用: {cur}（已 {age:.1f}s）")
        _busy["who"] = who
        _busy["since"] = now
        return True, "", 0.0


def _release(who):
    with _busy_lock:
        if _busy["who"] == who:
            _busy["who"] = None
            _busy["since"] = 0.0


# ---------------------------------------------------------------- 配置

def load_stt_options():
    hot, fixes = DEFAULT_HOTWORDS, dict(DEFAULT_FIXES)
    try:
        if CONFIG_PATH.exists():
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
            h = cfg.get("stt_hotwords")
            if isinstance(h, str) and h.strip():
                hot = h.strip()
            f = cfg.get("stt_fixes")
            if isinstance(f, dict) and f:
                fixes = {str(k): str(v) for k, v in f.items()}
    except Exception:
        pass
    return hot, fixes


# 汉字 + 中文标点（全角区）之间都不要留空格
_CJK_CLASS = r"\u4e00-\u9fff\u3000-\u303f\uff00-\uffef"
_CJK_GAP = re.compile(rf"(?<=[{_CJK_CLASS}])\s+(?=[{_CJK_CLASS}])")


def apply_fixes(text, fixes):
    if not text:
        return text
    text = str(text)
    text = _CJK_GAP.sub("", text)
    text = re.sub(r"\s{2,}", " ", text).strip()
    for wrong, right in fixes.items():
        if wrong and wrong in text:
            text = text.replace(wrong, right)
    return text


# ---------------------------------------------------------------- 模型

def load_model():
    global _model, _model_err, _model_name
    from funasr import AutoModel

    for name in MODEL_CANDIDATES:
        try:
            log(f"加载模型: {name} (device={DEVICE})")
            t0 = time.time()
            _model = AutoModel(model=name, device=DEVICE)
            _model_name = name.split("/")[-1]
            log(f"模型就绪，用时 {time.time() - t0:.1f}s")
            _loaded.set()
            return
        except Exception as e:
            _model_err = f"{name}: {e}"
            log(f"加载失败 -> {e}")
    log("所有候选模型都失败了")
    _loaded.set()


def transcribe(audio):
    if _model is None or audio is None or len(audio) == 0:
        return ""
    if float(np.sqrt(np.mean(audio ** 2))) < 1e-4:
        return ""

    hot, fixes = load_stt_options()
    res = None
    if hot:
        try:
            res = _model.generate(
                input=audio, fs=SAMPLE_RATE, batch_size_s=300, hotword=hot
            )
        except Exception as e:
            log(f"热词识别失败，退回普通模式: {e}")
    if res is None:
        res = _model.generate(input=audio, fs=SAMPLE_RATE, batch_size_s=300)

    if not res:
        return ""
    item = res[0]
    text = item.get("text", "") if isinstance(item, dict) else str(item)
    return apply_fixes(str(text).strip(), fixes)


def to_16k(audio, sr):
    if sr == SAMPLE_RATE:
        return audio.astype(np.float32)
    n = int(len(audio) * SAMPLE_RATE / float(sr))
    if n <= 0:
        return np.zeros(0, dtype=np.float32)
    idx = np.linspace(0, len(audio) - 1, n)
    return np.interp(idx, np.arange(len(audio)), audio).astype(np.float32)


# ---------------------------------------------------------------- 录音

def _mic_kwargs(mic=None):
    kw = dict(samplerate=SAMPLE_RATE, channels=1, dtype="float32", blocksize=320)
    dev = mic if mic not in (None, "") else MIC_INDEX
    if dev not in (None, ""):
        kw["device"] = int(dev)
    return kw


def record_utterance(mic=None, max_sec=15, silence_ms=700, start_timeout=6.0):
    """录一句（VAD 自动断句）。用 callback + 队列，不会卡死。"""
    import queue as _queue

    import sounddevice as sd

    block = 320
    silence_need = silence_ms / 1000.0
    q = _queue.Queue()

    def _cb(indata, frames, time_info, status):
        try:
            q.put(indata.copy())
        except Exception:
            pass

    kw = _mic_kwargs(mic)
    kw["callback"] = _cb

    with sd.InputStream(**kw):
        ambient = []
        t_amb = time.time()
        while len(ambient) < 15 and time.time() - t_amb < 2.0:
            try:
                d = q.get(timeout=0.5)
            except _queue.Empty:
                break
            ambient.append(float(np.sqrt(np.mean(d ** 2))))

        if not ambient:
            raise RuntimeError(
                "麦克风没有返回数据：设备可能被其它程序独占，"
                "或系统设置里禁止了应用访问麦克风"
            )

        noise = max(0.004, float(np.mean(ambient)))
        thresh = max(0.012, noise * 3.0)
        log(f"设备={kw.get('device', '默认')} 噪声={noise:.4f} 阈值={thresh:.4f}")

        buf, state, silence = [], "wait", 0.0
        t0 = time.time()
        last_data = time.time()

        while True:
            try:
                d = q.get(timeout=0.5)
                last_data = time.time()
            except _queue.Empty:
                if time.time() - last_data > 1.5:
                    raise RuntimeError("麦克风数据中断（设备可能被拔出或被占用）")
                continue

            rms = float(np.sqrt(np.mean(d ** 2)))

            if state == "wait":
                if rms > thresh:
                    state = "speech"
                    buf.append(d)
                    silence = 0.0
                elif time.time() - t0 > start_timeout:
                    return None
            else:
                buf.append(d)
                if rms > thresh:
                    silence = 0.0
                else:
                    silence += block / float(SAMPLE_RATE)
                    if silence >= silence_need:
                        break
                if len(buf) * block / SAMPLE_RATE >= max_sec:
                    break

    if not buf:
        return None
    return np.concatenate(buf, axis=0).reshape(-1).astype(np.float32)


# ---------------------------------------------------------------- 按住说话

def ptt_start(mic=None):
    import sounddevice as sd

    if _ptt["active"]:
        return False, "已经在录音"

    ok, who, age = _claim("ptt")
    if not ok:
        return False, f"{who} 正在占用（{age:.1f}s）"

    buf = []
    t0 = time.time()
    kw = _mic_kwargs(mic)

    def _cb(indata, frames, time_info, status):
        if time.time() - t0 <= PTT_MAX_SEC:
            buf.append(indata.copy())

    kw["callback"] = _cb

    try:
        stream = sd.InputStream(**kw)
        stream.start()
    except Exception as e:
        _release("ptt")
        log(f"打开麦克风失败: {e}")
        return False, f"打开麦克风失败：{e}"

    _ptt.update(active=True, buf=buf, stream=stream, t0=t0)
    log(f"PTT 开始录制 设备={kw.get('device', '默认')}")
    return True, "ok"


def ptt_stop():
    """结束录音并识别。无论中间发生什么，状态一定会被复位。"""
    if not _ptt["active"]:
        _release("ptt")          # 兜底：万一锁没释放
        return 400, {"error": "当前没有在录音"}

    stream = _ptt["stream"]
    buf = _ptt["buf"]
    dur = time.time() - _ptt["t0"]

    text, err = "", None
    try:
        if stream is not None:
            try:
                stream.stop()
            except Exception as e:
                log(f"stream.stop 失败(忽略): {e}")
            try:
                stream.close()
            except Exception as e:
                log(f"stream.close 失败(忽略): {e}")

        if buf:
            audio = np.concatenate(buf, axis=0).reshape(-1).astype(np.float32)
            audio = audio[-int(PTT_MAX_SEC * SAMPLE_RATE):]
            if len(audio) >= SAMPLE_RATE * 0.2:
                t1 = time.time()
                text = transcribe(audio)
                log(f"PTT 结束 时长={dur:.1f}s 识别={text!r} 耗时={time.time() - t1:.2f}s")
    except Exception as e:
        err = str(e)
        log(f"ptt_stop 异常: {e}")
        log(traceback.format_exc())
    finally:
        _ptt.update(active=False, buf=[], stream=None, t0=0.0)
        _release("ptt")

    if err:
        return 500, {"error": err}
    if not text:
        return 200, {"text": "", "reason": "没录到声音", "seconds": round(dur, 1)}
    return 200, {"text": text, "seconds": round(dur, 1)}


# ---------------------------------------------------------------- HTTP

def _query(path):
    return urllib.parse.parse_qs(urllib.parse.urlparse(path).query)


class Handler(BaseHTTPRequestHandler):
    # HTTP/1.0：每个请求一个连接，响应后即关，避免 keep-alive 的半开连接问题
    protocol_version = "HTTP/1.0"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, obj):
        try:
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            log(f"回包失败: {e}")

    def do_GET(self):
        try:
            if self.path.startswith("/health"):
                with _busy_lock:
                    who = _busy["who"]
                    since = _busy["since"]
                return self._send(200, {
                    "ok": True,
                    "loaded": _model is not None,
                    "model": _model_name,
                    "model_error": _model_err,
                    "device": DEVICE,
                    "busy": who,
                    "busy_seconds": round(time.time() - since, 1) if who else 0,
                    "ptt": _ptt["active"],
                })

            if self.path.startswith("/devices"):
                import sounddevice as sd

                try:
                    default_in = sd.default.device[0]
                except Exception:
                    default_in = -1

                devs = []
                for i, d in enumerate(sd.query_devices()):
                    try:
                        if int(d.get("max_input_channels", 0)) <= 0:
                            continue
                        devs.append({
                            "index": i,
                            "name": d.get("name", f"device {i}"),
                            "channels": int(d.get("max_input_channels", 0)),
                            "default": (i == default_in),
                        })
                    except Exception:
                        continue
                return self._send(200, {"devices": devs, "default_input": default_in})

            return self._send(404, {"error": "not found"})

        except Exception as e:
            log(f"GET {self.path} 异常: {e}")
            return self._send(500, {"error": str(e), "trace": traceback.format_exc()[-600:]})

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            qs = _query(self.path)
            mic = qs.get("mic", [None])[0]

            # ---------------- 纯音频转文字 ----------------
            if self.path.startswith("/stt"):
                if not _loaded.wait(120) or _model is None:
                    return self._send(503, {"error": "模型未加载", "detail": _model_err})
                audio, sr = sf.read(io.BytesIO(body), dtype="float32", always_2d=False)
                if getattr(audio, "ndim", 1) > 1:
                    audio = audio.mean(axis=1)
                audio = to_16k(audio, sr)
                t0 = time.time()
                text = transcribe(audio)
                return self._send(200, {"text": text, "ms": int((time.time() - t0) * 1000)})

            # ---------------- 按住说话 ----------------
            if self.path.startswith("/ptt/start"):
                if not _loaded.wait(120) or _model is None:
                    return self._send(503, {"error": "模型未加载", "detail": _model_err})
                ok, msg = ptt_start(mic)
                return self._send(200 if ok else 409, {"ok": ok, "message": msg})

            if self.path.startswith("/ptt/stop"):
                code, payload = ptt_stop()
                return self._send(code, payload)

            # ---------------- 录一句（VAD） ----------------
            if self.path.startswith("/listen"):
                if not _loaded.wait(120) or _model is None:
                    return self._send(503, {"error": "模型未加载", "detail": _model_err})

                ok, who, age = _claim("listen")
                if not ok:
                    return self._send(409, {
                        "error": f"{who} 正在占用（{age:.1f} 秒），请稍候",
                    })

                try:
                    t0 = time.time()
                    audio = record_utterance(mic=mic)
                    if audio is None or len(audio) < SAMPLE_RATE * 0.2:
                        return self._send(200, {
                            "text": "", "reason": "没听到声音",
                            "ms": int((time.time() - t0) * 1000),
                        })
                    text = transcribe(audio)
                    return self._send(200, {
                        "text": text, "ms": int((time.time() - t0) * 1000),
                    })
                finally:
                    _release("listen")

            return self._send(404, {"error": "not found"})

        except Exception as e:
            log(f"POST {self.path} 异常: {e}")
            log(traceback.format_exc())
            return self._send(500, {"error": str(e), "trace": traceback.format_exc()[-600:]})


def main():
    log(f"端口 {PORT}，设备 {DEVICE}，麦克风 {MIC_INDEX or '默认'}")
    threading.Thread(target=load_model, daemon=True).start()

    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    log(f"服务已启动: http://127.0.0.1:{PORT}/health")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log("退出")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()
