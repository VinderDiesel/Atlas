# ADR-0025：图表 spec 接入响应链与时间轴列事实源

- 日期：2026-09-14
- 状态：accepted（决策经用户确认，2026-09-14；落地批次 **P3**，其中 UI 部分**硬阻塞于 P-1**，见决策 ⑤。逐批工作卡见 `docs/design/dev-plan-0017-0025.md`）
- 相关：`agent/tools/chart.py`（spec 生成器，无调用方）、`agent/compiler.py:991/1031`（时间列别名规则）、
  `agent/graph.py:330-376`（`node_execute`，唯一执行通道）、`tests/test_chart.py`（16 用例）、
  README KL #21（`README.md:676-678`；行号随 README 增行漂移，以 `grep -n "^21\. \*\*" README.md`
  定位。本 ADR 初稿曾记为 `:671-673`，2026-09-14 复测已漂 +5 行）；
  ADR-0002（Git 唯一事实源）、ADR-0015（双权威源禁令，`0015:66`）、
  ADR-0017（时间智能；代价 ① Guard 阻断 CTE、⑦ `rank` 裸别名）、
  ADR-0018（前端控制台；决策 ⑦ 渲染顺序、理由段「spec → 组件的确定性映射」）、
  ADR-0021（决策 ②「沿 `time_dimension` 的既有解析先例」）、
  ADR-0022（HTTP 契约 v2；判据 6 断言 `explanation` 13 固定键）

---

## 背景

### 1. 现状：spec 生成器存在、被测试覆盖、但无调用方（2026-09-14 实测）

```
grep -rn render_chart --include="*.py"   → 仅 agent/tools/chart.py 自身 + tests/test_chart.py
grep -n chart serving/api.py agent/graph.py agent/state.py agent/cli.py → 0 命中
grep -c "def test" tests/test_chart.py   → 16
```

`chart.py:22-23` 的 docstring 自己已声明意图：「返回结构化 spec（数据即已执行 rows 的
子集），不做像素渲染；**前端渲染是 serving 层职责**」。ADR-0018 决策 ⑦ 也已把图表
锁定在渲染顺序末位（指标口径 → 行数/耗时 → 出口 SQL → 数据 → 图表），并在理由段写明
「前端只做 **spec → 组件的确定性映射**，不参与任何图表类型决策——决策在 `chart.py`」。
即：**上下游都已假定 spec 在响应里，而它不在**。这是 ADR-0018 背景表已登记的缺口，
本 ADR 裁定如何补齐。

