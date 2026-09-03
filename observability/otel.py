"""回合级 OpenTelemetry 埋点（Day 50，批次 E：可观测、评审、发布）。

目标（第 8 周交付门槛）
    OTel trace 能追到 question_id → metric_id → SQL → rows → cost。

口径与边界（诚实注记；改口径前重读 AGENTS.md 第 9 节）
1. 单 span 回合模型：确定性主链（plan → compile → guard → execute）无 LLM
   调用，不虚构 gen_ai 子 span；每回合完成时打一个 ``atlas.turn`` span，
   属性覆盖 question_id / metric_id / SQL / rows / latency / snapshot 版本。
2. ``gen_ai.token_cost`` 指标单位为 token（未接定价表不换算 USD——避免伪
   精确）；确定性回合 0 token → 不产出该指标数据点（如实，不虚报模型调用）。
3. prompt_version 仅在 LLM 候选链真正消耗 token 时记录（值读
   agent/prompts/generator_plan.yaml 的 version）；确定性回合不虚构
   model / prompt_version（N2 时态规范）。
4. 默认 no-op：未调用 configure_otel（或未设 OTEL_EXPORTER_OTLP_ENDPOINT）
   时不产生任何 I/O；埋点自身异常一律吞掉并告警一次——观测绝不反噬主链路。

启用方式（三选一，幂等，重复调用以最后一次为准）
    export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318   # 环境变量
    from observability.otel import configure_otel; configure_otel()  # 同效果
    configure_otel(tracer_provider=..., metric_reader=...)  # 测试注入

指标清单（Day 51 面板数据源，命名与属性随代码演进）
    gen_ai.token_cost  Counter  单位 token；属性 engine/prompt_version
    atlas.turn.count   Counter  属性 kind/engine（回合总量 → QPS/拒绝率分母）
    atlas.turn.latency Histogram 单位 ms；属性 kind（→ TTFT 近似/p95）
    atlas.turn.blocked Counter  属性 reason（Guard 拒绝 → 拒绝率分子）
"""

from __future__ import annotations

import atexit
import os
import re
import threading
import warnings
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any

from opentelemetry.metrics import Counter, Histogram, Meter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader, PeriodicExportingMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import SpanKind, Tracer

if TYPE_CHECKING:
    from agent.state import TurnResult

# --------------------------------------------------------------------------
# 配置态（模块级单例；configure_otel 可重复调用以重建——测试用注入）
# --------------------------------------------------------------------------
_TRACER_PROVIDER: TracerProvider | None = None
_TRACER: Tracer | None = None
_METER_PROVIDER: MeterProvider | None = None
_METER: Meter | None = None
_LOCK = threading.Lock()
_WARNED_TELEMETRY_FAILURE = False  # 埋点故障只告警一次，避免刷屏
_ATEXIT_FLUSH_REGISTERED = False  # 短进程（CLI 单回合）退出前 flush 一次即可

# gen_ai.* 语义约定键 + atlas.* 自定义键（集中定义，防拼写漂移）
ATTR_QUESTION_ID = "atlas.question_id"  # session_id#t<轮数>（会话内可复现回放）
ATTR_SESSION_ID = "atlas.session_id"
ATTR_KIND = "atlas.turn.kind"  # answer / clarify / blocked / error / handoff
ATTR_ENGINE = "atlas.engine"  # deterministic / stub / openai
ATTR_PATH = "atlas.path"  # deterministic / candidate
ATTR_METRIC_ID = "atlas.metric_id"  # 命中 Metric 名（未命中不写）
ATTR_SQL = "atlas.turn.sql"  # Guard 出口 SQL（已含 LIMIT；blocked 回合为被拒 SQL）
ATTR_ROWS = "atlas.turn.rows"  # 执行结果行数
ATTR_LATENCY_MS = "atlas.turn.latency_ms"  # 执行耗时（同 TurnResult.latency_ms 口径）
ATTR_DATA_VERSION = "atlas.snapshot.data_version"  # 快照 sha（未绑定不写，不编造）
ATTR_BLOCK_REASON = "atlas.block_reason"  # Guard 拒绝原因
ATTR_ERROR = "atlas.error"  # 执行期故障摘要（截断防敏感泄漏）
# gen_ai 语义约定（OpenTelemetry gen_ai semantic conventions 草案键）
ATTR_GENAI_PROMPT_TOKENS = "gen_ai.usage.prompt_tokens"
ATTR_GENAI_COMPLETION_TOKENS = "gen_ai.usage.completion_tokens"
ATTR_GENAI_MODEL = "gen_ai.request.model"
ATTR_PROMPT_VERSION = "gen_ai.prompt.version"

