# Atlas 前端重设计实施计划 - Phase 1 & 问数仪表舱

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现"星图仪表舱"设计系统，重构问数仪表舱（溯源脊、快照封印、确定性徽标）

**Architecture:** 设计令牌（tokens.ts）+ CSS 自定义属性（global.css）+ 星图网格纹理 + 3 个核心组件（ProvenanceSpine、SnapshotSeal、DeterminismBadge）+ 问数仪表舱重构

**Tech Stack:** React 18, TypeScript 5.4, AntD 5, Recharts 2, CSS custom properties, JetBrains Mono (Google Fonts)

**Spec:** `docs/design/frontend-redesign.md`（设计方案）

## Global Constraints

- **诚实性约束**：保留所有 ADR 与测试断言（渲染次序、快照 SHA 常驻、sources 溯源顶置、错误原文渲染、只读边界）
- **字体规则**：所有数字、SHA、标识符、SQL 一律 JetBrains Mono（等宽）+ `font-variant-numeric: tabular-nums`
- **品牌色**：深空蓝 `#0c1220`、青色数据 `#6ee7f5`、琥珀色注意 `#f5b56e`
- **对比度**：满足 WCAG AA（4.5:1 正文，3:1 大文本）
- **动效**：prefers-reduced-motion 全局关闭动画
- **测试守线**：`honesty.test.ts`、`order.test.ts`、`no-persist.test.ts`、`governance-readonly.test.ts`、`sha.test.ts` 全部通过

---

## Task 1: 更新设计令牌（Design Tokens）

**Files:**
- Modify: `frontend/src/theme/tokens.ts`
- Test: `frontend/src/__tests__/tokens.test.ts`（新建）

**Interfaces:**
- Consumes: 无
- Produces: `BRAND`（深空 4 层色）、`BRAND_LIGHT`（浅色主题）、`FONT_UI`、`FONT_CODE`、`darkTheme`、`lightTheme`

- [ ] **Step 1: 写失败测试**

创建 `frontend/src/__tests__/tokens.test.ts`：

```typescript
import { BRAND, BRAND_LIGHT, FONT_UI, FONT_CODE } from "../theme/tokens";

describe("Design Tokens", () => {
  it("BRAND has 4 deep space layers", () => {
    expect(BRAND.bg).toBe("#0c1220");
    expect(BRAND.surface).toBe("#131d2e");
    expect(BRAND.elevated).toBe("#1a2538");
    expect(BRAND.codeBg).toBe("#0a0f1a");
  });

  it("BRAND has semantic colors", () => {
    expect(BRAND.primary).toBe("#6ee7f5"); // cyan - verified
    expect(BRAND.accent).toBe("#f5b56e"); // amber - attention
    expect(BRAND.success).toBe("#34d399");
    expect(BRAND.danger).toBe("#f87171");
    expect(BRAND.handoff).toBe("#a78bfa"); // violet - human takeover
  });

  it("FONT_UI includes Outfit and CJK fallbacks", () => {
    expect(FONT_UI).toContain("Outfit");
    expect(FONT_UI).toContain("PingFang SC");
  });

  it("FONT_CODE is JetBrains Mono", () => {
    expect(FONT_CODE).toContain("JetBrains Mono");
  });
});
```

- [ ] **Step 2: 运行测试验证失败**

```bash
cd frontend
npm test -- tokens.test.ts
```

Expected: FAIL - `BRAND.elevated` undefined, `BRAND.handoff` undefined

- [ ] **Step 3: 更新 tokens.ts**

修改 `frontend/src/theme/tokens.ts`：

