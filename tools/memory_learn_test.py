# -*- coding: utf-8 -*-
"""
记忆提炼链路测试（开发用）。

它会：
1. 打印当前开关状态
2. 拿一句模拟对话跑一次提炼
3. 把结果写进库，并列出库内内容

    .\\.venv\\Scripts\\python.exe tools\\memory_learn_test.py
    .\\.venv\\Scripts\\python.exe tools\\memory_learn_test.py "我喜欢草莓蛋糕" "记住啦，草莓蛋糕！"
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from firefly_app.backend import llm, memory  # noqa: E402


def ask(question: str) -> int:
    """验证注入：带着长期记忆去问问题，看模型会不会用上"""
    memory.init_db()
    block = memory.prompt_block()
    print("=== 注入的记忆 ===")
    print(block if block else "(空，先去学两条)")

    print(f"\n=== 提问 ===\n{question}\n")
    print("=== 回答 ===")
    ans = llm.chat_once(question, extra_system=block)
    print(ans)
    return 0


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "--ask":
        return ask(sys.argv[2] if len(sys.argv) > 2 else "我叫什么名字？我喜欢吃什么？")

    q = sys.argv[1] if len(sys.argv) > 1 else "我叫小明，我最喜欢吃草莓蛋糕，尤其喜欢奶油多的那种。"
    a = sys.argv[2] if len(sys.argv) > 2 else "记住啦，草莓蛋糕加厚奶油，小明的最爱！"

    cfg = llm.load_config()
    print("=== 配置 ===")
    print(f"provider      : {cfg.get('provider')}")
    print(f"model         : {cfg.get('model')}")
    print(f"memory_enabled: {cfg.get('memory_enabled')}")
    print(f"memory_extract: {cfg.get('memory_extract')}")
    print(f"记忆库         : {memory.DB_PATH}")

    memory.init_db()
    before = memory.stats()["total"]

    print("\n=== 提炼 ===")
    print(f"用户: {q}")
    print(f"助手: {a}")

    t0 = time.time()
    memory._learn_worker(q, a)      # 同步调用，方便看输出
    print(f"耗时 {time.time() - t0:.2f}s")

    after = memory.stats()["total"]
    print(f"\n=== 结果：库内 {before} -> {after} 条 ===")

    rows = memory.recall(limit=10)
    for r in rows:
        print(f"  #{r['id']} [{r['hits']}次/{r['tier_days']}天] {r['content']}")

    block = memory.prompt_block()
    print("\n=== 会被注入的段落 ===")
    print(block if block else "（空）")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    raise SystemExit(main())