_METRIC_COST = "gen_ai.token_cost"
_METRIC_TURNS = "atlas.turn.count"
_METRIC_LATENCY = "atlas.turn.latency"
_METRIC_BLOCKED = "atlas.turn.blocked"

_PROMPTS_VERSION_RE = re.compile(r"^version:\s*(\S+)", re.MULTILINE)
_PROMPTS_YAML = Path(__file__).resolve().parent.parent / "agent" / "prompts" / "generator_plan.yaml"
_PROMPT_VERSION: str | None = None
_PROMPT_VERSION_READ = False  # None 与未读区分（文件缺失/无 version 都缓存为 None）

_ERROR_ATTR_MAX = 500  # error 属性截断长度（异常串可能含连接细节）


def _resource() -> Any:
    """SDK Resource（固定 service.instance.id）。

    不固定的话每个 CLI 短进程都会带随机 UUID 实例——counter/直方图被拆成
    每条回合一个独立序列（值恒 1），rate() 恒 0、p95 恒 NaN，面板静默空图
    （2026-09-03 冒烟实测定位）。单机部署所有进程聚合为一个逻辑实例符合
    本部署形态；多副本部署应注入唯一 instance id。
    """
    from opentelemetry.sdk.resources import SERVICE_INSTANCE_ID, SERVICE_NAME, Resource

    return Resource.create(
        {
            SERVICE_NAME: "atlas-data-agent",
            SERVICE_INSTANCE_ID: "atlas-local-single-node",
        }
    )


def configure_otel(
    endpoint: str | None = None,
    *,
    tracer_provider: TracerProvider | None = None,
    metric_reader: InMemoryMetricReader | PeriodicExportingMetricReader | None = None,
    service_name: str = "atlas-data-agent",
) -> None:
    """（重）配置全局 Tracer/MeterProvider；幂等语义：以最后一次调用为准。

    参数
    ----
    endpoint        : OTLP HTTP 端点（如 http://localhost:4318），None 时回退
                      OTEL_EXPORTER_OTLP_ENDPOINT 环境变量；都没有则不导出
                      （no-op，零 I/O）。
    tracer_provider : 注入（测试用 InMemorySpanExporter 场景），None 自建。
    metric_reader   : 注入（测试用 InMemoryMetricReader 场景）；None 且无
                      endpoint 时不挂 reader（数据丢弃，零 I/O）。
    service_name    : 自建 provider 的 Resource 服务名（注入场景忽略）。

    说明：旧 provider 若存在会被 shutdown 后替换——生产只调一次；测试可重复
    调用换新 exporter。进程退出前（atexit）自动 shutdown 同步 flush 待导出
    数据——CLI 短进程（单回合即退出）不丢埋点。
    """
    global _TRACER_PROVIDER, _TRACER, _METER_PROVIDER, _METER
    global _COST, _TURNS, _LATENCY, _BLOCKED  # instrument 绑定旧 meter，必须一起重置

    otlp_endpoint = endpoint or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    with _LOCK:
        if _TRACER_PROVIDER is not None:
            _shutdown(_TRACER_PROVIDER)
        if _METER_PROVIDER is not None:
            _shutdown(_METER_PROVIDER)

        if tracer_provider is not None:
            tp = tracer_provider
        else:
            tp = TracerProvider(resource=_resource())
            if otlp_endpoint:
                from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

                tp.add_span_processor(
                    BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{otlp_endpoint}/v1/traces"))
                )
        _TRACER_PROVIDER = tp
        _TRACER = tp.get_tracer("atlas.agent")

        if metric_reader is not None:
            mp = MeterProvider(metric_readers=[metric_reader], resource=_resource())
        else:
            mp = MeterProvider(resource=_resource())
            if otlp_endpoint:
                from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
                    OTLPMetricExporter,
                )
                from opentelemetry.sdk.metrics.export import (
                    PeriodicExportingMetricReader as _Reader,  # noqa: PLC0415 - 局部导入
                )

                mp = MeterProvider(
                    metric_readers=[
                        _Reader(OTLPMetricExporter(endpoint=f"{otlp_endpoint}/v1/metrics"))
                    ],
                    resource=_resource(),
                )
        _METER_PROVIDER = mp
        _METER = mp.get_meter("atlas.agent")
        _COST = _TURNS = _LATENCY = _BLOCKED = None
        _register_atexit_flush()


