# ADR-0028：前端演进——借鉴 open-webui 信息架构与 AG-UI 事件契约

- 日期：2026-09-17
- 状态：**accepted**（决策经用户逐条确认，2026-09-17；含裁定 A/B/C，见「决策」各条标注）
- 相关：
  - ADR-0018（前端控制台，本 ADR 是其增量演进，不改其技术栈锁定）、
    ADR-0011/0012/0022（服务面与安全分层——前端**不新增**任何执行面，N3）、
    ADR-0020（会话持久化 checkpointer，② 的服务端前提）、
    ADR-0025（图表 spec 接线）、ADR-0026（多步任务规划，④ 的消费对象）、
    ADR-0027（数据飞轮：指标只产 proposal，③ 拒绝写编辑器的依据）
  - 参考项目（外部，仅借鉴设计，零引入代码）：
    - open-webui <https://github.com/open-webui/open-webui>（SvelteKit SPA）
    - AG-UI <https://github.com/ag-ui-protocol/ag-ui>（Agent↔UI 事件协议，LangGraph 一等集成）
  - 被改动的既有文件：`frontend/src/App.tsx`（顶栏 `NAV_ITEMS:38-42`、路由 `119-167`）、
    `frontend/src/panels/governance/GovernanceLayout.tsx`、
    `frontend/src/panels/sessions/SessionPanel.tsx`、`frontend/src/state/session.ts`

---

## 背景

Atlas 控制台（ADR-0018）已落 P0b~P2：`App.tsx` 有顶栏三区导航（工作台/会话/治理）、
`/governance/:section` 8 只读子页 + 抽屉、会话日志（本标签页内存态）、角色切换、
快照徽标。产品愿景（`docs/text2insight.md`）要求前端随阶段二（多步分析，ADR-0026）
继续演进。用户要求：从两个成熟开源项目中提取**值得落地**的借鉴点，落成可执行决策。

两个参考项目**不在同一层**，借鉴方式根本不同，必须先立边界：

| 项目 | 本质 | 栈 | 许可证（实测 2026-09-17） | 借鉴边界 |
|---|---|---|---|---|
| open-webui | 成品 AI 聊天平台（SvelteKit SPA + Py 后端） | Svelte 5，**与 Atlas（React+AntD）不同栈** | **BSD-3 + 品牌保护条款**（读 `LICENSE` 第 4 条：>50 用户/30 天不得改 branding，除非企业授权；≤v0.6.5 为纯 BSD-3） | 只读**信息架构/页面组织**，**禁止 vendor 代码**（栈不符 + 品牌条款 + N7） |
| AG-UI | Agent↔UI **事件协议** + 多语言 SDK | 协议无关，前端可在 React 消费 | **MIT**（实测 `LICENSE` 首行 + GitHub API `spdx_id:MIT`）——permissive，兼容 Apache-2.0 | 采纳其**事件词表/状态模型**；是否引入 SDK 是更进一步的 gated 步骤 |

约束（引 AGENTS.md，不重述）：
- **N3 / ADR-0011**：前端不得新增执行面，一切数据经既有 `/api/v1`，SQL 仍过只读 Guard。
- **N7**：不引入外部/雇主真实代码。参考=读设计，不搬实现。
- **§5**：引入新依赖须 ①理由 ②ADR（本文）③许可证评估（已做）④预算可承受。
- **`no-persist.test.ts`（0018 §3.5 约束 3）**：运行时代码禁 localStorage/sessionStorage/
  cookie/IndexedDB，token 只在内存。这直接约束 ②「会话列表」不得走客户端缓存。
- **N1/N2**：文档与界面不得出现未经脚本产出的数字；gated 项不得写成"已实现"。
- **§7.3/§4**：`semantic/` 不新增权威目录；写操作不得破坏「Git 唯一事实源」。

