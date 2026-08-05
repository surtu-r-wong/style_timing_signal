#!/usr/bin/env python3
"""在 8 GiB 护栏下重跑未改动的 B3 三段流水，并留下可核对的执行证据。

三段顺序固定：preflight → build(all) → eval，每段都套
``systemd-run --user --scope -p MemoryMax=8G`` 与 ``/usr/bin/time -v``。
preflight / build 非零退出立即停；eval 的退出码 2 **保留为证据**不抹平——
统计已经产出、但一个与股本无关的 run 级闸门仍可能合法地把 final_verdict
留在 DATA_BLOCKED。

所有输出走 campaign 自己的目录（``--output-dir`` / ``--research-output-dir``
/ ``--backtest-output-dir``），绝不覆盖用户已有的 output 目录。
``b3_execution_receipt.json`` 最后写、``os.replace`` 原子落盘。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time

CAMPAIGN_DIR = Path(__file__).resolve().parent
STYLE_ROOT = CAMPAIGN_DIR.parents[1]
DEFAULT_PYTHON = "/home/elfbob/miniconda3/bin/python"
DATA_END = "2023-12-31"
MEMORY_MAX = "8G"
RECEIPT_NAME = "b3_execution_receipt.json"

_PEAK_RSS_RE = re.compile(
    r"Maximum resident set size \(kbytes\):\s*(\d+)"
)


def stage_specs(
    research_dir: Path,
    backtest_dir: Path,
    *,
    python: str = DEFAULT_PYTHON,
) -> tuple[dict, ...]:
    """The three frozen stages, in order, with campaign-scoped output paths."""

    return (
        {
            "name": "preflight",
            "argv": [
                python,
                "-m",
                "signals.style_basket.b3_build",
                "--stage",
                "preflight",
                "--data-end",
                DATA_END,
                "--output-dir",
                str(research_dir),
            ],
            "allowed_exit_codes": (0,),
        },
        {
            "name": "build",
            "argv": [
                python,
                "-m",
                "signals.style_basket.b3_build",
                "--stage",
                "all",
                "--data-end",
                DATA_END,
                "--output-dir",
                str(research_dir),
            ],
            "allowed_exit_codes": (0,),
        },
        {
            "name": "eval",
            "argv": [
                python,
                "-m",
                "backtest.b3_eval",
                "--data-end",
                DATA_END,
                "--research-output-dir",
                str(research_dir),
                "--backtest-output-dir",
                str(backtest_dir),
            ],
            # 2 = fail-closed run gate with statistics already produced.
            "allowed_exit_codes": (0, 2),
        },
    )


def guarded_command(argv: list[str], time_path: Path) -> list[str]:
    return [
        "systemd-run",
        "--user",
        "--scope",
        "-p",
        f"MemoryMax={MEMORY_MAX}",
        "/usr/bin/time",
        "-v",
        "-o",
        str(time_path),
        *argv,
    ]


def _sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def peak_rss_kib(time_path: Path) -> int | None:
    try:
        text = Path(time_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = _PEAK_RSS_RE.search(text)
    return int(match.group(1)) if match else None


def _write_json_atomic(path: Path, payload: dict) -> str:
    raw = (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str)
        + "\n"
    ).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(raw)
    os.replace(temporary, path)
    return hashlib.sha256(raw).hexdigest()


def run_stages(run_dir: Path, *, python: str = DEFAULT_PYTHON) -> dict:
    """Run the three stages in order and return the execution receipt payload."""

    run_dir = Path(run_dir)
    research_dir = run_dir / "research"
    backtest_dir = run_dir / "backtest"
    logs_dir = run_dir / "logs"
    for directory in (research_dir, backtest_dir, logs_dir):
        directory.mkdir(parents=True, exist_ok=True)

    stages: list[dict] = []
    stopped_at: str | None = None
    for spec in stage_specs(research_dir, backtest_dir, python=python):
        name = spec["name"]
        stdout_path = logs_dir / f"{name}.stdout.log"
        stderr_path = logs_dir / f"{name}.stderr.log"
        time_path = logs_dir / f"{name}.time.txt"
        command = guarded_command(spec["argv"], time_path)

        started = time.monotonic()
        with open(stdout_path, "wb") as out, open(stderr_path, "wb") as err:
            completed = subprocess.run(
                command, stdout=out, stderr=err, cwd=str(STYLE_ROOT), check=False
            )
        wall_seconds = time.monotonic() - started
        exit_code = int(completed.returncode)

        stages.append(
            {
                "name": name,
                "command": command,
                "exit_code": exit_code,
                "allowed_exit_codes": list(spec["allowed_exit_codes"]),
                "wall_seconds": round(wall_seconds, 3),
                "peak_rss_kib": peak_rss_kib(time_path),
                "files": {
                    "stdout": {
                        "path": str(stdout_path),
                        "sha256": _sha256(stdout_path),
                    },
                    "stderr": {
                        "path": str(stderr_path),
                        "sha256": _sha256(stderr_path),
                    },
                    "time": {
                        "path": str(time_path),
                        "sha256": _sha256(time_path),
                    },
                },
            }
        )
        if exit_code not in spec["allowed_exit_codes"]:
            stopped_at = name
            break

    return {
        "schema": "b3-wind-share-capital-execution",
        "version": 1,
        "data_end": DATA_END,
        "memory_max": MEMORY_MAX,
        "run_dir": str(run_dir),
        "research_output_dir": str(research_dir),
        "backtest_output_dir": str(backtest_dir),
        "stage_order": [spec["name"] for spec in stage_specs(research_dir, backtest_dir)],
        "stages": stages,
        "stopped_at": stopped_at,
        "complete": stopped_at is None and len(stages) == 3,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", default=str(CAMPAIGN_DIR / "run"))
    parser.add_argument("--python", default=DEFAULT_PYTHON)
    args = parser.parse_args(argv)

    run_dir = Path(args.run_dir)
    receipt = run_stages(run_dir, python=args.python)
    _write_json_atomic(run_dir / RECEIPT_NAME, receipt)
    print(json.dumps(receipt, ensure_ascii=False, indent=2, default=str))
    return 0 if receipt["complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
