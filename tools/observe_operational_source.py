"""Standalone read-only production inventory; can run over SSH via stdin.

No imports from production code, credentials, writes, strategy runs or event creation.
"""
import csv
from datetime import datetime, timezone
import fcntl
import hashlib
import io
import json
from pathlib import Path
import socket
import sys


def observe(root):
    root = Path(root).expanduser()
    observed = datetime.now(timezone.utc).isoformat()
    resolved_before = root.resolve()
    captured = {}
    def read(path):
        with path.open("rb") as stream:
            raw = stream.read(32 * 1024 * 1024 + 1)
        if len(raw) > 32 * 1024 * 1024:
            raise ValueError("source too large")
        captured[path] = raw
        return raw
    with (root/"logs/.daily_signals.lock").open("rb") as lock:
        fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
        status_path = root/"logs/daily_signals_status.json"
        state = json.loads(read(status_path))
        files = []
        for item in state.get("files", {}).values():
            rel = item.get("path", "")
            if not rel.startswith("output/recommended/") or not rel.endswith(".csv") or ".." in Path(rel).parts:
                continue
            rows = list(csv.DictReader(io.StringIO(read(root/rel).decode("utf-8-sig"))))
            files.append({"path": rel, "columns": list(rows[0]) if rows else [],
                          "status_date": item.get("last_date"), "data_date": rows[-1].get("date") if rows else None})
        for path, raw in captured.items():
            if path.read_bytes() != raw:
                raise ValueError("sources changed")
        # Detect deployment symlink changes too.
        if root.resolve() != resolved_before:
            raise ValueError("deployment changed")
        resolved_root = str(resolved_before)
    return {"status": "inspection_only", "event_ready": False,
            "project": "style_timing_signal", "host": socket.gethostname(),
            "root": str(root), "resolved_root": resolved_root, "observed_at": observed,
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "production": {k: state.get(k) for k in ("result", "topup", "started_at", "finished_at")},
            "files": files, "sources": [{"path": str(p), "sha256": hashlib.sha256(raw).hexdigest(),
                                          "bytes": len(raw)} for p, raw in captured.items()]}


if __name__ == "__main__":
    try:
        result = observe(sys.argv[1])
    except Exception:
        print('{"status":"blocked","reason":"SOURCE_MISSING_BUSY_OR_CHANGED","event_ready":false}')
        raise SystemExit(1)
    print(json.dumps(result, ensure_ascii=False, indent=2))
