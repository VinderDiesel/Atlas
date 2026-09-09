# Atlas

> **以 FIBO 本体为语义锚点的金融可信 AI 问数平台**｜统一语义层 + 受控 NL2SQL + 可解释 Data Agent
>
> Status: **under active development**
> 所有数字来自可复现脚本产物，不是营销断言。见 `EVAL_REPORT.md`。
> v0.1 发布说明与已知边界：`docs/release-notes-v0.1.md`
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
- [x] Day 27 指标版本机制：`governance_validate.py` 新增 supersedes 链跨文件校验——取代目标存在、非自身、新版本号严格大于被取代版本（递增天然防环）、被取代者不得仍为 active、治理记录不得重名；演进规范＝新名 + supersedes 旧名（同名全局唯一由 ossie_validate 强制，N8）；契约测试 10 例 `tests/test_governance_validate.py`，CI 经 `make lint` 自动执行
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
（`make seed-retail`，4 表入 dwd），gold 样本 19 条全锚定（14 中文含 1 歧义 + 5 英文，
锚定快照 1e5d35b，终验复验零回归报告 9749fc5——见 eval/gold/README.md 零售段与
EVAL_REPORT.md）；serving 已双模型路由（§9.1 请求体 `model` 字段）+ demo 集成测试
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
CI 经 `make lint` 自动执行。
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
├── gold/        # 自建黄金集：50 例目标，金融段为主（FIBO 概念标注），人工标注
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
- 产出：`eval/reports/<git sha>.json`（当前 `b933e20`：金融域 70 例（65 可解析 + 5 歧义）Plan Acc 65/65、反问 5/5、EX 65/65；零售域 19 例（18 + 1）Plan Acc 18/18、反问 1/1、EX 18/18——29 表全量快照，锚定 hash 未漂移，按域分节不混报；基线分析 `eval/reports/baseline-compiler-b933e20.json` 与 `docs/baseline-compiler.md`——注册语义域内确定性链零 LLM 覆盖：金融 70/70、零售 19/19；域外问题由 RAG+LLM（gold-50 时代实测 44/44 持平，`rag-llm-openai-7d48dcb.json`，历史对照不混报）/LoRA（blocked）策略对照承接）
- 闭环（Day 39-42 后）：`make report` → `eval/report.py` 机械转述生成 `EVAL_REPORT.md`（八节，无手写数字，每格带 source 列）；CI 回归 `.github/workflows/eval.yml`（dry Plan Acc 自洽断言；完整 EX 需数据环境，手动触发）；失败样本 `eval/failure_collect.py` 自动归类 → 人工确认 → `lora.flywheel` 五阶段进 SFT（answer 形态 = 合法 Plan JSON，ADR-0008）；LLM 实测后仍 0 失败（缺陷在评测侧修复归零），飞轮空转与 LoRA 训练（无 GPU）如实登记

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
| TPC-DI（零售经纪，2012-07-07~2017-07-07 实测数据段；已装载 17 张 ODS + 8 张 DWD，快照 `b47a6c1`） | 主场景数据：重建金融库表关系、指标场景 | 公开基准，注册下载 |
| FIBO 本体（FND+FBC+BE 域）+ OMG Commons/LCC | 语义锚点：概念 IRI 注册表与对齐映射 | EDM Council（MIT）/ OMG 规范（研究用途） |
| BIRD finance | 公开集能力对照（仅参照，不混报） | 公开学术基准 |
| 自建黄金集（50 例目标） | 主评测集 | 本人基于 TPC-DI 人工标注 |

### 7.2 明确声明

- ❌ 本项目**不使用、不包含**任何前雇主/现雇主的真实数据、代码、指标定义或业务指标
- ❌ 不使用真实公司名构造场景；实体关系由 TPC-DI 重建
- ❌ 公开集分数**不外推**为企业场景分数
- ✅ 已有工程经验（SQL LoRA、GraphRAG 检索、OTel 可观测、YAML 编排）作为**设计输入**，在本项目中以公开数据重新实现

---

## 8. 目录结构

