# Final Retro —— 8 周后：实际 vs 计划（Day 1-56）

> 撰写：2026-09-03（第 8 周末）。本文件所有数字与 README §3.3 / EVAL_REPORT.md 同源，
> 不引入新数字（AGENTS.md 9.1）。前序复盘：`docs/retro-p1.md`（Day 15-28）、
> `docs/retro-p2.md`（Day 29-42）、`docs/e2e-acceptance.md`（Day 48 验收）。

## 0. 一句话结论

8 周把 Atlas 从零做到了「**注册语义域内确定性链路全绿 + 全链路可审计**」的单机 MVP：
评测 Plan Acc 44/44、EX 44/44、零 LLM 覆盖 48/48、e2e 6/6、安全三层与 RBAC/行级实测、
FIBO 31 条概念映射可审计、可观测全链路冒烟——而最诚实的收获是：**清楚地知道哪些没做**。

## 1. 实际 vs 计划（阶段对照）

| 阶段 | 计划要点 | 实际 | 评价 |
|---|---|---|---|
| Day 1-14 基建 | 数据/存储/CI 基座 | 快照 b47a6c1 + seed 装载 17 ODS + 8 DWD、行数双验 | 如期 |
| Day 15-28 语义层 | 指标 6 起步、评测闭环 | 6→15→20 metrics 三轮扩展、gold 50、P1 链验收 | 超预期（指标扩展真实驱动检索回归，见 KL #18） |
| Day 29-42 检索+LLM 对照 | schema linking、RAG+LLM、飞轮 | 主链路 44/44；RAG+LLM 44/44 同分实证「域内 LLM 无增量」；飞轮空转实测定格 | 如期 + 诚实边界清晰（LoRA blocked 不编数） |
| Day 43-49 Agent | LangGraph 8 节点 | e2e 5 场景 + handoff 6/6、多轮会话、图表/反馈工具 | 如期 |
| Day 50-56 可观测/审计/发布 | 埋点→面板、全量审计、发布 | 冒烟全链路通（含 3 个 SDK 缺陷修复）；FIBO 审计补齐 31 条；release-notes v0.1 | 深度超预期（审计发现统计口径误判、KL #2 遗留口径） |

**计划内缩水/未做（如实登记）**：
- LoRA / LoRA+SC 两行对比：blocked（macOS arm64 无 CUDA + ml 依赖未装，`make train` exit 2 实测）
- BIRD finance 对照：判定历史对照不再新增（text2sql 生成器不可用，`eval/bird/README.md` 有登记）
- GitHub CI 真实执行 / 仓库转 public / 推文：**待用户动作**（本地无法模拟 GitHub runner；push/公开需凭证）
- 企业数据引入：全程未引入（项目红线，合规优先）

## 2. 最大坑（按烧掉的时间排序）

1. **可观测冒烟「看起来全通其实全空」**（Day 51-53 最深的坑，7 回合调试）：面板 200、
   collector 收包、prometheus 有指标——但 rate 恒 0、p95 NaN。根因链三层叠：
   ① CLI 短进程丢埋点（缺 atexit flush）；② **MeterProvider 漏传 resource**——TracerProvider
   传了而两处 MeterProvider 没传，metrics 静默用 SDK 默认资源（OTEL_SERVICE_NAME +
   随机 UUID instance），每进程独立序列恒 1；③ 面板指标名缺 exporter 规范化后缀
   （`_milliseconds`）。**教训**：埋点配置的「看起来通」不等于「口径对」；面板空图要查
   到 payload 层（collector debug exporter 抓包实证），不要在猜测层反复修。
2. **自写统计脚本先于读权威脚本**（Day 54）：FIBO 覆盖统计输出 0 与 README「22 条」矛盾，
   结论不是文档错而是我的遍历层级错（映射在 model 级而非 dataset/metric 级）。
   **教训**：仓库有 validate_alignments.py 就先用它的口径，不要凭结构假设重写统计。
3. **Ossie 版本漂移与类型体系**（Day 53）：0.2.0.dev0 DRAFT、sqlglot `Expr`/`Expression`
   基类关系导致 mypy 三连修。**教训**：先读库源码确认类型层级，再写注解。
4. **Doris FE 默认堆 8G 吃满单机**（Day 27）：全表聚合 OOM → fe.conf Xmx2g，内存
   5.5G→1.0G 实测。**教训**：单机部署容器默认值必须按机器资源显式覆盖。
