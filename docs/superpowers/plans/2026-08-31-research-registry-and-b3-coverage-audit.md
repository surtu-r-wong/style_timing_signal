# Research Registry and B3 Coverage Audit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立可校验、可渲染的研究结论台账，修正过度关账和陈旧状态，并提供与冻结 B3 coverage 口径完全一致的只读真首披覆盖审计。

**Architecture:** `docs/plans/research_registry.yaml` 是研究状态唯一事实源，`tools/research_registry.py` 负责 schema/清单校验及 README 受控区块渲染。`tools/audit_b3_disclosure_coverage.py` 只消费现有 B3 `monthly_exposures`，以 `backtest.b3_eval.compute_true_disclosure_coverage` 为唯一总体口径。`backtest/pit_metadata.py` 在 pure-style builders 与 formal verdicts 之间建立带输入哈希的结构化 PIT 元数据契约，历史不可变 run 不改。

**Tech Stack:** Python 3、PyYAML、pandas、pytest、现有 `backtest.run_manifest` provenance 工具、Markdown/YAML。

---

## 实施前约束

- 设计规格：`docs/superpowers/specs/2026-08-31-research-registry-and-b3-coverage-audit-design.md`。
- 在隔离 worktree 中执行；不要暂存或改动主工作区当前 8 个 `output/**/*.csv` 用户变更。
- 本计划不连接数据库，不跑正式 B3，不重写 `backtest/output/runs/` 或 `data_fixes/**/archive`。
- 每项任务坚持 RED → GREEN → focused tests → commit；不得先写实现再补测试。

## 文件职责

- Create: `tools/research_registry.py` — YAML 读取、状态/证据/路径/依赖/文档清单校验，README 受控区块渲染与 CLI。
- Create: `docs/plans/research_registry.yaml` — 唯一研究状态事实源及全部 `docs/plans/2026-*.md` 的 disposition。
- Modify: `docs/plans/README.md` — 保留人工说明，实验表改成受控生成区块。
- Create: `tests/test_research_registry.py` — 台账不变量、完整清单和渲染保护测试。
- Create: `tools/audit_b3_disclosure_coverage.py` — B3 coverage audit 纯函数与 CLI。
- Create: `tests/test_b3_disclosure_coverage_audit.py` — 总体口径、分组、异常与 CLI 产物测试。
- Create: `backtest/pit_metadata.py` — pure-style PIT 元数据生成、build/data 绑定和 verdict caveat 读取。
- Modify: `backtest/tail_pair_runner.py` — 在新 build metadata 中写 PIT contract 和数据文件哈希。
- Modify: `backtest/geometric_pairs_runner.py` — 同上。
- Modify: `backtest/fifth_bucket_formal.py` — 校验并消费相邻 tail build metadata，不再硬编码旧 caveat。
- Modify: `backtest/geometric_5b_formal.py` — 校验并消费相邻 geometric build metadata，不再硬编码旧 caveat。
- Modify: `tests/test_bt_p0_revalidation.py` — builder/verdict 元数据契约回归测试。
- Modify: `docs/plans/2026-08-13-probe-1b-mapping-grid.md` — 限缩“稳健最优”的外推范围。
- Modify: `docs/plans/2026-08-26-clean-evidence-revalidation-execution.md` — 修正已经合并后的分支状态。
- Modify: `docs/plans/2026-08-26-signal-generator-prescreens.md` — 限缩固定配置 prescreen 的关账范围并修正 dual-channel 状态。

### Task 1: 台账 schema 与 fail-closed 校验器

**Files:**
- Create: `tests/test_research_registry.py`
- Create: `tools/research_registry.py`

- [ ] **Step 1: 写最小合法 payload 和状态不变量失败测试**

在 `tests/test_research_registry.py` 先加入以下测试骨架；每个错误都要求稳定、可定位的消息，而不是只断言“抛异常”：

