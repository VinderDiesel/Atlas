# 开发计划：ADR-0028 前端演进落地（借鉴 open-webui 信息架构 + AG-UI 事件契约）

- 日期：2026-09-17
- 状态：**ready to execute（ADR-0028 已于 2026-09-17 经用户确认翻 `accepted`；任务拆解完成，未开工）**——
  G-A 已解除，非 gated 批次（B1/B3/B2a/B4a）即可开工；裁定 B 使 **B2b 转 MVP 外·不排期**、
  裁定 C 使 B4b/B4c 维持 gated。起点实测见 §0。
- 范围：**仅 ADR-0028**（open-webui/AG-UI 精选借鉴）。fork/搬码、引入 CopilotKit SDK、
  open-webui 无关子系统、在线写编辑器——见 §3 排除清单。
- 依据：批次序与门禁取自 0028 决策 ①~④ 的依赖与 gate 标注；实现细节以**编号引用** 0028，不复制正文。
- 本页**不新增任何裁定**。发现 ADR 缺口只登记为门禁（§1.3），裁定须回 0028。

## 本页纪律（与 `dev-plan-0027-feedback-flywheel.md` 同源）

1. **不转述**：判据一律以编号引用（如「0028 判据 3」「决策 ④」），不复制 ADR 正文；
2. **计数三要素**：跨批引用的计数写成「公式 + 撰写时实测值 + 重测时点」（本计划涉两处计数：
   治理子页 8、HTTP 契约端点 17——均逐条验证归属）；
3. **不含工期估算**：AGENTS.md N1 禁止未实测数字，工期无测量口径 → 只给**工作项数**与相对规模
   （S/M/L，**明示主观量级、不得当承诺**）；
4. **不拍板未裁定项**：§1.3 门禁在裁定前不得开工对应批次；
5. **停批条件优先于进度**：命中即停该批回改 0028，不得"先开工后补"。

---

## 0. 起点实测（2026-09-17，本页撰写时逐条复测）

前端不是白地——0018 P0b~P2 已落导航/治理/会话壳。落地前先钉"哪些已有、哪些缺、哪些是坑"：

| 事实 | 实测证据 | 复测命令 |
|---|---|---|
| **顶栏已是三区导航（工作台/会话/治理）** | `App.tsx` `NAV_ITEMS` 三项 + 路由 `/ask`、`/sessions`、`/governance/:section` | `grep -n "NAV_ITEMS" frontend/src/App.tsx` |
| **治理是扁平 8 Tabs（无二级分组）** | `GovernanceLayout.tsx:54-63` `SECTIONS` 8 项平铺 | `grep -nE 'key: "(models\|metrics\|...)"' frontend/src/panels/governance/GovernanceLayout.tsx` |
| **⚠ 8 条并发是限流契约前提（① 不得破坏）** | `GovernanceLayout.tsx` 头注：「挂载即发 8 条并发（ADR-0022 决策⑥ 限流 240/min 推导前提就是终态 8 条——**不得合并或懒加载**）」，8 个 `useCollection` | `grep -cE "= useCollection<" frontend/src/panels/governance/GovernanceLayout.tsx` → 8（不含函数定义行） |
| **token/会话只在内存，禁客户端存储** | `no-persist.test.ts` FORBIDDEN 含 localStorage/sessionStorage/cookie/IndexedDB；`App.tsx:58-61` 会话 log 内存态 | `grep -n "FORBIDDEN\|localStorage" frontend/src/__tests__/no-persist.test.ts` |
| **HTTP 契约是"17 条双向相等"硬断言** | `endpoints.ts` 头注「17 条」；`test_api_contract_v2.py` `EXPECTED_PATHS`(17) == `app.openapi()["paths"]` == `endpoints.ts` 提取集（判据 1/9） | `grep -nE "EXPECTED_PATHS\|len\(paths\), 17" tests/test_api_contract_v2.py` |
| **`/api/v1/sessions` 不存在（② 服务端前提缺）** | `endpoints.ts` 无 sessions；`serving/api.py` 无该路由 → 0028 决策② GATE-② 成立 | `grep -n "sessions" frontend/src/api/endpoints.ts serving/api.py` → 0 命中 |
| **`/api/v1/analyze` 存在但 request/response，全仓无流式** | `api.py:834` `def analyze(...) -> dict`；无 `StreamingResponse`/`EventSource` → 0028 决策④「未流式」成立 | `grep -nE "StreamingResponse\|EventSource" serving/api.py frontend/src -r` → 0 |
| **（④ 消费对象真实存在）ADR-0026 已落地** | commit `091ec7e`：`agent/analysis.py` + `/analyze` + `analysis` 17 键投影 `_analysis_payload` | `git log --oneline -1 \| cat` |
| **两参考项目许可证已实测** | open-webui `LICENSE` 第 4 条品牌条款；AG-UI `LICENSE` = MIT（GitHub API `spdx_id:MIT`） | 见 0028 背景表（已登记） |

