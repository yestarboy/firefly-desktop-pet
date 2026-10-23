# -*- coding: utf-8 -*-
"""
长期记忆库（SQLite）。

设计
- 库文件：data/memory.db（单独的数据目录，跟代码分开）
- 去重：内容规范化后取 SHA1 指纹做唯一索引；再加一层"同类型互相包含"的兜底
- 留存：按出现次数分三档，重复出现会续期
      1 次   -> 1 天
      2-3 次 -> 7 天
      >=4 次 -> 30 天
- 过期：每次读取前顺手清理，不需要定时任务
"""

import hashlib
import json
import re
import sqlite3
import threading
import time
import unicodedata
from pathlib import Path
from typing import List, Optional

from firefly_app.paths import ROOT

WORKSPACE_ROOT = ROOT
DB_DIR = WORKSPACE_ROOT / "data"
DB_PATH = DB_DIR / "memory.db"

# 出现次数 -> 存活天数
TIER_TABLE = [(4, 30), (2, 7), (1, 1)]

_lock = threading.Lock()


# ---------------------------------------------------------------- 基础设施

def _tier_days(hits: int) -> int:
    for threshold, days in TIER_TABLE:
        if hits >= threshold:
            return days
    return 1


def _connect() -> sqlite3.Connection:
    DB_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(DB_PATH), timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    return con


def init_db() -> None:
    with _connect() as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS memories (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                content     TEXT    NOT NULL,
                fingerprint TEXT    NOT NULL UNIQUE,
                kind        TEXT    NOT NULL DEFAULT 'fact',
                hits        INTEGER NOT NULL DEFAULT 1,
                first_seen  REAL    NOT NULL,
                last_seen   REAL    NOT NULL,
                tier_days   INTEGER NOT NULL DEFAULT 1,
                expires_at  REAL    NOT NULL
            )
            """
        )
        con.execute("CREATE INDEX IF NOT EXISTS idx_expires ON memories(expires_at)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_hits ON memories(hits DESC)")


def normalize(text: str) -> str:
    """去掉标点、空白、全半角差异，用于生成指纹和相似判断"""
    t = unicodedata.normalize("NFKC", str(text or "")).lower()
    t = re.sub(r"[\s\W_]+", "", t, flags=re.UNICODE)
    return t


def fingerprint(text: str) -> str:
    return hashlib.sha1(normalize(text).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- 写入

def remember(content: str, kind: str = "fact", now: Optional[float] = None) -> dict:
    """
    记一条。已存在则 hits+1 并续期。

    返回 {"inserted": bool, "hits": int, "id": int}
    """
    content = (content or "").strip()
    norm = normalize(content)
    if len(norm) < 2:
        return {"inserted": False, "hits": 0, "id": 0}

    content = content[:120]
    fp = fingerprint(content)
    now = time.time() if now is None else now

    with _lock, _connect() as con:
        row = con.execute(
            "SELECT id, hits FROM memories WHERE fingerprint = ?", (fp,)
        ).fetchone()

        # 兜底相似判断：未过期、且一句话包含另一句。
        # 注意这里【不按 kind 过滤】：同一件事被标成 fact 或 preference 都该合并，
        # 否则 "用户喜欢X" 和 "喜欢X" 会各存一条。
        if row is None:
            for r in con.execute(
                "SELECT id, content, hits FROM memories WHERE expires_at > ?", (now,)
            ):
                a = normalize(r["content"])
                b = norm
                if len(a) >= 4 and len(b) >= 4 and (a in b or b in a):
                    row = r
                    break

        if row is not None:
            hits = int(row["hits"]) + 1
            days = _tier_days(hits)
            con.execute(
                "UPDATE memories SET hits=?, last_seen=?, tier_days=?, expires_at=? WHERE id=?",
                (hits, now, days, now + days * 86400, row["id"]),
            )
            return {"inserted": False, "hits": hits, "id": int(row["id"])}

        days = _tier_days(1)
        cur = con.execute(
            """INSERT INTO memories
               (content, fingerprint, kind, hits, first_seen, last_seen, tier_days, expires_at)
               VALUES (?, ?, ?, 1, ?, ?, ?, ?)""",
            (content, fp, kind or "fact", now, now, days, now + days * 86400),
        )
        return {"inserted": True, "hits": 1, "id": int(cur.lastrowid)}


# ---------------------------------------------------------------- 读取

def purge_expired(now: Optional[float] = None) -> int:
    now = time.time() if now is None else now
    with _lock, _connect() as con:
        cur = con.execute("DELETE FROM memories WHERE expires_at <= ?", (now,))
        return cur.rowcount or 0


def recall(query: str = "", limit: int = 8, now: Optional[float] = None) -> List[dict]:
    """按 出现次数 > 最近出现 排序取出未过期的记忆"""
    now = time.time() if now is None else now
    purge_expired(now)

    sql = "SELECT * FROM memories WHERE expires_at > ?"
    args: list = [now]
    if query:
        # 简单关键词过滤：把查询拆成 2 字片段做 LIKE
        sql += " AND (content LIKE ?)"
        args.append(f"%{query[:20]}%")
    sql += " ORDER BY hits DESC, last_seen DESC LIMIT ?"
    args.append(int(limit))

    with _connect() as con:
        return [dict(r) for r in con.execute(sql, args)]


def prompt_block(limit: int = 8) -> str:
    """拼成注入系统提示的一段文字"""
    rows = recall(limit=limit)
    if not rows:
        return ""
    lines = [
        "[长期记忆]",
        "这些是你已经知道的关于用户的信息。用户问起相关的事情时，要自然地用上它们，",
        "不要生硬背诵，也不要说“根据我的记忆”这类话：",
    ]
    for r in rows:
        lines.append(f"- {r['content']}")
    return "\n".join(lines)


def stats() -> dict:
    now = time.time()
    with _connect() as con:
        total = con.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        alive = con.execute(
            "SELECT COUNT(*) FROM memories WHERE expires_at > ?", (now,)
        ).fetchone()[0]
        by_tier = {
            str(r["tier_days"]): r["n"]
            for r in con.execute(
                "SELECT tier_days, COUNT(*) AS n FROM memories WHERE expires_at > ? "
                "GROUP BY tier_days",
                (now,),
            )
        }
    return {"total": total, "alive": alive, "by_tier_days": by_tier, "db": str(DB_PATH)}


# ---------------------------------------------------------------- 自动学习

_EXTRACT_SYSTEM = """你是记忆整理器，只做一件事：从一轮对话中提取关于【用户本人】的长期有效信息。

