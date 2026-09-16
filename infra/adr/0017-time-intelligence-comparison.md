# ADR-0017：时间智能（同比/环比/累计/排名）——窗口函数编译与 Guard 的 CTE 阻断

- 日期：2026-09-14
- 状态：accepted（**补写 / retroactive**——代码与评测样本先于本决策记录存在，
  9 处引用长期悬空；补写批次 P-1）
- 相关：
  - 实现：`agent/planner.py`（`_COMPARISON_TRIGGERS_ZH/EN` L143-154、
    `_detect_comparison` L583-615、调用点 L411-414/434）、
    `agent/compiler.py`（`ComparisonSpec` L77-88、`Plan.comparison` L101、
    分发 L780-782、`_apply_comparison` L909-929、`_apply_rank` L931-945、
    `_apply_lag` L947+、`_wrap_with_lag` L1126+（`cte_name = "base"` L1134）、
    `_wrap_with_cumulative` L1165+）
  - 安全：`agent/security/sql_guard.py`（`check_tables` L363-373、
    `enforce` L478-512，其中 L495/L512 两次调用 `check_tables`）、
    `agent/graph.py`（`node_execute` 的 enforce L351/L353）
  - 评测：`eval/runner.py`（`plan_acc` 的 comparison 比对 L138-143、
    `evaluate` 的 compile→enforce→execute L266-273）、`eval/gold/schema.json:29`、
    `eval/gold/finance/gold-172~179`（8 条）、`eval/gold/retail/gold-072~078`（7 条）
  - 引用悬空点（本 ADR 补写前指向不存在的文件）：`agent/planner.py:141/411/586`、
    `agent/compiler.py:79/780/916`、`eval/runner.py:138`、`eval/gold/schema.json:29`、
    `docs/atlas_query_test_cases.md:177`
  - 上游决策：ADR-0003（只读 SQL 网关）、ADR-0010（评测方法论）、
    ADR-0014 ③（相对时间设计性不支持）、ADR-0015 ②（形态触发词外置 locale 词典）
  - 落地批次：B5（样本 `a11d779` 2026-09-09 / 实现 `dfedc16` 2026-09-09）

> **行号口径（2026-09-15 工作项 11 落地时补）**：本 ADR 正文的行号引用（如
> `check_tables` L363-373、`enforce` L478-512、`sql_guard.py:368-369`）是立项时
> 坐标，**工作项 11 落地后已漂**（`check_tables` 前插入了 `_cte_names` /
> `_is_cte_reference` 两个助手、`estimate_cost` 内加了两行），不追改；定位一律
> 用符号名：
> `grep -n "^def check_tables\|^def _cte_names\|^def _is_cte_reference\|^def estimate_cost" agent/security/sql_guard.py`。
> 理由同 ADR-0020 行号口径注：行号型指针在会增长的文件上不可靠。

---

## 背景

B5 批次给 Planner/Compiler 加了时间智能：问句含"同比/环比/累计/排名"（及英文
yoy/pop/cumulative/ytd/rank 等）时，`Plan` 携带 `ComparisonSpec(kind=...)`，
Compiler 在基础聚合 SQL 上包裹窗口函数。

但 AGENTS.md §11 的执行是**残缺**的：

| §11 步骤 | B5 实际执行 |
|---|---|
| 1. 无 ADR 则先写 ADR | ❌ **跳过**——9 处代码引用 `ADR-0017`，文件不存在 |
| 3. 先写评测 | ✅ 15 条样本先于实现入库（`a11d779` 早于 `dfedc16`） |
| 4. 最小实现 | ✅ 四 kind 均有 SQL 生成代码 |
| 8. 跑全量 `make lint && make test && make eval` | ❌ **未做**——B5 之后 `eval/reports/` 零产物 |

第 8 步缺失的后果可量化：`EVAL_REPORT.md` 与最新主报告 `eval/reports/5d1e22b.json`
均绑定 `5d1e22b`（2026-09-09T11:41），覆盖 finance 71 + retail 20 = **91 样本**；
而 `eval/gold/` 现存 finance 79 + retail 27 = **106 样本**，差额 15 恰为 B5 样本。
即**时间智能从未进入任何评测报告**。

