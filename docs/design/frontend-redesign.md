# Atlas 前端重设计方案

## 一、设计理念：星图仪表舱（Observatory Instrument Panel）

**核心隐喻**：Atlas 是希腊神话中托举天空的泰坦，也是"地图册"——天文台与星图的意象完美契合项目的 DNA（精确、可溯源、诚实）。

**设计哲学**：
1. **溯源脊（Provenance Spine）**：每条答案左侧有一条垂直的"溯源脊"，显示从问句 → Plan → SQL → 数据 → 图表的处理链。这是 Atlas 区别于所有 ChatBI 产品的独特签名——把"信任/可追溯"变成视觉元素。
2. **快照封印（Snapshot Seal）**：每个答案盖上快照 SHA 的"印章"，中文语境里"盖章"= 权威背书。
3. **凡数字皆等宽**：所有数字、SHA、标识符、SQL 一律等宽字体（JetBrains Mono）。数字是测量值，不是散文——这是"仪器读数"的视觉语言。
4. **确定性轨迹**：编译器路径（青色）vs LLM 路径（琥珀色）视觉区分。编译器是"已验证"路径，LLM 是"候选"路径。
5. **星图网格**：深空蓝底 + 细微网格纹理，像星图/经纬线。不是纯色而是有层次的深空。

---

## 二、视觉系统

### 2.1 色彩系统

**深空层次**（4 层，从深到浅）：
- `--atlas-bg`: `#0c1220`（最深，页面背景）
- `--atlas-surface`: `#131d2e`（表面，卡片/面板）
- `--atlas-elevated`: `#1a2538`（抬起，悬浮层/抽屉）
- `--atlas-code-bg`: `#0a0f1a`（代码背景）

**语义色**（6 种状态，对齐诚实性标志位）：
- **数据/已验证**（编译器、Guard 通过）：`#6ee7f5`（青色）
- **注意/人工**（LLM 候选、澄清、handoff）：`#f5b56e`（琥珀色）
- **成功**（查询成功、部署正常）：`#34d399`（绿色）
- **阻断**（Guard 拒绝、错误）：`#f87171`（红色）
- **交接**（转人工）：`#a78bfa`（紫罗兰）
- **未知**（契约外值、fallback）：`#8b98a9`（灰色）

**浅色主题**：保持 `BRAND_LIGHT` 色值，对比度满足 WCAG AA。

### 2.2 字体系统

**UI 字体**：Outfit（拉丁）+ PingFang SC / Noto Sans SC（中文）
- 标题：Outfit 600，20px
- Section 标题：Outfit 600，15px
- 正文：Outfit 400，14px
- 说明文字：Outfit 400，13-12px，次级色

**等宽字体**：JetBrains Mono（所有数字、SHA、标识符、SQL、代码）
- **强制规则**：表格中的数字列、所有 SHA、所有标识符、所有 SQL、所有 JSON 一律 `font-family: "JetBrains Mono"` + `font-variant-numeric: tabular-nums`

### 2.3 间距系统

4px 基础单位：8px / 12px / 16px / 20px / 24px / 32px / 48px

### 2.4 动效语言

- **渐入**：答案区 fade-slide-in（300ms ease-out）
- **溯源脊节点**：staggered reveal（每个节点延迟 50ms 依次出现）
- **活跃节点**：pulse（运行中的节点轻微脉动）
- **禁止**：bounce、spring——这是精密仪器，不是玩具
- **prefers-reduced-motion**：全局关闭动画

### 2.5 背景纹理

- **星图网格**：深空蓝底上叠加细微网格（2% 不透明度，1px 线宽，间距 40px）
- **浅色主题**：纯色背景（无网格）

---

## 三、模块划分

### 3.1 五大模块

| # | 模块 | 路由 | 隐喻 | 核心功能 |
|---|---|---|---|---|
| 1 | **问数仪表舱** | `/ask` | 驾驶舱 | 自然语言提问 → Plan → SQL → 数据 → 图表，带溯源脊 |
| 2 | **运行观测台** | `/runs` | 天文台 | 运行历史、执行路径图、节点详情、反馈 |
| 3 | **数据接入港** | `/setup` | 港口 | 数据源接入、语义草稿、发布流水线 |
| 4 | **治理星图** | `/governance` | 星图 | 8 子页：语义模型/指标/维度/同义词/值域 + 权限策略/评测报告/数据快照 |
| 5 | **优化引擎舱** | （未来路由；面板内部重设计已完成） | 引擎舱 | 反馈队列、训练任务、节点实验 |

### 3.2 全局组件

- **顶栏**：标题、快照封印（SnapshotBadge）、导航、主题切换、身份入口（SessionEntry）
- **溯源脊**：答案区左侧垂直链，显示处理步骤
- **快照封印**：答案区右上角，显示绑定的快照 SHA
- **确定性徽标**：编译器路径显示青色"已编译"，LLM 路径显示琥珀色"已生成"

---

## 四、诚实性设计约束（必须保留）

1. **渲染次序锁定**：答案区 7 个步骤的顺序由 `lib/order.ts` 的 `ANSWER_SECTIONS` 驱动。
2. **快照 SHA 常驻展示**：顶栏 `SnapshotBadge` + 答案区"快照封印"。
3. **sources 溯源顶置**：治理子页的 `sources` 字段必须显示在页头。
4. **错误原文如实渲染**：零遥测口径，不吞错。
5. **未知 kind 展示原始 payload**：不猜分支。
6. **只读边界**：治理面一律只读。
7. **角色切换轮换 session_id**：切身份即换会话。
8. **凡数字皆等宽**：所有数字、SHA、标识符、SQL 一律等宽字体。

