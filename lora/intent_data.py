"""T14 intent_v1 数据准备：DatasetManifest → 训练样本。

职责
----
- 从 DatasetManifest（T12）加载 reviewed 记录，转换为 intent adapter 训练样本。
- 数据格式：{question, answer (Plan JSON), loss_mask_prompt, source_family}。
- 拒绝规则：超长/无标签/泄漏样本不进入训练集。
- loss mask：prompt 部分 label=-100，只在 answer 部分计算 loss。

与 sql_v1 的区别
----------------
- sql_v1 的 answer 是完整 SQL；intent_v1 的 answer 是 Plan JSON。
- intent_v1 从 DatasetManifest（审核后的反馈标签）获取数据，不从 jsonl 文件。
- intent_v1 保留来源族信息，用于 split 隔离（D10）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# 默认最大长度（与 configs/intent_v1.yaml 的 max_length 对齐）
_DEFAULT_MAX_LENGTH = 2048


def validate_training_pair(
    pair: dict[str, Any],
    *,
    max_length: int = _DEFAULT_MAX_LENGTH,
) -> bool:
    """校验单条训练样本格式。

    拒绝条件：
    - question 为空
    - answer 为空或非合法 JSON
    - answer 缺少 metric 字段
    - question 超过 max_length

    Returns
    -------
    bool
        样本合法返回 True，否则 False。
    """
    q = str(pair.get("question", "")).strip()
    a = str(pair.get("answer", "")).strip()

    if not q:
        return False
    if not a:
        return False
    if len(q) > max_length:
        return False

    try:
        obj = json.loads(a)
    except (json.JSONDecodeError, TypeError):
        return False

    return isinstance(obj, dict) and "metric" in obj


def build_training_pairs(
    labels: list[Any],
    *,
    max_length: int = _DEFAULT_MAX_LENGTH,
) -> list[dict[str, Any]]:
    """从 ReviewedLabel 列表构建训练样本。

    转换规则：
    - verdict='corrected' 或 correction 非空 → 用 correction 作为 answer。
    - verdict='down' 且无 correction → 跳过（无标签不训练）。
    - 超长/无标签样本拒绝。

    Parameters
    ----------
    labels : list[ReviewedLabel]
        审核后的反馈标签列表（通常来自 DatasetManifest.records）。
    max_length : int
        问句最大长度，超出则拒绝。

    Returns
    -------
    list[dict]
        合法训练样本列表。
    """
    pairs: list[dict[str, Any]] = []

    for label in labels:
        correction = label.correction
        # 无标签（无 correction）→ 跳过
        if correction is None:
            continue

        answer = json.dumps(correction, ensure_ascii=False)
        pair = {
            "question": label.question,
            "answer": answer,
            "loss_mask_prompt": True,
            "source_family": label.source_family,
            "feedback_id": label.feedback_id,
        }

        if validate_training_pair(pair, max_length=max_length):
            pairs.append(pair)

    return pairs


def load_intent_dataset(
    manifest_path: Path,
    *,
    max_length: int = _DEFAULT_MAX_LENGTH,
) -> list[dict[str, Any]]:
    """从 DatasetManifest JSON 文件加载训练数据。

    Parameters
    ----------
    manifest_path : Path
        DatasetManifest JSON 文件路径（T12 build_intent_dataset 产物）。
    max_length : int
        问句最大长度。

    Returns
    -------
    list[dict]
        合法训练样本列表。
    """
    from serving.control.datasets import DatasetManifest

    if not manifest_path.exists():
        return []

    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    if data.get("record_count", 0) == 0:
        return []

    # 反序列化 manifest → records
    manifest = DatasetManifest.from_dict(data)
    labels = list(manifest.records)

    return build_training_pairs(labels, max_length=max_length)
