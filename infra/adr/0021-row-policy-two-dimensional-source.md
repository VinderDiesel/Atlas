# ADR-0021：行级策略事实源二维化——域定策略、策略定角色，并兑现 ADR-0011 的 broker 声称

- 日期：2026-09-14
- 状态：accepted（决策经用户确认，Q19；落地批次 P-2sec，独立 `sec` 提交）
- 相关：
  - ADR-0011（安全分层：**决策 3「三角色验证成门禁」的 broker 项不成立**，本 ADR
    兑现并追加落地注记；决策 2「行级策略是语义层一等对象」的事实源由本 ADR 二维化）
  - ADR-0002（Ossie 语义规范：治理扩展挂 `custom_extensions`，`vendor_name=ATLAS`）、
    ADR-0003（只读 SQL 网关：谓词下推）、ADR-0012（HTTP 面身份下推）、
    ADR-0018（前端控制台：角色切换器按域列出，是本 ADR 的消费方）、
    ADR-0015（场景解耦：域 = 语义模型，本 ADR 的「域」维度即此）
  - 权威源：`semantic/policies/row_policy.yml`（**Git 唯一事实源**，2 策略 × 各角色，
    `default_deny: true`）、`semantic/ossie/atlas_finance.ossie.yaml:101`
    （`default_row_policy: rp_branch_visible`）、`semantic/ossie/atlas_retail.ossie.yaml:75`
    （`default_row_policy: rp_dept_visible`）
  - 代码：`serving/auth.py:44-76`（`ROLE_DIRECTORY`，一维 `role → (policy, role, desc)`）、
    `:100-140`（`sign_token`，**只校验角色注册不校验 claims**）、`:286-336`（`resolve_claims`，
    `:305` 从一维表取 policy_name）、`:339-353`（`resolve_policy` 薄包装）
  - 代码：`agent/graph.py:328-348`（`node_execute` 调 `resolve_claims(identity)`，
    **无域上下文**）、`agent/cli.py:361-366`（`atlas query --role` 同形）、
    `agent/compiler.py:217-227`（模型级 `custom_extensions` 解析先例：`time_dimension`）、
    `agent/compiler.py:148-150`（`model.name` = 语义身份，与文件名解耦）
  - 校验：`semantic/governance_validate.py:7`（docstring 声称）、`:195-198`
    （`default_row_policy` 引用的策略必须存在于 `row_policy.yml`——**已在 `make lint` 内**）、
    `tests/test_auth.py:94-99`（单向检查：`ROLE_DIRECTORY` → yaml）
  - 验证工具：`serving/rls_verify.py:17-26`（docstring「三角色」实列 5 个）、
    `:181-222`（`run_finance`：hq_admin / branch_manager / compliance_auditor）、
    `:224-260`（`run_retail`：hq_admin / region_manager / category_analyst）——**broker 从未被测**

---

## 背景

### 事实源是一维的，而权威声明是二维的

`row_policy.yml` 的结构是 **2 个策略 × 各自的 roles**：

| 策略 | 域 | roles | 条件 |
|---|---|---|---|
| `rp_dept_visible` | 零售（TPC-DS） | `hq_admin` / `region_manager` / `category_analyst` | `1=1` / `dim_store.s_state = '{{ user.region }}'` / 前者 + `dim_item.i_category IN ({{ user.categories \| sql_in }})` |
| `rp_branch_visible` | 金融（TPC-DI） | `hq_admin` / `branch_manager` / **`broker`** / `compliance_auditor` | `1=1` / `dim_broker.branch = '{{ user.branch }}'` / `dim_broker.brokerid = {{ user.brokerid }}` / `dim_customer.tier <= {{ user.max_tier }}` |

共 **7 个 (策略, 角色) 对、6 个不同角色名**（`hq_admin` 在两策略下各一次）。

而运行时的 `ROLE_DIRECTORY`（`auth.py:46-76`）是**一维**的：

```python
ROLE_DIRECTORY: dict[str, tuple[str, str, str]] = {   # role → (policy_name, role_name, desc)
    "hq_admin": ("rp_branch_visible", "hq_admin", ...),   # ← 只能选一个，选了金融的
    ...
}
```

`resolve_claims` 在 `auth.py:305` 直接 `policy_name, role_name, _ = ROLE_DIRECTORY[role]`
——**策略名由角色决定，与查询的是哪个域无关**。一维表无法表达二维事实，于是：

**实测后果 1：零售域查询归因金融策略名。** 用 `hq_admin` 查零售域「2001 年销售额」：

```
policy_effect = 行级策略已生效（角色 hq_admin，策略 rp_branch_visible）
                                              ^^^^^^^^^^^^^^^^^ 金融域（TPC-DI）策略名
```

