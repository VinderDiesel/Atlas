# EVAL_REPORT — Atlas 评测汇总（自动生成）

> 生成器：`eval/report.py`（Day 39）；生成时间：2026-09-04T11:17:18+08:00（仅时间戳不可复现）
> 绑定 git sha：`30b8344`；数据快照：`data/snapshots/30b8344.meta.json`
> 规则：**无手写数字**；每格数字 source 列可追溯，缺失显示占位不推断。

---

## 1. 主评测（compiler-only 基线 · `make eval`）

| 指标 | 值 | source |
|---|---|---|
| finance_total | 62 | `30b8344.json` |
| retail_skipped | 2 | `30b8344.json` |
| plan_acc | 57/57 | `30b8344.json` |
| clarify | 5/5 | `30b8344.json` |
| ex | 57/57 | `30b8344.json` |
| ex_anchored | 0 | `30b8344.json` |
| exec_errors | 0 | `30b8344.json` |

**评测脚本**：`eval/runner.py`（30b8344.json `created_at`=2026-09-04T11:17:14+08:00）

## 2. 确定性链覆盖分析（`make baseline`）

| analysis | <缺失：报告文件不存在，先运行对应 make target> | `baseline-compiler-30b8344.json` |

## 3. 四策略对比（`make compare`）

| table | <缺失：报告文件不存在，先运行对应 make target> | `compare-4way-30b8344.json` |

## 4. RAG+LLM 生成链路（`make rag-eval ENGINE=<engine>`）

| summary | <缺失：报告文件不存在，先运行对应 make target> | `rag-llm-*-30b8344.json` |

## 5. 检索与 Schema Linking（`make schema-link` 等）

| 报告 | 指标 Recall@1 | 指标 Recall@5 | fail_cases | 说明 |
|---|---|---|---|---|
| `schema-link-bm25-30b8344.json` | <缺失：报告文件不存在，先运行对应 make target> | | | schema linking 图域粗筛（Day 29） |
| `retrieval-rerank-30b8344.json` | <缺失：报告文件不存在，先运行对应 make target> | | | rerank 主链路（retrieval，Day 24） |
| `retrieval-bm25-30b8344.json` | <缺失：报告文件不存在，先运行对应 make target> | | | BM25 全量域单路（对照） |
| `retrieval-fuse-30b8344.json` | <缺失：报告文件不存在，先运行对应 make target> | | | 双路 fuse（对照） |

## 6. 安全与权限验证

| 验证 | 结果 | source |
|---|---|---|
| 恶意 SQL 拦截（Guard）? | <缺失：报告文件不存在，先运行对应 make target> | `p1-chain-30b8344.json` |
| P1 五道 gates | <缺失：报告文件不存在，先运行对应 make target> | `p1-chain-30b8344.json` |
| 行级权限（三角色 row_count）<缺失：报告文件不存在，先运行对应 make target> | <缺失：报告文件不存在，先运行对应 make target> | `rls-verify-30b8344.json` |
| Polaris RBAC（atlas_analyst） | <缺失：报告文件不存在，先运行对应 make target> | `polaris-rbac-30b8344.json` |
| 派生指标全链路 verify | <缺失：报告文件不存在，先运行对应 make target> | `metrics-verify-30b8344.json` |

## 7. 失败样本归集（`eval/failure_collect.py`）

| category | pending_review | approved | source |
|---|---|---|---|
| （无失败样本目录——归集机制就绪，样本待 LLM 实测产生） | | | `eval/failures/` |

## 8. 报告来源清单（eval/reports/，绑定 sha）

```
30b8344.json
```

> 规则：本文件无手写数字；上表每个值均可在对应 source 文件中机械核对。缺失报告显示占位符而非推断值（AGENTS.md 9.3）。
