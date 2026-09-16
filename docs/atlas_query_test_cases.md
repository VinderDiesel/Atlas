# Atlas `query` 真实可用测试用例集

> 目的：提供一组**可直接复制执行、真实能出结果**的 `atlas query` 问句，覆盖金融/零售双域的
> 基础聚合、维度分组、过滤、派生指标、行级策略、英文问句与边界澄清等形态。
> 用于快速验证链路、演示、或作为集成测试的输入清单。
>
> **诚实声明（AGENTS.md §9）**：本文所有返回值均为 2026-09-11 在本地 Doris 实跑锚定，
> 绑定快照 `dc4f350`；2026-09-15 在 `ccb4c8b`（同数据多锁：dwd row_counts / snapshot_ids
> 指纹与 `a11d779`/`dc4f350` 一致，见 `data/snapshots/README.md`）上按
> `docs/run_query_cases.sh` 复跑，36 条用例退出码全部符合预期（含 E-03 新行为，见 §11）。
> 未做任何手工估算或外推。复跑时若数据/快照变更，值会随之变化。

---

## 1. 前置条件

| 项 | 要求 | 校验命令 |
|---|---|---|
| Doris 在线 | `atlas-doris-fe` 容器运行中 | `docker ps \| grep atlas-doris-fe` |
| 锁定快照存在 | `data/snapshots/*.meta.json` 至少一份 | `ls data/snapshots/*.meta.json` |
| 语义层已加载 | `semantic/ossie/atlas_{finance,retail}.ossie.yaml` | `make lint` |
| `.env`（仅 RLS 用例需要） | `ATLAS_JWT_SECRET` 已配置 | `grep ATLAS_JWT_SECRET .env` |

> `atlas query` 会自动从仓库根 `.env` 载入 `DORIS_*` 等连接变量（基于 `__file__` 解析，**与 cwd 无关**），
> 无需手动 `export` 或 `--env-file`。

## 2. 命令形态

```bash
# 一步问数（真连库）：Planner → Compiler → Guard → Doris
atlas query "<问句>" [--domain finance|retail] [--format table|json]
                     [--role <角色> --role-ctx k=v] [--llm]

# 仅看解析（不执行 SQL，不连库）：
atlas plan "<问句>" [--domain finance|retail]
```

退出码：`0` ok / `1` error（编译·执行·缺快照）/ `2` clarify（澄清）/ `3` blocked（Guard·策略拒绝）。

> 本仓库未安装 console_script 时，用 `.venv/bin/python -m agent.cli query ...` 等价替代。

## 3. 数据段（问句时间必须落在区间内，否则返回空集）

| 域 | 数据段 | 来源 |
|---|---|---|
| finance | 2012-07-07 ~ 2017-07-07 | TPC-DI Batch1（`tpcdi.trade.t_dts`） |
| retail | 1998-01-02 ~ 2003-01-02 | TPC-DS SF0.1（2003 年仅 37 订单，样本时间建议落 1998~2002） |

---

## 4. 金融域 · 基础聚合（年 / 季 / 月 / 日四粒度）

| # | 问句 | 预期 metric | 预期 time | 实测返回 | 退出码 |
|---|---|---|---|---|---|
| F-01 | `2015 年总交易额` | total_trade_value | year=2015 | `1351999243.4900` | 0 |
| F-02 | `2015 年佣金收入` | commission_revenue | year=2015 | `4042004.72` | 0 |
| F-03 | `2015 年第二季度交易额` | total_trade_value | quarter=2015Q2 | `337881243.6600` | 0 |
| F-04 | `2015 年 5 月交易笔数` | trade_count | month=201505 | `4191` | 0 |
| F-05 | `2015 年 6 月 30 日持仓市值` | holdings_value | date=2015-06-30 | `1531467.15` | 0 |
| F-06 | `2015 年交易笔数` | trade_count | year=2015 | `48881` | 0 |
| F-07 | `2015 年活跃客户数` | active_customer_count | year=2015 | `1011` | 0 |
| F-08 | `2015 年总成交量` | total_trade_quantity | year=2015 | `245223713.00` | 0 |
| F-09 | `2015 年平均成交价` | avg_trade_price | year=2015 | `5.5168` | 0 |

