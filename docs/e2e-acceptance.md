# 端到端验收（Day 48）：Data Agent 5 场景 + 人工接管

> **本文档所有数字均来自脚本产物** [eval/reports/e2e-acceptance.json](../eval/reports/e2e-acceptance.json)
> （`eval/e2e_acceptance.py` 实测输出，禁止手写数字；复跑命令见文末）。
> 涉及环境：真实 Doris（`eval/runner.execute_sql`）+ 确定性链路（无 LLM 调用）。

## 1. 验收元信息

| 项 | 值 |
|---|---|
| 验收日期 | 2026-09-03（报告 `created_at=2026-09-03T033501Z`） |
| 数据快照 | `7d48dcb`（TPC-DI，dwd 8 表 24 万级行，见 data/snapshots/7d48dcb.meta.json） |
| 执行引擎 | 真实 Doris（mysql.connector）+ LangGraph 状态机 + Guard 只读网关 |
| LLM | 未调用（注册域确定性链路；候选链模式仅 retrieve 确定性检索） |
| 报告文件 | `eval/reports/e2e-acceptance.json`（schema_version=1） |
| 结果 | 6/6 场景通过（5 场景 + handoff 人工接管） |

## 2. 总览

| 场景 | 验收点 | 结果 kind | 断言要点 |
|---|---|---|---|
| S1 正常提问 | 注册域问句走确定性链路 | answer | LIMIT 5 返回 5 行；出口表全在白名单；归因齐全 |
| S2 反问 | 歧义问句不猜答 | clarify | 0 次 SQL 执行；附确定性候选 |
| S3 权限拒绝 | Guard 纵深防御 | blocked | 被拒 SQL 不达执行器；原因不携带 SQL |
| S4 校验失败修复 | 反问 → 补口径 → 成功 | clarify → answer | 同 session 2 轮；Guard 层越权 SQL 拒后收敛 |
| S5 图表 + 解释 | answer 结果直接渲染 | answer | 柱状图轴名零发明；sql_sha256 绑定；归因全字段 |
| S6 人工接管 | 0 候选不空转 | handoff | handoff_reason 带完整原因链；0 次 SQL 执行 |

## 3. 场景记录

### S1 正常提问（确定性链路）

- **问句**：按分支统计 2013 年佣金收入，列出前 5 名（与 eval/gold/gold-102.json 同源）
- **轨迹**：plan(命中) → execute(compile→Guard→Doris) → explain，path=`deterministic`
- **Guard 出口 SQL**（`eval/runner.execute_sql` 实际执行）：

```sql
SELECT dim_broker.Branch AS Branch, SUM(fact_trades.Commission) AS commission_revenue
FROM atlas.dwd.fact_trades AS fact_trades
INNER JOIN atlas.dwd.dim_date AS dim_date ON fact_trades.SK_CreateDateID = dim_date.SK_DateID
INNER JOIN atlas.dwd.dim_broker AS dim_broker ON fact_trades.SK_BrokerID = dim_broker.SK_BrokerID
WHERE dim_date.CalendarYearID = 2013
GROUP BY dim_broker.Branch
ORDER BY commission_revenue DESC LIMIT 5
```

- **结果**：`row_count=5`，`latency_ms=408.8`（单次实测，非基准），result_hash=`557479f4a0f2`
  （样本前 3 行，branch 随机串/含 NULL 是 TPC-DS 数据特征，见 §5）：

| Branch | commission_revenue |
|---|---|
| jKgsNAQeUQpTwGTOJGUBqRQF | 372849.61 |
| NULL | 172777.70 |
| wjYPZaGSUHUxxicvCRtQYjjLIIoEQx | 99531.57 |

- **归因（explain）**：metric=`commission_revenue`，metric_expression=`SUM(fact_trades.Commission)`，
  dimensions=`[Branch]`，filters=`[]`（MVP 无 filter 解析，如实为空），
  tables=`[atlas.dwd.fact_trades, atlas.dwd.dim_date, atlas.dwd.dim_broker]`（Guard 出口 SQL AST 提取），
  data_version=`7d48dcb`，data_refreshed_at=`2026-09-03T09:53:50+08:00`（注入快照 meta，不编造）

### S2 反问（歧义不猜）

- **问句**：最近交易情况怎么样？（与 eval/gold/gold-104.json 同源，歧义样本）
- **轨迹**：plan(判定 unmatched) → clarify 终端
- **结果**：kind=`clarify`，clarification_kind=`unmatched`，reasons=`["无法确定指标口径（问句未命中任何指标同义词）"]`，
  candidates（确定性检索，0 LLM token）= `[total_trade_tax, trade_count, total_trade_value, active_account_count, average_trade_value]`
- **边界**：反问轮 `executor_calls=0`——不猜答、不执行 SQL。

### S3 权限拒绝（Guard 纵深）

- **问句**：按分支统计 2013 年佣金收入，列出前 5 名（同 S1；本轮预算白名单缩窄 1 张表：去掉 `atlas.dwd.fact_trades`）
- **轨迹**：plan(命中) → execute → Guard enforce 拒绝 → blocked 终端
- **结果**：kind=`blocked`，block_reason=`UnsafeQuery: 表不在白名单内：atlas.dwd.fact_trades`
- **边界**：`executor_calls=0`——被拒 SQL 不达执行器；block_reason 只给拒绝类型与表名，不携带被拒 SQL
  （纵深防御，AGENTS.md N3）。语义层有权限不代表 Guard 放行——权限以锁定快照白名单为准。

