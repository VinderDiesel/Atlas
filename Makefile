.PHONY: help install up down seed lint lint-ossie lint-governance export plan compile eval train report test adr

PYTHON       ?= .venv/bin/python
SNAPSHOT_SHA := $(shell git rev-parse --short HEAD 2>/dev/null || echo "unknown")
ICEBERG_SNAP ?= latest

help:
	@echo "Atlas - 可信 AI 问数平台（Apache Ossie + Iceberg + Polaris + Doris）"
	@echo ""
	@echo "  环境"
	@echo "    make install         安装依赖"
	@echo "    make up              启动 Apache 全栈基础设施"
	@echo "    make down            停止（不加 -v，保留数据卷）"
	@echo "    make seed            生成 TPC-DS SF1 → 写入 Iceberg → 锁定快照"
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
	@echo "    make eval            跑黄金集（绑定 Iceberg 快照）"
	@echo "    make train           用确认后的失败样本训练 SQL LoRA"
	@echo "    make report          生成 EVAL_REPORT.md"
	@echo ""
	@echo "  其他"
	@echo "    make lint            全部校验（ossie + governance + 语义层）"
	@echo "    make test            单元 + 契约测试"
	@echo "    make adr             新建 ADR（用法: make adr TITLE=\"标题\"）"

install:
	uv sync --all-extras || pip install -e ".[dev]"
	pre-commit install

up:
	docker compose up -d
	@echo ""
	@echo "等待服务健康（Polaris 与 Doris 启动较慢）..."
	@docker compose ps

down:
	docker compose down

# TPC-DS → Iceberg → 锁定快照
# Iceberg 的 snapshot id 直接服务于评测可复现（比导出 CSV 算 hash 更严谨）
seed:
	$(PYTHON) -m data.tpcds_gen --scale 1
	$(PYTHON) -m data.iceberg_load --catalog $(ICEBERG_CATALOG_NAME)
	$(PYTHON) -m data.snapshot --tag $(SNAPSHOT_SHA)

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

eval:
	$(PYTHON) -m eval.runner --set gold --snapshot $(SNAPSHOT_SHA) --iceberg-snap $(ICEBERG_SNAP)

train:
	$(PYTHON) -m lora.train --adapter sql_v1 --data eval/failures/approved_pairs.jsonl

report:
	$(PYTHON) -m eval.report > EVAL_REPORT.md
	@echo "已生成 EVAL_REPORT.md"

test:
	$(PYTHON) -m unittest discover -s tests -v

adr:
	$(PYTHON) -m infra.adr new "$(TITLE)"
