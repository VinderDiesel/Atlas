# ADR-0020：会话持久化——SQLite checkpointer、turns 单一事实源与 thread_id 命名空间隔离

- 日期：2026-09-14
- 状态：accepted（决策经用户确认，Q9/Q10；落地批次 P-1）
- 相关：
  - ADR-0012（HTTP 服务面 v1：**决策 1 的 workers=1 理由被本 ADR 收窄**、
    推翻条件第 1 条指定的是 Postgres checkpointer——本 ADR 选 SQLite 且**不解除**
    workers=1，偏离辨析见决策 ⑧；落地注记另附于该 ADR 末尾）
  - ADR-0009（Agent 框架：LangGraph 状态机）、ADR-0014 ②（多轮指代消解：`last_plan`
    跨轮读取的既有先例）、ADR-0011（安全分层 C2 硬化：会话 × 身份指纹 → 422）
  - ADR-0018（前端控制台：多轮续接与失忆提示的消费方）、
    ADR-0019（运行时快照口径：`/health` 同批扩字段）
  - 代码：`agent/graph.py:96-108`（`_CHECKPOINT_SERDE`）、`:142-153`（`build_graph` 签名）、
    `:172`（thread_id 约定 docstring）、`:222`（`state.get("last_plan")` 跨轮读）、
    `:408`（explain 回写 `last_plan`）、`:499`（`compile(checkpointer=MemorySaver(...))`）、
    `:502`（`turn_from_state(..., turns=1)`）、`:579`（`_session_turns`）、
    `:605`（`config = {"configurable": {"thread_id": sid}}`）、`:626-629`（`sessions` property）
  - 代码：`agent/state.py:6-7`（「每轮由 checkpointer 按 session_id 持久化，可回溯可审计」）、
    `:36-70`（`TurnState`，`total=False`，无 reducer）、`:87`（`TurnResult.turns_in_session`
    注释「Agent 层维护」）
  - 代码：`serving/api.py:293-298`（`holder["agents"/"sessions"]`）、`:400-414`（指纹绑定与 422）、
    `serving/ratelimit.py:30-33`（`_hits: dict[str, tuple[int,int]]`，进程内）
  - 依赖现状（实测 2026-09-14）：`langgraph 1.2.11`、`langgraph-checkpoint 4.2.0`（传递依赖，
    **不含 SQLite 实现**）、`langgraph-checkpoint-sqlite` **未安装**；
    `uv pip install --dry-run langgraph-checkpoint-sqlite` → 解析为
    `langgraph-checkpoint-sqlite==3.1.1` + `aiosqlite==0.22.1` + `sqlite-vec==0.1.9`（新增 3 包）
  - 测试：`tests/test_graph.py:498-499`（`agent.sessions[sid]` 的唯一外部消费点，2 处断言）

  > **行号口径（与 ADR-0019 同一处置）**：上面「代码」条目里的行号是**立项时坐标**，
  > 本批 P-1 会持续改动 `agent/graph.py` 与 `serving/api.py`，行号必然漂移（实测已漂：
  > `_CHECKPOINT_SERDE` 96→106、`compile(...)` 499→518、`_session_turns` 579→640、
  > `thread_id` 605→666、`sessions` 626→682）。**工作项 8 落 `ask` 的命名空间注释时又漂
  > 了一次**（`thread_id` 666→675）——这正是「不追改」的理由。定位一律用符号名，不用行号：
  > `grep -n "^_CHECKPOINT_SERDE\|^def build_graph\|self._session_turns\|def sessions" agent/graph.py`
  > 与 `grep -n "holder\[.sessions.\]\|_claims_fingerprint" serving/api.py`。
  > 本 ADR 正文保留原行号作为「立项时看到了什么」的历史证据，不追改。
  > **工作项 9 追加（2026-09-15）**：上述 grep 里的 `_session_turns` / `def sessions` /
  > `holder["sessions"]` / `_claims_fingerprint` **四个符号已被本项删除**（grep 返回空不是
  > 漂移，是删除本身）；对应的新符号指针改为
  > `grep -n "claims_fingerprint\|session_fingerprint\|\"turns\"\|SessionIdentityConflict"
  > agent/graph.py serving/auth.py`。
  > **工作项 10 追加（2026-09-15）**：本项修正了 ADR-0012 会话持久化注记里两个
  > 立项行号（`/ask` docstring 的 `serving/api.py:388`、healthcheck 的
  > `docker-compose.yml:230-231`——都被本批次的增补顶走），改为符号定位；
  > 「行号型指针在会增长的文件上不可靠」的又一实证。

---

## 背景

### 三处进程内态，重启即静默失忆且不报错

| 状态 | 位置 | 重启后 |
|---|---|---|
| 会话轨迹（多轮上下文 `last_plan`） | `MemorySaver`（`graph.py:499`） | thread 丢失 → 指代消解基线归零 |
| 轮数 | `DataAgent._session_turns`（`graph.py:579`） | `turns_in_session` 悄悄从 1 重新开始 |
| 会话 × 身份指纹 | `holder["sessions"]`（`api.py:297`） | 指纹丢失 → 旧 `session_id` **重新绑定**，不触发 422 |

三者叠加的失效模式：服务端重启后，前端若仍持旧 `session_id` 发请求，
**HTTP 全绿、无任何错误信号**，但 Agent 已不记得前文，轮数从 1 重算，且此时
换一个新角色签发的 token 也能绑上同一个 `session_id`（C2 硬化被绕过）。
用户以为在续接对话，实际在和一个失忆的 Agent 说话。

这与 `agent/state.py:6-7` 的自述矛盾——「每轮由 checkpointer 按 session_id
持久化，可回溯可审计」。今天 checkpointer 是内存实现，**既不可回溯也不可在重启后
审计**。按 N2，这句 docstring 属于「把设计写成已完成」。

### `thread_id` 只含 `session_id`，而 HTTP 会话键是 `(model, session_id)`

```python
# serving/api.py:401          # 会话身份按 (模型, 会话) 二维隔离
key = (model_name, sid)
# agent/graph.py:605          # checkpoint thread 只有一维
config: dict[str, Any] = {"configurable": {"thread_id": sid}}
```

`api.py:390-391` 明确声明「session_id 按模型隔离——同键跨模型请求是两个独立会话
（finance/retail Agent 各自单例）」。今天成立，**只因两个 Agent 各持一个独立的
`MemorySaver` 实例**。一旦换成共享同一个 SQLite 文件，同一 `sid` 在 finance 与
retail 两个图上会命中**同一个 thread**，两个不同状态图的 checkpoint 互相覆写——
跨域串话。这是持久化引入的**新**失效模式，必须在同一批次解决（决策 ④）。

### `turns` 有两个事实源

`turn_from_state(state, session_id, turns=1)`（`graph.py:502`）的 `turns` 由
`DataAgent.ask` 从 `self._session_turns` 算出后**作为参数注入**，而不是从状态里读。
但同一份状态里已经有一个跨轮字段的正确先例：`last_plan`——explain 节点回写
（`graph.py:408`），下一轮 plan 节点 `state.get("last_plan")` 读取（`graph.py:222`）。
`turns` 走同一条路即可，不需要平行维护一个进程内 dict。

### 依赖与实现约束（实测）

1. **新依赖**：`langgraph-checkpoint-sqlite` 不在 `pyproject.toml`，`uv.lock` 里的
   `langgraph-checkpoint 4.2.0` 只是基类包（`MemorySaver` 来自它，`SqliteSaver` 不来自它）。
   实测 `from langgraph.checkpoint.sqlite import SqliteSaver` → `ModuleNotFoundError`。
   引入触发 AGENTS.md §5 四步（理由 / ADR / 许可证 / 预算）。
2. **序列化白名单必须原样传递**：`_CHECKPOINT_SERDE`（`graph.py:100-107`）注册了
   `Plan` / `TimeSpec` / `OrderSpec` / `ClarificationRequest` 四个自研 dataclass，
   且注释记录了实测结论——`JsonPlusSerializer.with_msgpack_allowlist()` 在 permissive
   模式下**直接返回 self 不合并**（langgraph 1.2.11），因此只能走构造参数。
   换 saver 时若漏传 `serde`，多轮状态里的 `Plan` 会退化为告警式序列化。
3. **连接生命周期与单例冲突**：`SqliteSaver.from_conn_string()` 是 with 块语义
   （退出即关连接），而 Agent 是**长期单例**（`api.py:324-335` 按域懒建、
   进程生命周期内复用）。只能自持 `sqlite3.connect(path, check_same_thread=False)`
   并显式 `setup()` 一次。
4. **`build_graph` 的既有构造点数量**（初稿写「~400 个」，**2026-09-14 工作项 7 实测
   证伪，量级错一个数量级**，见决策 ① 的实测注）：默认值一旦改成 SQLite，`make test`
   会在仓库各处落 DB 文件并显著变慢。持久化必须是**可选注入**（决策 ①）。

约束：

- **N3/N9**：checkpoint 里含 `sql`（Guard 出口 SQL）与 `rows`（查询结果），
  落盘文件等同业务数据副本 → 必须 gitignore，且**不得**进镜像或备份到仓库外；
  DB 路径可配但默认在 `serving/state/`（与 `serving/audit/` 同级同纪律）。