补写约束（按 N2，必须区分三态，不得把"代码存在"写成"能力达成"）：

- **确定性优先**（§10.4）：时间智能由 Compiler 生成，不经 LLM。
- **绝对时间锚点强制**（ADR-0014 ③）：快照冻结下"上个月同比"必然漂移，
  评测不可复现 → 触发词命中但 `time` 未解析时必须澄清，不猜。
- **N3 只读红线**：包裹后的 CTE SQL 仍须过 `enforce`，不因其为内部生成而豁免。

---

## 备选方案

| 方案 | 优势 | 劣势 |
|---|---|---|
| **窗口函数 + CTE（选定）**：基础聚合入 CTE，外层 `LAG` / `SUM OVER` / `RANK` | 单次扫表；四 kind 用同一套结构表达；Doris 原生支持窗口函数；产物可被 sqlglot 往返解析（§7.2） | **CTE 名与 Guard 表白名单冲突**——实测 3/4 kind 被拒（见代价段 ①） |
| 自连接（两期 self-join 求差） | 不引入 CTE，Guard 白名单天然通过 | 扫表两次；yoy/pop 各写一套 join 条件；**cumulative 无法表达**（需不等值自连接，行数随月份平方增长）；rank 仍需窗口函数 → 四态不统一 |
| 应用层计算（取两期数据在 Python 里算差值/累加） | 完全绕开 SQL 方言差异与 Guard CTE 问题 | 违背计算下推：把聚合结果搬出库再算；`explanation` 无法用出口 SQL 解释归因，直接削弱项目核心卖点（可解释） |
| LLM 生成同比 SQL | 表达灵活 | 违反 §0.2「确定性优先」与「LLM 只是候选生成器」；结果不可复现，评测无法锚定 |

---

## 决策

### ① 四 kind 的语义、触发词与 SQL 形态

| kind | 中文触发词 | 英文触发词 | SQL 形态 | CTE 名 | Guard 实测 |
|---|---|---|---|---|---|
| `yoy` | 同比、年同比 | year-over-year, yoy, yearly comparison | 时间谓词扩为 `IN (prev, current)` + CTE + `LAG(...) OVER (ORDER BY 时间列)` | `base` | ❌ 拒 |
| `pop` | 环比、月环比、季环比 | period-over-period, pop, mom, qoq | 同 yoy，前驱期随粒度（月/季） | `base` | ❌ 拒 |
| `cumulative` | 累计、年初至今 | cumulative, ytd, mtd, running total | 降粒度到月 + CTE + `SUM(...) OVER (ORDER BY 月列 ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)` | `monthly` | ❌ 拒 |
| `rank` | 排名、排行 | rank, ranking | 直接在 SELECT 列表追加 `RANK() OVER (ORDER BY <metric> DESC) AS rank`，**不包 CTE** | 无 | ✅ 通过（cost 0.25） |

实测口径：2026-09-14，HEAD `bdcb6c0`，快照 `dc4f350`（白名单 29 表），
`SemanticModel()` 金融默认模型，`Plan(metric="total_trade_value",
time=TimeSpec(granularity="year", value=2015), comparison=ComparisonSpec(kind=...))`
→ `compile()` → `enforce(sql, budget=budget, model=m)`。

### ② 触发词命中但时间未解析 → 澄清，不猜

`_detect_comparison` 返回三态：`ComparisonSpec`（time 已解析）/ `None`（触发词
未命中，既有行为零变化）/ `ClarificationRequest(kind="relative_time")`（触发词
命中但 `time is None`），文案「「同比」需要绝对时间锚点（固定快照评测下会漂移，
请指定具体年份）」（en 侧同义英文文案）。与 ADR-0014 ③、ADR-0016 ②、KL #29
是同一条"宁缺不猜"原则，零新机制。

### ③ Plan 结构扩展，不新建平行类型

`ComparisonSpec` 是 `@dataclass(frozen=True)` 单字段（`kind: str`），挂
`Plan.comparison: ComparisonSpec | None = None`。缺省 `None` 使 B5 前样本与既有
调用方零改动；`eval/gold/schema.json` 的 `expected_comparison` 缺省 `null` 时
跳过比对（`runner.py:139-143`）。不引入"时间智能 Plan"独立类型，避免出现两套
解析路径与两套编译器入口。

