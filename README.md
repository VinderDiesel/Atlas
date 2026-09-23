# Atlas

> **以 FIBO 本体为语义锚点的金融可信 AI 问数平台**｜统一语义层 + 受控 NL2SQL + 可解释 Data Agent
>
> Status: **under active development**
> 所有数字来自可复现脚本产物，不是营销断言。见 `EVAL_REPORT.md`。
> v0.1 发布说明与已知边界：`docs/release-notes-v0.1.md`；当前版本 **v0.1.7**（执行内核重构 + 意图识别引擎 + 工作台全链路 + 前端控制台增强）
>
> English: [README.en.md](README.en.md)（快速开始 / 架构 / 评测复现 / 双域说明 / 已知限制摘要）

---

## 0. 一句话定位

Atlas 是一个**以真实可落地为目标构建的金融语义数据系统**（个人预算内：单机 + 公开数据，非企业生产环境）：
以**金融为主场景**，
**FIBO 金融业务本体作为语义锚点**，使用声明式语义层、指标编排、受控 SQL 执行与 LangGraph Agent，
打通**业务术语 → FIBO 概念 → 指标计划 → 安全查询 → 可解释结果 → 评测回流**的完整闭环。

**它刻意不做的事**：不训练基座模型、不追求 NL2SQL 榜单分数、不声称企业级生产能力。
**它刻意做好的事**：口径唯一、权限下推、SQL 只读、结果可解释、评测可复现。

### 0.1 技术栈：全栈 Apache

| 层 | 选型 | 说明 |
|---|---|---|
| 语义规范 | **Apache Ossie** (Incubating) | 语义元数据交换标准 |
| 治理扩展 | Atlas 自研 | 补 Ossie 缺的 owner / lineage / freshness |
| Catalog | **Apache Polaris** (Incubating) | Iceberg REST Catalog + RBAC |
| 表格式 | **Apache Iceberg** V2 | ACID、Time Travel、Hidden Partitioning |
| OLAP | **Apache Doris** 4.1 | 原生 Iceberg catalog，向量化执行 |
| 对象存储 | MinIO | S3 兼容 |
| 向量检索 | Milvus | 保留（已有工程经验） |
| 计算 / 编排 | Spark / Flink / Airflow | 已为 Apache |

见 `infra/adr/0004-apache-stack.md`。

**一个关键认知**：Apache Ossie **不是运行时**——它不执行查询、不解析指标、不在查询路径上。
社区类比是 Protocol Buffers：Ossie 定义"含义"，由 converter 编译为各平台语义层。
**所以 Compiler 仍需自己实现**，这正是本项目的核心工作量。

---

## 1. 为什么是"确定性优先"

绝大多数 NL2SQL 项目失败在企业不敢用，而不是模型不够准。Atlas 的核心纪律是：

```
已知指标  →  语义编译器直接生成 SQL（不经过 LLM，结果确定）
自由问句  →  RAG 检索 + LoRA/通用 LLM 生成候选
             ↓ 必须过：AST 只读检查 → 行级权限下推 → LIMIT/时间范围 → 成本预算
             ↓ 执行 → 结果校验 → 失败进修正日志 → 回流训练
```

**LLM 不是 SQL 的作者，是候选生成器。** 编译器和校验器才是权威。

多步分析走同一条确定性纪律（ADR-0026，已落地）：`POST /api/v1/analyze` 把
"分析 2013Q4 相对 2013Q3 的佣金收入按分支的变化贡献"这类问句（绝对双期间 +
明确维度）编译为**固定四步贡献模板**（两期总计 + 两期按维度分解），
Plan → 模板 → Compiler → Guard → 执行 → 精确算术综合，LLM 零参与。

---

## 2. 架构

```text
┌────────────────────── 用户与分析协作层 ──────────────────────┐
│ Web/Chat UI  │  Notebook  │  BI/API  │  Slack/OpenAPI         │
└───────────────────────┬──────────────────────────────────────┘
                        │ OAuth/JWT；部门/行级身份
┌───────────────────────▼──────────────────────────────────────┐
│ API Gateway + 审计 + 预算 + 只读 SQL 防火墙 + OTel SDK       │
└───────────────────────┬──────────────────────────────────────┘
                        │
┌───────────────────────▼──────────────────────────────────────┐
│  Data Agent（LangGraph 状态机；确定性工具优先）              │
│  落地（Day 43-48）：plan → execute → explain 确定性主链，  │
│  歧义反问 clarify / 候选链 generate·validate / handoff 等   │
│  条件分支；端到端验收见 docs/e2e-acceptance.md              │
└──────┬──────────────┬───────────────┬───────────────┬────────┘
       │              │               │               │
┌──────▼──────┐ ┌─────▼─────┐ ┌──────▼──────┐ ┌──────▼──────┐
│ 语义检索     │ │ 指标注册   │ │ NL2SQL 服务  │ │ 结果解释     │
│ Milvus+HNSW │ │ YAML/Git   │ │ LoRA/通用LLM │ │ LLM Judge    │
│ +Graph 关系 │ │ OpenAPI    │ │ RAG/SC/修正  │ │ 图表/归因    │
└──────┬──────┘ └─────┬─────┘ └──────┬──────┘ └──────┬──────┘
       │              │               │               │
┌──────▼──────────────▼───────────────▼───────────────▼───────┐
│  Semantic Compiler（自研）：FIBO 概念/指标/维度/时间/筛选 → SQL│
├──────────────────────────────────────────────────────────────┤
│  指标编排（YAML + Airflow）；血缘、版本、质量                 │
├──────────────────────────────────────────────────────────────┤
│  Apache Doris 4.1（OLAP，原生 Iceberg catalog）               │
│  Apache Iceberg V2 on MinIO（湖仓表格式，Time Travel）        │
│  Apache Polaris（REST Catalog + 对象级 RBAC；行级在 Guard 谓词层）│
│  PostgreSQL（元数据/策略）+ Milvus（向量）+ NetworkX（图）    │
├──────────────────────────────────────────────────────────────┤
│  Spark / Trino；对象存储；Docker Compose；CI/CD               │
└──────────────────────────────────────────────────────────────┘
                        │
┌───────────────────────▼──────────────────────────────────────┐
│  评测/反馈闭环：黄金集、Execution EX、Judge、修正日志、SFT     │
│  Prometheus + Grafana + OTel + 模型/提示词 Git 版本            │
└──────────────────────────────────────────────────────────────┘
```

**两条铁律：**

1. **Git 是唯一事实源**。语义定义、提示词、评测集、模型适配器版本全部入库，任何线上行为可回溯到 commit。
2. **查询引擎是可替换执行面**。语义层不绑定 ClickHouse/Doris/Trino 中的任何一个。

---

## 3. 30 分钟快速开始

> 以下命令均按开发日程逐条实测回填（状态与数据来源见 3.3 勾选清单与 §9 命令速查）；
> `make seed` 耗时分钟级（装载 17 张 ODS 并核验）；环境差异（端口占用 / Doris 内存）处理见
> infra/docker 与 ADR-0004 降级路径。

### 3.1 前置条件

| 依赖 | 版本 | 说明 |
|---|---|---|
| Docker / Docker Compose | ≥ 24 / ≥ 2.20 | 起 Polaris、Doris(FE+BE)、Iceberg/MinIO、Milvus、Grafana |
| Python | 3.11 | 主开发语言 |
| uv 或 pip | 最新 | 依赖管理（推荐 uv） |
| Git | ≥ 2.40 | 语义层版本控制 |
| Node.js（可选） | ≥ 20 | 仅前端控制台需要（P0b 已落工程边界，界面属 P1~P3）：`make ui-check` 门槛 / `make ui-build` 构建；node 经 nvm 安装时 Makefile 自动探测注入 PATH，未装则明确报错退出（ADR-0018） |
| 内存 | **建议 ≥ 16GB** | Doris FE+BE 双进程内存压力较大（ADR-0004 有降级路径） |
| 显卡（可选） | 单卡 24G 起 | 仅 LoRA 微调阶段需要；CPU 可跑通 P0–P1 |

> ⚠️ 若单机跑不动 Doris，按 ADR-0004 降级：保留 Iceberg + MinIO，查询引擎换回 ClickHouse。

### 3.2 启动

```bash
git clone <your-repo-url> atlas && cd atlas

# 1) 安装依赖
make install

# 2) 启动 Apache 全栈（Polaris / Doris / Iceberg+MinIO / Milvus / Grafana）
make up

# 3) TPC-DI Batch1（零售经纪，2012-07-07~2017-07-07 实测数据段）→ 装载 17 张 ODS → 锁定快照
#    （load 耗时分钟级；快照 meta 见 data/snapshots/<sha>.meta.json）
make seed

# 3b) DWD 加工：在 Doris 内执行 sql/dwd 的 8 张幂等 SQL（依赖 Doris 已建 atlas catalog）
make dwd

# 4) 校验语义层（Ossie 规范 + Atlas 治理扩展）
make lint-ossie
make lint-governance

# 5) 跑一次完整链路："2013 年第二季度总交易额是多少？"
make plan   Q="2013 年第二季度总交易额是多少？"
make compile
make eval

# 6) 生成评测报告
make report && cat EVAL_REPORT.md

# 7) 导出为 dbt MetricFlow YAML（证明语义层非封闭；20 指标三态映射报告见 exports/）
make export
```

### 3.3 验证成功

- [x] `make lint-ossie` 通过（Ossie 官方 schema 校验）
- [x] `make lint-governance` 通过（owner / lineage / policy 齐全）
- [x] `make plan` 输出 Plan 含 `metric='total_trade_value'`、`value='2013Q2'`（对应 gold-101，15/15 契约测试通过）
- [x] `make compile` 产出带 `LIMIT` 与时间范围约束的只读 SQL（gold-101~103 实测）
- [x] TPC-DI Batch1 → tpcdi 17 张 ODS 装载完成，行数与源文件逐行核验一致（data/loader.py QA 口径）
- [x] DWD 8 张加工完成（INSERT OVERWRITE 幂等，Doris 侧 COUNT 与 pyiceberg 侧双验一致），行数与 snapshot id 锁定于 `data/snapshots/b47a6c1.meta.json`
- [x] 语义层 source 命名与 Doris 实际路径统一（`atlas.dwd.*`）；gold-101~103 参考 SQL 已在 Doris 实跑返回（2013Q2 交易额 344129059.35 等，仅供链路验证，非评测数字）
- [x] `make eval` 已跑通且幂等：金融段 48 例（44 可解析 + 4 歧义）Plan Acc 44/44、歧义反问 4/4、EX 44/44，报告 `eval/reports/b47a6c1.json`（gold 共 50 条含零售 2，50 条目标达成，见 5.3）
- [x] `make retrieve` 指标检索双路实测：语料 15 指标文档（与 gold 同源）、查询 44 条可解析问句；BM25 Recall@1 44/44、Milvus 稀疏向量 Recall@1 42/44（两路 @5 均 44/44），报告 `eval/reports/retrieval-bm25-b47a6c1.json` / `retrieval-milvus-b47a6c1.json`
- [x] `make retrieve ENGINE=fuse` RRF 融合实测：BM25 + Milvus 双路各 top-20 融合后 Recall@1 = 44/44、@5 = 44/44（修复向量路 2 条 top-1 失手），报告 `eval/reports/retrieval-fuse-b47a6c1.json`；SemanticGraph 图约束过滤跨实体错配（现金域 × 证券维度），13 组 gold 维度样本契约测试全保留（tests/test_graph_store.py）
- [x] `make retrieve ENGINE=rerank` 元数据 Rerank 实测：双路 RRF top-20 后按词典序（同义词置信度 → 留一热度 → owner）重排，Recall@1 = 44/44、@5 = 44/44，报告 `eval/reports/retrieval-rerank-7d48dcb.json`；加权线性混合版曾实测 34/44@1（热度分系统性推高恒在热门指标），存档 `retrieval-rerank-7d48dcb.linear-weighted.json`，词典序修正设计理由见 retrieval/rerank.py；15 指标治理补齐（11 个新指标补 ATLAS 扩展），gold_test_cases 与评测集双向一致性由 governance_validate 强制
- [x] Day 25 三角色行级权限验证：同一问句（gold-146「按分支和客户等级统计 2015 年交易额 Top5」）走同一 Planner/Compiler/Guard 链，仅 JWT 角色不同 → 注入不同谓词 → Doris 实测：hq_admin 5 行 / branch_manager 2 行（仅本人分支）/ compliance_auditor 5 行（tier≤3，排除 tier8 与 NULL 档），结果差异集 3；权限生效在 SQL 谓词层（Guard 别名对齐 + 二次只读校验），非应用层过滤，报告 `eval/reports/rls-verify-7d48dcb.json`，截图 `docs/screenshots/rls-verify.png`
- [x] Day 25 Polaris 层 RBAC（纵深第二层验证）：同一 catalog（atlas，25 表）两个 principal——root 全可见；atlas_analyst（受限只读，仅授 dwd.dim_broker/dim_customer 表级权限）list_namespaces/list_tables Forbidden（防枚举）、load 授权表 OK、load fact_trades Forbidden，报告 `eval/reports/polaris-rbac-7d48dcb.json`，截图 `docs/screenshots/polaris-rbac.png`
- [x] Day 26 元数据抽取器：`metadata/parser.py` 确定性抽取（sqlglot Tokenizer 提注释规避字符串内 `--` 误判 + AST 提结构），25 个 SQL 脚本（sql/dwd 8 + loader 生成的 tpcdi ODS DDL 17）实测：dataset 候选 25（9 已注册）、measure 候选 42（13 已注册）、聚合 metric 候选 1（`daily_net` 账户日净额，未注册=新候选池）；主键/代理键/旗标/建库语句与窗口函数正确排除，候选不产已注册对象（known 标记防 N8），报告 `eval/reports/metadata-extract-7d48dcb.json`；候选≠发布（Day 27 人工审核）；TPC-DS 脚本随 ADR-0006 已退场，语料口径见 ADR-0006
- [x] Day 27 审核与发布：人工审核 Day 26 抽取候选——ODS 原始层 measure 候选拒绝入分析语义层（无权威口径锚点，理由记录于发布单）、`daily_net` 未物化登记待物化；**发布 5 个可计算派生指标**到 `atlas_finance.ossie.yaml`（15→20 metrics，governance v1 active + lineage，FIBO 概念映射 +5 键 MonetaryAmount/Fee/Balance）；`make lint` 全绿；新工具 `serving/metrics_verify.py` 走真实链路（YAML 权威表达式 → Compiler → Guard → Doris）实测：平均每笔成交金额 27578.61 / 平均每笔佣金 89.57 / 佣金率 0.3247% / 户均持仓市值 1136660.85 / 户均现金余额 -32465124.70（负值系数据特性，见 Known Limitations #17），报告 `eval/reports/metrics-verify-7d48dcb.json`；发布审核判定记录 `semantic/migrations/2026-09-02-release-day27.md`
- [x] Day 27 指标版本机制：`governance_validate.py` 新增 supersedes 链跨文件校验——取代目标存在、非自身、新版本号严格大于被取代版本（递增天然防环）、被取代者不得仍为 active、治理记录不得重名；演进规范＝新名 + supersedes 旧名（同名全局唯一由 ossie_validate 强制，N8）；契约测试 10 例 `tests/test_governance_validate.py`，由 `make lint` 执行（本地门槛；当前远程为 gitee，GitHub Actions 不执行——ADR-0018 ⑥）
- [x] Day 27 语料扩展回归（15→20 指标文档）：bm25 Recall@1 44/44→41/44、Milvus 42/44→35/44、fuse 44/44→40/44（三路 @5 均保持 44/44；15 语料旧值 44/42/44 记录于上两行）；rerank 主链路 44/44 无损；归因与后续见 Known Limitations #18；配套修复：Doris FE 官方默认 JVM 堆 8G 吃满单机内存致全表聚合查询 OOM → compose 挂载自定义 fe.conf（`infra/docker/doris/fe.conf`，Xmx2g），FE 内存 5.5G→1.0G 实测（docker stats）
- [x] Day 28 P1 端到端验收（`make p1-verify`，serving/p1_acceptance.py）：gold-102「按分支统计 2013 年佣金收入 Top5」全链路 = 唯一路由 → commission_revenue@v1（governance active）→ 编译断言 → Guard 注入行级策略 → Doris 实测；5 道 gates 全过：唯一路由 / @v1 / LIMIT+谓词 / **恶意 SQL 10 条全拒**（INSERT/UPDATE/DELETE/DROP/ALTER/GRANT/CREATE/sleep/pg_sleep/benchmark）/ **EX 匹配**（hq_admin 结果 sha256 = gold-102 锚定 hash）；branch_manager 只见注入分支 1 行；报告 `eval/reports/p1-chain-7d48dcb.json`、截图 `docs/screenshots/p1-chain.png`、验收记录 `docs/p1-acceptance.md`
- [x] Day 29 schema linking（`make schema-link`，agent/tools/schema_linker.py）：两阶段 = 图域约束粗筛（SemanticGraph 可达性预检，跨实体错配打分前剔除）→ 受限候选域打分（子域 BM25，可选双路 RRF）→ 元数据重排（同义词置信度主键）；44 条 gold 上 **指标 Recall@1 = 44/44**（同日 BM25 全量域单路基线 41/44——KL#18 主链路修复）、@3/@5 = 44/44、表覆盖 44/44（下界验证口径，6 表域无区分度如实声明）；报告 `eval/reports/schema-link-bm25-7d48dcb.json`，契约测试 7 例 `tests/test_schema_linker.py`（含 KL#18 市值问句回归、现金×证券错配剔除）
- [x] Day 30 compiler-only 基线（`make eval` + `make baseline`）：50 条 gold 盘存分离报告——金融段 48 条（44 可解析 + 4 歧义）全量实测 Plan Acc **44/44**、歧义反问 **4/4**、EX **44/44**（与 b47a6c1 时代锚定 hash 一致，0 失败 0 错误）、零售段 2 条如实跳过不混报；基线分析：**注册语义域内确定性链零 LLM 覆盖 48/48**（0 样本需要生成式猜测），域外边界不推断；报告 `eval/reports/7d48dcb.json` + `eval/reports/baseline-compiler-7d48dcb.json`，分析 `docs/baseline-compiler.md`；新快照 `data/snapshots/7d48dcb.meta.json`（数据指纹与 b47a6c1 一致）
- [x] Day 31-32 LLM 策略（`make rag-eval ENGINE=openai|stub`，agent/generator.py）：Generator = 问句 → **Plan 候选**（metric/dimensions/time/top_n，SQL 一律由确定性 Compiler 生成，Guard 只兜底 Compiler 产物——最小攻击面）；Prompt 资产 `agent/prompts/generator_plan.yaml`（version/owner/changelog 契约）；stub 引擎链路自检 44/44+4/4+44/44 与基线同口径（报告 `rag-llm-stub-7d48dcb.json`，显式声明 stub 不代表 LLM 能力）；**openai 实测（2026-09-03 端点就绪）：44/44 Plan Acc + 4/4 歧义反问 + 44/44 EX + 0 拒绝**，与 compiler-only 持平（deepseek-v4-flash，99597 tokens、2564.3ms/条、$0.0179 估算——报告 `rag-llm-openai-7d48dcb.json`）；实测驱动两处修复：max_tokens 300→800（截断拒答）与维度顺序注册序规范化（gold-146 hash 口径）；公开集重新评估：Spider 判定历史对照不再新增，BIRD finance 对照待 text2sql 生成器可用（`eval/spider|bird/README.md` 阻塞与恢复登记）
- [x] Day 33 自洽投票与执行校验（agent/tools/）：execution_validator 形态检查（空结果/全 NULL/非分组多行 → issues）+ self_consistency（候选执行 hash 聚类取众数、平局取候选序、top1 失败自动回退 top-k 首个有效），13 例契约测试全过；gold 域 0 失败 → 回退链 0 触发为设计结论（注册域不需要自愈）；LLM 单采样全量实测 44/44 无失败 → 多采样 0 触发为实测结论（LoRA 行解锁后由 lora-sc 承接）
- [x] Day 34 四策略对比（`make compare`，eval/compare_4way.py）：六维表 EX / Plan Acc / Token / Latency / Cost / 拒绝率——compiler-only 44/44 / 44/44 / 0（确定性设计事实）/ 178.6ms（现场重测）/ 0 / 0/44；rag-llm(openai) 44/44 / 44/44 / 99597 / 2564.3ms / $0.0179（估算）/ 0/44——**同分不同代价：注册域 LLM 无增量，确定性优先量级差异实证**；**lora / lora-sc 两行 blocked**（仅无 GPU，LLM 端点已就绪，不编数字 AGENTS.md N1）；所有策略 SQL 同一 Guard 无旁路（报告 `compare-4way-7d48dcb.json`）
- [x] Day 35 失败样本体系：7 类分类 schema（eval/failures/categories.json）+ 自动归类（failure_collect.py）+ 四步流程 README（人工确认后才可进 SFT）；compiler-only / rag-llm-stub / rag-llm-openai 三份报告程序化扫描 **0 失败**——空集是脚本产物不是假设；LLM 实测暴露的缺陷（截断/维度序）在评测侧修复归零，失败样本的价值以口径修复兑现（Day 31 补测记录）
- [x] Day 36 训练数据构造 + **架构对齐修正**（lora/build_pairs.py）：pair 形态 = question → **合法 Plan JSON**（mode=plan，非任务书旧字面 question→SQL；校验与推理共用模块级 `agent.generator.validate_plan_json`，ADR-0008 同口径）；三道红线 = gold 问句逐字拒绝 + 同义词/数字归一模板级检测 + 去重质量过滤（13 例契约测试锁定）；`lora/data/README.md` 数据源政策——gold 与 ETL SQL 不可作训练源，**空语料为设计结论**（脚本实测 [empty] 为证）
- [x] Day 37-38 LoRA 训练栈（ADR-0008）：`lora/train.py` 前置检查 exit 2 模式（语料非空 → 全量 validate_plan_json → min_samples=50 → ml 依赖 → CUDA，任一不满足 blocked 不烧钱）+ `lora/configs/sql_v1.yaml`（Qwen2.5-7B-Instruct QLoRA nf4、r16/alpha32、loss 只算 answer 段、权重落 lora/weights/ 不入库）；实测登记：macOS arm64 无 CUDA + ml 依赖 5 件未装 → `make train` blocked（exit 2）；训练路径未实测，ADR 验证方式 4 条全 [ ] 不勾选；Makefile train target 修正为先 build_pairs 后吃 pairs.jsonl
- [x] Day 39 评测闭环（`make report`，eval/report.py）：**机械转述** eval/reports 八类报告 → EVAL_REPORT.md（§1 主评测…§8 来源清单），每格数字带 source 列可核对、0 个「待填写」、只聚合当前 sha（防新旧混报）；契约测试 6 例 `tests/test_report.py`；旧版占位模板 EVAL_REPORT 被真实产物替换
- [x] Day 40 CI 回归评测（.github/workflows/eval.yml，部署目标 GitHub Actions）：push/PR 触发 plan-regression = lint + 契约测试 + **Plan Acc dry 回归**（eval.runner --dry 自洽断言，公共 runner 无数据库依赖）+ eval-data 手动 job（完整 EX，前置 compose+seed+快照）；诚实边界登记：EX 不可 CI 化（公共 runner 无 TPC-DI 数据/Doris）、prompts 变更对 dry 回归不敏感（仅被 LLM 引擎消费，待端点由 rag-eval 承接）；本地等价验证 dry 门槛通过（44/44+4/4+0 errors）；**真实执行待 push**（本地无法模拟 GitHub runner）；修复 lint.yml 最小依赖清单（mysql-connector-python/python-dotenv）
- [x] Day 41 数据飞轮（lora/flywheel.py）：五阶段状态机 scan（复用 failure_collect 归类）→ review（人工闸口，红线）→ export（approved + answer_plan 过 validate_plan_json 双闸）→ build（子进程防泄漏过滤）→ train（子进程，blocked exit 2 如实记录）；**空转实测**（0 失败样本下 scan {} → export 0 → build exit 0 → train exit 2，state 绑定 sha 落盘 `lora/data/flywheel-state.json`——设计结论不是缺陷）；6 例契约测试 `tests/test_flywheel.py`；完整轮转截图 blocked（需失败样本 + GPU）
- [x] Day 42 P2 验收：验收记录三件套（eval dashboard = EVAL_REPORT.md + Guard 恶意 SQL 10/10 逐条 kind 表 + 四歧义 gold 反问 4/4 结构化表）；P2 门槛复核：EVAL_REPORT 自动生成 ✓ / CI 回归（本地等价验证）✓ / 无泄漏 ✓ / 四策略数据齐全 [~]（compiler-only + RAG+LLM(openai) 实测；LoRA/LoRA+SC 两行 blocked 如实登记，2026-09-03 端点就绪后 RAG+LLM 行已解锁，见 Day 31-32 行）
- [x] Day 43-49 Data Agent 端到端落地（批次 D）：LangGraph 8 节点状态机（确定性主链 plan→execute→explain + clarify/候选链/handoff 条件分支，MemorySaver 多轮会话）+ 确定性工具四件套 + MCP 风格暴露（参数校验/作用域）+ 歧义反问 4/4（gold 歧义样本）+ 确定性图表（schema 必须来自已执行结果）+ 纠错反馈入口；**5 场景端到端验收**（含恶意 SQL 拒绝与 handoff）回归记录 `docs/e2e-acceptance.md`，报告 `eval/reports/e2e-acceptance.json`，演示入口 `make ask`（多轮）与 `make e2e`（门禁）
- [x] Day 50 OTel 全链路埋点（`observability/otel.py`，测试 7 例）：每回合一个 `atlas.turn` span——question_id（`session#tN` 可回放）/ metric_id / SQL / rows / latency / snapshot sha 全属性可追；`gen_ai.token_cost` 单位 token、仅 LLM 真消耗时产出；默认 no-op 零 I/O、埋点故障隔离、atexit flush（CLI 短进程不丢埋点，2026-09-03 实测修正）；启用：`.env` 设 `OTEL_EXPORTER_OTLP_ENDPOINT` 即自动导出
- [x] Day 51-53 可观测栈冒烟实测（Grafana 11 + Prometheus + otel-collector，`docker compose --profile obs up -d`）：provisioning（prometheus 数据源 / 6 面板 / 4 告警）加载实测 200；**真实回合 7 条（Doris 执行 answer + clarify）→ OTLP → collector → Prometheus → Grafana datasource proxy 全链路通**，6/6 面板表达式查询 success，p95 实测 242.5ms（回合 165-376ms 分布，冒烟数据非流量基线）；修复两处实测缺陷：面板/告警 latency 指标名缺 exporter 规范化后缀 `_milliseconds`（测试改为精确形态契约锁定，`tests/test_dashboards.py` 9 例）、CLI 短进程随机 instance 致序列碎片（固定 `service.instance.id`）；token_cost 面板无序列 = 确定性链路 0 token 设计事实（KL #24），QPS rate 形态注记 KL #23
- [x] Day 52 ADR 补全三篇（0009 LangGraph 框架取舍 / 0010 评测方法论 / 0011 安全三层纵深），0005 增补推翻条件，累计 11 篇；rbac-verify 实测通过（Polaris analyst 对 fact_trades/列表目录均 Forbidden，报告 `polaris-rbac-7d48dcb.json`）——ADR-0011 按实测修正口径
- [x] Day 53 `make export` 实测（`semantic/export_dbt.py` + 契约测试 7 例）：Ossie 20 指标 → dbt MetricFlow YAML 三态如实映射——**agg 14**（单列聚合→measure）/ **ratio 3**（分子/分母 measure）/ **unmapped 3**（`SUM(a*b)` 先乘后加，MetricFlow measure 无法表达，逐条登记理由不伪造等价物）；产物 `exports/dbt_semantic_models.yml` + `exports/metric-export-report.json`（绑定 git sha 7d48dcb）
- [x] 2026-09-03 filter 自然语言解析批次（ADR-0014 ①，评测先行 gold-149~155 已落库）：维度等值/排除 → WHERE、度量阈值 → HAVING（上/下界双向），时间词不并入 filter；语义层 gold_test_cases 双向引用补齐（lint-governance 强制）；扩张后全量实测 **Plan Acc 50/50、歧义反问 5/5、EX 50/50、0 执行错误**（报告 `eval/reports/a207284.json`，快照 `a207284.meta.json` 指纹与 b7e9ce7 一致）；契约测试 +16（planner/compiler）
- [x] 2026-09-03 指代消解 MVP + e2e 多轮追问（ADR-0014 ②，commit 341d7d7/bca94c5）：会话追问仅“同 metric 换时间/换维度”结构补全（链接词“那…呢/换成/按…呢/改为”，复用上轮 last_plan），自由代词与指代不明 → 澄清不猜测（契约测试 16 例）；e2e 场景 **7/7 实测通过**（S7「那 2014 年呢」多轮同构追问 + Guard 行级结果一致断言，报告 `eval/reports/e2e-acceptance.json`）
- [x] 2026-09-03 Guard 跨表 join 注入（KL #14 收窄，commit a3825bf/a207284）：策略谓词引用表不在查询时沿语义模型 join 图补 LEFT JOIN（契约测试单跳/双跳/组合注入、无路径拒绝、恶意策略注入后二次校验），无合法路径仍拒绝；rls-verify 三角色实测差异集 3 无回归（报告 `eval/reports/rls-verify-a207284.json`，Guard 注入+表白名单复核后行级链路不变）
- [x] 2026-09-04 派生指标补洞（KL #16/#17 收口，commit 3098247/30b8344/fea59f7/929da6b）：gold-156~162 七条样本覆盖 Day 27 五个派生指标（平均每笔成交金额 156/157、平均每笔佣金 158、佣金率 159/160、户均持仓市值 161——fact_holdings 域首样本、户均现金余额 162——负值锚定）；评测先行暴露 Planner 同义词子串歧义真实缺陷（派生词 ⊃ 基础词系统性误报口径歧义，dry 50/57）→ 最长命中消解修复（契约 +9）；实测锚定（快照 30b8344.meta.json 指纹与 a207284 全一致）**Plan Acc 57/57、歧义反问 5/5、EX 57/57、0 执行错误**（报告 `eval/reports/30b8344.json`）；expected_value_snapshot_sha 全 20 指标回填 + governance 第 7 条锚定校验启用（契约 +4）

