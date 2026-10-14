# 开发计划：阶段二归因分析的问数工作台前端消费

- 日期：2026-09-17
- 状态：**已实现（B0~B3 于 2026-09-17 落地，`make ui-check` 全绿：13 文件 99 测试含 `no-persist`/`honesty`/`app` 回归不破）**——
  入口形态取「查询/分析」模式切换，贡献可视化取 **C：前端 CSS 比例条**（零图表、零 `chart.py` 改动）。
  起点实测见 §0，交付事实见 §6。
- 范围：把 **ADR-0026 已落地的 `/api/v1/analyze` 多步归因**接进 `AskWorkbench`（纯前端消费）。
  流式（0028 ④a）、贡献图表（0025 图表管道）、任何后端/语义改动——见 §3 排除清单。
- **本页不对应新 ADR**：C 决定移除图表后，本阶段不含任何新增架构裁定，故
  **不立 ADR-0029、不占用编号**；实现细节以**编号引用** 既有 ADR：
  产物契约 = ADR-0026（决策⑥ 17 键投影）、控制台框架 = ADR-0018、前端演进 = ADR-0028。
- 发现契约缺口只登记为旁附注（§1.3），裁定须回对应 ADR，不在本页拍板。

## 本页纪律（与 `dev-plan-0027/0028` 同源）

1. **不转述**：判据一律以编号引用（如「0026 决策⑥」「0018 判据 9」），不复制 ADR 正文；
2. **计数三要素**：跨批引用计数写「公式 + 撰写时实测值 + 重测时点」（本页涉 HTTP 契约端点 17，
   逐条验证归属——见 §0，本阶段**不动**该计数）；
3. **不含工期估算**：AGENTS.md N1 禁止未实测数字，工期无测量口径 → 只给**工作项数**与相对规模
   （S/M/L，**明示主观量级、不得当承诺**）；
4. **不拍板未裁定项**：§1.3 旁附注在对应 ADR 处置前不得据此扩面；
5. **停批条件优先于进度**：命中即停该批回改设计，不得"先开工后补"。

---

## 0. 起点实测（2026-09-17，本页撰写时逐条复测）

阶段二后端不是白地——0026 已落地 `/analyze` 与 17 键投影，前端也已镜像类型；缺的只是"接进界面"：

