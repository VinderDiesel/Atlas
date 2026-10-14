"""飞轮指标变更提案生成器（ADR-0027 决策 ③）。

离线纯函数：读 approved pending → 产结构化 proposal → 落 eval/failures/_proposals/。
绝不提交、不改写 semantic/ossie/*.yaml——proposal 只读，生效走 lint+eval 门禁。

提案规则（确定性，不依赖 LLM）：
- wrong_metric + corrected_term → 指标变更提案
  （用户说"应该用 X 而非 Y"→ Y 的定义可能需要修订，或需要新增 X）
- wrong_value + corrected_term → 值域映射提案
  （用户说"值应该是 X"→ 值域别名可能需要补充）

提案文件名：prop_<sha>-<sample_id>.json，与源 pending 文件一一对应。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
FAILURES_DIR = REPO / "eval" / "failures"
PROPOSALS_DIR = FAILURES_DIR / "_proposals"


@dataclass(frozen=True)
class MetricProposal:
    """指标变更提案：建议审查或新增指标定义。"""

    proposal_type: str  # "review_metric" 或 "add_metric"
    target_metric: str  # 被质疑的指标名
    suggested_term: str  # 用户纠正后的措辞
    change_kind: str  # 反馈 kind（wrong_metric / misunderstood）
    source_feedback: str  # 来源 pending 文件相对路径
    question: str  # 原始问句
    comment: str  # 用户备注
    evidence_feedbacks: list[str]  # 关联的其他反馈路径（同 metric 的 pending）


@dataclass(frozen=True)
class ValueProposal:
    """值域映射提案：建议补充值域别名。"""

    proposal_type: str  # "add_alias"
    target_field: str  # 被质疑的字段名
    suggested_alias: str  # 用户纠正后的措辞
    source_feedback: str
    question: str
    comment: str


def _read_sample(payload: dict[str, Any]) -> dict[str, Any]:
    """统一读取：新格式从 sample 取，旧格式从顶层取（与 export_approved 同口径）。"""
    sample = payload.get("sample") or {}
    if not sample and "question" in payload:
        sample = {
            k: v
            for k, v in payload.items()
            if k
            not in (
                "schema_version",
                "status",
                "origin",
                "category",
                "sha",
                "source",
                "collected_at",
                "reviewed_by",
            )
        }
    return sample


def generate_proposals(
    failures_dir: Path | None = None,
    dry: bool = False,
    proposals_dir: Path | None = None,
) -> dict[str, int]:
    """读 approved pending → 产 proposal → 落 _proposals/。

    Parameters
    ----------
    failures_dir : 反馈根目录（默认 eval/failures/）。
    dry : True = 只返回计数，不落盘。
    proposals_dir : proposal 输出目录（默认 eval/failures/_proposals/）。

    Returns
    -------
    dict with keys: metric_proposals, value_proposals, skipped.
    """
    root = failures_dir or FAILURES_DIR
    prop_out = proposals_dir or PROPOSALS_DIR
    metric_count = 0
    value_count = 0
    skipped = 0

    # 第一遍：收集同 metric 的反馈路径列表（用于 evidence_feedbacks）
    metric_feedbacks: dict[str, list[str]] = {}
    for payload_path in sorted(root.glob("*/*.json")):
        payload: dict[str, Any] = json.loads(
            payload_path.read_text(encoding="utf-8")
        )
        if payload.get("status") != "approved":
            continue
        sample = _read_sample(payload)
        metric = str(sample.get("metric", "")).strip()
        if metric:
            rel = str(payload_path.relative_to(root))
            metric_feedbacks.setdefault(metric, []).append(rel)

    # 第二遍：生成 proposal
    for payload_path in sorted(root.glob("*/*.json")):
        payload: dict[str, Any] = json.loads(
            payload_path.read_text(encoding="utf-8")
        )
        if payload.get("status") != "approved":
            skipped += 1
            continue
        sample = _read_sample(payload)
        corrected = str(sample.get("corrected_term", "")).strip()
        if not corrected:
            skipped += 1
            continue
        kind = str(sample.get("kind", ""))
        question = str(sample.get("question", ""))
        comment = str(sample.get("comment", ""))
        metric = str(sample.get("metric", "")).strip()
        sha = str(payload.get("sha", payload_path.stem))
        sample_id = str(sample.get("id", payload_path.stem))
        rel_path = str(payload_path.relative_to(root))

        if kind in ("wrong_metric", "misunderstood") and metric:
            # 关联反馈：同 metric 的其他 approved pending（排除自身）
            evidence = [
                p
                for p in metric_feedbacks.get(metric, [])
                if p != rel_path
            ]
            proposal = MetricProposal(
                proposal_type="review_metric",
                target_metric=metric,
                suggested_term=corrected,
                change_kind=kind,
                source_feedback=rel_path,
                question=question,
                comment=comment,
                evidence_feedbacks=evidence,
            )
            metric_count += 1
            if not dry:
                prop_out.mkdir(parents=True, exist_ok=True)
                target = prop_out / f"prop_{sha}-{sample_id}.json"
                target.write_text(
                    json.dumps(asdict(proposal), ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
        elif kind == "wrong_value" and metric:
            proposal = ValueProposal(
                proposal_type="add_alias",
                target_field=metric,
                suggested_alias=corrected,
                source_feedback=rel_path,
                question=question,
                comment=comment,
            )
            value_count += 1
            if not dry:
                prop_out.mkdir(parents=True, exist_ok=True)
                target = prop_out / f"prop_{sha}-{sample_id}.json"
                target.write_text(
                    json.dumps(asdict(proposal), ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )

    return {
        "metric_proposals": metric_count,
        "value_proposals": value_count,
        "skipped": skipped,
    }
