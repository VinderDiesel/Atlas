.PHONY: help install up down seed seed-retail dwd lint lint-ossie lint-governance license-check export plan compile ask query eval baseline compare paraphrase e2e demo retrieve rag-eval schema-link extract-meta train train-distill train-dryrun lora-infer report report-latest test adr adr-list rls-verify rbac-verify rbac-verify-ensure metrics-verify p1-verify serve token api-verify profile-values ui-check ui-dev ui-build serve-dev

PYTHON       ?= .venv/bin/python

# 容器身份（ADR-0019 决策 ④）：镜像无 .git，构建参数 GIT_SHA 必须由宿主注入，且
# **不给硬编码默认值**（会随 HEAD 前进变成谎言——改造前 Dockerfile 与 compose 各写
# 一个不同的 sha，静默把容器绑到无零售表的快照）。注入点收在这里：`export` 之后
# compose 的 ${GIT_SHA:-} 才取得到值，Dockerfile 的构建守卫负责「没注入就失败」。
# 需复现旧镜像身份时命令行覆盖：make up GIT_SHA=<sha>（compose 变量优先级高于 .env）。
# 2>/dev/null：无 .git 环境下不让 fatal 信息污染每一次 make 调用；此时求值为空，
# 失败点仍在构建守卫（响亮且只响一次），而不是每次 make 都刷屏。
GIT_SHA      := $(shell git rev-parse --short HEAD 2>/dev/null)
export GIT_SHA

