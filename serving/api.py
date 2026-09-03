"""Atlas HTTP 服务面 v1（ADR-0012）：/health /plan /compile /ask + Bearer JWT。

契约口径（诚实声明）
--------------------
- 确定性默认：engine=stub，DataAgent 走确定性链路（Planner → Compiler → Guard →
  Doris 执行），LLM 引擎服务化属 Phase 2（ADR-0012）。
- 会话语义与 CLI 一致：Agent 在进程内懒建**单例**（MemorySaver checkpointer 与
  _session_turns 是实例态，每请求新建 = 会话断裂）；uvicorn 必须 workers=1
  （多 worker = 多份进程内状态，README KL #28）。
- 认证复用 serving/auth.py：verify_token 走 env ATLAS_JWT_SECRET（N9，无默认
  密钥）；v1 不注入行级角色策略（与 CLI 同口径，角色注入属 0011 gateway 硬化项）。
- 序列化：Decimal → str（保精度，不进浮点）、datetime/date → ISO8601、Enum →
  value、tuple → list——FastAPI 的 jsonable_encoder 会把 Decimal 转 float 丢精度，
  故 rows 值必须在此先行转换（确定性文本不经浮点）。
- CLI 语义的 HTTP 化：plan 歧义 exit 1 → 200 + kind=clarify；快照 meta 缺失
  （SystemExit 语义）→ 503。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from agent.compiler import CompileError, Compiler, Filter, OrderSpec, Plan, SemanticModel, TimeSpec
from agent.factory import SnapshotUnavailable, create_live_agent
from agent.graph import DataAgent
from agent.planner import ClarificationRequest, Planner
from agent.state import TurnResult
from serving.auth import AuthError, verify_token

# ---------------------------------------------------------------------------
# 请求/响应模型（HTTP 面粗约束；细约束在 Guard/Planner，不在此复制）
# ---------------------------------------------------------------------------


class QuestionBody(BaseModel):
    question: str = Field(min_length=1, max_length=500, description="自然语言问句")


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
    if isinstance(value, (datetime, date)):
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


def _turn_payload(result: TurnResult) -> dict[str, Any]:
    """TurnResult → 平铺 JSON：字段全集稳定输出（kind 无关字段为 null）。"""
    return _jsonable(
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
            "clarification": (
                _clarify_payload(result.clarification)
                if result.clarification is not None
                else None
            ),
            "block_reason": result.block_reason,
            "error": result.error,
            "handoff_reason": result.handoff_reason,
        }
    )


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
# 认证依赖
# ---------------------------------------------------------------------------


def require_bearer(authorization: str | None = Header(default=None)) -> dict[str, object]:
    """Bearer JWT → claims。失败一律 401，不外泄校验细节（0011 口径）。"""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401, detail="缺少 Bearer 访问令牌", headers={"WWW-Authenticate": "Bearer"}
        )
    token = authorization.removeprefix("Bearer ").strip()
    try:
        return verify_token(token)
    except AuthError as exc:
        raise HTTPException(status_code=401, detail="访问令牌无效或已过期") from exc


BearerClaims = Annotated[dict[str, object], Depends(require_bearer)]


# ---------------------------------------------------------------------------
# 应用工厂
# ---------------------------------------------------------------------------


def create_app(agent_factory: Callable[[], DataAgent] | None = None) -> FastAPI:
    """构造 API 应用。

    agent_factory 注入点：测试传 fake（无 DB，仿 tests/test_graph.py FakeExecutor
    注入模式）；缺省 create_live_agent（真 Doris + 锁定快照，CLI ask 同约束）。
    Agent 懒建单例：快照缺失在首个 /ask 暴露（503），meta 就绪后自动恢复。
    """

    factory = agent_factory or create_live_agent
    app = FastAPI(
        title="Atlas Serving API",
        version="0.1.0",
        description="Atlas 可信 AI 问数平台 HTTP 服务面 v1（ADR-0012）："
        "/health 公开；/plan /compile /ask 需 Bearer JWT（make token 签发）",
    )
    holder: dict[str, DataAgent | None] = {"agent": None}

    def _agent() -> DataAgent:
        agent = holder["agent"]
        if agent is None:
            try:
                agent = factory()
            except SnapshotUnavailable as exc:
                raise HTTPException(
                    status_code=503, detail=f"快照不可用，无法绑定评测数据：{exc}"
                ) from exc
            holder["agent"] = agent
        return agent

    @app.get("/health")
    def health() -> dict[str, str | None]:
        """存活与快照绑定状态（公开面）。"""
        # 延迟 import：health 是公开端点，不触碰数据库（runner 模块仅 git + 文件）
        from eval.runner import SNAPSHOT_DIR, git_short_sha

        sha = git_short_sha()
        snapshot_sha = sha if (SNAPSHOT_DIR / f"{sha}.meta.json").is_file() else None
        return {"status": "ok", "head_sha": sha, "snapshot_sha": snapshot_sha}

    @app.post("/plan", summary="问句 → Plan（歧义返回 kind=clarify）")
    def plan(
        body: QuestionBody,
        _claims: BearerClaims,
    ) -> dict[str, Any]:
        """问句 → 结构化 Plan；无法确定性解析时 200 + kind=clarify（CLI exit 1 语义）。"""
        result = Planner(SemanticModel()).plan(body.question)
        if isinstance(result, ClarificationRequest):
            return {"kind": "clarify", "plan": None, "clarification": _clarify_payload(result)}
        return {"kind": "plan", "plan": _plan_payload(result), "clarification": None}

    @app.post("/compile", summary="Plan JSON → 只读 SQL（Doris 方言）")
    def compile_plan(
        body: CompileBody,
        _claims: BearerClaims,
    ) -> dict[str, Any]:
        """Plan 结构非法 → 422（pydantic）；编译失败（字段不在语义层）→ 422。"""
        try:
            sql, _ = Compiler(SemanticModel()).compile(_compile_plan(body))
        except CompileError as exc:
            raise HTTPException(status_code=422, detail=f"编译失败：{exc}") from exc
        return {"sql": sql}

    @app.post("/ask", summary="问数会话（真实 Doris + 锁定快照；单轮/多轮）")
    def ask(
        body: AskBody,
        _claims: BearerClaims,
    ) -> dict[str, Any]:
        """单轮问答；session_id 复用即多轮续接（进程内内存态，重启即失）。"""
        return _turn_payload(_agent().ask(body.question, session_id=body.session_id))

    return app


# uvicorn serving.api:app 入口（make serve；workers=1，见模块 docstring）
app = create_app()