---

## 4. 语义层：Apache Ossie + Atlas 治理扩展

指标是 **Git 中的一等对象**，不是 LLM 提示词里的字段别名。

### 4.0 为什么是 Ossie，以及它不是什么

**Apache Ossie (Incubating)** 是语义元数据的交换规范。
前身是 Snowflake 于 2025-09 发起的 Open Semantic Interchange (OSI)，
**2026-07-10 进入 Apache 孵化器**并改名（避免与 OSI 缩写冲突）。
50+ 组织参与：Snowflake、Databricks、Salesforce、dbt Labs、Dremio、RelationalAI 等。

三条决定性边界，**对外介绍必须讲清**：

| 边界 | 含义 | 对 Atlas 的影响 |
|---|---|---|
| **不是运行时** | 不执行查询、不解析指标、不在查询路径上 | **Compiler 必须自己写**，Ossie 不替代它 |
| **expression-carrying** | 指标按命名方言存储（ANSI_SQL / SNOWFLAKE / BIGQUERY / DATABRICKS / MDX / TABLEAU / MAQL），不定义可移植表达式语言 | 需实现方言选择；只写 ANSI_SQL 则其他方言需翻译或拒绝 |
| **不标准化"信任"** | 核心规范**无** lineage / ownership / freshness / provenance | **这正是 Atlas 治理扩展要补的缺口** |

社区类比：**Protocol Buffers** —— `.proto` 定义 schema 编译成各语言代码，
Ossie 定义"含义"编译成各平台语义层。

### 4.1 分层结构

```
Atlas 治理层（owner / version / supersedes / lineage / row_policy / freshness）
        ↓ 通过 custom_extensions（vendor_name: ATLAS）挂载
Apache Ossie Core Spec（semantic_model / datasets / fields / relationships / metrics / ai_context）
        ↓ converter
下游：dbt MetricFlow / Cube / Doris / BI
```

### 4.2 语义模型文件

主模型：`semantic/ossie/atlas_finance.ossie.yaml`（8 datasets / 12 relationships / 20 metrics，
挂载 31 条 FIBO 概念映射（8 datasets 全覆盖 + 19/20 metrics；total_trade_tax 待办见 `data/fibo/README.md` 审计节），
见 `data/fibo/README.md`；指标审核发布记录见 `semantic/migrations/`）；
`atlas_retail.ossie.yaml` 自 2026-09-04 起转正为第二主评测域：TPC-DS SF0.1 数据已装载
（`make seed-retail`，4 表入 dwd），零售 gold 样本在 2026-09-05 双语批次为 19 条全锚定
（14 中文含 1 歧义 + 5 英文，锚定快照 1e5d35b，终验复验零回归报告 9749fc5——见
eval/gold/README.md 零售段与 EVAL_REPORT.md，该陈述绑定具名快照与报告，属历史记录不改写）；
**2026-09-14 实测当前为 27 份样本文件**（22 中文 + 5 英文），其中 18 条已锚定，余 9 条 =
6 条非歧义双占位（gold-072~077，P-1 批次可 EX 锚定）+ 3 条歧义样本（gold-047/068/078，
设计上走澄清、无结果行、永不产生 `result_hash`），五类分类明细见 KL #31 与 ADR-0023 决策 ④；
serving 已双模型路由（§9.1 请求体 `model` 字段）+ demo 集成测试
（`make demo`，双语 12 + RLS 身份 2）；此前“不再新增”裁定基于无数据前提，已废除
（裁定推翻注记见 eval/gold/README.md，沿 ADR 推翻条件记录流程）

```yaml
version: "0.2.0.dev0"
semantic_model:
  - name: atlas_finance_analytics
    description: Atlas 金融语义模型，基于 TPC-DI 零售经纪基准重建，FIBO 概念对齐（ADR-0007）
    ai_context:
      instructions: >
        使用本模型进行金融场景分析（账户/交易/持仓/佣金）。
        涉及"交易额/成交额"时优先使用 total_trade_value……
        当用户问句存在歧义时，必须反问澄清，不要猜测。
    datasets:
      - name: fact_trades
        source: iceberg_rest.atlas.dwd_fact_trades
        primary_key: [TradeID]
        fields:
          - name: TradePrice
            expression:
              dialects:
                - dialect: ANSI_SQL
                  expression: TradePrice
            datatype: Decimal
            ai_context:
              synonyms: ["成交价", "成交单价"]
    relationships:
      - name: trades_to_date
        from: fact_trades
        to: dim_date
        from_columns: [SK_CreateDateID]
        to_columns: [SK_DateID]
    metrics:
      - name: total_trade_value
        description: 总交易额（成交数量 × 成交单价）
        expression:
          dialects:
            - dialect: ANSI_SQL
              expression: SUM(fact_trades.Quantity * fact_trades.TradePrice)
        datatype: Decimal
    custom_extensions:
      - vendor_name: ATLAS
        data: |
          {
            "governance": { "owner": "finance@atlas.local", "version": 1, "status": "active" },
            "lineage": { "source_columns": ["fact_trades.Quantity", "fact_trades.TradePrice"], "dependencies": [] },
            "fibo_alignment": {
              "layer": "L2",
              "fibo_commit": "119fa8c091aa4beece7d22aefa6fe138021a4355",
              "mappings": {
                "fact_trades": {
                  "concept": "https://spec.edmcouncil.org/fibo/ontology/FBC/FinancialInstruments/FinancialInstruments/SecuritiesTransaction",
                  "match_type": "exact_match"
                }
              }
            }
          }
```

**对照参考**：`apache/ossie/examples/tpcds_semantic_model.yaml`（631 行 TPC-DS 示例）
—— 零售历史语义模型（`atlas_retail`）的建模参考；金融主模型的数据集为 TPC-DI（ADR-0006）。

### 4.3 第一约束：同一业务词只有一个权威定义

同名指标（无论 status）由 ossie_validate 跨文件全局唯一强制（AGENTS.md N8），指标演进一律采用
**新名 + `supersedes` 指向旧名**。`supersedes` 非空时**必须**填 `reason`、`migration_window`
（schema if-then 强制）；governance_validate 对 supersedes 链做跨文件语义校验：取代目标存在、
非自身、新版本号严格大于被取代版本（版本递增天然防环）、被取代者不得仍为 active
（发布新版本前须先置 deprecated）。契约测试 `tests/test_governance_validate.py` 10 例，
由 `make lint` 执行（本地门槛；当前远程为 gitee，GitHub Actions 不执行——ADR-0018 ⑥）。
（这套治理元数据挂在 custom_extensions 的 `governance` 节点下。）

### 4.4 与开源方案的关系

| 方案 | 定位 | Atlas 的关系 |
|---|---|---|
| **Apache Ossie** | 语义元数据交换规范 | **主规范**（ADR-0002） |
| dbt MetricFlow | 指标即代码，生态成熟 | 通过 `make export` 导出兼容，证明非封闭 |
| Cube | 集中指标 + 访问规则 + 多 API 暴露 | 接口兼容目标 |
| Apache Kylin | 预计算亚秒 OLAP | 不做主方案（固定报表场景更合适） |
| Apache Calcite | SQL 解析/校验/CBO 框架 | **MVP 不引入**，见 ADR-0005 的诚实取舍 |

### 4.5 校验

```bash
make lint-ossie        # Ossie 官方 ossie-schema.json 校验
make lint-governance   # Atlas 治理扩展 JSON Schema 校验
make export            # 导出 dbt MetricFlow YAML
```

> ⚠️ **Ossie 0.2.0.dev0 是 DRAFT**，schema 可能变化。
> 锁定时必须在 README 记录 `apache/ossie` 的具体 commit sha。
> 若 Ossie 未能从孵化器毕业，迁移路径见 ADR-0002「什么情况下应该推翻」。

---

## 5. NL2SQL：如何评测

### 5.1 两个指标分开报，不许混

| 指标 | 定义 | 用途 |
|---|---|---|
| **EX**（Execution Accuracy） | 执行结果与固定快照一致的占比 | 主指标，反映真实可用度 |
| **Plan Acc** | 逻辑计划（指标/维度/筛选/时间）与标注一致 | 用于定位错误发生在"理解"还是"生成" |

禁止用 Spider 分数代替企业分数，禁止用训练集准确率代替测试集准确率。

### 5.2 评测集

```text
eval/
├── gold/        # 自建黄金集：106 条样本（finance 79 + retail 27），人工标注 + FIBO 概念标注
│                # 另含 13 条 paraphrase 改写样本（不同产物类型，不共用 schema，见 KL #31）
├── spider/      # 历史对照判定：通用领域与金融场景不匹配，不再新增接入
├── bird/        # 历史对照判定：BIRD finance 不再新增（2026-09-03，见 eval/bird/README.md）
├── runner.py    # 固定快照 + expected result hash + CI
└── reports/     # 每次 commit 自动生成
```

每条黄金集样本结构：

```json
{
  "id": "gold-101",
  "question": "2013 年第二季度总交易额是多少？",
  "expected_metric": "total_trade_value",
  "expected_dimensions": [],
  "expected_time": "2013Q2",
  "expected_sql": "<人工标注 SQL>",
  "result_hash": "<固定数据快照下的执行结果哈希>",
  "snapshot_sha": "<git rev-parse HEAD of data snapshot>",
  "tags": ["aggregation", "time_range", "fibo_alignment"],
  "fibo_concepts": [
    { "term": "交易额", "concept": "…/FND/Accounting/CurrencyAmount/MonetaryAmount", "role": "measure" }
  ]
}
```

### 5.3 评测执行（eval/runner.py）

链路：`gold 问句 → Planner → Plan（记 Plan Acc）→ Compiler → Guard（只读 + 表白名单=快照内表）→ Doris 执行 → 结果 sha256`，与标注 `result_hash` 一致即 EX pass。

