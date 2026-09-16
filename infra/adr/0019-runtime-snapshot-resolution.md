# ADR-0019：运行时快照口径统一——身份单一事实源、按 created_at 选最新、域一致性启动校验

- 日期：2026-09-14
- 状态：accepted（决策经用户确认，Q2；落地批次 P-1）
- 相关：
  - AGENTS.md **N6**（禁止把 `data/snapshots/` 外的数据库当作评测基准）、N2（不把设计写成已完成）
  - ADR-0012（HTTP 服务面 v1：`/ask` 的 503 语义来自本 ADR 要修的严格口径）、
    ADR-0018（前端控制台：UI 必须回显绑定 sha，决策 ③⑥ 的消费方）、
    ADR-0010（评测方法论：EX 绑定固定 sha，本 ADR **不改**）、
    ADR-0016（值域画像：`snapshot_sha` 必须属于已锁集合，`semantic/governance_validate.py:129-134`）
  - 代码：`agent/factory.py:36-49`、`agent/cli.py:193-201`、`serving/api.py:337-345`、
    `eval/runner.py:64-83`（`git_short_sha`，唯一认 `ATLAS_GIT_SHA` 的实现）、
    `eval/runner.py:84-101`（`verify_snapshot`）、`eval/runner.py:198-209`（`build_budget`）、
    `eval/runner.py:321-328`（评测侧 HEAD 严格 + 指纹复核）、
    `semantic/lint.py:113-121`（`_latest_snapshot_meta`，**仓库内唯一按 created_at 取最新的正确实现**）
  - 数据：`data/snapshots/README.md:46-63`（同数据多锁纪律）、17 份 `*.meta.json`（实测见背景表）
  - 部署：`infra/docker/api/Dockerfile:10-11,33-34`、`docker-compose.yml:197-211`

---

## 背景

「绑定哪个快照」这件事在仓库里有**四套互不相同的口径**，且其中三套的 docstring
声称与另一套一致。实测（HEAD `bdcb6c0`，2026-09-14）：

| 口径 | 位置 | 规则 | 声称 |
|---|---|---|---|
| **A. HEAD 严格** | `eval/runner.py:323,328`（+`verify_snapshot`）、`agent/factory.py:38-44`、`serving/api.py:343-344`、`eval/rag_eval.py:165`、`eval/compare_4way.py:61`、`data/value_profile.py:226`、`tests/test_demo_e2e.py:69,119` | `{git_short_sha()}.meta.json`，缺则失败 | — |
| **B. 字典序最大** | `agent/cli.py:199`（`atlas query`）、`serving/p1_acceptance.py:100`、`serving/metrics_verify.py:63`、`serving/rls_verify.py:88` | `sorted(glob("*.meta.json"))[-1]` | 四处 docstring 全写「**取最新 meta**」「与 `eval/runner.build_budget` **同口径**」 |
| **C. 硬编码常量** | `eval/api_acceptance.py:69`（`b933e20`）、`eval/e2e_acceptance.py:48`（`7d48dcb`）、`tests/{test_api,test_api_hardening,test_chart,test_feedback,test_graph,test_mcp_server,test_tools_registry}.py`（均 `7d48dcb`） | 固定 sha 字面量 | — |
| **D. 集合校验** | `semantic/governance_validate.py:129-134`、`semantic/lint.py:113-121` | 已锁 sha 集合 / 按 `created_at` 取最新 | — |

> **本表是改造前的快照，不随实现更新**（背景节的作用就是留下「当时有四套口径」的证据）。
> 落地进度记在决策段，不覆盖本表：**口径 B 已于工作项 3 消除**（四处改走
> `resolve_runtime_snapshot()`，见代价 ⑧ 的落地注与判据 11）；**口径 A 拆成两侧**——
> 评测侧原样保留（N6），运行时侧（`agent/factory.py`）随工作项 3 改走三级解析，
> `serving/api.py` 随工作项 6（已改：`/health` 与预算均走 `resolve_runtime_snapshot()`，
> 仅降级分支用 `git_short_sha_or_none()`，仍是 `data/identity.py` 的同一出口）；
> 口径 C、D 不动（C 属 N6 例外白名单，D 见实施裁定 9）。

### 口径 B 的「最新」是错的：文件名是 hex sha，字典序 ≠ 时间序

实测 17 份 meta：

```
sorted()[-1]              = dc4f350   created_at = 2026-09-04T12:54:14+08:00
max(key=created_at)       = a11d779   created_at = 2026-09-09T11:56:23+08:00
```

`d` > `a`，所以字典序选中了一份**早 5 天**的快照。`semantic/lint.py:113-121`
已经用 `max(metas, key=lambda m: str(m.get("created_at", "")))` 做对了——说明
口径 B 不是设计选择，是四处复制粘贴的疏漏，且各自 docstring 声称「取最新」。

**「四处」的明细（2026-09-14 开工实测补齐；初稿只给了数量，无清单即不可复验）**。
检索口径是行为而非写法：**先列目录再挑一个**的位置：

| 处 | HEAD 行 | 写法 | 口径 |
|---|---|---|---|
| `agent/cli.py` | 199~202 | `sorted(SNAPSHOT_DIR.glob(…))` + `metas[-1]` | ❌ 字典序 |
| `serving/p1_acceptance.py` | 100~103 | 同上（当时路径还是内联的） | ❌ 字典序 |
| `serving/metrics_verify.py` | 63~66 | 同上（同上） | ❌ 字典序 |
| `serving/rls_verify.py` | 88~91 | 同上（同上） | ❌ 字典序 |
| `semantic/lint.py` | 118~122 | `sorted(...)` 收集后 `max(key=created_at)` | ✅ 正确 |

> 本表「HEAD 行」用 `开始~结束` 记**行号范围**（不是漂移记号 `A → B`），两种记号
> 同形曾在复核时读成「199 漂到了 202」，2026-09-14 改为连接号。

其余 **10 处**读 meta 的位置**不在本缺陷范围内**，理由必须写明否则「四处」不可复核
（按形态分类实测，生产侧不含 `tests/`）：

- **已知 sha 直接定位单个文件，不做任何选择：7 处** —— 形态
  `SNAPSHOT_DIR / f"{sha}.meta.json"`，位于 `serving/api.py:344`、`agent/factory.py:39`、
  `eval/runner.py:311`、`eval/rag_eval.py:165`、`eval/compare_4way.py:61`、
  `data/value_profile.py:215`、`data/snapshot.py:136`；
- **取全集而非取其一并存：1 处** —— `semantic/governance_validate.py:135`
  （构造已锁定 sha 集合）；
- **固定 sha 字面量：2 处** —— `eval/{api,e2e}_acceptance.py`，属 N6 侧，
  见决策 ⑤ 辨析与判据 5(b) 的例外白名单。

当前**侥幸无害**：实测 17 份 meta 的指纹（`row_counts` + `snapshot_ids` 全等）分两组，
`dc4f350` 与 `a11d779` 同组：

| 指纹组 | 份数 | source | 表数 | 总行数 | 成员 |
|---|---|---|---|---|---|
| 组 1 | **12** | TPC-DI + TPC-DS SF0.1 | 29 | 3,939,748 | 160795d 1e5d35b 26e7694 40b71e2 5d1e22b 7051ef6 73b5971 92033c9 9749fc5 a11d779 b933e20 dc4f350 |
| 组 2 | **5** | TPC-DI（单源） | 25 | 3,608,202 | 30b8344 7d48dcb a207284 b47a6c1 b7e9ce7 |

但这是**运气不是设计**：只要将来出现一个 hex 字典序更大、却属于组 2（或任何
表集更小的）快照，口径 B 的四处会静默用错白名单——症状是 Guard 拒绝合法查询，
而归因信息（`表不在白名单内`）不会指向「选错了 meta」。

### 口径 A 在运行时是过度严格，导致 `/ask` 直接不可用

`agent/factory.py:40` 要求 `{HEAD}.meta.json` 存在，否则 `SnapshotUnavailable`
→ HTTP 503（`serving/api.py:330-333`）/ CLI exit 1。实测当前 HEAD：

```
$ curl -s http://127.0.0.1:8000/health
{"status":"ok","head_sha":"bdcb6c0","snapshot_sha":null}     ← 宿主机：/ask 必然 503
$ curl -s http://127.0.0.1:8010/health
{"status":"ok","head_sha":"7d48dcb","snapshot_sha":"7d48dcb"} ← 容器：看似健康
```

宿主机的 `snapshot_sha: null` 是**设计内的正常状态**：`data/snapshots/README.md:62-63`
的「同数据多锁」纪律要求数据未变时 HEAD 前进后重锁新 sha，但**重锁是人工动作**，
两次 commit 之间必然存在「HEAD 无 meta」的窗口。评测侧严格是对的（N6：评测基准
必须绑固定 sha），运行时侧严格则把「还没重锁」变成「服务不可用」——而运行时
查询的白名单只需要「某个已锁快照的表集」，并不需要「等于 HEAD」。

### 容器身份指向组 2 快照，零售域结构性被拒（实测复现）

`docker-compose.yml:211` 的 `GIT_SHA: ${GIT_SHA:-7d48dcb}` 覆盖了
`infra/docker/api/Dockerfile:33` 的 `ARG GIT_SHA=b933e20`——两处默认值不同，
compose 生效的是 `7d48dcb`（**组 2，单源 25 表，无零售表**）。而 Dockerfile:11
的注释声称「GIT_SHA 默认 = 最新 29 表全量数据版本（b933e20）」，与实际生效值不符。

后果用 HEAD 代码 + 该预算复现（脚本见验证方式）：

```
身份 sha=7d48dcb  白名单=25  retail 问句「2001 年销售额」
  plan=Plan metric=total_sales_price            ← Planner 正常解析
  enforce=❌ UnsafeQuery: 表不在白名单内：atlas.dwd.store_sales

身份 sha=a11d779  白名单=29  同一问句
  enforce=✅ 通过  cost=0.25
```

语义模型声明的 4 张零售表在 `7d48dcb` 白名单中**全部缺失**：

```
atlas_retail_analytics.datasets → sources:
  atlas.dwd.date_dim / atlas.dwd.dim_item / atlas.dwd.dim_store / atlas.dwd.store_sales
  sha=7d48dcb: 缺失 = 全部 4 张        sha=a11d779: 缺失 = 无
```

即：容器 `/health` 报 ok、`/ask` 金融域可用，但**零售域每一次查询都会被 Guard
拒绝**，且拒绝信息不指向根因。这类「身份与能力不匹配」在启动期完全静默。

### sha 身份有 10 个生产副本，只有 1 个认 `ATLAS_GIT_SHA`

`ATLAS_GIT_SHA` 是容器/无 `.git` 环境的身份注入通道（`Dockerfile:34`
`ENV ATLAS_GIT_SHA=${GIT_SHA}`），但只有 `eval/runner.py:71` 读它。其余 10 个
副本硬跑 `git rev-parse --short HEAD`，其中 9 个 `check=True`（无 `.git` 即抛）、
1 个（`export_dbt`）容错返回 `"unknown"`：

| 副本 | 行 | 函数名 | 认 env | 无 .git 时 |
|---|---|---|---|---|
| `eval/runner.py`（权威） | 64 | `git_short_sha` | ✅ | 回退 git，失败则抛 |
| `serving/p1_acceptance.py` | 87 | `git_short_sha` | ❌ | `CalledProcessError` |
| `serving/metrics_verify.py` | 49 | `git_short_sha` | ❌ | 同上 |
| `serving/rbac_verify.py` | 61 | `git_short_sha` | ❌ | 同上 |
| `serving/rls_verify.py` | 73 | `git_short_sha` | ❌ | 同上 |
| `eval/retrieval_eval.py` | 40 | `git_short_sha` | ❌ | 同上 |
| `eval/schema_link_eval.py` | 39 | `git_short_sha` | ❌ | 同上 |
| `data/snapshot.py` | 45 | `git_short_sha` | ❌ | 同上 |
| `data/value_profile.py` | 204 | **`head_sha`** | ❌ | 同上 |
| `metadata/parser.py` | 85 | `git_short_sha` | ❌ | 同上 |
| `semantic/export_dbt.py` | 279 | **`_git_short_sha`** | ❌ | `except` → `"unknown"` |