- **§5**：新增依赖需 ADR + 许可证评估（本 ADR 即该记录；许可证实测见验证方式 1）。
- **§8**：改 `agent/graph.py` + `agent/state.py` + `serving/api.py` 属行为变更，
  与 ADR-0019 的 `refactor`（删 sha 副本）**不得同一提交**。

---

## 备选方案

| 方案 | 优势 | 劣势 |
|---|---|---|
| **SQLite checkpointer（选定）** | 官方实现，`_CHECKPOINT_SERDE` 直接复用；单文件零服务；重启不失忆；`sqlite3` 在 stdlib，运行时只加 1 个直接依赖（+2 传递） | 新增依赖需 §5 四步；单文件写并发受 SQLite 锁限制；**不解除 workers=1**（限流桶仍进程内）；DB 文件不入库 → 会话历史不可由 git 复现 |
| 前端 localStorage 存历史 + 后端 `turns_in_session` 一致性校验，失忆时显式提示 | 零新依赖；不碰 `graph.py`；失忆从「静默」变「可见」 | 后端仍失忆——`last_plan` 指代消解基线丢失，多轮能力实质退化；「Agent 记得」与「界面显示记得」两件事分裂，是更隐蔽的诚实性问题；C2 指纹绕过仍未解决 |
| Postgres checkpointer（ADR-0012 推翻条件第 1 条指定） | 跨进程共享，可真正解除 workers=1；栈内已有 PostgreSQL 16（Polaris 元数据库） | 推翻条件**未触发**（无多 worker/多实例需求）；会话状态与 Catalog 元数据共库，故障域耦合；连接池/迁移/备份全部新增运维面，与「个人可承受预算」冲突 |
| 自研 JSON/JSONL 文件持久化（每会话一文件） | 零依赖；人类可读，便于审计 | 要自己实现 checkpoint 的 version/parent 链与部分状态合并语义，等于重造 `BaseCheckpointSaver`；与 LangGraph 升级脱钩；违反决策优先级第 5 条（不过度设计） |
| Redis checkpointer | 跨进程 + 快 | 栈内无 Redis（AGENTS.md §5 未登记），新增一个常驻服务；个人单机预算下多一个进程要维护；数据非持久语义（默认策略）与「可回溯审计」冲突 |

---

## 决策

### ① checkpointer 可注入，默认仍是 MemorySaver

`build_graph(...)` 新增关键字参数 `checkpointer: BaseCheckpointSaver | None = None`：

- `None`（默认）→ `MemorySaver(serde=_CHECKPOINT_SERDE)`，**与今天完全一致**，
  既有构造点零影响、零落盘（数量实测见下）；
- 显式传入 → 用传入实例（`serde` 由调用方构造，见决策 ②）。

**实测注（2026-09-14 工作项 7）——「~400 个测试构造点」是数量级错误，结论不变**：

口径 = `grep -rn -F` 匹配 `build_graph(` / `DataAgent(` 的**行数**（一行多处只计一次；
排除 `.venv`/`__pycache__`/各 `._*_cache`/`atlas.egg-info`，排除本 ADR 新增的
`tests/test_session_persistence.py` 自身）：

| 位置 | `build_graph(` | `DataAgent(` |
|---|---|---|
| `tests/` | 3 | 27 |
| `eval/` | 0 | 9 |
| `agent/factory.py` | 0 | 1 |
| **合计** | **3** | **37** |

即真实构造点 **40 处**（不是 ~400）。`agent/graph.py` 内另有 4 行匹配，但那是
`def build_graph` 本身、类文档字符串里的示例、以及 `DataAgent.__init__` 的内部转发，
不属「调用方构造点」，故不计入。加上新测试自身的 11 行，全仓匹配为 55 行。

**为什么这条必须改而不只是标注**：数字本身不影响决策（40 处默认改 SQLite 照样会在仓库
各处落文件，可选注入的理由成立），但「~400」是一个**无计算脚本支撑的数量级断言**，
按 AGENTS.md N1 与 §9.2 属同一类违例——写进 ADR 的每一句都得能复算，错了就得改正文。
同理，`agent/graph.py` 的两处 docstring 与本 ADR 理由段第 3 条也一并改为实测值。

`create_live_agent()`（`agent/factory.py`）负责按环境决定是否持久化：

```python
db = os.environ.get("ATLAS_CHECKPOINT_DB", "").strip()   # 空 = 不持久化（MemorySaver）
```

即「是否持久化」由**部署方显式声明**，不是隐式默认。`make serve` 与 compose 的
`atlas-api` 服务显式设置该变量；`atlas ask` / `atlas query` CLI 走同一工厂，
因此默认也持久化（同一 DB 文件，见代价段 ④）。

### ② `_CHECKPOINT_SERDE` 原样复用，不得改用 `with_msgpack_allowlist`

```python
SqliteSaver(conn=sqlite3.connect(path, check_same_thread=False), serde=_CHECKPOINT_SERDE)
```

四个自研 dataclass（`Plan` / `TimeSpec` / `OrderSpec` / `ClarificationRequest`）的
注册**必须**通过构造参数——`graph.py:98-99` 的注释已记录 langgraph 1.2.11 的实测行为
（permissive 模式下 `with_msgpack_allowlist()` 返回 self 不合并）。将来 `TurnState`
新增任何自研 dataclass 字段，**必须同步加进这个白名单**（代价段 ⑤）。

> 本段初稿的「四个」是错的：实测白名单非空时未注册类型会被降级，`Plan` 的嵌套成员
> 各自独立判定，故实际需注册 **6 项**。落地时的复算见下方「实测注」表，代价 ⑤ 的
> 「随 `Plan` 一起注册」推定同时作废。

**实测注（2026-09-14 工作项 7）：白名单实际是 6 项，原「4 项」写法与代价 ⑤ 的推定都被证伪**

探针 = `langgraph.checkpoint.serde.jsonplus.JsonPlusSerializer` 的
`loads_typed(dumps_typed(...))` 直接往返（`langgraph 1.2.11` / `langgraph-checkpoint 4.2.0`），
被测对象是含 `TimeSpec` / `Filter` / `OrderSpec` / `ComparisonSpec` 的完整 `Plan`：

| # | 实测事实 | 对本 ADR 的后果 |
|---|---|---|
| 1 | **白名单非空时，未注册类型是被 `Blocked` 后降级为 `dict`**，不是「permissive 放行 + 警告」。警告只在**完全不传**白名单时出现 | 「嵌在 `Plan` 里的类型随 `Plan` 一起注册」（代价 ⑤ 原句）**为假**：注册 `Plan` 只放行 `Plan` 本身，其成员各自独立判定 |
| 2 | 用现有 4 项白名单往返 → `time=TimeSpec`、`order_by[0]=OrderSpec` 保住，**`filters[0]` 与 `comparison` 退化为 `dict`** | `TurnState.plan` / `last_plan` 每轮都过 serde（`MemorySaver` 同样 `self.serde.dumps_typed`），因此这是**今天就在的多轮缺陷**，不是持久化后才出现 |
| 3 | 端到端复现：首轮问 gold-151（带 `filters`）→ `kind=answer`；第 2 轮「那 2014 年呢」→ `kind=clarify`，原因链原文 `追问补全后编译失败：'dict' object has no attribute 'column'——请完整重述问句` | 缺陷的**用户可见症状是反问**，不是报错——编译器内部的类型退化被伪装成「用户没说清楚」，这是它此前没被发现的原因 |
| 4 | `_allowed_msgpack_modules` 是**三态**：`True`（未传 + permissive，全放行带警告）/ `None`（严格模式默认，只放内置安全类型）/ `set`（显式白名单） | 白名单断言只能读**内容**且必须区分三态：读到 `True` 时若按集合遍历会 `TypeError`，把契约失败伪装成实现崩溃（测试 helper 已显式判三态） |
| 5 | `LANGGRAPH_STRICT_MSGPACK=true` 在 `serde/_msgpack.py:12` 以**模块级常量**求值 → **只在进程启动时生效**，进程内 `os.environ[...] = "true"` 无效 | 判据 7 若要覆盖严格模式，必须 spawn 子进程（不是 `setUp` 里设 env）；本 ADR 的自动化面因此只测 permissive + 内容断言 |
| 6 | `SqliteSaver.__init__` 里 `self.jsonplus_serde = JsonPlusSerializer()`（`sqlite/__init__.py:92`）是一个**创建但从未使用**的属性，全部读写走 `self.serde`（`:420`/`:478` 等 8 处） | 决策 ② 的 `serde=` 构造参数路径**成立**（这是好消息）；但 `jsonplus_serde` 是陷阱：任何人以为「覆盖它即可换白名单」都会改了个死属性而毫无效果 |