```typescript
// ── 品牌色 ──────────────────────────────────────────────

/** 深色主题品牌色（深空 4 层 + 语义色）。 */
export const BRAND = {
  bg: "#0c1220",
  surface: "#131d2e",
  elevated: "#1a2538", // 新增：抬起层（悬浮/抽屉）
  border: "#1e2d42",
  primary: "#6ee7f5", // 青色 - 数据/已验证
  accent: "#f5b56e", // 琥珀色 - 注意/人工
  text: "#e2e8f0",
  muted: "#8b98a9",
  success: "#34d399",
  danger: "#f87171",
  handoff: "#a78bfa", // 新增：紫罗兰 - 转人工
  codeBg: "#0a0f1a",
} as const;

/** 浅色主题品牌色（对比度满足 WCAG AA）。 */
export const BRAND_LIGHT = {
  bg: "#f8fafc",
  surface: "#ffffff",
  elevated: "#f1f5f9",
  border: "#e2e8f0",
  primary: "#0891b2",
  accent: "#d97706",
  text: "#1e293b",
  muted: "#64748b",
  success: "#059669",
  danger: "#dc2626",
  handoff: "#7c3aed",
  codeBg: "#f1f5f9",
} as const;

// ── 字体栈 ──────────────────────────────────────────────

export const FONT_UI =
  '"Outfit", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", "Noto Sans CJK SC", system-ui, sans-serif';
export const FONT_CODE =
  '"JetBrains Mono", ui-monospace, "SF Mono", Menlo, Consolas, monospace';

/** 内容最大宽度。 */
export const CONTENT_MAX = "1200px";

// ── 深色主题 ────────────────────────────────────────────

export const darkTheme: ThemeConfig = {
  algorithm: theme.darkAlgorithm,
  token: {
    colorPrimary: BRAND.primary,
    colorBgContainer: BRAND.surface,
    colorBgLayout: BRAND.bg,
    colorBorder: BRAND.border,
    colorText: BRAND.text,
    colorTextSecondary: BRAND.muted,
    colorSuccess: BRAND.success,
    colorError: BRAND.danger,
    colorWarning: BRAND.accent,
    borderRadius: 6,
    fontFamily: FONT_UI,
    fontFamilyCode: FONT_CODE,
  },
  components: {
    Layout: {
      headerBg: BRAND.surface,
      bodyBg: BRAND.bg,
      headerColor: BRAND.text,
    },
    Menu: {
      darkItemBg: "transparent",
      darkItemColor: BRAND.text,
      darkItemSelectedColor: BRAND.primary,
      darkItemHoverColor: BRAND.primary,
      darkItemSelectedBg: "rgba(110, 231, 245, 0.08)",
    },
    Button: {
      primaryColor: BRAND.bg,
    },
  },
};

// ── 浅色主题 ────────────────────────────────────────────

export const lightTheme: ThemeConfig = {
  algorithm: theme.defaultAlgorithm,
  token: {
    colorPrimary: BRAND_LIGHT.primary,
    colorBgContainer: BRAND_LIGHT.surface,
    colorBgLayout: BRAND_LIGHT.bg,
    colorBorder: BRAND_LIGHT.border,
    colorText: BRAND_LIGHT.text,
    colorTextSecondary: BRAND_LIGHT.muted,
    colorSuccess: BRAND_LIGHT.success,
    colorError: BRAND_LIGHT.danger,
    colorWarning: BRAND_LIGHT.accent,
    borderRadius: 6,
    fontFamily: FONT_UI,
    fontFamilyCode: FONT_CODE,
  },
  components: {
    Layout: {
      headerBg: BRAND_LIGHT.surface,
      bodyBg: BRAND_LIGHT.bg,
      headerColor: BRAND_LIGHT.text,
    },
    Menu: {
      itemBg: "transparent",
      itemColor: BRAND_LIGHT.text,
      itemSelectedColor: BRAND_LIGHT.primary,
      itemHoverColor: BRAND_LIGHT.primary,
      itemSelectedBg: "rgba(8, 145, 178, 0.08)",
    },
    Button: {
      primaryColor: "#ffffff",
    },
  },
};
```

- [ ] **Step 4: 运行测试验证通过**