- 评测只认当前 HEAD：启动时复核 `data/snapshots/<sha>.meta.json` 数据指纹，漂移即拒绝出报告
- 首轮执行自动锚定：gold JSON 的 `result_hash` 占位符回填为实测 sha256（`snapshot_sha` 同步绑定），此后比对即 EX
- 歧义样本（`ambiguous: true`）要求返回澄清反问；反问命中 = pass，不猜
- 产出：`eval/reports/<git sha>.json`（**报告覆盖缺口如实登记（2026-09-14 实测）**：`eval/reports/` 下 48 份产物中含 7 份按域分节的评测主报告，其中最新一份非 dry 主报告是 `5d1e22b.json`（2026-09-09T11:41:32+08:00，金融 71 + 零售 20 = 91 例），**无任何报告覆盖当前 106 份样本**；机制性原因见 ADR-0017 代价 ③（`eval/runner.py` 的 `enforce` 位于 `try` 之外，Guard 拒绝冒泡中断整轮），须待 P-1 批次判据 3/4 修复后才可能产出覆盖 106 的报告。以下为历史引用（数字为当时报告的真实内容，不改写）：`b933e20` 报告金融域 70 例（65 可解析 + 5 歧义）Plan Acc 65/65、反问 5/5、EX 65/65；零售域 19 例（18 + 1）Plan Acc 18/18、反问 1/1、EX 18/18——29 表全量快照，锚定 hash 未漂移，按域分节不混报；基线分析 `eval/reports/baseline-compiler-b933e20.json` 与 `docs/baseline-compiler.md`——注册语义域内确定性链零 LLM 覆盖：金融 70/70、零售 19/19；域外问题由 RAG+LLM（gold-50 时代实测 44/44 持平，`rag-llm-openai-7d48dcb.json`，历史对照不混报）/LoRA（blocked）策略对照承接）
- 闭环（Day 39-42 后）：`make report` → `eval/report.py` 机械转述生成 `EVAL_REPORT.md`（八节，无手写数字，每格带 source 列）；CI 回归 `.github/workflows/eval.yml`（dry Plan Acc 自洽断言；完整 EX 需数据环境，手动触发）；失败样本 `eval/failure_collect.py` 自动归类 → 人工确认 → `lora.flywheel` 五阶段进 SFT（answer 形态 = 合法 Plan JSON，ADR-0008）；LLM 实测后仍 0 失败（缺陷在评测侧修复归零），飞轮空转与 LoRA 训练（无 GPU）如实登记

### 工作台证据清单（ADR-0031 T01）

```bash
.venv/bin/python -m eval.workbench_baseline --output "eval/reports/workbench-baseline-<唯一名称>.json"
.venv/bin/python -m eval.report --workbench-report "eval/reports/workbench-baseline-<唯一名称>.json"
```

这是代码与历史证据的清单，不是效果评测：记录完整代码 SHA、工作树摘要、历史报告与快照文件摘要，拒绝覆盖已有输出。规则、规则+LLM、SFT、RL 均为 `not_run`；Schema 不接受当前效果数字，文件存在也不代表接线或真链成功。测试见 `tests/test_workbench_baseline.py`；测试装配保留真实 API/Planner/Compiler/Guard，仅替换外部数据库执行器。

### 控制库与默认流程合同（ADR-0031 T02）

M0 T02 交付发布**原语**（不是发布功能）：`serving/control/store.py` 提供 SQLite 控制库（编号迁移+备份回滚、active 指针 CAS、草稿修订与状态机；私有文件 0600）；`agent/runtime/bundle.py` 装配不可变制品（逐文件摘要校验、拒绝未声明文件与重复键 JSON、默认流程必须存在且只声明已注册节点）；`agent/flows/contracts.py` 与 `templates/{query,analysis}.json` 定义并校验默认流程合同（D05：12 种 NodeType 词表仅 9 种已注册、边分支穷尽互斥覆盖五状态、预算上限）。测试：`tests/test_control_store.py`（7）、`tests/test_runtime_bundle.py`（14）、`tests/test_flows_contracts.py`（33）、`tests/test_runtime_rules.py`（5）。没有发布 UI、审核放行（T08）或运行目录/实时事件（T05/T06）；OIDC 登录见下节（T03）；不自动切换真实发布。

### 身份登录与控制权限（ADR-0031 T03）

M0 T03 交付私有部署的认证与控制 ACL（**测试全绿，真 IdP 验收待部署环境**）：`serving/control/auth.py` 控制面授权核心（Principal → `authorize(principal, action, resource_scope)`，违规 `ControlForbidden`；数据角色与控制能力分离——viewer 不能发布，即使数据角色是 hq_admin）；`serving/control/oidc.py` OIDC BFF（Authlib；一次性 state/nonce/PKCE、固定 RS256 验签与 issuer/audience/exp 校验、服务端内存会话 + CSRF、退出即失效）；`serving/control/routes.py` `/api/v1/auth/{login,callback,logout,session}` 四端点（统一错误体，配置阻塞三态 503，不伪装登录成功）；前端顶栏登录入口（`frontend/src/panels/auth/SessionEntry.tsx` + `state/auth.ts` 纯逻辑）——探测 `/auth/session` 三分法，**开发角色选择仅演示模式可见**。测试：`tests/test_control_auth.py`（11）、`tests/test_oidc_login.py`（30，受控 mock IdP 真实签/验 RS256）、`tests/test_auth_endpoints.py`（21，含 cookie 流、过期/重放/跨域/CSRF 缺失/双凭据）；前端 `src/__tests__/auth.test.ts`（14）+ `make ui-check` 全绿。**诚实边界**：真 IdP 回调验收 **BLOCKED**（需部署者提供 issuer/client 环境，不能声称私有登录已在真实 IdP 上可用）；私有模式下工作台/治理面仍走 Bearer（Cookie 会话是另一条认证链，`/runs` 通道属 T05/T06）；控制身份与数据授权分离尚未启用发布路由（T08）。

### 共享安全执行内核与数据身份（ADR-0031 T04）

M0 T04 把在线执行收敛为**唯一内核**并补齐数据身份（**测试全绿，锁定快照真链等价 6/6**）：`agent/runtime/execution.py` 共享执行内核——`execute_plan(Plan, RunContext)`（编译 → 身份策略 → Guard → 执行计时 → ExecutionValidator）与 `execute_guarded_sql`（Guard → 执行），失败三态 `ok/blocked/error` 用 `executed` 显式区分「未执行被拒」与「执行后失败」（耗时只计实际执行的子 SQL，ADR-0026 决策 ③）；缺编译器/执行器一律 fail-closed，不回退默认。`agent/runtime/context.py` `RunContext`（预算/执行器/身份/编译器/数据身份）+ `budget_from_snapshot`。`agent/runtime/connectors/{base,doris}.py` 连接器合同与 Doris 实现（只注册 Doris，不开放 generic SQL URL；`execute_sql` 自 `eval.runner` 逐字迁移，评测改为委托共享实现，不再反向成为在线执行工厂）。`agent/runtime/identity.py` 数据身份判别联合：`SnapshotDataIdentity`（可重放）/ `LiveDataIdentity`（结构性无伪 `snapshot_sha`），`analysis_refusal` 对 live / 未绑定返回 `analysis_consistency_unavailable`（四步分析一致性门不编造对账）。改造成委托的在线入口：`agent/graph.py` `node_execute`、`agent/tools/registry.py` `execute_readonly`、`agent/factory.py`、`agent/cli.py`、`eval/runner.py`——结构测试锁定（graph/registry 不再 import Guard `enforce`、graph 不再实例化 `ExecutionValidator`、factory/cli 不再 import `eval.runner`）；graph 侧保留非 dict 身份的 fail-closed 前置拒绝（绝不静默丢弃身份降级为无策略执行）。测试：`tests/test_source_identity.py`（17）、`tests/test_runtime_execution.py`（26）。**真链等价验证**（临时脚本，跑完即删）：在锁定快照 `1e2e557`（`source=latest`，`bound_to_head=false`，未改任何数据）上 6/6 用例（含 hq_admin `1 = 1` 与 branch_manager `dim_broker.branch = 'BR_A1'` 两条 RLS 注入；实测 2015 佣金收入 4042004.72、NASDAQ 过滤 340523496.3300）旧入口（`python -m agent.cli query`）与新入口（`DataAgent.ask`）guarded SQL 逐字相等、列与行值相等；MCP→registry→内核→Doris 链 1/1 等价（5 行结果一致，耗时 258.4ms）。**诚实边界**：live 在线源四步分析明确拒绝（`analysis_consistency_unavailable`）；`DorisConnector` 能力声明 metadata_probe/cancel_query/snapshot_read/consistent_analysis 全 False（未实现不多报）；内核不向 Guard 传 `model=`（时间窗回退与跨表谓词补 join 的 model 通道保持现状，不借重构扩大行为面）；本内核只覆盖「Plan/SQL → 结果」路径，不做图编排、发布切换或在线分析。

### 真实事件、运行恢复与最小反馈（ADR-0031 T05）

M0 T05 把工作台运行落为**控制库中的真实事实**（**测试全绿，运行面五套 56 项**）：控制库迁移 002 新增 `runs / run_events / artifacts / feedback` 四表；`serving/control/runs.py` 运行服务——提交幂等（`client_request_id` 为客户端幂等键：同键同内容返回原 run、同键不同内容 409，重试命中不重复入队、不重复执行）、执行在**单业务队列**（进程内单工作线程 FIFO 串行，`peak=1` 测试锁定；提交请求只持久化+入队，不在请求内执行 SQL）、重启恢复 `recover_interrupted()` 把遗留 queued/running 一律封 `interrupted` **不重跑**（旧登录态与授权可能已失效，重跑会以旧身份执行新数据）；结果正文**只在进程内存保留**（15 分钟窗口按会话最近合计时，重启/登出/权限撤销即不可读；不落盘、不进事件、不伪装可重放），`result_availability` 五态 pending/available/not_retained/expired/restricted（跨 owner 或作用域外与不存在同为 404，不泄露存在性；operator 仅得 restricted 摘要）。`serving/control/events.py` 事件接缝 EventSink——seq/event_id/occurred_at/release_id 一律由服务端在事务内分配，payload 是脱敏摘要通道（问句/SQL/结果行/Prompt 不进），写入失败 fail-closed（下一次 SQL 不得启动）；图侧 `agent/graph.py` 以 `event_sink` 接缝提交真实事件（RUN_ACCEPTED / RUN_STARTED / NODE_* / EDGE_TAKEN / TOOL_* / STATE_SNAPSHOT / RUN_FINISHED / RUN_INTERRUPTED，节点内子调用留痕；运行图与时间线由**同一事件流**归约，不从 UI 时间推断执行；`event_sink=None` 时旧行为逐字节不变——CLI/评测零开销）。**显式捕获**（D13）：授权审计在建单时同步执行，未装配/写失败/被禁用一律拒绝执行（run 封 `failed`、零 SQL 启动、503 `audit_unavailable`，不降级放行）；正文按白名单字段（question / node_io / result）物化为私有 artifact，且只物化实际发生的字段——被拒/失败 SQL 不进持久化。**最小反馈**（D13）：能力先判（403），他人或未知 run 与不存在同为 404；固定 `pending_review` / `training_eligible=false`（跨用户审核队列与训练资格属 T12）。**事件写入与 OTel 导出分离**（故障注入验证）：埋点故障不阻断运行、不改变事件流——故障 run 与正常 run 的事件类型序列逐字一致。四端点已入契约（`POST /runs` 202 收据 `{run_id,status,release_id}` / `GET /runs/{run_id}` 固定 10 键 / `POST /feedback` 201 恒 9 键 / `GET /feedback` 仅本人新→旧；EXPECTED_PATHS 22→25，`frontend/src/api/endpoints.ts` 双向相等）。**诚实边界**：`analyze` 模式运行暂 422 `mode_not_supported`（不静默降级成 ask）；SSE 实时推送、历史列表与运行图界面已由 T06 交付（见下节；本节交付事件持久化与查询原语）；保留期到期清理为原语、无后台调度（到期不自动删除，读取侧由五态与 `retain_until` 拒绝复用）；旧同步入口不加 `X-Atlas-Run-Id` 响应头（旧入口无 deployment 概念，不造假 ID 冒充追踪）；运行/反馈面未接限流桶（429 在该面不可达）且 `GET /feedback` 未分页——如实登记，不伪装已支持。

### 运行历史、运行图与真实 SSE（ADR-0031 T06）

M0 T06 把控制库的运行事实变成**可浏览、可续读、不重跑**的工作台视图（**后端 +10 / 前端 +52 项新测试全绿，浏览器实测覆盖**）：后端四端点——`GET /runs` 目录（游标分页默认 50 / 最多 100，行 = RunSummary **固定 12 键**仅摘要不含正文/问句/结果；域/时间/状态过滤，参数非法 422）、`GET /sessions` + `GET /sessions/{session_id}` 会话目录与回合（SessionSummary 固定 6 键；全部由控制库运行事实聚合，**非 checkpoint dump**、不读正文；未知与不可见统一 404）、`GET /runs/{run_id}/artifacts/{artifact_id}` 捕获正文钻取（对象 ACL 先判——他人/未知 404、摘要 403；**只回 capture 授权字段**；清理后 410 保墓碑、不返回正文）、`GET /runs/{run_id}/events` **真 SSE**——按 seq 回放**持久事件帧**（`id:` / `event:` / `data:` 三行，schema_version=1），`after_seq` 参数或 `Last-Event-ID` 头断线续读（前者优先；非负整数，非法 422），无法补齐（游标越界/事件缺口）410；建立连接即重新授权（401/403/404/410 同步返回、不进流）、心跳 `: ping`、终态发完即止；**订阅只读事实、不触碰执行器**（断线仅断开订阅，不重做查询）。前端 8 个新模块/组件——`api/control.ts`（类型化客户端）、`api/run-stream.ts`（SSE 消费；`Authorization` 头，token 不进 URL）、`state/run-events.ts`（`reduceRunEvent(state, event)` 按 seq 去重归约：重复帧不产生第二次工具调用、收到开始事件时任务尚未结束，**不从 UI 时间推断执行**）、`panels/runs/run-view-model.ts` + `RunPanel.tsx`（目录 + 详情：固定 10 键视图、五态 `result_availability` 如实显示「正文未保留/已过期」、结果 DataTable + 截断 Alert + 反馈提交）、`RunTimeline.tsx`（每一帧 = 一条真实事件；EDGE_TAKEN 即走过的边）、`RunGraph.tsx`（**固定节点/边模板与 Agent 图定义同源 + 事件流高亮**，图例五态；不把模板当执行路径——实际路径以事件流的 EDGE_TAKEN 为准）；会话面板补服务端会话目录（与标签页内存日志双面如实标注）。**许可门**：`@xyflow/react`（React Flow 12，MIT）经 `make license-check`（PASS 5/5）核验后引入，本任务用于只读图，T09 复用组件开放编辑、不倒置依赖。**浏览器实测**（chrome-devtools，2026-09-22；临时种子仪器 `serving/state/t06e_seed.py` 造 release `c0c039c4…` + 真实 Doris 执行两条运行）：未认证态如实「需要认证」→ 激活后目录列出真实运行（成功/ask/available/14 帧）→ 详情渲染 8 节点 + 5 态图例 + 14 帧时间线 + artifact 钻取（保留期与字段如实「保留至 2026-09-25T12:00:00+08:00；字段：result, question」）→ **刷新并重激活后历史仍在**（退出门：`GET /runs` 服务端历史，非标签页内存；浏览前后 run 的 `updated_at`/`last_seq` 不变——未重复执行）→ 无捕获运行如实「本次运行未捕获正文（请求未包含 capture 授权）」→ `/sessions` 深链 `/runs?run=<id>` 自动选中 → 反馈提交入 `pending_review`；旧 `/analyze/stream` 契约回归真跑 200（归因分析正常渲染，页面自述「确定性两期变化分解」，**不标实时/流式**——compute-then-stream 措辞如实）。`make ui-check`（tsc + vitest 24 files/184 passed + build）与 `make license-check` 全绿。**诚实边界**：运行图为「固定模板 + 事件流高亮」而非从事件流**重构**拓扑；运行创建无 UI（提交仍走 `POST /runs`，本次验证经 curl 造数）；SSE 是持久帧回放（断线续读）而非持续推送语义；运行面限流桶仍未接（429 在该面不可达）、`GET /feedback` 未分页（T12 承接）。

### 只读数据源接入向导（ADR-0031 T07）

M0 T07 把「已有 Doris → 可配置只读源」做成受控管理面（**后端 +30 / 前端 +13 项新测试全绿，真实 Doris smoke + 浏览器实测覆盖**）：`serving/control/sources.py` 源服务——**追加式修订**（同 source 新建修订版本递增、旧行不可变，已发布绑定引用的修订不被原地改写），秘密只收环境引用 `env:<NAME>`（明文密码/完整 DSN 在合同层 422、不进入服务路径，N9）；目标校验在**连接建立之前**完成——凭据从环境解析（缺失 → `credential_missing` 且零连接）、`_forbidden_address` 硬禁云元数据/link-local/多播（环回不拒）、DNS 解析结果必须命中允许列表且**按校验过的 IP 字面量连接**（防重绑定）、表越界双重校验（合同层 + 存储层探测前）。**受限探测**（`POST /manage/sources/{id}/probes`）固定目录查询、**无请求体**（不提供任意「测试 SQL」通道）、结果 = 带时间戳的证据（`observed_at`/`probe_id`/`schema_digest`/能力声明，`reproducible` 恒 false——历史结论不当作本次事实），9 类阻塞原因逐条中文展示；在线探测结果**不写 EX**（工作卡）。**Doris 4.x `SHOW GRANTS` 表格形态适配**（16 列、单元格 `atlas.dwd: Select_priv; …`；2026-09-22 真实连接实测发现的单测盲区，TDD 红→绿修复）：数据权限列（Global/Catalog/Database/Table/Col Privs）规范化为 `GRANT <PRIV> ON <scope>`；资源类列跳过（`normal: Usage_priv` 是 Doris 用户默认项，不授予数据表读写）；**Roles 非空或未知 `*Privs` 列原文保留 → fail-closed**（role 权限在 SHOW GRANTS 中不展开，不凭空声称 read_only——实证后定策）。**部署绑定**（`POST /manage/deployments`）创建即 draft（`active_release_id=null`，D13；请求夹带 `active_release_id` → 422）——发布/激活是 T08 的显式 CAS 动作。前端面板 5 `SourceWizard.tsx`（导航「接入」`/setup`）：表单先行拒绝明文凭据（不是「把秘密送进请求再让后端 422」的通道）、探测结果如实区分「本次探测证实 / 未证实」、部署行 `draft（未绑定发布）`。**真实 smoke**（2026-09-22；Doris 容器 healthy、`atlas_ro` 只读账号 `GRANT SELECT_PRIV ON atlas.dwd.*`）：HTTP 全绿——探测 ok（`probe_id=b3ab2cc4…`、`observed_at=2026-09-22T08:43:50.710918+00:00`、`engine_version=5.7.99`、`schema_digest=b9a22a32…`、capabilities read_only/metadata_probe=true 其余 false、`reproducible=false`）；浏览器（chrome-devtools）走完 未认证态 → 粘贴激活 → 登记修订 v1→v2（追加式）→ 受限探测通过（观察时间 `2026-09-22T08:46:38.981576+00:00`、只读连接/元数据探测「本次探测证实」、其余「未证实」）→ 创建 draft 部署 `finance-wizard`，部署列表并列展示 T06 的 `finance`（`active_release_id=c0c039c4…`）与其 draft。`make ui-check`（tsc + vitest 25 files/202 passed + build 1,762.88 kB / gzip 545.15 kB）与 `make license-check`（5/5）全绿。**诚实边界**：探测后源目录行「最近探测」列不自动刷新（探测结果面板即时更新；下一次 reload 动作或刷新页面后列表更新）；仅注册 Doris（不把协议兼容当数据源支持）；`cancel_query`/`snapshot_read`/`consistent_analysis` 能力声明仍 False（未实现不多报）；管理面端点未接限流桶（与运行面同状，如实登记）。

### 语义草稿、审核与 Git 制品发布（ADR-0031 T08）

