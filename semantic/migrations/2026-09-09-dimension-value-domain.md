# 维度值域注册（2026-09-09，B4）

决策依据：`infra/adr/0016-dimension-value-domain.md`
落地批次：P1 批次 B4（维度值域注册 + filter 值归属校验 + 快照锁定焊死）

---

## 1. 变更内容

| 项 | 变更 | 内容是否变化 |
|---|---|---|
| 值域画像生成 | 新增 `data/value_profile.py`（`make profile-values`）：从锁定快照 SELECT DISTINCT 生成 `semantic/values/<model>.<field>.json` | **新增**（机器生成值本体 + 人工追加别名） |
| 值域加载/解析 | 新增 `agent/value_domain.py`：`ValueProfile` / `Resolution` / `load_profile` / `resolve` / `parse_profile` | **新增**（只读加载器，不连库不猜值） |
| planner 值域校验 | `agent/planner.py` 新增 `_resolve_filter_value` + `plan_with_notices()` 入口 | **新增**（filter 值归属阶段加校验，归一记 ValueNotice） |
| SemanticModel.name | `agent/compiler.py` 增 `self.name = str(model["name"])` | **新增**（值域文件名绑定键） |
| lint 第 5 项 [values] | `semantic/lint.py` 新增 `check_value_profiles()`：snapshot_sha == 最新锁定 meta sha + model/field 存在性 + source_table 在快照白名单 | **新增**（漂移即红） |
| Makefile | 新增 `profile-values` 目标 | **新增** |
| ADR | `infra/adr/0016-dimension-value-domain.md` | **新增** |
| AGENTS.md | §4 目录职责补 `semantic/values/`；§7.3 YAML 校验条款补 values 目录；Known Limitations 新条 | **更新** |

## 2. 影响面（诚实声明）

- **filter 值校验**：维度值 filter 的值在归属阶段对 `semantic/values/` 的快照值域做校验——
  命中值本体 → 通过；命中人工别名或大小写折叠唯一命中 → **归一**（记录 ValueNotice）；
  既非值也非别名（含折叠多命中）→ ClarificationRequest 附候选值样例。未注册与被跳过的列
  （大基数）**不校验**，值原样透传（安全默认）。
- **受影响的角色与流程**：
  - 客户问数：`只看城市 MIDWAY 的 2000 年销售额` 经 case 归一为 `'Midway'`（记录 ValueNotice）；
    `只看类别 Toys 的 1999 年销售额`（Toys 不在值域）反问并列出 10 个候选值。
  - 别名归一：`只看交易所 NSDQ 的 2013 年佣金收入` 归一为 `NASDAQ`，Agent 解释链路可展示
    "你说 NSDQ，我按值域理解为 NASDAQ"。
  - 快照纪律：重新锁快照后**必须**重跑 `make profile-values`，否则 `make lint [values]` 红。
  - 评测：新增黄金集样本 gold-171（别名命中）+ gold-068（值不在值域 → 反问），实现前 dry
    基线已记录（finance 65/66、retail 1/2），目标态 finance 66/66 + retail 2/2。
- **不受影响**：行级策略、Guard 链条与安全默认值、历史报告、未注册列的 filter 透传行为。

## 3. 验证（数字全部来自脚本产物）

- `make lint`：5 项全绿（ossie / governance / gold 91 条 / authority / values）。
- `make test`：554 条全绿（skipped=15），新增 25 条（test_value_domain 20 + test_planner 5）。
- `make eval`：待 B4 产物入库后执行（目标态 finance 66/66 + retail 2/2）。

## 4. 已知边界（诚实登记）

- **值域是快照态而非实时**：数据重新装载后必须重跑 `make profile-values`，否则 lint 红。
  写入 README Known Limitations 新条。
- **同名维度字段首匹配**：编译器 `find_field` 按 datasets 顺序首匹配，`Status` 绑到
  `fact_trades`（唯一值 `Completed`）而非 `dim_account`（active/closed）。该事实如实记进
  profile 的 `bound_dataset` 与 `note`，本批不改编译器。
- **大基数列不校验**：`Branch`（2715 distinct）等被跳过，planner 对该列不做值校验——
  合法值与非法值都透传，静默漏匹配风险仍在。未来若需要，可考虑前缀匹配 / 模糊匹配扩展。