| 事实 | 实测证据 | 复测命令 |
|---|---|---|
| **`/analyze` 存在，请求体逐字同 `/ask`，返回 `TurnPayload`(+`analysis`)** | `serving/api.py:834` `def analyze(body: AskBody, ...) -> dict`；summary 注明「AskBody/Bearer/模型选择/限流与 /ask 同源」 | `grep -nE "def analyze\(|body: AskBody" serving/api.py` |
| **无意图/无法解析 → `analysis` 回落为 null** | `api.py:843-845`：无分析意图按 0026 决策③回落普通 `ask`（kind=answer 且携带单 SQL 结果）；无法解析 kind=clarify——两种回落 `analysis` 均为 null | `sed -n '840,846p' serving/api.py` |
| **`analysis` 17 键前端已镜像，`TurnPayload.analysis` 恒存** | `types.ts:79-112`（`AnalysisPayload` + `AnalysisPeriod/Item/Step`），注释「非分析请求值为 null 而非缺失」 | `grep -n "analysis: AnalysisPayload" frontend/src/api/types.ts` |
| **⚠ 前端零组件消费 `/analyze`（本阶段缺口本体）** | `API.analyze` 仅 `endpoints.ts:25` 声明；`frontend/src` 除类型外无组件 `postJson(API.analyze …)`、无 `AnalysisBlock` | `grep -rn "API.analyze" frontend/src --include=*.tsx` → 0 命中 |
| **`AskWorkbench` 是 3 动作 + `run(Busy)` 状态机 + 表驱动渲染** | `AskWorkbench.tsx:46` `Busy="ask"\|"plan"\|"compile"\|"execute"`；`:74` `run()`；`:139-172` `renderAnswer` 由 `ANSWER_SECTIONS` 驱动；`:174-259` `renderTurn` 按 `branchOf(kind)` 五态分支 | `grep -nE "type Busy\|ANSWER_SECTIONS\|branchOf" frontend/src/panels/workbench/AskWorkbench.tsx` |
| **可复用渲染件齐全（无需新造）** | `panels/workbench/`：`SqlPreview`/`DataTable`/`ExplanationBlock`/`TruncationNote`；`lib/honesty.ts` `truncationState` | `ls frontend/src/panels/workbench/` |
| **HTTP 契约是"17 条双向相等"，`/analyze` 已是成员——本阶段不动它** | `endpoints.ts` 头注「17 条」；`test_api_contract_v2.py` `EXPECTED_PATHS`(17)==`app.openapi()["paths"]`==`endpoints.ts`（判据 1/9） | `grep -nE "EXPECTED_PATHS\|len\(paths\), 17" tests/test_api_contract_v2.py` |
| **`analysis` 属固定响应 schema，前端消费零契约改动** | `/analyze` 无新端点、`analysis` 键 types 已镜像 → 不改 `endpoints.ts`/`EXPECTED_PATHS`/17 计数 | 见上两行 |
| **归因产物有金标，可派 fixture** | `agent/analysis.py` + `eval/analysis/finance` + `eval/analysis/schema.json`；`make analysis-eval` 产 `analysis-<sha>` 报告 | `ls eval/analysis/ && grep -n "analysis-eval" Makefile` |
| **`chart.py` 只吃执行产物（C 决定刻意规避此约束）** | `chart.py:175-176` `if not sql: raise ChartError`（无执行 SQL 的裸/派生数据不画）；`:85-91` y 轴要求 `Number`，而 `items` 数值是出网字符串 | `sed -n '175,176p' agent/tools/chart.py` |
| **token/结果只在内存，禁客户端存储** | `no-persist.test.ts` FORBIDDEN 含 localStorage/sessionStorage/cookie/IndexedDB；`App.tsx:58-61` 会话态内存 | `grep -n "FORBIDDEN\|localStorage" frontend/src/__tests__/no-persist.test.ts` |

**起点结论**：本阶段是**纯前端消费**——`/analyze` 契约、`AnalysisPayload` 类型、复用件、金标 fixture 源**全部就绪**，
只缺"工作台入口 + 归因视图"。因取 C（CSS 条，不碰图表管道），**无一处需要立新的架构裁定，也不触碰 `serving/`、`semantic/` 与图表实现**。

---

## 1. 顺序与旁附注

### 1.1 唯一合法序

```
（前提：C 决定 = 零图表 / 零 chart.py 改动，无新 ADR；依 0026 产物契约 + 0018 框架 + 0028 演进）
   │
   └─▶ B0（analysis 三态 fixture，派自 eval/analysis/finance，无新数字）    [§11③ 先行]
          └─▶ B1（AnalysisBlock 归因视图：headline+items 表+CSS 条+steps 折叠+三态文案，测试先行）
                 └─▶ B2（AskWorkbench 查询|分析 模式切换：POST /analyze + 渲染 AnalysisBlock + 隐藏查询专用动作）
                        └─▶ B3（收口：make ui-check + honesty/no-persist/app 不破 + 交付注记）
```

B0→B1→B2 严格串行（fixture→组件→接线，测试先行）；B3 收口。全批次**仅动 `frontend/` 与 `docs/`**，
**不触碰 `serving/`、`semantic/`、`agent/tools/chart.py`**，故无契约计数变化、无 `semantic` 提交。

### 1.2 每条边的依据（越界的具体后果）

