# Atlas v0.1 Release Notes

> 版本：v0.1（首个可审计交付，2026-09-03 准备）
> 绑定：git `7d48dcb`；数据快照 `data/snapshots/7d48dcb.meta.json`（sha 与评测绑定）
> 数字纪律：本文件**不引入任何新数字**，全部沿 README §3.3 / EVAL_REPORT.md / eval/reports/ 既有
> 实测口径；新增声明必须来自脚本产物（AGENTS.md 9.1）。评测主报告：`eval/reports/7d48dcb.json`。

---

## 1. 这是什么

Atlas 是一个**可信 AI 问数平台**的单机 MVP：统一语义层（Apache Ossie + 自研治理扩展）→
确定性 Compiler（Plan → SQL）→ 只读 Guard（三层纵深）→ 可解释 Data Agent（LangGraph），
并带完整可观测（OTel → Prometheus/Grafana）与评测回流闭环。数据为 TPC-DI 零售证券经纪
公开基准（不对应任何真实企业），语义锚点挂载 FIBO/OMG Commons 本体概念。

**设计立场**：确定性优先——注册语义域内已知指标由编译器直接产出 SQL，LLM 只做候选
生成且必须过校验；诚实不可协商——本文件每个数字可溯源，未验证项一律如实标注。

## 2. 本次交付的能力（均实测，非设计声明）

### 2.1 语义层与治理（Apache Ossie 0.2.0.dev0 + Atlas 扩展）
- 主模型 8 datasets / 12 relationships / 20 metrics，`make lint` 五类校验全绿
  （Ossie schema + governance schema + 血缘 + 唯一性 + 黄金集引用双向一致）
- FIBO L2 概念对齐 **31 条映射**（8 datasets 全覆盖 + 19/20 metrics），概念 IRI 全部
  经 `check_iris.py` 29 条权威清单实测存在于锁定闭包（FIBO commit `119fa8c0` + Commons 20250801）
- 治理扩展齐全：owner / lineage / freshness / policy / supersedes 版本链；
  指标发布审核记录见 `semantic/migrations/2026-09-02-release-day27.md`
- `make export`：Ossie → dbt MetricFlow YAML **三态如实导出**（agg 14 / ratio 3 /
  unmapped 3 逐条登记理由，不伪造等价物），证明语义层非封闭

### 2.2 确定性问数链路（compiler-only）
- 自建黄金集 50 例（金融段 48：44 可解析 + 4 歧义反问），真实 Doris 执行：
  **Plan Acc 44/44、歧义反问 4/4、EX 44/44、0 执行错误**（`eval/reports/7d48dcb.json`，
  快照指纹漂移自检通过）
- 注册语义域内确定性链**零 LLM 覆盖 48/48**（`baseline-compiler-7d48dcb.json`）；
  域外候选由 RAG+LLM / LoRA（blocked）对照承接
- 指标检索：schema linking 三阶段链路 Recall@1 44/44（`schema-link-bm25-7d48dcb.json`）；
  双路 RRF + 元数据重排 44/44（`retrieval-rerank-7d48dcb.json`）

### 2.3 安全（只读红线，纵深防御）
- Guard 三层：SQL 形态校验（sqlglot AST，非字符串拼接）+ 语句类型白名单 +
  行级策略注入；恶意 SQL 10 条全拒实测（INSERT/UPDATE/DELETE/DROP/ALTER/GRANT/
  CREATE/sleep/pg_sleep/benchmark，`p1-chain-7d48dcb.json`）
- Polaris 对象级 RBAC 实测：analyst 对非授权表/命名空间 Forbidden
  （`polaris-rbac-7d48dcb.json`）；三角色行级下推实测（`rls-verify-7d48dcb.json` +
  `docs/screenshots/rls-verify.png`）

### 2.4 Data Agent（LangGraph 状态机，8 节点）
- 真实 Doris 端到端 5 场景 + 人工接管全过（6/6，`eval/reports/e2e-acceptance.json`、
  `docs/e2e-acceptance.md`）：确定性 Planner → Compiler → Guard → 执行 → explain →
  图表（spec 级确定性渲染）→ 反馈留痕；4 歧义问句反问澄清实测 4/4
- 多轮会话 + 进程内记忆；RAG+LLM 对照实测 44/44 与确定性链同分
  （`rag-llm-openai-7d48dcb.json`：99597 tokens / 均值 2564.3ms / 成本估算 $0.0179，
  量级差异实证「注册域 LLM 无增量」）