`rp_branch_visible` 的谓词走 `dim_broker` / `dim_customer`，零售模型里根本没有这两张表。
本次因 `hq_admin` 条件恰为 `1=1` 而**语义无害**（无谓词注入），但：
① 前端归因面板显示错误策略名；② 审计流水（`serving/audit/audit.jsonl` 的 `claims` 字段）
记录错误关联；③ 一旦 `hq_admin` 的条件将来不再是 `1=1`，注入的谓词会引用不存在的表
→ Guard 报错或（更坏）静默改变结果集。

`serving/rls_verify.py:18` 的注释其实已经知道这件事：「hq_admin（总部）：条件 1=1 →
全量（**跨域共用，与策略名无关**）」——但代码仍按一维表报策略名。

**实测后果 2：`broker` 角色无法签发，ADR-0011 决策 3 的声称不成立。**

```
$ sign_token("broker", {"brokerid": 1})
AuthError: 角色未注册：'broker'（ROLE_DIRECTORY 可加）
```

`broker` 存在于 `row_policy.yml:39-41`（权威源），但 `ROLE_DIRECTORY` 未注册。
ADR-0011 决策 3 原文声称：

> **三角色验证成门禁**：hq_admin（全量）/ region_manager|branch_manager（本域）/
> **broker（本人 brokerid）**/ compliance_auditor（tier 降级）经 `make rls-verify`
> 在 SQL 谓词层回归（gold-146 问句 × 角色）

而 `rls_verify.py` 的实测覆盖是 `run_finance`（hq_admin / branch_manager /
compliance_auditor）+ `run_retail`（hq_admin / region_manager / category_analyst）
——**broker 连签发都过不去，更谈不上"在 SQL 谓词层回归"**。按 N2，这是一条
把设计写成已完成的既存声称（标题还写「三角色」却列了 5 个角色名）。

### 校验层已经在守二维一致性，但运行层不读它

`semantic/ossie/*.yaml` 已声明域 → 策略的权威映射：

```yaml
# atlas_finance.ossie.yaml:101          # atlas_retail.ossie.yaml:75
"default_row_policy": "rp_branch_visible"   "default_row_policy": "rp_dept_visible"
```

且 `semantic/governance_validate.py:195-198` **已在 `make lint` 内**校验「引用的策略
必须存在于 `row_policy.yml`」。但实测：

```
$ grep -rn "default_row_policy" --include="*.py" .
semantic/governance_validate.py:7,195,198      ← 只有校验器
```

**零运行时代码读它**。于是校验层与运行层用了两套事实源，且它们对 `hq_admin`
给出不同答案：校验层认为「零售模型的策略是 `rp_dept_visible`」，运行层认为
「`hq_admin` 的策略是 `rp_branch_visible`」。`make lint` 全绿也发现不了后果 1。

反向缺口同样存在：`tests/test_auth.py:94-99` 只检查
「`ROLE_DIRECTORY` 每个角色都能在 `row_policy.yml` 找到同名 role」（一维 → 二维的
一个方向），**没有检查反方向**（策略里的每个角色是否都可被签发）——这正是 `broker`
潜伏至今的原因。

### claims 契约无签发期校验（附带发现）

`sign_token`（`auth.py:124-125`）只校验角色是否注册，**不校验 `user_context` 是否
含该角色条件所需的键**。实测：

| 场景 | 签发 | 解析 |
|---|---|---|
| `sign_token("branch_manager", {})` | ✅ **成功** | ❌ `AuthError: 条件存在未渲染占位符：dim_broker.branch = '{{ user.branch }}'` |
| `sign_token("category_analyst", {"region":"TN","categories":["Electronics","Shoes"]})` | ✅ | ✅ `dim_store.s_state = 'TN' AND dim_item.i_category IN ('Electronics', 'Shoes')` |

签发一个**注定无法解析**的 token 是可能的，错误要到查询时才暴露。前端角色切换器
（ADR-0018）要动态渲染 context 表单，必须有「该角色需要哪些 claims 键」的机器可读
契约，否则表单字段只能靠猜。

### `resolve_claims` 没有域上下文可传

`node_execute`（`graph.py:342`）调用 `resolve_claims(identity)` 时，identity 只有
`role` + `user_context`——**请求体里没有域信息**（域由 `body.model` 决定，
只用于选 agent 单例，不下传到策略解析）。`agent/cli.py:366` 同形。
所以修复不只是改表结构，还要把「当前查的是哪个语义模型」这条信息接到策略解析上。

约束：

