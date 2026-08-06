#!/usr/bin/env python3
"""在没有 cgroup 的平台上测子进程内存峰值，写成 GNU time 认得的格式。

Windows 上既没有 ``systemd-run`` 也没有 ``/usr/bin/time -v``。本包装器起子进程、
轮询其内存峰值，退出后把结果按 GNU time ``-v`` 的那一行写进 report 文件——于是
``run_guarded_b3.peak_rss_kib`` 一个字都不用改，平台差异全部收在这一个文件里。

Windows 的 ``peak_wset`` 本身就是峰值，轮询只是为了在子进程消失前把它取到；
其它平台只有瞬时 ``rss``，取样本最大值（用于在开发机上验证本文件的行为）。

用法::

    python win_peak_run.py --report <path> -- <命令...>

退出码逐字透传子进程的退出码，因为调用方要靠它判断阶段成败。
"""

from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys
import time

import psutil

#: 轮询间隔。B3 三段是分钟到小时级的作业，50ms 的采样密度对峰值的误差可以忽略，
#: 而开销小到测不出来。
POLL_SECONDS = 0.05


def _sample_bytes(process: psutil.Process | None) -> int:
    if process is None:
        return 0
    try:
        info = process.memory_info()
    except psutil.Error:
        return 0
    return int(getattr(info, "peak_wset", info.rss))


def write_report(report: Path, argv: list[str], peak_bytes: int) -> None:
    """按 GNU time -v 的行格式落盘，键名必须与 peak_rss_kib 的正则一致。"""
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(
        f'\tCommand being timed: "{" ".join(argv)}"\n'
        f"\tMaximum resident set size (kbytes): {peak_bytes // 1024}\n",
        encoding="utf-8",
    )


def measure(argv: list[str], report: Path) -> int:
    """Run argv, record its peak memory, and return its exit code."""
    popen = subprocess.Popen(argv)
    try:
        process: psutil.Process | None = psutil.Process(popen.pid)
    except psutil.Error:
        process = None

    peak = 0
    while popen.poll() is None:
        peak = max(peak, _sample_bytes(process))
        time.sleep(POLL_SECONDS)
    peak = max(peak, _sample_bytes(process))

    exit_code = popen.wait()
    write_report(report, argv, peak)
    return exit_code


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--" not in argv:
        raise SystemExit(
            "usage: win_peak_run.py --report <path> -- <command...>"
        )
    separator = argv.index("--")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True)
    args = parser.parse_args(argv[:separator])

    command = argv[separator + 1:]
    if not command:
        raise SystemExit("no command to run")
    return measure(command, Path(args.report))


if __name__ == "__main__":
    raise SystemExit(main())
