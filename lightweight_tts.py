#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
轻量级TTS服务 - 不依赖大型AI模型
"""

import sys
import http.server
import socketserver
import json
import threading
import queue
import time
import win32com.client
import pythoncom
import subprocess
import unicodedata

# 兼容中文 Windows 控制台（默认可能是 GBK，打印 emoji 会炸）
try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

class LightweightTTS:
    def __init__(self):
        self.queue = queue.Queue()
        self.running = True
        self._speaker_ready = False
        self.thread = threading.Thread(target=self._process_queue)
        self.thread.daemon = True
        self.thread.start()
        # 注意：COM 初始化必须在线程内做；否则在后台线程 Speak 可能无声/异常
    
    def _setup_voice(self):
        """设置最好的中文声音"""
        try:
            # 每个线程都需要自己的 CoInitialize
            pythoncom.CoInitialize()
            self.speaker = win32com.client.Dispatch("SAPI.SpVoice")
            
            # 获取所有可用声音
            voices = self.speaker.GetVoices()
            print("可用声音列表:")
            best_voice = None
            
            for i in range(voices.Count):
                voice = voices.Item(i)
                desc = voice.GetDescription()
                print(f"  {i}: {desc}")
                
                # 优先选择中文女声
                desc_lower = desc.lower()
                if any(keyword in desc_lower for keyword in ["chinese", "中文", "xiaoxiao", "yunxi", "huihui"]):
                    if "female" in desc_lower or "女" in desc:
                        best_voice = voice
                        print(f"选择最佳声音: {desc}")
                        break
                    elif not best_voice:
                        best_voice = voice
            
            if best_voice:
                self.speaker.Voice = best_voice
            else:
                print("未找到中文声音，使用默认声音")
            self._speaker_ready = True
                
        except Exception as e:
            print(f"语音初始化失败: {e}")
            self.speaker = None
            self._speaker_ready = False
    
    def _process_queue(self):
        """处理TTS队列"""
        # 在播放线程里初始化 COM + Voice
        try:
            self._setup_voice()
        except Exception:
            pass
        while self.running:
            try:
                text = self.queue.get(timeout=1)
                if text:
                    if not self._speaker_ready or not self.speaker:
                        # 尝试重新初始化一次
                        self._setup_voice()
                    if self.speaker:
                        print(f"播放: {text}")
                        self.speaker.Speak(text)
                        print("播放完成")
                self.queue.task_done()
            except queue.Empty:
                continue
            except Exception as e:
                print(f"播放错误: {e}")
    
    def add_text(self, text):
        """添加文本到播放队列"""
        self.queue.put(text)

# 全局TTS实例
tts = LightweightTTS()


def speak(text: str):
    """
    轻量兜底：直接用 Windows SAPI 播放（当前线程同步）。
    说明：原本的队列线程在部分环境下会遇到 COM 初始化/线程上下文问题，
    导致“入队了但没播”。这里改为每次调用直接播放，保证必有声音。
    """
    try:
        t = (text or "").strip()
        if not t:
            return
        # 清洗：避免念出“波浪号/表情”等装饰符
        for ch in ("~", "～", "〜", "﹋", "﹌", "∼", "˜", "˷", "≈"):
            t = t.replace(ch, " ")
        # 去掉大部分 emoji/符号（保留中英文与常见标点）
        cleaned = []
        for c in t:
            o = ord(c)
            if 0xD800 <= o <= 0xDFFF:
                continue
            cat = unicodedata.category(c)
            if cat in ("So", "Sk", "Cs"):
                continue
            cleaned.append(c)
        t = " ".join("".join(cleaned).split()).strip()
        # 1) 优先走 SAPI.SpVoice（最快）
        try:
            pythoncom.CoInitialize()
            sp = win32com.client.Dispatch("SAPI.SpVoice")
            sp.Speak(t)
            return
        except Exception as e:
            # 2) 兜底：走 PowerShell System.Speech（更稳）
            ps = (
                "Add-Type -AssemblyName System.Speech; "
                "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                "$s.Speak([string]$args[0]);"
            )
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
                    "-ExecutionPolicy",
                    "Bypass",
                    "-WindowStyle",
                    "Hidden",
                    "-Command",
                    ps,
                    t,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creationflags,
                startupinfo=startupinfo,
            )
    except Exception as e:
        print(f"轻量TTS speak失败: {e}")

class TTSHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/':
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(b'{"message":"Lightweight TTS is running","status":"ok","voice":"windows_chinese"}')
        else:
            self.send_error(404)
    
    def do_POST(self):
        if self.path == '/tts':
            content_length = int(self.headers['Content-Length'])
            post_data = self.rfile.read(content_length)
            
            try:
                data = json.loads(post_data.decode('utf-8'))
                text = data.get('text', '')
                
                if text:
                    print(f'📝 收到TTS请求: {text}')
                    tts.add_text(text)
                    
                    response = {"message": f"TTS queued: {text}", "status": "success", "voice": "lightweight"}
                else:
                    response = {"error": "No text provided"}
                
                self.send_response(200)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps(response).encode('utf-8'))
                
            except Exception as e:
                self.send_response(500)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode('utf-8'))
        else:
            self.send_error(404)
    
    def log_message(self, format, *args):
        pass

if __name__ == '__main__':
    PORT = 9880
    
    print(f"轻量级TTS服务启动在端口 {PORT}")
    print("不依赖大型AI模型，节省磁盘空间")
    print("使用Windows内置中文TTS")
    print("自动选择最好的中文声音")
    
    try:
        with socketserver.TCPServer(("", PORT), TTSHandler) as httpd:
            httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nTTS服务已停止")
        tts.running = False