- **N3 / ADR-0011 决策 2**：`default_deny: true` 语义不得放宽；占位符未渲染即拒绝
  的 Guard 层强制不变。
- **ADR-0011 决策 5**：被拒路径与 `policy_effect` 都**只给角色 + 策略名，不给条件值**。
  本 ADR 修的是策略名的**正确性**，不扩大外泄面。
- **§7.3 / §4**：`semantic/policies/` 是权威目录，新增角色只能改 `row_policy.yml`；
  不得在代码里复制条件（`auth.py:45` 注释已明示「角色条件来自 row_policy.yml，不在此复制」）。
- **§8**：本 ADR 全部改动属 `sec`，不得与 `feat`（前端）或 `refactor`（ADR-0019 的
  sha 副本合并）混在同一提交。

---

## 备选方案

| 方案 | 优势 | 劣势 |
|---|---|---|
| **域定策略 + 策略定角色（选定）** | 与 `row_policy.yml` 和 ossie `default_row_policy` 的既有二维结构同构；`make lint` 已有半条检查可扩为双向；策略名从此正确；claims 契约机器可读（前端表单可生成） | `resolve_claims`/`resolve_policy` 签名破坏性变更（实测 9 处调用 + 8 处测试）；`ROLE_DIRECTORY` 结构变更 |
| `ROLE_DIRECTORY` 扩成 `role → {policy: (policy_name, role_name)}` 二维字典 | 改动集中在一个常量，签名不变 | 策略名仍由**代码里的表**决定，ossie 的 `default_row_policy` 依旧零运行时消费——校验层与运行层仍是两套事实源，后果 1 只是被"改对了值"而非"改对了机制"；新增域要改代码而非改 YAML |
| 由请求体显式传 `policy_name` | 最灵活 | 把安全决策交给客户端——调用方可指定任意策略名（含更宽松的），违反 ADR-0011「纵深防御」与 `default_deny`；必须再加白名单校验，等于绕回方案 1 |
| 从 claims 里推断域（`user_context` 有 `region` → 零售） | 无需改签名 | 用**数据形状**推断安全语义，脆弱且不可解释；`hq_admin` 两域都是空 context，无法推断；违反「确定性优先」 |
| 只补 `broker` 注册，不动结构 | 一行改动，兑现 ADR-0011 决策 3 | 后果 1（策略名误报）保留；`hq_admin` 的域歧义保留；claims 契约仍不可读；前端角色切换器仍无法按域过滤 |

---

## 决策

### ① 权威映射：域 → 策略（ossie），策略 → 角色 → 条件（row_policy.yml）

```
语义模型 custom_extensions.policy.default_row_policy   →  策略名（域决定）
row_policy.yml policies[].roles[]                       →  角色是否存在于该策略 + 条件模板
ROLE_DIRECTORY                                          →  角色注册 + claims 契约（不再含策略名）
```

`resolve_claims` 的策略名**不再来自 `ROLE_DIRECTORY`**，而由调用方按当前语义模型传入。
两个 YAML 仍是唯一事实源，代码不复制任何条件（`auth.py:45` 的既有纪律保持）。

### ② `SemanticModel.default_row_policy`：沿 `time_dimension` 的既有解析先例

`agent/compiler.py:217-227` 已有模型级 `custom_extensions`（`vendor_name == "ATLAS"`）
的解析循环，用于 `time_dimension`。同一循环内**同时**取 `policy.default_row_policy`：

```python
self.time_dimension: dict | None = None
self.default_row_policy: str | None = None      # 新增
for ext in model.get("custom_extensions", []):
    if ext.get("vendor_name") != "ATLAS":
        continue
    data = json.loads(ext["data"])               # 解析失败仍抛 CompileError（既有行为）
    if data.get("time_dimension") is not None and self.time_dimension is None:
        self.time_dimension = data["time_dimension"]
    policy = data.get("policy", {}).get("default_row_policy")
    if policy and self.default_row_policy is None:
        self.default_row_policy = str(policy)
```

现有循环里的 `break`（`:227`）必须去掉——它假设「第一个含 `time_dimension` 的扩展
就是全部」，而 `policy` 可能声明在另一个 ATLAS 扩展块里。

**缺失即拒绝**：模型未声明 `default_row_policy` 而本轮带 identity →
`node_execute` 返回 `{"error": "语义模型未声明 default_row_policy，无法注入行级策略"}`，
**不降级为无策略执行**（`default_deny` 精神；无身份路径不受影响，零变化）。

### ③ `resolve_claims` / `resolve_policy` 增必填 `policy_name`