| 边 | 依据 | 后果 |
|---|---|---|
| B0 先于 B1 | §11 步骤③（先写测试） | 无 fixture 直接写组件 = 无法对三态做可复现断言，测试退化为快照空跑 |
| B1 先于 B2 | 组件先于接线 | 先接线后补组件会让 `AskWorkbench` 内联渲染逻辑，违背既有"表驱动 + 独立 block"分层 |
| 本阶段不引新端点 | 0018 判据 9、`endpoints.ts` 头注纪律 | `/analyze` 已是 17 条成员；**任何"顺手加 `/analyze/stream`"都会撞双向相等断言**，须回 0028 G-S4（本阶段明令不做，见 §3） |
| 数字只来自 `analysis` | N1、0026 决策⑤（确定性归因）| 前端**不得重算** Δ/contribution_pct；CSS 条仅用后端给定值做宽度比例，显示文本永远是后端原字符串 |
| 视图措辞不越界 | N2、0026「不推断业务因果」 | 标题/文案用「两期变化**贡献分解**」，**不得**写「原因/因果分析」；回落态不得粉饰成"已归因" |

### 1.3 旁附注（不阻塞本计划；处置须回对应 ADR）

| # | 观察 | 归属 | 处置建议（非本页裁定） |
|---|---|---|---|
| **N-a** | ADR-0026 状态仍 `proposed`，但 `agent/analysis.py`/`/analyze`/投影**代码已落地**（commit `091ec7e`） | ADR-0026 | 属 0026 的状态↔实现对账（N2 隐患）。本阶段消费其**已存在且类型已镜像**的产物，不依赖其 ADR 状态即可推进；建议在 B3 交付注记里点名提示 0026 补「落地注记」，**不在本页翻状态** |
| **N-b** | `analysis` 键是否已在响应契约测试逐键断言（`test_api_contract_v2.py` 的 `analysis` 命中仅为报告文件名） | 0026 判据 | 若逐键断言缺失，B1.1 的前端 render 测试可兜住消费面正确性；后端逐键断言属 0026 判据补强，登记不扩面 |
| **N-c** | 工作区尚存未提交的 `infra/adr/0028-frontend-oss-borrowing.md` 与 `docs/design/dev-plan-0028-*.md` | ADR-0028 收尾 | 与本阶段独立；提醒用户单独以 `docs(adr)` 提交，勿混入本阶段 feat/test 提交 |

---

## 2. 批次工作卡

每卡五段：**开工前置 / 工作项 / 收口判据 / 提交拆分 / 停批条件**。工作项只写"做什么 + 依据哪个决策/判据编号"。

### 2.1 B0：归因三态 fixture（派生自金标，无新数字）

- **依据**：§11③（先写测试）；0026 决策⑥（17 键形态）
- **开工前置**：无
- **入口命令**：`make ui-check`

| # | 工作项 | 依据 | §11 | 规模 |
|---|---|---|---|---|
| B0.1 | 依 `AnalysisPayload` 类型 + `eval/analysis/finance` 金标**派生 3 份前端 fixture**：`ok`（含 totals+多条 items+成功 steps）、`unavailable`（totals null、items 空、带 `text`）、`blocked`/`error`（steps 含裁剪步：sql null + reason_code）。数值一律用后端原字符串，**不得新造/改动任何数字**（N1） | 0026 决策⑤⑥、N1 | ③ | S |
| B0.2 | fixture 落 `frontend/src/__tests__/fixtures/analysis*.ts`（或就近），头部注明"派生自 eval/analysis/finance，非真实评测数字，仅驱动渲染" | N2 诚实 | ⑦ | S |

- **收口判据**：`make ui-check` 绿（fixture 仅类型合法、无副作用）；fixture 结构与 `AnalysisPayload` 逐字段对齐（TS 编译通过即证）
- **提交拆分**：`test(ui): 归因三态 fixture（派生自 eval/analysis/finance，无新数字，ADR-0026 ⑥）`
- **停批条件**：若"为了让图更好看"想微调 fixture 里的数字 → 停（N1：数字只能来自后端产物）

