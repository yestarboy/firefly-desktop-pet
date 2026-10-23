# -*- coding: utf-8 -*-
"""
记忆库自测（开发用，跑完会清掉自己造的数据）。

    .\\.venv\\Scripts\\python.exe tools\\memory_selftest.py
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from firefly_app.backend import memory  # noqa: E402


def main() -> int:
    memory.init_db()
    tag = "T" + str(int(time.time()) % 100000)
    print(f"测试标记: {tag}\n")

    r1 = memory.remember(f"用户喜欢{tag}蛋糕", "preference")
    print(f"第1次        : {r1}")

    r2 = memory.remember(f"用户喜欢{tag}蛋糕", "preference")
    print(f"第2次        : {r2}  -> {memory._tier_days(r2['hits'])} 天档")

    r3 = memory.remember(f"用户喜欢{tag}蛋糕", "preference")
    r4 = memory.remember(f"用户喜欢{tag}蛋糕", "preference")
    print(f"第4次        : {r4}  -> {memory._tier_days(r4['hits'])} 天档")

    r5 = memory.remember(f"喜欢{tag}蛋糕", "fact")
    ok_dedupe = not r5["inserted"]
    print(f"包含式重复   : {r5}  -> {'去重成功 OK' if ok_dedupe else '未去重 FAIL'}")

    old = time.time() - 40 * 86400
    r6 = memory.remember(f"很久以前的事{tag}", "fact", now=old)
    before = memory.stats()["total"]
    gone = memory.purge_expired()
    after = memory.stats()["total"]
    ok_expire = gone >= 1
    print(f"过期清理     : {r6}  清掉 {gone} 条  {before} -> {after}  "
          f"{'OK' if ok_expire else 'FAIL'}")

    # 清理本次测试数据
    with memory._connect() as con:
        con.execute("DELETE FROM memories WHERE content LIKE ?", (f"%{tag}%",))

    print(f"\n测试数据已清理，库内剩余 {memory.stats()['total']} 条")
    return 0 if (ok_dedupe and ok_expire) else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    raise SystemExit(main())
