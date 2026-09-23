"""运行事件接缝（ADR-0031 D07）：EventSink 协议、图侧待提交事件与同源归约。

图与时间线由同一事件流归约（D07）：运行图（节点/边到达状态）与时间线
（按 seq 的事件序列）必须来自同一 list[RunEventRecord]，不从 UI 时间推断执行；
定义图上未到达的节点显示「未执行」（not_reached），「skipped」只来自显式
NODE_SKIPPED 事件（只有确定的分支裁决才产生 skipped，不编造开始/结束时间）。

边界
----
- 图侧提交的 RunEvent 是「待写事实」：seq/event_id/occurred_at/release_id 一律
  由服务端（ControlStore）在事务内分配，图侧不携带、不覆盖。
- payload 是脱敏摘要通道：问句、SQL、结果行、Prompt 一律不得进入（D07）。
- EventSink.append 写入失败必须抛异常（fail closed）：控制状态/强制审计写失败
  时，下一次 SQL 不得启动；调用方（图节点接缝）不得吞掉写入异常。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, Protocol

from pydantic import JsonValue

from serving.control.contracts import EventType, RunEventRecord

if TYPE_CHECKING:
    from serving.control.store import ControlStore


@dataclass(frozen=True)
class RunEvent:
    """图侧待提交事件；seq/occurred_at/event_id/release_id 由服务端分配。"""

    run_id: str
    event_type: EventType
    payload: Mapping[str, JsonValue] = field(default_factory=dict)
    node_id: str | None = None
    node_run_id: str | None = None
    parent_node_run_id: str | None = None
    attempt: int | None = None


class EventSink(Protocol):
    """事件提交接缝：返回事务分配的 seq；写入失败必须抛异常（fail closed）。"""

    def append(self, event: RunEvent) -> int: ...


class StoreEventSink:
    """ControlStore 适配器（唯一真实事件盘）：事务内写 run_events 并递增 seq。"""

    def __init__(self, store: ControlStore) -> None:
        self._store = store

    def append(self, event: RunEvent) -> int:
        record = self._store.append_event(
            event.run_id,
            event_type=event.event_type,
            payload=dict(event.payload),
            node_id=event.node_id,
            node_run_id=event.node_run_id,
            parent_node_run_id=event.parent_node_run_id,
            attempt=event.attempt,
        )
        return record.seq


NodeStatus = Literal["not_reached", "running", "finished", "failed", "skipped"]

_ENDED_NODE_STATUS: dict[str, NodeStatus] = {
    "NODE_FINISHED": "finished",
    "NODE_FAILED": "failed",
    "NODE_SKIPPED": "skipped",
}


@dataclass(frozen=True)
class NodeView:
    """归约出的单节点视图（同一 node_id 多次运行保留最近一次；edges 全量另记）。"""

    node_id: str
    status: NodeStatus = "not_reached"
    node_run_id: str | None = None
    started_seq: int | None = None
    ended_seq: int | None = None


@dataclass(frozen=True)
class RunEventReduction:
    """同源归约产物：运行图（nodes/edges）与时间线（timeline）出自同一事件流。"""

    last_seq: int
    nodes: dict[str, NodeView]
    edges: tuple[tuple[str, str], ...]
    timeline: tuple[RunEventRecord, ...]


def reduce_events(
    events: Sequence[RunEventRecord], *, defined_node_ids: Sequence[str] = ()
) -> RunEventReduction:
    """由同一事件流归约运行图与时间线（D07：图与时间线由相同事件归约）。

    参数
    ----
    events           : 同一 run 的事件（任意顺序；按 seq 升序归约，不重排事实）。
    defined_node_ids : 定义图节点清单（agent.graph.GRAPH_NODE_IDS）；未到达的
                       节点输出 status="not_reached"，不编造开始/结束时间。

    返回
    ----
    RunEventReduction：nodes / edges / timeline / last_seq 全部来自同一 events
    列表——单一事实源，不合并第二数据源（不做 UI 时间推断）。
    """
    ordered = sorted(events, key=lambda e: e.seq)
    nodes: dict[str, NodeView] = {
        node_id: NodeView(node_id=node_id) for node_id in defined_node_ids
    }
    edges: list[tuple[str, str]] = []
    for event in ordered:
        node_id = event.node_id
        if node_id is None:
            continue
        if event.event_type == "NODE_STARTED":
            nodes[node_id] = NodeView(
                node_id=node_id,
                status="running",
                node_run_id=event.node_run_id,
                started_seq=event.seq,
            )
        elif event.event_type in _ENDED_NODE_STATUS:
            current = nodes.get(node_id)
            nodes[node_id] = NodeView(
                node_id=node_id,
                status=_ENDED_NODE_STATUS[event.event_type],
                node_run_id=event.node_run_id,
                started_seq=current.started_seq if current else None,
                ended_seq=event.seq,
            )
        elif event.event_type == "EDGE_TAKEN":
            target = event.payload.get("to")
            if isinstance(target, str):
                edges.append((node_id, target))
    return RunEventReduction(
        last_seq=ordered[-1].seq if ordered else 0,
        nodes=nodes,
        edges=tuple(edges),
        timeline=tuple(ordered),
    )