### 2.2 B1：`AnalysisBlock` 归因视图（测试先行）

- **依据**：0026 决策⑥（消费面）；C 决定（CSS 条，不引图表）
- **开工前置**：B0
- **入口命令**：`make ui-check`

| # | 工作项 | 依据 | §11 | 规模 |
|---|---|---|---|---|
| B1.1 | **先写 render 测试**（喂 B0 三态 fixture）：`ok` 断言渲染出 headline（metric×dimension、baseline→current、totals Δ 原字符串）、items 表（按后端序，不前端重排/重算）、steps 可折叠；`unavailable`/`blocked`/`error` 断言渲染后端 `text` 且不出现任何"计算值"；失败步断言不渲染 sql/rows（后端已裁剪） | 0026 决策⑤⑥、N1/N2 | ③ 先写测试 | M |
| B1.2 | 实现 `panels/workbench/AnalysisBlock.tsx`：headline + `items` 贡献表（复用 `DataTable`/`SqlPreview`）+ **CSS 比例条**（宽度 = 后端 `contribution_pct`/`delta` 绝对值比例；符号用方向/颜色；**显示文本 = 后端原字符串**）+ `steps` 折叠（每步 `SqlPreview`+`DataTable`，复用 `truncationState`/`TruncationNote`） | C 决定、0018 复用件 | ④ | M |
| B1.3 | 三态与回落：`status != ok` 走后端 `text` 稳定文案；标题固定「两期变化贡献分解（确定性，不含业务因果）」类措辞，杜绝"原因分析"越界词 | N1、0026「不推断因果」 | ④ | S |

- **收口判据**：B1.1 render 测试全绿；`grep -nE "原因分析\|因果分" frontend/src/panels/workbench/AnalysisBlock.tsx` → 0；组件内**无** `reduce`/`sum`/`toFixed` 等对归因数值的再计算（N1）
- **提交拆分**：`test(ui): AnalysisBlock 归因视图三态渲染断言（ADR-0026 ⑥）` + `feat(ui): 归因视图 AnalysisBlock（表+CSS 贡献条，不引图表管道，ADR-0026）`
- **停批条件**：若发现必须前端聚合才能得到条形宽度 → 停（N1），回看是否 `contribution_pct` 已足够（应足以按比例展示），不改用前端计算

### 2.3 B2：`AskWorkbench` 查询/分析模式切换（接线）

- **依据**：入口决策 B（模式切换）；0018 框架（不改四态骨架）
- **开工前置**：B1
- **入口命令**：`make ui-check`

| # | 工作项 | 依据 | §11 | 规模 |
|---|---|---|---|---|
| B2.1 | `AskWorkbench.tsx` 顶部加 `Segmented`「查询｜分析」（默认查询，不改现有行为）；`type Busy` 追加 `"analyze"`；新增 `analyze()`：`postJson<TurnPayload>(API.analyze, {question, model, session_id}, token)` → `setPayload` + `onTurnSeen`（与 `ask()` 同构） | 入口 B、0026 决策⑥、`/analyze` 同 AskBody | ④ | M |
| B2.2 | **分析模式**下隐藏「只看计划/只编译」（查询模式专属动作），保留域选择器与身份提示；`renderTurn` 顶部：`p.analysis !== null` 时渲染 `<AnalysisBlock analysis={p.analysis}/>`，否则按回落态（answer 携带单 SQL 结果 / clarify）如实走既有分支 | 入口 B、0026 回落语义、N2 | ④ | M |
| B2.3 | 分析模式**绝对时间提示**：占位/辅助文案说明"归因需绝对时间区间"；相对时间后端回落 `clarify` → 界面如实显示澄清（不前端猜、不补默认区间） | 0014③/0026、N1 | ④⑦ | S |

