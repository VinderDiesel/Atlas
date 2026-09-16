"""端到端演示测试（README「快速开始」的可执行版；demo = 集成测试形态，P7）。

定位
----
demo 不是花哨脚本：以 pytest/unittest 形态锁定「按 README 跑通 Atlas」的最小
演示集——中英问句全链路（Planner → Compiler → Guard → Doris 执行，由 DataAgent
一次 ask 完成，engine=stub 确定性）双域各一组 + 带身份（RLS）两例。问句全部
直抄已锚定 gold 样本（同 Plan 同 SQL，结果可预期），语言走自动检测（P6）。

断言口径（数字只来自实测，机械转述）
------------------------------------
- 标量聚合（year/quarter/month/date 单值问句）→ rows == 1；
- TopN（top 5/3）→ rows == N（limit 语义）；
- gold-170（2013 佣金超 1000 万 Top5）→ rows == 0：2013 实测无分支佣金超
  1000 万（空结果集是数据事实，P5/P6 EX 锚定 hash e3b0c442… 同证）；
- 相对时间歧义 → kind=clarify（不猜答）；
- 带身份两例与 serving/rls_verify.py 同机制（resolve_policy → Guard Policy
  注入 → Doris 执行），零售 claims 值取 data-profile.md 实测值域：
  region_manager（州=TN）与 hq_admin 结果一致是**单州数据事实**（如实断言，
  不伪造差异）；差异由 category_analyst（2 品类 vs 全量 10 品类）承担。

环境（缺任一 → skip，消息注明复现步骤）
----------------------------------------
1. Doris 可达（DORIS_HOST/DORIS_PORT，同 eval.runner 环境约定，docker compose up）
2. 当前 HEAD 已锁快照 meta（data/snapshots/<sha>.meta.json —— make seed 锁定）
3. ATLAS_JWT_SECRET 已设置（仅带身份两例需要；未设置时这两例单独 skip）

用法（仓库根）：
    uv run python -m unittest tests.test_demo_e2e
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import unittest
from pathlib import Path
from uuid import uuid4

REPO = Path(__file__).resolve().parent.parent

# 环境探测：Doris TCP 可达（2s 快速探测，不依赖 mysql 握手）
def _doris_reachable() -> bool:
    host = os.environ.get("DORIS_HOST", "127.0.0.1")
    port = int(os.environ.get("DORIS_PORT", "9030"))
    try:
        with socket.create_connection((host, port), timeout=2):
            return True
    except OSError:
        return False


def _head_sha() -> str:
    """工作树的真实 HEAD 短 sha —— **刻意保留为独立预言机**（ADR-0019 决策 ② 测试侧裁定）。

    不改用 `data.identity.git_short_sha`：那个函数优先读 `ATLAS_GIT_SHA`（容器里由
    Dockerfile 注入镜像构建时的 sha），而本文件的用法是「当前 HEAD 是否已锁快照」
    这类 skip 判据与读 meta —— 注入的 sha 可能不等于工作树 HEAD，一旦不一致，
    用例就会对着错误的快照判定通过。目录路径（`eval.runner.SNAPSHOT_DIR`）复用无妨，
    需要独立的只有 sha 计算本身。
    """
    return (
        subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=REPO,
            capture_output=True,
            text=True,
            check=True,
        )
        .stdout.strip()
    )


def _snapshot_locked() -> bool:
    return (REPO / "data" / "snapshots" / f"{_head_sha()}.meta.json").is_file()


def _jwt_secret_set() -> bool:
    return bool(os.environ.get("ATLAS_JWT_SECRET", "").strip())


# 问句（直抄 gold 样本；= 锚定 SQL 的重放）
FIN_COMMISSION_TOP5 = "按分支统计 2013 年佣金收入，列出前 5 名"  # gold-102
FIN_Q2_TV = "2013 年第二季度总交易额是多少？"  # gold-101
FIN_AMBIGUOUS = "最近交易情况怎么样？"  # gold-104（相对时间 → 反问）
FIN_EN_TV_2015 = "What was the total trade value in 2015?"  # gold-163
FIN_EN_MAY_TRADES = "How many trades were there in May 2014?"  # gold-166
FIN_EN_OVER_10M = (  # gold-170：2013 实测无分支超 1000 万 → 空结果集
    "List the top 5 branches by commission revenue over 10 million in 2013"
)
RETAIL_YEAR_SALES = "2000 年总销售额是多少？"  # gold-051
RETAIL_TOP3 = "2000 年按品类统计销售额，列出前 3 名"  # gold-058
RETAIL_MIDWAY = "只看城市 Midway 的 2000 年销售额是多少？"  # gold-059
RETAIL_EN_1999 = "What were total sales in 1999?"  # gold-063
RETAIL_EN_TOP3 = "What were the top 3 categories by sales in 1999?"  # gold-065
RETAIL_EN_Q1 = "What were the total sales in Q1 1999?"  # gold-066

# RLS 载体（与 rls-verify 零售档同问句：同时 join dim_store + dim_item，
# 谓词列天然可达）与 claims 值（data-profile.md 实测值域，勿手改）
RLS_QUESTION = "2000 年按门店城市和品类统计销售额，列出前 3 名"
RLS_REGION = "TN"  # SF0.1 全库单州（12/12）
RLS_CATEGORIES = ["Shoes", "Electronics"]  # 实测 10 品类中取 2


@unittest.skipUnless(
    _doris_reachable() and _snapshot_locked(),
    "Doris 不可达或当前 HEAD 无锁定快照 meta：先 docker compose up + make seed 锁定快照",
)
class TestDemoE2E(unittest.TestCase):
    """真实 Doris 双域演示（finance/retail 各一 DataAgent，create_live_agent 同源）。"""

    @classmethod
    def setUpClass(cls) -> None:
        from agent.compiler import SemanticModel
        from agent.factory import create_live_agent

        cls.finance = create_live_agent()
        cls.retail = create_live_agent(
            model_path=REPO / "semantic" / "ossie" / "atlas_retail.ossie.yaml"
        )
        # 快照预算与执行器（RLS 用例复用；与 create_live_agent 同 meta）
        from eval.runner import SNAPSHOT_DIR, build_budget

        meta = json.loads(
            (SNAPSHOT_DIR / f"{_head_sha()}.meta.json").read_text(encoding="utf-8")
        )
        cls.budget = build_budget(meta)
        cls.retail_model = SemanticModel(
            REPO / "semantic" / "ossie" / "atlas_retail.ossie.yaml"
        )

    def _ask(self, agent, question: str):
        # 每问句独立会话（sid 逐例随机，多轮状态不跨例串扰）
        return agent.ask(question, session_id=f"demo-{uuid4().hex[:8]}")

    # ---- 中文 6 条（金融 3 + 零售 3）------------------------------------

    def test_finance_zh_commission_top5_by_branch(self) -> None:
        turn = self._ask(self.finance, FIN_COMMISSION_TOP5)
        self.assertEqual(turn.kind, "answer")
        self.assertEqual(turn.metric, "commission_revenue")
        self.assertEqual(turn.row_count, 5)
        self.assertIn("dim_broker", turn.sql or "")

    def test_finance_zh_quarter_total_trade_value(self) -> None:
        turn = self._ask(self.finance, FIN_Q2_TV)
        self.assertEqual(turn.kind, "answer")
        self.assertEqual(turn.metric, "total_trade_value")
        self.assertEqual(turn.row_count, 1)

    def test_finance_zh_relative_time_clarifies(self) -> None:
        """相对时间歧义 → 反问（确定性链路不猜答）。"""
        turn = self._ask(self.finance, FIN_AMBIGUOUS)
        self.assertEqual(turn.kind, "clarify")
        self.assertIsNotNone(turn.clarification)

    def test_retail_zh_year_total_sales(self) -> None:
        turn = self._ask(self.retail, RETAIL_YEAR_SALES)
        self.assertEqual(turn.kind, "answer")
        self.assertEqual(turn.metric, "total_sales_price")
        self.assertEqual(turn.row_count, 1)
        self.assertIn("store_sales", turn.sql or "")

    def test_retail_zh_top3_categories(self) -> None:
        turn = self._ask(self.retail, RETAIL_TOP3)
        self.assertEqual(turn.kind, "answer")
        self.assertEqual(turn.row_count, 3)
        self.assertEqual(len(turn.columns or []), 2)  # i_category + 度量

    def test_retail_zh_city_filter(self) -> None:
        turn = self._ask(self.retail, RETAIL_MIDWAY)
        self.assertEqual(turn.kind, "answer")
        self.assertEqual(turn.row_count, 1)
        self.assertIn("Midway", turn.sql or "")

    # ---- 英文 6 条（金融 3 + 零售 3，语言自动检测）----------------------

    def test_finance_en_year_total_trade_value(self) -> None:
        turn = self._ask(self.finance, FIN_EN_TV_2015)
        self.assertEqual(turn.kind, "answer")
        self.assertEqual(turn.metric, "total_trade_value")
        self.assertEqual(turn.row_count, 1)

    def test_finance_en_may_2014_trade_count(self) -> None:
        turn = self._ask(self.finance, FIN_EN_MAY_TRADES)
        self.assertEqual(turn.kind, "answer")
        self.assertEqual(turn.metric, "trade_count")
        self.assertEqual(turn.row_count, 1)

    def test_finance_en_over_10m_top5_empty_result(self) -> None:
        """诚实空结果：2013 无分支佣金超 1000 万 → answer 且 rows=0（数据事实）。"""
        turn = self._ask(self.finance, FIN_EN_OVER_10M)
        self.assertEqual(turn.kind, "answer")
        self.assertEqual(turn.metric, "commission_revenue")
        self.assertEqual(turn.row_count, 0)

    def test_retail_en_1999_total_sales(self) -> None:
        turn = self._ask(self.retail, RETAIL_EN_1999)
        self.assertEqual(turn.kind, "answer")
        self.assertEqual(turn.metric, "total_sales_price")
        self.assertEqual(turn.row_count, 1)

    def test_retail_en_top3_categories(self) -> None:
        turn = self._ask(self.retail, RETAIL_EN_TOP3)
        self.assertEqual(turn.kind, "answer")
        self.assertEqual(turn.row_count, 3)

    def test_retail_en_q1_1999(self) -> None:
        turn = self._ask(self.retail, RETAIL_EN_Q1)
        self.assertEqual(turn.kind, "answer")
        self.assertEqual(turn.row_count, 1)

    # ---- 带身份 2 条（RLS：与 rls-verify 同机制）------------------------

    def _rls_outcome(self, role: str, claims: dict, sql: str, budget):
        """token → 策略解析 → Guard 注入 → Doris 执行（rls-verify 同链路）。"""
        from agent.security.sql_guard import Policy, enforce
        from serving.auth import resolve_policy, sign_token

        token = sign_token(role, claims)
        resolved = resolve_policy(token, policy_name="rp_dept_visible")
        guarded, _ = enforce(
            sql, policy=Policy(name=resolved.policy_name, condition=resolved.condition), budget=budget
        )
        from eval.runner import execute_sql  # 模块级调用（类属性存函数会被实例绑定成方法）

        rows, _columns = execute_sql(guarded)
        return resolved.condition, guarded, rows

    def _rls_sql(self) -> str:
        from agent.compiler import Compiler, Plan
        from agent.planner import Planner

        plan = Planner(self.retail_model).plan(RLS_QUESTION)
        self.assertIsInstance(plan, Plan)
        assert isinstance(plan, Plan)  # 类型收窄
        sql, _ = Compiler(self.retail_model).compile(plan)
        return sql

    def _rls_skip_if_no_secret(self) -> None:
        """运行时自检 JWT 密钥（装饰器在 import 期求值不可靠：test_api 先设后弹）。"""
        if not _jwt_secret_set():
            self.skipTest("ATLAS_JWT_SECRET 未设置（带身份用例需要）")

    def test_rls_region_manager_single_state_fact(self) -> None:
        """region_manager（州=TN）：谓词注入生效；结果与 hq_admin 一致（单州数据事实）。"""
        self._rls_skip_if_no_secret()
        sql = self._rls_sql()
        hq_cond, hq_sql, hq_rows = self._rls_outcome("hq_admin", {}, sql, self.budget)
        cond, guarded, rows = self._rls_outcome(
            "region_manager", {"region": RLS_REGION}, sql, self.budget
        )
        self.assertEqual(cond, "dim_store.s_state = 'TN'")
        self.assertIn("dim_store.s_state = 'TN'", guarded)
        self.assertEqual(hq_cond, "1=1")
        self.assertNotIn("s_state", hq_sql)  # hq_admin 谓词 1=1：无行级过滤
        # SF0.1 全库单州：州谓词无过滤效果（数据事实，如实断言，不伪造差异）
        self.assertEqual(rows, hq_rows)

    def test_rls_category_analyst_restricted_difference(self) -> None:
        """category_analyst（TN + 2 品类）：结果与 hq_admin 不同（品类受限差异）。"""
        self._rls_skip_if_no_secret()
        sql = self._rls_sql()
        _hq_cond, _hq_sql, hq_rows = self._rls_outcome("hq_admin", {}, sql, self.budget)
        cond, guarded, rows = self._rls_outcome(
            "category_analyst",
            {"region": RLS_REGION, "categories": RLS_CATEGORIES},
            sql,
            self.budget,
        )
        self.assertIn("s_state = 'TN'", cond)
        self.assertIn("IN ('Shoes', 'Electronics')", guarded)
        self.assertTrue(hq_rows, "hq_admin 应返回数据")
        self.assertTrue(rows, "category_analyst 应返回数据")
        # 受限品类集不含全量 Top1 之外的高销品类 → 结果必不同（实测差异集 >= 2）
        self.assertNotEqual(rows, hq_rows)
        categories = {row[0] for row in rows}
        self.assertTrue(categories <= set(RLS_CATEGORIES), f"越权品类：{categories}")


if __name__ == "__main__":
    unittest.main()