**起点结论**：真正"可立即做且零依赖"的只有 **①（治理二级分组，纯 Tabs 视觉分区）** 与 **③（把"零写编辑器"固化为回归断言）**，以及 **②/④ 的前端可先行段**（内存分组展示、事件词表登记文档）。
②/④ 的**服务端/实现段卡在两个门禁**上：新端点会牵动"17 条契约双向相等"这一全仓最脆的断言，必须与 `serving/api.py`、`endpoints.ts`、`test_api_contract_v2.py` 同步，且流式端点须守住 N3。

---

## 1. 顺序与门禁

### 1.1 唯一合法序

```
（总前提 G-A：ADR-0028 accepted ✅ 已解除 2026-09-17 —— 非 gated 批次解锁）
   │
   ├─▶ B1（① 治理区 build/control 二级分组）        [无依赖·可先行]
   ├─▶ B3（③ 只读不变量防漂移断言 + 边界文档）        [无依赖·可与 B1 并行；建议先于 B1 兜住"顺手加编辑"]
   ├─▶ B2a（② 会话面板分组布局，数据源仍内存 log）    [无依赖·可先行]
   │      └─▶ B2b（② 服务端会话列表） ◀── 裁定 B：G-S2 裁为 MVP 外·不排期
   └─▶ B4a（④ AG-UI 事件词表 ↔ LangGraph 映射登记文档）[无依赖·可先行]
          └─▶ B4b（④a /analyze/stream SSE 实现） ◀── GATED G-S4
                 └─▶ B4c（④b 引入 AG-UI/CopilotKit SDK） ◀── 触发后另议（0028 推翻条件 b）
```

B1/B2a/B3/B4a 互不依赖，可并行；均为 frontend/ 或 docs/ 内改动，**不触碰 serving/ 与 semantic/ 权威源**。B2b、B4b、B4c 层层 gated。

### 1.2 每条边的依据（换序/越界的具体后果）

| 边 | 依据 | 后果 |
|---|---|---|
| G-A 先于一切编码 | AGENTS §11 步骤①（无 accepted ADR 不实现）；本页不替 ADR 拍板 | ADR 仍 proposed 即编码 = 计划越权裁定，破坏"ADR 答选什么、计划答怎么做"分工 |
| B1 保持 8 并发不变 | 0022 决策⑥（240/min 推导前提=终态 8 条）、`GovernanceLayout` 头注 | 二级分组若退化为懒加载/合并请求 → 撞限流口径失真，且违反既有不变量 |
| B3 建议先于 B1 | 0028 决策③ | ① 改导航最容易"顺手加编辑按钮"，先用断言兜住"零写控件"再动导航 |
| B2a → B2b | 0028 决策② GATE-② | B2b 依赖新端点；未落地只做内存分组，**不得**为"持久化"偷偷写 localStorage（no-persist 会红） |
| 新端点 ⇒ 三处同步 | `test_api_contract_v2` 判据 1/9、`endpoints.ts` 头注纪律 1 | 加 `/sessions` 或 `/analyze/stream` 只改后端 → 双向相等断言立即红；须同批改 `EXPECTED_PATHS` + 计数 17→N + `endpoints.ts` |
| B4b → B4c | 0028 决策④「④a 零依赖先行 / ④b 触发后再议」 | 先对事件词表（不引依赖）；为尚无第二消费者的场景提前引 SDK = 违反 §10「不过度设计」 |

### 1.3 门禁（开工前须解除；本页不拍板）