> **本表初稿漏了 `data/value_profile.py:204`（2026-09-14 开工实测补）**：初稿检索式按
> **函数名**匹配（`def git_short_sha` / `def _git_short_sha`），而该处叫 `head_sha`
> （无下划线）→ 漏检。但副本是按**语义**定义的（「同一份 git HEAD 解析逻辑」），
> 检索式却按命名定义，**口径小于裁定口径**，与 KL #31 第五项 (b) 同型（检索条件与
> 分类判据不同源）。正确检索式按行为匹配：
> `grep -rn "rev-parse" --include="*.py" . | grep -v '\.venv'`
> → 实测命中 **13 文件 = 11 生产 + 2 测试**（测试侧 2 处的处置见决策 ②）。
> 且已确认 `rev-parse` 是唯一形态：`"git"` / `git log` / `git describe` / `.git/HEAD`
> 在生产代码中除 `rev-parse` 外零命中，故 13 是完整集合而非又一次抽样。

同一进程内可以出现两个不同的「当前 sha」：`/health` 走 `eval.runner`（认注入），
`rls_verify` 走自己的副本（不认注入）。ADR-0011 的安全分层要求验证工具与业务面
同口径——这里不满足。

### 人工记录与文件系统漂移

`data/snapshots/README.md:46-60` 的演进表停在 `9749fc5`（P7，2026-09-04），
文件系统实际有 17 份，2026-09-09 的 6 份（`7051ef6` `40b71e2` `160795d`
`5d1e22b` `26e7694` `a11d779`）**无记录**。README:43 声称「被问数字怎么来的，
第一答案是绑定到快照 sha xxx」，但按 README 找不到这 6 份的来历。

> **实测更正（2026-09-14 工作项 5 复核文档判据时）——「6 份」应为 8 份**：逐份比对
> `data/snapshots/*.meta.json` 的 sha 与 README 全文提到的 7 位 hex 集合，未记录的是
> 上述 6 份（均 2026-09-09）**另加 2 份**：`b933e20`（2026-09-04T15:12，29 表——
> **恰好也在 `9749fc5`(13:58) 之后**，本段原句「演进表停在 9749fc5」其实已经蕴含它
> 无记录，只是计数时按日期分桶漏掉；而它正是决策 ④ 删掉的 Dockerfile 默认值），
> 以及 `b47a6c1`（2026-09-02T15:45，25 表，组 2 成员——**早于** README 该节标题声明的
> 「2026-09-04 起记」，按表自身口径不算漏记，但文件确实在盘上，收口时要决定是补记
> 还是注明「更早的 1 份见 git log」）。
>
> **同时暴露文档判据第 1 条的度量本身不可满足**（原文要求「`ls | wc -l` 与表内条目数
> 一致」）：README 全文还提到 `9cf70c7` / `b2a2e5c` 两个 7 位 hex，而它们是**commit 短
> sha**（README 原句「中间 9cf70c7/b2a2e5c/92033c9 语义与样本批次未动数据」），
> 盘上没有对应 meta——「7 位 hex」在 README 里同时承担「快照名」与「commit」两种身份，
> 数量相等式判据永远对不上。正确判据是**单向集合覆盖**（见验证方式段的改写）。

约束：

- **N6 不可放宽**：评测必须绑固定 sha。本 ADR 只动**运行时**（`/ask`、`atlas ask`、
  `atlas query`）的 meta 选择规则，评测侧（`eval/runner.py:321-328`）一行不改。
- **N9**：新增的 `ATLAS_SNAPSHOT_SHA` 是身份标识不是密钥，可进 `.env.example`。
- `make lint` 不得破：`semantic/lint.py` 与 `governance_validate.py`（口径 D）是
  lint 路径的一部分，改动需保持其行为。
- **§8**：删除 10 个生产副本属跨 5 个目录的重构，必须独立 `refactor` 提交，
  不得与 `feat`（前端）混合。

---

## 备选方案

| 方案 | 优势 | 劣势 |
|---|---|---|
| **运行时按 created_at 取最新 + HEAD 优先 + 显式回显（选定）** | 与 `semantic/lint.py` 既有正确口径收敛；HEAD 有 meta 时行为完全不变（可复现优先）；无 meta 时不再 503，且 UI/响应显式暴露 `bound_to_head=false` 供人判断 | 「最新」不等于「HEAD」时，运行时数字与该 sha 的评测数字不可直接互引，必须靠回显与文档纪律约束（代价段 ③） |
| 保持 HEAD 严格，改为「commit 后自动重锁快照」（git hook / CI） | 口径单一，无需回显字段 | 每次 commit 都要连 Polaris 读 29 表 manifest（`data/snapshot.py` 成本）；hook 可被 `--no-verify` 绕过；CI 无 DB（`lint.yml` 不起 compose）→ 自动化点其实不存在；且会产生大量指纹相同的 meta 噪声 |
| 运行时也走 `verify_snapshot()` 指纹复核，漂移即拒 | 最严格，能发现「数据被改但没重锁」 | 每次 `/ask` 都要全表 scan count（29 表 pyiceberg manifest 读），把毫秒级问答变成秒级；且运行时查询的不是评测基准，N6 并未要求 |
| 只修 `factory.py` 回退 `sorted[-1]`（与 `cli.py` 对齐） | 改动最小，一行 | 把错误口径（字典序）扩散到运行时权威路径；不解决身份副本分裂、容器默认值、域一致性三类问题；docstring 的谎言保留 |
| 前端自带快照选择器（UI 下拉选 sha） | 灵活，可对照多快照 | 引入「用户选错基准」的新失效模式；与 N6 的「基准唯一」精神冲突；且需要新增写/选择类端点，超出 ADR-0018 的只读消费面定位 |

---

## 决策

### ① 运行时快照解析：三级优先，单一函数

新增 `data/identity.py`（**仅 stdlib**：`os` / `subprocess` / `json` / `pathlib` /
`dataclasses`），提供运行时唯一的快照解析入口：

```python
def resolve_runtime_snapshot() -> RuntimeSnapshot:
    """运行时（非评测）快照绑定：显式 > HEAD > 最新已锁。

    Returns
    -------
    RuntimeSnapshot(sha, meta, source, bound_to_head)
      source ∈ {"env", "head", "latest"}；bound_to_head = (source != "latest")
      ↑ [已纠正] 该公式在 env 级会谎报，落地改为 (sha == HEAD)，见实施裁定 1

    Raises
    ------
    SnapshotUnavailable
      指定的 ATLAS_SNAPSHOT_SHA 无对应 meta；或 data/snapshots 无任何 meta。
    """
```

优先级（**从高到低，命中即止**）：

1. **`ATLAS_SNAPSHOT_SHA`**（环境变量，显式指定）→ `source="env"`；
   指定的 sha 无 meta 时**响亮失败**（不回退——显式意图被静默改写是更坏的失效模式）；
2. **HEAD**（`git_short_sha()`，认 `ATLAS_GIT_SHA`）有 `{sha}.meta.json` → `source="head"`；
   **与现行为完全一致**，可复现优先；
3. **最新已锁**（`max(metas, key=created_at)`）→ `source="latest"`，`bound_to_head=False`；
4. 全无 meta → `SnapshotUnavailable`（保持 ADR-0012 的 503 / CLI exit 1 语义）。

「最新」的键**必须是 `created_at`**，不得用文件名（收敛到 `semantic/lint.py:113-121`
的既有正确实现）。`created_at` 由 `data/snapshot.py` 生成，格式固定
ISO 8601 `+08:00`（AGENTS.md §7.3），定长字符串字典序 == 时间序；解析处仍加断言
（非空 + 可被 `datetime.fromisoformat` 解析），格式异常即报错而非静默选错。

**实施裁定（2026-09-14 工作项 3 落地时补；初稿只给了优先级与返回类型，以下九条是
落地时必须替它决定、而 ADR 不写清就会被各实现者各自决定掉的点）**：

1. **`bound_to_head` 按「解析出的 sha == 当前 HEAD」比较，不按 `source != "latest"`**。
   初稿 spec 里的原公式在 `source="env"` 时恒为 `True`，而决策 ③ 给出的补救手段
   正是「用 `ATLAS_SNAPSHOT_SHA` 指定含这些表的快照」——即显式指定一个**不等于 HEAD**
   的 sha，此时该字段（ADR-0018 决策 ③ 的 UI 警示信号）会谎报。实测：HEAD=`ccb4c8b`、
   设 `ATLAS_SNAPSHOT_SHA=a11d779` → 原公式 `True`、现实现 `False`。对 `head`/`latest`
   两级两种口径逐字等价，差别只在 env 级。判据 1 的表述随本条同步（见验证方式）。
2. **空值等同未指定**：`ATLAS_SNAPSHOT_SHA=""` 不得当成「显式指定了空 sha」。与
   `git_short_sha()` 对 `ATLAS_GIT_SHA` 的 `.strip()` 对称；不是宽容，是因为
   `.env.example` 每行都以 `KEY=` 结尾，否则照抄示例的人必然得到「指定的快照无 meta」。
3. **HEAD 不可解析不是「无快照」**：`git_short_sha()` 抛 `CalledProcessError` / `OSError`
   时继续走第三级，`bound_to_head=False`。理由：镜像内没有 `.git`，身份缺失属决策 ④
   的构建期职责；运行时把「身份不可知」升级成 503 会重演本 ADR 要修的那个症状。
   对诚实性信号，保守的一端是**不声称绑定 HEAD**，而不是崩。
4. **校验范围 = 被读到或被比较的 meta**：第三级要比较全部候选，故逐份校验；
   env / HEAD 级命中后不因目录里另有一份坏 meta 而失败。反过来断言会让一份手误的
   历史 meta 冻结所有查询。评测侧的全量指纹复核（`verify_snapshot()`）是另一条线，
   分工不同，不要拿它来要求运行时路径。
5. **文件名 stem 与内容 `sha` 必须相等**（判据未要求，实现时新增的断言）：这是
   决策 ② 「同一件事不得有两个来源」在 meta 上的直接体现——不校验则 `/health` 回显的
   sha 与 meta 自述可以互相矛盾而无人报错。实测 `data/snapshots/` 现有 **17 份全部
   一致**（2026-09-14 逐份比对文件名与内容），故该断言**零行为风险**，留着只为挡住
   将来有人手改其中一处。
6. **回显格式单一来源**：`RuntimeSnapshot.describe()` 输出
   `sha=… source=… bound_to_head=true|false created_at=…`（布尔用小写与 `/health`
   的 JSON 同形）。四处接入点各写一遍 f-string 的代价是：改一处其余三处静默分叉，
   而代价 ③ 说非 HEAD 绑定的**唯一**约束就是回显——格式分叉等于约束打折。
7. **回显落点**：`atlas query` 的 `[snapshot]` 行走 **stderr**（`--format json` 的
   stdout 是机读契约，扩键属决策 ⑥，不在本工作项）；三个 `*_verify` 工具除 stdout
   一行外，报告 JSON 增 `snapshot_sha` / `snapshot_source` / `snapshot_bound_to_head`
   三键，HTML 证据物同屏显示绑定行。加这三键的理由是代价 ③ 明令禁止的那件事：报告
   **文件名用代码 HEAD**，而数字绑在解析出的快照上，不写清就会出现「以
   `<HEAD>.json` 命名却被读成 HEAD 的评测结果」的互引。此系决策 ⑥ 回显面在验证工具
   上的延伸，不新增对外 HTTP 契约。
8. **配置面必须把变量透进容器**：`.env.example` 增 `ATLAS_SNAPSHOT_SHA` 与
   `ATLAS_GIT_SHA`（二者都是身份标识，不触及 N9），`docker-compose.yml` 的 atlas-api
   增 `ATLAS_SNAPSHOT_SHA: ${ATLAS_SNAPSHOT_SHA:-}`。若不透传，决策 ③ 的错误建议
   （「或用 `ATLAS_SNAPSHOT_SHA` 指定含这些表的快照」）在容器内**不可执行**——
   错误消息给出的是一个走不通的出路。
