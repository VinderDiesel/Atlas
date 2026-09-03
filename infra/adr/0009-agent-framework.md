# ADR-0009：Data Agent 编排框架采用 LangGraph 编译图（而非自研状态机/Agent 框架）

- 日期：2026-09-03
- 状态：accepted（Day 43-48 落地：agent/graph.py 8 节点条件路由）
- 相关：ADR-0010（评测方法论）、ADR-0003（只读网关）、README 第 2 节、
  `agent/state.py`、`agent/graph.py`、`docs/e2e-acceptance.md`

---

## 背景

Atlas 需要把 P2 评测链（问句 → Plan → SQL → 执行 → 归因）升级为**可多轮对话的
Data Agent**，同时守住两条产品红线：

1. **确定性优先**（AGENTS.md 决策优先级 4）：已知指标走语义编译器，LLM 只是候选
   生成器且必须过校验——因此 Agent 不能是"LLM 自由工具循环"，路由必须是显式图。
2. **诚实闭环**：歧义必须反问（不猜答）、无素材必须转人工（不编造）、每回合
   可留痕可纠错（Day 46 反馈回流）、SQL 必须过只读 Guard（N3）。

## 备选方案

| 方案 | 优势 | 劣势 |
|---|---|---|
| **LangGraph 编译图（选定）** | 节点/边显式可测；条件路由天然适配"plan 先行"；内置 checkpointer（多轮会话状态）；社区大、与 LangChain 生态可衔接 | 依赖版本演进快（本项目 1.2.11）；序列化层需自定义类型注册（踩坑见下） |
| 自研有限状态机 | 零依赖；完全可控；无序列化黑盒 | 多轮会话/重入/持久化全部自造；可观测与生态断层；把时间花在非业务复杂度上 |
| LangChain Agent（工具循环） | 上手快 | 循环不可控 → 与"确定性优先/不猜答"冲突；token 消耗不可预测；难做回合级契约测试 |
| ReAct 提示词自循环（无框架） | 实现最快 | 无法保证 clarify/blocked/handoff 等终态语义；评测不可复现（N1 红线） |

## 决策

1. 用 **LangGraph（≥0.2，本项目 1.2.11）+ MemorySaver checkpointer** 实现状态机；
2. **plan 先行**的条件路由（任务书七节点序列为 clarify → retrieve → plan →
   generate → validate → execute → explain，落地调整为 plan 最先——解析必须先于
   一切，命中即直达 execute，与 P2 确定性链路结论一致；Day 43 验收登记）；
3. 终态语义由路由保证：歧义/相对时间 → clarify 终端（反问不猜答，0 SQL）；
   unmatched 且非候选模式 → clarify（附确定性候选）；候选模式 retrieve 0 素材 →
   **handoff** 终端（转人工，不空转不编造，Day 48）；
   Guard 拒绝 → blocked 终端（只给原因，不外泄被拒 SQL）；执行故障 → error 终端；
4. 候选链 validate 失败在节点内重试 ≤1 次（共 ≤2 次 generate），仍败 → clarify，
   图无回环、失败轮不残留误导状态；
5. 每轮 ask 在 plan 节点冲刷上轮中间状态（保留 usage 累计与最近一问），
   同一 session 连续轮数由 DataAgent 维护（turns_in_session）；
6. 序列化：自定义类（Plan/TimeSpec/OrderSpec/ClarificationRequest）经
   JsonPlusSerializer 构造参数注册 allowlist——踩坑记录：`with_msgpack_allowlist()`
   在默认 permissive 模式是 no-op（直接 return self），必须用构造参数。

## 理由

1. "节点+边"即架构文档：8 节点拓扑可被测试逐点断言（tests/test_graph.py），
   契约测试锁路由口径——这是"确定性优先"的工程化表达；
2. checkpointer 免费给出多轮会话与状态隔离，避免自研持久化；
3. 回合结果（TurnResult）统一出口，使 eval/CLI/e2e/OTel 全部消费同一形态
   （Day 49 make ask、Day 50 全链路埋点均零成本接入）。

## 代价与限制

- LangGraph 版本演进快：API（如序列化/checkpoint 选项）跨版本漂移，
  升级需回归 test_graph + e2e（真实 Doris 6 场景）；
- 图内无回环 = 无"自我修复"编排：多轮修复依赖用户重问/纠错反馈，属明确边界
  （README Known Limitations 20）；
- 状态在进程内（MemorySaver）：重启丢会话，MVP 可接受，生产需换持久化
  checkpointer（接口不变）。

## 什么情况下应该推翻

- LangGraph API 破坏性变更导致升级成本 > 自研成本，且图拓扑已稳定 → 迁自研
  有限状态机（节点函数与 state.py 保持，迁移面 = 组装层）；
- 需要图内循环自愈（如"失败自动换口径重问"）且无法用节点内重试表达 → 重新
  评估 LangGraph 循环能力或调整产品边界；
- 多进程/分布式会话成为硬需求 → 换 Redis/Postgres checkpointer（API 不变）。

## 验证方式

- tests/test_graph.py：8 节点拓扑 + 路由口径契约（answer/clarify/blocked/error/
  handoff + 多轮计数 + 失败轮不泄漏）——237 个单元/契约测试全绿；
- docs/e2e-acceptance.md：真实 Doris 6/6 场景（正常/反问/拒绝/修复/图表/人工接管）；
- 推翻触发后重跑 test_graph + e2e 回归作为迁移门禁。
