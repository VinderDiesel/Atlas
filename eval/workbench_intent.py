"""ADR-0031 D09/T10 意图评测：最小差异样本 → 规则/LLM 对照（Recall 与 Plan/澄清分开）。

链路（与 agent/intent 同预算，绑定不做二次检索）：
    样本 question → 意图声明（rule=人工标注 / llm=intent.yaml + 模型）
                  → normalize_intent（证据三重校验/固定时钟/检索预算）
                  → retrieve_intent（授权∧注册全目录、每表达 top-5、RRF → K=5）
                  → bind_intent（完整绑定；未绑定只能澄清）
                  → 对照 expected（candidates_include / reason_code / unresolved / plan）
                  → eval/reports/workbench-intent-<engine>-<sha>.json（排他创建）

评测口径（诚实基线）：
- rule 引擎：确定性链路对「人工标注意图」的执行数字，**不代表任何 LLM 能力**；
  llm 引擎输出意图 JSON 后走同一条确定性链路（同一预算），Recall 与 Plan/澄清分列。
- 未配置真实 LLM（OPENAI_API_KEY）→ 报告 status=blocked：不运行假引擎、不产效果数字；
  测试注入的 fake 补全只为验证解析路径，不进入评测报告。
- EX（SQL 执行）not_run：本评测不触碰数据源；token 实测自 usage，不做 USD 估算。
- 样本按来源族冻结 split（finance-manual-v1=test，仅评测不进训练）。

用法（从仓库根执行）：
    .venv/bin/python -m eval.workbench_intent --engine rule   # 规则对照（默认）
    .venv/bin/python -m eval.workbench_intent --engine llm    # 真实 LLM（未配置 → blocked）
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
import time
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

import jsonschema
import yaml
from pydantic import BaseModel, ConfigDict, Field

from agent.compiler import Plan, SemanticModel
from agent.intent.bind import DEFAULT_LIMIT, bind_intent
from agent.intent.contracts import (
    Ambiguity,
    EvidenceSpan,
    FilterOp,
    IntentFilter,
    Mention,
    QuestionContext,
    SemanticIntent,
    SortDirection,
    SortSpec,
    Task,
)
from agent.intent.normalize import MAX_RETRIEVAL_EXPANSIONS, normalize_intent
from agent.intent.retrieve import (
    FINAL_K,
    MAX_CANDIDATES_PER_QUERY,
    IntentRetriever,
    retrieve_intent,
)
from agent.runtime.bundle import (
    RuntimeBundle,
    build_manifest,
    load_bundle,
    persist_bundle,
    runtime_code_sha,
)
from data.identity import REPO_ROOT, git_short_sha

GOLD_DIR = Path(__file__).resolve().parent / "gold" / "intent"
SCHEMA_FILE = GOLD_DIR / "schema.json"
PROMPT_FILE = REPO_ROOT / "agent" / "prompts" / "intent.yaml"
REPORT_DIR = Path(__file__).resolve().parent / "reports"
MODEL_PATH = "semantic/ossie/atlas_finance.ossie.yaml"

ENGINE_RULE = "rule"
ENGINE_LLM = "llm"
_DEFAULT_MODEL = "gpt-4o-mini"

# 制品装配文件（与 tests/workbench_support.bundle_files 同口径：HEAD 字节为准）。
BUNDLE_PATHS = (
    "semantic/ossie/atlas_finance.ossie.yaml",
    "semantic/synonyms/zh_cn.yml",
    "semantic/synonyms/en_us.yml",
    "semantic/synonyms/patterns_zh_cn.yml",
    "semantic/synonyms/patterns_en_us.yml",
    "semantic/policies/row_policy.yml",
    "agent/prompts/generator_plan.yaml",
    "agent/flows/templates/query.json",
    "agent/flows/templates/analysis.json",
)

DEFAULT_CATALOG_SUMMARY = (
    "注册指标覆盖交易额、佣金、持仓、账户、笔数等概念域（具体指标名由系统检索确定）。"
)

RULE_NOTES = (
    "engine=rule：样本标注（人工意图声明）→ normalize→retrieve→bind 确定性链路；"
    "数字代表系统对意图的执行，不代表任何 LLM 能力。",
    "Recall 与 Plan/澄清 Acc 分开报告（不与 EX 混合）；EX（SQL 执行）not_run：本评测不触碰数据源。",
    "绑定与检索同预算：K=5、每表达 top-5 经 RRF、原话+扩展≤2、default_limit=100。",
)
LLM_NOTES = (
    "engine=llm：真实模型输出意图 JSON（agent/prompts/intent.yaml）→ 同一确定性链路；"
    "token 实测自 usage（不做 USD 估算，避免伪精确）。",
    "Recall 与 Plan/澄清 Acc 分开报告（不与 EX 混合）；EX（SQL 执行）not_run：本评测不触碰数据源。",
    "绑定与检索同预算：K=5、每表达 top-5 经 RRF、原话+扩展≤2、default_limit=100。",
)
BLOCKED_NOTES = (
    "engine=llm 未配置 OPENAI_API_KEY（.env）：按 D09/T10 blocked——不运行假引擎、不产效果数字。",
    "样本已冻结（10 条 / 5 对 / split=test）；配置密钥后复跑："
    "python -m eval.workbench_intent --engine llm",
)

# LLM 输出的受限词表（与 agent/intent/contracts.py 一致）。
_LLM_TASKS = ("query", "compare", "attribution", "clarify", "unsupported")
_LLM_OPS = ("eq", "neq", "in", "not_in", "gt", "gte", "lt", "lte")

ChatFn = Callable[[str, str, str], tuple[str, dict[str, int]]]


class IntentSampleError(ValueError):
    """样本/提示词/LLM 输出不满足契约：fail-closed，不猜测。"""


class _Frozen(BaseModel):
    """报告与样本模型：未知字段拒绝（extra=forbid），实例不可变。"""

    model_config = ConfigDict(extra="forbid", frozen=True)


class AnnotationFilter(_Frozen):
    subject: str = Field(min_length=1)
    op: FilterOp
    values: tuple[str, ...] = Field(min_length=1)


class Annotation(_Frozen):
    metric_mentions: tuple[str, ...] = ()
    groups: tuple[str, ...] = ()
    filters: tuple[AnnotationFilter, ...] = ()
    time_mentions: tuple[str, ...] = ()


class Expected(_Frozen):
    candidates_include: tuple[str, ...] = ()
    reason_code: str = Field(min_length=1)
    unresolved_slots: tuple[str, ...] = ()
    plan: dict[str, Any] | None = None


class IntentSample(_Frozen):
    id: str = Field(min_length=1)
    pair_id: str = Field(min_length=1)
    split: str = Field(min_length=1)
    source_family: str = Field(min_length=1)
    question: str = Field(min_length=1)
    reference_time: datetime
    timezone: str = Field(min_length=1)
    annotation: Annotation
    expected: Expected


class IntentSampleResult(_Frozen):
    id: str
    question: str
    candidates: tuple[str, ...] = ()
    reason_code: str = ""
    unresolved_slots: tuple[str, ...] = ()
    plan: dict[str, Any] | None = None
    recall_ok: bool = False
    plan_ok: bool | None = None
    clarify_ok: bool | None = None
    latency_ms: float = 0.0
    usage: dict[str, int] = Field(default_factory=dict)
    error: str | None = None


class IntentSummary(_Frozen):
    total: int
    retrieval_recall: str
    plan_accuracy: str
    clarify_accuracy: str
    total_tokens: int


class IntentBudget(_Frozen):
    final_k: int
    max_candidates_per_query: int
    max_retrieval_expansions: int
    bind_same_budget: Literal[True] = True
    default_limit: int


class IntentReport(_Frozen):
    """报告：blocked 时 summary/samples 为空——禁止承载未运行的效果数字。"""

    schema_version: Literal[1] = 1
    report_type: Literal["workbench_intent"] = "workbench_intent"
    created_at: datetime
    code_sha: str
    engine: Literal["rule", "llm"]
    status: Literal["ok", "blocked"]
    prompt_version: str | None = None
    samples_digest: str
    catalog_digest: str | None = None
    budget: IntentBudget
    summary: IntentSummary | None = None
    samples: tuple[IntentSampleResult, ...] = ()
    notes: tuple[str, ...] = ()


# --- 样本加载 -----------------------------------------------------------------


def load_samples(gold_dir: Path = GOLD_DIR) -> tuple[IntentSample, ...]:
    """读取并逐条校验意图样本（schema.json 为权威；id 与文件名必须一致）。"""
    schema = json.loads(SCHEMA_FILE.read_text(encoding="utf-8"))
    samples: list[IntentSample] = []
    for path in sorted(gold_dir.glob("intent-*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise IntentSampleError(f"{path.name}: JSON 非法：{exc}") from exc
        try:
            jsonschema.validate(raw, schema)
        except jsonschema.ValidationError as exc:
            raise IntentSampleError(f"{path.name}: schema 校验失败：{exc.message}") from exc
        if raw.get("id") != path.stem:
            raise IntentSampleError(f"{path.name}: id 与文件名不一致（{raw.get('id')!r}）")
        samples.append(IntentSample.model_validate(raw))
    if not samples:
        raise IntentSampleError(f"{gold_dir} 下没有 intent-*.json 样本")
    return tuple(samples)


def _samples_digest(gold_dir: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(gold_dir.glob("intent-*.json")):
        digest.update(path.name.encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


# --- 意图声明（rule / llm） ----------------------------------------------------


def _mention(question: str, text: str) -> Mention:
    """逐字定位（唯一）：找不到或出现多次即拒绝（幻觉/歧义不猜）。"""
    start = question.find(text)
    if start < 0 or question.find(text, start + 1) != -1:
        raise IntentSampleError(f"提及 {text!r} 必须逐字且唯一出现在问句中")
    return Mention(
        text=text,
        evidence=(EvidenceSpan(source_id="question", start=start, end=start + len(text)),),
    )


def intent_from_annotation(sample: IntentSample) -> SemanticIntent:
    """rule 引擎：人工标注（模拟的模型输出）→ SemanticIntent（证据逐字定位）。"""
    question = sample.question
    annotation = sample.annotation
    return SemanticIntent(
        schema_version=1,
        task="query",
        metric_mentions=tuple(_mention(question, t) for t in annotation.metric_mentions),
        groups=tuple(_mention(question, t) for t in annotation.groups),
        filters=tuple(
            IntentFilter(
                subject=_mention(question, f.subject),
                op=f.op,
                values=tuple(_mention(question, v) for v in f.values),
            )
            for f in annotation.filters
        ),
        time_mentions=tuple(_mention(question, t) for t in annotation.time_mentions),
    )


def render_prompt(
    sample: IntentSample,
    *,
    prompt_path: Path = PROMPT_FILE,
    catalog_summary: str = DEFAULT_CATALOG_SUMMARY,
) -> tuple[str, str]:
    """渲染 intent.yaml 模板（system, user）；缺文件/缺字段直接报错。"""
    doc = yaml.safe_load(prompt_path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict):
        raise IntentSampleError(f"提示词文件形态非法：{prompt_path}")
    system = str(doc["system_template"])
    user = (
        str(doc["user_template"])
        .replace("__REFERENCE_TIME__", sample.reference_time.isoformat())
        .replace("__TIMEZONE__", sample.timezone)
        .replace("__CATALOG_SUMMARY__", catalog_summary)
        .replace("__QUESTION__", sample.question)
    )
    return system, user


def _texts(obj: dict[str, Any], key: str) -> tuple[str, ...]:
    value = obj.get(key)
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise IntentSampleError(f"{key} 必须是非空字符串数组")
    return tuple(str(v) for v in value)


def _items(obj: dict[str, Any], key: str) -> tuple[Any, ...]:
    value = obj.get(key)
    if value is None:
        return ()
    if not isinstance(value, list):
        raise IntentSampleError(f"{key} 必须是数组")
    return tuple(value)


def _filter_from_llm(item: Any, question: str) -> IntentFilter:
    if not isinstance(item, dict):
        raise IntentSampleError("filters 元素必须是对象")
    subject = item.get("subject")
    op = item.get("op")
    values = item.get("values")
    if not isinstance(subject, str) or not subject:
        raise IntentSampleError("filters.subject 必须是非空字符串")
    if not isinstance(op, str) or op not in _LLM_OPS:
        raise IntentSampleError(f"filters.op 非法：{op!r}")
    if not isinstance(values, list) or not all(isinstance(v, str) and v for v in values):
        raise IntentSampleError("filters.values 必须是非空字符串数组")
    return IntentFilter(
        subject=_mention(question, subject),
        op=cast(FilterOp, op),
        values=tuple(_mention(question, v) for v in values),
    )


def _sort_from_llm(item: Any, question: str) -> SortSpec | None:
    if item is None:
        return None
    if not isinstance(item, dict):
        raise IntentSampleError("sort 必须是对象或 null")
    direction = item.get("direction")
    by = item.get("by")
    if not isinstance(direction, str) or direction not in ("asc", "desc"):
        raise IntentSampleError(f"sort.direction 非法：{direction!r}")
    if not isinstance(by, str) or not by:
        raise IntentSampleError("sort.by 必须是非空字符串")
    return SortSpec(direction=cast(SortDirection, direction), by=_mention(question, by))


def _limit_from_llm(value: Any) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise IntentSampleError(f"limit 必须是正整数或 null：{value!r}")
    return value


def _ambiguity_evidence(
    slot: str,
    question: str,
    metric: tuple[Mention, ...],
    groups: tuple[Mention, ...],
    filters: tuple[IntentFilter, ...],
    times: tuple[Mention, ...],
) -> tuple[EvidenceSpan, ...]:
    if slot.startswith("metric"):
        spans = tuple(span for m in metric for span in m.evidence)
    elif slot.startswith("time"):
        spans = tuple(span for m in times for span in m.evidence)
    elif slot.startswith("groups"):
        spans = tuple(span for m in groups for span in m.evidence)
    elif slot.startswith("filters"):
        spans = tuple(span for f in filters for m in (f.subject, *f.values) for span in m.evidence)
    else:
        spans = ()
    if not spans:
        # 声明了歧义但无关联提及：证据落在整句（不伪造片段）。
        return (EvidenceSpan(source_id="question", start=0, end=len(question)),)
    return spans


def _ambiguity_from_llm(
    item: Any,
    question: str,
    metric: tuple[Mention, ...],
    groups: tuple[Mention, ...],
    filters: tuple[IntentFilter, ...],
    times: tuple[Mention, ...],
) -> Ambiguity:
    if not isinstance(item, dict):
        raise IntentSampleError("ambiguities 元素必须是对象")
    slot = item.get("slot")
    reason = item.get("reason_code")
    if not isinstance(slot, str) or not slot:
        raise IntentSampleError("ambiguities.slot 必须是非空字符串")
    if not isinstance(reason, str) or not reason:
        raise IntentSampleError("ambiguities.reason_code 必须是非空字符串")
    return Ambiguity(
        slot=slot,
        reason_code=reason,
        evidence=_ambiguity_evidence(slot, question, metric, groups, filters, times),
    )


def intent_from_llm(raw: str, question: str) -> SemanticIntent:
    """LLM 输出 → 语义意图：mention 逐字定位（唯一）；任何不满足即拒绝（fail-closed）。"""
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise IntentSampleError(f"输出非合法 JSON：{exc}") from exc
    if not isinstance(obj, dict):
        raise IntentSampleError("输出不是 JSON 对象")
    if obj.get("schema_version") != 1:
        raise IntentSampleError(f"schema_version 必须为 1：{obj.get('schema_version')!r}")
    task_raw = obj.get("task", "query")
    if not isinstance(task_raw, str) or task_raw not in _LLM_TASKS:
        raise IntentSampleError(f"未知 task：{task_raw!r}")
    metric = tuple(_mention(question, t) for t in _texts(obj, "metric_mentions"))
    groups = tuple(_mention(question, t) for t in _texts(obj, "groups"))
    times = tuple(_mention(question, t) for t in _texts(obj, "time_mentions"))
    filters = tuple(_filter_from_llm(item, question) for item in _items(obj, "filters"))
    ambiguities = tuple(
        _ambiguity_from_llm(item, question, metric, groups, filters, times)
        for item in _items(obj, "ambiguities")
    )
    return SemanticIntent(
        schema_version=1,
        task=cast(Task, task_raw),
        metric_mentions=metric,
        groups=groups,
        filters=filters,
        time_mentions=times,
        sort=_sort_from_llm(obj.get("sort"), question),
        limit=_limit_from_llm(obj.get("limit")),
        ambiguities=ambiguities,
    )


def llm_intent(
    sample: IntentSample,
    *,
    chat: ChatFn,
    model_name: str | None = None,
    prompt_path: Path = PROMPT_FILE,
    catalog_summary: str = DEFAULT_CATALOG_SUMMARY,
) -> tuple[SemanticIntent, dict[str, int]]:
    system, user = render_prompt(sample, prompt_path=prompt_path, catalog_summary=catalog_summary)
    model = model_name or os.environ.get("OPENAI_MODEL_NAME") or _DEFAULT_MODEL
    raw, usage = chat(system, user, model)
    return intent_from_llm(raw, sample.question), usage


def _rule_intent(sample: IntentSample) -> tuple[SemanticIntent, dict[str, int]]:
    return intent_from_annotation(sample), {}


def _openai_chat() -> ChatFn:
    """OpenAI 兼容 chat/completions（与 agent/generator.py 同可移植子集）。"""
    base = os.environ.get("OPENAI_BASE_URL", "").rstrip("/") or "https://api.openai.com/v1"
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()

    def chat(system: str, user: str, model: str) -> tuple[str, dict[str, int]]:
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.0,
            "max_tokens": 800,
        }
        headers = {"Content-Type": "application/json"}
        if api_key:  # N9：密钥只走请求头，绝不入 query
            headers["Authorization"] = f"Bearer {api_key}"
        request = urllib.request.Request(
            f"{base}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310 - 受管端点
            body: dict[str, Any] = json.loads(response.read().decode("utf-8"))
        content = str(body["choices"][0]["message"]["content"])
        usage = body.get("usage", {})
        return content, {
            "prompt_tokens": int(usage.get("prompt_tokens", 0)),
            "completion_tokens": int(usage.get("completion_tokens", 0)),
        }

    return chat


def _prompt_version(prompt_path: Path = PROMPT_FILE) -> str | None:
    try:
        doc = yaml.safe_load(prompt_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(doc, dict) or doc.get("version") is None:
        return None
    return str(doc["version"])


# --- 制品装配 -----------------------------------------------------------------


def _source_bytes(path: str) -> bytes:
    committed = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "show", f"HEAD:{path}"],
        capture_output=True,
        check=False,
    )
    if committed.returncode == 0:
        return committed.stdout
    return (REPO_ROOT / path).read_bytes()


def assemble_bundle() -> tuple[RuntimeBundle, str]:
    """从 HEAD 字节装配运行时制品；返回 (bundle, 语义模型 sha256)。"""
    files = {path: _source_bytes(path) for path in BUNDLE_PATHS}
    manifest = build_manifest(
        files,
        source_git_sha=runtime_code_sha(),
        runtime_code_sha=runtime_code_sha(),
        source_revision="workbench-intent-eval",
        eval_evidence_ids=(),
    )
    root = Path(tempfile.mkdtemp(prefix="workbench-intent-bundle-"))
    persist_bundle(root, files, manifest)
    bundle = load_bundle(root, manifest.release_id)
    return bundle, hashlib.sha256(files[MODEL_PATH]).hexdigest()


def _registry_summary(model: SemanticModel) -> str:
    return (
        f"共 {len(model.metrics)} 个注册指标（概念域：交易额、佣金、持仓、账户、笔数与派生比率）。"
        "指标名由系统检索与绑定确定——你的输出禁止包含指标名/列名，只允许问句原话。"
    )


# --- 评测与报告 ---------------------------------------------------------------


def project_plan(plan: Plan) -> dict[str, Any]:
    """Plan → schema.json 的 plan 投影（与样本 expected.plan 同结构）。"""
    return {
        "metric": plan.metric,
        "dimensions": list(plan.dimensions),
        "time": None
        if plan.time is None
        else {"granularity": plan.time.granularity, "value": plan.time.value},
        "filters": [{"column": f.column, "op": f.op, "value": f.value} for f in plan.filters],
        "order_by": [{"column": o.column, "desc": o.desc} for o in plan.order_by],
        "limit": plan.limit,
    }


def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 1)


def _failure(
    sample: IntentSample,
    *,
    latency_ms: float,
    usage: dict[str, int],
    error: str,
) -> IntentSampleResult:
    bound = sample.expected.reason_code == "bound"
    return IntentSampleResult(
        id=sample.id,
        question=sample.question,
        plan_ok=False if bound else None,
        clarify_ok=None if bound else False,
        latency_ms=latency_ms,
        usage=usage,
        error=error,
    )


def evaluate_sample(
    sample: IntentSample,
    *,
    bundle: RuntimeBundle,
    retriever: IntentRetriever,
    catalog_digest: str,
    make_intent: Callable[[IntentSample], tuple[SemanticIntent, dict[str, int]]],
) -> IntentSampleResult:
    """单样本全链路：意图声明 → normalize → retrieve → bind → 对照 expected。"""
    started = time.perf_counter()
    usage: dict[str, int] = {}
    try:
        intent, usage = make_intent(sample)
    except Exception as exc:  # noqa: BLE001 - 逐样本记录失败，不中断整轮
        return _failure(
            sample, latency_ms=_ms(started), usage=usage, error=f"{type(exc).__name__}: {exc}"
        )
    context = QuestionContext(
        question=sample.question,
        locale="zh_cn",
        catalog_digest=catalog_digest,
        reference_time=sample.reference_time,
        timezone=sample.timezone,
    )
    try:
        normalized = normalize_intent(intent, context)
        candidates = retrieve_intent(normalized, context, retriever=retriever)
        binding = bind_intent(normalized, candidates, bundle)
    except Exception as exc:  # noqa: BLE001 - 同上：失败如实记录
        return _failure(
            sample, latency_ms=_ms(started), usage=usage, error=f"{type(exc).__name__}: {exc}"
        )
    actual = [c.semantic_id for c in candidates.candidates]
    plan = None if binding.plan_candidate is None else project_plan(binding.plan_candidate)
    expected = sample.expected
    recall_ok = all(m in actual for m in expected.candidates_include)
    plan_ok: bool | None = None
    clarify_ok: bool | None = None
    if expected.reason_code == "bound":
        plan_ok = binding.reason_code == "bound" and plan == expected.plan
    else:
        clarify_ok = (
            binding.reason_code == expected.reason_code
            and sorted(binding.unresolved_slots) == sorted(expected.unresolved_slots)
            and plan is None
        )
    return IntentSampleResult(
        id=sample.id,
        question=sample.question,
        candidates=tuple(actual),
        reason_code=binding.reason_code,
        unresolved_slots=binding.unresolved_slots,
        plan=plan,
        recall_ok=recall_ok,
        plan_ok=plan_ok,
        clarify_ok=clarify_ok,
        latency_ms=_ms(started),
        usage=usage,
    )


def _summarize(results: tuple[IntentSampleResult, ...]) -> IntentSummary:
    bound = [r for r in results if r.plan_ok is not None]
    clarify = [r for r in results if r.clarify_ok is not None]
    recall = sum(1 for r in results if r.recall_ok)
    plan_ok = sum(1 for r in bound if r.plan_ok)
    clarify_ok = sum(1 for r in clarify if r.clarify_ok)
    tokens = sum(
        int(r.usage.get("prompt_tokens", 0)) + int(r.usage.get("completion_tokens", 0))
        for r in results
    )
    return IntentSummary(
        total=len(results),
        retrieval_recall=f"{recall}/{len(results)}",
        plan_accuracy=f"{plan_ok}/{len(bound)}",
        clarify_accuracy=f"{clarify_ok}/{len(clarify)}",
        total_tokens=tokens,
    )


def write_report(report: IntentReport, output: Path) -> None:
    """排他创建报告 JSON（既有文件绝不覆盖）。"""
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as handle:
        handle.write(report.model_dump_json(indent=2) + "\n")


def run(
    engine: str = ENGINE_RULE,
    *,
    gold_dir: Path = GOLD_DIR,
    chat: ChatFn | None = None,
    model_name: str | None = None,
    output: Path | None = None,
) -> IntentReport:
    """执行评测：加载样本 → 引擎执行 → 对照 → 报告（output 给定时排他写入）。"""
    if engine not in (ENGINE_RULE, ENGINE_LLM):
        raise ValueError(f"未知引擎：{engine!r}（支持 {ENGINE_RULE} / {ENGINE_LLM}）")
    if output is not None and (output.exists() or output.is_symlink()):
        raise FileExistsError(output)
    samples = load_samples(gold_dir)
    budget = IntentBudget(
        final_k=FINAL_K,
        max_candidates_per_query=MAX_CANDIDATES_PER_QUERY,
        max_retrieval_expansions=MAX_RETRIEVAL_EXPANSIONS,
        default_limit=DEFAULT_LIMIT,
    )
    base: dict[str, Any] = {
        "created_at": datetime.now(UTC),
        "code_sha": git_short_sha(),
        "engine": engine,
        "prompt_version": _prompt_version(),
        "samples_digest": _samples_digest(gold_dir),
        "budget": budget,
    }
    if engine == ENGINE_LLM and chat is None and not os.environ.get("OPENAI_API_KEY", "").strip():
        report = IntentReport(**base, status="blocked", notes=BLOCKED_NOTES)
        if output is not None:
            write_report(report, output)
        return report

    bundle, catalog_digest = assemble_bundle()
    model = bundle.semantic_model(MODEL_PATH)
    retriever = IntentRetriever(model)
    if engine == ENGINE_LLM:
        chat_fn = chat if chat is not None else _openai_chat()
        summary_text = _registry_summary(model)

        def make_intent(sample: IntentSample) -> tuple[SemanticIntent, dict[str, int]]:
            return llm_intent(
                sample, chat=chat_fn, model_name=model_name, catalog_summary=summary_text
            )

        notes = LLM_NOTES
    else:
        make_intent = _rule_intent
        notes = RULE_NOTES
    results = tuple(
        evaluate_sample(
            sample,
            bundle=bundle,
            retriever=retriever,
            catalog_digest=catalog_digest,
            make_intent=make_intent,
        )
        for sample in samples
    )
    report = IntentReport(
        **base,
        status="ok",
        catalog_digest=catalog_digest,
        summary=_summarize(results),
        samples=results,
        notes=notes,
    )
    if output is not None:
        write_report(report, output)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", default=ENGINE_RULE, choices=[ENGINE_RULE, ENGINE_LLM])
    parser.add_argument(
        "--model", default=None, help="LLM 模型名（缺省 OPENAI_MODEL_NAME / gpt-4o-mini）"
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="报告路径（默认 eval/reports/workbench-intent-<engine>-<sha>.json）",
    )
    args = parser.parse_args(argv)
    from dotenv import load_dotenv  # CLI 才读 .env；测试注入不经此路径

    load_dotenv()
    output = args.output or REPORT_DIR / f"workbench-intent-{args.engine}-{git_short_sha()}.json"
    try:
        report = run(args.engine, model_name=args.model, output=output)
    except FileExistsError:
        print(f"报告已存在，拒绝覆盖：{output}")
        return 2
    print(f"[{report.status}] 报告已写入：{output}")
    if report.summary is not None:
        print(json.dumps(report.summary.model_dump(), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