---

## 五、实施状态

### Phase 1 完成（2026-10-14）

- ✅ 设计令牌（深空 4 层 + 语义色）
- ✅ 全局 CSS（星图网格 + 等宽规则）
- ✅ 溯源脊组件（`frontend/src/components/ProvenanceSpine.tsx`）
- ✅ 快照封印组件（`frontend/src/components/SnapshotSeal.tsx`）
- ✅ 确定性徽标组件（`frontend/src/components/DeterminismBadge.tsx`）
- ✅ 问数仪表舱重构（`frontend/src/panels/workbench/AskWorkbench.tsx`）
- ✅ 顶栏 SnapshotBadge 增强

### Phase 2a 完成（2026-10-14）

运行观测台（`/runs`）：

- ✅ 运行目录卡片化（状态圆点 + 等宽 run_id + 删除按钮；`RunCatalogCard`）
- ✅ 左右分栏布局（320px 目录 sidebar + 详情 main；小屏降级单列）
- ✅ 状态仪表头（`RunStatusHeader`：run_id + 状态 + result_kind + 仪表读数行）
- ✅ 运行详情 Tab 化（结果 / 执行路径 / 时间线 / 捕获正文）
- ✅ 执行路径图仪器化（CSS 类节点 + 运行中脉冲 + taken 边发光 + 图内星图网格）
- ✅ 事件时间线改为事件日志（状态圆点 + seq 徽章 + 事件类型 Tag + 服务端时刻）

### Phase 2b 完成（2026-10-14）

治理星图（`/governance`）：

- ✅ 星体徽章（`StarBadge`：6 种色调 + 未知态，契约原文展示不翻译）
- ✅ 治理页头星图控制台（build/control 双区 + 星体徽章区标题）
- ✅ 8 子页表格星体化（状态列 → StarBadge；含 FIBO 对齐、注册态、bound 等）
- ✅ sources 展开区星图视觉（星体节点 + 连接线）+ 条数等宽

### Phase 2c 完成（2026-10-14）

数据接入港（`/setup`）：

- ✅ 航道导航（`HarborLane`：6 泊位节点 + 连接线，替换 AntD Steps，保留点击跳步）
- ✅ 泊位卡头（泊位号 chip 01–06 + 仪表读数：已连接源 / 已绑定部署）
- ✅ 泊位表格星体化（`ProbeBadge` ok/blocked、`ActiveReleaseBadge` bound/unbound）
- ✅ 草稿流水星体化（StarBadge 新增 verified 青色调；删除 STATUS_LABELS，契约原文展示）

### Phase 3 完成（2026-10-14）

优化引擎舱（`panels/optimization/` 三面板内部重设计；路由接入仍属未来工作）：

- ✅ 反馈队列星体化（verdict / status → StarBadge；StarBadge 新增引擎舱映射 up/down/approved/completed/corrected/pending_review）
- ✅ 训练机组仪表化（D11 配方数值等宽；任务状态列 → StarBadge；容器 `.atlas-page`）
- ✅ 节点实验台仪器化（导出 `ResultCard`：状态 → StarBadge；实验 ID / 节点 / release SHA 短写 → 等宽 `Text code`）

### 阶段性状态

五大模块的界面内部重设计全部完成（前端 294 测试通过）。模块 5（优化引擎舱）
的路由接入仍属未来工作——三面板目前保持未路由状态（同上方模块 5「未来路由」标注）。

---

## 六、相关文件

- `frontend/src/theme/tokens.ts` — 设计令牌
- `frontend/src/styles/global.css` — 全局样式与纹理
- `frontend/src/components/ProvenanceSpine.tsx` — 溯源脊
- `frontend/src/components/SnapshotSeal.tsx` — 快照封印
- `frontend/src/components/DeterminismBadge.tsx` — 确定性徽标
- `frontend/src/components/SnapshotBadge.tsx` — 顶栏快照封印
- `frontend/src/panels/workbench/AskWorkbench.tsx` — 问数仪表舱
- `frontend/src/components/RunStatusHeader.tsx` — 运行状态仪表头
- `frontend/src/panels/runs/RunPanel.tsx` — 运行观测台（左右分栏 + Tab 化）
- `frontend/src/panels/runs/RunGraph.tsx` — 执行路径图（仪器节点）
- `frontend/src/panels/runs/RunTimeline.tsx` — 事件日志时间线
- `docs/design/frontend-redesign-plan.md` — Phase 1 实施计划
- `docs/design/frontend-redesign-run-observatory-plan.md` — Phase 2a 实施计划
- `docs/design/frontend-redesign-governance-star-chart-plan.md` — Phase 2b 实施计划
- `frontend/src/panels/setup/HarborLane.tsx` — 数据接入港航道导航
- `docs/design/frontend-redesign-data-harbor-plan.md` — Phase 2c 实施计划
- `frontend/src/components/StarBadge.tsx` — 星体徽章（治理 / 接入港 / 引擎舱共享）
- `docs/design/frontend-redesign-optimization-engine-plan.md` — Phase 3 实施计划
