"""飞轮候选归纳器（ADR-0027 决策 ②：同义词 / 值域别名候选生成）。

离线纯函数：读 pending 反馈 → 产候选 → 落 _candidates/。
绝不写 synonyms/*.yml、值域 aliases、向量库——候选不自动生效。

归纳规则（确定性，不依赖 LLM）：
- 仅处理含非空 corrected_term 的 approved 样本（人工已确认修正方向）；
- wrong_metric / misunderstood + corrected_term → 同义词候选
  （用户说"应该是 X 而不是 Y"→ X 可能是 Y 的另一种说法）；
- wrong_value + corrected_term → 值域别名候选
  （用户说"值应该是 X"→ X 可能是某维度的别名写法）。

候选文件名：syn_<sha>-<sample_id>.json / val_<sha>-<sample_id>.json，
与源 pending 文件一一对应，方便审计溯源。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
FAILURES_DIR = REPO / "eval" / "failures"
SYN_CANDIDATES_DIR = REPO / "semantic" / "synonyms" / "_candidates"
VAL_CANDIDATES_DIR = REPO / "semantic" / "values" / "_candidates"


@dataclass(frozen=True)
class SynonymCandidate:
    """同义词候选：建议 term 作为 metric/dimension 的同义措辞。"""

    term: str
    suggest_for: str  # metric 或 dimension 名
    suggest_kind: str  # "metric" 或 "dimension"
    source_feedback: str  # 来源 pending 文件相对路径
    source_kind: str  # 反馈 kind（wrong_metric / misunderstood 等）
    question: str  # 原始问句
    comment: str  # 用户备注


@dataclass(frozen=True)
class ValueAliasCandidate:
    """值域别名候选：建议 term 作为某字段值的别名。"""

    term: str
    model: str
    field: str
    source_feedback: str
    source_kind: str
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


def induce_candidates(
    failures_dir: Path | None = None,
    dry: bool = False,
    syn_dir: Path | None = None,
    val_dir: Path | None = None,
) -> dict[str, int]:
    """读 approved pending → 产候选 → 落 _candidates/。

    Parameters
    ----------
    failures_dir : 反馈根目录（默认 eval/failures/）。
    dry : True = 只返回计数，不落盘。
    syn_dir : 同义词候选输出目录（默认 semantic/synonyms/_candidates/）。
    val_dir : 值域别名候选输出目录（默认 semantic/values/_candidates/）。

    Returns
    -------
    dict with keys: synonym_candidates, value_alias_candidates, skipped_no_correction,
    skipped_not_approved.
    """
    root = failures_dir or FAILURES_DIR
    syn_out = syn_dir or SYN_CANDIDATES_DIR
    val_out = val_dir or VAL_CANDIDATES_DIR
    syn_count = 0
    val_count = 0
    skipped_no_correction = 0
    skipped_not_approved = 0

    for payload_path in sorted(root.glob("*/*.json")):
        payload: dict[str, Any] = json.loads(
            payload_path.read_text(encoding="utf-8")
        )
        if payload.get("status") != "approved":
            skipped_not_approved += 1
            continue
        sample = _read_sample(payload)
        corrected = str(sample.get("corrected_term", "")).strip()
        if not corrected:
            skipped_no_correction += 1
            continue
        kind = str(sample.get("kind", ""))
        question = str(sample.get("question", ""))
        comment = str(sample.get("comment", ""))
        metric = str(sample.get("metric", "")).strip()
        sha = str(payload.get("sha", payload_path.stem))
        sample_id = str(sample.get("id", payload_path.stem))
        rel_path = str(payload_path.relative_to(root))

        if kind in ("wrong_metric", "misunderstood") and metric:
            candidate = SynonymCandidate(
                term=corrected,
                suggest_for=metric,
                suggest_kind="metric",
                source_feedback=rel_path,
                source_kind=kind,
                question=question,
                comment=comment,
            )
            syn_count += 1
            if not dry:
                syn_out.mkdir(parents=True, exist_ok=True)
                target = syn_out / f"syn_{sha}-{sample_id}.json"
                target.write_text(
                    json.dumps(asdict(candidate), ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
        elif kind == "wrong_value" and metric:
            # wrong_value：corrected_term 可能是值域别名
            # model 从 metric 推断（metric 名通常含 model 前缀，否则留空由人工补）
            model = ""
            field = metric
            if "." in metric:
                parts = metric.split(".", 1)
                model, field = parts[0], parts[1]
            candidate = ValueAliasCandidate(
                term=corrected,
                model=model,
                field=field,
                source_feedback=rel_path,
                source_kind=kind,
                question=question,
                comment=comment,
            )
            val_count += 1
            if not dry:
                val_out.mkdir(parents=True, exist_ok=True)
                target = val_out / f"val_{sha}-{sample_id}.json"
                target.write_text(
                    json.dumps(asdict(candidate), ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )

    return {
        "synonym_candidates": syn_count,
        "value_alias_candidates": val_count,
        "skipped_no_correction": skipped_no_correction,
        "skipped_not_approved": skipped_not_approved,
    }
