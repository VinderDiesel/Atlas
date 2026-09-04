.PHONY: help install up down seed seed-retail dwd lint lint-ossie lint-governance export plan compile ask eval e2e retrieve extract-meta train report test adr rls-verify rbac-verify rbac-verify-ensure metrics-verify p1-verify serve token api-verify

PYTHON       ?= .venv/bin/python

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
	@echo ""
	@echo "  评测"
	@echo "    make eval            跑黄金集（Plan Acc + EX，自动复核数据快照）"
	@echo "    make demo            端到端演示测试（双语 12 + RLS 身份 2，需 Doris + 锁定快照）"
	@echo "    make rls-verify      行级权限回归（双域：finance 差异集 3；retail 州/品类档）"
	@echo "    make e2e             Data Agent 端到端验收门禁：5 场景+handoff（Day 48）"
	@echo "    make train           用确认后的失败样本训练 SQL LoRA"
	@echo "    make report          生成 EVAL_REPORT.md"
	@echo ""
	@echo "  其他"
	@echo "    make lint            全部校验（ossie + governance + 语义层）"
	@echo "    make rls-verify      行级权限回归验证（三角色谓词下推，需 .env）"
	@echo "    make rbac-verify     Polaris 对象级 RBAC 回归验证（需 .env）"
	@echo "    make metrics-verify  新发布指标编译 + Doris 实测验证（Day 27）"
	@echo "    make p1-verify       P1 端到端验收：gold-102 全链路 + Guard×10（Day 28）"
	@echo "    make schema-link      schema linking 评测：指标 Recall@K + 表覆盖（Day 29）"
	@echo "    make test            单元 + 契约测试"
	@echo "    make serve           启动 HTTP API（uvicorn 127.0.0.1:8000，单进程）"
	@echo "    make token           签发本地测试 JWT（ROLE 变量，如 ROLE=branch_manager）"
	@echo "    make api-verify      HTTP API 真链验收（/plan→/compile→/ask + 认证）"
	@echo "    make adr             新建 ADR（用法: make adr TITLE=\"标题\"）"

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

plan:
	$(PYTHON) -m agent.cli plan "$(Q)"

compile:
	$(PYTHON) -m agent.cli compile query.plan.json

# Data Agent 多轮问数（agent/cli.py ask，Day 49）：真实 Doris + 锁定快照
# - 带问句参数单轮；省略进入交互会话（同一 session 连续多轮，空行退出）
# - 快照绑定当前 git HEAD meta；缺 meta 直接报错（AGENTS.md N6）
ask:
	uv run --env-file .env python -m agent.cli ask

# ---- HTTP API 服务面（ADR-0012，serving/api.py）----
# serve：uvicorn 单进程（默认 workers=1——checkpointer MemorySaver 与 _session_turns
#   是进程内状态，多 worker = 会话分裂，README KL #28）；工作目录必须为仓库根
#   （SemanticModel 加载语义层 YAML 依赖 cwd）；Ctrl-C 停止；P7 起 /plan /compile
#   /ask 请求体 model 字段选择语义域（finance|retail，缺省 finance，双 Agent 懒建）
# token：签发本地测试 JWT（serving/auth.sign_token，需 .env ATLAS_JWT_SECRET）
#   默认 ROLE=hq_admin；如 ROLE=branch_manager CONTEXT='{"branch": "east"}' 可覆盖
serve:
	uv run --env-file .env uvicorn serving.api:app --host 127.0.0.1 --port 8000

token:
	uv run --env-file .env python -c "import json, sys; from serving.auth import sign_token; print(sign_token(sys.argv[1], json.loads(sys.argv[2])))" "$(or $(ROLE),hq_admin)" "$(or $(CONTEXT),{})"

# HTTP API 真链验收（eval/api_acceptance.py，ADR-0012）：全 HTTP 栈 + 真 Doris
# + 锁定快照（b933e20 meta，29 表全量数据版本）：A1 全链 EX / A2 歧义反问 /
# A3 认证 / A4 存活 / A5 三角色差异 / A6 会话冲突 422 / A7 零售受限 + 跨域拒绝
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
# 语义层/策略/授权变更后运行，确认权限边界仍生效；脚本自动绑定当前 git sha，
# 产出 eval/reports/*.json 与 docs/screenshots/*.html
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

adr:
	$(PYTHON) -m infra.adr new "$(TITLE)"
