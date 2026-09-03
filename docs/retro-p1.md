# P1 复盘（第 3-4 周 · Day 15-28）

> P1 = 确定性问数链路 MVP：Planner/Compiler/Guard/权限（第 3 周）+ 检索/RLS/元数据
> 抽取/端到端验收（第 4 周）。复盘基于代码现状与绑定 sha 的报告，不写未实测结论。

## 1. 清单目标 vs 落地形态

| 清单目标 | 落地形态 | 偏差说明 |
|---|---|---|
| Day 15 `agent/state.py` Plan 结构 | Plan dataclass 在 `agent/compiler.py` | state.py 延后至 LangGraph（Day 43）；Plan/TimeSpec/Filter/OrderSpec 以 dataclass 落地，字段即清单约定 |
| Day 16 同义词独立表 ≥30 | `semantic/synonyms/business_terms.yml` + 指标/字段级 `ai_context.synonyms` | 双轨：业务术语表 + Ossie 内联同义词；冲突由 Planner 多命中 → 澄清（不猜） |
| Day 17 `time_parser.py` 20 种时间表达 | 解析器内置于 `planner._parse_time` | 绝对时间 5 类（date/quarter/month/year/ISO）；相对时间明确返回澄清——固定快照评测下相对时间会漂移，拒绝是口径不是偷懒 |
| Day 18 同比/环比编译 | **未实现** | gold 无对比类用例；不假装已做，后移 Phase 2（见 §5） |
| Day 19 Guard + 恶意 10 条进测试 | `agent/security/sql_guard.py` + tests 17 例 + p1-verify 复验 10 条 | 校验链顺序与 ADR-0003 一致；10 条恶意 SQL 在 Day 28 验收中逐条复验 |
| Day 20 成本估算独立 `budget.py` | Budget/estimate_cost 并入 sql_guard | 单文件闭环避免跨模块预算口径分裂；超预算抛 BudgetExceeded（拒绝而非降级） |
| Day 21 独立 `lineage.py` | ATLAS custom_extensions `lineage` 段，governance_validate 强制 | source_columns/dependencies 随指标发布同文件演进，lint 校验引用双向一致 |
| Day 22-28 检索/RLS/抽取/发布/验收 | 全部落地（报告见 §2） | 主要偏差见 §3 |

## 2. 实测数字（全部绑定报告，无手写）

- 检索（44 条 gold 问句）：BM25/RRF 融合/词典序 Rerank Recall@1 44/44、@5 44/44；
  Milvus 稀疏向量 42/44@1（报告 `retrieval-{bm25,fuse,rerank,milvus}-7d48dcb.json`）。
- RLS：三角色（hq_admin/branch_manager/compliance_auditor）同一问句结果差异集 3，
  谓词注入发生在 SQL 层（`rls-verify-7d48dcb.json`）。
- 元数据抽取：25 个金融 SQL 脚本 → dataset 候选 25 / measure 候选 42 / metric 候选 1
  （`metadata-extract-7d48dcb.json`）。
- Day 27 发布 5 指标 → 20 metrics；5 指标 Doris 实测值
  （`metrics-verify-7d48dcb.json`，其中 average_cash_balance 为负值——数据特性，KL#17）。
- Day 28 P1 端到端：5 道 gates 全过，hq_admin 结果 sha256 = gold-102 锚定 hash
  （`p1-chain-7d48dcb.json`）。

## 3. 主要偏差与坑

1. **零售 → 金融域切换（ADR-0006）的沉没成本**：早期 gmv/aov 零售语料规划在切换后
   成历史，评测主体重建为金融用例（gold-101~148）；机制复用但语料重写，
   检索/评测/语义层全部换血。这是方向调整的代价，ADR-0007 FIBO 对齐后才算走稳。
2. **Doris FE JVM 8G 堆占满 <8G Docker VM**：BE 全表聚合 MEM_LIMIT_EXCEEDED。
   修复：挂载自定义 fe.conf（Xmx2g/Xms1g），FE 内存 5.5G→1.0G（docker stats 实测）。
   教训：单机容器资源约束要先于功能验证排查，官方镜像默认值不适合个人预算环境。
3. **Milvus @1 复现漂移**：官方入口复现 35/44 稳定，自写复现脚本曾 38/44——以官方
   入口为准并记录复现方法（报告 note），避免"挑数字"。
4. **检索语料 15→20 单路退化**（BM25 44→41、Milvus 42→35、fuse 44→40）：归因
   average_holding_value 抢「市值合计」问句 top-1；rerank 主链路 44/44 无损。
   不掩盖不 hack，KL#18 登记，解决域归 Day 29 schema linking。
5. **负值指标口径核查**：average_cash_balance 实测 -32465124.70 → 核查 60.6% 行为负
   （TPC-DI 变造特性）→ 口径正确但解读受限，如实登记 KL#17，未改口径凑正值。

## 4. 如果重来

- **先定业务域再建语料**：金融 FIBO 域应在一开始就锁定（ADR-0006/0007 前移），
  可省零售语料整轮重写；机制代码（评测/抽取/编译）不受影响，这正是分层的好处。
- **资源约束前置**：单机 Docker VM 内存规划（FE/BE/等）先于查询链路开发，
  fe.conf 一上来就 2G，不踩 OOM。
- **检索退化要早测**：语料动态生成（metric_docs）后应立即全量回归 @1——本应
  Day 27 发布时同轮验证，实际是 Day 27 收尾发现并归因。流程上"发布 = 检索回归"
  已写进 Day 29 前置条件。

## 5. 明确未做（诚实登记，不写进完成清单）

- 同比/环比时间对比编译（gold 无用例，Phase 2）
- `agent/state.py` LangGraph 状态（Day 43）
- Milvus 真向量 HNSW 检索（当前为稀疏向量链路；Doris 原生向量为可选对比，未做）
- 指标运行时版本切换（supersedes 链校验已落地；"换口径"运行时语义属 Phase 2）
- 跨表 join 的行级策略注入（谓词引查询外表直接拒绝，KL#14，Phase 2）
- OTel 全链路 trace（Day 28 trace 为 MVP JSON 事件，Day 50 接 OTel）

## 6. P1 结论

P1 阶段门槛 4 项全部通过（详见 `docs/p1-acceptance.md` §6）。确定性链路
（Planner → Compiler → Guard → Doris）在 gold-102 上端到端 EX 匹配锚定 hash，
行级权限与恶意拦截均有可重复验证工具（`make p1-verify` / `make rls-verify`）。
P1 的可交付结论：**已知指标走确定性链路不需要 LLM**——这条基线正是 Day 30
compiler-only 实验的起点。
