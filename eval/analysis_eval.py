"""T09 独立 analysis 评测器（ADR-0026）：最后一个不被实现自证过关的关口。

职责（task-9-brief）：
- 用样本里人工书写的期望与参考结果，独立核对 agent 产出的计划、步骤、综合与拒答，
  **绝不调用 agent.synthesize 当 oracle**——贡献率/总量由本模块按加法分解口径独立重算；
- 单项失败独立计数（按 check 列分列），绝不拼装 composite「accuracy」；
- 真链模式必须显式提供 --snapshot-sha / 语义 sha，运行前后都复核快照数据指纹
  （meta vs 现场测量），源数据变化即拒绝出报告；绝不默认 HEAD；
- --dry 只做结构/计划校验：draft 可跑，执行项一律标 not_run，绝不写报告文件，
  绝不可用于放行；
- 评测器绝不回填/篡改样本期望（R3：真链数值期望由控制器人工核对后填写）。

诚实红线：缺期望、draft、空样本集、必测 skip、快照不符都会失败（禁止空跑绿灯）。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Callable
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from pathlib import Path
from typing import Final, NamedTuple, Protocol, TypeAlias, cast

import jsonschema

from agent.analysis import (
    _NULL_DIM_LABEL,
    ANALYSIS_ROLES,
    PENDING,
    AnalysisPlan,
    AnalysisResult,
    analysis_status,
    effective_reason_code,
    plan_projection,
    validate_analysis_plan,
    validate_analysis_result,
)
from agent.compiler import Filter, OrderSpec, Plan, SemanticModel, TimeSpec
from data.identity import SNAPSHOT_DIR, git_short_sha_or_none
from eval.runner import DOMAIN_MODELS, REPO_ROOT, REPORT_DIR, TZ

EXPECTED_INTENT: Final[str] = "change_contribution"
"""ADR-0026 固定分析意图（agent.analysis 的 _ANALYSIS_INTENT_ID 不导出，此处独立钉死）。"""

PCT_QUANT: Final[Decimal] = Decimal("0.000001")
ZERO: Final[Decimal] = Decimal(0)

FINGERPRINT_KEYS: Final[tuple[str, ...]] = (
    "data_range",
    "raw_size_bytes",
    "row_counts",
    "snapshot_ids",
)
"""参与漂移比对的数据指纹键（不含 sha：HEAD 前进而数据未变是合法情形）。"""

STRUCTURAL_CHECKS: Final[tuple[str, ...]] = (
    "sample_schema",
    "id_matches_filename",
    "maturity_ready",
    "shas_declared",
    "snapshot_match",
    "semantic_match",
    "plan_template",
    "reference_complete",
)

RESULT_CHECKS: Final[tuple[str, ...]] = (
    "executed",
    "contract",
    "kind",
    "sql_calls",
    "plan",
    "sub_plans",
    "steps_columns",
    "steps_rows",
    "attribution_totals",
    "attribution_items",
    "reason_code",
    "clarification",
    "bindings",
)

_KIND_BY_STATUS: Final[dict[str, str]] = {
    "ok": "answer",
    "unavailable": "unavailable",
    "clarify": "clarify",
    "blocked": "blocked",
    "error": "error",
}

Check: TypeAlias = tuple[str, str, "str | None"]
"""单个检查项：（check 名，状态 pass/fail/skip/not_run，失败详情）。"""

_SKIPPED_DETAIL: Final[str] = "结构门未通过，不执行（缺期望/不合格样本绝不进真链）"

with Path(__file__).parent.joinpath("analysis", "schema.json").open(encoding="utf-8") as _fh:
    _SCHEMA: Final[dict[str, object]] = json.load(_fh)


class SnapshotVerifyError(RuntimeError):
    """快照数据指纹与 meta 不符（运行前或运行后），评测结果无效。"""


class AnalysisAgent(Protocol):
    """被测 agent 的最小结构接口（DataAgent.analyze 结构兼容）。"""

    def analyze(self, question: str) -> object: ...


# ---------------------------------------------------------------------------
# 快照指纹复核（T09 自实现任意 sha 校验；data/snapshot.py --check 只认 HEAD，不能复用）
# ---------------------------------------------------------------------------


def _measure_fingerprint() -> dict[str, object]:
    """现场测量快照数据指纹（懒加载：data.snapshot 拖带 pyarrow/pyiceberg）。"""
    from data.snapshot import build_meta, fingerprint

    return dict(fingerprint(build_meta()))


def verify_snapshot_fingerprint(
    sha: str,
    *,
    snapshots_dir: Path | None = None,
    measure: Callable[[], dict[str, object]] | None = None,
) -> tuple[bool, str | None]:
    """把指定快照的 meta 与现场测量指纹逐键比对。

    参数 sha：快照短 sha（data/snapshots/<sha>.meta.json）。
    参数 snapshots_dir：覆盖快照目录（默认 data/snapshots）。
    参数 measure：注入的现场测量（默认调 data.snapshot.fingerprint(build_meta())）。
    返回 (是否一致, 不一致详情)；一致时详情为 None。
    """
    base = Path(snapshots_dir) if snapshots_dir is not None else SNAPSHOT_DIR
    meta_path = base / f"{sha}.meta.json"
    if not meta_path.is_file():
        return False, f"快照 meta 不存在：{meta_path}"
    try:
        raw = meta_path.read_text(encoding="utf-8")
    except OSError as exc:
        return False, f"快照 meta 读取失败：{exc!r}"
    try:
        meta = json.loads(raw)
    except json.JSONDecodeError as exc:
        return False, f"快照 meta 不是合法 JSON：{exc}"
    if not isinstance(meta, dict):
        return False, f"快照 meta 必须为 JSON 对象，实际 {type(meta).__name__}"
    if meta.get("sha") != sha:
        return False, f"meta.sha 与请求不符：meta={meta.get('sha')!r} ≠ 请求 {sha!r}"
    measure_fn = measure if measure is not None else _measure_fingerprint
    try:
        measured = measure_fn()
    except Exception as exc:  # 测量层任何异常都按不一致处理（绝不静默放行）
        return False, f"快照现场测量失败：{exc!r}"
    if not isinstance(measured, dict):
        return False, f"现场测量结果必须为 dict，实际 {type(measured).__name__}"
    diffs: list[str] = []
    for key in FINGERPRINT_KEYS:
        if key not in meta:
            diffs.append(f"{key}: meta 缺失该键")
        elif key not in measured:
            diffs.append(f"{key}: 现场测量缺失该键")
        elif meta[key] != measured[key]:
            diffs.append(f"{key}: meta={meta[key]!r} ≠ 实测={measured[key]!r}")
    if diffs:
        return False, "快照指纹与 meta 不符：" + "；".join(diffs)
    return True, None


# ---------------------------------------------------------------------------
# JSON → 契约对象（只做结构解析；语义校验交给 validate_analysis_plan）
# ---------------------------------------------------------------------------


def _time_spec_from_json(data: object, label: str) -> TimeSpec:
    """解析 TimeSpec JSON（granularity + string/int value，布尔不算整数）。"""
    if not isinstance(data, dict):
        raise ValueError(f"{label} 必须为对象，实际 {type(data).__name__}")
    granularity = data.get("granularity")
    value = data.get("value")
    if not isinstance(granularity, str) or not granularity:
        raise ValueError(f"{label}.granularity 必须为非空字符串")
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(f"{label}.value 必须为字符串或整数")
    return TimeSpec(granularity=granularity, value=value)


def _filters_from_json(data: object, label: str) -> tuple[Filter, ...]:
    """解析 Filter JSON 列表。"""
    if not isinstance(data, list):
        raise ValueError(f"{label} 必须为数组，实际 {type(data).__name__}")
    filters: list[Filter] = []
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            raise ValueError(f"{label}[{i}] 必须为对象")
        column = item.get("column")
        op = item.get("op")
        if not isinstance(column, str) or not column or not isinstance(op, str) or not op:
            raise ValueError(f"{label}[{i}].column/op 必须为非空字符串")
        filters.append(Filter(column=column, op=op, value=item.get("value")))
    return tuple(filters)


def _order_by_from_json(data: object, label: str) -> tuple[OrderSpec, ...]:
    """解析 OrderSpec JSON 列表。"""
    if not isinstance(data, list):
        raise ValueError(f"{label} 必须为数组，实际 {type(data).__name__}")
    specs: list[OrderSpec] = []
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            raise ValueError(f"{label}[{i}] 必须为对象")
        column = item.get("column")
        if not isinstance(column, str) or not column:
            raise ValueError(f"{label}[{i}].column 必须为非空字符串")
        desc = item.get("desc", False)
        if not isinstance(desc, bool):
            raise ValueError(f"{label}[{i}].desc 必须为布尔")
        specs.append(OrderSpec(column=column, desc=desc))
    return tuple(specs)


def _limit_from_json(data: object, label: str) -> int:
    """解析正整数 limit（布尔不算整数）。"""
    if isinstance(data, bool) or not isinstance(data, int) or data < 1:
        raise ValueError(f"{label} 必须为正整数，实际 {data!r}")
    return data


def _sub_plan_from_json(data: object, label: str) -> Plan:
    """解析单个子计划 JSON（canonical Plan 投影 + role，role 只作标注不参与构造）。"""
    if not isinstance(data, dict):
        raise ValueError(f"{label} 必须为对象，实际 {type(data).__name__}")
    metric = data.get("metric")
    if not isinstance(metric, str) or not metric:
        raise ValueError(f"{label}.metric 必须为非空字符串")
    dimensions = data.get("dimensions", [])
    if not isinstance(dimensions, list) or not all(isinstance(d, str) and d for d in dimensions):
        raise ValueError(f"{label}.dimensions 必须为非空字符串列表")
    time_raw = data.get("time")
    time = None if time_raw is None else _time_spec_from_json(time_raw, f"{label}.time")
    comparison = data.get("comparison")
    if comparison is not None:
        raise ValueError(f"{label}.comparison 必须为 null（分析比较用普通 Plan）")
    return Plan(
        metric=metric,
        dimensions=tuple(dimensions),
        time=time,
        filters=_filters_from_json(data.get("filters", []), f"{label}.filters"),
        order_by=_order_by_from_json(data.get("order_by", []), f"{label}.order_by"),
        limit=_limit_from_json(data.get("limit", 100), f"{label}.limit"),
    )


def _plan_from_json(plan_json: dict[str, object], subs_json: list[object]) -> AnalysisPlan:
    """解析期望 AnalysisPlan（plan 投影 + 四步子计划）。"""
    intent = plan_json.get("intent")
    metric = plan_json.get("metric")
    if not isinstance(intent, str) or not intent:
        raise ValueError("expected_plan.intent 必须为非空字符串")
    if not isinstance(metric, str) or not metric:
        raise ValueError("expected_plan.metric 必须为非空字符串")
    dimension = plan_json.get("dimension")
    if dimension is not None and not isinstance(dimension, str):
        raise ValueError("expected_plan.dimension 必须为字符串或 null")
    direction = plan_json.get("direction")
    if direction not in ("change", "decrease", "increase"):
        raise ValueError(
            f"expected_plan.direction 必须取 change/decrease/increase，实际 {direction!r}"
        )
    synthesizer = plan_json.get("synthesizer", "additive_delta_v1")
    if not isinstance(synthesizer, str) or not synthesizer:
        raise ValueError("expected_plan.synthesizer 必须为非空字符串")
    recipe_version = plan_json.get("recipe_version", 1)
    if (
        isinstance(recipe_version, bool)
        or not isinstance(recipe_version, int)
        or recipe_version < 1
    ):
        raise ValueError(f"expected_plan.recipe_version 必须为正整数，实际 {recipe_version!r}")
    subs = tuple(
        _sub_plan_from_json(item, f"expected_sub_plans[{i}]") for i, item in enumerate(subs_json)
    )
    return AnalysisPlan(
        intent=intent,
        metric=metric,
        dimension=dimension,
        baseline=_time_spec_from_json(plan_json.get("baseline"), "expected_plan.baseline"),
        current=_time_spec_from_json(plan_json.get("current"), "expected_plan.current"),
        filters=_filters_from_json(plan_json.get("filters", []), "expected_plan.filters"),
        direction=direction,
        sub_plans=subs,
        synthesizer=synthesizer,
        recipe_version=recipe_version,
    )


def _plan_summary(plan: AnalysisPlan) -> dict[str, object]:
    """AnalysisPlan → 与 expected_plan 同构的投影（便于逐键比对）。"""
    return {
        "intent": plan.intent,
        "metric": plan.metric,
        "dimension": plan.dimension,
        "baseline": {"granularity": plan.baseline.granularity, "value": plan.baseline.value},
        "current": {"granularity": plan.current.granularity, "value": plan.current.value},
        "direction": plan.direction,
        "filters": [{"column": f.column, "op": f.op, "value": f.value} for f in plan.filters],
        "synthesizer": plan.synthesizer,
        "recipe_version": plan.recipe_version,
    }


def _jsonable(value: object) -> object:
    """把 tuple/嵌套 dict 归一为 JSON 可比结构（投影 vs 样本 JSON 比对用）。"""
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


# ---------------------------------------------------------------------------
# 结构检查（执行前门：任何 fail 都会阻止该样本进真链）
# ---------------------------------------------------------------------------


def _reference_problems(sample: dict[str, object]) -> list[str]:
    """核对 ready answer/unavailable 样本的参考结果完整性（必测项不得静默 skip）。"""
    reference = sample.get("reference")
    if not isinstance(reference, dict):
        return ["answer/unavailable 的 ready 样本必须声明 reference"]
    sql = reference.get("sql")
    if not isinstance(sql, list) or not sql or not all(isinstance(s, str) and s for s in sql):
        return ["reference.sql 必须为非空字符串列表（人工核对的独立参考 SQL）"]
    results = reference.get("results")
    if not isinstance(results, list) or not results:
        return ["reference.results 必须为非空列表（四个角色块）"]
    problems: list[str] = []
    for role in ANALYSIS_ROLES:
        block = next((b for b in results if isinstance(b, dict) and b.get("role") == role), None)
        if block is None:
            problems.append(f"reference.results 缺角色块 {role}（必测项不得静默 skip）")
            continue
        columns = block.get("columns")
        if (
            not isinstance(columns, list)
            or not columns
            or not all(isinstance(c, str) and c for c in columns)
        ):
            problems.append(f"{role}: columns 必须为非空字符串列表")
        rows = block.get("rows")
        if not isinstance(rows, list):
            problems.append(f"{role}: rows 必须为列表")
        elif sample.get("expected_kind") == "answer" and not rows:
            problems.append(f"{role}: answer 参考行不得为空")
    return problems


def _structural_checks(
    sample: dict[str, object],
    *,
    dry: bool,
    snapshot_sha: str | None,
    semantic_sha256: str | None,
) -> list[Check]:
    """执行前结构门（--dry 下成熟度/sha/绑定门标 not_run，其余照跑）。"""
    checks: list[Check] = []
    kind_raw = sample.get("expected_kind")
    kind = kind_raw if isinstance(kind_raw, str) else None

    validation_target = {k: v for k, v in sample.items() if k != "_file"}
    try:
        jsonschema.validate(validation_target, _SCHEMA)
    except jsonschema.exceptions.ValidationError as exc:
        checks.append(("sample_schema", "fail", f"样本契约不符：{exc.message}"))
    except Exception as exc:  # schema 工具层异常也按失败计，绝不静默放行
        checks.append(("sample_schema", "fail", f"样本契约校验异常：{exc!r}"))
    else:
        checks.append(("sample_schema", "pass", None))

    file_raw = sample.get("_file")
    if not isinstance(file_raw, str):
        checks.append(("id_matches_filename", "skip", "内存样本，无文件上下文"))
    elif Path(file_raw).stem == sample.get("id"):
        checks.append(("id_matches_filename", "pass", None))
    else:
        checks.append(
            (
                "id_matches_filename",
                "fail",
                f"id {sample.get('id')!r} 与文件名 {Path(file_raw).stem!r} 不符",
            )
        )

    if dry:
        checks.append(("maturity_ready", "not_run", "--dry 允许 draft（只做结构校验）"))
    elif sample.get("maturity") == "ready":
        checks.append(("maturity_ready", "pass", None))
    else:
        checks.append(
            (
                "maturity_ready",
                "fail",
                f"真链评测只跑 ready 样本，实际 maturity={sample.get('maturity')!r}"
                "（draft 只能 --dry）",
            )
        )

    if dry:
        checks.append(("shas_declared", "not_run", "--dry 允许 <待填写> 占位"))
    else:
        sha_problems = [
            f"{key} 必须为实值 sha（不得为空或 {PENDING}）"
            for key in ("snapshot_sha", "semantic_sha256")
            if not isinstance(sample.get(key), str)
            or not str(sample.get(key))
            or str(sample.get(key)) == PENDING
        ]
        checks.append(
            ("shas_declared", "fail" if sha_problems else "pass", "；".join(sha_problems) or None)
        )

    if dry:
        checks.append(("snapshot_match", "not_run", "--dry 不绑定运行参数"))
        checks.append(("semantic_match", "not_run", "--dry 不绑定运行参数"))
    else:
        if sample.get("snapshot_sha") == snapshot_sha:
            checks.append(("snapshot_match", "pass", None))
        else:
            checks.append(
                (
                    "snapshot_match",
                    "fail",
                    f"样本快照 sha {sample.get('snapshot_sha')!r} ≠ 运行参数 {snapshot_sha!r}",
                )
            )
        if sample.get("semantic_sha256") == semantic_sha256:
            checks.append(("semantic_match", "pass", None))
        else:
            checks.append(
                (
                    "semantic_match",
                    "fail",
                    f"样本语义 sha {sample.get('semantic_sha256')!r} ≠ "
                    f"运行参数 {semantic_sha256!r}",
                )
            )

    if kind not in ("answer", "unavailable"):
        checks.append(("plan_template", "skip", f"终态 {kind!r} 无计划期望"))
        checks.append(("reference_complete", "skip", f"终态 {kind!r} 无参考结果"))
    else:
        plan_raw = sample.get("expected_plan")
        subs_raw = sample.get("expected_sub_plans")
        if not isinstance(plan_raw, dict) or not isinstance(subs_raw, list):
            checks.append(
                (
                    "plan_template",
                    "fail",
                    "answer/unavailable 必须声明 expected_plan 与 expected_sub_plans",
                )
            )
        else:
            try:
                expected_plan = _plan_from_json(plan_raw, subs_raw)
            except ValueError as exc:
                checks.append(("plan_template", "fail", f"期望计划不可解析：{exc}"))
            else:
                problems = list(validate_analysis_plan(expected_plan))
                if expected_plan.intent != EXPECTED_INTENT:
                    problems.append(
                        f"intent 必须为 {EXPECTED_INTENT}（ADR-0026 固定模板），"
                        f"实际 {expected_plan.intent!r}"
                    )
                checks.append(
                    (
                        "plan_template",
                        "fail" if problems else "pass",
                        "；".join(problems) if problems else None,
                    )
                )
        if sample.get("maturity") != "ready":
            checks.append(("reference_complete", "skip", "draft 样本参考结果允许占位"))
        else:
            problems = _reference_problems(sample)
            checks.append(
                (
                    "reference_complete",
                    "fail" if problems else "pass",
                    "；".join(problems) if problems else None,
                )
            )
    return checks


def _not_run_result_checks() -> list[Check]:
    """--dry：执行项一律 not_run（绝不用于放行）。"""
    return [(name, "not_run", "--dry 只做结构/计划校验") for name in RESULT_CHECKS]


def _skipped_result_checks(detail: str) -> list[Check]:
    """未执行的样本：执行项一律 skip（绝不算通过）。"""
    return [(name, "skip", detail) for name in RESULT_CHECKS]


# ---------------------------------------------------------------------------
# 独立 oracle：按加法分解口径从参考结果重算（绝不调用 agent.synthesize）
# ---------------------------------------------------------------------------


class _OracleItem(NamedTuple):
    """独立重算的贡献项。"""

    value: str
    baseline: Decimal
    current: Decimal
    delta: Decimal
    contribution_pct: Decimal


def _reference_block(results: list[object], role: str) -> dict[str, object]:
    """按角色取参考结果块。"""
    for block in results:
        if isinstance(block, dict) and block.get("role") == role:
            return block
    raise ValueError(f"参考结果缺少角色块 {role}")


def _columns_of(block: dict[str, object], role: str) -> list[str]:
    """取参考块的字符串列名（非法列名即视为 oracle 不可用）。"""
    columns = block.get("columns")
    if not isinstance(columns, list):
        raise ValueError(f"{role}: 参考块 columns 必须为列表")
    names = [c for c in columns if isinstance(c, str)]
    if len(names) != len(columns):
        raise ValueError(f"{role}: 参考块 columns 必须全为字符串")
    return names


def _metric_column(columns: list[str], metric: str) -> int:
    """指标列定位：必须存在与 metric 同名的列，否则 fail-closed。

    绝不静默回退取最后一列（T09 评审 Minor-1）：畸形参考块上错位取列
    会让 oracle 悄悄比对到错误数字，违背评测器 fail-closed 立场。
    调用方（_single_total / _grouped_values）在上层捕获 ValueError，
    样本级记 fail 并继续出报告，不炸整份评测。
    """
    try:
        return columns.index(metric)
    except ValueError:
        raise ValueError(
            f"参考块缺少与指标同名的列 {metric}（实际列：{columns}），拒绝回退取最后一列"
        ) from None


def _to_decimal(cell: object, label: str) -> Decimal:
    """单元格 → Decimal（精确文本数值；空值/非有限值都不可用）。"""
    if cell is None:
        raise ValueError(f"{label} 为空值")
    try:
        value = Decimal(str(cell))
    except InvalidOperation as exc:
        raise ValueError(f"{label} 无法转为 Decimal：{cell!r}") from exc
    if not value.is_finite():
        raise ValueError(f"{label} 非有限数值：{cell!r}")
    return value


def _single_total(results: list[object], role: str, metric: str) -> Decimal:
    """总量角色块必须恰有一行，取指标单元格。"""
    block = _reference_block(results, role)
    columns = _columns_of(block, role)
    rows = block.get("rows")
    if not isinstance(rows, list) or len(rows) != 1:
        raise ValueError(f"{role}: 总量参考块必须恰有一行")
    row = rows[0]
    if not isinstance(row, list):
        raise ValueError(f"{role}: 参考行必须为数组")
    idx = _metric_column(columns, metric)
    if len(row) <= idx:
        raise ValueError(f"{role}: 参考行缺指标列 {metric}")
    return _to_decimal(row[idx], f"{role}[{columns[idx]}]")


def _grouped_values(
    results: list[object], role: str, metric: str, dimension: str
) -> dict[str | None, Decimal]:
    """分组角色块 → 维度值 → 指标值（重复维度值即 oracle 不可用）。

    维度单元格契约镜像综合器（agent/analysis.py `_grouped_step_rows`：NULL 为
    None 独立桶）：None = NULL 维度独立桶，照常入 map；str 原样；其余类型
    fail-closed（窄接受，防 JSON float / DB 标量类型漂移被静默接受）。
    """
    block = _reference_block(results, role)
    columns = _columns_of(block, role)
    rows = block.get("rows")
    if not isinstance(rows, list):
        raise ValueError(f"{role}: 参考块 rows 必须为列表")
    if dimension not in columns:
        raise ValueError(f"{role}: 参考块缺维度列 {dimension}")
    dim_idx = columns.index(dimension)
    metric_idx = _metric_column(columns, metric)
    values: dict[str | None, Decimal] = {}
    for r, row in enumerate(rows):
        if not isinstance(row, list) or len(row) <= max(dim_idx, metric_idx):
            raise ValueError(f"{role}: 第 {r} 行列数不足")
        key_cell = row[dim_idx]
        if key_cell is not None and not isinstance(key_cell, str):
            raise ValueError(f"{role}: 第 {r} 行维度值必须为字符串或 null，实际 {key_cell!r}")
        if key_cell in values:
            raise ValueError(f"{role}: 存在重复维度值 {key_cell}")
        values[key_cell] = _to_decimal(row[metric_idx], f"{role}[{key_cell}]")
    return values


def _pct(delta: Decimal, total_delta: Decimal) -> Decimal:
    """贡献率 = delta / 总 delta × 100，量化到 1e-6（ROUND_HALF_EVEN）；-0 归 +0。"""
    if total_delta == 0:
        raise ValueError("总 delta 为 0，贡献率无定义")
    pct = (delta / total_delta * Decimal(100)).quantize(PCT_QUANT, rounding=ROUND_HALF_EVEN)
    if pct == 0:
        pct = ZERO.quantize(PCT_QUANT)
    return pct


def _typed_key(value: str | None) -> tuple[str, ...]:
    """维度值排序键（与综合器 `_typed_dimension_key` 同型的类型标注键）。

    None（NULL 桶）→ ("null",)：NULL ≠ 任何字符串，类型标签 "null" < "str"
    保证 NULL 桶排在所有字符串键之前；str → ("str", value)：同型按字典序。
    """
    if value is None:
        return ("null",)
    return ("str", value)


def _oracle_items(results: list[object], metric: str, dimension: str) -> tuple[_OracleItem, ...]:
    """独立重算贡献项：两期并集（缺侧补零）→ 精确 delta/贡献率 → 两段稳定排序。"""
    base_total = _single_total(results, "baseline_total", metric)
    cur_total = _single_total(results, "current_total", metric)
    total_delta = cur_total - base_total
    if total_delta == 0:
        raise ValueError(
            "参考总量 delta 为 0：answer 形态不适用（应为 unavailable/zero_total_delta）"
        )
    current_map = _grouped_values(results, "current_by_dimension", metric, dimension)
    baseline_map = _grouped_values(results, "baseline_by_dimension", metric, dimension)
    keys = list(current_map)
    keys.extend(k for k in baseline_map if k not in current_map)
    # 排序必须喂原始键（None/str）：综合器按 `_typed_dimension_key(draft.key)` 对原始
    # 键排序（agent/analysis.py），渲染（None→"(null)"）在其之后。若按渲染后 value 排
    # 序，字面 "(null)" 与 NULL 桶键同为 ("str", "(null)") 无法区分，且 "!" 等字典序
    # 靠前字符串桶会插到 NULL 桶前——窄平局下 oracle 序与真链序相反，误拒绝真链。
    pairs: list[tuple[str | None, _OracleItem]] = []
    for key in keys:
        baseline = baseline_map.get(key, ZERO)
        current = current_map.get(key, ZERO)
        delta = current - baseline
        pairs.append(
            (
                key,
                _OracleItem(
                    # None 桶渲染与综合器 _dimension_label 同源标签（import 共享，防字面量漂移）
                    value=_NULL_DIM_LABEL if key is None else key,
                    baseline=baseline,
                    current=current,
                    delta=delta,
                    contribution_pct=_pct(delta, total_delta),
                ),
            )
        )
    pairs.sort(key=lambda pair: _typed_key(pair[0]))
    pairs.sort(key=lambda pair: abs(pair[1].delta), reverse=True)
    return tuple(item for _, item in pairs)


# ---------------------------------------------------------------------------
# 执行 + 结果核对
# ---------------------------------------------------------------------------


def _normalize_rows(rows: object, label: str) -> list[list[str | None]]:
    """行 → 字符串单元格矩阵（None 保留），供归一化比对。"""
    if not isinstance(rows, list):
        raise ValueError(f"{label} 必须为列表")
    out: list[list[str | None]] = []
    for row in rows:
        if not isinstance(row, (list, tuple)):
            raise ValueError(f"{label} 每行必须为数组")
        out.append([None if cell is None else str(cell) for cell in row])
    return out


def _sorted_rows(rows: list[list[str | None]]) -> list[list[str | None]]:
    """行序归一（比对域 = 行集合而非顺序）。"""
    return sorted(rows, key=lambda row: tuple((c is None, c if c is not None else "") for c in row))


def _execute_and_check(
    sample: dict[str, object],
    agent: AnalysisAgent,
    *,
    snapshot_sha: str,
    semantic_sha256: str,
) -> list[Check]:
    """真链执行该样本并逐项核对（单项失败独立计数，互不平均）。"""
    checks: list[Check] = []
    question = sample.get("question")
    if not isinstance(question, str) or not question:
        checks.append(("executed", "fail", "样本 question 缺失或非字符串"))
        checks.extend(_skipped_result_checks("前序 executed 失败"))
        return checks
    try:
        outcome = agent.analyze(question)
    except Exception as exc:  # 单样本异常只判该样本失败，不吞掉同批其他样本
        checks.append(("executed", "fail", f"agent.analyze 抛出异常：{exc!r}"))
        checks.extend(_skipped_result_checks("前序 executed 失败"))
        return checks
    if not isinstance(outcome, AnalysisResult):
        checks.append(
            ("executed", "fail", f"agent.analyze 返回 {type(outcome).__name__}，非 AnalysisResult")
        )
        checks.extend(_skipped_result_checks("前序 executed 失败"))
        return checks
    checks.append(("executed", "pass", None))
    result = outcome

    violations = validate_analysis_result(result)
    checks.append(
        (
            "contract",
            "fail" if violations else "pass",
            "；".join(violations) if violations else None,
        )
    )

    kind_raw = sample.get("expected_kind")
    expected_kind = kind_raw if isinstance(kind_raw, str) else None
    try:
        actual_kind: str | None = _KIND_BY_STATUS[analysis_status(result)]
    except ValueError as exc:
        checks.append(("kind", "fail", f"终态推导失败：{exc}"))
        actual_kind = None
    else:
        if actual_kind == expected_kind:
            checks.append(("kind", "pass", None))
        else:
            checks.append(("kind", "fail", f"期望终态 {expected_kind!r}，实际 {actual_kind!r}"))

    expected_calls = sample.get("expected_sql_calls")
    actual_calls = sum(1 for step in result.steps if step.kind == "answer")
    if isinstance(expected_calls, int) and not isinstance(expected_calls, bool):
        if actual_calls == expected_calls:
            checks.append(("sql_calls", "pass", None))
        else:
            checks.append(
                ("sql_calls", "fail", f"期望 SQL 调用 {expected_calls} 次，实际 {actual_calls} 次")
            )
    else:
        checks.append(("sql_calls", "fail", f"expected_sql_calls 非法：{expected_calls!r}"))

    plan_raw = sample.get("expected_plan")
    metric = plan_raw.get("metric") if isinstance(plan_raw, dict) else None
    dimension = plan_raw.get("dimension") if isinstance(plan_raw, dict) else None
    metric_str = metric if isinstance(metric, str) else None
    dimension_str = dimension if isinstance(dimension, str) else None

    if isinstance(plan_raw, dict):
        if result.plan is None:
            checks.append(("plan", "fail", "结果缺 plan，但样本声明了期望计划"))
        else:
            summary = _plan_summary(result.plan)
            problems = [
                f"plan.{key}: 期望 {value!r}，实际 {summary.get(key)!r}"
                for key, value in plan_raw.items()
                if summary.get(key) != value
            ]
            checks.append(
                ("plan", "fail" if problems else "pass", "；".join(problems) if problems else None)
            )
    else:
        checks.append(("plan", "skip", "样本未声明期望计划"))

    subs_raw = sample.get("expected_sub_plans")
    if isinstance(subs_raw, list):
        if result.plan is None:
            checks.append(("sub_plans", "fail", "结果缺 plan，但样本声明了期望子计划"))
        elif len(result.plan.sub_plans) != len(subs_raw):
            checks.append(
                (
                    "sub_plans",
                    "fail",
                    f"子计划数 {len(result.plan.sub_plans)} ≠ 期望 {len(subs_raw)}",
                )
            )
        else:
            sub_problems: list[str] = []
            for i, sub in enumerate(result.plan.sub_plans):
                expected_sub = subs_raw[i]
                if not isinstance(expected_sub, dict):
                    sub_problems.append(f"expected_sub_plans[{i}] 必须为对象")
                    continue
                projection = cast("dict[str, object]", _jsonable(plan_projection(sub)))
                for key, value in expected_sub.items():
                    if key == "role":
                        continue
                    if projection.get(key) != value:
                        sub_problems.append(
                            f"sub_plans[{i}].{key}: 期望 {value!r}，实际 {projection.get(key)!r}"
                        )
            checks.append(
                (
                    "sub_plans",
                    "fail" if sub_problems else "pass",
                    "；".join(sub_problems) if sub_problems else None,
                )
            )
    else:
        checks.append(("sub_plans", "skip", "样本未声明期望子计划"))

    reference = sample.get("reference")
    results_list: list[object] | None = None
    if isinstance(reference, dict) and isinstance(reference.get("results"), list):
        results_list = reference["results"]

    if not results_list:
        checks.append(("steps_columns", "skip", "样本无参考结果"))
        checks.append(("steps_rows", "skip", "样本无参考结果"))
    else:
        column_problems: list[str] = []
        row_problems: list[str] = []
        for block in results_list:
            if not isinstance(block, dict):
                column_problems.append("参考结果块必须为对象")
                continue
            role = block.get("role")
            if not isinstance(role, str) or role not in ANALYSIS_ROLES:
                column_problems.append(f"参考块 role 非法：{role!r}")
                continue
            index = ANALYSIS_ROLES.index(role)
            if len(result.steps) <= index:
                column_problems.append(f"{role}: 结果步骤数 {len(result.steps)} 不足")
                continue
            step = result.steps[index]
            try:
                expected_rows = _normalize_rows(block.get("rows"), f"{role}.rows")
            except ValueError as exc:
                row_problems.append(str(exc))
                expected_rows = []
            actual_columns = list(step.columns)
            expected_columns = block.get("columns")
            if actual_columns != expected_columns:
                column_problems.append(
                    f"{role}: 列不符，期望 {expected_columns!r}，实际 {actual_columns!r}"
                )
            actual_rows = [
                [None if cell is None else str(cell) for cell in row] for row in step.rows
            ]
            if _sorted_rows(expected_rows) != _sorted_rows(actual_rows):
                row_problems.append(
                    f"{role}: 行不符（行序已归一），期望 {expected_rows!r}，实际 {actual_rows!r}"
                )
        checks.append(
            (
                "steps_columns",
                "fail" if column_problems else "pass",
                "；".join(column_problems) if column_problems else None,
            )
        )
        checks.append(
            (
                "steps_rows",
                "fail" if row_problems else "pass",
                "；".join(row_problems) if row_problems else None,
            )
        )

    if expected_kind not in ("answer", "unavailable"):
        checks.append(("attribution_totals", "skip", f"终态 {expected_kind!r} 不校验综合总量"))
    elif metric_str is None or results_list is None:
        checks.append(("attribution_totals", "fail", "样本缺期望指标或参考结果，无法独立核对总量"))
    else:
        attr = result.attribution
        if attr is None:
            checks.append(("attribution_totals", "fail", "结果缺 attribution"))
        elif attr.status == "unavailable" and attr.reason_code == "zero_total_delta":
            problems = []
            if attr.baseline is None or attr.current is None:
                problems.append("净零综合必须保留两期已验证总量（决策⑤ L207 修正①）")
            else:
                try:
                    base_total = _single_total(results_list, "baseline_total", metric_str)
                    cur_total = _single_total(results_list, "current_total", metric_str)
                except ValueError as exc:
                    problems.append(f"参考总量不可用：{exc}")
                else:
                    if attr.baseline != base_total:
                        problems.append(f"baseline 期望 {base_total}，实际 {attr.baseline}")
                    if attr.current != cur_total:
                        problems.append(f"current 期望 {cur_total}，实际 {attr.current}")
            if attr.delta != ZERO:
                problems.append(f"净零综合 delta 必须为 0，实际 {attr.delta}")
            checks.append(
                (
                    "attribution_totals",
                    "fail" if problems else "pass",
                    "；".join(problems) if problems else None,
                )
            )
        elif attr.status == "unavailable":
            problems = []
            if attr.baseline is not None or attr.current is not None or attr.delta is not None:
                problems.append(
                    f"不可用综合（{attr.reason_code}）不得携带总量，"
                    "baseline/current/delta 应为 None"
                )
            checks.append(
                (
                    "attribution_totals",
                    "fail" if problems else "pass",
                    "；".join(problems) if problems else None,
                )
            )
        else:
            problems = []
            try:
                base_total = _single_total(results_list, "baseline_total", metric_str)
                cur_total = _single_total(results_list, "current_total", metric_str)
            except ValueError as exc:
                problems.append(f"参考总量不可用：{exc}")
            else:
                if attr.baseline != base_total:
                    problems.append(f"baseline 期望 {base_total}，实际 {attr.baseline}")
                if attr.current != cur_total:
                    problems.append(f"current 期望 {cur_total}，实际 {attr.current}")
                oracle_delta = cur_total - base_total
                if attr.delta != oracle_delta:
                    problems.append(f"delta 期望 {oracle_delta}，实际 {attr.delta}")
            checks.append(
                (
                    "attribution_totals",
                    "fail" if problems else "pass",
                    "；".join(problems) if problems else None,
                )
            )

    if expected_kind != "answer":
        checks.append(("attribution_items", "skip", f"终态 {expected_kind!r} 不校验贡献项"))
    elif metric_str is None or dimension_str is None or results_list is None:
        checks.append(
            ("attribution_items", "fail", "样本缺期望指标/维度/参考结果，无法独立核对贡献项")
        )
    else:
        problems = []
        try:
            oracle = _oracle_items(results_list, metric_str, dimension_str)
        except ValueError as exc:
            problems.append(f"独立 oracle 不可用：{exc}")
            oracle = ()
        attr = result.attribution
        if attr is None:
            problems.append("结果缺 attribution")
        elif not problems:
            actual_items = attr.items
            if len(actual_items) != len(oracle):
                problems.append(f"贡献项数 {len(actual_items)} ≠ 期望 {len(oracle)}")
            else:
                for i, (actual_item, expected_item) in enumerate(
                    zip(actual_items, oracle, strict=True)
                ):
                    if actual_item.value != expected_item.value:
                        problems.append(
                            f"items[{i}].value 期望 {expected_item.value}，实际 {actual_item.value}"
                        )
                    if actual_item.baseline != expected_item.baseline:
                        problems.append(
                            f"items[{i}].baseline 期望 {expected_item.baseline}，"
                            f"实际 {actual_item.baseline}"
                        )
                    if actual_item.current != expected_item.current:
                        problems.append(
                            f"items[{i}].current 期望 {expected_item.current}，"
                            f"实际 {actual_item.current}"
                        )
                    if actual_item.delta != expected_item.delta:
                        problems.append(
                            f"items[{i}].delta 期望 {expected_item.delta}，实际 {actual_item.delta}"
                        )
                    if actual_item.contribution_pct != expected_item.contribution_pct:
                        problems.append(
                            f"items[{i}].contribution_pct 期望 {expected_item.contribution_pct}，"
                            f"实际 {actual_item.contribution_pct}"
                        )
        checks.append(
            (
                "attribution_items",
                "fail" if problems else "pass",
                "；".join(problems) if problems else None,
            )
        )

    expected_code = sample.get("expected_reason_code")
    code_problems: list[str] = []
    try:
        effective = effective_reason_code(result)
    except ValueError as exc:
        code_problems.append(f"原因码推导失败：{exc}")
        effective = None
    if effective != expected_code:
        code_problems.append(f"有效原因码期望 {expected_code!r}，实际 {effective!r}")
    if result.reason_code != expected_code:
        code_problems.append(
            f"result.reason_code 期望 {expected_code!r}，实际 {result.reason_code!r}"
        )
    checks.append(
        ("reason_code", "fail" if code_problems else "pass", "；".join(code_problems) or None)
    )

    if expected_kind != "clarify":
        checks.append(("clarification", "skip", f"终态 {expected_kind!r} 不校验澄清载荷"))
    else:
        problems = []
        expected_clar = sample.get("expected_clarification")
        clar = result.turn.clarification
        if not isinstance(expected_clar, dict):
            problems.append("clarify 样本必须声明 expected_clarification")
        elif clar is None:
            problems.append("结果缺 clarification")
        else:
            expected_reasons = expected_clar.get("reasons")
            if list(clar.reasons) != (
                expected_reasons if isinstance(expected_reasons, list) else None
            ):
                problems.append(
                    f"澄清理由不符：期望 {expected_reasons!r}，实际 {list(clar.reasons)!r}"
                )
            if clar.kind != expected_clar.get("kind"):
                problems.append(f"澄清 kind 期望 {expected_clar.get('kind')!r}，实际 {clar.kind!r}")
        checks.append(
            (
                "clarification",
                "fail" if problems else "pass",
                "；".join(problems) if problems else None,
            )
        )

    binding_problems = []
    if result.snapshot_sha != snapshot_sha:
        binding_problems.append(
            f"结果 snapshot_sha {result.snapshot_sha!r} ≠ 运行参数 {snapshot_sha!r}"
        )
    if result.semantic_sha256 != semantic_sha256:
        binding_problems.append(
            f"结果 semantic_sha256 {result.semantic_sha256!r} ≠ 运行参数 {semantic_sha256!r}"
        )
    checks.append(
        (
            "bindings",
            "fail" if binding_problems else "pass",
            "；".join(binding_problems) if binding_problems else None,
        )
    )
    return checks


# ---------------------------------------------------------------------------
# 评测入口
# ---------------------------------------------------------------------------


def _code_sha() -> str:
    """当前代码短 sha（git 不可用时显式标 unknown，绝不冒充）。"""
    return git_short_sha_or_none() or "unknown"


def _working_tree_dirty() -> bool:
    """工作区是否含未提交变更（git 不可用时保守视为 dirty）。"""
    try:
        proc = subprocess.run(  # noqa: S603
            ["git", "status", "--porcelain"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return True
    return bool(proc.stdout.strip())


def evaluate_analysis(
    agent: AnalysisAgent | None,
    samples: list[dict[str, object]],
    *,
    snapshot_sha: str | None = None,
    semantic_sha256: str | None = None,
    dry: bool = False,
    snapshots_dir: Path | None = None,
    measure: Callable[[], dict[str, object]] | None = None,
) -> dict[str, object]:
    """评测一批 analysis 样本，返回按 check 分列的报告（绝不拼装 composite accuracy）。

    参数 agent：被测 agent（--dry 可为 None）。
    参数 samples：样本列表（评测器只读，绝不回填样本期望）。
    参数 snapshot_sha / semantic_sha256：真链模式必填的运行参数（绝不默认 HEAD）。
    参数 dry：只做结构/计划校验，执行项标 not_run。
    参数 snapshots_dir / measure：快照指纹复核的目录与现场测量（可注入以便测试）。
    抛 ValueError：样本集为空，或真链模式缺 agent / 显式 sha。
    抛 SnapshotVerifyError：运行前/后快照数据指纹与 meta 不符（报告无效）。
    """
    if not samples:
        raise ValueError("样本集为空：没有任何样本可评测（禁止空跑绿灯）")
    if not dry:
        if agent is None:
            raise ValueError("真链评测必须提供被测 agent（--dry 才允许缺省）")
        if not snapshot_sha or not semantic_sha256:
            raise ValueError(
                "真链评测必须显式提供 snapshot_sha 与 semantic_sha256（绝不默认 HEAD；"
                "结构校验请用 --dry）"
            )
        before_ok, before_detail = verify_snapshot_fingerprint(
            snapshot_sha, snapshots_dir=snapshots_dir, measure=measure
        )
        if not before_ok:
            raise SnapshotVerifyError(f"运行前快照指纹复核失败：{before_detail}")

    sample_reports: list[dict[str, object]] = []
    tally: dict[str, dict[str, int]] = {
        name: {"pass": 0, "fail": 0, "skip": 0, "not_run": 0}
        for name in STRUCTURAL_CHECKS + RESULT_CHECKS
    }
    executed_any = False
    for sample in samples:
        structural = _structural_checks(
            sample, dry=dry, snapshot_sha=snapshot_sha, semantic_sha256=semantic_sha256
        )
        if dry:
            result_checks = _not_run_result_checks()
        elif any(status == "fail" for _, status, _ in structural):
            result_checks = _skipped_result_checks(_SKIPPED_DETAIL)
        else:
            assert agent is not None  # 真链模式已在入口把门
            result_checks = _execute_and_check(
                sample,
                agent,
                snapshot_sha=snapshot_sha or "",
                semantic_sha256=semantic_sha256 or "",
            )
            executed_any = True
        checks = structural + result_checks
        sample_reports.append(
            {
                "id": sample.get("id"),
                "file": sample.get("_file"),
                "maturity": sample.get("maturity"),
                "expected_kind": sample.get("expected_kind"),
                "ok": not any(status == "fail" for _, status, _ in checks),
                "checks": [
                    {"name": name, "status": status, "detail": detail}
                    for name, status, detail in checks
                ],
            }
        )
        for name, status, _detail in checks:
            tally[name][status] += 1

    after_ok: bool | None = None
    after_detail: str | None = None
    if not dry:
        assert snapshot_sha is not None
        after_ok, after_detail = verify_snapshot_fingerprint(
            snapshot_sha, snapshots_dir=snapshots_dir, measure=measure
        )
        if not after_ok:
            raise SnapshotVerifyError(f"运行后快照指纹复核失败：{after_detail}")

    all_checks = [
        (str(check["name"]), str(check["status"]))
        for entry in sample_reports
        for check in cast("list[dict[str, object]]", entry["checks"])
    ]
    checks_total = len(all_checks)
    checks_failed = sum(1 for _, status in all_checks if status == "fail")
    checks_not_run = sum(1 for _, status in all_checks if status in ("skip", "not_run"))
    ok_samples = sum(1 for entry in sample_reports if entry["ok"] is True)
    return {
        "evaluator": "analysis_eval",
        "created_at": datetime.now(TZ).isoformat(),
        "dry": dry,
        "run": "executed" if (not dry and executed_any) else "not_run",
        "code_sha": _code_sha(),
        "dirty": _working_tree_dirty(),
        "snapshot_sha": None if dry else snapshot_sha,
        "semantic_sha256": None if dry else semantic_sha256,
        "snapshot_verified": {
            "before": None if dry else before_ok,
            "after": after_ok,
        },
        "ok": checks_failed == 0,
        "summary": {
            "samples": len(sample_reports),
            "ok_samples": ok_samples,
            "failed_samples": len(sample_reports) - ok_samples,
            "checks_total": checks_total,
            "checks_failed": checks_failed,
            "checks_not_run": checks_not_run,
        },
        "checks": tally,
        "samples": sample_reports,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _load_samples(samples_dir: Path) -> list[dict[str, object]]:
    """加载 <samples_dir>/*/*.json 样本（schema.json 除外），注入 _file 上下文。"""
    samples: list[dict[str, object]] = []
    for path in sorted(samples_dir.glob("*/*.json")):
        if path.name == "schema.json":
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"样本必须是 JSON 对象：{path}")
        data["_file"] = str(path)
        samples.append(data)
    return samples


