"""候选归纳器（ADR-0027 T2）：从 pending 反馈生成同义词 / 值域别名候选。

设计约束（0027 决策 ② + 决策 ① 铁律）
--------------------------------------
- **离线纯函数**：induce_candidates() 不读盘、不调 LLM、无随机性
- **只落 _candidates/ 非权威区**：write_candidates() 绝不写 synonyms/*.yml
  或 values/*.json（权威源）
- **确定性**：同输入 → 同输出
- **候选不自动生效**：人工审核后才可把候选追加进 Git 权威源

候选生成触发条件（确定性规则，无 LLM）
---------------------------------------
- 同义词候选：kind ∈ {misunderstood, wrong_metric} 且 sample.corrected_term 非空
  → 建议把 corrected_term 作为 suggest_for_metric 的同义词
- 值域别名候选：kind = wrong_value 且 sample.corrected_term 非空
  → 建议把 corrected_term 作为值域别名

不满足上述条件的反馈不产候选（纯 kind 信号不够，需用户给出具体正确措辞）。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
SYNONYMS_CANDIDATES_DIR = REPO / "semantic" / "synonyms" / "_candidates"
VALUES_CANDIDATES_DIR = REPO / "semantic" / "values" / "_candidates"

_SYNONYM_KINDS = frozenset({"misunderstood", "wrong_metric"})
_VALUE_ALIAS_KINDS = frozenset({"wrong_value"})


@dataclass(frozen=True)
class SynonymCandidate:
    """同义词候选：建议把 term 作为 suggest_for_metric 的同义词。"""

    term: str
    suggest_for_metric: str
    source: str
    question: str


@dataclass(frozen=True)
class ValueAliasCandidate:
    """值域别名候选：建议把 term 作为某维度的值域别名。"""

    term: str
    source: str
    question: str


@dataclass
class CandidateSet:
    """一批归纳产物（同义词 + 值域别名）。"""

    synonyms: list[SynonymCandidate] = field(default_factory=list)
    value_aliases: list[ValueAliasCandidate] = field(default_factory=list)


def _get_sample(record: dict[str, Any]) -> dict[str, Any]:
    """提取 sample（统一 pending 形态，ADR-0027 决策 ④）。"""
    sample = record.get("sample")
    if isinstance(sample, dict):
        return sample
    return {}


def _synonym_candidates(records: list[dict[str, Any]]) -> list[SynonymCandidate]:
    """从 pending 记录提取同义词候选。"""
    out: list[SynonymCandidate] = []
    for rec in records:
        sample = _get_sample(rec)
        kind = str(sample.get("kind", ""))
        corrected = str(sample.get("corrected_term", "")).strip()
        metric = str(sample.get("metric", "")).strip()
        if kind in _SYNONYM_KINDS and corrected and metric:
            out.append(
                SynonymCandidate(
                    term=corrected,
                    suggest_for_metric=metric,
                    source=str(rec.get("source", "")),
                    question=str(sample.get("question", "")),
                )
            )
    return out


def _value_alias_candidates(records: list[dict[str, Any]]) -> list[ValueAliasCandidate]:
    """从 pending 记录提取值域别名候选。"""
    out: list[ValueAliasCandidate] = []
    for rec in records:
        sample = _get_sample(rec)
        kind = str(sample.get("kind", ""))
        corrected = str(sample.get("corrected_term", "")).strip()
        if kind in _VALUE_ALIAS_KINDS and corrected:
            out.append(
                ValueAliasCandidate(
                    term=corrected,
                    source=str(rec.get("source", "")),
                    question=str(sample.get("question", "")),
                )
            )
    return out


def induce_candidates(records: list[dict[str, Any]]) -> CandidateSet:
    """纯函数：从 pending 反馈记录归纳候选（不读盘、不调 LLM）。

    参数
    ----
    records : 统一 pending 形态记录列表（每条含 sample.kind / sample.corrected_term 等）。

    返回
    ----
    CandidateSet：同义词候选 + 值域别名候选。
    """
    return CandidateSet(
        synonyms=_synonym_candidates(records),
        value_aliases=_value_alias_candidates(records),
    )


def write_candidates(candidates: CandidateSet, semantic_dir: Path | None = None) -> list[Path]:
    """把候选写入 _candidates/ 非权威目录（绝不写权威源文件）。

    参数
    ----
    candidates    : induce_candidates 的产物。
    semantic_dir  : semantic/ 根目录（默认仓库根 semantic/）。测试注入用 tmp 路径。

    返回
    ----
    list[Path]：写入的文件路径列表。
    """
    if semantic_dir is None:
        syn_dir = SYNONYMS_CANDIDATES_DIR
        val_dir = VALUES_CANDIDATES_DIR
    else:
        syn_dir = semantic_dir / "synonyms" / "_candidates"
        val_dir = semantic_dir / "values" / "_candidates"

    written: list[Path] = []

    for c in candidates.synonyms:
        syn_dir.mkdir(parents=True, exist_ok=True)
        path = syn_dir / f"syn_{uuid.uuid4().hex[:8]}.json"
        path.write_text(
            json.dumps(
                {
                    "term": c.term,
                    "suggest_for_metric": c.suggest_for_metric,
                    "source": c.source,
                    "question": c.question,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        written.append(path)

    for c in candidates.value_aliases:
        val_dir.mkdir(parents=True, exist_ok=True)
        path = val_dir / f"val_{uuid.uuid4().hex[:8]}.json"
        path.write_text(
            json.dumps(
                {
                    "term": c.term,
                    "source": c.source,
                    "question": c.question,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        written.append(path)

    return written
