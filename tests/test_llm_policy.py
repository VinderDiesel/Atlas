"""B0/B1 · `resolve_llm_backend` 决策矩阵测试（ADR-0029 ②③⑥）。

纯函数、零网络：安全价值最高的一层，先把「narrative 绝不选云」「无权即拒」
「缺自托管回落模板」钉成可机读断言（dev-plan §5 判据 3/5）。
"""

from __future__ import annotations

from agent.llm_policy import (
    Backend,
    BackendDecision,
    LlmAction,
    LlmConfig,
    resolve_llm_backend,
)

# 角色能力集（承 ADR-0029 ⑥：role→caps 是配置；测试直接传 caps 集合）
CAPS_NONE: frozenset[str] = frozenset()
CAPS_ROUTE: frozenset[str] = frozenset({"candidate-fallback"})
CAPS_ALL: frozenset[str] = frozenset({"candidate-fallback", "narrative"})

_CLOUD = LlmConfig.with_route(base_url="https://api.example/v1", model_name="gpt-x")
_SELF = LlmConfig.with_self_hosted(base_url="http://vllm:8000/v1", model_name="atlas-instruct")
_BOTH = LlmConfig(route=_CLOUD.route, self_hosted=_SELF.self_hosted, route_backend=Backend.CLOUD)


def test_intent_off_is_disabled() -> None:
    """off 意图 → DISABLED，不触任何后端（确定性路径零改）。"""
    d = resolve_llm_backend("off", CAPS_ALL, _BOTH, self_hosted_ok=True)
    assert d.action is LlmAction.DISABLED
    assert d.backend is Backend.NONE


def test_route_allowed_uses_configured_backend() -> None:
    """candidate-fallback 有权且已配置 → 用 cfg.route_backend 指定后端（schema-only 可云）。"""
    d = resolve_llm_backend("candidate-fallback", CAPS_ROUTE, _BOTH, self_hosted_ok=True)
    assert d.action is LlmAction.USE_BACKEND
    assert d.backend is Backend.CLOUD
    assert d.data_class == "schema-only"
    assert d.endpoint is not None and d.endpoint.base_url == "https://api.example/v1"


def test_route_denied_without_capability() -> None:
    """角色无 route 能力 → DENY（显式拒，不静默降级）。"""
    d = resolve_llm_backend("candidate-fallback", CAPS_NONE, _BOTH, self_hosted_ok=True)
    assert d.action is LlmAction.DENY
    assert d.reason_code == "role_denied"


def test_route_not_configured_is_disabled() -> None:
    """有权但完全未配 LLM → DISABLED（回落确定性，不报错）。"""
    empty = LlmConfig()
    d = resolve_llm_backend("candidate-fallback", CAPS_ROUTE, empty, self_hosted_ok=True)
    assert d.action is LlmAction.DISABLED
    assert d.reason_code == "llm_not_configured"


def test_narrative_denied_without_capability() -> None:
    """角色无 narrate 能力 → DENY，即便自托管可用也不放行。"""
    d = resolve_llm_backend("narrative", CAPS_ROUTE, _BOTH, self_hosted_ok=True)
    assert d.action is LlmAction.DENY
    assert d.reason_code == "role_denied"


def test_narrative_forced_self_hosted_never_cloud() -> None:
    """核心红线格：narrative 即便 cfg.route_backend=CLOUD，也必须走自托管，绝不云。"""
    assert _BOTH.route_backend is Backend.CLOUD  # 前提：默认路由后端是云
    d = resolve_llm_backend("narrative", CAPS_ALL, _BOTH, self_hosted_ok=True)
    assert d.action is LlmAction.USE_BACKEND
    assert d.backend is Backend.SELF_HOSTED
    assert d.data_class == "result-bearing"
    assert d.tier == "self_hosted"
    assert d.endpoint is not None and d.endpoint.base_url == "http://vllm:8000/v1"


def test_narrative_failclosed_when_self_hosted_down() -> None:
    """自托管探活失败 → FALLBACK_TEMPLATE，绝不改道云（reason 记录原因）。"""
    d = resolve_llm_backend("narrative", CAPS_ALL, _BOTH, self_hosted_ok=False)
    assert d.action is LlmAction.FALLBACK_TEMPLATE
    assert d.backend is Backend.NONE
    assert d.reason_code == "self_hosted_unavailable"


def test_narrative_failclosed_when_self_hosted_not_configured() -> None:
    """只配了云、没有自托管 → narrative 回落模板，不拿云凑数。"""
    d = resolve_llm_backend("narrative", CAPS_ALL, _CLOUD, self_hosted_ok=True)
    assert d.action is LlmAction.FALLBACK_TEMPLATE
    assert d.reason_code == "self_hosted_unavailable"


def test_decision_is_frozen_pure_data() -> None:
    """BackendDecision 是不可变数据（策略层不触网、不改状态）。"""
    d = resolve_llm_backend("candidate-fallback", CAPS_ROUTE, _BOTH, self_hosted_ok=True)
    assert isinstance(d, BackendDecision)
    try:
        d.action = LlmAction.DENY  # type: ignore[misc]
    except Exception:  # noqa: BLE001 - frozen dataclass 赋值必抛
        pass
    else:
        raise AssertionError("BackendDecision 应为 frozen（不可变）")
