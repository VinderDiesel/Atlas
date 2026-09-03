# P2 复盘与验收（第 5-6 周 · Day 29-42）

> P2 = NL2SQL 基线与评测闭环：Schema Linking（Day 29）+ Compiler-only 基线（Day 30）
> + RAG+LLM 策略（Day 31-34）+ 失败样本体系（Day 35）+ 训练数据构造（Day 36）
> + LoRA 训练栈（Day 37-38）+ 自动评测报告（Day 39）+ CI 回归（Day 40）
> + 数据飞轮（Day 41）。复盘基于代码现状与绑定 sha 的报告，不写未实测结论。
> 所有数字与 `EVAL_REPORT.md` 同源（make report 机械转述），无手写。

## 1. 清单目标 vs 落地形态

| 清单目标 | 落地形态 | 偏差说明 |
|---|---|---|
| Day 29 两阶段 schema linking + 列级 rerank + JOIN 可达性 | `agent/tools/schema_linker.py` + `eval/schema_link_eval.py` | 图域粗筛（可达性预检剔跨实体错配）→ 受限候选域 BM25 → 元数据重排；指标 Recall@1 44/44（报告 `schema-link-bm25-7d48dcb.json`）；表集合无区分度（6 张 dwd 全连通）如实声明下界口径 |
| Day 30 compiler-only 基线 | `eval/baseline_compiler.py` + `docs/baseline-compiler.md` | 主评测与基线分析分离双报告；48 条金融 gold：解析命中 44/44 + 歧义反问 4/4 = 确定性链 48/48 零 LLM 全覆盖 |
| Day 31 策略 2：RAG + 通用 LLM | `agent/generator.py` + `agent/prompts/generator_plan.yaml` + `eval/rag_eval.py` | **架构对齐修正**：落地为 Plan 级候选（SQL 一律由确定性 Compiler 生成，最小攻击面），非任务书字面的"LLM 直接产 SQL"；stub 引擎链路自检 44/44 同基线口径；openai 实测 blocked（无 LLM 端点） |
| Day 32 公开集对照 | `eval/spider/README.md`、`eval/bird/README.md` | **消解**：Spider 判定为历史对照不再新增（与金融场景不匹配）；BIRD finance 对照待 text2sql 生成器可用后接入，阻塞与恢复命令已登记 |
| Day 33 自洽投票 + 执行校验 | `agent/tools/self_consistency.py` + `execution_validator.py` | 多候选 hash 聚类取众数（平局取候选序）+ top1 失败回退；校验器只做形态检查（空结果/全 NULL/非分组多行）不做数值语义判断；LLM 多采样版 blocked 待端点；gold 域 0 失败 → 回退链 0 触发为设计结论 |
| Day 34 四策略对比 | `eval/compare_4way.py`（Makefile `make compare`） | 六维表（EX/Plan Acc/Token/Latency/Cost/拒绝率）落地；compiler-only 全实测 + rag-llm(stub) 链路对照；LoRA/LoRA+SC 两行 blocked 如实登记（无 GPU） |
| Day 35 失败样本分析 | `eval/failures/categories.json`（7 类）+ `failure_collect.py` + README | 两份报告程序化扫描 0 失败——空集是脚本产物；样本预期在 LLM 实测后产生 |
| Day 36 训练数据构造 | `lora/build_pairs.py` + `lora/data/README.md` | **架构对齐修正**：pair 形态 = 合法 Plan JSON（mode=plan，非任务书字面"question → SQL"），校验与推理入口共用 `validate_plan_json`；三道红线（gold 逐字拒绝/模板级检测/去重质量过滤）；空语料为设计结论 |
| Day 37-38 LoRA 训练环境 | ADR-0008 + `lora/train.py` + `lora/configs/sql_v1.yaml` | 前置检查 exit 2 模式（语料/依赖/CUDA 任一不满足即 blocked，实测登记）；权重不入库（gitignore）；训练路径未实测——ADR 验证方式 4 条全 [ ] 不勾选 |
| Day 39 评测闭环 | `eval/report.py`（make report） | EVAL_REPORT.md 八节机械转述、0 个「待填写」、每格数字带 source 列；对比表 LoRA blocked 行如实保留 |
| Day 40 CI 回归评测 | `.github/workflows/eval.yml` | plan-regression（公共 runner：lint + 契约 + `--dry` Plan Acc 自洽断言）+ eval-data（手动：完整 EX）；EX 不可 CI 化的数据边界与 prompts 回归边界如实登记；真实执行待 push |
| Day 41 数据飞轮 | `lora/flywheel.py` + `lora/data/flywheel-state.json` | 五阶段状态机（scan→review→export→build→train），export 双闸（approved + validate_plan_json）；空转实测（0 失败样本下各阶段如实输出，train exit 2）；截图项 blocked 登记 |

