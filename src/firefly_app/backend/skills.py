# -*- coding: utf-8 -*-
"""
本地指令层：不经过大模型，直接干活的"小技能"。

为什么要有这一层：
- "打开QQ"这类话不需要 AI 推理，走模型既慢又可能瞎编；
- 更重要的是**安全**：绝不能把模型输出直接丢给 shell。
  这里只认白名单里的应用，其余一律不动手。

对外只暴露一个入口：
    try_handle(text) -> dict | None
        {"text": "要说的回复", "emotion": "开心", "action": "open:qq"}
    返回 None 表示不归本层管，交回 LLM。
"""

import ctypes
import os
import re
import shutil
import subprocess
import time
import webbrowser
from pathlib import Path

# ---------------------------------------------------------------- 系统按键

VK_MEDIA = {
    "播放": 0xB3, "暂停": 0xB3, "继续": 0xB3,
    "下一首": 0xB0, "上一首": 0xB1,
    "音量加": 0xAF, "音量减": 0xAE, "静音": 0xAD,
}
KEYEVENTF_KEYUP = 0x0002


def _press(vk: int, times: int = 1) -> None:
    """模拟按一次媒体/音量键（系统级，不影响其它窗口）"""
    for _ in range(max(1, times)):
        ctypes.windll.user32.keybd_event(vk, 0, 0, 0)
        ctypes.windll.user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
        time.sleep(0.05)


def _lock_screen() -> bool:
    return bool(ctypes.windll.user32.LockWorkStation())


def _screenshot() -> str:
    """用 Qt 截全屏，存到 图片\\Firefly_时间.png"""
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    screen = app.primaryScreen()
    pix = screen.grabWindow(0)

    out_dir = Path(os.path.expanduser("~")) / "Pictures"
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        out_dir = Path(os.path.expanduser("~"))
    path = out_dir / ("Firefly_%s.png" % time.strftime("%Y%m%d_%H%M%S"))
    pix.save(str(path), "PNG")
    return str(path)


def _open_folder(name: str) -> bool:
    """只允许打开盘符根目录或用户目录，防止拼出任意路径"""
    n = name.strip().upper().rstrip("盘").rstrip(":")
    drives = [d + ":\\" for d in "CDEFGH" if Path(d + ":\\").exists()]

    if len(n) == 1 and n in "CDEFGH":
        target = n + ":\\"
        if target in drives:
            os.startfile(target)
            return True
        return False

    for word, sub in (("下载", "Downloads"), ("文档", "Documents"), ("桌面", "Desktop"), ("图片", "Pictures")):
        if word in name:
            p = Path(os.path.expanduser("~")) / sub
            if p.exists():
                os.startfile(str(p))
                return True
    return False

# ---------------------------------------------------------------- 白名单

# 只允许这些键。值是"可能的可执行文件路径"，按顺序尝试。
APPS = {
    "qq": [
        r"D:\Program Files\Tencent\QQNT\QQ.exe",
        r"C:\Program Files\Tencent\QQNT\QQ.exe",
        r"C:\Program Files (x86)\Tencent\QQNT\QQ.exe",
        r"C:\Program Files (x86)\Tencent\QQ\Bin\QQScLauncher.exe",
        r"C:\Program Files\Tencent\QQ\Bin\QQScLauncher.exe",
    ],
    "微信": [
        r"D:\Program Files\Tencent\Weixin\Weixin.exe",
        r"C:\Program Files\Tencent\Weixin\Weixin.exe",
        r"C:\Program Files\Tencent\WeChat\WeChat.exe",
        r"C:\Program Files (x86)\Tencent\WeChat\WeChat.exe",
    ],
    "浏览器": [],
    "chrome": [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ],
    "edge": [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ],
    "记事本": [r"C:\Windows\System32\notepad.exe"],
    "计算器": [r"C:\Windows\System32\calc.exe"],
    "画图": [r"C:\Windows\System32\mspaint.exe"],
    "steam": [
        r"C:\Program Files (x86)\Steam\steam.exe",
        r"D:\Steam\steam.exe",
        r"F:\Steam\steam.exe",
        r"F:\steam\steam.exe",
    ],
    "vscode": [
        r"C:\Users\%s\AppData\Local\Programs\Microsoft VS Code\Code.exe" % os.environ.get("USERNAME", ""),
        r"C:\Program Files\Microsoft VS Code\Code.exe",
    ],
    "网易云音乐": [
        r"C:\Program Files (x86)\Netease\CloudMusic\cloudmusic.exe",
        r"D:\Program Files\Netease\CloudMusic\cloudmusic.exe",
    ],
    "哔哩哔哩": [
        r"C:\Program Files\bilibili\哔哩哔哩\哔哩哔哩.exe",
        r"D:\Program Files\bilibili\哔哩哔哩\哔哩哔哩.exe",
    ],
    "资源管理器": [r"C:\Windows\explorer.exe"],
    "文件管理器": [r"C:\Windows\explorer.exe"],
    "设置": [],
    "cmd": [r"C:\Windows\System32\cmd.exe"],
}