### ④ 评测口径：Plan Acc 先行，EX 待锚定

15 条样本的 `result_hash` 与 `snapshot_sha` **全部为占位符**（`<待执行后填写>` /
`<待锁定后填写>`，实测占位 15 / 已锚定 0），走 `runner.py:280-284` 的首轮
`anchored` 分支。这是 ADR-0010「评测先行」的正常形态——但 EX 目前**无法锚定**，
因为 ①③ 两类 SQL 过不了 Guard（代价段 ①②③）。

---

## 理由

1. **窗口函数是唯一能统一表达四态的形态**：`cumulative` 需要区间累加，自连接会
   行数爆炸；`rank` 本身就是窗口函数。选自连接就得为四 kind 写三套机制。
2. **CTE 而非内联子查询**：出口 SQL 要展示给用户看（`explanation` 与 CLI 都打印
   SQL），CTE 的可读性直接服务于可解释性这一核心卖点；且 Doris 对 CTE 有物化/
   下推优化。
3. **触发词硬编码在 planner 常量，是对 ADR-0015 ② 的偏离**——如实登记为债务：
   ADR-0015 ② 已把形态触发词外置到 `semantic/synonyms/patterns_<locale>.yml`
   （实测该词典含 `time`/`grouping`/`topn`/`filter_include`/`filter_exclude`/
   `threshold`/`magnitude`/`followup` 等段），而 `_COMPARISON_TRIGGERS_ZH/EN`
   仍在 `planner.py:143-154`，词典内 grep `comparison|同比|环比|yoy|rank` 零命中。
   B5 未走外置路径，理由未见记录；本 ADR 不追认该偏离为设计，列入代价段 ⑥。
4. **绝对时间锚点强制**：ADR-0014 ③ 已裁定相对时间设计性不支持，时间智能若不
   继承该约束，会让"上个月同比"在快照冻结下每次评测漂移，EX 永远无法锚定。

---

## 代价与限制（全部为实测或代码级可验证事实）

① **Guard 阻断 3/4 kind，根因是 CTE 名被当物理表**

`sql_guard.check_tables`（L363-373）遍历 `tree.find_all(exp.Table)` 并逐个查
`budget.allowed_tables`，而 **sqlglot 把 CTE 名解析为 `exp.Table`**；`sql_guard.py`
全文对 CTE 零处理（grep `CTE`/`cte`/`WITH`/`exp.With` 零命中）。于是 `base` /
`monthly` 被当成物理表拒绝：`UnsafeQuery: 表不在白名单内：base`。

> **已修复（2026-09-15 工作项 11）**：根因已消除，四 kind 全过 `enforce`
> （cost 各 0.250）、106 样本预检 0 拒——详见「验证方式」判据 1/2 落地注。
> 本节的两条复现命令自此失效：`grep -c "CTE\|cte" agent/security/sql_guard.py`
> 不再为 0（豁免实现必须引用 `exp.CTE`）。

② **`query` 与 `ask` 两条路径同样被拒**

`agent/graph.py:351/353` 的 `node_execute` 走的是同一个 `enforce`（无身份/有身份
两分支）。因此 yoy/pop/cumulative 在 `atlas ask` 下**同样不可用**。

> 纠错：`docs/atlas_query_test_cases.md` §12 与 E-03 写的「同比/环比/累计/排名在
> query 路径被 Guard 拦截，须走 `atlas ask`」有两处错误——(a) `rank` 实测可通过
> Guard，被过度概括进拦截清单；(b) `ask` 路径同样被拒，不是可用替代。该文档需在
> P-1 批次同步纠正。

③ **`make eval`（非 dry）会中断整轮，不产报告**

`eval/runner.py:266-268`：

```python
sql, _ = compiler.compile(plan)
guarded, _ = enforce(sql, budget=budget)   # ← 位于 try 之外
try:
    rows, columns = execute_sql(guarded)   # 只有执行失败被记录不中断
except Exception as exc:  # noqa: BLE001 - 执行失败记录到报告，不中断整轮
```

