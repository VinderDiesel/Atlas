# 开发计划：④a `/analyze/stream` SSE 流式端点（AG-UI 词表对齐·展示专用）

- 日期：2026-09-17
- 状态：**④a 已实现（B0~B3·代码 commit `5f8f019`/`ca4424c`，收口判据 1~7 实测见 §6）**。
  执行模型 A（compute-then-stream）；GATE-④ 对 ④a 的 gate 已由用户裁定解除
  （触发条件 (a) 满足，证据见 ADR-0028「GATE-④ 解锁」注记）；**④b（SDK）维持 gated**。
  诚实边界：已实现 ≠ 生产就绪，亦 ≠ AG-UI 兼容（N2，详 §6 落地注记）。
- 范围：新增 `POST /api/v1/analyze/stream`（SSE，`text/event-stream`），复用 `/analyze`
  同一执行路径产出 `AnalysisResult`，再按 `docs/design/agui-event-mapping.md` §2/§4
  词表把**已算好的分步产物回放**为事件流；前端 `AnalysisBlock` 渐进消费。
- 依据：ADR-0028 决策 ④（④a，本批解锁）；产物契约 = ADR-0026（`AnalysisPayload` 17 键）；
  事件词表 = `agui-event-mapping.md`（B4a 登记，借鉴非兼容）；控制台/前端栈 = ADR-0018。
- **本批不占新 ADR 编号**：④a 的架构裁定已在 ADR-0028 决策 ④，本页仅展开实现批次与判据。
- 发现契约缺口只登记为旁附注（§1.3），裁定须回对应 ADR，不在本页拍板。

## 本页纪律（与 dev-plan-0027/0028 同源）

1. **不转述**：判据以编号引用（「0028 决策④」「0026 决策⑥」「0018 判据 9/10」），不复制 ADR 正文；
2. **计数三要素**：HTTP 契约端点数写「公式 + 撰写时实测 + 重测时点」——
   本批 **17 → 18**（新增 `/analyze/stream` 一处），落 §5 判据 1 三处相等；
3. **不含工期估算**：AGENTS.md N1；只给工作项数与主观量级（S/M/L，非承诺）；
4. **不拍板未裁定项**：④b / per-request 锁重设计（方案 B）在对应裁定前不得扩面；
5. **停批条件优先于进度**：命中即停该批回改设计，不得「先开工后补」。

---

## 0. 起点实测（2026-09-17，本页撰写时逐条复测）

④a 不是白地：接缝、词表、真链评测证据均已就位，缺的是端点与消费。

| 事实 | 实测证据 | 复测命令 |
|---|---|---|
| 分步接缝存在（compute-then-stream 的产物源） | `agent/graph.py:_iter_analysis_step_events` + `AnalysisStepEvent`（`c0329fb`） | `grep -n "def _iter_analysis_step_events" agent/graph.py` |
| `/analyze` 同步、持实例级 RLock 一次性返回 | `serving/api.py:834`；`agent/graph.py:771 self._lock=RLock()` | `grep -n "threading.RLock" agent/graph.py` |
| AG-UI 事件词表已登记（对齐目标） | `docs/design/agui-event-mapping.md` §2/§4/§5 | `sed -n '28,82p' docs/design/agui-event-mapping.md` |
| 契约 17 条·三处双向相等 | `test_api_contract_v2.py`（26 passed+74 子测 @ `e7909f2`） | `uv run pytest tests/test_api_contract_v2.py -q` |
| 前端 request/response 消费已落地 | `AskWorkbench` mode=analyze + `AnalysisBlock`（`502650f`） | `ls frontend/src/panels/workbench/AnalysisBlock.tsx` |
| 执行安全已实测：被拒 SQL 不出网 | seam 测试 blocked 用例（`step.sql is None`） | `uv run pytest tests/test_analysis_stream_seam.py -q` |
| 真链评测绿（解锁依据） | 报告 `analysis-e7909f2`/`1e2e557`（EX 97/97） | `ls eval/reports/analysis-e7909f2.json eval/reports/1e2e557.json` |

