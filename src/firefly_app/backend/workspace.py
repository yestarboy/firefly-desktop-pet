# -*- coding: utf-8 -*-
"""
工作区：让 AI 帮你读代码、改文件、跑命令。

设计原则：
1. **只在配置的根目录内操作**（默认仓库根目录），路径一律做越界检查；
2. 命令执行有黑名单 + 超时，避免误伤系统；
3. AI 通过 OpenAI 的 tool calling 决定要做什么，每一步都记录，
   前端能看到它到底调用了什么。
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

import requests

from firefly_app.backend import llm
from firefly_app.paths import ROOT

DEFAULT_ROOT = ROOT

# 危险命令：直接拒绝（大小写不敏感）
BLOCKED = (
    "format ",
    "diskpart",
    "mkfs",
    "shutdown",
    "reg delete",
    "rm -rf /",
    "del /f /s /q c:",
    "rd /s /q c:",
    "cipher /w",
    "bcdedit",
)

MAX_FILE_BYTES = 400_000        # 单个文件读取上限（约 400KB）
MAX_TREE_ITEMS = 400            # 目录列表上限
CMD_TIMEOUT = 120               # 命令超时（秒）


def _root() -> Path:
    try:
        cfg = llm.load_config()
        d = cfg.get("workspace_dir")
        if isinstance(d, str) and d.strip():
            p = Path(d).expanduser()
            if p.exists() and p.is_dir():
                return p.resolve()
    except Exception:
        pass
    return DEFAULT_ROOT.resolve()


def safe_path(rel: str) -> Path:
    """把相对路径解析成绝对路径，并确保没跑出根目录"""
    root = _root()
    rel = (rel or "").replace("\\", "/").strip().lstrip("/")
    p = (root / rel).resolve()
    if root != p and root not in p.parents:
        raise ValueError("路径越界：只能操作工作区内的文件")
    return p


def _rel(p: Path) -> str:
    try:
        return p.relative_to(_root()).as_posix()
    except Exception:
        return p.as_posix()


# ---------------------------------------------------------------- 文件操作


def list_dir(rel: str = "") -> Dict:
    try:
        p = safe_path(rel)
        if not p.exists():
            return {"ok": False, "error": "目录不存在"}
        if not p.is_dir():
            return {"ok": False, "error": "不是目录"}

        items: List[Dict] = []
        for child in sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
            if len(items) >= MAX_TREE_ITEMS:
                break
            try:
                size = child.stat().st_size if child.is_file() else 0
            except Exception:
                size = 0
            items.append(
                {
                    "name": child.name,
                    "path": _rel(child),
                    "dir": child.is_dir(),
                    "size": size,
                }
            )
        return {"ok": True, "root": _root().as_posix(), "path": _rel(p), "items": items}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def read_file(rel: str) -> Dict:
    try:
        p = safe_path(rel)
        if not p.exists() or not p.is_file():
            return {"ok": False, "error": "文件不存在"}
        size = p.stat().st_size
        data = p.read_bytes()[:MAX_FILE_BYTES]
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return {
                "ok": False,
                "error": "看起来是二进制文件，无法当文本编辑",
                "size": size,
            }
        return {
            "ok": True,
            "path": _rel(p),
            "content": text,
            "truncated": size > len(data),
            "size": size,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def write_file(rel: str, content: str) -> Dict:
    try:
        p = safe_path(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8", newline="\n")
        return {"ok": True, "path": _rel(p), "size": len(content.encode("utf-8"))}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def run_command(command: str) -> Dict:
    cmd = (command or "").strip()
    if not cmd:
        return {"ok": False, "error": "命令为空"}

    low = cmd.lower()
    for bad in BLOCKED:
        if bad in low:
            return {"ok": False, "error": f"命令被安全规则拦截：{bad.strip()}"}

    t0 = time.time()
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(_root()),
            shell=True,
            capture_output=True,
            timeout=CMD_TIMEOUT,
        )
        out = (proc.stdout or b"").decode("utf-8", errors="replace")
        err = (proc.stderr or b"").decode("utf-8", errors="replace")
        combined = (out + ("\n" + err if err.strip() else "")).strip()
        if len(combined) > 20000:
            combined = combined[:20000] + "\n…（输出过长已截断）"
        return {
            "ok": proc.returncode == 0,
            "code": proc.returncode,
            "output": combined or "(无输出)",
            "seconds": round(time.time() - t0, 2),
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"命令超时（>{CMD_TIMEOUT}s）"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ---------------------------------------------------------------- AI 工具循环

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "列出工作区某个目录下的文件和子目录。不传参数就是根目录。",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "相对路径"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取工作区里的文本文件内容。",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "把内容写入工作区里的文件（会覆盖原文件）。改动代码前先 read_file。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "在工作区根目录执行一条命令并返回输出（超时 120 秒）。",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    },
]

SYSTEM = """你是桌面宠物「萤宝」里的编程助手，当前在一个项目工作区里帮用户干活。