# 前端工具链（ADR-0018 决策 ③⑥）：node 装在 nvm，而 make recipe 经非交互 /bin/sh
# 执行、看不到 nvm 初始化（`sh -c 'make ui-check'` 实测 command not found: node）。
# 探测顺序：PATH 优先（系统/CI 安装），否则取 nvm 版本目录（多版本并存时取字典序
# 末位——本机实测单版本，多版本场景再修）。探测为空时不注入，由目标内 node_guard
# 响亮报错（不静默跳过）。固定版本或强制失败态：命令行覆盖即可——
#   make ui-check NODE_BIN=$HOME/.nvm/versions/node/v24.16.0/bin
NODE_BIN     ?= $(shell command -v node 2>/dev/null || ls -d $$HOME/.nvm/versions/node/*/bin 2>/dev/null | tail -1)
ifneq ($(strip $(NODE_BIN)),)
export PATH := $(NODE_BIN):$(PATH)
endif

help:
	@echo "Atlas - 可信 AI 问数平台（Apache Ossie + Iceberg + Polaris + Doris）"
	@echo ""
	@echo "  环境"
	@echo "    make install         安装依赖"
	@echo "    make up              启动 Apache 全栈基础设施"
	@echo "    make down            停止（不加 -v，保留数据卷）"
	@echo "    make seed            装载 TPC-DI Batch1 → Iceberg → 锁定快照"
	@echo "    make seed-retail     装载 TPC-DS SF0.1（dsdgen bootstrap → 4 表入 dwd，零售域）"
	@echo "    make dwd             在 Doris 内执行 sql/dwd/*.sql（幂等，依赖 atlas catalog）"
	@echo ""
	@echo "  语义层（Apache Ossie）"
	@echo "    make lint-ossie      用 Ossie 官方 schema 校验语义模型"
	@echo "    make lint-governance 校验 Atlas 治理扩展（owner/lineage/policy）"
	@echo "    make export          导出为 dbt MetricFlow YAML（证明非封闭）"
	@echo ""
	@echo "  查询链路"
	@echo "    make plan            问句 → 指标计划（用法: make plan Q=\"上个月销售额\")"
	@echo "    make compile         计划 → 只读 SQL（Doris 方言）"
	@echo "    make ask             多轮问数会话（真实 Doris，无参数进交互；Day 49）"
	@echo "    make query           一步问数（agent/cli.py query）：Planner→Compiler→Guard→Doris"
	@echo ""
	@echo "  评测"
	@echo "    make eval            跑黄金集（Plan Acc + EX，自动复核数据快照）"
	@echo "    make paraphrase      同义改写鲁棒性评测（Planner-only，不需 Doris）"
	@echo "    make demo            端到端演示测试（双语 12 + RLS 身份 2，需 Doris + 锁定快照）"
	@echo "    make rls-verify      行级权限回归（双域：finance 差异集 3；retail 州/品类档）"
	@echo "    make e2e             Data Agent 端到端验收门禁：5 场景+handoff（Day 48）"
	@echo "    make train           用确认后的失败样本训练 SQL LoRA"
	@echo "    make train-distill   蒸馏冷启动语料（确定性编译器作 teacher，不碰 gold）"
	@echo "    make train-dryrun    语料飞轮自检（CPU，不训练）"
	@echo "    make report          生成 EVAL_REPORT.md"
	@echo "    make report-latest   跨最新多 sha 聚合生成 EVAL_REPORT.md（High4）"
	@echo ""
	@echo "  其他"
	@echo "    make lint            全部校验（ossie + governance + 语义层）"
	@echo "    make license-check   许可证守卫（受限路径/GPL 扫描/TPC carve-out；REPORT=1 产出依赖清单）"
	@echo "    make rls-verify      行级权限回归验证（三角色谓词下推，需 .env）"
	@echo "    make rbac-verify     Polaris 对象级 RBAC 回归验证（需 .env）"
	@echo "    make metrics-verify  新发布指标编译 + Doris 实测验证（Day 27）"
	@echo "    make p1-verify       P1 端到端验收：gold-102 全链路 + Guard×10（Day 28）"
	@echo "    make schema-link      schema linking 评测：指标 Recall@K + 表覆盖（Day 29）"
	@echo "    make test            单元 + 契约测试"
	@echo "    make serve           启动 HTTP API（uvicorn 127.0.0.1:8000，单进程）"
	@echo "    make serve-dev       单进程同源启动 API + dist 静态面（先 make ui-build）"
	@echo "    make ui-check        前端门槛：tsc + vitest + build（并入 pre-commit）"
	@echo "    make ui-build        构建 frontend/dist（serve-dev / 容器镜像前置）"
	@echo "    make ui-dev          vite dev server（HMR；API 由 make serve 另起）"
	@echo "    make token           签发本地测试 JWT（ROLE 变量，如 ROLE=branch_manager）"
	@echo "    make api-verify      HTTP API 真链验收（A1-A9：/api/v1 全链 + 治理面 + plan/execute）"
	@echo "    make adr             新建 ADR（用法: make adr TITLE=\"标题\" SLUG=\"english-slug\"）"
	@echo "    make adr-list        列出现有 ADR 编号与标题（含下一个可用编号）"

install:
	uv sync --extra dev || pip install -e ".[dev]"
	pre-commit install 2>/dev/null || true

up:
	docker compose up -d
	@echo ""
	@echo "等待服务健康（Polaris 与 Doris 启动较慢）..."
	@docker compose ps

down:
	docker compose down

# TPC-DI Batch1 → Iceberg → 锁定快照
# 数据链路（实测闭环 2026-09-02）：
#   1) data/loader.py 解析 Batch1 源文件写入 tpcdi 17 张 Iceberg 表（Polaris catalog atlas）
#   2) data/snapshot.py 枚举全部表并固化 <sha>.meta.json（git sha + 每表 snapshot id）
# DWD 加工不在 seed 内：sql/dwd/*.sql 需在 Doris 内执行（make dwd），
# 依赖 Doris 侧 Polaris catalog 'atlas' 已配置（见 infra/docker/polaris/ensure_catalog.py 背景说明）
seed:
	$(PYTHON) data/loader.py
	$(PYTHON) -m data.snapshot

# TPC-DS SF0.1 → Iceberg dwd 4 张零售表（P2 批次；口径声明见 scripts/setup_tpcds.sh 头注释）
# 链路：setup_tpcds.sh（clone 固定版本 → 编译 dsdgen/distcomp → SF0.1 补丁 →
#       dists.dmp → 生成 .dat）→ data/tpcds_loader.py（解析 .dat 装载
#       atlas.dwd.{store_sales,date_dim,dim_item,dim_store} + 数据探查打印）
# 表名注记：零售日期维度物理表为 date_dim——dwd.dim_date 已被金融 TPC-DI 占用（P2 实测撞名），
# 语义模型内 dataset 名（date_dim/dim_date）只是 SQL 别名，source 指向物理表不受限。
# 幂等：setup 产物齐全即跳过；loader 逐表 drop-if-exists -> create -> append。
# 注意：不锁快照（锁快照是评测前手工步骤，需 HEAD 已含装载代码）
seed-retail: up
	scripts/setup_tpcds.sh
	$(PYTHON) -m data.tpcds_loader

# DWD 加工：8 张 INSERT OVERWRITE 幂等 SQL（实测行数见 data/snapshots/<sha>.meta.json）
dwd:
	docker exec atlas-doris-fe mysql -h127.0.0.1 -P9030 -uroot -e "CREATE DATABASE IF NOT EXISTS atlas.dwd"
	@for f in sql/dwd/*.sql; do \
		echo "== $$f"; \
		docker exec -i atlas-doris-fe mysql -h127.0.0.1 -P9030 -uroot < "$$f"; \
	done

# ---- 语义层校验 ----
lint-ossie:
	$(PYTHON) -m semantic.ossie_validate semantic/ossie/*.ossie.yaml

lint-governance:
	$(PYTHON) -m semantic.governance_validate semantic/ossie/*.ossie.yaml

export:
	$(PYTHON) -m semantic.export_dbt --in semantic/ossie/atlas_finance.ossie.yaml \
	  --out exports/dbt_semantic_models.yml --report exports/metric-export-report.json

lint: lint-ossie lint-governance
	$(PYTHON) -m semantic.lint --all

# ---- 许可证与第三方内容守卫（ADR-0023 决策 ⑦；不并入 lint——lint 语义是语义层校验）----
# 断言：① data/raw 与 data/fibo/{fibo-src,vendor} 索引零文件（git ls-files --cached）
# ② LICENSE 首非空行 + NOTICE 存在 ③ pyproject 与 LICENSE 三方一致（含全文 sha256 钉）
# ④ 已安装分发的 GPL/LGPL/AGPL 嫌疑集 ⊆ 白名单（显式常量，现为空集）
# ⑤ TPC patch（若存在）文件头声明块；REPORT=1 追加 ⑥ exports/dependency-licenses.json
# 注（2026-09-16 实测）：GNU make 拒绝未知长选项——`make license-check --report` 报
#   "unrecognized option"，故报告模式按本仓变量惯例走 REPORT=1；脚本侧
#   `python -m infra.license_check --report` 原样可用（ADR-0023 判据 8 的形态）。
license-check:
	$(PYTHON) -m infra.license_check $(if $(REPORT),--report)

plan:
	$(PYTHON) -m agent.cli plan "$(Q)"

compile:
	$(PYTHON) -m agent.cli compile query.plan.json

# 一步问数（agent/cli.py query，本次新增）：Planner→Compiler→Guard→Doris 真连库
# - 默认 finance 域；--domain retail 切零售；--format json 机读（含 SQL/rows/退出状态）
# - 未知指标默认澄清（退出码 2）；--llm 走候选链（需 OPENAI_API_KEY）
# - 行级策略：--role branch_manager --role-ctx branch=BR_A1（与 serving/rls_verify 同机制）
# - 退出码：0 ok / 1 error / 2 clarify / 3 blocked（脚本可据此分流）
query:
	uv run --env-file .env python -m agent.cli query "$(Q)"

# Data Agent 多轮问数（agent/cli.py ask，Day 49）：真实 Doris + 锁定快照
# - 带问句参数单轮；省略进入交互会话（同一 session 连续多轮，空行退出）
# - 快照绑定走三级解析（ADR-0019 决策 ①）：ATLAS_SNAPSHOT_SHA > HEAD > 最新已锁；
#   全无 meta 或显式指定的 sha 未锁 → 报错退出（不回退）；绑定的快照缺本域所需表
#   → 构造期即拒（决策 ③），不再等 Guard 逐次拒绝
# - 注：本入口尚未回显绑定行（`[snapshot]` 目前只打在 atlas query 上），登记为
#   0019 实施裁定 7 的回显面缺口，随工作项 6 一并补
# - 会话持久化（ADR-0020 决策 ③）：`.env` 里设了 ATLAS_CHECKPOINT_DB 才落盘，
#   于是跨进程追问可续；未设即本入口历史上的老形态（进程内 MemorySaver，退出即失）
ask:
	uv run --env-file .env python -m agent.cli ask

# ---- HTTP API 服务面（ADR-0012 + ADR-0022 契约 v2，serving/api.py）----
# serve：uvicorn 单进程（workers=1——**理由已收窄**，ADR-0020 决策 ⑧：会话/轮数/
#   身份指纹已入 SQLite checkpoint 不再分裂；仍为进程内态的是限流两桶 + 审计 JSONL
#   追加写 + SQLite 单写者，多 worker 会让限流配额 ×N、审计行交错）；工作目录必须
#   为仓库根（SemanticModel 加载语义层 YAML 依赖 cwd）；Ctrl-C 停止；业务与治理面
#   全部在 /api/v1 前缀下（仅 /health 根路径保留为探针契约、双挂同 body）；
#   P7 起 /api/v1/plan /compile /ask 请求体 model 字段选择语义域
#   （finance|retail，缺省 finance，双 Agent 懒建）
# token：签发本地测试 JWT（serving/auth.sign_token，需 .env ATLAS_JWT_SECRET）
#   默认 ROLE=hq_admin；如 ROLE=branch_manager CONTEXT='{"branch": "east"}' 可覆盖
serve:
	uv run --env-file .env uvicorn serving.api:app --host 127.0.0.1 --port 8000

token:
	uv run --env-file .env python -c "import json, sys; from serving.auth import sign_token; print(sign_token(sys.argv[1], json.loads(sys.argv[2])))" "$(or $(ROLE),hq_admin)" "$(or $(CONTEXT),{})"

# HTTP API 真链验收（eval/api_acceptance.py，ADR-0012 + ADR-0022 契约 v2）：
# 全 HTTP 栈（/api/v1 前缀）+ 真 Doris
# + 固定快照：绑哪个 meta 由该脚本的 SNAPSHOT_META 决定（写死 sha 属 N6 例外，
#   见 ADR-0019 判据 5(b)）；本文件不复述该 sha——注释里的 sha 与脚本里的常量
#   会各说各话，同类缺陷已在 ADR-0019 决策 ④ 从 Dockerfile/compose 清掉
# 断言项：A1 全链 EX / A2 歧义反问 / A3 认证 / A4 存活（根+前缀双挂同 body）
#   / A5 三角色差异 / A6 会话冲突 422 / A7 零售受限 + 跨域拒绝 / A8 治理面 8 集合
#   一轮（全 200 且不撞业务桶）/ A9 /plan/execute 真链 EX
# 断言失败退出码 1；产出 eval/reports/api-acceptance-<sha>.json；需 .env（ATLAS_JWT_SECRET）
api-verify:
	uv run --env-file .env python -m eval.api_acceptance

# Data Agent 端到端验收门禁（eval/e2e_acceptance.py，Day 48）
# 5 场景 + handoff：真实 Doris 逐场景断言，任一失败退出码 1
# 产出 eval/reports/e2e-acceptance.json；文档 docs/e2e-acceptance.md
e2e:
	uv run --env-file .env python -m eval.e2e_acceptance --report eval/reports/e2e-acceptance.json

# 黄金集评测（eval/runner.py）：Planner→Compiler→Guard→Doris 执行→sha256
# - 启动时自动复核 data/snapshots/<sha>.meta.json（漂移即拒绝出报告）
# - 首轮执行锚定 result_hash 回填 gold JSON（占位符→实测值），此后比对即 EX
# - 产出 eval/reports/<git sha>.json；dry 模式：uv run python -m eval.runner --dry
eval:
	$(PYTHON) -m eval.runner

# 维度值域画像（ADR-0016，B4）：从锁定快照 SELECT DISTINCT 生成 semantic/values/*.json
# 前置 `data/snapshot.py --check`（数据指纹与已锁快照不一致 → 拒绝生成）
# 纪律：重新锁快照后必须重跑（否则 make lint [values] 红）
profile-values:
	$(PYTHON) -m data.value_profile

# 同义改写鲁棒性评测（High1）：Planner-only，不需 Doris；测量注册口径内改写掉落率
paraphrase:
	$(PYTHON) -m eval.paraphrase_eval

# 评测报告聚合（High4）：--latest 跨最新多 sha 聚合，避免 headline 报告为空
report-latest:
	$(PYTHON) -m eval.report --latest > EVAL_REPORT.md
	@echo "已生成 EVAL_REPORT.md（--latest 跨 sha 聚合）"

# compiler-only 基线分析（eval/baseline_compiler.py，Day 30）：读主评测报告
# 派生确定性链覆盖分析（多少问题不需要 LLM），产出
# eval/reports/baseline-compiler-<sha>.json；需先跑 make eval
baseline:
	$(PYTHON) -m eval.baseline_compiler

# 指标检索评测（eval/retrieval_eval.py）：BM25（默认）/ Milvus 稀疏向量
# - 语料 = atlas_finance.ossie.yaml 的指标文档（Day 27 发布后 20 条）；查询 = 44 条可解析 gold 问句
# - 产出 eval/reports/retrieval-<engine>-<sha>.json；ENGINE=milvus 需 atlas-milvus 在跑
retrieve:
	$(PYTHON) -m eval.retrieval_eval --engine $(or $(ENGINE),bm25)

# RAG+LLM 路由评测（eval/rag_eval.py，Day 31）：问句 → Generator(LLM) → Plan 候选
# （确定性校验兜底）→ Compiler → Guard → Doris 执行；与 compiler-only 同口径对照
# - 产出 eval/reports/rag-llm-<engine>-<sha>.json
# - engine=stub（默认测试链路）/ openai（需 .env OPENAI_API_KEY）；ENGINE 变量覆盖
rag-eval:
	$(PYTHON) -m eval.rag_eval --engine $(or $(ENGINE),stub) $(if $(MODEL),--model $(MODEL)) $(if $(LIMIT),--limit $(LIMIT))

# 四策略对比（eval/compare_4way.py，Day 34）：compiler-only / RAG+LLM / LoRA / LoRA+SC
# - 读主评测与 rag-llm 报告汇总六维对比表；--measure-compiler 现场补测 compiler
#   latency（44 条重跑计时）；产出 eval/reports/compare-4way-<sha>.json
# - LoRA 行在 adapter 训练完成前为 blocked（不编造数字）
compare:
	$(PYTHON) -m eval.compare_4way --rag-engine $(or $(RAG_ENGINE),stub) $(if $(MEASURE),--measure-compiler)

# 元数据抽取（metadata/parser.py，Day 26）：SQL/DDL 注释 → 语义对象候选
# - 语料 = sql/dwd 8 张 + loader --emit-ddl 的 17 张 tpcdi ODS DDL（共 25 个脚本）
# - 产出 eval/reports/metadata-extract-<sha>.json；候选≠发布（审核是 Day 27 流程）
extract-meta:
	uv run python metadata/parser.py

# LoRA 训练（Day 37-38，ADR-0008）：先 build_pairs 构造合规语料（空语料/泄漏会被拦），
# 再 lora.train（前置检查不过 exit 2；GPU 机上先 uv sync --extra ml）
train:
	uv run python -m lora.build_pairs --approved lora/data/approved_pairs.jsonl
	$(PYTHON) -m lora.train --adapter sql_v1 --data lora/data/pairs.jsonl

# 蒸馏冷启动语料（High2）：用确定性编译器作 teacher 生成 SFT 语料，不依赖 approved 样本
# 也不碰 gold 标签——解决「空语料」阻塞，作为 LoRA 预训练起点
train-distill:
	uv run python -m lora.build_pairs --distill

# 语料飞轮自检（CPU 可跑，无 GPU）：只校验数据/Plan 合法性/min_samples，不训练、不烧钱
train-dryrun:
	uv run python -m lora.train --adapter sql_v1 --data lora/data/pairs.jsonl --dry-run

# LoRA 影子推理（无 GPU 演练问句→Plan→SQL 全链路；配 ATLAS_LORA_ENDPOINT 走真·LoRA）
lora-infer:
	uv run python -m lora.infer "2013 年第二季度总交易额是多少？"

report:
	$(PYTHON) -m eval.report > EVAL_REPORT.md
	@echo "已生成 EVAL_REPORT.md"

test:
	$(PYTHON) -m unittest discover -s tests -v

# ---- 端到端演示（P7：demo = 集成测试形态，非花哨脚本）----
# tests/test_demo_e2e.py：中英问句 12 条全链路（plan→compile→guard→execute，双域各一
#   DataAgent）+ 带身份 2 条（region_manager/category_analyst，rls-verify 同机制）
# 环境：Doris 可达 + 当前 HEAD 已锁快照（缺任一 skip）；带身份 2 例另需 .env 的
#   ATLAS_JWT_SECRET（缺省时这两例单独 skip，不阻塞无密钥环境）
demo:
	uv run --env-file .env python -m unittest tests.test_demo_e2e -v
	@echo "demo 通过（双语 12 + RLS 身份 2，真 Doris 集成）"

# ---- 权限回归验证（.env 需有 ATLAS_JWT_SECRET / POLARIS_RBAC_CLIENT_*）----
# rls-verify：双域行级下推（SQL 谓词层，serving/rls_verify.py，默认 --domain all
#   分节报告不混报）：金融 gold-146 载体三角色（branch_manager/compliance_auditor）
#   + 零售 TPC-DS 载体三角色（region_manager/category_analyst，rp_dept_visible 物理列）
# rbac-verify：Polaris 层对象级 RBAC 验证（--ensure 幂等建 principal/roles/grants）
# 语义层/策略/授权变更后运行，确认权限边界仍生效；产出 eval/reports/*.json 与
#   docs/screenshots/*.html
# 绑定口径（ADR-0019 实施裁定 7）：rls-verify 与 metrics-verify / p1-verify 一样，
#   报告**文件名**用代码 HEAD，而数字绑在解析出的快照上，故报告 JSON 另含
#   snapshot_sha / snapshot_source / snapshot_bound_to_head 三键（防「以 <HEAD>.json
#   命名却被读成 HEAD 的评测结果」）；rbac-verify 只验 Polaris 对象级授权、
#   不读表也不绑快照，无这三键
# 注意：Makefile 其他 target 用 $(PYTHON)（由 shell 环境提供变量）；
# 这两个脚本依赖 .env 文件，故固定走 uv run --env-file .env

rls-verify:
	uv run --env-file .env python serving/rls_verify.py

rbac-verify:
	uv run --env-file .env python serving/rbac_verify.py

rbac-verify-ensure:
	uv run --env-file .env python serving/rbac_verify.py --ensure

# ---- 指标发布验证（Day 27）----
# metrics-verify：新发布指标全链路编译 + Guard + Doris 实测（serving/metrics_verify.py）
# 语义层发布指标后运行，确认可编译可执行且值非空；产出 eval/reports/metrics-verify-<sha>.json
metrics-verify:
	uv run --env-file .env python serving/metrics_verify.py

# ---- P1 端到端验收（Day 28）----
# p1-verify：gold-102 问句全链路（路由→@v1→编译→策略→执行→trace）+ Guard 恶意 SQL×10
# 产出 eval/reports/p1-chain-<sha>.json 与 docs/screenshots/p1-chain.html
p1-verify:
	uv run --env-file .env python serving/p1_acceptance.py

# ---- schema linking 评测（Day 29）----
# schema-link：SchemaLinker 两阶段（图域粗筛→受限召回→元数据重排）在 44 条 gold
# 上的指标 Recall@K + 表覆盖，含同日 BM25 单路基线对照（agent/tools/schema_linker.py）
# ENGINE=bm25 默认；ENGINE=fuse 需 atlas-milvus 在跑。产出 eval/reports/schema-link-<engine>-<sha>.json
schema-link:
	uv run python -m eval.schema_link_eval --engine $(or $(ENGINE),bm25)

# SLUG 为文件名英文段（既有惯例 0016-dimension-value-domain.md）；纯中文标题无法
# 自动推断 slug（不做翻译——译名会与人工命名漂移），故缺 slug 时由生成器响亮报错
adr:
	$(PYTHON) -m infra.adr new "$(TITLE)" $(if $(SLUG),--slug "$(SLUG)")

adr-list:
	$(PYTHON) -m infra.adr list

# ---- 前端工程门槛（P0b，ADR-0018 决策 ③⑥）----
# node_guard：node 不可见时打印指引并 exit 1（**不静默跳过**——静默跳过会让
# 「门槛通过」变成假信号，ADR-0018 判据 5 盯的就是这个失败形态）。覆盖两类场景：
# ① 探测全落空（PATH 与 nvm 都没有 node）；② 命令行显式置空（NODE_BIN= 的负向自测）。
# guard 放在目标内而非解析期，保证失败以「目标 exit 1」形态出现。
define node_guard
	command -v node >/dev/null 2>&1 || { \
		echo "错误：未找到 node。make 以非交互 shell 执行，nvm 安装的 node 默认不在 PATH。" >&2; \
		echo "  要求 node >= 20；nvm 用户先 nvm install 20，或显式指定：" >&2; \
		echo "    make <目标> NODE_BIN=$$HOME/.nvm/versions/node/<版本>/bin" >&2; \
		exit 1; \
	}
endef

# ui-check：本地唯一真实前端门槛（ADR-0018 决策 ⑥）：tsc --noEmit → vitest → 构建。
# 无 node_modules 时先 npm ci（lockfile 语义，与 ui.yml / Dockerfile 一致；已装则
# 复用，避免 dev 迭代每次重装）。.pre-commit-config.yaml 对 frontend/ 变更调用本目标。
ui-check:
	@$(node_guard)
	@cd frontend && { [ -d node_modules ] || npm ci; }
	@cd frontend && npm run typecheck
	@cd frontend && npm run test
	@cd frontend && npm run build

# ui-dev：vite dev server（HMR + dev proxy → 127.0.0.1:8000；禁 localhost 的实测
# 理由见 vite.config.ts 文件头注释）。API 由 `make serve` 另行启动。
ui-dev:
	@$(node_guard)
	@cd frontend && { [ -d node_modules ] || npm ci; }
	@cd frontend && npm run dev

# ui-build：只构建 frontend/dist（供 serve-dev 与容器镜像 COPY --from 使用）。
# 与 ui-check 分离：构建产物与全量门槛检查的代价不应互相绑定。
ui-build:
	@$(node_guard)
	@cd frontend && { [ -d node_modules ] || npm ci; }
	@cd frontend && npm run build

# serve-dev：prod 同源形态（ADR-0018 决策 ②，判据 8）——单进程 uvicorn 同时供
# /api/v1 与 frontend/dist 静态面。dist 缺失即 exit 1：本目标意图就是「跑 UI」，
# 无 dist 时应引导 make ui-build，而不是起一个浏览器白屏的进程。
# 与 make serve 的关系：serve 是纯 API 入口（对 dist 无前置要求），两者端口绑定同。
serve-dev:
	@[ -f frontend/dist/index.html ] || { \
		echo "错误：frontend/dist/index.html 不存在——先执行 make ui-build" >&2; \
		exit 1; \
	}
	uv run --env-file .env uvicorn serving.api:app --host 127.0.0.1 --port 8000