实测起点（本 ADR 立论依据，均来自本次抓取/读码）：
- open-webui 路由（`src/routes` 递归列举）：`c/[id]`（会话）、`folders/`（分组）、
  `workspace/{knowledge,models,prompts,tools,skills,functions}`（构建区，含 `*/edit`）、
  `admin/{users,settings,analytics,evaluations,functions}`（管控区，与 workspace 分离）、
  `automations`、`playground/{completions,images}`、`calendar/notes/channels/watch`。
- AG-UI 事件（`docs.ag-ui.com/concepts/events`）：Lifecycle `RunStarted/Finished/Error`、
  `StepStarted/StepFinished`；TextMessage `Start/Content/End/Chunk`；
  ToolCall `Start/Args/End/Result/Chunk`；State `StateSnapshot/StateDelta`(RFC6902
  JSON Patch)`/MessagesSnapshot`；Activity `ActivitySnapshot/ActivityDelta`
  （`activityType` 如 `PLAN`/`SEARCH`）；Reasoning `Start/Message*/End`（不暴露原始 CoT）；
  Special `Raw/Custom`。
- Atlas 现状：`App.tsx` 顶栏 `NAV_ITEMS` 已是 工作台/会话/治理 三项；会话 `log` 仅内存；
  治理为**扁平 8 子页**（未分「构建/管控」）；`/analyze`（ADR-0026）当前非流式。

---

## 备选方案

| 方案 | 优势 | 劣势 |
|---|---|---|
| **精选借鉴：open-webui 借 IA（无依赖）+ AG-UI 借事件契约（依赖 gated）（选定）** | 零 vendor 风险；把「可立即做的外壳整理」与「需新依赖/端点的流式改造」分级，各自可独立推翻；AG-UI 与 Atlas 的 LangGraph 天然映射 | 需要维护「采纳了哪些外部设计」的可追溯清单，避免日后误记为自研；④ 引入协议面复杂度 |
| fork open-webui / 搬其组件 | 现成精美 UI | **否决**：栈不符（Svelte→React 重写代价巨大）+ 品牌条款 + N7；且其语义锚点界面（Plan/Guard/snapshot/FIBO）根本不存在，省不了核心工作 |
| 全面接入 AG-UI + CopilotKit SDK | 一步到位拿到流式 agent UI | **否决（至少现在）**：为尚未成型的 0026 消费层引入一整个前端框架依赖，违反「确定性优先/不过度设计」（§10）；先采纳**事件词表**，SDK 留作触发后再议 |
| 完全不参考、闭门自绘 | 无许可证/契约牵连 | 重复造 open-webui 已趟过的 IA 轮子；agent 流式交互重新发明一套事件名，放弃与 LangGraph 生态对齐的免费一致性 |

---

## 决策

> 每项自成可独立推翻的粒度（沿 ADR-0016 惯例）；标注「采纳/前置」。

### ① 治理区二级化：拆分「语义构建」与「管控治理」（**采纳·立即落地·无新依赖**）

参考 open-webui 把 `workspace/*`（构建）与 `admin/*`（管控）分离的信息架构。Atlas 现状
是扁平 8 子页（models/metrics/dimensions/policies/values/synonyms/reports/snapshots）。
决策：在 `GovernanceLayout.tsx` 内把这 8 子页按职责分入两个二级组，**仅改 Tabs 视觉分组、路由零改动**，不改各 Section 的数据与只读性质：

- **语义构建（build）**：`models` / `metrics` / `dimensions` / `synonyms` / `values`
  - **裁定 A（2026-09-17）**：`values` 归 build——值域属语义定义侧，非权限/评测类管控。
- **管控治理（control）**：`policies`（行级权限）/ `reports`（评测）/ `snapshots`（快照绑定）

顶栏 `NAV_ITEMS` 的「治理」项不变；**因路由零改动**，`/governance/:section` 旧链接天然不 404。
此项**不含任何写操作**（写见 ③）。

### ② 会话列表与分组（**采纳设计·落地 gated：前置服务端只读端点**）

