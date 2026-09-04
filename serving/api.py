"""Atlas HTTP 服务面 v1（ADR-0012）：/health /plan /compile /ask + Bearer JWT。

契约口径（诚实声明）
--------------------
- 确定性默认：engine=stub，DataAgent 走确定性链路（Planner → Compiler → Guard →
  Doris 执行），LLM 引擎服务化属 Phase 2（ADR-0012）。
- 会话语义与 CLI 一致：Agent 在进程内懒建**单例**（MemorySaver checkpointer 与
  _session_turns 是实例态，每请求新建 = 会话断裂）；uvicorn 必须 workers=1
  （多 worker = 多份进程内状态，README KL #28）。
- 多模型路由（P7，2026-09-05）：/plan /compile /ask 请求体带 `model` 字段
  （finance|retail，缺省 finance——向后兼容，旧请求体零变化）；/plan /compile
  按域构造语义模型（无状态，与 v1 同构），/ask 按域懒建独立 Agent 单例
  （finance/retail 各一）——会话键（session_id）按模型隔离，跨模型不续接。
- 认证复用 serving/auth.py：verify_token 走 env ATLAS_JWT_SECRET（N9，无默认
  密钥）；v1 不注入行级角色策略（与 CLI 同口径，角色注入属 0011 gateway 硬化项，
  README KL #28 ③；零售角色的带身份实测载体 = rls-verify 零售档 + demo 集成
  测试，同机制：resolve_policy → Guard Policy 注入）。
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
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException
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
from agent.graph import DataAgent
from agent.planner import ClarificationRequest, Planner
from agent.state import TurnResult
from serving.auth import AuthError, verify_token

REPO_ROOT = Path(__file__).resolve().parent.parent
# 域 → 语义模型 YAML（与 eval/runner.DOMAIN_MODELS 同路径；模型选择白名单）
DOMAIN_MODEL_PATHS: dict[str, Path] = {
    "finance": FINANCE_MODEL,
    "retail": REPO_ROOT / "semantic" / "ossie" / "atlas_retail.ossie.yaml",
}

# ---------------------------------------------------------------------------
# 请求/响应模型（HTTP 面粗约束；细约束在 Guard/Planner，不在此复制）
# ---------------------------------------------------------------------------

# 模型选择：P7 多模型路由（缺省 finance 向后兼容；未知模型 → 422）


def _model_name(model: str) -> str:
    """校验 model 字段 ∈ 域白名单（未知模型 → 422，不落到语义层）。"""
    if model not in DOMAIN_MODEL_PATHS:
        raise HTTPException(
            status_code=422, detail=f"未知模型：{model!r}（可选 finance|retail）"
        )
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


def create_app(
    agent_factory: Callable[[str], DataAgent] | None = None,
) -> FastAPI:
    """构造 API 应用。

    agent_factory 注入点：测试传 fake（无 DB，仿 tests/test_graph.py FakeExecutor
    注入模式），签名 (model_name) -> DataAgent（按域返回，finance/retail 各自）；
    缺省 _live_agent（真 Doris + 锁定快照，CLI ask 同约束，模型路径按域选择）。
    Agent 按域懒建单例：快照缺失在首个 /ask 暴露（503），meta 就绪后自动恢复。
    """

    def _live_agent(model_name: str) -> DataAgent:
        """生产 agent：按域加载语义模型（P7 双模型；快照预算绑定与 CLI 同源）。"""
        return create_live_agent(model_path=DOMAIN_MODEL_PATHS[model_name])

    factory = agent_factory or _live_agent
    app = FastAPI(
        title="Atlas Serving API",
        version="0.1.0",
        description="Atlas 可信 AI 问数平台 HTTP 服务面 v1（ADR-0012）："
        "/health 公开；/plan /compile /ask 需 Bearer JWT（make token 签发）；"
        "请求体 model 字段选择语义模型域（finance|retail，缺省 finance）",
    )
    # 按域懒建单例（finance/retail 各一；会话状态按模型隔离）
    holder: dict[str, dict[str, DataAgent | None]] = {
        "agents": {name: None for name in DOMAIN_MODEL_PATHS}
    }

    def _agent(model_name: str) -> DataAgent:
        agents = holder["agents"]
        agent = agents[model_name]
        if agent is None:
            try:
                agent = factory(model_name)
            except SnapshotUnavailable as exc:
                raise HTTPException(
                    status_code=503, detail=f"快照不可用，无法绑定评测数据：{exc}"
                ) from exc
            agents[model_name] = agent
        return agent

    @app.get("/health")
    def health() -> dict[str, str | None]:
        """存活与快照绑定状态（公开面）。"""
        # 延迟 import：health 是公开端点，不触碰数据库（runner 模块仅 git + 文件）
        from eval.runner import SNAPSHOT_DIR, git_short_sha

        sha = git_short_sha()
        snapshot_sha = sha if (SNAPSHOT_DIR / f"{sha}.meta.json").is_file() else None
        return {"status": "ok", "head_sha": sha, "snapshot_sha": snapshot_sha}

    @app.post("/plan", summary="问句 → Plan（歧义返回 kind=clarify；model 选域）")
    def plan(
        body: QuestionBody,
        _claims: BearerClaims,
    ) -> dict[str, Any]:
        """问句 → 结构化 Plan；无法确定性解析时 200 + kind=clarify（CLI exit 1 语义）。"""
        model_name = _model_name(body.model)
        model = SemanticModel(DOMAIN_MODEL_PATHS[model_name])
        result = Planner(model).plan(body.question)
        if isinstance(result, ClarificationRequest):
            return {"kind": "clarify", "plan": None, "clarification": _clarify_payload(result)}
        return {"kind": "plan", "plan": _plan_payload(result), "clarification": None}

    @app.post("/compile", summary="Plan JSON → 只读 SQL（Doris 方言；model 选域）")
    def compile_plan(
        body: CompileBody,
        _claims: BearerClaims,
    ) -> dict[str, Any]:
        """Plan 结构非法 → 422（pydantic）；编译失败（字段不在语义层）→ 422。"""
        model_name = _model_name(body.model)
        model = SemanticModel(DOMAIN_MODEL_PATHS[model_name])
        try:
            sql, _ = Compiler(model).compile(_compile_plan(body))
        except CompileError as exc:
            raise HTTPException(status_code=422, detail=f"编译失败：{exc}") from exc
        return {"sql": sql}

    @app.post("/ask", summary="问数会话（真实 Doris + 锁定快照；model 选域；单轮/多轮）")
    def ask(
        body: AskBody,
        _claims: BearerClaims,
    ) -> dict[str, Any]:
        """单轮问答；session_id 复用即多轮续接（进程内内存态，重启即失）。

        model 缺省 finance：旧请求体向后兼容；session_id 按模型隔离——同键跨
        模型请求是两个独立会话（finance/retail Agent 各自单例）。
        """
        model_name = _model_name(body.model)
        agent = _agent(model_name)
        return _turn_payload(agent.ask(body.question, session_id=body.session_id))

    return app


# uvicorn serving.api:app 入口（make serve；workers=1，见模块 docstring）
app = create_app()
