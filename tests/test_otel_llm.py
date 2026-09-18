"""B5 · LLM 意图可观测测试（ADR-0029 ⑦ / N9）——分级+计数入 span，原文/密钥/结果数值零泄漏。

零网络 / 零 GPU。两类断言：
- **记录器契约**：`atlas.llm.narrative` span 属性齐（intent/data_class/tier/grounded/
  fallback/reason/token/model），token>0 才进 `gen_ai.token_cost`；0 token（回落/deny）
  **不虚构**数据点、不写 gen_ai 属性（与回合埋点同口径，诚实）。
- **成本与 Guard Budget 分离**（⑦）：LLM token 只进 `gen_ai.token_cost`，`sql_guard.Budget`
  的 `max_cost_units`（扫描广度）不掺入任何 token——量纲不同、互不喂入。
- **N9 非泄漏**（by construction + serving 端到端）：LLM span 属性是**低基数枚举/布尔/计数**
  集合（无 prompt 文本 / 叙述文本 / 结果数值）；注入的自托管 `api_key` 绝不出现在任何导出
  span 中（密钥无记录入口）。
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Span

from agent.graph import DataAgent
from agent.llm_policy import LlmConfig
from agent.security.sql_guard import Budget
from observability.otel import configure_otel, record_llm_narrative
from serving.api import API_PREFIX, create_app
from serving.audit import AuditLog
from serving.auth import sign_token

REPO = Path(__file__).resolve().parent.parent
SNAPSHOT_SHA = "7d48dcb"
GOLD102_Q = "按分支统计 2013 年佣金收入，列出前 5 名"

# LLM span 允许出现的属性键全集（低基数枚举/布尔/计数）——**无**任何文本/SQL/结果数值键，
# 结构上保证 prompt 原文与叙述文本不落遥测（N9）。
_ALLOWED_LLM_SPAN_KEYS = frozenset(
    {
        "atlas.llm.intent",
        "atlas.llm.data_class",
        "atlas.llm.tier",
        "atlas.llm.action",
        "atlas.llm.grounded",
        "atlas.llm.fallback",
        "atlas.llm.reason_code",
        "gen_ai.usage.prompt_tokens",
        "gen_ai.usage.completion_tokens",
        "gen_ai.request.model",
    }
)


def _budget() -> Budget:
    meta = json.loads((REPO / f"data/snapshots/{SNAPSHOT_SHA}.meta.json").read_text("utf-8"))
    allowed = frozenset(
        f"atlas.{ns}.{table}" for ns, tables in meta["row_counts"].items() for table in tables
    )
    return Budget(dialect="doris", max_rows=10_000, allowed_tables=allowed)


class _OtelBase(unittest.TestCase):
    """每测试重建 in-memory provider + reader（configure_otel 幂等，最后一次为准）。"""

    def setUp(self) -> None:
        self.exporter = InMemorySpanExporter()
        self.reader = InMemoryMetricReader()
        configure_otel(
            tracer_provider=TracerProvider(
                active_span_processor=SimpleSpanProcessor(self.exporter)
            ),
            metric_reader=self.reader,
        )

    def tearDown(self) -> None:
        configure_otel()  # 复位全局 provider，避免污染其它测试族

    def spans(self) -> list[Span]:
        return list(self.exporter.get_finished_spans())

    def attrs(self, span: Span) -> dict[str, Any]:
        return dict(span.attributes or {})

    def first_span(self, name: str) -> Span | None:
        return next((s for s in self.spans() if s.name == name), None)

    def metric_sum(self, name: str) -> int | float | None:
        data = self.reader.get_metrics_data()
        rms = data.resource_metrics if data is not None else []
        total = None
        for rm in rms:
            for sm in rm.scope_metrics:
                for metric in sm.metrics:
                    if metric.name != name:
                        continue
                    for point in metric.data.data_points:
                        total = (total or 0) + point.value  # type: ignore[attr-defined]
        return total

    def all_attr_values(self) -> list[str]:
        out: list[str] = []
        for span in self.spans():
            for value in (span.attributes or {}).values():
                out.append(str(value))
        return out


class TestRecorderContract(_OtelBase):
    def test_grounded_narrative_span_full_attrs(self) -> None:
        """接地发货：span 含分级 + token 计数 + model；token 进 gen_ai.token_cost（=15）。"""
        record_llm_narrative(
            intent="narrative",
            data_class="result-bearing",
            tier="self_hosted",
            action="use_backend",
            grounded=True,
            fallback=False,
            reason_code=None,
            model="atlas-instruct",
            usage={"prompt_tokens": 10, "completion_tokens": 5},
        )
        span = self.first_span("atlas.llm.narrative")
        assert span is not None
        attrs = self.attrs(span)
        self.assertEqual(attrs["atlas.llm.intent"], "narrative")
        self.assertEqual(attrs["atlas.llm.data_class"], "result-bearing")
        self.assertEqual(attrs["atlas.llm.tier"], "self_hosted")
        self.assertEqual(attrs["atlas.llm.grounded"], True)
        self.assertEqual(attrs["atlas.llm.fallback"], False)
        self.assertEqual(attrs["gen_ai.usage.prompt_tokens"], 10)
        self.assertEqual(attrs["gen_ai.usage.completion_tokens"], 5)
        self.assertEqual(attrs["gen_ai.request.model"], "atlas-instruct")
        # token 成本单位 token：10 + 5 = 15（无定价表不换算 USD）
        self.assertEqual(self.metric_sum("gen_ai.token_cost"), 15)
        self.assertEqual(self.metric_sum("atlas.llm.narrative.count"), 1)

    def test_fallback_template_no_token_fabricated(self) -> None:
        """缺自托管回落（未真调）：span grounded=false/fallback=true + reason；不虚构 token。"""
        record_llm_narrative(
            intent="narrative",
            data_class="result-bearing",
            tier="none",
            action="fallback_template",
            grounded=False,
            fallback=True,
            reason_code="self_hosted_unavailable",
        )
        attrs = self.attrs(self.first_span("atlas.llm.narrative"))  # type: ignore[arg-type]
        self.assertEqual(attrs["atlas.llm.fallback"], True)
        self.assertEqual(attrs["atlas.llm.reason_code"], "self_hosted_unavailable")
        self.assertNotIn("gen_ai.usage.prompt_tokens", attrs)
        self.assertNotIn("gen_ai.request.model", attrs)
        # 0 token → gen_ai.token_cost 无数据点（不虚报模型调用）
        self.assertIsNone(self.metric_sum("gen_ai.token_cost"))

    def test_ungrounded_carries_consumed_tokens(self) -> None:
        """新数字被丢：文本发模板，但模型已跑的 token 据实携带（成本不因回落而漏计，⑦）。"""
        record_llm_narrative(
            intent="narrative",
            data_class="result-bearing",
            tier="none",
            action="use_backend",
            grounded=False,
            fallback=True,
            reason_code="ungrounded",
            model="atlas-instruct",
            usage={"prompt_tokens": 10, "completion_tokens": 5},
        )
        attrs = self.attrs(self.first_span("atlas.llm.narrative"))  # type: ignore[arg-type]
        self.assertEqual(attrs["atlas.llm.reason_code"], "ungrounded")
        self.assertEqual(attrs["gen_ai.usage.prompt_tokens"], 10)
        self.assertEqual(self.metric_sum("gen_ai.token_cost"), 15)

    def test_llm_span_is_text_free_by_construction(self) -> None:
        """LLM span 属性键 ⊆ 低基数枚举集（结构保证：无 prompt/叙述/SQL/结果数值落遥测）。"""
        record_llm_narrative(
            intent="narrative",
            data_class="result-bearing",
            tier="self_hosted",
            action="use_backend",
            grounded=True,
            fallback=False,
            reason_code=None,
            model="atlas-instruct",
            usage={"prompt_tokens": 1, "completion_tokens": 1},
        )
        attrs = self.attrs(self.first_span("atlas.llm.narrative"))  # type: ignore[arg-type]
        self.assertLessEqual(set(attrs), _ALLOWED_LLM_SPAN_KEYS)

    def test_broken_record_is_isolated(self) -> None:
        """埋点故障被吞（告警一次），不抛、不反噬主链路（与回合埋点同纪律）。"""
        import observability.otel as otel

        orig = otel._record_llm_narrative_impl
        otel._WARNED_LLM_FAILURE = False
        otel._record_llm_narrative_impl = _boom  # type: ignore[assignment]
        try:
            with self.assertWarns(RuntimeWarning):
                record_llm_narrative(
                    intent="narrative",
                    data_class="result-bearing",
                    tier="self_hosted",
                    action="use_backend",
                    grounded=True,
                    fallback=False,
                    reason_code=None,
                )
        finally:
            otel._record_llm_narrative_impl = orig  # type: ignore[assignment]
            otel._WARNED_LLM_FAILURE = False


def _boom(**_kwargs: Any) -> None:
    raise RuntimeError("injected recorder failure")


class _FakeNarrClient:
    def __init__(self, text: str) -> None:
        self._text = text
        self.called = 0

    def complete(self, system: str, user: str, model: str) -> tuple[str, dict[str, int]]:
        self.called += 1
        return self._text, {"prompt_tokens": 7, "completion_tokens": 4}


class TestServingLlmTelemetry(_OtelBase):
    """端到端 N9：真走 /ask llm=narrative，密钥与结果数值绝不出现在导出遥测中。"""

    SECRET_KEY = "sk-SENTINEL-apikey-must-never-appear"

    def setUp(self) -> None:
        super().setUp()
        self._secret_was = os.environ.get("ATLAS_JWT_SECRET")
        os.environ["ATLAS_JWT_SECRET"] = "otel-llm-test-secret"
        self._tmp = tempfile.TemporaryDirectory(prefix="atlas-otel-llm-")
        self.audit = AuditLog(Path(self._tmp.name))
        self.agent = DataAgent(executor=lambda _s: ([("v",)], ["v"]), budget=_budget())

    def tearDown(self) -> None:
        self._tmp.cleanup()
        if self._secret_was is not None:
            os.environ["ATLAS_JWT_SECRET"] = self._secret_was
        else:
            os.environ.pop("ATLAS_JWT_SECRET", None)
        super().tearDown()

    def _ask_narrative(self, client: TestClient, role: str = "hq_admin") -> Any:
        tok = sign_token(
            role,
            {"branch": "east"} if role == "branch_manager" else {},
            secret="otel-llm-test-secret",
        )
        return client.post(
            f"{API_PREFIX}/ask",
            json={"question": GOLD102_Q, "llm": "narrative"},
            headers={"Authorization": f"Bearer {tok}"},
        )

    def test_no_api_key_leaks_into_any_span(self) -> None:
        """自托管 api_key 注入后：任何导出 span 的属性值都不含该密钥（N9）。"""
        cfg = LlmConfig.with_self_hosted(
            base_url="http://vllm:8000/v1", model_name="atlas-instruct", api_key=self.SECRET_KEY
        )
        with TestClient(
            create_app(
                agent_factory=lambda _m: self.agent,
                audit=self.audit,
                llm_config=cfg,
                self_hosted_probe=lambda _c: True,
                narrative_client_factory=lambda _ep: _FakeNarrClient("East 分支佣金收入最高。"),
            )
        ) as client:
            resp = self._ask_narrative(client)
        self.assertEqual(resp.status_code, 200)
        span = self.first_span("atlas.llm.narrative")
        self.assertIsNotNone(span, "narrative 请求应产出 atlas.llm.narrative span")
        leaked = [v for v in self.all_attr_values() if self.SECRET_KEY in v]
        self.assertEqual(leaked, [], "N9：密钥不得出现在任何遥测属性值中")

    def test_denied_intent_recorded_then_403(self) -> None:
        """无 narrate 能力角色 → 先落 deny span（无 token）再抛 403（拒因可观测）。"""
        with TestClient(
            create_app(
                agent_factory=lambda _m: self.agent,
                audit=self.audit,
                llm_config=LlmConfig(),
            )
        ) as client:
            resp = self._ask_narrative(client, "branch_manager")
        self.assertEqual(resp.status_code, 403)
        span = self.first_span("atlas.llm.narrative")
        assert span is not None
        attrs = self.attrs(span)
        self.assertEqual(attrs["atlas.llm.action"], "deny")
        self.assertEqual(attrs["atlas.llm.reason_code"], "role_denied")
        self.assertNotIn("gen_ai.usage.prompt_tokens", attrs)


if __name__ == "__main__":
    unittest.main()
