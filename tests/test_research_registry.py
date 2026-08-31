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