9. **口径 D 不并入（登记残留）**：`semantic/lint.py:114-122` 的 `_latest_snapshot_meta`
   与本函数的第三级是**同一规则的两份实现**。不合并的理由：lint 要的是「仓库里最新的
   那把锁」，不该吃 `ATLAS_SNAPSHOT_SHA`/HEAD，且约束节明写 `make lint` 行为不得变。
   两份实现现存差异（如实记下，将来收敛时以本条为准）：lint 用 `str(created_at)` 比较、
   不做非空/可解析/`+08:00` 三项断言，缺字段时按空串参与排序。

**评测路径不使用本函数**：`eval/runner.py:321-328` 保持 HEAD 严格 + `verify_snapshot()`
指纹复核原样不动（N6）。反向边界同样成立：`data/identity.py` 不调 `verify_snapshot()`
（备选方案第 3 行已否决，且会拉入评测侧依赖）——两条边界都由判据 10 锁死。

**`SnapshotUnavailable` 的归属（2026-09-14 实施时裁定，初稿未写）**：判据 1 要求本函数
抛 `SnapshotUnavailable`，而该异常现定义在 `agent/factory.py:24` —— 二者与决策 ② 的
「identity 仅 stdlib」相冲：`agent/factory.py:21` 顶层 `from agent.graph import DataAgent`
→ 拉入 langgraph，identity 若反向 import 就会让 `make lint` 与全部验证工具背上图编排
依赖（正是决策 ② 否决「留在 `eval/runner.py`」时用的同一条理由）。

裁定：**异常类一并移入 `data/identity.py`**（`Exception` 是 builtin，不破 stdlib 约束），
`agent/factory.py` 改为**显式 re-export**，三处既有消费方（`agent/cli.py:47`、
`serving/api.py:60`、`tests/test_api.py:27`）逐字零改动。否决的备选：
① identity 抛新异常、由 factory 捕获转译 —— 同一失效模式出现两个类型，catch 不全即
漏成 500，与「单一事实源」相悖；② 异常放 `data/snapshot.py` —— 该模块顶层
`from data.loader import`（拉 pyarrow + pyiceberg），比 factory 更重。
`X as X` 的显式形式不是风格选择，理由见判据 5(c) 的 mypy 踩坑实录。

### ② sha 身份单一事实源，删除 10 个生产副本

`git_short_sha()`（含 `ATLAS_GIT_SHA` 优先）与 `SNAPSHOT_DIR` 的权威实现移入
`data/identity.py`；上表 10 个生产副本全部删除，改为 import（9 个直接删，
`semantic/export_dbt.py` 的容错语义按代价 ⑤ 移到**调用点**）。
初稿标题为「删除 8 个副本」，2026-09-14 实测补入 `data/value_profile.py:204`
后为 10，纠正过程见上表注与代价 ⑤ 注。

> **口径消歧（本文两处数字看起来打架，实为两个不同计数）**：背景节实测「含
> `rev-parse` 的生产文件 = 11 个」，本条标题说「删除 10 个生产副本」。差的 1 个是
> `eval/runner.py` —— 它的定义是**移入** `data/identity.py` 的权威来源，不叫「删副本」
> （代价 ⑤ 纠正注 ① 明写「移入而非删除」）。凡后文出现「11 处」均指**定义总数**，
> 「10 处」均指**副本数**；引用时须带限定词，否则又成一次口径漂移。

**归属选择的理由**（三个候选都实测过 import 成本）：

| 候选 | 否决理由 |
|---|---|
| 留在 `eval/runner.py` | 顶层 `import mysql.connector` + `agent.compiler/planner/security`（`runner.py:40-44`）——`serving/*_verify.py`、`metadata/parser.py`、`semantic/export_dbt.py` 会被 DB 驱动与语义层污染；`agent/factory.py:35-36` 现在就用「延迟 import」规避这一点 |
| 放 `data/snapshot.py` | 顶层 `from data.loader import ...`（`snapshot.py:28`）→ `data/loader.py:40-45` 拉入 `pyarrow` + `pyiceberg`，同样过重 |
| **新建 `data/identity.py`（选定）** | 零重依赖；`data/__init__.py` 实测为空文件 → `import data.identity` 不触发 `loader`；概念上「仓库/快照身份」属 `data/` 域 |

**向后兼容**：`eval/runner.py` 顶部改为 `from data.identity import SNAPSHOT_DIR, git_short_sha`
——名字仍在 `eval.runner` 命名空间，既有消费方**零改动**。

**消费方计数（2026-09-14 实测回填，本条初稿称「15 处」不准）**：
`from eval.runner import …` 共 **20 条语句 / 19 个文件**，其中含 `git_short_sha` 的
**12 处**（`agent/factory.py:36`、`serving/api.py:341`、`eval/{api_acceptance:56,
baseline_compiler:30,compare_4way:33,rag_eval:38,report:27}`、`lora/{build_pairs:49,
flywheel:35,infer:136,train:37}`、`tests/test_report.py:30`），含 `SNAPSHOT_DIR` 的
**6 处**。初稿列举的 10 个例子漏了 `agent/factory.py` 与 `serving/api.py` 两处，
且把语句数（20）与含 `git_short_sha` 的语句数（12）混为一谈。**结论不变**（均零改动），
但收口复验时以本段实测值为基准，不得用初稿的 15。

**`SNAPSHOT_DIR` 副本清单（初稿无此表，2026-09-14 实测补，同日二次扩充 5 → 8）**：
决策 ② 已裁定它随 `git_short_sha()` 一同移入 `data/identity.py`，但初稿既未列副本也未给
判据（判据 5 只验 `git_short_sha` 的 `def` 归零）。下表行号均为**改造前 HEAD（ccb4c8b）**
位置，改造后这些行已不存在（可 `git show ccb4c8b:<file> | sed -n '<n>p'` 复核）：

| 定义处 | 行 | 写法 |
|---|---|---|
| `eval/runner.py`（权威） | 49 | `REPO_ROOT / "data" / "snapshots"` |
| `data/snapshot.py` | 31 | `REPO_ROOT / "data" / "snapshots"` |
| `data/value_profile.py` | 52 | `REPO_ROOT / "data" / "snapshots"` |
| `semantic/governance_validate.py` | 43 | `REPO / "data" / "snapshots"`（**锚变量名不同**） |
| `semantic/lint.py` | 33 | `REPO / "data" / "snapshots"`（**锚变量名不同**） |

上表**当时仍不全**：它按 `^SNAPSHOT_DIR *=` 检索，与 `git_short_sha` 初稿按函数名检索
**同型**，因而漏掉了「根本没有变量名」的内联写法。改按路径字面量检索后多出 3 处
（目录直接拼在调用行里，从未成为模块级常量）：

| 定义处 | 行 | 写法 |
|---|---|---|
| `serving/p1_acceptance.py` | 100 | `sorted((REPO_ROOT / "data" / "snapshots").glob(…))` 内联 |
| `serving/metrics_verify.py` | 63 | 同上 |
| `serving/rls_verify.py` | 88 | 同上 |

合计 **8 处副本**（5 具名 + 3 内联），分布在 `data/`、`eval/`、`semantic/`、`serving/`
四个顶层目录。八处值相等，但锚变量名有**三种**形态（`REPO_ROOT` / `REPO` / 无锚内联），
这正是副本漂移的典型形态。合并后全部改为 `from data.identity import SNAPSHOT_DIR`；
判据 5(b) 的检索式随之从「按变量名」改为「按构造形态」（见验证方式段）。

**测试侧 2 处副本的裁定：保留为独立预言机，不合并**（2026-09-14 开工实测发现，
初稿无此裁定）。`tests/test_export_dbt.py:24` 与 `tests/test_demo_e2e.py:55` 各有
一个 `_head_sha()`，同样跑 `rev-parse`，但**不在合并范围内**：

| 处 | 实际用途（实测调用点） | 若改为 import `data.identity` 的后果 |
|---|---|---|
| `test_export_dbt.py:24` | `:133` 断言 `data["sha"] == _head_sha()`，专门抓「导出产物里硬编码 sha」（`:131-132` 注释记该断言生于 2026-09-03 CI 修复） | 断言变成同义反复：产物值与被断言值同源于一个函数，该函数自身若返回常量则测不出来 |
| `test_demo_e2e.py:55` | `:69` 的 skip 判据与 `:119` 读 meta，语义是 **HEAD 严格**（HEAD 无 meta 即跳过），与评测口径同侧 | 换成 `resolve_runtime_snapshot()` 会把「最新已锁」回退引入测试前置，使 skip 判据不再等价于「HEAD 有 meta」 |

否决的备选（合并测试侧）理由：测试不与服务同进程，本 ADR 要消除的失效模式
（「同一进程内出现两个不同的当前 sha」，见上文背景节）在测试侧不成立；而合并
会实质削弱上述两处断言强度。**取舍依据 AGENTS.md §10 第 1 条（诚实优先于简洁）**。

**代价（登记，不掩盖）**：这 2 处不认 `ATLAS_GIT_SHA`，故**在容器内跑 `make test`
会失败**（`test_export_dbt.py:31` 的 `assert out.returncode == 0` 在无 `.git` 的镜像内
直接断言失败）。当前不构成问题：`make test` 只在宿主机执行，注入点实测只有
`Dockerfile:34` 一处，`.env` 与 `.env.example` 均无 `ATLAS_GIT_SHA`。若将来要在容器内
跑测试，这 2 处须改为「认 env 但仍独立解析」。

**纪律**：这 2 处必须带**显式注释**声明是刻意保留的独立预言机——否则下一个人会按
「副本归零」把它当漏删的副本「修掉」，从而静默削弱断言。判据 5(e) 验该注释存在。

### ③ 域-快照一致性：启动期校验，不等运行时被 Guard 拒

`create_live_agent(model_path)` 在 `build_budget(meta)` 之后、构造 `DataAgent`
之前校验：

```python
required = {ds.source for ds in model.datasets.values()}      # SemanticModel.datasets（compiler.py:151,173）
missing = required - budget.allowed_tables
if missing:
    raise SnapshotUnavailable(
        f"绑定快照 {sha}（{source}）缺少语义模型 {model.name} 所需的表：{sorted(missing)}"
        "——请重锁快照（make seed）或用 ATLAS_SNAPSHOT_SHA 指定含这些表的快照"
    )
```

把「容器零售域每次查询被 Guard 拒」提前为「retail agent 构建即 503，错误消息
直接列出缺失表与绑定 sha」。校验成本是一次集合差（模型 datasets 4~8 个），
在 agent 懒建单例路径上只执行一次（`serving/api.py:324-335`）。

**不校验行数**：`row_counts` 的绝对值不参与 Guard 判定，只用于指纹；把它纳入
启动校验会让「同数据多锁」的正常演进变成启动失败。

**实施裁定（2026-09-14 工作项 4 落地时补；初稿给了代码形状，以下三条是落地时
必须替它决定、而 ADR 不写清就会被各自决定掉的点）**：

1. **`model_path=None` 分支必须在本函数内显式构造 `SemanticModel()`**。初稿的代码形状
   没写 None 怎么办，而 `DataAgent.__init__` 是 `self.model = model or SemanticModel()`
   兜底（`agent/graph.py:566`）——若沿用改造前的「传 None 进去」，校验就只能覆盖
   显式传路径的 HTTP 侧；而 `agent/cli.py:126` 的 `create_live_agent()` **恰好不传参**，
   即背景节里最常用的那个入口会整条绕过防线。判据 6 未写这一条，故补
   `test_default_model_is_checked_too`。
2. **校验先于 `DataAgent` 构造**，判据断言的是「失败时一次都不构造」（`assert_not_called`）
   而非「构造后仍抛」——后者的实现同样能让「抛异常」这条判据通过，却把配置错误的
   暴露点重新推回运行期，正是本决策要修的形状。
3. **落点只有 `create_live_agent`，`atlas query` 的确定性路径不在保护内（登记残留，
   不在本工作项扩大）**：它不构造 agent，走 `_load_budget()` + 直接 `enforce()`
   （`agent/cli.py:336,383`）。同一 sha 实测：

   ```
   $ ATLAS_SNAPSHOT_SHA=7d48dcb atlas query "2001 年销售额" --domain retail
   [snapshot] sha=7d48dcb source=env bound_to_head=false created_at=2026-09-03T09:53:50+08:00
   [blocked] UnsafeQuery: 表不在白名单内：atlas.dwd.store_sales
   ```

   即症状原样保留；工作项 3 加的 `[snapshot]` 回显行让 sha **可见**（部分缓解归因），
   但仍没有「这些表是模型声明的 → 绑错快照」那一步推断。不合并的理由：给一条
   不构造 agent 的路径加校验，需要再抽一份 shared helper 并决定它在 CLI 里是
   warning 还是 exit——那是新决策，不是本决策的实施细节。

