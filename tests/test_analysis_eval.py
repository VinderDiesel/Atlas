"""T09 独立 analysis 评测器的 TDD 测试（ADR-0026，先红后绿）。

评测器是「最后一个不被实现自证过关的关口」（task-9-brief）：本测试用**手工构造的
独立 oracle 期望值**（不调用 agent.synthesize）钉住以下反例，每一种都必须导致
样本失败 / 评测失败，禁止空跑绿灯：

- 故意错误的 AnalysisPlan（分组步 limit 漂移）→ sub_plans 失败；
- 故意错误的步骤行（rows 与 attribution 自相矛盾的谎言实现）→ steps_rows 失败；
- 故意错误的总量 delta / 贡献百分比 → attribution_totals / attribution_items 失败；
- 故意错误的拒答 kind（answer 样本答 clarify、clarify 样本答 answer）→ kind 失败；
- 缺期望（answer 样本缺 expected_plan/reference）→ 结构门失败且不执行；
- draft 样本进真链 → maturity_ready 失败；
- 空样本集 → 拒绝出报告（ValueError / CLI exit 2）；
- 必测项缺失（ready answer 缺参考角色块）→ reference_complete 失败；
- 畸形参考块（列名不含指标同名列）→ oracle fail-closed（评审 Minor-1），
  绝不静默回退取最后一列错位比对；
- 参考块维度单元格为 NULL（R7 裁定）：评测器必须镜像综合器的 NULL 独立桶
  契约——"(null)" 项照常构建、并集补零、("null",) 排序键；数字单元格仍
  fail-closed，绝不静默接受非 str/None 类型漂移；
- 快照不符（样本声明 ≠ 运行参数）→ snapshot_match 失败；
- 快照指纹漂移（before/after）→ SnapshotVerifyError（CLI exit 2，不出报告）；
- agent 抛异常 / 返回非 AnalysisResult → executed 失败；
- 闭合矩阵违约（3 步 answer）→ contract 失败；
- 绑定失配（结果 sha ≠ 运行参数）→ bindings 失败。

全部测试不依赖真实 Doris（fake agent + 内存样本 + 临时目录）。
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from unittest import mock

from agent.analysis import (
    ANALYSIS_ROLES,
    AnalysisPlan,
    AnalysisResult,
    Attribution,
    AttributionItem,
)
from agent.compiler import OrderSpec, Plan, TimeSpec
from agent.planner import ClarificationRequest
from agent.state import TurnResult

REPO_ROOT = Path(__file__).resolve().parent.parent

METRIC = "commission_revenue"
DIMENSION = "Branch"
SNAP = "snap001"
SEM = "sem001"
GROUP_LIMIT = 10000


def _finance_semantic_sha() -> str:
    """CLI 真链模式会用真实语义模型算 sha：样本声明与结果绑定必须同源。"""
    from agent.compiler import SemanticModel

    return SemanticModel(
        REPO_ROOT / "semantic" / "ossie" / "atlas_finance.ossie.yaml"
    ).source_sha256


# ---------------------------------------------------------------------------
# 期望值（样本侧）：全部手工书写，与 agent 实现无关
# ---------------------------------------------------------------------------


def _expected_plan_json() -> dict[str, object]:
    """期望 AnalysisPlan 投影（canonical 7 键 + intent）。"""
    return {
        "intent": "change_contribution",
        "metric": METRIC,
        "dimension": DIMENSION,
        "baseline": {"granularity": "quarter", "value": "2013Q3"},
        "current": {"granularity": "quarter", "value": "2013Q4"},
        "direction": "change",
        "filters": [],
        "synthesizer": "additive_delta_v1",
        "recipe_version": 1,
    }


def _expected_sub_plans_json() -> list[dict[str, object]]:
    """固定四步模板期望（角色序 = ANALYSIS_ROLES，位置即角色）。"""
    base = {"granularity": "quarter", "value": "2013Q3"}
    cur = {"granularity": "quarter", "value": "2013Q4"}
    return [
        {
            "role": "baseline_total",
            "metric": METRIC,
            "dimensions": [],
            "time": base,
            "filters": [],
            "order_by": [],
            "limit": 1,
            "comparison": None,
        },
        {
            "role": "current_total",
            "metric": METRIC,
            "dimensions": [],
            "time": cur,
            "filters": [],
            "order_by": [],
            "limit": 1,
            "comparison": None,
        },
        {
            "role": "current_by_dimension",
            "metric": METRIC,
            "dimensions": [DIMENSION],
            "time": cur,
            "filters": [],
            "order_by": [{"column": DIMENSION, "desc": False}],
            "limit": GROUP_LIMIT,
            "comparison": None,
        },
        {
            "role": "baseline_by_dimension",
            "metric": METRIC,
            "dimensions": [DIMENSION],
            "time": base,
            "filters": [],
            "order_by": [{"column": DIMENSION, "desc": False}],
            "limit": GROUP_LIMIT,
            "comparison": None,
        },
    ]


def _reference_results() -> list[dict[str, object]]:
    """人工核对参考结果：角色块（列名定位 + 精确文本单元格）。

    数字口径：base 总 100 / cur 总 80；A 60→30（Δ-30，+150%）、B 40→50（Δ+10，-50%）。
    """
    return [
        {"role": "baseline_total", "columns": [METRIC], "rows": [["100"]]},
        {"role": "current_total", "columns": [METRIC], "rows": [["80"]]},
        {
            "role": "current_by_dimension",
            "columns": [DIMENSION, METRIC],
            "rows": [["A", "30"], ["B", "50"]],
        },
        {
            "role": "baseline_by_dimension",
            "columns": [DIMENSION, METRIC],
            "rows": [["A", "60"], ["B", "40"]],
        },
    ]


def _mangled_reference_results() -> list[dict[str, object]]:
    """畸形参考块：列名不含指标同名列（数值位置与 _reference_results 完全一致）。

    钉死评审 Minor-1：旧实现静默回退取最后一列会恰好错位比对「通过」，
    修复后 oracle 必须拒判（该样本 fail 且原因说明列缺失）。
    """
    return [
        {"role": "baseline_total", "columns": ["revenue_wrong"], "rows": [["100"]]},
        {"role": "current_total", "columns": ["revenue_wrong"], "rows": [["80"]]},
        {
            "role": "current_by_dimension",
            "columns": [DIMENSION, "revenue_wrong"],
            "rows": [["A", "30"], ["B", "50"]],
        },
        {
            "role": "baseline_by_dimension",
            "columns": [DIMENSION, "revenue_wrong"],
            "rows": [["A", "60"], ["B", "40"]],
        },
    ]


def _ready_answer_sample(**overrides: object) -> dict[str, object]:
    """ready answer 样本（期望与参考一致，配合 _ok_result 应全绿）。"""
    sample: dict[str, object] = {
        "id": "attribution-001",
        "maturity": "ready",
        "question": "分析 2013Q4 相对 2013Q3 的佣金收入按分支的变化贡献",
        "expected_kind": "answer",
        "expected_sql_calls": 4,
        "snapshot_sha": SNAP,
        "semantic_sha256": SEM,
        "expected_plan": _expected_plan_json(),
        "expected_sub_plans": _expected_sub_plans_json(),
        "reference": {
            "sql": ["SELECT SUM(x) FROM t WHERE quarter='2013Q3' LIMIT 1"],
            "results": _reference_results(),
            "reviewed_by": "controller",
            "reviewed_at": "2026-09-14T12:00:00+08:00",
        },
        "tags": ["analysis", "attribution"],
    }
    sample.update(overrides)
    return sample


def _ready_unavailable_sample(**overrides: object) -> dict[str, object]:
    """ready unavailable 样本（zero_total_delta：两期总量相同）。"""
    sample: dict[str, object] = {
        "id": "unavailable-001",
        "maturity": "ready",
        "question": "分析 2013Q4 相对 2013Q3 的佣金收入按分支的变化贡献（净零）",
        "expected_kind": "unavailable",
        "expected_reason_code": "zero_total_delta",
        "expected_sql_calls": 4,
        "snapshot_sha": SNAP,
        "semantic_sha256": SEM,
        "expected_plan": _expected_plan_json(),
        "expected_sub_plans": _expected_sub_plans_json(),
        "reference": {
            "sql": ["SELECT 1"],
            "results": [
                {"role": "baseline_total", "columns": [METRIC], "rows": [["100"]]},
                {"role": "current_total", "columns": [METRIC], "rows": [["100"]]},
                {
                    "role": "current_by_dimension",
                    "columns": [DIMENSION, METRIC],
                    "rows": [["A", "50"], ["B", "50"]],
                },
                {
                    "role": "baseline_by_dimension",
                    "columns": [DIMENSION, METRIC],
                    "rows": [["A", "50"], ["B", "50"]],
                },
            ],
            "reviewed_by": "controller",
            "reviewed_at": "2026-09-14T12:00:00+08:00",
        },
        "tags": ["analysis", "unavailable"],
    }
    sample.update(overrides)
    return sample


def _clarify_sample(**overrides: object) -> dict[str, object]:
    """clarify 样本（draft：相对时间澄清，零 SQL）。"""
    sample: dict[str, object] = {
        "id": "clarify-001",
        "maturity": "draft",
        "question": "分析 2013Q4 相对上季度的佣金收入变化贡献",
        "expected_kind": "clarify",
        "expected_sql_calls": 0,
        "expected_clarification": {
            "reasons": ["期间含相对时间表达，需绝对期间"],
            "kind": "relative_time",
        },
        "snapshot_sha": "<待填写>",
        "semantic_sha256": "<待填写>",
        "tags": ["analysis", "clarification"],
    }
    sample.update(overrides)
    return sample


def _blocked_sample(**overrides: object) -> dict[str, object]:
    """blocked 样本（guard_blocked 终态，一步 answer 前缀 + 一步 blocked）。"""
    sample: dict[str, object] = {
        "id": "blocked-001",
        "maturity": "ready",
        "question": "分析 2013Q4 相对 2013Q3 的佣金收入按分支的变化贡献（拦截）",
        "expected_kind": "blocked",
        "expected_reason_code": "guard_blocked",
        "expected_sql_calls": 1,
        "snapshot_sha": SNAP,
        "semantic_sha256": SEM,
        "tags": ["analysis", "blocked"],
    }
    sample.update(overrides)
    return sample


# ---------------------------------------------------------------------------
# agent 侧 fixture：手工构造 AnalysisResult（独立 oracle 的「被测输出」）
# ---------------------------------------------------------------------------


def _step(columns: list[str], rows: list[list[object]], sql: str) -> TurnResult:
    return TurnResult(
        kind="answer",
        session_id="s-eval",
        question="q",
        metric=METRIC,
        sql=sql,
        columns=tuple(columns),
        rows=tuple(tuple(row) for row in rows),
        row_count=len(rows),
        latency_ms=1.0,
    )


def _ok_steps() -> tuple[TurnResult, ...]:
    """与参考一致的四个 answer 步（单元格用 Decimal，模拟真实执行产物）。"""
    return (
        _step([METRIC], [[Decimal("100")]], f"-- {ANALYSIS_ROLES[0]}"),
        _step([METRIC], [[Decimal("80")]], f"-- {ANALYSIS_ROLES[1]}"),
        _step([DIMENSION, METRIC], [["A", Decimal("30")], ["B", Decimal("50")]], "-- cur"),
        _step([DIMENSION, METRIC], [["A", Decimal("60")], ["B", Decimal("40")]], "-- base"),
    )


def _correct_plan() -> AnalysisPlan:
    base = TimeSpec("quarter", "2013Q3")
    cur = TimeSpec("quarter", "2013Q4")
    subs = (
        Plan(metric=METRIC, time=base, limit=1),
        Plan(metric=METRIC, time=cur, limit=1),
        Plan(
            metric=METRIC,
            dimensions=(DIMENSION,),
            time=cur,
            order_by=(OrderSpec(DIMENSION),),
            limit=GROUP_LIMIT,
        ),
        Plan(
            metric=METRIC,
            dimensions=(DIMENSION,),
            time=base,
            order_by=(OrderSpec(DIMENSION),),
            limit=GROUP_LIMIT,
        ),
    )
    return AnalysisPlan(
        intent="change_contribution",
        metric=METRIC,
        dimension=DIMENSION,
        baseline=base,
        current=cur,
        filters=(),
        direction="change",
        sub_plans=subs,
    )


def _ok_attribution() -> Attribution:
    return Attribution(
        status="ok",
        baseline=Decimal("100"),
        current=Decimal("80"),
        delta=Decimal("-20"),
        items=(
            AttributionItem(
                value="A",
                baseline=Decimal("60"),
                current=Decimal("30"),
                delta=Decimal("-30"),
                contribution_pct=Decimal("150.000000"),
            ),
            AttributionItem(
                value="B",
                baseline=Decimal("40"),
                current=Decimal("50"),
                delta=Decimal("10"),
                contribution_pct=Decimal("-50.000000"),
            ),
        ),
        reason_code=None,
        text="变化贡献分解（测试 fixture）。",
    )


def _ok_result(**overrides: object) -> AnalysisResult:
    turn = TurnResult(kind="answer", session_id="s-eval", question="q", metric=METRIC)
    result = AnalysisResult(
        turn=turn,
        plan=_correct_plan(),
        steps=_ok_steps(),
        attribution=_ok_attribution(),
        reason_code=None,
        elapsed_ms=5.0,
        snapshot_sha=SNAP,
        semantic_sha256=SEM,
    )
    for key, value in overrides.items():
        result = replace(result, **{key: value})  # type: ignore[arg-type]
    return result


def _zero_steps() -> tuple[TurnResult, ...]:
    """净零场景的四步执行产物（与 _ready_unavailable_sample 参考同源）。"""
    return (
        _step([METRIC], [[Decimal("100")]], f"-- {ANALYSIS_ROLES[0]}"),
        _step([METRIC], [[Decimal("100")]], f"-- {ANALYSIS_ROLES[1]}"),
        _step([DIMENSION, METRIC], [["A", Decimal("50")], ["B", Decimal("50")]], "-- cur"),
        _step([DIMENSION, METRIC], [["A", Decimal("50")], ["B", Decimal("50")]], "-- base"),
    )


def _zero_delta_result() -> AnalysisResult:
    """unavailable + zero_total_delta 的正确形态（保留两期总量、delta=0、空 items）。"""
    return AnalysisResult(
        turn=TurnResult(kind="answer", session_id="s-eval", question="q", metric=METRIC),
        plan=_correct_plan(),
        steps=_zero_steps(),
        attribution=Attribution(
            status="unavailable",
            baseline=Decimal("100"),
            current=Decimal("100"),
            delta=Decimal("0"),
            items=(),
            reason_code="zero_total_delta",
            text="净零变化。",
        ),
        reason_code="zero_total_delta",
        elapsed_ms=5.0,
        snapshot_sha=SNAP,
        semantic_sha256=SEM,
    )


def _clarify_result() -> AnalysisResult:
    return AnalysisResult(
        turn=TurnResult(
            kind="clarify",
            session_id="s-eval",
            question="q",
            clarification=ClarificationRequest(
                "q", ("期间含相对时间表达，需绝对期间",), kind="relative_time"
            ),
        ),
        plan=None,
        steps=(),
        attribution=None,
        reason_code=None,
        elapsed_ms=1.0,
        snapshot_sha=SNAP,
        semantic_sha256=SEM,
    )


def _blocked_result() -> AnalysisResult:
    return AnalysisResult(
        turn=TurnResult(kind="blocked", session_id="s-eval", question="q", block_reason="x"),
        plan=_correct_plan(),
        steps=(
            _step([METRIC], [[Decimal("100")]], "-- ok step"),
            TurnResult(kind="blocked", session_id="s-eval", question="q", block_reason="x"),
        ),
        attribution=None,
        reason_code="guard_blocked",
        elapsed_ms=1.0,
        snapshot_sha=SNAP,
        semantic_sha256=SEM,
    )


class _FakeAgent:
    """固定返回值 / 固定异常的假 agent（不依赖真实链路）。"""

    def __init__(
        self, result: AnalysisResult | None = None, error: Exception | None = None
    ) -> None:
        self.calls: list[str] = []
        self._result = result
        self._error = error

    def analyze(
        self, question: str, *, session_id: str | None = None, identity: object = None
    ) -> object:
        self.calls.append(question)
        if self._error is not None:
            raise self._error
        return self._result


class _ScriptedAgent:
    """按问句脚本化返回（含按问句抛异常），用于隔离性测试。"""

    def __init__(self, script: dict[str, object]) -> None:
        self.script = script
        self.calls: list[str] = []

    def analyze(
        self, question: str, *, session_id: str | None = None, identity: object = None
    ) -> object:
        self.calls.append(question)
        item = self.script[question]
        if isinstance(item, Exception):
            raise item
        return item


# ---------------------------------------------------------------------------
# 断言辅助
# ---------------------------------------------------------------------------


def _find_check(report: dict[str, object], sample_id: str, name: str) -> dict[str, object]:
    for sample in report["samples"]:  # type: ignore[union-attr]
        if sample["id"] == sample_id:  # type: ignore[index]
            for check in sample["checks"]:  # type: ignore[index]
                if check["name"] == name:  # type: ignore[index]
                    return check  # type: ignore[return-value]
    raise AssertionError(f"报告缺少检查项 {sample_id}/{name}")


def _assert_fail(
    self: unittest.TestCase, report: dict[str, object], sample_id: str, name: str
) -> None:
    check = _find_check(report, sample_id, name)
    self.assertEqual(check["status"], "fail", msg=f"{sample_id}/{name}: {check}")


def _assert_pass(
    self: unittest.TestCase, report: dict[str, object], sample_id: str, name: str
) -> None:
    check = _find_check(report, sample_id, name)
    self.assertEqual(check["status"], "pass", msg=f"{sample_id}/{name}: {check}")


_SNAPSHOT_META: dict[str, object] = {
    "sha": SNAP,
    "created_at": "2026-09-14T12:00:00+08:00",
    "data_range": {"min": "2013-01-01", "max": "2013-12-31"},
    "raw_size_bytes": 1024,
    "row_counts": {"atlas": {"dwd_fact_trade": 100}},
    "snapshot_ids": {"atlas.dwd_fact_trade": "snap-#1"},
}

_FAKE_SNAP_DIR: Path | None = None


def _fake_snapshots_dir() -> Path:
    """一次性建好的假快照目录（含 snap001 的合法 meta），供真链模式指纹复核。"""
    global _FAKE_SNAP_DIR
    if _FAKE_SNAP_DIR is None:
        base = Path(tempfile.mkdtemp(prefix="atlas-analysis-eval-snap-"))
        (base / f"{SNAP}.meta.json").write_text(
            json.dumps(_SNAPSHOT_META, ensure_ascii=False), encoding="utf-8"
        )
        _FAKE_SNAP_DIR = base
    return _FAKE_SNAP_DIR


def _evaluate(
    samples: list[dict[str, object]], agent: object, **kwargs: object
) -> dict[str, object]:
    """统一入口：真链模式默认注入假快照环境（meta + 现场测量），隔离数据依赖。

    显式传入 snapshots_dir / measure 的用例（指纹漂移等）不受默认值影响。
    """
    from eval.analysis_eval import evaluate_analysis

    if not kwargs.get("dry"):
        kwargs.setdefault("snapshots_dir", _fake_snapshots_dir())
        kwargs.setdefault("measure", lambda: dict(_SNAPSHOT_META))
    return evaluate_analysis(agent, samples, **kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 测试
# ---------------------------------------------------------------------------


class TestHappyPath(unittest.TestCase):
    """正确实现 + 一致的期望/参考 → 全绿。"""

    def test_answer_happy_path(self) -> None:
        agent = _FakeAgent(result=_ok_result())
        sample = _ready_answer_sample()
        # 文件上下文（stem == id）：让 id_matches_filename 检查真实生效
        sample["_file"] = "/tmp/atlas-analysis-eval/attribution-001.json"
        report = _evaluate([sample], agent, snapshot_sha=SNAP, semantic_sha256=SEM)
        self.assertTrue(report["ok"], msg=json.dumps(report, ensure_ascii=False, default=str))
        self.assertEqual(report["run"], "executed")
        self.assertEqual(agent.calls, [sample["question"]])
        entry = report["samples"][0]  # type: ignore[index]
        self.assertTrue(entry["ok"])  # type: ignore[index]
        for check in entry["checks"]:  # type: ignore[union-attr]
            # clarification 对 answer 样本不适用 → skip；其余必须逐项 pass
            expected_status = "skip" if check["name"] == "clarification" else "pass"
            self.assertEqual(check["status"], expected_status, msg=str(check))
        # 报告契约：绑定信息 + 分列计数；禁止拼装 composite「accuracy」
        self.assertIn("code_sha", report)
        self.assertIn("snapshot_sha", report)
        self.assertIn("semantic_sha256", report)
        self.assertIn("dirty", report)
        self.assertIn("checks", report)
        self.assertNotIn("accuracy", json.dumps(report, ensure_ascii=False, default=str))

    def test_unavailable_zero_delta_happy_path(self) -> None:
        agent = _FakeAgent(result=_zero_delta_result())
        report = _evaluate(
            [_ready_unavailable_sample()], agent, snapshot_sha=SNAP, semantic_sha256=SEM
        )
        self.assertTrue(report["ok"], msg=json.dumps(report, ensure_ascii=False, default=str))

    def test_blocked_happy_path(self) -> None:
        agent = _FakeAgent(result=_blocked_result())
        report = _evaluate([_blocked_sample()], agent, snapshot_sha=SNAP, semantic_sha256=SEM)
        self.assertTrue(report["ok"], msg=json.dumps(report, ensure_ascii=False, default=str))

    def test_clarify_happy_path(self) -> None:
        agent = _FakeAgent(result=_clarify_result())
        # ready + 真 sha：real 模式的前置门要求 ready（draft 只能进 --dry）
        sample = _clarify_sample(maturity="ready", snapshot_sha=SNAP, semantic_sha256=SEM)
        report = _evaluate([sample], agent, snapshot_sha=SNAP, semantic_sha256=SEM)
        self.assertTrue(report["ok"], msg=json.dumps(report, ensure_ascii=False, default=str))

    def test_evaluator_never_mutates_samples(self) -> None:
        """R3：评测器绝不回填样本期望（调用前后样本逐字相等）。"""
        agent = _FakeAgent(result=_ok_result())
        sample = _ready_answer_sample()
        frozen = copy.deepcopy(sample)
        _evaluate([sample], agent, snapshot_sha=SNAP, semantic_sha256=SEM)
        self.assertEqual(sample, frozen)


class TestCounterExamples(unittest.TestCase):
    """brief 点名的每一类反例都必须失败（禁止空跑绿灯）。"""

    def test_wrong_analysis_plan_caught(self) -> None:
        wrong = replace(_correct_plan().sub_plans[2], limit=9999)
        bad_plan = replace(
            _correct_plan(),
            sub_plans=(
                _correct_plan().sub_plans[0],
                _correct_plan().sub_plans[1],
                wrong,
                _correct_plan().sub_plans[3],
            ),
        )
        agent = _FakeAgent(result=replace(_ok_result(), plan=bad_plan))
        report = _evaluate([_ready_answer_sample()], agent, snapshot_sha=SNAP, semantic_sha256=SEM)
        self.assertFalse(report["ok"])
        _assert_fail(self, report, "attribution-001", "sub_plans")

    def test_wrong_step_rows_caught(self) -> None:
        """谎言实现：rows 说 B=55，attribution 却按参考数字报——steps_rows 必须揭穿。"""
        steps = list(_ok_steps())
        steps[2] = _step(
            [DIMENSION, METRIC], [["A", Decimal("30")], ["B", Decimal("55")]], "-- cur"
        )
        agent = _FakeAgent(
            result=replace(_ok_result(), steps=(steps[0], steps[1], steps[2], steps[3]))
        )
        report = _evaluate([_ready_answer_sample()], agent, snapshot_sha=SNAP, semantic_sha256=SEM)
        self.assertFalse(report["ok"])
        _assert_fail(self, report, "attribution-001", "steps_rows")

    def test_wrong_total_delta_caught(self) -> None:
        bad_attr = replace(_ok_attribution(), delta=Decimal("-25"))
        agent = _FakeAgent(result=replace(_ok_result(), attribution=bad_attr))
        report = _evaluate([_ready_answer_sample()], agent, snapshot_sha=SNAP, semantic_sha256=SEM)
        self.assertFalse(report["ok"])
        _assert_fail(self, report, "attribution-001", "attribution_totals")

    def test_wrong_contribution_pct_caught(self) -> None:
        items = list(_ok_attribution().items)
        items[0] = replace(items[0], contribution_pct=Decimal("-40.000000"))
        bad_attr = replace(_ok_attribution(), items=(items[0], items[1]))
        agent = _FakeAgent(result=replace(_ok_result(), attribution=bad_attr))
        report = _evaluate([_ready_answer_sample()], agent, snapshot_sha=SNAP, semantic_sha256=SEM)
        self.assertFalse(report["ok"])
        _assert_fail(self, report, "attribution-001", "attribution_items")
        _assert_pass(self, report, "attribution-001", "attribution_totals")

    def test_answer_sample_gets_clarify_caught(self) -> None:
        agent = _FakeAgent(result=_clarify_result())
        report = _evaluate([_ready_answer_sample()], agent, snapshot_sha=SNAP, semantic_sha256=SEM)
        self.assertFalse(report["ok"])
        _assert_fail(self, report, "attribution-001", "kind")

    def test_clarify_sample_gets_answer_caught(self) -> None:
        agent = _FakeAgent(result=_ok_result())
        sample = _clarify_sample(maturity="ready", snapshot_sha=SNAP, semantic_sha256=SEM)
        report = _evaluate([sample], agent, snapshot_sha=SNAP, semantic_sha256=SEM)
        self.assertFalse(report["ok"])
        _assert_fail(self, report, "clarify-001", "kind")

    def test_missing_expectations_gate_and_not_executed(self) -> None:
        sample = _ready_answer_sample()
        del sample["expected_plan"]
        del sample["reference"]
        agent = _FakeAgent(result=_ok_result())
        report = _evaluate([sample], agent, snapshot_sha=SNAP, semantic_sha256=SEM)
        self.assertFalse(report["ok"])
        _assert_fail(self, report, "attribution-001", "sample_schema")
        _assert_fail(self, report, "attribution-001", "plan_template")
        _assert_fail(self, report, "attribution-001", "reference_complete")
        self.assertEqual(agent.calls, [], msg="缺期望的样本不得进执行")

    def test_draft_sample_refused_in_real_mode(self) -> None:
        sample = _ready_answer_sample(maturity="draft")
        agent = _FakeAgent(result=_ok_result())
        report = _evaluate([sample], agent, snapshot_sha=SNAP, semantic_sha256=SEM)
        self.assertFalse(report["ok"])
        _assert_fail(self, report, "attribution-001", "maturity_ready")
        self.assertEqual(agent.calls, [])

    def test_empty_sample_set_refused(self) -> None:
        agent = _FakeAgent(result=_ok_result())
        with self.assertRaises(ValueError):
            _evaluate([], agent, snapshot_sha=SNAP, semantic_sha256=SEM)

    def test_incomplete_reference_blocks_fail(self) -> None:
        """必测项不得静默 skip：ready answer 缺两个角色块 = 失败。"""
        reference = dict(_ready_answer_sample()["reference"])  # type: ignore[arg-type]
        reference["results"] = _reference_results()[:2]  # type: ignore[index]
        sample = _ready_answer_sample(reference=reference)
        agent = _FakeAgent(result=_ok_result())
        report = _evaluate([sample], agent, snapshot_sha=SNAP, semantic_sha256=SEM)
        self.assertFalse(report["ok"])
        _assert_fail(self, report, "attribution-001", "reference_complete")
        self.assertEqual(agent.calls, [])

    def test_reference_metric_column_missing_fails_closed(self) -> None:
        """畸形参考块（列名不含指标同名列）→ oracle fail-closed（评审 Minor-1）。

        数值位置不变：旧实现静默回退取最后一列会恰好比对「通过」；
        修复后该检查项必须记 fail，且原因说明指标列缺失（样本级失败，报告继续）。
        """
        reference = dict(_ready_answer_sample()["reference"])  # type: ignore[arg-type]
        reference["results"] = _mangled_reference_results()  # type: ignore[index]
        sample = _ready_answer_sample(reference=reference)
        agent = _FakeAgent(result=_ok_result())
        report = _evaluate([sample], agent, snapshot_sha=SNAP, semantic_sha256=SEM)
        self.assertFalse(report["ok"])
        totals = _find_check(report, "attribution-001", "attribution_totals")
        self.assertEqual(totals["status"], "fail", msg=str(totals))
        self.assertIn(METRIC, str(totals["detail"]), msg="失败原因必须说明指标列缺失")
        items = _find_check(report, "attribution-001", "attribution_items")
        self.assertEqual(items["status"], "fail", msg=str(items))
        self.assertIn(METRIC, str(items["detail"]), msg="失败原因必须说明指标列缺失")

    def test_snapshot_mismatch_caught(self) -> None:
        sample = _ready_answer_sample(snapshot_sha="other-snap")
        agent = _FakeAgent(result=_ok_result())
        report = _evaluate([sample], agent, snapshot_sha=SNAP, semantic_sha256=SEM)
        self.assertFalse(report["ok"])
        _assert_fail(self, report, "attribution-001", "snapshot_match")
        self.assertEqual(agent.calls, [])

    def test_bindings_mismatch_caught(self) -> None:
        agent = _FakeAgent(result=replace(_ok_result(), snapshot_sha="evil"))
        report = _evaluate([_ready_answer_sample()], agent, snapshot_sha=SNAP, semantic_sha256=SEM)
        self.assertFalse(report["ok"])
        _assert_fail(self, report, "attribution-001", "bindings")

    def test_agent_exception_isolated_not_fatal(self) -> None:
        """单样本异常只判该样本失败，不得吞掉同批其他样本的判定。"""
        q1 = "分析 2013Q4 相对 2013Q3 的佣金收入按分支的变化贡献"
        q2 = "另一个问句"
        agent = _ScriptedAgent({q1: RuntimeError("boom"), q2: _ok_result()})
        s1 = _ready_answer_sample(id="a-1", question=q1)
        s2 = _ready_answer_sample(id="a-2", question=q2)
        report = _evaluate([s1, s2], agent, snapshot_sha=SNAP, semantic_sha256=SEM)
        self.assertFalse(report["ok"])
        _assert_fail(self, report, "a-1", "executed")
        self.assertTrue(report["samples"][1]["ok"])  # type: ignore[index]

    def test_agent_non_result_caught(self) -> None:
        agent = _FakeAgent(result={"not": "a result"})  # type: ignore[assignment]
        report = _evaluate([_ready_answer_sample()], agent, snapshot_sha=SNAP, semantic_sha256=SEM)
        self.assertFalse(report["ok"])
        _assert_fail(self, report, "attribution-001", "executed")

    def test_contract_violation_caught(self) -> None:
        """3 步 answer：闭合矩阵违约（步骤数双射）→ contract + sql_calls 失败。"""
        steps = _ok_steps()[:3]
        agent = _FakeAgent(result=replace(_ok_result(), steps=steps))
        report = _evaluate([_ready_answer_sample()], agent, snapshot_sha=SNAP, semantic_sha256=SEM)
        self.assertFalse(report["ok"])
        _assert_fail(self, report, "attribution-001", "contract")
        _assert_fail(self, report, "attribution-001", "sql_calls")

    def test_unavailable_wrong_reason_caught(self) -> None:
        bad = replace(_zero_delta_result(), reason_code="direction_mismatch")
        agent = _FakeAgent(result=bad)
        report = _evaluate(
            [_ready_unavailable_sample()], agent, snapshot_sha=SNAP, semantic_sha256=SEM
        )
        self.assertFalse(report["ok"])
        _assert_fail(self, report, "unavailable-001", "reason_code")

    def test_zero_delta_must_keep_verified_totals(self) -> None:
        """净零综合丢弃已验证总量 = 违约（决策⑤ L207 修正①），评测器必须抓。"""
        bad = replace(
            _zero_delta_result(),
            attribution=replace(
                _zero_delta_result().attribution,
                baseline=None,
                current=None,  # type: ignore[arg-type]
            ),
        )
        agent = _FakeAgent(result=bad)
        report = _evaluate(
            [_ready_unavailable_sample()], agent, snapshot_sha=SNAP, semantic_sha256=SEM
        )
        self.assertFalse(report["ok"])
        _assert_fail(self, report, "unavailable-001", "attribution_totals")

    def test_row_order_normalized(self) -> None:
        """行序归一：同集合不同行序不误报（比对域 = 集合而非顺序）。"""
        steps = list(_ok_steps())
        steps[2] = _step(
            [DIMENSION, METRIC], [["B", Decimal("50")], ["A", Decimal("30")]], "-- cur"
        )
        agent = _FakeAgent(
            result=replace(_ok_result(), steps=(steps[0], steps[1], steps[2], steps[3]))
        )
        report = _evaluate([_ready_answer_sample()], agent, snapshot_sha=SNAP, semantic_sha256=SEM)
        self.assertTrue(report["ok"], msg=json.dumps(report, ensure_ascii=False, default=str))


class TestOracleNullBucketContract(unittest.TestCase):
    """评测器镜像综合器的 NULL 维度桶契约（R7 裁定，task-9-fixround2）。

    综合器（agent/analysis.py `_grouped_step_rows`）把 NULL 维度键定义为合法
    独立桶：None 键、"(null)" 展示标签、("null",) 平局排序键；编译器对分组步
    无 IS NOT NULL 过滤 → 真链 attribution.items 必含 "(null)" 项。评测器旧
    实现把维度值收窄为字符串-only：参考块含 NULL 行即 oracle 不可用，剔除
    NULL 行即与真链项数不符（attribution_items 逐位精确比较）。裁定评测器是
    缺陷方：本组测试钉住「参考块不剔除 NULL 行」的镜像契约。
    """

    def _null_reference_results(self) -> list[dict[str, object]]:
        """两总量 + 两分组块，维度含 NULL 单元格（数字口径手工书写）。

        base 总 100 / cur 总 80；NULL 桶 40→20（Δ-20，+100%）、A 60→60（Δ0，0%）。
        """
        return [
            {"role": "baseline_total", "columns": [METRIC], "rows": [["100"]]},
            {"role": "current_total", "columns": [METRIC], "rows": [["80"]]},
            {
                "role": "current_by_dimension",
                "columns": [DIMENSION, METRIC],
                "rows": [[None, "20"], ["A", "60"]],
            },
            {
                "role": "baseline_by_dimension",
                "columns": [DIMENSION, METRIC],
                "rows": [[None, "40"], ["A", "60"]],
            },
        ]

    def test_null_bucket_item_built_with_exact_numbers(self) -> None:
        """核心行为：参考块含 null 维度单元格 → oracle 正常构建且数值精确。"""
        from eval.analysis_eval import _oracle_items

        items = _oracle_items(self._null_reference_results(), METRIC, DIMENSION)
        self.assertEqual(len(items), 2)
        null_item = next(item for item in items if item.value == "(null)")
        self.assertEqual(null_item.baseline, Decimal("40"))
        self.assertEqual(null_item.current, Decimal("20"))
        self.assertEqual(null_item.delta, Decimal("-20"))
        self.assertEqual(null_item.contribution_pct, Decimal("100.000000"))
        a_item = next(item for item in items if item.value == "A")
        self.assertEqual(a_item.baseline, Decimal("60"))
        self.assertEqual(a_item.current, Decimal("60"))
        self.assertEqual(a_item.delta, Decimal("0"))
        self.assertEqual(a_item.contribution_pct, Decimal("0.000000"))

    def test_null_bucket_union_with_zero_fill(self) -> None:
        """并集与补零：None 桶仅出现在一侧分组块 → 另一侧补零后仍产出该项。"""
        from eval.analysis_eval import _oracle_items

        results = [
            {"role": "baseline_total", "columns": [METRIC], "rows": [["100"]]},
            {"role": "current_total", "columns": [METRIC], "rows": [["80"]]},
            {
                "role": "current_by_dimension",
                "columns": [DIMENSION, METRIC],
                "rows": [[None, "30"], ["A", "50"]],
            },
            {
                "role": "baseline_by_dimension",
                "columns": [DIMENSION, METRIC],
                "rows": [["A", "60"], ["B", "40"]],
            },
        ]
        items = _oracle_items(results, METRIC, DIMENSION)
        null_items = [item for item in items if item.value == "(null)"]
        self.assertEqual(len(null_items), 1, msg="None 桶并集去重后只产出一次")
        self.assertEqual(null_items[0].baseline, Decimal("0"))
        self.assertEqual(null_items[0].current, Decimal("30"))
        self.assertEqual(null_items[0].delta, Decimal("30"))
        self.assertEqual(null_items[0].contribution_pct, Decimal("-150.000000"))

    def test_null_sort_key_and_tiebreak(self) -> None:
        """排序键：("null",) 先于所有 ("str", …)；等 |delta| 平局时 None 桶在前。

        平局对手用 "!"（字典序在 "(null)" 之前）钉死契约：综合器按原始键排序
        （agent/analysis.py `_typed_dimension_key(draft.key)`），若 oracle 按渲染后
        value 排序，"!" 桶会插到 NULL 桶前、窄平局下与真链顺序相反（修复前红）。
        """
        from eval.analysis_eval import _oracle_items, _typed_key

        self.assertEqual(_typed_key(None), ("null",))
        self.assertLess(_typed_key(None), ("str", "anything"))
        results = [
            {"role": "baseline_total", "columns": [METRIC], "rows": [["100"]]},
            {"role": "current_total", "columns": [METRIC], "rows": [["80"]]},
            {
                "role": "current_by_dimension",
                "columns": [DIMENSION, METRIC],
                "rows": [["!", "30"], [None, "50"]],
            },
            {
                "role": "baseline_by_dimension",
                "columns": [DIMENSION, METRIC],
                "rows": [["!", "40"], [None, "60"]],
            },
        ]
        items = _oracle_items(results, METRIC, DIMENSION)
        # NULL 桶与 "!" 的 |delta| 同为 10：平局必须按原始键类型序把 None 桶排在前
        self.assertEqual([item.value for item in items], ["(null)", "!"])

    def test_literal_null_label_tiebreaks_after_true_null(self) -> None:
        """字面 "(null)" 字符串桶与真 NULL 桶等 |delta| 打平：真 NULL 桶在前。

        按渲染后 value 排序时两者键同为 ("str", "(null)") 无法区分，稳定排序退化为
        插入序（本用例字面桶在前 → 修复前红）；按原始键排序 None 桶恒先于一切
        字符串桶，与综合器两段稳定排序完全同构。
        """
        from eval.analysis_eval import _oracle_items

        results = [
            # 总量与分组和一致：base 50+10+40=100、cur 30+30+60=120（Δ+20 ≠ 0）
            {"role": "baseline_total", "columns": [METRIC], "rows": [["100"]]},
            {"role": "current_total", "columns": [METRIC], "rows": [["120"]]},
            {
                "role": "current_by_dimension",
                "columns": [DIMENSION, METRIC],
                # 字面 "(null)"（str）行在前、None 行在后：按渲染值排序会保留此插入序
                "rows": [["(null)", "30"], [None, "30"], ["Z", "60"]],
            },
            {
                "role": "baseline_by_dimension",
                "columns": [DIMENSION, METRIC],
                "rows": [["(null)", "50"], [None, "10"], ["Z", "40"]],
            },
        ]
        items = _oracle_items(results, METRIC, DIMENSION)
        # 三桶 |delta| 同为 20：顺序必须为 真 NULL（+20）、字面 "(null)"（-20）、"Z"
        self.assertEqual([item.value for item in items], ["(null)", "(null)", "Z"])
        self.assertEqual(items[0].delta, Decimal("20"), msg="真 NULL 桶（None 键）在前")
        self.assertEqual(items[1].delta, Decimal("-20"), msg="字面 (null) 字符串桶在后")

    def test_dimension_cell_fail_closed_on_non_string(self) -> None:
        """fail-closed：维度单元格为数字 42 → ValueError；None / str → 合法。"""
        from eval.analysis_eval import _grouped_values

        with self.assertRaises(ValueError) as ctx:
            _grouped_values(
                [
                    {
                        "role": "current_by_dimension",
                        "columns": [DIMENSION, METRIC],
                        "rows": [[42, "30"]],
                    }
                ],
                "current_by_dimension",
                METRIC,
                DIMENSION,
            )
        self.assertIn("current_by_dimension", str(ctx.exception))
        self.assertIn("42", str(ctx.exception))

        values = _grouped_values(
            [
                {
                    "role": "current_by_dimension",
                    "columns": [DIMENSION, METRIC],
                    "rows": [[None, "30"], ["A", "50"]],
                }
            ],
            "current_by_dimension",
            METRIC,
            DIMENSION,
        )
        self.assertEqual(set(values), {None, "A"})
        self.assertEqual(values[None], Decimal("30"))

    def test_null_label_matches_agent_constant(self) -> None:
        """标签一致性：oracle 渲染值与 agent.analysis._NULL_DIM_LABEL 相等（防漂移）。"""
        from agent.analysis import _NULL_DIM_LABEL
        from eval.analysis_eval import _oracle_items

        items = _oracle_items(self._null_reference_results(), METRIC, DIMENSION)
        null_item = next(item for item in items if item.delta == Decimal("-20"))
        self.assertEqual(null_item.value, _NULL_DIM_LABEL)
        self.assertEqual(null_item.value, "(null)")


class TestRealModeGates(unittest.TestCase):
    """真链模式参数门：缺 sha 即拒绝，不允许默认值。"""

    def test_real_mode_requires_snapshot_sha(self) -> None:
        agent = _FakeAgent(result=_ok_result())
        with self.assertRaises(ValueError):
            _evaluate([_ready_answer_sample()], agent, snapshot_sha=None, semantic_sha256=SEM)

    def test_real_mode_requires_semantic_sha(self) -> None:
        agent = _FakeAgent(result=_ok_result())
        with self.assertRaises(ValueError):
            _evaluate([_ready_answer_sample()], agent, snapshot_sha=SNAP, semantic_sha256=None)

    def test_real_mode_requires_agent(self) -> None:
        with self.assertRaises(ValueError):
            _evaluate([_ready_answer_sample()], None, snapshot_sha=SNAP, semantic_sha256=SEM)


class TestDryMode(unittest.TestCase):
    """--dry：只做结构/计划校验，draft 可跑，执行项标 not_run，绝不写报告文件。"""

    def test_dry_draft_sample_ok_and_not_run(self) -> None:
        agent = _FakeAgent(result=_ok_result())
        sample = _ready_answer_sample(
            maturity="draft",
            snapshot_sha="<待填写>",
            semantic_sha256="<待填写>",
            reference={"sql": None, "results": None, "reviewed_by": None, "reviewed_at": None},
        )
        report = _evaluate([sample], agent, dry=True)
        self.assertTrue(report["ok"], msg=json.dumps(report, ensure_ascii=False, default=str))
        self.assertEqual(report["run"], "not_run")
        self.assertEqual(agent.calls, [], msg="dry 模式不得触达 agent")
        self.assertEqual(report["snapshot_sha"], None)
        _assert_pass(self, report, "attribution-001", "plan_template")
        self.assertEqual(_find_check(report, "attribution-001", "executed")["status"], "not_run")

    def test_dry_broken_template_fails(self) -> None:
        subs = _expected_sub_plans_json()
        subs[1]["filters"] = [{"column": DIMENSION, "op": "=", "value": "A"}]  # 四步不同 WHERE
        sample = _ready_answer_sample(expected_sub_plans=subs)
        report = _evaluate([sample], _FakeAgent(), dry=True)
        self.assertFalse(report["ok"])
        _assert_fail(self, report, "attribution-001", "plan_template")

    def test_dry_wrong_intent_fails(self) -> None:
        sample = _ready_answer_sample()
        sample["expected_plan"] = {**sample["expected_plan"], "intent": "attribution"}  # type: ignore[index]
        report = _evaluate([sample], _FakeAgent(), dry=True)
        self.assertFalse(report["ok"])
        _assert_fail(self, report, "attribution-001", "plan_template")


class TestSnapshotFingerprint(unittest.TestCase):
    """快照指纹复核（before/after）：meta 读取 + 现场测量比对（可注入测量）。"""

    def _write_meta(self, directory: Path, sha: str) -> dict[str, object]:
        meta = {
            "sha": sha,
            "created_at": "2026-09-14T12:00:00+08:00",
            "data_range": {"min": "2013-01-01", "max": "2013-12-31"},
            "raw_size_bytes": 1024,
            "row_counts": {"atlas": {"dwd_fact_trade": 100}},
            "snapshot_ids": {"atlas.dwd_fact_trade": "snap-#1"},
        }
        (directory / f"{sha}.meta.json").write_text(
            json.dumps(meta, ensure_ascii=False), encoding="utf-8"
        )
        return meta

    def test_match(self) -> None:
        from eval.analysis_eval import verify_snapshot_fingerprint

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            meta = self._write_meta(base, SNAP)
            ok, detail = verify_snapshot_fingerprint(
                SNAP, snapshots_dir=base, measure=lambda: dict(meta)
            )
            self.assertTrue(ok, msg=detail)
            self.assertIsNone(detail)

    def test_drift(self) -> None:
        from eval.analysis_eval import verify_snapshot_fingerprint

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            meta = self._write_meta(base, SNAP)

            def drifted() -> dict[str, object]:
                return {**meta, "row_counts": {"atlas": {"dwd_fact_trade": 999}}}

            ok, detail = verify_snapshot_fingerprint(SNAP, snapshots_dir=base, measure=drifted)
            self.assertFalse(ok)
            self.assertIn("row_counts", detail or "")

    def test_missing_meta(self) -> None:
        from eval.analysis_eval import verify_snapshot_fingerprint

        with tempfile.TemporaryDirectory() as tmp:
            ok, detail = verify_snapshot_fingerprint(SNAP, snapshots_dir=Path(tmp), measure=dict)
            self.assertFalse(ok)
            self.assertIsNotNone(detail)

    def test_meta_sha_mismatch(self) -> None:
        from eval.analysis_eval import verify_snapshot_fingerprint

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            self._write_meta(base, "shaAAA")
            ok, detail = verify_snapshot_fingerprint("shaBBB", snapshots_dir=base, measure=dict)
            self.assertFalse(ok)

    def test_evaluate_raises_on_drift_before_run(self) -> None:
        from eval.analysis_eval import SnapshotVerifyError

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            self._write_meta(base, SNAP)
            agent = _FakeAgent(result=_ok_result())
            with self.assertRaises(SnapshotVerifyError):
                _evaluate(
                    [_ready_answer_sample()],
                    agent,
                    snapshot_sha=SNAP,
                    semantic_sha256=SEM,
                    snapshots_dir=base,
                    measure=lambda: {"row_counts": {}},
                )
            self.assertEqual(agent.calls, [], msg="指纹漂移必须在运行前拒绝")


class TestCli(unittest.TestCase):
    """CLI：退出码 0/1/2 与报告落盘契约。"""

    def _run_main(self, argv: list[str], *, agent: object = None, measure: object = None) -> int:
        from eval.analysis_eval import main

        return main(argv, agent=agent, measure=measure)  # type: ignore[arg-type]

    def test_missing_snapshot_sha_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"ATLAS_GIT_SHA": "testsha1"}):
                code = self._run_main(["--report-dir", tmp], agent=_FakeAgent(result=_ok_result()))
            self.assertEqual(code, 2)
            self.assertEqual(list(Path(tmp).iterdir()), [], msg="拒绝时不得产出报告")

    def test_empty_samples_dir_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            samples = Path(tmp) / "finance"
            samples.mkdir()
            reports = Path(tmp) / "reports"
            with mock.patch.dict(os.environ, {"ATLAS_GIT_SHA": "testsha1"}):
                code = self._run_main(
                    [
                        "--snapshot-sha",
                        SNAP,
                        "--samples",
                        str(tmp),
                        "--report-dir",
                        str(reports),
                        "--model",
                        str(REPO_ROOT / "semantic/ossie/atlas_finance.ossie.yaml"),
                        "--snapshots-dir",
                        str(_fake_snapshots_dir()),
                    ],
                    agent=_FakeAgent(result=_ok_result()),
                )
            self.assertEqual(code, 2)

    def test_real_run_happy_writes_report(self) -> None:
        sem = _finance_semantic_sha()
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            samples = base / "finance"
            samples.mkdir()
            (samples / "attribution-001.json").write_text(
                json.dumps(_ready_answer_sample(semantic_sha256=sem), ensure_ascii=False),
                encoding="utf-8",
            )
            meta = {
                "sha": SNAP,
                "created_at": "2026-09-14T12:00:00+08:00",
                "data_range": {"min": "2013-01-01", "max": "2013-12-31"},
                "raw_size_bytes": 1024,
                "row_counts": {"atlas": {"dwd_fact_trade": 100}},
                "snapshot_ids": {"atlas.dwd_fact_trade": "snap-#1"},
            }
            (base / f"{SNAP}.meta.json").write_text(json.dumps(meta), encoding="utf-8")
            reports = base / "reports"
            with mock.patch.dict(os.environ, {"ATLAS_GIT_SHA": "testsha1"}):
                code = self._run_main(
                    [
                        "--snapshot-sha",
                        SNAP,
                        "--samples",
                        str(base),
                        "--report-dir",
                        str(reports),
                        "--model",
                        str(REPO_ROOT / "semantic/ossie/atlas_finance.ossie.yaml"),
                        "--snapshots-dir",
                        str(base),
                    ],
                    agent=_FakeAgent(result=_ok_result(semantic_sha256=sem)),
                    measure=lambda: dict(meta),
                )
            self.assertEqual(code, 0)
            report_path = reports / "analysis-testsha1.json"
            self.assertTrue(report_path.is_file())
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertTrue(report["ok"])
            self.assertEqual(report["snapshot_sha"], SNAP)
            self.assertIn("semantic_sha256", report)

    def test_real_run_failure_exit_1(self) -> None:
        sem = _finance_semantic_sha()
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            samples = base / "finance"
            samples.mkdir()
            (samples / "attribution-001.json").write_text(
                json.dumps(_ready_answer_sample(semantic_sha256=sem), ensure_ascii=False),
                encoding="utf-8",
            )
            meta = {
                "sha": SNAP,
                "created_at": "2026-09-14T12:00:00+08:00",
                "data_range": {"min": "2013-01-01", "max": "2013-12-31"},
                "raw_size_bytes": 1024,
                "row_counts": {"atlas": {"dwd_fact_trade": 100}},
                "snapshot_ids": {"atlas.dwd_fact_trade": "snap-#1"},
            }
            (base / f"{SNAP}.meta.json").write_text(json.dumps(meta), encoding="utf-8")
            reports = base / "reports"
            with mock.patch.dict(os.environ, {"ATLAS_GIT_SHA": "testsha1"}):
                code = self._run_main(
                    [
                        "--snapshot-sha",
                        SNAP,
                        "--samples",
                        str(base),
                        "--report-dir",
                        str(reports),
                        "--model",
                        str(REPO_ROOT / "semantic/ossie/atlas_finance.ossie.yaml"),
                        "--snapshots-dir",
                        str(base),
                    ],
                    agent=_FakeAgent(
                        result=replace(
                            _ok_result(semantic_sha256=sem),
                            attribution=replace(_ok_attribution(), delta=Decimal("-25")),
                        )
                    ),
                    measure=lambda: dict(meta),
                )
            self.assertEqual(code, 1)
            report = json.loads((reports / "analysis-testsha1.json").read_text(encoding="utf-8"))
            self.assertFalse(report["ok"])
            self.assertEqual(report["summary"]["checks_failed"], 1)

    def test_fingerprint_drift_after_run_refuses_report(self) -> None:
        sem = _finance_semantic_sha()
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            samples = base / "finance"
            samples.mkdir()
            (samples / "attribution-001.json").write_text(
                json.dumps(_ready_answer_sample(semantic_sha256=sem), ensure_ascii=False),
                encoding="utf-8",
            )
            meta = {
                "sha": SNAP,
                "created_at": "2026-09-14T12:00:00+08:00",
                "data_range": {"min": "2013-01-01", "max": "2013-12-31"},
                "raw_size_bytes": 1024,
                "row_counts": {"atlas": {"dwd_fact_trade": 100}},
                "snapshot_ids": {"atlas.dwd_fact_trade": "snap-#1"},
            }
            (base / f"{SNAP}.meta.json").write_text(json.dumps(meta), encoding="utf-8")
            reports = base / "reports"
            calls = {"n": 0}

            def drift_after_first() -> dict[str, object]:
                calls["n"] += 1
                if calls["n"] == 1:
                    return dict(meta)
                return {**meta, "row_counts": {"atlas": {"dwd_fact_trade": 404}}}

            with mock.patch.dict(os.environ, {"ATLAS_GIT_SHA": "testsha1"}):
                code = self._run_main(
                    [
                        "--snapshot-sha",
                        SNAP,
                        "--samples",
                        str(base),
                        "--report-dir",
                        str(reports),
                        "--model",
                        str(REPO_ROOT / "semantic/ossie/atlas_finance.ossie.yaml"),
                        "--snapshots-dir",
                        str(base),
                    ],
                    agent=_FakeAgent(result=_ok_result(semantic_sha256=sem)),
                    measure=drift_after_first,
                )
            self.assertEqual(code, 2, msg="运行后数据漂移 → 报告无效，不得落盘")
            self.assertEqual(list(reports.iterdir()) if reports.exists() else [], [])

    def test_dry_repo_samples_exit_0_and_no_report(self) -> None:
        """对仓库样本 dry 跑：结构全过 → 0；不写任何报告文件。"""
        reports_dir = REPO_ROOT / "eval" / "reports"
        before = {p.name for p in reports_dir.glob("analysis-*.json")}
        with mock.patch.dict(os.environ, {"ATLAS_GIT_SHA": "testsha1"}):
            code = self._run_main(["--dry"], agent=_FakeAgent())
        self.assertEqual(code, 0)
        after = {p.name for p in reports_dir.glob("analysis-*.json")}
        self.assertEqual(before, after, msg="dry 模式不得写报告文件")


class TestE2EAcceptanceReportGates(unittest.TestCase):
    """T10 验收报告门禁（task-10-brief TDD 顺序，不依赖真库）。

    五条门禁单测钉住 e2e_acceptance 的报告契约：

    1. run_scenario 捕获单场景异常记 fail（含 error 摘要）继续跑完（不中断）；
    2. summary 从实际 scenario status 生成（禁止 passed=len(scenarios) 硬编码）；
    3. 报告构建含 code_sha 非空 + snapshot_sha；
    4. 分析场景资格证据缺失 → 失败信号（非 skip——快照缺资格证据时场景失败
       而非静默跳过）；
    5. `e2e-acceptance-<hex>.json` 的 hex ≠ git_short_sha → 拒绝归档。
    """

    def test_scenario_failure_recorded_not_fatal(self) -> None:
        from eval.e2e_acceptance import run_scenario

        def boom() -> dict[str, object]:
            raise AssertionError("S-x 期望 answer，实际 blocked")

        def fine() -> dict[str, object]:
            return {"kind": "answer"}

        failed = run_scenario("S-x", "抛异常场景", "异常必须记 fail", boom)
        self.assertEqual(failed["status"], "fail")
        self.assertIn("AssertionError", str(failed["error"]))
        self.assertIn("期望 answer", str(failed["error"]))
        passed = run_scenario("S-y", "正常场景", "异常不得中断后续场景", fine)
        self.assertEqual(passed["status"], "pass")

    def test_summary_counts_from_actual_statuses(self) -> None:
        from eval.e2e_acceptance import build_summary

        scenarios = [
            {"id": "S1", "status": "pass"},
            {"id": "S2", "status": "pass"},
            {"id": "S3", "status": "fail", "error": "boom"},
        ]
        self.assertEqual(build_summary(scenarios), {"total": 3, "passed": 2, "failed": 1})

    def test_report_carries_code_sha_and_snapshot_sha(self) -> None:
        from eval.e2e_acceptance import build_report

        scenarios = [
            {"id": "S1", "status": "pass"},
            {"id": "S2", "status": "fail", "error": "boom"},
        ]
        report = build_report(
            scenarios,
            snapshot_sha="7c966e9",
            code_sha="b95a9e9",
            created_at="2026-09-16T00:00:00+00:00",
        )
        self.assertEqual(report["code_sha"], "b95a9e9")
        self.assertTrue(report["code_sha"], msg="code_sha 必须非空")
        self.assertEqual(report["snapshot_sha"], "7c966e9")
        # summary 与报告同源：从实际 status 统计，不是 len(scenarios)
        self.assertEqual(report["summary"], {"total": 2, "passed": 1, "failed": 1})

    def test_analysis_scenario_requires_eligibility_evidence(self) -> None:
        from eval.e2e_acceptance import require_eligibility_evidence, run_scenario

        with tempfile.TemporaryDirectory() as tmp:
            # 证据文件缺失 → 拒绝（失败信号，非 skip）
            with self.assertRaises(AssertionError) as ctx:
                require_eligibility_evidence("deadbee", snapshots_dir=Path(tmp))
            self.assertIn("deadbee", str(ctx.exception))

            # 走 run_scenario 通道：门失败 = 场景记 fail（不是静默跳过/缺席）
            def gated() -> dict[str, object]:
                require_eligibility_evidence("deadbee", snapshots_dir=Path(tmp))
                return {"kind": "answer"}  # pragma: no cover - 不可达

            record = run_scenario("S8", "绝对期间贡献", "缺证据必须记 fail", gated)
            self.assertEqual(record["status"], "fail")
            self.assertIn("deadbee", str(record["error"]))

            # 证据存在但 eligible 非 true → 仍拒绝（fail-closed）
            (Path(tmp) / "cafebad.analysis.json").write_text(
                json.dumps({"snapshot_sha": "cafebad", "eligible": False}), encoding="utf-8"
            )
            with self.assertRaises(AssertionError):
                require_eligibility_evidence("cafebad", snapshots_dir=Path(tmp))

            # 合格证据 → 放行并返回证据内容
            evidence = {
                "snapshot_sha": "beefcafe",
                "eligible": True,
                "semantic_sha256": "sem001",
            }
            (Path(tmp) / "beefcafe.analysis.json").write_text(
                json.dumps(evidence), encoding="utf-8"
            )
            loaded = require_eligibility_evidence("beefcafe", snapshots_dir=Path(tmp))
            self.assertEqual(loaded["snapshot_sha"], "beefcafe")
            self.assertTrue(loaded["eligible"])

    def test_report_filename_sha_gate(self) -> None:
        from eval.e2e_acceptance import validate_report_path

        # sha 形态文件名与当前 HEAD 不符 → 拒绝归档（不手造报告）
        with self.assertRaises(ValueError):
            validate_report_path("eval/reports/e2e-acceptance-0000000.json", "b95a9e9")
        # sha 一致 → 放行
        self.assertEqual(
            validate_report_path("eval/reports/e2e-acceptance-b95a9e9.json", "b95a9e9"),
            Path("eval/reports/e2e-acceptance-b95a9e9.json"),
        )
        # 非 sha 形态路径不受闸门约束（既有 --report 任意路径行为保留）
        self.assertEqual(
            validate_report_path("eval/reports/e2e-acceptance.json", "b95a9e9"),
            Path("eval/reports/e2e-acceptance.json"),
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