`enforce` 抛出的 `UnsafeQuery` 不在 `try` 覆盖范围内 → 向上冒泡中断整轮。这是
「B5 之后零评测产物」的**机制性原因**，不只是"忘了跑"。
（本条为静态分析结论 + `enforce` 拒绝行为已实测的合成判断；**未实跑真 eval**，
因真 eval 会回填修改 15 个 gold 文件，需单独授权。）

④ **dry 基线：retail 有 1 条失败**

`python -m eval.runner --dry`（HEAD `bdcb6c0`，106 样本，exit 0，不写报告不连 DB）：

| 域 | total | Plan Acc | clarify | EX |
|---|---|---|---|---|
| finance | 79 | **73/73** | 6/6 | n/a（dry 不执行） |
| retail | 27 | **23/24** | 3/3 | n/a |

唯一失败：`gold-076`。

⑤ **`gold-076` 的失败根因与时间智能无关**

问句「按门店统计 2001 年销售额排名」，标注期望 `dimensions: ["s_state"]`（其
`fibo_concepts` note 自承「SF0.1 单州，按 s_state 代理」）。实测 planner 返回
`dims=()`——retail 维度同义词表里没有"门店"这个词：

```
i_category: ('品类','类别','商品类别')   s_store_sk: ('店铺','分店')
i_brand:    ('品牌',)                    s_state:    ('州','省份','门店州')
                                         s_city:     ('城市','门店城市')
```

对照实测「按门店统计 2001 年销售额」（去掉"排名"）同样 `dims=()` → 该同义词缺口
**先于 B5 存在**，gold-076 只是第一次让它变成可测量的失败。而 `comparison` 解析
是正确的（`ComparisonSpec(kind='rank')`）。

注意 `dims=()` 是**静默降级**（用户要分组却没分组，不澄清），比反问更糟。
口径争议待裁定：给 `s_state` 加"门店"同义词（迎合标注，但用户问门店得到州级
1 行结果，语义误导）vs 把标注改为 `s_store_sk`（忠于语义，但 SF0.1 单州无区分度）
vs 让"门店"命中失败时澄清（符合"宁缺不猜"）。本 ADR 不擅自裁定，列入 P-1。

> **裁定落地（2026-09-16 P-1 收口；用户拍板选第 1 选项「语义纠正：`s_store_sk`」）**：
> 裁定前摆齐三选项实测数据，两项新事实定案——① `dim_store` 12 家门店**全在单州 TN**
> （`s_state` 单值），给 `s_state` 加"门店"会让用户问门店得到 1 行州级结果，语义
> 误导坐实；② 词表缺口是双层的：`compiler.py` 的 `dimension_synonyms` 只收
> `dim_*` 表字段，事实表 `ss_store_sk` 上的"门店"本就到不了 planner——上表只是
> 第一层。落地 = `dim_store.s_store_sk` 词表补"门店"（语义锚回门店粒度）+
> `planner._dim_hits` 升级**最长词优先**（重叠区间按词长降序归属、子串词让位，
> 防「门店城市」/「门店州」因子串包含而双命中误伤 `gold-057`）+ 标注改
> `["s_store_sk"]` 并重锚（真链 6 行：2001 年有销售的门店 6/12 家，
> `result_hash=75c238ad30c9…`、`snapshot_sha=ccb4c8b`）。测试
> `TestStoreDimensionResolution` 3 例先行（1 主例 + 2 防误伤例，落改造前主例红），
> 变异回退「多命中全取」恰红 2 防误伤例。retail `plan_acc` 23/24 → **24/24**
> （`eval/reports/ccb4c8b.json`）。

⑥ **触发词硬编码，偏离 ADR-0015 ②**（见理由 3）——客户/行业定制触发词时必须改
Python 代码，而非改 YAML 词典，治理流程（`make lint`）覆盖不到。

⑦ **`rank` 的列别名是裸 `rank`**：产物为 `RANK() OVER (...) AS rank`。`rank` 在
MySQL 8 / Doris 中是窗口函数名，作裸别名存在保留字风险；因 EX 无法执行（①），
**未实测**是否被 Doris 接受，不在此断言。