命令示例：

```bash
atlas query "2015 年总交易额" --format json
atlas query "2015 年第二季度交易额" --format json
atlas query "2015 年 6 月 30 日持仓市值" --format json
```

## 5. 金融域 · 维度分组与 TopN

> 维度分组要求**显式结构词**"按 X 统计 / 分组"；"各分行"类表述不触发维度解析。

| # | 问句 | 预期 metric | 预期 dims | 实测返回（节选） | 退出码 |
|---|---|---|---|---|---|
| F-10 | `按客户等级统计 2015 年交易额，列出前 5 名` | total_trade_value | [Tier] | Tier 3=`792882117.36` / 2=`406587534.34` / 1=`124871920.84` / null=`26420250.35` / 8=`1237420.60`（5 行） | 0 |
| F-11 | `按证券类型统计 2015 年成交量，列出前 3 名` | total_trade_quantity | [Issue] | COMMON=`182048717` / PREF_A=`34884551` / PREF_B=`17126162`（3 行） | 0 |
| F-12 | `按分支统计 2015 年佣金收入，列出前 5 名` | commission_revenue | [Branch] | 前 3：`lKcoaVWgOJrAPcmYQCdY`=`289680.01` / `kYEiarXJdlALMKBcaOkRZOqsGXdmaB`=`240073.18` / null=`179593.03`（5 行） | 0 |

> 注：`Branch` 值为 TPC-DI 变造哈希串（distinct=2715，超值域快照阈值 200 被 skip，见
> `semantic/values/atlas_finance_analytics.Branch.json`），问句中**不可**用具体分支名做过滤演示，
> 但分组/TopN 正常。

## 6. 金融域 · 过滤（filter）

> 过滤值须取**实测值域**（`semantic/values/*.json` 有快照的列），否则返回空集。

| # | 问句 | 预期 metric | 过滤 | 实测返回 | 退出码 |
|---|---|---|---|---|---|
| F-13 | `只看交易所 NASDAQ 的 2015 年交易额是多少？` | total_trade_value | ExchangeID=NASDAQ | `340523496.3300` | 0 |
| F-14 | `只看客户等级 3 的客户，2015 年交易额是多少？` | total_trade_value | Tier=3 | `792882117.3600` | 0 |

可用过滤值域（实测锚定，快照 dc4f350）：

| 列 | 可选值 |
|---|---|
| ExchangeID | PCX / NASDAQ / NYSE / AMEX |
| Issue | COMMON / PREF_A / PREF_B / PREF_C / PREF_D |
| Tier | 0 / 1 / 2 / 3 / 7 / 8 |
| Gender | F / M / f / m / …（大小写混存，共 24 值） |
| Status | Completed（单值） |

## 7. 金融域 · 派生指标

| # | 问句 | 预期 metric | 实测返回 | 退出码 |
|---|---|---|---|---|
| F-15 | `2015 年佣金率是多少？` | commission_rate | `0.002989` | 0 |
| F-16 | `2015 年户均持仓市值` | average_holding_value | `366307.251507` | 0 |
| F-17 | `2015 年平均每笔成交金额是多少？` | average_trade_value | `27658.99313618` | 0 |
| F-18 | `2015 年平均每笔佣金是多少？` | average_commission_per_trade | `82.690712` | 0 |

## 8. 金融域 · 行级策略（RLS，需 `.env` 的 `ATLAS_JWT_SECRET`）

| # | 问句 + 角色 | 注入谓词 | 实测返回 | 退出码 |
|---|---|---|---|---|
| F-19 | `2015 年总交易额` `--role hq_admin` | `AND 1 = 1`（恒真，不限制） | `1351999243.4900`（全量，policy=rp_branch_visible） | 0 |
| F-20 | `2015 年总交易额` `--role branch_manager --role-ctx branch=BR_A1` | `AND dim_broker.branch = 'BR_A1'` | `null`（空集） | 0 |

> F-20 返回空集是**数据现实**：示例参数 `BR_A1` 与 `dim_broker.branch` 实际哈希串不匹配，
> 策略谓词注入本身正确（纵深防御下最小可见性）。详见 `docs/atlas_query_e2e_test.md` §8.1。

