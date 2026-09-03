"""observability/ 面板与告警配置契约测试（Day 51，批次 E）。

防漂移断言：面板/告警里 PromQL 引用的指标必须来自 observability/otel.py
的注册清单（改名必须同步，否则面板静默出空图）；datasource uid 全局一致；
任务书要求的五类面板（QPS / p95 / token_cost / 拒绝率 / kind 拆分）在位。

注意：本测试只校验「配置自洽」，不产生也不断言任何运行时数字——
面板本身是查询配置，不是实测声明（AGENTS.md 9.1 口径见 observability/README.md）。
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
OTEL_PY = REPO / "observability" / "otel.py"
DASHBOARD = REPO / "observability" / "dashboards" / "atlas-data-agent.json"
ALERTS = REPO / "observability" / "provisioning" / "alerting" / "atlas-alerts.yaml"
DATASOURCES = REPO / "observability" / "provisioning" / "datasources" / "prometheus.yaml"

# 任务书 Day 51 面板清单（标题关键字 → 断言意图）
REQUIRED_PANELS = ("QPS", "拒绝率", "p95", "token", "by kind")


def _registered_metrics() -> set[str]:
    """otel.py 中 _METRIC_* 常量值（点号→下划线的 Prometheus 形态）。"""
    src = OTEL_PY.read_text(encoding="utf-8")
    return set(re.findall(r'_METRIC_\w+\s*=\s*"([a-z_.]+)"', src))


def _prom_metric_names(expr: str) -> set[str]:
    """从 PromQL 提取指标名（atlas_* / gen_ai_*，含 _total/_bucket 等后缀）。

    先剔除 by/on 分组与 label selector，避免把 label 名（如 atlas_turn_kind）
    误判为指标。
    """
    body = re.sub(r"\b(?:by|on|without|ignoring)\s*\([^)]*\)", "", expr)
    body = re.sub(r"\{[^}]*\}", "", body)
    return set(re.findall(r"\b(?:atlas|gen_ai)_[a-z_]+(?:_total|_bucket|_sum|_count)?\b", body))


def _prom_exporter_names(registered: set[str]) -> set[str]:
    """otel.py 注册名 → prometheus exporter 实测输出名的精确形态。

    2026-09-03 冒烟实测（otel-collector prometheus exporter add_metric_suffixes）：
    - Counter → <base>_total（如 atlas_turn_count_total）
    - Histogram（unit ms）→ <base>_milliseconds_{bucket,sum,count}
    面板/告警必须引用这些精确名——前缀匹配放行过错误形态（如缺
    _milliseconds）会静默出空图，故此处用精确集合断言。
    """
    histogram_ms = {"atlas.turn.latency"}  # otel.py 中 unit="ms" 的直方图
    names: set[str] = set()
    for name in registered:
        base = name.replace(".", "_")
        if name in histogram_ms:
            names |= {f"{base}_milliseconds_{s}" for s in ("bucket", "sum", "count")}
        else:
            names.add(f"{base}_total")
    return names


def _is_registered(token: str, registered: set[str]) -> bool:
    """token 必须等于某注册指标经 exporter 规范化后的精确形态。"""
    return token in _prom_exporter_names(registered)


class TestDashboardConfig(unittest.TestCase):
    def setUp(self) -> None:
        self.dashboard = json.loads(DASHBOARD.read_text(encoding="utf-8"))
        self.registered = _registered_metrics()
        self.assertGreaterEqual(len(self.registered), 4)  # token_cost/count/latency/blocked

    def test_registered_metrics_must_have_metric_constants(self) -> None:
        """注册清单至少覆盖任务书要求的四类指标。"""
        for required in ("gen_ai.token_cost", "atlas.turn.count", "atlas.turn.latency"):
            self.assertIn(required, self.registered)

    def test_dashboard_panels_required(self) -> None:
        """五类面板在位（任务书 Day 51 清单）。"""
        titles = " | ".join(p["title"] for p in self.dashboard["panels"])
        for keyword in REQUIRED_PANELS:
            self.assertIn(keyword, titles, f"缺少面板：{keyword}")

    def test_promql_metrics_all_registered(self) -> None:
        """面板引用的每个指标都必须能在 otel.py 注册清单中找到。"""
        missing: list[str] = []
        for panel in self.dashboard["panels"]:
            for target in panel.get("targets", []):
                for token in _prom_metric_names(target["expr"]):
                    if not _is_registered(token, self.registered):
                        missing.append(f"{panel['title']}: {token}")
        self.assertEqual(missing, [], f"面板引用了未注册指标：{missing}")

    def test_datasource_uid_consistent(self) -> None:
        """面板/告警按 uid 引用数据源，uid 与 provisioning 数据源一致。"""
        ds = yaml.safe_load(DATASOURCES.read_text(encoding="utf-8"))["datasources"][0]
        uid = ds["uid"]
        self.assertEqual(uid, "prometheus")
        for panel in self.dashboard["panels"]:
            for target in panel.get("targets", []):
                self.assertEqual(target["datasource"]["uid"], uid)


class TestAlertConfig(unittest.TestCase):
    def setUp(self) -> None:
        self.alerts = yaml.safe_load(ALERTS.read_text(encoding="utf-8"))
        self.registered = _registered_metrics()

    def _all_exprs(self) -> list[str]:
        exprs: list[str] = []
        for group in self.alerts["groups"]:
            for rule in group["rules"]:
                for item in rule["data"]:
                    if "expr" in item["model"]:
                        exprs.append(str(item["model"]["expr"]))
        return exprs

    def test_rules_present(self) -> None:
        """至少四组规则：拒绝率 / p95 时延 / 故障率 / 数据链路中断。"""
        titles = [rule["title"] for group in self.alerts["groups"] for rule in group["rules"]]
        self.assertEqual(len(titles), 4)
        for keyword in ("拒绝率", "p95", "故障率", "中断"):
            self.assertTrue(any(keyword in t for t in titles), f"缺少告警：{keyword}")

    def test_alert_exprs_reference_registered_metrics_only(self) -> None:
        """告警 PromQL 引用的指标必须注册（防改名后静默失效）。"""
        missing: list[str] = []
        for expr in self._all_exprs():
            for token in _prom_metric_names(expr):
                if not _is_registered(token, self.registered):
                    missing.append(token)
        self.assertEqual(missing, [], f"告警引用了未注册指标：{missing}")

    def test_every_rule_has_condition_and_for(self) -> None:
        """每条规则都有评估条件与 for（告警配置完整性）。"""
        for group in self.alerts["groups"]:
            for rule in group["rules"]:
                self.assertIn("condition", rule, rule["title"])
                self.assertIn("for", rule, rule["title"])


class TestCollectorConfig(unittest.TestCase):
    def test_collector_yaml_parses_with_expected_pipeline(self) -> None:
        """Collector 配置可解析：OTLP HTTP 接收 + prometheus 导出。"""
        cfg = yaml.safe_load((REPO / "observability" / "otel-collector.yaml").read_text("utf-8"))
        self.assertIn("http", cfg["receivers"]["otlp"]["protocols"])
        exporters = cfg["exporters"]
        self.assertIn("prometheus", exporters)
        self.assertEqual(
            exporters["prometheus"]["endpoint"], "0.0.0.0:8889", "与 prometheus.yml 抓取目标一致"
        )
        self.assertEqual(cfg["service"]["pipelines"]["metrics"]["exporters"][0], "prometheus")

    def test_prometheus_scrapes_collector(self) -> None:
        """Prometheus 抓取 target 与 collector 的 exporter 端口一致。"""
        cfg = yaml.safe_load((REPO / "observability" / "prometheus.yml").read_text("utf-8"))
        targets = cfg["scrape_configs"][0]["static_configs"][0]["targets"]
        self.assertIn("otel-collector:8889", targets)


if __name__ == "__main__":
    unittest.main()