**处置**：`_CHECKPOINT_SERDE` 补 `("agent.compiler", "Filter")` 与
`("agent.compiler", "ComparisonSpec")` 两项，`graph.py` 的注释改写为上表第 1 条的口径。
完整性论证：`TurnState` 里 dataclass 类型的字段只有 `plan` / `last_plan`（`Plan`）与
`clarification`（`ClarificationRequest`，实测字段全为 `str` / `tuple[str, ...]` / `Literal`，
不含自研类型），而 `Plan` 的嵌套自研类型恰为 `TimeSpec` / `Filter` / `OrderSpec` /
`ComparisonSpec` 四个 → **6 项即全覆盖**，不是「先补两个再说」。
锁死它的两条断言见判据 7（内容面 + 行为面各一条，另有一条走真图多轮路径）。

### ③ 自持连接 + 落盘位置 + 卷挂载

- **连接**：`sqlite3.connect(path, check_same_thread=False)` 由 `DataAgent`
  （或工厂构造的 saver 持有者）在进程生命周期内持有；`setup()` 调用一次
  （建表幂等）。**不用** `from_conn_string`（with 块退出即关，与单例冲突）。
- **默认路径**：`serving/state/checkpoints.sqlite`。**落地形态与初稿不同（2026-09-14
  工作项 8 实测后裁定）**：这个路径是**部署层的推荐值（`.env.example`），不是代码里的
  默认值**——`checkpoint_saver_from_env()` 在未设/空串/纯空白时返回 `None`，图回落
  `MemorySaver`。初稿写成「代码默认路径」会直接推翻判据 2（默认构造零落盘）与决策 ①
  （默认 MemorySaver），且与 ADR-0019 决策 ④ 同一条纪律：隐式默认会在没人要求持久化
  的场合（CI、评测、`atlas query`）把含业务数据副本的文件写进仓库。**这条不是纸上推理：
  变异 M2 把 `os.environ.get(..., "")` 改成带默认路径，`serving/state/checkpoints.sqlite`
  当场在仓库里被创建（20480 字节，跑完已清除）**——它已被 `.gitignore` 挡住所以不会被
  误提交，但「没人要求持久化时仍写出业务数据副本」这件事本身已经违背决策 ① 的承诺；
  而且这次变异跑的是 `.venv/bin/python -m unittest`（**不读 `.env`**，即 `make test` 的
  形态）——代码层默认值无法被「不配置」关掉，而 `.env.example` 层的推荐值留空即关。
  为什么 Makefile 也**刻意不** `export` 这个变量（与 ADR-0019 决策 ④ 的 `GIT_SHA` 相反）：
  实测 **进程环境高于 `uv run --env-file`**（同一个 `ATLAS_W8_PROBE`：父进程 unset 时取
  到 `from-env-file`、set 时取到 `from-parent`）——一旦 make 层给默认值，`.env` 里改成
  别的值会被静默盖住。单次覆盖走命令行（实测 make 会把命令行变量传进 recipe 环境）：
  `make serve ATLAS_CHECKPOINT_DB=/tmp/ckpt.sqlite`。
- 根 `.gitignore` 新增 `serving/state/`（紧邻既有 `serving/audit/`，`.gitignore:45-46`
  同纪律：运行时产物不入库）。目录不存在时 `mkdir(parents=True, exist_ok=True)`。
  **路径相对进程 cwd**（`make serve/ask` 在仓库根；api 镜像 `WORKDIR=/app`，Dockerfile
  已写明 cwd 约束），因此容器里的相对路径正好落在下面那个卷上。
- **compose**：`atlas-api` 服务新增 `serving/state` 卷挂载。理由：审计日志今天
  就因**无卷挂载**而困在容器内（实测改造前 `atlas-api` **没有任何 `volumes`**，
  `docker compose config atlas-api` 只有本次新增的那一条），checkpoint 若同样漏挂，
  「持久化」在容器里等于假的。落地为**命名卷** `atlas-api-state:/app/serving/state`
  而不是宿主目录绑定：checkpoint 内容是 Guard 出口 SQL + 结果行副本，不该落到仓库目录
  被宿主侧其他工具读走（N3 精神）。同时透传 `ATLAS_CHECKPOINT_DB: ${ATLAS_CHECKPOINT_DB:-}`
  ——**只透传不给值**（与 ADR-0019 决策 ④ 对 `GIT_SHA` 的处置同纪律），容器内留空即关。
  渲染实测（未设 / 设值两次 `docker compose config`）：未设 → `ATLAS_CHECKPOINT_DB: ""`，
  设值 → 原样进容器，卷 `source: atlas-api-state → target: /app/serving/state`。
- **只读边界**：checkpoint 由 LangGraph 写入，**不经 Guard**（不是 SQL 查询路径）；
  其内容是 Guard 出口 SQL 与结果副本，因此该文件的访问权限等同业务数据（N3 精神）。

> **落地状态（2026-09-14 工作项 8）**：本决策的通路是
> `agent/factory.py::checkpoint_saver_from_env()` → `create_live_agent(checkpointer=…)`
> ——判据清单里**没有**一条覆盖「env 变量真的变成图的 checkpointer」这段（原判据只管
> 默认侧与注入侧），故新增 `TestCheckpointDbEnv` 4 例补齐：未设/空串/纯空白 → `None`；
> 设路径 → 建父目录 + 建表 + 白名单内容齐全；`create_live_agent()` 两条（设 env →
> 图上是 `SqliteSaver`；留空 → 图上仍是 `MemorySaver` 且不建目录）。
> 变异实测：M2 代码层给默认路径 → 红「未设即不持久化」那 1 例；M3 注入分支漏
> `serde=` → 红「白名单内容」那 1 例；M4 删 `saver.setup()` → 红同 1 例（表不存在）。
> **一处与上游文档相抵触，如实登记**：`SqliteSaver.setup()` 的 docstring 写着
> "called automatically when needed and should not be called directly by the user"，
> 这里仍显式调一次——理由是要在构造期就把「文件可写、表可建」变成响亮失败（而不是等
> 第一次写 checkpoint 时才知道），且 M4 证明这条承诺有断言守着。若上游把自动调用改成
> 必需、或显式调用变得有害，按「LangGraph 大版本升级改变实现细节」那条的处置方式重验并
> 回填本节（该条原文只覆盖 allowlist 语义，`setup()` 的调用契约是**同类新增**，不混为已有）。

### ④ `thread_id` 按模型命名空间隔离

```python
# agent/graph.py:605 改为
thread_id = f"{self.model.name}:{sid}"          # atlas_finance_analytics / atlas_retail_analytics
config: dict[str, Any] = {"configurable": {"thread_id": thread_id}}
```

- `self.model.name` 是**语义身份**而非文件名（`agent/compiler.py:148-150` 注释：
  「与 ossie 文件名解耦——文件名是部署约定，模型名是语义身份」），正是命名空间该用的键；
- 对外 `TurnResult.session_id` 仍是原始 `sid`（HTTP 契约不变，`api.py:401` 的
  `(model_name, sid)` 二维键语义由此在 checkpoint 层真正落地）；
- `graph.py:172` 的 thread_id 约定 docstring 同步更新。

> **已落地（2026-09-14 工作项 8）**：`DataAgent.ask` 里 `config` 的 `thread_id` 已按
> 上式拼装（定位：`grep -n '"thread_id"' agent/graph.py`，得 `build_graph` 返回段文档 +
> `ask` 的 `config` 两处），`build_graph` 返回段与 `ask` 的 `session_id` 参数文档同步
> 写明「前缀由 `ask` 负责、对外仍是裸 `sid`」。`get_state` 一类直读 thread 的用法
> 只出现在测试里（`tests/test_session_persistence.py` 的 `_thread()` helper 与本约定同源），
> 生产侧唯一拼装点就是 `ask`。

### ⑤ `turns` 提进 `TurnState`，删除 `_session_turns` 与 `sessions` property

- `TurnState` 新增 `turns: int`；
- `node_plan`（首个节点）读 `state.get("turns", 0) + 1` 并随返回值写回——
  **与 `last_plan` 完全同构**（`graph.py:222` 读 / `:408` 写），复用已被证明的
  跨轮状态机制，不新增 reducer（`TurnState` 现有字段全部无 `Annotated` reducer）；
- `turn_from_state` 的 `turns` 参数改为从 `state["turns"]` 读取（保留参数默认值
  `turns=1` 以兼容既有直调测试）；
- 删除 `DataAgent._session_turns`（`graph.py:579`）与 `sessions` property
  （`graph.py:626-629`）；`tests/test_graph.py:498-499` 两处断言改为查
  `TurnResult.turns_in_session`（唯一外部消费点，实测仅 2 处）。

> **已落地（2026-09-15 工作项 9）**：三处落点与原文一致或更窄，逐条登记。
> ① `node_plan` 的返回值里多出一个计算键 `"turns": int(state.get("turns") or 0) + 1`
> （定位：`grep -n '"turns"' agent/graph.py`）——它刻意**不在**本节点开头的冲刷集合里
> （冲刷清的是上一轮终点残留；turns 属承接）；`or 0` 兜住首轮键不存在与 None 两种形态。
> ② `turn_from_state` 的组装改为 `int(state.get("turns") or turns)`，签名与默认值
> `turns=1` 不动（直调桩状态 `{"question": "x"}` 仍走兜底，既有用例零改动）。
> ③ `_session_turns` 与 `sessions` property 已删除；`tests/test_graph.py` 那两处断言
> 没有改写成 `turns_in_session` 的重复检查（同一函数里 `r1/r2/r3.turns_in_session`
> 已逐轮断言），落成一句 `assertFalse(hasattr(DataAgent, "sessions"))`——**判据 5 原文
> 写的 `assertNotHasattr` 不是 unittest 的方法**（实测 AttributeError），见判据 5 落地注。
> 本 ADR 提到的 `graph.py:579` / `:626-629` 两个行号指针已随本次改动失效（符号口径
> 见「行号口径」注）。

