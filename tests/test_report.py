"""eval.report 契约测试（Day 39）：EVAL_REPORT.md 无手写数字。

锁定规则（任务清单 Day 39 验收）：
1. 输出不含「待填写」（旧模板占位必须清零）；
2. 每行表格值的 source 列引用的文件必须真实存在；
3. 缺失报告显示占位文案，不推断数值（AGENTS.md 9.3）；
4. 报告绑定当前 git sha，只聚合当前 sha 的报告。
"""

from __future__ import annotations

import io
import re
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from eval import report as report_mod
from eval.runner import git_short_sha

REPORTS_DIR = Path(__file__).resolve().parent.parent / "eval" / "reports"


def _render() -> str:
    """捕获 main() 的 stdout 输出。"""
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = report_mod.main()
    assert rc == 0
    return buf.getvalue()


class TestReportContract(unittest.TestCase):
    def test_header_binds_sha(self) -> None:
        """头部绑定 git sha（报告可追溯的根）。"""
        out = _render()
        self.assertIn(f"绑定 git sha：`{git_short_sha()}`", out)

    def test_no_placeholder_digits(self) -> None:
        """输出不含「待填写」（任何未测数字都必须占位而非编造）。"""
        out = _render()
        self.assertNotIn("待填写", out)

    def test_source_files_exist(self) -> None:
        """表格 source 列引用的文件必须真实存在（无手写数字的机械可核对性）。"""
        out = _render()
        sources = set(re.findall(r"`(?:eval/reports/)?([\w.-]+\.json)`", out))
        for src in sources:
            # §8 的清单行不是表格引用；文件名以 sha 结尾或 sha.json 形态才算引用
            if src == f"{git_short_sha()}.json" or src.endswith(f"-{git_short_sha()}.json"):
                self.assertTrue((REPORTS_DIR / src).exists(), f"source 文件缺失：{src}")

    def test_missing_report_placeholder(self) -> None:
        """缺失报告 → 占位文案而非空/猜数。"""
        for section in (
            report_mod._section_main("0" * 7),
            report_mod._section_compare("0" * 7),
            report_mod._section_baseline("0" * 7),
        ):
            joined = "\n".join(section)
            self.assertIn("缺失", joined)

    def test_only_current_sha_reports(self) -> None:
        """只聚合当前 sha 报告：历史 sha（如 b47a6c1）不得出现。"""
        out = _render()
        self.assertNotIn("b47a6c1", out)

    def test_lora_blocked_row_kept(self) -> None:
        """对比表 blocked 行如实保留（不删除不美化，AGENTS.md）。"""
        out = _render()
        self.assertIn("blocked", out)


if __name__ == "__main__":
    sys.exit(unittest.main())