def _register_atexit_flush() -> None:
    """进程退出前 shutdown 当前 provider（同步 flush；幂等，只注册一次）。

    周期导出线程在短进程里可能来不及发送（CLI 单回合 1-2s 即退出），
    而 shutdown 会同步 flush——观测数据不因进程生命周期而丢。
    """
    global _ATEXIT_FLUSH_REGISTERED
    if _ATEXIT_FLUSH_REGISTERED:
        return
    _ATEXIT_FLUSH_REGISTERED = True

    def _flush() -> None:
        # 引用当前（最后一次 configure 的）provider，重复 shutdown 幂等
        with suppress(Exception):
            if _TRACER_PROVIDER is not None:
                _TRACER_PROVIDER.shutdown()
        with suppress(Exception):
            if _METER_PROVIDER is not None:
                _METER_PROVIDER.shutdown()

    atexit.register(_flush)


def _shutdown(provider: TracerProvider | MeterProvider) -> None:
    """关闭旧 provider；失败静默（不影响重建主路径）。"""
    with suppress(Exception):
        provider.shutdown()


def _tracer() -> Tracer:
    if _TRACER is None:
        configure_otel()
    assert _TRACER is not None
    return _TRACER


def _meter() -> Meter:
    if _METER is None:
        configure_otel()
    assert _METER is not None
    return _METER


# --------------------------------------------------------------------------
# 指标 instrument（惰性注册）
# --------------------------------------------------------------------------
_COST: Counter | None = None
_TURNS: Counter | None = None
_LATENCY: Histogram | None = None
_BLOCKED: Counter | None = None


def _instruments() -> tuple[Counter, Counter, Histogram, Counter]:
    global _COST, _TURNS, _LATENCY, _BLOCKED
    m = _meter()
    if _COST is None:
        _COST = m.create_counter(
            _METRIC_COST,
            unit="{token}",
            description="按回合计量的 LLM token 消耗（单位 token；未接定价表不换算 USD，"
            "口径见模块 docstring——确定性链路 0 token 不产出数据点）",
        )
    if _TURNS is None:
        _TURNS = m.create_counter(_METRIC_TURNS, description="Data Agent 回合总量")
    if _LATENCY is None:
        _LATENCY = m.create_histogram(_METRIC_LATENCY, unit="ms", description="回合执行耗时")
    if _BLOCKED is None:
        _BLOCKED = m.create_counter(_METRIC_BLOCKED, description="Guard 拒绝回合数")
    return _COST, _TURNS, _LATENCY, _BLOCKED


def _prompt_version() -> str | None:
    """读 agent/prompts/generator_plan.yaml 的 version（仅 LLM 真正参与时使用）。

    读取失败/文件缺失 → None（不伪造版本号；确定性回合不引用该值）。
    读取结果缓存（提示词文件变更需重启进程，与 PromptOps 回归门禁不冲突）。
    """
    global _PROMPT_VERSION, _PROMPT_VERSION_READ
    if not _PROMPT_VERSION_READ:
        _PROMPT_VERSION_READ = True
        if _PROMPTS_YAML.is_file():
            m = _PROMPTS_VERSION_RE.search(_PROMPTS_YAML.read_text(encoding="utf-8"))
            if m:
                _PROMPT_VERSION = m.group(1)
    return _PROMPT_VERSION


