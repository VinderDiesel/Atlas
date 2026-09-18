"""B3 · Generator 注入点测试（ADR-0029 ①）——候选路由跟随服务端分级后端。

口径：
- 注入 `chat`（假补全，零网络）与 `endpoint`（`resolve_llm_backend` 产物）向后兼容；
- LLM 只做 Plan 候选：输出必过 `validate_plan_json`，SQL 仍由 Compiler 产（N3）；
- 只用可移植子集：`_chat` payload 不含 response_format 等越界字段（违 ① 即停批）。
"""

from __future__ import annotations

import json

import pytest

from agent.compiler import Compiler, SemanticModel
from agent.generator import Generator
from agent.llm_policy import BackendEndpoint

MODEL = SemanticModel()
COMPILER = Compiler(MODEL)


def _fake_chat(payload: dict[str, object]) -> tuple[object, list]:
    """返回可编程 fake：(chat_fn, calls)。chat_fn 记录入参并返回预置 JSON。"""

    calls: list[dict[str, str]] = []

    def _chat(system: str, user: str, model: str) -> tuple[str, dict[str, int]]:
        calls.append({"system": system, "user": user, "model": model})
        return json.dumps(payload), {"prompt_tokens": 3, "completion_tokens": 2}

    return _chat, calls


_VALID_PLAN = {
    "metric": "commission_revenue",
    "dimensions": ["Branch"],
    "time": {"granularity": "year", "value": "2013"},
    "top_n": 5,
}


def test_injected_chat_produces_validated_plan() -> None:
    """注入 fake chat 产合法 Plan → 过确定性校验，chat 被调用一次。"""
    chat, calls = _fake_chat(_VALID_PLAN)
    gen = Generator(MODEL, engine="openai", chat=chat)
    r = gen.generate("按分支统计 2013 年佣金收入，列出前 5 名")
    assert r.plan is not None and not r.refused
    assert r.plan.metric == "commission_revenue"
    assert r.plan.limit == 5
    assert len(calls) == 1


def test_endpoint_model_name_flows_to_chat() -> None:
    """候选路由用注入后端的 model_name（跟随 B1 解析结果）。"""
    ep = BackendEndpoint(base_url="http://vllm:8000/v1", model_name="atlas-instruct")
    chat, calls = _fake_chat(_VALID_PLAN)
    gen = Generator(MODEL, engine="openai", endpoint=ep, chat=chat)
    gen.generate("按分支统计 2013 年佣金收入，列出前 5 名")
    assert gen.model_name == "atlas-instruct"
    assert calls[0]["model"] == "atlas-instruct"


def test_explicit_model_name_overrides_endpoint() -> None:
    ep = BackendEndpoint(base_url="http://x/v1", model_name="from-endpoint")
    chat, calls = _fake_chat(_VALID_PLAN)
    gen = Generator(MODEL, engine="openai", model_name="explicit", endpoint=ep, chat=chat)
    gen.generate("2013 年佣金收入")
    assert calls[0]["model"] == "explicit"


def test_llm_candidate_still_gated_by_validation() -> None:
    """LLM 编造未注册指标 → 仍被确定性校验拒绝（不猜，N8）。"""
    chat, _ = _fake_chat({"metric": "gmv", "top_n": None})
    gen = Generator(MODEL, engine="openai", chat=chat)
    r = gen.generate("总交易额是多少？")
    assert r.plan is None and r.refused
    assert "未注册" in (r.refusal.reason if r.refusal else "")


def test_llm_never_produces_sql_compiler_does() -> None:
    """即便 LLM 输出夹带 sql 字段也被忽略；SQL 只来自 Compiler（N3）。"""
    payload = {**_VALID_PLAN, "sql": "DROP TABLE trade"}
    chat, _ = _fake_chat(payload)
    gen = Generator(MODEL, engine="openai", chat=chat)
    r = gen.generate("按分支统计 2013 年佣金收入，列出前 5 名")
    assert r.plan is not None
    assert not hasattr(r.plan, "sql")  # Plan 结构里根本没有 SQL 槽位
    sql, _ = COMPILER.compile(r.plan)  # SQL 由 Compiler 独立生成
    assert "SELECT" in sql.upper() and "LIMIT 5" in sql
    assert "DROP" not in sql


def test_env_path_fails_closed_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """无注入且 env 无密钥 → 抛错拒绝，绝不静默打到别处（③ fail-closed）。"""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    gen = Generator(MODEL, engine="openai")
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        gen._chat("sys", "usr")


def test_portable_subset_only_fields() -> None:
    """请求体只用可移植子集：禁止 response_format 等越界字段（违 ① 即停批）。"""
    import inspect
    import re

    src = inspect.getsource(Generator._chat)
    payload_block = src[src.index("payload = {") : src.index("}", src.index("payload = {"))]
    for forbidden in ("response_format", "tools", "function_call", "stream"):
        assert not re.search(rf'"{forbidden}"', payload_block)