### ④ 容器身份不给默认值，构建期注入

删除 `Dockerfile:33` 的 `ARG GIT_SHA=b933e20` 默认与 `docker-compose.yml:211`
的 `${GIT_SHA:-7d48dcb}` 默认，改为**无注入即构建/启动失败**，并把注入点收到
`make`（`GIT_SHA=$$(git rev-parse --short HEAD)`）。

理由：任何硬编码的 sha 默认值都会随 HEAD 前进变成谎言（N2），而两处默认值不同
已经实测造成了「注释说 b933e20、实际生效 7d48dcb、零售域不可用」。响亮失败优于
静默错绑。同时删除 Dockerfile:11 那句与实际不符的注释。

**实施裁定（2026-09-14 工作项 5 落地时补；初稿说「无注入即构建/启动失败」，落在哪
一层、失败多快、谁能覆盖，都必须在实施时定死）**：

1. **必填校验只在构建期，compose 用 `${GIT_SHA:-}` 而非 `${GIT_SHA:?}`**。
   实测（docker compose 29.5.2，最小复现文件）：`:?` 在 `config`、`ps`、`down` 三个
   子命令上**一律**报 `required variable GIT_SHA is missing a value` ——compose 的变量
   插值发生在任何动作之前，于是「停服」也需要身份，这是本决策新造的运维陷阱
   （镜像已跑着却停不下来）。改由 Dockerfile 的构建守卫承担响亮失败，compose 只透传。
   该选择由 `test_stop_and_inspect_work_without_identity` 锁死（变异验证 M2：改回
   `:?` 即红）。
2. **守卫排在依赖层之前**（`FROM` 之后、`COPY pyproject` 之前），实测真构建
   `docker build`（无 `--build-arg`）退出码 1 且日志中 `uv sync` / `COPY pyproject`
   出现 **0 次**——放在文件尾会让每次误构建先装几分钟依赖才知道身份不对。
   ARG 在整个 stage 内有效，故 `ENV ATLAS_GIT_SHA=${GIT_SHA}` 留在原位（ENV 本身是层，
   提前会让身份变化击穿依赖缓存）。
3. **注入点 `Makefile: GIT_SHA := $(shell git rev-parse --short HEAD 2>/dev/null)`
   \+ `export`**。三处细节各有理由：`$(shell)` 必须在 make 侧求值（否则 compose 拿到
   空值）；`export` 是因为 compose 是 make 的子进程，不 export 的写法在文件里看着
   一样、行为上完全没注入；`2>/dev/null` 是无 `.git` 环境下不让 `fatal:` 污染**每一次**
   make 调用（实测无它时 `make help` 都刷一行），此时值为空，失败点仍在构建守卫。
   `:=` 保留命令行覆盖能力（`make up GIT_SHA=<sha>`，复现旧镜像身份用）。
4. **配置文件不留 sha 值，连注释也不留**：回填过程中自己踩到的——第一版 compose 注释
   写了「原先默认 7d48dcb 而 Dockerfile 默认 b933e20」，被本工作项的判据判红。裁定：
   历史值属于本 ADR 背景节（那里才是唯一事实源），部署文件只写规则与指向，否则又造出
   第二个 sha 来源。Dockerfile 的断言因此用**通用式**（不得出现任何 7 位 hex），
   而不是枚举那两个已知值。
5. **运行时侧不因此失败**：镜像能存在就说明守卫放过、`ATLAS_GIT_SHA` 非空；容器内
   无 `.git` 时 HEAD 不可解析的情形已由决策 ① 实施裁定 3 归入第三级。**残留（登记，
   不在本工作项解决）**：镜像内的身份是**构建期常量**，`make up` 不带 `--build` 时
   复用旧镜像 → 身份停留在旧 HEAD，运行期无从发现。可见化属决策 ⑥ 的回显面
   （`/health` 的 `head_sha` 即该常量），要与宿主真实 HEAD 对照需另加运行时注入通道。

### ⑤ N6 辨析：本 ADR 不放宽评测基准

N6 的原文是「把 `data/snapshots/` 外的数据库当作**评测基准**」。本 ADR：

| 路径 | 口径 | 是否受本 ADR 影响 |
|---|---|---|
| `make eval`（EX / result_hash / 报告文件名） | HEAD 严格 + `verify_snapshot()` 指纹复核 | **不变** |
| gold 样本锚定（`anchor_hash` / `anchor_snapshot_sha`） | 写入当次评测的 HEAD sha | **不变** |
| 值域画像 `snapshot_sha`（ADR-0016） | 必须属于已锁集合（口径 D） | **不变** |
| `/ask`、`atlas ask`、`atlas query` 的**表白名单预算** | 三级优先（决策 ①） | **本 ADR 放宽** |

放宽的**只是**「运行时查询允许触碰哪些表」的选择依据，数据源仍然是
`data/snapshots/` 内的已锁快照——不存在「用未锁的漂移数据库当基准」的情形。
补偿措施是决策 ⑥ 的强制回显：任何非 HEAD 绑定都必须对用户可见。

### ⑥ 强制回显 + 文档补齐

**`/health`（公开面）扩字段**（`serving/api.py:337-345`，**改造前 HEAD 的行号窗口**；
本决策落地后该函数已增长并移位，复测请按键集而不是行号，定位命令见 dev-plan §0 起点表）：

```json
{
  "status": "ok",
  "head_sha": "bdcb6c0",
  "snapshot_sha": "a11d779",
  "snapshot_source": "latest",
  "snapshot_bound_to_head": false,
  "snapshot_created_at": "2026-09-09T11:56:23+08:00",
  "snapshot_tables": 29
}
```

`snapshot_sha` 的语义由「HEAD 是否恰好有 meta」变为「运行时实际绑定的 sha」；
`snapshot_bound_to_head=false` 是**必须展示**的信号（ADR-0018 决策 ③ 的 UI 消费点）。
既有两个键保留原名与原类型（`str | None`→`str`，仅在无 meta 时才为 `None`），
`docker-compose.yml:230-231` 的 healthcheck 只探 URL 不解析 body，不受影响。

**`/ask` 响应**增 `snapshot_sha` + `snapshot_bound_to_head`（与 `explanation` 并列，
不塞进 `explanation` 的 **13 个固定键**内——实测 `agent/graph.py:383-400` 的
`node_explain`：`metric` / `metric_expression` / `dimensions` / `time` / `filters` /
`sql` / `tables` / `row_count` / `latency_ms` / `path` / `engine` / `data_version` /
`data_refreshed_at`；策略生效轮额外追加 `policy_effect`）。

诚实注记（修正本 ADR 初稿的口径）：这 13 键**并未被契约测试锁定键集本身**——
`tests/` 实测只对其中 11 个键做逐键断言（`grep -rho 'explanation\["[a-z_]*"\]' tests/`
去重得 11 项，含条件键 `policy_effect`；`sql` 与 `engine` 无任何断言），
无 `assertEqual(set(explanation), …)` 形态的键集断言。故「塞进 `explanation` 会
破坏契约测试」不成立，真正的理由是**语义归属**：快照绑定状态是回合级事实
（与 `kind` / `row_count` 同层），不是归因解释的一部分；且 `_turn_payload`
（撰写时 `serving/api.py:183-213`，**行号只作历史坐标**：该 `def` 在 HEAD 确为 `:183`，
工作项 6 给它加了 `snapshot` 形参与两键后函数体增行，现漂到 `:193`；
定位用 `grep -n "def _turn_payload" serving/api.py`）的字段全集恒定
原则要求新键在顶层显式出现。
P-1 落地时若要把 13 键升级为锁定契约，需**新增**键集断言（本 ADR 不假设它已存在）。

**`data/snapshots/README.md`**：补齐 2026-09-09 的 6 份（**「6 份」是少计，实测 8 份**，
见下方实施裁定 7 与「文档判据」第 1 条），并按实测改为「指纹组」
记法（组 1 = 12 份双源 29 表 / 组 2 = 5 份单源 25 表），追加一行纪律：
「新增 meta 必须同批更新本表；`created_at` 是运行时选最新的唯一键」。

**实施裁定（2026-09-14 工作项 6 落地；括注数字为当时实测，收口须复跑，见判据 13）**

上方示例 JSON 是**本决策自己的 7 键**；落地时叠上 ADR-0020 决策 ⑦ 的 `boot_id`
→ `/health` 终值 **8 键**。键集的权威全文清单在 ADR-0022 决策 ①（它自己声明
「不再增删键」，故 0022 是引用方、本决策与 0020 是定义方）。逐条落地裁定：

1. **回显值只能来自解析函数**：`head_sha` 取 `RuntimeSnapshot.head`（同一次解析的产物），
   不在端点里再跑一次 `git_short_sha()`——否则 `head_sha` 与 `snapshot_sha` 是两次读取，
   之间 commit 一次就能让响应自相矛盾（`head_sha == snapshot_sha` 而
   `bound_to_head=false`）。断言方式是「把 `resolve_runtime_snapshot` 换成注入值，
   响应必须跟着变」，且必须**逐键**断言：首轮变异验证里 `head_sha` 恰恰没被断言，
   「端点自己再解析 HEAD」这条变异在静止仓库上**完全不可见**——本 ADR 第六次
   「测试通过 ≠ 契约成立」，也是第一次由**变异验证自身**暴露判据缺陷（前五次都是
   检索口径小于裁定口径）。
2. **降级路径同键集**：无快照可绑时 `status="degraded"`、绑定各键为 `null`，HTTP 仍
   200（探针语义：存活面不因数据面缺配置而 5xx），**键数不得从 8 缩成 4**——
   字段全集恒定优先于「少报坏消息」。
3. **`snapshot_tables` 的口径 = Guard 白名单**：与 `eval.runner.build_budget(meta)`
   的 `allowed_tables` 元素数对**全部真实 meta** 逐个断言相等（回显 29 而只放行 25
   就是误导，且这类不一致靠注释保证不住）；`row_counts` 缺键或形态不对时**抛错 →
   degraded + `snapshot_tables=null`**，不静默给 0（回显「这快照没表」同样是假话）。
4. **`DataAgent` 的绑定单来源守卫**：`snapshot.meta` 与 `snapshot_meta` 不一致时
   构造期抛 `ValueError` 且消息**点出两份 sha**。缺本条则 Guard 白名单来自 A、
   `/ask` 回显的 sha 来自 B，响应会自信地指错快照——这是决策 ② 的「单一事实源」
   在构造签名上的落点。守卫不得过宽：同一份 meta 传两次仍须放过。
5. **503 前缀文案改写**（判据 6 末登记的残留就此关闭）：「无法绑定评测数据」→
   「无法绑定锁定快照，本轮查询不可执行：{exc}」。绑错快照 ≠ 无评测数据，且
   「评测数据」在 N6 语境特指 `eval/runner` 的 HEAD 严格路径，原案会把人引到错误的
   现场。**代价（即代价 ⑥ 预告的那次同步）**：`tests/test_api.py` 一处既有断言
   `assertIn("快照不可用", detail)` 随之失效，改为断言**原始异常消息必须被带出**并
   指向本文件的文案契约用例——改断言而不改文案，才是本条的正当方向。
6. **CLI 面补齐（实施裁定 7 的落点清单不完整，落地时才发现）**：裁定 7 只登记了
   `atlas query` 与三个 `*_verify` 工具，**`atlas ask` 从未在列**——它同样是运行时入口
   （决策 ⑤ 的放宽表里就写着它），不回显即等于「放宽只在 HTTP 面可见」。现补上，
   绑定行与 `cmd_query` 同格式（裁定 6 的 `describe()`）、同流（**stderr**）：
   stdout 是可机读产物，诊断行混入即破坏该前提，故「走 stderr」本身也有断言，
   不只是「出现了这行字」。