## 2. 验收记录（Day 42）

### 2.1 Eval dashboard

- 形态 = `EVAL_REPORT.md`（`make report` 自动生成，绑定 sha 7d48dcb + 快照
  `data/snapshots/7d48dcb.meta.json`）：§1 主评测 / §2 确定性链覆盖 / §3 四策略对比
  / §4 RAG+LLM 链路 / §5 检索与 schema linking / §6 安全与权限 / §7 失败样本归集
  / §8 来源清单。规则：无手写数字，每格数字带 source 列可机械核对，缺失显示占位
  不推断（AGENTS.md 9.3）。
- 实测核对（测试 `tests/test_report.py` 6 例锁定）：报告头部绑定 sha、全文 0 个
  「待填写」、source 文件逐行可核对、历史 sha 报告不混入、blocked 行保留。

### 2.2 拒绝案例（结构化，转述 `p1-chain-7d48dcb.json`）

Guard 只读网关恶意 SQL 10/10 全拒（gate `malicious_10_blocked=True`），逐条
kind → blocked 记录：

| # | kind | 示例（转述报告字段） | 拦截 |
|---|---|---|---|
| 1 | insert | `INSERT INTO atlas.dwd.fact_trades ...` | blocked |
| 2 | update | `UPDATE atlas.dwd.fact_trades SET Commission = 0` | blocked |
| 3 | delete | `DELETE FROM atlas.dwd.fact_trades WHERE Commission > 0` | blocked |
| 4 | drop | `DROP TABLE atlas.dwd.fact_trades` | blocked |
| 5 | alter | `ALTER TABLE atlas.dwd.fact_trades ADD COLUMN hacked INT` | blocked |
| 6 | grant | `GRANT SELECT ON atlas.dwd.fact_trades TO public` | blocked |
| 7 | create | `CREATE TABLE atlas.dwd.evil AS SELECT 1 AS one` | blocked |
| 8 | sleep | `SELECT sleep(10)` | blocked |
| 9 | pg_sleep | `SELECT pg_sleep(...)` | blocked |
| 10 | benchmark | 类时间耗尽函数 | blocked |

- P2 无旁路（Day 34 代码路径验证）：compiler-only / rag-llm / 候选回退的全部
  SQL 一律经 `compiler.compile` → 同一 Guard `enforce`，无第二通道。
- 权限拒绝层（同 sha 报告）：行级策略三角色结果差异（`rls-verify-7d48dcb.json`）、
  Polaris RBAC atlas_analyst 验证（`polaris-rbac-7d48dcb.json`）。

### 2.3 反问案例（结构化，转述 `7d48dcb.json`）

4 条歧义 gold 全部反问 pass（clarify 4/4，不猜答）：

| id | 问句 | ambiguous | clarify_ok |
|---|---|---|---|
| gold-104 | 最近交易情况怎么样？ | true | true |
| gold-121 | 上个月总成交量是多少？ | true | true |
| gold-122 | 2013 年成交量和交易额分别是多少？ | true | true |
| gold-148 | 2014 年成交笔数和成交证券数分别是多少？ | true | true |

- 评测口径：歧义样本给出 Plan/答案 = 猜答判失败；不给答案（反问 pass）= 正确。
- 边界：当前报告仅记录"未给答案"（answer=null）作为反问判据，多轮澄清文案
  是 Day 44 clarify 节点（LangGraph 多轮）的交付内容，不在 P2 评测口径内。

### 2.4 截图项

- P2 阶段未新增浏览器形态 dashboard：EVAL_REPORT.md 为 markdown 机械产物；
  将 markdown 渲染为 HTML 快照需新增 md→html 工具（新工具应走 ADR + 契约测试），
  超出本阶段范围。P1 截图（p1-chain/rls-verify/metrics-verify/polaris-rbac 的
  html+png）继续有效但属 P1 验收证据，不重复引用。
- **截图 blocked 如实登记**：`docs/screenshots/` 无 P2 新增截图；dashboard 形态
  化留待 Day 48 e2e / Day 50-56（OTel + Grafana 发布阶段）统一产出。

## 3. 实测数字（与 EVAL_REPORT.md 同源，全部绑定报告）

- 主评测（`7d48dcb.json`）：finance_total 48、retail_skipped 2（不混报）、
  plan_acc 44/44、clarify 4/4、ex 44/44、ex_anchored 0、exec_errors 0。
- 确定性链覆盖（`baseline-compiler-7d48dcb.json`）：48/48（44 解析命中 + 4 歧义
  反问），sample_classes 与主评测交叉一致。