工作区根目录：{root}

规则：
- 用工具读文件、改文件、跑命令，别凭空猜代码内容。
- 改代码前先 read_file 看清楚；改完尽量 run_command 验证（比如构建、跑测试）。
- 路径一律用相对工作区根目录的写法。
- 回复用中文，简洁说明你做了什么、结果如何；不要贴大段代码，说清改了哪里即可。
"""


def _system_prompt() -> str:
    """工作区助手的系统提示：同样以最底层的「爱」为总纲"""
    text = SYSTEM.format(root=_root().as_posix())
    try:
        from firefly_app.backend import soul

        text = soul.creed_block() + "\n\n" + text
    except Exception:
        pass
    return text


def _endpoint() -> str:
    cfg = llm.load_config()
    base = (cfg.get("base_url") or "https://api.deepseek.com").rstrip("/")
    return base + "/chat/completions"


def agent_ask(
    question: str,
    emit: Optional[Callable[[str, str], None]] = None,
    max_steps: int = 8,
) -> Dict:
    """跑一轮工具循环，返回 {ok, answer}

    emit(kind, text) 用来把过程实时推给前端，kind 为 log/reply。
    """

    def _emit(kind: str, text: str):
        if emit:
            try:
                emit(kind, text)
            except Exception:
                pass

    cfg = llm.load_config()
    key = llm._clean_key(cfg.get("api_key"))
    if not key:
        return {"ok": False, "error": "还没配置 API Key"}

    messages: List[dict] = [
        {"role": "system", "content": _system_prompt()},
        {"role": "user", "content": question},
    ]
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    for step in range(max_steps):
        payload = {
            "model": cfg.get("model") or "deepseek-chat",
            "messages": messages,
            "tools": TOOLS,
            "tool_choice": "auto",
            "temperature": 0.3,
            "max_tokens": 2048,
        }
        try:
            r = requests.post(
                _endpoint(),
                headers=headers,
                json=payload,
                timeout=(10, 120),
            )
        except Exception as e:
            return {"ok": False, "error": f"请求失败：{e}"}

        if r.status_code != 200:
            return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:200]}"}

        msg = (r.json().get("choices") or [{}])[0].get("message") or {}
        calls = msg.get("tool_calls") or []

        if not calls:
            answer = (msg.get("content") or "").strip() or "(没有返回内容)"
            return {"ok": True, "answer": answer, "steps": step}

        messages.append(
            {"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls}
        )

        for call in calls:
            fn = call.get("function") or {}
            name = fn.get("name") or ""
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except Exception:
                args = {}

            _emit("log", f"调用 {name}({json.dumps(args, ensure_ascii=False)[:120]})")

            if name == "list_dir":
                result = list_dir(args.get("path", ""))
                summary = f"{len(result.get('items', []))} 项" if result.get("ok") else result.get("error", "")
            elif name == "read_file":
                result = read_file(args.get("path", ""))
                summary = f"{result.get('size', 0)} 字节" if result.get("ok") else result.get("error", "")
            elif name == "write_file":
                result = write_file(args.get("path", ""), args.get("content", ""))
                summary = result.get("path", "") if result.get("ok") else result.get("error", "")
            elif name == "run_command":
                result = run_command(args.get("command", ""))
                summary = f"exit={result.get('code')} ({result.get('seconds')}s)" if "code" in result else result.get("error", "")
            else:
                result = {"ok": False, "error": f"未知工具 {name}"}
                summary = "未知工具"

            _emit("log", f"  → {summary}")

            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.get("id") or name,
                    "content": json.dumps(result, ensure_ascii=False)[:12000],
                }
            )

    return {"ok": False, "error": f"超过 {max_steps} 步仍未完成，先停下来了"}
