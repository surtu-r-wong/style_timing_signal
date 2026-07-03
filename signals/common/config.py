"""读取 config/settings.yaml 的数据库连接配置（gitignored，模板见 settings.example.yaml）。"""
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
CONFIG_FILE = ROOT / "config" / "settings.yaml"
_REQUIRED = ("host", "port", "name", "user", "password", "schema")


def load_db_config(config_file: str | Path = CONFIG_FILE) -> dict:
    config_file = Path(config_file)
    if not config_file.exists():
        raise FileNotFoundError(
            f"{config_file} 不存在：复制 config/settings.example.yaml 为 "
            f"config/settings.yaml 并填入数据库连接信息"
        )
    cfg = yaml.safe_load(config_file.read_text(encoding="utf-8")) or {}
    db = cfg.get("database") or {}
    missing = [k for k in _REQUIRED if db.get(k) in (None, "")]
    if missing:
        raise ValueError(f"settings.yaml database 段缺少字段或值为空: {missing}")
    return db
