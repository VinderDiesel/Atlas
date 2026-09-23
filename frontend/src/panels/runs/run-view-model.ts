/**
 * T06d 运行展示纯逻辑（组件只做渲染，决策全在这里可测）。
 *
 * 纪律（与 run-view-model.test.ts 锁定）：
 * - **定义 vs 执行不混淆**：RUN_GRAPH_SPEC 是固定模板（节点与
 *   `agent/graph.py::GRAPH_NODE_IDS` 同源 8 节点、模板边取自装配拓扑），
 *   执行状态只来自 `reduceRunEvent` 归约的 RunViewState——模板边恒在
 *   （taken=false），只有 EDGE_TAKEN 走过的边标 taken。
 * - **不伪造答案**：`availabilityNotice` 只在 `available` 且有结果时返回 null
 *   （正常渲染结果区）；其余四态给明确文案，`available` 无结果给诚实兜底。
 * - **状态只来自事件**：`statusTag(null)`（未收事件）为「未知」，不猜运行中。
 * - **只读图固定布局**：坐标来自模板，不重排事实（T09 才开放编辑）。
 */
import type { ResultAvailability, RunNodeStatus, RunStatus } from "../../api/types";
import type { RunViewState } from "../../state/run-events";

/** 运行状态标签（AntD Tag 色名；null = 未收事件，不猜）。 */
export interface StatusTag {
  label: string;
  color: string;
}

export function statusTag(status: RunStatus | null): StatusTag {
  switch (status) {
    case "succeeded":
      return { label: "成功", color: "green" };
    case "failed":
      return { label: "失败", color: "red" };
    case "running":
      return { label: "运行中", color: "blue" };
    case "queued":
      return { label: "排队中", color: "default" };
    case "blocked":
      return { label: "已拦截", color: "orange" };
    case "interrupted":
      return { label: "已中断", color: "volcano" };
    default:
      return { label: "未知", color: "default" };
  }
}

/**
 * 结果区提示文案：null = 正常渲染结果；非 null = 只渲染该提示（不伪造答案）。
 *
 * @param availability 服务端 result_availability 投影（ACL 裁剪后的口径）。
 * @param hasResult 服务端是否实际返回了正文（available 但空时给兜底文案）。
 */
export function availabilityNotice(
  availability: ResultAvailability,
  hasResult: boolean,
): string | null {
  switch (availability) {
    case "available":
      return hasResult ? null : "结果标记为可用，但服务端未返回内容（正文未保留或被策略裁剪）";
    case "pending":
      return "运行尚未结束，暂无结果";
    case "not_retained":
      return "结果正文未保留，仅可查看执行记录与安全摘要";
    case "expired":
      return "结果正文已过期（超出保留期），仅可查看执行记录";
    case "restricted":
      return "结果受限：当前身份不可查看正文";
  }
}

/** 图节点模板（id 与 agent/graph.py::GRAPH_NODE_IDS 同源）。 */
export interface GraphNodeSpec {
  id: string;
  label: string;
  position: { x: number; y: number };
}

export interface RunGraphSpec {
  nodes: GraphNodeSpec[];
  /** 模板边（取自 graph.py 装配拓扑的内部边；END 边不显示，终态由 run 状态表示）。 */
  templateEdges: Array<[string, string]>;
}

/** 主链一行 y=80（x 步进 220）：plan→retrieve→generate→validate→execute→explain。 */
export const RUN_GRAPH_SPEC: RunGraphSpec = {
  nodes: [
    { id: "plan", label: "规划", position: { x: 0, y: 80 } },
    { id: "retrieve", label: "检索", position: { x: 220, y: 80 } },
    { id: "generate", label: "生成", position: { x: 440, y: 80 } },
    { id: "validate", label: "校验", position: { x: 660, y: 80 } },
    { id: "execute", label: "执行", position: { x: 880, y: 80 } },
    { id: "explain", label: "解释", position: { x: 1100, y: 80 } },
    { id: "clarify", label: "澄清", position: { x: 440, y: 220 } },
    { id: "handoff", label: "交接", position: { x: 220, y: 220 } },
  ],
  templateEdges: [
    ["plan", "retrieve"],
    ["plan", "execute"],
    ["plan", "clarify"],
    ["retrieve", "generate"],
    ["retrieve", "handoff"],
    ["generate", "validate"],
    ["generate", "clarify"],
    ["validate", "execute"],
    ["validate", "clarify"],
    ["execute", "explain"],
  ],
};

/** 布局输出节点（React Flow 转换在 RunGraph 组件内做；此处只出纯数据）。 */
export interface GraphFlowNode {
  id: string;
  label: string;
  position: { x: number; y: number };
  status: RunNodeStatus;
}

export interface GraphFlowEdge {
  id: string;
  source: string;
  target: string;
  taken: boolean;
}

export interface RunGraphLayout {
  nodes: GraphFlowNode[];
  edges: GraphFlowEdge[];
}

function edgeKey(source: string, target: string): string {
  return `${source}->${target}`;
}

/** 模板节点/边与执行状态合并：模板恒在，状态与 taken 只来自事件归约。 */
export function layoutRunGraph(state: RunViewState): RunGraphLayout {
  const nodes: GraphFlowNode[] = RUN_GRAPH_SPEC.nodes.map((spec) => ({
    id: spec.id,
    label: spec.label,
    position: spec.position,
    status: state.nodes[spec.id]?.status ?? "not_reached",
  }));
  const taken = new Set(state.edges.map(([source, target]) => edgeKey(source, target)));
  const edges: GraphFlowEdge[] = RUN_GRAPH_SPEC.templateEdges.map(([source, target]) => ({
    id: edgeKey(source, target),
    source,
    target,
    taken: taken.has(edgeKey(source, target)),
  }));
  return { nodes, edges };
}

/** 时间线摘要上限（时间线列不膨胀；超长截断）。 */
const SUMMARY_LIMIT = 160;

function inlineValue(value: unknown): string {
  if (typeof value === "string") {
    return value;
  }
  if (value === null) {
    return "null";
  }
  if (typeof value === "object") {
    return JSON.stringify(value) ?? String(value);
  }
  return String(value);
}

/** payload 安全摘要（脱敏通道内容）：`k=v, k2=v2`；嵌套值 JSON 内联；超长截断。 */
export function payloadSummary(payload: Record<string, unknown>): string {
  const text = Object.entries(payload)
    .map(([key, value]) => `${key}=${inlineValue(value)}`)
    .join(", ");
  return text.length > SUMMARY_LIMIT ? `${text.slice(0, SUMMARY_LIMIT)}…` : text;
}

/**
 * 从事件流提取捕获引用（STATE_SNAPSHOT payload.artifacts；D07 脱敏引用通道）。
 *
 * 去重保序；非数组/非字符串项一律忽略（不猜、不补全）——引用只来自事件，
 * 不从 result 或本地推断（正文读取走显式钻取 + 服务端保留期判定）。
 */
export function collectArtifactIds(state: RunViewState): string[] {
  const seen = new Set<string>();
  const ids: string[] = [];
  for (const event of state.timeline) {
    if (event.event_type !== "STATE_SNAPSHOT") {
      continue;
    }
    const value = event.payload["artifacts"];
    if (!Array.isArray(value)) {
      continue;
    }
    for (const item of value) {
      if (typeof item === "string" && !seen.has(item)) {
        seen.add(item);
        ids.push(item);
      }
    }
  }
  return ids;
}
