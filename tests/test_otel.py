"""observability/otel.py 埋点契约测试（Day 50，批次 E）。

验收口径（第 8 周交付门槛的机器可检查部分）
- trace 能追到 question_id → metric_id → SQL → rows → cost（answer 回合）；
- 确定性链路（0 token）不虚构 gen_ai 属性与指标数据点（诚实）；
- Guard 拒绝回合带 block_reason 且进 blocked 计数；
- snapshot 版本（data_version）随 span 上报（绑定才有，不编造）；
- 埋点故障被隔离：不影响主链路，仅告警一次。
"""

from __future__ import annotations

import unittest
from pathlib import Path
from typing import Any

from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Span

from agent.graph import DataAgent
from agent.security.sql_guard import Budget
from agent.state import TurnResult

REPO = Path(__file__).resolve().parent.parent
SNAPSHOT_SHA = "7d48dcb"


def _meta() -> dict[str, Any]:
    import json

    p = REPO / f"data/snapshots/{SNAPSHOT_SHA}.meta.json"
    return json.loads(p.read_text(encoding="utf-8"))


def _budget() -> Budget:
    meta = _meta()
    allowed = frozenset(
        f"atlas.{ns}.{table}" for ns, tables in meta["row_counts"].items() for table in tables
    )
    return Budget(dialect="doris", max_rows=10_000, allowed_tables=allowed)


class _Exec:
    """记录 Guard 出口 SQL，返回固定结果集（与 tests/test_graph.py FakeExecutor 同构）。"""

    def __init__(self, rows: list[tuple[Any, ...]] = (("branch-a", "100"),)) -> None:
        self.calls: list[str] = []
        self.rows = rows

    def __call__(self, sql: str) -> tuple[list[tuple[Any, ...]], list[str]]:
        self.calls.append(sql)
        return [tuple(r) for r in self.rows], ["Branch", "commission_revenue"]


class OtelSpanTestBase(unittest.TestCase):
    """每测试重建 in-memory provider + reader（configure_otel 幂等以最后一次为准）。"""

    def setUp(self) -> None:
        from observability.otel import configure_otel

        self.exporter = InMemorySpanExporter()
        self.reader = InMemoryMetricReader()
        configure_otel(
            tracer_provider=TracerProvider(
                active_span_processor=SimpleSpanProcessor(self.exporter)
            ),
            metric_reader=self.reader,
        )
        self.meta = _meta()
        self.budget = _budget()
        self.executor = _Exec()
        self.agent = DataAgent(executor=self.executor, budget=self.budget, snapshot_meta=self.meta)

    def finished_spans(self) -> list[Span]:
        return list(self.exporter.get_finished_spans())

    def span_attrs(self, span: Span) -> dict[str, Any]:
        return dict((span.attributes or {}).items())

    def metric_value(self, name: str) -> int | float | None:
        """在 InMemoryMetricReader 的导出中找 name 的数据点求和（None = 无数据点）。"""
        data = self.reader.get_metrics_data()
        for metric in data.resource_metrics[0].scope_metrics[0].metrics:
            if metric.name != name:
                continue
            total = 0
            for point in metric.data.data_points:
                total += point.value  # type: ignore[attr-defined]
            return total
        return None


class TestTraceChain(OtelSpanTestBase):
    GOLD102_Q = "按分支统计 2013 年佣金收入，列出前 5 名"

    def test_answer_turn_span_full_chain(self) -> None:
        """answer 回合：question_id → metric_id → SQL → rows → snapshot 版本齐全。"""
        r = self.agent.ask(self.GOLD102_Q, session_id="trace-1")
        self.assertEqual(r.kind, "answer")
        spans = self.finished_spans()
        self.assertEqual(len(spans), 1)
        attrs = self.span_attrs(spans[0])
        self.assertEqual(attrs["atlas.question_id"], "trace-1#t1")
        self.assertEqual(attrs["atlas.session_id"], "trace-1")
        self.assertEqual(attrs["atlas.metric_id"], "commission_revenue")
        self.assertIn("SELECT", attrs["atlas.turn.sql"])
        self.assertEqual(attrs["atlas.turn.rows"], 1)
        self.assertEqual(attrs["atlas.turn.kind"], "answer")
        self.assertEqual(attrs["atlas.path"], "deterministic")
        self.assertEqual(attrs["atlas.snapshot.data_version"], SNAPSHOT_SHA)
        # 确定性链路 0 token：不虚构 gen_ai 属性（诚实，N2）
        self.assertNotIn("gen_ai.usage.prompt_tokens", attrs)
        self.assertNotIn("gen_ai.request.model", attrs)
        self.assertNotIn("gen_ai.prompt.version", attrs)

    def test_question_id_increments_with_session_turns(self) -> None:
        """同一 session 第二回合 question_id 轮数递增（trace 可回放会话）。"""
        self.agent.ask(self.GOLD102_Q, session_id="trace-2")
        self.agent.ask(self.GOLD102_Q, session_id="trace-2")
        ids = [self.span_attrs(s)["atlas.question_id"] for s in self.finished_spans()]
        self.assertEqual(ids, ["trace-2#t1", "trace-2#t2"])

    def test_guard_blocked_turn_carries_reason(self) -> None:
        """Guard 拒绝：span 带 block_reason、rows=0、blocked 计数 1（拒绝率口径）。"""
        meta = _meta()
        narrow = {
            "sha": meta["sha"],
            "created_at": meta["created_at"],
            "row_counts": {"dwd": {"dim_date": meta["row_counts"]["dwd"]["dim_date"]}},
        }
        budget = Budget(
            dialect="doris",
            max_rows=10_000,
            allowed_tables=frozenset(f"atlas.dwd.{t}" for t in narrow["row_counts"]["dwd"]),
        )
        agent = DataAgent(executor=_Exec(), budget=budget, snapshot_meta=narrow)
        r = agent.ask(self.GOLD102_Q)
        self.assertEqual(r.kind, "blocked")
        self.assertIsNotNone(r.block_reason)
        attrs = self.span_attrs(self.finished_spans()[-1])
        self.assertEqual(attrs["atlas.turn.kind"], "blocked")
        self.assertIn("表不在白名单内", attrs["atlas.block_reason"])  # 拒绝原因随 span（审计留痕）
        self.assertEqual(attrs["atlas.turn.rows"], 0)
        self.assertEqual(self.metric_value("atlas.turn.blocked"), 1)
        # blocked 回合同样计回合数（QPS/拒绝率分母一致）
        self.assertEqual(self.metric_value("atlas.turn.count"), 1)