```python
from pathlib import Path

import pytest
import yaml

from tools.research_registry import load_registry, validate_registry


def valid_payload():
    return {
        "schema_version": 1,
        "inventory": {"include_globs": ["docs/plans/2026-*.md"], "exclusions": []},
        "studies": [{
            "id": "mapping-grid",
            "title": "持仓映射 32 格",
            "family": "position-mapping",
            "status": "closed",
            "outcome": "stop",
            "evidence_level": "committed_formal_probe",
            "scope": "冻结的 4×2×4 网格、2014-2023 选择窗及三项部署门槛",
            "claim": "网格内没有候选同时通过三项门槛。",
            "non_claims": ["不证明所有持仓映射均劣于现役。"],
            "caveats": ["赢家的 Sharpe 侧改善以更深回撤和更高换手为代价。"],
            "reopen_condition": "网格外候选须重新预登记。",
            "production": {"affects": True, "role": "incumbent_unchanged"},
            "documents": {
                "spec": [],
                "report": ["docs/plans/2026-08-13-probe-1b-mapping-grid.md"],
                "evidence": ["backtest/output/probe_1b_mapping_verdict.csv"],
            },
            "supersedes": [],
            "depends_on": [],
        }],
    }


def test_closed_study_requires_exact_scope_and_evidence(tmp_path):
    payload = valid_payload()
    payload["studies"][0]["scope"] = ""
    payload["studies"][0]["documents"]["evidence"] = []
    errors = validate_registry(payload, tmp_path, check_inventory=False)
    assert "studies[0].scope: expected non-empty string" in errors
    assert "studies[0].documents.evidence: expected non-empty list" in errors


@pytest.mark.parametrize("status", ["provisional", "blocked"])
def test_nonterminal_status_requires_reopen_condition(status, tmp_path):
    payload = valid_payload()
    payload["studies"][0]["status"] = status
    payload["studies"][0]["reopen_condition"] = ""
    errors = validate_registry(payload, tmp_path, check_inventory=False)
    assert "studies[0].reopen_condition: expected non-empty string" in errors


def test_registry_rejects_duplicate_ids(tmp_path):
    payload = valid_payload()
    second = dict(payload["studies"][0])
    payload["studies"] = [payload["studies"][0], second]
    errors = validate_registry(payload, tmp_path, check_inventory=False)
    assert any("duplicate id 'mapping-grid'" in error for error in errors)


def test_registry_rejects_unknown_relations(tmp_path):
    payload = valid_payload()
    payload["studies"][0]["depends_on"] = ["missing-study"]
    payload["studies"][0]["supersedes"] = ["missing-predecessor"]
    errors = validate_registry(payload, tmp_path, check_inventory=False)
    assert "study 'mapping-grid': unknown depends_on target 'missing-study'" in errors
    assert "study 'mapping-grid': unknown supersedes target 'missing-predecessor'" in errors


@pytest.mark.parametrize("relation, label", [
    ("depends_on", "dependency"),
    ("supersedes", "supersedes"),
])
def test_registry_rejects_relation_cycles(relation, label, tmp_path):
    payload = valid_payload()
    second = {
        **payload["studies"][0],
        "id": "second-study",
        "documents": {"spec": [], "report": [], "evidence": []},
    }
    payload["studies"][0][relation] = ["second-study"]
    second[relation] = ["mapping-grid"]
    payload["studies"].append(second)
    errors = validate_registry(payload, tmp_path, check_inventory=False)
    assert any(error.startswith(f"{label} cycle:") for error in errors)


def test_load_registry_rejects_non_mapping_yaml(tmp_path):
    path = tmp_path / "registry.yaml"
    path.write_text("- not-a-mapping\n", encoding="utf-8")
    with pytest.raises(ValueError, match="top level must be a mapping"):
        load_registry(path)
```

- [ ] **Step 2: 运行测试并确认 RED**

Run:

```bash
/home/elfbob/miniconda3/bin/python -m pytest tests/test_research_registry.py -q
```

Expected: collection fails with `ModuleNotFoundError: No module named 'tools.research_registry'`。

- [ ] **Step 3: 实现 schema、路径和图校验的最小核心**

在 `tools/research_registry.py` 定义稳定公开接口：

```python
from __future__ import annotations

import argparse
from pathlib import Path, PurePosixPath
import sys

import yaml


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REGISTRY = ROOT / "docs" / "plans" / "research_registry.yaml"
DEFAULT_README = ROOT / "docs" / "plans" / "README.md"
STATUSES = {"adopted", "closed", "provisional", "research_only", "open", "blocked"}
OUTCOMES = {"selected", "pass", "stop", "all_fail", "data_blocked", "descriptive", "pending"}
EVIDENCE_LEVELS = {
    "immutable_formal_run",
    "legacy_formal_archive",
    "committed_formal_probe",
    "committed_prescreen",
    "exploratory",
}
DOCUMENT_KEYS = ("spec", "report", "evidence")


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
    errors, visiting, visited = [], set(), set()

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


def validate_registry(payload: object, root: Path = ROOT, *, check_inventory: bool = True) -> list[str]:
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
            if study.get("status") in {"adopted", "closed", "provisional"} and not documents["evidence"]:
                errors.append(f"{prefix}.documents.evidence: expected non-empty list")
        if isinstance(sid, str):
            dependency_graph[sid] = list(study.get("depends_on", []))
            supersedes_graph[sid] = list(study.get("supersedes", []))

    known = set(ids)
    for relation, graph in (("depends_on", dependency_graph), ("supersedes", supersedes_graph)):
        for sid, targets in graph.items():
            for target in targets:
                if target not in known:
                    errors.append(f"study '{sid}': unknown {relation} target '{target}'")
    errors.extend(_cycle_errors(dependency_graph, "dependency"))
    errors.extend(_cycle_errors(supersedes_graph, "supersedes"))
    if check_inventory:
        errors.extend(_inventory_errors(payload.get("inventory"), Path(root), paths))
    return sorted(set(errors))
```
YAML 只存 canonical 的出向 `supersedes` 和 `depends_on`。validator/renderer 反向推导 `superseded_by` 视图，不在 YAML 重复维护正反关系；两个出向图分别检查未知 ID 和有向环。


