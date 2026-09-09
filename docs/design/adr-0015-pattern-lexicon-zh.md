# B3a 细化设计：中文形态触发词外置的**正则等价性对照表**

对应决策：`infra/adr/0015-scenario-decoupling-locale.md` §②
本批纪律：**纯搬运不改语义**——任何"顺手优化解析"都不允许混入（AGENTS.md §8）。
因此本表逐条给出「代码现值 → 词典目标值 → 等价性如何被机械证明」，实现前定稿、
实现后按表复验。

---

## 1. 搬运范围（zh 形态层，`agent/planner.py` 字面量）

| # | 代码常量（现值位置） | 目标词典位置 | 搬运单元 | 等价性证明方式 |
|---|---|---|---|---|
| 1 | `_DATE_RE` `_ISO_DATE_RE` `_QUARTER_RE` `_ISO_QUARTER_RE` `_MONTH_RE` `_YEAR_RE` | `patterns_zh_cn.yml: time.patterns[]`（**有序列表**，每项 `kind` + `pattern`） | 完整正则字符串逐字复制 | ① `kind` 白名单（缺 unknown kind → 加载失败）；② 列表顺序 = 原尝试顺序（结构即顺序，消除"变量名暗示顺序"的隐性依赖）；③ 新增用例：对 6 种时间形态各 1 条问句断言粒度与取值（含 `2013Q2` 无空格、`2013 Q2` 有空格两条） |
| 2 | `_RELATIVE_TIME`（12 词） | `time.relative_reject.words` | 字符串列表逐字 | 长度与内容基线断言（12 条，顺序无关，命中即反问） |
| 3 | `_GROUP_RE` | `grouping.pattern` | 完整正则字符串 | 既有分组用例（中/英各若干）不改断言 |
| 4 | `_TOP_N_RE` | `topn.pattern` | 完整正则字符串 | 既有 TopN 用例不改断言 |
| 5 | `_EQ_FILTER_RE` | `filter_include.pattern` | 完整正则字符串（含 `(?=的\|，\|,\|$)` 截断逻辑） | 既有等值 filter 用例（gold-149/150/153/155 形态） |
| 6 | `_EXC_FILTER_RE` | `filter_exclude.pattern` | 完整正则字符串 | 既有排除用例（gold-152 形态） |
| 7 | `_THRESHOLD_GT_RE` / `_THRESHOLD_LT_RE` | `threshold.greater.pattern` / `threshold.less.pattern` | 完整正则字符串 | 既有阈值用例（含 `万/千万/亿` 组合，gold-151/154 形态） |
| 8 | `_CN_UNIT`（亿/千万/百万/万） | `magnitude.cn_units`（映射） | 键值对逐字（**JSON 整数允许下划线分隔**：`10_000_000`） | 长形优先由 regex 交替保证（词典侧不改正则，只改词表来源）；映射值逐条断言 |
| 9 | `_CN_NUM`（一二三四 → 1..4） | `magnitude.cn_numerals` | 映射逐字 | 季度中文编号用例（`第X季度`）不改断言 |
| 10 | `_FOLLOWUP_PREFIXES`（6 词） | `followup.prefixes` | 字符串列表逐字 | 既有指代追问用例（S7 场景 + planner 单测） |
| 11 | `_FOLLOWUP_DIM_RE` | `followup.dim_pattern` | 完整正则字符串 | 同上 |

**不搬运**：解析算法本体（匹配顺序之外的分支逻辑、澄清判定、值归属），
`_ISO_DATE_RE`/`_ISO_QUARTER_RE` 与英文共享但**归属 zh 词典**（英文侧 B3b 复用同一份
`time.patterns` 里的 ISO 形态，不复制第二份，避免双源）。

## 2. 为什么"完整正则字符串"而不是"词表 + 代码重组"

形态词的 regex 里含截断 lookahead、非贪婪锚定、`$` 边界等**语义关键部分**。
若把词表拆出来在代码里重组（`f"(?:{'|'.join(words)})"`），重组函数本身就成了
新的正确性负担（转义、顺序、边界）——那是"优化"，不是"搬运"。
故：词表类（相对时间词/前缀词/量级映射）按列表搬运，**带语法结构的模式串整体搬运**。

