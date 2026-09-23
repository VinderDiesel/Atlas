"""ADR-0031 D09 绑定：意图槽位 → Plan 字段的确定性映射（未绑定只能澄清）。

职责边界
--------
- **完整绑定，不丢条件**：每个意图槽位要么产出一条 `SlotBinding`，要么进
  `unresolved_slots`；有未绑定条件时 `plan_candidate` 必须为 None（澄清），
  不存在"理解成功但条件掉落"的发布路径。
- **候选是唯一指标来源**：metric 匹配域 = 候选集（检索预算产物）∩ 注册目录；
  注册目录内但未被召回的指标不越权绑定（不静默扩召回），目录外文本不创造。
- **关系可达性是硬检查**：groups/filters 的维度字段必须 `can_group_by`
  （与 Compiler._join_chain 同源）；不可达即未绑定，不做跨实体拼接。
- **算子诚实映射**：Compiler 只支持标量 `= != < <= > >=`；多值 in/not_in
  首版无力绑定 → 澄清，不拆成多个等值（会改变语义）。
- 歧义声明一律进 unresolved（不强猜）；`ambiguities` 的 slot 由模型声明。
- 默认值：无 limit → `DEFAULT_LIMIT`（与 Plan 默认一致）；order_by/comparison
  保持 Plan 默认；时间只绑首个（Plan.time 单值），其余未绑定即澄清。
"""

from __future__ import annotations

from agent.compiler import Filter, OrderSpec, Plan, SemanticModel, TimeSpec
from agent.intent.contracts import (
    BindingResult,
    CandidateSet,
    IntentFilter,
    NormalizedIntent,
    NormalizedValue,
    SlotBinding,
)
from agent.runtime.bundle import RuntimeBundle
from retrieval.graph_store import SemanticGraph

# 语义模型的制品内路径（发布域 = 金融；零售域接入时按装配显式传入）。
DEFAULT_MODEL_PATH = "semantic/ossie/atlas_finance.ossie.yaml"

# 意图算子 → Compiler 标量算子（Compiler 只支持这 6 种）。
_OP_MAP = {"eq": "=", "neq": "!=", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}
# NormalizedTime 粒度 → TimeSpec 粒度（day 在 TimeSpec 里是 date）。
_GRANULARITY_MAP = {"year": "year", "quarter": "quarter", "month": "month", "day": "date"}

DEFAULT_LIMIT = 100


def _norm(text: str) -> str:
    """标签归一：去空白 + 小写（中文不变，英文大小写不敏感）。"""
    return "".join(text.split()).lower()


def _match_label(target: str, labels: frozenset[str]) -> bool:
    """完全相等或"注册标签是原文子串"（如 '总交易额' 含 '交易额'）。"""
    return target in labels or any(label and label in target for label in labels)


