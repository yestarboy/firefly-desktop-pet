# -*- coding: utf-8 -*-
"""
记忆库命令行工具。

用法：
    .\\.venv\\Scripts\\python.exe tools\\memory_cli.py stats
    .\\.venv\\Scripts\\python.exe tools\\memory_cli.py list
    .\\.venv\\Scripts\\python.exe tools\\memory_cli.py add "用户喜欢草莓蛋糕" --kind preference
    .\\.venv\\Scripts\\python.exe tools\\memory_cli.py find 蛋糕
    .\\.venv\\Scripts\\python.exe tools\\memory_cli.py purge
    .\\.venv\\Scripts\\python.exe tools\\memory_cli.py wipe        # 清空（会二次确认）
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from firefly_app.backend import memory  # noqa: E402


def _fmt_ts(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def _left(expires_at: float) -> str:
    sec = expires_at - time.time()
    if sec <= 0:
        return "已过期"
    if sec >= 86400:
        return f"{sec / 86400:.1f} 天"
    return f"{sec / 3600:.1f} 小时"


def cmd_stats(_args) -> int:
    s = memory.stats()
    print(f"数据库 : {s['db']}")
    print(f"总条数 : {s['total']}")
    print(f"未过期 : {s['alive']}")
    print("按留存 :")
    for days, n in sorted(s["by_tier_days"].items(), key=lambda x: -int(x[0])):
        print(f"    {days:>2} 天档 : {n} 条")
    return 0


def cmd_list(args) -> int:
    rows = memory.recall(limit=args.limit)
    if not rows:
        print("（空）")
        return 0
    print(f"{'ID':>4}  {'次数':>4}  {'档位':>4}  剩余        内容")
    print("-" * 78)
    for r in rows:
        print(
            f"{r['id']:>4}  {r['hits']:>4}  {r['tier_days']:>3}天  "
            f"{_left(r['expires_at']):<10}  {r['content']}"
        )
    return 0


def cmd_add(args) -> int:
    res = memory.remember(args.content, kind=args.kind)
    if res["hits"] == 0:
        print("内容太短，忽略")
        return 1
    if res["inserted"]:
        print(f"新增 #{res['id']}：{args.content}")
    else:
        print(f"已存在 #{res['id']}，次数 +1 -> {res['hits']}（留存自动升级）")
    return 0


def cmd_find(args) -> int:
    rows = memory.recall(query=args.keyword, limit=args.limit)
    if not rows:
        print("没找到")
        return 0
    for r in rows:
        print(f"#{r['id']} [{r['hits']}次/{r['tier_days']}天] {r['content']}")
    return 0


def cmd_purge(_args) -> int:
    n = memory.purge_expired()
    print(f"清理过期记忆 {n} 条")
    return 0


def cmd_wipe(_args) -> int:
    if input(f"确定清空 {memory.DB_PATH} ? (yes/no) ").strip().lower() != "yes":
        print("已取消")
        return 0
    with memory._connect() as con:
        con.execute("DELETE FROM memories")
    print("已清空")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Firefly 记忆库工具")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("stats").set_defaults(func=cmd_stats)
    sub.add_parser("purge").set_defaults(func=cmd_purge)
    sub.add_parser("wipe").set_defaults(func=cmd_wipe)

    pl = sub.add_parser("list")
    pl.add_argument("--limit", type=int, default=20)
    pl.set_defaults(func=cmd_list)

    pa = sub.add_parser("add")
    pa.add_argument("content")
    pa.add_argument("--kind", default="fact", choices=["fact", "preference", "plan"])
    pa.set_defaults(func=cmd_add)

    pf = sub.add_parser("find")
    pf.add_argument("keyword")
    pf.add_argument("--limit", type=int, default=20)
    pf.set_defaults(func=cmd_find)

    args = p.parse_args()
    memory.init_db()
    return args.func(args)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    raise SystemExit(main())
