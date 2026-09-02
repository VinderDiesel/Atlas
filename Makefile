.PHONY: help install up down seed dwd lint lint-ossie lint-governance export plan compile eval train report test adr

PYTHON       ?= .venv/bin/python

help:
	@echo "Atlas - 可信 AI 问数平台（Apache Ossie + Iceberg + Polaris + Doris）"
	@echo ""
	@echo "  环境"
	@echo "    make install         安装依赖"
	@echo "    make up              启动 Apache 全栈基础设施"
	@echo "    make down            停止（不加 -v，保留数据卷）"
	@echo "    make seed            装载 TPC-DI Batch1 → Iceberg → 锁定快照"
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
	@echo ""
	@echo "  评测"
	@echo "    make eval            跑黄金集（Plan Acc + EX，自动复核数据快照）"
	@echo "    make train           用确认后的失败样本训练 SQL LoRA"
	@echo "    make report          生成 EVAL_REPORT.md"
	@echo ""
	@echo "  其他"
	@echo "    make lint            全部校验（ossie + governance + 语义层）"
	@echo "    make test            单元 + 契约测试"
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
	$(PYTHON) -m semantic.export_dbt --in semantic/ossie/atlas_retail.ossie.yaml \
	  --out exports/dbt_semantic_models.yml

lint: lint-ossie lint-governance
	$(PYTHON) -m semantic.lint --all

plan:
	$(PYTHON) -m agent.cli plan "$(Q)"

compile:
	$(PYTHON) -m agent.cli compile query.plan.json

# 黄金集评测（eval/runner.py）：Planner→Compiler→Guard→Doris 执行→sha256
# - 启动时自动复核 data/snapshots/<sha>.meta.json（漂移即拒绝出报告）
# - 首轮执行锚定 result_hash 回填 gold JSON（占位符→实测值），此后比对即 EX
# - 产出 eval/reports/<git sha>.json；dry 模式：uv run python -m eval.runner --dry
eval:
	$(PYTHON) -m eval.runner

# 指标检索评测（eval/retrieval_eval.py）：BM25（默认）/ Milvus 稀疏向量
# - 语料 = atlas_finance.ossie.yaml 的 15 指标文档；查询 = 44 条可解析 gold 问句
# - 产出 eval/reports/retrieval-<engine>-<sha>.json；ENGINE=milvus 需 atlas-milvus 在跑
retrieve:
	$(PYTHON) -m eval.retrieval_eval --engine $(or $(ENGINE),bm25)

train:
	$(PYTHON) -m lora.train --adapter sql_v1 --data eval/failures/approved_pairs.jsonl

report:
	$(PYTHON) -m eval.report > EVAL_REPORT.md
	@echo "已生成 EVAL_REPORT.md"

test:
	$(PYTHON) -m unittest discover -s tests -v

adr:
	$(PYTHON) -m infra.adr new "$(TITLE)"