```python
def resolve_claims(
    claims: dict[str, object],
    *,
    policy_name: str,                    # 必填：由语义模型 default_row_policy 提供
    policy_path: Path = POLICY_PATH,
) -> ResolvedPolicy:
```

- 角色不在**该策略**的 `roles` 里 → `AuthError(f"角色 {role!r} 不属于策略 {policy_name!r}（域不匹配）")`。
  这把「用零售角色查金融域」从**静默按一维表解析**变成显式拒绝；
- `ResolvedPolicy` 结构**不变**（`role` / `policy_name` / `condition` / `claims`，
  `frozen dataclass`）——`tests/test_auth.py` 对它有断言，且 `describe()` 被报告消费；
- `resolve_policy(token, *, policy_name, ...)` 同步（薄包装，`auth.py:339-353`）。

调用方改造（实测 9 处 + 测试 8 处）：

| 调用方 | 传入的 policy_name |
|---|---|
| `agent/graph.py:342`（`node_execute`） | `compiler.model.default_row_policy`（`Compiler.model`，`compiler.py:670`） |
| `agent/cli.py:366`（`atlas query --role`） | 同上（该命令已按 `--model` 构造 `SemanticModel`） |
| `serving/rls_verify.py:109` | 由 `run_finance` / `run_retail` 分别传 `rp_branch_visible` / `rp_dept_visible`（函数本就按域分档） |
| `serving/p1_acceptance.py:231,245` | `rp_branch_visible`（金融档验收） |
| `tests/test_demo_e2e.py:215`、`tests/test_auth.py:68,86,91,114,176` | 按用例域显式传 |

### ④ `ROLE_DIRECTORY` 重塑为 claims 契约注册表

```python
@dataclass(frozen=True)
class RoleSpec:
    """角色注册项：claims 契约 + 说明。**不含策略名**——策略由域决定（决策 ①）。"""
    required_claims: tuple[str, ...]     # 条件模板所需的 user_context 键（顺序 = 文档顺序）
    list_claims: frozenset[str] = frozenset()   # 需 sql_in 渲染的列表值键
    description: str = ""

ROLE_DIRECTORY: dict[str, RoleSpec] = {
    "hq_admin":           RoleSpec((), frozenset(), "总部管理员：全量可见（条件 1=1，两域共用）"),
    "branch_manager":     RoleSpec(("branch",), frozenset(), "分支经理：仅见本分支"),
    "broker":             RoleSpec(("brokerid",), frozenset(), "经纪人：仅见本人 brokerid 名下"),   # ← 新注册
    "compliance_auditor": RoleSpec(("max_tier",), frozenset(), "合规审计：仅见 tier ≤ 上限的客户档"),
    "region_manager":     RoleSpec(("region",), frozenset(), "大区（州）经理：仅见本州门店"),
    "category_analyst":   RoleSpec(("region", "categories"), frozenset({"categories"}), "品类分析师：本州 + 指定品类集"),
}
```

`required_claims` / `list_claims` **从 `row_policy.yml` 的 condition 模板推导并交叉校验**
（模板里的 `{{ user.X }}` 与 `{{ user.X | sql_in }}` 占位符即契约），
在 `make lint` 中比对，避免代码里的元组与 YAML 漂移（决策 ⑥）。

`sign_token`（`auth.py:124-140`）**新增签发期校验**：`user_context` 缺任一
`required_claims` 键、或 `list_claims` 键的值不是非空列表 → `AuthError`。
把「签发一个注定解析失败的 token」拦在签发时（实测今天可以签发成功）。

### ⑤ 注册 `broker`，并按域暴露角色清单

- `broker` 进 `ROLE_DIRECTORY`（决策 ④），条件仍只来自 `row_policy.yml:39-41`
  （`dim_broker.brokerid = {{ user.brokerid }}`，**模板不带引号** → `_literal` 渲染
  int，与 `max_tier` 同形）；
- 新增 `roles_for_policy(policy_name, *, policy_path=POLICY_PATH) -> tuple[str, ...]`
  （读 YAML，返回该策略下**已注册**的角色，按 YAML 顺序）——前端角色切换器
  （ADR-0018）与 `make token` 的帮助文本共用；
- 域 × 角色的最终矩阵（实测 `row_policy.yml` + 决策 ④）：

| 角色 | 金融 `rp_branch_visible` | 零售 `rp_dept_visible` | 必需 claims |
|---|---|---|---|
| `hq_admin` | ✅ | ✅ | — |
| `branch_manager` | ✅ | ❌ | `branch` |
| `broker` | ✅ | ❌ | `brokerid` |
| `compliance_auditor` | ✅ | ❌ | `max_tier` |
| `region_manager` | ❌ | ✅ | `region` |
| `category_analyst` | ❌ | ✅ | `region`, `categories`（列表） |