```text
atlas-data-platform/
├── .github/workflows/   # GitHub Actions（lint / eval 回归 / tag 自动版本锚点）
├── semantic/
│   ├── ossie/           # ⭐ Apache Ossie 语义模型（主规范）
│   ├── governance/      # ⭐ Atlas 治理扩展 Schema（补 Ossie 缺口）
│   ├── policies/        # 行级权限策略
│   └── schema/          # ⚠️ 已废弃：早期自研 DSL，仅作演进对照
├── sql/                 # tpcds_ddl（零售历史，只读）/ dwd / dws / views（金融表待建）
├── metadata/            # SQL 元数据抽取器（候选提取，非计算层）
├── airflow/             # yaml_jobs（源）+ dags/generated（自动生成，勿手改）
├── agent/               # graph（LangGraph 状态机）/ planner / compiler / security / feedback /
│                        #   tools（registry 四件套 · mcp_server · chart）/ cli / prompts
├── retrieval/           # bm25 / milvus_client / graph_store
├── serving/             # api（HTTP 服务面 v1，ADR-0012）/ auth / 验证工具
├── observability/       # otel / dashboards
├── eval/                # gold / spider / bird / runner / reports
├── lora/                # SQL 适配器训练与数据飞轮
├── infra/               # docker / adr；ci 为历史遗留草案（活动 CI 在 .github/workflows/）
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
| `make serve` | 启动 HTTP API（uvicorn 127.0.0.1:8000，单进程，见 §9.1） |
| `make token` | 签发本地测试 JWT（默认 ROLE=hq_admin；如 `ROLE=branch_manager CONTEXT='{"branch": "east"}'`） |
| `make api-verify` | HTTP API 真链验收（A1-A7：全链 EX / 认证 / 三角色差异 / 会话冲突 422 / 跨域拒绝，产出 `eval/reports/api-acceptance-<sha>.json`） |

### 9.1 对外 HTTP API（v1）

Atlas 的服务面（ADR-0012，落地 [serving/api.py](serving/api.py)）：同一确定性链路
（Planner → Compiler → Guard → Doris 只读执行）的 HTTP 出口，engine=stub 确定性
默认，LLM 引擎服务化属 Phase 2。

| 端点 | 认证 | 请求 | 响应 |
|---|---|---|---|
| `GET /health` | 公开 | — | `{status, head_sha, snapshot_sha\|None}`（存活 + 快照绑定状态） |
| `POST /plan` | Bearer | `{question, model?}`（≤500 字符） | `{kind: "plan", plan}` 或 `{kind: "clarify", clarification}`（歧义 200，CLI exit 1 语义的 HTTP 化） |
| `POST /compile` | Bearer | Plan JSON（`metric/dimensions/time/filters/order_by/limit`，含 `model?`） | `{sql}`（Doris 只读方言）；结构非法/编译失败 422 |
| `POST /ask` | Bearer | `{question, session_id?, model?}` | TurnResult 全集（kind ∈ answer/clarify/blocked/error；rows 的 Decimal→str 保精度、datetime→ISO8601）；快照 meta 缺失 503 |

多模型路由（P7）：`model` 字段选语义模型域（`finance` 缺省——向后兼容，旧请求体
零变化 / `retail`），未知值 422；会话键（session_id）按模型隔离，跨模型不续接
（finance/retail 各一 Agent 单例，uvicorn 仍须 workers=1，见 KL #28）。

认证：JWT（HS256）复用 `serving/auth.py`，密钥走 env `ATLAS_JWT_SECRET`（N9，
无默认值）。本地签发测试 token：

```bash
make token                          # ROLE=hq_admin
curl -H "Authorization: Bearer $(make token)" \
  http://127.0.0.1:8000/plan -d '{"question":"2013 年第二季度总交易额是多少？"}'
```

> 本地 `make serve` 监听 127.0.0.1:8000；容器部署宿主端口映射为 **8001**
> （本机 8000 被其他服务占用，见 compose 注释）——容器 curl 请用
> `http://127.0.0.1:8001/`。

身份 → 行级策略（2026-09-05 服务面硬化批次，ADR-0011 落地注记在档）：
`/ask` 把已认证 claims 下推为行级身份（`agent.ask(identity=claims)` → Guard
Policy 注入；`/plan` `/compile` 无执行面不注入）。可见信号与语义：

- `explanation.policy_effect` = 「行级策略已生效（角色 X，策略 Y）」——只给
  角色与策略名，**不给条件值**（0011 不外泄细节，与 blocked 不回流 SQL 同精神）；
- 会话 × 身份：session_id 绑定首个请求的身份指纹（全 claims）；同一会话换
  身份 → **422「会话身份冲突，请换新 session_id」**（换身份必须换会话）；