7. **README 补记的份数更正**：上方「补齐 2026-09-09 的 6 份」是**少计**——实测缺记
   **8 份**：09-09 当日 6 份（`7051ef6` `40b71e2` `160795d` `5d1e22b` `26e7694`
   `a11d779`），另有 `b47a6c1`（09-02）与 `b933e20`（09-04）。补后盘上 17 份全覆盖。
   该条现在是**自动化判据**而非人工点数，见「文档判据」第 1 条。

**本决策未覆盖的部分**（2026-09-14 工作项 6 收窄一次）：真链判据 7 的 **`/ask` 半句**与
判据 8（需 Doris，留 P-1 收口）—— 该判据的 `/health` 半句不需 Doris，**本工作项已在
宿主机真机验通**（见判据 7 落地注），不再算未覆盖；`tests/` 里 7 个文件
写死组 2 meta 的耦合（代价 ①，属 P0a）；以及「非 HEAD 绑定不得被写进 HEAD 命名的
报告」——回显只做到可见，见代价 ③。

> **收口更新（2026-09-16）**：真链判据 7 的 `/ask` 半句与判据 8 均已验通（见判据 7/8
> 各自的收口落地注）——本决策的未覆盖项只剩后两条（7 文件组 2 meta 耦合属 P0a；
> 「回显只做到可见、无技术强制」的风险判断不变，见代价 ③）。

---

## 理由

1. **收敛到仓库内已存在的正确实现**，而不是发明新规则：`semantic/lint.py:113-121`
   已按 `created_at` 取最新。四处 `sorted[-1]` 是复制粘贴疏漏，且 docstring 声称
   「取最新」——按 N2，声称与实现不符本身就是债务。
2. **HEAD 优先保证零行为变化**：只要维护者按纪律重锁快照，运行时与今天完全一致；
   放宽只在「HEAD 无 meta」这个**本就 503 的窗口**里生效。也就是说本决策
   **不可能让原本可用的路径变得不可用**，只会把不可用变成可用 + 显式标注。
3. **启动期校验优于运行期拒绝**：Guard 的 `表不在白名单内` 是安全语义，不该被
   用作「快照绑错了」的诊断通道。决策 ③ 让配置错误在第一次构建 agent 时就带上
   缺失表清单暴露，而实测证明这个错误今天就存在于运行 10 天的容器里。
4. **身份副本必须合并**：ADR-0011 的安全分层要求验证工具与业务面同口径；9 个副本
   中 8 个不认注入，等于容器内验证工具无法运行（`CalledProcessError`）。合并到
   零依赖模块，既不污染 `serving/`，也不破坏 15 处既有 import。
5. **不给默认 sha**：与 `make adr` 缺 slug 时响亮报错同理（`infra/adr/__main__.py`）——
   译名/默认值这类「猜一个看起来合理的值」的做法正是 N1/N2 要禁止的。

---

## 代价与限制

① **7 个测试文件硬编码 `7d48dcb`（组 2，单源 25 表）**：`tests/{test_api,
test_api_hardening,test_chart,test_feedback,test_graph,test_mcp_server,
test_tools_registry}.py` 用它构造 `_META` / `Budget`。这意味着**契约测试的白名单
不含零售表**——P0a 给 `render_chart` 接零售时间序列时，`tests/test_chart.py`
必须换用组 1 的 meta（或注入合成 meta），否则零售用例会被白名单挡在测试之外。
本 ADR 不改这 7 处（属 P0a 范围），但登记为已知耦合。

② **`ATLAS_GIT_SHA` 使身份与代码解耦**：容器实测代码含 retail 域（`DOMAIN_MODEL_PATHS`
有 `retail` 且能进 Planner），而注入身份 `7d48dcb` 的白名单无零售表。决策 ③ 是
唯一防线；若将来有人为「让服务先起来」而放宽 ③，这个失效模式会立刻回归。
**不得放宽。**

> **状态更正（2026-09-14 工作项 4）**：③ 已从「待建」变为「已建」，回归会被
> `tests/test_domain_snapshot_consistency.py` 8 例打红（三条细则各有独立变异验证，
> 见判据 6 落地状态）。但本条的**风险判断不变**：③ 只覆盖构造 `DataAgent` 的路径
> （`/ask` 与 `atlas ask`），`atlas query` 的确定性路径仍只靠 Guard（实施裁定 3
> 登记的残留）。「唯一防线」因此仅在那两条入口上成立。

③ **`source="latest"` 时，运行时数字与该 sha 的评测数字不可互引**：运行时绑定
`a11d779` 的表集，但 HEAD 是 `bdcb6c0`；此时得到的数值不能写进任何以
`bdcb6c0` 命名的报告，也不能与 `eval/reports/a11d779.json` 的 EX 并列陈述
（口径不同，N1/N10 精神）。约束靠回显字段 + 本节文字，**没有技术强制**——
这是本决策最主要的诚实性风险。

> **落地进度（2026-09-14）**：三个 `*_verify` 工具的产物已带 `snapshot_*` 三键
> （实施裁定 7），即「报告文件名 = 代码 HEAD / 数字绑在另一份快照」这种互引现在
> **在文件里就能看出来**；`/health` 与 `/ask` 的对外回显键**已落**（2026-09-14 工作项 6，
> 决策 ⑥ + 判据 13）——它让「绑在哪」可见，但不改变本条的风险判断。
> 「没有技术强制」这句**仍然成立**：没有任何脚本会因为 `bound_to_head=false`
> 而拒绝把数字写进以 HEAD 命名的文档。

④ **`created_at` 格式假设**：定长 ISO 8601 `+08:00` 才能用字符串比较。若将来
`data/snapshot.py` 改为 UTC 或可变精度，字典序与时间序会脱钩。决策 ① 的解析期
断言只能发现「不可解析」，不能发现「时区不一致但仍可解析」。补偿：断言里加
「必须以 `+08:00` 结尾」（AGENTS.md §7.3 的显式时区要求）。

⑤ **跨 5 个目录删副本**：`serving/`(4) + `eval/`(2) + `data/`(2) + `metadata/`(1)
+ `semantic/`(1) = **10 处副本文件**（初稿写 `data/`(1) = 9 处，漏 `value_profile.py`），
属 `refactor` 提交（§8 禁止与 `feat`/`semantic` 混合）。`semantic/export_dbt.py:279`
的副本有 `except → "unknown"` 的容错语义（非 git 环境也能导出），合并后必须保留
该容错（**在调用点而非实现内**）。

> **文件改动总数纠正（2026-09-14 实测；本注自身也经两次纠正）**：初稿的「9 处文件
> 改动」**严重不完整**，漏了三类：① `eval/runner.py` 自身（权威实现，**移入**而非删除，
> 决策 ② 向后兼容段已明写）；② **新建** `data/identity.py`；③ `SNAPSHOT_DIR` 的
> **3 个额外副本文件**（`data/value_profile.py`、`semantic/governance_validate.py`、
> `semantic/lint.py`）。**实测准确值 = 14 个文件**（仍跨 5 个目录，初稿该部分正确）：
>
> ```
> A = 生产代码中含 rev-parse 的文件：11 个（按行为检索，不按函数名）
> B = 生产代码中构造快照目录 Path 的文件：10 个（按构造形态检索，不按变量名）
>     其中 8 个需合并，2 个为 N6 例外（eval/{api_acceptance,e2e_acceptance}.py
>     点名固定 sha，本就该写死，见决策 ② 副本清单与判据 5(b)）
> A ∩ B（需合并部分）= 6 个（data/{snapshot,value_profile}.py、eval/runner.py、
>     serving/{metrics_verify,p1_acceptance,rls_verify}.py）
> A ∪ B（需合并部分）= 11 + 8 - 6 = 13 个既有文件需改，+ 新建 data/identity.py = 14
> ```
>
> 复现命令（不依赖人工计数，收口时重跑；**两项都必须按行为检索，不得按命名**）：
> `grep -rl --include='*.py' 'rev-parse' . | grep -v '\.venv' | grep -v '^./tests/'`
> 与「按路径构造形态检索快照目录」（见判据 5(b) 的口径说明）的文件集并集，再减去
> 2 个 N6 例外，最后 +1（新建 `data/identity.py`）。
>
> **三次教训（同一形态，递进）**：
> (i) 初稿只按**单一符号**（`git_short_sha`）数副本，而决策 ② 实际要合并**两个**符号；
> (ii) 本注第一版改为按 `def git_short_sha` / `def _git_short_sha` 检索（仍按**函数名**），
>      于是又漏掉 `data/value_profile.py:204 head_sha()`——A 集算成 10 而非 11；
>      且 ③ 里「`value_profile.py` 无 `git_short_sha` 定义」这句当时字面为真、却是**误导**
>      （它有自己的同逻辑副本，只是名字不同）。
> (iii) 本注第二版把 B 集写成「`SNAPSHOT_DIR` 定义所在文件：5 个」——**仍是按变量名**
>      检索，于是漏掉 `serving/` 三处从未成为模块级常量的内联拼法，B 算成 5 而非 8。
>      同一次修正里给出的复现命令 `grep '^SNAPSHOT_DIR *='` 因此也是错口径的。
>
> 结论：**计数口径必须与裁定口径同源**——裁定说的是「同一份逻辑」，检索就必须按
> 「同一份逻辑的可观测行为」（`rev-parse` / 路径构造形态）而非按命名。同类形态见
> KL #31 第五项 (b)。**并且：并集总数不足以自证**——(ii) 与 (iii) 两次 B/A 分项都算错，
> 而 A∪B 都恰好等于 13、总数都恰好等于 14，机制是「漏检项正好全落在另一个集合里」
> （`serving/` 三个验证工具同时有 sha 副本与内联路径）。**只看并集总数的判据，永远
> 发现不了被漏计的是交集项**——这正是收口时必须分项复验、而不是只核总数的理由。
> 总数正确掩盖归因错误，两次同型。

⑥ **`/health` 响应体扩字段是契约变更**：`tests/test_api.py` / `test_api_hardening.py`
对 `/health` 有断言，需同步；`docker-compose.yml:230-231` 的 healthcheck 只探 URL
（`urllib.request.urlopen`，2xx 即健康），不受影响。字段只增不改名，属向后兼容扩展。

> **同步的实际发生（2026-09-14 工作项 6，本段是对上句的实测更正）**：既有 `/health`
> 调用点共 3 处（分属 2 个测试方法），**没有一处因扩键而变红**——
> `tests/test_api.py:129 test_health_fields`
> 是逐键断言（`status` / `head_sha` / `snapshot_sha` 三键，**无键集断言**），
> `test_api_hardening.py:296-297` 只断言 200。真正变红的唯一一处是
> `tests/test_api.py:318` 的 `assertIn("快照不可用", detail)`，它断言的是决策 ⑥
> 顺带改写的 **503 文案**（见决策 ⑥ 实施裁定 5），与 `/health` 无关。
> 教训与本 ADR 一贯同型：**代价登记的位置要准**——本条把风险押在「键集」上，
> 实际破裂点在「文案」上；按登记去改测试会一处改不到、另一处空改。
> 反过来说，「只增不改名所以向后兼容」这句**当时并无断言支撑**（无人断言键集，
> 扩到几键都不会红），现在由 `tests/test_identity_echo.py` 补上 8 键键集断言
> （含降级路径同键集），以及 compose 探针的反向断言（`docker-compose.yml` 里
> 不出现 `snapshot_sha`，即探针不得开始解析 body）。

⑦ **决策 ④ 会让「不带 GIT_SHA 的 docker compose build」失败**：这是有意的，
但意味着任何自动化构建脚本都必须显式注入。目前仓库内唯一的构建入口是
`docker-compose.yml` + Makefile，无第三方 CI 构建（GitHub Actions 在 gitee 远程下
不执行，见 ADR-0018 决策 ⑥），影响面可控。

⑧ **工作项 2 只归一路径与身份，`[-1]` 口径有意未动**（2026-09-14 实施时登记）：
决策 ② 的落地是纯 `refactor`，把 8 处 `SNAPSHOT_DIR` 构造与 11 处 HEAD 解析定义
（10 处副本 + `eval/runner.py` 权威移入，口径见决策 ② 标题下消歧注）收敛到
`data/identity.py`，但**四处 `sorted(metas)[-1]` 表达式逐字未改**。不同批做的理由：实测
字典序选中 `dc4f350`（`created_at 2026-09-04`）而按 `created_at` 应选 `a11d779`
（`2026-09-09`）——背景节那两个数字今日独立复跑再次得到，**两者确实是不同的 meta**，
改它即改变这 4 个工具实际使用的白名单，属行为变化，必须与决策 ① 的
`resolve_runtime_snapshot()` 同批落地并一起过真链判据（AGENTS.md §8：禁止一个提交
混合 `refactor` 与行为变更）。

