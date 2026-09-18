"""指标变更 proposal 生成器（ADR-0027 T3）：从 pending 反馈生成语义层变更提案。

设计约束（0027 决策 ③ + 决策 ① 铁律）
--------------------------------------
- **离线纯函数**：induce_proposals() 不读盘、不调 LLM、无随机性
- **只落 _proposals/ 非权威区**：write_proposals() 绝不写 semantic/ossie/*.yaml
- **proposal 不自动生效**：人工依 proposal 改语义层定义 → make lint → make eval 回归
  → 按 ADR-0010 报告门禁合入
- **只读操作**：proposal 生成前后 semantic/ossie/ 的 git diff 为空（判据 4）

proposal 生成触发条件（确定性规则）
-----------------------------------
- kind = wrong_metric 且 sample.metric 非空
  → 生成 proposal：affected_metric = metric，change_type = wrong_metric
  → 若 sample.corrected_term 非空，suspected_definition = corrected_term
- 同 metric 多条反馈 → 合并为一条 proposal（evidence 聚合）
- 其他 kind 不产 proposal（wrong_value 由 lora.candidates 处理值域别名）
"""

from __future__ import annotations

import json
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
PROPOSALS_DIR = REPO / "eval" / "failures" / "_proposals"

_PROPOSAL_KINDS = frozenset({"wrong_metric"})


@dataclass
class Proposal:
    """语义层变更提案：某 metric 的定义可能有问题，需人工审阅。"""

    affected_metric: str
    change_type: str
    evidence: list[str] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)
    suspected_definition: str = ""


def _get_sample(record: dict[str, Any]) -> dict[str, Any]:
    """提取 sample（统一 pending 形态，ADR-0027 决策 ④）。"""
    sample = record.get("sample")
    if isinstance(sample, dict):
        return sample
    return {}


def induce_proposals(records: list[dict[str, Any]]) -> list[Proposal]:
    """纯函数：从 pending 反馈记录归纳指标变更 proposal（不读盘、不调 LLM）。

    参数
    ----
    records : 统一 pending 形态记录列表（每条含 sample.kind / sample.metric 等）。

    返回
    ----
    list[Proposal]：按 affected_metric 分组的变更提案。
    """
    grouped: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"evidence": [], "questions": [], "change_type": "", "suspected": ""}
    )

    for rec in records:
        sample = _get_sample(rec)
        kind = str(sample.get("kind", ""))
        metric = str(sample.get("metric", "")).strip()
        if kind not in _PROPOSAL_KINDS or not metric:
            continue

        entry = grouped[metric]
        entry["change_type"] = kind
        entry["evidence"].append(str(rec.get("source", "")))
        entry["questions"].append(str(sample.get("question", "")))
        corrected = str(sample.get("corrected_term", "")).strip()
        if corrected and not entry["suspected"]:
            entry["suspected"] = corrected

    return [
        Proposal(
            affected_metric=metric,
            change_type=info["change_type"],
            evidence=info["evidence"],
            questions=info["questions"],
            suspected_definition=info["suspected"],
        )
        for metric, info in grouped.items()
    ]


def write_proposals(
    proposals: list[Proposal], failures_dir: Path | None = None
) -> list[Path]:
    """把 proposal 写入 _proposals/ 非权威目录（绝不写权威源文件）。

    参数
    ----
    proposals    : induce_proposals 的产物。
    failures_dir : eval/failures/ 根目录（默认仓库根 eval/failures/）。测试注入用 tmp 路径。

    返回
    ----
    list[Path]：写入的文件路径列表。
    """
    if failures_dir is None:
        proposals_dir = PROPOSALS_DIR
    else:
        proposals_dir = failures_dir / "_proposals"

    written: list[Path] = []

    for p in proposals:
        proposals_dir.mkdir(parents=True, exist_ok=True)
        path = proposals_dir / f"proposal_{uuid.uuid4().hex[:8]}.json"
        path.write_text(
            json.dumps(
                {
                    "affected_metric": p.affected_metric,
                    "change_type": p.change_type,
                    "evidence": p.evidence,
                    "questions": p.questions,
                    "suspected_definition": p.suspected_definition,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        written.append(path)

    return written