命令示例：

```bash
atlas query "2015 年总交易额" --role hq_admin --format json
atlas query "2015 年总交易额" --role branch_manager --role-ctx branch=BR_A1 --format json
```

## 9. 金融域 · 英文问句（locale=en_us）

| # | 问句 | 预期 metric | 实测返回 | 退出码 |
|---|---|---|---|---|
| F-21 | `What was the total trade value in 2015?` | total_trade_value | `1351999243.4900` | 0 |
| F-22 | `Show commission revenue by branch in 2015 and list the top 5 branches` | commission_revenue | 分组 5 行 | 0 |
| F-23 | `What was the commission revenue only for exchange NASDAQ in 2015?` | commission_revenue | `872093.53` | 0 |

## 10. 零售域（`--domain retail`）

| # | 问句 | 预期 metric | 预期 time/dims | 实测返回 | 退出码 |
|---|---|---|---|---|---|
| R-01 | `2001 年销售额` | total_sales_price | year=2001 | `86183391.24` | 0 |
| R-02 | `1999 年第一季度销售额` | total_sales_price | quarter=1999Q1 | `13623986.64` | 0 |
| R-03 | `2000 年 5 月的销售额` | total_sales_price | month=200005 | `4184441.85` | 0 |
| R-04 | `2000 年净利润` | net_profit | year=2000 | `-37659039.81` | 0 |
| R-05 | `按品类统计 2001 年销售额，列出前 3 名` | total_sales_price | [i_category] | Electronics=`8939209.92` / Shoes=`8789053.25` / Men=`8768152.50`（3 行） | 0 |
| R-06 | `只看城市 Midway 的 2001 年销售额是多少？` | total_sales_price | s_city=Midway | `70561714.74` | 0 |
| R-07 | `What were total sales in 1999?` | total_sales_price | year=1999 | `85603673.45` | 0 |
| R-08 | `2000 年客单价` | avg_order_value | year=2000 | `21444.078985` | 0 |
| R-09 | `2000 年订单量` | order_count | year=2000 | `4023` | 0 |

可用过滤值域（零售，实测锚定）：

| 列 | 可选值 |
|---|---|
| i_category | Music / Shoes / Electronics / Men / Home / Women / Children / Sports / …（共 10 类） |
| s_city | Midway / Fairview（SF0.1 仅 2 城） |
| s_state | TN（SF0.1 全库单州，无区分度，分组/过滤建议用 s_city） |

命令示例：

```bash
atlas query "2001 年销售额" --domain retail --format json
atlas query "按品类统计 2001 年销售额，列出前 3 名" --domain retail --format json
atlas query "只看城市 Midway 的 2001 年销售额是多少？" --domain retail --format json
```

## 11. 边界与澄清场景

| # | 问句 | 预期行为 | 实测输出 | 退出码 |
|---|---|---|---|---|
| E-01 | `2023 年客户满意度评分` | 未知指标 → 澄清 | `exit_status=clarify`，reasons=`无法确定指标口径（问句未命中任何指标同义词）` | 2 |
| E-02 | `2015 年总交易额和佣金收入分别是多少？` | 双指标歧义 → 澄清 | `exit_status=clarify`，candidates=`[commission_revenue, total_trade_value]` | 2 |
| E-03 | `2001 年销售额同比`（retail） | 时间智能 CTE 已豁免（工作项 11），一步问数直出 | `2000=86269529.76` / `2001=86183391.24`（prev=86269529.76，2 行） | 0 |
| E-04 | `最近交易情况怎么样？` | 口语问句未命中指标同义词 → 澄清 | `exit_status=clarify`，reasons=`无法确定指标口径（问句未命中任何指标同义词）` | 2 |

> E-03 说明：同比/环比/累计/排名（时间智能，B5 ADR-0017）生成的 SQL 含 CTE（`WITH base AS ...`）。
> Guard 表白名单此前不识别 CTE 别名（`[blocked] 表不在白名单内：base`），2026-09 已豁免
> CTE 引用（工作项 11），`atlas query` 一步问数路径直出；会话路径 `atlas ask` 同样可用
> （对照样本 `eval/gold/*/gold-072~078`）。残余边界（filter 组合 / 跨年季度环比 / 累计+维度）见 §12.4。

