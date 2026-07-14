from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from signals.style_basket.b3_config import config_hash, load_b3_config


def test_b3_config_freezes_candidates_windows_and_execution():
    cfg = load_b3_config()

    assert cfg["candidates"] == ["B3_unified", "B3_dual_target"]
    assert cfg["windows"] == {
        "discovery": ["2014-01-01", "2020-12-31"],
        "confirmation": ["2021-01-01", "2023-12-31"],
        "report_only": ["2024-01-01", "2026-12-31"],
    }
    assert cfg["execution"]["cost_bps"] == 3.0
    assert cfg["execution"]["annualization"] == 245
    assert cfg["portfolio"]["weight_cap"] == 0.01
    assert cfg["portfolio"]["min_leg_size"] == 100
    assert cfg["bootstrap"] == {
        "block_days": 20,
        "draws": 5000,
        "seed": 20260713,
        "adjusted_tail_max": 0.10,
    }


def test_b3_config_hash_is_order_independent_and_value_sensitive():
    cfg = load_b3_config()
    reordered = dict(reversed(list(cfg.items())))
    changed = deepcopy(cfg)
    changed["signal"]["z_window"] = 41

    assert config_hash(reordered) == config_hash(cfg)
    assert config_hash(changed) != config_hash(cfg)


def test_b3_config_rejects_candidate_expansion(tmp_path):
    cfg = load_b3_config()
    cfg["candidates"].append("B3_after_the_fact")
    path = Path(tmp_path) / "b3_config.yaml"
    path.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")

    with pytest.raises(ValueError, match="exactly"):
        load_b3_config(path)