参考 open-webui `c/[id]` + `folders/` 的「左栏会话流 + 分组」。Atlas 的 `sessionState.log`
只在**当前标签页内存**（`App.tsx:58-61`），刷新即失、跨标签不可见——这与 open-webui 的
持久会话列表差距最大。决策：

- 界面采纳：`SessionPanel` 演进为「会话列表（可按域/身份分组）+ 选中查看回合」的**布局范式**（React 自绘，非搬码）。
- **数据只从服务端读**：列表来源是 ADR-0020 checkpointer 的 thread 集合，经一个**新增只读端点**
  `GET /api/v1/sessions`（属 ADR-0022 治理只读面家族）返回，绑定当前身份（RLS 下推）。
- **硬约束**：**禁止**用 localStorage/sessionStorage 缓存会话列表（`no-persist.test.ts` 会红）。
- **前置门（GATE-②）**：`/api/v1/sessions` 端点未落地前，②只做纯前端分组展示（仍只喂内存 log），**不谎称持久化**（N2）。
- **裁定 B（2026-09-17 用户确认）**：GATE-② 裁为 **MVP 外 / 暂缓**——单标签页内存会话已够用，跨端持久列表属增量，不为它现在扩 `serving` 只读契约面（新端点须同步「17 条双向相等」断言）。故 ② 实际交付面收窄为「`SessionPanel` 纯前端分组展示（喂内存 log）」，服务端列表端点在真实需求出现前不排期（触发见「什么情况下应该推翻」②）。

### ③ 拒绝「在线写编辑器」，只保留「列表→详情抽屉」浏览范式（**采纳否定决策·立即生效**）

open-webui `workspace/*/edit` 提供在线编辑并回写。Atlas **明确不采纳**该写路径：
指标/同义词/值域的权威源是 Git（ADR-0002/0015/0016、N8），任何前端直写都会造成
第二事实源。落地：

- 保留并统一 `ReportDrawer` / `ValuesDrawer` 的「列表→只读详情抽屉」范式（已存在，本 ADR 确认其为标准）。
- 若将来需要编辑 UI，它**只服务 ADR-0027 的 proposal 生成**（提交候选，人工 + CI 落 Git），
  且必须走 0027 的 `_*` 候选区与 lint 断言，**不得**直接 PUT 语义定义。
- 本决策同时约束 ①：二级化不得顺手加「编辑」按钮。

### ④ 采纳 AG-UI 事件词表作为流式消费契约（**采纳契约·实现 gated：新依赖或 SSE 端点**）

面向 ADR-0026 多步分析与回合渲染，采纳 AG-UI 的**事件类型词表**作为前端消费层契约
（不是现在引入 SDK，而是先让 Atlas 的流式产物**对齐这套命名与语义**，与 LangGraph 生态保持一致）：

| AG-UI 事件 | Atlas 落点（映射，非照搬） |
|---|---|
| `RunStarted/Finished/Error` | 一次 `/ask` 或 `/analyze` 生命周期 |
| `StepStarted/StepFinished` | LangGraph 节点 `clarify→retrieve→plan→generate→validate→execute→explain` 的进度可视化 |
| `ActivitySnapshot/ActivityDelta`（`activityType:"PLAN"`） | ADR-0026 `AnalysisPlan` 的子步骤与 attribution 增量 |
| `ToolCallStart/Args/End/Result` | `agent/tools/*`（只读 SQL 执行、`render_chart`、schema_link）的透明化展示 |
| `StateSnapshot/StateDelta`(RFC6902) | `Plan`/LangGraph state 的「边生成边看」 |
| `Reasoning*` | `ExplanationBlock` 的升级：**展示推理信号但不暴露原始 CoT**、不造假（N1） |
| `Custom(name,value)` | 承载 Atlas 专属字段：`snapshot_sha`、governance verdict、role |

