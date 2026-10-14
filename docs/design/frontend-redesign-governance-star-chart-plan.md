# Atlas 前端重设计实施计划 - Phase 2b: 治理星图

> **Goal:** 将 `/governance` 治理面重构为"治理星图"（Governance Star Chart）——星图分区导航 + 星体状态徽章 + 星体卡片条目，保持 8 子页只读边界与 sources 溯源顶置。

**Architecture:** 星图控制台（域/locale 选择器 + 规则折叠）→ 星图分区 Tab（构建面 5 页 / 管控面 3 页）→ 子页表格（星体徽章 + 等宽强化）。

**Spec:** `docs/design/frontend-redesign.md`

**Tech Stack:** React 18, TypeScript, AntD 5, CSS custom properties

## Global Constraints

- 诚实性约束：治理面一律只读（ADR-0028 决策 ③）；sources 顶置（0022 决策 ⑤）；错误原文渲染；未知 kind 展示原始 payload
- 挂载即发 8 条（ADR-0022 决策 ⑥ 限流推导前提）不得合并或懒加载
- 子页切换不重拉
- 凡数字/标识符等宽（JetBrains Mono）
- 品牌色与 Phase 1/2a 一致（深空 4 层 + 语义色）
- 测试守线：`governance-readonly.test.ts`、`governance-grouping.test.ts` 等必须通过

---

## Task 1: 星体徽章组件（StarBadge）

**Files:**
- Create: `frontend/src/components/StarBadge.tsx`
- Create: `frontend/src/__tests__/star-badge.test.tsx`

**内容:** 状态 → 星体徽章（点 + 标签），状态映射：

- `active` / `passed` / `ok` → 绿（--atlas-success）
- `draft` / `pending` → 琥珀（--atlas-accent）
- `retired` / `rejected` → 灰（--atlas-muted）
- `failed` / `error` / `blocked` → 红（--atlas-danger）
- `superseded` → 紫（--atlas-handoff）
- 未知值 → 灰 + 原文展示（不猜分支）

## Task 2: 治理页头增强（星图控制台）

**Files:**
- Modify: `frontend/src/panels/governance/GovernanceLayout.tsx`
- Modify: `frontend/src/styles/global.css`

**内容:** 域/locale 选择器与规则折叠放入 `.atlas-console` 控制台条；Tabs 加 `.atlas-star-chart` 容器与分组视觉。

## Task 3: 子页表格星体化

**Files:**
- Modify: `frontend/src/panels/governance/ModelsSection.tsx`
- Modify: `frontend/src/panels/governance/MetricsSection.tsx`
- Modify: `frontend/src/panels/governance/DimensionsSection.tsx`
- Modify: `frontend/src/panels/governance/SynonymsSection.tsx`
- Modify: `frontend/src/panels/governance/ValuesSection.tsx`
- Modify: `frontend/src/panels/governance/PoliciesSection.tsx`
- Modify: `frontend/src/panels/governance/ReportsSection.tsx`
- Modify: `frontend/src/panels/governance/SnapshotsSection.tsx`

**内容:** 每子页的状态列改用 StarBadge；FIBO 对齐列加对齐徽章（青 = exact、琥珀 = broad、灰 = none）。

## Task 4: SectionShell 增强

**Files:**
- Modify: `frontend/src/panels/governance/GovernanceSection.tsx`
- Modify: `frontend/src/styles/global.css`

**内容:** sources 展开区加星图视觉（连接线 + 文件图标）；count 显示加等宽。

## Task 5: 最终验证与文档

- 全量测试 + typecheck + build
- 更新 `docs/design/frontend-redesign.md` 标记 Phase 2b 完成
