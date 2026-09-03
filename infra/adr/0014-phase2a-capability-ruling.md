# ADR-0014：Phase 2a 能力裁定（filter/指代消解 MVP 范围、相对时间设计性不支持、公开集对照收口）

- 日期：2026-09-03
- 状态：accepted（决策经用户确认；落地随 release-notes §4 后续路线推进批次）
- 相关：ADR-0010（评测方法论——快照绑定）、ADR-0011（安全分层——Guard 行级）、
  ADR-0012（HTTP 服务面）、ADR-0013（执行面规划）、docs/release-notes-v0.1.md §4

---

## 背景

release-notes-v0.1.md §4 后续路线六项在 v0.1 定格后推进，其中三项纯软件能力
（filter 解析 / 相对时间 / 指代消解）与一项安全扩展（Guard 跨表 join 注入）需先
裁定 MVP 范围再排期；公开集对照（BIRD/Spider）存在文档自相矛盾需仲裁；Polaris
行级下推项需评估登记。本 ADR 一次性记录裁定，避免实现中边做边改范围。

事实基线（HEAD=2b00fa7 核实）：compiler 已有 Filter 模型（column/op/value，
等值与比较）且 /compile 可透传（Planner 是唯一缺口）；gold 金融段零 filter 问句
样本；gold-104/121 为故意设计的相对时间歧义样本；gold schema 为单轮形态（无会话
载体）；sql_guard.py 注释已声明跨表谓词沿 join 图补表的 Phase 2 方案。

## 裁定

### ① filter 解析 MVP 范围（release-notes §4 行 4 之一）

**纳入**（Planner 确定性规则解析，不经过 LLM）：
- 维度值等值："只看/仅统计/只统计 <维度值>"
- 维度排除："排除/除…外/不含 <维度值>"
- 度量阈值："超过/大于/高于 N" 与 "低于/小于/不足 N"（HAVING 语义，问句必须已
  解析出 metric；N 支持中文量级词 万/千万/亿）
- 上述形态与既有显式分组、时间范围（TimeSpec）、TopN 可组合

**不纳入（→ ClarificationRequest，不猜测，AGENTS.md 决策优先级）**：
- 自由双指标比较（"佣金高于成交量的分支"）
- 维度值模糊无法命中语义层同义词
- 时间词并入 filter（时间一律走 TimeSpec；相对时间见 ③）

评测载体：gold-149~155 新增样本先行（7 条：6 可解析 + 1 歧义），其中 6 条解析
失败基线先于实现记录（评测先行，ADR-0010 口径）；Plan Acc 分母随样本扩张如实
机械转述（44 可解析 → 50 可解析），不修改历史报告。

### ② 指代消解 MVP 范围（release-notes §4 行 4 之二）

**纳入**：仅"同 metric 换时间/换维度"的结构补全——追问句命中链接词（"那…呢 /
换成 / 按…呢 / 改为"）或与上轮 Plan 同构的碎片时，复用上轮 Plan 的 metric/
dimension，仅替换本轮新解析片段，合并结果仍走既有确定性校验链。
**不纳入**：自由代词（"它/这些/上轮那个"）与指代不明的推断 → 澄清。
理由：确定性优先（不引入 LLM 猜测）；MVP 覆盖多轮会话最高价值路径（换时间/
换维度追问）。

评测载体：gold schema 为单轮形态，**不加会话结构**（schema 变更过重）；指代
能力由契约测试（合并/澄清/跨会话不泄漏）+ e2e 多轮场景（第 6 场景）覆盖，
报告机械转述（6/6 → 7/7 以实测为准）。

### ③ 相对时间：设计性不支持（release-notes §4 行 4 之三收口）

