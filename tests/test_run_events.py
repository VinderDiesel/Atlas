"""T05 运行事件契约：seq 事务递增、终态事件同事务、append-only（真实 SQLite）。

图接缝（T05b）：真实图执行产生的事件流、内部子调用留痕、同源归约与
fail-closed（事件盘失败禁止下一 SQL）；全部基于真实 store，仅执行器为 spy。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

import pytest


def _store(tmp_path: Path):
    from serving.control.store import ControlStore

    store = ControlStore(tmp_path / "state" / "control.sqlite")
    store.migrate()
    return store


def _owner(subject: str = "viewer"):
    from serving.control.contracts import Owner

    return Owner(issuer="https://issuer.example", subject=subject)


def _run(store, *, client_request_id: str = "req-1"):
    from serving.control.contracts import content_digest

    store.create_deployment(
        "finance", owner=_owner("publisher"), scope="finance", source_id="atlas_finance"
    )
    run, _ = store.create_run(
        owner=_owner(),
        deployment_id="finance",
        mode="ask",
        session_id="session-1",
        client_request_id=client_request_id,
        request_digest=content_digest({"question": "总交易额"}),
    )
    return run


def test_append_event_assigns_gapless_sequence_and_bumps_last_seq(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = _run(store)
    first = store.append_event(run.run_id, event_type="RUN_ACCEPTED", payload={})
    second = store.append_event(run.run_id, event_type="RUN_STARTED", payload={"node": "plan"})
    assert (first.seq, second.seq) == (1, 2)
    assert first.event_id != second.event_id
    # 事件时间由服务端时钟生成；必须带显式时区（不编造、不错标 UTC 为本地）
    assert datetime.fromisoformat(first.occurred_at).tzinfo is not None
    assert store.get_run(run.run_id).last_seq == 2
    events = store.list_events(run.run_id)
    assert [e.event_type for e in events] == ["RUN_ACCEPTED", "RUN_STARTED"]


def test_unknown_event_type_is_rejected_without_consuming_sequence(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = _run(store)
    with pytest.raises(ValueError):
        store.append_event(run.run_id, event_type="TOTALLY_MADE_UP", payload={})
    assert store.get_run(run.run_id).last_seq == 0
    # 拒绝的失败不产生空洞：下一次合法 append 仍是 seq=1
    assert store.append_event(run.run_id, event_type="RUN_STARTED", payload={}).seq == 1


def test_events_for_missing_run_are_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(KeyError):
        store.append_event("no-such-run", event_type="RUN_STARTED", payload={})


def test_finalize_writes_terminal_event_in_same_snapshot(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = _run(store)
    store.append_event(run.run_id, event_type="RUN_ACCEPTED", payload={})
    store.finalize_run(
        run.run_id, status="succeeded", result_kind="answer", availability="available"
    )
    events = store.list_events(run.run_id)
    final = events[-1]
    assert final.event_type == "RUN_FINISHED"
    # 终态、终态事件与 last_seq 一致（单事务写入的可核对性）
    assert final.seq == store.get_run(run.run_id).last_seq == 2
    assert final.payload.get("status") == "succeeded"
    assert final.payload.get("result_kind") == "answer"


def test_recovery_writes_interrupted_event_for_each_sealed_run(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = _run(store)
    store.append_event(run.run_id, event_type="RUN_ACCEPTED", payload={})
    assert store.mark_interrupted() == 1
    events = store.list_events(run.run_id)
    assert events[-1].event_type == "RUN_INTERRUPTED"
    assert events[-1].seq == store.get_run(run.run_id).last_seq == 2


def test_events_can_be_resumed_after_seq(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = _run(store)
    for index in range(4):
        store.append_event(run.run_id, event_type="NODE_STARTED", payload={"index": index})
    tail = store.list_events(run.run_id, after_seq=2)
    assert [e.seq for e in tail] == [3, 4]


def test_run_events_are_append_only(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = _run(store)
    store.append_event(run.run_id, event_type="RUN_ACCEPTED", payload={})
    with sqlite3.connect(store.path) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE run_events SET payload_json = '{}'")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM run_events")
        assert conn.execute("SELECT COUNT(*) FROM run_events").fetchone()[0] == 1


# ---------------------------------------------------------------------------
# 图接缝（T05b）：真实事件流、子调用留痕、同源归约与 fail-closed
# ---------------------------------------------------------------------------

DETERMINISTIC_Q = "按分支统计 2013 年佣金收入，列出前 5 名"
OUT_OF_DOMAIN_Q = "2013年各分支机构的绩效奖金总额排名"


def _budget():
    """与 test_graph.py 同口径：只能触碰已锁快照的表。"""
    import json

    from agent.security.sql_guard import Budget

    repo = Path(__file__).resolve().parent.parent
    meta = json.loads((repo / "data/snapshots" / "7d48dcb.meta.json").read_text(encoding="utf-8"))
    allowed = frozenset(
        f"atlas.{ns}.{table}" for ns, tables in meta["row_counts"].items() for table in tables
    )
    return Budget(dialect="doris", max_rows=10_000, allowed_tables=allowed)


def _run_config(run, *, parent: str | None = None) -> dict:
    payload: dict = {"run_id": run.run_id}
    if parent is not None:
        payload["parent_node_run_id"] = parent
    # 与真实运行上下文同构（T05c）：thread_id 管会话续接（build_graph 默认
    # MemorySaver 要求），atlas_run 管事件归属——两键同传，互不代替
    return {"configurable": {"thread_id": f"run:{run.run_id}", "atlas_run": payload}}


def _traced_graph(store, executor, **kwargs):
    """真实图 + 真实 store sink（仅外部执行器为 spy）。"""
    from agent.graph import build_graph
    from serving.control.events import StoreEventSink

    return build_graph(
        executor=executor, budget=_budget(), event_sink=StoreEventSink(store), **kwargs
    )


def test_deterministic_turn_emits_ordered_real_events(tmp_path: Path) -> None:
    from tests.workbench_support import ExecutorSpy

    store = _store(tmp_path)
    run = _run(store)
    spy = ExecutorSpy()
    graph = _traced_graph(store, spy)
    graph.invoke({"question": DETERMINISTIC_Q, "session_id": "session-1"}, config=_run_config(run))
    events = store.list_events(run.run_id)
    assert [e.seq for e in events] == list(range(1, len(events) + 1))
    starts = [e for e in events if e.event_type == "NODE_STARTED"]
    finishes = [e for e in events if e.event_type == "NODE_FINISHED"]
    assert [e.node_id for e in starts] == ["plan", "execute", "explain"]
    assert [e.node_id for e in finishes] == ["plan", "execute", "explain"]
    # 同一节点运行的 start/finish 用同一 node_run_id；每次进入节点独立
    assert starts[0].node_run_id == finishes[0].node_run_id
    assert len({e.node_run_id for e in starts}) == 3
    edges = [(e.node_id, e.payload["to"]) for e in events if e.event_type == "EDGE_TAKEN"]
    assert edges == [("plan", "execute"), ("execute", "explain")]


def test_executor_calls_match_tool_events(tmp_path: Path) -> None:
    """退出门：一条查询的调用次数能与事件核对（不靠猜）。"""
    from tests.workbench_support import ExecutorSpy

    store = _store(tmp_path)
    run = _run(store)
    spy = ExecutorSpy()
    graph = _traced_graph(store, spy)
    graph.invoke({"question": DETERMINISTIC_Q, "session_id": "session-1"}, config=_run_config(run))
    events = store.list_events(run.run_id)
    started = [
        e
        for e in events
        if e.event_type == "TOOL_STARTED" and e.payload.get("name") == "execute_plan"
    ]
    finished = [
        e
        for e in events
        if e.event_type == "TOOL_FINISHED" and e.payload.get("name") == "execute_plan"
    ]
    assert len(spy.calls) == 1
    assert len(started) == 1 and len(finished) == 1
    assert finished[0].payload.get("kind") == "ok"
    # 工具事件归属同一次节点运行（node_run_id 可核对）
    assert started[0].node_run_id == finished[0].node_run_id


def test_internal_retrieval_and_generation_leave_traces(tmp_path: Path) -> None:
    """内部检索与模型子调用同样留事件（不出现「图一查、生成器又查却不显示」）。"""
    from tests.test_graph import FakeGenerator, FakeLinker, _answer_plan
    from tests.workbench_support import ExecutorSpy

    store = _store(tmp_path)
    run = _run(store)
    graph = _traced_graph(
        store,
        ExecutorSpy(),
        allow_candidate=True,
        linker=FakeLinker(candidates=("commission_revenue",)),
        generator=FakeGenerator(plans=[_answer_plan()]),
    )
    graph.invoke({"question": OUT_OF_DOMAIN_Q, "session_id": "session-1"}, config=_run_config(run))
    events = store.list_events(run.run_id)
    tool_trace = [(e.event_type, e.payload.get("name")) for e in events if "TOOL" in e.event_type]
    assert tool_trace == [
        ("TOOL_STARTED", "schema_linking"),
        ("TOOL_FINISHED", "schema_linking"),
        ("TOOL_STARTED", "generate"),
        ("TOOL_FINISHED", "generate"),
        ("TOOL_STARTED", "execute_plan"),
        ("TOOL_FINISHED", "execute_plan"),
    ]


def test_event_write_failure_blocks_execution(tmp_path: Path) -> None:
    """fail closed：事件盘在 SQL 启动前失败 → 执行器零调用（已有结果不按成功交付）。"""
    from agent.graph import build_graph
    from serving.control.events import StoreEventSink
    from tests.workbench_support import ExecutorSpy

    class _FailingSink:
        def __init__(self, inner, fail_after: int) -> None:
            self._inner = inner
            self._fail_after = fail_after
            self.count = 0

        def append(self, event):
            self.count += 1
            if self.count > self._fail_after:
                raise RuntimeError("事件盘不可用（测试注入）")
            return self._inner.append(event)

    store = _store(tmp_path)
    run = _run(store)
    spy = ExecutorSpy()
    # 前 4 条（plan 两节点事件 + 边裁决 + execute 开始）成功，第 5 条（execute 的
    # TOOL_STARTED）失败 → execute_plan 不得被调用
    sink = _FailingSink(StoreEventSink(store), fail_after=4)
    graph = build_graph(executor=spy, budget=_budget(), event_sink=sink)
    with pytest.raises(RuntimeError):
        graph.invoke(
            {"question": DETERMINISTIC_Q, "session_id": "session-1"}, config=_run_config(run)
        )
    assert spy.calls == []
    assert [e.event_type for e in store.list_events(run.run_id)] == [
        "NODE_STARTED",
        "NODE_FINISHED",
        "EDGE_TAKEN",
        "NODE_STARTED",
    ]


def test_node_failure_is_recorded_and_reraised(tmp_path: Path) -> None:
    """节点异常：NODE_FAILED 留痕，原始异常必须原样传播（不被二次写入失败掩盖）。"""
    from tests.workbench_support import ExecutorSpy

    class _BoomLinker:
        def link(self, question: str, k: int = 5):
            raise ValueError("检索崩了（测试注入）")

    store = _store(tmp_path)
    run = _run(store)
    graph = _traced_graph(store, ExecutorSpy(), allow_candidate=True, linker=_BoomLinker())
    with pytest.raises(ValueError):
        graph.invoke(
            {"question": OUT_OF_DOMAIN_Q, "session_id": "session-1"}, config=_run_config(run)
        )
    events = store.list_events(run.run_id)
    assert events[-1].event_type == "NODE_FAILED"
    assert events[-1].node_id == "retrieve"


def test_same_event_stream_reduces_to_graph_and_timeline(tmp_path: Path) -> None:
    """图与时间线由相同事件归约；未到达节点 = not_reached（不编造 skipped）。"""
    from agent.graph import GRAPH_NODE_IDS
    from serving.control.events import reduce_events
    from tests.workbench_support import ExecutorSpy

    store = _store(tmp_path)
    run = _run(store)
    spy = ExecutorSpy()
    graph = _traced_graph(store, spy)
    graph.invoke({"question": DETERMINISTIC_Q, "session_id": "session-1"}, config=_run_config(run))
    # 定义图节点常量与实际装配同源
    assert set(GRAPH_NODE_IDS) <= set(graph.get_graph().nodes)
    events = store.list_events(run.run_id)
    reduction = reduce_events(events, defined_node_ids=GRAPH_NODE_IDS)
    assert reduction.last_seq == events[-1].seq
    assert reduction.nodes["plan"].status == "finished"
    assert reduction.nodes["execute"].status == "finished"
    assert reduction.nodes["explain"].status == "finished"
    for unreached in ("retrieve", "generate", "validate", "clarify", "handoff"):
        assert reduction.nodes[unreached].status == "not_reached"
    # 图（节点/边）与时间线共享同一事件流，seq 可回溯核对
    assert reduction.edges == (("plan", "execute"), ("execute", "explain"))
    assert tuple(e.seq for e in reduction.timeline) == tuple(e.seq for e in events)
    execute_started = next(
        e for e in events if e.event_type == "NODE_STARTED" and e.node_id == "execute"
    )
    assert reduction.nodes["execute"].started_seq == execute_started.seq
    assert reduction.nodes["execute"].node_run_id == execute_started.node_run_id


def test_persisted_events_contain_no_question_sql_or_rows(tmp_path: Path) -> None:
    """持久事件默认无正文：不含问句、SQL、结果值（脱敏摘要，D07）。"""
    import json

    from tests.workbench_support import ExecutorSpy

    store = _store(tmp_path)
    run = _run(store)
    spy = ExecutorSpy()
    spy.rows = [(987654321,)]
    graph = _traced_graph(store, spy)
    graph.invoke({"question": DETERMINISTIC_Q, "session_id": "session-1"}, config=_run_config(run))
    assert spy.calls  # 真的执行过 SQL，否则下列断言是空洞的
    events = store.list_events(run.run_id)
    raw = json.dumps([e.payload for e in events], ensure_ascii=False)
    assert "佣金收入" not in raw
    assert "SELECT" not in raw and "FROM" not in raw
    assert "987654321" not in raw
    for token in ("question", "sql", "rows", "prompt"):
        assert f'"{token}"' not in raw  # 事件 payload 不出现正文/敏感键（值级缩面）


def test_retried_generation_leaves_second_generate_trace(tmp_path: Path) -> None:
    """重试不消失：validate 内重生成一次 → 两组 generate 工具事件均可核对。"""
    from tests.test_graph import FakeGenerator, FakeLinker, FlakyCompiler, _answer_plan
    from tests.workbench_support import ExecutorSpy

    store = _store(tmp_path)
    run = _run(store)
    graph = _traced_graph(
        store,
        ExecutorSpy(),
        allow_candidate=True,
        linker=FakeLinker(candidates=("commission_revenue",)),
        generator=FakeGenerator(plans=[_answer_plan(), _answer_plan()]),
        compiler=FlakyCompiler(),
    )
    graph.invoke({"question": OUT_OF_DOMAIN_Q, "session_id": "session-1"}, config=_run_config(run))
    events = store.list_events(run.run_id)
    tool_trace = [(e.event_type, e.payload.get("name")) for e in events if "TOOL" in e.event_type]
    assert tool_trace == [
        ("TOOL_STARTED", "schema_linking"),
        ("TOOL_FINISHED", "schema_linking"),
        ("TOOL_STARTED", "generate"),
        ("TOOL_FINISHED", "generate"),
        ("TOOL_STARTED", "generate"),
        ("TOOL_FINISHED", "generate"),
        ("TOOL_STARTED", "execute_plan"),
        ("TOOL_FINISHED", "execute_plan"),
    ]


def test_events_carry_parent_run_for_subcall_association(tmp_path: Path) -> None:
    """子调用关联：atlas_run.parent_node_run_id 落到该次 invoke 的每条事件。"""
    from tests.workbench_support import ExecutorSpy

    store = _store(tmp_path)
    run = _run(store)
    graph = _traced_graph(store, ExecutorSpy())
    graph.invoke(
        {"question": DETERMINISTIC_Q, "session_id": "session-1"},
        config=_run_config(run, parent="parent-node-run-1"),
    )
    events = store.list_events(run.run_id)
    assert events  # 非空基线，否则 all() 是空洞的
    assert all(e.parent_node_run_id == "parent-node-run-1" for e in events)