# 别名 → 白名单 key
ALIASES = {
    "扣扣": "qq",
    "腾讯qq": "qq",
    "vx": "微信",
    "wechat": "微信",
    "谷歌浏览器": "chrome",
    "谷歌": "chrome",
    "微软浏览器": "edge",
    "浏览器": "浏览器",
    "我的电脑": "资源管理器",
    "此电脑": "资源管理器",
    "文件夹": "资源管理器",
    "计算器": "计算器",
    "记事本": "记事本",
}

_OPEN_ANY = re.compile(r"(?:打开|启动|运行|开一下|开)\s*([^\s，。！？,.!?]{1,14})")
_SEARCH_ANY = re.compile(r"(?:搜索|搜一下|搜|查一下|查)\s*([^\s，。！？,.!?]{1,20})")
_TIME_RE = re.compile(r"(几点|什么时间|现在时间|今天几号|几号了|星期几)")
_COMPOUND_RE = re.compile(r"(?:然后|接着|再|并且|顺便)")
# 句首这些语气词/称呼不影响指令
_FILLER_RE = re.compile(
    r"^(?:哦|那|嗯|啊|诶|喂|好|那个|就是|流萤|萤宝|银宝|迎宝|莹宝|赢宝|英宝|应宝|营宝)+"
)


def _normalize(text: str) -> str:
    s = (text or "").strip()
    for _ in range(3):                      # 可能连着好几层前缀
        s2 = _FILLER_RE.sub("", s)
        s2 = re.sub(r"^(?:请|帮我|麻烦你|麻烦|能不能|能|可以|可不可以)\s*", "", s2)
        if s2 == s:
            break
        s = s2
    return s.strip(" ，,。.！!？?、")


def _clean(name: str) -> str:
    s = (name or "").strip()
    s = re.sub(r"(吧|呀|啊|哦|啦|呢|嘛|好吗|可以吗)[。！？!?.\s]*$", "", s)
    s = s.strip("。！？!?.,，、 \t\"'“”")
    return s.lower()


def _find_exe(key: str):
    for cand in APPS.get(key, []):
        p = Path(cand)
        if p.exists():
            return p
    return None


def _launch_key(key: str):
    """启动白名单里的应用；成功返回 True"""
    if key == "浏览器":
        webbrowser.open("https://www.bing.com")
        return True

    exe = _find_exe(key)
    if exe is not None:
        subprocess.Popen([str(exe)], close_fds=True)
        return True

    # 个别应用在 PATH 里（如 code）
    w = shutil.which(key)
    if w:
        subprocess.Popen([w], close_fds=True)
        return True

    return False


def _human_time() -> str:
    now = time.localtime()
    week = "一二三四五六日"[now.tm_wday]
    return time.strftime("%H:%M", now), f"{now.tm_year}年{now.tm_mon}月{now.tm_mday}日 星期{week}"


