"""eval.report 契约测试（Day 39，2026-09-03 CI 修复后 fixture 化）。

锁定规则（任务清单 Day 39 验收）：
1. 输出不含「待填写」（旧模板占位必须清零）；
2. 报告齐全时，每行表格值的 source 列引用的文件必须真实存在；
3. 缺失报告显示占位文案，不推断数值（AGENTS.md 9.3）；
4. 报告绑定当前 git sha，只聚合当前 sha 的报告；
5. blocked 行如实保留（不删除不美化，AGENTS.md）；
6. §8 概念层一致率数字 = concept-<sha>.json 产物原文；头部数据快照引用 =
   真实存在的最新 meta（不再硬编码 HEAD sha 假引用；ADR-0032 M1-T5）；
7. §9 来源清单只列真实存在的文件（主报告缺失时不产生幻影条目；与规则 6 同批修复）。

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
        # 显式空 argv：main() 内部 argparse 若不传参会读 sys.argv，
        # unittest discover 的 `-s tests` 会被当作未知参数拒收（pytest 下恰好不现）
        rc = report_mod.main([])
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
        snapshots = root / "snapshots"
        snapshots.mkdir()
        # 头部快照 fixture：aaa1111 新于 bbb2222 → 头部必须引用 aaa1111
        (snapshots / "bbb2222.meta.json").write_text(
            json.dumps({"created_at": "2026-01-01T00:00:00+08:00"}), encoding="utf-8"
        )
        (snapshots / "aaa1111.meta.json").write_text(
            json.dumps({"created_at": "2026-10-01T00:00:00+08:00"}), encoding="utf-8"
        )
        cls.reports = reports
        cls.sha = git_short_sha()
        cls._write_fixtures(reports)
        cls._patchers = [
            mock.patch.object(report_mod, "REPORTS_DIR", reports),
            mock.patch.object(report_mod, "FAILURES_DIR", root / "failures"),
            mock.patch.object(report_mod, "SNAPSHOT_DIR", snapshots),
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
                    # 2026-09-05 起 runner 报告 summary 按域分节（finance/retail 不混报），
                    # finance_total/retail_skipped 平铺键已不存在
                    "finance": {
                        "total": 48,
                        "plan_acc": "44/44",
                        "clarify": "4/4",
                        "ex": "44/44",
                        "ex_anchored": 0,
                        "exec_errors": 0,
                    },
                    "retail": {
                        "total": 2,
                        "plan_acc": "2/2",
                        "clarify": "0/0",
                        "ex": "2/2",
                        "ex_anchored": 0,
                        "exec_errors": 0,
                    },
                },
            },
            f"baseline-compiler-{sha}.json": {
                "analysis": {
                    "finance": {
                        "deterministic_coverage": "48/48",
                        "plan_hit": "44/44",
                        "clarify": "4/4",
                        "ex": "44/44",
                        "exec_errors": 0,
                    },
                    "retail": {
                        "deterministic_coverage": "2/2",
                        "plan_hit": "2/2",
                        "clarify": "0/0",
                        "ex": "2/2",
                        "exec_errors": 0,
                    },
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
            f"concept-{sha}.json": {
                "stage": "matcher",
                "layer_enabled": True,
                "created_at": "2026-09-03T10:00:00+08:00",
                "consistency": {"hit": 5, "mismatch": 1, "miss": 0, "rate": "5/6"},
                "stats": {
                    "samples_annotated": 5,
                    "terms_total": 6,
                    "concepts_distinct": 4,
                    "registry_coverage": {"not_in_registry": 0},
                },
                "notes": "三分口径：hit/mismatch/miss；rate = hit/标注总数（fixture 转述）。",
            },
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
            report_mod._section_concept("0" * 7),
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


class TestConceptSection(_ReportFixture):
    """§8 概念层一致率（ADR-0032 M1-T5）：渲染值 = 产物原文机械转述。"""

    def test_section_renders_artifact_values(self) -> None:
        out = _render()
        self.assertIn("## 8. 概念层一致率", out)
        self.assertIn(f"`concept-{self.sha}.json`", out)
        self.assertIn("| stage | matcher |", out)
        self.assertIn("| layer_enabled | True |", out)
        self.assertIn("| hit | 5 |", out)
        self.assertIn("| rate | 5/6 |", out)

    def test_meta_renumbered_to_9(self) -> None:
        """来源清单重编号为 §9（概念节顶替 §8；2026-10-14 两处机械改）。"""
        out = _render()
        self.assertIn("## 9. 报告来源清单", out)
        self.assertNotIn("## 8. 报告来源清单", out)

    def test_layer_off_warning_rendered(self) -> None:
        """layer_enabled=false → 渲染无层 0 命中基线声明（防误读为全错）。"""
        payload = {
            "stage": "matcher",
            "layer_enabled": False,
            "created_at": "2026-10-14T00:00:00+08:00",
            "consistency": {"hit": 0, "mismatch": 0, "miss": 6, "rate": "0/6"},
            "stats": {"registry_coverage": {"not_in_registry": 0}},
            "notes": "无层全 miss（fixture）。",
        }
        with mock.patch.object(report_mod, "_load", return_value=payload):
            joined = "\n".join(report_mod._section_concept("0" * 7))
        self.assertIn("layer_enabled=false（无层）", joined)
        self.assertIn("0 命中基线", joined)


class TestSnapshotRef(_ReportFixture):
    """头部数据快照引用：按 created_at 取最新真实 meta（诚实红线）。"""

    def test_header_shows_latest_real_meta(self) -> None:
        out = _render()
        self.assertIn("aaa1111.meta.json", out)
        self.assertNotIn("bbb2222", out)
        self.assertNotIn(f"snapshots/{self.sha}.meta.json", out)

    def test_missing_snapshots_placeholder(self) -> None:
        with mock.patch.object(report_mod, "SNAPSHOT_DIR", REPORTS_DIR / "no-snapshots"):
            ref = report_mod._snapshot_ref()
        self.assertIn("缺失", ref)


class TestMetaListing(_ReportFixture):
    """§9 来源清单只列真实存在的文件（幻影条目 = 诚实性缺陷，2026-10-14 修）。"""

    def test_main_report_omitted_when_missing(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / f"concept-{'0' * 7}.json").write_text("{}", encoding="utf-8")
            with mock.patch.object(report_mod, "REPORTS_DIR", root):
                joined = "\n".join(report_mod._section_meta("0" * 7))
        lines = joined.splitlines()
        self.assertIn("concept-0000000.json", lines)
        self.assertNotIn("0000000.json", lines)

    def test_main_report_listed_when_present(self) -> None:
        lines = "\n".join(report_mod._section_meta(self.sha)).splitlines()
        self.assertIn(f"{self.sha}.json", lines)


if __name__ == "__main__":
    sys.exit(unittest.main())
