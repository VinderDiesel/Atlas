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

引擎类型维度（ADR-0030 ①，加性扩展）
-------------------------------------
ADR-0029 ① 的「统一线格式」是其**推翻条件**已列明的前提：Jev System One 的
判别线格式（Choice/Score/Noul）客观不落在 `chat/completions` 子集内。为此本模块
加一个**与 `Backend` 正交**的 `EngineKind` 维度——`Backend` 答「数据发去哪」
（受敏感度划界约束，红线不变），`EngineKind` 答「用什么格式说话」。因此：

- Jev（`Backend.JEV`）**继承 CLOUD 的全部出境约束**，不被当作自托管豁免；
- `narrative`（result-bearing）**无条件禁入 Jev**，由本模块硬保证（见下方分支）；
- 默认 `engine_kind=chat_completions`，既有调用方零感知、行为逐字不变。

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
    """物理后端形态。`NONE` = 不走 LLM（确定性路径）。

    `JEV`（ADR-0030 ②）= Jev System One 托管端点。它**继承 CLOUD 的全部出境
    约束**（仅有托管 API、无本地部署、未公开权重 → 数据离开自有系统），
    绝不被当作自托管豁免；`narrative`（result-bearing）无条件禁止路由到它。
    """

    CLOUD = "cloud"
    SELF_HOSTED = "self_hosted"
    JEV = "jev"
    NONE = "none"


class EngineKind(StrEnum):
    """线格式（引擎类型）维度——与 `Backend`（数据去向）**正交**（ADR-0030 ①）。

    数据去向由 `Backend` 回答、受敏感度划界约束；用什么线格式说话由本枚举回答。
    二者正交意味着：换引擎类型**不放松**任何出境红线（ADR-0029 ② 原样继承）。
    """

    CHAT_COMPLETIONS = "chat_completions"  # OpenAI 兼容可移植子集（ADR-0029 ①，默认）
    SYSTEM_ONE = "system_one"  # Jev 判别原语 Choice/Score/Noul（ADR-0030 ③）


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
    # Jev System One 端点（ADR-0030 ③）。仅承载 schema-only 判断，无默认值
    # （未配置即 `None` → 决策回落 DISABLED，不触网）。
    jev: BackendEndpoint | None = None
    # 引擎类型开关（ADR-0030 ④）。**默认 CHAT_COMPLETIONS**：既有调用方
    # 零感知，`llm=off` 与默认值下行为逐字不变（ADR-0029 验证方式首条）。
    engine_kind: EngineKind = EngineKind.CHAT_COMPLETIONS

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
    def with_jev(cls, base_url: str, model_name: str, api_key: str = "") -> LlmConfig:
        """构造启用 System One 引擎（仅配 Jev 端点）的 config（测试便捷）。"""
        return cls(
            jev=BackendEndpoint(base_url, model_name, api_key),
            engine_kind=EngineKind.SYSTEM_ONE,
        )

    @classmethod
    def from_env(cls) -> LlmConfig:
        """从环境变量装配（AGENTS.md §13：密钥只走 env，绝不硬编码）。

        云路由复用既有 `OPENAI_*`（`Generator` 同源）；自托管走 `ATLAS_SELFHOSTED_*`；
        `ATLAS_LLM_ROUTE_BACKEND` 选 schema-only 路由用云还是自托管（默认云）；
        `ATLAS_JEV_*` 配 Jev 端点，`ATLAS_ENGINE_KIND` 切引擎类型（ADR-0030 ③④，
        默认 chat_completions——既有调用方零感知）。
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
        jev = _endpoint("ATLAS_JEV_BASE_URL", "ATLAS_JEV_MODEL_NAME", "ATLAS_JEV_API_KEY", "jev-1")
        rb = os.environ.get("ATLAS_LLM_ROUTE_BACKEND", _CLOUD_ROUTE_DEFAULT).strip().lower()
        # 未知引擎值 → ValueError（fail-fast，不静默退默认；配置错须尽早暴露）。
        ek = os.environ.get("ATLAS_ENGINE_KIND", EngineKind.CHAT_COMPLETIONS.value).strip().lower()
        return cls(
            route=route,
            self_hosted=self_hosted,
            route_backend=Backend(rb),
            jev=jev,
            engine_kind=EngineKind(ek),
        )


@dataclass(frozen=True)
class BackendDecision:
    """一次策略判决的不可变结果（含审计所需的 intent/data_class/tier/reason）。"""

    action: LlmAction
    intent: str
    data_class: str
    backend: Backend = Backend.NONE
    endpoint: BackendEndpoint | None = None
    tier: str = "none"  # 响应 narrative.tier 取此值：cloud|self_hosted|jev|none
    reason_code: str | None = None
    # 消费者据此选用哪个客户端协议（ADR-0030 ③）：CHAT_COMPLETIONS →
    # `narrative.ChatClient` / `generator.ChatFn`；SYSTEM_ONE → `jev_engine.SystemOneClient`。
    engine_kind: EngineKind = EngineKind.CHAT_COMPLETIONS


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
        # schema-only 判断走 Jev System One 引擎（ADR-0030 ③④）：仅在
        # `ATLAS_ENGINE_KIND=system_one` 且端点已配置时生效；未配置则回落
        # CHAT_COMPLETIONS 原路径（**绝不因缺配置而 DISABLED** —— 那会把
        # 「换引擎」变成「关能力」，违背可切换语义）。
        if cfg.engine_kind is EngineKind.SYSTEM_ONE and cfg.jev is not None:
            return BackendDecision(
                LlmAction.USE_BACKEND,
                it.value,
                data_class,
                backend=Backend.JEV,
                endpoint=cfg.jev,
                tier=Backend.JEV.value,
                engine_kind=EngineKind.SYSTEM_ONE,
            )
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

    # NARRATIVE：result-bearing → 只允许自托管，云无条件排除（②/③）。
    # ADR-0030 ②：即便 engine_kind=system_one，本分支也**不读 cfg.jev**——
    # Jev 仅托管、数据出境，result-bearing 一律禁入。这是本模块最重要的一条
    # 硬保证，由 tests/test_llm_policy.py 的专项格钉死。
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