# --------------------------------------------------------------------------
# 回合打点（graph.DataAgent.ask 每回合调用一次）
# --------------------------------------------------------------------------
def record_turn(result: TurnResult, *, snapshot_sha: str | None = None) -> None:
    """把一回合 TurnResult 记录为 atlas.turn span + 指标（失败不反噬主链路）。

    参数
    ----
    result      : 回合结果（含 kind/metric/sql/rows/latency/usage/block_reason…）。
    snapshot_sha: 评测快照 sha（绑定过才有；None 时回退 explanation.data_version，
                  两者皆无则不写版本属性——不编造）。

    抛异常：本函数吞掉一切自身异常（观测故障不得中断问数），
    仅首次失败告警一次 RuntimeWarning。
    """

    try:
        _record_turn_impl(result, snapshot_sha)
    except Exception as exc:  # noqa: BLE001 - 见 docstring：观测故障隔离
        global _WARNED_TELEMETRY_FAILURE
        if not _WARNED_TELEMETRY_FAILURE:
            _WARNED_TELEMETRY_FAILURE = True
            warnings.warn(
                f"[otel] 回合埋点失败（已隔离，不影响主链路）：{exc!r}",
                RuntimeWarning,
                stacklevel=2,
            )


def _record_turn_impl(result: TurnResult, snapshot_sha: str | None) -> None:
    attrs: dict[str, Any] = {
        ATTR_SESSION_ID: result.session_id,
        ATTR_QUESTION_ID: f"{result.session_id}#t{result.turns_in_session}",
        ATTR_KIND: result.kind,
        ATTR_ENGINE: result.engine,
    }
    if result.path is not None:
        attrs[ATTR_PATH] = result.path
    if result.metric is not None:
        attrs[ATTR_METRIC_ID] = result.metric
    if result.sql is not None:
        attrs[ATTR_SQL] = result.sql
    attrs[ATTR_ROWS] = result.row_count
    if result.latency_ms:
        attrs[ATTR_LATENCY_MS] = result.latency_ms
    if result.kind == "blocked" and result.block_reason:
        attrs[ATTR_BLOCK_REASON] = result.block_reason
    if result.kind == "error" and result.error:
        attrs[ATTR_ERROR] = result.error[:_ERROR_ATTR_MAX]

    sha = snapshot_sha
    if sha is None and result.explanation:
        dv = result.explanation.get("data_version")
        sha = dv if isinstance(dv, str) else None
    if sha:
        attrs[ATTR_DATA_VERSION] = sha

    usage = result.usage or {}
    prompt_tokens = int(usage.get("prompt_tokens", 0))
    completion_tokens = int(usage.get("completion_tokens", 0))
    used_llm = prompt_tokens > 0 or completion_tokens > 0
    if used_llm:
        # 只有 LLM 真参与才记录 gen_ai 属性（确定性回合不虚构模型/版本）
        attrs[ATTR_GENAI_PROMPT_TOKENS] = prompt_tokens
        attrs[ATTR_GENAI_COMPLETION_TOKENS] = completion_tokens
        if result.engine not in ("deterministic",):
            attrs[ATTR_GENAI_MODEL] = str(result.engine)
        pv = _prompt_version()
        if pv:
            attrs[ATTR_PROMPT_VERSION] = pv

    tracer = _tracer()
    span = tracer.start_span("atlas.turn", kind=SpanKind.SERVER, attributes=attrs)
    span.end()

    cost, turns, latency, blocked = _instruments()
    turns.add(1, {"atlas.turn.kind": result.kind, "atlas.engine": result.engine})
    if result.latency_ms:
        latency.record(float(result.latency_ms), {"atlas.turn.kind": result.kind})
    if used_llm:
        cost_attrs = {
            "atlas.engine": result.engine,
            "gen_ai.prompt.version": _prompt_version() or "unknown",
        }
        cost.add(prompt_tokens + completion_tokens, cost_attrs)
    if result.kind == "blocked":
        blocked.add(1, {"atlas.reason": str(result.block_reason or "unspecified")})


__all__ = [
    "configure_otel",
    "record_turn",
    "ATTR_QUESTION_ID",
    "ATTR_SESSION_ID",
    "ATTR_KIND",
    "ATTR_ENGINE",
    "ATTR_PATH",
    "ATTR_METRIC_ID",
    "ATTR_SQL",
    "ATTR_ROWS",
    "ATTR_LATENCY_MS",
    "ATTR_DATA_VERSION",
    "ATTR_BLOCK_REASON",
    "ATTR_ERROR",
    "ATTR_GENAI_PROMPT_TOKENS",
    "ATTR_GENAI_COMPLETION_TOKENS",
    "ATTR_GENAI_MODEL",
    "ATTR_PROMPT_VERSION",
]
