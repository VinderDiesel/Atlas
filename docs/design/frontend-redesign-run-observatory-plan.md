# Atlas 前端重设计实施计划 - Phase 2a: 运行观测台

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 将 `/runs` 运行历史面板重构为"运行观测台"（Run Observatory），采用左右分栏 + 仪器面板风格

**Architecture:** 运行目录 sidebar + 运行详情 main + 状态仪表头 + 执行路径图/时间线/节点详情三区布局

**Tech Stack:** React 18, TypeScript, AntD 5, Recharts 2, React Flow, CSS custom properties

**Spec:** `docs/design/frontend-redesign.md`

## Global Constraints

- 诚实性约束：事件流是运行事实唯一来源、不伪造答案、捕获正文显式钻取、错误原文渲染
- 所有 run_id、seq、timestamp 等数字/标识符一律等宽字体
- 品牌色与 Phase 1 一致
- 测试守线：`run-timeline.test.tsx`、`run-graph.test.tsx`、`run-view-model.test.ts` 等必须通过

---

## Task 1: 运行目录卡片化（Run Catalog Cards）

**Files:**
- Modify: `frontend/src/panels/runs/RunPanel.tsx`

**Interfaces:**
- Consumes: `RunSummary` 列表
- Produces: 左侧运行目录卡片列表

- [ ] **Step 1: 提取目录为独立渲染函数**

在 `RunPanel.tsx` 中添加 `RunCatalogCard` 渲染：

```tsx
function RunCatalogCard({
  run,
  selected,
  onClick,
}: {
  run: RunSummary;
  selected: boolean;
  onClick: () => void;
}) {
  const meta = statusTag(run.status);
  return (
    <div
      className={`atlas-run-card${selected ? " atlas-run-card--selected" : ""}`}
      onClick={onClick}
      role="button"
      tabIndex={0}
    >
      <div className="atlas-run-card__head">
        <span className={`atlas-run-card__dot atlas-run-card__dot--${meta.color}`} />
        <Text strong className="atlas-mono">{run.run_id}</Text>
      </div>
      <div className="atlas-run-card__meta">
        <Text type="secondary">{run.question ?? "（无问句）"}</Text>
      </div>
      <div className="atlas-run-card__foot">
        <Tag color={meta.color}>{meta.label}</Tag>
        <Text type="secondary" className="atlas-mono">seq {run.last_seq}</Text>
      </div>
    </div>
  );
}
```

- [ ] **Step 2: 添加 CSS**

在 `frontend/src/styles/global.css` 中添加：

```css
/* ── 运行目录卡片（Run Observatory） ─────────────────── */

.atlas-run-card {
  padding: 12px 14px;
  border: 1px solid var(--atlas-border);
  border-radius: 8px;
  background: var(--atlas-surface);
  cursor: pointer;
  transition: border-color 150ms ease, background 150ms ease;
}

.atlas-run-card:hover {
  border-color: var(--atlas-primary);
}

.atlas-run-card--selected {
  border-color: var(--atlas-primary);
  background: rgba(110, 231, 245, 0.06);
}

.atlas-run-card__head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 6px;
}

.atlas-run-card__dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
}

.atlas-run-card__dot--green { background: var(--atlas-success); }
.atlas-run-card__dot--red { background: var(--atlas-danger); }
.atlas-run-card__dot--orange { background: var(--atlas-accent); }
.atlas-run-card__dot--blue { background: var(--atlas-primary); }

.atlas-run-card__meta {
  font-size: 13px;
  margin-bottom: 8px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.atlas-run-card__foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
```

- [ ] **Step 3: 运行测试验证**

```bash
cd frontend
npm test -- run
```

Expected: 所有 run 相关测试通过

- [ ] **Step 4: 提交**

```bash
git add frontend/src/panels/runs/RunPanel.tsx frontend/src/styles/global.css
git commit -m "feat(runs): redesign run catalog as observatory cards"
```