> **已实测销账（2026-09-15 工作项 12）**：Doris 接受裸别名 `rank`——排名类样本
> `gold-076/077/177/178` 真链执行通过并完成 EX 锚定（`eval/reports/ccb4c8b.json`），
> 保留字风险实测消除，不改别名。

> **列名逐字补强（2026-09-16，P3 工作项 8 真链复跑）**：零售域 rank 问句（「2001
> 年品类销售额排名」）实测 `kind=answer`、返回 `columns=['total_sales_price','rank']`
> ——裸别名不仅被 Doris 接受执行，且在真实结果集中**逐字保留**（不折叠、不改写）。
> 补齐「可执行 ≠ 列名逐字」的最后一格：消费方按列名逐字定位，该断言现由实测覆盖。

⑧ **对外声明缺失**：`README.md` 与 `README.en.md` 对时间智能/同比/环比/comparison
grep 零命中。这不构成 N2 违规（未声称已实现），但能力登记表（README §11）缺该项。

⑨ **跨年季度/月环比方向反转（未修，登记）**：composite 两期谓词已正确扩为
`(1999, Q4) OR (2000, Q1)`，但外层 `ORDER BY <粒度列>` 单键丢年份序。实测
（2026-09-15，`ccb4c8b`）：`2000 年第一季度销售额环比` 输出 `[[1, 13407348.02,
null], [4, 37991488.51, 13407348.02]]`——2000Q1 行 prev=NULL、prev 值错挂在
1999Q4 行（正确前驱 1999Q4=37991488.51）。无金样本触发（3 条 pop 样本均同年
两期）。修法待评估：外层排序键补年份或合成 `year*10+period` 键。

⑩ **yoy/pop + 非时间 filter 丢失（未修，登记）**：`_rebuild_where_with_in` 整替换
WHERE 时未保留 filter 谓词。实测：`只看城市 Midway 的 2001 年销售额同比` 的产物
WHERE 仅剩时间 IN（残留 `dim_store` JOIN 仅起到去掉孤儿外键行），2001=`84992249.00`
（Midway 正确口径 `70561714.74`、全量 `86183391.24`）。无金样本触发（yoy/pop 样本
带 filter 的 0 条）。修法待评估：重建时把非时间谓词 AND 合入。

⑪ **cumulative + 维度未支持（未修，登记）**：外层不投影维度列、窗口无
`PARTITION BY`，各类别行混算且 LIMIT 被打满。实测：`按类别统计 2001 年各月累计
销售额`（100 行均为 d_moy≤10 的类别枚举行，累计值为跨类别混算）。无金样本触发
（2 条 cumulative 样本均无维度）。修法同 ① 的维度分支形态（`_wrap_with_cumulative`
加 `PARTITION BY <dims>`）。

---

## 什么情况下应该推翻

- Guard 加了 CTE 名豁免后（`check_tables` 识别 `exp.With` 的 CTE 名并排除，同时
  **仍校验 CTE 内部物理表**以防绕过），四 kind 仍无法在 Doris 执行 → 说明窗口函数
  形态与 Doris 方言不兼容，改自连接（yoy/pop）+ 应用层累加（cumulative），并重新
  评估可解释性损失；
- 出现"相对时间同比"的真实业务需求 → 必须先推翻 ADR-0014 ③，代价是评测不可复现，
  需引入"运行时快照 + 结果不锚定"的独立评测口径（与 ADR-0019 辨析）；
- 触发词需按客户/行业定制 → 外置到 `semantic/synonyms/patterns_<locale>.yml`
  新增 `comparison` 段（ADR-0015 ②），本 ADR 决策 ① 的硬编码常量作废；
- 时间智能需要跨指标比较（如"交易额同比 + 佣金同比"同句）→ `ComparisonSpec`
  单字段结构不足，需扩展为 per-metric 列表，届时本 ADR 决策 ③ 被推翻。

---

## 验证方式

**现状可复现（无需 DB、无副作用）**：

- `python -m eval.runner --dry` → finance 73/73、retail 23/24，失败样本仅 `gold-076`；
  > **（2026-09-16 收口起此读数更新**：`gold-076` 口径裁定并重锚后 retail **24/24**，
  > 失败样本归零——见代价 ⑤ 裁定落地注与判据 5 落地注。**）**