- 限流：per-token 共享桶（/plan /compile /ask 同桶计数），超限 → **429 +
  Retry-After 头**（下一窗口起点秒数）；/health 公开、401 路径不计。

| env | 缺省 | 说明 |
|---|---|---|
| `ATLAS_JWT_SECRET` | 无（N9） | JWT 签发/校验密钥（本地 `make token`） |
| `ATLAS_AUDIT_DISABLED` | 空 = 开 | `1` 关闭业务审计 JSONL（`serving/audit/audit.jsonl`，gitignore；每业务请求一行，不含 SQL——SQL 由 OTel span 承担） |
| `ATLAS_RATE_LIMIT_MAX` | `60` | per-token 每分钟上限（**配置占位非实测阈值**——真实容量边界需压测）；`0` = 关 |
| `ATLAS_RATE_LIMIT_WINDOW_SECONDS` | `60` | 限流窗口秒数；`0` = 关 |

容器化部署（单机，依赖 Doris 已在 compose 内）：

```bash
docker compose up -d --build atlas-api   # 8001:8000；镜像无 .git，快照身份由
                                         # build arg GIT_SHA 注入（默认 b933e20，
                                         # 即 data/snapshots/ 最新 29 表全量数据
                                         # 版本；数据重装后更新 .env 的 GIT_SHA）
curl http://127.0.0.1:8001/health
```

真链验收与报告：`make api-verify`（全 HTTP 栈 + 真 Doris + 锁定快照）：
A1 问→编→问 EX 与 gold 锚点一致 / A2 歧义反问 / A3 认证拦截 / A4 存活 /
A5 三角色行级差异（gold-146 × hq_admin vs branch_manager，策略名可见且条件值
不外泄）/ A6 会话身份冲突 422 / A7 零售品类受限（category_analyst）+ 跨域
Guard 拒绝（region_manager × finance → blocked，0011 决策 4 真链证据）。
最新报告 `eval/reports/api-acceptance-4a547e7.json`（A1-A7 全绿，snapshot_sha
=b933e20）。硬化后剩余边界如实：会话/限流/身份指纹为进程内（uvicorn 必须
workers=1）、审计本地 JSONL 非防篡改、身份为本地签发 HS256（无 IdP）、Doris
per-user identity 透传属 0011 决策 4 独立项——见 KL #28 ③。

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
    `eval/reports/rls-verify-4a547e7.json`（finance 差异集 3；retail 差异集 2：
    region_manager（州=TN）与 hq_admin 结果一致系 SF0.1 单州数据事实，如实报告，
    差异由 category_analyst 品类受限承担）；带身份 HTTP 化实测见 api-verify
    A5-A7（`eval/reports/api-acceptance-4a547e7.json`：三角色差异 / 会话身份冲突
    422 / 零售品类受限 + 跨域 Guard 拒绝）与 demo 集成测试
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
    类目）降级表格并注记；NaN/Inf/空结果拒绝（agent/tools/chart.py docstring）
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
    Prometheus/Grafana 只覆盖 ask 路径；会话记忆为进程内 MemorySaver（重启即失，
    会话内多轮有效）
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
28. **HTTP API v1 边界（ADR-0012，2026-09-03；服务面硬化批次 2026-09-05 收窄
    ③）**：服务面为本地演示/集成面，非生产部署——①会话是进程内内存态
    （MemorySaver checkpointer + 进程内轮数），重启即失、无横向扩展；②uvicorn
    必须 workers=1，多 worker = 会话/限流/身份指纹多份分裂；③**身份下推/限流/
    审计已落地（2026-09-05）**：/ask 已验证 claims → 行级策略随 Guard 注入
    （explanation 可见信号，条件值不外泄）、per-token 共享桶限流（429 +
    Retry-After）、业务审计 JSONL（每请求一行，不含 SQL）——绑定 api-verify
    报告 `eval/reports/api-acceptance-4a547e7.json`（A5-A7 身份场景）；**剩余
    边界如实**：限流为进程内固定窗口（默认 60 次/分钟是配置占位非实测阈值）、
    审计本地 JSONL 非防篡改（生产需外置）、身份为本地签发 HS256 JWT（无 IdP，
    0011 决策 4「真实多租户 → gateway 认证先行」推翻条件未触发）、Doris
    per-user identity 透传属 0011 决策 4 独立项；④engine=stub 确定性默认，LLM
    引擎服务化属 Phase 2；⑤容器内 /ask 依赖构建时注入的快照身份 ATLAS_GIT_SHA
    （镜像无 .git）且对应 meta 随仓库进入镜像——带 seed 数据的环境才可答；
    ⑥/api 契约测试 43 例（tests/test_api.py 26 + tests/test_api_hardening.py
    17，fake 注入无 DB）+ 真链验收 make api-verify（A1-A7）在档