## 1. 唯一合法实施序与前置门

**B0 → B1 → B2 → B3**（测试先行；任一批停批条件命中即停并回改设计）。

- **B0 前置**：无（接缝/词表已就位）。
- **B1 前置**：B0 红测试到位，定义事件名枚举与 18 路径契约。
- **B2 前置**：B1 端点可返回稳定事件序列。
- **B3 前置**：B1/B2 全绿。

### 1.3 旁附注（发现即登记，不擅自拍板）

- **N-α**：若 `StreamingResponse` 令 `app.openapi()["paths"]` 计数或响应 schema 解析异常，
  先登记、按 0012/0018 契约测试口径处置，不改既有 17 端点的 request/response 形态。
- **N-β**：`STEP_*` 事件名以词表「LangGraph 节点」为准；分析四步 `ANALYSIS_ROLES` 若与节点
  命名冲突，落 `Custom(name="analysis_step")` 承载（词表 §3），不污染标准事件——如须裁定回 0028。
- **N-γ**：真渐进（方案 B，SQL 执行期跨 yield 推送）需 per-request 锁改造，属**新裁定**，
  本批排除（§3）。

## 2. 批次卡（五段：目标 / 前置 / 工作项 / 测试先行 / 停批条件）

### B0 · 契约与事件测试（红先行）
- **目标**：锁定「18 路径 + SSE 事件名逐一对齐词表 + N3 grep 判据」为可机读断言。
- **工作项（S）**：新增 `tests/test_analyze_stream_contract.py`：
  ① 端点存在（TestClient 对 `/api/v1/analyze/stream` 非 404/405）；② 事件名枚举 ==
  本页 §2 附表（`RUN_STARTED/STEP_STARTED/STEP_FINISHED/TOOL_CALL_RESULT/STATE_SNAPSHOT/RUN_FINISHED/RUN_ERROR`）；
  ③ N3 grep（流式函数内无 `execute|INSERT|UPDATE|DROP`）；④ 三处计数相等 @18。
- **测试先行**：B0 **即**测试；此时无实现 → 预期红（端点未注册 / 计数不等）。
- **停批**：若事件词表与 `agui-event-mapping.md` 无法一一对应 → 停，回 0028 补 N-β 裁定。

### B1 · 后端 SSE 端点 + 序列化 + 契约 18（绿）
- **目标**：compute-then-stream——复用 `agent.analyze()` 得 `AnalysisResult`，释放锁后回放为 SSE。
- **前置**：B0 红测试定义目标。
- **工作项（M）**：
  - `serving/api.py` 新增 `analyze_stream`（镜像 `analyze` 的鉴权/模型/限流/`AskBody`），
    返回 `StreamingResponse(media_type="text/event-stream")`；
  - 序列化助手 `AnalysisResult → 事件序列`，复用 `_analysis_payload`/`_jsonable`
    （Decimal→字符串出网，与 request/response 逐字同源）；
  - 三处同步：`EXPECTED_PATHS` +1、`endpoints.ts` +`analyzeStream`、OpenAPI 自动 +1。
- **测试先行**：B0 测试转绿；`test_api_contract_v2.py` 全绿 @18。
- **停批**：① 若在端点内出现任何**新** SQL 构造/执行语句（绕过 `agent.analyze` 唯一通道）→
  立即停（N3 最高优先）；② 若三处计数无法一次对齐 18 → 停，回改 §3/§5 而非硬凑。

### B2 · 前端 SSE 消费 + `AnalysisBlock` 渐进渲染（绿）
- **目标**：`AskWorkbench` 分析模式改走流式端点；终态与 request/response 版渲染逐字一致。
- **前置**：B1 端点可返回稳定事件序列。
- **工作项（M）**：
  - `frontend/src/api/` 增 POST SSE 读取器（`fetch` + `ReadableStream` 手动解析 `data:` 帧，
    Bearer 从内存注入；**不用** `EventSource`——不支持 POST+`Authorization`，token 不入 query，N9）；
  - `AnalysisBlock` 支持「事件累积 → 渐进渲染」，终态 `STATE_SNAPSHOT` 即完整 17 键 payload；
  - 贡献比例条仍走 C（前端 CSS 宽度 = 后端 `contribution_pct` 原串，零重算，N1）。
