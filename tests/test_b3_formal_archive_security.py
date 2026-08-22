import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from tools.verify_b3_formal_archive import verify_inventory


SCRIPT = Path(__file__).parents[1] / "tools" / "verify_b3_formal_archive.py"


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _item(path="evidence.txt", size=1, sha256=None):
    return {
        "path": path,
        "size": size,
        "sha256": _sha256(b"x") if sha256 is None else sha256,
    }


def _run_cli(inventory: Path, root: Path):
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--inventory",
            str(inventory),
            "--root",
            str(root),
        ],
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize(
    ("inventory", "expected"),
    [
        (None, ["inventory: expected object"]),
        ([], ["inventory: expected object"]),
        ({}, ["core_files: expected non-empty list"]),
        ({"core_files": None}, ["core_files: expected non-empty list"]),
        ({"core_files": []}, ["core_files: expected non-empty list"]),
        ({"core_files": "bad"}, ["core_files: expected non-empty list"]),
        ({"core_files": [None]}, ["core_files[0]: expected object"]),
        ({"core_files": ["bad"]}, ["core_files[0]: expected object"]),
    ],
)
def test_verify_inventory_rejects_malformed_schema(tmp_path, inventory, expected):
    assert verify_inventory(tmp_path, inventory) == expected


@pytest.mark.parametrize(
    "path",
    [
        None,
        "",
        ".",
        "..",
        "../escape",
        "/absolute",
        "a\\b",
        "./a",
        "a//b",
        "a/../b",
        "a/",
    ],
)
def test_verify_inventory_rejects_unsafe_or_noncanonical_paths(tmp_path, path):
    inventory = {"core_files": [_item(path=path)]}
    assert verify_inventory(tmp_path, inventory) == [
        "core_files[0].path: expected non-empty normalized relative POSIX path"
    ]


def test_verify_inventory_rejects_duplicate_normalized_path(tmp_path):
    inventory = {"core_files": [_item(), _item()]}
    assert verify_inventory(tmp_path, inventory) == [
        "core_files[1].path: duplicate normalized path 'evidence.txt'"
    ]


@pytest.mark.parametrize("size", [True, False, -1, 1.5, "1", None])
def test_verify_inventory_rejects_invalid_size(tmp_path, size):
    inventory = {"core_files": [_item(size=size)]}
    assert verify_inventory(tmp_path, inventory) == [
        "core_files[0].size: expected non-negative integer"
    ]


@pytest.mark.parametrize(
    "sha256",
    [
        None,
        "0" * 63,
        "0" * 65,
        "A" * 64,
        "g" * 64,
        0,
    ],
)
def test_verify_inventory_rejects_invalid_sha256(tmp_path, sha256):
    item = _item()
    item["sha256"] = sha256
    inventory = {"core_files": [item]}
    assert verify_inventory(tmp_path, inventory) == [
        "core_files[0].sha256: expected 64 lowercase hexadecimal characters"
    ]


def test_verify_inventory_rejects_symlink_and_intermediate_symlink_escape(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    target = root / "target.txt"
    target.write_bytes(b"x")
    link = root / "link.txt"
    link.symlink_to(target)

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "escaped.txt").write_bytes(b"x")
    (root / "linkdir").symlink_to(outside, target_is_directory=True)

    inventory = {
        "core_files": [
            _item(path="target.txt"),
            _item(path="link.txt"),
            _item(path="linkdir/escaped.txt"),
        ]
    }
    errors = verify_inventory(root, inventory)
    assert "link.txt: symlink not allowed" in errors
    assert "linkdir/escaped.txt: symlink not allowed" in errors


def test_verify_inventory_rejects_directory_and_non_regular_file(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "directory").mkdir()
    os.mkfifo(root / "fifo")
    inventory = {
        "core_files": [
            _item(path="directory", size=0, sha256=_sha256(b"")),
            _item(path="fifo", size=0, sha256=_sha256(b"")),
        ]
    }
    assert verify_inventory(root, inventory) == [
        "directory: not a regular file",
        "fifo: not a regular file",
    ]


def test_verify_inventory_rejects_unlisted_regular_file_and_symlink(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    listed = root / "listed.txt"
    listed.write_bytes(b"x")
    (root / "extra.txt").write_bytes(b"extra")
    (root / "extra-link").symlink_to(listed)
    inventory = {"core_files": [_item(path="listed.txt")]}
    assert verify_inventory(root, inventory) == [
        "extra-link: unlisted entry",
        "extra.txt: unlisted entry",
    ]


def test_verify_inventory_rejects_unlisted_non_regular_file(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "listed.txt").write_bytes(b"x")
    os.mkfifo(root / "extra-fifo")
    inventory = {"core_files": [_item(path="listed.txt")]}
    assert verify_inventory(root, inventory) == ["extra-fifo: unlisted entry"]


def test_verify_inventory_fails_closed_on_unreadable_subtree(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "listed.txt").write_bytes(b"x")
    locked = root / "locked"
    locked.mkdir()
    (locked / "extra.txt").write_bytes(b"extra")
    locked.chmod(0)
    inventory = {"core_files": [_item(path="listed.txt")]}
    try:
        errors = verify_inventory(root, inventory)
    finally:
        locked.chmod(0o700)
    assert errors == ["locked: inaccessible directory"]


def test_cli_reports_malformed_json_without_traceback(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    inventory = tmp_path / "inventory.json"
    inventory.write_text("{", encoding="utf-8")
    result = _run_cli(inventory, root)
    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr == "ERROR: inventory is not valid JSON\n"
    assert "Traceback" not in result.stderr


def test_cli_reports_invalid_utf8_without_traceback(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    inventory = tmp_path / "inventory.json"
    inventory.write_bytes(b"\xff")
    result = _run_cli(inventory, root)
    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr == "ERROR: inventory is not valid UTF-8\n"
    assert "Traceback" not in result.stderr


def test_cli_reports_missing_inventory_without_traceback(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    inventory = tmp_path / "missing.json"
    result = _run_cli(inventory, root)
    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr == f"ERROR: cannot read inventory: {inventory}\n"
    assert "Traceback" not in result.stderr


def test_cli_reports_schema_error_without_traceback(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    inventory = tmp_path / "inventory.json"
    inventory.write_text(json.dumps({"core_files": []}), encoding="utf-8")
    result = _run_cli(inventory, root)
    assert result.returncode == 1
    assert result.stdout == "core_files: expected non-empty list\n"
    assert result.stderr == ""
    assert "Traceback" not in result.stdout