> 纠错（ADR-0018 理由段）：该段称「后端 spec 已含 type/x/y/**series** 与 `sql_sha256`」。
> 实测 `grep -rn series agent/tools/chart.py tests/test_chart.py` = **0 命中**，spec 无
> `series` 键。0018 未提交，已随本 ADR 同批修正该处（见「代价与限制」⑤）。

### 2. spec 的实际键集（实测 `chart.py:171-179` / `:183-190` / `:233-242`）

| 分支 | 恒定键 | 条件键 |
|---|---|---|
| `bar` / `line` | `type` `x` `y` `data` `sql_sha256` `skipped`（恒 0） | `note`（`:241-242`，仅 `note_parts` 非空时出现） |
| `table` | `type` `columns` `rows` `sql_sha256` `note` `skipped` | — |

两处形态差异必须进契约：`x` 是 **string**（`"(行序)"` 或列名）而 `y` 是 **list**
（`[y_col.name]`）；`bar`/`line` 有 `data` 无 `columns`/`rows`，`table` 反之。
`y` 已是列表形态但 `data` 只有单 `y` 值——这是潜在的多序列预留，见「代价」③。

### 3. 缺陷实测：时间轴列识别在两域都失效，且后果比「bar 而非 line」严重

实验方法（**合成行 + 真实编译产物列名 + 真实 Guard 预算**）：用
`Compiler.compile()` 产出 SQL，以 `sqlglot` 取外层 SELECT 的 `alias_or_name` 作为
`columns`，`rows` 按真实类型构造（时间列 `int`、度量 `Decimal`，对齐 `chart.py:80-81`
记录的 Doris `SUM(decimal)` 形态），Budget 由 `eval.runner.build_budget()` 从
`data/snapshots/a11d779.meta.json` 构造（29 张白名单表，`dialect=doris`）。
**未连 DB 执行**——`rows` 是构造值，故本实验只能证明**轴选择逻辑**的行为，
不构成任何 EX / 数据正确性声明（N1）。

| 域 | comparison | 外层 SELECT 列名（实测） | Guard | `render_chart` 输出 |
|---|---|---|---|---|
| finance | 无 | `total_trade_value` | PASS | `bar`，x=`(行序)`，y=`total_trade_value` |
| finance | `yoy` | `calendaryearid`, `total_trade_value`, `prev_period_value` | **BLOCKED**：表不在白名单内：`base` | `bar`，x=`(行序)`，**y=`calendaryearid`** |
| finance | `pop` | 同上 | **BLOCKED**：`base` | 同上 |
| finance | `cumulative` | `calendarmonthid`, `total_trade_value`, `cumulative_value` | **BLOCKED**：`monthly` | `bar`，**y=`calendarmonthid`** |
| finance | `rank` | `total_trade_value`, `rank` | PASS | `bar`，y=`total_trade_value` |
| retail | 无 | `total_sales_price` | PASS | `bar`，x=`(行序)`，y=`total_sales_price` |
| retail | `yoy` / `pop` | `d_year`, `total_sales_price`, `prev_period_value` | **BLOCKED**：`base` | `bar`，**y=`d_year`** |
| retail | `cumulative` | `d_moy`, `total_sales_price`, `cumulative_value` | **BLOCKED**：`monthly` | `bar`，**y=`d_moy`** |
| retail | `rank` | `total_sales_price`, `rank` | PASS | `bar`，y=`total_sales_price` |

Guard 阻断 3/4 kind **独立复现了 ADR-0017 代价 ①**（sqlglot 把 CTE 名解析为
`exp.Table`，`sql_guard.check_tables` 逐个查白名单）。注意 `check_tables`
（`sql_guard.py:368-369`）在 `allowed_tables` 为空时**直接 return**——用默认
`Budget()` 做实验会得到全 PASS 的假信号，本表已用真实快照预算。

从上表得出三条事实：

**(a) `type="line"` 在确定性编译路径上不可达。** `compiler.py:690-691` 对
`field.is_time` 的维度**直接抛 `CompileError`**（「时间字段请使用 Plan.time」），
故无 comparison 时 SELECT 里根本没有时间列；有 comparison 时时间列被别名成小写
（见 (b)）仍不命中。10 行实验里 `type` 全是 `bar`。`line` 分支当前**只被
`tests/test_chart.py:61` 的手写 fixture 触发**（该 fixture 用列名 `CalendarYearID`）。

**(b) 金融域也失效，根因是别名小写化，不是「frozenset 少了零售列名」。**
`compiler.py:991` `time_alias = time_col.lower()`（`:1031` 的 cumulative 同）把
`CalendarYearID` 变成 `calendaryearid`，而 `chart.py:101` 是**精确大小写的集合成员
判定** `time_like = name in _TIME_COLUMN_NAMES`。于是 frozenset 里那 4 个金融列名
（`CalendarYearID`/`CalendarQtrID`/`CalendarMonthID`/`DateValue`，逐字来自
`atlas_finance.ossie.yaml:334-343`）在编译产物中**永不出现**。全仓 grep 证实
`CalendarYearID` 作为结果列名只出现在 `tests/test_chart.py:61`；在编译产物里它只
出现在 WHERE 谓词（`tests/test_compiler.py:55` `CalendarYearID = 2013`）。
零售 4 列（`d_year`/`d_qoy`/`d_moy`/`d_date`，`atlas_retail.ossie.yaml:83-92`）
本就全不在集合内（`chart.py:39-40` 注释已自承「零售结果集列名识别待实测扩展」）。

**(c) 后果不是「画错图型」，是「画错量」。** `chart.py:164` 把 `time_like` 列排除出
y 候选；因时间列未被识别，它成了 `numeric_cols[0]`，于是 **y 轴 = 年份/月份编号本身**
（`y=['d_year']`），真正的度量 `total_sales_price` 与 `prev_period_value` 双双落进
`note` 的「未渲染」清单。图表会画出一条 2012/2013/2014 的柱子，标题写着销售额。
`note` 在字面上是诚实的（确实声明了未渲染哪些列），但**图形本身是错的**，且这类错误
不会抛异常、不会红测试——属静默误导，比 `ChartError` 更危险。

**(d) 附带：模块 docstring 与代码不符。** `chart.py:14-15` 声称「x 为时间列
（dim_date 物理列或**列名含 date/year/quarter/month 等时间词**）→ 折线」，而实现是
精确集合成员判定，**无任何子串/词形匹配**（`d_year` 含 `year`、`d_date` 含 `date`，
均 `time_like=False`）。这是文档声称了代码没有的行为，须随本 ADR 修正（N2 邻接）。

### 4. 事实源盘点：三处读同一事实，一处规则只活在代码里

| # | 事实 | 位置 | 是否源自 Git 唯一事实源 |
|---|---|---|---|
| 1 | 字段级「这是时间列」 | ossie `dimension.is_time` → `Field.is_time`（`compiler.py:113`，解析于 `:168`） | ✅ |
| 2 | 模型级「粒度 → 物理列」 | ossie `custom_extensions.time_dimension.columns` → `SemanticModel.time_dimension`（`compiler.py:158`，解析于 `:217-227`） | ✅ |
| 3 | 图表用「哪些列名算时间轴」 | `chart.py:41-43` `_TIME_COLUMN_NAMES` 硬编码 frozenset（6 项） | ❌ **脱离 Git 事实源** |
| 4 | 时间列进 SELECT 时的**实际别名** | `compiler.py:991` / `:1031` 的 `.lower()` | ⚠️ 规则只存在于代码，无任何出口 |

1 与 2 是同一 Git 事实源在不同粒度上的两次读取（合规）；**3 是唯一的第三套事实源**，
与 ADR-0021 决策 ①「同一事实不得有两处权威定义」和 ADR-0015 的双权威源禁令同源冲突。
4 是本 ADR 新识别的问题：**即使把 3 改成读 ossie（消除第三套源），仍会失败**——因为
消费者拿到的是 `CalendarYearID` 而结果集里是 `calendaryearid`，要匹配就必须**复制
`.lower()` 规则**，那又制造了第四个副本。这是「读权威源」这一直觉方案的非显然陷阱。

### 5. 约束

AGENTS.md：N1（合成行实验不得当作 EX 声明）、N2（不得把「spec 已在响应里」写成现状）、
N3（图表只读已执行结果，`chart.py:5-7` 的「无执行 SQL 引用的裸数据一律拒绝」是该红线
在图表层的体现，本 ADR 不得削弱）、§10 优先级 4（确定性优先：轴选择必须是确定性规则，
不得按值特征猜测）、§10 优先级 5（不过度设计）、§11 第 1 步（先 ADR）、§12（不确定选型
写 ADR 不擅自决定）；ADR-0018 决策 ⑦（渲染顺序）与理由段（前端不做图表决策）。

---

## 备选方案

争议点是**「哪些结果列是时间轴」这一事实由谁产出**。

| 方案 | 优势 | 劣势 |
|---|---|---|
| **① 由 compiler 产出实际别名，经状态携带到渲染点；`chart.py` 零名称推断（选定）** | 消除第三套事实源，且不复制 `.lower()` 规则（单一调用点）；生产者即别名规则的拥有者，天然一致；事实进 checkpoint 可审计「本轮把哪列当时间轴」；`chart.py` 退化为纯函数，前端「spec → 组件」映射的前提（0018 理由段）才真正成立 | 需给 `TurnState` 加一个 `str \| None` 字段并改 `render_chart` 签名；跨 `compiler`/`graph`/`chart` 三模块，比只改 `chart.py` 大 |
| ② `chart.py` 从 ossie `time_dimension.columns` 读（照 ADR-0021 决策 ② 先例） | 消除第三套源；沿既有解析先例，姿态正确 | **仍会失败**：读到的列名未经 `.lower()`，必须在 chart 层复制别名规则 → 制造第四套副本；且 `render_chart(execution)` 的 `ExecutionLike`（`chart.py:50-64`）只有 sql/rows/columns，**拿不到 model**，须把 `SemanticModel` 传进图表工具，让 `agent/tools/` 反向依赖 `agent/compiler` 的模型类型 |
| ③ 扩 `_TIME_COLUMN_NAMES` 补零售 4 列 + 金融 4 列的小写变体（共 14 项） | 改动最小（一个 frozenset）；`test_chart.py` 16 用例零改动 | 第三套事实源不但保留还**变大**；每加一个语义模型/改一个粒度列名都要改 Python（ADR-0015 ② 外置化的反向）；`.lower()` 规则仍被复制；`make lint` 覆盖不到 |
| ④ 前端按列名推断时间轴 | 后端零改动 | 直接违反 ADR-0018 理由段「前端不参与任何图表类型决策」；把语义判断搬到 TS 侧即制造跨语言第四套源；违反 §10 优先级 4 |
| ⑤ 按值特征推断（数值单调递增 / 值域像年份 → 判为时间轴） | 无需任何名称事实 | 是**猜测**，违反 §10 优先级 4 与全仓「宁缺不猜」口径；`rank` 列（1,2,3…）与 `d_moy`（1..12）都单调递增，会把排名列画成时间序列 |

---

## 决策

### ① 时间轴列由 compiler 产出，`chart.py` 不做任何名称推断

`_TIME_COLUMN_NAMES` 的权威地位**取消**。改为：

1. `Compiler` 暴露只读纯函数 `emitted_time_column(plan: Plan) -> str | None`，返回
   **本轮编译产物中时间列的实际别名**（无时间列则 `None`）；
2. `.lower()` 规则收敛到**单一调用点**：`compiler.py:991` 与 `:1031` 改为调用该函数
   取别名，而不是各自内联 `time_col.lower()` / `sub_col.lower()`——即规则只写一遍，
   公开访问器与 SQL 生成共用同一实现（这是方案 ① 相对 ② 的关键收益：**不复制规则**）；
3. `node_execute`（`graph.py:365-376` 的返回字典）新增 `"time_column": compiler.emitted_time_column(plan)`；
4. `render_chart` 签名改为 `render_chart(execution, *, time_columns: Collection[str] = ())`，
   轴选择只依据入参；**入参为空时回落 `_TIME_COLUMN_NAMES`**（见 ③）。

`mode: single`（金融）与 `mode: composite`（零售）**不需要不同的轴推断**：两模式的差异
只在「粒度 → 哪个物理列」，而这已由 `time_dimension.columns[granularity]` 统一表达
（`compiler.py:968-972` / `:1023-1027` 都走 `columns.get(granularity)`）。故本 ADR
**不为 composite 增任何特例分支**——差异吸收在既有声明里，不进图表层。

### ② chart spec 挂 `_turn_payload` 独立 `chart` 键，不进 `explanation`，不进 checkpoint

- **独立 `chart` 键**：`explanation` 的 13 固定键已被 ADR-0022 判据 6 用作
  「`/plan/execute` 与 `/ask` 同构」的断言对象；把 spec 塞进去会让该判据的键集随图表
  变更漂移，且 `explanation` 语义是「归因」（为什么是这个数），而 spec 是「呈现」
  （怎么画），两者混一层会让 0018 决策 ⑦ 的渲染顺序在数据结构上无从表达。
- **计数纪律**（沿本批确立的「公式 + 撰写时实测值 + 重测时点」）：`chart` 使
  `_turn_payload` **+1 键**。撰写时实测 19 键（`serving/api.py:183-213`）→ 本 ADR 落地后
  **20 键**；ADR-0019 落地再 +2（`snapshot_sha`/`snapshot_bound_to_head`）→ **22 键**。
  落地当日重测，不等于预期值先归因再改断言。
- **`chart` 不写入 `TurnState`**：只有 ① 的 `time_column`（一个 `str | None`）进状态。
  spec 是 `rows` + `columns` + `sql` + `time_column` 的确定性函数，可重算；把派生数据
  写进审计轨迹会让 checkpoint 体积随 `rows` 近似翻倍，并新增「状态里的 spec 与重算结果
  不一致」这一原本不存在的失败模式。渲染点为 `turn_from_state`（`graph.py:502`），
  CLI 与 HTTP 由此**共用单一调用点**。
- **无需动 msgpack 白名单**：`_CHECKPOINT_SERDE`（`graph.py:100-107`）的
  `allowed_msgpack_modules` 只登记**自定义类**（4 项：`TimeSpec`/`OrderSpec`/`Plan`/
  `ClarificationRequest`）。`chart` spec 是纯 `dict`，`time_column` 是 `str | None`，
  均不需注册；`Decimal` 早已在状态序列化面内（`TurnState.rows` 声明为
  `tuple[tuple[Any, ...], ...]`，`state.py:55`，Doris 返回 Decimal 见 `chart.py:80-81`）。
  故「spec 进白名单会增加序列化面」这一顾虑**不成立**，本 ADR 予以关闭。

### ③ `_TIME_COLUMN_NAMES` 降级为**兜底**，并修正 docstring 的不实声称

保留该 frozenset，但语义从「权威判定」降为「**非编译路径的兜底**」，适用面仅限：
`--llm` 候选链产出的任意合法只读 SQL（其 SELECT 可能直出物理列名，如
`sql/dwd/dim_date.sql:47` 的 `calendar_year_id AS CalendarYearID`）、以及直接喂
`execute_readonly` 输出的调用方。相应改动：

1. `chart.py:39-43` 注释重写：明确「本集合**不是**时间列权威源；编译路径的时间列由
   `Compiler.emitted_time_column` 携带（ADR-0025 ①）。本集合仅兜底非编译路径的裸物理
   列名」；
2. `chart.py:14-15` 的 docstring 修正：删去「列名含 date/year/quarter/month 等时间词」
   ——实现是精确集合成员判定，无子串匹配（背景 3(d)）。**不得**为了圆这句话去加子串
   匹配：那会让 `update_date`、`years_held` 一类列被误判为时间轴，是拿一个新缺陷掩盖
   一处文档错误；
3. 兜底路径的存在必须**在 spec 里可见**：走兜底（`time_columns` 入参为空且命中
   frozenset）时，`note` 追加「时间轴按列名兜底识别（非编译器声明）」——让读者知道
   这一轴的权威性等级较低。这是把 0022 决策 ⑦ 的诚实性标志位思路用到图表层。

### ④ 图表点数上限无需新增：`MAX_CATEGORIES` 已经约束

`chart.py:170` 在 `len(rows) > MAX_CATEGORIES`（200，`:35`）时**直接降级为 table**，
故 `bar`/`line` 的 `data` 长度**恒 ≤ 200**；`MAX_TABLE_ROWS`（500，`:37`）只约束
**降级后的表格**展示行数，与图表点数无关。因此「500 点的折线在浏览器里不可读」这一
顾虑在当前代码下**不可达**，本 ADR 不新增任何点数上限（§10 优先级 5：不过度设计）。
前端侧对应义务：`type === "table"` 时必须渲染 `note` 与 `skipped`，不得把降级表格
再画成图（ADR-0018 决策 ⑦ 的「数据」与「图表」两档在此合一）。

### ⑤ 批次归属：P3，且**阻塞于 P-1**

- 图表接入排 ADR-0018 的 **P3**（沿 `docs/design/frontend-console-plan.md` §2 面板 5）；
- **硬阻塞**：`yoy`/`pop`/`cumulative` 当前被 Guard 拒绝（背景 3 表，复现 0017 代价 ①），
  故这三类的时间序列图表在 **P-1 修好 CTE 白名单之前无法端到端验证**。P-1 之前只允许
  落**契约测试**（轴选择的纯函数断言，用编译产物列名 + 合成行，如背景 3 的实验形态），
  **不允许**落 UI——否则界面会展示一个只能画 `plain`/`rank` 两种形态的「图表能力」，
  而这两种形态恰恰**都不含时间列**（背景 3(a)），即 P1/P2 阶段上线的图表面板 100% 出
  `bar`，构成对「支持时间序列图表」的隐性声称（N2）；
- 本 ADR 与 ADR-0024（Cube 导出目标）**无顺序依赖**：0024 决策 ②「查询路径逐字不变」，
  而本 ADR 只动 compiler 的别名收敛与状态携带，不动 Guard、不动只读红线、不动 compiler
  的 SQL 语义（①的 2 是**等价重构**：别名值逐字不变，判据 3 以 `git diff` + 编译产物
  逐字比对断言）。

> **硬阻塞已解除（2026-09-15，P-1 工作项 11）**：Guard 的 CTE 名豁免已落地
> （ADR-0017 判据 1：四 kind 全过 `enforce`、106 样本预检 0 拒——见 0017 验证方式
> 落地注）。本节「P-1 之前只允许契约测试、不允许落 UI」的限制**随之过期**，P3 可落
> UI。但三态区分（N2）不变：「Guard 面通过」≠「Doris 真链可执行」——判据 4 的真链
> 验证仍须 P3 本批实测（含 Doris 列名与保留字两风险，见代价 ④⑥）。

### ⑥ 不扩多序列：`yoy`/`pop` 的图表只画当期

KL #21（`README.md:676-678`）已声明「多数值列只渲染第一个」。本 ADR **保持该限制**，
并显式登记其后果：`yoy`/`pop` 的结果集含 `prev_period_value`（`cumulative` 含
`cumulative_value`），但图表只画当期度量，前期值仅出现在 `note` 的「未渲染」清单里。
即**图表无法承载「比较」这一表达**，同比/环比的结论只能读数据表。理由：扩多序列要把
`data` 从 `[{x, y}]` 改为 `[{x, <seriesName>: value}]`，是 spec 的破坏性变更，牵动
16 个测试用例 + 前端映射 + 本 ADR ② 的键集断言，而收益仅在 comparison 场景（立项时
3/4 kind 还被 Guard 挡着——**2026-09-15 起已不被挡**：P-1 工作项 11 落地 CTE 豁免，
真链可执行性待 P3 实测）。属触发条件到期再评估（见「什么情况下应该推翻」）。

---

## 理由

1. **方案 ② 是直觉解但会失败**——这是本 ADR 存在的核心理由。「别硬编码，去读 ossie」
   在绝大多数场景是对的（ADR-0021 决策 ② 就是这么做的），但这里读到的列名与结果集里的
   列名之间隔着一层 `.lower()` 别名规则，而该规则**只活在 `compiler.py:991`**。任何
   消费者要么复制它（第四套副本），要么让生产者把结果交出来。选后者。
2. **单一调用点 > 等价断言**：把 `:991`/`:1031` 改为调用公开访问器，规则物理上只写一遍，
   不需要靠测试去守两份实现的一致性。判据 3 的逐字比对仍然保留，但它的角色从「防副本
   漂移」降级为「防回归」——这是更强的保证形态。
3. **`time_column` 进状态是可审计性收益，不是负担**：一个 `str | None` 让「本轮把哪一列
   当时间轴」成为 checkpoint 里可回溯的事实。当图表画出令人困惑的形状时，这是第一个
   要看的字段。对照 spec 本身进状态：那是派生数据，重算即可，进状态只增体积与不一致面。
4. **兜底保留而非删除**：删掉 `_TIME_COLUMN_NAMES` 会让 `--llm` 候选链与 ad-hoc
   `execute_readonly` 结果失去时间轴识别（这类 SQL 直出物理列名，确实命中现有 6 项）。
   保留 + 降级 + 在 `note` 里标注权威等级，比「一刀切删除」更诚实，也让 16 个既有测试
   零改动通过（§10 优先级 5）。
5. **P3 + 阻塞 P-1 是防止「界面先于能力」**：若把图表排进 P1/P2，上线的将是一个只能画
   `bar` 的图表面板（背景 3(a)：这两种可执行形态都不含时间列）。用户看到「图表」标签
   即会假定时间序列可用——这是 N2 的隐性形态，比写错文档更难发现。

---

## 代价与限制

① **改动跨三模块，且 `render_chart` 签名变更是破坏性的**
`compiler.py`（新增访问器 + 收敛 2 处 `.lower()`）、`graph.py`（`node_execute` 返回值
+ `turn_from_state` 渲染点 + `TurnState` 新字段）、`state.py`（`TurnResult` 新增
`chart` 与 `time_column`）、`serving/api.py`（`_turn_payload` +1 键）、`chart.py`
（签名 + 注释 + docstring + `note` 追加）。`render_chart` 的调用方当前只有
`tests/test_chart.py`（16 用例，实测 `grep -c "def test"`），因 ③ 保留兜底，**16 用例
预期零改动通过**——但这是预期，须以判据 5 实测确认，不得先声称。

② **`time_column` 只描述「编译器声明的那一列」，不覆盖 LLM 候选链**
候选链（`--llm`）产出的 SQL 不由 `Compiler.compile()` 生成，`emitted_time_column`
对其返回 `None`，故该路径**只能走 ③ 的兜底**，权威等级较低且会在 `note` 里如实标注。
这是设计使然：候选链的 SQL 形态不受语义层约束，无从声明。

③ **多序列缺失使 comparison 图表的表达力受限**（决策 ⑥）
`spec["y"]` 已是 `list` 形态（`chart.py:236`）但 `data` 只有单 `y` 值——这个不一致
**保留**（改它即破坏性变更）。后果：`yoy` 的图看起来像单序列柱状图，读者若不读 `note`
会以为「同比」没有同比项。缓解是 `note` 恒在（多数值列必触发 `:195-197`），
但 `note` 是文字，不是图形。

④ **P-1 未落地前，本 ADR 的时间轴修正无法端到端验证**
背景 3 表显示 `yoy`/`pop`/`cumulative` 全被 Guard 拒。故判据 4 的「真链验证」在 P-1
之前**不可能绿**，只能落判据 1~3、5 的静态/契约层。本 ADR 落地批次必须晚于或等于
P-1；若先落，须在 commit message 里显式声明「真链判据未验，待 P-1」（N1）。

> **前置已满足（2026-09-15，P-1 工作项 11）**：Guard 面已解（见决策 ⑤ 落地注），
> 「落地批次晚于或等于 P-1」的约束**已可满足**；本条余下部分（判据 4 真链验证须
> P3 本批实测）继续有效。

⑤ **ADR-0018 理由段的 `series` 声称是错的，已同批修正**
实测 spec 无 `series` 键（背景 1 纠错框）。0018 未提交，直接改正文；同时其背景表
「时间序列误判为 bar」的表述**弱于实测**（真相是 (a) `line` 不可达 + (c) y 轴画成
年份本身），一并改为本 ADR 的口径。

⑥ **`rank` 列的裸别名风险不因本 ADR 消除**
ADR-0017 代价 ⑦ 已登记 `RANK() OVER (...) AS rank` 的保留字风险且**未实测**（当时
因 ① 无法执行）。本 ADR 实测 `rank` kind **可通过 Guard**（背景 3 表），故该风险现在
可测——但**本 ADR 不测**（超出范围，且需连 Doris）。登记为移交项：P-1 修好 CTE 后
应一并实测 `rank` 在 Doris 的可执行性，回填 0017 代价 ⑦。

> **移交状态（2026-09-15）**：P-1 工作项 11 已修 Guard 面（CTE 豁免），`rank` 与三
> CTE kind 的 Doris 可执行性实测**仍未做**——归 P3 批次（dev-plan §2.8 移交项：
> 须在工作项 8 的同一次 Doris 连接里顺手取证），届时回填 ADR-0017 代价 ⑦。

> **移交完成（2026-09-16，P3 工作项 8，retail 域真链复跑）**：可执行性面已由 P-1
> 工作项 12 先行销账（`rank` 随排名类样本 EX 锚定，0017 ⑦ 注）；本批补齐列名面——
> ① **判据 4 通过**：yoy 问句（「2001 年销售额同比」）实测 `kind=answer`、
> `columns=['d_year','total_sales_price','prev_period_value']`，
> `emitted_time_column` 在结果列中**逐字命中 = True**，**未触发**推翻条件第 2 条
> （`time_column` 写入点保持编译后）；② **移交项落定**：rank 问句（「2001 年品类
> 销售额排名」）实测 `columns=['total_sales_price','rank']`，裸别名在结果集中逐字
> 保留，已回填 ADR-0017 代价 ⑦。

⑦ **图表能力仍未对外声明，README 能力登记表缺该项**
沿 ADR-0017 代价 ⑧ 的同款缺口：`README.md` / `README.en.md` 对 `render_chart` 的
grep 命中仅在 KL #21（限制段），能力登记表（README §11）无图表项。本 ADR 落地批次须
补登记，否则「有限制无能力」的表述会让读者误判该功能不存在。

> **补登记已完成（2026-09-16，P3 文档同步）**：README §11 新增图表项（spec 级
> 确定性渲染与接入）；README.en.md §9 实现状态行与 KL digest #35 同步；0018 的
> `series` 纠错注记于本 ADR 撰写时（2026-09-14）已落正文（`0018:225-234`），
> P3 批次在 0018 追加「落地注记：P3 批次」汇总证据。

---

## 什么情况下应该推翻

- **出现需要多序列的具名业务需求**（如「同比图必须同时画当期与前期两条线」）→ 推翻
  决策 ⑥，把 `data` 改为 `{x, <series>: value}` 形态。代价：spec 破坏性变更，16 个
  测试用例 + `_turn_payload` 键集断言 + 前端映射同批改；须同批更新 KL #21；
- **P-1 修好 CTE 白名单后，实测发现 Doris 返回的时间列名与编译产物别名不一致**
  （如 Doris 对未加引号别名做大小写折叠/保留原样）→ 决策 ① 的「生产者携带」仍成立，
  但携带的值须改为**执行器实测返回的列名**而非编译期别名，即 `time_column` 的写入点
  从 `node_execute` 编译后移到执行后（`graph.py:359` 之后），按 `columns` 做大小写
  不敏感匹配。此为本 ADR 最可能的推翻形态，判据 4 正是为捕捉它而设；
- **第三个语义模型域接入**（当前金融 single / 零售 composite 两域）且其 `time_dimension`
  出现第三种 `mode` → 决策 ① 末段「composite 无需特例」的结论须重验；若新 mode 的
  粒度列映射不是 `columns.get(granularity)` 形态，`emitted_time_column` 需按 mode 分支；
- **前端需要自行决定图型**（如用户手动在 bar/line 间切换）→ 推翻 ADR-0018 理由段
  「前端不参与任何图表类型决策」，须先改 0018 再改本 ADR ②（spec 需从「结论」变为
  「候选集」）。此路径会削弱确定性优先（§10 优先级 4），须给出补偿；
- **`_TIME_COLUMN_NAMES` 兜底被证明无真实使用者**（`--llm` 候选链上线后实测无一次
  命中）→ 推翻决策 ③，直接删除该 frozenset 与其 4 个死条目，`render_chart` 的
  `time_columns` 入参改为必填。判据 6 为此设探针。

---

## 验证方式

> 全部判据在 P3 落地批次执行；判据 4 依赖 P-1（决策 ⑤、代价 ④）。
> 本 ADR 撰写时的实验（背景 3）是**合成行**实验，只证轴选择逻辑，**不构成 EX 声明**（N1）。

1. **单一调用点断言（决策 ①2）**：`grep -c "\.lower()" agent/compiler.py` 在时间列
   别名语义上的命中数 = **1**（即只有 `emitted_time_column` 内部）；
   `tests/test_compiler.py` 新增用例断言
   `COMPILER.emitted_time_column(plan) == sqlglot.parse_one(COMPILER.compile(plan)[0]).expressions[0].alias_or_name`
   对 `{finance, retail} × {yoy, pop, cumulative, rank, plain}` **10 个组合逐条成立**
   （`plain`/`rank` 两侧均为 `None`，须一并断言，防「只测通过路径」）；
2. **契约测试：轴选择（决策 ①③）**：`tests/test_chart.py` 新增用例，用背景 3 表的
   **实测列名**（`calendaryearid`/`d_year`/`calendarmonthid`/`d_moy`）+ 合成行断言：
   传入 `time_columns={"d_year"}` 时 `spec["type"] == "line"` 且 `spec["x"] == "d_year"`
   且 `spec["y"] == ["total_sales_price"]`（**y 是度量不是年份**——这条直接锁住背景 3(c)
   的缺陷）；`time_columns=()` 且列名不在 frozenset 时 `spec["type"] == "bar"`；
   兜底命中时 `note` 含「兜底识别」字样（决策 ③3）；
3. **等价重构零漂移（决策 ①2、⑤）**：判据 1 的 10 个组合，重构前后
   `compile(plan)[0]` 的 SQL **逐字相同**（以 `git stash` 前后各跑一次并 diff 断言，
   或把 10 条 SQL 作为黄金字面量存进测试）；同时 `tests/test_compiler.py` 既有
   `CalendarYearID = 2013`（`:55`）等 WHERE 谓词断言**逐字未改**——证明 ① 只动了
   别名的**产生方式**，没动别名的**值**，也没动 SQL 语义（N3 邻接：Guard 出口不变）；
4. **真链验证（P-1 之后才可执行）**：连 Doris 跑一次 `retail` 的 `yoy` 问句
   （`atlas query "2013 年销售额同比" --domain retail`），断言
   `executor` 实测返回的 `columns` 中**确有**一列等于 `emitted_time_column` 的返回值
   （大小写敏感逐字比对）。**若不等**（Doris 折叠大小写）→ 触发推翻条件第 2 条，
   把 `time_column` 写入点后移，**不得**改成大小写不敏感匹配了事（那会掩盖真实列名
   事实，且让 `columns.index(x_col.name)`（`chart.py:213`）在真实结果上抛 ValueError）；
5. **零回归**：`make test` 全绿，其中 `tests/test_chart.py` 的 **16** 个既有用例
   **逐字未改**（以 `git diff tests/test_chart.py` 断言只增不改）——这是决策 ③ 保留
   兜底的直接验证；若为通过而修改了既有用例，说明兜底语义被削弱，须回改设计而非改测试；
6. **兜底死条目探针（推翻条件第 5 条）**：`--llm` 候选链上线后，在
   `eval/reports/` 的候选链样本里统计「走兜底且命中 frozenset」的次数。连续两个评测
   批次为 **0** → 触发推翻条件第 5 条，删除 frozenset。本判据是**延后执行**的探针，
   P3 批次只需在 `chart.py` 注释里登记该探针的存在与判据编号，不产出数字（N1）；
7. **契约键集（决策 ②）**：`tests/test_api.py`（或 0022 的 `test_api_contract_v2.py`，
   取落地时已存在者）断言 `_turn_payload` 键数 = 撰写时公式值（19 + 1 = **20**；若
   ADR-0019 已落地则 21 + 1 = **22**），且 `chart` 键在 `kind != "answer"` 时为 `null`
   而非缺失（沿 `api.py:184` docstring「字段全集稳定输出」的既有契约）；
8. **文档同步（代价 ⑤⑦）**：ADR-0018 理由段的 `series` 声称与背景表的「误判为 bar」
   表述已改（`git diff infra/adr/0018-frontend-console.md` 非空）；README 能力登记表
   （§11）新增图表项；KL #21 追加「时间轴列由编译器声明携带（ADR-0025），
   `_TIME_COLUMN_NAMES` 仅兜底非编译路径」，**不得删除或放宽 #21 既有任何一句**（N4）。

**证伪条件**：若判据 1 的 10 个组合中任一 `emitted_time_column` 与实际首列别名不符，
则「生产者携带」方案的基础假设（compiler 知道自己的输出列名）被证伪，须回退到方案 ②
并接受其 `.lower()` 副本代价；若判据 4 发现 Doris 返回列名与编译别名不一致，则「编译期
别名即结果集列名」被证伪，按推翻条件第 2 条处理。
