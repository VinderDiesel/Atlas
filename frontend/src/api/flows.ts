/**
 * T09c: 流程定义 API 类型与校验函数。
 *
 * 后端 `/api/v1/flows/validate` 的前端消费；当前为客户端校验（与 Python
 * validate_flow 同口径的子集），后端校验留待 M2 接入。
 */

export interface FlowNode {
  node_id: string;
  node_type: string;
  type_version: number;
  implementation_id: string;
  config_ref: string | null;
  input_contract: string;
  output_contract: string;
}

export interface FlowEdge {
  from_node: string;
  to_node: string;
  condition: {
    op: "equals" | "in";
    values: string[];
  };
}

export interface FlowBudget {
  model_calls?: number;
  node_attempts?: number;
  tool_calls?: number;
  sql_calls?: number;
  run_seconds?: number;
  model_input_tokens?: number;
  model_output_tokens?: number;
  model_total_tokens?: number;
}

export interface FlowDefinition {
  schema_version: 1;
  flow_id: string;
  revision: number;
  entry: string;
  budget: FlowBudget;
  nodes: FlowNode[];
  edges: FlowEdge[];
}

export interface FlowIssue {
  code: string;
  node_id?: string;
  edge_id?: string;
  message: string;
}

// 已注册节点类型（与 agent/flows/contracts.py REGISTERED_NODE_TYPES 同步）
const REGISTERED_TYPES = new Set([
  "rule_plan",
  "understand",
  "retrieve",
  "bind_plan",
  "execute_plan",
  "analysis",
  "explain",
  "chart",
  "clarify",
  "handoff",
  "switch",
  "subflow",
]);

const NODE_STATUSES = ["ok", "clarify", "unmatched", "blocked", "error"] as const;
const TERMINAL_TYPES = new Set(["clarify", "handoff", "explain", "chart"]);

/**
 * 客户端流程校验（与后端 validate_flow 同口径子集）。
 * 后端校验留待 M2 API 接入。
 */
export async function validateFlowDefinition(flow: FlowDefinition): Promise<FlowIssue[]> {
  const issues: FlowIssue[] = [];

  // 节点类型检查
  for (const node of flow.nodes) {
    if (!REGISTERED_TYPES.has(node.node_type)) {
      issues.push({
        code: "unknown_node_type",
        node_id: node.node_id,
        message: `未注册的节点类型: ${node.node_type}`,
      });
    }
  }

  // entry 检查
  const nodeIds = new Set(flow.nodes.map((n) => n.node_id));
  if (!nodeIds.has(flow.entry)) {
    issues.push({ code: "entry_not_found", message: `entry 指向不存在的节点: ${flow.entry}` });
  }

  // 边引用检查
  for (const edge of flow.edges) {
    if (!nodeIds.has(edge.from_node) || !nodeIds.has(edge.to_node)) {
      issues.push({
        code: "edge_unknown_node",
        edge_id: `${edge.from_node}->${edge.to_node}`,
        message: "边引用未声明的节点",
      });
    }
  }

  // 分支穷尽检查
  for (const node of flow.nodes) {
    if (TERMINAL_TYPES.has(node.node_type)) continue;
    const nodeEdges = flow.edges.filter((e) => e.from_node === node.node_id);
    if (nodeEdges.length === 0) continue;
    const covered = new Set(nodeEdges.flatMap((e) => e.condition.values));
    const missing = NODE_STATUSES.filter((s) => !covered.has(s));
    if (missing.length > 0) {
      const hasFailureExit = covered.has("blocked") || covered.has("error");
      if (!hasFailureExit) {
        issues.push({ code: "no_failure_exit", node_id: node.node_id, message: "blocked/error 无出口" });
      } else {
        issues.push({
          code: "branch_not_exhaustive",
          node_id: node.node_id,
          message: `分支未穷尽，缺少: ${missing.join(", ")}`,
        });
      }
    }
  }

  return issues;
}