- **收口判据**：`make ui-check` 全绿（`app.test.tsx` 现有查询路径不破）；分析模式结果视图经 `AnalysisBlock`；模式切换与结果**只内存态**（`no-persist.test.ts` 保持绿）
- **提交拆分**：`feat(ui): 问数工作台查询/分析模式切换，消费 /analyze 归因（ADR-0026 ⑥）`
- **停批条件**：若接线诱发对 `/analyze` 的**流式化/新端点**冲动 → 停，本阶段维持 request/response（流式回 0028 G-S4）

### 2.4 B3：收口验证与交付注记

- **依据**：AGENTS §11⑦⑧；0018 判据 9（回归）
- **开工前置**：B2
- **入口命令**：`make ui-check` →（若旁及后端）`make test`

| # | 工作项 | 依据 | §11 | 规模 |
|---|---|---|---|---|
| B3.1 | 前端全量：`make ui-check`（render/honesty/no-persist/app/治理既有测试）；类型 `tsc`/build 通过 | 0018 判据 9 | ⑧ | S |
| B3.2 | 交付注记：在 `docs/design/`（或本页 §5 收口表）登记"阶段二归因**已接前端（request/response，非流式）**；数字全源自 0026 后端投影；图表未做（C 决定）；流式仍 gated 0028 G-S4"，并点名 N-a（提示 0026 状态对账） | N2 诚实 | ⑦ | S |

- **收口判据**：见 §5 全绿；**无新增端点、无契约计数变化、无 `chart.py` 改动**（三项用命令复验）
- **提交拆分**：`docs(design): 阶段二归因前端消费交付注记（非流式，ADR-0026 消费面）`
- **停批条件**：若为"顺手"改了 `serving/`/`semantic/`/`chart.py` 任一处 → 停（越出本阶段"纯前端消费"范围）

---

## 3. 明确排除（不在本计划，越界即违本阶段"纯前端消费"边界）

| 项 | 归属 | 排除理由 |
|---|---|---|
| `/analyze/stream` SSE / 任何流式呈现 | **ADR-0028 ④a（G-S4）** | 本阶段刻意维持 request/response；先把"稳定分步产物 + 真实前端消费者"做出来，为将来解锁 G-S4 铺路，但不在本阶段做 |
| 引入 Recharts/AG-UI 画贡献图、扩展 `chart.py` | **C 决定移除** | `chart.py` 只吃执行 SQL 产物，归因量是派生值；画图需破 0025 单一图表事实源（须专门 ADR），不划算 |
| 新增后端端点 / 改 `/analyze` 响应 schema | 0026 / 0018 判据 9 | `/analyze` 与 `analysis` 已就绪；扩面即动 17 条契约双向相等（全仓最脆断言） |
| 前端聚合/重算 Δ 或 contribution_pct | N1 | 数字只在后端 0026 确定性算一次，前端零重算 |
| 业务因果解释 / 预测 | 0026 边界 | 0026 明确"不推断业务因果"，视图只做两期变化贡献分解 |
| 把归因结果写客户端存储 | `no-persist` | 模式切换与结果只内存态 |

---

## 4. 提交拆分总则（AGENTS.md §8）

- 全批次仅 `frontend/` 与 `docs/`：**B0/B1.1** `test(ui)`、**B1.2** `feat(ui)`、**B2** `feat(ui)`、**B3** `docs(design)`。
- **不触碰 `serving/`、`semantic/`、`agent/`**：故无 `semantic` 提交、无 refactor+semantic 混提风险、无契约计数（17）变化。
- 若 N-b/N-c 处置牵动 `serving/api.py` 或 ADR-0026 状态，属独立事项，另开提交，不并入本阶段 feat/test。

---

## 5. 收口总判据（逐批复验；机读式优先于行号）

