# EVAL_REPORT — Atlas 评测汇总（自动生成）

> 生成器：`eval/report.py`（Day 39）；生成时间：2026-10-14T16:56:03+08:00（仅时间戳不可复现）
> 绑定 git sha：`213d5f8`；数据快照：`data/snapshots/ad99a2b.meta.json`（按 `created_at` 取最新 meta；快照 sha 与 HEAD 无必然对应）
> 规则：**无手写数字**；每格数字 source 列可追溯，缺失显示占位不推断。
> 聚合模式：当前 sha 单轮。

---

## 0. 主评测趋势（最近评测轮次，按 mtime）

| sha | finance Plan Acc | retail Plan Acc | finance EX | retail EX |
|---|---|---|---|---|
| `ad99a2b` | 73/73 | 24/24 | 73/73 | 24/24 |
| `ccb4c8b` | 73/73 | 24/24 | 73/73 | 24/24 |
| `b95a9e9` | 73/73 | 24/24 | 73/73 | 24/24 |
| `b933e20` | 65/65 | 18/18 | 65/65 | 18/18 |
| `b7e9ce7` | <缺失：报告文件不存在，先运行对应 make target> | <缺失：报告文件不存在，先运行对应 make target> | <缺失：报告文件不存在，先运行对应 make target> | <缺失：报告文件不存在，先运行对应 make target> |

## 1. 主评测（compiler-only 基线 · `make eval`）

| 指标 | 值 | source |
|---|---|---|
> 当前 HEAD `213d5f8` 尚无主评测报告，已回退聚合最新一次 `ad99a2b`（按 mtime 选择；数字不可跨快照混报，见报告 source）。

| finance_total | 79 | `ad99a2b.json` |
| finance_plan_acc | 73/73 | `ad99a2b.json` |
| finance_clarify | 6/6 | `ad99a2b.json` |
| finance_ex | 73/73 | `ad99a2b.json` |
| finance_ex_anchored | 0 | `ad99a2b.json` |
| finance_exec_errors | 0 | `ad99a2b.json` |
| finance_zh_total | 71 | `ad99a2b.json` |
| finance_en_total | 8 | `ad99a2b.json` |
| retail_total | 27 | `ad99a2b.json` |
| retail_plan_acc | 24/24 | `ad99a2b.json` |
| retail_clarify | 3/3 | `ad99a2b.json` |
| retail_ex | 24/24 | `ad99a2b.json` |
| retail_ex_anchored | 0 | `ad99a2b.json` |
| retail_exec_errors | 0 | `ad99a2b.json` |
| retail_zh_total | 22 | `ad99a2b.json` |
| retail_en_total | 5 | `ad99a2b.json` |

**评测脚本**：`eval/runner.py`（ad99a2b.json `created_at`=2026-10-14T15:11:41+08:00）

## 2. 确定性链覆盖分析（`make baseline`）

| analysis | <缺失：报告文件不存在，先运行对应 make target> | `baseline-compiler-213d5f8.json` |

## 3. 四策略对比（`make compare`）

| table | <缺失：报告文件不存在，先运行对应 make target> | `compare-4way-213d5f8.json` |

## 4. RAG+LLM 生成链路（`make rag-eval ENGINE=<engine>`）

| summary | <缺失：报告文件不存在，先运行对应 make target> | `rag-llm-*-213d5f8.json` |

## 5. 检索与 Schema Linking（`make schema-link` 等）

| 报告 | 指标 Recall@1 | 指标 Recall@5 | fail_cases | 说明 |
|---|---|---|---|---|
| `schema-link-bm25-213d5f8.json` | <缺失：报告文件不存在，先运行对应 make target> | | | schema linking 图域粗筛（Day 29） |
| `retrieval-rerank-213d5f8.json` | <缺失：报告文件不存在，先运行对应 make target> | | | rerank 主链路（retrieval，Day 24） |
| `retrieval-bm25-213d5f8.json` | <缺失：报告文件不存在，先运行对应 make target> | | | BM25 全量域单路（对照） |
| `retrieval-fuse-213d5f8.json` | <缺失：报告文件不存在，先运行对应 make target> | | | 双路 fuse（对照） |

## 6. 安全与权限验证

| 验证 | 结果 | source |
|---|---|---|
| 恶意 SQL 拦截（Guard）? | <缺失：报告文件不存在，先运行对应 make target> | `p1-chain-213d5f8.json` |
| P1 五道 gates | <缺失：报告文件不存在，先运行对应 make target> | `p1-chain-213d5f8.json` |
| 行级权限（三角色 row_count）<缺失：报告文件不存在，先运行对应 make target> | <缺失：报告文件不存在，先运行对应 make target> | `rls-verify-213d5f8.json` |
| Polaris RBAC（atlas_analyst） | <缺失：报告文件不存在，先运行对应 make target> | `polaris-rbac-213d5f8.json` |
| 派生指标全链路 verify | <缺失：报告文件不存在，先运行对应 make target> | `metrics-verify-213d5f8.json` |

## 7. 失败样本归集（`eval/failure_collect.py`）

| category | pending_review | approved | source |
|---|---|---|---|
| （无失败样本目录——归集机制就绪，样本待 LLM 实测产生） | | | `eval/failures/` |

## 8. 概念层一致率（`make concept-eval`；ADR-0032，与 EX / Plan Acc 分开报告）

| 指标 | 值 | source |
|---|---|---|
| stage | matcher | `concept-213d5f8.json` |
| layer_enabled | True | `concept-213d5f8.json` |
| hit | 55 | `concept-213d5f8.json` |
| mismatch | 12 | `concept-213d5f8.json` |
| miss | 95 | `concept-213d5f8.json` |
| rate | 55/162 | `concept-213d5f8.json` |
| samples_annotated | 86 | `concept-213d5f8.json` |
| terms_total | 162 | `concept-213d5f8.json` |
| concepts_distinct | 11 | `concept-213d5f8.json` |
| registry_coverage.not_in_registry | 0 | `concept-213d5f8.json` |

> 口径（转述产物 `notes`）：三分口径：hit=标注术语被概念层命中且概念一致；mismatch=命中但概念不一致；miss=未命中或标注概念不在索引。rate = hit/标注总数，不折算任何加权；layer_enabled=false（无层）时全部计 miss（0 命中基线，本体对照）。
**评测脚本**：`eval/concept_eval.py`（concept-213d5f8.json `created_at`=2026-10-14T16:49:24+08:00）

## 9. 报告来源清单（eval/reports/，绑定 sha）

```
concept-213d5f8.json
```

> 规则：本文件无手写数字；上表每个值均可在对应 source 文件中机械核对。缺失报告显示占位符而非推断值（AGENTS.md 9.3）。
