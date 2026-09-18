"""④a `/analyze/stream` SSE 端点契约测试（ADR-0028 决策④ · dev-plan B0）。

被测目标：`POST /api/v1/analyze/stream`——compute-then-stream 流式端点
（执行模型 A）：复用 `/analyze` 同一执行路径跑完 `agent.analyze()`（SQL 已全部
经 Guard），释放锁后把已算好的分步产物按 `docs/design/agui-event-mapping.md`
§2/§4 词表**回放**为 SSE。

红线（本文件即机读判据，见 dev-plan §5）：
- **N3 单一执行通道**：流式端点**绝不**新增 SQL 构造/执行——唯一路径 = 调
  `agent.analyze()`（其 `node_execute` 过 Guard）。故本文件对 `analyze_stream`
  源码做静态守卫：不得出现任何执行原语或新编译入口。
- **只承载展示**：事件 data 全部源自 `AnalysisResult`；被拒步无 `TOOL_CALL_RESULT`
  （被拒 SQL 不出网，沿用接缝 blocked 语义）。
- **借鉴词表 ≠ AG-UI 兼容**（N2）：本测试只断言事件名与已登记词表一一对应。

B0 相位说明：端点尚未实现（B1 落地），本文件当前**预期红**——路由缺失 / 事件名
无法解析。既有 `test_api_contract_v2.py` 的 17→18 同步在 B1 与实现同批进行（三处
一致），此处用 `assertIn` 单独断言，不预先打破既有契约测试。全部走 fake 执行器，
不碰网络与 Doris；数字为合成测试输入（N1）。
"""

from __future__ import annotations

import inspect
import json
import os
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from agent.analysis import ANALYSIS_ROLES
from serving.api import API_PREFIX, _analysis_payload, create_app
from serving.audit import AuditLog
from serving.auth import sign_token
from serving.ratelimit import RateLimiter

# 复用分析编排已验证的 fake 夹具（不重造桩，避免伪造测试）。
from tests.test_analysis_agent import FULL_Q, AnalysisExecutor, _agent

API = API_PREFIX
STREAM_PATH = "/api/v1/analyze/stream"
SECRET = "analyze-stream-contract-secret"

# 前端 `frontend/src/api/types.ts` 的 TurnPayload 接口键（TS 无法从 Python 导入，
# 此处镜像字面量作超集断言——STATE_SNAPSHOT 必覆盖前端消费的全部字段）。
TURN_PAYLOAD_KEYS = frozenset(
    {
        "kind",
        "session_id",
        "question",
        "turns_in_session",
        "metric",
        "sql",
        "columns",
        "rows",
        "row_count",
        "latency_ms",
        "engine",
        "path",
        "usage",
        "validation_issues",
        "explanation",
        "chart",
        "clarification",
        "block_reason",
        "error",
        "handoff_reason",
        "snapshot_sha",
        "snapshot_bound_to_head",
        "analysis",
    }
)

# 词表已登记的 AG-UI 事件类型（docs/design/agui-event-mapping.md §2/§4，借鉴非兼容）
RUN_STARTED = "RUN_STARTED"
STEP_STARTED = "STEP_STARTED"
STEP_FINISHED = "STEP_FINISHED"
TOOL_CALL_RESULT = "TOOL_CALL_RESULT"
STATE_SNAPSHOT = "STATE_SNAPSHOT"
RUN_FINISHED = "RUN_FINISHED"
RUN_ERROR = "RUN_ERROR"
VOCAB = frozenset(
    {
        RUN_STARTED,
        STEP_STARTED,
        STEP_FINISHED,
        TOOL_CALL_RESULT,
        STATE_SNAPSHOT,
        RUN_FINISHED,
        RUN_ERROR,
    }
)