同文件实现 `_inventory_errors`；不要自动从标题或正文猜状态：

```python
def _inventory_errors(inventory: object, root: Path, referenced: set[str]) -> list[str]:
    if not isinstance(inventory, dict) or set(inventory) != {"include_globs", "exclusions"}:
        return ["inventory: expected include_globs and exclusions"]
    globs = inventory["include_globs"]
    exclusions = inventory["exclusions"]
    if not isinstance(globs, list) or not globs or any(not _nonempty(item) for item in globs):
        return ["inventory.include_globs: expected non-empty string list"]
    if not isinstance(exclusions, list):
        return ["inventory.exclusions: expected list"]

    errors, excluded = [], set()
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

    discovered = set()
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
```

- [ ] **Step 4: 运行 focused tests 并补齐边界**

Run:

```bash
/home/elfbob/miniconda3/bin/python -m pytest tests/test_research_registry.py -q
```

Expected: 当前 Task 1 测试全部 PASS。

- [ ] **Step 5: 提交校验器核心**

```bash
git add tools/research_registry.py tests/test_research_registry.py
git commit -m "feat: validate research registry contracts"
```

### Task 2: 建立完整台账、文档清单与 README 受控渲染

**Files:**
- Create: `docs/plans/research_registry.yaml`
- Modify: `tools/research_registry.py`
- Modify: `tests/test_research_registry.py`
- Modify: `docs/plans/README.md`

- [ ] **Step 1: 写 repository-level inventory 和渲染保护失败测试**

追加测试：

```python
from tools.research_registry import render_readme, replace_generated_block


ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "docs" / "plans" / "research_registry.yaml"
README = ROOT / "docs" / "plans" / "README.md"


def test_repository_registry_is_valid_and_covers_decision_documents():
    payload = load_registry(REGISTRY)
    assert validate_registry(payload, ROOT) == []


def test_readme_is_exact_registry_render():
    payload = load_registry(REGISTRY)
    current = README.read_text(encoding="utf-8")
    assert replace_generated_block(current, render_readme(payload, README)) == current


def test_generated_block_preserves_manual_text_outside_markers():
    original = "prefix\n<!-- research-registry:start -->\nold\n<!-- research-registry:end -->\nsuffix\n"
    got = replace_generated_block(original, "new\n")
    assert got == "prefix\n<!-- research-registry:start -->\nnew\n<!-- research-registry:end -->\nsuffix\n"
```

- [ ] **Step 2: 运行测试并确认 RED**

Run:

```bash
/home/elfbob/miniconda3/bin/python -m pytest tests/test_research_registry.py -q
```

Expected: missing registry/render functions 或 README marker assertion FAIL。

- [ ] **Step 3: 写 canonical registry，按真实证据强度逐项登记**

YAML 顶层固定为：

```yaml
schema_version: 1
inventory:
  include_globs:
    - docs/plans/2026-*.md
  exclusions: []
studies: []
```

初始 studies 不得少于下表；同一行可以引用多个 prereg/report，但不能把不同 scope 合成一个无限外推结论：