```bash
cd frontend
npm test -- tokens.test.ts
```

Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add frontend/src/theme/tokens.ts frontend/src/__tests__/tokens.test.ts
git commit -m "feat(theme): update design tokens with deep space layers and semantic colors"
```

---

## Task 2: 更新全局 CSS（星图网格 + 等宽规则）

**Files:**
- Modify: `frontend/src/styles/global.css`
- Test: 无（视觉验证：浏览器 DevTools 检查 CSS 变量）

**Interfaces:**
- Consumes: `tokens.ts` 的 `BRAND` / `BRAND_LIGHT`
- Produces: CSS 自定义属性 `--atlas-*`、`.atlas-mono` 类、星图网格背景

- [ ] **Step 1: 更新 CSS 变量**

修改 `frontend/src/styles/global.css`：

```css
/* ── 深色主题（默认） ─────────────────────────────────── */

:root,
[data-theme="dark"] {
  --atlas-bg: #0c1220;
  --atlas-surface: #131d2e;
  --atlas-elevated: #1a2538; /* 新增 */
  --atlas-border: #1e2d42;
  --atlas-primary: #6ee7f5;
  --atlas-accent: #f5b56e;
  --atlas-text: #e2e8f0;
  --atlas-muted: #8b98a9;
  --atlas-success: #34d399;
  --atlas-danger: #f87171;
  --atlas-handoff: #a78bfa; /* 新增 */
  --atlas-code-bg: #0a0f1a;
  --atlas-content-max: 1200px;
}

/* ── 浅色主题 ──────────────────────────────────────────── */

[data-theme="light"] {
  --atlas-bg: #f8fafc;
  --atlas-surface: #ffffff;
  --atlas-elevated: #f1f5f9;
  --atlas-border: #e2e8f0;
  --atlas-primary: #0891b2;
  --atlas-accent: #d97706;
  --atlas-text: #1e293b;
  --atlas-muted: #64748b;
  --atlas-success: #059669;
  --atlas-danger: #dc2626;
  --atlas-handoff: #7c3aed;
  --atlas-code-bg: #f1f5f9;
}
```

- [ ] **Step 2: 添加等宽字体规则**

在 `global.css` 中添加：

```css
/* ── 等宽字体强制（凡数字皆等宽） ─────────────────────── */

.atlas-mono,
.atlas-sha,
.atlas-sql,
.atlas-number {
  font-family: "JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, monospace;
  font-variant-numeric: tabular-nums;
}

/* 表格数字列 */
.atlas-table-number {
  font-family: "JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, monospace;
  font-variant-numeric: tabular-nums;
  text-align: right;
}
```

- [ ] **Step 3: 添加星图网格背景**

在 `global.css` 中添加：

```css
/* ── 星图网格背景（深空蓝底 + 细微网格） ──────────────── */

body {
  background:
    linear-gradient(rgba(110, 231, 245, 0.02) 1px, transparent 1px),
    linear-gradient(90deg, rgba(110, 231, 245, 0.02) 1px, transparent 1px),
    var(--atlas-bg);
  background-size: 40px 40px, 40px 40px, 100% 100%;
  color: var(--atlas-text);
  font-family: "Outfit", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei",
    "Noto Sans CJK SC", system-ui, sans-serif;
  transition: background 200ms, color 200ms;
}

[data-theme="light"] body {
  background: var(--atlas-bg); /* 浅色主题不用网格 */
}
```

- [ ] **Step 4: 浏览器验证**

```bash
cd frontend
npm run dev
```

打开浏览器，检查：
- 深色主题：深空蓝底 + 细微青色网格
- 浅色主题：纯色背景（无网格）
- `.atlas-mono` 类的元素显示等宽字体

- [ ] **Step 5: 提交**

```bash
git add frontend/src/styles/global.css
git commit -m "feat(theme): add star chart grid texture and monospace rules"
```

---

## Task 3: 创建溯源脊组件（ProvenanceSpine）

**Files:**
- Create: `frontend/src/components/ProvenanceSpine.tsx`
- Test: `frontend/src/__tests__/provenance-spine.test.tsx`

**Interfaces:**
- Consumes: `children`（答案区内容）
- Produces: 左侧垂直脊（2px 青色线）+ 节点（圆点，8px）+ staggered reveal 动效

- [ ] **Step 1: 写失败测试**

创建 `frontend/src/__tests__/provenance-spine.test.tsx`：

```typescript
import { render, screen } from "@testing-library/react";
import ProvenanceSpine from "../components/ProvenanceSpine";

