"""T10d 接缝：Generator 显式候选与隐式二次检索移除（ADR-0031 D09 L265）。

口径：
- **候选输入路径**（retrieve 产物显式传入）：Prompt 候选块逐字使用该清单
  （顺序 = 调用方/retrieve 融合序，不重排不截断），不构造 SchemaLinker、
  不触发隐式二次检索；显式空清单（0 命中）明确标注，不落回检索。
- **旧入口适配器**（未传候选）：保留既有隐式检索行为，如实记录其上下文口径
  （含全量指标清单），不构成公平 top-K 对照声明。
- **默认图**（build_graph）：候选路径把 retrieve 产物显式传入 generate 节点，
  同一轮 validate 重试沿用同一候选清单。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from types import SimpleNamespace
from typing import Any

import pytest

from agent.compiler import OrderSpec, Plan, SemanticModel, TimeSpec
from agent.generator import GenerationResult, Generator
from agent.graph import DataAgent

MODEL = SemanticModel()

_QUESTION = "按分支统计 2013 年佣金收入，列出前 5 名"

_VALID_PLAN = {
    "metric": "commission_revenue",
    "dimensions": ["Branch"],
    "time": {"granularity": "year", "value": "2013"},
    "top_n": 5,
}


def _fake_chat() -> tuple[Any, list[dict[str, str]]]:
    """返回 (chat_fn, calls)：记录入参并返回预置合法 Plan JSON（零网络）。"""
    calls: list[dict[str, str]] = []

    def _chat(system: str, user: str, model: str) -> tuple[str, dict[str, int]]:
        calls.append({"system": system, "user": user, "model": model})
        return json.dumps(_VALID_PLAN), {"prompt_tokens": 3, "completion_tokens": 2}

    return _chat, calls


def _answer_plan() -> Plan:
    """与 gold-102 人工标注一致的 Plan（图集成夹具）。"""
    return Plan(
        metric="commission_revenue",
        dimensions=("Branch",),
        time=TimeSpec("year", 2013),
        order_by=(OrderSpec("commission_revenue", desc=True),),
        limit=5,
    )


class _ExplodingLinker:
    """构造即失败的 SchemaLinker 桩：候选路径若依赖检索器立即暴露。"""

    def __init__(self, model: object) -> None:
        raise AssertionError("候选路径不得构造 SchemaLinker（隐式二次检索已移除）")

    def link(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("候选路径不得触发隐式检索")


def test_explicit_candidates_skip_implicit_retrieval(monkeypatch: pytest.MonkeyPatch) -> None:
    """显式候选：不构造检索器、不做二次检索；候选块逐字使用显式清单。"""
    monkeypatch.setattr("agent.generator.SchemaLinker", _ExplodingLinker)
    chat, calls = _fake_chat()
    gen = Generator(MODEL, engine="openai", chat=chat)
    result = gen.generate(_QUESTION, candidates=("commission_revenue", "total_trade_value"))
    assert result.plan is not None and not result.refused
    user = calls[0]["user"]
    assert "候选1：commission_revenue" in user
    assert "候选2：total_trade_value" in user


def test_explicit_candidates_render_verbatim_in_order() -> None:
    """显式清单逐字渲染：顺序 = 调用方（retrieve 融合序），不重排不截断。"""
    chat, calls = _fake_chat()
    gen = Generator(MODEL, engine="openai", chat=chat)
    given = ("total_trade_value", "commission_revenue", "trade_count")
    gen.generate(_QUESTION, candidates=given)
    user = calls[0]["user"]
    positions = [user.index(f"候选{i}：{name}") for i, name in enumerate(given, start=1)]
    assert positions == sorted(positions)
    assert "候选4" not in user


def test_explicit_empty_candidates_marked_in_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    """显式空清单（retrieve 0 命中）：明确标注无候选，不落回隐式检索。"""
    monkeypatch.setattr("agent.generator.SchemaLinker", _ExplodingLinker)
    chat, calls = _fake_chat()
    gen = Generator(MODEL, engine="openai", chat=chat)
    gen.generate(_QUESTION, candidates=())
    user = calls[0]["user"]
    assert "（本轮无候选）" in user
    assert "候选1" not in user


class _RecordingLinker:
    """记录构造与 link 调用的检索器桩（旧入口适配器路径的确定性观察点）。"""

    instances: list[_RecordingLinker] = []

    def __init__(self, model: object) -> None:
        self.model = model
        self.links: list[tuple[str, int]] = []
        _RecordingLinker.instances.append(self)

    def link(self, question: str, k: int = 5) -> SimpleNamespace:
        self.links.append((question, k))
        return SimpleNamespace(candidates=("commission_revenue", "total_trade_value"), dims=())


def test_legacy_adapter_keeps_implicit_retrieval(monkeypatch: pytest.MonkeyPatch) -> None:
    """旧入口适配器：构造期惰性；未传候选时保留隐式检索候选块（旧行为不回归）。"""
    _RecordingLinker.instances = []
    monkeypatch.setattr("agent.generator.SchemaLinker", _RecordingLinker)
    chat, calls = _fake_chat()
    gen = Generator(MODEL, engine="openai", chat=chat)
    assert _RecordingLinker.instances == []  # 构造 Generator 不触发检索器
    gen.generate(_QUESTION)
    assert len(_RecordingLinker.instances) == 1
    assert _RecordingLinker.instances[0].links == [(_QUESTION, 5)]
    user = calls[0]["user"]
    assert "候选1：commission_revenue" in user
    assert "候选2：total_trade_value" in user


class _RecordingGenerator:
    """记录 generate 入参的哨兵（签名与 PlanGenerator 协议一致）。"""

    engine = "stub"

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def generate(
        self, question: str, k: int = 5, *, candidates: Sequence[str] | None = None
    ) -> GenerationResult:
        self.calls.append({"question": question, "k": k, "candidates": candidates})
        return GenerationResult(question=question, plan=_answer_plan())


def test_default_graph_passes_retrieved_candidates() -> None:
    """默认图：generate 节点显式携带 retrieve 产物（同一轮只检索一次）。"""
    from tests.test_graph import BUDGET, OUT_OF_DOMAIN_Q, FakeExecutor, FakeLinker

    spy = _RecordingGenerator()
    agent = DataAgent(
        executor=FakeExecutor(),
        budget=BUDGET,
        allow_candidate=True,
        generator=spy,  # type: ignore[arg-type]
        linker=FakeLinker(candidates=("commission_revenue",)),  # type: ignore[arg-type]
    )
    result = agent.ask(OUT_OF_DOMAIN_Q)
    assert result.kind == "answer"
    assert spy.calls == [
        {"question": OUT_OF_DOMAIN_Q, "k": 5, "candidates": ("commission_revenue",)}
    ]


def test_default_graph_candidate_path_never_builds_generator_linker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """默认图 + 真实 Generator：候选路径不构造内部检索器（爆炸桩证明）。"""
    from tests.test_graph import BUDGET, OUT_OF_DOMAIN_Q, FakeExecutor, FakeLinker

    monkeypatch.setattr("agent.generator.SchemaLinker", _ExplodingLinker)
    chat, _ = _fake_chat()
    gen = Generator(MODEL, engine="openai", chat=chat)
    agent = DataAgent(
        executor=FakeExecutor(),
        budget=BUDGET,
        allow_candidate=True,
        generator=gen,  # type: ignore[arg-type]
        linker=FakeLinker(),  # type: ignore[arg-type]
    )
    result = agent.ask(OUT_OF_DOMAIN_Q)
    assert result.kind == "answer"
    assert result.metric == "commission_revenue"
