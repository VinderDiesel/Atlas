# ADR-0012：HTTP 服务面（FastAPI + uvicorn，v1 = /health /plan /compile /ask + JWT）

- 日期：2026-09-03
- 状态：accepted（决策经用户确认；落地：`agent/factory.py` + `serving/api.py` +
  Makefile serve/token，随本批次 commit）
- 相关：ADR-0003（只读 SQL 网关）、ADR-0009（Agent 框架选型）、ADR-0011
  （安全分层——gateway 认证属后续硬化项）、README §4 目录树

---

## 背景

README 目录树 serving 注释 `api / auth / gateway` 与实际内容错位：serving/ 下
`auth.py` 是"真"（sign_token/verify_token + 三角色策略），`rls_verify.py` 等其余
是验证工具。对外无服务面：Atlas 本体是 CLI（`agent/cli.py` plan/compile/ask），
docker-compose 全是第三方中间件——"如何部署对外提供服务"没有答案。

服务化可用资产（无需新造）：
1. `agent/graph.py` DataAgent 全注入式（executor/budget/snapshot_meta…），
   `ask()` 返回 TurnResult（kind 全套字段）——CLI 语义可原样 HTTP 化；
2. `serving/auth.py` 已实现 HS256 JWT sign/verify + ROLE_DIRECTORY，认证不新开面；
3. pyproject 已依赖 pydantic>=2.8，模型校验与文档有现成底座。

范围经用户确认：能力端点（/plan /compile）与 ask 会话**一起**纳入 v1。

## 备选方案

| 方案 | 优势 | 劣势 |
|---|---|---|
| **FastAPI + uvicorn（选定）** | 原生 OpenAPI/文档与 pydantic 校验（依赖已存在）；MIT 许可兼容 Apache-2.0 项目 | 新增 fastapi/uvicorn 两个运行时依赖 |
| Flask | 生态成熟、体积小 | 校验/文档需手写或另引 marshmallow 等，回到"自己造 gateway 细节" |
| stdlib http.server | 零依赖 | 无校验/无文档/无并发模型，生产性弱 |
| ask 延后（v1 只上 plan/compile） | 首版更小 | 会话本就是 CLI 现有能力（进程内 session_id），包装成本低；用户已确认一起上 |

## 决策

1. **框架**：FastAPI + uvicorn。单进程 **uvicorn workers=1**——checkpointer
   （MemorySaver）与 `DataAgent._session_turns` 是进程内状态，多 worker = 会话
   分裂，与 CLI 会话语义不一致（README/KL 明示）。
2. **v1 端点**（确定性默认：engine=stub，LLM 引擎服务化属 Phase 2）：
   - `GET /health`（公开）：`{status, head_sha, snapshot_sha|None}`；
   - `POST /plan`（Bearer）：`{question}` → `{kind: "plan"|"clarify",
     plan|clarification}`——CLI 歧义 exit 1 语义 HTTP 化为 200 + kind=clarify；
   - `POST /compile`（Bearer）：`{plan}`（pydantic 模型镜像
     agent.compiler.Plan/TimeSpec/Filter/OrderSpec）→ `{sql}`；结构非法 422；
   - `POST /ask`（Bearer）：`{question, session_id?}` → TurnResult JSON（kind 全套
     字段）；快照 meta 缺失 → 503。
3. **认证**：`Authorization: Bearer` + `serving/auth.verify_token`
   （`_jwt_secret()` 已读 env，无新配置面）；无 token/坏 token/伪造签名 → 401。
4. **序列化**：rows 值 Decimal → str（保精度）、datetime → ISO8601、Enum → value；
   JSON 可表达边界与 mcp_server 先例（default=str 序列化）一致，确定性文本不进
   浮点转换。
5. **问句长度上限**（500 字符）在 pydantic 层粗限——HTTP 面拦截，Guard 细限不变。

## 理由

1. **CLI 与 HTTP 共源**：`agent/factory.create_live_agent()` 自 cli._live_agent
   提取（快照 meta 校验 → 抛可捕获异常供 HTTP 层转 503，SystemExit 只留 CLI）；
   同一确定性链路两种出口，不复制逻辑。
2. 会话语义与 CLI 一致（同进程内存会话，session_id 键），不是新会话模型。
3. 认证复用 auth.py：0011 记录的 gateway 硬化项（限流/审计/身份下推）未来直接挂
   在 HTTP 面，不另起炉灶。

## 代价与限制

- 单进程内存会话：重启即失、无横向扩展 → v1 不承诺生产部署（README KL #28）；
- 未做限流/审计/生产部署验证（本地演示面，与 make ask 同约束）；
- `SemanticModel()` 构造加载语义层 YAML 依赖 cwd → API 进程工作目录必须是仓库根
  （Docker WORKDIR=/app；本地 make serve 在根目录）。

## 什么情况下应该推翻

- 出现多 worker/多实例需求 → langgraph Postgres checkpointer 替换 MemorySaver，
  会话跨进程持久；
- 出现真实多租户/外部访问 → gateway 认证先行（0011 硬化项）：限流、审计、
  Doris identity mode 下推真实用户；
- LLM 引擎需要服务化 → engine 参数开放（v2，本 ADR 不设计）。

## 验证方式

- `tests/test_api.py` 契约测试（fake agent_factory 注入，仿 tests/test_graph.py
  FakeExecutor，无 DB，约 14 例）：health 字段 / plan 两种 kind / compile 422 /
  ask rows 序列化 / 401 三种形态 / 503 meta 缺失 / session_id 自动生成；
- `eval/api_acceptance.py` 真实链路（真 Doris + 锁定快照）：gold-101 问句
  /plan → /compile（断言只读 SQL 形态）→ /ask（EX 与快照一致）；一例歧义
  → kind=clarify；输出 eval/reports/api-acceptance-<sha>.json；`make api-verify`；
- `make lint && make test` 全绿。

## 落地注记：服务面硬化批次（2026-09-05，与 ADR-0011 落地注记同批，不改正文）

正文限制「未做限流/审计」与推翻条件中的硬化项已**部分兑现**（commit 4a547e7
feat(serving) + f98470f test）：/ask 身份下推（已验证 claims → 行级策略随 Guard
注入，explanation 可见信号）、会话 × 身份指纹（同会话换身份 → 422）、per-token
共享桶限流（429 + Retry-After）、业务审计 JSONL（每请求一行，不含 SQL）——绑定
api-verify 报告 eval/reports/api-acceptance-4a547e7.json（A5 三角色差异 / A6 会话
身份冲突 / A7 零售品类受限 + 跨域 Guard 拒绝）；契约测试现 43 例（test_api.py 26
+ test_api_hardening.py 17）。仍未兑现：生产部署验证、Doris identity mode 下推
真实用户（ADR-0011 决策 4 独立项）；「真实多租户/外部访问」推翻条件未触发。