| # | 门禁 | 阻塞 | 解除方式 |
|---|---|---|---|
| **G-A** ✅ 已解除 | ~~ADR-0028 仍 `proposed`~~ | ~~全部编码批次~~ | **2026-09-17 用户逐条确认 → ADR-0028 已 `accepted`；各卡「开工前置：G-A」视为满足** |
| **G-S2** ✅ 已裁定 | `GET /api/v1/sessions` 只读端点未落地（§0 实测 0 命中） | **B2b** | **裁定 B（2026-09-17）= 取②：裁为 MVP 外**——B2a 内存分组已满足单标签页诉求，不为持久列表现在扩 `serving` 契约面。**B2b 不排期**，触发条件回 0028「什么情况下应该推翻」② |
| **G-S4** （裁定 C 维持 gated） | ④ 流式实现的前置：0026 编排器稳定分步产物 + SSE 是否作为对外契约（牵动判据 9 计数）+ N3 流式不得成执行通道 | **B4b** | 解除触发条件（0028 推翻段 ④a）：0026 分步稳定 ≥1 评测周期且流式有真实消费价值；且裁定 `/analyze/stream` 是否纳入 17 条契约集。**裁定 C（2026-09-17）：当前确认仅 B4a 登记，④a/④b 维持 gated、不编码** |

> **SDK 门禁（B4c）**：不列 G 编号——它由 0028 推翻条件 (b)「≥2 前端消费者需同一事件流」自然触发，属更远期，本计划仅登记不排期。

---

## 2. 批次工作卡

每卡五段：**开工前置 / 工作项 / 收口判据 / 提交拆分 / 停批条件**。工作项只写"做什么 + 依据哪个决策/判据编号"。

### 2.1 B1：治理区 build/control 二级分组（①）

- **依据**：0028 决策 ①
- **开工前置**：G-A（B3 建议先行）
- **入口命令**：`make ui-check`

| # | 工作项 | 依据 | §11 | 规模 |
|---|---|---|---|---|
| B1.1 | **先写结构断言**：8 子页被划入 build（models/metrics/dimensions/synonyms/values）/ control（policies/reports/snapshots）两组；8 个 `key` 不变；旧 `/governance/:section` 路由仍可解析（不 404）；`unknownSectionNote`「8 子页」措辞仍属实 | 决策①、0028 验证方式① | ③ 先写测试 | S |
| B1.2 | `GovernanceLayout.tsx` 给 `SECTIONS` 增 `group` 字段并按两组渲染 Tabs 分区——**纯视觉/导航分组**，`useCollection` 调用与"挂载即发 8 条并发"保持逐字不变 | 决策①、0022 决策⑥ | ④ | S |
| B1.3 | 顶栏 `App.tsx`「治理」项与 `NAV_ITEMS` 不变；二级化只落在 `GovernanceLayout` 层，不新增顶层路由 | 决策① | ④ | S |

- **收口判据**：B1.1 测试绿；`make ui-check` 全绿（`no-persist`/`honesty`/`app` 不破）；`grep -cE "= useCollection<"` 仍 = 8（8 并发不变量，不含定义行）
- **提交拆分**：`test(ui): 治理二级分组结构断言（8 组归并 + 旧路由兼容）` + `feat(ui): 治理区 build/control 分组（无请求行为变化，ADR-0028 ①）`
- **停批条件**：若分组实现不可避免地改为懒加载/合并请求（破坏 8 并发）→ 停，回 0028 决策① 重议（不得违 0022 决策⑥）

### 2.2 B3：只读不变量防漂移断言（③）

- **依据**：0028 决策 ③
- **开工前置**：G-A（可最先）
- **入口命令**：`make ui-check` + `grep`

| # | 工作项 | 依据 | §11 | 规模 |
|---|---|---|---|---|
| B3.1 | 静态断言测试（仿 `no-persist.test.ts` 扫源码形态）：治理面板运行时代码**无** PUT/POST/写方法调用、**无** `editable`/写控件；`ReportDrawer`/`ValuesDrawer` 仅只读 | 决策③、0028 验证方式③ | ③ 先写测试 | S |
| B3.2 | 边界登记：在 `GovernanceLayout` 头注/`docs` 写明「治理面只读；编辑只可能服务 ADR-0027 proposal（提交候选，人工+CI 落 Git），绝不直写语义定义」 | 决策③、N8、ADR-0002 | ⑦ 文档 | S |

- **收口判据**：B3.1 断言绿；0028 验证方式③ `grep -nE '(PUT|POST).*semantic' frontend/src/panels/governance/` → 0 命中
- **提交拆分**：`test(ui): 治理面只读不变量断言（拒在线写编辑器，ADR-0028 ③）`
- **停批条件**：若发现现网前端已在调用某**写**语义端点（与起点实测矛盾）→ 停，回 0028 决策③ 复核（可能触及 N8 第二事实源）

