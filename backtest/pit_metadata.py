from __future__ import annotations

import hashlib
from pathlib import Path


PIT_SCHEMA_VERSION = 1
BUILD_SCHEMA_VERSION = 2


def current_pit_metadata(statement_types) -> dict:
    return {
        "schema_version": PIT_SCHEMA_VERSION,
        "periodic_statement_policy": "first_disclosure_else_statutory_deadline",
        "periodic_statement_types": sorted(statement_types),
        "first_disclosure_source": "stock_first_disclosure.first_disclosure_date",
        "first_disclosure_coverage": "partial",
        "fallback_policy": "statutory_deadline",
        "dividend_policy": "event_ann_date_capped_by_statutory_deadline",
        "limitations": [{
            "code": "late_filer_fallback",
            "text": (
                "缺失或无效首披日的定期报告回退法定截止日；超期披露者仍可能被过早"
                "视为可知，因此结论保持 provisional。"
            ),
        }],
    }


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def data_artifact(path: Path, base: Path) -> dict:
    path = Path(path)
    return {
        "path": path.relative_to(base).as_posix(),
        "size": path.stat().st_size,
        "sha256": sha256_file(path),
    }
