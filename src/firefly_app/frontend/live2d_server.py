# -*- coding: utf-8 -*-
"""
Live2D HTTP 服务器
用于提供 Live2D 模型和库文件
"""

import http.server
import socketserver
import threading
from pathlib import Path


class Live2DServer:
    def __init__(self, root_dir, port=8765):
        self.root_dir = Path(root_dir)
        self.port = port
        self.server = None
        self.thread = None
    
    def start(self):
        """启动服务器"""
        handler = http.server.SimpleHTTPRequestHandler
        
        class CustomHandler(handler):
            def __init__(self, *args, **kwargs):
                # 注意：这里的 self 是 Handler 实例，不是 Live2DServer
                super().__init__(*args, directory=str(CustomHandler.root_dir), **kwargs)
            
            def log_message(self, format, *args):
                pass  # 禁用日志输出
        
        CustomHandler.root_dir = self.root_dir

        class ReusableTCPServer(socketserver.TCPServer):
            allow_reuse_address = True
        
        # 端口可能被占用（例如重复启动桌宠），这里自动向后找可用端口
        port = int(self.port)
        while True:
            try:
                self.server = ReusableTCPServer(("127.0.0.1", port), CustomHandler)
                self.port = port
                break
            except OSError:
                port += 1

        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

        # Windows 控制台默认可能是 GBK，打印 emoji 容易直接崩溃
        print(f"Live2D 服务器已启动: http://127.0.0.1:{self.port}")
    
    def stop(self):
        """停止服务器"""
        if self.server:
            self.server.shutdown()
            print("Live2D 服务器已停止")
    
    def get_url(self, path=""):
        """获取 URL"""
        return f"http://127.0.0.1:{self.port}/{path}"