- Guard 阻断：`compile()` → `enforce()` 逐 kind 实测，得 ① 的表格（3 拒 1 通过）；
  > **（2026-09-15 起此实测已翻转**：四 kind 全通过，原命令不再复现「3 拒」——
  > 保留原文作为立项时证据，当前口径见判据 1/2 落地注。**）**
  **2026-09-14 由 ADR-0025 独立复现**（双域 × 5 形态共 10 组合，用
  `eval.runner.build_budget()` 从 `data/snapshots/a11d779.meta.json` 构造真实预算，
  29 张白名单表）：`yoy`/`pop` 拒于 CTE 名 `base`、`cumulative` 拒于 `monthly`、
  `plain`/`rank` 通过——与本表逐条一致。注意 `check_tables`（`sql_guard.py:368-369`）
  在 `allowed_tables` 为空时**直接 return**，用默认 `Budget()` 复现会得到全通过的
  假信号（ADR-0025 背景 3 已登记该陷）；
- 引用可达：`grep -rn "ADR-0017"` 的 9 处全部指向本文件。

**P-1 批次修复后的验收判据（全部可脚本化）**：

1. `check_tables` 对 CTE 名豁免，且 `tests/test_sql_guard.py` 新增契约：CTE 名不判
   违规、**CTE 内部物理表仍受白名单约束**（防用 CTE 包装非白名单表绕过 N3）；

   > **已落地（2026-09-15 工作项 11）**：实现落 `sql_guard.py` 的 `_cte_names` /
   > `_is_cte_reference` 两个助手 + `check_tables` 跳过「裸名（无 catalog/db）
   > 且命中 CTE 定义名」的 Table 引用；CTE 内部物理表仍在同一 AST 上照常遍历
   > 校验。**豁免只认裸名**的边界由测试锁住：`FROM xdb.base` 带前缀照常拒。
   > **同根因扩大一处（如实登记）**：`estimate_cost` 的无 stats 分支也把 CTE 名
   > 计为扫描表（多 1/8，与 stats 分支口径不一——stats 查不到 CTE 名本就贡献 0），
   > 同批修正并锁 `test_ignores_cte_names`。**测试 5 例**（`TestCteNameExemption`：
   > lag 形态 / CTE 链 / `assertIn("atlas.dwd.secret")` 的拒因正例 / 限定名反例 /
   > 无白名单早退不变），先写红 5 例（含 3 subfailed）后转绿。
   > **变异 4 次全红且 md5 全复原**：M1 删豁免 → 红 `test_cte_chain_passes` /
   > `test_cte_hidden_table_still_rejected` / `test_lag_shape_passes`（+`test_all_four_kinds_pass`
   > 的 3 subfailed）；M2 豁免放宽到限定名 → 红 `test_qualified_name_not_exempt`；
   > M3 `estimate_cost` 回退 → 红 `test_ignores_cte_names`；M4 跳过 CTE 内部表 →
   > 红 `test_cte_hidden_table_still_rejected`（M1/M4 双变异分别锁住两个方向：
   > 豁免缺失与豁免过度）。

2. 四 kind 全部通过 `enforce`；

   > **已验（2026-09-15 工作项 11）**：`TestComparisonKindsPassGuard` 用真实
   > Compiler 产物 + 快照 `a11d779` 的 29 表白名单预算实测四 kind 全通过
   > （cost 各 **0.250**；修复前 yoy/pop 拒于 `base`、cumulative 拒于 `monthly`）。
   > 另有全样本预检（compile → enforce，不执行、不连 DB）：**106 样本 0 拒**
   > （修复前 9 拒 = `gold-172~176` + `gold-072~075`，全部为 CTE 误拒）。
   > 本判据只到 Guard 面（AST）；Doris 真链执行属判据 4（工作项 12）。

