# B3b 细化设计：英文形态触发词外置的**正则等价性对照表**

对应决策：`infra/adr/0015-scenario-decoupling-locale.md` §②（B3a 中文页见同目录
`adr-0015-pattern-lexicon-zh.md`）
本批纪律同 B3a：**纯搬运不改语义**——任何"顺手优化解析"都不允许混入（AGENTS.md §8）。
中文页的四条通用约定（常量名与类型不变 / 模式串整串搬运 / 缺节即报错 / 对象同一性
断言）在本页**全部适用**，不再重复；本页只写英文侧特有的差异与新增机制。

---

## 1. 搬运范围（en 形态层，`agent/planner.py` 字面量）

| # | 代码常量（现值位置） | 目标词典位置（`patterns_en_us.yml`） | 搬运单元 | 等价性证明方式 |
|---|---|---|---|---|
| 1 | `_EN_RELATIVE_TIME`（11 词，含空格词组） | `time.relative_reject.words` | 字符串列表逐字 | 内容基线断言（11 条）；`TestEnglishGoldPlanner` 相对时间用例不改断言 |
| 2 | `_EN_QUARTER_RE`（Q2 2013 / 2013 Q2 两向） | `time.patterns[]` 中 `kind: quarter` | 完整正则字符串 | 既有用例（gold-061/066 形态）不改断言 |
| 3 | `_EN_MONTH_RE`（12 月份名 + `re.IGNORECASE`） | `time.patterns[]` 中 `kind: month`：`pattern`（月份名交替串整串）+ `flags: [IGNORECASE]` | 完整正则字符串（**不再由 `_EN_MONTHS` 拼接**） | ① 与 #4 的交叉不变量测试：交替串里的 12 个月份名 == `months` 表键（数量与逐名）；② 月份名互不为前缀 → 交替顺序不影响匹配（测试断言该前提，前提破了就要显式改设计）；③ `May 2014` / `may 2014`（小写）两用例 |
| 4 | `_EN_MONTHS` + `_EN_MONTH_NUM`（月份名 → 序号） | `months`（`<小写月份名>: 1..12` 映射） | 映射逐字 | 12 键与取值逐条基线断言；#3 的交叉不变量 |
| 5 | `_EN_YEAR_PREP_RE`（介词引导年份） | `time.patterns[]` 中 `kind: year_prep` | 完整正则字符串 | 既有用例不改断言 |
| 6 | `_EN_YEAR_BARE_RE`（无介词裸年兜底） | `time.patterns[]` 中 `kind: year_bare` | 完整正则字符串 | 既有用例不改断言 |
| 7 | `_EN_THRESHOLD_WORDS`（9 词，裸年兜底**封锁表**） | `time.threshold_gate.words` | 字符串列表逐字（含 `more than` 等空格词组） | 内容基线断言；"over 5000" 不被读成年份的用例不改断言 |
| 8 | `_ISO_DATE_RE` / `_ISO_QUARTER_RE`（中英共享） | `time.patterns[]` 中 `kind: iso_date` / `iso_quarter`，写 `ref` 不写 `pattern` | **引用** zh 词典的同一形态（不复制第二份，见 §2.1） | 加载期解析引用；断言 en 侧取到的正则与 zh 侧是**同一对象** |
| 9 | `_EN_GROUP_RE` | `grouping.pattern` | 完整正则字符串（含截断 lookahead） | 既有 en 分组用例不改断言 |
| 10 | `_EN_TOP_N_RE` / `_EN_TOP_N_DIM_RE` | `topn.pattern` / `topn_dim.pattern` | 两条完整正则字符串 | 既有 TopN 用例（"top 3 categories by sales in 1999"）不改断言 |
| 11 | `_EN_ONLY_RE` / `_EN_EXCL_RE` | `filter_include.pattern` / `filter_exclude.pattern` | 完整正则字符串（含实体复数词裁剪与 lookahead） | 既有 filter 用例（gold-063/067 形态）不改断言 |
| 12 | `_EN_THRESHOLD_GT_RE` / `_EN_THRESHOLD_LT_RE` | `threshold.greater.pattern` / `threshold.less.pattern` | 完整正则字符串 | 既有阈值用例（含 `million/billion/K/M/B`）不改断言 |
| 13 | `_EN_UNIT`（6 键：三词 + 三字母后缀） | `magnitude.en_units` | 映射逐字（大小写键共存） | 键与倍率逐条基线断言 |
| 14 | `_EN_FOLLOWUP_WHAT_RE` / `_EN_FOLLOWUP_INSTEAD_RE` | `followup.what.pattern` / `followup.instead.pattern` | 两条完整正则字符串 | 既有指代追问用例 + `make e2e` S7 多轮场景 |

**不搬运**：解析算法本体（`_parse_time_en` 的分支结构、澄清判定、值归属裁剪逻辑）。

## 2. 英文侧的两个新增机制（都为"不复制第二份 / 不重组正则"服务）

### 2.1 `ref`：共享形态引用

`(\d{4})-(\d{2})-(\d{2})` 与 `(\d{4})\s*[Qq]([1-4])` 与语言无关，B3a 已落在 zh 词典。
英文词典若照抄一遍，就成了同一形态的两个可写来源（改一处漏一处）。故 en 的
`time.patterns` 条目允许 `{kind, ref}` 形式：`ref` 指向 zh 词典 `time.patterns` 里的
同名 kind，加载期替换为**同一已编译对象**。规则收紧到最小：

