"""指标-数据集-维度 语义关系图（NetworkX，MVP 内存图；Neo4j 为可选扩展）。

图的来源与对齐
--------------
- 节点：`metric:<name>`（15 指标）、`dataset:<name>`（8 数据集）、
  `field:<name>`（维度表中有同义词的业务字段，= Planner 维度口径）。
- 边：metric ↔ 其表达式引用的数据集（deterministic 解析）、
  dataset ↔ 字段、dataset ↔ dataset（YAML relationships，**与 Compiler
  `_join_chain` 同源**）。
- 用途：检索候选的**可达性预检**——若指标的数据集与问句维度的所在表
  无 join 路径，Compiler 必然编译失败（CompileError），图约束在检索层
  提前剔除该候选，防止"跨实体错误召回"（Day 23 验收项）。

已知边界（MVP，诚实声明）
------------------------
- 只建模语义层图（指标/数据集/维度字段），不含具体实体实例
  （如"某个具体账户"的图）；实体级过滤待 Agent 阶段评估。
- 字段名全局唯一假设沿用 SemanticModel.find_field 语义（同名字段后者覆盖）。
- **图约束比 Compiler 保守**：可达性禁止事实表间桥接（fact→fact，如现金域经
  fact_holdings 中转到证券维度）。Compiler 目前技术上能编译这种多跳 SQL
  （缺维度域检查，见 AGENTS 决策优先级 4），但同指标值会跨多事实粒度重复，
  语义上视为跨实体错误——图在检索层提前剔除，双方口径差异属已知边界。
"""

from __future__ import annotations

import re

import networkx as nx
from sqlglot import exp, parse_one

from agent.compiler import SemanticModel

# 节点 id 前缀（不同类型同名安全共存）
_METRIC = "metric:"
_DATASET = "dataset:"
_FIELD = "field:"

_FIELD_REF_RE = re.compile(r"\b([a-z_]+)\.([A-Za-z_]+)\b")


class SemanticGraph:
    """语义层可达性图：检索候选的维度-指标合法性预检。

    用法::

        graph = SemanticGraph(SemanticModel())
        dims = graph.detect_dimensions("按分支统计的佣金收入")   # ("Branch",)
        kept = graph.filter_candidates(["total_trade_value", "cash_balance"], dims)
    """

    def __init__(self, model: SemanticModel) -> None:
        self._model = model
        self._g = nx.Graph()
        self._field_ds: dict[str, str] = {}  # 字段名 → 所在 dataset（find_field 语义）
        self._metric_datasets: dict[str, set[str]] = {}  # 指标 → 表达式引用数据集
        for ds_name, ds in model.datasets.items():
            for fname in ds.fields:
                if fname in model.dimension_synonyms:
                    # 与 SemanticModel.find_field 同口径：遍历序后者覆盖
                    self._field_ds[fname] = ds_name
        self._build(model)

    def _build(self, model: SemanticModel) -> None:
        g = self._g
        # 1) metric → 引用数据集
        for name, expr in model.metrics.items():
            refs = {
                col.table
                for col in parse_one(expr).find_all(exp.Column)
                if col.table
            }
            if not refs:  # 防御：表达式必须有显式表前缀（compiler 同样要求）
                refs = {m.group(1) for m in _FIELD_REF_RE.finditer(expr)}
            self._metric_datasets[name] = refs
            g.add_node(_METRIC + name)
            for ds in refs:
                g.add_node(_DATASET + ds)
                g.add_edge(_METRIC + name, _DATASET + ds)
        # 2) dataset → 维度字段
        for fname, ds_name in self._field_ds.items():
            g.add_node(_FIELD + fname)
            g.add_edge(_FIELD + fname, _DATASET + ds_name)
        # 3) dataset ↔ dataset（relationships，与 Compiler._join_chain 同源）
        for rel in model.relationships:
            g.add_edge(_DATASET + rel.from_ds, _DATASET + rel.to_ds)

    # -- 查询 API ----------------------------------------------------------

    def datasets_for(self, metric: str) -> set[str]:
        """指标表达式引用的数据集集合（目前恒单元素，保留集合语义防未来多表）。"""
        return set(self._metric_datasets.get(metric, ()))

    def dimension_table(self, field: str) -> str | None:
        """维度字段所在数据集（无同义词字段或不存在 → None）。"""
        return self._field_ds.get(field)

    def can_group_by(self, metric: str, field: str) -> bool:
        """指标能否按某维度字段分组（维度域可达性，见模块 docstring 边界）。

        规则：field 所在表可从 metric 引用的**任一**数据集出发，沿
        dataset 边到达；边合法条件 = 不得连接两个事实表（fact→fact 桥接
        会把单一事实指标摊到另一事实的粒度上，属跨实体错误）。
        """
        field_ds = self.dimension_table(field)
        if field_ds is None:
            return False
        return any(field_ds in self._reachable_datasets(ds) for ds in self.datasets_for(metric))

    def _reachable_datasets(self, start_ds: str) -> set[str]:
        """受限 BFS：从 start_ds 沿合法边可达的 dataset 集合（含起点）。

        合法边规则：起点为事实表时只能先进入维度表（维度域）；进入维度域后
        只允许沿 dim→dim 层级边扩展，禁止 dim→fact（经维度表中转到另一事实
        表会把单一事实指标摊到别的事实粒度上，属跨实体错误，见模块 docstring）。
        """
        visited = {start_ds}
        queue = [start_ds]
        while queue:
            cur = queue.pop(0)
            for node in self._g.neighbors(_DATASET + cur):
                if not node.startswith(_DATASET):
                    continue
                nxt = node[len(_DATASET):]
                if nxt in visited:
                    continue
                if cur.startswith("fact_"):
                    if nxt.startswith("fact_"):
                        continue  # fact→fact 直连桥接（防御，当前 rel 无此形态）
                elif nxt.startswith("fact_"):
                    continue  # dim→fact：离开维度域回事实表 = 桥接
                visited.add(nxt)
                queue.append(nxt)
        return visited

    def filter_candidates(
        self, candidates: list[str], dimension_fields: list[str]
    ) -> list[str]:
        """按维度约束过滤候选指标（保序）。

        全部 dimension_fields 都可达才保留；无维度约束时原样返回。
        """
        if not dimension_fields:
            return list(candidates)
        return [
            m
            for m in candidates
            if all(self.can_group_by(m, f) for f in dimension_fields)
        ]

    def detect_dimensions(self, question: str) -> list[str]:
        """问句 → 命中的维度字段（子串匹配，不要求显式分组结构词）。

        与 Planner._parse_dimensions 的区别：检索层粗筛不依赖"按X统计"结构词，
        问句中出现的任何维度同义词都视为潜在分组意图；无命中 → 空列表（不过滤）。
        dim_date 的日历字段（"年/季/月"等）排除在维度意图之外——时间粒度应走
        Plan.time 通道（与 Planner/gold 口径一致，避免"2013 年"被误检为维度词）。
        """
        hits = [
            name
            for name, syns in self._model.dimension_synonyms.items()
            if any(s in question for s in syns)
            and self.dimension_table(name) != "dim_date"
        ]
        # 去重保序（同义词命中多个字段时按模型字段序）
        seen: set[str] = set()
        ordered: list[str] = []
        for name in hits:
            if name not in seen:
                seen.add(name)
                ordered.append(name)
        return ordered
