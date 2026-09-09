# Atlas `query` 端到端实测报告

> 目的：用**真实自然语言问题**驱动 `atlas query` 全链路（Planner → Compiler → Guard → Doris 执行），
> 验证确定性编译、行级策略注入、跨域、未知指标澄清等路径，并将**问题 / 中间过程 / 结果**一并归档。
> 所有数字均来自实跑输出，未做任何手工估算。

---

## 1. 执行环境与命令

- `atlas` 是 `pyproject.toml` 的 `[project.scripts]` 控制台入口，资源（语义模型、快照）全部基于
  `__file__` 解析，**与当前工作目录无关**。
- 本报告所有命令均在 **`/tmp`** 下执行（`cd /tmp && /.../atlas ...`），以证明 cwd 无关性。
- 命令形态：

```bash
# 一步问数（真连库）：Planner→Compiler→Guard→Doris
atlas query "2013 年总交易额" --format json
atlas query "2001 年总销售额" --domain retail --format json
atlas query "2013 年总交易额" --role branch_manager --role-ctx branch=BR_A1 --format json

# 仅看解析（不执行）：
atlas plan "2013 年按分行统计总交易额" [--domain retail]
```

- 前置：Doris 在线 + 锁定快照存在（`data/snapshots/*.meta.json`，Guard 预算锁快照表白名单）。

---

## 2. 链路架构

```
问句
  │
  ▼
Planner.plan(question)           确定性解析 → Plan(metric / dimensions / time / filters)
  │  └─ 未知指标(unmatched) 且 --llm → Generator 候选链（仍须命中已注册指标）
  ▼
Compiler.compile(plan)           确定性 → 只读 SQL（带 LIMIT + 时间约束）
  │
  ▼
Guard.enforce(sql, budget, policy)
  │   ├─ 只读校验（禁 INSERT/UPDATE/DELETE/...）
  │   ├─ 预算锁快照表白名单（build_budget + SNAPSHOT_DIR/*.meta.json）
  │   ├─ 时间窗下界（仅当 SQL 未含时间约束时叠加，见 §4.2）
  │   └─ 行级策略 predicate（--role 注入，见 §7）
  ▼
execute_sql(guarded_sql)         via eval/runner，真连 Doris → rows
```

退出码：`0` 成功 / `1` 错误（编译·执行·缺快照）/ `2` 澄清 / `3` 被 Guard 拒绝。

---

## 3. 测试中暴露并修复的真实问题

实测中发现 5 个会阻断"链路成功返回数据"的真实缺陷，均已修复（非脚本问题）：

| # | 问题 | 根因 | 修复 | 影响面 |
|---|---|---|---|---|
| 4.1 | Guard 生成的 `INTERVAL '730 DAY'` Doris 报语法错 | `exp.Literal.string` 把 `730 DAY` 包成引号字符串（Postgres 写法，Doris 拒） | 改为 `INTERVAL 730 DAY`（数字+单位） | 所有真连库路径 |
| 4.2 | 历史年份查询返回 `null`（如 2013） | Guard 仅认 `dim_date.DateValue` 判定"是否已有时间约束"；编译器对 year/quarter/month 用 `CalendarYearID/CalendarQtrID/CalendarMonthID`，被误判为无约束 → 叠加 730 天窗口（≈2024 起）排除全部历史数据 | Guard 改为识别**模型声明的全部时间列** | `query`/`ask`/`p1-verify` 历史查询 |
| 4.3 | `--format json` 执行后崩溃，退出码 1 | Doris 返回 `Decimal`/`date`，`json.dumps` 无 `default` 兜底 | 加 `default` 兜底：`Decimal→str`（保精度）、`date→iso` | 所有 json 机读输出 |
| 4.4 | `atlas plan` 不支持 `--domain`，retail 解析失败 | plan/compile 子命令未接 `DOMAIN_MODEL_PATHS` | 补 `--domain {finance,retail}`（与 query 一致） | 跨域解析 |
| 4.5 | "按分行统计"/"按门店统计"不分组 | 语义层维度字段缺中文同义词：`dim_broker.Branch` 缺"分行"；`dim_store.s_store_sk` 无同义词 | 补 `Branch`="分行"+`s_store_sk`="店铺/分店"。**注意**：retail 的"门店"一词已被 `gold-076` 绑定为 `s_state`（SF0.1 单州降级），故 `s_store_sk` 用"店铺/分店"规避，不动 gold 标注 | 维度分组可解析 |

> 附：本次实测用的 shell 包装函数曾因 **zsh 不对未引号变量做分词**（`${extra}` 把 `--role hq_admin` 当一个参数传给 argparse）导致 `SystemExit(2)`，属测试脚本坑，**非代码问题**，已用显式 `if/else + eval` 规避。

---

## 4. 测试场景总览

