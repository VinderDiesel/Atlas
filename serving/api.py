"""Atlas HTTP 服务面 v2（ADR-0022）：/api/v1 前缀、/plan/execute、治理面、限流两桶。

契约口径（诚实声明）
--------------------
- 路由契约 v2（ADR-0022 决策 ①②）：业务端点一律挂 `/api/v1` 前缀
  （/api/v1/plan /compile /ask /plan/execute /analyze /analyze/stream），旧无前缀路径**硬切**（404，
  无兼容期、不保留别名）；`/health` **双挂**（根 + 前缀，同一 handler）——根挂点
  是给 docker-compose healthcheck 与外部探针留的（ADR-0022 理由 2：探针字符串
  改了而无人验证过 compose，失败链没有自动兜底）。「契约 v2」指 URL 契约，
  不开放 `engine` 参数（ADR-0012 推翻条件第 3 条未触发）。
- 治理面（ADR-0022 决策 ④⑤）：/api/v1/governance/*（8 集合 + 2 钻取）由
  serving/governance.build_governance_router 构造后装配——一律只读 Git 文件与
  产物目录，**绝不触发 DB 与 Agent 构造**：/ask 因快照缺失 503 时治理面板仍 200
  （那正是排查该故障时最需要看的画面）。
- 限流两桶（ADR-0022 决策 ⑥）：业务桶（ATLAS_RATE_LIMIT_*，60/60 占位）覆盖全部
  业务端点；治理桶（ATLAS_GOVERNANCE_RATE_LIMIT_*，240/60 占位）覆盖治理面；
  两桶独立实例互不挤占，429 的 detail 标桶名 + Retry-After，审计行带 bucket 字段。
- /plan/execute（ADR-0022 决策 ③）：Plan 直接执行，仍走 node_execute 唯一通道
  （编译 → resolve_claims 渲染策略 → Guard 注入 + 二次只读校验 → 执行），HTTP
  层不复制执行逻辑（N3）；kind ∈ answer|blocked|error，永不为 clarify|handoff
  （无自然语言解析面即无歧义面，不进候选链）。
- 确定性默认：engine=stub，DataAgent 走确定性链路（Planner → Compiler → Guard →
  Doris 执行），LLM 引擎服务化属 Phase 2（ADR-0012）。
- 会话语义与 CLI 一致：Agent 在进程内懒建**单例**（会话状态在它自己的 checkpointer
  里——默认 MemorySaver 进程内、重启即失；设 ATLAS_CHECKPOINT_DB 后落盘续接，
  ADR-0020）；uvicorn 必须 workers=1——理由已收窄（ADR-0020 决策 ⑧）：会话不再
  分裂，仍为进程内态的是限流桶、审计写与 SQLite 单写者（README KL #28）。
- 多模型路由（P7，2026-09-05）：业务端点请求体带 `model` 字段（finance|retail，
  缺省 finance——向后兼容，旧请求体零变化）；/plan /compile 按域构造语义模型
  （无状态），/ask /plan/execute 按域懒建独立 Agent 单例（finance/retail 各一）
  ——会话键（session_id）按模型隔离，跨模型不续接。
- 服务面硬化（2026-09-05 批次 C2）：
  - /ask /plan/execute 把已验证 claims 下推为身份（agent.run_plan/ask(identity=…)
    → 行级策略随 Guard 注入，见 agent/graph.py）；/plan /compile 无执行面不注入。
  - 会话 × 身份：session_id 首轮绑定 claims 指纹，同会话换身份（含重签 token）→
    422「会话身份冲突，请换新 session_id」。指纹自 ADR-0020 决策 ⑥ 起存在 **Agent
    的 checkpoint 里**（`session_fingerprint`，只存 sha256 摘要不存 claims），故
    重启/换实例后校验依旧生效——不再是「会话续上了、身份校验却重置」的进程内表。
  - 审计 JSONL（serving/audit.py）：业务面每请求一行 + 429/422 业务拒绝
    （bucket="business"）；治理面每请求一行 kind="governance_read"
    （bucket="governance"）；均不含 SQL（OTel span 承担），claims 只取 role/sub；
    ATLAS_AUDIT_DISABLED=1 关闭（单开关，不新增第二个）。
  - 限流（serving/ratelimit.py）：两桶独立实例（决策 ⑥），429 + Retry-After；
    默认 60/240 次每分钟为配置占位非实测阈值（env 覆盖，0=关）；
    /health（根 + 前缀）公开、401 路径不限流。
- 认证复用 serving/auth.py：verify_token 走 env ATLAS_JWT_SECRET（N9，无默认
  密钥）；require_bearer / BearerClaims 自 ADR-0022 起同源在 auth.py——业务面与
  治理面共用同一实现（认证语义单源，两个 router 只装配不复制）；claims 服务端
  已验证（用户不可伪造 role/user_context——0011 决策 2 硬化项兑现；零售角色的带
  身份实测载体 = rls-verify 零售档 + demo 集成测试同机制，api-verify A5-A7 为
  HTTP 面载体）。
- 序列化：Decimal → str（保精度，不进浮点）、datetime/date → ISO8601、Enum →
  value、tuple → list——FastAPI 的 jsonable_encoder 会把 Decimal 转 float 丢精度，
  故 rows 值必须在此先行转换（确定性文本不经浮点）。
- CLI 语义的 HTTP 化：plan 歧义 exit 1 → 200 + kind=clarify；快照 meta 缺失
  （SystemExit 语义）→ 503。OpenAPI version 读 importlib.metadata（ADR-0022
  决策 ⑦ 附带修正）：pyproject.toml 是版本单一事实源，手写常量属 N2 型漂移。
- SPA 静态面（P0b，ADR-0018 决策 ②⑤）：`frontend/dist/index.html` 存在时才挂
  catch-all（**必须注册在两个 router 之后**，否则吞掉 /api/v1 的 404）；dist 缺失
  跳过并打 warning——fresh clone 未构建前端时 API 照常可用（条件挂载）。
- 认证面（ADR-0031 D02/D13，/api/v1/auth/*）：serving/control/routes.build_auth_router
  构造后装配——OIDC BFF 私有登录（Cookie 会话 + CSRF + 同源 Origin）；未配置/
  配置不全 503 配置阻塞（不伪装登录成功）；旧 Bearer 端点零改动（其余业务/治理
  面仍走 require_bearer，两条认证链不混用）。
- ④a SSE 流式面（ADR-0028 决策 ④·执行模型 A compute-then-stream）：`/analyze/stream`
  复用 `/analyze` 同一执行路径跑完 `agent.analyze()`（SQL 已全部经 Guard、锁已释放）→
  把已算好的分步产物按 `docs/design/agui-event-mapping.md` §2/§4 词表**回放**为
  `text/event-stream`。事件**只承载展示**、不新增任何 SQL 构造/执行通道（N3）；
  借鉴词表 ≠ AG-UI 兼容（N2，不宣称兼容）。被拒步无 `TOOL_CALL_RESULT`（被拒 SQL 不出网）。
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from importlib import metadata as importlib_metadata
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from agent.analysis import (
    ANALYSIS_ROLES,
    AnalysisResult,
    analysis_status,
    effective_reason_code,
)
from agent.compiler import (
    FINANCE_MODEL,
    CompileError,
    Compiler,
    Filter,
    OrderSpec,
    Plan,
    SemanticModel,
    TimeSpec,
)
from agent.factory import SnapshotUnavailable, create_live_agent
from agent.graph import DataAgent, SessionIdentityConflict
from agent.llm_policy import BackendEndpoint, LlmAction, LlmConfig, resolve_llm_backend
from agent.narrative import ChatClient, OpenAICompatClient, synthesize_narrative
from agent.planner import ClarificationRequest, Planner
from agent.state import ANALYSIS_RECORD_VERSION, TurnResult
from data.identity import (
    RuntimeSnapshot,
    git_short_sha_or_none,
    resolve_runtime_snapshot,
)
from observability.otel import record_llm_narrative
from serving.audit import AuditLog
from serving.auth import BearerClaims, caps_for_role
from serving.control.auth import load_control_grants
from serving.control.oidc import OidcBff, OidcSettings
from serving.control.router import ControlServices, build_control_router
from serving.control.routes import build_auth_router
from serving.governance import build_governance_router
from serving.ratelimit import RateLimiter

if TYPE_CHECKING:
    from serving.control.auth import Principal
    from serving.control.contracts import RunRecord, RunRequest

REPO_ROOT = Path(__file__).resolve().parent.parent

logger = logging.getLogger(__name__)

# 契约 v2 唯一前缀（ADR-0022 决策 ①②）：业务面与治理面全部路径共用一个前缀——
# ADR-0018 决策 ② 的 SPA fallback 才能写成一条 `startswith("/api/")` 规则而不是
# 随端点增长的手工白名单。装饰器只写相对段，前缀在这里声明一次。
API_PREFIX = "/api/v1"

# SPA 产物目录（P0b，ADR-0018 决策 ②⑤）：本地由 `make ui-build` 生成；容器内由
# Dockerfile 多阶段构建注入（COPY --from=<node 阶段> /app/frontend/dist）。
# 不存在时跳过挂载并打 warning（条件挂载），不把"UI 缺失"升级为"服务不可用"。
FRONTEND_DIST = REPO_ROOT / "frontend" / "dist"

# 进程生命周期标识（ADR-0020 决策 ⑦）：启动时生成一次，`/health` 回显。
# 用途是判断「服务端是否重启过」——即使会话已持久化，限流桶与 OTel 聚合仍随重启
# 归零，boot_id 变化是唯一可靠信号。刻意**不从 env 读**：env 是身份注入通道
# （ATLAS_GIT_SHA），而本值必须不可伪造地为「本次进程」所有。不参与任何鉴权。
BOOT_ID = uuid4().hex
# 域 → 语义模型 YAML（与 eval/runner.DOMAIN_MODELS 同路径；模型选择白名单）
DOMAIN_MODEL_PATHS: dict[str, Path] = {
    "finance": FINANCE_MODEL,
    "retail": REPO_ROOT / "semantic" / "ossie" / "atlas_retail.ossie.yaml",
}


def _package_version() -> str:
    """OpenAPI 版本 = pyproject.toml 单一事实源（ADR-0022 决策 ⑦ 附带修正）。

    旧实现硬编码 "0.1.0"，与包实际版本漂移（实测 pyproject 为 0.1.3）：OpenAPI
    页面自称的版本是假的（N2 型）。包未安装（源码树裸跑单测等）→ "unknown"——
    如实说不知道，而不是回落某个看似合理的常量。
    """
    try:
        return importlib_metadata.version("atlas")
    except importlib_metadata.PackageNotFoundError:
        return "unknown"


# ---------------------------------------------------------------------------
# 自托管后端探活（ADR-0029 ③：分级路由决策唯一的外部 I/O，隔离在 serving 侧）
# ---------------------------------------------------------------------------
# `resolve_llm_backend` 是纯函数，存活状态以合并布尔 `self_hosted_ok` 传入。本函数是
# 该布尔的唯一生产者：未配置自托管直接 False（零网络）；已配置则对 `/v1/models` 短超时
# 探活，结果短时缓存防惊群。探活失败（无 GPU / 端点未起 / 超时）一律 False → 叙述回落
# 确定性模板，**绝不因探活失败而升级云**（③ fail-closed）。
_SELFHOSTED_PROBE_TTL_S = 30.0
_selfhosted_probe_cache: dict[str, tuple[bool, float]] = {}


def _probe_self_hosted(endpoint: BackendEndpoint) -> bool:
    """对自托管 `/v1/models` 短超时探活；任何异常（连接拒绝/超时/非 2xx）→ False。"""
    headers = {"Content-Type": "application/json"}
    if endpoint.api_key:  # N9：密钥走请求头，绝不入 query
        headers["Authorization"] = f"Bearer {endpoint.api_key}"
    req = urllib.request.Request(
        f"{endpoint.base_url.rstrip('/')}/models", headers=headers, method="GET"
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310 - 受管端点
            return 200 <= resp.status < 300
    except Exception:  # noqa: BLE001 - 探活失败即视为不可用（fail-closed），不外泄异常
        return False


def self_hosted_available(cfg: LlmConfig) -> bool:
    """自托管「已配置 ∩ 探活通过」的合并布尔（带 TTL 缓存；密钥不入缓存键）。"""
    ep = cfg.self_hosted
    if ep is None:
        return False
    now = time.monotonic()
    hit = _selfhosted_probe_cache.get(ep.base_url)
    if hit is not None and now - hit[1] < _SELFHOSTED_PROBE_TTL_S:
        return hit[0]
    ok = _probe_self_hosted(ep)
    _selfhosted_probe_cache[ep.base_url] = (ok, now)
    return ok


class _NullClient:
    """占位客户端：仅用于 `FALLBACK_TEMPLATE`（无自托管）分支——`synthesize_narrative`
    在该分支直接回落模板、绝不调用本类；若被调用即说明路由错（抛错响亮失败）。"""

    def complete(self, system: str, user: str, model: str) -> tuple[str, dict[str, int]]:
        raise RuntimeError("_NullClient 不应被调用（叙述路由未解析出自托管后端）")


# ---------------------------------------------------------------------------
# 请求/响应模型（HTTP 面粗约束；细约束在 Guard/Planner，不在此复制）
# ---------------------------------------------------------------------------

# 模型选择：P7 多模型路由（缺省 finance 向后兼容；未知模型 → 422）


def _model_name(model: str) -> str:
    """校验 model 字段 ∈ 域白名单（未知模型 → 422，不落到语义层）。"""
    if model not in DOMAIN_MODEL_PATHS:
        raise HTTPException(status_code=422, detail=f"未知模型：{model!r}（可选 finance|retail）")
    return model


class QuestionBody(BaseModel):
    question: str = Field(min_length=1, max_length=500, description="自然语言问句")
    model: str = Field(default="finance", description="语义模型域：finance（缺省）| retail")


class AskBody(QuestionBody):
    session_id: str | None = Field(
        default=None, min_length=1, max_length=64, description="会话键（缺省自动生成，单轮）"
    )
    llm: Literal["off", "candidate-fallback", "narrative"] = Field(
        default="off",
        description="LLM 意图旗标（ADR-0029 ⑤，加性可选；缺省 off 逐字向后兼容）。"
        "后端/分级/是否允许全由服务端定（客户端不能选 backend）。",
    )


class CompileTime(BaseModel):
    granularity: str  # year / quarter / month / date（与 compiler.TimeSpec 一致）
    value: int | str


class CompileFilterSpec(BaseModel):
    column: str
    op: str
    value: Any


class CompileOrderSpec(BaseModel):
    column: str
    desc: bool = False


class CompileBody(BaseModel):
    """Plan JSON 镜像（agent.compiler.Plan 结构，CLI compile 输入同构）。"""

    metric: str
    dimensions: list[str] = []
    time: CompileTime | None = None
    filters: list[CompileFilterSpec] = []
    order_by: list[CompileOrderSpec] = []
    limit: int = Field(default=100, ge=1, le=10_000)
    model: str = Field(default="finance", description="语义模型域：finance（缺省）| retail")


class PlanExecuteBody(CompileBody):
    """Plan JSON 镜像 + 执行面两键（ADR-0022 决策 ③；响应与 /ask 同构）。"""

    session_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        description="会话键（缺省 = 一次性 thread，不续接、不产生可复用的会话态）",
    )
    question: str | None = Field(
        default=None,
        min_length=1,
        max_length=500,
        description="审计与归因展示用文本（缺省取 Plan 规范化文本；不参与解析）",
    )


# ---------------------------------------------------------------------------
# JSON 序列化
# ---------------------------------------------------------------------------


def _jsonable(value: Any) -> Any:
    """递归转 JSON 安全值：Decimal → str、datetime/date → ISO8601、Enum → value。

    FastAPI 的 jsonable_encoder 会把 Decimal 编码成 float（丢精度），rows 值必须
    在此先行转 str（契约测试锁定 Decimal 字符串形态）。
    """
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, tuple):
        return [_jsonable(v) for v in value]
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    return value


def _clarify_payload(c: ClarificationRequest) -> dict[str, Any]:
    return {
        "question": c.question,
        "reasons": list(c.reasons),
        "candidates": list(c.candidates),
        "kind": c.kind,
    }


def _plan_payload(plan: Plan) -> dict[str, Any]:
    """Plan → 平铺 JSON（compile 请求体的同构形态，CLI 打印的 JSON 版）。"""
    return {
        "metric": plan.metric,
        "dimensions": list(plan.dimensions),
        "time": (
            {"granularity": plan.time.granularity, "value": _jsonable(plan.time.value)}
            if plan.time is not None
            else None
        ),
        "filters": [
            {"column": f.column, "op": f.op, "value": _jsonable(f.value)} for f in plan.filters
        ],
        "order_by": [{"column": o.column, "desc": o.desc} for o in plan.order_by],
        "limit": plan.limit,
    }


def _plan_text(plan: Plan) -> str:
    """Plan 的规范化文本（`/plan/execute` 缺省 `question` 的替代值）。

    确定性拼接（同 Plan 恒同文本），只进审计与归因展示——Plan 的解析面是结构化
    字段本身，本函数不参与任何语义解析（ADR-0022 决策 ③ 的 `question?` 口径）。
    """
    parts = [f"直接执行 Plan：{plan.metric}"]
    if plan.dimensions:
        parts.append("按 " + "、".join(plan.dimensions))
    if plan.time is not None:
        parts.append(f"{plan.time.value} {plan.time.granularity}")
    return "，".join(parts)


def _turn_payload(
    result: TurnResult,
    snapshot: RuntimeSnapshot | None = None,
    *,
    analysis: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """TurnResult → 平铺 JSON：字段全集稳定输出（kind 无关字段为 null）。

    `snapshot` 是**该 agent 构造时解析出的绑定**（`DataAgent.snapshot`），由调用方
    传入而不是在这里再 `resolve_runtime_snapshot()` 一次：本轮回答用的白名单来自
    建 agent 那一刻的解析结果，重新解析可能拿到另一份快照（期间有人重锁或 HEAD
    前进），于是回显的 sha 与真正执行查询的 sha 不是同一个——那正是决策 ⑥ 要消灭的
    「看不出绑在哪」。None（测试桩 / 评测路径构造的 agent）→ 两键为 null，
    键本身不消失（字段全集恒定）。

    两键与 `explanation` **并列**而不塞进去：快照绑定状态是回合级事实（与 `kind`
    / `row_count` 同层），不是归因解释的一部分（0019 决策 ⑥ 的语义归属裁定）。

    `analysis` 是多步分析的 17 键投影（ADR-0026 决策 ⑥，`_analysis_payload` 产出），
    同样与 explanation 并列：分析是父轮之上的独立产出，不是解释的附属。键**恒在**，
    非分析请求（普通 ask / plan/execute / fallback / clarify）值恒为 null——
    「键存在且为 null」与「键不存在」是两种契约，前端类型按 `| null` 建模。
    """
    payload = _jsonable(
        {
            "kind": result.kind,
            "session_id": result.session_id,
            "question": result.question,
            "turns_in_session": result.turns_in_session,
            "metric": result.metric,
            "sql": result.sql,
            "columns": list(result.columns),
            "rows": list(result.rows),
            "row_count": result.row_count,
            "latency_ms": result.latency_ms,
            "engine": result.engine,
            "path": result.path,
            "usage": result.usage,
            "validation_issues": list(result.validation_issues),
            "explanation": result.explanation,
            # 图表 spec 独立挂键（ADR-0025 决策 ②）：与 explanation 并列——
            # 归因回答「为什么是这个数」，spec 回答「怎么画」，混层会让
            # 0022 判据 6 的 explanation 键集断言随图表变更漂移
            "chart": result.chart,
            "clarification": (
                _clarify_payload(result.clarification) if result.clarification is not None else None
            ),
            "block_reason": result.block_reason,
            "error": result.error,
            "handoff_reason": result.handoff_reason,
            "snapshot_sha": snapshot.sha if snapshot is not None else None,
            "snapshot_bound_to_head": (snapshot.bound_to_head if snapshot is not None else None),
            "analysis": analysis,
        }
    )
    assert isinstance(payload, dict)  # 静态收窄：_jsonable 递归后必为 dict
    return payload


def _analysis_payload(result: AnalysisResult) -> dict[str, Any] | None:
    """AnalysisResult → 决策⑥固定 17 键投影（键名逐字照抄 ADR-0026 L228-230）。

    `plan is None`（无分析意图 fallback / clarify）→ 返回 None：分析未开始，
    调用方把顶层 `analysis` 键置为该 None（键本身不消失，见 `_turn_payload`）。
    plan 存在时返回 17 键投影，不适用字段用 null / 空数组——「字段全集恒定」
    同 0022 判据 6 口径。

    序列化规则：
    - Decimal → str（`_jsonable` 保精度；totals/items 的数值一律字符串出网）；
    - 步骤按 ANALYSIS_ROLES 角色序 zip（执行顺序即角色序，T05 契约）；成功步
      携带 role/kind/sql/columns/rows/latency_ms，失败步（blocked/error）裁为
      安全摘要——sql/columns/rows 置空，只留角色、终态、原因码与尝试耗时
      （与 T07 checkpoint 证据同口径：被拒 SQL 与失败步已执行 SQL 都不出网，
      底层异常文本不出网；机器可读原因在 analysis.reason_code）；
    - reason_code 取 `effective_reason_code`（attribution 缺失时回落 result 码）。
    """
    plan = result.plan
    if plan is None:
        return None
    attribution = result.attribution
    steps: list[dict[str, Any]] = []
    # 步骤数不得多于角色数（T05 不变量）；strict=False 容纳的是反向合法形态——
    # unavailable 前置门失败时 plan 在而 steps=()（或部分执行后终止），缺位角色
    # 不虚构（N1）。越界（steps > roles）属不变量破坏，必须响亮失败，不能被
    # zip 静默丢尾掩盖（评审 Minor-3）
    assert len(result.steps) <= len(ANALYSIS_ROLES)
    for role, step in zip(ANALYSIS_ROLES, result.steps, strict=False):
        if step.kind == "answer":
            steps.append(
                {
                    "role": role,
                    "kind": step.kind,
                    "sql": step.sql,
                    "columns": list(step.columns),
                    "rows": _jsonable(list(step.rows)),
                    "latency_ms": step.latency_ms,
                }
            )
        else:
            # 失败步安全摘要：不区分「被拒」与「执行后失败」，一律不带 SQL/数据
            steps.append(
                {
                    "role": role,
                    "kind": step.kind,
                    "sql": None,
                    "columns": [],
                    "rows": [],
                    "latency_ms": step.latency_ms,
                    "reason_code": (
                        "guard_blocked" if step.kind == "blocked" else "execution_error"
                    ),
                }
            )
    has_totals = (
        attribution is not None
        and attribution.baseline is not None
        and attribution.current is not None
        and attribution.delta is not None
    )
    payload = _jsonable(
        {
            "schema_version": ANALYSIS_RECORD_VERSION,
            "intent": plan.intent,
            "status": analysis_status(result),
            "metric": plan.metric,
            "dimension": plan.dimension,
            "baseline": {
                "granularity": plan.baseline.granularity,
                "value": _jsonable(plan.baseline.value),
            },
            "current": {
                "granularity": plan.current.granularity,
                "value": _jsonable(plan.current.value),
            },
            "filters": [
                {"column": f.column, "op": f.op, "value": _jsonable(f.value)} for f in plan.filters
            ],
            "snapshot_sha": result.snapshot_sha,
            "semantic_sha256": result.semantic_sha256,
            "recipe_version": plan.recipe_version,
            "totals": (
                {
                    "baseline": attribution.baseline,
                    "current": attribution.current,
                    "delta": attribution.delta,
                }
                if has_totals
                else None
            ),
            "items": (
                [
                    {
                        "value": item.value,
                        "baseline": item.baseline,
                        "current": item.current,
                        "delta": item.delta,
                        "contribution_pct": item.contribution_pct,
                    }
                    for item in attribution.items
                ]
                if attribution is not None
                else []
            ),
            "steps": steps,
            "reason_code": effective_reason_code(result),
            "text": attribution.text if attribution is not None else None,
            "elapsed_ms": result.elapsed_ms,
        }
    )
    assert isinstance(payload, dict)  # 静态收窄：_jsonable 递归后必为 dict
    return payload


# --- ④a SSE 流式序列化（ADR-0028 决策④·执行模型 A compute-then-stream）---
# 事件名为已登记 AG-UI 词表字面量（docs/design/agui-event-mapping.md §2/§4，借鉴非兼容）。
RUN_STARTED = "RUN_STARTED"
STEP_STARTED = "STEP_STARTED"
STEP_FINISHED = "STEP_FINISHED"
TOOL_CALL_RESULT = "TOOL_CALL_RESULT"
STATE_SNAPSHOT = "STATE_SNAPSHOT"
RUN_FINISHED = "RUN_FINISHED"
RUN_ERROR = "RUN_ERROR"


def _iter_analysis_events(
    result: AnalysisResult, turn_payload: dict[str, Any]
) -> Iterator[tuple[str, Any]]:
    """AnalysisResult → AG-UI 词表事件序列（展示专用，N3：不执行、不新编译）。

    全成流：RUN_STARTED → 每 role 一对 STEP_STARTED/STEP_FINISHED（成功步中间插
    TOOL_CALL_RESULT 携已执行事实）→ STATE_SNAPSHOT（完整父轮 TurnPayload，含
    `analysis` 17 键）→ RUN_FINISHED。STATE_SNAPSHOT 直发 `/analyze` 同一份
    TurnPayload（`_analysis_turn_payload`）→ 前端零重构、终态逐字一致（N1 单源）。
    blocked/error 终态步 → 该步 STEP_STARTED 后直接 RUN_ERROR（被拒 SQL 不出网，
    无 TOOL_CALL_RESULT、无 STATE_SNAPSHOT，沿用 _analysis_payload 失败步裁剪口径）。
    无分析意图（plan is None）→ 仅 RUN_STARTED/RUN_FINISHED（analysis 为 null，对齐
    request/response 版）。
    """
    plan = result.plan
    yield RUN_STARTED, {"intent": plan.intent if plan is not None else None}
    if plan is None:
        yield RUN_FINISHED, {"status": analysis_status(result)}
        return
    for role, step in zip(ANALYSIS_ROLES, result.steps, strict=False):
        yield STEP_STARTED, {"role": role}
        if step.kind == "answer":
            yield (
                TOOL_CALL_RESULT,
                _jsonable(
                    {
                        "role": role,
                        "columns": list(step.columns),
                        "rows": list(step.rows),
                        "row_count": step.row_count,
                    }
                ),
            )
            yield STEP_FINISHED, {"role": role, "status": step.kind, "latency_ms": step.latency_ms}
            continue
        yield (
            RUN_ERROR,
            {
                "reason_code": "guard_blocked" if step.kind == "blocked" else "execution_error",
                "text": f"分析步骤 {role} 未成功（{step.kind}）",
            },
        )
        return
    yield STATE_SNAPSHOT, turn_payload
    yield RUN_FINISHED, {"status": analysis_status(result)}


def _sse_frame(event: str, data: Any) -> str:
    """单帧 SSE：``event: <名>\ndata: <json>\n\n``（compact JSON，无换行污染分帧）。"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _compile_plan(body: CompileBody) -> Plan:
    return Plan(
        metric=body.metric,
        dimensions=tuple(body.dimensions),
        time=TimeSpec(body.time.granularity, body.time.value) if body.time is not None else None,
        filters=tuple(Filter(f.column, f.op, f.value) for f in body.filters),
        order_by=tuple(OrderSpec(o.column, o.desc) for o in body.order_by),
        limit=body.limit,
    )


