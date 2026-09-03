# ADR-0013：spark/ 目录归位改名 metadata/ + 计算执行面规划

- 日期：2026-09-03
- 状态：accepted（落地：commit 026ef81，`git mv spark/ → metadata/`）
- 相关：ADR-0004（Apache 全栈与计算层选型）、README §4 目录树、
  `metadata/parser.py`、`tests/test_metadata_parser.py`

---

## 背景

`spark/` 目录名沿自规划期技术栈（ADR-0004 计算层 = Spark / Flink），当初预想
"Spark 侧代码"放这里（AGENTS.md §4 原写 `extractors/ # DDL/ETL 注释 → 语义对象
候选`）。但 v0.1 实际落地后：

1. 目录内唯一代码 `metadata_parser.py` 是**纯 Python + sqlglot** 的 SQL 注释/结构
   解析器，零 pyspark import，与 Spark 运行时无关——包名是计算框架误导；
   `extractors/` 子目录从未启用（本地空目录，git 无跟踪）
2. Spark 从未入场：数据装载是 Python 直写 Iceberg（`data/loader.py`），DWD 加工
   是 Doris 内 SQL（`sql/dwd/*.sql`），查询经 Doris 执行——"计算"由单机 Python
   与 Doris（MPP 引擎）承担
3. 仓库真实"计算执行点"盘点：`data/loader.py`（CSV → Iceberg ODS）、
   `sql/dwd`（Doris INSERT OVERWRITE 幂等加工）、`eval/runner.execute_sql`
   （只读查询）——三处目前均直接实现，无统一后端抽象

## 备选方案

| 方案 | 优势 | 约束/劣势 |
|---|---|---|
| A. `spark/` 内建 ExecutionBackend 协议层（spark_impl/flink_impl/ray_impl 子目录） | 保留计算命名空间，未来分布式直接填空 | 名字继续误导；`metadata_parser.py` 非计算任务，放进"计算层"依旧错位；协议无消费者 = 死代码 |
| B. 改名 + "活协议层"（`compute/` Backend 接口，loader/dwd 迁入作为首个实现） | 协议被真实代码消费，非纸面 | loader 迁移涉及 Makefile/tests/snapshot 引用链，成本中等；当前无第二后端需求（YAGNI），Doris 已承担 SQL 计算（README 铁律 2：查询引擎可替换执行面本就成立） |
| C. **改名 `metadata/`（本次），协议层只写规划不写代码** | 成本近零（引用面 1 test + 文档）；名字如实；执行面演化留给实测驱动 | 无（协议延迟到有真实需求时） |

## 决策

选 C：`git mv spark/ metadata/`，模块 `metadata_parser.py` → `parser.py`，
`extractors/` 规划并入单模块（候选抽取职责不变）。AGENTS.md §4 目录职责表同步
（契约变更，commit 标注 `[contract]`）。

**计算执行面规划（Phase 2 草案，本 ADR 记录，不提前写代码）**：
- 未来协议形态：`Backend` 三方法——`load_batch()`（源 → ODS Iceberg）、
  `run_sql(sql, read_only)`（加工/查询）、`snapshot_meta()`（快照枚举与指纹）；
  现有三处执行点各自收敛为一个实现：`LocalPythonBackend`（装载）、
  `DorisSqlBackend`（加工+查询），Spark/Flink/Ray 为后续实现
- 立项触发条件（不满足不立项）：① 数据量/耗时实测超出单机 Python 装载或
  Doris 承载能力（profile 数据支撑，非直觉）；② 出现第二个需要多后端的消费者
  （如检索侧向量化批处理）
- 在此之前：执行面维持现状，README 铁律 2（查询引擎可替换）已覆盖 SQL 层替换性

## 影响与验证

- 引用更新：Makefile extract-meta、README §3.3 Day 26 勾选与目录树、AGENTS.md §4、
  `tests/test_metadata_parser.py` import、模块 docstring、报告 `tool` 字段
- 快照 sha 不变（无数据变更）；`eval/reports/metadata-extract-7d48dcb.json` 留档
  不改（`tool` 字段是 7d48dcb 时刻的历史事实；`dialect: "spark"` 是 SQL 方言
  标记，合法保留）
- 验证：14 例 test_metadata_parser 全绿 + 全仓零 `spark/metadata_parser` 引用残留