前端角色切换器必须**按当前域过滤**该矩阵，且切换角色时换 `session_id`
（ADR-0020 决策 ⑥ 的身份指纹跨重启校验会拒同会话换身份）。
`agent/cli.py:_parse_role_ctx` 的既有限制不变（列表值不在 CLI 解析 →
`category_analyst` 仍只能走 HTTP JSON 上下文）。

### ⑥ `make lint` 增双向一致性检查（校验层与运行层锁定同一事实源）

`semantic/governance_validate.py` 在既有 `:195-198`（default_row_policy → 策略存在）
基础上新增：

1. **策略 → 角色 → 注册**：每个被 `default_row_policy` 引用的策略，其 `roles[]` 里的
   每个角色必须在 `ROLE_DIRECTORY` 注册。**今天 `broker` 就会命中此检查**
   （即：这条检查一旦上线，既存债务立刻可见，`make lint` 由绿转红，直到决策 ④ 落地）；
2. **注册 → 策略**：`ROLE_DIRECTORY` 的每个角色必须至少属于一个被引用的策略
   （防注册孤儿角色）；
3. **claims 契约一致**：`RoleSpec.required_claims` / `list_claims` 必须等于该角色
   条件模板里的占位符集合（含 `| sql_in` 区分）——防代码元组与 YAML 漂移；
4. **域覆盖**：每个被引用的策略必须恰好被 ≥1 个语义模型声明（防策略孤儿）。

`tests/test_auth.py:94-99` 的单向检查扩展为双向（或删除，由 lint 覆盖 + 契约测试
断言 lint 结果）。**执行点是 `make lint`**（ADR-0018 决策 ⑥：本地 make 是唯一真实门槛）。

### ⑦ 兑现 ADR-0011 决策 3：`rls-verify` 增补 broker 档

`serving/rls_verify.py:181-222` 的 `run_finance` 增加第 4 个角色档：

- `broker` 的 `brokerid` 取自 `hq_admin` 全量结果中的真实值（与现有
  `branch_manager` 的 branch 取值方式同构，`:195-203`），**不硬编码**；
- 断言：`broker` 结果行数 < `hq_admin` 行数，且结果集的 brokerid 列单值等于注入值
  （谓词真实生效，非应用层过滤）；
- 报告 `eval/reports/rls-verify-<sha>.json` 的 `roles` 从 3 增至 4，
  docstring `:17-26` 的「三角色」措辞改为按实测档数表述；
- ADR-0011 末尾追加**落地注记**（沿 `:86` 与 ADR-0012 `:89` 的先例，不改正文）：
  如实记录「决策 3 的 broker 项在 2026-09-05 批次未兑现（`ROLE_DIRECTORY` 未注册，
  签发即 AuthError），由本 ADR 于 <实测后填写> 兑现，绑定报告
  `eval/reports/rls-verify-<sha>.json`」，并把「三角色」标题措辞的实列 5 角色
  矛盾一并注记。

**N1 纪律**：注记中的兑现日期与报告 sha 必须来自实跑产物，未跑不得填写。

---

## 理由

1. **机制正确优于数值正确**：备选方案第 2 行（二维字典）能把 `hq_admin` 的策略名
   "改对"，但 `default_row_policy` 依旧零运行时消费——下次新增域或改策略，同样的
   漂移会再来一次。决策 ①② 让运行层读**校验层已经在守的那份声明**，两套事实源
   合并为一套，`make lint` 从「只校验引用存在」升级为「校验运行时可用性」。
2. **复用既有解析先例，不新造机制**：`custom_extensions` 的模型级解析在
   `compiler.py:217-227` 已经跑通（`time_dimension` 驱动金融 single / 零售 composite
   两种时间形态）。`default_row_policy` 走同一条路，零新概念。
3. **安全语义不得由客户端决定**：备选方案第 3 行（请求体传 policy_name）会把
   策略选择权交给调用方，与 `default_deny` 和纵深防御直接冲突。域信息必须来自
   **服务端已选定的语义模型**（`body.model` → agent 单例 → `model.default_row_policy`）。
4. **既存声称必须兑现或删除**：ADR-0011 决策 3 写了 broker 而实测无法签发。按 N2/N4
   精神，选择**兑现**（决策 ⑦）而不是删掉那句声称——删掉是美化，兑现是还债，
   且 broker 是 `row_policy.yml` 里已声明的权威角色，本就该可用。