# ---------------------------------------------------------------------------
# SPA 静态面（P0b，ADR-0018 决策 ②⑤）——条件挂载；dist 缺失不阻断 API
# ---------------------------------------------------------------------------

# 全方法注册：若只按 GET 注册，POST 未知路径会被 Starlette 判 405（部分匹配）
# 而不是 404——旧无前缀路径的「硬切 404」含 POST 流（0022 判据 1），必须走
# fallback 的非 GET 分支自行回 404。
_SPA_METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]


def _attach_spa(app: FastAPI, dist: Path) -> None:
    """catch-all 三态分流：真实文件直出 → index.html → 404 JSON（0018 决策 ②）。

    调用时机：create_app 内**必须在两个 router（业务 + 治理）的 include_router
    之后**调用——catch-all 抢先注册会把 /api/v1 的 404 吞成 HTML 200（决策 ② 的
    原文风险：接口错误变静默失败）。

    实现选择（与 0018 文面「StaticFiles 挂载」的偏差，理由在此）：不另挂
    StaticFiles——`mount("/")` 是终止匹配、会把 fallback 一起吞掉；而
    `StaticFiles(html=True)` 只对目录请求回 index.html，多级路径刷新依旧 404
    （决策 ② 亲述的事实）。单路由内三态分流把行为收敛在一处，即判据 7 本身；
    文件直出仍走 FileResponse（StaticFiles 内部同款实现），无能力损失。

    三态（即判定链顺序，勿重排）：
    1. `api/` 前缀（含裸 `api`）→ 404 JSON：API 空间（含未注册路径）保持 JSON
       契约，永不落 HTML；
    2. 非 GET/HEAD → 404：覆盖 POST /plan、POST /compile 等旧无前缀硬切（0022
       判据 1）。**已知副作用**：已知 API 路径配错误方法（如 GET /api/v1/plan）
       从 Starlette 默认 405 归并为 404——0022 契约未承诺 405、全仓无断言无
       消费方依赖（实测 grep），登记为已知行为而非缺陷；
    3. GET/HEAD：命中 dist 内真实文件（vite 产物 assets/*.js|css）回文件，
       否则回 index.html——多级 BrowserRouter 路径（/governance/metrics）刷新
       200 HTML。
    """
    index_file = dist / "index.html"
    dist_root = dist.resolve()

    @app.api_route("/{full_path:path}", methods=_SPA_METHODS, include_in_schema=False)
    def spa_catch_all(request: Request, full_path: str) -> FileResponse:
        """SPA catch-all（三态判定见 _attach_spa docstring；不进 OpenAPI schema）。"""
        if full_path == "api" or full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="Not Found")
        if request.method not in ("GET", "HEAD"):
            raise HTTPException(status_code=404, detail="Not Found")
        # 路径穿越防御：编码过的 `..` 段会解码进 full_path——解析后必须仍在 dist
        # 之内，越界一律当 SPA 深路径回 index.html（而不是去读仓外文件）
        candidate = (dist / full_path).resolve()
        if candidate.is_relative_to(dist_root) and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(index_file)