| # | 域 | 问句 | 路径 | 维度 | 退出码 | 结果摘要 |
|---|---|---|---|---|---|---|
| 1 | finance | `2013 年总交易额` | deterministic | — | 0 | `1354513501.41` |
| 2 | finance | `2013 年按分行统计总交易额` | deterministic | Branch | 0 | 多分行分组 |
| 3 | finance | `2013 年按客户等级分组总交易额` | deterministic | Tier | 0 | 5 行（Tier 1/2/3/8/null） |
| 4 | finance | `2013 年第一季度总交易额` | deterministic | — | 0 | `327605656.21` |
| 5 | finance | `2017-07-07 的持仓市值` | deterministic | — | 0 | `2083098.42` |
| 6 | finance | `2013 年总交易额` + `--role hq_admin` | deterministic+RLS | — | 0 | 全量 `1354513501.41` |
| 7 | finance | `2013 年总交易额` + `--role branch_manager --role-ctx branch=BR_A1` | deterministic+RLS | — | 0 | 空集（策略注入正确） |
| 8 | finance | `2023 年客户满意度评分` | 未知指标 | — | 2 | 澄清（附候选） |
| 9 | retail | `2001 年总销售额` | deterministic | — | 0 | `86183391.24` |
| 10 | retail | `2001 年按店铺统计总销售额` | deterministic | s_store_sk | 0 | 6 行（store 1/2/4/7/8/10） |

> 全部退出码符合预期。retail 数据年份为 **1998–2003**，故用 `2001`（若用 `2013` 会因无销售记录返回空集，非链路问题）。

---

## 5. 各场景详细记录

### 场景 1 · `2013 年总交易额`（单指标 + 年份）

- **Plan**：`metric=total_trade_value`，`dimensions=[]`，`time={granularity: year, value: 2013}`
- **SQL（Compiler）**：
```sql
SELECT SUM(fact_trades.Quantity * fact_trades.TradePrice) AS total_trade_value
FROM atlas.dwd.fact_trades AS fact_trades
INNER JOIN atlas.dwd.dim_date AS dim_date
  ON fact_trades.SK_CreateDateID = dim_date.SK_DateID
WHERE dim_date.CalendarYearID = 2013
LIMIT 100
```
- **guarded_sql（Guard）**：与 SQL 一致（Guard 已识别 `CalendarYearID` 为时间约束，**不再叠加 730 天窗口**）。
- **rows**：`[[1354513501.41]]`，`exit_status=ok`，退出码 `0`。

### 场景 2 · `2013 年按分行统计总交易额`（维度分组）

- **Plan**：`dimensions=['Branch']`
- **SQL**：在上例基础上追加 `GROUP BY dim_broker.Branch`，并 `LEFT JOIN atlas.dwd.dim_broker ... ON fact_trades.SK_BrokerID = dim_broker.SK_BrokerID`。
- **rows**：按分行聚合的多行结果（`exit 0`）。
- 注：维度分组要求**显式结构词**"按X统计/分组"；"各分行"不触发维度。

### 场景 3 · `2013 年按客户等级分组总交易额`（维度分组）

- **Plan**：`dimensions=['Tier']`
- **SQL**：`GROUP BY dim_customer.Tier`。
- **rows**（节选）：
```
Tier 1   ->  若干金额
Tier 2   ->  若干金额
Tier 3   ->  若干金额
Tier 8   ->  若干金额
(null)   ->  若干金额
```
共 5 行，`exit 0`。证明维度分组链路完全成功。

### 场景 4 · `2013 年第一季度总交易额`（季度粒度）

- **Plan**：`time={granularity: quarter, value: 20131}`
- **SQL**：`WHERE dim_date.CalendarQtrID = 20131`
- **rows**：`[[327605656.21]]`，退出码 `0`。

### 场景 5 · `2017-07-07 的持仓市值`（date 粒度）

- **Plan**：`metric=holdings_value`，`time={granularity: date, value: 2017-07-07}`
- **SQL**：`WHERE dim_date.DateValue = CAST('2017-07-07' AS DATE)`
- **rows**：`[[2083098.42]]`，退出码 `0`。date 粒度天然命中 `DateValue`，Guard 不叠加窗口。

### 场景 6 · `2013 年总交易额` + `--role hq_admin`（行级策略·全量）

- **guarded_sql**：在场景 1 SQL 末尾追加 `AND 1 = 1`（hq_admin 策略 condition 恒真，即不限制）。
- **rows**：`[[1354513501.41]]`（与无角色一致），退出码 `0`。

### 场景 7 · `2013 年总交易额` + `--role branch_manager --role-ctx branch=BR_A1`（行级策略·过滤）

- **guarded_sql**：自动 `LEFT JOIN atlas.dwd.dim_broker ... AND dim_broker.branch = 'BR_A1'`。
```sql
SELECT SUM(fact_trades.Quantity * fact_trades.TradePrice) AS total_trade_value
FROM atlas.dwd.fact_trades AS fact_trades
INNER JOIN atlas.dwd.dim_date AS dim_date
  ON fact_trades.SK_CreateDateID = dim_date.SK_DateID
LEFT JOIN atlas.dwd.dim_broker AS dim_broker
  ON fact_trades.SK_BrokerID = dim_broker.SK_BrokerID
WHERE dim_date.CalendarYearID = 2013 AND dim_broker.branch = 'BR_A1'
LIMIT 100
```
- **rows**：`[[null]]`（空集）。原因见 §8.1——`dim_broker.branch` 实际值为哈希串，与策略示例参数 `BR_A1` 不匹配；**策略谓词注入本身正确**，返回空集符合行级最小可见性。
- 退出码 `0`（策略解析/执行成功，仅数据为空）。