M0 T08 把「改语义」做成**草稿非权威、制品靠显式 Git commit、切换靠 CAS 指针**的受控链路（**后端 +46 / 前端 +26 项新测试全绿，真实 Git 往返 smoke + 浏览器全流程覆盖**）：`serving/control/drafts.py` DraftService——草稿生命周期（create/edit/view/list/validate/review/export_patch；编辑仅限本人、审核跨人可见同域草稿），编辑改内容即撤销放行（旧审核失效）；`validate` 跑三套确定性校验器（结构/治理/策略）落**固定 8 键证据行**并推进 validated，`review` **只认 validated**、approved 绑定内容摘要，`export_patch` 输出最小统一 diff（base = `base_git_sha` 对象库文本、不读脏工作树；未编辑即空串）与影响面（指标/维度增删改）。`serving/control/releases.py` ReleaseService——`import_bundle` 只收**完整 40 hex 且对象库可达**的显式 commit（不接受工作树/索引/引用），白名单收集（符号链接/子模块/超大文件拒绝）→ 内容 ≠ 已审核草稿 409 → 制品整体确定性门禁（结构/治理/策略/值域/编译往返）→ 登记**不激活**、草稿推进 `release_ready`；`publish`/`rollback` 为**显式 CAS**（expected 必须等于当前指针、首发 null），域/源不匹配与无当前只读探测证据均 409。**制品身份 = 内容摘要（release_id == content_digest）且源码 commit 参与身份——同内容不同 commit 是不同 release**；**运行中切换不串版本**（运行固定 Manifest；规则/索引缓存键含制品摘要）。前端 `SemanticDraft.tsx`（导航「接入」`/setup`；含客户端显式 CAS 的 PUT `If-Match`）与治理面 `PublishIdentitySection.tsx`（只读显示各部署当前活动发布；**显式点击加载**——不占治理页挂载即发 8 条集合的限流口径）。**真实 Git 往返 smoke**（2026-09-23；`serving/state/t08d_smoke.sh`，gitignored 临时仪器；**执行者不代用户提交**——制品来源为既有 commit，全程只读对象库、无 git 写命令）：草稿 A（base=服务端取 HEAD `eb52974`）→ 校验 passed → 审核 approved → patch 空（内容与 base 逐块一致）→ 导入 `ce213ac` 得 **R1 `9c980bd8…`** → 首发 CAS（null→R1）→ 导入 `eb52974` 得 **R2 `3aea28b8…`** → 发布 R1→R2 → 回退 R2→R1（rev4）。**浏览器全流程**（chrome-devtools + browser-use；vite dev 5173 代理 8300）：未认证态如实「需要认证」→ 粘贴激活（hq_admin）→ 界面创建草稿 C `38bb3627…`（document = 68,459 字符真实模型 JSON）→ 确定性校验「已校验（结构/治理/策略）」→ 导出 patch「无变更（内容与 base Git 版本逐块一致）」→ 审核 approved（`review_id=46fc15a2…`）→ 导入 `51ded72` 得 **R3 `5d59f1e2…`** → CAS 发布 R3（rev5）→ CAS 回退 R1（rev6）→ 治理页「加载发布身份」显示 finance-live active=`9c980bd8…`。**收口修正**：本节路径总数曾于 T08b 批次误写 48，实测 **43**（33 + T08a 2 + T08a-s2 3 + T08b 5；表格 45 行按合并口径展开 = 43，EXPECTED_PATHS 与 openapi 双向相等），本次已修正。`pytest tests -q` 1625 passed、14 skipped、534 subtests（= T07 基线 1579 + 46）；vitest 27 files/228 passed（= 25/202 + 26）；`make ui-check`（build 1,779.74 kB / gzip 549.96 kB）、`make lint`（106 条样本）、`make license-check`（5/5）全绿；ruff 33 / mypy 81 与基线持平（错误清单与 T08 改动零交集）。**诚实边界**：新语义只记录样例 smoke、**不伪造 EX**（无新固定快照即不产生评测数字）；smoke 与浏览器流程属一次性验证仪器（不入 Git）；「选表→元数据候选」交互式引导未实现（草稿新建为 `target`/`document` 直接编辑，全链可用）——如实登记。

### 理解合同、召回与完整绑定（ADR-0031 T10）

M0 T10 把「问句理解」做成**确定性归一 → 全目录召回 → 完整绑定**的合同链路，并把工作台的简单问数接到同一结构化 Plan 合同上（**后端 +63 / 前端 +14 项新测试全绿**；**默认图尚未接理解节点——本批交付的是独立合同与工具链，当前无生产调用方**，如实登记）：`agent/intent/normalize.py` 承接 D09 槽位做确定性归一（证据三重校验、时区固定时钟、原句保留 + 有限扩展的边界、相对时间只摘录不猜测、`unresolved_slots` 显式化）；`retrieve.py` **授权 ∧ 已注册全目录召回**（不被旧 top-K 蒙蔽、候选来源去重、RRF 融合 K=5，检索与绑定同预算）；`bind.py` 槽位**完整绑定**为完整 Plan——未绑定槽位只能澄清（`plan_candidate=None`、澄清 `unresolved_slots` 逐条列出），**理解成功但条件掉落的路径不可发布**（T10 退出门）；`agent/graph.py` Generator 显式候选接缝（keyword-only candidates 逐字渲染、重试沿用同轮候选，**移除隐式二次检索**）。**gold 最小差异样本**（`eval/gold/intent`：10 条 5 对——包含/排除、金额/笔数、分组/筛选、上月/去年、明确/歧义；schema 互斥校验 bound↔plan、澄清↔plan=null；来源族 `finance-manual-v1` split=test 冻结，只评测不进训练）与 `eval/workbench_intent.py` 双引擎对照（规则：Recall 10/10、Plan 9/9、澄清 1/1；内网自托管 LLM `deepseek-v4-flash`：9/10、8/9、1/1、11080 tokens，intent-04 输出非 JSON → **fail-closed 如实失败不重试**；**Recall 与 Plan/EX 分开报告、未配置模型即 blocked 不产数字**）。前端工作台（T10f）：`PlanComposer.tsx` 指标/时间/筛选**结构化控件**（选项取自治理面 metrics/dimensions、排除 is_time 列——不硬编码词表；筛选算子与 `agent/compiler.py` 的 `_compare` 逐字一致）与 `lib/plan-contract.ts` **唯一请求体构造点**——控件与计划卡片的 `/compile`、`/plan/execute` 只发结构化合同（执行体 8 键恒不含 question，缺省由后端从 Plan 生成规范文本），校验先行（指标必选/时间配套/筛选行完整性在提交前拦截）。门禁：`pytest tests -q` 1697 passed、14 skipped、536 subtests（T10a-d +63 / T10e +9）；vitest 28 files/242 passed（+14）；`make ui-check` 与 ruff 33 / mypy 81（与基线持平）全绿。**诚实边界**：意图接口尚无生产调用方（默认图 understand→retrieve→bind_plan 接线在后续批次）；LLM 对照数字仅标识内网自托管环境、不构成对外模型结论；**T10 不训练模型、不替换已覆盖规则答案**。

### 最小发行、运维与首次接入验收（ADR-0031 T13）

M0 T13 把「运维与发行」做成受控入口（**后端 +19 / 前端 +1 项新测试全绿**；compose.connect.yml 无默认秘密、Makefile 运维入口、诊断端点源故障仍 200）：`serving/control/maintenance.py` 备份/恢复原语——`backup_control` 用 SQLite backup API（不直接复制活跃 WAL 文件，D12）、`restore_control` 先隔离验证再恢复（不自动恢复未完成 SQL/训练/旧登录态）、`BackupManifest`/`RestoreReport` 固定键摘要；`GET /manage/diagnostics`（D12/D13）管理面诊断——源故障仍 200、不返回 DSN/密钥/原始 Prompt、需 operator 能力（ops.read），展示源连接状态/发布完整性/存储可写性/schema 版本。`infra/docker/compose.connect.yml` 最小发行配置——连接已有基础设施的轻量部署（无默认秘密，N9）。Makefile 新增 `backup`/`restore`/`diagnostics`/`acceptance` 运维入口。`eval/workbench_acceptance.py` 首次接入验收脚本——记录动作与起止时间，输出 JSON 报告，不填主观体验数字。门禁：`pytest tests -q` 1716 passed、15 skipped、537 subtests（= T10 基线 1697 + T13 新增 19）；vitest 28 files/242 passed；路径集合 43→44（Python EXPECTED_PATHS + TS endpoints.ts 双向同步）；ruff 33 / mypy 85（mypy 新增 4 错误来自新文件，与基线清单零交集）。**诚实边界**：诊断端点展示的是注册状态而非实时探测结果；compose.connect.yml 需部署者提供环境变量（无默认秘密）；验收脚本不自动签发 token（诊断端点检查跳过）。

### 受约束图校验与拓扑编译（ADR-0031 T09）

M2 T09 把「图编排」做成**声明式合同 + 结构化校验 + 拓扑编译**的受控链路（**后端 +25 / 前端 +6 项新测试全绿**；默认模板零 issue 通过，execute_plan 必经路径验证）：`agent/flows/registry.py` 节点目录——`node_catalog()` 返回已注册节点类型（含端口合同），新增类型须同时更新代码版本；`agent/flows/validate.py` FlowIssue 结构化校验器——14 种错误码（unknown_node_type / branch_not_exhaustive / no_failure_exit / execute_plan_bypassed 等），接受 dict 或 FlowDefinition，port_mismatch 预留（非 ok 边携带转换数据，静态不可判定）；CLI `python -m agent.flows validate <path>` 直接校验模板文件。`agent/flows/compile.py` FlowTopology 拓扑编译器——`compile_flow()` 从 FlowDefinition 提取拓扑（edge_target 路由查询 / terminal_nodes / paths_to_terminal 全路径枚举 / budget 透传），不执行节点、不注入实现。前端 `frontend/src/api/flows.ts` FlowDefinition/FlowIssue 类型 + `validateFlowDefinition()` 客户端校验（与 Python validate_flow 同口径子集）；`frontend/src/panels/flows/flowLayout.ts` BFS 层级布局工具（entry/terminal/protected 标记）。门禁：`pytest tests -q` 1741 passed、15 skipped、537 subtests（= T13 基线 1716 + T09 后端 25）；vitest 29 files/248 passed（+6）；ruff 33 / mypy agent/flows/ 0 errors；`make ui-check` + `make license-check`（5/5）全绿。**诚实边界**：compile.py 只做拓扑编译（不构造 LangGraph 运行时），生产流量仍走 `agent/graph.py` 硬编码图；前端画布为布局预览（未接拖拽编辑/发布 API）；port_mismatch 校验预留未启用。

### 规则/Prompt/Tools 节点实验与发布（ADR-0031 T11）

M2 T11 把「节点工具访问」做成**受保护的中间层**（**后端 +19 / 前端 +5 项新测试全绿**）：`agent/tools/broker.py` ToolBroker——节点类型→工具白名单（understand 禁 execute_readonly、execute_plan 全工具、handoff 无工具等 10 种节点类型分别授权）、工具调用预算追踪、敏感度传播（high 时全部拒绝）、token 预算（输入 2048/输出 512/累计 10240，D05 BudgetSpec）。`agent/flows/llm_node.py` LlmNodeExecutor——受约束模型调用 + 工具循环 + 确定性回退（chat_fn=None 时回退到规则路径，不报错）；通过 ToolBroker 访问工具，不直接持有 DeterministicTools。`agent/prompts/node_action.yaml` 外置 Prompt v0.1.0（AGENTS.md §7.4：禁止内联）。`serving/control/experiments.py` ExperimentService——单节点对照实验（不改 active、不执行在线 SQL、release ID 64 hex 校验、节点类型注册表校验）。前端 `frontend/src/panels/optimization/NodeExperiment.tsx` 实验表单（baseline/candidate SHA + 节点类型 + 数据集版本）+ 结果展示 + 历史占位。门禁：`pytest tests -q` 1760 passed、15 skipped、537 subtests（= T09 基线 1741 + T11 后端 19）；vitest 30 files/253 passed（+5）；ruff 33 = 基线（零新增）；mypy 新文件零错误；`make ui-check` 全绿。**诚实边界**：LLM 节点首版为骨架（chat_fn=None 走确定性回退；完整 JSON action 工具循环随后续迭代补齐）；ExperimentService 首版只记录实验不真实执行节点对照（需加载两个 release bundle）；前端面板 API 集成待完成。

### 5.4 准确率提升手段（按优先级）

1. **Schema Linking 两阶段**：图域约束粗筛 → BM25 + 向量召回 → 列级 rerank → JOIN 可达性验证
2. **自洽投票（self-consistency）**：多次采样，按执行结果聚类取众数
3. **执行结果校验**：空结果 / 全 NULL / 行数异常 → 触发修正或澄清
4. **错误反馈修正**：失败样本进 `eval/failures/`，人工确认后进 SFT 飞轮
5. **多轮澄清**：问句歧义时主动反问，而不是猜

---

## 6. 安全红线

**只读网关是硬约束，不允许任何绕过路径。**

```python
# agent/security/sql_guard.py（设计示意，实现见源码）
READONLY_AST_RULES = [
    Insert,
    Update,
    Delete,
    Drop,
    AlterTable,
    Grant,
    Copy,  # 全部禁止
]
```

强制执行链（顺序不可调换）：

```
parse(AST) → 禁 DDL/DML → 函数黑名单 → apply LIMIT → apply 时间范围
          → 注入行级策略谓词 → 成本估算 → 超预算拒绝 → 执行
```

必须覆盖的边界：CTE、子查询、视图展开、动态 SQL、函数黑名单、权限表达式注入。

**PoC 阶段不开放任何写操作。** 数据库账号本身也必须是只读账号（纵深防御，不依赖单一层）。

---

## 7. 数据来源与诚实边界

### 7.1 本项目使用的数据

| 数据 | 用途 | 来源 |
|---|---|---|
| TPC-DI（零售经纪，2012-07-07~2017-07-07 实测数据段；已装载 17 张 ODS + 8 张 DWD，快照 `b47a6c1`） | 主场景数据：重建金融库表关系、指标场景 | TPC 基准；不入库，使用者自行获取（见 §12） |
| FIBO 本体（FND+FBC+BE 域）+ OMG Commons/LCC | 语义锚点：概念 IRI 注册表与对齐映射 | FIBO：EDM Council（MIT）；OMG：仅 IRI 字符串入库，条款未存（见 §12） |
| BIRD finance | 公开集能力对照（仅参照，不混报） | 公开学术基准 |
| 自建黄金集（实测 106 条：金融 79 + 零售 27；另 13 条 paraphrase 改写样本） | 主评测集 | 本人基于 TPC-DI（金融）/ TPC-DS SF0.1（零售）人工标注 |

### 7.2 明确声明

- ❌ 本项目**不使用、不包含**任何前雇主/现雇主的真实数据、代码、指标定义或业务指标
- ❌ 不使用真实公司名构造场景；实体关系由 TPC-DI 重建
- ❌ 公开集分数**不外推**为企业场景分数
- ✅ 已有工程经验（SQL LoRA、GraphRAG 检索、OTel 可观测、YAML 编排）作为**设计输入**，在本项目中以公开数据重新实现

---

## 8. 目录结构

```text
atlas-data-platform/
├── .github/workflows/   # GitHub Actions（lint / eval 回归 / tag 自动版本锚点；当前远程为 gitee，不执行——ADR-0018 ⑥）
├── semantic/
│   ├── ossie/           # ⭐ Apache Ossie 语义模型（主规范）
│   ├── governance/      # ⭐ Atlas 治理扩展 Schema（补 Ossie 缺口）
│   ├── policies/        # 行级权限策略
│   └── schema/          # ⚠️ 已废弃：早期自研 DSL，仅作演进对照
├── sql/                 # tpcds_ddl（零售历史，只读）/ dwd / dws / views（金融表待建）
├── metadata/            # SQL 元数据抽取器（候选提取，非计算层）
├── airflow/             # yaml_jobs（源）+ dags/generated（自动生成，勿手改）
├── agent/               # graph（LangGraph 状态机）/ planner / compiler / security / feedback /
│                        #   tools（registry 四件套 · mcp_server · chart · broker）/ cli / prompts /
│                        #   runtime（共享安全执行内核 + Doris 连接器 + 数据身份，ADR-0031 T04）/
│                        #   intent（意图归一 / 全目录召回 / 槽位绑定，ADR-0031 T10）/
│                        #   flows（声明式图编排：合同 / 校验 / 拓扑编译，ADR-0031 T09）/
│                        #   jev_engine.py（JEV 决策引擎接入，ADR-0030 proposed）
├── retrieval/           # bm25 / milvus_client / graph_store
├── serving/             # api（HTTP 服务面 v1，ADR-0012）/ auth / control（控制台 API：
│                        #   运行/事件/反馈/源/部署/草稿/发布/诊断/备份，ADR-0031）/ mcp_stdio
├── frontend/            # 前端控制台工程边界（ADR-0018；P0b：构建链 + 端点常量，界面属 P1~P3）
├── observability/       # otel / dashboards
├── eval/                # gold（含 intent 差异样本）/ spider / bird / runner / reports /
│                        #   workbench_*（基线/意图/验收评测脚本，ADR-0031）
├── lora/                # SQL 适配器训练与数据飞轮 / intent RL 训练管线（intent_*）/ registry
├── infra/               # docker（含 compose.connect.yml 最小发行）/ adr（累计 14 篇，含 0030 JEV 引擎 / 0031 可信工作台）；ci 为历史遗留草案（活动 CI 在 .github/workflows/）
├── docs/                # 验收记录、发布文案、术语表、素材图
└── data/snapshots/      # 固定数据快照（记录 sha，保证评测可复现）
```

**约定**：`airflow/dags/generated/` 下所有文件由 YAML 生成，**禁止手工编辑**。

---

## 9. 命令速查

| 命令 | 作用 |
|---|---|
| `make install` | 安装依赖 |
| `make up` | 启动基础设施 |
| `make seed` | 装载 TPC-DI Batch1 → Iceberg → 锁定快照 |
| `make lint` | 校验语义层定义（JSON Schema + 唯一性 + 血缘） |
| `make plan Q="..."` | 问句 → 指标计划（不执行） |
| `make compile` | 计划 → 只读 SQL |
| `make ask` | Data Agent 多轮问数（真实 Doris + 锁定快照；无参数进交互会话） |
| `make query Q="..."` | 一步问数（Planner→Compiler→Guard→Doris 真连库）：`--domain retail` 切零售、`--format json` 机读、未知指标默认澄清、`--role branch_manager --role-ctx branch=BR_A1` 注入行级策略；退出码 0/1/2/3 分流。**依赖装好后也可直接 `atlas query "..."`：控制台入口基于 `__file__` 定位资源，可在任意目录执行，无需在仓库根** |
| `make e2e` | Data Agent 端到端验收门禁：5 场景 + handoff（Day 48） |
| `make eval` | 跑评测集，产出 report JSON |
| `make retrieve` | 指标检索评测（BM25；`ENGINE=milvus` 走 Milvus 稀疏向量；`ENGINE=fuse` 走 RRF 双路融合；`ENGINE=rerank` 走元数据 Rerank） |
| `make extract-meta` | SQL/DDL 注释 → 语义对象候选（Day 26，产出 `eval/reports/metadata-extract-<sha>.json`） |
| `make train` | 用确认后的失败样本训练 SQL LoRA |
| `make report` | 生成 EVAL_REPORT.md |
| `make rls-verify` | 行级权限回归验证（gold-146 问句 → 角色 JWT → Guard 谓词下推 → Doris 实测差异；需 `.env` 的 ATLAS_JWT_SECRET） |
| `make rbac-verify` | Polaris 层对象级 RBAC 回归验证（`make rbac-verify-ensure` 幂等建 principal/roles/grants；需 `.env` 的 POLARIS_RBAC_*） |
| `make metrics-verify` | 新发布指标编译 + Guard + Doris 实测验证（Day 27，产出 `eval/reports/metrics-verify-<sha>.json`） |
| `make test` | 全量单元 + 契约测试 |
| `make backup` | 控制库备份（SQLite backup API，产物落 `backups/`） |
| `make restore` | 从备份恢复（`RESTORE_MANIFEST=<path>`） |
| `make diagnostics` | 调用诊断端点（`ATLAS_TOKEN=<jwt>`） |
| `make acceptance` | T13 首次接入验收（记录动作与起止时间，输出报告） |
| `make serve` | 启动 HTTP API（uvicorn 127.0.0.1:8000，单进程，见 §9.1） |
| `make token` | 签发本地测试 JWT（默认 ROLE=hq_admin；如 `ROLE=branch_manager CONTEXT='{"branch": "east"}'`） |
| `make api-verify` | HTTP API 真链验收（A1-A9：全链 EX / 认证 / 三角色差异 / 会话冲突 422 / 跨域身份拒绝 / 治理面 8 集合一轮全绿（A8）/ `/plan/execute` 真链（A9），产出 `eval/reports/api-acceptance-<sha>.json`） |

