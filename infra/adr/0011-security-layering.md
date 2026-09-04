# ADR-0011：安全边界分层（只读 Guard + 行级谓词下推 + Catalog RBAC 的纵深顺序）

- 日期：2026-09-03
- 状态：accepted（落地：agent/security/sql_guard.py、semantic/policies/row_policy.yml、
  serving/rls_verify.py、Makefile rls-verify / rbac-verify）
- 相关：ADR-0003（只读 SQL 网关）、ADR-0004（Polaris 引入）、README 第 6 节、
  AGENTS.md 第 1 节 N3

---

## 背景

LLM 生成的 SQL 进库前需要**不可绕过的多层防线**，且每层职责必须单一：
"谁能查什么行"（行级）与"谁能访问什么表"（对象级）是两类问题，不该混在一层。
Polaris RBAC 是目标态（catalog 层强制，0004），但 Polaris 属孵化器项目、Doris
per-user identity mode 联调成本未实测——需要明确 MVP 的安全交付顺序，并诚实记录
"哪层已验证、哪层是配置态"。

## 备选方案

| 方案 | 优势 | 劣势 |
|---|---|---|
| **三层纵深（选定）**：① AST 只读 Guard（语法层）→ ② 行级谓词注入（SQL 层）→ ③ Catalog RBAC（对象层，渐进启用） | 每层职责单一；任一层被绕过仍有下层兜底；可分批验证 | 层间谓词/别名对齐需要测试锁定（如 dim_broker.branch 注入到编译 SQL 的别名） |
| 只等 Polaris RBAC 全就绪再交付权限 | 概念最简 | 孵化器不确定性阻塞 MVP（0004 已记录降级路径）；行级权限本就必须在 SQL 层做 |
| 应用层结果过滤（查全量后按角色裁） | 实现最快 | 数据已出库才过滤 = 假权限（0003 已否定） |
| 全部权限写死在编译器 | 与指标绑定紧 | 问句/工具/新模型都要改编译器，扩展性差 |

## 决策

1. **纵深顺序不可调换**：
   Guard（AST 白名单 + LIMIT/时间约束 + 成本预算）→ 行级谓词（RowPolicy 编译为
   SQL 谓词，与编译 SQL 的别名对齐后注入）→ Catalog 对象级 RBAC（Polaris，
   `make rbac-verify` 验证，失败不阻断数据链路但阻断"声明已支持对象级权限"）；
2. **行级策略是语义层一等对象**：semantic/policies/row_policy.yml，默认
   deny（default_deny: true），角色条件含占位符（{{ user.region }} 等）由请求
   上下文渲染——占位符未渲染即拒绝（Guard 层强制，防绕过）；
3. **三角色验证成门禁**：hq_admin（全量）/ region_manager|branch_manager（本域）/
   broker（本人 brokerid）/ compliance_auditor（tier 降级）经
   `make rls-verify` 在 SQL 谓词层回归（gold-146 问句 × 角色）；
4. **对象级 RBAC 已实测（2026-09-03）**：`make rbac-verify` 真实 Polaris 验证
   （报告 eval/reports/polaris-rbac-7d48dcb.json）：root 全表可读；analyst 仅获
   dwd.dim_broker/dim_customer 的 TABLE_LIST/READ 类 grant——list_namespaces 与
   fact_trades load 均被 catalog 层 ForbiddenError 拒绝。运行时强制**在档**；
   生产化的 per-user 透传（Doris identity mode 下推真实用户）仍待硬化。
5. 被拒路径不外泄细节：blocked 只回原因类型，不回 SQL/谓词（Day 43+ 锁定，
   e2e S3 验证）。

## 理由

1. 行级权限**必须**在 SQL 谓词层做（0003 结论），与对象级 RBAC 正交——Polaris
   到位后谓词注入仍保留，形成"行级 SQL 层 + 对象级 catalog 层"的标准湖仓纵深；
2. 策略放语义层而不是代码里：改权限走 YAML + lint（make lint-governance），
   与指标同等的治理流程；
3. 逐层验证使"安全声明"有脚本支撑：rls-verify 断言谓词真实出现在出口 SQL 并
   按角色返回不同行数——不是"代码里有策略"这种无法证伪的说法。

