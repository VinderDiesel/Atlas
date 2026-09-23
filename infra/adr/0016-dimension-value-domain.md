# ADR-0016：维度值域注册——从"人工探查"变成"快照派生"

- 日期：2026-09-09
- 状态：accepted（决策经用户确认，随 P1 批次 B4 落地）
- 相关：ADR-0010（评测先行）、ADR-0014 ①（filter 解析）、ADR-0015（locale 词典）、
  `data/value_profile.py`、`agent/value_domain.py`、`semantic/values/`、
  `semantic/lint.py` 第 5 项 `[values]`、`agent/planner.py::_resolve_filter_value`

---

## 背景

filter 要求精确值，但 planner 对维度值没有任何值域注册。已有实证代价：

1. **静默漏匹配**：客户问"只看城市 MIDWAY 的 2000 年销售额"，planner 解析出
   `s_city = 'MIDWAY'`，但真实数据里是 `'Midway'`（首字母大写）。SQL 执行返回
   0 行，**不报错**——用户以为"该城市没有销售额"，实际是值写法不对。
2. **人工探查不可复现**：零售样本因 SF0.1 全库单州无区分度，两次（P4 gold-059、
   P6 gold-067）把州过滤降级为城市过滤，靠 `eval/gold/retail/data-profile.md` 人工
   探查。客户环境的"华东 / 华东大区 / hd / 大小写"会让 filter 同样静默漏匹配。
3. **别名无法归一**：交易所 `NASDAQ` 在旧文档里常写作 `NSDQ`，planner 把 `NSDQ`
   当作字面值传给编译器，编译出的 SQL 是 `ExchangeID = 'NSDQ'`，结果 0 行。

约束：

- **确定性优先**（AGENTS.md §2）：值域必须是数据驱动的规则，不引入 LLM 猜测。
- **宁缺不猜**（KL #29）：无法确定性归属的值必须澄清，不静默改写。
- **快照纪律**（ADR-0001）：值域只能来自已锁快照，不能连实时库——否则"权威口径"
   随数据漂移，评测失去意义。
- **安全默认**：不为无证据的列制造反问。未注册 / 大基数跳过的列必须原样透传，
   否则既有 89 条样本（Branch 高基数、Tier 整数、metric 阈值列不参与维度值域）
   会大面积回归。

---

## 决策

### ① 值域从锁定快照派生，不手写

新增 `data/value_profile.py`（`make profile-values`）：对语义模型注册维度
（`SemanticModel.dimension_synonyms` 的键，planner 可寻址的 dim_* 非时间字段）
的**编译器绑定列**（`find_field` 首匹配），从当前锁定快照执行 `SELECT DISTINCT`
生成 `semantic/values/<model>.<field>.json`。

- 采集前后复核锁定快照指纹（数据指纹不一致 → 拒绝生成）。初版委托 `data/snapshot.py --check`；2026-09-22 修复支持显式已锁快照，见下文实施补充。
- 每条 SQL 过 `sql_guard.enforce`（N3 红线）+ Guard 预算（表白名单 = 锁定快照内的
  表，与 `make eval` 同一个 `build_budget` 口径）。
- 大基数（`distinct > --max-cardinality`，默认 200）的列记 `status: skipped`，
  不注册 values（planner 对该列不校验，安全默认）。
- 人工 `aliases` 是文件里唯一的手写字段，重新生成时原样保留并校验别名目标
  仍在新值域内（悬空别名 → 响亮报错，不静默放行）。

### ② 值归属四态：exact / alias / case / unknown

planner filter 值归属阶段加一道校验（`Planner._resolve_filter_value`）：

| 命中形态 | 行为 | 记录 |
|---|---|---|
| exact（大小写敏感精确命中值本体） | 通过，**不改值** | 不记 |
| alias（casefold 命中人工别名表） | 归一到别名目标 | `ValueNotice` |
| case（casefold **唯一**命中值本体） | 归一到值本体 | `ValueNotice` |
| unknown（既非值也非别名，或 casefold 多命中） | `ClarificationRequest` 附候选值样例 | 不记 |
| unregistered（profile 缺失 / skipped） | 原值透传，不校验 | 不记 |

- casefold 多命中（实测 Gender 列 `F`/`f`、`M`/`m` 并存）→ 不归一，反问不猜。
- 归一记录通过 `Planner.plan_with_notices()` 暴露；`plan()` 保持历史签名不变
  （CLI / HTTP / 图路由 / 529 条契约测试零改动）。
- 反问形态复用 `ClarificationRequest`（kind=ambiguous），与 KL #29 同一条原则，
  零新机制——候选值按频次降序取前 10 个，用户可直接改成其中一个重问。

### ③ 快照锁定焊死

`semantic/lint.py` 增第 5 项 `[values]` 检查：

- 每个 `semantic/values/*.json` 的 `snapshot_sha` 必须等于当前最新锁定 meta 的 sha
  （按 `created_at` 取最大）。漂移即红，错误信息指向 `make profile-values`。