### 2.5 可观测与评测闭环
- OTel 回合级 `atlas.turn` span（question_id 可回放 / metric_id / SQL / rows /
  latency / snapshot sha）+ `gen_ai.token_cost`；真实回合 7 条全链路冒烟通
  （Grafana 6/6 面板表达式 success，p95 实测 242.5ms，冒烟数据非流量基线）；
  atexit flush 与 MeterProvider resource 两处 SDK 缺陷实测修复
- `make eval` / `make baseline` / `make retrieve` / `make compare` / `make report` 全链路，
  EVAL_REPORT.md 机械转述 8 类报告、每格带 source 列；13 份报告文件与 README 声明逐项可对账
- 11 篇 ADR（Ossie / Apache 全栈 / Calcite 取舍 / 评测方法论 / 安全三层…均含推翻条件）

## 3. 已知边界（如实声明，详见 README §10 共 27 条）

- **规模**：TPC-DI 基准默认规模（294 万行、17 ODS + 8 DWD、8 注册 dataset）；
  PB 级 / 千表 / 高并发均为未验证假设（KL #1/#2/#4）
- **语义层未完成项**：FIBO 映射 19/20（total_trade_tax 待扩展闭包域，KL #27）；
  5 个 Day 27 新指标无黄金用例背书（发布定义 ≠ 数值背书，KL #16）；
  户均现金余额为负是数据特性（KL #17）
- **Planner 确定性边界**：仅显式分组问句、无 filter 解析、相对时间不支持
  （KL #11）；多轮无指代消解（KL #20）
- **检索词法层限制**：Milvus 为确定性稀疏向量（无语义相似）；单路引擎在 20 语料上
  41/35/40@1 属引擎设计内，schema linking 主链路已吸收（KL #12/#18）
- **导出非无损**：3 指标（先乘后加）MetricFlow 无法表达（KL #26）；DATABRICKS
  方言未实测
- **LoRA 训练路径 blocked**：macOS arm64 无 CUDA + ml 依赖未装（KL #19，不编数字）
- **可观测边界**：CLI 短进程 counter 形态致 QPS rate 失真（KL #23）；阈值是配置
  占位非实测统计边界（KL #22）；评测批处理不经 Grafana（KL #25）

## 4. 后续路线（诚实标注状态）

| 项 | 状态 | 解锁条件 |
|---|---|---|
| 训练/测试切分与 SFT 数据飞轮实转 | 空转已实测（0 失败样本为设计结论） | 失败样本 + GPU（`make train` 一键） |
| LoRA 对照出数（compare 4 行补齐） | blocked | CUDA 环境（LLM 端点已就绪） |
| BIRD finance 对照 | 判定历史对照，不新增 | text2sql 生成器可用（`eval/bird/README.md`） |
| filter 解析 / 相对时间 / 指代消解 | 待实现（KL #11/#20） | Phase 2 |
| 行级权限下推 Polaris / 跨表 join 注入 | 未实现（KL #14/#15） | Phase 2 |
| 企业数据合规引入 | **不引入**（项目红线） | — |

## 5. 仓库结构速览

```text
semantic/        # Ossie 语义模型 + 治理扩展 + schema + export_dbt
agent/           # LangGraph 状态机（planner/compiler/security/tools/prompts）
retrieval/       # bm25 / milvus / graph_store / schema linker
serving/         # api / auth / guard / rls_verify / rbac 验证
observability/   # otel.py + grafana provisioning（6 面板 / 4 告警）
eval/            # gold 50 例 + runner + 八类报告（commit sha 绑定）
data/fibo/       # FIBO 子集锁定 + 校验脚本（ADR-0007）
infra/adr/       # 11 篇架构决策记录
docs/            # 验收记录 / 发布文案 / screenshots 素材
```

## 6. 复现（五条命令）

```bash
make install && make up        # 依赖 + 全栈容器
make seed                      # TPC-DI 装载（分钟级，会重置数据）
make lint                      # 语义层五类校验
make eval && make report       # 评测 + 自动报告（绑定 git sha）
```

发布交付门槛（17 项自检）已并入 README §3.3 勾选记录（逐日实测证据链，非新声明）；
本文件不替代 README，README 是唯一事实源。