### ⑥ 身份指纹入 checkpoint，删除 `holder["sessions"]`

C2 硬化（同会话换身份 → 422）必须**跨重启**成立，否则持久化会话 + 进程内指纹
= 「会话续上了、身份校验却重置了」的新不一致。做法：

- `TurnState` 新增 `session_fingerprint: str`（存 `_claims_fingerprint(claims)` 的
  **哈希值，不存 claims 本体**——ADR-0011 决策：不外泄身份细节）；
- 首轮由 `node_plan` 写入；后续轮 `DataAgent.ask` 在 invoke **之前**用
  `self._graph.get_state(config)` 读出既有指纹，不一致则抛类型化异常
  `SessionIdentityConflict`；
- `serving/api.py` 捕获该异常 → 422 + `audit_log.record(kind="conflict", status=422)`
  （审计契约不变），删除 `holder["sessions"]` 与 `api.py:400-414` 的进程内绑定逻辑；
- **每轮仍独立下推 identity**（`ask(identity=claims)` 经 invoke config 传递、
  不落 checkpoint）——持久化的只是**校验用哈希**，不是授权用身份。授权永远用本轮
  token 的 claims，避免「用昨天的高权限继续今天的查询」。

> **已落地（2026-09-15 工作项 9）**：指纹落成 `serving/auth.py::claims_fingerprint()`
> ——`sha256(canonical JSON)` 的 64 位十六进制（`hashlib` 是 auth 模块既有 import，
> 无新依赖）；`agent/graph.py` 早已 `from serving.auth import AuthError, resolve_claims`，
> 故调用方向不是新增分层。`serving/api.py` 删掉 `_claims_fingerprint` 与
> `holder["sessions"]` 整块，另删 `import json`（实测删除前 api.py 里 `json.` 只此一处，
> 留着就是 F401），改为捕获 `SessionIdentityConflict` → 422 + `audit(kind="conflict",
> status=422)`（审计契约逐字不变）。
> **与原文两处不同，如实登记**：
> ① 原文「首轮由 `node_plan` 写入」落成「**首轮由 `ask` 经 invoke payload 写入**」
> （键 `session_fingerprint`，与 `question`/`session_id` 同构）——校验本来就在 `ask`
> 的 invoke 前，若让节点也写一遍就是同一值两处计算。**这处换法有一个实测的硬约束**：
> LangGraph 对 invoke 输入里**不在 `TurnState` 的键是静默忽略**的（探针：多传
> `unknown_key` 后 `final` 里没有它）——所以该字段必须先加进 `TurnState`；漏加不会报错，
> 只会让指纹从不进 checkpoint、`bound` 永远为 None、**永不冲突**，正是本文档反复要
> 消灭的静默失效形态。
> ② 校验用 `self._graph.get_state(config).values.get("session_fingerprint")` 读——
> **空 thread 不抛**（实测 `StateSnapshot(values={}, next=())`），故首轮天然是「无绑定 →
> 采用」，无需先判 thread 存在。读的次数：identity 非 None 的轮每轮一次（CLI 轮不读），
> 与代价 ⑥ 同条。
> **类型面一处收紧**：`ask` 里的 config 标注为 `RunnableConfig`（裸 `dict` 实测被 mypy
> 判为 `invoke`/`get_state` 双「无匹配重载」）——无参 mypy 因此由 42 降到 **39**（13
> 文件 / 47 源文件，未新增错误；graph.py 唯余 `RunnableConfig` 不显式导出一条为基线既有）。
> **测试夹具的必要变更**：`tests/test_api_hardening.py` 的 fake factory 由「两个域共用
> 一个 DataAgent」改为**按域各建**（与真实 `_live_agent` 同形）——旧实现指纹键含 model，
> 共用实例不会串；新实现的跨域隔离靠 thread_id 前缀，共用实例会让 retail 首启被 finance
> 的绑定误判为冲突。

### ⑦ `/health` 暴露 `boot_id`

进程启动时生成一次 `BOOT_ID = uuid4().hex`，`/health` 返回
`{"boot_id": BOOT_ID, ...}`（与 ADR-0019 决策 ⑥ 的快照字段同批扩展）。

用途：前端可据此判断「服务端是否重启过」——即使会话已持久化，限流桶与 OTel
聚合仍随重启归零，`boot_id` 变化是唯一可靠信号；审计排查时也能把 JSONL 行按
进程生命周期分段。`boot_id` **不是**会话标识，不参与任何鉴权。

**实施裁定（2026-09-14 工作项 6 落地）**：

1. **刻意不从 env 读**（初稿未写，落地时补为断言）。可注入的 boot_id 不再是
   「进程身份」而是「又一个可伪装的标签」——运维可以把 `ATLAS_BOOT_ID=stable`
   写进 compose，于是重启永远不可见，本决策要消灭的静默失忆换一个位置复活。
   因此 `serving/api.py` 里 `BOOT_ID` 必须是模块级 `uuid4().hex`，且
   `tests/test_identity_echo.py` 按文本断言行内不出现 `environ`。
2. **与 0019 决策 ⑥ 同批改键集，不是先后两批**：`/health` 的 7 + 1 = **8 键**
   是一次落地完成的（ADR-0022 决策 ① 之后照抄为权威清单并声明不再增删键），
   降级路径（无快照可绑）也返回同一 8 键集——`boot_id` 在 degraded 时**尤其**有用。
3. **不进鉴权、当前也不进审计记录**（实测生产代码里 `boot_id` 只出现在
   `serving/api.py` 这一个文件，`serving/audit.py` 的 JSONL 行没有该字段）。
   登记为裁定而非既成事实的理由：
   审计行已有 `session_id` 与进程无关的身份指纹，若把 boot_id 升成关联主键，
   跨重启的会话会被切成两条时间线，正好与决策 ⑤ 的 `turns` 单一事实源相冲突。
   「按进程生命周期分段排查」因此是**消费方**（读 `/health` 前后对比）的能力，
   不是本仓已产出的字段——写清这条边界，以免日后有人把「用途」读成「已实现」。

### ⑧ workers=1 不解除，理由收窄（ADR-0012 决策 1 的落地注记）

ADR-0012:37-39 的原文理由是「checkpointer（MemorySaver）与 `DataAgent._session_turns`
是进程内状态，多 worker = 会话分裂」。本 ADR 之后：

| 状态 | 是否仍进程内 | 多 worker 后果 |
|---|---|---|
| 会话轨迹 | ❌ 已持久化（SQLite） | 不分裂 |
| `turns` | ❌ 已入 checkpoint | 不分裂 |
| 身份指纹 | ❌ 已入 checkpoint | 不分裂 |
| **限流桶** | ✅ `RateLimiter._hits`（`ratelimit.py:33`） | **每 worker 一份 → 实际配额 = N × 60/min** |
| **审计 JSONL** | ✅ 进程内 append + flush（`audit.py:52` docstring 明写「单 worker 无锁冲突」） | 多进程交错写，行完整性无保证 |
| Agent 单例 | ✅ `holder["agents"]` | 每 worker 各建一份（内存 ×N，且各自持有 SQLite 连接） |

**结论不变（workers=1），理由收窄为「限流桶 + 审计写 + SQLite 单写者」**。
ADR-0012 推翻条件第 1 条（「多 worker 需求 → Postgres checkpointer」）**未触发**：
本 ADR 的目标是「重启不失忆」，不是「横向扩展」；若将来真要解除 workers=1，
必须先解决限流与审计的跨进程一致性，且届时应按推翻条件改走 Postgres（SQLite 的
单写者锁在多进程下会成为瓶颈）。

按 ADR-0011:86 / ADR-0012:89 的既有先例（「沿 0005 增补口径先例，不改裁定正文」），
本节以**落地注记**形式追加到 ADR-0012 末尾，正文不改。

### ⑨ 新增依赖登记（AGENTS.md §5 四步）

| 包 | 版本（dry-run 实测） | 角色 | 许可证 |
|---|---|---|---|
| `langgraph-checkpoint-sqlite` | 3.1.1 | 直接依赖：`SqliteSaver` | ✅ **MIT**（`License-Expression: MIT`，随附 `licenses/LICENSE` 首行「MIT License / Copyright (c) 2024 LangChain, Inc.」，**字段与文件互证**） |
| `aiosqlite` | 0.22.1 | 传递（异步接口，同步路径不用） | ✅ **MIT**（无 `License-Expression`，但有 `Classifier: License :: OSI Approved :: MIT License`，随附 `licenses/LICENSE` 首行「MIT License / Copyright (c) 2022 Amethyst Reese」） |
| `sqlite-vec` | 0.1.9 | 传递（向量扩展，**本项目不用**） | ✅ **MIT OR Apache-2.0**（仅 `License: MIT License, Apache License, Version 2.0`；⚠️ **证据较弱**，见下方注） |

