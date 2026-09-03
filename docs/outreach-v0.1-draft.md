# Atlas v0.1 对外介绍草稿（发布前审阅）

> 本文件是**发布动作的文案素材**，不是交付声明。所有数字与 README §3.3 /
> EVAL_REPORT.md / `docs/release-notes-v0.1.md` 同源，不引入任何新数字。
> 发布前请通读一遍：确认措辞不超前于实现、blocked 项不出现、数字可溯源
> （AGENTS.md §9 规范）。建议发布顺序：仓库 public → CI 绿 → 本文案。

---

## A. 一句话版（仓库 About / 社交简介）

> 以 FIBO 金融本体为语义锚点的可信 AI 问数平台：声明式语义层 → 确定性 SQL 编译器 →
> 只读 Guard → 可解释 Agent，自建 50 例黄金集全链路评测可复现。Apache Ossie /
> Polaris / Doris / Iceberg 全栈单机可跑，公开数据构建。

## B. 推文版（~300 字内）

标题候选：《不做更好用的 NL2SQL，我做了个不敢撒谎的》

正文草稿：

```
单机 + 公开数据，做了一个「不敢撒谎」的金融问数系统 Atlas。

三条纪律：
1. 已知指标不走 LLM——语义编译器确定性生成 SQL（44/44 计划命中）
2. 所有 SQL 必须过只读 Guard，恶意语句实测全拒
3. README 每个数字都能 30 秒追到评测脚本与报告文件

Apache 全栈：Ossie 语义规范 + Polaris/Iceberg 目录 + Doris 执行；
FIBO 金融本体做概念锚点（31 条映射全部校验存在）；
LangGraph 编排的 Data Agent 端到端 6/6 场景过。

最诚实的部分写在 Known Limitations 第 27 条里——包括我做不到的。
公开数据、无企业机密、评测绑定数据快照 sha，可复现。

#NL2SQL #SemanticLayer #ApacheDoris #FIBO #数据平台
```

## C. 长文版（技术博客骨架，~10 分钟阅读）

### 标题候选
《可信问数：一个"确定性优先 + 诚实红线"的 NL2SQL 项目》

### 引言
为什么绝大多数 NL2SQL 项目企业不敢用：不是模型不准，是不可信——
口径谁定义？权限怎么下推？数字能不能复现？Atlas 是我用 8 周做的回答，
全部使用公开数据（TPC-DI 零售经纪基准 + FIBO 本体），单机预算。

### 1. 语义层是信任的地基
- Apache Ossie 定义 Metric/Dimension（注：Ossie 不是运行时，Compiler 自己写）
- 治理扩展补齐 owner/lineage/freshness/版本链
- 同一业务词只有一个权威定义（20 指标，发布记录带审核单）
- FIBO L2：业务字段挂到本体概念 IRI，31 条映射全部实测存在于锁定闭包

### 2. 确定性优先：LLM 不是 SQL 作者
- 已知指标：Planner → 确定性 Compiler → SQL（注册域零 LLM 覆盖 48/48）
- LLM 只是域外候选生成器，且必须过 Guard + 执行校验
- RAG+LLM 对照实测与确定性链同分（44/44），代价高两个量级——
  "域内 LLM 无增量"是实测结论不是立场

### 3. 安全三层：只读是不可协商红线
- Guard 三层：AST 形态校验 / 语句白名单 / 行级策略注入
- Polaris 对象级 RBAC + 三角色行级实测
- 恶意 SQL 十类全拒、LIMIT 与时间范围强制

### 4. 评测闭环：数字只来自脚本
- 50 例自建黄金集（人工标注），执行结果与固定快照比对
- 快照 = Iceberg 表 + snapshot id 绑定 git sha，漂移自检拒绝出报告
- 每个报告文件名 = commit sha，EVAL_REPORT 机械转述、逐格带 source

### 5. 可观测：从"看起来通"到"口径对"
- OTel 回合级 span：question_id → metric_id → SQL → 结果行数 → 成本
- 踩坑实录（对外可选讲）：MeterProvider 漏传 resource 导致 metrics 用随机
  instance、prometheus exporter 指标名规范化、CLI 短进程丢埋点——
  修完才拿到真实 p95（冒烟口径，非流量基线）

### 6. Known Limitations：项目里最值钱的一节
- 单机规模 / 千表未验证 / LoRA 路径 blocked（无 GPU，不编数字）/
  FIBO 尚有 1 个指标找不到贴切本体类——全部如实登记

### 结语
如果只带走一件事：**可信不是功能，是纪律**——口径唯一、权限下推、
SQL 只读、数字可复现，缺一项都是营销。代码与完整评测在仓库，欢迎审阅
Known Limitations 先于架构图。

### 附：截图素材（docs/screenshots/）
- p1-chain.png：安全链路验收（恶意 SQL 全拒 + EX 匹配）
- rls-verify.png：三角色行级权限
- polaris-rbac.png：对象级 RBAC Forbidden
- Grafana 面板可补截（可选启动，见 README §3.2 obs profile）

---

## 发布前自检清单（每条都过才能发）

- [ ] 所有数字与 README/报告同源（无手打新数字）
- [ ] 无「已上线 / 生产 / 企业级 / 准确率 95%」类表述
- [ ] LoRA / BIRD / CI 真实执行等 blocked 项未以完成态出现
- [ ] 术语符合 GLOSSARY（Metric/Measure/EX/Guard…不混用）
- [ ] 数据声明含口径（TPC-DI 公开基准、非企业数据）
- [ ] 仓库已 public 且 CI 绿（先发仓库后发文案）
