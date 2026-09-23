"""显式捕获正文组装（ADR-0031 D13）：只物化白名单内且实际发生的字段。

单一投影原则
------------
正文一律从该轮安全 TurnPayload（`_turn_payload` 的 JSON 化产物）取材，不从原始
TurnResult 二次转换——原始对象可能含 tuple 等非 JSON 值（如结构化 explanation），
二次转换会与 result 投影产生两份漂移。

边界（诚实与安全）
------------------
- `question`：该轮真实问句（execute_plan 缺省时为 Plan 规范化文本，与 turn 同源）；
- `node_io`：**只在作答（answer）时物化** {sql, explanation}——被拒/失败的 SQL
  不进持久化（D07：内部持久化不保存被拒 SQL 原文）；
- `result`：该轮安全 TurnPayload 全文（与 GET RunView 的 result 同源）；
- 未授权字段不因「内容可用」而落盘；无可物化内容时返回空 dict，调用方不建空制品。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from pydantic import JsonValue

if TYPE_CHECKING:
    from serving.control.contracts import CaptureGrant


def capture_content(grant: CaptureGrant, payload: dict[str, Any]) -> dict[str, JsonValue]:
    """按授权字段从 TurnPayload 投影组装捕获正文（D13）。

    Parameters
    ----------
    grant : 显式保留授权（fields 是内容硬边界）
    payload : 该轮安全 TurnPayload（已 JSON 化；与 RunView.result 同源）

    Returns
    -------
    dict[str, JsonValue] : 键 ⊆ grant.fields；无可物化内容时为空 dict
    """
    content: dict[str, JsonValue] = {}
    question = payload.get("question")
    if "question" in grant.fields and isinstance(question, str) and question:
        content["question"] = question
    if "node_io" in grant.fields and payload.get("kind") == "answer":
        content["node_io"] = {
            "sql": cast("JsonValue", payload.get("sql")),
            "explanation": cast("JsonValue", payload.get("explanation")),
        }
    if "result" in grant.fields:
        content["result"] = cast("JsonValue", payload)
    return content