**门禁结论（2026-09-14 实测，验证方式 1 已达成）**：三包**均为 permissive**，
不触发推翻条件第 3 条，**P-1 可开工**。体积实测：`sqlite-vec` = 164K（包）+ 20K
（dist-info），含 **1 个**平台二进制 `vec0.dylib`；另两包为纯 Python。

> **两项必须如实登记的实测发现**：
>
> 1. **`sqlite-vec` 的许可证声称无随附文件佐证**：其 `dist-info/` 只有
>    `INSTALLER` / `METADATA` / `RECORD` / `REQUESTED` / `WHEEL` / `top_level.txt`，
>    **无 `licenses/` 目录也无 LICENSE 文件**（另两包均有）。故它的 permissive 判定
>    **只基于 METADATA 一行自述**，证据强度低于另两包。按本 ADR 判据 1 的口径
>    （读 `METADATA` 的许可证字段）判定成立，但该差异不得隐藏；若 P0a（ADR-0023）
>    的 `make license-check` 要求随附文件级证据，它会是首个命中项。
> 2. **`sqlite-vec` 是声明但零使用的依赖**：`Requires-Dist: sqlite-vec>=0.1.6` 是硬声明，
>    但 `grep -rn "sqlite_vec" .venv/…/langgraph/checkpoint/sqlite/` = **0 命中**，且
>    **卸载它后 `SqliteSaver` 仍可导入、`setup()` 仍成功建出 `checkpoints` / `writes`
>    两表**（实测）。即不仅「本项目不用」（本 ADR 原判定），**上游包自身也不用**。
>    仍**不**走 `--no-deps` 排除，理由：决策 ⑨ 的排除条件是「许可证**或**体积不可接受」，
>    两项实测均可接受，条件未触发；擅自排除会违反上游依赖契约，且 `uv sync` 无部分
>    排除能力（需 override，复杂度不划算，§10 优先级 5 简洁）。已装回保持与声明一致。

- **理由**：官方 checkpointer 实现，避免自研 checkpoint 链语义（备选方案第 4 行）；
- **预算**：零成本（stdlib `sqlite3` + 单文件，无常驻服务、无云资源）；
- **许可证**：`uv pip install` 后逐包读 `site-packages/*.dist-info/METADATA` 的
  `License-Expression`，回填上表——**未回填不得声称评估完成**（与 ADR-0018 决策 ①
  同一纪律）。若任一包为非 permissive，改走备选方案第 2 行（前端 localStorage +
  失忆显式提示）；
- **`sqlite-vec` 是意外的传递依赖**：它带一个平台相关的二进制扩展，本项目完全不用。
  若其许可证或体积不可接受，评估 `--no-deps` 或向上游提 issue；本 ADR 记录该事实，
  不预设结论。

---

## 理由

1. **静默失败比失败更贵**：备选方案第 2 行（我最初的推荐）能把失忆从静默变成可见，
   但代价是「后端能力实质退化、前端假装记得」——`last_plan` 指代消解（ADR-0014 ②）
   是已实现且有测试的能力，让它随重启归零而界面照常显示历史，是更隐蔽的诚实性
   问题。用户选择了成本更高但不撒谎的路径。
2. **复用既有正确机制，不发明新的**：`turns` 与身份指纹都走 `last_plan` 已经证明
   可用的跨轮状态通道（`graph.py:222/408`），而不是新增 reducer、新增存储、
   新增同步逻辑。
3. **默认不变 = 风险可控**：`build_graph` 默认仍 `MemorySaver`，持久化由部署方
   显式开启。这让 40 处既有构造点（决策 ① 实测注）、CLI 的 `plan/compile` 路径（不碰 DB，
   `factory.py:4-6` 注释）完全不受影响，把变更面收敛到 `create_live_agent` 与
   `serving/api.py`。
4. **`thread_id` 隔离必须在同批解决**：它是持久化**引入**的新失效模式（今天因
   各持独立 MemorySaver 而侥幸不成立）。分两批做等于中间态存在跨域串话窗口。
5. **不把 workers=1 说成已解除**：这是本 ADR 最重要的诚实性动作。持久化解决的是
   「重启」，不是「多实例」；限流桶与审计写仍是进程内，结论不变、理由收窄，
   并以落地注记而非改正文的方式记录（ADR-0011/0012 先例）。

---

## 代价与限制

① **会话历史不可由 git 复现**：DB 文件 gitignore（N3/N9 精神），因此「某个
session 说了什么」无法从仓库还原。可复现性只保留在**审计 JSONL**（每请求一行，
8 字段契约锁定）与**评测报告**（绑 sha）上。这与 AGENTS.md 决策优先级第 3 条
（可复现）有张力：**取舍是「会话内容属运行时数据，不属代码事实源」**。

② **SQLite 单写者**：**最常见的共享形态不是跨进程，而是同进程内两条连接**——
`serving/api.py` 按域懒建 finance / retail 两个 Agent，各自经 `create_live_agent()`
打开同一个文件。跨进程再加一种：同时跑 `make serve` 与 `atlas ask`，两者读同一个
`ATLAS_CHECKPOINT_DB`（不设 = 两者都不落盘，谈不上争锁）。缓解措施已实测：
`sqlite3.connect()` 不传 `timeout` 时默认 **5000 ms**（`PRAGMA busy_timeout` 回读
5000），写冲突表现为**等 5.17s 后抛 `OperationalError: database is locked`**（不是
无限挂起）；上游 `setup()` 把库切成 **WAL**（`sqlite/__init__.py:141`），故读不阻塞写、
写仍串行。**并发写未压测**（两个连接各问一句不构成压力）——若真出现该异常，出路是改为
按进程分文件（`checkpoints-{boot_id}.sqlite`）或显式指定不同 `ATLAS_CHECKPOINT_DB`，
**不预设结论**。

③ **DB 文件随时间无界增长**：checkpoint 存全量状态（含 `rows`，最多
`MAX_TABLE_ROWS` 行）。当前**无清理策略**——不实现 TTL/GC（超出本批次范围，
且删数据与「可回溯审计」冲突）。登记为已知限制，进 README KL #28 的收窄改写。

④ **CLI 与 HTTP 共享同一 DB**：`atlas ask`（交互式多轮）与 `/ask` 都走
`create_live_agent` → 同一文件。会话键是随机 `session-{hex8}`（符号定位：
`grep -n 'session_id or' agent/graph.py`，本批实测 1 处命中），碰撞概率可忽略，
但「CLI 的历史出现在 HTTP 会话列表里」是设计后果而非缺陷；
若需要隔离，用 `ATLAS_CHECKPOINT_DB` 分别指定。
**决策 ④ 落地后的补充（2026-09-14）**：`thread_id` 带模型前缀使**跨域**不再互相覆写，
但**同域同 `sid` 仍是同一条 thread**（CLI 与 HTTP 撞键时两边续写同一会话）——这是共享
文件的既定语义，不是隔离失效；判据 4 的行为面断言测的正是这条边界的另一侧。

⑤ **序列化白名单是隐性耦合**：`TurnState` 将来新增任何自研 dataclass（例如
ADR-0017 的 `ComparisonSpec`）都必须同步进 `_CHECKPOINT_SERDE`，否则多轮状态静默退化。
**原稿据此写的「它现在嵌在 `Plan` 内，随 `Plan` 一起注册，尚不需单列」已被 2026-09-14
实测证伪**（决策 ② 实测注第 1、2 条）：`ComparisonSpec` 与 `Filter` 当时都**不在**白名单里，
跨轮读回即为 `dict`，`Filter` 那一项的下游症状是「追问被伪装成反问」——两项现已补入，
并由判据 7 的三条断言（内容面 / serde 行为面 / 真图多轮面）锁死。
**仍然无自动检查**：`TurnState` 新增 dataclass 字段时不会有任何机制提醒你回去加白名单，
补偿只有两件事——判据 7 的契约测试（新增字段一旦进 checkpoint 就会红）与
`graph.py` 注释里那条「白名单非空 = 未注册即降级」的口径。

⑥ **`get_state` 增加一次读**：决策 ⑥ 在每轮 invoke 前读一次 checkpoint 状态
（身份指纹校验）。相对一次 Doris 查询（实测 `latency_ms` 数百毫秒量级）可忽略，
但**未单独 profile**，不声称具体开销。
**落地细化（2026-09-15 工作项 9）**：只对 identity 非 None 的轮读（CLI 轮零开销）；
读的是已有 thread 的状态，不含额外 SQL 往返之外的动作。

⑦ **`sessions` property 是公开 API 的破坏性删除**：实测仅 `tests/test_graph.py:498-499`
消费，但它是 `DataAgent` 的公开成员。删除属破坏性变更，需在 commit message 的
破坏性变更摘要中登记（AGENTS.md §8）。
**已落地（2026-09-15 工作项 9）**：property 与 `_session_turns` 一并删除（对外
替代面 = `TurnResult.turns_in_session`，HTTP 契约无该字段之外的变化）；
`tests/test_graph.py` 的消费点改为 `assertFalse(hasattr(DataAgent, "sessions"))`，
另有专类 `TestTurnsSingleSourceOfTruth` 锁「新实例接着数」。

