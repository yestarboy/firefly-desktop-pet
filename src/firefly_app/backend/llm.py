# -*- coding: utf-8 -*-
"""
LLM 接入层：本地 Ollama / 线上 OpenAI 兼容 API（DeepSeek 等）

设计目标
- 只依赖 requests，不引入 openai SDK，少装一个包少一分冲突
- 统一成 chat_stream() -> 逐段吐出文字，上层做“边收边显示 / 边收边合成”
- 配置走 firefly_config.json + 环境变量，key 不写死在代码里
- 出问题时抛 LLMError，消息是中文可执行的（key 错了 / 没余额 / 限流）

自测：
    python -m firefly_app.backend.llm "你好，用一句话介绍你自己"
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Iterator, List, Optional

import requests

from firefly_app.paths import ROOT

WORKSPACE_ROOT = ROOT
CONFIG_PATH = WORKSPACE_ROOT / "firefly_config.json"

# 默认配置；firefly_config.json 里的同名字段会覆盖
DEFAULTS = {
    "provider": "deepseek",          # deepseek | ollama
    "api_key": "",
    "base_url": "https://api.deepseek.com",
    "model": "deepseek-chat",
    "temperature": 0.9,
    "max_tokens": 512,
    "timeout": 60,
    "system_prompt": "你叫流萤是桌面宠物助手。说话简短自然、可爱一点，不要长篇大论。",
    # TTS 相关（设置面板可改）
    "tts_ref_wav": "",          # 参考音色文件名；空=用 firefly 目录里第一条
    "tts_speed": 1.0,           # 语速 0.5~2.0
    "tts_seed": 1234,           # 随机种子；-1 = 每次随机（会偶发吞字）
    # 记忆
    "memory_enabled": True,     # 回复前注入长期记忆
    "memory_extract": True,     # 回复后自动提炼新记忆
    # 语音输入
    "stt_mic": "",              # 麦克风设备索引；空=系统默认
    "voice_mode": "always",     # always=全程麦（点一下录一句） / hotkey=热键麦（按住说话）
    "voice_hotkey": "F9",       # 热键名，见 firefly_app/backend/hotkey.py 的 VK_MAP
    # Ollama 兜底（不想用线上时把 provider 改成 ollama）
    "ollama_url": "http://127.0.0.1:11434/api/chat",
    "ollama_model": "gemma3:4b",
}

_CONFIG_CACHE: Optional[dict] = None
_FILE_CFG: dict = {}                 # 配置文件里原样的内容（用于诊断来源）
_CONFLICTS: List[tuple] = []         # [(环境变量名, 字段, 环境变量值)] 文件和环境变量打架时记录


class LLMError(RuntimeError):
    """LLM 调用失败，message 直接可以给用户看"""


# ---------------------------------------------------------------- 配置

def _is_placeholder(value) -> bool:
    """配置文件里的占位符不算“填过”"""
    s = str(value or "").strip()
    if not s:
        return True
    return s.startswith("sk-把") or s.startswith("sk-在这里") or "填你的" in s or "替换成" in s


def load_config(force: bool = False) -> dict:
    """
    优先级：firefly_config.json > 环境变量 > 内置默认值

    为什么不按惯例让环境变量优先：
    上一版就是这么干的，结果“改了文件却一直报 401”，排查了半天。
    桌宠是本机单人用的，文件是看得见摸得着的，理应以文件为准；
    环境变量只在文件没填该项时补位，两者都填但不同则明确报警。
    """
    global _CONFIG_CACHE
    if _CONFIG_CACHE is not None and not force:
        return _CONFIG_CACHE

    cfg = dict(DEFAULTS)
    file_cfg: dict = {}
    _CONFLICTS.clear()

    try:
        if CONFIG_PATH.exists():
            with CONFIG_PATH.open("r", encoding="utf-8-sig") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                file_cfg = loaded
    except Exception as e:
        raise LLMError(f"读取 {CONFIG_PATH.name} 失败：{e}")

    _FILE_CFG.clear()
    _FILE_CFG.update(file_cfg)

    for k, v in file_cfg.items():
        if v is not None and str(v).strip() != "":
            cfg[k] = v

    # 环境变量：文件没填才补位；和文件冲突时以文件为准，并记下来提醒用户
    env_map = {
        "FIREFLY_LLM_PROVIDER": "provider",
        "DEEPSEEK_API_KEY": "api_key",
        "DEEPSEEK_BASE_URL": "base_url",
        "DEEPSEEK_MODEL": "model",
    }
    for env_key, cfg_key in env_map.items():
        v = os.environ.get(env_key)
        if not v:
            continue
        file_filled = cfg_key in file_cfg and not _is_placeholder(file_cfg.get(cfg_key))
        if file_filled and str(cfg.get(cfg_key)) != v:
            _CONFLICTS.append((env_key, cfg_key, v))
        else:
            cfg[cfg_key] = v

    _CONFIG_CACHE = cfg
    return cfg


def config_hint() -> str:
    return (
        f"请把 DeepSeek 的 key 填进 {CONFIG_PATH}（复制 {CONFIG_PATH.name}.example 改名即可），"
        f"或者设置环境变量 DEEPSEEK_API_KEY。"
    )


# ---------------------------------------------------------------- 工具

def _endpoint(base_url: str) -> str:
    b = (base_url or "").strip().rstrip("/")
    if b.endswith("/chat/completions"):
        return b
    return b + "/chat/completions"


def _models_endpoint(base_url: str) -> str:
    b = (base_url or "https://api.deepseek.com").strip().rstrip("/")
    for suffix in ("/chat/completions", "/v1"):
        if b.endswith(suffix):
            b = b[: -len(suffix)]
    return b + "/models"


# 复制粘贴 key 时最容易带进来的“隐形字符”
_KEY_JUNK = " \t\r\n\u00a0\u200b\u200c\u200d\u2060\ufeff\"'“”‘’"


def _clean_key(raw) -> str:
    k = str(raw or "")
    for ch in _KEY_JUNK:
        k = k.replace(ch, "")
    return k


def _mask(k: str) -> str:
    if not k:
        return "(空)"
    if len(k) <= 8:
        return "*" * len(k)
    return f"{k[:3]}...{k[-4:]}"


def _key_source() -> str:
    if _FILE_CFG and not _is_placeholder(_FILE_CFG.get("api_key")):
        return f"{CONFIG_PATH}（文件优先，环境变量不会覆盖）"
    if os.environ.get("DEEPSEEK_API_KEY"):
        return "环境变量 DEEPSEEK_API_KEY"
    return "（未配置）"


def _friendly_http_error(resp) -> str:
    code = resp.status_code
    try:
        body = (resp.text or "")[:300]
    except Exception:
        body = ""
    mapping = {
        400: "请求格式错误（400）",
        401: "API Key 无效或已被删除（401）",
        402: "账户余额不足（402），去 DeepSeek 控制台充值",
        403: "没有权限（403），确认这个 key 能访问该模型",
        404: "接口地址不对（404），检查 base_url",
        429: "请求太频繁（429），稍等一下",
        500: "服务端错误（500）",
        503: "服务繁忙（503）",
    }
    head = mapping.get(code, f"HTTP {code}")
    return f"{head}：{body}" if body else head


def _messages(
    user_text: str,
    history: Optional[List[dict]] = None,
    extra_system: str = "",
    system_override: Optional[str] = None,
) -> List[dict]:
    cfg = load_config()
    if system_override is not None:
        # 内部任务（记忆提炼等）需要干净的提示，不能带角色人设
        system = str(system_override)
    else:
        system = str(cfg.get("system_prompt") or "")
        if extra_system:
            # 长期记忆等附加上下文并进同一条 system，避免出现多条 system 被忽略
            system = (system + "\n\n" + extra_system).strip()
    msgs = [{"role": "system", "content": system}]
    if history:
        msgs.extend(history)
    msgs.append({"role": "user", "content": user_text})
    return msgs


# ---------------------------------------------------------------- 线上 API

def _stream_openai(
    user_text: str,
    history: Optional[List[dict]],
    extra_system: str = "",
    system_override: Optional[str] = None,
) -> Iterator[str]:
    cfg = load_config()
    key = _clean_key(cfg.get("api_key"))
    if not key or key.startswith("sk-把"):
        raise LLMError("还没配置 DeepSeek API Key。" + config_hint())

    payload = {
        "model": cfg.get("model") or "deepseek-chat",
        "messages": _messages(user_text, history, extra_system, system_override),
        "stream": True,
        "temperature": float(cfg.get("temperature") or 0.9),
        "max_tokens": int(cfg.get("max_tokens") or 512),
    }
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }
    url = _endpoint(cfg.get("base_url") or "https://api.deepseek.com")

    try:
        resp = requests.post(
            url,
            headers=headers,
            json=payload,
            stream=True,
            timeout=(10, float(cfg.get("timeout") or 60)),
        )
    except requests.exceptions.ConnectTimeout:
        raise LLMError("连接 DeepSeek 超时，检查网络/代理设置")
    except requests.exceptions.RequestException as e:
        raise LLMError(f"连接 DeepSeek 失败：{e}")

    with resp:
        if resp.status_code != 200:
            raise LLMError(_friendly_http_error(resp))

        for raw in resp.iter_lines():
            if not raw:
                continue
            try:
                line = raw.decode("utf-8", errors="replace").strip()
            except Exception:
                continue
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                obj = json.loads(data)
            except Exception:
                continue
            choices = obj.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta") or {}
            # deepseek-reasoner 会先吐 reasoning_content，这里只要正式内容
            piece = delta.get("content")
            if piece:
                yield piece


# ---------------------------------------------------------------- 本地 Ollama

def _stream_ollama(
    user_text: str,
    history: Optional[List[dict]],
    extra_system: str = "",
    system_override: Optional[str] = None,
) -> Iterator[str]:
    cfg = load_config()
    payload = {
        "model": cfg.get("ollama_model") or "gemma3:4b",
        "messages": _messages(user_text, history, extra_system, system_override),
        "stream": True,
    }
    try:
        resp = requests.post(
            cfg.get("ollama_url") or "http://127.0.0.1:11434/api/chat",
            json=payload,
            stream=True,
            timeout=(5, float(cfg.get("timeout") or 60)),
        )
    except requests.exceptions.RequestException as e:
        raise LLMError(f"连不上本地 Ollama（{e}），确认 ollama serve 在跑")

    with resp:
        if resp.status_code != 200:
            raise LLMError(_friendly_http_error(resp))
        for raw in resp.iter_lines():
            if not raw:
                continue
            try:
                obj = json.loads(raw.decode("utf-8", errors="replace"))
            except Exception:
                continue
            piece = (obj.get("message") or {}).get("content")
            if piece:
                yield piece
            if obj.get("done"):
                break


# ---------------------------------------------------------------- 对外接口

def chat_stream(
    user_text: str,
    history: Optional[List[dict]] = None,
    extra_system: str = "",
    system_override: Optional[str] = None,
) -> Iterator[str]:
    """逐段吐出模型回复。调用方自己拼完整答案。"""
    text = (user_text or "").strip()
    if not text:
        return
    cfg = load_config()
    provider = str(cfg.get("provider") or "deepseek").lower()
    if provider == "ollama":
        yield from _stream_ollama(text, history, extra_system, system_override)
    else:
        yield from _stream_openai(text, history, extra_system, system_override)


def chat_once(
    user_text: str,
    history: Optional[List[dict]] = None,
    extra_system: str = "",
    system_override: Optional[str] = None,
) -> str:
    return "".join(
        chat_stream(user_text, history, extra_system, system_override)
    ).strip()


# ---------------------------------------------------------------- 自测

def _check() -> int:
    """检查 key 配置：格式、长度、来源、是否真的能通过鉴权"""
    cfg = load_config()
    raw_key = cfg.get("api_key")
    key = _clean_key(raw_key)

    print("=" * 56)
    print(f"provider : {cfg.get('provider')}")
    print(f"base_url : {cfg.get('base_url')}")
    print(f"model    : {cfg.get('model')}")
    print(f"key      : {_mask(key)}   长度={len(key)}")
    print(f"key 来源 : {_key_source()}")
    for env_name, cfg_key, env_val in _CONFLICTS:
        shown = _mask(_clean_key(env_val)) if cfg_key == "api_key" else env_val
        print(f"⚠ 环境变量 {env_name}={shown} 与配置文件不同，已按文件为准")
    if raw_key is not None and str(raw_key) != key:
        print(f"⚠ 原值里有空白/引号/零宽字符，已清洗掉 {len(str(raw_key)) - len(key)} 个字符")
    print("=" * 56)

    ok = True
    if not key:
        print("✗ api_key 是空的")
        return 1
    if not key.startswith("sk-"):
        print("✗ DeepSeek 的 key 一定以 sk- 开头，你这份不是")
        ok = False
    if len(key) < 30:
        print(f"✗ 长度只有 {len(key)}，正常是 35 左右，多半复制时漏了尾巴")
        ok = False
    if len(key) > 60:
        print(f"✗ 长度 {len(key)} 太长，可能多粘了别的东西")
        ok = False

    # 真正打一次 /models 验证鉴权
    url = _models_endpoint(cfg.get("base_url"))
    print(f"\n正在验证密钥 → {url}")
    try:
        r = requests.get(
            url,
            headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
            timeout=15,
        )
    except requests.exceptions.RequestException as e:
        print(f"✗ 连不上服务器：{e}")
        return 1

    if r.status_code == 200:
        try:
            models = [m.get("id") for m in (r.json().get("data") or [])]
        except Exception:
            models = []
        print("✓ key 有效，鉴权通过")
        if models:
            print(f"✓ 可用模型：{', '.join(m for m in models[:8] if m)}")
        return 0 if ok else 1

    print(f"✗ 服务端拒绝：{_friendly_http_error(r)}")
    if r.status_code == 401:
        print("\n可能原因（按概率排）：")
        print("  1. key 复制不完整 —— 去 platform.deepseek.com 用“复制”按钮重新复制")
        print("  2. key 已被删除/重置 —— 在控制台新建一个")
        print("  3. 这是别人家的 key（中转站/淘宝）—— 那种必须同时改 base_url")
        print("  4. 环境变量里还留着旧 key，覆盖了配置文件 —— 看上面“key 来源”")
    return 1


def _main() -> int:
    args = sys.argv[1:]
    if args and args[0] in ("--check", "-c", "check"):
        return _check()

    text = args[0] if args else "你好，用一句话介绍你自己"
    cfg = load_config()
    print(f"[llm] provider={cfg['provider']} model={cfg['model']} url={cfg.get('base_url')}")
    print(f"[llm] config={CONFIG_PATH}")
    print("-" * 50)
    t0 = time.time()
    first = None
    try:
        for piece in chat_stream(text):
            if first is None:
                first = time.time() - t0
            sys.stdout.write(piece)
            sys.stdout.flush()
    except LLMError as e:
        print(f"\n[llm] 失败：{e}")
        return 1
    total = time.time() - t0
    print(f"\n{'-' * 50}")
    print(f"[llm] 首字 {first:.2f}s / 全文 {total:.2f}s")
    return 0


if __name__ == "__main__":
    # 中文 Windows 控制台可能是 GBK，打印中文会炸
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    raise SystemExit(_main())
