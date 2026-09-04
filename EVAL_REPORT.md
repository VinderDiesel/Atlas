# EVAL_REPORT — Atlas 评测汇总（自动生成）

> 生成器：`eval/report.py`（Day 39）；生成时间：2026-09-04T14:00:51+08:00（仅时间戳不可复现）
> 绑定 git sha：`9749fc5`；数据快照：`data/snapshots/9749fc5.meta.json`
> 规则：**无手写数字**；每格数字 source 列可追溯，缺失显示占位不推断。

---

## 1. 主评测（compiler-only 基线 · `make eval`）

| 指标 | 值 | source |
|---|---|---|
| finance_total | 70 | `9749fc5.json` |
| finance_plan_acc | 65/65 | `9749fc5.json` |
| finance_clarify | 5/5 | `9749fc5.json` |
| finance_ex | 65/65 | `9749fc5.json` |
| finance_ex_anchored | 0 | `9749fc5.json` |
| finance_exec_errors | 0 | `9749fc5.json` |
| finance_zh_total | 62 | `9749fc5.json` |
| finance_en_total | 8 | `9749fc5.json` |
| retail_total | 19 | `9749fc5.json` |
| retail_plan_acc | 18/18 | `9749fc5.json` |
| retail_clarify | 1/1 | `9749fc5.json` |
| retail_ex | 18/18 | `9749fc5.json` |
| retail_ex_anchored | 0 | `9749fc5.json` |
| retail_exec_errors | 0 | `9749fc5.json` |
| retail_zh_total | 14 | `9749fc5.json` |
| retail_en_total | 5 | `9749fc5.json` |

**评测脚本**：`eval/runner.py`（9749fc5.json `created_at`=2026-09-04T13:59:03+08:00）

## 2. 确定性链覆盖分析（`make baseline`）

| analysis | <缺失：报告文件不存在，先运行对应 make target> | `baseline-compiler-9749fc5.json` |

## 3. 四策略对比（`make compare`）

| table | <缺失：报告文件不存在，先运行对应 make target> | `compare-4way-9749fc5.json` |

## 4. RAG+LLM 生成链路（`make rag-eval ENGINE=<engine>`）

| summary | <缺失：报告文件不存在，先运行对应 make target> | `rag-llm-*-9749fc5.json` |

## 5. 检索与 Schema Linking（`make schema-link` 等）

| 报告 | 指标 Recall@1 | 指标 Recall@5 | fail_cases | 说明 |
|---|---|---|---|---|
| `schema-link-bm25-9749fc5.json` | <缺失：报告文件不存在，先运行对应 make target> | | | schema linking 图域粗筛（Day 29） |
| `retrieval-rerank-9749fc5.json` | <缺失：报告文件不存在，先运行对应 make target> | | | rerank 主链路（retrieval，Day 24） |
| `retrieval-bm25-9749fc5.json` | <缺失：报告文件不存在，先运行对应 make target> | | | BM25 全量域单路（对照） |
| `retrieval-fuse-9749fc5.json` | <缺失：报告文件不存在，先运行对应 make target> | | | 双路 fuse（对照） |

## 6. 安全与权限验证

| 验证 | 结果 | source |
|---|---|---|
| 恶意 SQL 拦截（Guard）? | <缺失：报告文件不存在，先运行对应 make target> | `p1-chain-9749fc5.json` |
| P1 五道 gates | <缺失：报告文件不存在，先运行对应 make target> | `p1-chain-9749fc5.json` |
| 行级权限（三角色 row_count）<缺失：报告文件不存在，先运行对应 make target> | <缺失：报告文件不存在，先运行对应 make target> | `rls-verify-9749fc5.json` |
| Polaris RBAC（atlas_analyst） | <缺失：报告文件不存在，先运行对应 make target> | `polaris-rbac-9749fc5.json` |
| 派生指标全链路 verify | <缺失：报告文件不存在，先运行对应 make target> | `metrics-verify-9749fc5.json` |

## 7. 失败样本归集（`eval/failure_collect.py`）

| category | pending_review | approved | source |
|---|---|---|---|
| （无失败样本目录——归集机制就绪，样本待 LLM 实测产生） | | | `eval/failures/` |

## 8. 报告来源清单（eval/reports/，绑定 sha）

```
9749fc5.json
```

> 规则：本文件无手写数字；上表每个值均可在对应 source 文件中机械核对。缺失报告显示占位符而非推断值（AGENTS.md 9.3）。
