#!/usr/bin/env python3
"""SPA 静态面契约测试（P0b，ADR-0018 决策 ②⑤；无 DB）。

两态 × 三态分流的完整矩阵。注入面：mock.patch("serving.api.FRONTEND_DIST")——
create_app 不新增该构造参数（保持 API 表面不变）；**两态都必须显式注入**：
本机跑过 `make ui-build` 后真实 FRONTEND_DIST 存在，"无 dist 态"不能依赖环境
（fresh clone 与本地机器上的测试结果必须一致）。

有 dist 态（tmp 目录构造最小产物 index.html + assets/app.js）：
- SPA 深路径 /governance/metrics → 200 HTML（0018 判据 7 前半；P1 浏览器走查同形态）；
- 根 / → 200 HTML；
- 真实文件 /assets/app.js → 200 直出（不是 index.html 回落，两条分支可区分）；
- GET /api/v1/<不存在路径> → **404 JSON**（判据 7 后半：fallback 未吞 API 404）；
- 裸 /api → 404 JSON（前缀判定含裸段）；
- POST /plan 等 4 条旧无前缀路径 → 404（非 GET 分支——0022 判据 1 的硬切不被
  catch-all 破坏）；
- GET /api/v1/plan → 404（**已登记归并**：已知路径错误方法 405→404，见
  serving/api.py `_attach_spa` docstring 的「已知副作用」段；此处显式锁行为）；
- 路径穿越编码 → 不回仓外文件（守卫见 `spa_catch_all`）。

无 dist 态（FRONTEND_DIST 锚到不存在路径）：
- create_app 打 warning（条件挂载的验收形态；0018 决策 ⑤）；
- 深路径 → 404 JSON（无 fallback = API 默认行为）；/api/v1/health → 200（API 不受影响）。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from serving.api import create_app
from serving.audit import AuditLog


class _BaseCase(unittest.TestCase):
    def setUp(self) -> None:
        self._audit_tmp = tempfile.TemporaryDirectory(prefix="atlas-spa-audit-")
        self._dist_tmp = tempfile.TemporaryDirectory(prefix="atlas-spa-dist-")
        self.audit = AuditLog(Path(self._audit_tmp.name))

    def tearDown(self) -> None:
        self._audit_tmp.cleanup()
        self._dist_tmp.cleanup()

    def _build_dist(self) -> Path:
        """最小 vite 产物形态（index.html + assets/app.js；直出分支用）。"""
        dist = Path(self._dist_tmp.name)
        (dist / "assets").mkdir(exist_ok=True)
        (dist / "index.html").write_text(
            "<!doctype html><title>Atlas 测试壳</title>", encoding="utf-8"
        )
        (dist / "assets" / "app.js").write_text("console.log('atlas');", encoding="utf-8")
        return dist

    def _client(self, dist: Path) -> TestClient:
        """按指定 FRONTEND_DIST 构造 app（patch 只需覆盖 create_app 调用期）。"""
        with mock.patch("serving.api.FRONTEND_DIST", dist):
            app = create_app(agent_factory=lambda _m: None, audit=self.audit)
        return TestClient(app)


class TestSpaWithDist(_BaseCase):
    """有 dist 态：catch-all 三态分流（深路径 HTML / 文件直出 / API 空间 404 JSON）。"""

    def setUp(self) -> None:
        super().setUp()
        self.dist = self._build_dist()
        self.client = self._client(self.dist)
        self.addCleanup(self.client.close)

    def test_deep_path_returns_html_200(self) -> None:
        resp = self.client.get("/governance/metrics")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/html", resp.headers["content-type"])
        self.assertIn("Atlas 测试壳", resp.text)

    def test_root_returns_html_200(self) -> None:
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/html", resp.headers["content-type"])

    def test_real_asset_file_is_served_directly(self) -> None:
        resp = self.client.get("/assets/app.js")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("console.log", resp.text)
        self.assertNotIn("Atlas 测试壳", resp.text)

    def test_unknown_api_path_is_404_json_not_html(self) -> None:
        """判据 7 后半：fallback 不得把 API 404 吞成 HTML 200。"""
        resp = self.client.get("/api/v1/definitely-not-a-route")
        self.assertEqual(resp.status_code, 404)
        self.assertIn("application/json", resp.headers["content-type"])
        self.assertEqual(resp.json(), {"detail": "Not Found"})

    def test_bare_api_prefix_is_404_json(self) -> None:
        resp = self.client.get("/api")
        self.assertEqual(resp.status_code, 404)
        self.assertIn("application/json", resp.headers["content-type"])

    def test_unprefixed_business_paths_still_404(self) -> None:
        """0022 判据 1 的硬切不被 catch-all 破坏（POST 落非 GET 分支）。"""
        for old in ("/plan", "/compile", "/ask", "/plan/execute"):
            with self.subTest(path=old):
                resp = self.client.post(old, json={})
                self.assertEqual(resp.status_code, 404, f"{old} 被 SPA fallback 吞掉")

    def test_wrong_method_on_known_api_path_collapses_to_404(self) -> None:
        """已登记归并：GET /api/v1/plan 原 405 → 现 404（契约未承诺 405）。

        锁行为而非"修"为 405：全方法 catch-all 的判定顺序（FULL 先于 PARTIAL）
        决定该归并，属 serving/api.py `_attach_spa` docstring 登记的已知副作用。
        """
        resp = self.client.get("/api/v1/plan")
        self.assertEqual(resp.status_code, 404)

    def test_path_traversal_does_not_escape_dist(self) -> None:
        """编码的 `..` 段（httpx 不归一化 %2e%2e）不得读到 dist 之外的仓内文件。"""
        resp = self.client.get("/%2e%2e/%2e%2e/pyproject.toml")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Atlas 测试壳", resp.text)

    def test_registered_api_routes_unaffected(self) -> None:
        self.assertEqual(self.client.get("/api/v1/health").status_code, 200)
        self.assertEqual(self.client.get("/health").status_code, 200)


class TestSpaWithoutDist(_BaseCase):
    """无 dist 态：条件挂载跳过 + warning；API 不受影响（0018 决策 ⑤）。"""

    def setUp(self) -> None:
        super().setUp()
        self.missing = Path(self._dist_tmp.name) / "not-built"
        self.client = self._client(self.missing)
        self.addCleanup(self.client.close)

    def test_missing_dist_logs_warning(self) -> None:
        with (
            mock.patch("serving.api.FRONTEND_DIST", self.missing),
            self.assertLogs("serving.api", level="WARNING") as cm,
        ):
            create_app(agent_factory=lambda _m: None, audit=self.audit)
        self.assertTrue(any("frontend/dist" in line for line in cm.output), cm.output)

    def test_deep_path_is_default_404_without_fallback(self) -> None:
        resp = self.client.get("/governance/metrics")
        self.assertEqual(resp.status_code, 404)
        self.assertIn("application/json", resp.headers["content-type"])

    def test_api_unaffected(self) -> None:
        self.assertEqual(self.client.get("/api/v1/health").status_code, 200)


if __name__ == "__main__":
    unittest.main()
