# Atlas 前端重设计实施计划 - Phase 2c: 数据接入港

> **状态：已完成（2026-10-14）** —— Task 1–5 全部落地，288 个前端测试通过。

> **Goal:** 将 `/setup` 接入面重构为"数据接入港"（Data Harbor）——航道导航（6 泊位节点）+ 泊位卡头读数 + 状态星体化，保持全部写入/校验/CAS 逻辑不变，纯表现层改造。

**Architecture:** 航道导航（HarborLane：泊位节点 + 连接线，替换 AntD Steps）→ 泊位卡片（berth chip + 仪表读数）→ 表格星体化（source probe / deployment bound / draft status）。

**Spec:** `docs/design/frontend-redesign.md`（模块 3：数据接入港，隐喻"港口"）

**Tech Stack:** React 18, TypeScript, AntD 5, CSS custom properties

## Global Constraints

- 诚实性约束：凭据只接受 `env:<NAME>` 引用（不显示明文值）；渲染只取固定键；`token` 空串不发请求（SSR 初始态零请求）
- 纯表现层：不改任何提交 / 校验 / 审核 / 导入 / CAS 发布逻辑与表单校验规则
- 状态展示改用**契约原文**（沿用 Phase 2b 决定：StarBadge 展示 raw 值不翻译）；`STATUS_LABELS` / `statusColor` 删除
- 凡数字/标识符等宽（JetBrains Mono）
- 品牌色与 Phase 1/2a/2b 一致（深空 4 层 + 语义色）
- 测试守线：`app.test.tsx`（/setup 未认证态"接入流水线"+"尚未登录"）、`control.test.ts`、`source-wizard.test.tsx`、`semantic-draft.test.tsx` 必须通过

---

## Task 1: 港区航道导航组件（HarborLane）

**Files:**
- Create: `frontend/src/panels/setup/HarborLane.tsx`
- Create: `frontend/src/__tests__/harbor-lane.test.tsx`
- Modify: `frontend/src/styles/global.css`

**内容:** 6 泊位航道导航（`PIPELINE_STEPS` 迁入本组件并导出）：

- 节点状态：`done`（绿，i < current）/ `active`（青 + 光晕，i === current）/ `wait`（灰，i > current）
- 节点编号 `01`–`06` 等宽；点击泊位跳步（与旧 `Steps onChange` 同语义）；`aria-current="step"`
- 连接线：泊位间横轨（done 段绿、active 段青→灰渐变、wait 段灰）

## Task 2: 泊位卡头与仪表读数

**Files:**
- Modify: `frontend/src/panels/setup/SetupPipeline.tsx`

**内容:**

- `Steps` 替换为 `HarborLane`（`PIPELINE_STEPS` 定义移除，改从 HarborLane 导入）
- 6 个 Step Card 标题加泊位号 chip（`.atlas-berth-chip`，`01`–`06` 等宽）
- Card extra 计数改仪表读数（`.atlas-gauge`）：已连接源 / 已绑定部署；`已发布制品 (N)` 数字等宽
- probe Alert 描述中 `engine_version` / `observed_at` 等宽
- `PageHeader` description 加"数据接入港 · "前缀

## Task 3: 泊位表格星体化

**Files:**
- Modify: `frontend/src/panels/setup/SetupPipeline.tsx`

**内容:**

- source 表「探测」列：`last_probe.status` → `StarBadge`（`ok`→绿 / `blocked`→红，已有映射）；`null` 保持「未探测」文本（契约外事实不造词）
- deployment 表「活动发布」列：`active_release_id` 为 null → `StarBadge("unbound")`；非 null → `StarBadge("bound")` + 等宽 sha
- 「白名单表 N 张」数字等宽；source_id 列等宽（`Text code`）

## Task 4: 草稿流水状态星体化

**Files:**
- Modify: `frontend/src/components/StarBadge.tsx`
- Modify: `frontend/src/styles/global.css`
- Modify: `frontend/src/__tests__/star-badge.test.tsx`
- Modify: `frontend/src/panels/setup/SetupPipeline.tsx`

**内容:**

- StarBadge 新增 `verified` 色调（青，`--atlas-primary`："青色 = 已验证"）与流水线映射：
  `validated` / `reviewed` → verified；`source_imported` / `release_ready` / `published` → active
- Step 3 Card extra 与 Descriptions 的 status Tag → `StarBadge(status)`（契约原文）
- 草稿/制品选择器选项的状态标注改用 raw 值；删除 `STATUS_LABELS` 与 `statusColor`

## Task 5: 最终验证与文档

- 全量测试 + typecheck + build
- 更新 `docs/design/frontend-redesign.md` 标记 Phase 2c 完成
- 更新本计划文件勾选