def _handle_one(raw: str):
    """处理单个分句（不含"然后"这类连接词）"""
    if not raw:
        return None

    # ---- 时间 ----
    if _TIME_RE.search(raw) and len(raw) <= 18:
        hm, date = _human_time()
        return {"text": "现在是 %s，%s。" % (hm, date), "emotion": "平静", "action": "time"}

    # ---- 媒体 / 音量 ----
    for word, vk in VK_MEDIA.items():
        if word in raw:
            times = 3 if re.search(r"(大点|小点|加点|减点|大声|小声)", raw) else 1
            _press(vk, times)
            if word in ("播放", "暂停", "继续"):
                return {"text": "好，%s。" % word, "emotion": "开心", "action": "media:" + word}
            return {"text": "%s，调好了。" % word, "emotion": "开心", "action": "media:" + word}

    # ---- 锁屏 ----
    if "锁屏" in raw and len(raw) <= 12:
        if _lock_screen():
            return {"text": "锁上了，回见。", "emotion": "平静", "action": "lock"}
        return {"text": "锁屏失败，可能被系统拦住了。", "emotion": "难过", "action": "lock-fail"}

    # ---- 截图 ----
    if re.search(r"(截个?图|截屏|截图)", raw) and len(raw) <= 16:
        try:
            path = _screenshot()
            return {
                "text": "截好啦，存在图片文件夹里。",
                "emotion": "开心",
                "action": "screenshot:" + path,
            }
        except Exception as e:
            return {"text": "截图失败了：%s" % e, "emotion": "难过", "action": "screenshot-fail"}

    # ---- 打开盘符 / 常用文件夹 ----
    m = re.search(r"打开\s*([A-Ha-h])\s*盘?", raw) or re.search(r"打开(下载|文档|桌面|图片)", raw)
    if m:
        arg = m.group(1)
        if _open_folder(arg):
            return {
                "text": "打开了 %s。" % arg,
                "emotion": "开心",
                "action": "folder:" + arg,
            }

    # ---- 打开应用 ----
    m = _OPEN_ANY.search(raw)
    if m:
        target = _clean(m.group(1))
        key = ALIASES.get(target, target)

        if key in APPS:
            if _launch_key(key):
                return {
                    "text": "好的，%s 打开啦。" % target,
                    "emotion": "开心",
                    "action": "open:" + key,
                }
            return {
                "text": "我找不到 %s 装在哪里，你先告诉我路径好不好？" % target,
                "emotion": "难过",
                "action": "open-miss:" + key,
            }
        # 不在白名单：不执行，交给 LLM 用嘴解释
        return None

    # ---- 搜索 ----
    m = _SEARCH_ANY.search(raw)
    if m:
        kw = _clean(m.group(1))
        if kw:
            url = "https://www.bing.com/search?q=" + __import__("urllib.parse").parse.quote(kw)
            webbrowser.open(url)
            return {
                "text": "我帮你搜「%s」啦，浏览器见。" % kw,
                "emotion": "开心",
                "action": "search:" + kw,
            }

    # ---- 关机/重启：明确拒绝，避免误触 ----
    if re.search(r"(关机|重启|注销|休眠)", raw) and len(raw) <= 12:
        return {
            "text": "关机这种事还是你自己来比较安全，我可不敢乱按。",
            "emotion": "害羞",
            "action": "refuse:power",
        }

    return None


def try_handle(text: str):
    """命中就返回答复，未命中返回 None

    支持口语前缀（"哦帮我打开QQ"）和复合句（"打开浏览器然后搜索哔哩哔哩"）。
    """
    raw = _normalize(text)
    if not raw:
        return None

    # 复合句：拆开逐段执行，最多 2 段
    parts = [p for p in _COMPOUND_RE.split(raw) if p.strip()]
    if len(parts) > 1:
        hits = []
        for p in parts[:2]:
            r = _handle_one(_normalize(p))
            if r:
                hits.append(r)
        if hits:
            return {
                "text": "".join(h["text"] for h in hits),
                "emotion": hits[0].get("emotion", "开心"),
                "action": "+".join(h["action"] for h in hits),
            }
        return None

    return _handle_one(raw)


# ---------------------------------------------------------------- 自测

if __name__ == "__main__":
    import sys

    tests = ["打开QQ", "帮我打开记事本", "打开浏览器", "搜索 流萤 星穹铁道", "现在几点了", "关机"]
    for t in tests:
        r = try_handle(t)
        print(("%-22s" % t), "->", (r["text"] if r else "(交给 LLM)"))
    sys.exit(0)