29. **filter 不支持形态 → 澄清（不猜测）**：自由双指标比较（“佣金高于成交量的
    分支”）、维度值模糊无法命中语义层同义词、HAVING 语义度量阈值但问句未解析出
    metric（无从挂载聚合比较）均返回 ClarificationRequest；时间词不并入 filter
    （时间一律走 TimeSpec，相对时间见第 11 条）——与 ADR-0014 ① 裁定一致，
    这些形态是澄清机制的评测载体而非缺陷（gold-149~155 中歧义样本即为此类）
30. **维度值域是快照态而非实时（ADR-0016，B4）**：`semantic/values/*.json` 由
    `make profile-values` 从锁定快照 SELECT DISTINCT 生成，值域与最新锁定快照绑定
    （`make lint [values]` 漂移即红）。数据重新装载后必须重跑 `make profile-values`
    同步值域，否则 lint 红。同名维度字段跨数据集时，编译器按 datasets 顺序首匹配
    （实测金融 `Status` 绑到 `fact_trades` 而非 `dim_account`），该事实如实记进
    profile 的 `bound_dataset` 与 `note`，本批不改编译器。大基数列（distinct > 200）
    跳过注册，planner 对该列不做值校验——合法值与非法值都透传，静默漏匹配风险仍在

**如果有真实企业数据，我会优先补做**：数据契约、IAM 集成、审计留痕、容灾、并发压测、模型红队测试、变更管理流程。

---

## 11. 能力来源登记（避免夸大）

| 能力 | 状态 | 本项目中的动作 |
|---|---|---|
| Multi-LoRA SQL 推理、SFT 飞轮 | 已有工程经验 | 用公开数据重建训练/评测流水线，**不声称是新发明** |
| 元数据治理、SQL/tokenizer 解析 | 已有工程经验 | 改造为 Atlas 的 DDL/ETL 注释抽取器 |
| 标签体系、Bitmap、OneID、销售/金融指标 | 已有业务经验 | 抽象为 Atlas 的指标/实体/分群/权限设计模式 |
| YAML 编排 + Airflow + GitLab CI | 已有工程经验 | 迁移为指标任务编排与 CI 契约测试 |
| GraphRAG + Milvus 混合过滤 | 已有工程经验 | 复用于指标、同义词、业务术语检索 |
| OpenTelemetry + PromptOps + LLM Judge | 已有工程经验 | 接入 SQL 调用链、成本、回归评测 |
| 声明式语义层、指标版本血缘 | **需新建** | 基于 Apache Ossie 规范实现编译器、治理扩展与测试 |
| Data Agent 状态机与工具链（LangGraph 编排 / MCP 暴露 / 防幻觉图表 / 反馈与 handoff） | 已有工程经验 | 以确定性优先落地 agent/ 状态机（8 节点）+ tools 四件套 + MCP 工具服务器 + chart + feedback；7 场景 e2e 实测（含多轮追问 S7）见 docs/e2e-acceptance.md（数字全部出自 eval/reports/e2e-acceptance.json，不另立声明） |
| Ossie / Polaris / Iceberg / Doris | **需新建** | 单机部署、基准测试、维护 ADR（0002/0004/0005） |
| Agent 安全执行与自动洞察 | **需新建** | 先做安全工具，再扩展规划与归因 |
| HTTP API / 认证中间件 | 已有工程经验 | 以 FastAPI 落地 serving/api.py（ADR-0012：/health /plan /compile /ask + JWT），/api 契约测试 43 例（+17 硬化：身份注入/会话冲突 422/限流 429/审计字段集）+ 真链验收 api-verify A1-A7 在档 |

---

## 12. 许可与声明

- 代码：`Apache-2.0`
- 数据：TPC-DI / BIRD / FIBO（MIT）/ OMG Commons（研究用途）遵循各自原始许可

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