- 四策略对比（`compare-4way-7d48dcb.json` + 各源报告）：compiler-only EX 44/44、
  Plan Acc 44/44、token 0、latency 202.6ms（单次现场测量）、cost 0、拒绝 0/44；
  rag-llm(stub) 44/44 同口径（stub 不代表 LLM 能力）；lora / lora-sc 两行 blocked。
- Schema linking（`schema-link-bm25-7d48dcb.json`）：指标 Recall@1 44/44、@5 44/44、
  0 fail_cases；对照 BM25 全量域单路 41/44（KL#18 修复后主链路无退化）。
- 归集（`eval/failures/` 程序化扫描）：compiler-only + rag-llm-stub 两份报告
  0 失败 → 飞轮空转（`lora/data/flywheel-state.json`：scan {} → export 0 → build
  exit 0 → train exit 2）。

## 4. 主要偏差与坑

1. **两处任务书措辞与落地架构冲突，均以落地架构为准并登记**：Day 31「LLM 产
   SQL」→ Plan 级候选（SQL 由确定性 Compiler 生成，最小攻击面，Guard 只兜底
   Compiler 产物）；Day 36「question → SQL」→ question → 合法 Plan JSON
   （ADR-0008）。两次修正的共同根因：任务书沿用早期「NL2SQL」想象，而项目
   架构演进后 **Plan 是 Generator 与训练语料的唯一中间形态**。教训：架构决策
   先行（ADR 固化）再写训练/生成任务书。
2. **双阻塞（无 LLM 端点 + 无 GPU）下的诚实推进模式**：工程就绪 + 前置检查脚本
   断言 + blocked 登记 + 恢复命令（`make rag-eval ENGINE=openai` / `make train`
   一键就绪）——四策略两行与训练实测全部以此模式处理，不编造数字不假装跑通。