5. **claims 契约机器可读是前端的前置**：ADR-0018 的角色切换器要动态渲染 context
   表单。没有 `required_claims`，表单字段只能硬编码在前端 → 前端变成第二套事实源。
   决策 ④⑤ 让表单可由端点生成（ADR-0022 的治理端点消费 `roles_for_policy` + `RoleSpec`）。

---

## 代价与限制

① **破坏性签名变更**：`resolve_claims` / `resolve_policy` 增必填 `policy_name`，
实测影响 9 处生产调用 + 8 处测试调用；`ROLE_DIRECTORY` 值类型从 `tuple[str,str,str]`
变 `RoleSpec`，影响 `auth.py` 内 4 处解包（`:124,168,239,305`）与 `tests/test_auth.py:99,149`。
必须在同一 `sec` 提交内改完（半改状态 = 全链路 500）。

② **`default_row_policy` 成为运行时硬依赖**：任何新语义模型若漏声明它，带 identity
的查询会**全部失败**（决策 ②「缺失即拒绝」）。这是有意的（`default_deny`），
但意味着「加一个域」的成本上升：必须同时改 ossie YAML + `row_policy.yml` +
`ROLE_DIRECTORY` + lint 通过。补偿是决策 ⑥ 的 lint 会在提交前拦住。

③ **`hq_admin` 的跨域语义仍未统一**：两域条件都是 `1=1`，所以「全量可见」这个角色
在两个策略下重复声明。决策 ① 让策略名报对了，但**重复声明本身保留**（`row_policy.yml`
是权威源，不在代码里合并）。若将来两域的 `hq_admin` 需要不同条件，现有结构支持；
若需要「一个角色跨所有域」，则要引入策略继承——超出本 ADR 范围。

④ **`broker` 的验证依赖真实数据分布**：`rls_verify` 的 brokerid 取自 `hq_admin`
全量结果，若 SF0.1 数据中 brokerid 基数极小（甚至单值），则「broker 行数 < hq_admin
行数」的断言可能不成立。**未实测 SF0.1 的 brokerid 基数**——决策 ⑦ 的断言形态
需在实跑后按数据事实调整（`rls_verify.py:24-26` 已有同类先例：零售单州 TN 导致
`region_manager` 与 `hq_admin` 结果一致，如实报告而非伪造差异）。

⑤ **`list_claims` 只覆盖 `sql_in` 一种列表渲染**：若将来策略需要
`NOT IN` / 区间等其他列表形态，`RoleSpec` 需扩展，`_literal` 与 Guard 的
`_escape_literal` 双份实现（`auth.py:356-368` 与 `sql_guard.py:196-205`，
注释自承「重复校验无妨」）也要同步。

⑥ **lint 检查 1 上线即红**：决策 ⑥ 的第 1 条会让 `make lint` 在 `broker` 注册前失败。
因此**落地顺序必须是「先决策 ④ 注册 broker，再决策 ⑥ 上检查」**，同一提交内完成；
否则 CI（`lint.yml` 无 paths 过滤，任何 push 都跑）会红。

⑦ **前端角色切换器的域过滤依赖 ADR-0022 的端点**：`roles_for_policy` 需要经治理
端点暴露（否则前端要硬编码矩阵，即第二套事实源）。两个 ADR 的落地批次因此耦合：
P-2sec（本 ADR）必须先于 **P-2api**（ADR-0022 治理端点，含端点 6 `policies` 的
`registered` 与角色清单）完成。批次名订正：本条初稿写作「P0b」，而 P0b 是
ADR-0018 的**工程边界**批次（`frontend/.gitignore` / `.dockerignore` / `make ui-check`），
治理端点属 ADR-0022 的 P-2api；完整顺序见 ADR-0018 落地注记第 3 条
（P-1 → P-2sec → P-2api → P0a → P0b → P1~P3）。排序意图不变，仅标签错位。

---

## 什么情况下应该推翻

- **出现「一个角色在不同域需要不同 claims 契约」**（例如金融 `region_manager` 需要
  `branch` 而零售需要 `region`）→ 决策 ④ 的「角色 → claims」一维契约不够，
  `RoleSpec` 需按 (策略, 角色) 二维化，`ROLE_DIRECTORY` 退化为角色名注册；
- **策略数量增长到需要组合/继承**（例如「基础策略 + 临时豁免」）→ `row_policy.yml`
  的扁平 `policies[].roles[]` 结构与决策 ① 的单值 `default_row_policy` 都不够，
  需引入策略表达式与优先级，并重估 Guard 的谓词注入顺序；
- **接入真实 IdP（ADR-0011 决策 4：Doris identity mode 下推真实用户）** →
  角色来源从本地 `ROLE_DIRECTORY` 变为 IdP 的 group/role claim，决策 ④ 的注册表
  降级为「IdP 角色 → 本地角色」映射，claims 契约需改为从 IdP 声明推导；