## 代价与限制

- 谓词别名对齐脆弱：编译 SQL 的 join 别名变化会破坏注入点 → tests 锁定当前形态，
  编译器变更需回归 rls-verify；
- Polaris RBAC 已验证为**对象级**（namespace/table 的 load 拒绝）；行级仍需 SQL
  谓词层（本 ADR 第 1 层），两者组合的端到端（analyst 身份经 Doris 下推）尚未
  联调——README/KL 按此口径声明；
- 占位符渲染上下文来自请求方：gateway 层必须保证 user.* 不可被客户端伪造
  （serving/auth 属后续硬化项，MVP 单用户/本地演示）。

## 什么情况下应该推翻

- rls-verify 的谓词断言与编译器别名解耦失败（维护成本持续高）→ 把注入点移到
  编译产物层（Policy 作为 Plan 字段进入编译），重写注入逻辑；
- Polaris RBAC 运行时验证完成且成本可承受 → 把第 4 条升级为“默认强制”
  （对象级入口强制 + Guard 表白名单降级为兜底）——注意：行级谓词不因此撤销；
- 出现真实多租户需求 → gateway 认证先行（当前 MVP 不处理），再谈行级上下文。

## 验证方式

- make rls-verify 全绿：三角色出口 SQL 均含对应谓词且结果行数符合角色边界；
- make rbac-verify 全绿且报告在档（2026-09-03 实测：analyst 对 fact_trades/
  list_namespaces 的拒绝为 catalog 层 ForbiddenError）；README/KL 按“对象级已
  验证、行级谓词层 + 对象级组合的端到端待硬化”口径声明；
- 恶意/绕过 SQL 用例（含视图、CTE、无 LIMIT）全拦截（tests/test_security* 与
  eval/e2e S3 blocked 场景）。

---

## 落地注记：服务面硬化批次（2026-09-05，沿 0005 增补口径先例，不改裁定正文）

### 决策 2 的 gateway 硬化项（正文代价段「serving/auth 属后续硬化项」）已兑现

- **身份注入下沉 DataAgent 主链**（commit b933e20，feat(agent)）：serving/auth.py
  拆出 `resolve_claims(claims)` 纯函数（resolve_policy(token) 薄包装保留原签名
  零破坏）；`DataAgent.ask(identity=claims)` 把已验证 claims 渲染为 Policy 随
  Guard 主链注入（agent/graph.py node_execute，无身份路径零变化契约锁定）；
  answer 的 explanation 追加「行级策略已生效（角色 X，策略 Y）」——只给角色与
  策略名，不给条件值（与被拒路径不外泄细节同精神）。
- **HTTP 面身份下推 + 会话指纹 + 审计 + 限流**（commit 4a547e7，feat(serving)）：
  /ask 以 BearerClaims 为身份（/plan /compile 无执行面不注入）；session_id 绑定
  首个 claims 指纹，同会话换身份 → 422；业务审计 JSONL 每请求一行（无 SQL）；
  per-token 共享桶限流 429 + Retry-After。
- **验证门证据**（commit f98470f，test）：契约测试 +17（tests/test_api_hardening.py：
  身份路由谓词/422 冲突/429+Retry-After/审计字段集）；make test 435 全绿
  （skipped 14）；make eval b933e20 双域零回归；make api-verify A1-A7 全绿报告
  eval/reports/api-acceptance-4a547e7.json（A5 三角色差异 / A6 会话身份冲突 / A7
  零售品类受限 + 跨域 Guard 拒绝）；make rls-verify 双域不回归（报告
  rls-verify-4a547e7.json）。

### 硬化边界如实收窄（README KL #28 ③ 同步）

- 会话指纹/限流/审计均为进程内（uvicorn workers=1，多 worker = 多份状态）；
- 审计 JSONL 为本地文件，非防篡改——生产需外置（README 口径不后退为「已生产」）；
- 身份仍为本地签发 HS256 JWT（无 IdP）；「真实多租户 → gateway 认证先行」推翻
  条件仍未触发；Doris per-user identity 透传（0011 决策 4 独立项）不在本批范围。
