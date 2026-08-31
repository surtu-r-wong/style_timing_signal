from __future__ import annotations

import argparse
import difflib
import os
from pathlib import Path, PurePosixPath
import sys

import yaml


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REGISTRY = ROOT / "docs" / "plans" / "research_registry.yaml"
DEFAULT_README = ROOT / "docs" / "plans" / "README.md"
STATUSES = {"adopted", "closed", "provisional", "research_only", "open", "blocked"}
OUTCOMES = {
    "selected",
    "pass",
    "stop",
    "all_fail",
    "data_blocked",
    "descriptive",
    "pending",
}
EVIDENCE_LEVELS = {
    "immutable_formal_run",
    "legacy_formal_archive",
    "committed_formal_probe",
    "committed_prescreen",
    "exploratory",
}
DOCUMENT_KEYS = ("spec", "report", "evidence")
START = "<!-- research-registry:start -->"
END = "<!-- research-registry:end -->"


def load_registry(path: Path = DEFAULT_REGISTRY) -> dict:
    try:
        payload = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ValueError(f"cannot load registry: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("registry top level must be a mapping")
    return payload


def _repo_path(value: object) -> str | None:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        return None
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        return None
    return path.as_posix()


def _nonempty(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _cycle_errors(graph: dict[str, list[str]], label: str) -> list[str]:
    errors: list[str] = []
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str, trail: tuple[str, ...]) -> None:
        if node in visiting:
            errors.append(f"{label} cycle: " + " -> ".join((*trail, node)))
            return
        if node in visited:
            return
        visiting.add(node)
        for target in graph.get(node, []):
            visit(target, (*trail, node))
        visiting.remove(node)
        visited.add(node)

    for node in sorted(graph):
        visit(node, ())
    return sorted(set(errors))


def _inventory_errors(inventory: object, root: Path, referenced: set[str]) -> list[str]:
    if not isinstance(inventory, dict) or set(inventory) != {"include_globs", "exclusions"}:
        return ["inventory: expected include_globs and exclusions"]
    globs = inventory["include_globs"]
    exclusions = inventory["exclusions"]
    if not isinstance(globs, list) or not globs or any(not _nonempty(item) for item in globs):
        return ["inventory.include_globs: expected non-empty string list"]
    if not isinstance(exclusions, list):
        return ["inventory.exclusions: expected list"]

    errors: list[str] = []
    excluded: set[str] = set()
    for index, item in enumerate(exclusions):
        prefix = f"inventory.exclusions[{index}]"
        if not isinstance(item, dict) or set(item) != {"path", "reason"}:
            errors.append(f"{prefix}: expected path and reason")
            continue
        path = _repo_path(item["path"])
        if path is None or not _nonempty(item["reason"]):
            errors.append(f"{prefix}: invalid path or empty reason")
            continue
        if path in excluded:
            errors.append(f"{prefix}.path: duplicate '{path}'")
        elif not (root / path).is_file():
            errors.append(f"{prefix}.path: missing '{path}'")
        excluded.add(path)

    discovered: set[str] = set()
    for pattern in globs:
        if Path(pattern).is_absolute() or ".." in PurePosixPath(pattern).parts:
            errors.append(f"inventory.include_globs: unsafe pattern '{pattern}'")
            continue
        discovered.update(
            path.relative_to(root).as_posix()
            for path in root.glob(pattern)
            if path.is_file()
        )
    for path in sorted(referenced & excluded):
        errors.append(f"inventory: '{path}' is both referenced and excluded")
    for path in sorted(discovered - referenced - excluded):
        errors.append(f"inventory: unregistered document '{path}'")
    for path in sorted((referenced | excluded) - discovered):
        if path.startswith("docs/plans/2026-") and path.endswith(".md"):
            errors.append(f"inventory: disposition outside discovery set '{path}'")
    return errors


def validate_registry(
    payload: object,
    root: Path = ROOT,
    *,
    check_inventory: bool = True,
) -> list[str]:
    errors: list[str] = []
    if not isinstance(payload, dict):
        return ["registry: expected mapping"]
    if payload.get("schema_version") != 1:
        errors.append("schema_version: expected 1")
    studies = payload.get("studies")
    if not isinstance(studies, list) or not studies:
        return errors + ["studies: expected non-empty list"]

    ids: list[str] = []
    paths: set[str] = set()
    dependency_graph: dict[str, list[str]] = {}
    supersedes_graph: dict[str, list[str]] = {}
    required_text = ("id", "title", "family", "scope", "claim", "reopen_condition")
    for index, study in enumerate(studies):
        prefix = f"studies[{index}]"
        if not isinstance(study, dict):
            errors.append(f"{prefix}: expected mapping")
            continue
        for key in required_text:
            if not _nonempty(study.get(key)):
                errors.append(f"{prefix}.{key}: expected non-empty string")
        sid = study.get("id")
        if isinstance(sid, str):
            if sid in ids:
                errors.append(f"{prefix}.id: duplicate id '{sid}'")
            ids.append(sid)
        if study.get("status") not in STATUSES:
            errors.append(f"{prefix}.status: invalid value")
        if study.get("outcome") not in OUTCOMES:
            errors.append(f"{prefix}.outcome: invalid value")
        if study.get("evidence_level") not in EVIDENCE_LEVELS:
            errors.append(f"{prefix}.evidence_level: invalid value")
        for key in ("non_claims", "caveats", "supersedes", "depends_on"):
            value = study.get(key)
            if not isinstance(value, list) or any(not _nonempty(item) for item in value):
                errors.append(f"{prefix}.{key}: expected string list")
        if study.get("status") in {"adopted", "closed", "provisional"}:
            if not study.get("non_claims"):
                errors.append(f"{prefix}.non_claims: expected non-empty list")
            if not study.get("caveats"):
                errors.append(f"{prefix}.caveats: expected non-empty list")
        production = study.get("production")
        if (
            not isinstance(production, dict)
            or set(production) != {"affects", "role"}
            or type(production.get("affects")) is not bool
            or not _nonempty(production.get("role"))
        ):
            errors.append(f"{prefix}.production: expected affects(bool) and role(str)")
        documents = study.get("documents")
        if not isinstance(documents, dict) or set(documents) != set(DOCUMENT_KEYS):
            errors.append(f"{prefix}.documents: expected keys {DOCUMENT_KEYS}")
        else:
            for key in DOCUMENT_KEYS:
                values = documents[key]
                if not isinstance(values, list):
                    errors.append(f"{prefix}.documents.{key}: expected list")
                    continue
                for value in values:
                    normalized = _repo_path(value)
                    if normalized is None:
                        errors.append(f"{prefix}.documents.{key}: invalid repo path")
                    elif not (Path(root) / normalized).is_file():
                        errors.append(f"{prefix}.documents.{key}: missing '{normalized}'")
                    else:
                        paths.add(normalized)
            if (
                study.get("status") in {"adopted", "closed", "provisional"}
                and not documents["evidence"]
            ):
                errors.append(f"{prefix}.documents.evidence: expected non-empty list")
        if isinstance(sid, str):
            dependencies = study.get("depends_on", [])
            supersedes = study.get("supersedes", [])
            dependency_graph[sid] = dependencies if isinstance(dependencies, list) else []
            supersedes_graph[sid] = supersedes if isinstance(supersedes, list) else []

    known = set(ids)
    for relation, graph in (
        ("depends_on", dependency_graph),
        ("supersedes", supersedes_graph),
    ):
        for sid, targets in graph.items():
            for target in targets:
                if target not in known:
                    errors.append(f"study '{sid}': unknown {relation} target '{target}'")
    errors.extend(_cycle_errors(dependency_graph, "dependency"))
    errors.extend(_cycle_errors(supersedes_graph, "supersedes"))
    if check_inventory:
        errors.extend(_inventory_errors(payload.get("inventory"), Path(root), paths))
    return sorted(set(errors))


def _md(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def _relative_link(repo_path: str, readme_path: Path) -> str:
    target = ROOT / repo_path
    return Path(os.path.relpath(target, readme_path.parent)).as_posix()


def render_readme(payload: dict, readme_path: Path = DEFAULT_README) -> str:
    rows = [
        "<!-- generated by tools/research_registry.py; do not edit this block manually -->",
        "| ID | 状态 | 精确范围与当前结论 | 限制/不得外推 | 重开条件 | 权威证据 |",
        "|---|---|---|---|---|---|",
    ]
    for study in payload["studies"]:
        links = "；".join(
            f"[{Path(path).name}]({_relative_link(path, readme_path)})"
            for path in study["documents"]["evidence"]
        )
        rows.append("| " + " | ".join([
            f"`{_md(study['id'])}`",
            f"{_md(study['status'])} / {_md(study['outcome'])}",
            _md(study["scope"] + "；" + study["claim"]),
            _md("；".join((*study["non_claims"], *study["caveats"]))),
            _md(study["reopen_condition"]),
            links,
        ]) + " |")
    return "\n".join(rows) + "\n"


def replace_generated_block(text: str, block: str) -> str:
    if text.count(START) != 1 or text.count(END) != 1 or text.index(START) > text.index(END):
        raise ValueError("README must contain exactly one ordered registry marker pair")
    before, tail = text.split(START, 1)
    _, after = tail.split(END, 1)
    return before + START + "\n" + block + END + after


def _validated_payload(registry_path: Path) -> tuple[dict | None, int]:
    try:
        payload = load_registry(registry_path)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return None, 2
    errors = validate_registry(payload, ROOT)
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return None, 1
    return payload, 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate and render the research registry")
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--readme", type=Path, default=DEFAULT_README)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("validate")
    render_parser = subparsers.add_parser("render")
    mode = render_parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--write", action="store_true")
    args = parser.parse_args(argv)

    payload, exit_code = _validated_payload(args.registry)
    if payload is None:
        return exit_code
    if args.command == "validate":
        return 0

    try:
        current = args.readme.read_text(encoding="utf-8")
        expected = replace_generated_block(current, render_readme(payload, args.readme))
    except (OSError, UnicodeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.check:
        if current == expected:
            return 0
        diff = difflib.unified_diff(
            current.splitlines(), expected.splitlines(),
            fromfile=str(args.readme), tofile=f"{args.readme} (registry render)",
            lineterm="",
        )
        print("\n".join(diff), file=sys.stderr)
        return 1
    args.readme.write_text(expected, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