| 判据 | 落在哪批 | 复验命令 |
|---|---|---|
| 前端回归全绿（render/honesty/no-persist/app/治理不破） | B3.1 | `make ui-check` |
| 归因数字零前端重算 | B1.2/B1.3 | `grep -nE "reduce\(|\.sum\(|toFixed" frontend/src/panels/workbench/AnalysisBlock.tsx` → 0 |
| 视图无越界因果措辞 | B1.3 | `grep -rnE "原因分析\|业务因果\|导致了" frontend/src/panels/workbench/AnalysisBlock.tsx` → 0（标题反向：文案须声明"不含因果"） |
| 三态回落诚实渲染（不粉饰） | B1.1/B2.2 | 新增 `AnalysisBlock` render 测试绿（含 unavailable/blocked 仅显 `text`，失败步无 sql/rows） |
| 无新端点 / 无契约计数变化 | B3 收口 | `grep -nE "len\(paths\), 17" tests/test_api_contract_v2.py` 仍 17；`git diff --stat serving/ agent/tools/chart.py` → 无改动 |
| 模式切换不违存储红线 | B2 收口 | `make ui-check`（`no-persist` 绿即证） |
| 交付注记诚实（非流式/无图/点名 N-a） | B3.2 | 注记存在且无"已上线流式/图表"类表述（N2） |

**总证伪条件**：① 若 `AnalysisBlock` 无法在"前端零重算"下呈现贡献分布 → 退化为纯 items 表（去 CSS 条），仍不引图表管道/前端计算；② 若接线必须动 `serving/`/`chart.py` 才跑得通 → 停，说明本阶段边界判断有误，回设计重议。

---

## 6. 落地注记（2026-09-17，B0~B3 已执行）

| 批次 | 产物 | 状态 |
|---|---|---|
| B0 | `frontend/src/__tests__/fixtures/analysis.ts`（ok/unavailable/blocked 三态，身份字段取自金标 `attribution-001`，数值为内部自洽占位串并标 rendering-only） | ✅ |
| B1 | `frontend/src/panels/workbench/AnalysisBlock.tsx` + `__tests__/analysis-block.test.tsx`（8 断言）：headline + items 贡献表（CSS 条宽 = 后端 `contribution_pct` 原串）+ steps 折叠（复用 `SqlPreview`/`DataTable`/`truncationState`）+ 三态诚实文案 | ✅ |
| B2 | `AskWorkbench.tsx`：`Segmented`「查询｜分析」（默认查询，既有行为零改动）；分析模式 `POST /analyze`（与 `/ask` 同 AskBody）、`payload.analysis !== null` 走 `AnalysisBlock`、隐藏「只看计划/只编译」、绝对时间提示 | ✅ |
| B3 | 收口验证 + 本注记 | ✅ |

**收口实测（逐条复跑）**：`make ui-check` → typecheck 干净、`vitest run` **13 文件 99 测试全绿**（`no-persist`/`honesty`/`app`/`governance-*` 回归不破）；
`grep` 零前端重算（无 `reduce`/`sum`/`toFixed`）0 命中；无因果越界词 0 命中；`git status --short serving/ agent/ semantic/` → 无改动；`EXPECTED_PATHS` 仍 **17**（未动端点）。

**诚实边界（N2）**：本阶段交付的是**多步归因的前端呈现（request/response，非流式）**；
- **未做**流式（`/analyze/stream` 仍 gated 于 0028 G-S4）；
- **未做**图表（C 决定，未碰 `chart.py`/Recharts）；
- 所有归因数字**只来自** ADR-0026 后端 `analysis` 投影，前端零重算；fixture 数字为占位、**不得**当评测结果引用。

**旁附待办（非本阶段，见 §1.3）**：N-a —— **ADR-0026 状态仍 `proposed` 但代码已落地**（commit `091ec7e`），建议单独对账补「落地注记」并翻状态；N-c —— ADR-0028 两个文件仍**未提交**，应单独以 `docs(adr)` 落库，勿与本阶段 feat/test 混提。