def _expected_event_names() -> list[str]:
    """四步全成 → 期望事件名序列（结构层，与载荷解耦）。

    RUN_STARTED → 每个 role 一对 STEP_STARTED/STEP_FINISHED（成功步中间插
    TOOL_CALL_RESULT）→ STATE_SNAPSHOT → RUN_FINISHED。
    """
    names: list[str] = [RUN_STARTED]
    for _role in ANALYSIS_ROLES:
        names.append(STEP_STARTED)
        names.append(TOOL_CALL_RESULT)  # 全成：每步都有已执行事实
        names.append(STEP_FINISHED)
    names.append(STATE_SNAPSHOT)
    names.append(RUN_FINISHED)
    return names


def _parse_sse(text: str) -> list[str]:
    """把 SSE 文本解析为事件名序列（只认 ``event:`` 行，逐 ``\\n\\n`` 分帧）。"""
    names: list[str] = []
    for frame in text.split("\n\n"):
        for line in frame.splitlines():
            if line.startswith("event: "):
                names.append(line[len("event: ") :].strip())
                break
    return names


def _payloads(text: str, want_event: str) -> list[dict]:
    """取出指定事件的所有 ``data:`` JSON 载荷。"""
    out: list[dict] = []
    for frame in text.split("\n\n"):
        ev = None
        data = None
        for line in frame.splitlines():
            if line.startswith("event: "):
                ev = line[len("event: ") :].strip()
            elif line.startswith("data: ") and ev == want_event:
                data = json.loads(line[len("data: ") :])
        if ev == want_event and data is not None:
            out.append(data)
    return out


class _BaseCase(unittest.TestCase):
    """共享注入面：ATLAS_JWT_SECRET 环境恢复 + tmp 审计目录 + fake 分析 agent。"""

    def setUp(self) -> None:
        self._secret_was_set = "ATLAS_JWT_SECRET" in os.environ
        self._secret_orig = os.environ.get("ATLAS_JWT_SECRET")
        os.environ["ATLAS_JWT_SECRET"] = SECRET
        self._audit_tmp = tempfile.TemporaryDirectory(prefix="atlas-stream-")
        self.audit = AuditLog(Path(self._audit_tmp.name))
        self.executor = AnalysisExecutor()
        self.agent = _agent(self.executor)
        self.agent_result = None  # _stream_text 内捕获，供终态一致性断言

    def tearDown(self) -> None:
        self._audit_tmp.cleanup()
        if self._secret_was_set:
            os.environ["ATLAS_JWT_SECRET"] = self._secret_orig or ""
        else:
            os.environ.pop("ATLAS_JWT_SECRET", None)

    def _auth(self, role: str = "hq_admin") -> dict[str, str]:
        return {"Authorization": f"Bearer {sign_token(role, {}, secret=SECRET)}"}

    def _client(self) -> TestClient:
        app = create_app(
            agent_factory=lambda _m: self.agent,
            audit=self.audit,
            rate_limiter=RateLimiter(max_requests=10_000, window_seconds=60),
            governance_rate_limiter=RateLimiter(max_requests=10_000, window_seconds=60),
        )
        client = TestClient(app)
        self.addCleanup(client.close)
        return client

    def _stream_text(self) -> str:
        resp = self._client().post(
            f"{API}/analyze/stream", json={"question": FULL_Q}, headers=self._auth()
        )
        self.assertEqual(resp.status_code, 200, f"流式端点未可达：{resp.status_code}")
        self.assertTrue(
            resp.headers["content-type"].startswith("text/event-stream"),
            f"content-type 非 SSE：{resp.headers.get('content-type')}",
        )
        self.agent_result = self.agent.analyze(FULL_Q, session_id="stream-cap")
        return resp.text


class TestStreamRoute(_BaseCase):
    """路由登记（三处一致的 OpenAPI 侧；契约测试翻 18 见 B1）。"""

    def test_analyze_stream_path_is_registered(self) -> None:
        app = create_app(agent_factory=lambda _m: None, audit=self.audit)
        paths = set(app.openapi()["paths"])
        self.assertIn(STREAM_PATH, paths, f"{STREAM_PATH} 未进 OpenAPI paths（④a 未落地）")