# ---------------------------------------------------------------------------
# 认证依赖
# ---------------------------------------------------------------------------
# require_bearer / BearerClaims 自 ADR-0022 起住在 serving/auth.py（业务面与治理面
# 共用同一实现——认证语义单源，两个 router 只装配不复制）；本模块直接 import 使用。


# ---------------------------------------------------------------------------
# 应用工厂
# ---------------------------------------------------------------------------


def _default_oidc_factory() -> Callable[[], OidcBff | None]:
    """惰性 OIDC BFF 单例工厂（ADR-0031 D02）：首次访问才解析 env，之后缓存。

    - 全空 → None（演示模式；路由投影 503 oidc_not_configured，前端据此显示角色切换）
    - 部分配置 → OidcConfigError（路由投影 503 oidc_config_incomplete）
    - 全配 → OidcBff 单例——pending/session/JWKS 缓存必须在同一实例上（多实例会
      让回调找不到 state）；grants 从 ATLAS_CONTROL_GRANTS 读，未配置 = 全体零
      能力（fail-closed，可登录不可动作）。

    只缓存成功解析（含演示模式 None）；配置错误不缓存——每次请求如实 503，修正
    env 重启即恢复，不把一次失败永久钉死（单进程模型，ADR-0031 D01）。
    """
    holder: dict[str, OidcBff | None] = {}

    def factory() -> OidcBff | None:
        if "bff" in holder:
            return holder["bff"]
        settings = OidcSettings.from_env()
        bff = None if settings is None else OidcBff(settings, grants=load_control_grants())
        holder["bff"] = bff
        return bff

    return factory


