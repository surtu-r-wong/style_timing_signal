from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path


def create_run_dir(root: Path, run_id: str) -> Path:
    candidate = Path(run_id)
    if (
        not candidate.parts
        or candidate == Path(".")
        or candidate.is_absolute()
        or len(candidate.parts) != 1
        or candidate.parts[0] == ".."
    ):
        raise ValueError("run_id must be a single relative path component")
    target = Path(root) / candidate
    target.mkdir(parents=True, exist_ok=False)
    (target / "inputs").mkdir()
    (target / "outputs").mkdir()
    (target / "logs").mkdir()
    return target


def artifact_record(path: Path, base: Path) -> dict:
    path = Path(path)
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return {
        "path": path.relative_to(base).as_posix(),
        "size": path.stat().st_size,
        "sha256": digest.hexdigest(),
    }


def write_manifest(run_dir: Path, payload: dict) -> Path:
    target = Path(run_dir) / "manifest.json"
    temporary = Path(run_dir) / ".manifest.json.tmp"
    rendered = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    temporary.write_text(rendered, encoding="utf-8")
    os.replace(temporary, target)
    return target


def _git_output(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout


def git_state(root: Path) -> dict[str, object]:
    return {
        "commit": _git_output(root, "rev-parse", "HEAD").strip(),
        "dirty": bool(_git_output(root, "status", "--porcelain").strip()),
    }


def query_table_cutoffs(connection, schema: str, contract: dict[str, str]) -> dict[str, str]:
    out = {}
    with connection.cursor() as cursor:
        for table, column in contract.items():
            cursor.execute(f"SELECT max({column})::text FROM {schema}.{table}")
            value = cursor.fetchone()[0]
            if value is None:
                raise RuntimeError(f"{schema}.{table}.{column} has no cutoff")
            out[table] = value
    return out