class IntentBinder:
    """确定性绑定器：构造期固定【注册 ∧ 授权】目录与可达性图，运行期纯函数。

    参数
    ----
    model      : 已注册目录的语义模型。
    authorized : 权限硬过滤集合（授权可见的 semantic_id）；`None` = 全可见。
    """

    def __init__(self, model: SemanticModel, *, authorized: frozenset[str] | None = None) -> None:
        self._model = model
        self._graph = SemanticGraph(model)
        registered = set(model.metrics)
        if authorized is not None:
            registered &= set(authorized)
        self._registered = frozenset(registered)
        self._fields: dict[str, tuple[str, tuple[str, ...]]] = {}
        for ds_name, ds in model.datasets.items():
            for fname, field in ds.fields.items():
                if field.is_time:
                    continue
                self._fields[fname] = (ds_name, field.synonyms)

    def bind(self, intent: NormalizedIntent, candidates: CandidateSet) -> BindingResult:
        """槽位逐一绑定；有未绑定条件时只产出澄清结果（见模块 docstring）。"""
        visible = tuple(
            c.semantic_id for c in candidates.candidates if c.semantic_id in self._registered
        )
        bound: list[SlotBinding] = []
        unresolved: list[str] = []

        metric = self._bind_metric(intent, visible, bound, unresolved)

        dims: list[str] = []
        filters: tuple[Filter, ...] = ()
        time_spec: TimeSpec | None = None
        orders: tuple[OrderSpec, ...] = ()
        if metric is None:
            # 依赖 metric 的槽位无法验证归属/可达性 → 逐槽未绑定（不静默丢）。
            unresolved.extend(f"groups.{i}" for i in range(len(intent.groups)))
            unresolved.extend(f"filters.{i}" for i in range(len(intent.filters)))
            unresolved.extend(f"time.{i}" for i in range(len(intent.times)))
            if intent.sort is not None:
                unresolved.append("sort")
        else:
            dims = self._bind_groups(intent, metric, bound, unresolved)
            filters = self._bind_filters(intent, metric, bound, unresolved)
            time_spec = self._bind_time(intent, bound, unresolved)
            orders = self._bind_sort(intent, metric, tuple(dims), bound, unresolved)

        limit = intent.limit if intent.limit is not None else DEFAULT_LIMIT
        if intent.limit is not None:
            bound.append(SlotBinding(slot="limit", plan_field="limit", value=str(limit)))

        for ambiguity in intent.ambiguities:
            # 歧义不强猜：声明的槽位一律未绑定（去重保序）。
            if ambiguity.slot not in unresolved:
                unresolved.append(ambiguity.slot)

        plan: Plan | None = None
        if not unresolved and metric is not None:
            plan = Plan(
                metric=metric,
                dimensions=tuple(dims),
                time=time_spec,
                filters=filters,
                order_by=orders,
                limit=limit,
            )
        if not unresolved:
            reason_code = "bound"
        elif not candidates.candidates:
            reason_code = "no_candidates"
        else:
            reason_code = "clarification_required"
        return BindingResult(
            plan_candidate=plan,
            slot_bindings=tuple(bound),
            unresolved_slots=tuple(unresolved),
            reason_code=reason_code,
        )

    # -- 槽位绑定 ----------------------------------------------------------

    def _bind_metric(
        self,
        intent: NormalizedIntent,
        visible: tuple[str, ...],
        bound: list[SlotBinding],
        unresolved: list[str],
    ) -> str | None:
        if intent.metric_mentions:
            matched = [self._match_metric(m.text, visible) for m in intent.metric_mentions]
            names = {name for name in matched if name is not None}
            if len(names) == 1 and all(name is not None for name in matched):
                metric = names.pop()
                bound.append(SlotBinding(slot="metric", plan_field="metric", value=metric))
                return metric
            unresolved.append("metric")
            return None
        # 无提及：候选 rank1 是检索的确定性选择（仍须属注册目录）。
        if visible:
            metric = visible[0]
            bound.append(SlotBinding(slot="metric", plan_field="metric", value=metric))
            return metric
        unresolved.append("metric")
        return None

    def _bind_groups(
        self,
        intent: NormalizedIntent,
        metric: str,
        bound: list[SlotBinding],
        unresolved: list[str],
    ) -> list[str]:
        dims: list[str] = []
        for i, mention in enumerate(intent.groups):
            field = self._match_field(mention.text)
            if field is None or not self._graph.can_group_by(metric, field):
                unresolved.append(f"groups.{i}")
                continue
            if field not in dims:
                dims.append(field)
            bound.append(SlotBinding(slot=f"groups.{i}", plan_field="dimensions", value=field))
        return dims

    def _bind_filters(
        self,
        intent: NormalizedIntent,
        metric: str,
        bound: list[SlotBinding],
        unresolved: list[str],
    ) -> tuple[Filter, ...]:
        filters: list[Filter] = []
        cursor = 0  # intent.values 与 filters 值平铺同序（normalize 同源产物）
        for i, spec in enumerate(intent.filters):
            normalized = intent.values[cursor : cursor + len(spec.values)]
            cursor += len(spec.values)
            bound_filter = self._bind_filter(spec, metric, normalized)
            if bound_filter is None:
                unresolved.append(f"filters.{i}")
                continue
            filters.append(bound_filter)
            bound.append(
                SlotBinding(
                    slot=f"filters.{i}",
                    plan_field="filters",
                    value=f"{bound_filter.column} {bound_filter.op} {bound_filter.value}",
                )
            )
        return tuple(filters)

    def _bind_filter(
        self, spec: IntentFilter, metric: str, normalized: tuple[NormalizedValue, ...]
    ) -> Filter | None:
        op = spec.op
        if op in ("in", "not_in"):
            if len(spec.values) != 1:
                return None  # 多值 IN 无标量等价 → 澄清（不拆等值）
            op = "eq" if op == "in" else "neq"
        compiler_op = _OP_MAP.get(op)
        if compiler_op is None:
            return None
        column = self._match_filter_column(spec.subject.text, metric)
        if column is None:
            return None
        value: object = spec.values[0].text
        if normalized and normalized[0].number is not None:
            value = normalized[0].number
        return Filter(column=column, op=compiler_op, value=value)

    def _bind_time(
        self,
        intent: NormalizedIntent,
        bound: list[SlotBinding],
        unresolved: list[str],
    ) -> TimeSpec | None:
        spec: TimeSpec | None = None
        for i, item in enumerate(intent.times):
            if i == 0:
                spec = TimeSpec(granularity=_GRANULARITY_MAP[item.granularity], value=item.value)
                bound.append(SlotBinding(slot="time.0", plan_field="time", value=str(item.value)))
            else:
                unresolved.append(f"time.{i}")  # Plan.time 单值：其余时间条件只能澄清
        if not intent.times and intent.time_mentions:
            unresolved.extend(f"time.{i}" for i in range(len(intent.time_mentions)))
        return spec

    def _bind_sort(
        self,
        intent: NormalizedIntent,
        metric: str,
        dims: tuple[str, ...],
        bound: list[SlotBinding],
        unresolved: list[str],
    ) -> tuple[OrderSpec, ...]:
        if intent.sort is None:
            return ()
        desc = intent.sort.direction == "desc"
        text = intent.sort.by.text
        if self._match_metric(text, (metric,)) is not None:
            order = OrderSpec(column=metric, desc=desc)
        else:
            field = self._match_field(text)
            if field is None or field not in dims:
                unresolved.append("sort")
                return ()
            order = OrderSpec(column=field, desc=desc)
        bound.append(
            SlotBinding(
                slot="sort",
                plan_field="order_by",
                value=f"{order.column} {'desc' if desc else 'asc'}",
            )
        )
        return (order,)

    # -- 确定性匹配 --------------------------------------------------------

    def _match_metric(self, text: str, cand_ids: tuple[str, ...]) -> str | None:
        target = _norm(text)
        scores: dict[str, int] = {}
        for cand in cand_ids:
            best = 0
            for label in self._metric_labels(cand):
                if label == target:
                    best = max(best, len(label) + 1)  # 完全相等优先于任何子串
                elif label and label in target:
                    best = max(best, len(label))
            if best:
                scores[cand] = best
        if not scores:
            return None
        top = max(scores.values())
        winners = [cand for cand, score in scores.items() if score == top]
        return winners[0] if len(winners) == 1 else None

    def _metric_labels(self, metric: str) -> frozenset[str]:
        labels = {_norm(metric)}
        labels.update(_norm(s) for s in self._model.metric_synonyms.get(metric, ()))
        return frozenset(labels)

    def _match_field(self, text: str) -> str | None:
        target = _norm(text)
        scores: dict[str, int] = {}
        for name, (_, synonyms) in self._fields.items():
            labels = {_norm(name)} | {_norm(s) for s in synonyms}
            best = 0
            for label in labels:
                if label == target:
                    best = max(best, len(label) + 1)
                elif label and label in target:
                    best = max(best, len(label))
            if best:
                scores[name] = best
        if not scores:
            return None
        top = max(scores.values())
        winners = [name for name, score in scores.items() if score == top]
        return winners[0] if len(winners) == 1 else None

    def _match_filter_column(self, text: str, metric: str) -> str | None:
        # 度量阈值（column == metric）优先；否则按维度字段（可达性硬检查）。
        if self._match_metric(text, (metric,)) is not None:
            return metric
        field = self._match_field(text)
        if field is not None and self._graph.can_group_by(metric, field):
            return field
        return None


def bind_intent(
    intent: NormalizedIntent,
    candidates: CandidateSet,
    bundle: RuntimeBundle,
    *,
    model_path: str = DEFAULT_MODEL_PATH,
    authorized: frozenset[str] | None = None,
) -> BindingResult:
    """设计页接口（T10）：`bind_intent(intent, candidates, bundle) -> BindingResult`。

    从发布制品装配语义模型（`bundle.semantic_model(model_path)`），不做 Git 导入
    审核（由装配/发布路径保证）；`authorized` 为权限硬过滤集合。
    """
    model = bundle.semantic_model(model_path)
    return IntentBinder(model, authorized=authorized).bind(intent, candidates)