class TestStreamEventSequence(_BaseCase):
    """事件序列对齐词表 + 只承载展示（N3 / N2）。"""

    def test_event_sequence_matches_role_order(self) -> None:
        names = _parse_sse(self._stream_text())
        self.assertEqual(names, _expected_event_names(), "SSE 事件名序列与词表编排不符")

    def test_every_event_is_in_registered_vocab(self) -> None:
        names = _parse_sse(self._stream_text())
        self.assertLessEqual(set(names), VOCAB, f"出现未登记事件名：{set(names) - VOCAB}")

    def test_state_snapshot_is_full_turn_payload(self) -> None:
        """STATE_SNAPSHOT 直发 `/analyze` 同一份父轮 TurnPayload（单源·零重构）。"""
        snapshots = _payloads(self._stream_text(), STATE_SNAPSHOT)
        self.assertEqual(len(snapshots), 1, "全成流恰一个终态 STATE_SNAPSHOT")
        snap = snapshots[0]
        self.assertIsInstance(snap, dict, "STATE_SNAPSHOT 载荷须为对象")
        self.assertLessEqual(
            TURN_PAYLOAD_KEYS, set(snap), "STATE_SNAPSHOT 须为完整 TurnPayload 形态（含前端消费键）"
        )
        self.assertIn("analysis", snap, "父轮 TurnPayload 必含 analysis 键")
        # analysis 即 `_analysis_payload` 的 17 键投影（与 request/response 同函数）
        self.assertEqual(set(snap["analysis"]), set(_analysis_payload(self.agent_result)))

    def test_run_started_is_first_and_run_finished_last(self) -> None:
        names = _parse_sse(self._stream_text())
        self.assertEqual(names[0], RUN_STARTED, "流以 RUN_STARTED 开")
        self.assertEqual(names[-1], RUN_FINISHED, "成功流以 RUN_FINISHED 关")


class TestN3GuardStatic(_BaseCase):
    """N3 静态守卫：`/analyze/stream` 的闭包 handler 不得引入新执行通道。

    handler 是 `create_app` 内的闭包（与 `/ask` / `/analyze` 同构，非模块属性）——
    本类经注册路由定位**真实 handler** 并对其源码做静态断言：唯一执行入口须是
    `agent.analyze(`，绝不出现执行原语 / DML / 新编译入口（N3 单通道红线）。
    """

    FORBIDDEN = (
        ".execute(",
        "node_execute",
        "INSERT",
        "UPDATE",
        "DELETE",
        "DROP",
        "compile_plan(",
        "build_graph(",
    )

    def _endpoint(self):
        app = create_app(agent_factory=lambda _m: None, audit=self.audit)
        for route in app.routes:
            methods = {getattr(m, "value", m) for m in getattr(route, "methods", set())}
            if getattr(route, "path", None) == STREAM_PATH and "POST" in methods:
                return route.endpoint
        return None

    def test_stream_route_is_registered(self) -> None:
        self.assertIsNotNone(self._endpoint(), f"未找到 {STREAM_PATH} 的注册 handler")

    def test_handler_introduces_no_execution_primitive(self) -> None:
        endpoint = self._endpoint()
        self.assertIsNotNone(endpoint, "④a 未实现：无 handler 可静态审计")
        src = "\n".join(line.strip() for line in inspect.getsource(endpoint).splitlines())
        for token in self.FORBIDDEN:
            self.assertNotIn(token, src, f"N3 违规：出现执行/新编译原语 {token!r}")

    def test_handler_delegates_to_agent_analyze(self) -> None:
        endpoint = self._endpoint()
        self.assertIsNotNone(endpoint, "④a 未实现：无 handler 可静态审计")
        src = "\n".join(line.strip() for line in inspect.getsource(endpoint).splitlines())
        self.assertIn("agent.analyze(", src, "须复用 agent.analyze 作为唯一执行入口")


if __name__ == "__main__":
    unittest.main()