5. **检索语料扩展引发静默退化**（Day 27，KL #18）：15→20 语料后单路 Recall@1
   44/44→41/44 且非显而易见——评测闭环（make eval 同批回归）把它暴露，schema linking
   重排层吸收。**教训**：语料/指标变更必须触发检索回归，不能只跑主评测。

## 3. 如果重来会怎么做

1. **可观测在 Day 1 就接入**（哪怕 no-op）：SDK 三缺陷是「短进程形态 + 双 Provider
   配置」的通用问题，早接入早发现，不必挤在发布周连续调试。
2. **评测「数字墙」前置**：gold 锚定 hash 与 snapshot sha 绑定机制（Day 30 定型）是
   全项目最值钱的机制之一——若 Day 15 就做，中途三次指标扩展的回归会更快定位。
3. **「先用仓库里权威脚本的口径」写进自查清单**：FIBO 统计误判类问题可完全避免。
4. **单机资源规划提前量**：Doris FE 堆、容器镜像体积、Milvus 内存应在选型日（ADR-0004）
   就按 16G 单机预算写下限值，而非等到 OOM。
5. **对外动作提前登记**：GitHub 公开 / CI 真实执行 / 推文都依赖账号动作——应 Day 40 前
   就与用户确认凭证与意向，而不是留到第 8 周末（本项目以本地验证等价替代，push 后补跑）。

## 4. 明确未做（诚实清单，不写进完成记录）

- [ ] LoRA 训练/推理实测出数（compare 4 行补齐）——blocked，解锁条件见 KL #19
- [ ] 训练/测试切分在真实语料上执行（飞轮空转 0 样本为设计结论，非执行）
- [ ] filter 解析 / 相对时间 / 指代消解 / 图表自动洞察（KL #11/#20/#21，Phase 2）
- [ ] 行级权限下推 Polaris / 跨表 join 注入（KL #14/#15，Phase 2）
- [ ] GitHub CI 真实运行 + 仓库 public + 对外介绍（待用户动作）
- [ ] 生产级诉求（高可用/限流/容灾/IAM/审计）——KL #3/#4 明确不在 MVP 范围

## 5. 交付门槛自检（Day 56，17 项）

| 门槛 | 状态 | 证据 |
|---|---|---|
| 五条命令全通 | ✅（seed 为历史实测 + 今日快照指纹 noop 复核一致，不重跑以免重置） | 3.3 L165-168 + eval 快照自检 |
| `make lint-ossie` | ✅ | README §4.5 + 今日 make lint |
| `make lint-governance` | ✅ | 同上（含 FIBO registry 校验） |
| `make export` dbt YAML | ✅ | exports/ + KL #26 |
| 5+ 指标 / 50 黄金集 / 报告自动生成 | ✅ 20 / 50 / EVAL_REPORT.md | README §5 |
| Guard 恶意 SQL 全拒 + CTE/子查询/视图 | ✅ | p1-chain 报告 + tests |
| 三角色行级验证 | ✅ | rls-verify 报告 + 截图 |
| 四策略对比数据齐全无手写 | ✅（lora 两行 blocked 如实） | compare-4way 报告 |
| 训练/测试切分可解释 | ✅（空转设计结论） | lora/data/README.md |
| LangGraph Agent e2e 含反问与接管 | ✅ 6/6 | e2e-acceptance.json |
| OTel trace 追 question_id→cost | ✅ | KL #23-25 边界内 |
| Iceberg time travel 复现快照 | ✅（8 张 DWD 表的 Iceberg snapshot id 逐表绑定于 meta，
可直接 time travel 复现评测数据） | data/snapshots/*.meta.json snapshot_ids |
| Polaris RBAC 验证 | ✅ | polaris-rbac 报告 |
| 5+ ADR（三篇必写） | ✅ 11 篇 | infra/adr/ |
| e2e 验收回归 5 场景 | ✅ | e2e-acceptance.md |
| Known Limitations 诚实 | ✅ 27 条 | README §10 |
| GitHub 仓库 public | ⏳ 待用户动作 | 本地已验证，push 后完成 |

## 6. 结论

Atlas 完成的是「**可信问数的参考实现**」：以 Ossie/语义层 + 确定性 Compiler + 只读
Guard + 评测回流证明了一条可审计的 NL2SQL 路线——并诚实标注了它未覆盖的边界。
项目最有说服力的产出不是任何单一数字，而是「每个数字都能被 30 秒内追到脚本与报告」
这一事实本身（附：诚实承诺见 README 末节）。
