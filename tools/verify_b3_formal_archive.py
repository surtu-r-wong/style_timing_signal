from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys


_PATH_ERROR = "expected non-empty normalized relative POSIX path"
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


def _valid_relative_posix_path(value: object) -> bool:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        return False
    path = PurePosixPath(value)
    parts = value.split("/")
    return (
        not path.is_absolute()
        and all(part not in {"", ".", ".."} for part in parts)
        and path.as_posix() == value
    )


def _validate_inventory(inventory: object) -> tuple[list[dict], list[str]]:
    if not isinstance(inventory, dict):
        return [], ["inventory: expected object"]
    raw_items = inventory.get("core_files")
    if not isinstance(raw_items, list) or not raw_items:
        return [], ["core_files: expected non-empty list"]
    validated = []
    errors = []
    seen_paths = set()
    for index, item in enumerate(raw_items):
        prefix = f"core_files[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{prefix}: expected object")
            continue
        raw_path = item.get("path")
        if not _valid_relative_posix_path(raw_path):
            errors.append(f"{prefix}.path: {_PATH_ERROR}")
            continue
        normalized = PurePosixPath(raw_path).as_posix()
        if normalized in seen_paths:
            errors.append(f"{prefix}.path: duplicate normalized path '{normalized}'")
            continue
        seen_paths.add(normalized)
        size = item.get("size")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            errors.append(f"{prefix}.size: expected non-negative integer")
            continue
        sha256 = item.get("sha256")
        if not isinstance(sha256, str) or _SHA256_RE.fullmatch(sha256) is None:
            errors.append(f"{prefix}.sha256: expected 64 lowercase hexadecimal characters")
            continue
        validated.append({"path": normalized, "size": size, "sha256": sha256})
    return validated, errors


def _root_directory(root: Path) -> tuple[Path | None, list[str]]:
    try:
        root = Path(root)
        root_stat = root.lstat()
    except (OSError, TypeError, ValueError):
        return None, ["root: missing or inaccessible directory"]
    if stat.S_ISLNK(root_stat.st_mode):
        return None, ["root: symlink not allowed"]
    if not stat.S_ISDIR(root_stat.st_mode):
        return None, ["root: not a directory"]
    return root.resolve(), []


def _inspect_listed_file(root: Path, item: dict) -> list[str]:
    relative = item["path"]
    current = root
    final_stat = None
    parts = PurePosixPath(relative).parts
    for index, part in enumerate(parts):
        current = current / part
        try:
            current_stat = current.lstat()
        except (FileNotFoundError, NotADirectoryError):
            return [f"{relative}: missing"]
        except OSError:
            return [f"{relative}: inaccessible"]
        if stat.S_ISLNK(current_stat.st_mode):
            return [f"{relative}: symlink not allowed"]
        if index < len(parts) - 1 and not stat.S_ISDIR(current_stat.st_mode):
            return [f"{relative}: missing"]
        final_stat = current_stat
    if final_stat is None or not stat.S_ISREG(final_stat.st_mode):
        return [f"{relative}: not a regular file"]
    if final_stat.st_size != item["size"]:
        return [f"{relative}: size mismatch"]
    try:
        digest = hashlib.sha256()
        with current.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return [f"{relative}: inaccessible"]
    if digest.hexdigest() != item["sha256"]:
        return [f"{relative}: sha256 mismatch"]
    return []


def _relative_label(root: Path, path: object) -> str:
    try:
        relative = Path(path).relative_to(root).as_posix()
    except (TypeError, ValueError):
        return str(path)
    return "root" if relative == "." else relative


def _actual_file_and_symlink_paths(root: Path) -> tuple[set[str], list[str]]:
    entries = set()
    errors = set()

    def record_walk_error(error: OSError) -> None:
        errors.add(
            f"{_relative_label(root, error.filename)}: inaccessible directory"
        )

    for directory, dirnames, filenames in os.walk(
        root, topdown=True, onerror=record_walk_error, followlinks=False
    ):
        directory_path = Path(directory)
        traversable = []
        for name in sorted(dirnames):
            path = directory_path / name
            try:
                mode = path.lstat().st_mode
            except OSError:
                errors.add(f"{_relative_label(root, path)}: inaccessible entry")
                continue
            if stat.S_ISLNK(mode):
                entries.add(path.relative_to(root).as_posix())
            else:
                traversable.append(name)
        dirnames[:] = traversable
        for name in sorted(filenames):
            path = directory_path / name
            try:
                path.lstat()
            except OSError:
                errors.add(f"{_relative_label(root, path)}: inaccessible entry")
                continue
            entries.add(path.relative_to(root).as_posix())
    return entries, sorted(errors)


def verify_inventory(root: Path, inventory: dict) -> list[str]:
    items, errors = _validate_inventory(inventory)
    if errors:
        return errors
    checked_root, root_errors = _root_directory(root)
    if root_errors:
        return root_errors
    assert checked_root is not None
    for item in items:
        errors.extend(_inspect_listed_file(checked_root, item))
    listed_paths = {item["path"] for item in items}
    actual_paths, scan_errors = _actual_file_and_symlink_paths(checked_root)
    errors.extend(scan_errors)
    for extra in sorted(actual_paths - listed_paths):
        errors.append(f"{extra}: unlisted entry")
    return errors


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        payload = args.inventory.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        print("ERROR: inventory is not valid UTF-8", file=sys.stderr)
        return 2
    except OSError:
        print(f"ERROR: cannot read inventory: {args.inventory}", file=sys.stderr)
        return 2
    try:
        inventory = json.loads(payload)
    except json.JSONDecodeError:
        print("ERROR: inventory is not valid JSON", file=sys.stderr)
        return 2
    errors = verify_inventory(args.root, inventory)
    if errors:
        print("\n".join(errors))
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
