"""B4 · serving LLM 接线测试（ADR-0029 ⑤⑥）——契约加性、RBAC 403、接地叙述发货/回落。

零网络/零 GPU：自托管存活经 `self_hosted_probe` 注入、叙述客户端经
`narrative_client_factory` 注入假实现；`llm_config` 注入决定分级。核心不变量：
- `off`（及不带 llm 字段）响应**无 `narrative` 键**，与接入前逐字一致；
- 角色无该意图 → 403（⑥，不静默降级）；
- 无自托管 → FALLBACK_TEMPLATE → `narrative.fallback=true`（③，绝不升级云）；
- grounded=false（冒出 payload 外新数字）→ 丢 LLM 文本发模板（N1）。
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from agent.graph import DataAgent
from agent.llm_policy import LlmConfig
from agent.security.sql_guard import Budget
from serving.api import API_PREFIX, create_app
from serving.audit import AuditLog
from serving.auth import ROLE_DIRECTORY, caps_for_role, sign_token

REPO = Path(__file__).resolve().parent.parent
SECRET = "llm-serving-test-secret"
GOLD102_Q = "按分支统计 2013 年佣金收入，列出前 5 名"

_META = json.loads((REPO / "data/snapshots" / "7d48dcb.meta.json").read_text(encoding="utf-8"))
ALLOWED = frozenset(
    f"atlas.{ns}.{table}" for ns, tables in _META["row_counts"].items() for table in tables
)
BUDGET = Budget(dialect="doris", max_rows=10_000, allowed_tables=ALLOWED)


class FakeExecutor:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, sql: str) -> tuple[list[tuple[object, ...]], list[str]]:
        self.calls.append(sql)
        return [("v",)], ["v"]


class _FakeNarrClient:
    """注入式叙述客户端：返回预置文本（零网络）。"""

    def __init__(self, text: str) -> None:
        self._text = text
        self.called = 0

    def complete(self, system: str, user: str, model: str) -> tuple[str, dict[str, int]]:
        self.called += 1
        return self._text, {"prompt_tokens": 5, "completion_tokens": 3}


class _LlmServingCase(unittest.TestCase):
    def setUp(self) -> None:
        self._secret_was = "ATLAS_JWT_SECRET" in os.environ
        self._secret_orig = os.environ.get("ATLAS_JWT_SECRET")
        os.environ["ATLAS_JWT_SECRET"] = SECRET
        self._tmp = tempfile.TemporaryDirectory(prefix="atlas-llm-")
        self.audit = AuditLog(Path(self._tmp.name))
        self.agent = DataAgent(executor=FakeExecutor(), budget=BUDGET)

    def tearDown(self) -> None:
        self._tmp.cleanup()
        if self._secret_was:
            os.environ["ATLAS_JWT_SECRET"] = self._secret_orig or ""
        else:
            os.environ.pop("ATLAS_JWT_SECRET", None)

    def _auth(
        self, role: str = "hq_admin", context: dict[str, Any] | None = None
    ) -> dict[str, str]:
        return {"Authorization": f"Bearer {sign_token(role, context or {}, secret=SECRET)}"}

    def _client(self, **kwargs: Any) -> TestClient:
        kwargs.setdefault("agent_factory", lambda _m: self.agent)
        kwargs.setdefault("audit", self.audit)
        return TestClient(create_app(**kwargs))

    def _ask(
        self,
        client: TestClient,
        body: dict[str, Any],
        role: str = "hq_admin",
        context: dict[str, Any] | None = None,
    ) -> Any:
        return client.post(f"{API_PREFIX}/ask", json=body, headers=self._auth(role, context))


class TestOffBackwardCompat(_LlmServingCase):
    def test_off_has_no_narrative_key(self) -> None:
        """不带 llm / llm=off → 响应无 narrative 键且键集不变（off 逐字向后兼容）。"""
        with self._client() as client:
            base = self._ask(client, {"question": GOLD102_Q}).json()
            off = self._ask(client, {"question": GOLD102_Q, "llm": "off"}).json()
        self.assertNotIn("narrative", base)
        # session_id/轮数逐请求随机，不能比全 body；键集相等证明 off 未新增任何字段
        self.assertEqual(set(base), set(off), "off 与缺省字段响应的键集必须一致")

    def test_invalid_llm_value_422(self) -> None:
        """未知意图 → pydantic 422（Literal 约束；不落到策略层）。"""
        with self._client() as client:
            resp = self._ask(client, {"question": GOLD102_Q, "llm": "bogus"})
        self.assertEqual(resp.status_code, 422)


class TestRbacGate(_LlmServingCase):
    def test_narrative_denied_for_route_only_role(self) -> None:
        """branch_manager 无 narrate → llm=narrative 直接 403（⑥，不静默降级）。"""
        with self._client() as client:
            resp = self._ask(
                client,
                {"question": GOLD102_Q, "llm": "narrative"},
                "branch_manager",
                {"branch": "east"},
            )
        self.assertEqual(resp.status_code, 403)
        self.assertIn("narrative", resp.json()["detail"])

    def test_candidate_fallback_gate_no_change(self) -> None:
        """hq_admin 有 route → candidate-fallback 200，回答不变、无 narrative 键（仅门控）。"""
        with self._client() as client:
            resp = self._ask(client, {"question": GOLD102_Q, "llm": "candidate-fallback"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["kind"], "answer")
        self.assertNotIn("narrative", resp.json())

    def test_caps_mapping(self) -> None:
        """角色→能力映射（⑥ 配置）：narrate 仅 hq_admin/region_manager/category_analyst。"""
        narrate = {r for r, s in ROLE_DIRECTORY.items() if "narrative" in s.llm}
        self.assertEqual(narrate, {"hq_admin", "region_manager", "category_analyst"})
        self.assertTrue(all("candidate-fallback" in s.llm for s in ROLE_DIRECTORY.values()))
        self.assertEqual(caps_for_role("branch_manager"), frozenset({"candidate-fallback"}))
        self.assertEqual(caps_for_role("ghost_role"), frozenset())


class TestNarrativeFailClosed(_LlmServingCase):
    def test_no_self_hosted_falls_back_to_template(self) -> None:
        """未配置自托管 → FALLBACK_TEMPLATE → narrative.fallback=true（③，绝不升级云）。"""
        with self._client(llm_config=LlmConfig()) as client:
            resp = self._ask(client, {"question": GOLD102_Q, "llm": "narrative"})
        block = resp.json()["narrative"]
        self.assertEqual(block["fallback"], True)
        self.assertEqual(block["grounded"], False)
        self.assertEqual(block["reason_code"], "self_hosted_unavailable")
        self.assertEqual(block["tier"], "none")

    def test_grounded_text_shipped_with_provenance(self) -> None:
        """自托管可用 + 无新数字文本 → 发货，tier=self_hosted、带 model（① 溯源）。"""
        cfg = LlmConfig.with_self_hosted(
            base_url="http://vllm:8000/v1", model_name="atlas-instruct"
        )
        with self._client(
            llm_config=cfg,
            self_hosted_probe=lambda _c: True,
            narrative_client_factory=lambda _ep: _FakeNarrClient("East 分支佣金收入最高。"),
        ) as client:
            resp = self._ask(client, {"question": GOLD102_Q, "llm": "narrative"})
        block = resp.json()["narrative"]
        self.assertEqual(block["grounded"], True)
        self.assertEqual(block["fallback"], False)
        self.assertEqual(block["tier"], "self_hosted")
        self.assertEqual(block["model"], "atlas-instruct")
        self.assertEqual(block["text"], "East 分支佣金收入最高。")

    def test_novel_number_dropped_to_template(self) -> None:
        """LLM 冒出 payload 外新数字 → grounded=false、发模板（N1 硬闸）。"""
        cfg = LlmConfig.with_self_hosted(
            base_url="http://vllm:8000/v1", model_name="atlas-instruct"
        )
        fake = _FakeNarrClient("总额高达 987654321 元。")
        with self._client(
            llm_config=cfg,
            self_hosted_probe=lambda _c: True,
            narrative_client_factory=lambda _ep: fake,
        ) as client:
            resp = self._ask(client, {"question": GOLD102_Q, "llm": "narrative"})
        block = resp.json()["narrative"]
        self.assertEqual(block["grounded"], False)
        self.assertEqual(block["fallback"], True)
        self.assertEqual(block["reason_code"], "ungrounded")
        self.assertEqual(fake.called, 1)  # 真调了模型，但其文本被丢弃


if __name__ == "__main__":
    unittest.main()