def _control_db_path() -> Path:
    """控制库路径：ATLAS_CONTROL_DB 显式指定 > 仓库 serving/state/control.sqlite。

    与 checkpoint 库（ATLAS_CHECKPOINT_DB）同纪律：路径可配；默认落 serving/state
    （私有状态目录，权限 0700/0600 由 ControlStore 强制）。控制库缺失即现建，
    不是错误——首建只是一个空库加已应用的迁移。
    """
    raw = os.environ.get("ATLAS_CONTROL_DB", "").strip()
    return Path(raw) if raw else REPO_ROOT / "serving" / "state" / "control.sqlite"


def _bundle_root_path() -> Path:
    """发布制品根路径：ATLAS_BUNDLE_ROOT 显式指定 > 仓库 serving/state/bundles。

    与 _control_db_path 同纪律：制品按内容寻址落盘（不可变、不可覆写），默认落
    私有状态目录（gitignored）；路径可配以便容器卷挂载。
    """
    raw = os.environ.get("ATLAS_BUNDLE_ROOT", "").strip()
    return Path(raw) if raw else REPO_ROOT / "serving" / "state" / "bundles"


def _default_control_factory(audit: AuditLog | None = None) -> Callable[[], ControlServices]:
    """惰性控制面单例（ADR-0031 D07/D13）：首个 /runs 请求才建库与运行服务。

    与 _default_oidc_factory 同纪律：create_app 不触碰磁盘（未配置控制面的测试与
    部署照常启动）。装配内容：ControlStore（迁移）+ 单业务队列 ThreadTaskRunner
    + 启动恢复（封存遗留 queued/running、不重跑，D07）+ 部署授予表（fail-closed，
    未配置 = 全体零能力）+ 反馈服务 + 发布服务（制品根 ATLAS_BUNDLE_ROOT；导入
    门禁与 CAS 激活见 serving.control.releases）+ 捕获授权审计接缝（D13：写业务
    审计 JSONL，kind=capture_authorized；审计被禁用或写失败 → 拒绝捕获，fail closed）。

    运行代理按部署 scope 域懒建：`create_live_agent(..., event_sink=StoreEventSink,
    persist_session=False)`——事件写真控制库（D07②），会话上下文是进程内存 15
    分钟窗口（D07），不读 ATLAS_CHECKPOINT_DB、不落盘。
    """
    audit_log = audit if audit is not None else AuditLog()
    holder: dict[str, ControlServices] = {}

    def factory() -> ControlServices:
        if "services" not in holder:
            from serving.control.deployments import DeploymentService
            from serving.control.drafts import DraftService
            from serving.control.events import StoreEventSink
            from serving.control.feedback import FeedbackService
            from serving.control.releases import ReleaseService
            from serving.control.runs import RunAgent, RunService, ThreadTaskRunner
            from serving.control.sources import SourceService
            from serving.control.store import ControlStore

            store = ControlStore(_control_db_path())
            store.migrate()

            def _live_run_agent(record: RunRecord) -> RunAgent:
                from agent.runtime.identity import snapshot_identity

                agent = create_live_agent(
                    model_path=DOMAIN_MODEL_PATHS[record.scope],
                    event_sink=StoreEventSink(store),
                    persist_session=False,
                )
                identity = (
                    snapshot_identity(agent.snapshot) if agent.snapshot is not None else None
                )
                return RunAgent(agent=agent, data_identity=identity)

            def _audit_capture(principal: Principal, request: RunRequest) -> None:
                """D13 捕获授权审计：审计被禁用同样拒绝（fail-closed，不静默放行）。"""
                if not audit_log.enabled:
                    raise RuntimeError("强制审计已关闭：显式捕获拒绝执行（fail-closed）")
                audit_log.record(
                    endpoint=f"{API_PREFIX}/runs",
                    claims=principal.user_context,
                    kind="capture_authorized",
                    bucket="business",
                )

            runs = RunService(
                store,
                agent_resolver=_live_run_agent,
                runner=ThreadTaskRunner(),
                capture_audit=_audit_capture,
            )
            runs.recover_interrupted()
            holder["services"] = ControlServices(
                store=store,
                runs=runs,
                feedback=FeedbackService(store),
                sources=SourceService(store),
                deployments=DeploymentService(store),
                drafts=DraftService(store),
                releases=ReleaseService(
                    store, bundle_root=_bundle_root_path(), repo_root=REPO_ROOT
                ),
                grants=load_control_grants(),
            )
        return holder["services"]

    return factory


