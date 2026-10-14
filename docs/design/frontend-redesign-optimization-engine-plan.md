# Atlas 前端重设计实施计划 - Phase 3: 优化引擎舱

> **状态：已完成（2026-10-14）**——4 任务全部完成；前端全量 294 测试通过（39 文件）、typecheck 与 build 成功。
>
> **Goal:** 将 `panels/optimization/` 三个面板（反馈审核队列 / 训练任务 / 节点实验）按星图仪表语言完成内部重设计——容器统一 `.atlas-page`、状态星体化（StarBadge）、读数等宽。保持全部文案与"API 集成待完成"骨架边界不变（路由接入属未来工作，见 frontend-redesign.md 模块 5「未来路由」）。

**Architecture:** 三个面板保持独立组件结构不变，仅做表现层替换：Tag 状态 → StarBadge；数字/时间 → `.atlas-mono`；容器 → `.atlas-page`。

**Spec:** `docs/design/frontend-redesign.md`（模块 5：优化引擎舱，隐喻"引擎舱"）

**Tech Stack:** React 18, TypeScript, AntD 5, CSS custom properties

## Global Constraints

- 诚实性约束：三面板是**骨架实现**（API 集成待完成）——不得移除「API 集成待完成」「无 GPU 显示 BLOCKED」「实验不修改 active 发布」等如实告知文案
- 纯表现层：不改任何 TODO/API 对接逻辑、不动表单校验规则
- 状态展示用**契约原文**（StarBadge raw 值，沿用 Phase 2b/2c 决定）
- 凡数字/标识符/时间等宽（JetBrains Mono）
- 测试守线：`feedback-queue.test.tsx`、`training-job.test.tsx`、`node-experiment.test.tsx`、`star-badge.test.tsx` 必须通过（既有中文文案断言保持）

---

## Task 1: 反馈队列星体化 + StarBadge 映射扩展

**Files:**
- Modify: `frontend/src/components/StarBadge.tsx`
- Modify: `frontend/src/__tests__/star-badge.test.tsx`
- Modify: `frontend/src/panels/optimization/FeedbackQueue.tsx`

**内容:**

- StarBadge 新增引擎舱映射：`up` / `approved` → active；`down` → failed；`corrected` / `pending_review` → draft；`running` → verified（青色，与运行图 running 一致）；`completed` → active
- FeedbackQueue：容器 `div[style=padding]` → `.atlas-page`；verdict 与 status 的 Tag → StarBadge（契约原文）；提交时间等宽

## Task 2: 训练机组仪表化

**Files:**
- Modify: `frontend/src/panels/optimization/TrainingJob.tsx`

**内容:**

- 容器 → `.atlas-page`
- 训练配方 Descriptions 数值参数（LoRA r/α、dropout、epochs、lr、batch×accum、max_length、seed、min_samples）用 `.atlas-mono`
- 任务状态列 Tag → StarBadge（raw 值；queued/cancelled 无映射 → 灰）

## Task 3: 节点实验台仪器化

**Files:**
- Modify: `frontend/src/panels/optimization/NodeExperiment.tsx`

**内容:**

- 容器 → `.atlas-page`
- 实验结果卡：status → StarBadge（`completed` → 绿 / `pending` → 琥珀）；release SHA 短写用 `Text code`（等宽）

## Task 4: 最终验证与文档

- 全量测试 + typecheck + build
- 更新 `docs/design/frontend-redesign.md` 标记 Phase 3 完成（五大模块全部完成）
