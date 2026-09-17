"""ADR-0026 T02：可加组合登记与快照资格证据（eval/analysis_eligibility.py 契约测试）。

逐条覆盖 task-2-brief.md 的四条 checklist：
1. 未声明组合 / 比率 / AVG / DISTINCT / 窗口 / 期末余额 / 未知字段均拒绝；
   用最小临时语义模型测试 SUM（含行内乘法）/ COUNT 正例。
2. 两个初始组合（total_trade_value / commission_revenue × Branch）登记；
   治理 schema 与 governance_validate 对 analysis 块的封闭校验。
3. 连接质量证明：重复目标键、孤儿引用、行放大/漏行、跨事实表达式、权限追加
   JOIN 全覆盖；检查 SQL 全部 sqlglot AST 构造、标量/计数型、带显式绝对时间
   谓词（覆盖锁定快照全期）、逐条 Guard enforce 后执行；覆盖不全不签发 eligible。
4. factory 装配资格事实（证据缺失仍可构造普通 ask）；CLI 显式 --snapshot-sha
   先复核锁定数据，失败非零且不写入合格状态。

绑定断言（brief 原文）：重复目标键与孤儿引用 fixture 必须
`self.assertFalse(report["eligible"])`；semantic hash 不一致时 load_eligibility
不得返回 eligible=True。全部用 TemporaryDirectory + 假执行器，不触碰真实
快照数据、不连任何数据库。
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import jsonschema
import yaml
from sqlglot import exp, parse_one

from agent.compiler import SemanticModel
from agent.factory import create_live_agent
from agent.graph import DataAgent
from agent.security.sql_guard import Budget
from eval.analysis_eligibility import (
    Executor,
    evaluate_combo,
    load_eligibility,
    main,
    record_eligibility,
    verify_eligibility,
)
from semantic.governance_validate import validate_file

REPO = Path(__file__).resolve().parent.parent
SCHEMA_PATH = REPO / "semantic" / "governance" / "atlas_governance.schema.json"
FINANCE_MODEL_PATH = REPO / "semantic" / "ossie" / "atlas_finance.ossie.yaml"

_SHA = "e11a001"
_META = {
    "sha": _SHA,
    "created_at": "2026-09-15T08:00:00+08:00",
    "source": "单元测试fixture",
    "data_range": "2012-07-07T00:03:11~2017-07-07T23:59:59",
    "row_counts": {
        "dwd": {
            "fact_trades": 10,
            "fact_holdings": 10,
            "dim_broker": 2,
            "dim_date": 3,
            "dim_account": 2,
            "dim_customer": 2,
            "dim_region": 1,
        }
    },
}
_TABLES = frozenset(
    f"atlas.dwd.{t}"
    for t in (
        "fact_trades",
        "fact_holdings",
        "dim_broker",
        "dim_date",
        "dim_account",
        "dim_customer",
        "dim_region",
    )
)


# ---------------------------------------------------------------------------
# 最小临时语义模型构造（不依赖仓库 ossie 文件；TemporaryDirectory 场景用 doc=）
# ---------------------------------------------------------------------------


def _field(name: str, *, is_time: bool = False) -> dict[str, Any]:
    return {
        "name": name,
        "expression": {"dialects": [{"dialect": "ANSI_SQL", "expression": name}]},
        "datatype": "String",
        "dimension": {"is_time": is_time},
    }


def _dataset(
    name: str, fields: list[dict[str, Any]], pk: list[str] | None = None
) -> dict[str, Any]:
    ds: dict[str, Any] = {"name": name, "source": f"atlas.dwd.{name}", "fields": fields}
    if pk:
        ds["primary_key"] = pk
    return ds


def _metric(name: str, expression: str, dims: list[str] | None = None) -> dict[str, Any]:
    """dims=None = 不声明 analysis（未登记）；list = 登记 analysis.attribution.dimensions。"""
    data: dict[str, Any] = {
        "governance": {
            "owner": "t@atlas.local",
            "version": 1,
            "status": "active",
            "supersedes": None,
        }
    }
    if dims is not None:
        data["analysis"] = {"attribution": {"dimensions": list(dims)}}
    return {
        "name": name,
        "expression": {"dialects": [{"dialect": "ANSI_SQL", "expression": expression}]},
        "custom_extensions": [{"vendor_name": "ATLAS", "data": json.dumps(data)}],
    }


def _model_doc(
    metrics: list[dict[str, Any]],
    *,
    extra_datasets: list[dict[str, Any]] | None = None,
    extra_relationships: list[dict[str, Any]] | None = None,
    model_ext: dict[str, Any] | None = None,
) -> dict[str, Any]:
    datasets = [
        _dataset(
            "fact_trades",
            [
                _field("TradeID"),
                _field("Quantity"),
                _field("TradePrice"),
                _field("Commission"),
                _field("SK_BrokerID"),
                _field("SK_DateID"),
                _field("SK_AccountID"),
            ],
            pk=["TradeID"],
        ),
        _dataset(
            "dim_broker",
            [_field("SK_BrokerID"), _field("Branch"), _field("Office")],
            pk=["SK_BrokerID"],
        ),
        _dataset(
            "dim_date",
            [
                _field("SK_DateID"),
                _field("DateValue", is_time=True),
                _field("CalendarYearID", is_time=True),
                _field("CalendarQuarterID", is_time=True),
                _field("CalendarMonthID", is_time=True),
            ],
            pk=["SK_DateID"],
        ),
    ]
    relationships = [
        {
            "name": "trades_to_broker",
            "from": "fact_trades",
            "to": "dim_broker",
            "from_columns": ["SK_BrokerID"],
            "to_columns": ["SK_BrokerID"],
        },
        {
            "name": "trades_to_date",
            "from": "fact_trades",
            "to": "dim_date",
            "from_columns": ["SK_DateID"],
            "to_columns": ["SK_DateID"],
        },
    ]
    if extra_datasets:
        datasets.extend(extra_datasets)
    if extra_relationships:
        relationships.extend(extra_relationships)
    ext = (
        model_ext
        if model_ext is not None
        else {
            "time_dimension": {
                "table": "dim_date",
                "mode": "single",
                "columns": {
                    "year": "CalendarYearID",
                    "quarter": "CalendarQuarterID",
                    "month": "CalendarMonthID",
                    "date": "DateValue",
                },
            }
        }
    )
    return {
        "semantic_model": [
            {
                "name": "elig_tmp",
                "datasets": datasets,
                "relationships": relationships,
                "metrics": metrics,
                "custom_extensions": [{"vendor_name": "ATLAS", "data": json.dumps(ext)}],
            }
        ]
    }


def _model(metrics: list[dict[str, Any]], **kw: Any) -> SemanticModel:
    return SemanticModel(doc=_model_doc(metrics, **kw))


_HOLDINGS_DATASET = _dataset(
    "fact_holdings",
    [_field("HoldingID"), _field("CurrentValue"), _field("SK_BrokerID")],
    pk=["HoldingID"],
)
_REGION_DATASET = _dataset("dim_region", [_field("Region")], pk=["Region"])

_GROSS = _metric("gross_amount", "SUM(fact_trades.Quantity * fact_trades.TradePrice)", ["Branch"])
_OK_MODEL = _model([_GROSS])

# 权限追加 JOIN fixture：rp_tmp 条件引用 dim_customer（需经 dim_account 两跳可达）
_ACCOUNT_DATASET = _dataset(
    "dim_account", [_field("SK_AccountID"), _field("SK_CustomerID")], pk=["SK_AccountID"]
)
_CUSTOMER_DATASET = _dataset(
    "dim_customer", [_field("SK_CustomerID"), _field("tier")], pk=["SK_CustomerID"]
)
_POLICY_RELATIONSHIPS = [
    {
        "name": "trades_to_account",
        "from": "fact_trades",
        "to": "dim_account",
        "from_columns": ["SK_AccountID"],
        "to_columns": ["SK_AccountID"],
    },
    {
        "name": "account_to_customer",
        "from": "dim_account",
        "to": "dim_customer",
        "from_columns": ["SK_CustomerID"],
        "to_columns": ["SK_CustomerID"],
    },
]
_POLICY_MODEL_EXT = {
    "time_dimension": {
        "table": "dim_date",
        "mode": "single",
        "columns": {
            "year": "CalendarYearID",
            "quarter": "CalendarQuarterID",
            "month": "CalendarMonthID",
            "date": "DateValue",
        },
    },
    "policy": {"default_row_policy": "rp_tmp"},
}
_POLICY_YAML = """\
policies:
  - name: rp_tmp
    description: 测试策略（合规审计员经 dim_customer 过滤）
    roles:
      - name: compliance_auditor
        condition: "dim_customer.tier <= {{ user.max_tier }}"
      - name: branch_manager
        condition: "dim_broker.branch = '{{ user.branch }}'"
