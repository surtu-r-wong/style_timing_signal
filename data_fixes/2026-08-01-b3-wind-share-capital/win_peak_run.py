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

#: 轮询间隔。每次采样要枚举一遍进程树，0.2s 对分钟到小时级的作业既足够密、
#: 开销又可以忽略；Windows 的 peak_wset 本身单调，采样频率只影响短命进程。
POLL_SECONDS = 0.2


def _sample_tree(root: psutil.Process) -> tuple[int, int]:
    """返回 (当前整棵树的驻留合计, 树中单进程历史峰值的最大者)。

    **必须连子孙一起量**：Windows 上 venv 的 ``Scripts\\python.exe`` 会把基础
    解释器作为子进程拉起来，直接子进程只是个几 MB 的转发器，真正吃内存的是
    孙进程。只量直接子进程会把每一段都记成空壳（实测 202 MB 记成 6 MB）。
    """
    try:
        processes = [root, *root.children(recursive=True)]
    except psutil.Error:
        return 0, 0

    concurrent = 0
    highest = 0
    for process in processes:
        try:
            info = process.memory_info()
        except psutil.Error:
            continue
        concurrent += int(getattr(info, "wset", info.rss))
        highest = max(highest, int(getattr(info, "peak_wset", info.rss)))
    return concurrent, highest


def write_report(report: Path, argv: list[str], peak_bytes: int) -> None:
    """按 GNU time -v 的行格式落盘，键名必须与 peak_rss_kib 的正则一致。"""
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(
        f'\tCommand being timed: "{" ".join(argv)}"\n'
        f"\tMaximum resident set size (kbytes): {peak_bytes // 1024}\n",
        encoding="utf-8",
    )


def measure(argv: list[str], report: Path) -> int:
    """Run argv, record its whole tree's peak memory, and return its exit code.

    两个量取大者：采样得到的"整棵树同时驻留"的最大值，以及树中任一进程的历史
    峰值。前者在真有并发时不会低估，后者在采样错过瞬时高点时兜底（单调，采到
    一次就够）。
    """
    popen = subprocess.Popen(argv)
    try:
        root: psutil.Process | None = psutil.Process(popen.pid)
    except psutil.Error:
        root = None

    peak_concurrent = 0
    peak_single = 0
    while popen.poll() is None:
        if root is not None:
            concurrent, highest = _sample_tree(root)
            peak_concurrent = max(peak_concurrent, concurrent)
            peak_single = max(peak_single, highest)
        time.sleep(POLL_SECONDS)

    exit_code = popen.wait()
    write_report(report, argv, max(peak_concurrent, peak_single))
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