### 场景 8 · `2023 年客户满意度评分`（未知指标 → 澄清）

- Planner 返回 `ClarificationRequest`（kind=unmatched，reasons 含"无法确定指标口径"）。
- 默认**不**走 LLM（无 `--llm`），直接澄清退出码 `2`，附候选指标。
- 若加 `--llm` 且配置了 `OPENAI_API_KEY`，则走 Generator 候选链（候选仍须命中已注册指标）。

### 场景 9 · `2001 年总销售额`（retail 域）

- **Plan**：`metric=total_sales_price`，`time={granularity: year, value: 2001}`
- **SQL**：
```sql
SELECT SUM(store_sales.ss_sales_price) AS total_sales_price
FROM atlas.dwd.store_sales AS store_sales
INNER JOIN atlas.dwd.date_dim AS date_dim
  ON store_sales.ss_sold_date_sk = date_dim.d_date_sk
WHERE date_dim.d_year = 2001
LIMIT 100
```
- **rows**：`[[86183391.24]]`，退出码 `0`。证明跨域确定性编译 + 真连库成功。

### 场景 10 · `2001 年按店铺统计总销售额`（retail 维度分组）

- **Plan**：`dimensions=['s_store_sk']`
- **SQL**：`GROUP BY dim_store.s_store_sk`（自动 `LEFT JOIN dim_store`）。
- **rows**（节选）：`store 1/2/4/7/8/10` 各销售额，共 6 行，退出码 `0`。

---

## 6. 行级策略专项（§4.5 修复后验证）

`--role` 经 `serving.auth.resolve_claims(claims)` → `Policy(name, condition)` → `enforce(policy=...)`，
与 `serving/rls_verify` 同源机制。两个角色对比：

| 角色 | 注入谓词 | 结果 |
|---|---|---|
| `hq_admin`（condition `1=1`） | `... AND 1 = 1` | 全量 `1354513501.41` |
| `branch_manager`（`branch='BR_A1'`） | `... AND dim_broker.branch = 'BR_A1'` | 空集（`[[null]]`） |

策略谓词注入正确；返回空集是数据现实（见 §8.1），非链路缺陷。

---

## 7. 已知限制 / 注意事项

1. **行级策略参数与数据值脱节**：示例策略参数 `BR_A1` 与 `dim_broker.branch` 实际哈希串值不匹配，
   故 `branch_manager` 返回空集。这是种子数据与策略示例的不一致，Guard 注入逻辑正确。
2. **未知指标默认澄清**：未命中已注册指标且未带 `--llm` 时退出码 `2`（确定性优先，离线无 key 不报错）。
3. **维度分组需显式结构词**：必须"按X统计/分组"，"各分行"类表述不触发维度解析。
4. **维度同义词与 gold 标注强耦合**：新增维度同义词须评估对 `eval/gold/*` 的影响（如 §4.5 中"门店"已绑定 `s_state`）。
5. **retail 数据年份 1998–2003**：问句年份须落在该区间，否则返回空集（非链路问题）。

---

## 8. 回归与校验

- `make lint`：语义层结构 / 治理扩展 / 黄金集 schema / 权威目录唯一性 / 值域快照 **全部通过**。
- 单元测试（`test_cli_query` / `test_sql_guard` / `test_compiler` / `test_planner`）：**全部通过（无失败）**。
  - `test_compiler.test_retail_dimension_synonyms_collected` 已同步 `s_store_sk` 同义词预期。
- `ruff check`：`agent/cli.py`、`agent/security/sql_guard.py` 等改动文件 **0 error**。

---

## 9. 改动清单（本次为支持端到端验证所作修改）

| 文件 | 改动 |
|---|---|
| `agent/security/sql_guard.py` | ① `INTERVAL 730 DAY` 渲染修复；② 时间约束判定改为识别模型全部时间列 |
| `agent/cli.py` | ① json 输出加 `Decimal/date` 序列化兜底；② 输出补充 `dimensions`/`time`；③ `plan`/`compile` 加 `--domain`；④ 自动从仓库根载入 `.env` |
| `semantic/ossie/atlas_finance.ossie.yaml` | `dim_broker.Branch` 补"分行"同义词 |
| `semantic/ossie/atlas_retail.ossie.yaml` | `dim_store.s_store_sk` 补"店铺/分店"同义词（避开 gold-076 的"门店→s_state"） |
| `pyproject.toml` | 加 `[build-system]` + `[project.scripts] atlas = "agent.cli:main"` |
| `tests/test_cli_query.py` | 新增 8 个单测（路由/输出格式/退出码/RLS） |
| `tests/test_compiler.py` | 同步 `s_store_sk` 维度同义词断言 |
| `README.md` | 常用命令表补充 `make query` / `atlas query` 说明 |
