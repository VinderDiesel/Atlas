# EVAL_REPORT — Atlas 评测汇总（自动生成）

> 生成器：`eval/report.py`（Day 39）；生成时间：2026-09-03T13:05:46+08:00（仅时间戳不可复现）
> 绑定 git sha：`7d48dcb`；数据快照：`data/snapshots/7d48dcb.meta.json`
> 规则：**无手写数字**；每格数字 source 列可追溯，缺失显示占位不推断。

---

## 1. 主评测（compiler-only 基线 · `make eval`）

| 指标 | 值 | source |
|---|---|---|
| finance_total | 48 | `7d48dcb.json` |
| retail_skipped | 2 | `7d48dcb.json` |
| plan_acc | 44/44 | `7d48dcb.json` |
| clarify | 4/4 | `7d48dcb.json` |
| ex | 44/44 | `7d48dcb.json` |
| ex_anchored | 0 | `7d48dcb.json` |
| exec_errors | 0 | `7d48dcb.json` |

**评测脚本**：`eval/runner.py`（7d48dcb.json `created_at`=2026-09-03T13:05:44+08:00）

## 2. 确定性链覆盖分析（`make baseline`）

| deterministic_coverage | 48/48 | `baseline-compiler-7d48dcb.json` |
| plan_hit | 44/44 | `baseline-compiler-7d48dcb.json` |
| clarify | 4/4 | `baseline-compiler-7d48dcb.json` |
| ex | 44/44 | `baseline-compiler-7d48dcb.json` |
| exec_errors | 0 | `baseline-compiler-7d48dcb.json` |

> 结论（转述 `conclusion` 字段）：注册语义域内（48 条金融 gold 覆盖的口径）确定性链零 LLM 全覆盖：48/48（解析命中 44/44 + 歧义反问 4/4）；EX 44/44 与锚定快照一致。此结论不推断域外泛化：未见指标/复合分析/新措辞问句不在基线内，是 Day 31-34 RAG+LLM 候选生成器的对照实验对象。

## 3. 四策略对比（`make compare`）

| 策略 | status | EX | Plan Acc | clarify | token_total | latency_mean_ms | cost_usd_est | refuse(clear) | source |
|---|---|---|---|---|---|---|---|---|---|
| compiler-only | measured | 44/44 | 44/44 | 4/4 | 0 | 178.6 | 0.0 | 0/44 | `eval/reports/7d48dcb.json` |
| rag-llm(openai) | measured | 44/44 | 44/44 | 4/4 | 99597 | 2564.3 | 0.017857 | 0/44 | `eval/reports/rag-llm-openai-7d48dcb.json` |
| lora | blocked | n/a | n/a | n/a | n/a | n/a | n/a | n/a | `compare-4way-7d48dcb.json` |
| lora-sc | blocked | n/a | n/a | n/a | n/a | n/a | n/a | n/a | `compare-4way-7d48dcb.json` |

**口径声明（转述 notes）**：
- 六维口径：EX/Plan Acc/歧义反问与 eval/runner.py 一致；token/latency/cost 来自各策略报告；refuse_rate_clear = 非歧义样本中拒绝数（歧义反问不算拒绝）。
- compiler-only token=0/cost=0 是确定性链路的设计事实；latency 未埋点前为 n/a（OTel 全链路 Day 50；可用 --measure-compiler 现场补测）。
- LoRA/LoRA+SC 登记 blocked 不编造数字（AGENTS.md N1）；解锁条件：GPU 训练 adapter（Day 37-38；LLM 端点已就绪）。

## 4. RAG+LLM 生成链路（`make rag-eval ENGINE=<engine>`）

### engine=openai

| 指标 | 值 | source |
|---|---|---|
| non_ambiguous | 44 | `rag-llm-openai-7d48dcb.json` |
| ambiguous | 4 | `rag-llm-openai-7d48dcb.json` |
| plan_acc | 44/44 | `rag-llm-openai-7d48dcb.json` |
| clarify | 4/4 | `rag-llm-openai-7d48dcb.json` |
| ex | 44/44 | `rag-llm-openai-7d48dcb.json` |
| refused_on_clear | 0 | `rag-llm-openai-7d48dcb.json` |
| exec_errors | 0 | `rag-llm-openai-7d48dcb.json` |
| total_tokens | 99597 | `rag-llm-openai-7d48dcb.json` |
| mean_latency_ms | 2564.3 | `rag-llm-openai-7d48dcb.json` |
| cost_usd_est | 0.017857 | `rag-llm-openai-7d48dcb.json` |

