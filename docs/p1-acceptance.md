# P1 端到端验收记录（Day 28 · 绑定 HEAD `7d48dcb`）

> 验收方式：`make p1-verify`（serving/p1_acceptance.py）可重复回归，非一次性演示。
> 报告：`eval/reports/p1-chain-7d48dcb.json`；截图：`docs/screenshots/p1-chain.png`（901px 浏览器全页渲染）。

## 1. 验收场景

- **载体问句**：gold-102「按分支统计 2013 年佣金收入，列出前 5 名」（真实 gold 用例，result_hash 已锚定）
- **目标链路**：问句 → Planner 唯一路由 → 选中 `commission_revenue@v1`（active）→ Compiler 编译 → Guard 注入行级策略 → Doris 执行 → 结果 + trace

## 2. 五道断言（全部通过）

| # | 断言 | 证据 |
|---|---|---|
| 1 | 问句路由到**唯一**指标（不猜测、歧义即澄清） | Plan.metric=`commission_revenue`，dimensions=`["Branch"]`，time=2013，limit=5，order_by=降序 |
| 2 | 选中指标版本为 governance **v1 active**（supersedes=null，链头部） | `metric_version` 字段 |
| 3 | 编译 SQL 带 **LIMIT 5 + GROUP BY + 2013 时间谓词** | `sql_compiled` 字段 |
| 4 | Guard **恶意 SQL 10 条全部拦截**（门槛第 4 项） | `malicious_gate`：10 类全 blocked（见 §3） |
| 5 | **hq_admin 结果 sha256 = gold-102 锚定 result_hash**（EX 匹配）+ branch_manager 只见注入分支 | `gold_hash_match: true`，分支行数 1 |

## 3. Guard 恶意拦截 10 条（类别与代表语句）

INSERT / UPDATE / DELETE / DROP TABLE / ALTER TABLE / GRANT / CREATE TABLE /
`SELECT sleep(10)` / `SELECT pg_sleep(10)` / `SELECT benchmark(1000000, md5(1))`
——10 条全部以 `UnsafeQuery` 拒绝（Guard 校验链：只读检查 → 函数黑名单 → 表白名单 → LIMIT → 策略 → 二次只读）。

## 4. 执行结果（Doris 实测）

hq_admin（策略 1=1，全量）Top5——2013 年佣金收入前 5 分支：

| Branch | commission_revenue |
|---|---|
| jKgsNAQeUQpTwGTOJGUBqRQF | 372849.61 |
| NULL（无 broker 关联） | 172777.70 |
| wjYPZaGSUHUxxicvCRtQYjjLIIoEQx | 99531.57 |
| XWpanJdTmELMLhgTTO dTydaFDO | 76415.31 |
| MjZZDHSKvLFQXioPMTDdcQmQ | 38259.88 |

branch_manager（谓词 `branch = 'jKgsNAQeUQpTwGTOJGUBqRQF'`）只见 1 行 = Top1 分支
（`372849.61`）——行级权限在 SQL 谓词层生效。

**数据特性如实说明**：Top5 第 2 名 Branch 为 NULL——TPC-DI 变造数据中部分 trade 的
broker 关联在 dim_broker 无匹配（INNER JOIN 语义下应为空集，此处非 NULL 说明该
broker 记录存在但 Branch 列为 NULL，属维度表数据特性）。该结果与 gold-102 锚定
result_hash 完全一致（EX=pass），非口径错误，解读时需注意。

## 5. trace（MVP 结构化耗时事件，OTel 全链路见 Day 50）

| phase | detail | ms |
|---|---|---|
| route | 唯一命中 commission_revenue | 23.6 |
| select_version | commission_revenue@v1 active | 51.3 |
| compile | LIMIT/分组/年份断言通过 | 52.4 |
| malicious_gate | 10 条全部拒绝 | 58.0 |
| execute_hq | 行数 5，sha256 匹配 | 795.5 |
| execute_branch | 分支仅 1 行 | 1088.8 |

执行耗时（~0.8-1.1s/查询）为 Doris 真实查询耗时，其中表全量聚合扫描为主。

## 6. P1 阶段门槛勾选

- [x] 问句能路由到唯一指标定义 —— p1-verify gate 1 + tests/test_planner 契约（Planner 多命中返回澄清）
- [x] 编译出的 SQL 带行级谓词 + LIMIT —— p1-verify gate 3（谓词经 Guard 注入后二次只读校验）
- [x] 三角色行级权限验证通过（`make rls-verify`）—— Day 28 复核：三角色结果差异集 3，
      报告 `eval/reports/rls-verify-7d48dcb.json`
- [x] 只读 Guard 拦截 10 条恶意 SQL 全部成功 —— p1-verify gate 4（10 条）+ tests/test_sql_guard.py 17 例全绿

## 7. 边界（不夸大）

- 版本选择为 governance 记录校验（v1 active 唯一合法发布态），未实现多版本运行时切换
  （supersedes 链校验已落地，运行时"换口径"属 Phase 2）。
- trace 为 MVP JSON 事件；question_id → metric_id → SQL → cost 的 OTel 全链路是 Day 50 任务。
- Branch 值取无空格变造串注入（Guard 防注入字面量边界不放松）；带空格分支仅作 Top 展示。
