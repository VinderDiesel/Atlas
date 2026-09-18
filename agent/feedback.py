"""用户纠错反馈入口（Day 46）：一键纠错 → 修正日志（人工确认后进数据飞轮）。

口径（与 eval/failures/README.md 数据飞轮对齐）
------------------------------------------------
- 用户反馈与评测失败是**两个入口、同一归集**：评测失败由 failure_collect
  自动归集（question → 报告样本）；用户纠错是运行时人工信号——都先落
  status=pending_review，**人工确认后才可进 lora/data/approved_pairs.jsonl**
  （AGENTS.md：失败样本先归集、人工确认后才可进 SFT，任何失败样本不得在
  未人工确认时进入训练数据）。
- 默认落盘 eval/failures/user_feedback/（与评测归类目录平级，不混入
  failure_collect 的输出目录，避免自动工具误读用户反馈为评测失败）。
- 字段如实记录回合快照（question/sql/metric/path/engine/turns），不带
  任何推断结论；comment 是用户原话（非空则存，空串=纯一键纠错）。
- 只写不删不覆盖（时间戳+uuid 文件名），人工归档与审计轨迹分离。

时间一律 UTC ISO 8601（AGENTS.md 7.3：日期 ISO 8601 + 显式时区）。
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

FeedbackKind = Literal[
    "wrong_value",  # 数值不对（聚合口径/数据问题）
    "wrong_metric",  # 指标选错
    "wrong_time",  # 时间范围理解错
    "misunderstood",  # 问东答西（问题理解错）
    "other",
]

REPO = Path(__file__).resolve().parent.parent
DEFAULT_FEEDBACK_DIR = REPO / "eval" / "failures" / "user_feedback"

# kind 合法值（可读校验错误消息）
_KINDS: tuple[str, ...] = ("wrong_value", "wrong_metric", "wrong_time", "misunderstood", "other")


@dataclass(frozen=True)
class FeedbackSubmission:
    """一次用户纠错：回合快照 + 用户信号（写盘前校验，不编造字段）。"""

    question: str
    kind: FeedbackKind
    comment: str = ""
    session_id: str = ""
    metric: str | None = None
    sql: str | None = None
    path: str | None = None
    engine: str | None = None
    corrected_term: str = ""
    turns_in_session: int = 1
    submitted_at: str = ""


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _validated(submission: FeedbackSubmission) -> FeedbackSubmission:
    if not submission.question.strip():
        raise ValueError("feedback question 不能为空")
    if submission.kind not in _KINDS:
        raise ValueError(f"feedback kind 必须是 {_KINDS} 之一，收到 {submission.kind!r}")
    if not submission.submitted_at:
        submission = FeedbackSubmission(**{**asdict(submission), "submitted_at": _now_iso()})
    return submission


def submit_feedback(
    submission: FeedbackSubmission,
    feedback_dir: Path = DEFAULT_FEEDBACK_DIR,
) -> Path:
    """把纠错信号写入修正日志（pending_review），返回落盘路径。

    参数
    ----
    submission  : 纠错内容（question/kind 必填；submitted_at 缺省补 UTC 当前）。
    feedback_dir: 修正日志目录（默认 eval/failures/user_feedback/；测试注入）。

    返回
    ----
    Path：写入的 JSON 文件路径（不覆盖已存在文件）。

    异常
    ----
    ValueError：question 为空或 kind 不在枚举内。
    """
    submission = _validated(submission)
    feedback_dir.mkdir(parents=True, exist_ok=True)
    name = f"{submission.submitted_at.replace(':', '')}_{uuid4().hex[:8]}.json"
    # 统一 pending 形态（ADR-0027 决策 ④）：与 failure_collect 同构，
    # 下游 export_approved 只读 payload["sample"] 一套 schema。
    # 历史 user_feedback（顶层 question/kind）由 export_approved 兼容读取。
    payload = {
        "schema_version": 2,
        "status": "pending_review",
        "origin": "user",
        "sample": asdict(submission),
    }
    path = feedback_dir / name
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _main() -> None:
    """CLI 兜底入口（服务端未接入前可手工喂）：从 stdin 读 JSON。

    echo '{"question": "...", "kind": "wrong_metric", "comment": "..."}' \\
        | uv run python -m agent.feedback
    """
    raw = sys.stdin.read().strip()
    if not raw:
        print("用法：stdin 传入 JSON {question, kind[, comment, session_id, ...]}")
        sys.exit(2)
    try:
        submission = FeedbackSubmission(**json.loads(raw))
    except (json.JSONDecodeError, TypeError) as exc:
        print(f"JSON 解析失败：{exc}")
        sys.exit(2)
    path = submit_feedback(submission)
    print(f"已写入修正日志：{path}")


if __name__ == "__main__":
    _main()