### 2.3 B2a：会话面板分组布局（② 前端可先行段）

- **依据**：0028 决策 ②（前端部分，不触发 GATE-②）
- **开工前置**：G-A
- **入口命令**：`make ui-check`

| # | 工作项 | 依据 | §11 | 规模 |
|---|---|---|---|---|
| B2a.1 | `SessionPanel.tsx` 演进为「会话列表（按域/身份分组）+ 选中查看回合」布局，**数据源仍为 `App.tsx` 内存 `sessionState.log`**（不引客户端存储）；React 自绘，零搬 open-webui 码 | 决策②、no-persist 约束 | ④ | M |
| B2a.2 | 显式声明边界文案：列表仅当前标签页内存态、刷新即失（未接服务端列表前不得写"历史会话/持久"，N2） | 决策②、GATE-② | ⑦ 文档 | S |

- **收口判据**：`no-persist.test.ts` 保持绿（证明未用任何 `*Storage`）；分组为纯展示
- **提交拆分**：`feat(ui): 会话面板分组布局（内存态，不新增持久化，ADR-0028 ②）`
- **停批条件**：若为"持久化"引入 localStorage/sessionStorage → `no-persist` 立即红，禁止；须改走 B2b（GATE-②）

### 2.4 B2b：服务端会话列表端点（② 实现段）—— **裁定 B：MVP 外·不排期**

- **依据**：0028 决策 ② 裁定 B
- **状态**：**不排期**。G-S2 已由裁定 B 裁为 MVP 外——单标签页内存会话（B2a）已满足诉求，不为跨端持久列表现在扩 `serving` 只读契约面。
- **若将来触发**（0028「推翻」②：产品确需跨端持久列表）：补卡按 §11——`serving/api.py` 加只读 `GET /api/v1/sessions`（Bearer + 限流桶 + RLS 按身份过滤 checkpointer thread）+ **同批**改 `endpoints.ts` + `test_api_contract_v2.py` `EXPECTED_PATHS` 与判据 9 计数（17→N）+ `SessionPanel` 改读端点。
- **停批条件**：本批默认不做；若有人为"持久化"改走 localStorage → `no-persist` 立即红，禁止。

### 2.5 B4a：AG-UI 事件词表 ↔ LangGraph 映射登记（④ 契约登记段）

- **依据**：0028 决策 ④（词表采纳，不实现）
- **开工前置**：G-A
- **入口命令**：`grep` 自查（纯文档）

| # | 工作项 | 依据 | §11 | 规模 |
|---|---|---|---|---|
| B4a.1 | 写 `docs/design/agui-event-mapping.md`：固化 0028 ④ 表——`RunStarted/StepStarted/ActivitySnapshot(PLAN)/StateDelta(RFC6902)/ToolCall*/Reasoning*/Custom` 各映射到 LangGraph 八节点、`AnalysisPlan`(0026)、`agent/tools/*`、`ExplanationBlock` 的 Atlas 语义；`Custom` 字段登记 `snapshot_sha`/governance verdict/role | 决策④ | ⑦ 文档 | M |
| B4a.2 | 诚实措辞守线：文档只写「事件模型**借鉴 AG-UI 词表**」，**不得**写「Atlas 支持/兼容 AG-UI 协议」（未成 server，N2）；标明 ④a/④b 均 gated、实现待触发 | 决策④、N2 | ⑦ 文档 | S |

- **收口判据**：0028 验证方式④——映射文档存在；`grep -rn "支持 AG-UI 协议\|AG-UI 兼容" docs frontend` → 0 命中
- **提交拆分**：`docs(design): AG-UI 事件词表↔LangGraph 映射登记（借鉴，非兼容声明，ADR-0028 ④）`
- **停批条件**：无（纯登记）；但若文档措辞越界宣称兼容 → 回改，不粉饰

### 2.6 B4b / B4c：SSE 实现 / SDK 引入 —— **GATED**

- **依据**：0028 决策 ④、GATE-④（G-S4）；B4c 由 0028 推翻条件 (b) 触发
- **开工前置**：B4a 已登记 + G-S4 解除（B4b）/ 触发条件 (b)（B4c）
- **工作项**：留空。解除后按 §11「先写契约测试锁事件名枚举 → 实现 → N3 复核」补卡；B4c 额外按 §5 逐包回填 `node_modules/*/LICENSE` + `make license-check`（0018 P0b 口径）。
- **停批条件**：若流式路径出现绕过 Guard 直接执行 SQL → 立即否决（N3，最高优先）