**边界与分级（关键，守 N3）**：
- 事件**只承载展示**；SQL 仍走只读 Guard，流式端点**不得**成为新的执行通道。
- 分两步：**④a（近）** 定义 SSE 端点 `/analyze/stream`，事件名对齐上表（不引 SDK，零前端新依赖）；
  **④b（远·触发后）** 若确需 CopilotKit/AG-UI SDK 客户端，方按 §5 逐包读
  `node_modules/*/LICENSE` 回填并登记。MIT 已核实，兼容性无碍。
- **前置门（GATE-④）**：ADR-0026 编排器需先有稳定的分步产物可流；未就绪前 ④ 仅为契约登记，不实现。
- **裁定 C（2026-09-17 用户确认）**：当前仅接受「事件词表登记」（映射文档，零前端依赖）；④a SSE 实现与 ④b SDK **均维持 gated**，未触发前不编码、对外不宣称 AG-UI 兼容（N2）。

---

## 理由

- **为什么精选而非全采纳**：两个项目一个「不同栈的成品」、一个「协议」，全量接入都付出不成比例的复杂度或许可证/契约风险；只取「外壳整理」与「事件命名对齐」两处确定性收益。
- **①**：治理扁平 8 页在认知上把「定义语义」和「管控权限/评测」混为一谈；open-webui 的 build/control 二分是被验证过的心智，成本仅是导航分组，零数据改动。
- **②**：会话持久化是 Atlas 相对 open-webui 的真实缺口，但补它必须先补**只读端点**（服务面变更），不能靠客户端存储绕过 `no-persist` 契约——所以设计采纳、落地排队。
- **③（否定决策的价值）**：开源项目最容易被"顺手抄个编辑页"带偏成第二事实源，破 ADR-0002 根基。显式写「不采纳写编辑器」比沉默更能防止未来漂移。
- **④**：Atlas 用 LangGraph，AG-UI 与 LangGraph 是一等集成关系；对齐其事件词表几乎零摩擦地换来「多步 + 归因 + 工具执行」的实时透明呈现，正是阶段二前端最缺的能力。先对词表（无依赖）再谈 SDK（有依赖）是分险。

---

## 代价与限制

1. **open-webui 不可搬码**：品牌条款 + 栈不符 + N7；① 借的只是「分组思想」，实现全部 React 自绘，无法复用其任何 `.svelte`。
2. **② 引入一次服务面扩张**：`GET /api/v1/sessions` 是新只读端点，须并入 ADR-0022 的鉴权/限流/RLS 面；在其落地前 ② 只能做内存态分组展示，**能力受限于 GATE-②**。
3. **④ 使 `/ask` 语义双轨**：既有 request/response（`turn_from_state`），若加 SSE 流式则同一回合两种消费形态并存，契约漂移风险上升——需契约测试同时锁两态（见验证方式）。
4. **④a 事件命名是"对齐"非"实现"**：Atlas 不因此成为 AG-UI 兼容 server（无 AG-UI 客户端能直接连）；对外**不得**宣称"支持 AG-UI"，只能说"事件模型借鉴 AG-UI 词表"（N2）。
5. **③ 不解决编辑诉求**：治理面板保持只读，日常改指标仍走 Git + PR；若产品强要"界面改指标"，本决策需被推翻并回到 ADR-0027 proposal 链路重新论证。
6. 本 ADR 落地后，若引入 AG-UI SDK，将首次把**运行时 JS 依赖**从「直接依赖仅 5 个」（0018 P1 注记）扩张，代码审阅面（遥测/存储）需重跑。

---

## 什么情况下应该推翻

- **①**：若治理子页 ≤5 或二级分组使可达深度 >2 步反增摩擦，则回到扁平结构。
- **②**：若产品确认「会话只需当前标签内有效、无需跨端持久列表」，则撤销 GATE-②，仅保留现有内存 `SessionPanel`。
- **③**：仅当出现**不破坏 Git 唯一事实源**的编辑回写设计（如前端仅产 Git PR/patch，绝不直写运行态），才允许引入编辑 UI；否则永久拒绝。
- **④**：满足任一即重估——
  (a) ADR-0026 编排器稳定分步 ≥1 个评测周期，流式有真实消费价值 → 启动 ④a 实现；
  (b) 出现 ≥2 个前端消费者需同一事件流（CLI 之外），自绘 SSE 维护成本持续高于接 SDK → 启动 ④b；
  (c) AG-UI 规范发生 Atlas 无法承受的破坏性变更 → 退回纯自研事件名。