"""


def _budget(allowed: frozenset[str] | None = None) -> Budget:
    return Budget(
        dialect="doris", max_rows=1000, allowed_tables=_TABLES if allowed is None else allowed
    )


def _executor(markers: dict[str, int] | None = None) -> tuple[Executor, list[str]]:
    """假执行器（eval.runner.execute_sql 同契约：sql → (rows, columns)）。

    markers：SQL 标量别名 → 返回值（dup_cnt / orphan_cnt / row_delta），
    未命中别名返回 0（全部检查通过）。calls 记录收到的 SQL 供断言。
    """
    calls: list[str] = []

    def run(sql: str) -> tuple[list[tuple[Any, ...]], list[str]]:
        calls.append(sql)
        for alias, value in (markers or {}).items():
            if alias in sql:
                return [(value,)], [alias]
        return [(0,)], ["v"]

    return run, calls


def _never_executor() -> Executor:
    def run(sql: str) -> tuple[list[tuple[Any, ...]], list[str]]:
        raise AssertionError(f"不应执行任何 SQL（未过 enforce 即执行）：{sql}")

    return run


class TestComboClassification(unittest.TestCase):
    """checklist ①：结构分类——各拒绝形态 + SUM/COUNT 正例（最小临时语义模型）。"""

    def test_sum_with_rowwise_multiply_is_ok(self) -> None:
        m = _model([_GROSS])
        r = evaluate_combo(m, "gross_amount", "Branch")
        self.assertTrue(r["eligible"])
        self.assertIsNone(r["reason"])

    def test_plain_sum_is_ok(self) -> None:
        m = _model([_metric("commission_sum", "SUM(fact_trades.Commission)", ["Branch"])])
        r = evaluate_combo(m, "commission_sum", "Branch")
        self.assertTrue(r["eligible"])
        self.assertIsNone(r["reason"])

    def test_count_is_ok(self) -> None:
        m = _model([_metric("trade_cnt", "COUNT(fact_trades.TradeID)", ["Branch"])])
        r = evaluate_combo(m, "trade_cnt", "Branch")
        self.assertTrue(r["eligible"])
        self.assertIsNone(r["reason"])

    def test_undeclared_combination_rejected(self) -> None:
        m = _model([_metric("trade_cnt", "COUNT(fact_trades.TradeID)")])
        r = evaluate_combo(m, "trade_cnt", "Branch")
        self.assertFalse(r["eligible"])
        self.assertEqual(r["reason"], "undeclared_combination")

    def test_undeclared_dimension_on_registered_metric_rejected(self) -> None:
        """已登记 Branch 的指标换未登记维度 = 未声明组合（封闭注册表）。"""
        m = _model([_GROSS])
        r = evaluate_combo(m, "gross_amount", "Office")
        self.assertFalse(r["eligible"])
        self.assertEqual(r["reason"], "undeclared_combination")

    def test_unknown_dimension_rejected(self) -> None:
        r = evaluate_combo(_OK_MODEL, "gross_amount", "NoSuchDim")
        self.assertFalse(r["eligible"])
        self.assertEqual(r["reason"], "unknown_dimension")

    def test_ratio_rejected(self) -> None:
        m = _model(
            [
                _metric(
                    "ratio_metric",
                    "SUM(fact_trades.Commission) / NULLIF(COUNT(fact_trades.TradeID), 0)",
                    ["Branch"],
                )
            ]
        )
        r = evaluate_combo(m, "ratio_metric", "Branch")
        self.assertFalse(r["eligible"])
        self.assertEqual(r["reason"], "ratio_expression")

    def test_average_rejected(self) -> None:
        m = _model([_metric("avg_metric", "AVG(fact_trades.Quantity)", ["Branch"])])
        r = evaluate_combo(m, "avg_metric", "Branch")
        self.assertFalse(r["eligible"])
        self.assertEqual(r["reason"], "average_aggregate")

    def test_distinct_count_rejected(self) -> None:
        m = _model(
            [_metric("distinct_cnt", "COUNT(DISTINCT fact_trades.SK_AccountID)", ["Branch"])]
        )
        r = evaluate_combo(m, "distinct_cnt", "Branch")
        self.assertFalse(r["eligible"])
        self.assertEqual(r["reason"], "distinct_aggregate")

    def test_window_function_rejected(self) -> None:
        m = _model(
            [
                _metric(
                    "window_metric",
                    "SUM(fact_trades.Commission) OVER (PARTITION BY fact_trades.SK_BrokerID)",
                    ["Branch"],
                )
            ]
        )
        r = evaluate_combo(m, "window_metric", "Branch")
        self.assertFalse(r["eligible"])
        self.assertEqual(r["reason"], "window_function")

    def test_unknown_field_rejected(self) -> None:
        m = _model([_metric("ghost_sum", "SUM(fact_trades.NoSuchColumn)", ["Branch"])])
        r = evaluate_combo(m, "ghost_sum", "Branch")
        self.assertFalse(r["eligible"])
        self.assertEqual(r["reason"], "unknown_field")

    def test_cross_fact_expression_rejected(self) -> None:
        m = _model(
            [
                _metric(
                    "cross_metric",
                    "SUM(fact_trades.Commission) + SUM(fact_holdings.CurrentValue)",
                    ["Branch"],
                )
            ],
            extra_datasets=[_HOLDINGS_DATASET],
        )
        r = evaluate_combo(m, "cross_metric", "Branch")
        self.assertFalse(r["eligible"])
        self.assertEqual(r["reason"], "cross_fact_expression")

    def test_two_aggregates_same_fact_rejected(self) -> None:
        m = _model(
            [
                _metric(
                    "double_sum",
                    "SUM(fact_trades.Commission) + SUM(fact_trades.Quantity)",
                    ["Branch"],
                )
            ]
        )
        r = evaluate_combo(m, "double_sum", "Branch")
        self.assertFalse(r["eligible"])
        self.assertEqual(r["reason"], "unsupported_expression")

    def test_balance_metric_not_registered_rejected(self) -> None:
        """期末余额（SUM(CurrentValue) 类）不进封闭注册表 → 拒绝。

        ADR-0026 119-123：余额不是期间内可加的事件型指标；支持余额须先裁定
        分解数学与可加范围（ADR-0026 末段），不能只靠放开结构白名单。
        """
        m = _model(
            [_metric("holdings_value", "SUM(fact_holdings.CurrentValue)")],
            extra_datasets=[_HOLDINGS_DATASET],
        )
        r = evaluate_combo(m, "holdings_value", "Branch")
        self.assertFalse(r["eligible"])
        self.assertEqual(r["reason"], "undeclared_combination")


class TestRealModelRegistrations(unittest.TestCase):
    """checklist ②：金融模型登记两个初始组合；表达式复用不复制；加载契约。"""

    def test_initial_combos_registered(self) -> None:
        m = SemanticModel()
        self.assertEqual(m.attribution_dimensions.get("total_trade_value"), ("Branch",))
        self.assertEqual(m.attribution_dimensions.get("commission_revenue"), ("Branch",))

    def test_registered_combos_classify_ok(self) -> None:
        m = SemanticModel()
        for metric in ("total_trade_value", "commission_revenue"):
            r = evaluate_combo(m, metric, "Branch")
            self.assertTrue(r["eligible"], msg=f"{metric} 应为可加正例")

    def test_balances_and_distinct_metrics_not_registered(self) -> None:
        m = SemanticModel()
        for metric in (
            "holdings_value",
            "cash_balance",
            "active_account_count",
            "average_cash_balance",
        ):
            r = evaluate_combo(m, metric, "Branch")
            self.assertFalse(r["eligible"], msg=f"{metric} 不应登记")
            self.assertEqual(r["reason"], "undeclared_combination")

    def test_metric_expressions_reused_not_duplicated(self) -> None:
        m = SemanticModel()
        self.assertEqual(
            m.metrics["total_trade_value"], "SUM(fact_trades.Quantity * fact_trades.TradePrice)"
        )
        self.assertEqual(m.metrics["commission_revenue"], "SUM(fact_trades.Commission)")

    def test_dataset_primary_key_loaded(self) -> None:
        m = SemanticModel()
        self.assertEqual(m.datasets["fact_trades"].primary_key, ("TradeID",))

    def test_source_sha256_is_model_file_hash(self) -> None:
        self.assertEqual(
            SemanticModel().source_sha256,
            hashlib.sha256(FINANCE_MODEL_PATH.read_bytes()).hexdigest(),
        )


class TestGovernanceSchema(unittest.TestCase):
    """checklist ②：atlas_governance.schema.json 的 analysis 块封闭校验。"""

    schema: Any

    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

    def _validate(self, payload: dict[str, Any]) -> None:
        jsonschema.validate(payload, self.schema)

    def test_analysis_block_valid(self) -> None:
        self._validate({"analysis": {"attribution": {"dimensions": ["Branch"]}}})

    def test_analysis_rejects_extra_keys(self) -> None:
        with self.assertRaises(jsonschema.ValidationError):
            self._validate({"analysis": {"attribution": {"dimensions": ["Branch"], "extra": 1}}})
        with self.assertRaises(jsonschema.ValidationError):
            self._validate({"analysis": {"foo": 1}})

    def test_analysis_rejects_empty_dimensions(self) -> None:
        with self.assertRaises(jsonschema.ValidationError):
            self._validate({"analysis": {"attribution": {"dimensions": []}}})

    def test_analysis_rejects_duplicate_dimensions(self) -> None:
        with self.assertRaises(jsonschema.ValidationError):
            self._validate({"analysis": {"attribution": {"dimensions": ["Branch", "Branch"]}}})

    def test_analysis_rejects_non_string_dimensions(self) -> None:
        with self.assertRaises(jsonschema.ValidationError):
            self._validate({"analysis": {"attribution": {"dimensions": [1]}}})


class TestGovernanceValidator(unittest.TestCase):
    """checklist ②：governance_validate 对 analysis 块的语义校验（位置/字段存在性/时间字段）。"""

    def _write_and_validate(self, doc: dict[str, Any]) -> list[str]:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tmp_model.ossie.yaml"
            path.write_text(
                yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8"
            )
            errors: list[str] = []
            validate_file(path, json.loads(SCHEMA_PATH.read_text(encoding="utf-8")), errors)
            return errors

    def test_real_finance_model_passes(self) -> None:
        errors: list[str] = []
        validate_file(
            FINANCE_MODEL_PATH, json.loads(SCHEMA_PATH.read_text(encoding="utf-8")), errors
        )
        self.assertEqual(errors, [])

    def test_model_level_analysis_rejected(self) -> None:
        doc = _model_doc([_GROSS])
        ext = json.loads(doc["semantic_model"][0]["custom_extensions"][0]["data"])
        ext["analysis"] = {"attribution": {"dimensions": ["Branch"]}}
        doc["semantic_model"][0]["custom_extensions"][0]["data"] = json.dumps(ext)
        errors = self._write_and_validate(doc)
        self.assertTrue(any("analysis" in e and "指标级" in e for e in errors), msg=errors)

    def test_unknown_dimension_rejected(self) -> None:
        doc = _model_doc(
            [
                _metric(
                    "gross_amount", "SUM(fact_trades.Quantity * fact_trades.TradePrice)", ["Ghost"]
                )
            ]
        )
        errors = self._write_and_validate(doc)
        self.assertTrue(any("Ghost" in e for e in errors), msg=errors)

    def test_time_dimension_field_rejected(self) -> None:
        doc = _model_doc(
            [
                _metric(
                    "gross_amount",
                    "SUM(fact_trades.Quantity * fact_trades.TradePrice)",
                    ["DateValue"],
                )
            ]
        )
        errors = self._write_and_validate(doc)
        self.assertTrue(any("时间字段" in e for e in errors), msg=errors)

    def test_valid_analysis_passes(self) -> None:
        doc = _model_doc([_GROSS])
        errors = self._write_and_validate(doc)
        self.assertEqual([e for e in errors if "analysis" in e], [])


class TestVerifyEligibilityChecks(unittest.TestCase):
    """checklist ③：连接质量证明——重复键/孤儿/行放大/漏行/权限 JOIN/覆盖完整性。"""

    def test_happy_path_eligible_and_report_shape(self) -> None:
        run, calls = _executor()
        report = verify_eligibility(_OK_MODEL, _META, run, _budget())
        self.assertTrue(report["eligible"])
        self.assertEqual(report["snapshot_sha"], _SHA)
        self.assertEqual(report["semantic_sha256"], _OK_MODEL.source_sha256)
        self.assertEqual(
            [(p["metric"], p["dimension"]) for p in report["pairs"]], [("gross_amount", "Branch")]
        )
        self.assertTrue(report["checks"])
        for c in report["checks"]:
            self.assertIn("sql", c)
            self.assertEqual(c["result"], "pass")
            self.assertEqual(c["scope"]["time_range"], {"min": "2012-07-07", "max": "2017-07-07"})
            self.assertIsNone(c["scope"]["sharding"])
            self.assertIn("LIMIT 1", c["sql"])
            # 显式绝对时间谓词覆盖锁定快照全期（不依赖 Guard 默认时间窗回退）
            self.assertIn("CAST('2012-07-07' AS DATE)", c["sql"])
            self.assertIn("CAST('2017-07-07' AS DATE)", c["sql"])
        self.assertTrue(calls)
        for sql in calls:
            self.assertIn("LIMIT 1", sql)
            self.assertIn("CAST('2012-07-07' AS DATE)", sql)

    def test_check_sql_ast_built_scalar_count_only(self) -> None:
        run, calls = _executor()
        report = verify_eligibility(_OK_MODEL, _META, run, _budget())
        for c in report["checks"]:
            ast = parse_one(c["sql"])
            self.assertIsInstance(ast, exp.Select)
            self.assertIsInstance(ast.args.get("limit"), exp.Limit)
            self.assertIsNotNone(ast.find(exp.Between), msg=c["sql"])
            self.assertEqual(len(ast.expressions), 1, msg=c["sql"])
            self.assertTrue(_is_scalar_expr(ast.expressions[0]), msg=c["sql"])

    def test_check_coverage_complete(self) -> None:
        run, _ = _executor()
        report = verify_eligibility(_OK_MODEL, _META, run, _budget())
        names = {c["name"] for c in report["checks"]}
        # 每条 join 边（含时间谓词追加的 dim_date 边）都要有重复键 + 孤儿检查
        self.assertTrue(
            any(n.startswith("duplicate_target_key") and "dim_broker" in n for n in names),
            msg=names,
        )
        self.assertTrue(
            any(n.startswith("duplicate_target_key") and "dim_date" in n for n in names), msg=names
        )
        self.assertTrue(
            any(n.startswith("orphan_reference") and "dim_broker" in n for n in names), msg=names
        )
        self.assertTrue(
            any(n.startswith("orphan_reference") and "dim_date" in n for n in names), msg=names
        )
        self.assertIn("row_invariant", names)
        self.assertEqual(
            sorted(report["join_paths"]),
            sorted(
                [
                    "fact_trades → dim_broker（trades_to_broker）",
                    "fact_trades → dim_date（trades_to_date）",
                ]
            ),
        )

    def test_duplicate_target_key_fails(self) -> None:
        """绑定断言：重复目标键 fixture → assertFalse(report["eligible"])。"""
        run, _ = _executor({"dup_cnt": 2})
        report = verify_eligibility(_OK_MODEL, _META, run, _budget())
        self.assertFalse(report["eligible"])
        dup = [c for c in report["checks"] if c["name"].startswith("duplicate_target_key")]
        self.assertTrue(any(c["result"] == "fail" and c["observed"] == 2 for c in dup), msg=dup)

    def test_orphan_reference_fails(self) -> None:
        """绑定断言：孤儿引用 fixture → assertFalse(report["eligible"])。"""
        run, _ = _executor({"orphan_cnt": 3})
        report = verify_eligibility(_OK_MODEL, _META, run, _budget())
        self.assertFalse(report["eligible"])
        orphan = [c for c in report["checks"] if c["name"].startswith("orphan_reference")]
        self.assertTrue(
            any(c["result"] == "fail" and c["observed"] == 3 for c in orphan), msg=orphan
        )

    def test_row_amplification_fails(self) -> None:
        run, _ = _executor({"row_delta": -5})
        report = verify_eligibility(_OK_MODEL, _META, run, _budget())
        self.assertFalse(report["eligible"])
        inv = next(c for c in report["checks"] if c["name"] == "row_invariant")
        self.assertEqual(inv["result"], "fail")
        self.assertEqual(inv["observed"], -5)

    def test_dropped_rows_fail(self) -> None:
        run, _ = _executor({"row_delta": 7})
        report = verify_eligibility(_OK_MODEL, _META, run, _budget())
        self.assertFalse(report["eligible"])

    def test_executor_exception_fails_closed(self) -> None:
        def run(sql: str) -> tuple[list[tuple[Any, ...]], list[str]]:
            raise RuntimeError("连接中断")

        report = verify_eligibility(_OK_MODEL, _META, run, _budget())
        self.assertFalse(report["eligible"])
        self.assertTrue(all(c["result"] == "fail" for c in report["checks"]))
        self.assertTrue(any("连接中断" in str(c["observed"]) for c in report["checks"]))

    def test_guard_unsafe_query_fails_closed_before_execution(self) -> None:
        """白名单缺表 → Guard UnsafeQuery → 检查失败（逐条 enforce 先于执行）。"""
        run = _never_executor()
        budget = Budget(
            dialect="doris", max_rows=1000, allowed_tables=frozenset({"atlas.dwd.dim_broker"})
        )
        report = verify_eligibility(_OK_MODEL, _META, run, budget)
        self.assertFalse(report["eligible"])
        self.assertTrue(report["checks"])
        self.assertTrue(all(c["result"] == "fail" for c in report["checks"]))

    def test_unresolvable_dimension_path_fails_without_sql(self) -> None:
        run, calls = _executor()
        m = _model(
            [_metric("by_region", "SUM(fact_trades.Quantity)", ["Region"])],
            extra_datasets=[_REGION_DATASET],
        )
        report = verify_eligibility(m, _META, run, _budget())
        self.assertFalse(report["eligible"])
        self.assertEqual(calls, [])
        self.assertTrue(any("join" in r for r in report["reasons"]), msg=report["reasons"])

    def test_cross_fact_combo_skips_sql(self) -> None:
        run, calls = _executor()
        m = _model(
            [
                _metric(
                    "cross_metric",
                    "SUM(fact_trades.Commission) + SUM(fact_holdings.CurrentValue)",
                    ["Branch"],
                )
            ],
            extra_datasets=[_HOLDINGS_DATASET],
        )
        report = verify_eligibility(m, _META, run, _budget())
        self.assertFalse(report["eligible"])
        self.assertEqual(calls, [])
        self.assertEqual(report["pairs"][0]["reason"], "cross_fact_expression")

    def test_no_declared_combinations_not_eligible(self) -> None:
        run, calls = _executor()
        m = _model([_metric("gross_amount", "SUM(fact_trades.Quantity)")])
        report = verify_eligibility(m, _META, run, _budget())
        self.assertFalse(report["eligible"])
        self.assertEqual(calls, [])
        self.assertIn("no_declared_combination", report["reasons"])

    def test_permission_added_join_covered(self) -> None:
        """权限追加 JOIN：策略条件表（dim_customer，经 dim_account 两跳）必须进检查范围。"""
        run, calls = _executor()
        m = _model(
            [_GROSS],
            extra_datasets=[_ACCOUNT_DATASET, _CUSTOMER_DATASET],
            extra_relationships=_POLICY_RELATIONSHIPS,
            model_ext=_POLICY_MODEL_EXT,
        )
        with tempfile.TemporaryDirectory() as tmp:
            policy_path = Path(tmp) / "row_policy.yml"
            policy_path.write_text(_POLICY_YAML, encoding="utf-8")
            report = verify_eligibility(m, _META, run, _budget(), policy_path=policy_path)
        self.assertTrue(report["eligible"], msg=report["reasons"])
        all_sql = "\n".join(c["sql"] for c in report["checks"])
        self.assertIn("dim_customer", all_sql)
        invariant = next(c for c in report["checks"] if c["name"] == "row_invariant")
        self.assertIn("dim_customer", invariant["sql"])
        self.assertTrue(
            any("dim_customer" in p for p in report["join_paths"]), msg=report["join_paths"]
        )

    def test_unresolvable_policy_table_fails_closed(self) -> None:
        """策略条件表无法经 relationships 到达 → fail closed（覆盖不全不得签发）。"""
        run, calls = _executor()
        m = _model(
            [_GROSS],
            extra_datasets=[_REGION_DATASET],
            model_ext={**_POLICY_MODEL_EXT},
        )
        policy_yaml = _POLICY_YAML.replace("dim_customer.tier", "dim_region.region")
        with tempfile.TemporaryDirectory() as tmp:
            policy_path = Path(tmp) / "row_policy.yml"
            policy_path.write_text(policy_yaml, encoding="utf-8")
            report = verify_eligibility(m, _META, run, _budget(), policy_path=policy_path)
        self.assertFalse(report["eligible"])
        self.assertEqual(calls, [])
        self.assertTrue(any("policy" in r for r in report["reasons"]), msg=report["reasons"])

    def test_missing_policy_file_fails_closed(self) -> None:
        run, _ = _executor()
        m = _model([_GROSS], model_ext=_POLICY_MODEL_EXT)
        with tempfile.TemporaryDirectory() as tmp:
            report = verify_eligibility(
                m, _META, run, _budget(), policy_path=Path(tmp) / "absent.yml"
            )
        self.assertFalse(report["eligible"])
        self.assertTrue(any("policy" in r for r in report["reasons"]), msg=report["reasons"])

    def test_invalid_data_range_fails_closed(self) -> None:
        run, calls = _executor()
        bad_meta = {**_META, "data_range": "not-a-range"}
        report = verify_eligibility(_OK_MODEL, bad_meta, run, _budget())
        self.assertFalse(report["eligible"])
        self.assertEqual(calls, [])

    def test_duplicate_check_sql_scopes_referenced_keys(self) -> None:
        """重复键检查的引用键子查询必须以锁定快照时间范围过滤事实侧。"""
        run, calls = _executor()
        verify_eligibility(_OK_MODEL, _META, run, _budget())
        dup_sqls = [s for s in calls if "dup_cnt" in s]
        self.assertTrue(dup_sqls)
        for sql in dup_sqls:
            self.assertIn("IN (SELECT", sql)
            self.assertIn("fact_trades", sql)


def _is_scalar_expr(e: exp.Expr) -> bool:
    """外层输出列必须是聚合或标量子查询的算术组合（不倾倒行集）。"""
    if isinstance(e, exp.Alias):
        # 带别名的 SELECT 输出列（COUNT(*) AS dup_cnt）递归判定内层
        return _is_scalar_expr(e.this)
    if isinstance(e, (exp.Subquery, exp.AggFunc)):
        return True
    if isinstance(e, (exp.Add, exp.Sub, exp.Mul, exp.Div)):
        return _is_scalar_expr(e.this) and _is_scalar_expr(e.expression)
    return False


class TestLoadEligibility(unittest.TestCase):
    """checklist ③/④：只读绑定证据——缺失/过期/哈希漂移返回不可用事实，不抛异常。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.snap_dir = Path(self._tmp.name)
        patcher = mock.patch("data.identity.SNAPSHOT_DIR", self.snap_dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        (self.snap_dir / f"{_SHA}.meta.json").write_text(json.dumps(_META), encoding="utf-8")

    def _write_evidence(self, **overrides: Any) -> None:
        evidence = {
            "snapshot_sha": _SHA,
            "semantic_sha256": _OK_MODEL.source_sha256,
            "model": "elig_tmp",
            "eligible": True,
            "pairs": [
                {"metric": "gross_amount", "dimension": "Branch", "eligible": True, "reason": None}
            ],
            "checks": [],
        }
        evidence.update(overrides)
        (self.snap_dir / f"{_SHA}.analysis.json").write_text(
            json.dumps(evidence, ensure_ascii=False), encoding="utf-8"
        )

    def test_missing_evidence_returns_unavailable_fact(self) -> None:
        r = load_eligibility(_OK_MODEL, _META)
        self.assertFalse(r["available"])
        self.assertFalse(r["eligible"])
        self.assertEqual(r["reason"], "missing_evidence")

    def test_matching_evidence_available(self) -> None:
        self._write_evidence()
        r = load_eligibility(_OK_MODEL, _META)
        self.assertTrue(r["available"])
        self.assertTrue(r["eligible"])
        self.assertEqual(r["evidence"]["snapshot_sha"], _SHA)

    def test_semantic_hash_mismatch_never_eligible(self) -> None:
        """绑定断言：semantic hash 不一致 → load_eligibility 不得返回 eligible=True。"""
        self._write_evidence(semantic_sha256="deadbeef")
        r = load_eligibility(_OK_MODEL, _META)
        self.assertFalse(r["eligible"])
        self.assertFalse(r["available"])
        self.assertEqual(r["reason"], "semantic_hash_mismatch")

    def test_snapshot_sha_mismatch_rejected(self) -> None:
        self._write_evidence(snapshot_sha="other")
        r = load_eligibility(_OK_MODEL, _META)
        self.assertFalse(r["eligible"])
        self.assertEqual(r["reason"], "snapshot_mismatch")

    def test_invalid_json_unavailable(self) -> None:
        (self.snap_dir / f"{_SHA}.analysis.json").write_text("{not json", encoding="utf-8")
        r = load_eligibility(_OK_MODEL, _META)
        self.assertFalse(r["available"])
        self.assertEqual(r["reason"], "invalid_evidence")

    def test_ineligible_evidence_not_available(self) -> None:
        self._write_evidence(eligible=False)
        r = load_eligibility(_OK_MODEL, _META)
        self.assertFalse(r["available"])

    def test_real_model_binds_by_file_hash(self) -> None:
        real = SemanticModel()
        meta = {**_META, "sha": "rt1file"}
        (self.snap_dir / "rt1file.analysis.json").write_text(
            json.dumps(
                {
                    "snapshot_sha": "rt1file",
                    "semantic_sha256": hashlib.sha256(FINANCE_MODEL_PATH.read_bytes()).hexdigest(),
                    "model": real.name,
                    "eligible": True,
                    "pairs": [],
                    "checks": [],
                }
            ),
            encoding="utf-8",
        )
        r = load_eligibility(real, meta)
        self.assertTrue(r["available"])
        self.assertTrue(r["eligible"])


class TestRecordEligibility(unittest.TestCase):
    """checklist ③：合格证据落盘 data/snapshots/<sha>.analysis.json（机器生成，不合格不落盘）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.snap_dir = Path(self._tmp.name)

    def test_ineligible_report_not_written(self) -> None:
        run, _ = _executor({"dup_cnt": 1})
        report = verify_eligibility(_OK_MODEL, _META, run, _budget())
        self.assertFalse(report["eligible"])
        self.assertIsNone(record_eligibility(report, self.snap_dir))
        self.assertEqual(list(self.snap_dir.iterdir()), [])

    def test_eligible_report_written_and_round_trips(self) -> None:
        run, _ = _executor()
        report = verify_eligibility(_OK_MODEL, _META, run, _budget())
        path = record_eligibility(report, self.snap_dir)
        assert path is not None
        self.assertEqual(path, self.snap_dir / f"{_SHA}.analysis.json")
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertTrue(payload["eligible"])
        with mock.patch("data.identity.SNAPSHOT_DIR", self.snap_dir):
            r = load_eligibility(_OK_MODEL, _META)
        self.assertTrue(r["available"])
        self.assertTrue(r["eligible"])


class TestCli(unittest.TestCase):
    """checklist ④：资格 CLI——显式 --snapshot-sha，先复核锁定数据，失败非零不写合格状态。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.snap_dir = Path(self._tmp.name) / "snapshots"
        self.snap_dir.mkdir()
        (self.snap_dir / f"{_SHA}.meta.json").write_text(json.dumps(_META), encoding="utf-8")
        self.model_path = Path(self._tmp.name) / "tmp_model.ossie.yaml"
        self.model_path.write_text(
            yaml.safe_dump(_model_doc([_GROSS]), allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )

    def _argv(self) -> list[str]:
        return [
            "--snapshot-sha",
            _SHA,
            "--model",
            str(self.model_path),
            "--snapshots-dir",
            str(self.snap_dir),
        ]

    def test_cli_success_writes_evidence(self) -> None:
        run, calls = _executor()
        rc = main(self._argv(), executor=run)
        self.assertEqual(rc, 0)
        out = self.snap_dir / f"{_SHA}.analysis.json"
        self.assertTrue(out.exists())
        self.assertTrue(json.loads(out.read_text(encoding="utf-8"))["eligible"])
        self.assertTrue(calls)  # 复核 = 真实重跑检查 SQL，而非信任已有产物

    def test_cli_failure_nonzero_no_write(self) -> None:
        run, _ = _executor({"dup_cnt": 9})
        rc = main(self._argv(), executor=run)
        self.assertNotEqual(rc, 0)
        self.assertFalse((self.snap_dir / f"{_SHA}.analysis.json").exists())

    def test_cli_requires_snapshot_sha(self) -> None:
        with self.assertRaises(SystemExit):
            main(
                ["--model", str(self.model_path), "--snapshots-dir", str(self.snap_dir)],
                executor=_executor()[0],
            )

    def test_cli_unknown_sha_nonzero(self) -> None:
        rc = main(
            [
                "--snapshot-sha",
                "nosuch",
                "--model",
                str(self.model_path),
                "--snapshots-dir",
                str(self.snap_dir),
            ],
            executor=_executor()[0],
        )
        self.assertNotEqual(rc, 0)

    def test_cli_meta_sha_mismatch_nonzero(self) -> None:
        """锁定数据复核：meta 文件名与内容 sha 不一致 → 非零，不写任何产物。"""
        bad = {**_META, "sha": "forged"}
        (self.snap_dir / f"{_SHA}.meta.json").write_text(json.dumps(bad), encoding="utf-8")
        run, calls = _executor()
        rc = main(self._argv(), executor=run)
        self.assertNotEqual(rc, 0)
        self.assertEqual(calls, [])
        self.assertFalse((self.snap_dir / f"{_SHA}.analysis.json").exists())


class TestFactoryWiring(unittest.TestCase):
    """checklist ④：factory 读取匹配的资格产物；证据缺失时普通 ask 仍可构造。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        snap_dir = Path(self._tmp.name)
        real_meta = {
            "sha": _SHA,
            "created_at": "2026-09-15T08:00:00+08:00",
            "source": "TPC-DI + TPC-DS SF0.1",
            "data_range": "2012-07-07T00:03:11~2017-07-07T23:59:59",
            "row_counts": {
                "dwd": {
                    "fact_trades": 10,
                    "fact_holdings": 10,
                    "fact_cash_balances": 10,
                    "dim_account": 10,
                    "dim_broker": 10,
                    "dim_customer": 10,
                    "dim_date": 10,
                    "dim_security": 10,
                }
            },
        }
        (snap_dir / f"{_SHA}.meta.json").write_text(json.dumps(real_meta), encoding="utf-8")
        env = mock.patch.dict(
            os.environ, {"ATLAS_SNAPSHOT_SHA": _SHA, "ATLAS_CHECKPOINT_DB": ""}, clear=False
        )
        env.start()
        self.addCleanup(env.stop)
        self.snap_dir = snap_dir

    def test_agent_constructs_without_evidence(self) -> None:
        """证据缺失 = 不可用事实：构造不抛异常，普通 ask 入口仍在。"""
        with mock.patch("data.identity.SNAPSHOT_DIR", self.snap_dir):
            agent = create_live_agent()
        self.assertIsInstance(agent, DataAgent)
        r = agent.analysis_eligibility  # type: ignore[attr-defined]  # 动态附加属性
        self.assertFalse(r["available"])
        self.assertFalse(r["eligible"])
        self.assertEqual(r["reason"], "missing_evidence")

    def test_agent_binds_matching_evidence(self) -> None:
        evidence = {
            "snapshot_sha": _SHA,
            "semantic_sha256": hashlib.sha256(FINANCE_MODEL_PATH.read_bytes()).hexdigest(),
            "model": SemanticModel().name,
            "eligible": True,
            "pairs": [],
            "checks": [],
        }
        (self.snap_dir / f"{_SHA}.analysis.json").write_text(json.dumps(evidence), encoding="utf-8")
        with mock.patch("data.identity.SNAPSHOT_DIR", self.snap_dir):
            agent = create_live_agent()
        self.assertTrue(agent.analysis_eligibility["available"])  # type: ignore[attr-defined]
        self.assertTrue(agent.analysis_eligibility["eligible"])  # type: ignore[attr-defined]


if __name__ == "__main__":
    unittest.main()