### engine=stub

| 指标 | 值 | source |
|---|---|---|
| non_ambiguous | 44 | `rag-llm-stub-7d48dcb.json` |
| ambiguous | 4 | `rag-llm-stub-7d48dcb.json` |
| plan_acc | 44/44 | `rag-llm-stub-7d48dcb.json` |
| clarify | 4/4 | `rag-llm-stub-7d48dcb.json` |
| ex | 44/44 | `rag-llm-stub-7d48dcb.json` |
| refused_on_clear | 0 | `rag-llm-stub-7d48dcb.json` |
| exec_errors | 0 | `rag-llm-stub-7d48dcb.json` |
| total_tokens | 0 | `rag-llm-stub-7d48dcb.json` |
| mean_latency_ms | 0.1 | `rag-llm-stub-7d48dcb.json` |
| cost_usd_est | 0.0 | `rag-llm-stub-7d48dcb.json` |

> stub 引擎 = 确定性假引擎（走 Planner），仅验证评测链路口径，
> 不具任何 LLM 能力（真实引擎实测见本段其他 engine 小节）。


## 5. 检索与 Schema Linking（`make schema-link` 等）

| 报告 | 指标 Recall@1 | 指标 Recall@5 | fail_cases | 说明 |
|---|---|---|---|---|
| `schema-link-bm25-7d48dcb.json` | 44/44 | 44/44 | 0 | schema linking 图域粗筛（Day 29） |
| `retrieval-rerank-7d48dcb.json` | 44/44 | 44/44 | 0 | rerank 主链路（retrieval，Day 24） |
| `retrieval-bm25-7d48dcb.json` | 41/44 | 44/44 | 0 | BM25 全量域单路（对照） |
| `retrieval-fuse-7d48dcb.json` | 40/44 | 44/44 | 0 | 双路 fuse（对照） |

## 6. 安全与权限验证

| 验证 | 结果 | source |
|---|---|---|
| 恶意 SQL 拦截（Guard）10/10（all_blocked=True） | 按分支统计 2013 年佣金收入，列出前 5 名 | `p1-chain-7d48dcb.json` |
| P1 五道 gates | {'route_unique_metric': True, 'sql_limit_and_time': True, 'malicious_10_blocked': True, 'gold102_hash_match': True, 'branch_policy_effective': True} | `p1-chain-7d48dcb.json` |
| 行级权限（三角色 row_count）hq_admin:5；branch_manager:2；compliance_auditor:5 | 按分支和客户等级统计 2015 年交易额，列出前 5 名 | `rls-verify-7d48dcb.json` |
| Polaris RBAC（atlas_analyst） | atlas_analyst | `polaris-rbac-7d48dcb.json` |
| 派生指标全链路 verify | ok | `metrics-verify-7d48dcb.json` |

## 7. 失败样本归集（`eval/failure_collect.py`）

| category | pending_review | approved | source |
|---|---|---|---|
| generation | 0 | 0 | `eval/failures/generation/` |
| understanding | 0 | 0 | `eval/failures/understanding/` |

## 8. 报告来源清单（eval/reports/，绑定 sha）

```
baseline-compiler-7d48dcb.json
compare-4way-7d48dcb.json
metadata-extract-7d48dcb.json
metrics-verify-7d48dcb.json
p1-chain-7d48dcb.json
polaris-rbac-7d48dcb.json
rag-llm-openai-7d48dcb.json
rag-llm-stub-7d48dcb.json
retrieval-bm25-7d48dcb.json
retrieval-fuse-7d48dcb.json
retrieval-milvus-7d48dcb.json
retrieval-rerank-7d48dcb.json
rls-verify-7d48dcb.json
schema-link-bm25-7d48dcb.json
7d48dcb.json
```

> 规则：本文件无手写数字；上表每个值均可在对应 source 文件中机械核对。缺失报告显示占位符而非推断值（AGENTS.md 9.3）。
