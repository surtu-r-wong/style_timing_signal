import hashlib
import json
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = Path(__file__).with_name("b3_config.yaml")
EXPECTED_CANDIDATES = ["B3_unified", "B3_dual_target"]
EXPECTED_TOP_LEVEL = {
    "version",
    "candidates",
    "windows",
    "pit",
    "exposure",
    "portfolio",
    "signal",
    "model",
    "execution",
    "production_gates",
    "shadow",
    "bootstrap",
}


def validate_b3_config(cfg):
    if not isinstance(cfg, dict):
        raise ValueError("B3 config must be a mapping")
    if set(cfg) != EXPECTED_TOP_LEVEL:
        raise ValueError("B3 config must contain exactly the expected top-level keys")
    if cfg.get("version") != 1:
        raise ValueError("B3 config version must be 1")
    if cfg.get("candidates") != EXPECTED_CANDIDATES:
        raise ValueError(f"B3 candidates must be exactly {EXPECTED_CANDIDATES}")

    pit = cfg.get("pit")
    expected_policies = ["legal_deadline", "legal_deadline_plus_one_month_end"]
    if not isinstance(pit, dict) or pit.get("policies") != expected_policies:
        raise ValueError(f"B3 PIT policies must be exactly {expected_policies}")

    portfolio = cfg.get("portfolio")
    if not isinstance(portfolio, dict) or portfolio.get("weight_cap") != 0.01:
        raise ValueError("B3 portfolio weight_cap must be 0.01")
    if portfolio.get("min_leg_size") != 100:
        raise ValueError("B3 portfolio min_leg_size must be 100")

    bootstrap = cfg.get("bootstrap")
    if not isinstance(bootstrap, dict) or bootstrap.get("draws") != 5000:
        raise ValueError("B3 bootstrap draws must be 5000")


def load_b3_config(path=CONFIG_PATH):
    cfg = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(cfg, dict):
        raise ValueError("B3 config must be a mapping")
    validate_b3_config(cfg)
    return cfg


def config_hash(cfg):
    canonical = json.dumps(
        cfg,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
