# -*- coding: utf-8 -*-
"""运行时性能采样：内存 / CPU（桌面宠物整套服务）

用法：
    python tools/measure_perf.py [采样秒数]
"""

from __future__ import annotations

import subprocess
import sys
import time


def run_ps(script: str) -> str:
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return out.stdout.strip()


def snapshot() -> list[dict]:
    """一次采样：各进程内存（MB）"""
    ps = r"""
$rows = @()
Get-CimInstance Win32_Process | Where-Object { $_.Name -match 'python|QtWebEngine|node' } | ForEach-Object {
  $tag = '?'
  $cmd = $_.CommandLine
  if ($cmd -match 'live2d_pet') { $tag = 'PET' }
  elseif ($cmd -match 'stt_server') { $tag = 'STT' }
  elseif ($cmd -match 'api_v2') { $tag = 'TTS' }
  elseif ($_.Name -match 'QtWebEngine') { $tag = 'WEB' }
  $p = Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue
  if ($p -and $tag -ne '?') {
    $rows += [PSCustomObject]@{ tag = $tag; pid = $_.ProcessId; mb = [math]::Round($p.WorkingSet64/1MB); cpu = $p.TotalProcessorTime.TotalSeconds }
  }
}
$rows | ConvertTo-Json -Compress
"""
    import json

    raw = run_ps(ps)
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except Exception:
        return []
    return data if isinstance(data, list) else [data]


def main() -> int:
    seconds = int(sys.argv[1]) if len(sys.argv) > 1 else 12

    print(f"采样 {seconds} 秒…")
    before = {r["tag"] + str(r["pid"]): r for r in snapshot()}
    time.sleep(seconds)
    after = {r["tag"] + str(r["pid"]): r for r in snapshot()}

    cpu_count = int(run_ps("(Get-CimInstance Win32_Processor).NumberOfLogicalProcessors") or 1)
    # WMI 的这两个字段单位是 KB
    free_gb = float(run_ps("(Get-CimInstance Win32_OperatingSystem).FreePhysicalMemory") or 0) / 1024 / 1024
    total_gb = float(run_ps("(Get-CimInstance Win32_OperatingSystem).TotalVisibleMemorySize") or 0) / 1024 / 1024

    rows = []
    total_cpu = 0.0
    total_mb = 0
    for key, b in before.items():
        a = after.get(key)
        if not a:
            continue
        dcpu = max(0.0, a["cpu"] - b["cpu"])
        pct = dcpu / seconds / cpu_count * 100.0
        total_cpu += pct
        total_mb += a["mb"]
        rows.append((b["tag"], a["pid"], a["mb"], pct))

    print()
    print(f"{'模块':<6}{'PID':<8}{'内存MB':>8}{'CPU%':>8}")
    for tag, pid, mb, pct in sorted(rows, key=lambda r: -r[2]):
        print(f"{tag:<6}{pid:<8}{mb:>8}{pct:>7.1f}%")

    print()
    print(f"合计内存: {total_mb} MB")
    print(f"合计 CPU: {total_cpu:.1f}%（占 {cpu_count} 线程）")
    if total_gb > 0:
        used = total_gb - free_gb
        print(
            f"系统内存: 已用 {used:.1f}/{total_gb:.1f} GB，"
            f"这套服务占 {total_mb / 1024:.1f} GB（约 {total_mb / 1024 / total_gb * 100:.0f}%）"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
