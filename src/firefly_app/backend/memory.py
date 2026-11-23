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
from contextlib import contextmanager
from pathlib import Path
from typing import List, Optional

from firefly_app.paths import ROOT

WORKSPACE_ROOT = ROOT
LEGACY_DB = WORKSPACE_ROOT / "data" / "memory.db"


def _memory_dir() -> Path:
    """记忆库专用目录：默认 data/memory，可用配置 memory_dir 覆盖"""
    try:
        from firefly_app.backend import llm

        cfg = llm.load_config()
        d = cfg.get("memory_dir")
        if isinstance(d, str) and d.strip():
            p = Path(d).expanduser()
            return p if p.is_absolute() else WORKSPACE_ROOT / p
    except Exception:
        pass
    return WORKSPACE_ROOT / "data" / "memory"


DB_DIR = _memory_dir()
DB_PATH = DB_DIR / "memory.db"
WARN_STAMP = DB_DIR / ".last_warn"
ARCHIVE_DIR = DB_DIR / "archives"

_last_compress_check = 0.0


def disk_free_mb() -> float:
    """记忆库所在磁盘的剩余空间（MB）"""
    try:
        import shutil

        base = DB_DIR if DB_DIR.exists() else WORKSPACE_ROOT
        return shutil.disk_usage(str(base)).free / 1048576.0
    except Exception:
        return 0.0


def _limits() -> tuple[float, float]:
    """(容量上限 MB, 提醒阈值百分比)

    上限默认按磁盘剩余算：free × memory_limit_ratio（默认 2%）；
    仍然可以设 memory_max_mb 作为绝对上限（0 = 只用磁盘比例）。
    环境变量 FIREFLY_MEMORY_MAX_MB 可临时覆盖（调试用）。
    """
    try:
        import os

        from firefly_app.backend import llm

        cfg = llm.load_config()

        env_mb = os.environ.get("FIREFLY_MEMORY_MAX_MB")
        if env_mb:
            mb = float(env_mb)
            pct = float(cfg.get("memory_warn_percent") or 85)
            return max(0.001, mb), max(50.0, min(99.0, pct))

        ratio = float(cfg.get("memory_limit_ratio") or 0.02)
        cap = float(cfg.get("memory_max_mb") or 0)

        free = disk_free_mb()
        auto = free * ratio if free > 0 else 0.0
        limit = auto if cap <= 0 else (min(auto, cap) if auto > 0 else cap)
        limit = max(10.0, limit)          # 再小也给 10MB 余地

        pct = float(cfg.get("memory_warn_percent") or 85)
        return limit, max(50.0, min(99.0, pct))
    except Exception:
        return 50.0, 85.0


def compress_threshold_mb() -> float:
    """压缩阈值：配的 GB 数与"上限的 85%"取小，保证小上限时也能自动压缩"""
    try:
        from firefly_app.backend import llm

        gb = float(llm.load_config().get("memory_compress_gb") or 10)
    except Exception:
        gb = 10.0
    limit_mb, _ = _limits()
    return max(5.0, min(gb * 1024.0, limit_mb * 0.85))


def _migrate_legacy() -> None:
    """老版本把库放在 data/memory.db，这里搬到独立目录一次"""
    try:
        if LEGACY_DB.exists() and not DB_PATH.exists():
            DB_DIR.mkdir(parents=True, exist_ok=True)
            import shutil

            shutil.move(str(LEGACY_DB), str(DB_PATH))
            for suffix in ("-wal", "-shm", "-journal"):
                old = Path(str(LEGACY_DB) + suffix)
                if old.exists():
                    shutil.move(str(old), str(DB_PATH) + suffix)
            print(f"[memory] 记忆库已迁移到 {DB_PATH}", flush=True)
    except Exception as e:
        print(f"[memory] 记忆库迁移失败（继续用旧位置）: {e}", flush=True)

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

    last = None
    for attempt in range(3):
        con = None
        try:
            con = sqlite3.connect(str(DB_PATH), timeout=10)
            con.row_factory = sqlite3.Row

            try:
                con.execute("PRAGMA journal_mode=WAL")
            except sqlite3.OperationalError:
                # WAL 要额外的 -wal/-shm 文件，某些环境（杀软、残留锁、
                # 非 NTFS 卷）会报 disk I/O error，退回传统日志模式即可，
                # 功能完全不受影响。
                con.execute("PRAGMA journal_mode=DELETE")

            con.execute("PRAGMA synchronous=NORMAL")
            return con
        except sqlite3.OperationalError as e:
            last = e
            if con is not None:
                try:
                    con.close()
                except Exception:
                    pass
            time.sleep(0.2 * (attempt + 1))

    raise last if last else RuntimeError("打开记忆库失败")


@contextmanager
def _db():
    """正确用完就关的连接。

    注意：`with sqlite3.connect(...) as con` 只负责提交/回滚，
    **不会关闭连接**，每次调用都会泄漏一个文件句柄。句柄攒多了，
    WAL 的 -wal/-shm 就无法清理，进而报 disk I/O error。
    """
    con = _connect()
    try:
        yield con
        con.commit()
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
        raise
    finally:
        try:
            con.close()
        except Exception:
            pass