本 refactor 行为零变化的证据（收口时复跑）：`SNAPSHOT_DIR` 八处值相等由判据 5(b) 断言；
4 处 `load_budget()` 实测均返回 `dialect=doris / max_rows=10000 / allowed_tables=29`，
与背景节实测的 `sorted()[-1] = dc4f350` 同组（组 1 为 29 表）。待办输入即背景节
「四处的明细」表，工作项 3 逐条替换为 `resolve_runtime_snapshot()`。

> **待办已完成（2026-09-14 工作项 3 落地，本段是复验证据）**：四处全部改走
> `resolve_runtime_snapshot()`，`sorted(metas)[-1]` 在生产代码中归零（三处 docstring
> 仍**保留**该写法作改造前归因说明，属散文，见判据 10 的 code-only 检索口径）。
> 落地后的真实目录解析结果：`sha=a11d779 source=latest bound_to_head=false`，四处
> `load_budget()` 均为 `dialect=doris / max_rows=10000 / allowed_tables=29` —— 与改造前
> 的 `dc4f350` **同属组 1（29 表）**，所以白名单集合完全相同（实测 `table sets
> identical: True`），**本工作项今日数值中性**：它修的是「将来出现组 2 或更小表集时
> 会静默用错白名单」这条风险，不是今天的某个错误结果。字典序 `dc4f350` 与
> `created_at` 序 `a11d779` 的差值在本批第三次独立复跑中重现（背景节 → 工作项 2 →
> 本工作项），说明该缺陷是真的而非抽样噪声。

---

## 什么情况下应该推翻

- **运行时也需要「与评测数字严格同源」**（例如 UI 上要并列展示「本次结果」与
  「该 sha 的评测 EX」）→ 决策 ① 的第 3 级回退必须去掉，回到 HEAD 严格，
  并改用「commit 后自动重锁」路径（备选方案第 2 行）；
- **出现多快照并行对照需求**（同一问句在不同快照上的结果对比）→ 决策 ① 的
  单一解析结果不够，需要请求级 `snapshot_sha` 参数，并重估 N6 与审计口径；
- **`data/snapshots/` 的 meta 数量增长到使 `max(created_at)` 的全量读取变慢**
  （当前 17 份、每份 < 4KB，全读 < 10ms）→ 改为维护单一 `latest.json` 指针文件，
  由 `data/snapshot.py` 锁定时原子更新；
- **`ATLAS_GIT_SHA` 注入通道被移除**（例如镜像内保留 `.git`）→ 决策 ② 的 env
  优先分支可简化，但 9 副本仍应合并；
- **语义模型的 `datasets[].source` 不再等于物理表全名**（例如引入视图层或
  跨 catalog 联邦）→ 决策 ③ 的集合差校验失效，需改为按 Guard 的表白名单口径
  重新推导 required 集合。

---

## 验证方式

**P-1 批次判据（契约测试，无 DB，进 `make test`）**：

1. `resolve_runtime_snapshot()` 四级优先各有用例：设 `ATLAS_SNAPSHOT_SHA` 命中 /
   设了但无 meta → 抛 `SnapshotUnavailable`（**不回退**）；HEAD 有 meta → `source="head"`；
   HEAD 无 meta → `source="latest"` 且 `bound_to_head=False`；空目录 → 抛。
   用 `tmp_path` 构造快照目录，不碰真实 `data/snapshots/`。
   **落地状态（2026-09-14）**：`tests/test_snapshot_resolution.py` 中
   `TestExplicitEnvLevel` 5 + `TestHeadLevel` 1 + `TestLatestLevel` 3 +
   `TestNoSnapshotAtAll` 2 = **11 例**（`TestLatestLevel` 里有 1 例同时是判据 2）。
   三条本判据未写、但实施裁定决定的分支也各有用例：空串 env = 未指定（裁定 2）、
   HEAD 不可解析 → 第三级（裁定 3）、目录里只有非 `.meta.json` 文件时仍算无快照。
2. **选最新的键是 `created_at` 不是文件名**：构造两份 meta，文件名为 `fff0000`
   （created_at 较早）与 `0000aaa`（created_at 较晚），断言选中 `0000aaa`——
   这条用例专门锁死口径 B 的回归。**落地状态**：`test_latest_key_is_created_at_not_filename`
   （两个文件名照抄本判据，故意做成「字典序大的时间早」）。
3. `created_at` 缺 `+08:00` 后缀或不可解析 → 断言报错（决策 ① 的断言）。
   **落地状态**：`TestCreatedAtAssertions` 7 例，覆盖「缺键 / 不可解析 / naive 无偏移 /
   其他偏移（`+00:00`）/ 第二级命中也断言 / 第三级扫描时任一份坏即报错 /
   未被读到的坏 meta 不阻塞本次解析」（末条即实施裁定 4 的两端）。
4. `git_short_sha()`：设 `ATLAS_GIT_SHA=abc1234` → 返回 `abc1234`（不调 git，
   可用 monkeypatch 让 `subprocess.run` 抛异常以证明未被调用）；未设 → 走 git。
5. **副本归零（按行为检索；2026-09-14 多轮扩充，每轮起因见各项括注）**：
   - (a) `grep -rn "rev-parse" --include="*.py" . | grep -v '\.venv'` 命中集 =
     **`data/identity.py` + `tests/test_export_dbt.py` + `tests/test_demo_e2e.py` 共 3 处**，
     即生产代码中**只命中 `data/identity.py` 一处**（起点实测生产 11 + 测试 2 = 13）。
     **本项初稿的检索式按函数名**（`def git_short_sha` / `def _git_short_sha`），会漏掉
     `data/value_profile.py:204 head_sha()` 而误判「已归零」，2026-09-14 改为按行为检索；
     **同一检索式的反方向失效（2026-09-14 工作项 5 实测）**：它按**关键词**扫 `.py`，
     于是新写的 `tests/test_container_identity.py` 仅在断言文本与 docstring 里**提到**
     那条命令，就成为第 4 个命中项，被「测试侧命中集恰为 2 处独立预言机」判红。
     这是本批第一次 **口径大于裁定口径**（前四次均为小于）：把「提到命令」与
     「实现命令」混为一谈。处置选择：**改测试，不放宽判据**——该测试改为行为断言
     （make 求出的身份 == `git_short_sha()`）后字面量自然消失，断言反而更强，
     本项命中集仍是 3。不选「往期望集加白名单」的理由：一旦允许「提到即可」，
     下一处真副本就能以「我只是在注释里写」混过 (a)。
   - (b) **按构造形态检索快照目录**（2026-09-14 二次扩充：初版按变量名
     `^SNAPSHOT_DIR *=` 检索，起点实测「5 处」，漏掉 `serving/` 三处内联写法，
     与 (a) 修正前同型）。检索式取「路径除法 + 以 `data` 打头的字符串字面量」这一
     构造特征，覆盖三种实际形态（`data` 与 `snapshots` 分两段 / 合一段 / 带 `f`
     前缀再拼文件名），因而既不误报错误提示文案与 docstring 里的目录名说明，
     也不漏掉任何一段字面量写法。生产侧期望命中集 = `data/identity.py` **加两处
     显式例外**：`eval/api_acceptance.py`、`eval/e2e_acceptance.py` 直接点名**固定
     sha** 的 meta 文件，属评测/验收链路（N6 要求绑死快照，写死 sha 正是它们的
     职责）。**例外必须写进期望集**而不是留成「看起来漏了」：判据同时断言「多出的
     副本」与「消失的例外」，白名单因此不能单向膨胀。`tests/` 侧不做等于断言——测试
     读固定快照 meta 是 N6 允许的既有资产，数量随用例增长，写死等于会把「新增一个
     测试」变成「必须改判据」；
   - (c) `from eval.runner import git_short_sha` 仍可用（向后兼容 re-export），
     起点实测的 **12 处**该类消费方（决策 ② 消费方计数段）**逐字未改**。
     **本项限定（2026-09-14 工作项 3 起）**：12 处中 `agent/factory.py` 已**有意**改走
     `data.identity`（它需要的是解析函数而非 HEAD 严格 sha，决策 ① 的权威入口就是它），
     故本项断言的对象是「**其余 11 处逐字未改 + re-export 仍可用**」。`serving/api.py`
     那处会随工作项 6 同样改走 identity，届时本项改为 10 处。
     **该预告已兑现（2026-09-14 工作项 6 实测）**：AST 口径命中集 HEAD = **12**、
     工作区 = **11**，其中 `eval/` 6 + `lora/` 4 = **10 处原有消费方逐字未改**，
     第 11 处是新加的契约测试自身（`tests/test_identity.py:204`，它正是「re-export
     仍可用」的断言点，属消费方而非副本）。**期望集由此固化为 10**，复验命令 =
     下方 AST 口径脚本，不是行 grep。
     **复验命令不得按行 grep**：工作项 3 当时 `grep "from eval.runner import" |
     grep git_short_sha` 实测得 **10** 而非 11 —— `eval/compare_4way.py:33` 与
     `eval/rag_eval.py:38` 是**括号多行** import，函数名不在 `from` 行上。（本批
     落地后同一对数字变成 **9 vs 11**：AST 侧少掉的 2 处正是 `factory.py` 与
     `api.py`，多行 import 造成的**差值 2** 不变——引用这两个数时要分清是哪个口径、
     哪个时点，否则就是本页反复警告的「记号歧义」。）这是本 ADR 第四次「检索口径小于
     裁定口径」，且这次是**按写法**（单行 vs 多行）而非按命名，与前两次同型；
     正确口径按 AST 取 `ImportFrom` 的 `names` 集合（本批实测脚本口径：
     `from eval.runner import …` 含 `git_short_sha` = 工作项 3 时点 **11 原有 + 1 新加
     = 12**；工作项 6 后 **10 原有 + 1 新加 = 11**，新加的那处是
     `tests/test_identity.py` 里断言 re-export 的用例本身）。
     **本项必须同时断言运行时与类型层**（2026-09-14 实施时补）：`eval/runner.py`
     的 import 须写成 `from data.identity import X as X` 的**显式再导出**形式，
     因为 mypy strict 默认 `--no-implicit-reexport`，裸 import 会让 9 个消费方文件
     报 12 条 `attr-defined`。踩坑实录：只写运行时断言（`assertIs`）时判据 5(c)
     与 `make test` **全绿 565 项**，而 `mypy` 已报 12 条——`make test` 不跑 mypy，
     二者互不覆盖。**「测试通过」不等于「契约成立」**，故收口判据须含
     `mypy` 的 `eval.runner` 相关错误 = 0（本仓 mypy 基线为 42 条既有错误，
     跨 13 文件；其中 1 条是 `agent/graph.py:65` 的 `langgraph.types.RunnableConfig`，
     属第三方包同类问题）。
     **比对方法纠正（2026-09-14 工作项 3）**：上面那句「逐条比对均不在本次改动行上」
     的**做法不可信**，已废弃。逐行 blame（错误所在 (文件,行) ∩ `git diff -U0 HEAD` 的
     hunk）会把「同一条既有错误恰好落在我重写的行上」误判成新错误——实测工作项 3
     就报出 1 条：`serving/rls_verify.py: Call to untyped function "load_budget"`，
     而它在 HEAD 副本的 `:280` 逐字存在（只因该函数上方少了两行注释而行号不同）。
     改用**基线快照的消息级比对**：
     `git worktree add /tmp/atlas-head HEAD` → 在该 worktree 内跑同一套门禁 →
     按 **(文件, 消息) 多重集（忽略行号）** 求差。实测：mypy **42 = 42，新增 0 / 消失 0**；
     `ruff check .` **28 = 28，同样 0 / 0**；worktree 内全量测试 **554 例通过**
     （= 本批工作项 3 后的 595 − 新增 41，逐项核对见 dev-plan 的「工作项 3 落地实测」段）。
     附带澄清一个误解：`data/identity.py` 不在 mypy 配置的 `files` 列表里，但它作为
     被跟随模块**确实被分析**（`--log-silence` 输出含 `Metadata fresh for data.identity`；
     单独 `mypy data/identity.py` → Success）。
   - (d) `semantic/export_dbt.py` 的容错语义已按代价 ⑤ 移到**调用点**：该文件内
     无 `rev-parse`（被 (a) 覆盖）且调用处有 `try/except` 包裹，
     非 git 环境仍返回 `"unknown"` 而不抛（初稿裁定了但无判据验）；
   - (e) **测试侧 2 处独立预言机带显式注释**（决策 ② 末段裁定）：
     `tests/test_export_dbt.py` 与 `tests/test_demo_e2e.py` 的 `_head_sha()` docstring
     含「独立预言机」字样与不合并的理由，且两文件**不 import** `data.identity`。
     初稿无本项：无它则 (a) 的 3 处命中集看起来像漏删，下一个人会把它们「修掉」，
     从而静默削弱断言强度。