3. `runner.py` 的 `enforce` 移入 `try`（或单独捕 `UnsafeQuery` 记为 blocked 态），
   整轮 eval 不因单样本 Guard 拒绝而中断；

   > **已落地（2026-09-15 工作项 12）**：`eval/runner.py::evaluate` 的 `enforce`
   > 拉入 `try`、捕 `UnsafeQuery` 记 `guard: "blocked"` + `guard_reason` 后提前返回
   > （**SQL 未落库**，与执行失败的 `error` 键分开——`exec_errors` 语义不含安全拒绝）；
   > `summarize` 增 `guard_blocked` 计数（两态分列）。契约测试落 `tests/test_runner.py`
   > （3 例：blocked 不冒泡且无 `error` 键 / 执行失败仍记 `error` 的回归锁 / 汇总分开
   > 计数，打桩 `enforce` 抛 `UnsafeQuery`——若豁免前形态回归该例即红）。真跑侧证据：
   > `eval/reports/ccb4c8b.json` 双域 `guard_blocked: 0`、`exec_errors: 0`（判据 1/2
   > 落地后无现实触发，机制面向未来样本）。
4. `make eval` 真跑成功，**13 条非歧义样本** EX 锚定（`result_hash`/`snapshot_sha`
   回填；实测为 `gold-172~178` + `gold-072~077`），产出 `eval/reports/<sha>.json`
   覆盖 106 样本；
   另 **2 条歧义样本**（`gold-179`「交易额同比」、`gold-078`「销售额同比」，均
   `ambiguous: true`）按决策 ② 走澄清、**无结果行故无 EX 可锚定**，其修法是
   **改字段而非跑评测**：`result_hash` 由 `<待执行后填写>` 改为 `null`、
   `snapshot_sha` 回填澄清行为实测时的快照、tags 补 `ambiguous` 且 `clarify` →
   `clarification`（对齐既有 7 条歧义样本的写法；实测对照表见 ADR-0023 代价 ④）。

   > **纠错（2026-09-14 二次实测）**：本判据初稿写「15 条样本 EX 锚定」，把
   > 「`snapshot_sha` 为占位符的样本数」（13 + 2 = 15）误当成「可 EX 锚定的
   > 样本数」。该错误已下游传导为 ADR-0023 决策 ④/判据 5 的分母 **99**
   > （正确为 **97** = 已锚定 84 + 可锚定 13），两处已同批修正。
   > 教训：歧义样本的占位符是**永不可能被兑现的承诺**（它们设计上就不执行），
   > 故「占位符个数」与「可锚定个数」在含歧义样本的批次里**必然不相等**。

   > **已落地（2026-09-15 工作项 12）**：真跑两轮 `make eval`，锁定快照
   > `ccb4c8b`（与 `a11d779`/`dc4f350` 同数据多锁，指纹一致）。13 条非歧义样本
   > 锚定完成（`gold-172~178` + `gold-072~077`）；2 条歧义样本按上文纠错段修字段
   > （`result_hash` → `null`、tags 补 `ambiguous`/`clarification`，不跑执行）。
   > 报告 `eval/reports/ccb4c8b.json`（106 样本）：finance `plan_acc 73/73`、
   > `clarify 6/6`、`ex 73/73`；retail `plan_acc 23/24`（唯一失败 `gold-076`，
   > 见代价 ⑤，口径待裁定）、`clarify 3/3`、`ex 24/24`；双域 `exec_errors 0`、
   > `guard_blocked 0`，第二轮全 pass。
   >
   > **真链锚定暴露并修复 3 类编译器缺陷**（先写测试红后转绿：`tests/test_compiler.py`
   > +4 例、文件 30 例全绿；13 条 × 6 轮稳定性矩阵全 STABLE）：
   > ① `gold-173`（按分支统计 2015 年交易额同比）六轮六 hash、`gold-073` 六轮五 hash
   > ——yoy/pop + 维度三重复合缺陷：`LAG` 无 `PARTITION BY` 跨维度串算 + base 的
   > `ORDER BY 时间 LIMIT 100` 把 200+ 维度值 × 2 期截成单期（实测 100 行全是 2014）
   > + 外层仅按时间列排序、同年全并列。修复：`_apply_lag` 维度分支去 base 截断与
   > 排序，新 `_wrap_with_lag_by_dims`（`PARTITION BY <dims> ORDER BY <time>`）；
   > 无维度形态字节不变（7 条已锚定样本走历史路径重跑一致）。
   > ② `gold-074`（2001 年第二季度销售额环比）空结果——composite 谓词把
   > `_time_value_to_int` 的**年值**放进季列（`d_qoy IN (2001, 2001)`，恒不命中）；
   > 修复 `_build_in_predicate` 为 OR-of-ANDs 逐期 `(year = Y AND col = V)`。
   > ③ `gold-177`（按分支统计 2015 年交易额排名）无外层 `ORDER BY`、LIMIT 100 打满时
   > 截断点随扫描序；修复 `_apply_rank` 默认 `ORDER BY rank DESC, <dims>`（Doris 接受
   > SELECT 别名排序，真链实测）。
   >
   > **同批发现 3 项未修缺陷**（均无金样本触发，登记为代价 ⑨⑩⑪，修复留后续批次）。
