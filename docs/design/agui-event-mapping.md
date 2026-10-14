# AG-UI 事件词表 ↔ LangGraph 映射登记

- 日期：2026-09-17
- 状态：**契约登记（借鉴，非兼容声明）**——仅登记事件模型对齐关系，零实现、零依赖
- 依据：ADR-0028 决策 ④（采纳 AG-UI 事件词表作为流式消费契约的**命名对齐**，不引入 SDK、不宣称兼容）
- 门禁：④a SSE 实现与 ④b SDK 引入均 **gated**（G-S4 未解除），本文档仅为词表登记

---

## 1. 边界声明（必读）

**本文档登记的是「事件模型借鉴关系」，不是「AG-UI 兼容声明」。**

- ✅ 正确措辞：「Atlas 事件模型**借鉴 AG-UI 词表**」「事件命名**对齐** AG-UI 规范」
- ❌ 禁止措辞：「Atlas **支持** AG-UI 协议」「Atlas **兼容** AG-UI」「Atlas 是 AG-UI server」

**原因**：
- 当前 Atlas 无 AG-UI 客户端能直连的 server 实现（无 `/analyze/stream` SSE 端点）
- 对外宣称「支持/兼容」= 未实现写为已实现 = 违反 AGENTS.md N2（诚实红线）
- 本文档仅登记「若将来实现流式端点，事件名应对齐哪些 AG-UI 词汇」

---

## 2. 映射总表