### S4 校验失败修复（多轮）

- **轨迹**（同一 session `e2e-s4`，turns_in_session=2）：
  1. turn1 问「最近交易情况怎么样？」→ kind=`clarify`（见 S2）
  2. turn2 用户补充口径「按分支统计 2013 年佣金收入，列出前 5 名」→ kind=`answer`，
     row_count=5，result_hash=`557479f4a0f2`（与 S1 同口径结果 hash 一致，可复验）
- **Guard 层补验**（只读通道纵深）：
  - 越权 SQL `SELECT * FROM atlas.dwd.fact_balances LIMIT 5` → Guard 拒绝（`UnsafeQuery`，表不在白名单）
  - 修复 = 收敛到授权域：S1 出口 SQL 再次 enforce → pass
- **边界**：修复发生在用户侧（反问 → 补口径）与守卫侧（越权 → 收敛授权域），
  系统全程不猜测不绕过；MVP 无跨轮自动纠错（无 LLM 参与）。

### S5 图表 + 解释

- **问句**：同 S1；answer 结果直接作为 `render_chart` 输入（防幻觉：schema 必须来自已执行结果）
- **图表 spec**（确定性，无像素渲染）：

| 字段 | 值 |
|---|---|
| type | `bar` |
| x | `Branch`（执行结果维度列） |
| y | `[commission_revenue]`（Decimal 数值列，真实执行器形态） |
| data_points | 5 |
| sql_sha256 | `de3aff6548ca`（执行 SQL 摘要，spec 可溯源） |
| note | 无（单数值列、5 行 ≤ 200 类目上限，无需注记） |

- **数据样例**（y 为 Decimal → 报告归一化 str）：`[{"x": "jKgs…", "y": "372849.61"}, {"x": null, "y": "172777.70"}, …]`
- **解释**：explanation 全字段（metric/tables/row_count/latency_ms/data_version/data_refreshed_at/path=deterministic）
- **边界**：轴名零发明（x/y 均来自执行结果 columns）；行数 ≤ 200 不降级；Branch 含 NULL 时如实渲染为缺失类目。

### S6 人工接管（handoff 节点，Day 48）

- **问句**：2013年各分支机构的绩效奖金总额排名（域外措辞，真实检索 0 候选）
- **配置**：allow_candidate=True（候选模式：unmatched 问句允许进入 retrieve → generate 链）
- **轨迹**：plan(unmatched) → retrieve（真实 schema linking 0 候选）→ **handoff 终端**
- **结果**：kind=`handoff`，handoff_reason=

  > 本回合无法自动完成：schema linking 未检索到注册域候选指标；LLM 候选生成无素材、反问无候选可澄清，系统不空转不编造——已转人工接管

- **边界**：`executor_calls=0`。handoff 触发条件 = 候选链素材空（LLM 无生成基础、反问无候选可澄清）；
  与 clarify（有素材可反问）、blocked（Guard 拒绝，人工也不得绕过，AGENTS.md N3）、
  error（执行期故障，运维排查）语义互斥，不互相吞并。

## 4. handoff 节点设计说明（agent/graph.py）

- **图**：`START → plan →(unmatched, allow_candidate) retrieve → generate → validate → execute → explain → END`，
  retrieve 0 候选 → `handoff → END`（第 8 个节点，任务书 7 节点 + Day 48 接管）。
- **状态**：`TurnState.handoff_reason`（每轮 plan 节点冲刷，防跨轮残留）；`TurnResult.kind="handoff"` 对外暴露。
- **不转人工的安全边界**：Guard 拒绝（blocked）不 handoff——人工接管也不得绕过只读红线；
  执行器故障（error）不 handoff——属运维排查。
- **契约测试**：tests/test_graph.py TestHandoff 3 例（0 候选 → handoff；原因链完整；多轮冲刷不残留）。

## 5. 诚实注记（实测事实与边界）

1. **Branch 随机字符串与 NULL**：TPC-DS/TPC-DI 生成数据特征（dim_broker 分支名为随机串、含 NULL），
   非程序缺陷；图表如实渲染缺失类目。
2. **latency_ms 为单次实测**（S1=408.8ms / S5=232.7ms，不同轮次），不代表性能基准——基准需 profile 数据
   （AGENTS.md 决策优先级 6）。
3. **无 LLM 参与**：全部场景走确定性 Planner/Compiler/Guard 链路；S6 候选模式仅使用确定性 retrieve。
4. **result_hash 口径**：列序=SELECT 序、行序=返回序、NULL 固定表示（与 eval/runner.py 同源），
   可对快照 `7d48dcb` 复验。
5. **截图项**：沿用 retro-p2 登记口径——本验收为 markdown 形态；前端渲染与可视化截图由
   Day 50-56（serving/可观测）统一产出，此处不重复引用占位图。
6. 本轮验收未改动 `semantic/` 与任何数据快照；报告 `eval/reports/e2e-acceptance.json` 为脚本机械产物。

## 6. 复跑命令

```bash
# 前置：Doris 已 up 且锁定快照数据可查（AGENTS.md：make up / make seed 后）
uv run python -m eval.e2e_acceptance --report eval/reports/e2e-acceptance.json
# 退出码 0 = 6/6 通过；非 0 = 场景断言失败（可作门禁）
```

契约测试（不依赖 Doris）：`uv run python -m unittest tests.test_graph tests.test_chart -v`