6. 域一致性校验：用组 2 meta（`7d48dcb`）+ retail 模型构造 agent → 抛
   `SnapshotUnavailable` 且消息含 4 张缺失表名；用组 1 meta → 构造成功。
   **落地状态（2026-09-14 工作项 4）**：`tests/test_domain_snapshot_consistency.py`
   **8 例**（无 DB）＝ 本判据字面 3 例（组 2+retail 抛且逐表断言 4 张 / 组 1+retail
   成功 / **组 2+finance 成功**——反向断言，锁死「非组 1 即拒」这种过度实现）
   + 实施裁定 3 例 + 不校验行数 1 例 + Guard 纵深 1 例。真实目录实测
   （`ATLAS_SNAPSHOT_SHA=7d48dcb`，即走 env 级而非测试里的 head 级）抛出的消息原文：

   ```
   绑定快照 7d48dcb（source=env）缺少语义模型 atlas_retail_analytics 所需的表：
   ['atlas.dwd.date_dim', 'atlas.dwd.dim_item', 'atlas.dwd.dim_store',
   'atlas.dwd.store_sales']——请重锁快照（make seed）或用 ATLAS_SNAPSHOT_SHA
   指定含这些表的快照
   ```

   （上方为排版换行，实际是单行。）同一 sha 下 finance（required 8 张）构造成功、
   默认解析（`a11d779`，组 1）下两域均成功——印证理由 2「**不可能让原本可用的路径
   变得不可用**」。表集差值另经独立复算与背景节一致：retail 4 张在 `7d48dcb` **全缺**、
   在 `a11d779` 不缺；finance 8 张两处均不缺。
   **本判据已做变异验证**（2026-09-14，三次各打红且仅打红一例、其余七例仍绿）：
   校验移到构造之后 → `test_check_happens_before_agent_construction`；None 分支不
   构造模型 → `test_default_model_is_checked_too`；顺带校验行数 →
   `test_row_counts_are_not_verified`。三条细则因此不是「有没有抛异常」的重复断言；
   变异后 `md5` 逐字核对已恢复原文件。HTTP 侧 503 通路已核（`serving/api.py:328-333`
   捕获 `SnapshotUnavailable` 且用 `{exc}` 带出原消息），但 503 的**前缀文案**
   「无法绑定评测数据」对本例不准确——绑错快照不是「无快照」，且「评测数据」在
   N6 语境下是另一个意思；该文件属决策 ⑥（工作项 6）范围，届时一并改。

**判据 10（N6 双向边界，结构判据）** —— 编号续在真链判据 9 之后、**不重排 7~9**
（它们已被 dev-plan 的收口矩阵与 0017/0020 的引用锁定）；写成加粗标签而非列表项，
是因为 Markdown 会把跟在 `6.` 后的 `10.` 重新编号成 `7.`，编号本身即判据身份时不能赌渲染器。

判据 1~3 测的是解析函数**本身**，测不到「有人把 `eval/runner.py` 改成调用它」，
也测不到「有人把指纹复核塞进运行时」。两个方向的放宽都只改变数字的**归属**而不改变
任何返回值，故只能按代码位置断言，三个方向缺一不可：

- `eval/` 下任何 `.py` 都不得引用 `resolve_runtime_snapshot`（三级回退一旦进入
  评测，EX 就绑在会漂移的基准上，违反 N6）；
- `eval/runner.py` 的三条评测口径特征必须**仍在**：`f"{sha}.meta.json"` 单文件定位
  （不做选择）、`git_short_sha()` 提供 sha、`verify_snapshot()` 先复核指纹；
- `data/identity.py` 不得出现 `verify_snapshot`，也不得 `import eval.…`
  （反向依赖会把 mysql 驱动与语义层拉进 `make lint` 路径，破决策 ② 的「仅 stdlib」）。

**检索口径：先抹掉注释与字符串字面量（`tokenize`），再在剩余代码里找**——本 ADR
要求 docstring 保留改造前的写法与「不调用本函数」这类说明（N2 要能看出改了什么），
按原文检索会自造命中；而为绕开误报改用「行首必须是 import」之类的窄式，就又回到
判据 5 那一族「检索口径小于裁定口径」的漏检。
落地：`tests/test_snapshot_resolution.py::TestEvaluationPathUntouched` 3 例。

**判据 11（四处接入，代价 ⑧ 待办的验收面）** —— `agent/cli._load_budget` 与
`serving/{metrics_verify,rls_verify,p1_acceptance}.load_budget` 四者的 Guard 预算
**必须来自解析结果**。同样不做字符串断言（`metas[-1]` 一改名就漏检，而散文里的该写法
是刻意保留的证据），改为**行为**断言：把 `resolve_runtime_snapshot` 换成一张真实目录里
不存在的表，断言四处返回的 `allowed_tables` 恰为该表——仍在自己列目录的那处会拿到真实
meta 而失败。**本判据已做变异验证**（2026-09-14）：把 `serving/rls_verify.py` 的解析
调用改回「列目录后 `[-1]`」形态后，两条用例（预算来源 / 不可用→`SystemExit`）
**同时变红**，证明它们不是空断言。
落地：`TestCallSitesUseResolvedSnapshot` 2 例 × 4 站点（`subTest`）；
`agent/cli.py` 的退出码与 stderr 回显另见 `tests/test_cli_query.py`。

**判据 12（容器身份无默认值，决策 ④）** —— 落地：`tests/test_container_identity.py`
**14 例**，其中 3 例需要 docker CLI（`skipUnless`，本机实测可跑，不需守护进程以外的东西：
`docker compose config` 在客户端做插值）。四类断言分别是：

- **默认值归零**：`ARG GIT_SHA` 行不含 `=`；Dockerfile 全文不含任何 7 位 hex
  （通用式，理由见实施裁定 4）；同一通用式推到 compose 与 Makefile（收口时补，
  起因见文档判据第 2 条）；compose 的 `${GIT_SHA:-}` 后不得有值；
- **构建期响亮失败**：把 Dockerfile 里那行守卫**原样取出**用 `sh -c` 执行（测试里
  不抄副本），空身份必须非零退出且消息点名 `GIT_SHA`，给值必须为 0；另断言守卫
  位于 `RUN uv sync` 之前；
- **注入点真生效**：`make` 侧赋值含 `$(shell`（**不锁赋值运算符**——第一版把 `:=`
  写进判据等于把风格当契约，已放宽为 `[:?]=`）；`export` 用子进程环境实测；
  且 make 求出的值 == `data.identity.git_short_sha()`（身份同源，跨决策 ②/④）；
- **反向保护**：`${GIT_SHA:?}` 不得回归（`docker compose ps` 在无身份下必须成功）；
  `ATLAS_SNAPSHOT_SHA` 必须仍是可留空的 `${ATLAS_SNAPSHOT_SHA:-}`。

**本判据做过 9 次变异验证**（2026-09-14，每次变红 1~3 例、其余仍绿，改完 `md5`
核验三文件逐字节恢复）：compose 恢复硬编码默认 → 3 例；compose 改 `:?` → 2 例；
去掉 `export` → 2 例；守卫挪到依赖层之后 → 2 例；`ARG` 给默认值 → 2 例；
**make 侧写死成「恰好等于当前 HEAD」的值 → 只有求值点那 1 例红**，而看起来更强的
同源断言**通过**（值本来就相等）——这是判据 5(c)「测试通过 ≠ 契约成立」的第六次
复现，也是本批第一次由**行为断言与结构断言互补**才挡住的形态：只做行为断言会漏
「今天恰好对」，只做文本断言会漏「写法对但值错」。
收口时补的 2 次针对通用式扫描：Makefile 注释里放回 sha → 1 例红；compose 注释里放
一个**不在 `KNOWN_SHAS` 里的新 sha** → 1 例红（这一条才证明扫的是通用式而不是枚举，
枚举式对它全绿）。（第二次跑出的「2 例红」是**变异脚本自身的缺陷**——批次里各次变异
之间没恢复，Makefile 的残留一起算了；隔离重跑即得 1 例。教训：变异脚本必须
每次改完立刻恢复，否则「变红例数」这个证据本身就不可信。）

真链侧证据（不属 `make test`，手工复跑）：`docker build` 无 `--build-arg` → 退出码 1，
失败在守卫行，日志中依赖层出现 0 次；同一行守卫在 `python:3.11-slim` 容器内
`GIT_SHA=abc1234` → rc 0、`GIT_SHA=` → rc 1。附带发现（与本决策无关、既有）：
本机缓存的基础镜像是 `linux/amd64` 而宿主是 arm64，构建与 `docker run` 都报平台
告警——记此以免日后把它误读成本批引入的问题。

**判据 13（强制回显，决策 ⑥ + ADR-0020 决策 ⑦）** —— 落地：`tests/test_identity_echo.py`
**26 例**（无 DB，不连 Doris；HTTP 走 `TestClient` + 桩 agent 工厂）。四类断言：

- **键集**：`/health` 恰好 8 键，且**正常路径与降级路径同一键集**；`_turn_payload`
  恰好 **21 键**（决策 ⑥ 的 2 + 既有 19），未绑定 agent 也回显两键为 `null`
  （不是缺键）；`snapshot_bound_to_head` 与「`snapshot_sha == head_sha`」必须自洽；
  `explanation` 的 13 个固定键不得被塞进新键（语义归属，见决策 ⑥ 的诚实注记）；
- **单一事实源**：把 `resolve_runtime_snapshot` 换成注入值后**逐键**断言响应跟变
  （含 `head_sha`）；`serving/api.py` 内无 HEAD 解析命令字面量、无快照目录拼接、
  `head_sha` 必须 `from data.identity import`（旧 `eval.runner` re-export 路径不得复用）；
- **口径一致**：`snapshot_tables` == `build_budget(meta).allowed_tables` 元素数，
  对**全部真实 meta**（实测 17 份）逐个 `subTest`；坏 `row_counts` → degraded + `null`；
  `DataAgent` 双来源不一致必须构造期抛且消息带出两份 sha（含「一致放过」「只给 meta
  则 `snapshot` 为 None」两条反向，防守卫过宽）；
- **重启可见 + 探针无副作用**：`boot_id` 同进程恒定、形状 `^[0-9a-f]{32}$`、`BOOT_ID`
  是模块常量且**不从 env 读**；另用 `importlib.reload` 断言**模块体重跑即换值**
  （0020 判据 9 后半句「新进程不同」的机制面：spawn 两个解释器属真链侧，而
  「值在模块求值时随机生成」是造成跨进程差异的**唯一**机制，改成读 env、落缓存文件、
  或做成常驻类属性都会让 reload 给出同一个值）；`/health` 不得触发 agent 工厂
  （构造 = 连 Doris）。