- **测试先行**：先加前端测试——喂固定事件序列 fixture → 断言渲染出与 request/response 相同的
  totals/items/steps，且 `honesty.test.ts`（无来源数字）、`no-persist.test.ts`（无 `*Storage`）保持绿。
- **停批**：若渐进渲染路径与 request/response 路径产出不一致 → 停（单一事实源破，N1/0026 决策⑥）。

### B3 · 文档与状态回写
- **目标**：诚实反映落地事实。
- **工作项（S）**：本页状态 →「已实现」+ 追加落地注记；README/Known Limitations 据实
  标注「④a SSE 已落地（借鉴 AG-UI 词表，非兼容声明）」；**④b 仍 gated**（N2）。
- **停批**：README 出现「AG-UI 兼容」字样 → 停（N2）。

## 3. 明确排除（本批不做）

- **④b**：AG-UI/CopilotKit SDK 接入（触发条件 (b) 未达，gated）；
- **per-request 锁重设计 / 真渐进-during-SQL**（方案 B）：并发/一致性重设计，属新裁定；
- **图表 / `chart.py` 改动**：`render_chart` 只吃执行 SQL 产物、归因量属派生值（0025 约束）；
- **`STATE_DELTA`（JSON Patch）/ `Reasoning*`**：无真实增量源/推理数据 = 造假风险（N1）；
- **新执行通道**：流式端点绝不成为第二执行面（N3）。

### 附：本批 SSE 事件 → 载荷（展示字段，全部源自 `AnalysisResult`）

| 事件 | data 载荷 | 来源 |
|---|---|---|
| `RUN_STARTED` | `{intent}` | `AnalysisResult.plan.intent`（回落 null） |
| `STEP_STARTED` | `{role}` | `ANALYSIS_ROLES` 逐子计划 |
| `STEP_FINISHED` | `{role, status, latency_ms}` | 每步 `TurnResult.kind/latency_ms` |
| `TOOL_CALL_RESULT` | `{role, columns, rows(str), row_count}` | 每步已执行事实（`_jsonable`；被拒步无此事件，N3） |
| `STATE_SNAPSHOT` | 完整父 `TurnPayload`（含 `analysis` 17 键投影） | 复用 `_analysis_turn_payload`（与 `/analyze` 同源；仅在有分析产物且全成功时发出——plan None / blocked / error 无此事件） |
| `RUN_FINISHED` | `{status}` | `analysis_status(result)`（ok/unavailable/…） |
| `RUN_ERROR` | `{reason_code, text}` | blocked/error 终态 |

## 4. 技术要点 / 不变量

- **单一执行通道**：端点**不得**自行编译/执行 SQL；唯一路径 = 调 `agent.analyze()`
  （其 `node_execute` 过 Guard）。词表 §5 硬边界 + `grep` N3 判据双锁。
- **锁生命周期**：`self._lock` 是 RLock（可重入）；`analyze()` 内完整持有并返回，
  流式**在 `analyze()` 返回后**才逐事件推送 → 执行期不跨 yield 持锁（避免实例级串行化）。
- **SSE 帧格式**：`data: <json>\n\n`；`X-Accel-Buffering: no`；终态后 `RUN_FINISHED` 关流。
- **契约测试兼容**：`/analyze/stream` 的 OpenAPI 响应声明为 `text/event-stream`；
  确认不破坏 `test_api_contract_v2.py` 双向相等（路径计数 +1、既有 17 端点形态不变）。
- **前端无 EventSource**：POST + Bearer（内存）→ `fetch` 流式读取手动解析。
- **no-persist**：流式事件仅内存态累积，不落任何 `*Storage`。

## 5. 机读收口判据（全绿才算 B 批完成）

