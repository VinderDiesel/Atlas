"""LLM 引擎分级路由策略（ADR-0029 ②③⑥）：意图 + 敏感度 + 角色 → 后端决策。

设计原则（与 AGENTS.md 对齐）
-----------------------------
- **纯函数、零 I/O**：`resolve_llm_backend` 不触网、不读环境（配置经 `LlmConfig`
  注入、自托管存活经布尔入参传入）——决策矩阵因此可在无网络/无 GPU 下逐格单测
  （dev-plan §5 判据 3）。健康探活是唯一的 I/O，隔离在 `serving` 侧（B4）。
- **按数据敏感度划界**（②）：候选路由 = schema-only（prompt 无结果数值）→ 云或自托管
  皆可；叙述 = result-bearing（prompt 必含 TurnPayload 数字）→ **强制自托管，云无条件排除**。
- **fail-closed**（③）：叙述缺自托管 → 回落确定性模板（`FALLBACK_TEMPLATE`），绝不静默升级云。
- **RBAC**（⑥）：角色能力集不含该 intent → `DENY`（上层转 403），不静默降级、不假装成功。

后端线格式统一为 OpenAI 兼容 `chat/completions`（①），本模块只决定「用不用、用哪个、
能不能」，实际调用在 `Generator` / `narrative`（可移植子集由调用方约束）。

    cfg = LlmConfig.from_env()
    decision = resolve_llm_backend("narrative", role_caps, cfg, self_hosted_ok=probe_alive(cfg))
    if decision.action is LlmAction.USE_BACKEND: ...
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum

_CLOUD_ROUTE_DEFAULT = "cloud"


class Backend(StrEnum):
    """物理后端形态。`NONE` = 不走 LLM（确定性路径）。"""

    CLOUD = "cloud"
    SELF_HOSTED = "self_hosted"
    NONE = "none"


class LlmAction(StrEnum):
    """策略判决动作（消费方据此分流；无副作用）。"""

    DISABLED = "disabled"  # 未启用/未配置/意图 off → 确定性路径，不报错
    USE_BACKEND = "use_backend"  # 放行，用 decision.endpoint 指定的后端
    FALLBACK_TEMPLATE = "fallback_template"  # 叙述缺自托管 → 回落确定性模板（不升级云）
    DENY = "deny"  # 角色无权 → 上层 403


class LlmIntent(StrEnum):
    """客户端意图旗标（AskBody/AnalyzeBody.llm 的取值；ADR-0029 ⑤）。"""

    OFF = "off"
    CANDIDATE_FALLBACK = "candidate-fallback"
    NARRATIVE = "narrative"


@dataclass(frozen=True)
class BackendEndpoint:
    """一个 OpenAI 兼容后端的接入坐标（密钥可为空，如本地 vLLM）。"""

    base_url: str
    model_name: str
    api_key: str = ""


@dataclass(frozen=True)
class LlmConfig:
    """从环境装配的 LLM 能力配置（纯数据；不含存活状态）。

    `route_backend` 只对 schema-only 候选路由生效（云/自托管皆合法）；叙述恒定走
    `self_hosted`，不受 `route_backend` 影响（② 数据敏感度划界）。
    """

    route: BackendEndpoint | None = None
    self_hosted: BackendEndpoint | None = None
    route_backend: Backend = Backend.CLOUD

    @classmethod
    def with_route(cls, base_url: str, model_name: str, api_key: str = "") -> LlmConfig:
        """构造仅配云路由的 config（测试便捷）。"""
        return cls(route=BackendEndpoint(base_url, model_name, api_key))

    @classmethod
    def with_self_hosted(cls, base_url: str, model_name: str, api_key: str = "") -> LlmConfig:
        """构造仅配自托管的 config（测试便捷）。"""
        return cls(
            self_hosted=BackendEndpoint(base_url, model_name, api_key),
            route_backend=Backend.SELF_HOSTED,
        )

    @classmethod
    def from_env(cls) -> LlmConfig:
        """从环境变量装配（AGENTS.md §13：密钥只走 env，绝不硬编码）。

        云路由复用既有 `OPENAI_*`（`Generator` 同源）；自托管走 `ATLAS_SELFHOSTED_*`；
        `ATLAS_LLM_ROUTE_BACKEND` 选 schema-only 路由用云还是自托管（默认云）。
        """

        def _endpoint(
            base: str, model: str, key: str, default_model: str
        ) -> BackendEndpoint | None:
            base_url = os.environ.get(base, "").strip()
            if not base_url:
                return None
            return BackendEndpoint(
                base_url=base_url.rstrip("/"),
                model_name=os.environ.get(model, "").strip() or default_model,
                api_key=os.environ.get(key, "").strip(),
            )

        route = _endpoint("OPENAI_BASE_URL", "OPENAI_MODEL_NAME", "OPENAI_API_KEY", "gpt-4o-mini")
        self_hosted = _endpoint(
            "ATLAS_SELFHOSTED_BASE_URL",
            "ATLAS_SELFHOSTED_MODEL_NAME",
            "ATLAS_SELFHOSTED_API_KEY",
            "atlas-instruct",
        )
        rb = os.environ.get("ATLAS_LLM_ROUTE_BACKEND", _CLOUD_ROUTE_DEFAULT).strip().lower()
        return cls(route=route, self_hosted=self_hosted, route_backend=Backend(rb))


@dataclass(frozen=True)
class BackendDecision:
    """一次策略判决的不可变结果（含审计所需的 intent/data_class/tier/reason）。"""

    action: LlmAction
    intent: str
    data_class: str
    backend: Backend = Backend.NONE
    endpoint: BackendEndpoint | None = None
    tier: str = "none"  # 响应 narrative.tier 取此值：cloud|self_hosted|none
    reason_code: str | None = None


def data_class_for_intent(intent: LlmIntent) -> str:
    """意图 → 数据敏感度类（② 划界的唯一映射，集中一处便于审计）。"""
    if intent is LlmIntent.CANDIDATE_FALLBACK:
        return "schema-only"
    if intent is LlmIntent.NARRATIVE:
        return "result-bearing"
    return "none"


def resolve_llm_backend(
    intent: str,
    role_caps: frozenset[str],
    cfg: LlmConfig,
    self_hosted_ok: bool,
) -> BackendDecision:
    """分级路由决策（ADR-0029 ②③⑥）。纯函数、无副作用。

    Parameters
    ----------
    intent : "off" | "candidate-fallback" | "narrative"（未知值抛 ValueError，上层须先校验）
    role_caps : 当前角色被授予的意图集合（⑥，如 frozenset({"candidate-fallback"})）
    cfg : 环境装配的后端配置
    self_hosted_ok : 自托管后端「已配置且探活通过」的合并布尔（存活判定唯一 I/O，隔离在外）

    Returns
    -------
    BackendDecision：调用方按 `action` 分流（USE_BACKEND 用 `endpoint`；FALLBACK_TEMPLATE
    回落确定性模板；DENY 转 403；DISABLED 走原确定性路径）。

    Raises
    ------
    ValueError
        intent 不属于三种合法取值。
    """
    it = LlmIntent(intent)  # 未知 → ValueError（HTTP 层 pydantic 已挡，二次防御）
    data_class = data_class_for_intent(it)

    if it is LlmIntent.OFF:
        return BackendDecision(LlmAction.DISABLED, it.value, data_class)

    # ① RBAC 先闸（不静默降级）
    if it.value not in role_caps:
        return BackendDecision(LlmAction.DENY, it.value, data_class, reason_code="role_denied")

    if it is LlmIntent.CANDIDATE_FALLBACK:
        if cfg.route is None:
            return BackendDecision(
                LlmAction.DISABLED, it.value, data_class, reason_code="llm_not_configured"
            )
        # schema-only：云/自托管皆合法，用配置指定后端
        return BackendDecision(
            LlmAction.USE_BACKEND,
            it.value,
            data_class,
            backend=cfg.route_backend,
            endpoint=cfg.route,
            tier=cfg.route_backend.value,
        )

    # NARRATIVE：result-bearing → 只允许自托管，云无条件排除（②/③）
    if cfg.self_hosted is None or not self_hosted_ok:
        return BackendDecision(
            LlmAction.FALLBACK_TEMPLATE,
            it.value,
            data_class,
            reason_code="self_hosted_unavailable",
        )
    return BackendDecision(
        LlmAction.USE_BACKEND,
        it.value,
        data_class,
        backend=Backend.SELF_HOSTED,
        endpoint=cfg.self_hosted,
        tier=Backend.SELF_HOSTED.value,
    )
