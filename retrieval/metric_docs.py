"""指标检索语料：语义模型 → 检索文档（确定性生成，无外部状态）。

每篇文档 = 指标名 + 全部同义词 + 指标描述，字段拼接为纯文本交由
Bm25Index 统一 tokenize。文档与 50 条 gold 评测问句同源（同一份
ossie.yaml），保证"语料即权威定义"——检索不会引入语义层之外的词。
"""

from __future__ import annotations

from dataclasses import dataclass

from agent.compiler import SemanticModel


@dataclass(frozen=True)
class MetricDoc:
    """检索语料中的一篇指标文档。"""

    doc_id: str  # 指标名（与 gold expected_metric 同口径）
    text: str


def build_metric_docs(model: SemanticModel) -> list[MetricDoc]:
    """把模型内全部指标转成检索文档（按指标定义序，与 YAML 一致）。

    参数
    ----
    model : 已加载的语义模型（atlas_finance.ossie.yaml）。

    返回
    ----
    MetricDoc 列表；无 description 的指标降级为名称 + 同义词。
    """
    docs: list[MetricDoc] = []
    for name in model.metrics:
        parts = [name]
        parts.extend(model.metric_synonyms.get(name, ()))
        desc = model.metric_descriptions.get(name)
        if desc:
            parts.append(desc)
        docs.append(MetricDoc(doc_id=name, text=" ".join(parts)))
    return docs