| ID | status / outcome | 必须表达的边界 |
|---|---|---|
| `production-equal-weight-long-flat` | adopted / selected | 采用理由是回撤、换手和风险偏好；①a Sharpe 差不显著 |
| `mapping-grid-32` | closed / stop | 只关冻结 32 格和三门槛；不是所有映射的全局最优 |
| `divergence-probe` | closed / stop | 只排除预登记强度与网格内的二阶增量 |
| `fusion-slope20` | closed / stop | 关闭指定融合候选，不否定未来新增独立信息源 |
| `pair-set` | closed / stop | 关闭已测配对子集与 dividend partner 候选 |
| `conditional-modulation` | closed / stop | 不得引成“状态无关” |
| `fund-crowding` | closed / stop | 关闭预登记拥挤候选 |
| `mian2-cross-section` | closed / stop | 关闭指定横截面候选 |
| `dual-channel` | closed / stop | 两种正式秤均 STOP；不得写“换执行标的无价值” |
| `staged-entry` | closed / stop | 关闭已测分批建仓规格 |
| `family-unification` | closed / stop | 限定已测 family unification 规格 |
| `threshold-and-microcap-grids` | closed / stop | 限定已测阈值/联合网格 |
| `tail-fifth-bucket` | closed / stop | 当前架构下增量不可辨认，不等于尾部无信息 |
| `geometric-five-bucket` | closed / stop | 当前等比全替换候选不可辨认，不等于重划无价值 |
| `axes-batch-1` | closed / all_fail | 低波/动量/流动性/股息在冻结同窗和功效下 ALL_FAIL |
| `axes-batch-2-quality` | closed / all_fail | 质量轴同上 |
| `dual-engine-v1` | closed / stop | 关闭已测 dual-engine 候选，不替代后续 dual-channel 的独立裁决 |
| `breadth-divergence` | closed / stop | 只关闭已测 breadth/divergence 规格与方向 |
| `leverage-probe` | closed / stop | 关闭两融轴冻结候选，不外推为“两融数据永远无效” |
| `thermo-probe` | closed / stop | 关闭涨跌停/市场温度冻结候选 |
| `rotation-short-window` | closed / stop | 关闭 B2 派生的 rotation 短窗命题 |
| `long-axes-probes` | closed / all_fail | 分开列明广度多头、ERP 等已测 scope；C1 基差另存 blocked 记录 |
| `momentum-transform` | closed / stop | 冻结扫描下替代候选不过换主信号门槛，保留已登记第一替补语义 |
| `yearly-concentration` | research_only / descriptive | 集中度是现役与未来挑战者的常规诊断，不是独立 GO/STOP 候选 |
| `basis-c1-retest` | blocked / pending | `futures_daily` 新鲜度恢复并增长样本后新预登记复检 |
| `adaptive-bucket` | research_only / descriptive | 无正式 verdict，不进生产 |
| `mixed-ensemble` | research_only / descriptive | 无正式 verdict，不进生产 |
| `rotation-target` | research_only / descriptive | 无正式 verdict，不进生产 |
| `signal-generator-ewma-std` | closed / stop | 只关闭指定 EWMA-std 配置并降低线性滤波再参数化优先级 |
| `signal-generator-cusum-hamilton` | closed / stop | 只关闭固定 CUSUM/Hamilton 规格；ML 未实测，不能宣称实证关闭 |
| `signal-generator-smoothing` | closed / stop | 关闭 raw/DEMA(5)/DEMA(7) 插点范围 |
| `b1-style-replication` | closed / pass | 复现目标完成 |
| `b2-industry-neutral` | closed / stop | 测量分解完成，切换生产主信号不过闸 |
| `b3-continuous-style-state` | provisional / data_blocked | statistical STOP + final DATA_BLOCKED；真首披模型行审计和冻结重跑是唯一终局门 |
| `gate0r-data-foundation` | closed / pass | 当前锚和完整生产库重验通过，不等于研究候选 GO |
| `carry-mode` | research_only / descriptive | IC/IM 承载是描述性分析，受 futures 数据陈旧限制 |
| `equal-weight-5d20z` | research_only / stop | 明确不作为生产候选，后续再决定是否退出日更链 |

所有 `docs/plans/2026-*.md` 必须通过以下规则落位：研究 spec/report 加入相应 `documents`；纯工程计划、runbook、环境说明、方法学机器或数据修复交接进入 `inventory.exclusions`，每个写具体 reason。禁止用目录级豁免，也禁止 `reason: other` 一类无审计价值理由。

- [ ] **Step 4: 实现确定性 renderer 和 CLI**

在 `tools/research_registry.py` 增加：

```python
START = "<!-- research-registry:start -->"
END = "<!-- research-registry:end -->"


def _md(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def _relative_link(repo_path: str, readme_path: Path) -> str:
    target = Path(ROOT / repo_path)
    return Path(__import__("os").path.relpath(target, readme_path.parent)).as_posix()


def render_readme(payload: dict, readme_path: Path = DEFAULT_README) -> str:
    rows = [
        "<!-- generated by tools/research_registry.py; do not edit this block manually -->",
        "| ID | 状态 | 精确范围与当前结论 | 限制/不得外推 | 重开条件 | 权威证据 |",
        "|---|---|---|---|---|---|",
    ]
    for study in payload["studies"]:
        evidence = study["documents"]["evidence"]
        links = "；".join(
            f"[{Path(path).name}]({_relative_link(path, readme_path)})" for path in evidence
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
```

CLI 固定为：

```bash
python -m tools.research_registry validate
python -m tools.research_registry render --check
python -m tools.research_registry render --write
```

`validate` errors 输出 stderr、exit 1；载入/参数错误 exit 2；`render --check` 有 diff 时 exit 1；`--write` 只写 marker 内部并在写前先 validate。

- [ ] **Step 5: 在 README 放 marker、渲染并验证**

把现有实验表替换为：

```markdown
<!-- research-registry:start -->
<!-- generated by tools/research_registry.py; do not edit this block manually -->
<!-- research-registry:end -->
```

让 renderer 固定保留 generated 注释作为 block 第一行，然后运行：

```bash
/home/elfbob/miniconda3/bin/python -m tools.research_registry validate
/home/elfbob/miniconda3/bin/python -m tools.research_registry render --write
/home/elfbob/miniconda3/bin/python -m tools.research_registry render --check
/home/elfbob/miniconda3/bin/python -m pytest tests/test_research_registry.py -q
```

Expected: 三条命令 exit 0，tests PASS，第二次 render 不产生 diff。

- [ ] **Step 6: 提交台账与生成索引**

```bash
git add docs/plans/research_registry.yaml docs/plans/README.md tools/research_registry.py tests/test_research_registry.py
git commit -m "docs: establish authoritative research registry"
```

### Task 3: 修正三份已知陈旧或过度关账文档