def main(
    argv: list[str] | None = None,
    *,
    agent: AnalysisAgent | None = None,
    measure: Callable[[], dict[str, object]] | None = None,
) -> int:
    """CLI 入口：真链写 eval/reports/analysis-<code_sha>.json；--dry 只打印不落盘。

    返回 0（全绿）/ 1（有失败项）/ 2（评测被拒绝：缺参、空样本、快照漂移等）。
    """
    parser = argparse.ArgumentParser(description="Atlas 多步分析独立评测器（ADR-0026 T09）")
    parser.add_argument("--snapshot-sha", default=None, help="必填（真链）：待评测数据快照 sha")
    parser.add_argument(
        "--dry", action="store_true", help="只做结构/计划校验（draft 可跑，绝不放行）"
    )
    parser.add_argument(
        "--samples", type=Path, default=None, help="样本根目录（默认 eval/analysis）"
    )
    parser.add_argument("--domain", default="finance", help="语义模型域（默认 finance）")
    parser.add_argument(
        "--report-dir", type=Path, default=None, help="报告目录（默认 eval/reports）"
    )
    parser.add_argument(
        "--model", type=Path, default=None, help="语义模型路径（默认按 --domain 取）"
    )
    parser.add_argument(
        "--snapshots-dir", type=Path, default=None, help="覆盖快照目录（默认 data/snapshots）"
    )
    args = parser.parse_args(argv)

    samples_dir = args.samples if args.samples is not None else REPO_ROOT / "eval" / "analysis"
    report_dir = args.report_dir if args.report_dir is not None else REPORT_DIR

    if not args.dry and not args.snapshot_sha:
        print(
            "真链评测必须显式 --snapshot-sha（绝不默认 HEAD）；仅结构校验请加 --dry",
            file=sys.stderr,
        )
        return 2

    samples = _load_samples(samples_dir)
    if not samples:
        print(f"样本目录没有任何样本：{samples_dir}", file=sys.stderr)
        return 2

    if args.model is not None:
        model_path: Path = args.model
    elif args.domain in DOMAIN_MODELS:
        model_path = DOMAIN_MODELS[args.domain]
    else:
        print(f"未知 domain：{args.domain}（可选：{sorted(DOMAIN_MODELS)}）", file=sys.stderr)
        return 2
    semantic_sha256 = SemanticModel(model_path).source_sha256

    resolved_agent = agent
    if resolved_agent is None and not args.dry:
        from agent.factory import create_live_agent

        resolved_agent = create_live_agent(model_path)

    try:
        report = evaluate_analysis(
            resolved_agent,
            samples,
            snapshot_sha=args.snapshot_sha,
            semantic_sha256=semantic_sha256,
            dry=args.dry,
            snapshots_dir=args.snapshots_dir,
            measure=measure,
        )
    except (SnapshotVerifyError, ValueError) as exc:
        print(f"评测被拒绝：{exc}", file=sys.stderr)
        return 2

    payload = json.dumps(report, ensure_ascii=False, indent=2, default=str)
    if args.dry:
        print(payload)
        return 0 if report["ok"] is True else 1

    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"analysis-{report['code_sha']}.json"
    report_path.write_text(payload + "\n", encoding="utf-8")
    print(f"报告已写入 {report_path}")
    return 0 if report["ok"] is True else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