def init_db() -> None:
    _migrate_legacy()
    DB_DIR.mkdir(parents=True, exist_ok=True)
    with _db() as con:
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

    with _lock, _db() as con:
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


def remember_then_check(content: str, kind: str = "fact") -> dict:
    """写入一条记忆，并顺手检查是否该压缩归档（60 秒最多查一次）"""
    r = remember(content, kind)
    try:
        maybe_auto_compress()
    except Exception:
        pass
    return r


# ---------------------------------------------------------------- 读取

def purge_expired(now: Optional[float] = None) -> int:
    now = time.time() if now is None else now
    with _lock, _db() as con:
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

    with _db() as con:
        return [dict(r) for r in con.execute(sql, args)]


def prompt_block(limit: int = 8, query: str = "") -> str:
    """拼成注入系统提示的一段文字（带 query 时会顺带查压缩包）"""
    rows = recall(limit=limit)
    lines = []
    if rows:
        lines = [
            "[长期记忆]",
            "这些是你已经知道的关于用户的信息。用户问起相关的事情时，要自然地用上它们，",
            "不要生硬背诵，也不要说“根据我的记忆”这类话：",
        ]
        for r in rows:
            lines.append(f"- {r['content']}")

    # 压缩包里如果有相关内容，也一并给模型看（相当于"翻旧档案"）
    if query:
        try:
            hits = search_archives(query, limit=3)
        except Exception:
            hits = []
        if hits:
            if not lines:
                lines = ["[长期记忆]"]
            lines.append("[归档记忆] 以下是更早以前归档下来的相关内容：")
            for h in hits:
                lines.append(f"- {h['content']}（{h['date']}）")

    return "\n".join(lines)


def stats() -> dict:
    now = time.time()
    with _db() as con:
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


def storage_info() -> dict:
    """记忆库占用了多少空间、离上限还有多远"""
    limit_mb, warn_pct = _limits()

    size = 0
    try:
        if DB_PATH.exists():
            size += DB_PATH.stat().st_size
        for suffix in ("-wal", "-shm", "-journal"):
            p = Path(str(DB_PATH) + suffix)
            if p.exists():
                size += p.stat().st_size
    except Exception:
        pass

    size_mb = size / (1024 * 1024)
    percent = (size_mb / limit_mb * 100.0) if limit_mb > 0 else 0.0

    alive = 0
    try:
        info = stats()
        alive = info.get("alive", 0)
    except Exception:
        pass

    return {
        "dir": str(DB_DIR),
        "db": str(DB_PATH),
        "size_bytes": size,
        "size_mb": round(size_mb, 2),
        "limit_mb": round(limit_mb, 1),
        "percent": round(percent, 1),
        "warn_percent": warn_pct,
        "near_full": percent >= warn_pct,
        "full": percent >= 100.0,
        "alive": alive,
        "free_mb": round(disk_free_mb(), 1),
        "compress_threshold_mb": round(compress_threshold_mb(), 1),
        "archives": len(list_archives()),
    }


def check_and_maybe_warn(cooldown_hours: float = 12.0) -> Optional[dict]:
    """容量接近上限时返回提醒文案（同一提醒冷却 cooldown_hours 小时）

    返回 None 表示不用提醒。
    """
    info = storage_info()
    if not info["near_full"]:
        return None

    # 冷却：避免每轮对话都念一遍
    try:
        if WARN_STAMP.exists():
            last = float(WARN_STAMP.read_text(encoding="utf-8").strip() or 0)
            if time.time() - last < cooldown_hours * 3600:
                return None
    except Exception:
        pass

    try:
        WARN_STAMP.parent.mkdir(parents=True, exist_ok=True)
        WARN_STAMP.write_text(str(time.time()), encoding="utf-8")
    except Exception:
        pass

    level = "满了" if info["full"] else "快满了"
    limit_txt = f"{info['limit_mb']:.0f}" if info["limit_mb"] >= 10 else f"{info['limit_mb']:.1f}"
    text = (
        f"主人，记忆库{level}——现在 {info['size_mb']} MB / 上限 {limit_txt} MB"
        f"（{info['percent']:.0f}%），要不要我帮你清理一些旧的？"
    )
    info["message"] = text
    return info


def trim(keep_recent_days: float = 0) -> dict:
    """清理记忆库：先删过期，必要时再删最旧的一批，最后 VACUUM 回收空间

    keep_recent_days > 0 时，额外只保留最近这么多天的记忆。
    返回 {freed_mb, before_mb, after_mb, removed}
    """
    before = storage_info()["size_bytes"]
    removed = 0
    try:
        with _db() as con:
            cur = con.execute("DELETE FROM memories WHERE expires_at <= ?", (time.time(),))
            removed += cur.rowcount or 0

            if keep_recent_days > 0:
                cutoff = time.time() - keep_recent_days * 86400
                cur = con.execute(
                    "DELETE FROM memories WHERE last_seen < ?", (cutoff,)
                )
                removed += cur.rowcount or 0

            con.commit()
            try:
                con.execute("VACUUM")
            except Exception:
                pass
    except Exception as e:
        return {"ok": False, "error": str(e)}

    after = storage_info()["size_bytes"]
    return {
        "ok": True,
        "removed": removed,
        "before_mb": round(before / 1048576, 2),
        "after_mb": round(after / 1048576, 2),
        "freed_mb": round(max(0, before - after) / 1048576, 2),
    }