**本判据做过 7 次变异验证**（2026-09-14，每条改完立刻恢复并 `md5` 核验）：
首轮 **M1、M5 两条没有变红**——M1 是「`/health` 自己再跑一次 git 当 `head_sha`」，
没红的原因是注入用例**从未断言 `head_sha`**；M5 是「删掉 `DataAgent` 双来源守卫」，
没红的原因是**根本没有这条断言**。两条都不是「判据太宽」，而是**我写的测试没覆盖它
声称覆盖的东西**：补齐（注入用例加 `head=` 与 `assertEqual(body["head_sha"], …)`；
新增 `TestAgentBindingSingleSource` 3 例）后 6 条全红，例数 22 → 25。补 M7
（`BOOT_ID` 改成写死常量）时又发现跨进程语义无断言，于是加 reload 例，例数 25 → 26。
教训：**「先写评测」的验收标准不是「测试变绿」**——绿色只证明没有回归，不证明有覆盖；
能证明覆盖的是「把判据声称防住的失效形态逐个注入后测试变红」。这是本 ADR 里第一次
由**变异验证自身**（而非检索口径）暴露判据缺陷，也是「测试通过 ≠ 契约成立」的第六次
复现，形态从「类型层盲区」升级成「断言存在性盲区」。

> **变异验证的恢复纪律（本批以一次事故坐实，写进契约）**：恢复**必须**用
> 「脚本内先读原始字节 → `finally` 写回 → `md5` 核验」，**不得**用版本控制回滚命令。
> 事故实录：给 M7 收尾时用 `git checkout serving/api.py` 恢复，而该文件承载的是
> **本批未提交的工作项 6 全部改动**（69+/20−）——checkout 直接把它退回到 HEAD，
> 等于顺手删了一个工作项的产物。当时靠 IDE 本地历史取回同名快照，**`md5` 与变异前
> 逐字节一致**（`6647fe39…`）才确认无损失，随后重跑判据 13 的 26 例全绿验证等价性。
> 判据 12 段已登记过「变异脚本各次之间没恢复，导致红例数不可信」，本条是同一纪律的
> 更强形态：**未恢复不只是证据失真，还会吃掉代码**。


**判据 13 未覆盖的部分（如实记下，2026-09-14 收窄一次）**：本判据锁的是**形状与同源**，
不是「这台机器上解析到的值对不对」（后者属判据 1~3 的地盘）。原先登记的
「`/health` 8 键的真链取值待 Doris」**是过度保守**：`health()` 不构造 agent
（这正是判据 13 自己断言的一条），故宿主机起服务 + `curl` 即可验，已补验通过，
证据见真链判据 7 的落地注。**收窄后只剩一条**，且它确实需 Doris：`/ask` 在**真实**
Agent 上回显两键 —— 判据 13 的该例走的是桩 agent 工厂，真链侧由判据 7 的后半句负责。

> **收口更新（2026-09-16）**：该「只剩一条」已验通——`/ask` 在真实 Agent 上回显
> 两键（判据 7 收口落地注）；本判据「未覆盖的部分」至此归零。

**真链判据（跑起来的服务；判据 7 的 `/health` 半侧不需 Doris，其余需 Doris，
进 `make api-verify` 或独立脚本）**：

7. 宿主机 HEAD 无 meta 时：`/health` 返回 `snapshot_source="latest"` +
   `snapshot_bound_to_head=false` + 实际 sha；`/ask` 金融与零售问句各一条返回
   `kind="answer"`（**当前实测为 503，改后必须可用**），响应含 `snapshot_sha`。
   **代码侧前提已具备、真链结论待判（2026-09-14 工作项 3）**：HEAD 仍为无 meta 的
   `ccb4c8b`（与「当前实测为 503」一致），而 `create_live_agent()` 实测已能构建成功并
   绑到 `a11d779`（改造前该调用必抛 `SnapshotUnavailable`）——即 503 的成因已消除。
   本判据要求的 `snapshot_source` / `snapshot_bound_to_head` 两键属决策 ⑥（工作项 6），
   故本判据**整体留到 P-1 收口连 Doris 一起验**，不在本工作项声称通过。
   **`/health` 半侧已真机验通，`/ask` 半侧仍待 Doris（2026-09-14 工作项 6）**：两键连同
   其余 6 键已在 `/health` 落地并被判据 13 锁住形状与同源；而「这台机器上取到的值对
   不对」其实**不需要 Doris** —— `health()` 不构造 agent，起服务即可答。当场补验：
   `make serve` 同参起 uvicorn（`--host 127.0.0.1 --port 8000`；`.env` 里**没有**
   `ATLAS_SNAPSHOT_SHA` 这一行，故第 1 级未触发）→ `curl /health` 返回
   **HTTP 200 + 恰好 8 键**，取值逐条符合本判据：`head_sha=ccb4c8b`
   （= `git rev-parse --short HEAD`，且 `ls data/snapshots/ccb4c8b.meta.json` 不存在，
   即「HEAD 无 meta」前提在宿主机成立）→ `snapshot_sha=a11d779` +
   `snapshot_source=latest` + `snapshot_bound_to_head=false` + `snapshot_tables=29` +
   `status=ok`。**但本判据整体仍未通过**：剩下的「`/ask` 金融与零售各一条返回
   `kind="answer"`」必须连 Doris，留 P-1 收口，不在这里改判。
   **附带拿到跨进程重启证据**（补 ADR-0020 判据 9 的机制面之外缺的那半）：连起两次服务，
   `boot_id` 由 `b5900113236849919c3958eb81fa1980` 变为 `66420615bf9b4e988b8d7e9e4a0f3f0f`，
   而**其余 7 键连同键序完全一致**——「重启可见」在真机上成立，且身份信息不随重启漂移。
   **收口落地（2026-09-16 P-1 收口复验；本判据整体转通过）**：`/ask` 半句验通——
   宿主机 `make serve` 同参起服务（`ATLAS_CHECKPOINT_DB` 指向本地会话 DB），金融与
   零售各一条真链问句均 `kind="answer"` 且含 `snapshot_sha` 回显：finance「2013 年
   总交易额」=`344129059.3500`、retail R-01「2001 年销售额」=`86183391.24`（判据 8
   同批）。**形态变化如实登记**：判据原文的触发前提「宿主机 HEAD 无 meta」在工作项 12
   锁 `ccb4c8b` 后**已不成立**——收口实测落在决策 ① 三级解析的**第 2 级**：`/health`
   8 键为 `head_sha=ccb4c8b` + `snapshot_sha=ccb4c8b` + `snapshot_source="head"` +
   `snapshot_bound_to_head=true` + `snapshot_tables=29`（第 3 级 latest 形态已在工作项 6
   时点真机验过、第 1 级 env 由 13 例契约测试锁定，见上注与判据 13 段）。`/ask` 响应
   21 键（19 + 本判据两键）——设计页 §5 第 2 条断言（此前只桩面验过）同批真链取到。
   **附带 boot_id 复验**：两连拍 `9fcc1c90f48840e1a5b7541e75aefe0e` →
   `0837c579a9d647e7b529b1bd201388d0`，其余 7 键连同值完全一致（ADR-0020 判据 11 注）。
8. 零售域问句（`docs/atlas_query_test_cases.md` R-01 `2001 年销售额`）返回值与
   该文档记录的实测值 `86183391.24` 一致——证明「最新已锁」与「字典序最大」
   在组 1 内等价，放宽未改变数值。

   > **已验（2026-09-16 P-1 收口）**：`/ask` 真实 Agent 上 R-01 返回 `86183391.24`，
   > 与文档记录一致（同批 finance 对照值 `344129059.3500`，与本判据的数值无关、仅作
   > 双域可用的旁证）——「组 1 内最新已锁与字典序最大等价」在真链成立，
   > 未触发下方证伪条件。
9. 复现脚本（决策依据，保留为回归证据）：HEAD 代码 + `build_budget(7d48dcb meta)`
   → retail `enforce` 抛 `UnsafeQuery: 表不在白名单内：atlas.dwd.store_sales`；
   换 `a11d779` → 通过、cost=0.25。

**证伪条件**：若判据 8 的数值与 `docs/atlas_query_test_cases.md` 不符，说明
「组 1 内 12 份指纹一致」的实测结论有误，决策 ① 的第 3 级回退会改变查询语义
→ 立即回退到 HEAD 严格（备选方案第 2 行），并重做指纹分组测量。

**文档判据**（两条，2026-09-14 工作项 5 收口时改写）：

1. **演进表覆盖（单向集合包含）**：`data/snapshots/*.meta.json` 的 sha 集合必须
   **⊆** `data/snapshots/README.md` 提到的 7 位 hex 集合（反向不成立，见下）。
   **状态：满足（2026-09-14 工作项 6）且已自动化** —— 补记 8 份后实测：盘上 **17** 份
   全部被提及，README 的 7+ 位 hex token 共 **23** 个 = 17 快照名 + 6 commit
   （`36c878c` `4a547e7` `6af4cde` `9cf70c7` `b2a2e5c` `dfedc16`，盘上无同名 meta）。
   **本条不再是人工点数**：落地为 `tests/test_identity.py::TestSnapshotLedgerCoverage`
   3 例，三个方向各锁一件事——
   - **覆盖**：`meta 集合 − README token 集合 == ∅`（漏记任何一份即红）；
   - **身份**：反向按**行为**判定，「无同名 meta 的 token 必须是本仓一个真实 commit」
     （`git rev-parse --verify --quiet <sha>^{commit}`）。**不选枚举白名单**：
     枚举集会随每次加出处注释而增长，增长还得人来批，等于把纪律交回自觉；
     而写错的快照名、外仓 sha 这类「第三种身份」无论怎么写都跑不掉。
     （第一版确实写成枚举 `{9cf70c7, b2a2e5c}`，实测立刻又冒出 5 个 token——
     包括当时的 HEAD，这本身就是枚举式判据不可维持的证据）；
   - **份数**：「组 1 29 表 … 12 份」「组 2 25 表 … 5 份」必须是重算得出的数
     （N1：文档数字只能来自脚本产物）。配对口径是**同一行内**「X 表」之后紧邻的
     「N 份」——全文找「，N 份」会把并列的另一组算进来（自测时误红过一次：
     本节开头就是一句并列两组的写法）；并加前置守卫「同表数不得出现两个指纹组」，
     否则分组口径本身需要扩充，该失败要显式报出来而不是让两边各自去找数字。
   token 与 meta 名的取法也都不是恰好 7 位：`_metas()` 取**文件名首段**、正则用
   `{7,}`——短 sha 位数由 git 决定，硬切 7 位会在出现 8 位短 sha 时把同一份 meta
   认成两个 token（两侧同口径，否则集合比较的分子分母不同基）。
   **本条做过 3 次变异验证**（2026-09-14，每次跑**全量**、改完 `md5` 核对逐字节恢复）：
   删一条快照条目 → 覆盖例红；加一个既非快照也非 commit 的 hex → 身份例红；
   把「12 份」改成「11 份」 → 份数例红。三次各**恰红 1 例**。
   仍**未**覆盖的方向（如实记下）：README 把某份快照的**出处/说明写错**（token 在、
   数字对，但叙述不实）——本判据只锁「有没有记」，不锁「记得对不对」，后者仍是人工。

   **改写理由**：原文是「`ls data/snapshots/*.meta.json | wc -l` 与表内条目数一致」，
   即**数量相等**。实测该式**永远无法满足**：README 里另有两个 7 位 hex
   （`9cf70c7` / `b2a2e5c`）是 **commit 短 sha**，盘上无对应 meta——7 位 hex 在这份
   README 中同时承担「快照名」与「commit」两种身份。数量相等把两种身份当成一种，
   判据本身失效，故改为单向集合覆盖：漏记快照 = 红；README 提及无 meta 的 sha
   （commit / 历史值）= 可容。
2. **配置文件不留 sha**：Dockerfile 内不再有与生效值不符的 sha 注释，且**任何**
   硬编码 sha 都不出现在身份注入链的部署配置文件里（注释与默认值同罪）。
   **状态：满足**（工作项 5）—— 实测 `infra/docker/api/Dockerfile`、
   `docker-compose.yml`、`Makefile` 三个文件的 7 位 hex 命中集均为空；断言见
   `tests/test_container_identity.py` 的 `test_no_sha_literal_anywhere_in_dockerfile`
   与 `test_no_sha_literal_in_compose_and_makefile`（后者是收口时补的：`Makefile` 里
   原有一句「锁定快照（b933e20 meta，29 表…）」，描述的是 `eval/api_acceptance.py`
   内 `SNAPSHOT_META` 写死的值——注释与常量各说各话，正是本判据要消灭的形态；
   现改为指向脚本常量并注明该 sha 属 N6 例外，见判据 5(b)）。