### 9.1 对外 HTTP API（契约 v2：`/api/v1` 前缀 + 治理面）

Atlas 的服务面（ADR-0012；URL 契约 v2 = ADR-0022，落地 [serving/api.py](serving/api.py)
与 [serving/governance.py](serving/governance.py)）：同一确定性链路（Planner → Compiler
→ Guard → Doris 只读执行）的 HTTP 出口。**硬切**：业务与治理全部路径在 `/api/v1`
前缀下（仅 `/health` 根路径保留为探针契约、双挂同 body），旧路径一律 404、无兼容期。
治理面端点**只读 Git 文件与评测产物，不触发 DB 与 Agent 构造**——快照缺失时业务面
如实 503、治理面仍 200（面板恰在故障排查时最该可用）。engine=stub 确定性默认，
LLM 引擎服务化属 Phase 2。

| 端点 | 前缀 | 认证 | 限流桶 | 请求 | 响应 |
|---|---|---|---|---|---|
| `GET /health` | 根 + `/api/v1`（双挂同 body） | 公开 | — | — | **8 键**：`{status, head_sha, snapshot_sha, snapshot_source, snapshot_bound_to_head, snapshot_created_at, snapshot_tables, boot_id}`（存活 + **本轮实际绑定** + 进程身份）；无快照可绑时 `status=degraded`、绑定各键为 `null`，仍返回 200（探针语义）。键集权威定义见 ADR-0019 决策 ⑥ + ADR-0020 决策 ⑦，降级与正常两条路径同键集 |
| `POST /plan` | `/api/v1` | Bearer | 业务 | `{question, model?}`（≤500 字符） | `{kind: "plan", plan}` 或 `{kind: "clarify", clarification}`（歧义 200，CLI exit 1 语义的 HTTP 化） |
| `POST /compile` | `/api/v1` | Bearer | 业务 | Plan JSON（`metric/dimensions/time/filters/order_by/limit`，含 `model?`） | `{sql}`（Doris 只读方言）；结构非法/编译失败 422 |
| `POST /ask` | `/api/v1` | Bearer | 业务 | `{question, session_id?, model?}` | TurnResult 全集（kind ∈ answer/clarify/blocked/error；rows 的 Decimal→str 保精度、datetime→ISO8601）+ `snapshot_sha` / `snapshot_bound_to_head` 两键回显本轮绑定；快照绑定失败 503（消息带出原始原因） |
| `POST /plan/execute` | `/api/v1` | Bearer | 业务 | Plan JSON（`/compile` 请求体同构）+ `session_id?` + `question?`（审计与归因展示用，缺省取 Plan 规范化文本，不参与解析） | 与 `/ask` 同构（kind ∈ answer/blocked/error——不经 Planner 故永无 clarify；**非法 Plan → 200 + `kind="error"`**，不是 422 不是 500，统一响应形态）。`session_id` 缺省 = 一次性 thread、不产生会话态；给定则与 `/ask` 同一会话空间（身份指纹 422 约束同），且本轮 Plan 成为该会话下轮残句追问的补全基线（ADR-0022 代价 ⑥） |
| `POST /analyze` | `/api/v1` | Bearer | 业务 | `{question, session_id?, model?}`（AskBody 与 `/ask` 同构，身份只来自 Bearer claims，无分析意图时按 ADR-0026 决策③回落普通 ask） | 与 `/ask` 同形字段全集外加顶层 `analysis` 键（ADR-0026 决策⑥）：**17 键投影**（schema_version / intent / status / metric / dimension / baseline / current / filters / snapshot_sha / semantic_sha256 / recipe_version / totals / items / steps / reason_code / text / elapsed_ms），键恒在、非分析轮（无意图回落或 clarify）整体为 `null`。`analysis.status` ∈ ok/unavailable/blocked/error（全终态；clarify 轮整体为 null、不投影 status），机器可读原因在 `reason_code`；`analysis.steps[]` 按固定角色序（baseline_total → current_total → current_by_dimension → baseline_by_dimension）携带 role/kind/sql——成功步另有 columns/rows/latency_ms，失败步 sql/columns/rows 置空只留 role/kind/latency_ms/reason_code（被拒 SQL 不出网）；totals（baseline/current/delta）与 items 数值一律 **Decimal 字符串**或 null，综合不可用时 totals=null、items=[]。父轮不冒充单 SQL 结果（sql/explanation 为 null、rows 空、row_count=0）。**分析为固定四步模板编译，不经 LLM 生成**；422 会话身份冲突与 `/ask` 同规则，快照绑定失败 503 同源 |
| `POST /analyze/stream` | `/api/v1` | Bearer | 业务 | `{question, session_id?, model?}`（`AskBody` 与 `/analyze` **逐字同构**，身份只来自 Bearer claims） | **SSE `text/event-stream`**（④a，ADR-0028 决策④·执行模型 A compute-then-stream）：复用 `/analyze` 同一 `agent.analyze()` 执行路径（SQL 全过 Guard、锁在返回时释放），再把**已算好的分步产物**按 AG-UI **事件词表回放**（借鉴词表、**非 AG-UI 协议兼容声明**，N2）。事件序列：`RUN_STARTED → 每角色 STEP_STARTED/TOOL_CALL_RESULT/STEP_FINISHED → STATE_SNAPSHOT → RUN_FINISHED`；终态 `STATE_SNAPSHOT` 直发与 `/analyze` 同一份完整 TurnPayload（单一事实源，流式终态逐字一致）。被拒/失败步 → 该步 `STEP_STARTED` 后直接 `RUN_ERROR`（被拒 SQL 不出网、无 `TOOL_CALL_RESULT`，N3）。无分析意图 → 仅 `RUN_STARTED/RUN_FINISHED`（前端回落 request/response 取完整 TurnPayload）。422 会话身份冲突、503 快照绑定失败与 `/analyze` 同源 |
| `GET /auth/login` | `/api/v1` | 公开 | — | — | 302 到 IdP 授权端点（PKCE + state + nonce；discovery/issuer 不符不重定向）；OIDC 未配置（演示模式）/配置不全/控制授予非法 → 503 配置阻塞，**不伪装登录成功** |
| `GET /auth/callback` | `/api/v1` | 公开（一次性 state） | — | `code` + `state` | 严格验证（一次性 state + 固定 RS256 验签 + issuer/audience/exp）后 302 回 `/` 并下发 `HttpOnly; Secure; SameSite=Lax` 会话 Cookie（IdP token 不出服务端内存，D02 ③）；缺参/state 失效 422，token 交换或验签失败 401 |
| `POST /auth/logout` | `/api/v1` | Cookie 会话 | — | （`X-Atlas-CSRF` 头 + 同源 Origin） | 204：撤销会话并清 Cookie；无会话/双凭据 401，Origin 或 CSRF 不符 403 |
| `GET /auth/session` | `/api/v1` | Cookie 会话 | — | — | `{subject, issuer, role, capabilities, scopes, csrf_token, expires_at}`（**不返回 IdP token**）；未登录 401 |
| `POST /runs` | `/api/v1` | Bearer | — | `{deployment_id, mode, client_request_id, question?/plan?, session_id?, capture?}`（mode=ask/analyze/execute_plan；字段组合严格校验、未知字段拒绝） | 202 收据 `{run_id, status, release_id}`；幂等（同键同内容返回原 run、同键不同内容 409）；错误码：401 `invalid_identity`、403 `forbidden`、404 `not_found`（部署）、409 `session_context_expired` / `idempotency_conflict`、422 `invalid_request` / `mode_not_supported`（analyze 未接通）/ `capture_invalid`、503 `audit_unavailable`（强制审计失败不降级放行） |
| `GET /runs/{run_id}` | `/api/v1` | Bearer | — | — | **固定 10 键** RunView（`run_id/session_id/release_id/status/result/result_availability/data_identity/replay_of/last_seq/trace_summary`）；`result_availability` 五态 pending/available/not_retained/expired/restricted；不可见与不存在一律 404（不泄露存在性） |
| `GET /runs` | `/api/v1` | Bearer | — | `?limit?&cursor?&scope?&status?&since?&until?`（默认 50 / 最多 100，非负游标；参数非法 422） | `{items, next_cursor}`——行 = RunSummary **固定 12 键**（`run_id/session_id/deployment_id/scope/mode/status/result_availability/result_kind/replay_of/last_seq/created_at/updated_at`）仅摘要、不含正文/问句/结果（D13）；可见性同 `GET /runs/{run_id}` |
| `GET /sessions` | `/api/v1` | Bearer | — | `?limit?&cursor?&scope?` | `{items, next_cursor}`——本人会话目录（SessionSummary 固定 6 键：`session_id/deployment_id/scope/run_count/last_run_at/last_status`）；**控制库运行事实聚合，非 checkpoint dump** |
| `GET /sessions/{session_id}` | `/api/v1` | Bearer | — | `?limit?&cursor?&deployment_id?` | `{items, next_cursor}`——会话回合（= 控制库运行行，新→旧；不读 checkpoint 正文）；未知与不可见统一 404 |
| `GET /runs/{run_id}/events` | `/api/v1` | Bearer | — | `after_seq?` 或 `Last-Event-ID` 头（前者优先；非负整数，非法 422） | **SSE `text/event-stream`**：`id: <seq>` / `event: <event_type>` / `data: <RunEventRecord JSON>` 三行帧（schema_version=1）；按 seq 回放**持久事实**并支持断线续读（按 seq 去重），无法补齐（游标越界/事件缺口）410；建立连接即授权（401/403/404/410 同步返回、不进流）；心跳 `: ping` 注释帧；终态发完即止；**订阅只读、不重跑运行**（不触碰执行器） |
| `GET /runs/{run_id}/artifacts/{artifact_id}` | `/api/v1` | Bearer | — | — | 捕获正文钻取：对象 ACL 先判（他人/未知 404、摘要 403），**只回 capture 授权字段**；保留期清理后 410 `artifact_expired`（保墓碑元数据、不返回正文） |
| `POST /feedback` | `/api/v1` | Bearer | — | `{run_id, verdict(up/down/corrected), comment?, correction?}` | 201 FeedbackRecord **恒 9 键**（固定 `pending_review` / `training_eligible=false`）；能力先判 403、他人或未知 run 404 |
| `GET /feedback` | `/api/v1` | Bearer | — | — | `{"items":[...]}`——仅本人的反馈（新→旧，未分页）；无能力 403；跨用户审核队列属 T12 |
| `POST /manage/sources` | `/api/v1` | Bearer | — | `{source_id, revision, connector_kind, secret_ref, allowed_catalogs, allowed_tables, timezone, tls_policy, query_budget}`（追加式修订；secret_ref 只收 `env:<NAME>`；connector_kind 限 doris；未知字段拒绝） | 201 SourceRevisionView **固定 13 键**（`source_id/version/revision/connector_kind/secret_ref/allowed_catalogs/allowed_tables/timezone/tls_policy/query_budget/created_by/created_at/last_probe`）；同 source 新修订**版本递增、旧行不可变**；明文凭据/DSN 与表白名单越界 422；无 source.manage 能力 403 |
| `GET /manage/sources` | `/api/v1` | Bearer | — | — | `{"items":[...]}`——源目录（每源**最新修订**，固定 13 键；`last_probe` 为探测证据摘要 `{probe_id, status, blocked_reason, observed_at}` 或 null）；不回显任何凭据或连接串（D03） |
| `POST /manage/sources/{source_id}/probes` | `/api/v1` | Bearer | — | **无请求体**（带体 422——不提供任意「测试 SQL」通道） | 200 ProbeResult **固定 10 键**（`probe_id/source_id/version/status/blocked_reason/observed_at/engine_version/schema_digest/capabilities/reproducible`）；`status ∈ ok/blocked`，`blocked_reason` ∈ 9 类（credential_missing / target_forbidden / target_not_allowlisted / tls_error / table_out_of_whitelist / read_only_unconfirmed / metadata_missing / credential_rejected / connect_failed）；`capabilities` 6 键、`reproducible` 恒 false；未知源 404、无能力 403 |
| `POST /manage/deployments` | `/api/v1` | Bearer | — | `{deployment_id, scope, source_id}`（**不得夹带 active_release_id**——未知字段 422） | 201 DeploymentView **固定 8 键**（`deployment_id/scope/source_id/active_release_id/revision/created_by/created_at/updated_at`）；**创建即 draft（`active_release_id=null`）**；重复 id 409、未知源 404、作用域未授权 403（fail-closed） |
| `GET /manage/deployments` | `/api/v1` | Bearer | — | — | `{"items":[...]}`——部署目录（按已授权领域**服务端裁剪**；发布动作能力亦可读，但不获得管理写入） |
| `GET /manage/deployments/{deployment_id}` | `/api/v1` | Bearer | — | — | 部署详情（发布 CAS 的前置读取，不改写指针）；未授权领域 403、未知 404 |
| `POST /manage/drafts` | `/api/v1` | Bearer | — | `{kind, scope, content}`（content 键必须恰为 `target`/`document`；target 命中制品白名单与 kind 前缀；`base_git_sha` 由服务端取本地 HEAD） | 201 草稿行**固定 11 键**（`draft_id/kind/owner/scope/base_git_sha/revision/status/content/content_digest/created_at/updated_at`）；**草稿非权威：创建不改变发布指针**（激活是 T08b 的显式 CAS 发布）；无 draft.edit 403、作用域未授权 403、形状/路径非法 422 |
| `GET /manage/drafts` | `/api/v1` | Bearer | — | — | `{"items":[...]}`——草稿目录（按已授权领域**服务端裁剪**；draft.edit/review/export 可读，不因人裁剪——审核需跨人可见同域草稿） |
| `GET/PUT /manage/drafts/{draft_id}` | `/api/v1` | Bearer | — | PUT：`{content}` + `If-Match: "<revision>"` 头（缺头/形态非法 422，盲写不允许；**方法形态为 D13 目标表的 PUT**，PATCH 不注册） | 详情/编辑（对象 ACL：编辑仅限本人草稿，他人 403）；修订 CAS 冲突 409、未知 404、他域 403；编辑改内容即撤销放行状态（旧审核失效，store 原语） |
| `POST /manage/drafts/{draft_id}/validations` | `/api/v1` | Bearer | — | **无请求体**（校验对象就是草稿内容本身，不接受调用方指定规则集） | 201 校验证据行**固定 8 键**（`validation_id/draft_id/revision/content_digest/status/findings/actor/created_at`）；`status ∈ passed/failed`，`findings` 元素 `{code, message}`（code ∈ structure/governance/policy = 三套确定性校验器归因）；**passed 且仍在 draft 时同事务推进 validated**（重复校验只追加证据、不漂移状态）；非 semantic kind 422、未知 404、无 draft.validate 403 |
| `POST /manage/drafts/{draft_id}/reviews` | `/api/v1` | Bearer | — | `{decision: approved|rejected, comment?}`（decision 非法/缺字段 422） | 201 审核证据行**固定 8 键**（`review_id/draft_id/revision/content_digest/decision/comment/actor/created_at`）；**只认 validated**（未校验或编辑后失效 409）；approved 推进 reviewed、rejected 只落证据（同修订可重审）；无 draft.review 403 |
| `GET /manage/drafts/{draft_id}/patch` | `/api/v1` | Bearer | — | — | 导出视图**固定 7 键**（`draft_id/revision/content_digest/base_git_sha/target/patch/impact`）；`patch` 为最小统一 diff（base = `base_git_sha` 对象库文本，不读脏工作树；未编辑即空串、新文件走 `/dev/null` 形态）；`impact` 6 键（指标 `name` / 维度 `dataset.field` 的增删改）；**纯只读**，无 draft.export 403、未知 404 |
| `POST /manage/releases/imports` | `/api/v1` | Bearer | — | `{draft_id, source_git_sha, source_id}`（`source_git_sha` 须完整 40 hex commit 且对象库可达；**不接受工作树/索引/引用**） | 201 导入视图**固定 7 键**（`release_id/content_digest/draft_id/draft_revision/status/target/created_at`）；门禁链：显式 commit 白名单收集（符号链接/子模块/超大文件 422）→ 内容 ≠ 已审核草稿当前内容 409 → 制品整体确定性门禁（结构/治理/策略/值域/编译）422 → 登记**不激活**、草稿推进 `release_ready`；未知草稿/源 404、无 release.import 403、重复登记 409 |
| `POST /manage/deployments/{deployment_id}/releases` | `/api/v1` | Bearer | — | `{release_id, expected_active_release_id}`（**CAS**：必须等于当前指针，首发为 null；不匹配 409） | 200 激活视图**固定 6 键**（`deployment_id/action/previous_release_id/active_release_id/revision/updated_at`）；`action=publish`；域/源不匹配 409、无当前只读探测证据 409 `evidence_required`；**旧运行仍引用旧制品**（运行固定 Manifest，切换不影响已运行实例） |
| `POST /manage/deployments/{deployment_id}/rollbacks` | `/api/v1` | Bearer | — | 同发布（`{release_id, expected_active_release_id}` + CAS） | 200 激活视图（`action=rollback`）；**回退只改指针**，能力/域/证据门禁与发布同等；404/409 语义同发布 |
| `GET /manage/releases` | `/api/v1` | Bearer | — | — | `{"items":[...]}`——发布登记目录（ReleaseRecord **固定 8 键**：`release_id/content_digest/scope/source_id/source_revision/manifest/created_by/created_at`；按已授权领域**服务端裁剪**，发布/导入/部署管理能力可读） |
| `GET /manage/releases/{release_id}` | `/api/v1` | Bearer | — | — | 发布详情（同一 8 键；`manifest` 为 13 键版本合同：制品摘要 + 源码/运行时身份 + 规则/索引/提示词摘要 + 评测证据回流槽位，**无模型正文与源凭据**）；未授权领域 403、未知 404 |
| `GET /governance/models` | `/api/v1` | Bearer | 治理 | — | `{kind: "governance.models", count, sources, items}`——语义模型清单（权威 YAML 路径 + 版本） |
| `GET /governance/metrics` | `/api/v1` | Bearer | 治理 | `?model?`（finance 缺省） | `{kind: "governance.metrics", …}`——指标清单（含治理扩展与 FIBO 对齐） |
| `GET /governance/dimensions` | `/api/v1` | Bearer | 治理 | `?model?`（finance 缺省） | `{kind: "governance.dimensions", …}`——维度清单（物理列 + 值域注册状态） |
| `GET /governance/synonyms` | `/api/v1` | Bearer | 治理 | `?locale?`（zh_cn 缺省） | `{kind: "governance.synonyms", …}`——locale 词典（含空占位标志与权威源说明） |
| `GET /governance/values` | `/api/v1` | Bearer | 治理 | — | `{kind: "governance.values", …}`——值域注册表清单（含 skipped 与 skip_reason）；钻取 `GET /governance/values/{item}` 返回完整 values + 别名 |
| `GET /governance/policies` | `/api/v1` | Bearer | 治理 | — | `{kind: "governance.policies", …}`——行级策略与角色目录（condition 模板原文） |
| `GET /governance/reports` | `/api/v1` | Bearer | 治理 | — | `{kind: "governance.reports", …}`——评测报告索引（主报告结构化，非主报告模式标签）；钻取 `GET /governance/reports/{name}`（主报告结构化 / 非主报告 raw 降级） |
| `GET /governance/snapshots` | `/api/v1` | Bearer | 治理 | — | `{kind: "governance.snapshots", …}`——数据快照清单（created_at 降序 + 最新标志） |

