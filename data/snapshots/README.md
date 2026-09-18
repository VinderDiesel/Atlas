# 数据快照

评测的**唯一可信基准**。所有 EX / result_hash 必须绑定某个具体 sha。

## 元数据格式

每个快照对应一个 `<sha>.meta.json`：

```json
{
  "sha": "<git rev-parse --short HEAD>",
  "created_at": "<ISO 8601, +08:00>",
  "source": "TPC-DI",  # 全库含 TPC-DS SF0.1 零售表时自动为 "TPC-DI + TPC-DS SF0.1"
  "data_range": "<tpcdi.trade.t_dts min~max, 实测>",
  "raw_size_bytes": "<data/raw/tpcdi 递归字节, 实测>",
  "row_counts": {
    "dwd.fact_trades": "<待填写>",
    "dwd.dim_account": "<待填写>"
  },
  "snapshot_ids": {
    "dwd.fact_trades": "<current snapshot id, 重跑即变>"
  },
  "generation_seconds": "<待填写>",
  "notes": "<生成环境、随机种子等>"
}
```

## 锁定脚本

`data/snapshot.py`（从仓库根 `uv run python -m data.snapshot`）：

- 枚举 Polaris 下全部表（pyiceberg scan count）并写入 `row_counts` 与 `snapshot_ids`
  （2026-09-04 起全库 29 表 = tpcdi 17 + dwd 12：金融 8 + TPC-DS 零售 4；`source`
  字段随 dwd 是否含零售表自动声明多源）
- git sha 只锁代码/清单版本；**snapshot id 锁数据文件版本**：任何重跑 loader 或
  DWD 加工都会改变它，必须重新锁快照
- 防呆：目标文件已存在时复核数据指纹，一致则 noop；不一致则拒绝覆盖
  （要求先 git commit 产生新 sha），防止旧评测引用被悄悄改写

## 规则

1. **大文件不入库**（已 gitignore），只提交 meta.json
2. 换快照 = 所有历史评测数字失效，必须重跑并更新 EVAL_REPORT
3. 被问“数字怎么来的”时，第一答案是“绑定到快照 sha xxx”

## 已锁快照与演进

**指纹组记法**（ADR-0019 决策 ⑥，2026-09-14 改）：同组 = `row_counts` 与
`snapshot_ids` **全一致**的同数据多锁（数据未动，HEAD 前进后仅重锁新 sha）。
盘上 21 份 meta 按此实测恰为两组：**组 1 双源 29 表 16 份** / **组 2 单源 25 表 5 份**。
分组与份数不是手工点数——本表的覆盖性由 `tests/test_identity.py::TestSnapshotLedgerCoverage`
断言（漏记任何一份盘上存在的 meta 即测试变红）。本节曾与文件系统漂移 8 份，正是
加这条断言的理由。

### 组 2 · 单源 25 表（TPC-DI，5 份，按 `created_at` 升序）

- `b47a6c1`（09-02 15:45，随 commit `7d48dcb` 入库）：本节所载最早一份，Batch1 全量
  + DWD 加工后状态；评测产物 `eval/reports/b47a6c1.json`（其 `sha` 字段实测 =
  `b47a6c1`，与本快照名一致）与 `eval/reports/retrieval-{bm25,fuse,milvus}-b47a6c1.json`
- `7d48dcb` / `b7e9ce7`：TPC-DI 单源 25 表（agent 批次与 filter 样本批次）
- `a207284` / `30b8344`：TPC-DI 单源同数据多锁（后续批次 HEAD 前进后重锁，
  指纹与 b7e9ce7 全一致，见对应 eval 批次 commit）

### 组 1 · 双源 29 表（TPC-DI + TPC-DS SF0.1，16 份，按 `created_at` 升序）

- `dc4f350`（09-04 12:54）：TPC-DI + TPC-DS SF0.1 双源 29 表（P2b 零售装载后首次全库锁定）
- `92033c9`（09-04 13:08）：双源 29 表同数据多锁（P5 零售锚定前重锁，指纹与 dc4f350
  全一致——中间 `9cf70c7`/`b2a2e5c` 两个 commit 的语义与样本批次未动数据）；零售 13 条
  gold 样本锚定绑定此 sha（eval/reports/92033c9.json）
- `1e5d35b`（09-04 13:25）：双源 29 表同数据多锁（P6 双语样本批次：planner locale 化 +
  英文样本 13 条，未动数据，指纹与 92033c9 全一致）；英文样本 13 条锚定绑定此 sha
  （eval/reports/1e5d35b.json，per-domain by_lang 分节）
- `73b5971`（09-04 13:39）/ `9749fc5`（09-04 13:58）：双源 29 表同数据多锁（P7 收口批次：
  API 多模型路由 + demo 集成测试，未动数据，指纹与 1e5d35b 全一致）；终验复验零回归绑定
  `9749fc5`（eval/reports/9749fc5.json，89 条全绿；EVAL_REPORT.md per-domain 分节绑定此 sha）