---

## Task 2: 左右分栏布局（Observatory Layout）

**Files:**
- Modify: `frontend/src/panels/runs/RunPanel.tsx`

- [ ] **Step 1: 重构主布局为左右分栏**

将 `RunPanel` 主体渲染改为：

```tsx
<div className="atlas-observatory">
  <aside className="atlas-observatory__sidebar">
    {/* 运行目录 */}
  </aside>
  <main className="atlas-observatory__main">
    {/* 运行详情 */}
  </main>
</div>
```

- [ ] **Step 2: 添加 CSS**

```css
/* ── 运行观测台布局（Run Observatory） ───────────────── */

.atlas-observatory {
  display: grid;
  grid-template-columns: 320px 1fr;
  gap: 20px;
  align-items: start;
}

.atlas-observatory__sidebar {
  display: flex;
  flex-direction: column;
  gap: 12px;
  max-height: calc(100vh - 180px);
  overflow-y: auto;
  padding-right: 4px;
}

.atlas-observatory__main {
  min-width: 0;
}

@media (max-width: 900px) {
  .atlas-observatory {
    grid-template-columns: 1fr;
  }
}
```

- [ ] **Step 3: 运行测试**

```bash
cd frontend
npm test -- run
```

- [ ] **Step 4: 提交**

```bash
git add frontend/src/panels/runs/RunPanel.tsx frontend/src/styles/global.css
git commit -m "feat(runs): add observatory sidebar+main layout"
```

---

## Task 3: 状态仪表头（Status Instrument Header）

**Files:**
- Create: `frontend/src/components/RunStatusHeader.tsx`
- Modify: `frontend/src/panels/runs/RunPanel.tsx`

- [ ] **Step 1: 创建状态仪表头组件**

```tsx
/**
 * 运行状态仪表头（Run Status Instrument Header）
 *
 * 将 run 元信息展示为仪器读数面板：run_id / status / session_id / seq / release_id。
 */
import { Descriptions, Tag, Typography } from "antd";
import type { RunView } from "../api/types";
import { statusTag } from "../panels/runs/run-view-model";

const { Text } = Typography;

interface Props {
  detail: RunView;
}

export default function RunStatusHeader({ detail }: Props) {
  const meta = statusTag(detail.status);
  return (
    <div className="atlas-run-header">
      <div className="atlas-run-header__title">
        <Text className="atlas-mono atlas-run-header__id">{detail.run_id}</Text>
        <Tag color={meta.color}>{meta.label}</Tag>
      </div>
      <Descriptions size="small" column={4} className="atlas-run-header__desc">
        <Descriptions.Item label="session">
          <Text code className="atlas-mono">{detail.session_id}</Text>
        </Descriptions.Item>
        <Descriptions.Item label="last_seq">
          <Text className="atlas-mono">{detail.last_seq}</Text>
        </Descriptions.Item>
        <Descriptions.Item label="release">{detail.release_id ?? "—"}</Descriptions.Item>
        <Descriptions.Item label="availability">{detail.result_availability}</Descriptions.Item>
      </Descriptions>
    </div>
  );
}
```

- [ ] **Step 2: 添加 CSS**

```css
/* ── 运行状态仪表头（Run Observatory） ───────────────── */

.atlas-run-header {
  background: var(--atlas-surface);
  border: 1px solid var(--atlas-border);
  border-radius: 8px;
  padding: 16px 20px;
  margin-bottom: 20px;
}

.atlas-run-header__title {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 12px;
}

.atlas-run-header__id {
  font-size: 18px;
  font-weight: 600;
}
```

- [ ] **Step 3: 替换原 Descriptions**

在 `RunDetailSection` 中用 `<RunStatusHeader detail={detail} />` 替换原来的 `Descriptions` 块。

- [ ] **Step 4: 运行测试**

```bash
cd frontend
npm test -- run
```

- [ ] **Step 5: 提交**