---

## 3. 明确排除（不在本计划，越界即违 0028 决策）

| 项 | 归属 | 排除理由 |
|---|---|---|
| fork / 搬 open-webui 代码或组件 | 设计性不做 | 栈不符（Svelte→React）+ **BSD-3 品牌条款**（>50 用户不得去 branding）+ N7；借 IA 不借实现 |
| 引入 CopilotKit / AG-UI SDK 客户端 | 0028 ④b·触发后另议 | 为无第二消费者的场景引框架级依赖违 §10；MIT 已核实但依赖面扩张须重跑遥测/存储审阅 |
| 把 Atlas 声明为「AG-UI 兼容 server」 | 设计性不做 | 现仅对齐词表，无 AG-UI 客户端能直连；宣称=N2 造假 |
| 在线写编辑器（指标/同义词/值域） | 0028 决策③已否 | 破坏 Git 唯一事实源（0002）/N8；编辑仅可能经 0027 proposal 链路 |
| open-webui 的 calendar/notes/channels/watch/playground-images/automations | 域外 | 与「金融问数控制台」无关，非借鉴点 |
| 自助式语义层 / 交互式引导（text2insight §3.3.1/3.3.2） | 各自 ADR | 触及 0002 Git 权威源与 N8，需独立裁定，非本页 |

---

## 4. 提交拆分总则（AGENTS.md §8）

- 正常批次全在 `frontend/` 与 `docs/`：**B1** `test`+`feat(ui)`、**B3** `test`、**B2a** `feat(ui)`、**B4a** `docs(design)`。均**不触碰 `serving/` 与 `semantic/` 权威源**，故无 `semantic` 提交、无 refactor+semantic 混提风险。
- **唯一会触碰 `serving/` 契约的是 G-S2（`/sessions`）与 B4b（`/analyze/stream`）**，二者都 gated。一旦开工，**必须同一提交集内同步四处**：`serving/api.py` 端点 + `endpoints.ts` 常量 + `test_api_contract_v2.py` `EXPECTED_PATHS` + 判据 9 计数（17→N）；拆开提交 = 契约测试红。
- `frontend/` 若触发 `make ui-*` 目标（`ui-check`/`ui-build`），改动须在 commit 说明影响的端点/面板（AGENTS §8「说明影响面」的前端对应物）。

---

## 5. 收口总判据（映射 0028 验证方式，逐批复验）

| 0028 判据 | 落在哪批 | 复验命令（示意，取可机读式而非行号） |
|---|---|---|
| ui-check 全绿（no-persist/honesty/app 不破） | B1/B2a/B3 每批收口 | `make ui-check` |
| ① 结构：8 子页两组 + 旧路由可解析 + **8 并发不变** | B1.1/B1.2 | 新增导航测试绿 + `grep -cE "= useCollection<" frontend/src/panels/governance/GovernanceLayout.tsx` → 8 |
| ② 会话不违 no-persist | B2a.1 | `make ui-check`（no-persist 绿即证未用客户端存储） |
| ③ 治理面零写控件 | B3.1 | `grep -rnE "(PUT\|POST).*semantic\|editable" frontend/src/panels/governance/` → 0 |
| ④ 词表登记诚实（不宣称兼容） | B4a.2 | `grep -rn "支持 AG-UI 协议\|AG-UI 兼容" docs/` → 0 |
| ④a N3 不破（G-S4 解除后） | B4b | 流式端点 `grep` 无执行语句 + SQL 经 `sql_guard`；契约测试同锁 request/response 与 SSE 两态 |
| 许可证（若 B4c 引 SDK） | B4c | `make license-check` 绿 + 0028 决策①表逐包 `node_modules/*/LICENSE` 回填 |
| 全量回归 | 每批收口 | 前端批 `make ui-check`；触 `serving/` 批 `make test`（含判据 9 集合相等） |

**总证伪条件**（同 0028）：① 若二级分组无法在不破坏"8 条并发"契约下实现 → ① 退化为不加二级导航、维持扁平 8 页；④ 若任何流式端点构成绕过 Guard 的执行面 → ④ 一律否决，回退纯 request/response。