⑧ **`sqlite-vec` 传递依赖带入平台二进制**：本项目不使用它，但它会随
`langgraph-checkpoint-sqlite` 一起装进 `.venv` 与镜像（体积与平台兼容性）。
决策 ⑨ 已登记，许可证与体积**已于 2026-09-14 实测回填**（三包均 permissive；
`sqlite-vec` 184K + 1 个 `vec0.dylib`）。**残留限制**：`vec0.dylib` 是 macOS arm64
产物，镜像内为 Linux x86_64/arm64，**跨平台可用性本批未实测**（无 Linux 环境）；
因上游与本项目均零引用它，预期不影响功能，但这是**推定而非实测**，不得当结论用。
若镜像构建失败，退路为决策 ⑨ 的 `--no-deps`（此时排除条件因「平台兼容性不可接受」
而被触发，与本批「不排除」的决定不矛盾）。

---

## 什么情况下应该推翻

- **出现多 worker / 多实例部署需求** → 本 ADR 的 SQLite 选型作废，按 ADR-0012
  推翻条件第 1 条改 Postgres checkpointer，并**同时**解决限流桶与审计写的跨进程
  一致性（否则 workers=1 仍不能解除）；
- **会话历史需要成为可复现事实源**（例如要把多轮对话纳入评测集或审计留档）→
  决策 ③ 的 gitignore 与代价 ① 冲突，需改为「导出的会话快照入库」而非「DB 入库」；
- **`langgraph-checkpoint-sqlite` 许可证实测为非 permissive，或 `sqlite-vec`
  不可接受** → 回退备选方案第 2 行（前端 localStorage + `turns_in_session`
  一致性校验 + 失忆显式提示），并保留决策 ④⑤（thread_id 隔离与 turns 单一事实源
  与持久化无关，独立成立）；
- **LangGraph 大版本升级改变 `JsonPlusSerializer` 的 allowlist 语义** →
  决策 ② 的构造参数方式需重验。**已锚定的重验基线（2026-09-14，`langgraph 1.2.11` +
  `langgraph-checkpoint 4.2.0`）**：升级后逐条复测决策 ② 实测注的 6 项，任一项变即触发
  本条——其中两项已有自动化哨兵：`_allowed_msgpack_modules` 属性消失时
  `tests/test_session_persistence.py::_allowlist()` 显式 `AssertionError`（不让断言静默
  通过），白名单内容不足 6 项时 `test_default_memory_saver_carries_the_allowlist` 红；
  另四项（blocked vs 警告、三态语义、`jsonplus_serde` 是否仍为死属性、strict 开关的
  求值时机）目前只有本 ADR 的文字记录，升级时需人工重跑探针；
- **需要跨会话检索/分析**（例如「列出我所有会话」并全文搜索）→ SQLite checkpoint
  的内部表结构是 LangGraph 私有实现细节，不应被业务查询依赖；届时需另设会话索引，
  而不是直接读 checkpoint 表。

---

## 验证方式

**依赖与许可证（P-1 前置，未过不得开工）**：

1. `uv add langgraph-checkpoint-sqlite` 后逐包读
   `.venv/lib/python3.11/site-packages/{langgraph_checkpoint_sqlite,aiosqlite,sqlite_vec}*.dist-info/METADATA`
   的 `License-Expression`，回填决策 ⑨ 表格的 3 个 ⬜；任一非 permissive → 触发
   推翻条件第 3 条。
   > **已达成（2026-09-14）**：三包均 permissive，表格 3 个 ⬜ 已回填为 ✅，未触发
   > 推翻条件。**两处方法差异如实登记**：(a) 实际先用 `uv pip install`（只入 `.venv`）
   > 而非判据写的 `uv add`，因为 `uv add` 会立即改写 `pyproject.toml` + `uv.lock`，
   > 而门禁语义是「判定不通过则停批」——先装到 `.venv` 读完许可证再写入声明文件，
   > 才能保证停批时**零文件残留**；(b) 仅读 `License-Expression` 不够：`aiosqlite` 与
   > `sqlite-vec` **无该字段**（只有 `Classifier:` 与 `License:`），故判据口径应扩为
   > 「三个字段任一 + 随附 `licenses/` 文件互证」，否则会把 2 个 MIT 包误判为未知。
2. `make lint && make test` 在**未设置** `ATLAS_CHECKPOINT_DB` 时全绿，且
   `git status` 无新增未跟踪文件（证明默认路径不落盘、40 处构造点未受影响）。
   **落地状态（工作项 7 起步、工作项 8 收口，2026-09-14）**：前半句已绿
   （`make lint` 5 项全过、`make test` 663 例 OK(skipped=14)，全程未注入 env）。
   后半句的两处原因现已分别处置：
   ① `serving/state/` **已**进 `.gitignore`（实测 `git check-ignore -v` 命中
   `.gitignore:50`），并且这条承诺转成了自动化断言 `test_state_dir_is_git_ignored`——
   它问的是「这个路径会不会被忽略」而不是通读 `git status`，因此不受他人未跟踪文件的
   干扰；带一条反向对照（已跟踪的 `serving/api.py` 必须**不**被忽略），否则「命令跑
   成功即全绿」会把它变成空断言；非 git 检出时 `skipTest` 而非失败。
   ② 「无新增未跟踪文件」作为 `git status` 的人工判读仍不进 `make test`；代码面由
   `test_default_construction_creates_no_state_file` 守（默认构造前后取 `serving/state/`
   目录差集，落任何文件即红）。两条合起来覆盖「默认不落盘」+「落了也不会被误提交」，
   **仍不覆盖**「把默认持久化写到仓库外别处」的形态（判据口径如实保留这一缺口）。
   变异实测：M2 在代码层给默认路径 → 红 `test_unset_or_blank_env_means_no_saver`
   （且真的在仓库里造出了文件，见决策 ③ 的同条证据）；M5 删 `.gitignore` 那一行 →
   红 `test_state_dir_is_git_ignored`。

**契约测试（无 DB 依赖，进 `make test`）**：

3. `build_graph()` 无参 → `type(compiled.checkpointer) is MemorySaver`；
   传 `checkpointer=X` → `compiled.checkpointer is X`（决策 ①）。
   **落地状态（2026-09-14 工作项 7）**：已由 `tests/test_session_persistence.py`
   覆盖并加严两处——① 判据用 `is None` 而非真值判定，故补 `EmptySaver(__len__=0)`
   用例把这条理由变成契约（`or` 写法会把显式开启的持久化静默换回 MemorySaver）；
   ② 默认分支也断言 serde 白名单内容（判据原文只管实例，漏传 `serde` 不在其覆盖内）。
   变异实测：`checkpointer or …` → 红 1；忽略入参 → 红 4；默认分支漏 `serde` → 红 1；
   `DataAgent` 不透传 → 红 1。
4. `thread_id` 命名空间：同一 `sid` 分别在 finance / retail 模型上 `ask` 两轮，
   断言两个 thread 的 `last_plan` 互不可见（用 fake executor，`tmp_path` DB）。
   **这条专门锁死决策 ④ 的回归**——去掉命名空间后该用例必须失败。
   **落地状态（2026-09-14 工作项 8）**：由 `TestThreadIdNamespace::
   test_same_sid_lands_on_per_model_threads` 覆盖，但**「互不可见」按字面不可执行**，
   如实登记并纠正（先写成字面版的我也踩了：实现正确时那条断言反而红）——两个 thread
   存在**同一个** checkpoint 文件里（共用 saver 正是本用例的前提，复刻生产形态），
   任何一方按对方的 `thread_id` 都查得到，所以「在 retail 图上查 finance 的 thread
   应为空」这种断言永假。改用两件真正可证伪的事：
   - (a) **存储层**：直接查 `checkpoints` 表的 `DISTINCT thread_id`，必须恰为
     `{atlas_finance_analytics:<sid>, atlas_retail_analytics:<sid>}` 两条
     （为什么查表不走 `saver.list()`：实测其 `config` 是必填位置参数）；
   - (b) **行为层**：finance 先问、retail 后问，再用同 `sid` 在 finance 上追问
     「那 2014 年呢」→ `metric` 必须仍是 finance 首轮那个指标。
     **测先问的那一侧是必须的**：同一 thread 上的读回由**后写者**决定（实测：两个 Agent
     写同一 thread 两次，`get_state` 读回的是第二次的 `question`），所以裸 `sid` 时
     retail 侧看不出异常，只有 finance 的追问会补到 retail 的计划。
   变异实测 M1（去掉 `f"{self.model.name}:{sid}"` 的前缀）→ 红 1 例，正是本用例。
   **一处自觉的耦合，登记为代价**：(a) 直读 `checkpoints` 表，与「什么情况下应该推翻」
   末条（内部表结构是 LangGraph 私有实现细节，不应被业务查询依赖）同向——该条禁的是
   **业务**查询，测试侧直读是有意的响亮失败（上游改表名/列名时本用例红，提示重验），
   但它确实是判据 4 里唯一依赖私有 schema 的一环；(b) 那条行为层断言不依赖表结构。