5. `gold-076` 口径裁定后 retail Plan Acc 达 24/24；
6. `make report` 重生成 `EVAL_REPORT.md`，绑定新 sha 与 106 样本口径；

   > **已落地（2026-09-16 P-1 收口）**：判据 5 = `gold-076` 口径裁定为「语义纠正：
   > `s_store_sk`」并完成落地与重锚（对照数据与机制见代价 ⑤ 裁定落地注）；
   > `make eval` 重跑 106 样本，retail `plan_acc 24/24`、`ex 24/24`（`gold-076`
   > plan_ok + ex 双过）。判据 6 = `make report` 重生成 `EVAL_REPORT.md`，
   > 绑定 `ccb4c8b`、retail 24/24，与 `eval/reports/ccb4c8b.json` 同源。测试账：
   > `make test` 690 → **693**（+3 = `TestStoreDimensionResolution`）；
   > `ruff check .` 27 / `mypy` 39-13-47 对照基线不变。

7. `docs/atlas_query_test_cases.md` §12 与 E-03 按代价段 ② 的纠错同步修订。

   > **已落地（2026-09-15 工作项 12）**：§11 E-03 从「被 Guard 拦截（exit 3）」改写为
   > 「CTE 已豁免、直出同比（exit 0）」，实测行值 `2000=86269529.76` /
   > `2001=86183391.24`（prev=86269529.76）；说明块与命令示例同步（含历史 blocked
   > 行为与豁免批次的时态区分）。§12.4 从「query 路径被拦截」改写为**残余边界三形态**
   > （实测口径，与代价 ⑨⑩⑪ 同源）：filter 丢失（`只看城市 Midway 的 2001 年销售额
   > 同比` 2001=`84992249.00` vs Midway 口径 `70561714.74`）、跨年季度环比方向反转、
   > 累计+维度混算。§12.6 的 `[-1]` 旧口径改写为三级解析（ADR-0019 决策 ①）。
   > §13 实跑数字更新为 **2026-09-15：通过 36 / 失败 0 / 跳过 0**（33 ok + 3 clarify，
   > E-03 由 blocked 移入 ok）；顶部诚实声明补 `ccb4c8b` 复跑行；附录条数 89 → 106
   > （finance 79 + retail 27）。配套脚本 `docs/run_query_cases.sh` 的 E-03 预期码
   > `run 3` → `run 0` 后重跑，36/0/0 全绿（修订前一轮实测 35/1，唯一失败即 E-03
   > 预期码过时——该 35/1 本身是新行为的取证）。

**证伪条件**：若判据 1~3 全部达成后 EX 仍无法锚定（Doris 拒绝该窗口形态或结果
不稳定），则决策 ① 的窗口函数形态被推翻，转备选方案第 2/3 行。

> **2026-09-15 工作项 12 注**：锚定过程中确实出现「结果不稳定」读数（`gold-173`
> 六轮六 hash、`gold-073` 六轮五 hash），但 6 轮矩阵定位根因为 yoy/pop 编译形态的
> 三处缺陷（判据 4 落地注 ①）而非 Doris 窗口函数语义——修复后 13 条 × 6 轮全
> STABLE，**未触发本证伪条件**；窗口函数形态在 Doris 真链可执行（判据 4 报告）。
