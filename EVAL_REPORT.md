# EVAL_REPORT — Atlas 评测汇总（自动生成）

> 生成器：`eval/report.py`（Day 39）；生成时间：2026-09-03T17:20:39+08:00（仅时间戳不可复现）
> 绑定 git sha：`a207284`；数据快照：`data/snapshots/a207284.meta.json`
> 规则：**无手写数字**；每格数字 source 列可追溯，缺失显示占位不推断。

---

## 1. 主评测（compiler-only 基线 · `make eval`）

| 指标 | 值 | source |
|---|---|---|
| finance_total | 55 | `a207284.json` |
| retail_skipped | 2 | `a207284.json` |
| plan_acc | 50/50 | `a207284.json` |
| clarify | 5/5 | `a207284.json` |
| ex | 50/50 | `a207284.json` |
| ex_anchored | 0 | `a207284.json` |
| exec_errors | 0 | `a207284.json` |

**评测脚本**：`eval/runner.py`（a207284.json `created_at`=2026-09-03T17:19:37+08:00）

## 2. 确定性链覆盖分析（`make baseline`）

| deterministic_coverage | 55/55 | `baseline-compiler-a207284.json` |
| plan_hit | 50/50 | `baseline-compiler-a207284.json` |
| clarify | 5/5 | `baseline-compiler-a207284.json` |
| ex | 50/50 | `baseline-compiler-a207284.json` |
| exec_errors | 0 | `baseline-compiler-a207284.json` |

> 结论（转述 `conclusion` 字段）：注册语义域内（48 条金融 gold 覆盖的口径）确定性链零 LLM 全覆盖：55/55（解析命中 50/50 + 歧义反问 5/5）；EX 50/50 与锚定快照一致。此结论不推断域外泛化：未见指标/复合分析/新措辞问句不在基线内，是 Day 31-34 RAG+LLM 候选生成器的对照实验对象。

## 3. 四策略对比（`make compare`）

| table | <缺失：报告文件不存在，先运行对应 make target> | `compare-4way-a207284.json` |

## 4. RAG+LLM 生成链路（`make rag-eval ENGINE=<engine>`）

| summary | <缺失：报告文件不存在，先运行对应 make target> | `rag-llm-*-a207284.json` |

## 5. 检索与 Schema Linking（`make schema-link` 等）

| 报告 | 指标 Recall@1 | 指标 Recall@5 | fail_cases | 说明 |
|---|---|---|---|---|
| `schema-link-bm25-a207284.json` | <缺失：报告文件不存在，先运行对应 make target> | | | schema linking 图域粗筛（Day 29） |
| `retrieval-rerank-a207284.json` | <缺失：报告文件不存在，先运行对应 make target> | | | rerank 主链路（retrieval，Day 24） |
| `retrieval-bm25-a207284.json` | <缺失：报告文件不存在，先运行对应 make target> | | | BM25 全量域单路（对照） |
| `retrieval-fuse-a207284.json` | <缺失：报告文件不存在，先运行对应 make target> | | | 双路 fuse（对照） |

## 6. 安全与权限验证

| 验证 | 结果 | source |
|---|---|---|
| 恶意 SQL 拦截（Guard）10/10（all_blocked=True） | 按分支统计 2013 年佣金收入，列出前 5 名 | `p1-chain-a207284.json` |
| P1 五道 gates | {'route_unique_metric': True, 'sql_limit_and_time': True, 'malicious_10_blocked': True, 'gold102_hash_match': True, 'branch_policy_effective': True} | `p1-chain-a207284.json` |
| 行级权限（三角色 row_count）hq_admin:5；branch_manager:2；compliance_auditor:5 | 按分支和客户等级统计 2015 年交易额，列出前 5 名 | `rls-verify-a207284.json` |
| Polaris RBAC（atlas_analyst） | atlas_analyst | `polaris-rbac-a207284.json` |
| 派生指标全链路 verify | ok | `metrics-verify-a207284.json` |

## 7. 失败样本归集（`eval/failure_collect.py`）

| category | pending_review | approved | source |
|---|---|---|---|
| （无失败样本目录——归集机制就绪，样本待 LLM 实测产生） | | | `eval/failures/` |

## 8. 报告来源清单（eval/reports/，绑定 sha）

```
api-acceptance-a207284.json
baseline-compiler-a207284.json
metrics-verify-a207284.json
p1-chain-a207284.json
polaris-rbac-a207284.json
rls-verify-a207284.json
a207284.json
```

> 规则：本文件无手写数字；上表每个值均可在对应 source 文件中机械核对。缺失报告显示占位符而非推断值（AGENTS.md 9.3）。
