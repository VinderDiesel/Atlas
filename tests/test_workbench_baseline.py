"""ADR-0031 T01：历史报告不能冒充当前实测，夹具走真实 HTTP 链。"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_explicit_repository_identity_does_not_use_runtime_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from data.identity import git_full_sha

    monkeypatch.setenv("ATLAS_GIT_SHA", "abcdef0")
    expected = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "show", "-s", "--format=%H", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert git_full_sha(REPO_ROOT) == expected
    with pytest.raises(subprocess.CalledProcessError):
        git_full_sha(tmp_path)


def test_baseline_does_not_promote_history(tmp_path: Path) -> None:
    from eval.workbench_baseline import BaselineReport, collect_baseline

    output = tmp_path / "baseline.json"
    report = collect_baseline(REPO_ROOT, output)
    assert report.current_metrics is None
    assert report.historical_evidence
    assert all(item.code_sha for item in report.historical_evidence)
    assert {item.strategy for item in report.strategies} == {"rules", "rules_llm", "sft", "rl"}
    assert all(item.status == "not_run" and item.metrics is None for item in report.strategies)
    assert BaselineReport.model_validate_json(output.read_text()) == report
    assert report.data_verified is False
    assert report.snapshots
    assert all(item.verification == "not_run" for item in report.snapshots)


def test_report_rejects_unexecuted_metrics(tmp_path: Path) -> None:
    from pydantic import ValidationError

    from eval.workbench_baseline import BaselineReport, collect_baseline

    report = collect_baseline(REPO_ROOT, tmp_path / "baseline.json").model_dump()
    report["current_metrics"] = {"ex": 1.0}
    with pytest.raises(ValidationError):
        BaselineReport.model_validate(report)
    report["current_metrics"] = None
    report["strategies"][0]["metrics"] = {"ex": 1.0}
    with pytest.raises(ValidationError):
        BaselineReport.model_validate(report)


def test_inventory_retains_missing_identity_and_invalid_files(tmp_path: Path) -> None:
    from eval.workbench_baseline import inventory_reports

    directory = tmp_path / "eval" / "reports"
    directory.mkdir(parents=True)
    (directory / "valid.json").write_text(json.dumps({"sha": "abcdef1", "summary": {"ex": "1/1"}}))
    (directory / "unbound.json").write_text('{"summary":{"ex":"1/1"}}')
    (directory / "broken.json").write_text("{")
    (directory / "array.json").write_text("[]")
    outside = tmp_path / "private.json"
    outside.write_text('{"sha":"abcdef2","secret":"do-not-export"}')
    (directory / "link.json").symlink_to(outside)
    evidence, issues = inventory_reports(tmp_path)
    assert [(item.path, item.code_sha) for item in evidence] == [
        ("eval/reports/valid.json", "abcdef1")
    ]
    assert {item.reason for item in issues} == {"missing_code_sha", "invalid_json", "unsafe_path"}
    assert len(issues) == 4
    assert "do-not-export" not in str(evidence) + str(issues)


def test_baseline_never_infers_wiring_from_file_presence(tmp_path: Path) -> None:
    from eval.workbench_baseline import collect_baseline

    report = collect_baseline(REPO_ROOT, tmp_path / "baseline.json")
    capabilities = {item.evidence_id: item for item in report.capabilities}
    for evidence_id in ("E05", "E06", "E08", "E11"):
        assert capabilities[evidence_id].runtime_verification == "not_run"
        assert capabilities[evidence_id].sources
    assert report.worktree_digest
    assert len(report.code_sha) == 40


def test_baseline_refuses_overwriting_existing_evidence(tmp_path: Path) -> None:
    from eval.workbench_baseline import collect_baseline

    output = tmp_path / "baseline.json"
    output.write_text("existing evidence")
    with pytest.raises(FileExistsError):
        collect_baseline(REPO_ROOT, output)
    assert output.read_text() == "existing evidence"


def test_report_renderer_separates_workbench_inventory(tmp_path: Path) -> None:
    from eval.report import render_workbench_baseline
    from eval.workbench_baseline import collect_baseline

    report = collect_baseline(REPO_ROOT, tmp_path / "baseline.json")
    rendered = render_workbench_baseline(report)
    assert "not_run" in rendered
    assert "本次未执行业务评测" in rendered
    assert report.code_sha in rendered
    assert "当前 EX =" not in rendered


def test_baseline_cli_and_renderer(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from eval.report import main as render
    from eval.workbench_baseline import main as collect

    output = tmp_path / "cli.json"
    assert collect(["--repo", str(REPO_ROOT), "--output", str(output)]) == 0
    capsys.readouterr()
    assert render(["--workbench-report", str(output)]) == 0
    text = capsys.readouterr().out
    assert "本次未执行业务评测" in text
    assert "主评测趋势" not in text
    with pytest.raises(SystemExit) as error:
        render(["--latest", "--workbench-report", str(output)])
    assert error.value.code == 2
    assert collect(["--repo", str(REPO_ROOT), "--output", str(output)]) == 2


def test_governance_keeps_baseline_unstructured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from eval.workbench_baseline import collect_baseline
    from serving import governance
    from tests.workbench_support import WorkbenchHarness

    reports = tmp_path / "reports"
    reports.mkdir()
    name = "workbench-baseline-abcdef1-m0-t01"
    report = collect_baseline(REPO_ROOT, reports / f"{name}.json")
    monkeypatch.setattr(governance, "REPORTS_DIR", reports)
    monkeypatch.setattr(governance, "REPO_ROOT", tmp_path)
    with WorkbenchHarness(tmp_path / "harness") as harness:
        response = harness.request("GET", "/api/v1/governance/reports")
        assert response.status_code == 200
        items = response.json()["items"]
        assert len(items) == 1
        assert items[0]["name"] == name
        assert items[0]["pattern"] == "workbench-baseline-<sha>-m0-t01"
        assert items[0]["structured"] is False
        assert set(items[0]) == {"name", "pattern", "structured", "size_bytes", "mtime"}
        response = harness.request("GET", f"/api/v1/governance/reports/{name}")
        assert response.status_code == 200
        detail = response.json()
        assert detail["structured"] is False
        assert detail["raw"] == report.model_dump(mode="json")
        assert detail["raw"]["current_metrics"] is None
        assert all(s["status"] == "not_run" for s in detail["raw"]["strategies"])
        assert harness.executor_spy.calls == []


def test_harness_uses_real_routes_and_guards(tmp_path: Path) -> None:
    from tests.workbench_support import WorkbenchHarness

    with WorkbenchHarness(tmp_path) as harness:
        assert harness.request("GET", "/health").status_code == 200
        assert (
            harness.request(
                "POST", "/api/v1/ask", actor=None, json={"question": "总交易额"}
            ).status_code
            == 401
        )
        response = harness.request(
            "POST", "/api/v1/ask", json={"question": "2013 年第二季度总交易额"}
        )
        assert response.status_code == 200
        assert response.json()["kind"] == "answer"
        assert len(harness.executor_spy.calls) == 1
        assert "LIMIT" in harness.executor_spy.calls[0]
        assert "WHERE" in harness.executor_spy.calls[0]
        assert harness.audit_path.parent == tmp_path / "audit"
        assert harness.audit_path.exists()
        invalid = harness.request("POST", "/api/v1/plan/execute", json={"metric": "unregistered"})
        assert invalid.status_code == 200
        assert invalid.json()["kind"] == "error"
        assert len(harness.executor_spy.calls) == 1