5. `turns` 单一事实源：同一 `sid` 连续 `ask` 3 轮 → `turns_in_session` 依次
   1/2/3；`DataAgent` 无 `sessions` 属性（`assertNotHasattr`）；
   **新建一个 `DataAgent` 指向同一 DB 文件**再 `ask` → `turns_in_session == 4`
   （证明轮数来自 checkpoint 而非进程内 dict）。
   **落地状态（2026-09-15 工作项 9）**：已达成，两处与原文不同如实登记：
   ① `assertNotHasattr` **不是 unittest 的方法**（实测 AttributeError），落成
   `assertFalse(hasattr(DataAgent, "sessions"))`；
   ② 「新实例」同判据 6 口径 = 新 `sqlite3.Connection` 打开同一文件（不是 spawn 进程）。
   覆盖面：`TestTurnsSingleSourceOfTruth` 3 例——三连 1/2/3（第 2 轮用残句追问，
   走的正是跨轮状态）、状态里 `turns == 3` 且新实例第 4 轮、`sessions` 不存在。
   变异实测：M3（`"turns"` 恒为 1）→ 红 2 例；M4（组装忽略状态、回到调用方记账的
   形态）→ 红 2 例——两次红的都是「三连」与「新实例第 4 轮」，`sessions` 不存在那条
   两边都不红（它锁的是删除动作，不是计数机制）。门禁账：`make test` 671 例
   `OK (skipped=14)`（663 + 本项 8 例：turns 3 + 指纹 5）。
6. 重启不失忆：Agent A 问「2015 年总交易额」→ Agent B（新实例、同 DB、同 sid）
   问「那 2014 年呢」→ 断言 `kind == "answer"` 且 `metric` 与首轮一致
   （`last_plan` 指代消解跨实例生效，ADR-0014 ②）。
   **落地状态（2026-09-14 工作项 8）**：代码面由
   `TestThreadIdNamespace::test_new_agent_on_same_db_continues_the_session` 覆盖，
   两处与判据原文不同、如实登记：① 首轮问句用的是本文件既有的 finance 问句
   （`FINANCE_Q`）而非「2015 年总交易额」——机制相同（跨轮 `last_plan` 补全），
   换问句只为复用同一套 fixture；② 「新实例」落成了**新 `sqlite3.Connection` 打开同一
   文件**（不是 spawn 新进程），它等价于「进程重启后重开文件」的存储语义，但**不覆盖**
   解释器级冷启动（如 `LANGGRAPH_STRICT_MSGPACK` 之类的启动期求值开关）。真链的
   `/ask` 跨重启半句仍留 P-1 收口（**2026-09-16 收口已验通**，见判据 10 收口注）。
   **这条不断言命名空间**（两个实例同域，裸 `sid`
   也能过），因此它不能替代判据 4——反向也成立。
7. 序列化白名单往返：写入含 `Plan(TimeSpec, OrderSpec, ComparisonSpec)` 的状态后
   读回，断言类型仍是自研 dataclass 而非 dict（决策 ②；`ComparisonSpec` 随 `Plan`
   嵌套注册，ADR-0017）。
   **落地状态（2026-09-14 工作项 7）：本条判据当场抓出一个既有缺陷**——括号里
   「随 `Plan` 嵌套注册」的假设不成立（决策 ② 实测注第 1、2 条），实测首轮按原判据
   写出的断言即红：`filters[0]` 与 `comparison` 往返后是 `dict`。处置与覆盖：
   - `TestCheckpointStateFidelity::test_nested_plan_types_survive_the_default_serde`
     用**图真正持有的那个** serde 实例往返完整 `Plan`（含 `Filter`），四个嵌套字段逐一
     断言类型——刻意不 `from agent.graph import _CHECKPOINT_SERDE`，否则「新建同内容
     实例」与「漏传」再次无法区分；
   - `test_multiturn_followup_keeps_the_filter_predicate` 走真图两轮（gold-151 问句 →
     「那 2014 年呢」），断言第 2 轮 `kind=answer` 且 SQL 仍含阈值 `10000000`。它证明
     这不是 serde 层的纸面问题：修复前该用例红在 `kind='clarify'`，原因链是
     `'dict' object has no attribute 'column'`；
   - 变异实测 M5：只从 `_CHECKPOINT_SERDE` 删 `ComparisonSpec` → 红 2 例（行为面 +
     内容面各一）；M6：删 `Filter` + `ComparisonSpec`（= 修复前形态）→
     `FAILED (failures=3)`，三条正是 `test_multiturn_followup_keeps_the_filter_predicate`
     / `test_nested_plan_types_survive_the_default_serde` /
     `test_default_memory_saver_carries_the_allowlist`。两次均按「读原始字节 → 变异 →
     `finally` 写回 → md5 核验」执行（本批全部未提交，不可用 `git checkout` 恢复）。
   **未覆盖部分（如实登记）**：以上都在 permissive 默认模式下测。严格模式
   （`LANGGRAPH_STRICT_MSGPACK=true`）的往返断言**未进自动化**——该开关只在进程启动时
   求值（实测注第 5 条），要覆盖必须 spawn 子进程，本批未做。风险边界：严格模式下
   已注册类型行为与 permissive 一致（实测注表第 2 行的对照组），差别只在未注册类型是
   「降级」还是「报错」，而内容面断言已保证 6 项全覆盖，故不补不阻塞收口；若 langgraph
   升级改变该语义，触发推翻条件第 4 条。
8. 身份指纹跨重启：Agent A 用 `hq_admin` 指纹建会话 → Agent B（新实例、同 DB）
   用 `branch_manager` 指纹 + 同 `sid` → 抛 `SessionIdentityConflict`；
   HTTP 层断言 422 + 审计行 `kind="conflict", status=422`（决策 ⑥，替换
   `tests/test_api_hardening.py` 现有的进程内指纹用例）。
   **落地状态（2026-09-15 工作项 9）**：已达成。Agent 面 =
   `TestIdentityFingerprintInCheckpoint` 5 例：冲突在 invoke 前抛出（**冲突轮不执行
   SQL、不写状态、不推进轮数**三条断言）、同身份第二轮无假冲突（按内容不按对象身份）、
   存的是 64 位 sha256 摘要且跨轮稳定、**新连接 + 新 Agent + 同文件**后换身份仍冲突且
   同身份仍续接、无身份轮（CLI 语义）不落指纹（之后带身份的首轮可正常采用）。
   HTTP 面 = `tests/test_api_hardening.py` 既有三例（422 冲突、按域隔离、审计行），
   实现路径已从进程内表换成 checkpoint——**用例名与断言未改**，只是红灯的成因变了
   （见该文件 fake factory 的必要变更，决策 ⑥ 落地注）；「真进程重启两次」的 HTTP
   用例**未加**：它属判据 10 的真链（需 Doris），本项的跨重启证据止于「新连接 + 新
   实例 + 同文件」。
   变异实测：M1（关掉冲突校验）→ 红 5 例（Agent 面 2 + HTTP 面 3）；M2（指纹存
   claims 本体而非哈希）→ 红 1 例（摘要形态）；M5（api 不捕获异常）→ 红 3 例（HTTP
   面）。三处均按「原始字节 → 变异 → `finally` 写回 → md5 核验」执行。
9. `/health` 含 `boot_id`，且**同进程内两次调用值相同**、新进程不同（决策 ⑦）。
   **落地状态（2026-09-14 工作项 6）**：前半句与「形状」由
   `tests/test_identity_echo.py::TestBootId` 覆盖（同进程两次相等 + `^[0-9a-f]{32}$`
   + 与模块常量同值）。「新进程不同」**不起子进程**，改用 `importlib.reload` 断言
   模块体重跑即换值——它测的是造成跨进程差异的唯一机制（求值时随机生成），
   因此写死常量、读 env、落缓存文件三种「跨进程恒定」的实现都会被打红
   （实测变异：`BOOT_ID` 改成固定 hex → 恰红该 1 例）。真正的两次 spawn 进程比对**已在
   2026-09-14 于宿主机补验**（起两次 uvicorn：`b5900113236849919c3958eb81fa1980` →
   `66420615bf9b4e988b8d7e9e4a0f3f0f`，其余 7 键连同键序一致；证据同 ADR-0019 真链
   判据 7 的落地注）——**且它不需 Doris**，因为 `/health` 不构造 agent。该比对不属
   `make test`，是手工复跑证据，故本判据在自动化面仍只锁机制，不声称覆盖跨进程取值。

**真链判据（需 Doris，进 `make api-verify`）**：