# ---------------------------------------------------------------- 归档压缩


def _fmt_date(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def list_archives() -> list:
    """列出所有压缩包（按时间倒序）"""
    out = []
    if not ARCHIVE_DIR.exists():
        return out
    for p in sorted(ARCHIVE_DIR.glob("*.json.gz"), reverse=True):
        try:
            size = p.stat().st_size
        except Exception:
            size = 0
        name = p.name[:-8]      # 去掉 .json.gz
        start, _, end = name.partition("~")
        out.append(
            {
                "file": p.name,
                "path": str(p),
                "start": start,
                "end": end or start,
                "size_kb": round(size / 1024, 1),
            }
        )
    return out


def archive_old(force: bool = False) -> dict:
    """把最旧的一半记忆压成一个归档文件，文件名是内容的日期范围

    触发条件：库体积 >= compress_threshold_mb()（或 force=True）。
    压缩后从库里删掉这些行并 VACUUM，库体积回落。
    """
    info = storage_info()
    threshold = compress_threshold_mb()
    if not force and info["size_mb"] < threshold:
        return {
            "ok": False,
            "reason": f"还没到压缩阈值（{info['size_mb']} / {threshold:.1f} MB）",
        }

    with _db() as con:
        rows = [
            dict(r)
            for r in con.execute(
                "SELECT * FROM memories ORDER BY last_seen ASC"
            )
        ]
    if not rows:
        return {"ok": False, "reason": "记忆库是空的"}

    take = max(1, len(rows) // 2)
    picked = rows[:take]

    import gzip

    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    start = _fmt_date(picked[0]["last_seen"])
    end = _fmt_date(picked[-1]["last_seen"])
    name = f"{start}~{end}" if start != end else start

    target = ARCHIVE_DIR / f"{name}.json.gz"
    n = 2
    while target.exists():
        target = ARCHIVE_DIR / f"{name}-{n}.json.gz"
        n += 1

    payload = {
        "range": [start, end],
        "count": len(picked),
        "archived_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "items": [
            {
                "content": r["content"],
                "kind": r["kind"],
                "hits": r["hits"],
                "first_seen": r["first_seen"],
                "last_seen": r["last_seen"],
            }
            for r in picked
        ],
    }
    with gzip.open(str(target), "wt", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)

    before = storage_info()["size_mb"]
    ids = [r["id"] for r in picked]
    with _db() as con:
        con.executemany("DELETE FROM memories WHERE id = ?", [(i,) for i in ids])
        con.commit()
        try:
            con.execute("VACUUM")
        except Exception:
            pass
    after = storage_info()["size_mb"]

    return {
        "ok": True,
        "file": target.name,
        "path": str(target),
        "count": len(picked),
        "start": start,
        "end": end,
        "before_mb": before,
        "after_mb": after,
    }


def search_archives(query: str, limit: int = 5) -> list:
    """在压缩包里按关键词找记忆（读完就丢，不占常驻内存）"""
    q = (query or "").strip()
    if not q or not ARCHIVE_DIR.exists():
        return []

    hits = []
    for p in sorted(ARCHIVE_DIR.glob("*.json.gz"), reverse=True):
        try:
            import gzip

            with gzip.open(str(p), "rt", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            continue
        for item in data.get("items", []):
            if q in item.get("content", ""):
                hits.append(
                    {
                        "content": item.get("content", ""),
                        "date": _fmt_date(item.get("last_seen", 0)),
                        "archive": p.name,
                    }
                )
                if len(hits) >= limit:
                    return hits
    return hits


def search_all(query: str, limit: int = 8) -> dict:
    """同时查在线记忆与压缩包——AI 据此"读取"历史"""
    live = []
    try:
        for r in recall(query=query, limit=limit):
            live.append({"content": r["content"], "date": _fmt_date(r["last_seen"])})
    except Exception:
        pass
    archived = search_archives(query, limit=limit)
    return {"live": live, "archived": archived}


def maybe_auto_compress() -> Optional[dict]:
    """写记忆后顺手检查一次（60 秒最多查一次，避免频繁读磁盘）"""
    global _last_compress_check
    now = time.time()
    if now - _last_compress_check < 60:
        return None
    _last_compress_check = now
    try:
        info = storage_info()
        if info["size_mb"] < compress_threshold_mb():
            return None
        result = archive_old(force=True)
        if result.get("ok"):
            print(
                f"[memory] 已压缩旧记忆 -> {result['file']}"
                f"（{result['count']} 条，{result['before_mb']} -> {result['after_mb']} MB）",
                flush=True,
            )
        return result
    except Exception as e:
        print(f"[memory] 自动压缩失败: {e}", flush=True)
        return None


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
            r = remember_then_check(it["content"], it["kind"])
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