> 来源：ADR-0028 决策 ④ 表格；Atlas 落点为 LangGraph 八节点 + ADR-0026 AnalysisPlan + agent/tools/* + ExplanationBlock

| AG-UI 事件 | Atlas 落点（映射，非照搬） | 说明 |
|---|---|---|
| `RunStarted` | 一次 `/ask` 或 `/analyze` 请求开始 | LangGraph 状态机进入 `clarify` 节点前 |
| `RunFinished` | 一次 `/ask` 或 `/analyze` 请求正常结束 | LangGraph 状态机完成 `explain` 节点后 |
| `RunError` | 一次 `/ask` 或 `/analyze` 请求异常终止 | LangGraph 状态机任意节点抛异常 |
| `StepStarted` | LangGraph 节点进入 | 八节点之一：`clarify` / `retrieve` / `plan` / `generate` / `validate` / `execute` / `explain` |
| `StepFinished` | LangGraph 节点退出 | 同上，节点正常完成 |
| `ActivitySnapshot` / `ActivityDelta`（`activityType:"PLAN"`） | ADR-0026 `AnalysisPlan` 的子步骤与 attribution 增量 | 多步分析时，每个子步骤的进度与归因信号 |
| `ToolCallStart` / `ToolCallArgs` / `ToolCallEnd` / `ToolCallResult` | `agent/tools/*` 调用 | 只读 SQL 执行（`execute_sql`）、`render_chart`、`schema_link` 的透明化展示 |
| `StateSnapshot` / `StateDelta`（RFC6902 JSON Patch） | `Plan` / LangGraph state 的「边生成边看」 | 前端实时展示状态变化（如 plan 字段逐步填充） |
| `ReasoningStart` / `ReasoningMessage*` / `ReasoningEnd` | `ExplanationBlock` 的升级 | 展示推理信号（如「为何选择此指标」「为何过滤此维度」）但**不暴露原始 CoT**、不造假（N1） |
| `Custom(name, value)` | Atlas 专属字段 | 承载 AG-UI 标准事件无法覆盖的 Atlas 领域信息（见 §3） |

---

## 3. Custom 字段登记

> AG-UI `Custom(name, value)` 事件用于承载 Atlas 专属字段，不污染标准事件语义

| Custom name | value 类型 | 说明 | 来源 |
|---|---|---|---|
| `snapshot_sha` | `string` | 当前查询绑定的数据快照 sha（固定数据快照，绑定 git sha） | ADR-0006、AGENTS.md §3「Snapshot」 |
| `governance_verdict` | `string` | 治理裁决结果（如「指标已注册」「维度值域已校验」「行级策略已下推」） | ADR-0002、semantic/governance/ |
| `role` | `string \| null` | 当前激活角色（如 `branch_manager` / `hq_admin` / `null`） | ADR-0018 §3.5 约束 1（切角色即换会话） |
| `fibo_alignment` | `string` | 语义对象 → FIBO 概念 IRI 的映射（L2 概念对齐层） | ADR-0007、AGENTS.md §3「fibo_alignment」 |

---

## 4. LangGraph 八节点 ↔ StepStarted/StepFinished 映射

> Atlas Agent 状态机（`agent/graph.py`）的八节点与 AG-UI `Step*` 事件的对应关系

| LangGraph 节点 | 职责 | StepStarted 触发时机 | StepFinished 触发时机 |
|---|---|---|---|
| `clarify` | 问句澄清（消歧、补全） | 进入 clarify 节点 | clarify 节点正常退出 |
| `retrieve` | 语义检索（FIBO 概念对齐、指标/维度召回） | 进入 retrieve 节点 | retrieve 节点正常退出 |
| `plan` | 生成 AnalysisPlan（ADR-0026） | 进入 plan 节点 | plan 节点正常退出 |
| `generate` | 候选 SQL 生成（LLM/LoRA） | 进入 generate 节点 | generate 节点正常退出 |
| `validate` | SQL 校验（语法、语义、权限） | 进入 validate 节点 | validate 节点正常退出 |
| `execute` | 只读 SQL 执行（经 Guard） | 进入 execute 节点 | execute 节点正常退出 |
| `explain` | 生成 ExplanationBlock（可解释性） | 进入 explain 节点 | explain 节点正常退出 |

**注意**：`clarify` 与 `retrieve` 是 Atlas 特有节点（AG-UI 标准示例中未列出），映射时保留 Atlas 语义。

---

## 5. 安全边界（N3 纵深防御）

> 事件**只承载展示**，不得成为新的执行通道

- ✅ 事件可携带：状态变化、进度信号、工具调用参数、推理信号、Atlas 专属字段
- ❌ 事件**不得**携带：SQL 执行语句、INSERT/UPDATE/DELETE/DDL、绕过 Guard 的任何操作
- ✅ SQL 仍走只读 Guard（`agent/security/sql_guard.py`），流式端点**不得**成为新的执行通道
- ❌ 流式路径若出现绕过 Guard 直接执行 SQL → 立即否决（N3，最高优先）

---

## 6. 实现状态与门禁

| 项 | 状态 | 门禁 | 说明 |
|---|---|---|---|
| 词表登记（本文档） | ✅ 已完成 | 无 | 零实现、零依赖 |
| ④a SSE 端点 `/analyze/stream` | ❌ 未实现 | G-S4（GATE-④） | ADR-0026 编排器需先有稳定的分步产物可流 |
| ④b 引入 AG-UI/CopilotKit SDK | ❌ 未实现 | 触发条件 (b) | ≥2 个前端消费者需同一事件流 |

**触发条件**（见 ADR-0028「什么情况下应该推翻」④）：
- (a) ADR-0026 编排器稳定分步 ≥1 个评测周期，流式有真实消费价值 → 启动 ④a 实现
- (b) 出现 ≥2 个前端消费者需同一事件流（CLI 之外），自绘 SSE 维护成本持续高于接 SDK → 启动 ④b
- (c) AG-UI 规范发生 Atlas 无法承受的破坏性变更 → 退回纯自研事件名

---

## 7. 验证方式

> 收口判据取自 dev-plan-0028 §5

- ✅ 映射文档存在（本文档）
- ✅ `grep -rn "支持 AG-UI 协议\|AG-UI 兼容" docs frontend` → 0 命中（诚实措辞守线）
- ⏳ ④a 实现后：契约测试断言 SSE 事件名与本文表格一一对应
- ⏳ ④a 实现后：`grep -nE 'execute\|INSERT\|UPDATE\|DROP' serving/api.py` 确认流式路径不新增执行语句
- ⏳ ④b 实现后：`make license-check` 绿 + 逐包回填 `node_modules/*/LICENSE`

---

## 8. 参考

- ADR-0028：前端演进——借鉴 open-webui 信息架构与 AG-UI 事件契约
- ADR-0026：多步任务规划（AnalysisPlan）
- ADR-0007：FIBO 本体作为语义锚点
- AG-UI 规范：<https://docs.ag-ui.com/concepts/events>
- LangGraph 文档：<https://langchain-ai.github.io/langgraph/>