10. `make serve` 后 `/ask` 两轮（同 `session_id`）→ 第二轮残句「那 2014 年呢」
    返回 answer；**杀掉 uvicorn 重启**，用同一 `session_id` 发第三轮残句 →
    仍返回 answer 且 `turns_in_session == 3`（今天实测为 1）。

    > **已落地（2026-09-16 P-1 收口真链；宿主机与容器双路径）**：本地 `make serve`
    > 同参起服务（`ATLAS_CHECKPOINT_DB` 指向本地会话 DB）+ 同一 token：三轮实测
    > `turns_in_session` **1 → 2 → 3**（第 2/3 轮为残句，走跨轮 `last_plan` 补全，
    > 全程 `kind="answer"`、`metric` 稳定），杀进程重启后第三轮 = 3——「今天实测
    > 为 1」的缺口闭合（改造前重启后该轮会从 1 静默重报，正是本判据立项时要消灭的
    > 形态）。
    > 收口复验在同一会话上继续续接：重启后第 4、第 5 轮 = **4 → 5**（跨 4 次进程
    > 启动），状态完整在 SQLite 中。容器路径同验：`docker compose down && up` 前
    > 后 `turns` **4 → 5**（会话 `p1close-c1`）。跨环境的「第五轮」数值逐字一致
    > （`1354513501.4100`，同问句 + 同快照），旁证两条路径落同一数据口径。
11. 重启后 `/health` 的 `boot_id` 变化，而 `session_id` 续接成功（证明「持久化会话」
    与「进程重启」两个信号被正确分离）。
    **落地状态（2026-09-14 工作项 6）**：前半句**已验**（宿主连起两次 uvicorn，
    `boot_id` 变、其余 7 键不变，不需 Doris）。后半句**未验且现在不可能验**：
    `session_id` 跨重启续接要等本 ADR 工作项 7~10（SQLite checkpointer）落地，
    故整条判据仍未通过——不要把「两信号已分离」写进任何已完成清单。

    > **收口落地（2026-09-16 P-1 收口；本判据整体转通过）**：宿主与容器路径各两连拍
    > `/health`——本地 `9fcc1c90f48840e1a5b7541e75aefe0e` →
    > `0837c579a9d647e7b529b1bd201388d0`、容器 `19948f7a29b44969b76d57979958cf54` →
    > `346cddcba60e467e8c6e848f2c29386e`：`boot_id` 每次启动必变，其余 7 键
    > （`status`/`head_sha`/`snapshot_sha`/`snapshot_source`/`snapshot_bound_to_head`/
    > `snapshot_created_at`/`snapshot_tables`）连同值逐键一致；**且同批重启后的
    > `session_id` 续接成功**（判据 10 的 turns 4→5）——两个信号在真链上正确分离，
    > 「不写进已完成清单」的保留解除。身份连续性说明：同一 token 贯穿全部轮次，
    > 未重签（`claims_fingerprint` 含 `iat`/`exp`，重签即新身份，口径见决策 ⑥ 与
    > `serving/auth.py` docstring——本批据此在重启验证中复用首轮 token）。
12. compose 内验证卷挂载：`docker compose exec atlas-api ls -l /app/serving/state/`
    能看到 DB 文件，且 `docker compose down && up` 后会话仍可续接（决策 ③）。

    > **已落地（2026-09-16 P-1 收口复验）**：`docker compose exec atlas-api
    > ls -l /app/serving/state/` 见 `atlas.db`（163840）+ `-shm`（32768）+
    > `-wal`（189552）在命名卷 `atlas_atlas-api-state` 内；`docker compose down
    > && up -d`（透传 `ATLAS_CHECKPOINT_DB=/app/serving/state/atlas.db`，未加 `-v`）
    > 后文件仍在、同 `session_id` 续接成功（`turns` 4 → 5，判据 10 收口注）。
    > down 的 `Network atlas_default Resource is still in use` 是外部容器占用网络
    > 的环境噪音（atlas 栈容器均已正常停止，exit=0）。
    >
    > **连带缺陷取证与修复（本判据落地过程发现，如实登记）**：`.dockerignore` 漏排
    > `serving/state/`——宿主本地服务产生的会话 DB 混入镜像构建上下文、被 COPY 进
    > 镜像层，容器首启时命名卷初始化再把它复制进卷（实测卷内 `p1close.db` 4096 +
    > `-shm` 32768 + `-wal` 832272，wal 含 Guard 出口 SQL 与结果行副本——与
    > `factory.py` docstring 的 N3 精神直接冲突）。取证：对旧镜像
    > `docker run --rm --entrypoint ls atlas-atlas-api:latest -la /app/serving/state/`
    > 可见三文件。修复：`.dockerignore` 增 `serving/state` 行并 rebuild，同一命令
    > 返回 `No such file or directory`（容器内路径本就由
    > `checkpoint_saver_from_env` 落盘时 `mkdir` 自建，无需构建期预置）；卷内事故
    > 残留三文件已 `rm` 清理（验收剩 `atlas.db` 三件套）。教训：**镜像构建上下文
    > 与运行时落盘位置必须互斥**——同一目录同时充当两者时，「镜像自带」与
    > 「运行时生成」在卷里无法区分，且前者会静默携带宿主数据跨环境传播。

**文档判据**：

13. 「workers=1 因为 MemorySaver + `_session_turns` 进程内」的 **7 个副本全部同步**
    （`Makefile:130-133`、`docker-compose.yml:203`、`infra/docker/api/Dockerfile:8-9`、
    `serving/api.py:295-296`、`README` KL #28①②、`agent/graph.py:45`、
    `tests/test_api.py:7`），改为收窄后的理由（决策 ⑧ 表格）；漏一个即新的 N2。
    **落地状态（2026-09-15 工作项 10）**：已达成，且由人工核对转为自动化断言。
    7 个法定副本逐处：5 处改写（`Makefile` serve 注释 / `docker-compose.yml` /
    `infra/docker/api/Dockerfile` / `serving/api.py` 模块 docstring / README
    KL #28 ①②），2 处已随工作项 9 同步（`agent/graph.py` 模块 docstring、
    `tests/test_api.py` 口径行）本批核对锁定。**按行为检索又发现 11 处同类失真
    一并同步**（跨 8 个文件：`README.md` 硬化段行内引用 + KL #25、`README.en.md`
    两段、ADR-0011 落地注记、ADR-0018 决策 ⑦、前端计划两处、`serving/audit.py`
    docstring、`agent/cli.py` 与 `tests/test_demo_e2e.py` 会话键注释）——其中
    ADR-0011/0018/前端计划 #32 三处曾把「身份指纹是进程内态」**转述**给决策 ⑧
    （0020 表格自始即列指纹入 checkpoint），属转述错误。断言落点
    `tests/test_session_persistence.py::TestWorkers1RationaleNarrowed`（3 例 /
    24 subtests，覆盖 14 个文件）：旧符号 `_session_turns` 绝迹 + 收窄后三关键词
    （限流/审计/单写者；英文副本 rate-limit/audit/single-writer）在场——先写断言
    实测 11 例红（stale 2 + narrowed 8 + _en 1），文本收窄后转绿。变异 4 次
    全红且 md5 全复原（M1 Makefile 符号回流 / M2 compose 删「单写者」/ M3 英文
    副本失修 / M4 扩集后 audit.py 删「单写者」）。
14. ADR-0012 末尾追加落地注记（不改正文），README KL #28① 的「重启即失」
    如实收窄为「重启不失（会话/轮数/身份指纹已持久化），但无横向扩展」。
    **落地状态（2026-09-15 工作项 10）**：ADR-0012 末尾「会话持久化批次」注记
    （不改正文）已含三段：workers=1 理由收窄、推翻条件第 1 条未触发、代价段
    收窄；本批修正其中两处立项行号（`/ask` docstring 的 `api.py:388`、
    healthcheck 的 `docker-compose.yml:230-231`——行号随批次增补漂移，改符号
    定位）并追加「判据 13/14 副本同步」段。README KL #28① 收窄落成**两形态
    并述**：「设 ATLAS_CHECKPOINT_DB 时会话/轮数/身份指纹随 SQLite checkpoint
    落盘、跨重启不失；未设时仍是进程内 MemorySaver（重启即失）。两种形态都无
    横向扩展」——比判据原文的单一句多出「未设形态」限定，如实登记：单写
    「重启不失」会掩盖默认形态仍失忆，反而失真。

## 落地注记（2026-09-16，ADR-0026 多步分析对本文档边界的影响）

> 本节为落地注记，不重写上文历史正文。记录 ADR-0026 固定四步贡献分析（`POST /api/v1/analyze`）落地后，本文档所定持久化语义的三处补充口径。

**父轮记账例外**：一个用户逻辑轮 = 一次 checkpoint 写入——分析轮在**父轮收口时**写 checkpoint，分析子步不单独落 checkpoint；实现采用 `update_state(as_node=...)` 收口模式。这延续了本文档「用户轮为记账单位」的设计，分析的四步子执行对持久化层透明。

**子步串行执行**：分析子步走 DataAgent 实例级可重入锁 + 屏障，并发 analyze 请求被串行化，父会话不变量（单写者、轮数记账、身份指纹）不因并发分析请求破坏——与本文档 workers=1 的单写者前提一致。

**中断语义**：analyze 轮以 finish 或父轮 error 收口后 `next=()`（图自然终止）；父轮失败但带 parent_hint 时，用户可在同一会话继续追问；blocked 子步保留为 blocked 步骤记录（`kind=blocked` + reason_code），而非静默丢弃。