- **`default_row_policy` 需要按指标/维度粒度而非模型粒度声明**（例如同一模型内
  敏感指标用更严策略）→ 决策 ② 的模型级解析不够，需下沉到 metric 级
  `custom_extensions`，并重新设计 Guard 的多谓词合并；
- **实测发现 SF0.1 数据无法区分 broker 与 hq_admin 结果**（代价 ④）→ 决策 ⑦ 的
  断言形态需改为「谓词已注入且 SQL 可解释」而非「行数差异」，沿
  `rls_verify.py:24-26` 的「如实报告数据事实」先例。

---

## 验证方式

**契约测试（无 DB，进 `make test`）**：

1. `SemanticModel(DOMAIN_MODELS["finance"]).default_row_policy == "rp_branch_visible"`；
   retail 同理 == `"rp_dept_visible"`（决策 ②）。构造一个**缺 `policy` 声明**的
   临时 YAML → `default_row_policy is None`，且带 identity 的 `ask` 返回
   `kind="error"` 而非静默无策略执行。
2. `resolve_claims({"role":"region_manager",...}, policy_name="rp_branch_visible")`
   → `AuthError` 含「域不匹配」；同 claims 配 `rp_dept_visible` → 成功（决策 ③）。
3. **`hq_admin` 策略名按域正确**：金融 → `rp_branch_visible`，零售 → `rp_dept_visible`；
   两者 `condition == "1=1"`。**这条专门锁死后果 1 的回归**。
4. `sign_token("broker", {"brokerid": 1})` 成功；`resolve_claims(..., policy_name="rp_branch_visible")`
   → `condition == "dim_broker.brokerid = 1"`（int 无引号，与 `max_tier` 同形）。
5. `sign_token("branch_manager", {})` → **签发期** `AuthError` 列出缺失键 `branch`
   （决策 ④；今天实测签发成功、解析才失败）；`sign_token("category_analyst",
   {"region":"TN","categories":[]})` → 签发期拒绝（空列表）。
6. `roles_for_policy("rp_dept_visible") == ("hq_admin","region_manager","category_analyst")`
   （YAML 顺序）；`roles_for_policy("rp_branch_visible")` 含 `broker` 共 4 项（决策 ⑤）。
7. `policy_effect` 文案形态不变（只含角色 + 策略名，不含条件值）——
   `tests/test_graph.py:228` 既有断言保持通过（ADR-0011 决策 5）。

**lint 判据（`make lint`）**：

8. 决策 ⑥ 的 4 条检查各有正反用例：删掉 `broker` 注册 → 检查 1 报错；
   注册一个 YAML 里不存在的角色 → 检查 2 报错；`RoleSpec.required_claims`
   与模板占位符不符 → 检查 3 报错；某策略无任何模型引用 → 检查 4 报错。
9. 落地后 `make lint` 全绿（证明代价 ⑥ 的顺序问题已按「先注册再上检查」处理）。

**真链判据（需 Doris，`make rls-verify`）**：

10. `eval/reports/rls-verify-<sha>.json` 的 `roles` 含 4 个金融角色（broker 在列），
    报告 sha 与实跑 HEAD 一致；broker 档的注入后 SQL 含
    `dim_broker.brokerid = <实测值>` 且结果集 brokerid 单值。
11. 零售域 `/ask`（`hq_admin`）的 `explanation.policy_effect` 报
    `rp_dept_visible`（**当前实测为 `rp_branch_visible`**），审计 JSONL 同步正确。
12. 跨域误用：用 `region_manager` 的 token 查金融域 → `kind="error"`
    （身份策略解析失败：域不匹配），且不执行任何 SQL（Guard 之前即拒）。

**文档判据**：

13. ADR-0011 末尾追加落地注记（不改正文），如实记录 broker 声称的未兑现与兑现时点；
    `rls_verify.py:17-26` docstring 的「三角色」措辞与实列角色数一致。
14. 角色矩阵的**文字口径**统一到 6 角色 × 2 域（决策 ⑤ 表格），涉及三处实测不一致：
    `serving/rls_verify.py:17` 写「三角色」却在 `:18-22` 实列 5 角色（同判据 13）；
    `serving/auth.py` 的 `ROLE_DIRECTORY` 实测 5 键（无 `broker`）；ADR-0018 `:36`
    的角色切换器条目只描述了 `broker` 缺失与 `hq_admin` 误报两个症状，**未给角色数**
    （故本判据初稿写的「ADR-0018 不再是『5 角色』」无可改对象，订正为：ADR-0018
    若在前端批次引用角色清单，一律指向本 ADR 决策 ⑤ 表格，不另立数字）。