> 上表 45 行的合并口径：`/health` 双挂 2 条并 1 行、`values` 与 `reports` 的集合+钻取各并 1 行、`runs` / `feedback` / `sources` / `deployments` / `drafts` 的 POST/GET 两行各同属一条路径——展开后即 openapi 的
> **43 条路径**（0026 起新增 `POST /api/v1/analyze`、④a 新增
> `POST /api/v1/analyze/stream`、ADR-0031 D13 新增 `auth` 4 条与运行面 3 条
> `/api/v1/runs`、`/api/v1/runs/{run_id}`、`/api/v1/feedback`，T06 新增运行面 4 条
> `/api/v1/runs/{run_id}/events`、`/api/v1/runs/{run_id}/artifacts/{artifact_id}`、
> `/api/v1/sessions`、`/api/v1/sessions/{session_id}`，T07 新增管理面 4 条
> `/api/v1/manage/sources`、`/api/v1/manage/sources/{source_id}/probes`、
> `/api/v1/manage/deployments`、`/api/v1/manage/deployments/{deployment_id}`，T08a 新增草稿面 2 条
> `/api/v1/manage/drafts`、`/api/v1/manage/drafts/{draft_id}`，T08a-s2 新增草稿面校验/审核/导出 3 条
> `/api/v1/manage/drafts/{draft_id}/validations`、`/api/v1/manage/drafts/{draft_id}/reviews`、
> `/api/v1/manage/drafts/{draft_id}/patch`，T08b 新增发布面 5 条
> `/api/v1/manage/releases/imports`、`/api/v1/manage/deployments/{deployment_id}/releases`、
> `/api/v1/manage/deployments/{deployment_id}/rollbacks`、`/api/v1/manage/releases`、
> `/api/v1/manage/releases/{release_id}`），由
> `tests/test_api_contract_v2.py` 的 `EXPECTED_PATHS`（43 条字面量）逐条锁定。
> 治理集合信封统一为 `{kind, count, sources, items}`（count 条目数、sources 为
> Git 文件路径），全部 **GET 只读**、不触发 DB 与 Agent 构造。
>
> 认证面（ADR-0031 D02/D13，2026-09-22 T03 落地）：错误体统一
> `{"error":{code,message,request_id}}`（不含底层异常）；配置阻塞三态——全空
> （演示模式）503 `oidc_not_configured`、部分配置 503 `oidc_config_incomplete`
> （message 列缺失变量名）、授予非法 503 `control_grants_invalid`。前端顶栏
> 探测 `/auth/session` 三分法：演示模式 → 角色切换器；私有模式未登录 → 登录
> 入口；已登录 → 身份 + 退出（**开发角色选择不出现在私有模式**）。业务/治理面
> 仍走 Bearer，与 Cookie 会话是两条不混用的认证链；**真 IdP 回调验收 BLOCKED
> （需部署者环境），不能声称私有登录已在真实 IdP 上可用**。

**快照绑定的可见性**（ADR-0019 决策 ①/⑥）：运行时按「`ATLAS_SNAPSHOT_SHA` → HEAD
→ 最新已锁」三级解析选快照，因此**可以合法地绑在与代码 HEAD 不同的快照上**——此时
`snapshot_bound_to_head=false` 且 `snapshot_source` 为 `env` 或 `latest`，`/health`
（双挂）、`/api/v1/ask`、`atlas ask` 三处都回显该事实（CLI 走 stderr，不污染可机读
的 stdout）。
这类回合得到的数值**不能**与该 sha 的评测数字并列陈述，也不能写进以代码 HEAD 命名的
报告（口径不同；AGENTS.md N1/N6 同源，约束见 ADR-0019 代价 ③）。评测面
（`make eval`）不受影响：它仍然只认 HEAD 的锁定快照并复核数据指纹。

多模型路由（P7）：`model` 字段选语义模型域（`finance` 缺省——向后兼容，旧请求体
零变化 / `retail`），未知值 422；会话键（session_id）按模型隔离，跨模型不续接
（finance/retail 各一 Agent 单例，uvicorn 仍须 workers=1，见 KL #28）。

认证：JWT（HS256）复用 `serving/auth.py`，密钥走 env `ATLAS_JWT_SECRET`（N9，
无默认值）。本地签发测试 token：

```bash
make token                          # ROLE=hq_admin
curl -H "Authorization: Bearer $(make token)" \
  http://127.0.0.1:8000/api/v1/plan -d '{"question":"2013 年第二季度总交易额是多少？"}'

# 多步分析（ADR-0026）：绝对双期间 + 明确维度
curl -H "Authorization: Bearer $(make token)" \
  http://127.0.0.1:8000/api/v1/analyze \
  -d '{"question":"分析 2013Q4 相对 2013Q3 的佣金收入按分支的变化贡献"}'
```

> 本地 `make serve` 监听 127.0.0.1:8000；容器部署宿主端口映射为 **8010**
> （本机 8000 被其他服务占用，见 compose 注释）——容器 curl 请用
> `http://127.0.0.1:8010/`。

身份 → 行级策略（2026-09-05 服务面硬化批次，ADR-0011 落地注记在档）：
`/api/v1/ask` 与 `/api/v1/plan/execute` 把已认证 claims 下推为行级身份
（`agent.ask(identity=claims)` → Guard Policy 注入；`/api/v1/plan`
`/api/v1/compile` 无执行面不注入）。可见信号与语义：

- `explanation.policy_effect` = 「行级策略已生效（角色 X，策略 Y）」——只给
  角色与策略名，**不给条件值**（0011 不外泄细节，与 blocked 不回流 SQL 同精神）；
- 会话 × 身份：session_id 绑定首个请求的身份指纹（全 claims）；同一会话换
  身份 → **422「会话身份冲突，请换新 session_id」**（换身份必须换会话）；
- 限流：per-token **两桶互不挤占**（ADR-0022 决策 ⑥）——业务桶（`/api/v1` 下
  plan / compile / ask / plan/execute / analyze 同桶计数）与治理桶（`/api/v1/governance/*`）
  各自独立计数；超限 → **429 + Retry-After 头**（下一窗口起点秒数，detail 标明
  「业务面/治理面」）；/health 公开、401 路径不计。

| env | 缺省 | 说明 |
|---|---|---|
| `ATLAS_JWT_SECRET` | 无（N9） | JWT 签发/校验密钥（本地 `make token`） |
| `ATLAS_AUDIT_DISABLED` | 空 = 开 | `1` 关闭业务审计 JSONL（`serving/audit/audit.jsonl`，gitignore；每业务/治理请求一行，含 429/422 拒绝，不含 SQL——SQL 由 OTel span 承担；治理读 kind=`governance_read` 与两桶 429 共用此开关，不新增第二开关） |
| `ATLAS_RATE_LIMIT_MAX` | `60` | 业务桶 per-token 每分钟上限（**配置占位非实测阈值**——真实容量边界需压测）；`0` = 关 |
| `ATLAS_RATE_LIMIT_WINDOW_SECONDS` | `60` | 业务桶限流窗口秒数；`0` = 关 |
| `ATLAS_GOVERNANCE_RATE_LIMIT_MAX` | `240` | 治理桶 per-token 每分钟上限（**配置占位非实测阈值**；240 的依据是可算而非可测：治理页挂载 8 请求 + 平均 2 次钻取 ≈ 10 请求/次导航 → ≈24 次导航/分钟）；`0` = 关 |
| `ATLAS_GOVERNANCE_RATE_LIMIT_WINDOW_SECONDS` | `60` | 治理桶限流窗口秒数；`0` = 关 |

容器化部署（单机，依赖 Doris 已在 compose 内）：

```bash
docker compose up -d --build atlas-api   # 8010:8000；镜像无 .git，快照身份由
                                         # build arg GIT_SHA 注入——**无默认值**
                                         # （ADR-0019 决策 ④）：`make up` 从 HEAD
                                         # 求值并 export；直接敲 compose 命令时
                                         # 需 --build-arg GIT_SHA=$(git rev-parse --short HEAD)，
                                         # 否则构建守卫响亮失败（不静默错绑）
curl http://127.0.0.1:8010/health        # 根路径为探针契约（compose healthcheck
                                         # 依赖，ADR-0018 决策 ⑤ 落地注记）；
                                         # /api/v1/health 双挂点同 body
```

真链验收与报告：`make api-verify`（全 HTTP 栈 + 真 Doris + 锁定快照）：
A1 问→编→问 EX 与 gold 锚点一致 / A2 歧义反问 / A3 认证拦截 / A4 存活（根与
`/api/v1` 双挂点同 body）/ A5 三角色行级差异（gold-146 × hq_admin vs
branch_manager，策略名可见且条件值不外泄）/ A6 会话身份冲突 422 / A7 零售双档
（category_analyst 品类受限 + hq_admin 策略名按域报 rp_dept_visible）+ 跨域身份
拒绝（region_manager × finance → error，域不匹配，Guard 之前即拒——ADR-0021
判据 11/12）/ A8 治理面一轮挂载（8 集合全 200 且不消耗业务桶）/ A9
`/plan/execute` 真链（`/plan` 产物原样回填执行，EX 与 gold 锚点一致）。
最新报告 `eval/reports/api-acceptance-95cba68.json`（A1-A9 共 11 场景全绿，
snapshot_sha=b933e20）。剩余边界如实（契约 v2 批次后更新，ADR-0022）：会话/轮数/
身份指纹已入 SQLite checkpoint（设 ATLAS_CHECKPOINT_DB 时跨重启续接）；限流两桶、
审计写与 SQLite 单写者仍为进程内（uvicorn 必须 workers=1，理由见 KL #28 ②）、
审计本地 JSONL 非防篡改、身份为本地签发 HS256（无 IdP）、Doris per-user identity
透传属 0011 决策 4 独立项——见 KL #28 ③。

---

## 10. 已知限制（Known Limitations）

> 这一节是**诚实性的核心**，禁止删除或美化。

1. **数据规模有限**：TPC-DI 为基准默认规模（实测数据段 2012-07-07~2017-07-07，294 万行），与真实金融机构 PB 级、上千张表的复杂度不可比
2. **Schema Linking 未大规模验证**：当前仅在语义层注册域上验证——8 张注册 dataset，
   其中 gold 判别域 6 表（5 dim + fact_trades，全连通无区分度，见 `schema-link-bm25-7d48dcb.json`
   note 口径）；1000 表场景属于**待验证假设**
3. **权限模型简化**：行级策略为自研简化实现，未经过真实 IAM/审计/合规检验
4. **并发与容灾未验证**：MVP 为单机部署，无高可用、无限流压测
5. **数字纪律与回填状态（历史注记）**：早期文档的「待填写」占位已随评测闭环全部回填
   为实测值（来源见 3.3 勾选行与 EVAL_REPORT.md 各报告）；新增任何数字仍必须来自脚本
   产物，不得预估（AGENTS.md N1）
6. **Apache Ossie 0.2.0.dev0 是 DRAFT**：schema 可能变化；且 Ossie 是孵化器项目，
   存在无法毕业的可能（迁移路径见 ADR-0002）
7. **Ossie 不含治理字段**：owner / lineage / freshness 由 Atlas 扩展补齐，
   该扩展是本项目自研，未经过行业标准检验
8. **Polaris 与 Doris 为新增组件**：学习成本与运维复杂度高于原方案；
   若单机资源不足，按 ADR-0004 降级
9. **Apache Calcite 未引入**：MVP 用 sqlglot，无 CBO 与语义校验（取舍见 ADR-0005）
10. **图表与归因能力为最小实现**：仅做确定性渲染，无自动洞察
11. **Planner 为确定性规则版**：维度解析要求显式分组结构词（“按X统计/分组”）
    且仅匹配 dim_* 维度表字段；相对时间为**设计性不支持**（返回澄清，理由见
    ADR-0014 ③——固定快照评测下相对时间必然漂移，非待实现项）；filter 支持
    维度等值/排除与度量阈值（HAVING），不支持形态见第 29 条（详见
    agent/planner.py 已知边界）
12. **检索语料与查询同源、MVP 向量为词法级**：Recall 评测的问句措辞来自语义层
    同义词（同源口径验证，非跨领域泛化数字）；Milvus 向量为确定性词法稀疏向量
    （tf-IP，无 idf），不编码语义相似（“佣金”与“手续费”不同 token），嵌入与
    rerank 待 Schema Linking 阶段评估（见 retrieval/bm25.py、milvus_client.py）
13. **图约束比 Compiler 保守**：SemanticGraph 禁止事实表间桥接（dim→fact 回跳）的
    跨实体召回，Compiler 目前技术上能编译这类多跳 SQL（缺维度域检查）；检索层先剔除，
    双方口径差异属已知边界（见 retrieval/graph_store.py）
14. **Guard 跨表谓词已支持 join 注入，剩余边界为无路径拒绝**：策略引用表不在
    查询中时，沿语义模型 relationships join 图补 LEFT JOIN（与编译器同形态：
    catalog.db.table AS base 名 + 关系列 EQ，Phase 2 落地）；**无合法 join 路径
    或未提供模型仍拒绝**（安全底线不放开），注入后二次只读校验含补表后的表白
    名单复核；已知边界：视图展开递归校验仍待实现（见 agent/security/sql_guard.py
    模块 docstring）；**成本估算已落地**为基于扫描表数 / 表行数统计的启发式代理
    （非压测标定，阈值 1.0 为保守护栏，部署按实际表数调参），**时间范围防御已落地**
    （查询 join 语义模型声明的时间维表且无时间谓词时，强制补 `time_col >= 当前 - N
    天` 下界——纵深防御填补原 apply_time_range 空操作）
15. **行级权限实现的两个诚实边界**：① Polaris 层为对象级（表/命名空间粒度）
    授权，无行级能力（实测 polaris-rbac-7d48dcb.json 为对象级；无行级为官方
    文档核对结论，见 ADR-0014 ⑤）——行级过滤维持 Guard SQL 谓词层为架构终局，
    Polaris 下推为不适用项；② 地理/品类角色已跨域实测（2026-09-04 收窄）：
    TPC-DI 无地理/品类维度（实测 dim_broker.Branch 为随机变造串）是金融域
    数据事实；规划原文「华东区 / 某品类」零售角色随 TPC-DS SF0.1 数据落地已实测
    ——rp_dept_visible 未落地逻辑列 region/product_category 对齐物理
    dim_store.s_state / dim_item.i_category（row_policy.yml），rls-verify 双域差异报告
    `eval/reports/rls-verify-e0f2d29.json`（finance 差异集 4：ADR-0021 起 broker 档
    兑现（brokerid=5460 实测值谓词，3 行 < hq_admin 5 行）；retail 差异集 2：
    region_manager（州=TN）与 hq_admin 结果一致系 SF0.1 单州数据事实，如实报告，
    差异由 category_analyst 品类受限承担）；带身份 HTTP 化实测见 api-verify
    A5-A7（`eval/reports/api-acceptance-95cba68.json`：三角色差异 / 会话身份冲突
    422 / 零售双档 + 跨域身份拒绝）与 demo 集成测试
    （tests/test_demo_e2e.py RLS 2 例——库级 resolve_policy → Guard 注入，
    README 快速开始库级载体；HTTP 身份链路由 api-verify A5-A7 承担）
16. **Day 27 派生指标数值背书已闭环（2026-09-04）**：5 个派生指标 gold_test_cases 与
    expected_value_snapshot_sha 已回填（gold-156~162、快照 30b8344，Plan Acc 57/57、
    EX 57/57 全绿，报告 `eval/reports/30b8344.json`）；评测先行暴露的 Planner 同义词
    子串歧义缺陷（派生词 ⊃ 基础词系统性误报）已按最长命中消解修复（契约 +9）；
    governance 校验第 7 条（check_snapshot_anchor）防回填值指向未锁快照——metrics-verify
    的「可编译可执行」已升级为样本级口径锚定，发布定义 = 数值背书
17. **户均现金余额为负是数据特性，非口径错误（gold-162 已锚定）**：average_cash_balance
    实测 -32465124.70（户均），核查 atlas.dwd.fact_cash_balances：219214 行中 132758 行
    （60.6%）Cash<0，行区间 -12477107.22 ~ +9250918.74、无低于 -1 亿的值——负值源于
    TPC-DI 变造数据与该子集抽取口径；指标按 YAML 表达式计算正确，负值已随 gold-162
    钉为发布口径下的正确结果（快照 30b8344）；该数值不具「户均余额」业务代表性——
    若未来调整口径（如剔除透支账户）需新 ADR 并重锚定全部受影响样本
18. **检索单路词法引擎在语料扩展后退化**（指标文档 15→20，Day 27 发布所致）：BM25
    Recall@1 44/44→41/44、Milvus 42/44→35/44、fuse 44/44→40/44（三路 @5 均保持
    44/44），rerank 主链路 44/44 无损；归因：新增「平均/比率」类指标与存量「合计」
    类指标同域词法重叠（市值合计问句被户均市值文档抢 top-1 等），元数据重排
    （同义词置信度优先）吸收了漂移。**当前状态（Day 29 已落地解决路径）**：
    schema linking 三阶段链路（图域粗筛 → 受限域打分 → 元数据重排）在 44 条 gold 上
    Recall@1 = 44/44（基线对照见 `eval/reports/schema-link-bm25-7d48dcb.json`）；
    单路引擎（无重排层）仍 41/44 属引擎设计内（Day 24 rerank 引擎本就含重排层），
    不再视为待修复回归；Milvus/fuse 单路在 20 语料上的 35/40 复测同属词法层限制
19. **LoRA 策略路径尚未实测（环境阻塞，非能力声明）；RAG+LLM 已于 2026-09-03 实测**：
    RAG+LLM（deepseek-v4-flash）注册域 44/44 Plan Acc + 4/4 歧义反问 + 44/44 EX + 0
    拒绝（报告 `rag-llm-openai-7d48dcb.json`）——与确定性链同分但代价高两个量级
    （99597 tokens / 2564ms 每批均值 vs 0 token / 178.6ms），域内无增量的实证；LoRA /
    LoRA+SC 仍 blocked = macOS arm64 无 CUDA + ml 依赖未装（Day 37-38 前置检查 exit 2
    实测，LLM 端点已就绪）。所有 LoRA 相关数字 = blocked 登记（不编数字，AGENTS.md
    N1）；解锁后 `make train`（vLLM serve + `make compare RAG_ENGINE=openai`）一键出数
20. **Data Agent MVP 多轮边界（ADR-0014 ②，如实声明）**：同一会话支持连续提问 + 每轮
    事实留痕与结果冲刷 + **指代消解 MVP**（仅“同 metric 换时间/换维度”结构补全：
    “那…呢/换成/按…呢/改为”链接词命中时复用上轮 Plan，其余自由代词“它/上轮
    那个”与指代不明 → 澄清，确定性优先不猜测）；explain 已回填 last_plan 供追问
    复用；handoff 仅在候选链检索 0 素材时触发，
    Guard 拒绝（blocked）与执行期故障（error）不转人工——安全与运维边界，人工也
    不得绕过只读红线（详见 agent/state.py、agent/graph.py docstring）
21. **图表为 spec 级确定性渲染（无像素）**：schema 必须来自已执行结果（无执行 SQL
    引用的裸数据拒绝）；多数值列只渲染第一个；折线不插值不排序；密集结果（>200
    类目）降级表格并注记；NaN/Inf/空结果拒绝（agent/tools/chart.py docstring）。
    **P3 落地追加（ADR-0025，2026-09-16；上方各句逐字不变）**：时间轴列由编译器
    声明携带（`Compiler.emitted_time_column` → `render_chart` 的 `time_columns`
    入参，前端不推断），`_TIME_COLUMN_NAMES` 仅兜底非编译路径、命中时 spec 的
    `note` 追加权威等级标注
22. **可观测栈为可选启动（配置态，Day 51-53 如实声明）**：埋点默认 no-op（未配
    `OTEL_EXPORTER_OTLP_ENDPOINT` 零 I/O）；otel-collector/prometheus 属 compose
    `obs` profile，`make up` 不启动，需 `docker compose --profile obs up -d` 点亮
    （grafana 默认起，宿主机 3001）；面板与告警阈值是**配置占位**（2026-09-03 冒烟
    7 回合不构成流量基线，阈值非实测统计边界），告警规则未经真实事件触发验证
