"""B0/B1 · `resolve_llm_backend` 决策矩阵测试（ADR-0029 ②③⑥ + ADR-0030 ①②④）。

纯函数、零网络：安全价值最高的一层，先把「narrative 绝不选云」「无权即拒」
「缺自托管回落模板」钉成可机读断言（dev-plan §5 判据 3/5）。

ADR-0030 追加格：引擎类型（`EngineKind`）与后端形态（`Backend`）正交，换引擎
**不放松出境红线**——尤其「narrative 即便 engine_kind=system_one 也绝不进 Jev」。
"""

from __future__ import annotations

import os
from unittest import mock

from agent.llm_policy import (
    Backend,
    BackendDecision,
    EngineKind,
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


# ---------------------------------------------------------------------------
# ADR-0030 · Jev System One 引擎格（①正交维度 / ②出境红线 / ④可切换）
# ---------------------------------------------------------------------------

_JEV = LlmConfig.with_jev(base_url="https://jev.example.com", model_name="jev-1")
_BOTH_PLUS_JEV = LlmConfig(
    route=_CLOUD.route,
    self_hosted=_SELF.self_hosted,
    route_backend=Backend.CLOUD,
    jev=_JEV.jev,
    engine_kind=EngineKind.SYSTEM_ONE,
)


def test_default_engine_kind_is_chat_completions() -> None:
    """默认引擎类型必须是 chat_completions：既有调用方零感知（④）。"""
    assert LlmConfig().engine_kind is EngineKind.CHAT_COMPLETIONS


def test_system_one_routes_candidate_to_jev() -> None:
    """schema-only 候选 + system_one + 已配 Jev 端点 → 路由到 Jev（③④）。"""
    d = resolve_llm_backend("candidate-fallback", CAPS_ROUTE, _BOTH_PLUS_JEV, self_hosted_ok=True)
    assert d.action is LlmAction.USE_BACKEND
    assert d.backend is Backend.JEV
    assert d.engine_kind is EngineKind.SYSTEM_ONE
    assert d.endpoint is not None and d.endpoint.base_url == "https://jev.example.com"
    assert d.data_class == "schema-only"


def test_system_one_without_jev_endpoint_falls_back_not_disabled() -> None:
    """开了 system_one 但没配 Jev 端点 → 回落 chat_completions 原路径。

    关键：**不得 DISABLED**。换引擎失败不应变成关能力——那会静默削弱系统，
    违背 ADR-0030 ④ 的可切换语义。
    """
    cfg = LlmConfig(
        route=_CLOUD.route, route_backend=Backend.CLOUD, engine_kind=EngineKind.SYSTEM_ONE
    )
    d = resolve_llm_backend("candidate-fallback", CAPS_ROUTE, cfg, self_hosted_ok=True)
    assert d.action is LlmAction.USE_BACKEND
    assert d.backend is Backend.CLOUD  # 原路径
    assert d.engine_kind is EngineKind.CHAT_COMPLETIONS


def test_narrative_never_routes_to_jev_even_when_system_one() -> None:
    """**本 ADR 最重要的一格**：narrative(result-bearing) 绝不进 Jev（②）。

    即便引擎开关为 system_one、Jev 端点已配置且自托管可用——叙述仍必须走
    自托管 chat_completions。Jev 仅托管、数据出境，不得因成本/延迟优势开口子。
    """
    d = resolve_llm_backend("narrative", CAPS_ALL, _BOTH_PLUS_JEV, self_hosted_ok=True)
    assert d.action is LlmAction.USE_BACKEND
    assert d.backend is Backend.SELF_HOSTED
    assert d.engine_kind is EngineKind.CHAT_COMPLETIONS
    assert d.endpoint is not None and d.endpoint.base_url == "http://vllm:8000/v1"
    assert d.data_class == "result-bearing"


def test_narrative_never_routes_to_jev_when_only_jev_configured() -> None:
    """只配了 Jev（无自托管）时，narrative 回落模板，**绝不拿 Jev 凑数**。"""
    d = resolve_llm_backend("narrative", CAPS_ALL, _JEV, self_hosted_ok=True)
    assert d.action is LlmAction.FALLBACK_TEMPLATE
    assert d.backend is Backend.NONE
    assert d.reason_code == "self_hosted_unavailable"


def test_jev_backend_denied_without_capability() -> None:
    """RBAC 先闸对 Jev 同样生效（⑥ 不因换引擎而绕过）。"""
    d = resolve_llm_backend("candidate-fallback", CAPS_NONE, _BOTH_PLUS_JEV, self_hosted_ok=True)
    assert d.action is LlmAction.DENY
    assert d.reason_code == "role_denied"


def test_from_env_defaults_to_chat_completions() -> None:
    """无任何 Jev 环境变量时，装配结果与 ADR-0030 前等价（引擎默认 + 端点 None）。"""
    with mock.patch.dict(os.environ, {}, clear=True):
        cfg = LlmConfig.from_env()
    assert cfg.engine_kind is EngineKind.CHAT_COMPLETIONS
    assert cfg.jev is None


def test_from_env_reads_jev_and_engine_kind() -> None:
    """ATLAS_JEV_* 配端点、ATLAS_ENGINE_KIND 切引擎（④，密钥走 env，N9）。"""
    env = {
        "ATLAS_JEV_BASE_URL": "https://jev.example.com/",
        "ATLAS_JEV_MODEL_NAME": "jev-1.13.0",
        "ATLAS_JEV_API_KEY": "secret-not-logged",
        "ATLAS_ENGINE_KIND": "system_one",
    }
    with mock.patch.dict(os.environ, env, clear=True):
        cfg = LlmConfig.from_env()
    assert cfg.engine_kind is EngineKind.SYSTEM_ONE
    assert cfg.jev is not None
    assert cfg.jev.base_url == "https://jev.example.com"  # 尾斜杠规范化
    assert cfg.jev.model_name == "jev-1.13.0"


def test_from_env_rejects_unknown_engine_kind() -> None:
    """未知引擎值 → fail-fast（配置错误须尽早暴露，不静默退默认）。"""
    with mock.patch.dict(os.environ, {"ATLAS_ENGINE_KIND": "bogus"}, clear=True):
        try:
            LlmConfig.from_env()
        except ValueError:
            pass
        else:
            raise AssertionError("未知 ATLAS_ENGINE_KIND 应抛 ValueError")