命令示例：

```bash
atlas query "2023 年客户满意度评分" --format json          # 退出码 2
atlas query "2015 年总交易额和佣金收入分别是多少？" --format json  # 退出码 2
atlas query "2001 年销售额同比" --domain retail            # 退出码 0（输出 2000/2001 两行同比）
```

---

## 12. 已知限制

1. **行级策略参数与数据值脱节**：示例策略参数 `BR_A1` 与 `dim_broker.branch` 实际哈希串不匹配，
   `branch_manager` 返回空集。Guard 注入逻辑正确，属种子数据与策略示例的不一致。
2. **维度分组需显式结构词**：必须"按 X 统计 / 分组"，"各分行"类表述不触发维度解析。
3. **过滤值须取实测值域**：`Branch`/`Symbol` 等高基数列（distinct > 200）无值域快照，
   问句中用具体值过滤会返回空集；用 `ExchangeID`/`Issue`/`Tier`/`i_category`/`s_city` 等有快照的列。
4. **时间智能残余边界（实测，基础形态可用）**：同比/环比/累计/排名的基础形态已在
   query 路径直出（E-03）；以下组合尚未支持——① 同比/环比 + filter：过滤谓词在时间窗
   重建时丢失，`只看城市 Midway 的 2001 年销售额同比` 实测 2001=`84992249.00`（残留
   JOIN 仅去掉孤儿外键行），而 Midway 正确口径为 `70561714.74`；② 跨年季度环比方向
   反转：`2000 年第一季度销售额环比` 实测 2000Q1 行 prev=NULL、prev 值错挂在 1999Q4 行
   （`ORDER BY d_qoy` 丢年份序）；③ 累计 + 维度：外层丢维度投影且窗口无 PARTITION BY，
   各类别行混算（`按类别统计 2001 年各月累计销售额` 实测）。
5. **零售数据年份 1998~2003**：问句年份须落该区间，否则空集（非链路问题）。
6. **快照绑定口径**：运行时快照按「显式 > HEAD > 最新已锁」三级解析
   （`data/identity.py` 的 `resolve_runtime_snapshot`，ADR-0019 决策 ①）：`.env` 的
   `ATLAS_SNAPSHOT_SHA` 优先（显式指定不回退）；否则 HEAD 有其 meta 即绑 HEAD
   （当前 HEAD=`ccb4c8b`）；最后回退 `created_at` 最新的已锁快照。`--format json`
   首行 `[snapshot] sha=… source=… bound_to_head=…` 即回显本次绑定。
   重新锁快照后须重跑锚定本文返回值。

## 13. 一键复跑（脚本化）

完整可执行脚本已落 [`docs/run_query_cases.sh`](run_query_cases.sh)，逐条跑过本文 36 条用例并比对
退出码（不比对具体值——值随快照变，见 §12.6）：

```bash
bash docs/run_query_cases.sh
```

**2026-09-15 实跑结果：通过 36 / 失败 0 / 跳过 0**（`.env` 已配 `ATLAS_JWT_SECRET`，RLS 2 条未跳过）。
退出码分布：33 条 ok（0）、3 条 clarify（2），全部符合预期（E-03 由旧的 blocked 变 ok，见 §11）。

> RLS 用例（F-19/F-20）需 `.env` 的 `ATLAS_JWT_SECRET`，未配置时脚本自动跳过，不阻塞无密钥环境。

---

## 附：与现有文档的关系

| 文档 | 定位 |
|---|---|
| `docs/atlas_query_e2e_test.md` | 一次端到端实测**报告**（含暴露并修复的 5 个真实缺陷） |
| **本文** | 可重复执行的**用例清单**（脚本化输入，绑定快照锚定值） |
| `eval/gold/finance/`、`eval/gold/retail/` | 黄金评测集（106 条 = finance 79 + retail 27，`make eval` 主评测，含 result_hash 锚定） |
| `tests/test_cli_query.py` | query 子命令单元测试（隔离 DB，打桩 Guard/执行） |