1. **三处计数相等 @18**：`EXPECTED_PATHS` == `app.openapi()["paths"]` == `endpoints.ts`；
   复测 `uv run pytest tests/test_api_contract_v2.py -q` 绿。
2. **事件名枚举断言**：`test_analyze_stream_contract.py` 断言 SSE 事件名与本页 §2 附表
   一一对应（AGENTS.md §3 术语 + 词表 §2）。
3. **N3 零执行**：`grep -nE "execute|INSERT|UPDATE|DROP" serving/api.py` 在流式函数体内 0 命中；
   被拒 SQL 不出网（`step.sql is None` 语义沿用）。
4. **N1 零编造**：`grep -nE "reduce\(|\.sum\(|toFixed" frontend/.../AnalysisBlock.tsx` 命中 0；
   所有数字源自后端字符串。
5. **N9 / no-persist**：前端 `grep EventSource` = 0、无 token-in-query、`no-persist.test.ts` 绿。
6. **N2 对外口径**：全仓 `grep -nE "AG-UI 兼容|支持 AG-UI"`（README/KL/界面）= 0；
   ④a 未落地前 README 标 gated。
7. **回归不破**：`make test` 全绿（含接缝测试、379 分析测试）；`make ui-check` 全绿；
   request/response 版 `/analyze` 行为与渲染**逐字不变**（流式为增量，非替换）。

## 6. 落地注记

**交付 commit**（按时序）：文档 `7ae9c3a` → 后端 B1 `72dfec9` / 契约测试 B0 `80dba40` /
快照台账 `6ab38f6` → B1.1 精修（STATE_SNAPSHOT 发完整 TurnPayload）`5f8f019` →
前端 B2（reducer+reader+渐进渲染+回落）`ca4424c`。

**收口判据 1~7 实测输出**（2026-09-17本机逐条复测）：

| 判据 | 实测 | 命令 |
|---|---|---|
| 1 三处计数 @18 | `test_api_contract_v2` + `test_analyze_stream_contract`：**34 passed, 76 subtests** | `uv run pytest tests/test_api_contract_v2.py tests/test_analyze_stream_contract.py -q` |
| 2 事件名枚举断言 | 流式契约 8 例全绿（事件序列逐一对齐词表） | 同上 | 
| 3 N3 零执行 | 定位注册路由 `route.endpoint` 源码无 execute/INSERT/…、含 `agent.analyze(`；被拒步无 `TOOL_CALL_RESULT` | 绿 |
| 4 N1 零重算 | `AnalysisBlock.tsx` grep `.reduce(`、`.sum(`、`toFixed` 命中 **0** | grep |
| 5 N9 / no-persist | 前端运行时代码 `EventSource` 命中 **0**；`no-persist.test.ts` 绿；token 只在 Authorization 头（`analysis-stream.test.ts` 断言 URL 不含 token） | grep + vitest |
| 6 N2 对外口径 | README/KL/界面无肯定式「AG-UI 兼容/支持 AG-UI」（仅否定式声明与 grep 模式例） | grep |
| 7 回归不破 | 全量 `uv run pytest -q` = **1219 passed, 14 skipped, 532 subtests**；`make ui-check` 全绿（tsc → vitest **113** → build）；request/response `/analyze` 行为与渲染逐字不变（流式为增量） | make |

**诚实边界**（不得越过）：
- **compute-then-stream ≠ SQL 边执行边流**：`agent.analyze()` 跑完释放实例 RLock 后，才逐事件回放已算好的产物（执行模型 A）；真渐进（方案 B、SQL 执行期跨 yield 推送）需 per-request 锁重设计，属新裁定（§3 排除）。
- **借鉴词表 ≠ AG-UI 兼容**：事件名对齐 `agui-event-mapping.md` 登记词表，无 AG-UI 客户端可直连；④b（SDK 接入）**仍 gated**。
- **无 `STATE_DELTA` / `Reasoning*`**：无真实增量源/推理数据，不伪造（N1）。
- **前端回落**：流式故障或无分析意图（无 STATE_SNAPSHOT）时回落 `/analyze` request/response；`/analyze` 端点本身未变。
