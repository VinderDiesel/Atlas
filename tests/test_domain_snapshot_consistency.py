#!/usr/bin/env python3
"""启动期「域 ↔ 快照」一致性校验（ADR-0019 决策 ③，判据 6，无 DB）。

失效形态（背景节实测复现）：容器身份注入 `7d48dcb`（组 2，单源 25 表，**无零售表**），
而代码里的 retail 语义模型声明 4 张表全不在该白名单内 —— `/health` 报 ok、金融域可用，
零售域**每一次**查询都被 Guard 以「表不在白名单内」拒绝，且该消息不指向根因
（真实原因是「绑错快照」，表面症状像「用户问了不该问的东西」）。本批实测的表集
差值（2026-09-14，与背景节独立复算一致）：

    retail required = 4 张    sha=7d48dcb 缺失 = 全部 4 张    sha=a11d779 缺失 = 0
    finance required = 8 张   两个 sha 均缺失 = 0

决策 ③ 把这条失效从「运行期逐次拒绝」提前为「构建 agent 即抛 `SnapshotUnavailable`
（HTTP 503 / CLI exit 1），消息直接列出缺失表与绑定的 sha」。

不触碰 Doris：只 patch `agent.factory.resolve_runtime_snapshot`（工厂把它 import 进
自己的命名空间，patch 模块属性即可），执行器不会被调用。
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import agent.factory as factory
from agent.compiler import FINANCE_MODEL
from data.identity import RuntimeSnapshot, SnapshotUnavailable

REPO_ROOT = Path(__file__).resolve().parent.parent
RETAIL_MODEL = REPO_ROOT / "semantic" / "ossie" / "atlas_retail.ossie.yaml"

# 真实已锁 meta（N6 允许的既有资产：测试读固定快照，不读活的数据库）
GROUP1_SHA = "a11d779"  # 组 1：TPC-DI + TPC-DS SF0.1，29 表
GROUP2_SHA = "7d48dcb"  # 组 2：TPC-DI 单源，25 表，无零售表
_CREATED_AT = "2026-09-09T11:56:23+08:00"

# retail 模型声明的 4 张表（实测来自模块 docstring 的那段差值计算）
RETAIL_TABLES = (
    "atlas.dwd.date_dim",
    "atlas.dwd.dim_item",
    "atlas.dwd.dim_store",
    "atlas.dwd.store_sales",
)


def _meta_from_locked(sha: str) -> dict[str, Any]:
    path = REPO_ROOT / "data" / "snapshots" / f"{sha}.meta.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _synthetic_meta(tables: tuple[str, ...], *, sha: str = "synthe1", rows: int = 10) -> dict:
    """按表全名（`atlas.<ns>.<table>`）合成最小 meta：只给 build_budget 需要的键。"""
    row_counts: dict[str, dict[str, int]] = {}
    for full in tables:
        _, ns, table = full.split(".", 2)
        row_counts.setdefault(ns, {})[table] = rows
    return {"sha": sha, "created_at": _CREATED_AT, "row_counts": row_counts}


def _build_with(meta: dict[str, Any], model_path: Path | None) -> Any:
    """把工厂的快照来源换成给定 meta，其余走真实代码路径（不 mock DataAgent）。"""
    snap = RuntimeSnapshot(sha=str(meta["sha"]), meta=meta, source="head", bound_to_head=True)
    with mock.patch.object(factory, "resolve_runtime_snapshot", return_value=snap):
        return factory.create_live_agent(model_path)


class TestGroupSnapshotsAgainstRealModels(unittest.TestCase):
    """判据 6 的字面要求：组 2 + retail 必抛且列出 4 张表；组 1 + retail 必成。"""

    def test_group2_meta_with_retail_model_raises_listing_all_four_tables(self) -> None:
        meta = _meta_from_locked(GROUP2_SHA)
        with self.assertRaises(SnapshotUnavailable) as ctx:
            _build_with(meta, RETAIL_MODEL)
        message = str(ctx.exception)
        for table in RETAIL_TABLES:
            self.assertIn(
                table,
                message,
                f"缺失表 {table} 未出现在错误消息里——本判据的全部价值就是让人不必"
                "再去猜「Guard 为什么拒」（ADR-0019 理由 3）",
            )

    def test_group1_meta_builds_retail_agent(self) -> None:
        agent = _build_with(_meta_from_locked(GROUP1_SHA), RETAIL_MODEL)
        self.assertEqual(agent.model.name, "atlas_retail_analytics")
        self.assertEqual(agent.snapshot_meta["sha"], GROUP1_SHA)

    def test_finance_model_passes_group2_meta(self) -> None:
        """金融域在组 2 快照上必须仍然可构建：校验不是「非组 1 即拒」。

        反向断言这条若失败，说明实现把「表数少于 29」当成了判据，那会让今天
        可用的容器（背景节实测「金融域可用」）退化成不可用——本 ADR 的
        理由 2 明写「不可能让原本可用的路径变得不可用」。
        """
        agent = _build_with(_meta_from_locked(GROUP2_SHA), FINANCE_MODEL)
        self.assertEqual(agent.model.name, "atlas_finance_analytics")


class TestCheckSemantics(unittest.TestCase):
    """决策 ③ 的四条细则：只查表集、默认模型也查、发生在构造之前、消息可归因。"""

    def test_row_counts_are_not_verified(self) -> None:
        """表集齐、行数全 0 → 仍构造成功（决策 ③ 末段「不校验行数」）。

        把 row_counts 绝对值纳入启动校验，会让「同数据多锁」的正常演进
        （data/snapshots/README.md 的纪律）变成启动失败。
        """
        model_tables = tuple(
            sorted({f"atlas.dwd.{t}" for t in ("date_dim", "dim_item", "dim_store", "store_sales")})
        )
        meta = _synthetic_meta(model_tables, sha="zerorow", rows=0)
        agent = _build_with(meta, RETAIL_MODEL)
        self.assertEqual(agent.snapshot_meta["sha"], "zerorow")
        self.assertEqual(agent.model.name, "atlas_retail_analytics")

    def test_default_model_is_checked_too(self) -> None:
        """`model_path=None`（CLI ask 的调用形态）也必须校验。

        改造前工厂在 None 时不构造 SemanticModel，而是让 DataAgent 内部
        `model or SemanticModel()` 兜底——若校验只写在「显式传了路径」的分支上，
        CLI 的 `atlas ask` 就绕过整条防线，而它恰是背景节里最常用的入口。
        """
        finance_only = _synthetic_meta(
            ("atlas.dwd.dim_account", "atlas.dwd.fact_trades"), sha="partial"
        )
        with self.assertRaises(SnapshotUnavailable) as ctx:
            _build_with(finance_only, None)
        message = str(ctx.exception)
        self.assertIn("atlas_finance_analytics", message, "缺模型名则无法判断是哪个域绑错")
        self.assertIn("atlas.dwd.dim_date", message)

    def test_check_happens_before_agent_construction(self) -> None:
        """启动期而非运行期：校验失败时 `DataAgent` 一次都不该被构造。"""
        meta = _meta_from_locked(GROUP2_SHA)
        with (
            mock.patch.object(factory, "DataAgent") as built,
            self.assertRaises(SnapshotUnavailable),
        ):
            _build_with(meta, RETAIL_MODEL)
        built.assert_not_called()

    def test_message_carries_sha_source_and_remediation(self) -> None:
        """错误消息的三个可归因要素：绑到哪（sha + source）、怎么修（两条出路）。"""
        with self.assertRaises(SnapshotUnavailable) as ctx:
            _build_with(_meta_from_locked(GROUP2_SHA), RETAIL_MODEL)
        message = str(ctx.exception)
        self.assertIn(GROUP2_SHA, message)
        self.assertIn("head", message, "不写来源就分不清是显式指定还是 HEAD 命中")
        self.assertIn("ATLAS_SNAPSHOT_SHA", message)
        self.assertIn("make seed", message)


class TestGuardIsStillTheLastLine(unittest.TestCase):
    """启动校验**不取代** Guard：绕过 agent 构造直接给 Guard 一张表，仍须被拒。

    决策 ③ 的定位是「配置错误提前暴露」，纵深防御的原样不动（AGENTS.md N3）。
    若有人把启动校验当成「白名单已经保证安全」而放宽 Guard，本例仍应成立。
    """

    def test_guard_rejects_table_absent_from_budget(self) -> None:
        from agent.security.sql_guard import enforce

        snap = RuntimeSnapshot(
            sha="synthe1",
            meta=_synthetic_meta(("atlas.dwd.store_sales",), sha="synthe1"),
            source="head",
            bound_to_head=True,
        )
        from eval.runner import build_budget

        budget = build_budget(snap.meta)
        with self.assertRaises(Exception) as ctx:
            enforce("SELECT 1 FROM atlas.dwd.not_in_snapshot LIMIT 10", budget=budget)
        self.assertIn("白名单", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