规则：
1. 只记录用户说的内容；助手说的话一律不要记录（包括助手对自己的设定）
2. 只记录长期有效的信息：偏好、事实、计划、称呼、重要日期
3. 不要记录寒暄、临时话题、一次性的问题
4. 每条不超过 30 字，用第三人称，例如 "用户喜欢草莓蛋糕"
5. 只输出 JSON 数组，不要解释、不要 markdown 代码块

格式：[{"content": "...", "kind": "fact|preference|plan"}]
没有值得记的内容时，只输出 []"""

_EXTRACT_USER = """用户：{q}
助手：{a}"""


def learn_async(question: str, answer: str) -> None:
    """把一轮对话丢给 LLM 提炼记忆（后台线程，不阻塞回复）"""
    if not question or len(question.strip()) < 2:
        return
    threading.Thread(
        target=_learn_worker, args=(question, answer), daemon=True
    ).start()


def _learn_worker(question: str, answer: str) -> None:
    try:
        from firefly_app.backend import llm

        cfg = llm.load_config()
        if not cfg.get("memory_extract", True):
            print("[memory] memory_extract=false，跳过提炼", flush=True)
            return

        raw = llm.chat_once(
            _EXTRACT_USER.format(q=question[:500], a=(answer or "")[:500]),
            system_override=_EXTRACT_SYSTEM,
        )
        print(f"[memory] 提炼原始输出: {raw[:150]!r}", flush=True)

        items = _parse_items(raw)
        print(f"[memory] 解析出 {len(items)} 条: {items}", flush=True)

        for it in items:
            r = remember(it["content"], it["kind"])
            flag = "新增" if r["inserted"] else "命中"
            print(
                f"[memory] {flag} #{r['id']} hits={r['hits']} "
                f"档={_tier_days(r['hits'])}天  {it['content']}",
                flush=True,
            )
    except Exception as e:
        print(f"[memory] 提炼失败: {e}", flush=True)


def _parse_items(raw: str) -> List[dict]:
    if not raw:
        return []
    start, end = raw.find("["), raw.rfind("]")
    if start < 0 or end <= start:
        return []
    try:
        data = json.loads(raw[start : end + 1])
    except Exception:
        return []
    if not isinstance(data, list):
        return []

    out = []
    for it in data[:3]:
        if isinstance(it, str):
            content, kind = it, "fact"
        elif isinstance(it, dict):
            content = str(it.get("content") or "").strip()
            kind = str(it.get("kind") or "fact").strip()
        else:
            continue
        if 2 <= len(content) <= 60:
            out.append({"content": content, "kind": kind})
    return out
