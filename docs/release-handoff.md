# 发布操作单（Release Handoff）——v0.1 对外发布交接

> 目的：把 8 周本地成果安全发布到 GitHub。本单是**给发布执行人（你）的检查单**，
> 所有命令可在本仓库直接执行；涉及 push/公开的步骤需本人确认后操作。
> 状态日期：2026-09-03。发布前质量门已在当日全部实跑通过：
> `make lint` ✅ / 244 测试 ✅ / `make eval`（快照指纹无漂移，plan_acc 44/44）✅ /
> `make report` ✅ / ruff 0 / mypy 0（详见 `docs/逐日任务清单.md` Day 54 登记）。

## 0. 当前仓库状态

- 历史仅 2 个 commit（`b47a6c1` 基建 / `7d48dcb` agent CLI），当前工作区 **130 个文件
  未提交**（37 modified + 93 untracked）——8 周实质工作都在工作区
- remote：`origin = git@github.com:xinlongyang/Atlas.git`（SSH，已配置）
- 快照/评测绑定 sha = `7d48dcb`（**注意**：eval/reports 报告文件名与 git sha 绑定；
  新 commit 后旧报告保持原名不动，EVAL_REPORT.md 只聚合当前 sha——若想在新 sha 下
  重新出报告，需重跑 `make eval`（新 sha 报告文件）+ `make report`）

## 1. 建议 commit 序列（按 AGENTS.md §8，语义层独立评审）

> 一次一个大主题，便于回滚与评审。`semantic` 类变更不与其他混（§8 规则）。

| 序 | 命令（type(scope)） | 内容 |
|---|---|---|
| 1 | `semantic(finance): 指标 6→20 + 治理扩展 + FIBO 31 条映射审计补齐` | `semantic/`（ossie yaml、schema、governance、migrations）+ `data/fibo/`（check_iris 29、registry、README 审计节） |
| 2 | `feat(agent): LangGraph 8 节点状态机 + 工具四件套 + 提示词资产` | `agent/` 全部 + `agent/prompts/` |
| 3 | `feat(retrieval): schema linking 三阶段 + 图约束 + rerank` | `retrieval/` |
| 4 | `sec(guard): 只读 Guard 三层 + RBAC/行级验证脚本` | `serving/`（guard、rls_verify、rbac、metrics_verify）+ 相关测试 |
| 5 | `eval: gold 50 例 + runner/八类报告 + 失败样本登记` | `eval/`（gold、脚本、reports 现存报告文件**一并入库**——它们是数字溯源证据） |
| 6 | `feat(observability): OTel 回合级埋点 + Grafana 6 面板/4 告警` | `observability/` + `docker-compose.yml`（obs profile） |
| 7 | `feat(lora): SFT 飞轮状态机 + 训练栈前置检查（blocked 如实登记）` | `lora/`（无实测数字，仅机制） |
| 8 | `docs: README 定稿 + KL 27 条 + release-notes-v0.1 + retro-final` | `README.md`、`docs/*.md`、`EVAL_REPORT.md` |
| 9 | `chore(ci): eval.yml 回归评测 workflow + 其余工具链` | `.github/`、`Makefile`、`.env.example`、`spark/`、`exports/` |

commit 序列执行时可精简合并（如 2+3 合并 `feat(agent)` 的检索配套），
但 **1 必须独立**、**4 独立**、**8 独立**（docs 不夹代码）。

## 2. push 前复跑（30 秒级）

```bash
make lint && make test          # 语义层五类校验 + 244 单测
# （eval/report 已在当日实跑；若 commit 改了语义层或代码需重跑 make eval）
```

## 3. push 后 CI 验证（.github/workflows/）

- `lint.yml`：push/PR 触发（ruff + 契约测试 + `make lint`）
- `eval.yml`：plan-regression job = lint + 契约测试 + **Plan Acc dry 回归**
  （公共 runner 无数据库也可跑）；eval-data 手动 job 需 compose+seed+快照
- **验证点**：Action 页面看 plan-regression 绿；eval-data 按需手动触发
- 诚实边界提醒：EX 完整评测不可 CI 化（公共 runner 无 TPC-DI 数据/Doris），
  dry 回归是自洽门槛——真实 EX 以本地 `make eval` 报告为准（README §3.3 Day 40 行登记）

## 4. 仓库 public（交付门槛最后一项）

1. push 全部 commit 后，GitHub 仓库 Settings → General → Danger Zone → Change visibility → Public
2. 对照门槛勾选 `docs/逐日任务清单.md` 末尾「GitHub 仓库为 public」一项
3. 建议顺带：仓库 About 区加描述（可用 README 首段）、Topics 加
   `nl2sql` / `semantic-layer` / `fibo` / `apache-doris` / `opentelemetry`

## 5. 对外发布顺序建议（可选，不占门槛）

1. 先发仓库（public）+ CI 绿
2. 再发长文/推文（草稿在 `docs/outreach-v0.1-draft.md`，口径已核：只陈述实测能力与
   KL 边界，数字与 README/报告同源；对外文案禁止出现 blocked 项数字与未验证声明）
3. 如需演示环境：`make up && make seed && make lint && make eval && make report` 一键复现
   （seed 会重置数据，勿在保留评测数据时执行）

## 6. 发布后（v0.2 候选路线，见 release-notes §4）

失败样本积累 → 飞轮实转（GPU 解锁 `make train`）→ LoRA 两行补齐 compare →
filter/相对时间 → Phase 2 边界。任何新数字进 README 前必须过脚本（AGENTS.md N1）。