- `model` 必须是已知 ossie 模型名；`field` 必须是该模型 dim_* 数据集的非时间字段。
- registered 值域的 `source_table` 必须在最新锁定快照的表白名单内。
- 目录不存在或为空 → 不报错（值域未启用，完整性检查在 tests/）。

纪律：重新锁快照后**必须**重跑 `make profile-values`，否则 lint 红。
值域文件的 `snapshot_sha` 与最新锁定 meta 一致 = 值域派生自当前权威数据。

### ④ 绑定列如实登记，不改编译器

`SemanticModel.find_field` 按 datasets 顺序首匹配。实测金融 `Status` 同时存在于
`fact_trades` 与 `dim_account`，绑定的是先出现的 `fact_trades`（唯一值 `Completed`），
而非直觉的 `dim_account`（active/closed）。

本批不改编译器首匹配规则——该事实如实记进 profile 的 `bound_dataset` 与 `note`
字段（"同名字段出现在 N 个数据集，编译器按 datasets 顺序绑定首个"）。
planner 值域校验按 compiler 实际绑定列做，不凭直觉跨表。

---

## 后果

### 正面

- **静默漏匹配 → 显式澄清**：`只看城市 MIDWAY 的 2000 年销售额` 经 case 归一为
  `'Midway'`（记录 ValueNotice）；`只看类别 Toys 的 1999 年销售额`（Toys 不在值域）
  反问并列出 10 个候选值。
- **别名归一可追溯**：`只看交易所 NSDQ 的 2013 年佣金收入` 归一为 `NASDAQ`，
  Agent 解释链路可展示"你说 NSDQ，我按值域理解为 NASDAQ"。
- **快照纪律焊死**：值域与最新锁定快照绑定，`make lint` 漂移即红，杜绝"值域过期
  但没人发现"的口径风险。
- **零回归**：未注册 / skipped 列原样透传，既有 89 条样本零影响。

### 负面

- **值域是快照态而非实时**：数据重新装载后必须重跑 `make profile-values`，否则
  lint 红。写入 README Known Limitations 新条。
- **同名维度字段首匹配**：编译器首匹配规则不改，意味着 `Status` 这类跨数据集
  同名字段的值域可能不是直觉的那张表。ADR-0016 §④ 如实登记，不掩盖。
- **大基数列不校验**：`Branch`（2715 distinct）等被跳过，planner 对该列不做值
  校验——合法值与非法值都透传，静默漏匹配风险仍在。未来若需要，可考虑
  前缀匹配 / 模糊匹配扩展，本批不纳入。

---

## 实施补充：隔离工作区重采样（2026-09-22）

M0 开发前发现派生值域仍绑定 `7c966e9`，最新锁定快照为 `1e2e557`。
用户授权只读重采样，不修改 Snapshot、Gold set、Metric 定义或业务数据库。

- `data.value_profile.generate`/CLI 新增 `snapshot_sha`/`--snapshot-sha` 与
  `raw_dir`/`--raw-dir`；默认继续使用 `git_short_sha()`，不自动选择最新快照。
- 显式身份只接受 7–40 位小写十六进制；必须存在对应普通 meta 文件，不接受符号链接。
- 复用 `eval.analysis_eval.verify_snapshot_fingerprint` 的数据指纹口径，真实测量经
  `data.snapshot.build_meta(raw_dir=...)`；显式原始目录不存在时拒绝。
- 前后均复核指纹，且 meta 内容必须与采集前一致；所有列和人工别名校验成功后才写入。
  画像文件不接受符号链接；数据库中断、悬空别名或指纹失败不会留下部分刷新的文件。
  多文件落盘不是文件系统事务，写盘阶段失败仍需复核并重新生成。
- 实际 dry-run 与生成均完成：21 列中注册 10、跳过 11；生成后 `make lint` 通过。
  全部文件只有快照身份、生成时间及说明中的身份变化，值本体、计数、别名未变。
- 回归覆盖 `tests/test_value_profile_generation.py`；本次不改下述历史验收口径，也不
  宣称执行了新的 EX/Plan Acc 评测。工作树 HEAD 无同名快照，旧 `make eval` 仍未运行。

## 验收

- 新增黄金集样本（实现前 dry 基线已记录）：
  - `eval/gold/finance/gold-171.json`：别名命中（NSDQ → NASDAQ）
  - `eval/gold/retail/gold-068.json`：值不在值域 → 期望反问
- `tests/test_value_domain.py`：值域加载契约（tmp fixture）+ resolve 四态 + 绑定列冲突。
- `tests/test_planner.py` 新类：别名归一 + 记录、unknown → 澄清带候选、case 归一
  `MIDWAY` → `Midway`、casefold 歧义 Gender f/F → 反问、skipped 列透传、metric
  阈值不受影响。
- `make lint && make test && make eval && make baseline && make report` 全链绿。
- 目标态：finance Plan Acc 66/66（含 gold-171）、retail 反问 2/2（含 gold-068）。