- **通用**：若安全评审认定任何流式端点构成绕过 Guard 的执行面（N3），④ 一律否决。

---

## 验证方式

- **不破坏既有契约（每次改动必跑）**：
  `make ui-check` → `frontend/src/__tests__/` 全绿，特别是：
  - `no-persist.test.ts` 保持绿（② 不得引入客户端存储）；
  - `honesty.test.ts` 保持绿（①③ 界面不得出现无脚本来源的数字，N1）；
  - `app.test.tsx` 冒烟保持绿（① 改导航后 `renderToString(<App/>)` 仍出「Atlas 控制台」）。
- **① 结构断言**：新增/调整 `governance` 导航测试，断言 8 子页被划入 build/control 两组且旧 `/governance/:section` 路由仍可解析（不 404）。
- **② 端到端（GATE-② 后）**：契约测试覆盖 `GET /api/v1/sessions` 返回按身份过滤（RLS 下推，复用 `serving/rls_verify.py` 思路）；前端测试断言列表数据源为端点响应而非任何 `*Storage`。
- **③ 静态防漂移**：**以 B3.1 回归测试为准**（断言治理面板运行时无任何写 governance 端点的方法调用），`grep -nE '(PUT|POST).*semantic|editable' frontend/src/panels/governance/` 仅作辅助（注：前端写路径实为 `/api/v1/governance/*`、不含 `semantic` 字样，且 `editable` 可能误伤 AntD 只读配置，故 grep 非权威）；抽屉保持只读断言。
- **④a 契约测试**：`/analyze/stream` SSE 事件名与本文表格一一对应的枚举断言；并 `grep -nE 'execute|INSERT|UPDATE|DROP' serving/api.py` 确认流式路径**不新增执行语句**、SQL 仍经 `sql_guard`（N3）。
- **④b 许可证回填**：若引入 SDK，按 §5 逐包读 `frontend/node_modules/<pkg>/LICENSE*` 并回填本 ADR 决策 ①表（沿 0018 P0b 口径），`make license-check` 绿。
- **诚实登记**：②④ 未实现前，README/Known Limitations 标注「设计已定，实现 gated」，不得写「已上线」（N2）。

---

## 落地注记（追加，不改裁定 C / 不翻 GATE-④）

### GATE-④ 技术前置就位（2026-09-17）

> 依用户决策「先满足 GATE-④ 触发条件的技术前置」。本注记**只登记技术前置**，
> **不**裁定 GATE-④ 解除、**不**实现任何 SSE/端点、**不**改契约 17 条、**不**对外
> 宣称 AG-UI 兼容（严守裁定 C 与 N2）。

- **登记事实**：`DataAgent.analyze()` 内部四步循环已抽出为私有生成器
  `_iter_analysis_step_events`（`agent/graph.py`），每执行一步产出不可变
  `AnalysisStepEvent`（`agent/analysis.py`）——满足本 ADR 第 131 行 GATE-④ 前置门
  「编排器需先有稳定分步产物可流」的**技术面**：分步产物现在是可独立契约测试
  锁定的显式对象。
- **急切驱动约束**：生成器只在 `analyze()` 同一把锁内被 `list(...)` 驱动到耗尽，
  锁生命周期不外泄给消费者；公开签名、`/analyze` request/response 形态、契约计数
  逐字不变。接缝**不是**公开流，无任何外部慢读路径。
- **契约测试**：`tests/test_analysis_stream_seam.py`（5 例 + 4 子测）锁定 yield
  顺序=角色序、blocked/error 裁剪且被拒 SQL 不入事件（N3）、失败后零调用、逐步
  落账副作用；`test_analysis_agent.py`/`test_analysis_execution.py`（379 例）原样全绿
  证明重构行为等价。
