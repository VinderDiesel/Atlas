"""eval.report 契约测试（Day 39，2026-09-03 CI 修复后 fixture 化）。

锁定规则（任务清单 Day 39 验收）：
1. 输出不含「待填写」（旧模板占位必须清零）；
2. 报告齐全时，每行表格值的 source 列引用的文件必须真实存在；
3. 缺失报告显示占位文案，不推断数值（AGENTS.md 9.3）；
4. 报告绑定当前 git sha，只聚合当前 sha 的报告；
5. blocked 行如实保留（不删除不美化，AGENTS.md）。

渲染在注入的 fixture 报告域内执行（setUpClass patch REPORTS_DIR/FAILURES_DIR）：
不依赖仓库内「当前 sha 报告已出」的磁盘状态——CI 干净 checkout 无报告时
契约同样可复现（2026-09-03 lint.yml 失败根因之一：把报告产物状态耦合进了
单元测试；真实仓库无 HEAD 报告时的占位路径由 test_missing_report_placeholder
与 test_real_repo_without_reports_stays_placeholder 锁定）。
"""

from __future__ import annotations

import io
import json
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

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


class _ReportFixture(unittest.TestCase):
    """把渲染域指向临时 reports/failures（报告齐全场景的 fixture）。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        root = Path(cls._tmp.name)
        reports = root / "reports"
        reports.mkdir()
        (root / "failures").mkdir()  # §7：空 failures 域 → 渲染「无样本」声明行
        cls.reports = reports
        cls.sha = git_short_sha()
        cls._write_fixtures(reports)
        cls._patchers = [
            mock.patch.object(report_mod, "REPORTS_DIR", reports),
            mock.patch.object(report_mod, "FAILURES_DIR", root / "failures"),
        ]
        for p in cls._patchers:
            p.start()

    @classmethod
    def tearDownClass(cls) -> None:
        for p in cls._patchers:
            p.stop()
        cls._tmp.cleanup()

    @classmethod
    def _write_fixtures(cls, reports: Path) -> None:
        """每类报告各一份最小合法 JSON（字段与各 _section_* 读取路径一致）。"""
        sha = cls.sha
        payloads: dict[str, dict] = {
            f"{sha}.json": {
                "created_at": "2026-09-03T10:00:00+08:00",
                "summary": {
                    "finance_total": 48,
                    "retail_skipped": 2,
                    "plan_acc": "44/44",
                    "clarify": "4/4",
                    "ex": "44/44",
                    "ex_anchored": 0,
                    "exec_errors": 0,
                },
            },
            f"baseline-compiler-{sha}.json": {
                "analysis": {
                    "deterministic_coverage": "40/44",
                    "plan_hit": "40/44",
                    "clarify": "4/4",
                    "ex": "44/44",
                    "exec_errors": 0,
                },
                "conclusion": "确定性链覆盖全部非歧义样本",
            },
            f"compare-4way-{sha}.json": {
                "table": [
                    {
                        "strategy": "lora",
                        "status": "blocked",
                        "ex": "-",
                        "plan_acc": "-",
                        "clarify": "-",
                        "token_total": "-",
                        "latency_mean_ms": "-",
                        "cost_usd_est": "-",
                        "refuse_rate_clear": "-",
                    }
                ]
            },
            f"rag-llm-stub-{sha}.json": {
                "engine": "stub",
                "summary": {
                    "non_ambiguous": 12,
                    "ambiguous": 2,
                    "plan_acc": "12/12",
                    "clarify": "2/2",
                    "ex": "-",
                    "refused_on_clear": 0,
                    "exec_errors": 0,
                    "total_tokens": 0,
                    "mean_latency_ms": 0.0,
                    "cost_usd_est": 0.0,
                },
            },
            f"schema-link-bm25-{sha}.json": {
                "metrics": {"recall_at_1": "44/44", "recall_at_5": "44/44", "fail_cases": 0}
            },
            f"retrieval-rerank-{sha}.json": {
                "metrics": {"recall_at_1": "44/44", "recall_at_5": "44/44", "fail_cases": 0}
            },
            f"retrieval-bm25-{sha}.json": {
                "metrics": {"recall_at_1": "41/44", "recall_at_5": "42/44", "fail_cases": 3}
            },
            f"retrieval-fuse-{sha}.json": {
                "metrics": {"recall_at_1": "35/44", "recall_at_5": "40/44", "fail_cases": 9}
            },
            f"p1-chain-{sha}.json": {
                "question": "查佣金收入",
                "gates": "五道全过",
                "malicious_gate": {"blocked": 10, "total": 10, "all_blocked": True},
            },
            f"rls-verify-{sha}.json": {
                "question": "三角色行级",
                "roles": {
                    "analyst": {"row_count": 100},
                    "compliance": {"row_count": 50},
                    "ops": {"row_count": 50},
                },
            },
            f"polaris-rbac-{sha}.json": {"analyst_principal": "atlas_analyst"},
            f"metrics-verify-{sha}.json": {"status": "pass"},
        }
        for name, data in payloads.items():
            (reports / name).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


class TestReportContract(_ReportFixture):
    def test_header_binds_sha(self) -> None:
        """头部绑定 git sha（报告可追溯的根）。"""
        out = _render()
        self.assertIn(f"绑定 git sha：`{self.sha}`", out)

    def test_no_placeholder_digits(self) -> None:
        """输出不含「待填写」（任何未测数字都必须占位而非编造）。"""
        out = _render()
        self.assertNotIn("待填写", out)

    def test_source_files_exist(self) -> None:
        """报告齐全时，表格 source 列引用的文件必须真实存在（机械可核对性）。"""
        out = _render()
        sources = set(re.findall(r"`(?:eval/reports/)?([\w.-]+\.json)`", out))
        for src in sources:
            # §8 的清单行不算表格引用；以 sha 结尾或 sha.json 形态才算
            if src == f"{self.sha}.json" or src.endswith(f"-{self.sha}.json"):
                self.assertTrue((self.reports / src).exists(), f"source 文件缺失：{src}")

    def test_missing_report_placeholder(self) -> None:
        """缺失报告 → 占位文案而非空/猜数。"""
        for section in (
            report_mod._section_main("0" * 7),
            report_mod._section_compare("0" * 7),
            report_mod._section_baseline("0" * 7),
        ):
            joined = "\n".join(section)
            self.assertIn("缺失", joined)

    def test_real_repo_without_reports_stays_placeholder(self) -> None:
        """真实仓库无当前 sha 报告时渲染仍为占位（CI 干净 checkout 场景）。

        2026-09-03 回归锁：报告产物在本地生成后入库，commit 前进期间仓库内
        报告 sha 落后于 HEAD 是常态——此时渲染必须是占位而非失败或编数。
        """
        with mock.patch.object(report_mod, "REPORTS_DIR", REPORTS_DIR):
            out = _render()
        if not (REPORTS_DIR / f"{git_short_sha()}.json").exists():
            self.assertIn("缺失", out)

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
