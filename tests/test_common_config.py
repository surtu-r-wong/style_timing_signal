import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from signals.common.config import load_db_config  # noqa: E402


def test_load_db_config_reads_yaml(tmp_path):
    f = tmp_path / "settings.yaml"
    f.write_text(
        "database:\n  host: 1.2.3.4\n  port: 5432\n  name: market_monitor\n"
        "  user: admin\n  password: x\n  schema: stock_selector\n",
        encoding="utf-8",
    )
    db = load_db_config(f)
    assert db["host"] == "1.2.3.4"
    assert db["schema"] == "stock_selector"


def test_load_db_config_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError, match="settings.example.yaml"):
        load_db_config(tmp_path / "nope.yaml")


def test_load_db_config_missing_key_raises(tmp_path):
    f = tmp_path / "settings.yaml"
    f.write_text("database:\n  host: 1.2.3.4\n", encoding="utf-8")
    with pytest.raises(ValueError, match="port"):
        load_db_config(f)


def test_load_db_config_empty_value_raises(tmp_path):
    f = tmp_path / "settings.yaml"
    f.write_text(
        "database:\n  host: 1.2.3.4\n  port: 5432\n  name: market_monitor\n"
        "  user: admin\n  password:\n  schema: stock_selector\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="password"):
        load_db_config(f)
