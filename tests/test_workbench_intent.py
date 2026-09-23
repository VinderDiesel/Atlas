"""T10e 意图评测（ADR-0031 D09）：最小差异样本契约 + eval/workbench_intent 规则/LLM 对照。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from eval.workbench_intent import (
    ENGINE_LLM,
    ENGINE_RULE,
    IntentSampleError,
    load_samples,
    run,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
GOLD_DIR = REPO_ROOT / "eval" / "gold" / "intent"


def _intent_json(sample: Any) -> dict[str, Any]:
    """样本标注 → 理想 LLM 输出（意图 JSON；证据由系统逐字定位）。"""
    annotation = sample.annotation
    return {
        "schema_version": 1,
        "task": "query",
        "metric_mentions": list(annotation.metric_mentions),
        "groups": list(annotation.groups),
        "filters": [
            {"subject": f.subject, "op": f.op, "values": list(f.values)} for f in annotation.filters
        ],
        "time_mentions": list(annotation.time_mentions),
        "sort": None,
        "limit": None,
        "ambiguities": [],
    }


class TestSamples:
    """样本契约：schema 校验 + 5 对最小差异结构 + 来源族 split 冻结。"""

    def test_load_ten_samples_five_pairs(self) -> None:
        samples = load_samples()
        assert [s.id for s in samples] == [f"intent-{i:02d}" for i in range(1, 11)]
        pairs: dict[str, list[str]] = {}
        for sample in samples:
            pairs.setdefault(sample.pair_id, []).append(sample.id)
        assert sorted(pairs) == [
            "amount-count",
            "definite-ambiguous",
            "group-filter",
            "include-exclude",
            "month-year",
        ]
        assert all(len(ids) == 2 for ids in pairs.values())

    def test_pairs_are_minimal_differences(self) -> None:
        by_id = {s.id: s for s in load_samples()}
        # include-exclude：同一过滤字段，op 反转（= / !=）
        f1 = by_id["intent-01"].expected.plan["filters"]
        f2 = by_id["intent-02"].expected.plan["filters"]
        assert f1[0]["column"] == f2[0]["column"] == "Branch"
        assert {f1[0]["op"], f2[0]["op"]} == {"=", "!="}
        # amount-count：同一时间，金额 / 笔数
        assert by_id["intent-03"].expected.plan["metric"] == "total_trade_value"
        assert by_id["intent-04"].expected.plan["metric"] == "trade_count"
        # group-filter：分组 / 筛选
        assert by_id["intent-05"].expected.plan["dimensions"] == ["ExchangeID"]
        assert by_id["intent-06"].expected.plan["filters"][0]["column"] == "ExchangeID"
        # month-year：相对时间颗粒度不同
        assert by_id["intent-07"].expected.plan["time"]["granularity"] == "month"
        assert by_id["intent-08"].expected.plan["time"]["granularity"] == "year"
        # definite-ambiguous：明确 / 澄清
        assert by_id["intent-09"].expected.reason_code == "bound"
        assert by_id["intent-10"].expected.reason_code == "clarification_required"
        assert by_id["intent-10"].expected.plan is None

    def test_split_frozen_by_source_family(self) -> None:
        assert {(s.source_family, s.split) for s in load_samples()} == {
            ("finance-manual-v1", "test")
        }

    def test_invalid_sample_rejected(self, tmp_path: Path) -> None:
        bad = json.loads((GOLD_DIR / "intent-10.json").read_text(encoding="utf-8"))
        bad["id"] = "intent-99"
        bad["expected"]["plan"] = {"metric": "x"}  # 澄清样本不得携带 plan（allOf 拒绝）
        (tmp_path / "intent-99.json").write_text(
            json.dumps(bad, ensure_ascii=False), encoding="utf-8"
        )
        with pytest.raises(IntentSampleError):
            load_samples(tmp_path)


class TestRuleEngine:
    """规则引擎：标注（人工意图声明）→ 确定性链路，Recall 与 Plan/澄清分列。"""

    def test_rule_engine_full_pass_separated_metrics(self) -> None:
        report = run(ENGINE_RULE)
        assert report.status == "ok"
        assert report.engine == ENGINE_RULE
        assert report.summary is not None
        assert report.summary.retrieval_recall == "10/10"
        assert report.summary.plan_accuracy == "9/9"
        assert report.summary.clarify_accuracy == "1/1"
        assert len(report.samples) == 10
        assert all(s.recall_ok for s in report.samples)
        assert not any(s.error for s in report.samples)

    def test_report_budget_declared_and_exclusive(self, tmp_path: Path) -> None:
        out = tmp_path / "intent-report.json"
        run(ENGINE_RULE, output=out)
        data = json.loads(out.read_text(encoding="utf-8"))
        assert data["report_type"] == "workbench_intent"
        assert data["status"] == "ok"
        assert data["budget"] == {
            "final_k": 5,
            "max_candidates_per_query": 5,
            "max_retrieval_expansions": 2,
            "bind_same_budget": True,
            "default_limit": 100,
        }
        assert any("not_run" in note for note in data["notes"])  # EX 不运行：不产假数字
        with pytest.raises(FileExistsError):
            run(ENGINE_RULE, output=out)


class TestLlmEngine:
    """LLM 对照：未配置行为 blocked（不产假数字）；fake 仅验证解析路径，不计效果。"""

    def test_blocked_without_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        report = run(ENGINE_LLM)
        assert report.status == "blocked"
        assert report.engine == ENGINE_LLM
        assert report.summary is None
        assert report.samples == ()
        assert report.catalog_digest is None
        assert any("OPENAI_API_KEY" in note for note in report.notes)

    def _perfect_chat(self) -> Any:
        by_question = {s.question: s for s in load_samples()}

        def chat(system: str, user: str, model: str) -> tuple[str, dict[str, int]]:
            question = user.split("「", 1)[1].rsplit("」", 1)[0]
            raw = json.dumps(_intent_json(by_question[question]), ensure_ascii=False)
            return raw, {"prompt_tokens": 1, "completion_tokens": 1}

        return chat

    def test_fake_chat_end_to_end(self) -> None:
        report = run(ENGINE_LLM, chat=self._perfect_chat())
        assert report.status == "ok"
        assert report.summary is not None
        assert report.summary.retrieval_recall == "10/10"
        assert report.summary.plan_accuracy == "9/9"
        assert report.summary.clarify_accuracy == "1/1"
        assert report.summary.total_tokens == 20

    def test_fake_chat_hallucination_fails_closed(self) -> None:
        bad = json.dumps(
            {
                "schema_version": 1,
                "task": "query",
                "metric_mentions": ["虚构指标"],
                "groups": [],
                "filters": [],
                "time_mentions": [],
                "sort": None,
                "limit": None,
                "ambiguities": [],
            },
            ensure_ascii=False,
        )

        def chat(system: str, user: str, model: str) -> tuple[str, dict[str, int]]:
            return bad, {"prompt_tokens": 1, "completion_tokens": 1}

        report = run(ENGINE_LLM, chat=chat)
        assert report.status == "ok"  # 报告仍产出，但逐样本如实失败
        assert report.summary is not None
        assert report.summary.retrieval_recall == "0/10"
        assert report.summary.plan_accuracy == "0/9"
        assert report.summary.clarify_accuracy == "0/1"
        assert all(s.error for s in report.samples)