---

## 落地注记：P-2sec 批次（2026-09-16，独立 `sec` 提交；沿 ADR-0011 `:86` 先例，不改裁定正文）

### 判据 1~14 逐条兑现（证据 = 实跑产物，N1）

| # | 兑现证据 |
|---|---|
| 1 | `tests/test_compiler.py::TestDefaultRowPolicyDeclaration`（双域声明值 + 缺声明 None + 双扩展块去 break 回归锁）+ `tests/test_graph.py::test_missing_policy_model_rejects_identity`（缺声明 + identity → `kind="error"` 且执行器零调用） |
| 2 | `tests/test_auth.py`（同 claims 配 `rp_dept_visible` 成功、配 `rp_branch_visible` → AuthError 含「域不匹配」） |
| 3 | `test_hq_admin_policy_name_follows_domain`（两域策略名 + condition 均 `1=1`）+ 真链 api-verify A7c |
| 4 | `test_broker_brokerid_int_rendering`（`dim_broker.brokerid = 7285` 无引号同形）+ 收口实测 `condition == "dim_broker.brokerid = 1"` |
| 5 | `TestRoleSpecContract`：`sign_token("branch_manager", {})` → 签发期 AuthError 列缺失键 `branch`；空列表 / 标量列表均签发期拒 |
| 6 | `TestRolesForPolicy`：`rp_dept_visible` → `("hq_admin","region_manager","category_analyst")`（YAML 顺序）；`rp_branch_visible` 含 `broker` 共 4 项 |
| 7 | `tests/test_graph.py` 既有断言保持（生效句只含角色 + 策略名，条件值不外泄） |
| 8 | `tests/test_governance_validate.py::TestPolicyConsistency`（正例真实数据 + 5 个单点反例覆盖 4 检查） |
| 9 | `make lint` 5 项全过（2026-09-16 实测；新增 4 检查上线即绿） |
| 10 | `eval/reports/rls-verify-e0f2d29.json`：金融 `roles` 4 角色（`broker` 在列）/ brokerid=5460 / 3 行 < hq_admin 5 行 / 差异集 4；报告 sha 与实跑 HEAD 一致 |
| 11 | api-verify A7c 真链：零售 hq_admin `policy_effect` 报 `rp_dept_visible`（不再误报 `rp_branch_visible`）；审计 JSONL 同步由 `test_ask_retail_hq_admin_policy_name_follows_domain` 锁定 |
| 12 | api-verify A7b 真链：`kind="error"` + 「域不匹配」+ `executed_sql_count=0`（Guard 之前即拒）；审计行 kind 同步 error 由 `test_ask_cross_domain_identity_rejected_no_execution` 锁定 |
| 13 | ADR-0011 兑现行已回填（2026-09-16 + 报告 sha，见该 ADR 注记段）；`rls_verify.py` docstring 按实测档数表述（金融 4 档 / 零售 3 档） |
| 14 | `ROLE_DIRECTORY` 6 键（`broker` 已注册）；ADR-0018:36 为缺陷症状历史描述（指向本 ADR），按订正不另立数字 |

### 其他落地事实

- **顺序约束按代价 ⑥ 处理**：broker 注册（决策 ④⑤）先于 lint 检查接线（决策 ⑥），
  同一提交完成——`make lint` 落地即绿，未出现「检查上线即红」中间态。
- **真链两报告**：`make rls-verify` 双域全过（金融 4 档含 broker / 零售 3 档、
  差异集 2）；`make api-verify` 9/9 全绿（A7b 旧断言 `kind="blocked"`（Guard 兜底）
  重锚为域不匹配的 `kind="error"`——行为变更点：域校验从 Guard 兜底提前到身份
  解析层，先于任何 SQL 执行）。
- **`make test`**：Ran 718 OK (skipped=14；全为 TestDemoE2E 类级跳过——新 HEAD 无
  同名 meta，属常态非回归；相对 P-1 基线 693 净增 25 例）。
- **P-2api 耦合（代价 ⑦）**：`roles_for_policy` 已就绪、未经治理端点暴露——在
  P-2api 落地前，前端角色矩阵**不得硬编码**，唯一口径 = 本 ADR 决策 ⑤ 表格。
- **正文占位符说明**：决策 ⑦ 正文的 `<实测后填写>`（兑现日期自指引用）= 本注记
  2026-09-16（正文不改，沿 ADR-0011 `:86` 先例由注记回填）；正文中的 `<sha>`
  为报告文件名模式说明，保留原样。