- **仍未满足**：第 163 行触发条件 (a) 的**时间前置**「稳定分步 ≥1 个评测周期」需
  后续真实 `make analysis-eval`/`make eval` 运行积累证据后由用户裁定，非本次可自证。

### GATE-④ 解锁·启动 ④a（2026-09-17，追加；supersede 上一节「仍未满足」）

> 上一节末行「时间前置仍未满足」反映的是技术前置登记当时（当日更早时点）的状态；
> 本注记在其后追加，**据用户裁定解除 GATE-④ 对 ④a 的 gate**（触发条件 (a) 满足）。

- **裁定（用户确认 2026-09-17）**：④a（SSE 端点 `/analyze/stream`）**解除 gate、授权实现**；
  **④b（AG-UI/CopilotKit SDK）维持 gated**（触发条件 (b)「≥2 前端消费者需同一事件流」未达）。
- **解除依据·3 个干净评测周期**（均在含分步接缝重构的 HEAD 上，报告已入库）：
  1. `eval/reports/analysis-e7909f2.json`——真链 `analysis-eval`（绑定快照 `7c966e9`、
     前后 `snapshot_verified` 双 True、2/2 样本、42 检查项 0 失败、独立重算核对）；
  2. `--dry` 全量 `eval.runner` @ `e0e0422`——纯解析 `plan_acc 97/97`、0 执行错误；
  3. `eval/reports/1e2e557.json`——真链全量 `make eval`：`EX 97/97`、`plan_acc 97/97`、
     `exec_errors 0`、`guard_blocked 0`、`ex_anchored 0`（gold 未回填·纯比对通过）。
- **④a 执行模型（用户选定 A：compute-then-stream）**：复用 `/analyze` **同一把锁、同一
  执行路径**跑完 `analyze()`（SQL 已全部经 Guard）→ **释放锁** → 把已算好的分步产物按
  `docs/design/agui-event-mapping.md` §2/§4 词表**回放**为 SSE。事件**只承载展示**、
  不携带任何执行语句、不新增执行通道（N3）；不宣称「SQL 边执行边流」（诚实）。
- **仍守红线**：对外**不得**宣称「AG-UI 兼容」（仅「事件模型借鉴 AG-UI 词表」，N2）；
  契约 17→18 需四处同步（`serving/api.py`、`test_api_contract_v2.py`、`endpoints.ts`、
  OpenAPI 双向相等）；README/Known Limitations 在 ④a 未落地前仍标「实现 gated」（N2）。
- **配套开发计划**：`docs/design/dev-plan-0028-analyze-stream-sse.md`（B0~B3，测试先行）。

### ④a 落地回执（2026-09-17，追加）

> 上方「README/KL 在 ④a 未落地前仍标 gated」的前置条件已达成——**④a 已实现**，
> README/KL 已据实从「gated」更新为「已落地·借鉴词表非协议兼容」；**④b 仍 gated 不变**。

- **交付 commit**：后端 B1 `72dfec9` + B1.1 `5f8f019`（STATE_SNAPSHOT 发完整 TurnPayload·
  单一事实源）、契约测试 B0 `80dba40`、快照台账 `6ab38f6`、前端消费 B2 `ca4424c`
  （reducer + reader + 渐进渲染 + 失败回落 request/response）。
- **收口判据 1~7 全绿**（实测输出见 dev-plan §6）：契约三处 @18、事件名枚举断言、
  N3 零执行、N1 零重算、N9 无 EventSource/token 只在头、N2 README 无该字样、
  全量 pytest 1219 passed + `make ui-check` 全绿。
- **诚实边界不变**：compute-then-stream ≠ SQL 边执行边流；事件词表为借鉴、非对外
  协议兼容声明（无 AG-UI 客户端可直连）；④b（SDK 接入）仍 gated。