## 3. 加载契约（`agent/compiler.py::load_locale_patterns`）

- 封闭 locale 注册表（与 `load_locale_synonyms` 同一约定）；
- 顶层节白名单：`time` / `grouping` / `topn` / `filter_include` / `filter_exclude` /
  `threshold` / `magnitude` / `followup`，**缺节 = 报错**（B3a 要求 zh 词典完整，
  静默缺节会让某类形态在无人察觉时不再解析）；
- 模式串必须可 `re.compile`，失败即报错并带节名；
- `time.patterns[].kind` 必须落在解析器已实现的 kind 集合内（一致性断言在 planner 侧：
  `{词典 kind} == set(_TIME_DISPATCH)`，不等即导入失败）；
- 结果缓存（进程内一次读取），**返回缓存对象本身**——不深拷贝，因为 planner 在模块
  导入时绑定常量且 `test_planner_constants_come_from_lexicon` 靠对象同一性断言证据；
  因此约定调用方**只读**（不得就地改词典派生结构）。

## 4. 验收（B3a，plan 原文 + 本表补充）

1. `make lint`（含 `[authority]`）全绿；
2. `make test` 全绿，且**既有中/英 planner 用例一条都不改断言**（改了就是语义变化）；
3. `python -m eval.runner --dry` 双域与 b933e20 逐域逐语言一致：
   finance `65/65` + `5/5`（zh 57/57+5/5、en 8/8）、retail `18/18` + `1/1`（zh 13/13+1/1、en 5/5）、`exec_errors 0`；
4. `make eval`（非 dry，需 Doris）在既有快照复验 `EX 65/65` + `18/18`、0 执行错误，
   报告随本批 commit 入库（文件名 = 绑定 sha）；
5. `make e2e` 多轮场景无回归（B3b 收尾时统一跑，见 plan 的 B3b 验收）。

### 4.1 实测结果（2026-09-09，commit `7051ef6`）

| 验收项 | 结果 |
|---|---|
| 1 `make lint` | ✅ 4 项全绿（含 `[authority]`） |
| 2 `make test` | ✅ 481 → **502 全绿**（新增 `tests/test_locale_patterns.py` 21 例）；既有中/英 planner 用例**断言零改动**（`git show` 核实：`tests/test_planner.py` 本批不在改动集内） |
| 3 dry 一致性 | ✅ 非 EX 维度与 `eval/reports/b933e20.json` 逐样本逐字段**完全一致**：finance `65/65` + `5/5`（zh 57/57 + 5/5、en 8/8）、retail `18/18` + `1/1`（zh 13/13 + 1/1、en 5/5） |
| 4 非 dry EX | ✅ `EX` finance `65/65`、retail `18/18`，`exec_errors 0`（`eval/reports/7051ef6.json`）；**全量报告比对（含 sql / hash / row_count / columns）与 b933e20 逐字相等**，89/89 条 SQL 文本一致 |
| 5 `make e2e` | ✅ 随 B3b 收尾统一跑：7/7 场景完成，与历史报告逐字段比对差异仅时间戳与 `latency_ms`（详见 `adr-0015-pattern-lexicon-en.md` §4.1） |

附：两份报告的唯一结构差异是 `samples[].domain`——该字段由 `9cb8913`（晚于
b933e20）在 runner 里添加，与本批无关（已 `git merge-base --is-ancestor` 核实）。
快照侧：`data/snapshots/7051ef6.meta.json` 与 `b933e20.meta.json` 除 sha/时间戳外
**逐字段相等**（29 表同一指纹）→ 未重建数据，仅按纪律在新 sha 上重锁。

## 5. 本表能证伪什么

若第 1 节任一条的"等价性证明方式"失败（尤其 ③：时间形态用例需要改断言才能通过），
说明搬运引入了语义变化——本批回退重做，不允许以"顺带修好"的名义继续。

第 4.1 节的实测结果中，"全量报告逐字相等"是本表最强的证伪探针：它同时覆盖
Plan 结构与出口 SQL 与执行结果 hash，任何解析侧的隐性变化（包括顺序变化）
都会在此暴露，而不需要额外发明对比机制。