裁定为**设计性不支持**（不是"待实现"）：固定快照评测下相对时间必然漂移
（eval/gold/README.md 原设计理由，数据段 2012-07~2017-07；"上个月"随真实日期
变化无稳定语义），支持它需要引入评测时间锚定设计，与"评测绑定固定快照"
（AGENTS.md N6、ADR-0010）冲突。gold-104/121 保留为歧义反问样本（反问是正确
行为，是澄清机制的评测载体）。

### ④ 公开集对照收口：BIRD/Spider 不再新增接入

仲裁 release-notes §4 行 3（"判定历史对照，不新增"）与 eval/bird/README.md 旧版
（"待 text2sql 生成器可用后执行"计划）的自相矛盾——以**不新增**为准：
1. 无 text2sql 生成器且架构不对齐：Generator 只产出 Plan 候选，SQL 一律由确定性
   Compiler 生成；BIRD 需要问句 → SQL 直出能力对照，与确定性优先架构无契合点；
2. 数据与工程成本无收益：BIRD dev finance 体积大、需下载建库；
3. 公开集分数对企业场景无外推意义（AGENTS.md N10），gold 是主评测。

### ⑤ Polaris 行级下推：不适用项（C8 补记，2026-09-03）

**结论：Polaris 行级下推不适用——行级权限维持 Guard 谓词层为架构终局，
不重复造轮子。**

证据分级（如实区分实测与文档核对）：

1. **对象级授权 = 实测**（serving/rbac_verify.py + `polaris-rbac-7d48dcb.json`，
   2026-09-02）：principal → principal-role → catalog-role 两级角色，grant 单元最细
   到表（TABLE_LIST / TABLE_READ_DATA / TABLE_FULL_METADATA /
   TABLE_READ_PROPERTIES）；compose 对象模型注释一致（CATALOG / NAMESPACE /
   TABLE / PRINCIPAL / ROLE / GRANT）。
2. **无行级能力 = 官方文档核对**（2026-09-03 检索 polaris.apache.org
   access-control/policy 文档，in-dev main/unreleased 分支——未发布文档已是
   能力上界，发布版不超出）：securable object 全集 = Catalog / Namespace /
   Iceberg table / View / Policy；表级 privilege 十个全部为对象/数据级，无行过滤
   （行级谓词）项；Policy 框架系统类型仅 4 类生命周期规则（data-compaction /
   metadata-compaction / orphan-file-removal / snapshot-expiry），行/列级策略在
   官方路线图中（未发布）。

推论：跨表谓词能力（C7 已落地）与行级策略注入均属 Guard SQL 谓词层职责，
Polaris 只做对象级第二层（纵深防御第二层定位不变，ADR-0011 口径）；
KL #15 ① 同步收窄。Doris 4.1 per-user identity mode 属身份透传（真实用户
身份替代服务账号），不改变行级过滤位置。

> 本裁定为**文档核对级**（非实测否定）；若 Polaris 发布行级策略能力且引擎侧
> （Doris）支持消费，按推翻条件重审。

## 什么情况下应该推翻

- ①/②：快照评测引入时间锚定设计（相对时间可稳定评测）或真实多轮/过滤需求
  超出 MVP 范围 → 重审范围并补评测样本（新 ADR 或本 ADR 增补记录）；
- ④：出现真实 text2sql 生成器（LoRA adapter 完成 / 直出 SQL 的 LLM 端点 + Guard）
  → 按 eval/bird/README.md 恢复路径重审（需新 ADR）；
- ⑤：Polaris 发布行级能力并有真实下推需求 → 重审下推方案。

## 验证方式

- 评测先行：gold-149~155 于实现前落库并记录失败基线（不入库）；
- 契约测试：filter 解析/编译用例（test_planner + test_compiler）、指代合并/澄清/
  隔离用例（test_graph）、Guard 跨表注入/拒绝/再校验用例（test_sql_guard）；
- 全量回归：make lint + make test + make eval（gold 扩张后）+ make e2e
  （多轮场景）+ 报告入库；EVAL_REPORT / README 数字机械转述，不修改历史报告。