class TestTokenCostMetric(OtelSpanTestBase):
    def test_llm_tokens_recorded_when_usage_nonzero(self) -> None:
        """LLM 真参与（usage>0）才记 gen_ai 属性与 token_cost 指标；0 token 无数据点。"""
        from observability.otel import record_turn

        llm_turn = TurnResult(
            kind="answer",
            session_id="llm-1",
            question="候选链问句",
            metric="commission_revenue",
            engine="openai",
            path="candidate",
            usage={"prompt_tokens": 12, "completion_tokens": 8},
            latency_ms=321.5,
            row_count=5,
        )
        record_turn(llm_turn, snapshot_sha=SNAPSHOT_SHA)
        attrs = self.span_attrs(self.finished_spans()[-1])
        self.assertEqual(attrs["gen_ai.usage.prompt_tokens"], 12)
        self.assertEqual(attrs["gen_ai.usage.completion_tokens"], 8)
        self.assertEqual(attrs["gen_ai.request.model"], "openai")
        self.assertEqual(attrs["gen_ai.prompt.version"], "0.1.0")  # generator_plan.yaml
        # token_cost 单位 token：12 + 8 = 20（无定价表不换算 USD）
        self.assertEqual(self.metric_value("gen_ai.token_cost"), 20)

    def test_zero_token_turn_produces_no_cost_data_point(self) -> None:
        """确定性回合：gen_ai.token_cost 无数据点（不虚报 LLM 消耗）。"""
        # setUp 里 gold-102 ask 已产生一回合 deterministic；再直接打点一回合
        from observability.otel import record_turn

        det_turn = TurnResult(
            kind="answer",
            session_id="det-1",
            question="确定性问句",
            metric="commission_revenue",
            engine="deterministic",
            usage={"prompt_tokens": 0, "completion_tokens": 0},
        )
        record_turn(det_turn)
        self.assertIsNone(self.metric_value("gen_ai.token_cost"))
        attrs = self.span_attrs(self.finished_spans()[-1])
        self.assertNotIn("gen_ai.request.model", attrs)


class TestTelemetryIsolation(OtelSpanTestBase):
    def test_broken_record_never_breaks_main_chain(self) -> None:
        """埋点输入畸形 → 告警一次且不抛；此后正常回合仍能埋点（不永久熔断）。"""
        from observability.otel import record_turn

        with self.assertWarns(RuntimeWarning):
            record_turn("not-a-turn-result")  # type: ignore[arg-type]
        # 主链路不受影响：正常回合照常执行 + 埋点
        r = self.agent.ask("2013 年佣金收入按分支前 5", session_id="iso-1")
        self.assertEqual(r.kind, "answer")
        self.assertEqual(len(self.finished_spans()), 1)

    def test_default_noop_without_configure(self) -> None:
        """未 configure（无 provider）时 ask 不炸、无 span 可查（零 I/O 承诺）。"""
        # 重建纯对象不调 configure_otel；record_turn 内部惰性构造 no-op provider
        from observability.otel import configure_otel, record_turn

        configure_otel()  # no endpoint / 无注入 → no-op 配置
        r = DataAgent(executor=_Exec(), budget=self.budget).ask(
            "按分支统计 2013 年佣金收入，列出前 5 名"
        )
        self.assertEqual(r.kind, "answer")
        record_turn(r)


if __name__ == "__main__":
    unittest.main()