def create_app(
    agent_factory: Callable[[str], DataAgent] | None = None,
    *,
    audit: AuditLog | None = None,
    rate_limiter: RateLimiter | None = None,
    governance_rate_limiter: RateLimiter | None = None,
    llm_config: LlmConfig | None = None,
    self_hosted_probe: Callable[[LlmConfig], bool] | None = None,
    narrative_client_factory: Callable[[BackendEndpoint], ChatClient] | None = None,
    oidc_bff_factory: Callable[[], OidcBff | None] | None = None,
    control_services_factory: Callable[[], ControlServices] | None = None,
) -> FastAPI:
    """构造 API 应用（URL 契约 v2，ADR-0022）。

    agent_factory 注入点：测试传 fake（无 DB，仿 tests/test_graph.py FakeExecutor
    注入模式），签名 (model_name) -> DataAgent（按域返回，finance/retail 各自）；
    缺省 _live_agent（真 Doris + 锁定快照，CLI ask 同约束，模型路径按域选择）。
    Agent 按域懒建单例：快照缺失在首个 /ask 暴露（503），meta 就绪后自动恢复；
    治理面不经过本工厂（决策 ④：只读文件，不受快照 503 影响）。

    audit / rate_limiter / governance_rate_limiter 注入点：测试传 tmp 目录 AuditLog /
    小窗口 RateLimiter（不碰真实 .env 与仓库目录）；缺省按 env 构造——审计默认开
    可关（ATLAS_AUDIT_DISABLED）；业务桶 60/60、治理桶 240/60（均为配置占位，不是
    实测容量边界；见 serving/audit.py 与 serving/ratelimit.py）。

    oidc_bff_factory 注入点：测试传受控 BFF（httpx2.MockTransport，不触网）；
    缺省 _default_oidc_factory（惰性单例按 env 解析：全空 = 演示模式 → auth 面
    503 配置阻塞；全配 = 私有 OIDC）。认证面端点不触 agent/DB，与业务面隔离。

    control_services_factory 注入点：测试传受控控制面（tmp 控制库 + 同步任务
    执行器 + 假授予表，不碰真实 .env 与仓库状态）；缺省 _default_control_factory
    （惰性单例：首个 /runs 请求才建控制库、装配运行服务并做启动恢复）。
    """

    def _live_agent(model_name: str) -> DataAgent:
        """生产 agent：按域加载语义模型（P7 双模型；快照预算绑定与 CLI 同源）。"""
        return create_live_agent(model_path=DOMAIN_MODEL_PATHS[model_name])

    factory = agent_factory or _live_agent
    app = FastAPI(
        title="Atlas Serving API",
        version=_package_version(),
        description="Atlas 可信 AI 问数平台 HTTP 服务面（ADR-0012；URL 契约 v2 见 "
        f"ADR-0022，多步分析见 ADR-0026）：/health（根与 {API_PREFIX}/health 双挂"
        f"同一实现）公开；{API_PREFIX}/{{plan,compile,ask,plan/execute,analyze}} 与 "
        f"{API_PREFIX}/governance/*"
        "（8 集合 + 2 钻取，只读文件）需 Bearer JWT（make token 签发）；"
        "请求体 model 字段选择语义模型域（finance|retail，缺省 finance）",
    )
    # 按域懒建单例（finance/retail 各一；会话状态在各自 Agent 的 checkpoint 里，
    # 身份指纹与轮数随之持久化——ADR-0020 决策 ④⑤⑥，api 层不再持有会话表）
    holder: dict[str, dict[str, Any]] = {
        "agents": {name: None for name in DOMAIN_MODEL_PATHS},
    }
    # 审计/限流注入（测试传 tmp/小窗口；缺省按 env 构造——默认开、占位阈值）
    audit_log = audit if audit is not None else AuditLog()
    limiter = rate_limiter if rate_limiter is not None else RateLimiter.from_env()
    # 治理桶独立实例（决策 ⑥）：两桶互不挤占——面板浏览不烧问数配额，反向亦然
    gov_limiter = (
        governance_rate_limiter
        if governance_rate_limiter is not None
        else RateLimiter.governance_from_env()
    )

    # LLM 服务化 seam（ADR-0029）：缺省生产装配（env 配置 + 真探活 + OpenAI 兼容客户端）；
    # 测试注入假配置/探活/客户端以在无网络/无 GPU 下验接地发货路径。
    llm_cfg = llm_config if llm_config is not None else LlmConfig.from_env()
    probe = self_hosted_probe if self_hosted_probe is not None else self_hosted_available
    client_factory = (
        narrative_client_factory
        if narrative_client_factory is not None
        else (lambda ep: OpenAICompatClient(ep.base_url, ep.api_key))
    )

    def _apply_narrative(
        body: AskBody, claims: BearerClaims, payload: dict[str, Any], *, endpoint: str
    ) -> dict[str, Any]:
        """确定性结果之上的 LLM 意图后处理（⑤）。**只在 `llm != "off"` 时动作**，
        且只在成功发货/回落时新增 `narrative` 键——`off` 路径不触本函数任何分支，
        返回的 payload 与接入前逐字一致（向后兼容硬约束）。

        分级：角色无该意图 → 403（⑥，不静默降级）；narrative → 自托管接地叙述（不可用
        /失败/非接地 → 回落确定性模板，`fallback=true`）；candidate-fallback → 仅过
        RBAC 门（注册域内候选链仍不可达，不改确定性实答，守 §10 与诚实口径）。
        """
        intent = body.llm
        if intent == "off":
            return payload
        caps = caps_for_role(str(claims.get("role") or ""))
        decision = resolve_llm_backend(intent, caps, llm_cfg, self_hosted_ok=probe(llm_cfg))
        if decision.action is LlmAction.DENY:
            # ⑦ 先落观测再抛 403（拒因可观测；无 token、无原文）。
            record_llm_narrative(
                intent=intent,
                data_class=decision.data_class,
                tier="none",
                action=str(decision.action),
                grounded=False,
                fallback=False,
                reason_code=decision.reason_code or "role_denied",
            )
            audit_log.record(
                endpoint=endpoint, claims=claims, kind="llm_denied", status=403, bucket="business"
            )
            raise HTTPException(status_code=403, detail=f"角色无权使用 LLM 意图：{intent}")
        if intent != "narrative":
            # candidate-fallback：已门控，本注册域内不改确定性实答（未真调 LLM → 无 token）。
            record_llm_narrative(
                intent=intent,
                data_class=decision.data_class,
                tier=decision.tier,
                action=str(decision.action),
                grounded=False,
                fallback=False,
                reason_code=decision.reason_code,
            )
            return payload
        if decision.action is LlmAction.USE_BACKEND and decision.endpoint is not None:
            client: ChatClient = client_factory(decision.endpoint)
        else:
            client = _NullClient()  # FALLBACK_TEMPLATE：不真调，synthesize 直接回落
        result = synthesize_narrative(payload, decision, client=client)
        # ⑦ 接地叙述观测：只记分级 + token 计数（result.usage 已由 narrative 侧剥去原文）。
        record_llm_narrative(
            intent=intent,
            data_class=decision.data_class,
            tier=result.tier,
            action=str(decision.action),
            grounded=result.grounded,
            fallback=result.fallback,
            reason_code=result.reason_code,
            model=result.model or None,
            usage=result.usage,
        )
        payload["narrative"] = result.to_payload()
        return payload

    def _require_rate_limit(request: Request, claims: BearerClaims) -> None:
        """业务桶限流依赖：per-token 单维共享桶；429 命中也是审计事件。

        /health 不声明本依赖（公开面不限流）；401 无有效 claims 在 require_bearer
        先拒（限流只对已认证请求计数——不放大无效请求成本）。治理桶以同形依赖
        住在 serving/governance.py（决策 ⑥：两桶不共享实例，429 文案标桶名）。
        """
        key = str(claims.get("sub") or claims.get("role") or "anonymous")
        allowed, retry_after = limiter.check(key)
        if not allowed:
            audit_log.record(
                endpoint=request.url.path,
                claims=claims,
                kind="rate_limited",
                status=429,
                bucket="business",
            )
            raise HTTPException(
                status_code=429,
                detail="请求过于频繁（业务面），请稍后再试",
                headers={"Retry-After": str(retry_after)},
            )

    def _agent(model_name: str) -> DataAgent:
        agents = holder["agents"]
        agent = agents[model_name]
        if agent is None:
            try:
                agent = factory(model_name)
            except SnapshotUnavailable as exc:
                # 文案只说「绑定锁定快照失败 + 原始原因」：原前缀「无法绑定评测数据」
                # 有两处不准——① 决策 ③ 拦下的是「绑到的快照缺表」，不是没有数据；
                # ②「评测数据」在 N6 语境特指 eval/runner 的 HEAD 严格路径，会把人
                # 引到评测现场去查一个其实是运行时配置的问题（诚实优先于顺口）
                raise HTTPException(
                    status_code=503, detail=f"无法绑定锁定快照，本轮查询不可执行：{exc}"
                ) from exc
            agents[model_name] = agent
        return agent

    def health() -> dict[str, Any]:
        """存活 + 快照绑定回显（ADR-0019 决策 ⑥ 的 7 键 + ADR-0020 决策 ⑦ 的 boot_id）。

        根与前缀双挂同一 handler（ADR-0022 决策 ①）→ 两条路径返回同一 body。
        公开面、无副作用：只读 git 与 `data/snapshots/`，**不构造 agent**（构造会
        连 Doris，探针不得成为打库入口）。键名与键数在成功/降级两条路径上相同
        （决策 ⑥「只增不改名」+ 字段全集恒定），变的是 `status` 的值：无快照可绑
        却报 `ok`，等于把决策 ① 的回退机制换成新的静默失效。
        """
        try:
            snapshot = resolve_runtime_snapshot()
            # table_count 也在 try 内：meta 形态异常（row_counts 不可解析）同样
            # 属于「说不清绑在哪」，走降级分支而不是 500
            body: dict[str, Any] = {
                "status": "ok",
                "head_sha": snapshot.head,
                "snapshot_sha": snapshot.sha,
                "snapshot_source": snapshot.source,
                "snapshot_bound_to_head": snapshot.bound_to_head,
                "snapshot_created_at": snapshot.meta.get("created_at"),
                "snapshot_tables": snapshot.table_count,
                "boot_id": BOOT_ID,
            }
        except SnapshotUnavailable:
            body = {
                "status": "degraded",
                "head_sha": git_short_sha_or_none(),
                "snapshot_sha": None,
                "snapshot_source": None,
                "snapshot_bound_to_head": None,
                "snapshot_created_at": None,
                "snapshot_tables": None,
                "boot_id": BOOT_ID,
            }
        return body

    # 双挂（决策 ①）：同一 handler 两次注册——根挂点是 compose healthcheck / 外部
    # 探针的既有入口（判据 2 要求 compose 字符串逐字未变），前缀挂点供 SPA 统一
    # 前缀规则（ADR-0018 决策 ②）。两条路径都进 OpenAPI（判据 1 的 16 条之一）。
    app.add_api_route(
        "/health",
        health,
        methods=["GET"],
        tags=["ops"],
        summary="存活 + 快照绑定回显（公开面；根与 /api/v1 双挂同一实现）",
    )
    app.add_api_route(
        f"{API_PREFIX}/health",
        health,
        methods=["GET"],
        tags=["ops"],
        summary="存活 + 快照绑定回显（公开面；根与 /api/v1 双挂同一实现）",
    )

    # 业务面 router（契约 v2 决策 ①②）：前缀只在 APIRouter 声明一次，装饰器只写
    # 相对段——路径前缀的唯一事实源
    router = APIRouter(prefix=API_PREFIX, tags=["business"])

    @router.post("/plan", summary="问句 → Plan（歧义返回 kind=clarify；model 选域）")
    def plan(
        body: QuestionBody,
        _claims: BearerClaims,
        _rl: None = Depends(_require_rate_limit),
    ) -> dict[str, Any]:
        """问句 → 结构化 Plan；无法确定性解析时 200 + kind=clarify（CLI exit 1 语义）。"""
        model_name = _model_name(body.model)
        model = SemanticModel(DOMAIN_MODEL_PATHS[model_name])
        result = Planner(model).plan(body.question)
        if isinstance(result, ClarificationRequest):
            audit_log.record(
                endpoint=f"{API_PREFIX}/plan",
                claims=_claims,
                kind="clarify",
                bucket="business",
            )
            return {"kind": "clarify", "plan": None, "clarification": _clarify_payload(result)}
        audit_log.record(
            endpoint=f"{API_PREFIX}/plan", claims=_claims, kind="plan", bucket="business"
        )
        return {"kind": "plan", "plan": _plan_payload(result), "clarification": None}

    @router.post("/compile", summary="Plan JSON → 只读 SQL（Doris 方言；model 选域）")
    def compile_plan(
        body: CompileBody,
        _claims: BearerClaims,
        _rl: None = Depends(_require_rate_limit),
    ) -> dict[str, Any]:
        """Plan 结构非法 → 422（pydantic）；编译失败（字段不在语义层）→ 422。"""
        model_name = _model_name(body.model)
        model = SemanticModel(DOMAIN_MODEL_PATHS[model_name])
        try:
            sql, _ = Compiler(model).compile(_compile_plan(body))
        except CompileError as exc:
            audit_log.record(
                endpoint=f"{API_PREFIX}/compile",
                claims=_claims,
                kind="compile_error",
                status=422,
                bucket="business",
            )
            raise HTTPException(status_code=422, detail=f"编译失败：{exc}") from exc
        audit_log.record(
            endpoint=f"{API_PREFIX}/compile", claims=_claims, kind="compiled", bucket="business"
        )
        return {"sql": sql}

    @router.post("/ask", summary="问数会话（真实 Doris + 锁定快照；model 选域；单轮/多轮）")
    def ask(
        body: AskBody,
        _claims: BearerClaims,
        _rl: None = Depends(_require_rate_limit),
    ) -> dict[str, Any]:
        """单轮问答；session_id 复用即多轮续接（状态在 Agent 的 checkpointer 里，
        默认 MemorySaver 进程内、设 ATLAS_CHECKPOINT_DB 后跨重启续接，ADR-0020）。

        model 缺省 finance：旧请求体向后兼容；session_id 按模型隔离——同键跨
        模型请求是两个独立会话（finance/retail Agent 各自单例）。

        身份下推（C2 硬化）：已验证 claims 即本轮身份 → agent.ask(identity=…)
        行级策略随 Guard 注入；会话首轮在 checkpoint 里绑定 claims 指纹，同会话
        换身份（含重签 token）→ agent 抛 SessionIdentityConflict → 422 冲突
        （换身份必须换 session_id；指纹随会话持久化，见 ADR-0020 决策 ⑥）。
        """
        model_name = _model_name(body.model)
        agent = _agent(model_name)
        sid = body.session_id
        try:
            result = agent.ask(body.question, session_id=sid, identity=_claims)
        except SessionIdentityConflict as exc:
            audit_log.record(
                endpoint=f"{API_PREFIX}/ask",
                claims=_claims,
                session_id=sid,
                kind="conflict",
                status=422,
                bucket="business",
            )
            raise HTTPException(status_code=422, detail="会话身份冲突，请换新 session_id") from exc
        audit_log.record(
            endpoint=f"{API_PREFIX}/ask",
            claims=_claims,
            session_id=result.session_id,
            kind=result.kind,
            # row_count/latency_ms 只在 answer 轮有语义（执行过 SQL）；
            # clarify/blocked/error/handoff 无值即 null（字段全集恒定）
            row_count=result.row_count if result.kind == "answer" else None,
            latency_ms=result.latency_ms if result.kind == "answer" else None,
            bucket="business",
        )
        payload = _turn_payload(result, agent.snapshot)
        return _apply_narrative(body, _claims, payload, endpoint=f"{API_PREFIX}/ask")

    @router.post(
        "/plan/execute",
        summary="Plan 直接执行（复用 Guard 唯一通道；kind ∈ answer|blocked|error）",
    )
    def plan_execute(
        body: PlanExecuteBody,
        _claims: BearerClaims,
        _rl: None = Depends(_require_rate_limit),
    ) -> dict[str, Any]:
        """执行结构化的 Plan（`/plan` 的产物可直接回填；`/compile` 请求体同构 + 两键）。

        与 `/ask` 的差异只在入口：不经过 Planner（graph 的 `plan_override` 短路，
        没有自然语言解析面 → 永不 `clarify`），执行仍经 `node_execute` 唯一通道
        （编译 → `resolve_claims` 渲染策略 → Guard + 二次只读校验 → 执行）——
        HTTP 层不复制执行逻辑（N3）。响应与 `/ask` 同构（`_turn_payload` 字段全集）。

        - 指标名不存在 → `kind="error"`（CompileError 在 graph 内转回合级 error，
          不是 422 不是 500；ADR-0022 代价⑤的契约形态）；
        - 带 identity → 与 /ask 同机制注入行级策略（claims → resolve_claims）；
        - session_id 缺省 = 一次性 thread（不续接、不产生可复用的会话态）；给定则与
          `/ask` 同一会话空间（含身份指纹 422 约束），且本轮 Plan 会成为该会话下轮
          残句追问的补全基线（ADR-0022 代价⑥，README 已写明）。
        """
        model_name = _model_name(body.model)
        plan = _compile_plan(body)
        agent = _agent(model_name)
        try:
            result = agent.run_plan(
                plan,
                session_id=body.session_id,
                identity=_claims,
                question=body.question or _plan_text(plan),
            )
        except SessionIdentityConflict as exc:
            audit_log.record(
                endpoint=f"{API_PREFIX}/plan/execute",
                claims=_claims,
                session_id=body.session_id,
                kind="conflict",
                status=422,
                bucket="business",
            )
            raise HTTPException(status_code=422, detail="会话身份冲突，请换新 session_id") from exc
        audit_log.record(
            endpoint=f"{API_PREFIX}/plan/execute",
            claims=_claims,
            session_id=result.session_id,
            kind=result.kind,
            row_count=result.row_count if result.kind == "answer" else None,
            latency_ms=result.latency_ms if result.kind == "answer" else None,
            bucket="business",
        )
        return _turn_payload(result, agent.snapshot)

    def _analysis_turn_payload(
        result: AnalysisResult, snapshot: RuntimeSnapshot | None
    ) -> dict[str, Any]:
        """`/analyze` 与 `/analyze/stream` 共用的父轮 TurnPayload 单源构造（N1）。

        含决策⑥净化（T08/评审 Minor-2）：仅分析轮（plan 在）的 error 出网前裁为
        稳定文案（底层异常含连接信息不出网）；无意图 fallback 轮（plan=None）与
        /ask 同形不净化。两种传输面共用本函数 → 终态逐字一致（流式非第二事实源）。
        """
        turn = result.turn
        if result.plan is not None and turn.kind == "error" and turn.error is not None:
            turn = replace(turn, error="分析子步骤执行失败（execution_error）")
        return _turn_payload(turn, snapshot, analysis=_analysis_payload(result))

    @router.post(
        "/analyze",
        summary="多步变化贡献分析（ADR-0026；AskBody/Bearer/模型选择/限流与 /ask 同源）",
    )
    def analyze(
        body: AskBody,
        _claims: BearerClaims,
        _rl: None = Depends(_require_rate_limit),
    ) -> dict[str, Any]:
        """多期间变化贡献分析（决策③⑥）：请求面与 /ask 完全同构，产出面多一个 `analysis`。

        复用项（ADR-0026 L220-233）：AskBody 逐字同 /ask（不新增身份字段，身份只来自
        服务端 Bearer claims）、model 选域懒建同一 Agent 单例、同一业务限流桶、
        SessionIdentityConflict → 422 冲突审计 + 「请换新 session_id」、快照缺失 503。
        分析无意图时 agent.analyze 内部按决策③回落普通 ask（kind=answer 且携带
        单 SQL 结果），无法解析时 kind=clarify——两种回落 `analysis` 值均为 null。

        决策⑥响应形态：
        - 父轮不冒充单 SQL 结果：sql/explanation null、columns/rows 空、row_count=0、
          metric=目标指标、latency_ms=已执行子 SQL 耗时和；
        - `analysis` 17 键投影（`_analysis_payload`）：键恒在，非分析为 null；
        - 执行故障轮 error 字段**出网前净化**为稳定文案——仅分析轮（plan 在）：
          node_execute 的 error 值含底层异常文本，如连接串信息（决策⑥：分析路径
          不泄露；机器可读原因留在 analysis.reason_code）；无意图 fallback 轮
          （plan=None）没有分析子步，与 /ask /plan/execute 维持 R1 现状同形
          （原始 error 文本与 post-Guard SQL 照常出网），误标「分析子步骤失败」
          属归因错误（评审 Minor-2）；
        - 一次 HTTP 请求恰一条业务审计行（与 /ask 同款 answer/blocked/error 形态）。
        """
        model_name = _model_name(body.model)
        agent = _agent(model_name)
        sid = body.session_id
        try:
            result = agent.analyze(body.question, session_id=sid, identity=_claims)
        except SessionIdentityConflict as exc:
            audit_log.record(
                endpoint=f"{API_PREFIX}/analyze",
                claims=_claims,
                session_id=sid,
                kind="conflict",
                status=422,
                bucket="business",
            )
            raise HTTPException(status_code=422, detail="会话身份冲突，请换新 session_id") from exc
        payload = _analysis_turn_payload(result, agent.snapshot)
        payload = _apply_narrative(body, _claims, payload, endpoint=f"{API_PREFIX}/analyze")
        audit_log.record(
            endpoint=f"{API_PREFIX}/analyze",
            claims=_claims,
            session_id=payload["session_id"],
            kind=payload["kind"],
            row_count=payload["row_count"] if payload["kind"] == "answer" else None,
            latency_ms=payload["latency_ms"] if payload["kind"] == "answer" else None,
            bucket="business",
        )
        return payload

    @router.post(
        "/analyze/stream",
        summary="多步归因 SSE 流式面（ADR-0028 ④a·执行模型 A compute-then-stream）",
        response_class=StreamingResponse,
    )
    def analyze_stream(
        body: AskBody,
        _claims: BearerClaims,
        _rl: None = Depends(_require_rate_limit),
    ) -> StreamingResponse:
        """④a：`/analyze` 的流式展示面——请求面与 `/analyze` 同构（AskBody/Bearer/限流）。

        执行模型 A（compute-then-stream，非边执行边流）：调 `agent.analyze()`
        （SQL 已全部经 Guard，内部锁在返回时已释放）→ 把已算好的分步产物回放为
        SSE。本端点自身不构造/执行任何 SQL（N3 唯一通道 = agent.analyze）；事件只
        承载展示。借鉴 AG-UI 词表 ≠ 兼容（N2）。
        """
        model_name = _model_name(body.model)
        agent = _agent(model_name)
        sid = body.session_id
        try:
            result = agent.analyze(body.question, session_id=sid, identity=_claims)
        except SessionIdentityConflict as exc:
            audit_log.record(
                endpoint=f"{API_PREFIX}/analyze/stream",
                claims=_claims,
                session_id=sid,
                kind="conflict",
                status=422,
                bucket="business",
            )
            raise HTTPException(status_code=422, detail="会话身份冲突，请换新 session_id") from exc
        # 终态 STATE_SNAPSHOT 直发 `/analyze` 同一份 TurnPayload（单源、零重构）。
        turn_payload = _analysis_turn_payload(result, agent.snapshot)
        turn_payload = _apply_narrative(
            body, _claims, turn_payload, endpoint=f"{API_PREFIX}/analyze/stream"
        )
        audit_log.record(
            endpoint=f"{API_PREFIX}/analyze/stream",
            claims=_claims,
            session_id=turn_payload["session_id"],
            kind=turn_payload["kind"],
            row_count=turn_payload["row_count"] if turn_payload["kind"] == "answer" else None,
            latency_ms=turn_payload["latency_ms"] if turn_payload["kind"] == "answer" else None,
            bucket="business",
        )
        frames = [
            _sse_frame(name, data) for name, data in _iter_analysis_events(result, turn_payload)
        ]
        return StreamingResponse(
            iter(frames),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # 装配（决策 ①②④）：业务 router + 治理 router 同 app；治理面不经过 _agent，
    # 快照缺失时业务面 503 与治理面 200 并存（判据 5 的隔离断言）
    app.include_router(router)
    app.include_router(
        build_governance_router(
            audit=audit_log,
            rate_limiter=gov_limiter,
            model_paths=DOMAIN_MODEL_PATHS,
            require_model=_model_name,
        )
    )
    # 认证面 router（ADR-0031 D02/D13 /auth/*）：同受 SPA 约束——必须在本行之前的
    # 已有 include 之后、catch-all 之前（catch-all 会吞掉 /auth 的 JSON 404）
    auth_factory = oidc_bff_factory if oidc_bff_factory is not None else _default_oidc_factory()
    app.include_router(build_auth_router(auth_factory, prefix=f"{API_PREFIX}/auth"))
    # 运行面 router（ADR-0031 D07/D13 /runs）：与 auth 面同受 SPA 约束——必须在
    # 已有 include 之后、catch-all 之前。控制面惰性装配（首个 /runs 请求才建库）。
    control_factory = (
        control_services_factory
        if control_services_factory is not None
        else _default_control_factory(audit_log)
    )
    app.include_router(build_control_router(control_factory, prefix=API_PREFIX))

    # SPA 静态面（P0b，ADR-0018 决策 ②⑤）：条件挂载——**必须在上面两条
    # include_router 之后**，否则 catch-all 吞掉 /api/v1 的 404（决策 ②）。
    # dist 不存在则跳过并打 warning：fresh clone / 未构建前端时 API 照常可用
    if (FRONTEND_DIST / "index.html").is_file():
        _attach_spa(app, FRONTEND_DIST)
    else:
        logger.warning(
            "frontend/dist/index.html 不存在——跳过 SPA 挂载（API 照常可用）；"
            "需要界面时先跑 `make ui-build`"
        )

    return app


# uvicorn serving.api:app 入口（make serve；workers=1，见模块 docstring）
app = create_app()