describe("ProvenanceSpine", () => {
  it("renders children with spine wrapper", () => {
    render(
      <ProvenanceSpine>
        <div data-testid="child">Test Content</div>
      </ProvenanceSpine>
    );
    expect(screen.getByTestId("child")).toBeInTheDocument();
  });

  it("has spine container with correct class", () => {
    const { container } = render(
      <ProvenanceSpine>
        <div>Content</div>
      </ProvenanceSpine>
    );
    expect(container.querySelector(".atlas-spine")).toBeInTheDocument();
  });

  it("has spine line element", () => {
    const { container } = render(
      <ProvenanceSpine>
        <div>Content</div>
      </ProvenanceSpine>
    );
    expect(container.querySelector(".atlas-spine__line")).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: 运行测试验证失败**

```bash
cd frontend
npm test -- provenance-spine.test.tsx
```

Expected: FAIL - module not found

- [ ] **Step 3: 实现组件**

创建 `frontend/src/components/ProvenanceSpine.tsx`：

```typescript
/**
 * 溯源脊（Provenance Spine）——答案区左侧垂直链，显示处理步骤。
 *
 * 视觉：2px 宽青色垂直线 + 节点圆点（8px）。节点依次出现（staggered reveal）。
 * 这是 Atlas 区别于 ChatBI 的独特签名——把"信任/可追溯"变成视觉元素。
 */
import type { ReactNode } from "react";

interface Props {
  children: ReactNode;
}

export default function ProvenanceSpine({ children }: Props) {
  return (
    <div className="atlas-spine">
      <div className="atlas-spine__line" aria-hidden="true" />
      <div className="atlas-spine__content">{children}</div>
    </div>
  );
}
```

- [ ] **Step 4: 添加 CSS**

在 `frontend/src/styles/global.css` 中添加：

```css
/* ── 溯源脊（Provenance Spine） ─────────────────────── */

.atlas-spine {
  position: relative;
  display: flex;
  gap: 16px;
}

.atlas-spine__line {
  position: absolute;
  left: 4px;
  top: 0;
  bottom: 0;
  width: 2px;
  background: var(--atlas-primary);
  opacity: 0.3;
}

.atlas-spine__content {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 20px;
}

/* 节点圆点（由子组件添加 .atlas-spine__node） */
.atlas-spine__node {
  position: relative;
  padding-left: 24px;
}

.atlas-spine__node::before {
  content: "";
  position: absolute;
  left: 0;
  top: 8px;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--atlas-primary);
  box-shadow: 0 0 0 3px var(--atlas-bg);
}

/* Staggered reveal 动效 */
.atlas-spine__node {
  animation: fadeSlideIn 300ms ease-out both;
}

.atlas-spine__node:nth-child(1) { animation-delay: 0ms; }
.atlas-spine__node:nth-child(2) { animation-delay: 50ms; }
.atlas-spine__node:nth-child(3) { animation-delay: 100ms; }
.atlas-spine__node:nth-child(4) { animation-delay: 150ms; }
.atlas-spine__node:nth-child(5) { animation-delay: 200ms; }
.atlas-spine__node:nth-child(6) { animation-delay: 250ms; }
.atlas-spine__node:nth-child(7) { animation-delay: 300ms; }
```

- [ ] **Step 5: 运行测试验证通过**

```bash
cd frontend
npm test -- provenance-spine.test.tsx
```

Expected: PASS

- [ ] **Step 6: 提交**

```bash
git add frontend/src/components/ProvenanceSpine.tsx frontend/src/__tests__/provenance-spine.test.tsx frontend/src/styles/global.css
git commit -m "feat(components): add ProvenanceSpine with staggered reveal animation"
```

---

## Task 4: 创建快照封印组件（SnapshotSeal）

**Files:**
- Create: `frontend/src/components/SnapshotSeal.tsx`
- Test: `frontend/src/__tests__/snapshot-seal.test.tsx`

**Interfaces:**
- Consumes: `snapshotSha: string | null`、`boundToHead: boolean`
- Produces: 印章样式的徽标（圆角矩形，边框 2px，显示 `snapshot: abc1234`）

- [ ] **Step 1: 写失败测试**

创建 `frontend/src/__tests__/snapshot-seal.test.tsx`：

```typescript
import { render, screen } from "@testing-library/react";
import SnapshotSeal from "../components/SnapshotSeal";

describe("SnapshotSeal", () => {
  it("renders snapshot SHA in mono font", () => {
    render(<SnapshotSeal snapshotSha="abc1234def5678" boundToHead={true} />);
    const seal = screen.getByText(/abc1234/);
    expect(seal).toBeInTheDocument();
    expect(seal).toHaveClass("atlas-mono");
  });

  it("shows 'unbound' when SHA is null", () => {
    render(<SnapshotSeal snapshotSha={null} boundToHead={false} />);
    expect(screen.getByText(/未绑定/)).toBeInTheDocument();
  });

  it("has seal visual class", () => {
    const { container } = render(<SnapshotSeal snapshotSha="abc1234" boundToHead={true} />);
    expect(container.querySelector(".atlas-seal")).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: 运行测试验证失败**

```bash
cd frontend
npm test -- snapshot-seal.test.tsx
```

Expected: FAIL - module not found

- [ ] **Step 3: 实现组件**

创建 `frontend/src/components/SnapshotSeal.tsx`：

```typescript
/**
 * 快照封印（Snapshot Seal）——答案区右上角的"印章"样式徽标。
 *
 * 视觉：圆角矩形，边框 2px，内部显示 `snapshot: abc1234`。
 * 中文语境里"盖章"= 权威背书。未绑定时显示灰色"未绑定"。
 */
import { Tag } from "antd";
import { shortSha } from "../lib/sha";

interface Props {
  snapshotSha: string | null;
  boundToHead: boolean;
}

export default function SnapshotSeal({ snapshotSha, boundToHead }: Props) {
  if (snapshotSha === null) {
    return (
      <Tag className="atlas-seal atlas-seal--unbound" color="default">
        <span className="atlas-seal__label">snapshot</span>
        <span className="atlas-seal__value atlas-mono">未绑定</span>
      </Tag>
    );
  }

  const color = boundToHead ? "cyan" : "blue";
  return (
    <Tag className="atlas-seal" color={color}>
      <span className="atlas-seal__label">snapshot</span>
      <span className="atlas-seal__value atlas-mono">{shortSha(snapshotSha)}</span>
    </Tag>
  );
}
```

- [ ] **Step 4: 添加 CSS**

在 `frontend/src/styles/global.css` 中添加：

```css
/* ── 快照封印（Snapshot Seal） ─────────────────────── */

.atlas-seal {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 4px 10px;
  border: 2px solid currentColor;
  border-radius: 4px;
  font-size: 12px;
  font-weight: 600;
  letter-spacing: 0.02em;
}

.atlas-seal__label {
  text-transform: uppercase;
  opacity: 0.7;
}

.atlas-seal__value {
  font-family: "JetBrains Mono", ui-monospace, monospace;
  font-variant-numeric: tabular-nums;
}

.atlas-seal--unbound {
  opacity: 0.5;
}
```

- [ ] **Step 5: 运行测试验证通过**

```bash
cd frontend
npm test -- snapshot-seal.test.tsx
```

Expected: PASS

- [ ] **Step 6: 提交**

```bash
git add frontend/src/components/SnapshotSeal.tsx frontend/src/__tests__/snapshot-seal.test.tsx frontend/src/styles/global.css
git commit -m "feat(components): add SnapshotSeal stamp visual"
```

---

## Task 5: 创建确定性徽标组件（DeterminismBadge）

**Files:**
- Create: `frontend/src/components/DeterminismBadge.tsx`
- Test: `frontend/src/__tests__/determinism-badge.test.tsx`

**Interfaces:**
- Consumes: `path: "compiler" | "llm"`
- Produces: 青色"已编译"或琥珀色"已生成"徽标

- [ ] **Step 1: 写失败测试**

创建 `frontend/src/__tests__/determinism-badge.test.tsx`：

```typescript
import { render, screen } from "@testing-library/react";
import DeterminismBadge from "../components/DeterminismBadge";

describe("DeterminismBadge", () => {
  it("renders 'compiled' in cyan for compiler path", () => {
    render(<DeterminismBadge path="compiler" />);
    const badge = screen.getByText(/已编译/);
    expect(badge).toBeInTheDocument();
  });

  it("renders 'generated' in amber for llm path", () => {
    render(<DeterminismBadge path="llm" />);
    const badge = screen.getByText(/已生成/);
    expect(badge).toBeInTheDocument();
  });

  it("has correct color class", () => {
    const { container } = render(<DeterminismBadge path="compiler" />);
    expect(container.querySelector(".atlas-badge--compiler")).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: 运行测试验证失败**

```bash
cd frontend
npm test -- determinism-badge.test.tsx
```

Expected: FAIL - module not found

- [ ] **Step 3: 实现组件**

创建 `frontend/src/components/DeterminismBadge.tsx`：

```typescript
/**
 * 确定性徽标（Determinism Badge）——编译器路径（青色）vs LLM 路径（琥珀色）。
 *
 * 编译器是"已验证"路径，LLM 是"候选"路径。视觉区分帮助用户理解答案的可信度。
 */
import { Tag } from "antd";

interface Props {
  path: "compiler" | "llm";
}

export default function DeterminismBadge({ path }: Props) {
  if (path === "compiler") {
    return (
      <Tag className="atlas-badge atlas-badge--compiler" color="cyan">
        已编译
      </Tag>
    );
  }
  return (
    <Tag className="atlas-badge atlas-badge--llm" color="orange">
      已生成
    </Tag>
  );
}
```

- [ ] **Step 4: 添加 CSS**

在 `frontend/src/styles/global.css` 中添加：

```css
/* ── 确定性徽标（Determinism Badge） ────────────────── */

.atlas-badge {
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.04em;
  text-transform: uppercase;
}

.atlas-badge--compiler {
  border-color: var(--atlas-primary);
  color: var(--atlas-primary);
}

.atlas-badge--llm {
  border-color: var(--atlas-accent);
  color: var(--atlas-accent);
}
```

- [ ] **Step 5: 运行测试验证通过**

```bash
cd frontend
npm test -- determinism-badge.test.tsx
```

Expected: PASS

- [ ] **Step 6: 提交**

```bash
git add frontend/src/components/DeterminismBadge.tsx frontend/src/__tests__/determinism-badge.test.tsx frontend/src/styles/global.css
git commit -m "feat(components): add DeterminismBadge for compiler vs LLM path"
```

---

## Task 6: 重构问数仪表舱布局（AskWorkbench）

**Files:**
- Modify: `frontend/src/panels/workbench/AskWorkbench.tsx`
- Test: 现有测试 `frontend/src/__tests__/analysis-block.test.tsx` 等必须通过

**Interfaces:**
- Consumes: `ProvenanceSpine`、`SnapshotSeal`、`DeterminismBadge`
- Produces: 重构后的工作台布局（溯源脊包裹答案区、快照封印右上角、确定性徽标 SQL 区）

- [ ] **Step 1: 运行现有测试建立基线**

```bash
cd frontend
npm test
```

Expected: 所有测试通过（记录当前状态）

- [ ] **Step 2: 导入新组件**

在 `AskWorkbench.tsx` 顶部添加：

```typescript
import ProvenanceSpine from "../../components/ProvenanceSpine";
import SnapshotSeal from "../../components/SnapshotSeal";
import DeterminismBadge from "../../components/DeterminismBadge";
```

- [ ] **Step 3: 用 ProvenanceSpine 包裹答案区**

找到 `renderAnswer` 函数的 return 语句，用 `ProvenanceSpine` 包裹：

```typescript
function renderAnswer(p: TurnPayload): ReactNode {
  const t = truncationState({ rowCount: p.row_count, sql: p.sql });
  const sections: Record<AnswerSection, ReactNode> = {
    metric: <ExplanationBlock metric={p.metric} explanation={p.explanation} />,
    counts: (
      <Space direction="vertical" size={0}>
        <Typography.Text>
          指标={p.metric ?? "（无）"} | 行数={p.row_count} | 执行 {p.latency_ms}ms
        </Typography.Text>
        <Typography.Text type="secondary">
          会话 {p.session_id.slice(0, 8)} · 第 {p.turns_in_session} 轮
          {p.snapshot_sha !== null
            ? ` · 本轮绑定快照 ${shortSha(p.snapshot_sha)}（bound_to_head=${String(
                p.snapshot_bound_to_head,
              )}）`
            : " · 本轮未回显快照（snapshot_sha 为 null）"}
        </Typography.Text>
      </Space>
    ),
    sql: (
      <div style={{ position: "relative" }}>
        <SqlPreview sql={p.sql} title="出口 SQL" />
        <div style={{ position: "absolute", top: 8, right: 8 }}>
          <DeterminismBadge path="compiler" />
        </div>
      </div>
    ),
    data: <DataTable columns={p.columns} rows={p.rows} renderedRows={t.renderedRows} />,
    truncation: <TruncationNote state={t} />,
    chart: <ChartBlock spec={p.chart} />,
  };
  return (
    <ProvenanceSpine>
      <Space direction="vertical" size="middle" style={{ width: "100%" }}>
        {ANSWER_SECTIONS.map((section) => (
          <div key={section} className="atlas-spine__node">
            {sections[section]}
          </div>
        ))}
        <div className="atlas-spine__node">
          <NarrativeBlock narrative={p.narrative} />
        </div>
      </Space>
      <div style={{ position: "absolute", top: 0, right: 0 }}>
        <SnapshotSeal
          snapshotSha={p.snapshot_sha}
          boundToHead={p.snapshot_bound_to_head}
        />
      </div>
    </ProvenanceSpine>
  );
}
```

- [ ] **Step 4: 运行测试验证通过**

```bash
cd frontend
npm test
```

Expected: 所有测试通过（诚实性约束保留）

- [ ] **Step 5: 浏览器视觉验证**

```bash
cd frontend
npm run dev
```

打开工作台，提问一个问题，检查：
- 答案区左侧有青色溯源脊
- 每个步骤是一个节点（圆点），依次出现
- 右上角有快照封印（显示 SHA 或"未绑定"）
- SQL 区右上角有"已编译"青色徽标

- [ ] **Step 6: 提交**

```bash
git add frontend/src/panels/workbench/AskWorkbench.tsx
git commit -m "feat(workbench): redesign with ProvenanceSpine, SnapshotSeal, DeterminismBadge"
```

---

## Task 7: 更新 SnapshotBadge（顶栏常驻）

**Files:**
- Modify: `frontend/src/components/SnapshotBadge.tsx`
- Test: 现有测试必须通过

**Interfaces:**
- Consumes: `HealthPayload`
- Produces: 增强的顶栏徽标（保留原有 4 态，增加封印视觉）

- [ ] **Step 1: 运行现有测试**

```bash
cd frontend
npm test -- sha.test.ts
```

Expected: 通过

- [ ] **Step 2: 增强视觉**

修改 `SnapshotBadge.tsx`，添加封印样式类：

```typescript
export default function SnapshotBadge({ health, error }: Props) {
  if (error !== null) {
    return (
      <Tag className="atlas-seal" color="red" title={error}>
        <span className="atlas-seal__label">snapshot</span>
        <span className="atlas-seal__value atlas-mono">错误：{error}</span>
      </Tag>
    );
  }
  if (health === null) {
    return (
      <Tag className="atlas-seal" aria-live="polite">
        <span className="atlas-seal__label">snapshot</span>
        <span className="atlas-seal__value atlas-mono">加载中…</span>
      </Tag>
    );
  }
  const badge = snapshotBadge(health);
  return (
    <Tooltip
      title={`head_sha=${health.head_sha ?? "null"} · snapshot_tables=${
        health.snapshot_tables ?? "null"
      } · boot_id=${health.boot_id}`}
    >
      <Tag className="atlas-seal" color={SEVERITY_COLOR[badge.severity]}>
        <span className="atlas-seal__label">snapshot</span>
        <span className="atlas-seal__value atlas-mono">{badge.text}</span>
      </Tag>
    </Tooltip>
  );
}
```

- [ ] **Step 3: 运行测试验证通过**

```bash
cd frontend
npm test -- sha.test.ts
```

Expected: 通过

- [ ] **Step 4: 提交**

```bash
git add frontend/src/components/SnapshotBadge.tsx
git commit -m "feat(components): enhance SnapshotBadge with seal visual"
```

---

## Task 8: 最终验证与文档

**Files:**
- Modify: `docs/design/frontend-redesign.md`（标记 Phase 1 完成）

- [ ] **Step 1: 运行全量测试**

```bash
cd frontend
npm test
```

Expected: 所有测试通过

- [ ] **Step 2: 运行 lint**

```bash
cd frontend
npm run typecheck
```

Expected: 无错误

- [ ] **Step 3: 构建验证**

```bash
cd frontend
npm run build
```

Expected: 构建成功

- [ ] **Step 4: 浏览器全面验证**

```bash
cd frontend
npm run dev
```

检查：
- 深色/浅色主题切换正常
- 星图网格背景（深色）/ 纯色背景（浅色）
- 问数仪表舱：溯源脊、快照封印、确定性徽标
- 顶栏 SnapshotBadge 封印样式
- 所有数字、SHA、SQL 等宽字体
- 诚实性约束保留（渲染次序、错误原文、只读边界）

- [ ] **Step 5: 更新设计文档**

在 `docs/design/frontend-redesign.md` 的"实施建议"章节添加：

```markdown
**Phase 1 完成**（2026-10-14）：
- ✅ 设计令牌（深空 4 层 + 语义色）
- ✅ 全局 CSS（星图网格 + 等宽规则）
- ✅ 溯源脊组件
- ✅ 快照封印组件
- ✅ 确定性徽标组件
- ✅ 问数仪表舱重构
- ✅ SnapshotBadge 增强

**下一步**：
- Phase 2a: 运行观测台（Run Observatory）
- Phase 2b: 治理星图（Governance Star Chart）
- Phase 2c: 数据接入港（Data Harbor）
- Phase 3: 优化引擎舱（Optimization Engine Bay）
```

- [ ] **Step 6: 提交**

```bash
git add docs/design/frontend-redesign.md
git commit -m "docs: mark Phase 1 complete and outline next phases"
```

---

## 后续计划建议

本计划覆盖 **Phase 1 + 问数仪表舱**。建议创建独立计划：

1. **`frontend-redesign-run-observatory.md`** - 运行观测台（运行时间线、执行路径图、节点详情）
2. **`frontend-redesign-governance-star-chart.md`** - 治理星图（8 子页导航、来源路径列表）
3. **`frontend-redesign-data-harbor.md`** - 数据接入港（向导、草稿、发布流水线）
4. **`frontend-redesign-optimization-engine.md`** - 优化引擎舱（反馈队列、训练任务、节点实验）

每个计划遵循相同的结构：设计令牌 → 组件 → 布局 → 测试 → 验证。