**Files:**
- Modify: `docs/plans/2026-08-13-probe-1b-mapping-grid.md`
- Modify: `docs/plans/2026-08-26-clean-evidence-revalidation-execution.md`
- Modify: `docs/plans/2026-08-26-signal-generator-prescreens.md`
- Modify: `tests/test_research_registry.py`

- [ ] **Step 1: 写 wording regression 失败测试**

```python
def test_known_stale_or_overbroad_wording_is_absent():
    mapping = (ROOT / "docs/plans/2026-08-13-probe-1b-mapping-grid.md").read_text()
    prescreen = (ROOT / "docs/plans/2026-08-26-signal-generator-prescreens.md").read_text()
    execution = (ROOT / "docs/plans/2026-08-26-clean-evidence-revalidation-execution.md").read_text()
    assert "现役是稳健最优\"" not in mapping
    assert "唯一实测有增量、待裁" not in prescreen
    assert "整层归档关闭，不立项" not in prescreen
    assert "尚未合并到 `main`" not in execution
```

- [ ] **Step 2: 运行单测确认 RED**

Run: `/home/elfbob/miniconda3/bin/python -m pytest tests/test_research_registry.py::test_known_stale_or_overbroad_wording_is_absent -q`

Expected: FAIL，至少命中四个旧短语。

- [ ] **Step 3: 精确修改而不改历史数值**

- mapping 将“现役是稳健最优”改为“冻结 32 格及既定回撤/换手约束内，没有候选足以替换现役”；保留赢家 Sharpe 更高的原始数字。
- prescreen 开头、§2 和 §5 改成“指定 EWMA-std、固定 CUSUM/Hamilton 与 raw/DEMA 插点关闭；相应方法族降优先级”。明确 ML 未实测，只因功效先验不立项，不能写成实证失败。dual-channel 改为“08-18 已按两种正式秤 STOP 关账，但留有小幅增量迹象”。
- clean execution §4 改成“该分支已合并到 main；本文保留运行时分支名作为历史 provenance”，不要改 run commit/hash。

- [ ] **Step 4: 运行台账、渲染和 wording tests**

```bash
/home/elfbob/miniconda3/bin/python -m pytest tests/test_research_registry.py -q
/home/elfbob/miniconda3/bin/python -m tools.research_registry validate
/home/elfbob/miniconda3/bin/python -m tools.research_registry render --check
```

Expected: PASS / exit 0。

- [ ] **Step 5: 提交措辞修复**

```bash
git add docs/plans/2026-08-13-probe-1b-mapping-grid.md docs/plans/2026-08-26-clean-evidence-revalidation-execution.md docs/plans/2026-08-26-signal-generator-prescreens.md tests/test_research_registry.py
git commit -m "docs: narrow signal research closure claims"
```

### Task 4: B3 真首披 coverage audit 纯函数与 CLI

**Files:**
- Create: `tests/test_b3_disclosure_coverage_audit.py`
- Create: `tools/audit_b3_disclosure_coverage.py`

- [ ] **Step 1: 写与冻结 coverage 函数一致的失败测试**

用完整 2014-01—2023-12 月网格、两个冻结 policy、每格一个 ticker 构造 240 个 model rows；另加 `size_only` 与 2024 窗外行验证它们不进分母：

```python
import json
from pathlib import Path

import pandas as pd
import pytest

from backtest.b3_eval import DataBlocked, compute_true_disclosure_coverage
from tools.audit_b3_disclosure_coverage import audit_frame, main


POLICIES = ["legal_deadline", "legal_deadline_plus_one_month_end"]


def exposure_grid(verified=True):
    rows = []
    for policy in POLICIES:
        for period in pd.period_range("2014-01", "2023-12", freq="M"):
            rows.append({
                "universe_role": "model",
                "pit_policy": policy,
                "formation_date": period.end_time.normalize(),
                "ticker": "000001.SZ",
                "true_first_disclosure_verified": verified,
            })
    return pd.DataFrame(rows)


def test_audit_total_is_the_existing_coverage_contract():
    frame = exposure_grid()
    frame = pd.concat([frame, pd.DataFrame([{
        "universe_role": "size_only", "pit_policy": POLICIES[0],
        "formation_date": "2014-01-31", "ticker": "000002.SZ",
        "true_first_disclosure_verified": False,
    }])], ignore_index=True)
    summary, missing = audit_frame(frame, POLICIES)
    assert summary["coverage"] == compute_true_disclosure_coverage(frame, POLICIES)
    assert summary["coverage_ready"] is True
    assert summary["coverage"]["required_denominator"] == 240
    assert missing.empty


def test_partial_coverage_reports_exact_model_key():
    frame = exposure_grid()
    frame.loc[0, "true_first_disclosure_verified"] = False
    summary, missing = audit_frame(frame, POLICIES)
    assert summary["coverage_ready"] is False
    assert summary["coverage"]["verified_numerator"] == 239
    assert missing[["pit_policy", "formation_date", "ticker"]].to_dict("records") == [{
        "pit_policy": POLICIES[0], "formation_date": "2014-01-31", "ticker": "000001.SZ"
    }]
    assert summary["by_formation_month"][0]["formation_month"] == "2014-01"
    assert summary["by_policy_formation_month"][0]["formation_month"] == "2014-01"


@pytest.mark.parametrize("mutation", ["empty-model", "duplicate", "missing-column", "integer-bool"])
def test_audit_rejects_invalid_contract(mutation):
    frame = exposure_grid()
    if mutation == "empty-model":
        frame["universe_role"] = "size_only"
    elif mutation == "duplicate":
        frame = pd.concat([frame, frame.iloc[[0]]], ignore_index=True)
    elif mutation == "missing-column":
        frame = frame.drop(columns="ticker")
    else:
        frame["true_first_disclosure_verified"] = 1
    with pytest.raises(DataBlocked):
        audit_frame(frame, POLICIES)
```