```bash
git add frontend/src/components/RunStatusHeader.tsx frontend/src/panels/runs/RunPanel.tsx frontend/src/styles/global.css
git commit -m "feat(runs): add RunStatusHeader instrument panel"
```

---

## Task 4: 运行详情三区 Tab 化

**Files:**
- Modify: `frontend/src/panels/runs/RunPanel.tsx`

- [ ] **Step 1: 将详情改为 Tabs**

用 AntD `Tabs` 组织详情区：
- `result`：结果区
- `graph`：执行路径图 + 节点详情
- `timeline`：事件时间线
- `artifacts`：捕获正文

```tsx
import { Tabs } from "antd";

<Tabs
  items={[
    { key: "result", label: "结果", children: <ResultBlock detail={detail} /> },
    { key: "graph", label: "执行路径", children: <>
      <RunGraph ... />
      {selectedNode && <NodeInspector ... />}
    </> },
    { key: "timeline", label: "时间线", children: <RunTimeline ... /> },
    { key: "artifacts", label: "捕获正文", children: <ArtifactsSection ... /> },
  ]}
/>
```

- [ ] **Step 2: 运行测试**

```bash
cd frontend
npm test -- run
```

- [ ] **Step 3: 提交**

```bash
git add frontend/src/panels/runs/RunPanel.tsx
git commit -m "feat(runs): organize run detail into observatory tabs"
```

---

## Task 5: 增强执行路径图（RunGraph Observatory Styling）

**Files:**
- Modify: `frontend/src/panels/runs/RunGraph.tsx`
- Modify: `frontend/src/styles/global.css`

- [ ] **Step 1: 更新节点样式**

节点颜色映射：
- idle: `--atlas-muted`
- running: `--atlas-primary`（青色脉动）
- completed: `--atlas-success`
- failed: `--atlas-danger`

- [ ] **Step 2: 添加脉冲动效 CSS**

```css
@keyframes nodePulse {
  0%, 100% { box-shadow: 0 0 0 0 rgba(110, 231, 245, 0.4); }
  50% { box-shadow: 0 0 0 8px rgba(110, 231, 245, 0); }
}

.atlas-run-node--running {
  animation: nodePulse 1.5s ease-in-out infinite;
}
```

- [ ] **Step 3: 运行测试**

```bash
cd frontend
npm test -- run-graph
```

- [ ] **Step 4: 提交**

```bash
git add frontend/src/panels/runs/RunGraph.tsx frontend/src/styles/global.css
git commit -m "feat(runs): style RunGraph as observatory instrument"
```

---

## Task 6: 增强时间线（RunTimeline Observatory Styling）

**Files:**
- Modify: `frontend/src/panels/runs/RunTimeline.tsx`
- Modify: `frontend/src/styles/global.css`

- [ ] **Step 1: 更新时间线条目样式**

将时间线条目改为左侧竖线 + 圆点 + 右侧内容卡片。

- [ ] **Step 2: 运行测试**

```bash
cd frontend
npm test -- run-timeline
```

- [ ] **Step 3: 提交**

```bash
git add frontend/src/panels/runs/RunTimeline.tsx frontend/src/styles/global.css
git commit -m "feat(runs): style RunTimeline as observatory event log"
```

---

## Task 7: 最终验证与文档

- [ ] **Step 1: 运行全量测试**

```bash
cd frontend
npm test
```

Expected: 全部通过

- [ ] **Step 2: 运行 typecheck**

```bash
cd frontend
npm run typecheck
```

- [ ] **Step 3: 运行 build**

```bash
cd frontend
npm run build
```

- [ ] **Step 4: 更新设计文档**

在 `docs/design/frontend-redesign.md` 中标记 Phase 2a 完成。

- [ ] **Step 5: 提交**

```bash
git add docs/design/frontend-redesign.md
git commit -m "docs: mark Phase 2a Run Observatory complete"
```