3. **CI 现实约束**：公共 runner 无 TPC-DI 数据与 Doris → EX 不可 CI 化（eval-data
   手动 job + 前置注释）；prompts 只被 LLM 引擎消费 → dry 回归对 prompts 变更不
   敏感（如实登记，敏感对象 = semantic/** 与确定性链）。门槛用自洽断言而非硬编码
   基线（gold 演进合法改变分母）。
4. **测试驱动暴露归因语义歧义**：flywheel 契约测试首版样本形态错误（plan_ok=false
   + CompileError 实际归 understanding，因 classify 中 plan_ok=false 优先）；
   修正样本为 plan_ok=true + CompileError 并与 `failures/categories.json` 定义
   对齐（generation = Plan 正确但编译出错）。测试首跑失败 → 清理残留 + 修正断言，
   6 例全绿。
5. **本地 .venv 全量安装掩盖 CI 依赖缺口**：Day 39/40 引入的顶层 import
   （mysql-connector-python / python-dotenv）在 lint.yml 最小依赖清单缺失，
   CI 上 make test 必失败——本地等价验证无法替代 CI 真实执行（Day 40 登记
   「实测待推送」）。

## 5. 如果重来

- **任务书措辞紧跟架构**：Day 31/36 的「SQL 生成」措辞应随 ADR-0002/0008 同步
  改写为 Plan 形态，避免执行期反复消解（两次修正均消耗一轮回归与文档同步）。
- **LLM 端点与 GPU 侦察前置到批次规划**：Day 31 发现无端点、Day 37 发现无 GPU
  都是执行日才确认——批次开始前先做环境侦察（.env 配置 + 硬件探测写进前置检查），
  阻塞类任务整体后移或预先设计 stub/空转路径。
- **CI 依赖清单以「顶层 import 扫描」为准**：lint.yml 最小清单应能从
  `ruff import 扫描` 自动生成或人工复核，而非等 CI 失败暴露。

## 6. 明确未做（诚实登记，不写进完成清单）

- RAG+LLM 真实引擎（openai）实测：无 LLM 端点（blocked，恢复 = .env 端点后
  `make rag-eval ENGINE=openai`）
- LoRA / LoRA+SC 训练与实测：无 GPU（blocked，恢复 = CUDA 机器 `make train`
  → vLLM serve → `make compare RAG_ENGINE=openai`）；ADR-0008 验证方式 4 条全 [ ]
- BIRD finance 对照实测：待 text2sql 生成器可用（`eval/bird/README.md`）
- 完整飞轮轮转与截图：0 失败样本 + 无 GPU → 空转 + blocked 登记（Day 41）
- EVAL_REPORT dashboard 的 HTML/截图形态：markdown 产物为准，可视化留 Day 50-56
- CI workflow 真实执行：待 push 后 action 日志（本地无法模拟 GitHub runner）
- OTel 全链路 latency p95：Day 34 延迟为单次现场测量（202.6ms），稳定口径待 Day 50

## 7. P2 阶段门槛

- [x] **四策略对比表结构齐全、compiler-only / rag-llm(stub) 实测**
- [ ] **四策略数据齐全（完整）**——LoRA / LoRA+SC / 真实 LLM 三行 blocked：
  无 GPU + 无 LLM 端点（AGENTS.md N1：不编数字；解锁条件与恢复命令见 §6）
- [x] **EVAL_REPORT.md 自动生成，无手写数字**（Day 39 验收：0 个「待填写」、
  全部数字带 source 列、契约测试 6 例锁定）
- [x] **CI 回归评测生效（本地等价验证通过）**——workflow 已部署、dry 门槛断言
  自洽通过（44/44 + 4/4 + 0 errors）、YAML 语法校验通过；真实 runner 执行待
  push（无法本地模拟，不编造绿色状态）
- [x] **训练/测试集无泄漏（切分方式可复述）**——当前训练集为空（合规来源 =
  人工确认失败样本，gold 与 ETL SQL 不可作训练源，`lora/data/README.md`）；
  pair 构造含三道红线：gold 问句逐字拒绝 / 同义词与数字归一后的模板级检测 /
  去重与质量过滤（`lora/build_pairs.py`，13 例契约测试锁定）——无泄漏面 +
  机制双保险

## 8. P2 结论

P2 的主结论来自 Day 30/39/40 三项实测与登记：**注册语义域内（48 条金融 gold 覆盖
口径）确定性链 48/48 零 LLM 全覆盖**（EVAL_REPORT §2 转述结论，不推断域外泛化）；
评测闭环（make report 无手写数字）+ CI 回归（dry 门槛）+ 失败驱动飞轮（五阶段
状态机）三件机制全部工程就绪。LLM/LoRA 两条策略路径受端点与 GPU 阻塞，已按
「工程就绪 + 前置检查断言 + 恢复命令」模式登记——P2 的可交付结论：**确定性优先
架构在注册域内不需要生成式猜测；LLM 的价值域（域外新措辞/新指标）与其评测闭环
已就位，等待端点解锁实测**。该结论是 P3（LangGraph Agent）把 clarify/回退/解释
编排为多轮状态机的前置基础。

---

## 9. 解锁补测记录（2026-09-03：LLM 端点就绪后）

> 本复盘写于 2026-09-03（P2 收口时点，上表 §1-8 保持当时状态）；当日用户配置
> OPENAI_API_KEY/OPENAI_BASE_URL/OPENAI_MODEL_NAME 后，RAG+LLM 实测解锁，本节省要记录。

- **实测结果**：`make rag-eval ENGINE=openai`（deepseek-v4-flash）全量 = Plan Acc
  44/44 + 歧义反问 4/4 + EX 44/44 + 0 拒绝 + 0 错误，与 compiler-only 基线持平
  （48/48 无回归）；99597 tokens、平均延迟 2564.3ms/条、成本估算 $0.0179（单价
  假设非账单）——报告 `eval/reports/rag-llm-openai-7d48dcb.json`。
- **四策略表刷新**（`compare-4way-7d48dcb.json`）：compiler-only 44/44（latency
  178.6ms 现场重测）+ rag-llm(openai) 44/44（99597 tokens / 2564.3ms / $0.017857）
  双行实测；stub 行退出对比表；lora / lora-sc 仍 blocked（仅无 GPU）。**同分不同
  代价**——注册域内 LLM 无增量（0 token/178.6ms vs 99597 tokens/2564.3ms），
  确定性优先的量化论据落地；LLM 域外价值待扩展评测。
- **实测驱动的两处工程修复（非模型语义失败）**：① generator max_tokens 300→800
  （首轮 6/7 拒答 = 输出截断 completion 300/300）；② `validate_plan_json` 维度顺序
  注册序规范化（gold-146：LLM dims=[Branch,Tier] vs 标注 [Tier,Branch] 同集合不同
  序 → Plan Acc fail + EX hash 不一致；规范化后与 Planner/标注同口径）。附带修复
  模型名配置链（OPENAI_MODEL_NAME 原本无代码消费）。
- **失败样本复核**：`rag-llm-openai` 报告程序化扫描 0 失败 → 飞轮维持空转（实证
  状态）；失败样本的价值以「驱动评测口径修复」形式兑现。
- **门槛状态更新（§7 复核）**：「四策略数据齐全」由 [~] 升至部分解锁：RAG+LLM 真实
  引擎行已实测，LoRA/LoRA+SC 两行仍 blocked（无 GPU——剩余唯一环境阻塞）。