CLI 测试还必须断言：`--output-dir` 必填、已存在目录拒绝覆盖、partial 返回 1、invalid 返回 2、ready 返回 0；`coverage_audit.json` 最后写出，未覆盖 CSV 即使为空也有固定 header。

- [ ] **Step 2: 运行测试确认 RED**

Run: `/home/elfbob/miniconda3/bin/python -m pytest tests/test_b3_disclosure_coverage_audit.py -q`

Expected: import/collection FAIL。

- [ ] **Step 3: 实现审计核心，禁止复制总体判据**

`tools/audit_b3_disclosure_coverage.py` 的核心接口固定为：

```python
def audit_frame(frame: pd.DataFrame, policies: list[str]) -> tuple[dict, pd.DataFrame]:
    coverage = compute_true_disclosure_coverage(frame, policies)
    model = frame.loc[frame["universe_role"].eq("model")].copy()
    dates = pd.to_datetime(model["formation_date"], errors="raise")
    required = model.loc[dates.dt.to_period("M").between("2014-01", "2023-12")].copy()
    required["formation_date"] = dates.loc[required.index].dt.strftime("%Y-%m-%d")
    required["formation_month"] = dates.loc[required.index].dt.to_period("M").astype(str)
    required["verified"] = required["true_first_disclosure_verified"].map(bool)

    by_policy = _group_stats(required, ["pit_policy"])
    by_month = _group_stats(required, ["formation_month"])
    by_policy_month = _group_stats(required, ["pit_policy", "formation_month"])
    if sum(row["denominator"] for row in by_policy) != coverage["required_denominator"]:
        raise DataBlocked("policy breakdown does not add to total coverage")
    if sum(row["numerator"] for row in by_policy) != coverage["verified_numerator"]:
        raise DataBlocked("policy breakdown does not add to verified coverage")

    missing = required.loc[~required["verified"], ["pit_policy", "formation_date", "ticker"]]
    missing = missing.sort_values(["pit_policy", "formation_date", "ticker"]).reset_index(drop=True)
    summary = {
        "schema_version": 1,
        "coverage_ready": (
            coverage["required_denominator"] > 0
            and coverage["verified_numerator"] == coverage["required_denominator"]
        ),
        "coverage": coverage,
        "by_policy": by_policy,
        "by_formation_month": by_month,
        "by_policy_formation_month": by_policy_month,
    }
    return summary, missing
```

`_group_stats` 输出排序稳定的 `{keys..., numerator, denominator, ratio}`。CLI 用 `load_b3_config(args.config)` 和 `config_hash(cfg)`；输入只支持 `.csv`/`.csv.gz`，记录输入 SHA-256 与 `backtest.run_manifest.git_state(ROOT)`。先完成全部计算，再以 `mkdir(exist_ok=False)` 创建目录，写 `uncovered_model_rows.csv`，计算其 hash 后把 provenance/artifact record 加入 summary，最后原子写 `coverage_audit.json`。

- [ ] **Step 4: 运行 focused tests**

```bash
/home/elfbob/miniconda3/bin/python -m pytest tests/test_b3_disclosure_coverage_audit.py tests/test_b3_eval.py -q
```

Expected: PASS；既有 B3 coverage 测试无回归。

- [ ] **Step 5: 提交 B3 审计工具**

```bash
git add tools/audit_b3_disclosure_coverage.py tests/test_b3_disclosure_coverage_audit.py
git commit -m "feat: audit B3 true disclosure coverage"
```

### Task 5: 让 pure-style builders 生成结构化 PIT 元数据并绑定数据文件

**Files:**
- Create: `backtest/pit_metadata.py`
- Modify: `backtest/tail_pair_runner.py`
- Modify: `backtest/geometric_pairs_runner.py`
- Modify: `tests/test_bt_p0_revalidation.py`

- [ ] **Step 1: 写 builder metadata 失败测试**

扩展现有两个 runner 测试：

```python
tail_meta = json.loads((tmp_path / "tail_pair_build.json").read_text())
assert tail_meta["schema_version"] == 2
assert tail_meta["artifact_type"] == "tail_pair_build"
assert tail_meta["pit"]["periodic_statement_policy"] == "first_disclosure_else_statutory_deadline"
assert tail_meta["pit"]["first_disclosure_coverage"] == "partial"
assert tail_meta["data_artifact"]["path"] == "tail_pair_daily.csv"
assert len(tail_meta["data_artifact"]["sha256"]) == 64
```

