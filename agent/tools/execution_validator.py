"""执行结果校验器（Day 33）：空结果 / 全 NULL / 行数异常 → 结构化问题报告。

设计（确定性优先，与评测口径对齐）
----------------------------------
- 输入是执行器返回的 rows/columns（与 eval/runner.py execute_sql 的返回同构）。
- 输出 ValidationResult：ok=False 时附带 issue 列表，供上层触发修正
  （候选回退 / LLM 重生成提示 / 澄清），本模块不做决策。
- 判定规则（MVP，如实声明）：
  - empty_result：0 行（含聚合查询无匹配）
  - all_null_column：任一列全部 NULL（聚合断裂的典型信号）
  - unexpected_multi_row：非分组查询（dimensions 为空）返回 >1 行——
    单行聚合查询多行 = 缺少 GROUP BY 的口径异常
- 分组查询按行数阈值异常（超 LIMIT 等）由 Guard 的 max_rows 承接，不在此重复。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ValidationResult:
    """校验结论：ok=False 时 issues 说明全部问题。"""

    ok: bool
    issues: tuple[str, ...] = field(default_factory=tuple)

    def __bool__(self) -> bool:
        return self.ok


class ExecutionValidator:
    """对执行结果做确定性健康检查（不执行 SQL，不做数值语义判断）。"""

    def check(
        self,
        rows: list[tuple[Any, ...]],
        columns: list[str],
        grouped: bool = False,
    ) -> ValidationResult:
        """校验结果集。

        参数
        ----
        rows : 执行器返回的行（与 eval/runner.execute_sql 同构）
        columns : 列名列表
        grouped : 查询是否含 GROUP BY（分组查询允许多行；单行聚合期望 1 行）

        返回
        ----
        ValidationResult：ok=True 表示无已知异常形态；否则 issues 说明。
        """
        issues: list[str] = []
        if not rows:
            issues.append("empty_result")
        elif not grouped and len(rows) > 1:
            issues.append("unexpected_multi_row")
        if rows and columns:
            for idx, col in enumerate(columns):
                # 列数可能少于 rows 宽度（防御，不 panic）
                if idx >= len(rows[0]):
                    continue
                if all(row[idx] is None for row in rows):
                    issues.append(f"all_null_column:{col}")
        return ValidationResult(ok=not issues, issues=tuple(issues))