- `b933e20`（09-04 15:12，随 commit `4a547e7` 入库）：同数据多锁（API 身份下推 + 会话
  指纹 422 + 审计 JSONL + 限流批次）；产物 `eval/reports/b933e20.json` 与
  `eval/reports/baseline-compiler-b933e20.json`；也是 `make api-verify` 所用
  `eval/api_acceptance.py` 的 `SNAPSHOT_META`（该脚本写死 sha 属 N6 例外，见 0019 判据 5(b)）
- `7051ef6`（09-09 10:02，随 commit `6af4cde` 入库）：同数据多锁（B3a 中文形态外置
  复验）；产物 `eval/reports/7051ef6.json`
- `40b71e2`（09-09 10:41，随 commit `36c878c` 入库）：同数据多锁（B3b 英文形态外置
  复验）；产物 `eval/reports/40b71e2.json`、`eval/reports/e2e-acceptance-40b71e2.json`
- `160795d`（09-09 11:39，随 commit `5d1e22b` 入库）：同数据多锁（B4 值域画像入库）
- `5d1e22b`（09-09 11:40，随 commit `26e7694` 入库）：同数据多锁（B4 评测回流
  gold-171）；产物 `eval/reports/5d1e22b.json`、`eval/reports/baseline-compiler-5d1e22b.json`
- `26e7694`（09-09 11:51，随 commit `a11d779` 入库）：同数据多锁（B5 评测先行：15 条
  时间智能样本 + 黄金集 schema 扩展）
- `a11d779`（09-09 11:56，随 commit `dfedc16` 入库）：同数据多锁（`atlas query` 一步
  问数 + 时间智能）；**2026-09-14 时为 `created_at` 最大** → 当时 ADR-0019 决策 ①
  第 3 级「最新已锁」解析到的就是它（此后由 `ccb4c8b`、`7c966e9` 依次接替）。
  2026-09-14 实测：HEAD 前进到无同名 meta 的 commit 时，
  `/health` 与 `RuntimeSnapshot.describe()` 回显
  `sha=a11d779 source=latest bound_to_head=false`（此处刻意**不写**当时的 HEAD sha——
  它是会前进的位置，写死即把一条时效性断言伪装成历史事实）
- `ccb4c8b`（09-15 16:08，P-1 工作项 12 EX 锚定批次）：双源 29 表同数据多锁（HEAD
  前进后重锁，未动数据，指纹与 a11d779 全一致）；15 条时间智能样本（gold-172~179 +
  gold-072~078）绑定此 sha（13 条非歧义 EX 锚定 + 2 条歧义仅绑 sha）；产物
  `eval/reports/ccb4c8b.json`
- `7c966e9`（09-16 11:43，P0a 许可证统一批次）：双源 29 表同数据多锁（HEAD 前进后
  重锁，未动数据，指纹与 ccb4c8b 全一致）；数据库驱动迁移（mysql-connector-python →
  PyMySQL，ADR-0023 决策 ④）验证的 `make eval` 绑定此 sha（判据 5）；产物
  `eval/reports/7c966e9.json`
- `e0e0422`（09-17，④a 解锁证据批次·用户授权补锁+跑）：双源 29 表同数据多锁（HEAD
  前进到无同名 meta 的 commit 后重锁，未动数据，指纹与 7c966e9 全一致）；作
  `--dry` 全量 `eval.runner`（plan_acc 97/97，不锁不执行）与真链 `make analysis-eval`
  报告 `eval/reports/analysis-e7909f2.json`（绑定 7c966e9）的评测基线
- `1e2e557`（09-17，同批）：双源 29 表同数据多锁（HEAD 再前进后重锁，指纹与 e0e0422
  全一致）；真链全量 `make eval` 绑定此 sha：`eval/reports/1e2e557.json`（EX 97/97、
  plan_acc 97/97、exec_errors 0、guard_blocked 0、ex_anchored 0）——GATE-④ 解锁时间前置证据

**7 位 hex 在本文件里有两种身份**：快照名，与 commit 短 sha。上头的「随 commit
`4a547e7` 入库」这类出处、以及叙述里的 `9cf70c7` / `b2a2e5c` 属后者——盘上**没有**同名
`<sha>.meta.json`；而 `7d48dcb` / `5d1e22b` 两种身份重合（commit 与其锁定的快照同名），
按快照计。区分方法只有一个：看有没有同名 meta 文件。上述测试因此**按行为**判定反向
（无 meta 的 token 必须是本仓真实 commit），而不是把 commit sha 列成白名单——枚举集
每次加出处注释都要增长，增长还得人来批，等于把纪律交回自觉。真漏记则无论怎么写都跑不掉：
覆盖性断言查的是「盘上有 meta 而本表没提」。

换快照纪律：数据未变时 HEAD 前进仅重锁新 sha（同数据多锁先例），meta.json
逐个保留可追溯，README 记演进即可，不需重锚历史样本。

**新增 meta 必须同批更新本表**；**`created_at` 是运行时选最新的唯一键**——ADR-0019
决策 ① 的第 3 级回退按 `created_at` 取最大，不按文件名字典序（改造前四处
`sorted(dir.glob(...))[-1]` 按字典序取，与本纪律不等价，是缺陷而非风格差异）。