geometric 对应断言 `artifact_type == "geometric_pairs_build"`、path 为 `geo5_pairs_daily.csv`。另写纯函数测试确认 `periodic_statement_types == sorted(FD_STATEMENTS)`，limitations 含 `late_filer_fallback` 且不含字符串 `approximate-PIT` 或固定日期 `2025-03-31`。

- [ ] **Step 2: 运行测试确认 RED**

Run: `/home/elfbob/miniconda3/bin/python -m pytest tests/test_bt_p0_revalidation.py -q`

Expected: missing schema/pit/data_artifact assertions FAIL。

- [ ] **Step 3: 实现共享 metadata contract**

`backtest/pit_metadata.py`：

```python
from __future__ import annotations

import hashlib
import json
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
            "text": "缺失或无效首披日的定期报告回退法定截止日；超期披露者仍可能被过早视为可知，因此结论保持 provisional。",
        }],
    }


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def data_artifact(path: Path, base: Path) -> dict:
    return {
        "path": Path(path).relative_to(base).as_posix(),
        "size": Path(path).stat().st_size,
        "sha256": sha256_file(path),
    }
```

runner 写 CSV 后构建 `schema_version`、`artifact_type`、`pit=current_pit_metadata(FD_STATEMENTS)` 和 `data_artifact(...)`；保留现有 `n_days/window/n_by_date/skipped/elapsed_s` 字段，避免破坏既有消费者。

- [ ] **Step 4: 运行 runner tests**

Run: `/home/elfbob/miniconda3/bin/python -m pytest tests/test_bt_p0_revalidation.py tests/test_bt_pure_style_builder.py -q`

Expected: PASS。

- [ ] **Step 5: 提交生产者契约**

```bash
git add backtest/pit_metadata.py backtest/tail_pair_runner.py backtest/geometric_pairs_runner.py tests/test_bt_p0_revalidation.py
git commit -m "feat: record pure style PIT build metadata"
```

### Task 6: Formal verdicts 校验并消费 build metadata

**Files:**
- Modify: `backtest/pit_metadata.py`
- Modify: `backtest/fifth_bucket_formal.py`
- Modify: `backtest/geometric_5b_formal.py`
- Modify: `tests/test_bt_p0_revalidation.py`

- [ ] **Step 1: 写 metadata missing/mismatch/stale wording 失败测试**

在 verdict 参数化测试中创建与输入 CSV 绑定的合法 build JSON，并显式传 `--build-metadata`。新增：

```python
from backtest.pit_metadata import current_pit_metadata, data_artifact
from backtest.pure_style_builder import FD_STATEMENTS


def _write_build_metadata(data_path, metadata_path, artifact_type):
    payload = {
        "schema_version": 2,
        "artifact_type": artifact_type,
        "pit": current_pit_metadata(FD_STATEMENTS),
        "data_artifact": data_artifact(data_path, data_path.parent),
    }
    metadata_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


@pytest.mark.parametrize(
    ("module_name", "input_flag", "metadata_name", "artifact_type"),
    [
        ("backtest.fifth_bucket_formal", "--tail-csv", "tail_pair_build.json", "tail_pair_build"),
        ("backtest.geometric_5b_formal", "--geo-csv", "geo5_pairs_build.json", "geometric_pairs_build"),
    ],
)
def test_verdict_rejects_missing_or_mismatched_build_metadata(
    tmp_path, module_name, input_flag, metadata_name, artifact_type
):
    module = importlib.import_module(module_name)
    data_path = tmp_path / "input.csv"
    data_path.write_text("date,value\n2024-01-02,0.0\n", encoding="utf-8")
    metadata_path = tmp_path / metadata_name
    args = [input_flag, str(data_path), "--output-dir", str(tmp_path / "out")]

    with pytest.raises(RuntimeError, match="build metadata"):
        module.main([*args, "--build-metadata", str(metadata_path)])

    _write_build_metadata(data_path, metadata_path, "wrong_artifact")
    with pytest.raises(RuntimeError, match="artifact_type"):
        module.main([*args, "--build-metadata", str(metadata_path)])

    _write_build_metadata(data_path, metadata_path, artifact_type)
    data_path.write_text("date,value\n2024-01-02,0.1\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="sha256"):
        module.main([*args, "--build-metadata", str(metadata_path)])
```

成功路径断言：

```python
payload = json.loads(output.read_text())
joined = json.dumps(payload, ensure_ascii=False)
assert payload["pit_metadata"]["periodic_statement_policy"] == "first_disclosure_else_statutory_deadline"
assert payload["caveats"][0].startswith("缺失或无效首披日")
assert "approximate-PIT" not in joined
assert "2025-03-31" not in joined
```

- [ ] **Step 2: 运行参数化 verdict tests 确认 RED**

