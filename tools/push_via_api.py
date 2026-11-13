# -*- coding: utf-8 -*-
"""
从 Windows 凭据管理器读取 GitHub 凭据，并用 API 推送提交

背景：这台机器的 Git 安装缺 git-remote-https.exe，且下载补装被墙，
但 api.github.com 可达、凭据管理器里也有 git:https://github.com 的令牌。
所以直接用 GitHub Git Data API 建 blob/tree/commit 并更新分支。

用法：
    python tools/push_via_api.py --check          # 只检查令牌和远端状态
    python tools/push_via_api.py --dry-run        # 打印将要提交的文件
    python tools/push_via_api.py --message "..."  # 真正推送
"""

from __future__ import annotations

import base64
import ctypes
import json
import subprocess
import sys
from ctypes import wintypes
from pathlib import Path

import requests

REPO = "yestarboy/firefly-desktop-pet"
API = "https://api.github.com"
ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------- 凭据读取


class _CREDENTIAL(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_byte)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


def read_git_credential() -> tuple[str, str]:
    """返回 (用户名, 令牌)；读不到就抛异常"""
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    cred = ctypes.POINTER(_CREDENTIAL)()
    ok = advapi.CredReadW(
        ctypes.c_wchar_p("git:https://github.com"),
        1,   # CRED_TYPE_GENERIC
        0,
        ctypes.byref(cred),
    )
    if not ok:
        raise RuntimeError(f"读取凭据失败: {ctypes.get_last_error()}")

    try:
        blob = ctypes.string_at(cred.contents.CredentialBlob, cred.contents.CredentialBlobSize)
        password = blob.decode("utf-16-le", errors="ignore").rstrip("\x00")
        user = cred.contents.UserName or ""
        return user, password
    finally:
        advapi.CredFree(cred)


def session() -> requests.Session:
    user, token = read_git_credential()
    s = requests.Session()
    s.headers.update(
        {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
    )
    return s


# ---------------------------------------------------------------- Git 操作


def git(*args: str) -> str:
    out = subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8"
    )
    if out.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败: {out.stderr[:200]}")
    return out.stdout


def changed_files(base: str = "origin/main") -> list[tuple[str, str]]:
    """返回 [(状态, 路径)]，状态为 A/M/D

    core.quotepath=false 很关键：默认 git 会把中文路径转成
    "data/voices/\\346\\216\\245..." 这种八进制转义，
    直接拿去建 tree 会被 GitHub 拒绝（422）。
    """
    raw = git("-c", "core.quotepath=false", "diff", "--name-status", base, "HEAD")
    rows = []
    for line in raw.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            rows.append((parts[0][0], parts[-1]))
    return rows


def blob_for(path: str) -> str:
    data = (ROOT / path).read_bytes()
    r = requests.post(
        f"{API}/repos/{REPO}/git/blobs",
        headers={
            "Authorization": session().headers["Authorization"],
            "Accept": "application/vnd.github+json",
        },
        json={"content": base64.b64encode(data).decode(), "encoding": "base64"},
        timeout=60,
    )
    r.raise_for_status()
    return r.json()["sha"]


def main() -> int:
    args = sys.argv[1:]
    s = session()

    # 1) 校验令牌
    me = s.get(f"{API}/user", timeout=30)
    if me.status_code != 200:
        print(f"令牌无效: HTTP {me.status_code}")
        return 1
    print(f"已登录: {me.json().get('login')}")

    # 2) 远端当前状态
    ref = s.get(f"{API}/repos/{REPO}/git/ref/heads/main", timeout=30)
    ref.raise_for_status()
    remote_sha = ref.json()["object"]["sha"]
    commit = s.get(f"{API}/repos/{REPO}/git/commits/{remote_sha}", timeout=30).json()
    remote_tree = commit["tree"]["sha"]
    print(f"远端 main: {remote_sha[:7]}  tree {remote_tree[:7]}")

    local_base = git("rev-parse", "origin/main").strip()
    print(f"本地 origin/main: {local_base[:7]}")
    if local_base != remote_sha:
        print(
            "  注意：远端比本地 origin/main 新，将把本地改动应用在远端最新内容之上"
        )
    else:
        print("  远端与本地基线一致")

    rows = changed_files()
    print(f"待推送变更: {len(rows)} 个文件")
    for st, p in rows:
        print(f"  {st}  {p}")

    if "--check" in args:
        return 0
    if "--dry-run" in args:
        return 0

    msg = "chore: 同步更新"
    if "--message" in args:
        msg = args[args.index("--message") + 1]

    # 3) 先取远端完整 tree，用来跳过没变化的文件、避免重复删除
    remote_map = {}
    rt = s.get(
        f"{API}/repos/{REPO}/git/trees/{remote_tree}?recursive=1", timeout=120
    )
    if rt.status_code == 200:
        remote_map = {
            item["path"]: item["sha"]
            for item in rt.json().get("tree", [])
            if item["type"] == "blob"
        }
    print(f"远端已有文件: {len(remote_map)}")

    # 4) 建 blob（跳过内容相同的文件）
    entries = []
    added = modified = deleted = skipped = 0
    for st, p in rows:
        if st == "D":
            if p in remote_map:
                entries.append({"path": p, "mode": "100644", "type": "blob", "sha": None})
                deleted += 1
            else:
                skipped += 1          # 远端已经没有了，不用再删
            continue

        sha = blob_for(p)
        if remote_map.get(p) == sha:
            skipped += 1              # 内容一样，不用进 tree
            continue

        entries.append({"path": p, "mode": "100644", "type": "blob", "sha": sha})
        if st == "A":
            added += 1
        else:
            modified += 1
        print(f"  {st} {p} -> {sha[:7]}")

    print(f"实际变更: 新增 {added} / 修改 {modified} / 删除 {deleted} / 跳过 {skipped}")
    if not entries:
        print("没有需要提交的变更")
        return 0

    # 4) 建 tree（以远端 tree 为基准）
    tree = s.post(
        f"{API}/repos/{REPO}/git/trees",
        json={"base_tree": remote_tree, "tree": entries},
        timeout=300,
    )
    if tree.status_code != 201:
        print("tree 创建失败:", tree.status_code)
        print("返回:", tree.text[:600])
        return 3
    tree_sha = tree.json()["sha"]
    print(f"tree: {tree_sha[:7]}")

    # 5) 建 commit
    cm = s.post(
        f"{API}/repos/{REPO}/git/commits",
        json={"message": msg, "tree": tree_sha, "parents": [remote_sha]},
        timeout=60,
    )
    cm.raise_for_status()
    new_sha = cm.json()["sha"]
    print(f"commit: {new_sha[:7]}")

    # 6) 更新分支
    up = s.patch(
        f"{API}/repos/{REPO}/git/refs/heads/main",
        json={"sha": new_sha, "force": False},
        timeout=60,
    )
    up.raise_for_status()
    print(f"✅ 已更新 main -> {new_sha[:7]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
