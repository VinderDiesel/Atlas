"""Atlas HTTP 服务面 v2（ADR-0022）：/api/v1 前缀、/plan/execute、治理面、限流两桶。

契约口径（诚实声明）
--------------------
- 路由契约 v2（ADR-0022 决策 ①②）：业务端点一律挂 `/api/v1` 前缀
  （/api/v1/plan /compile /ask /plan/execute），旧无前缀路径**硬切**（404，
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
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from importlib import metadata as importlib_metadata
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

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
from agent.planner import ClarificationRequest, Planner
from agent.state import TurnResult
from data.identity import (
    RuntimeSnapshot,
    git_short_sha_or_none,
    resolve_runtime_snapshot,
)
from serving.audit import AuditLog
from serving.auth import BearerClaims
from serving.governance import build_governance_router
from serving.ratelimit import RateLimiter

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


def _turn_payload(result: TurnResult, snapshot: RuntimeSnapshot | None = None) -> dict[str, Any]:
    """TurnResult → 平铺 JSON：字段全集稳定输出（kind 无关字段为 null）。

    `snapshot` 是**该 agent 构造时解析出的绑定**（`DataAgent.snapshot`），由调用方
    传入而不是在这里再 `resolve_runtime_snapshot()` 一次：本轮回答用的白名单来自
    建 agent 那一刻的解析结果，重新解析可能拿到另一份快照（期间有人重锁或 HEAD
    前进），于是回显的 sha 与真正执行查询的 sha 不是同一个——那正是决策 ⑥ 要消灭的
    「看不出绑在哪」。None（测试桩 / 评测路径构造的 agent）→ 两键为 null，
    键本身不消失（字段全集恒定）。

    两键与 `explanation` **并列**而不塞进去：快照绑定状态是回合级事实（与 `kind`
    / `row_count` 同层），不是归因解释的一部分（0019 决策 ⑥ 的语义归属裁定）。
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
        }
    )
    assert isinstance(payload, dict)  # 静态收窄：_jsonable 递归后必为 dict
    return payload


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


def create_app(
    agent_factory: Callable[[str], DataAgent] | None = None,
    *,
    audit: AuditLog | None = None,
    rate_limiter: RateLimiter | None = None,
    governance_rate_limiter: RateLimiter | None = None,
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
    """

    def _live_agent(model_name: str) -> DataAgent:
        """生产 agent：按域加载语义模型（P7 双模型；快照预算绑定与 CLI 同源）。"""
        return create_live_agent(model_path=DOMAIN_MODEL_PATHS[model_name])

    factory = agent_factory or _live_agent
    app = FastAPI(
        title="Atlas Serving API",
        version=_package_version(),
        description="Atlas 可信 AI 问数平台 HTTP 服务面（ADR-0012；URL 契约 v2 见 "
        f"ADR-0022）：/health（根与 {API_PREFIX}/health 双挂同一实现）公开；"
        f"{API_PREFIX}/{{plan,compile,ask,plan/execute}} 与 {API_PREFIX}/governance/*"
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
        return _turn_payload(result, agent.snapshot)

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