Run: `/home/elfbob/miniconda3/bin/python -m pytest tests/test_bt_p0_revalidation.py -k 'verdict' -q`

Expected: new metadata flag/fields/validation assertions FAIL。

- [ ] **Step 3: 实现严格 metadata loader**

在 `backtest/pit_metadata.py` 增加 `load_build_pit_metadata(build_path, data_path, expected_artifact_type)`：

- JSON 顶层必须 mapping，`schema_version == 2`；
- `artifact_type` 精确匹配；
- `data_artifact.path` 必须等于数据文件 basename，size 和 SHA-256 必须复核；
- PIT schema/policy/source/fallback/coverage/limitations 类型必须精确；
- limitation code 唯一、text 非空；
- 任何失败统一抛 `RuntimeError`，消息带字段名但不吞掉原异常。

返回深复制的 PIT dict，避免调用方修改 build payload。

- [ ] **Step 4: formal runners 消费元数据并删除硬编码陈旧 caveat**

两个 CLI 增加 `--build-metadata`；未传时分别推导为输入 CSV 同目录的 `tail_pair_build.json` / `geo5_pairs_build.json`。构造 `Data` 前完成 metadata 校验。

输出统一加入：

```python
"pit_metadata": pit_metadata,
"caveats": [item["text"] for item in pit_metadata["limitations"]] + methodology_caveats,
```

第五桶 `verdict_case` 将 `approximate-PIT` 改为“首披缺失回退限制”；geometric 保留“候选全自建 vs 现役官方序列的信息不对称”作为 methodology caveat。同步更新两个模块 docstring，删除固定 2025-03-31 和旧 approximate-PIT 声称。

- [ ] **Step 5: 运行 P0 focused tests**

```bash
/home/elfbob/miniconda3/bin/python -m pytest tests/test_bt_p0_revalidation.py tests/test_bt_pure_style_builder.py -q
```

Expected: PASS；P0 fake runner 测试若只伪造 artifacts 而不执行 formal main，不需要伪造新 metadata。

- [ ] **Step 6: 提交 verdict 消费契约**

```bash
git add backtest/pit_metadata.py backtest/fifth_bucket_formal.py backtest/geometric_5b_formal.py tests/test_bt_p0_revalidation.py
git commit -m "fix: derive formal verdict caveats from build metadata"
```

### Task 7: 综合验收、不可变证据保护与交付

**Files:**
- No planned edits：验收失败必须回到拥有该行为的 Task 修复并重跑，不在终验阶段临时扩 scope。

- [ ] **Step 1: 验证台账与 README 无漂移**

```bash
/home/elfbob/miniconda3/bin/python -m tools.research_registry validate
/home/elfbob/miniconda3/bin/python -m tools.research_registry render --check
```

Expected: both exit 0。

- [ ] **Step 2: 运行全部 focused tests**

```bash
/home/elfbob/miniconda3/bin/python -m pytest tests/test_research_registry.py tests/test_b3_disclosure_coverage_audit.py tests/test_b3_eval.py tests/test_bt_p0_revalidation.py tests/test_bt_pure_style_builder.py -q
```

Expected: PASS；warnings 只允许既有 pandas FutureWarning。

- [ ] **Step 3: 复核不可变证据和用户输出未被改动**

```bash
git diff --name-only 0d0612a -- backtest/output/runs data_fixes/2026-08-01-b3-wind-share-capital-formal-archive output
python -m tools.verify_b3_formal_archive --inventory data_fixes/2026-08-01-b3-wind-share-capital-formal-archive/inventory.json --root data_fixes/2026-08-01-b3-wind-share-capital-formal-archive/core
```

Expected: 第一条对 worktree 无输出；archive verifier 输出 `OK`。主工作区的 8 个 CSV 仅是进入任务前既有修改，不得被复制、暂存或覆盖。

- [ ] **Step 4: 运行全量测试**

```bash
/home/elfbob/miniconda3/bin/python -m pytest -q
```

Expected: 全量 PASS；记录通过数、warnings 和耗时。失败时先按 systematic-debugging 区分本次回归与环境问题，不得跳过。

- [ ] **Step 5: 运行静态终检**

```bash
git diff --check
git status --short
rg -n 'approximate-PIT|有效窗截至 2025-03-31' backtest/fifth_bucket_formal.py backtest/geometric_5b_formal.py
rg -n '唯一实测有增量、待裁|整层归档关闭，不立项|尚未合并到 `main`' docs/plans/2026-08-26-signal-generator-prescreens.md docs/plans/2026-08-26-clean-evidence-revalidation-execution.md
```

Expected: diff check clean；两个 `rg` 均 exit 1/no matches；status 只含本计划明确文件。

- [ ] **Step 6: 完成前代码审查与交付说明**

使用 `requesting-code-review` 检查 spec 覆盖、状态语义、B3 分母、fail-closed 路径和不可变证据边界；修复所有重要问题后重新执行相关测试。最终说明必须明确：本阶段只交付治理和审计能力，B3 仍是 provisional/data-blocked，正式重跑是下一独立阶段。