23. **Prometheus counter 形态受进程生命周期影响**：CLI 短进程（每回合独立进程）的
    counter 每进程从 0 累计——固定 `service.instance.id` 后同序列值恒 1 不增，
    rate()/QPS 趋 0 失真（非真实流量为 0）；QPS 面板在长驻服务进程下语义才成立
    （serving 常驻模式未部署）。p95 直方图经 reset 修正不受影响（冒烟实测 242.5ms）；
    埋点数据仅代表人工问数冒烟，不构成性能声明
24. **token_cost 空序列与单位口径**：确定性链路 0 token 不产出 `gen_ai.token_cost`
    （Grafana token 面板无序列是设计事实，非链路故障）；单位 token 不换算 USD（未接
    定价表，避免伪精确）；gen_ai 属性（model/prompt_version）仅 LLM 真消耗 token 时
    写入（N2 不虚构调用）
25. **trace 粒度与评测链路埋点边界**：trace 为回合级单 span（无 LLM 调用级细分）；
    评测批处理（make eval / rag-eval）直接驱动内部函数不经 DataAgent ask 路径，
    **评测数字不出现在 Grafana**——评测口径一律以 eval/reports/*.json 为准，
    Prometheus/Grafana 只覆盖 ask 路径；会话记忆默认进程内 MemorySaver（未设
    ATLAS_CHECKPOINT_DB 时重启即失；设后随 SQLite 落盘续接，ADR-0020）
26. **dbt MetricFlow 导出为三态非无损映射**（Day 53 `make export` 实测）：20 指标中
    agg 14（单列聚合→measure）/ ratio 3（聚合后除法）可表达；**unmapped 3**
    （`SUM(a*b)` 先乘后加，如 total_trade_value）MetricFlow measure 无法表达，
    理由逐条登记在产物 header（`exports/dbt_semantic_models.yml`），不伪造等价物；
    DATABRICKS 方言列为占位，未实测不填（AGENTS.md N1）
27. **FIBO L2 映射覆盖 19/20 指标**（Day 54 审计补齐后）：8 datasets 全覆盖（含
    fact_holdings → Holding）；唯一缺口 total_trade_tax——锁定闭包（FND+FBC+BE +
    Commons 20250801）无贴切税务金额类（候选仅税务治理概念/经纪服务费，语义不贴切），
    补映射需先扩展闭包域并重跑冒烟验证，登记待办不硬补；审计方法与数字见
    `data/fibo/README.md` 覆盖审计节（2026-09-03）
28. **HTTP API 边界（ADR-0012，2026-09-03；服务面硬化批次 2026-09-05 收窄
    ③；会话持久化批次 2026-09-15 收窄 ①②；契约 v2 批次 2026-09-16 前缀化 +
    治理面 + 两桶限流）**：服务面为本地演示/集成面，非生产
    部署——①会话持久化（ADR-0020）：设 ATLAS_CHECKPOINT_DB 时会话/轮数/身份指纹
    随 SQLite checkpoint 落盘、跨重启不失；未设时仍是进程内 MemorySaver（重启即
    失）。两种形态都无横向扩展；②uvicorn 必须 workers=1，理由收窄为「限流桶 +
    审计写 + SQLite 单写者」（ADR-0020 决策 ⑧）：多 worker = 配额 ×N、审计行交错、
    SQLite 多写者；③**身份下推/限流/
    审计已落地（2026-09-05）**：/api/v1/ask 与 /api/v1/plan/execute 已验证 claims →
    行级策略随 Guard 注入
    （explanation 可见信号，条件值不外泄）、per-token 两桶限流（业务 60 / 治理 240
    独立计数，429 + Retry-After + detail 标桶名）、业务审计 JSONL（每业务/治理请求
    一行，含 429/422 拒绝，不含 SQL）——绑定 api-verify
    报告 `eval/reports/api-acceptance-95cba68.json`（A1-A9 共 11 场景）；**剩余
    边界如实**：限流为进程内固定窗口（60/240 均为配置占位非实测阈值）、
    审计本地 JSONL 非防篡改（生产需外置）、身份为本地签发 HS256 JWT（无 IdP，
    0011 决策 4「真实多租户 → gateway 认证先行」推翻条件未触发）、Doris
    per-user identity 透传属 0011 决策 4 独立项；④engine=stub 确定性默认，LLM
    引擎服务化属 Phase 2；⑤容器内 /ask 依赖构建时注入的快照身份 ATLAS_GIT_SHA
    （镜像无 .git）且对应 meta 随仓库进入镜像——带 seed 数据的环境才可答；
    ⑥/api 契约测试 96 例（tests/test_api.py 26 + tests/test_api_hardening.py
    19 + tests/test_api_contract_v2.py 25 + tests/test_identity_echo.py 26，
    fake 注入无 DB）+ 真链验收 make api-verify（A1-A9）在档
29. **filter 不支持形态 → 澄清（不猜测）**：自由双指标比较（“佣金高于成交量的
    分支”）、维度值模糊无法命中语义层同义词、HAVING 语义度量阈值但问句未解析出
    metric（无从挂载聚合比较）均返回 ClarificationRequest；时间词不并入 filter
    （时间一律走 TimeSpec，相对时间见第 11 条）——与 ADR-0014 ① 裁定一致，
    这些形态是澄清机制的评测载体而非缺陷（gold-149~155 中歧义样本即为此类）
30. **维度值域是快照态而非实时（ADR-0016，B4）**：`semantic/values/*.json` 由
    `make profile-values` 从锁定快照 SELECT DISTINCT 生成，值域与最新锁定快照绑定
    （`make lint [values]` 漂移即红）。数据重新装载后必须重跑 `make profile-values`
    同步值域，否则 lint 红。代码 HEAD 已前进时可用 `python -m data.value_profile --snapshot-sha "<已锁SHA>" --raw-dir "<TPC-DI原始目录>"`；默认仍按代码身份选快照，不自动回退。采集前后复核指纹，全部列与别名校验成功后才写入；不承诺多文件写入的文件系统事务性。同名维度字段跨数据集时，编译器按 datasets 顺序首匹配
    （实测金融 `Status` 绑到 `fact_trades` 而非 `dim_account`），该事实如实记进
    profile 的 `bound_dataset` 与 `note`，本批不改编译器。大基数列（distinct > 200）
    跳过注册，planner 对该列不做值校验——合法值与非法值都透传，静默漏匹配风险仍在
31. **黄金集计数口径已纠正；paraphrase 集无结构门槛；附过期计数全仓清单（2026-09-14 实测）**：
    `eval/gold/` 下实测 120 个 JSON = **106 条黄金样本**（`finance/` 79 + `retail/` 27）
    + **13 条 paraphrase 改写样本**（`eval/gold/paraphrase/pp-*.json`）+ `schema.json` 本身。
    本文件、`README.en.md`、`eval/gold/README.md` 此前分别写「50 例目标」/「89 samples」/
    「70 + 19」，均为过期计数，已同批改为实测值（测量：`find eval/gold -name
    "gold-*.json" | wc -l`；`make lint` 输出的「106 条样本」即同源）。
    两类样本是**不同产物类型，不共用 schema**。**第一项（schema 归属，结构必需）**：
    13 份 `pp-*.json` **全部不符合**
    `eval/gold/schema.json`（缺 required 的 `expected_sql`/`result_hash`/`snapshot_sha`
    三键，另有 `base`/`intended_metric`/`note` 三个 schema 未声明的键；实测
    `jsonschema.validate` 13/13 失败），故 `eval/gold/validate_gold.py:37` 的 glob `*/gold-*.json`
    将其排除是**结构必需而非疏漏**——并入 glob 则 `make lint` 当场 13 红。
    **第二项（paraphrase 侧无任何结构门槛）**：`eval/paraphrase_eval.py:60` 只
    `glob` + `json.loads`，`:53-55` 用 `.get()` 取标注键，故 `expected_metric` 一类键名
    拼写错误会静默变成 `None` 并使该样本计为 Plan Acc **失败**——被误归因为 Planner
    缺陷而非样本缺陷（对照 `:69` 的 `r["base"]` 用下标取值、缺键则崩，两种失败形态
    不一致）。**第三项（声明与实际不符）**：`--domain` 参数被接受并写进报告（`:126`）
    但**不参与模型选择**（`:94` 恒用 `MODEL_PATH` = finance），`--domain retail` 会产出标注为 retail、
    实际用金融模型评测的报告（`:91` 的 help 文本「当前仅 finance 改写集」是唯一提示）。
    同时 `validate_gold.py` 的 docstring 称「验证 `eval/gold/<domain>/*.json` **全部**
    符合 schema.json」，与其 glob 只覆盖 `gold-*.json` 不一致。
    **第四项（同批新发现，可追溯性）**：13 份 pp 的 `base` 字段只被
    `paraphrase_eval.py:69` 当作分组标签，**从不解析为样本文件**；其中 4 份写
    `base: "gold-comm"`，而 `eval/gold/` 下**不存在** `gold-comm*.json`（实测被引用
    的 base 中只有 `gold-101`（6 份）/`gold-146`（3 份）真实存在）。报告的 `by_base` 因此出现
    一个无法回溯到任何样本的分组键，失败样本无从定位。
    **第五项（过期计数全仓清单，2026-09-14 逐文件实测）**：按处置方式分两组
    逐个登记归属；除 ① 经用户授权当批修正外，**其余本批均不改**。
    **A 组·现在时声称（① 已修，②~⑤ 待修）**：① `AGENTS.md:63` 术语表「Gold set｜自建
    黄金评测集（50 例）」与 `:271` §9.1 示例模板——契约文件，**已于 2026-09-14
    经用户授权修正完毕**：两处均**不再写死条数**，改为「以 `make lint` 输出的
    「N 条样本」为当前口径」（实测该输出为「106 条样本」），故后续扩张不会再度过期。
    按 AGENTS.md §14 须走**独立 `contract` 提交**并在正文说明理由，不得与本批
    文档变更混合（§8）；②
    `docs/atlas_query_test_cases.md:226`「89 条」——该文档另有 §12/E-03 两处 Guard
    口径错误，已由 ADR-0017 代价 ② 归入 P-1 批次同批纠正，本批不做部分修
    （避免同一文档出现“半新半旧”口径）；③ **对外传播稿**（发布前须重测；
    且数据卡片图片内的数字无法用文本批改，需重渲染）：
    `docs/outreach-series/05-trustworthy-accuracy.md:101`「89 条」、
    `docs/outreach-wechat.md:133/:224`「89 例（70 + 19）」与 `:137` 图注、
    `:289`「最新 65 例」（“最新”是现在时）、`docs/outreach-xiaohongshu.md:7/:40`
    「50 例（48 + 2）」。注：wechat 的**评测结果数字**已绑定具名 sha
    （`9749fc5`/`7d48dcb`，属 B 组），但**样本文件条数**不随快照冻结，故仍会过期；
    ④ **活配置（最可操作）**：`airflow/yaml_jobs/04_semantic_publish_eval.yaml:23-24`
    依赖注释「评测含零售域 19 条」/「评测含金融域 70 条」与 `:39` 任务描述
    「黄金集 Plan Acc + EX（双域 89 条全跑…）」——`yaml_jobs/` 是
    AGENTS.md §4 的可编辑区（非 `dags/generated/`），属 `chore`，可与文档批次分开提交；
    ⑤ 其他现在时声称：`eval/spider/README.md:12`「自建 gold 集（50 条人工标注）
    **仍是**主评测」、`lora/data/README.md:25`「gold 48 条…不可作训练源」（位于
    「**当前状态**（诚实声明）」节下）、`infra/adr/0010-eval-methodology.md:4`
    **状态行**「落地：eval/gold/ 51 例」（正文 `:23/:30/:58` 同值）——ADR **状态行**
    是对当前落地态的声称，与 ADR **正文**记录决策当时语境不同，故归 A 组。
    **B 组·历史记录，不改写**（改了即伪造历史，N1）：⑥
    `README.md:171/:183`（48 例/50 条，绑定具名报告 `b47a6c1.json`/`7d48dcb.json`
    与 Day 30 逐日验收记录）、`docs/release-notes-v0.1.md:33/:102`（50 例，绑定 v0.1
    发布）、`data/snapshots/README.md:60`（89 条，绑定 `9749fc5.json` 与 P7 收口批次）、
    `eval/failures/README.md:28`（48 条，节标题即「当前状态（2026-09-03，HEAD
    7d48dcb）」且绑定 `7d48dcb.json`）、
    `semantic/migrations/2026-09-08-fee-synonyms.md:41`（89 条，绑定具名快照
    `b933e20`）与 `semantic/migrations/2026-09-08-locale-lexicon-and-legacy-archive.md:34`
    （gold 89 条，绑定该迁移文档自身日期的 `make lint` 输出）、
    `infra/adr/0001-*.md:28` 与 `infra/adr/0006-*.md:61/:104`（50 条/50 例，属决策
    当时的目标口径）、`infra/adr/0015-*.md:176`（89 条，绑定具名 commit `9cb8913`
    与具名报告 `7051ef6`/`b933e20`）、`infra/adr/0016-*.md:31/:107`（89 条，绑定该
    ADR 自身 accepted 状态与 P1 批次 B4 落地语境；实测全文**不含** `9cb8913`，
    初稿曾误将其与 0015 合写为同一绑定，已拆开）。
    ⑦ **同形不同源，不属本清单**（登记以免后来者误「修」）：
    `data/fibo/README.md:68`「20 metrics 中 19 条锚定」是 FIBO 映射分母、
    `infra/adr/0022-*.md:349` 与 `docs/design/frontend-console-plan.md:555`「48 条」
    是治理索引条数、`infra/adr/0023-*.md:571`「565 例」与
    `docs/design/adr-0015-pattern-lexicon-en.md:84`「521 全绿」是契约测试数、
    `docs/design/adr-0015-pattern-lexicon-en.md:86` 与
    `docs/design/adr-0015-pattern-lexicon-zh.md:67`「EX 65/65 + 18/18」绑定具名报告
    ——分母均与黄金集样本数无关。
    **本清单自身的三次纠错（检索与引用方法缺陷）**：(a) 初稿曾把
    `docs/design/adr-0015-pattern-lexicon-*.md` 列入过期计数，复核该两文件**只引用
    具名样本 id**（gold-061/063/066/067/149~155），不含任何黄金集总数，引用不成立，
    已删除。(b) **更严重**：初稿检索时加了 `grep -v "eval/reports/"` 过滤，而
    **绑定具名报告正是 B 组的判定标志**，该过滤把 B 组条目系统性删除，导致漏掉
    `README.md:171/:183`、`data/snapshots/README.md:60`、`eval/failures/README.md:28`
    三处（含 README 自身），却在文中自称「完整清单」。去掉该过滤重跑检索后才有上表。
    教训：**排除条件不得与分类判据同源**，否则筛选会静默吞掉整个类别；
    「完整」这类全称声称必须附可复现的检索命令，且命令本身要接受审查。
    (c) 初稿⑦ 曾写 `docs/design/adr-0015-pattern-lexicon-*.md:84`（**错误写法原样留档，
    非有效引用**，自动校验器仍会报它越界，属已知例外），而该 glob 实测
    匹配 **2 个文件**（`-en.md` 与 `-zh.md`）且两者行数不同（`-zh.md` 仅 82 行，
    无 `:84`）——**glob 不得与行号连用**，已拆为逐文件引用。上述三处均由
    自动引用校验器（逐条展开 glob + 比对行号是否越界）捕获，非人工复读发现。
    上述五项中，**第一项为结构必需（非债务，不得「修」）**，第二~四项为待修债务，
    第五项内 A 组 ① 已修、②~⑤ 待修，B 组 ⑥ 不改写，⑦ 不属本清单。
    本批只如实登记不改代码：修 paraphrase 校验与 `--domain` 属 `eval` 类型变更，
    改 airflow YAML 注释属 `chore`，改 AGENTS.md 属 `contract` 类型变更（须说明
    理由），三者须分别单独提交（AGENTS.md §8 禁止一个 PR 混合类型）。
    本清单的可复现检索命令（不含任何与分类判据同源的排除项）：
    `grep -rn "48 例\|48 条\|50 条\|50 例\|51 例\|89 条\|89 例\|65 例\|70 条\|70 例\|19 条\|19 例" --include="*.md" --include="*.yaml" --include="*.yml" .`
    （排除项只允许 `.venv/`、`eval/reports/` 产物本体与 `EVAL_REPORT.md` 机器生成物；
    **不得**排除正文中引用 `eval/reports/` 的行，那正是 B 组判据。
    2026-09-14 实测命中 **74 行**，其中包含本 KL 条目自身对上述数字的引用（自指），
    需人工剔除后再按 A / B / ⑦ 三组归类）
32. **前端不解除任何后端限制，且引入第二套工具链（ADR-0018，P0b，2026-09-16）**：
    `workers=1` 约束仍在（0020 决策 ⑧ 收窄后的理由：会话/轮数/身份指纹已入 checkpoint，
    仍为进程内态的是限流桶、审计写与 SQLite 单写者）；前端只是只读消费面，不改善可扩展性。
    仓库同时从 Python 单语言变为双工具链：`uv sync` 不再足以准备开发环境，node 仅存于
    nvm（实测 `~/.nvm/versions/node/v24.16.0/bin`），**非交互 shell 不可见**（实测
    `command not found: node`），一切脚本化调用必须经 make 的 PATH 注入。`make ui-*`
    与 `serve-dev` 探测不到 node 时打印指引并 `exit 1`，**不静默跳过**。fresh clone 无
    `frontend/dist`（被局部 `.gitignore` 忽略）：API 照常启动（条件挂载跳过并打 warning），
    但浏览器无界面可用——需先 `make ui-build`；P0b 本体也只有最小占位壳，五个面板与
    图表属 P1~P3（**时点补充，2026-09-16**：P1 面板 1 与 P2 面板 2/3 + 面板 4 前
    6 子页已落地，见 #33/#34；图表与 reports/snapshots 两子页属 P3——本条其余事实不变）。
    构建门槛 `make ui-check`（tsc + vitest + build）在本地执行；
    pre-commit 配置已就绪（`frontend/` 变更触发 `make ui-check`，`check-added-large-files`
    已 exclude lock 文件），但本机 GitHub 不可达（实测 `git fetch` 被 reset），hook
    环境初始化无法完成——门槛以 make 目标为准，pre-commit 端到端待网络可达时补验。
33. **前端 P1 工作台的四条诚实边界（ADR-0018 P1 批次，2026-09-16）**：① **截断只报
    「可能」**（门禁 G3 接受降级）：`row_count == limit` 的确定截断标志未进 0022 契约
    （设计页 §6.5），`TruncationNote` 只显示「结果可能被 Plan 的 limit 截断（当前 = N）」
    ——确定标志落地前不改语气（把推测写成事实即 N1/N2 违约）；与之并列的**前端渲染
    上限是确定事实**：响应行已全部到达浏览器，表格对超过 500 行的部分仅渲染前 500
    （前端常量，非后端截断），完整数据出口 = `atlas query --format json`（同一只读
    网关）或收窄 Plan 的 `limit` 后重发，**不得**引导站外执行 SQL（N3）；② **前端
    零遥测**（门禁 G1 裁定，0018 落地注记 P1 批次第 1 节）：无上报端点、无遥测 SDK，
    错误原文如实渲染不吞错；③ **P1 只交付面板 1**（**时点更正，2026-09-16 P2 起**：
    本子条记录的是 P1 交付时点的路由形态——彼时任意深层路径（如 /governance/metrics）
    刷新均渲染同一工作台；P2 已落地 `/ask`、`/sessions`、`/governance/{6 子页}` 路由，
    reports / snapshots 与图表属 P3。SPA 兜底 200 HTML 仍是 0018 判据 7 的能力，
    与路由能力是两件事）；④ **token 只存内存**
    （设计页 §3.5 约束 3）：顶栏粘贴通道刷新即失，签发只能 `make token ROLE=…`，
    前端不内置签发逻辑或密钥。
34. **前端 P2 治理面的四条诚实边界（ADR-0018 P2 批次，2026-09-16）**：① **会话时间线
    只能展示当前运行的会话**（跨重启、跨标签页历史**无数据源**）：0022 未开
    `/governance/sessions` 类端点，0020 的 SQLite checkpoint 是 Agent 内部状态存储、
    不是可查询的会话目录——本标签页本次运行之外累积的 `session_id` 前端列不出来
    （服务端重启后 `boot_id` 变化，历史会话仍在 SQLite 里但列不出来）；要列出历史
    会话需新增只读端点并裁定其权限口径（会话含问句原文，属敏感面），**未裁定**；
    ② **治理端点一律 Bearer**（2026-09-16 实测：无 token 请求
    `/api/v1/governance/policies` → 401）→ 角色矩阵取不到就不能签发，**首次激活与
    刷新后都必须先粘贴一次 `make token` 的输出**；③ **dev 签发通道仅 `make ui-dev`
    存在**：`POST /__dev/sign` 是 vite dev-only 中间件（`spawn uv run --env-file .env`
    调用 `serving.auth.sign_token`，与 `make token` 逐字同构，**vite 进程不读密钥**）；
    `make serve-dev` 与容器无此端点，面板降级为「复制 make 命令 + 粘贴」——不伪造
    第二套签发逻辑；④ **P2 治理面 6 子页挂载即发 6 条并发请求**
    （models / metrics / dimensions / synonyms / values / policies），子页切换不重拉；
    `reports` / `snapshots` 子页与 ReportDrawer 属 P3——该 6 条是 0022 决策 ⑥
    「240/min 治理桶」推导的前提，**不得**为减请求数合并或懒加载。（**P3 时点
    更正，2026-09-16**：`reports` / `snapshots` 两子页与 ReportDrawer 已随 P3
    落地，治理面终态 8 条并发已实态化；本条其余各句不变。）
35. **图表接入已落地（P3，ADR-0025，2026-09-16），能力边界仍有三条**：① **时间轴
    列以编译声明为权威**——`yoy`/`pop`/`cumulative` 的 x 轴由
    `Compiler.emitted_time_column` 声明携带（`render_chart` 的 `time_columns` 入参，
    前端不推断）；非编译路径（`--llm` 候选链）走 `_TIME_COLUMN_NAMES` 兜底、命中时
    spec 的 `note` 标注权威等级（与 #21 的 P3 追加同源）；② **`yoy`/`pop` 只画当期，
    不画多序列对比**——对比值只在数据表与 `note` 里（扩多序列需推翻 ADR-0025
    决策 ⑥，属 spec 破坏性变更）；③ **图表数据恒 ≤ 200 点**（`MAX_CATEGORIES`
    超限降级表格并注记，非本批新增）。**与 #21 的分工**：#21 记录渲染语义
    （spec 级确定性、无像素），本条记录**接入状态与能力边界**（ADR-0025 裁定于
    2026-09-14，接入落地于 P3 2026-09-16），两条不得合并阅读。
36. **多步分析的不可加组合直接拒绝，不做近似（ADR-0026，2026-09-16）**：未登记
    可加资格的指标/维度组合不做近似、不引入 LLM 猜测——前置资格门在执行任何
    SQL 之前直接置 `analysis.status=unavailable`（reason_code=`missing_eligibility`，
    资格证据随快照登记于 `data/snapshots/<sha>.analysis.json`），零 SQL 达执行器；
    初始登记组合的验收场景 = attribution-001（佣金收入按分支 2013Q4 vs 2013Q3）。
37. **综合截断不产出可能不完整的数字（ADR-0026 决策⑤）**：Budget 行数/组数上限
    触发 `possible_truncation` 时，综合直接置 `unavailable`（totals=null、items=[]、
    固定措辞文本），绝不给出可能不完整的贡献数字——「不可用即拒答」是设计行为
    而非缺陷。
38. **多步分析只对固定快照验证（可变数据边界）**：能力只在锁定快照上验证（分析
    评测与资格证据绑定快照 sha；本批实测锚定 `7c966e9`）；数据变更需重新锚定 sha
    并重建资格证据（`*.analysis.json`），跨快照数字不可比——与 #30 值域快照态
    同源纪律。
39. **无任务级硬超时**：Budget 只有行数/表/组数上限，无任务级 deadline/取消机制
    ——分析轮的耗时上界不由系统保证（`elapsed_ms`/`latency_ms` 只如实记录已执行
    子 SQL 耗时，不构成时延承诺）。
40. **分析界面为控制台工作台一档，非独立产品**：多步分析既经 HTTP API
    （`POST /api/v1/analyze` request/response；`POST /api/v1/analyze/stream` SSE
    流式回放，④a/ADR-0028 决策④）交付，也在前端控制台工作台「分析」模式呈现
    （`AnalysisBlock` 渲染 `analysis` 投影；流式终态与 request/response 逐字一致）。
    边界如实声明：流式为 **compute-then-stream**（`agent.analyze()` 跑完后回放，
    **非** SQL 边执行边流）；事件名**借鉴 AG-UI 词表、非 AG-UI 协议兼容**（无 AG-UI
    客户端可直连）；④b（SDK 接入）仍 gated。治理面板只读 Git 文件与产物，不是分析入口。
41. **数据飞轮沉淀需人工落源，非实时自进化（ADR-0027）**：同义词/值域别名候选与指标
    proposal 由飞轮自动归纳并落非权威区（`semantic/synonyms/_candidates/`、
    `semantic/values/_candidates/`、`eval/failures/_proposals/`），但**必须人工确认
    后追加进 Git 权威源才生效**——候选不自动影响检索、编译或评测（判据 1/3/4）。
    以下能力**未实现**：① 任务完成度评估体系（与 ADR-0010 的 EX/Plan Acc 二元口径
    直接冲突，需独立 ADR 仲裁评测地基）；② 高频查询自动优化缓存（当前无缓存层、
    无 profile 证据）；③ LLM 自动改写提示词/口径（提示词走 `agent/prompts/*.yaml` +
    CI 回归，至多产 proposal）。
42. **LLM 引擎服务化为部分落地：确定性内核已实现，真机质量/EX 数字未测（ADR-0029）**：
    已落地并单元/契约验证的是**确定性内核**——`agent/llm_policy.py` 分级路由（候选
    =schema-only 云/自托管皆可；叙述=result-bearing **强制自托管、云无条件排除**）、
    `agent/narrative_guard.py` 数字接地硬闸、fail-closed 回落（缺自托管/端点失败/非接地
    → 确定性模板，**绝不升级云**）、serving 侧 `llm` 加性可选旗标（默认 off 逐字向后兼容，
    契约不增端点）+ `serving.auth` RoleSpec 的 `llm` 能力位（无权 → 403，不静默
    降级）+ `atlas.llm.narrative` 埋点（只记分级与 token 计数，不落 prompt 原文/结果数值/
    密钥，N9；成本记入 `gen_ai.token_cost`，与 Guard `Budget` 分离）。**未测**：真自托管 vLLM
    的候选路由与叙述质量数字——环境无 GPU/端点，`make compare RAG_ENGINE=openai` 与真机叙述
    评测一律 **blocked 登记，不编数字**（AGENTS.md N1）。engine=stub 仍是确定性默认；接地叙述
    文本 `grounded=false` 永不发货；角色→`llm` 能力为配置态映射，非真实 IAM。

43. **可信问数工作台尚处 M0 开发（ADR-0031）**：T01-T10/T13 已交付（详见 §5.3 各子节），
    整体测试全绿（1760 passed + vitest 30 files/253）。**关键诚实边界**：① 真 IdP 回调
    验收 BLOCKED（需部署者环境）；② live 在线源四步分析明确拒绝
    （`analysis_consistency_unavailable`）；③ `DorisConnector` 元数据探测/取消查询/
    快照读/一致性分析能力声明全 False（未实现不多报）；④ 运行/反馈面未接限流桶、
    `GET /feedback` 未分页；⑤ 默认图尚未接理解节点（T10 合同链无生产调用方）；
    ⑥ T09 拓扑编译只做编译不构造 LangGraph 运行时，生产流量仍走 `agent/graph.py`
    硬编码图。历史报告数字不迁移为新链路效果，快照清单不等于数据指纹已复核

**如果有真实企业数据，我会优先补做**：数据契约、IAM 集成、审计留痕、容灾、并发压测、模型红队测试、变更管理流程。

---

## 11. 能力来源登记（避免夸大）

| 能力 | 状态 | 本项目中的动作 |
|---|---|---|
| 工作台证据基线（ADR-0031 T01） | **新增清单与测试装配，M0 未完成** | 详见 §5.3「工作台证据清单」 |
| 可信问数工作台控制原语（ADR-0031 T02） | **新增发布原语与默认流程合同** | 详见 §5.3「控制库与默认流程合同」 |
| 私有部署认证与控制 ACL（ADR-0031 T03） | **新增登录与控制权限，真 IdP 验收 BLOCKED** | 详见 §5.3「身份登录与控制权限」 |
| 共享安全执行内核与数据身份（ADR-0031 T04） | **新增唯一执行通道，真链等价 6/6** | 详见 §5.3「共享安全执行内核与数据身份」 |
| 真实事件、运行恢复与最小反馈（ADR-0031 T05） | **新增运行事实与最小反馈** | 详见 §5.3「真实事件、运行恢复与最小反馈」 |
| 运行历史目录、运行图与真实 SSE（ADR-0031 T06） | **新增运行历史/事件流端点与只读视图** | 详见 §5.3「运行历史、运行图与真实 SSE」 |
| 只读数据源接入向导（ADR-0031 T07） | **新增源修订/受限探测/部署绑定** | 详见 §5.3「只读数据源接入向导」 |
| 语义草稿、审核与 Git 制品发布（ADR-0031 T08） | **新增草稿/校验/审核/CAS 发布/回退** | 详见 §5.3「语义草稿、审核与 Git 制品发布」 |
| 理解合同、召回与完整绑定（ADR-0031 T10） | **新增意图合同链路，默认图未接理解节点** | 详见 §5.3「理解合同、召回与完整绑定」 |
| 最小发行、运维与首次接入验收（ADR-0031 T13） | **新增备份/恢复/诊断/验收入口** | 详见 §5.3「最小发行、运维与首次接入验收」 |
| Multi-LoRA SQL 推理、SFT 飞轮 | 已有工程经验 | 用公开数据重建训练/评测流水线，**不声称是新发明** |
| 元数据治理、SQL/tokenizer 解析 | 已有工程经验 | 改造为 Atlas 的 DDL/ETL 注释抽取器 |
| 标签体系、Bitmap、OneID、销售/金融指标 | 已有业务经验 | 抽象为 Atlas 的指标/实体/分群/权限设计模式 |
| YAML 编排 + Airflow + GitLab CI | 已有工程经验 | 迁移为指标任务编排与 CI 契约测试 |
| GraphRAG + Milvus 混合过滤 | 已有工程经验 | 复用于指标、同义词、业务术语检索 |
| OpenTelemetry + PromptOps + LLM Judge | 已有工程经验 | 接入 SQL 调用链、成本、回归评测 |
| 声明式语义层、指标版本血缘 | **需新建** | 基于 Apache Ossie 规范实现编译器、治理扩展与测试 |
| Data Agent 状态机与工具链（LangGraph 编排 / MCP 暴露 / 防幻觉图表 / 反馈与 handoff） | 已有工程经验 | 以确定性优先落地 agent/ 状态机（8 节点）+ tools 四件套 + MCP 工具服务器 + chart + feedback；7 场景 e2e 实测（含多轮追问 S7）见 docs/e2e-acceptance.md（数字全部出自 eval/reports/e2e-acceptance.json，不另立声明） |
| 图表 spec 级确定性渲染与接入（无像素；前端零图表类型决策） | 已有工程经验 | 以 ADR-0025 接入 `/ask` 响应链：时间轴列由 `Compiler.emitted_time_column` 声明携带、兜底命中 note 显式标注、`yoy`/`pop` 只画当期、恒 ≤ 200 点；`tests/test_chart.py` 16 既有用例逐字未改 + 6 新增全绿；浏览器走查实测（截图 `docs/screenshots/p3-walkthrough-1-ask-chart.png`） |
| 多步任务规划与归因分析（ADR-0026，固定四步贡献模板） | **已实现（单一模板 + 初始组合）** | 支持范围：单一模板——两期总计 + 两期按维度分解的四步贡献分析；绝对双时间（如 2013Q4 相对 2013Q3）；初始组合 = attribution-001 同构场景（佣金收入按分支 2013Q4 vs 2013Q3，可加资格随快照登记，未登记组合直接拒绝不做近似）。确定性措辞：Plan → 模板 → Compiler → Guard → 执行 → 精确算术综合，LLM 零参与。API/SDK 入口：`POST /api/v1/analyze`（CLI `python -m eval.e2e_acceptance` 为验收入口而非用户产品）；验收证据：analysis-eval + e2e S8~S12 + api-verify A10a~A10d（报告见 `eval/reports/`）。④a 另交付 SSE 流式端点 `POST /api/v1/analyze/stream`（**compute-then-stream**、借鉴 AG-UI 词表非兼容）与前端流式消费（契约 `tests/test_analyze_stream_contract.py` 绿） |
| 数据飞轮（ADR-0027：反馈采集 → 候选生成 → 人工落源） | **部分落地** | SFT 侧五阶段状态机已落地（`lora/flywheel.py`，scan→review→export→build→train），当前空转（0 失败样本 + 无 GPU）；语义沉淀路径本次交付——同义词/值域别名候选归纳（`lora/candidates.py`）+ 指标变更 proposal 生成（`lora/proposals.py`），产物落非权威区（`_*` 候选目录 + `eval/failures/_proposals/`），**须人工确认后追加进 Git 权威源才生效**（判据 1/3）；候选生成前后 `make lint` 逐字一致（判据 3）；指标 proposal 只读、不写 `semantic/ossie/`（判据 4） |
| Ossie / Polaris / Iceberg / Doris | **需新建** | 单机部署、基准测试、维护 ADR（0002/0004/0005） |
| Agent 安全执行与自动洞察 | **需新建** | 先做安全工具，再扩展规划与归因 |
| HTTP API / 认证中间件 | 已有工程经验 | 以 FastAPI 落地 serving/api.py + serving/governance.py（ADR-0012 + ADR-0022 契约 v2：`/api/v1` 前缀 + 治理面 8 集合/2 钻取 + 限流两桶 + `/plan/execute` + JWT），/api 契约测试 96 例（身份注入/会话冲突 422/限流两桶 429/契约 v2 防漂移 22 路径/审计字段集）+ 真链验收 api-verify A1-A9 在档 |
| LLM 引擎服务化（ADR-0029：分级路由 + 接地叙述 + fail-closed + 受控门控） | **部分落地（确定性内核已实现，真机评测 blocked）** | 已实现并测：`resolve_llm_backend` 决策矩阵（叙述强制自托管、云排除）、`verify_grounded` 数字硬闸、serving `llm` 加性旗标（off 逐字兼容、该旗标不增端点）+ RBAC 403 + `atlas.llm.narrative` 埋点（N9 无原文/密钥；成本与 Guard Budget 分离）；测试：`tests/test_llm_policy.py`/`test_narrative_guard.py`/`test_narrative.py`/`test_generator_injection.py`/`test_api_llm_serving.py`/`test_otel_llm.py` + 前端 `narrative-block.test.tsx`。真机候选/叙述质量与 EX 数字 = blocked（无 GPU/端点，不编数，见 Known Limitations #42） |
| JEV 决策引擎接入（ADR-0030：可插拔判别后端，加性扩展 ADR-0029） | **proposed（设计已定，实现未启动）** | 为「非 OpenAI 兼容线格式」判别引擎（如 TypeSafe System One 的 Choice/Score/Noul 强类型原语）补引擎类型维度 + 判别客户端协议；继承 ADR-0029 全部安全约束（敏感度划界 / fail-closed / RBAC / 可观测）；Jev 永不产 SQL、不新增执行通道（N3）；CJK 支持须自测，不得假定可用；**实现未启动，不声称已落地** |

---

## 12. 许可与声明

- **代码**：`Apache-2.0`（全文见 [`LICENSE`](LICENSE)；版权归属与第三方声明见 [`NOTICE`](NOTICE)）
- **数据与本体**（均不入库，Atlas 不分发；由使用者自行获取、各自遵循原始许可）：
  - TPC-DI 源数据：TPC 基准条款；生成工具 PDGF 受 BANKMARK EULA 约束（`data/raw/gen_tpcdi.sh`）
  - TPC-DS kit（SF0.1）：TPC EULA v2.2；clone 指令见 `scripts/setup_tpcds.sh`
  - FIBO 本体（FND+FBC+BE）：MIT License（Copyright 2020 EDM Council）；FIBO 为 EDM Council 商标；clone 指令见 `data/fibo/README.md`
  - OMG Commons / LCC：RDF 内容由使用者自行下载（`data/fibo/vendor/` 不入库），其许可条款**未在本仓留存文本**，使用前需自行向 OMG 确认；本仓只入库 IRI 标识符字符串
  - BIRD finance：公开学术基准，仅作历史对照、不新增接入（ADR-0014）
- **依赖**：声明见 `pyproject.toml` / `uv.lock`；GPLv2 / LGPL-3.0 数据库驱动已于 P0a 移除（ADR-0023）；机器生成清单：`make license-check REPORT=1` → [`exports/dependency-licenses.json`](exports/dependency-licenses.json)（GNU make 不接受 `--report` 长选项，故用变量触发；脚本侧 `python -m infra.license_check --report` 原样可用）
- **第三方工具与素材**：
  - `scripts/tpcds_kit_sf01.patch`：含 48 行 TPC-DS kit 源码（16 删除 + 32 上下文），受 TPC EULA v2.2 约束，**不适用** Apache-2.0（文件头声明块 + `NOTICE` §2）
  - Remotion（`docs/outreach-video/`）：source-available 两层许可——个人 / ≤3 人营利组织 / 非营利适用 Free License，否则需 Company License；`node_modules` 与渲染视频不入库（`NOTICE` §3）
  - 入库媒体（`docs/contact-wechat.png`、`docs/outreach-wechat-assets/*` 等）为自产，无第三方素材

**许可证版本边界**：历史 tag（v0.1.0 / v0.1.1 / v0.1.3）树内不含 `LICENSE` 文件；MIT 文本于 `b547489` 入库、经合并 `14f210a` 进入主干；自 P0a 批次（2026-09-16）起统一为 Apache-2.0 全文。历史文本不追溯改写，判别以 Git 历史为准。

**TPC 性能结果约束**（TPC-DS kit EULA 4.c）：README 中的耗时/行数类数字均为 Atlas 自身链路实测，非 TPC 工具性能结果；今后若公开 dsdgen/dsqgen 相关性能数字，需按 4.c 加显式标识（TPC Benchmark Result / 学术研究且非营销声明 / 声明不可比）。

---

## 附：我的诚实承诺

本项目 README 中的每一个数字，只能来自以下之一：

- Git commit / CI log
- 评测 JSON（`eval/reports/`）
- Prometheus / Grafana 导出
- 云账单
- 公开基准官方结果

**没有计算脚本的数字，不写。单次运行得出的性能结论，不写。**

---

## 关于作者与交流

**范德塞尔** · 江苏 苏州

> 数据平台 / 可信 AI 问数方向。Atlas 是我个人设计与实现的完整项目——
> 从 Apache 语义层（Ossie + 自研治理扩展）到确定性 NL2SQL 编译器、
> 再到可复现评测闭环（黄金集 + 快照锁定），全链路动手实现；
> 本 README 中的每一个数字都绑定 commit 或评测报告（见文末《我的诚实承诺》）。

如果你在做同类方向（语义层 / 指标平台 / NL2SQL / 数据 Agent），欢迎交流：
项目里踩过的坑（Ossie 不是运行时、固定快照评测、TPC-DI 时间编码等）
也许能帮你少走弯路，也欢迎对 Atlas 提 issue / PR。

<div align="center">

<img src="docs/contact-wechat.png" width="280" alt="微信二维码：扫一扫添加好友" />

**扫码添加微信，备注「Atlas」**（注明来意，方便我知道你是从仓库来的）

</div>