- 一个条目要么有 `pattern`、要么有 `ref`，两者互斥且必居其一；
- `ref` 只能指向 zh 词典已声明的 kind，指向不存在的 kind = 加载失败；
- `ref` 不携带 `pattern`，因此不允许"先抄一份再用 ref 校对"这种半搬运动作。

中文词典里的 ISO 形态在 planner 侧原本还有一层 `_ISO_DATE_RE` / `_ISO_QUARTER_RE`
别名（B3a 留下的，**只**为英文 if-chain 服务）。英文改走自己的有序表后，别名成了
死代码，随本批删除；"中英同源"这一事实改由对象同一性断言锁定
（`test_planner_en_constants_come_from_lexicon`：英文表里取到的就是中文词典那个对象）。

### 2.2 `flags`：唯一的大小写不敏感形态

12 个月份名是全仓唯一需要 `re.IGNORECASE` 的形态（客户会写 `may 2014`）。flags 是
**声明式**属性而非重组逻辑，故放行；但只加在 en 校验器上——中文形态不需要 flags，
zh 校验器保持"恰含 kind 与 pattern"的严格形态，不把未经验证的能力摊开。
允许的 flag 名封闭为 `IGNORECASE / MULTILINE / DOTALL / VERBOSE`，其余报错。

## 3. 顺序契约（英文与中文的差异要写清）

`_parse_time_en` 的尝试顺序（ISO date → en quarter → ISO quarter → 月份名 → 介词年 →
裸年兜底）原本就写在函数体的 if-chain 里，**不是**中文那种"变量书写顺序"的隐性依赖。
本批把它同样改为有序 `time.patterns` + kind 分发表，目的是让"顺序"这个语义资产与
中文侧同源可评审；函数体的 if-chain 换成循环后行为逐字等价，唯一细节：

- `year_bare` 的 handler 需要句内上下文（含阈值词则不启用），故 en 侧 handler 签名是
  `(question, match) -> TimeSpec | None`，返回 `None` = 本形态不启用、继续下一形态。
  `year_bare` 是表内最后一项，因此"继续"必然落到函数尾部的 `return None`，与原实现
  完全一致（不是语义扩展，是同一分支的另一种写法）。

## 4. 验收（B3b）

1. `make lint`（含 `[authority]`）全绿；
2. `make test` 全绿，且**既有中/英 planner 用例一条都不改断言**（含
   `TestEnglishGoldPlanner` 全部用例与 `test_compiler.py`/`test_mcp_server.py` 的英文问句）；
3. `python -m eval.runner --dry` 双域非 EX 维度与 b933e20 逐样本逐字段一致；
4. `make eval`（非 dry，真实 Doris）：`EX` finance `65/65`、retail `18/18`、`exec_errors 0`，
   并与 b933e20 逐字段比对（含出口 SQL 与结果 hash）；报告随本批入库；
5. `make e2e`（plan 指定的 B3 收尾项）：5 场景 + handoff 多轮追问无回归。

### 4.1 实测结果（2026-09-09，commit `40b71e2`）

| 验收项 | 结果 |
|---|---|
| 1 `make lint` | ✅ 4 项全绿（`[authority]` 已覆盖 `patterns_en_us.yml`） |
| 2 `make test` | ✅ 502 → **521 全绿**（`tests/test_locale_patterns.py` 新增 19 例）；既有中/英 planner 用例**断言零改动**（`tests/test_planner.py` 不在本批改动集内，116 例直接通过） |
| 3 dry 一致性 | ✅ 与 `eval/reports/b933e20.json` 非 EX 维度逐样本逐字段相等，差异仅 `created_at` / `sha` / `dry` 三处元数据：finance `65/65` + `5/5`（zh 57/57 + 5/5、en 8/8）、retail `18/18` + `1/1`（zh 13/13 + 1/1、en 5/5） |
| 4 非 dry EX | ✅ `EX` finance `65/65`、retail `18/18`，`exec_errors 0`（`eval/reports/40b71e2.json`）；剔除 sha/时间戳与 `samples[].domain`（`9cb8913` 新增字段）后**全量报告零差异**，89 条出口 SQL 与逐行结果 hash 全等 |
| 5 `make e2e` | ✅ 7/7 场景执行完成（S1–S7 含 handoff 与多轮追问）；与历史报告 `eval/reports/e2e-acceptance.json` 逐字段比对，差异**仅** `created_at` 与各场景 `latency_ms`（kind / metric / SQL / row_count / result_hash / guard 全等）→ 无回归；产物存新文件 `eval/reports/e2e-acceptance-40b71e2.json`，**不覆盖历史报告** |

两个必须如实说清的地方：

1. `make e2e` 的 `summary.passed` 是"脚本跑完即计数"（`run_scenario` 不吞异常，
   逐场景断言在场景函数里），因此真正的无回归证据是**与历史报告逐字段比对**，
   不是 `7/7` 这个分数；
2. `eval/e2e_acceptance.py` 的 `SNAPSHOT_META` 硬指向 `7d48dcb.meta.json`（零售装载
   前的历史锁），所以新报告的 `snapshot_sha` 标签沿用该值——非本批引入，登记为
   后续修正项（今日数据指纹与 `b933e20` / `40b71e2` 锁逐字段相等，结果本身不受影响）。

## 5. 本表能证伪什么

- #3/#4 的交叉不变量若断言失败 → 月份名在 pattern 与 months 表里漂移了（双源征兆）；
- 任一既有英文用例需要改断言才能通过 → 搬运引入了语义变化，本批回退重做；
- 非 dry 全量报告与 b933e20 出现 SQL 文本或结果 hash 差异 → 同上（这是最强探针）。
