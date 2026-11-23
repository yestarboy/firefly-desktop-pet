# -*- coding: utf-8 -*-
"""
自主迭代：让萤宝自己看代码、自己提改进、自己改、自己验证。

安全设计（很重要，别删）：
1. **每次迭代开一个独立分支** `auto/iter-<时间戳>`，不动主分支；
2. 改完必须跑验证（编译 / 前端语法 / 构建），**验证不过就自动回滚**；
3. 全程只写 `logs/evolve/` 下的报告，**永不自动推送远端**；
4. 工作区必须干净才允许开始，避免把用户未提交的改动卷进去。
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Callable, Dict, Optional

from firefly_app.backend import workspace

REPORT_DIR = workspace.DEFAULT_ROOT / "logs" / "evolve"

PROPOSE_PROMPT = """你在一个个人项目里担任「自主改进的工程师」。

先自己看一看这个工作区（用 list_dir / read_file），找出**一个**最值得做的小改进：
- 优先真实问题：明显的 bug、缺失的错误处理、可读性差的地方、能减少重复的抽象；
- 必须是能在 10 分钟内完成的**小改动**，不要大重构、不要引入新依赖；
- 不要改模型文件、不要改配置里的密钥、不要动 .git。

然后输出两行，格式严格如下：
GOAL: <一句话说明要做什么>
WHY: <一句话说明为什么值得做>
"""

SYSTEM_EXTRA = """
你现在在**自动迭代模式**下工作，必须遵守：
- 只做这一件事，改动尽量小，能少改文件就少改文件；
- 改完自己用 run_command 验证（python -m py_compile 或 node --check 或 npm run build）；
- 如果验证失败，继续修，直到通过；实在修不好就说明原因并停下；
- 完成后用一句话总结改了什么、验证结果如何。
"""


def _git(*args: str) -> str:
    out = subprocess.run(
        ["git", *args],
        cwd=str(workspace.DEFAULT_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if out.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败: {(out.stderr or out.stdout)[:300]}")
    return out.stdout.strip()


def _is_clean() -> bool:
    return _git("status", "--porcelain") == ""


def _current_branch() -> str:
    return _git("rev-parse", "--abbrev-ref", "HEAD")


def _changed_files() -> list[str]:
    raw = _git("-c", "core.quotepath=false", "status", "--porcelain")
    return [ln[3:].strip() for ln in raw.splitlines() if len(ln) > 3]


def verify_changes() -> Dict:
    """对改动过的文件做最基础的验证：Python 编译 / JS 语法 / 前端构建"""
    files = _changed_files()
    if not files:
        return {"ok": False, "output": "没有任何改动"}

    py = [f for f in files if f.endswith(".py")]
    js = [f for f in files if f.endswith(".js") and "mmd.bundle" not in f]
    need_build = any(f.startswith("panel3d/") for f in files)

    logs = []

    if py:
        cmd = ["python", "-m", "compileall", "-q", *py]
        r = subprocess.run(cmd, cwd=str(workspace.DEFAULT_ROOT), capture_output=True, text=True)
        logs.append(f"$ python -m compileall ({len(py)} 个文件) -> exit {r.returncode}")
        if r.returncode != 0:
            logs.append((r.stdout + r.stderr)[-1500:])
            return {"ok": False, "output": "\n".join(logs)}

    if js:
        for f in js:
            r = subprocess.run(
                ["node", "--check", f], cwd=str(workspace.DEFAULT_ROOT), capture_output=True, text=True
            )
            logs.append(f"$ node --check {f} -> exit {r.returncode}")
            if r.returncode != 0:
                logs.append((r.stdout + r.stderr)[-800:])
                return {"ok": False, "output": "\n".join(logs)}

    if need_build:
        r = subprocess.run(
            "npm run build",
            cwd=str(workspace.DEFAULT_ROOT / "panel3d"),
            capture_output=True,
            text=True,
            shell=True,
            timeout=300,
        )
        logs.append(f"$ npm run build -> exit {r.returncode}")
        if r.returncode != 0:
            logs.append((r.stdout + r.stderr)[-1500:])
            return {"ok": False, "output": "\n".join(logs)}

    return {"ok": True, "output": "\n".join(logs) or "无需验证的改动"}


def run_iteration(
    goal: str = "",
    emit: Optional[Callable[[str, str], None]] = None,
) -> Dict:
    """跑一轮自主迭代；返回 {ok, goal, branch, summary, report, diff}"""

    def _emit(kind: str, text: str):
        if emit:
            try:
                emit(kind, text)
            except Exception:
                pass

    if not _is_clean():
        return {"ok": False, "error": "工作区有未提交的改动，先处理干净再自动迭代"}

    origin_branch = _current_branch()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    branch = f"auto/iter-{stamp}"

    _emit("evolve_log", f"基线分支：{origin_branch}")

    # 1) 用户没给目标 → 先自己找一个改进点
    if not goal.strip():
        _emit("evolve_log", "没有指定目标，先自己看代码找改进点…")
        r = workspace.agent_ask(PROPOSE_PROMPT, emit=lambda k, t: _emit("evolve_log", t), max_steps=6)
        if not r.get("ok"):
            return {"ok": False, "error": r.get("error", "提案失败")}
        text = r.get("answer", "")
        g = ""
        w = ""
        for line in text.splitlines():
            if line.startswith("GOAL:"):
                g = line[5:].strip()
            elif line.startswith("WHY:"):
                w = line[4:].strip()
        if not g:
            return {"ok": False, "error": "没能形成明确目标：" + text[:200]}
        goal = g
        _emit("evolve_propose", f"{g}\n理由：{w}")

    _emit("evolve_log", f"目标：{goal}")

    # 2) 开分支
    try:
        _git("checkout", "-b", branch)
        _emit("evolve_log", f"已创建分支 {branch}")
    except Exception as e:
        return {"ok": False, "error": f"创建分支失败：{e}"}

    summary = ""
    try:
        # 3) 让 AI 干活
        task = goal + "\n\n" + SYSTEM_EXTRA
        r = workspace.agent_ask(task, emit=lambda k, t: _emit("evolve_log", t), max_steps=12)
        if not r.get("ok"):
            raise RuntimeError(r.get("error", "AI 执行失败"))
        summary = r.get("answer", "")

        # 4) 基础验证
        _emit("evolve_log", "开始验证改动…")
        v = verify_changes()
        _emit("evolve_log", v["output"][-1200:])
        if not v["ok"]:
            raise RuntimeError("验证没通过")

        # 5) 提交到临时分支
        diff = _git("-c", "core.quotepath=false", "diff", "--stat")
        _git("add", "-A")
        _git(
            "-c",
            "user.name=Firefly",
            "-c",
            "user.email=firefly@local",
            "commit",
            "-m",
            f"auto: {goal[:60]}",
        )
        diff = _git("-c", "core.quotepath=false", "show", "--stat", "--oneline", "HEAD")

        report = _write_report(stamp, goal, branch, summary, v["output"], diff)
        _emit("evolve_ok", branch)
        return {
            "ok": True,
            "goal": goal,
            "branch": branch,
            "summary": summary,
            "diff": diff,
            "report": str(report),
        }

    except Exception as e:
        # 6) 失败：回滚这次的所有改动，回到原分支
        _emit("evolve_log", f"失败：{e}，正在回滚…")
        try:
            _git("checkout", "--", ".")
            _git("clean", "-fd")
            _git("checkout", origin_branch)
            _git("branch", "-D", branch)
        except Exception as e2:
            _emit("evolve_log", f"回滚时又出错：{e2}")
        report = _write_report(stamp, goal, branch, f"失败：{e}", "", "", failed=True)
        return {"ok": False, "error": str(e), "report": str(report)}


def _write_report(
    stamp: str,
    goal: str,
    branch: str,
    summary: str,
    verify: str,
    diff: str,
    failed: bool = False,
) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    p = REPORT_DIR / f"{stamp}.md"
    lines = [
        f"# 自动迭代报告 {stamp}",
        "",
        f"- 结果：{'❌ 失败（已回滚）' if failed else '✅ 成功（在分支 ' + branch + '）'}",
        f"- 目标：{goal}",
        "",
        "## 过程总结",
        summary or "(无)",
        "",
        "## 改动",
        "```",
        diff or "(无改动)",
        "```",
        "",
        "## 验证输出",
        "```",
        verify or "(未执行)",
        "```",
        "",
    ]
    p.write_text("\n".join(lines), encoding="utf-8")
    return p


def status() -> Dict:
    try:
        return {
            "branch": _current_branch(),
            "clean": _is_clean(),
            "reports": sorted(p.name for p in REPORT_DIR.glob("*.md"))[-5:] if REPORT_DIR.exists() else [],
        }
    except Exception as e:
        return {"error": str(e)}
