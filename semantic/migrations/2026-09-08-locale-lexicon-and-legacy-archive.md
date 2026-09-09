# locale 词典外置与幽灵定义归档（2026-09-08，B2）

决策依据：`infra/adr/0015-scenario-decoupling-locale.md`
落地批次：P0 解耦 B2（英文同义词外置 + 幽灵语义定义归档 + lint 权威源唯一性）

---

## 1. 变更内容

| 项 | 变更 | 内容是否变化 |
|---|---|---|
| 英文同义词 | `agent/planner.py` 代码常量 → `semantic/synonyms/en_us.yml`（17 指标 / 15 维度字段） | **否**（逐词搬运，合并语义 `en = 模型注记 + 表`、`zh = 模型注记` 逐字保持） |
| 中文侧词典 | 新建 `semantic/synonyms/zh_cn.yml` 为**空表**（内容计划见 ADR-0015 §② 与 B3a） | 否（空占位，无措辞） |
| 加载入口 | `agent/compiler.py::load_locale_synonyms(locale)`（封闭 locale 注册表 + 严格形态校验 + 缓存） | — |
| 幽灵定义 | `models/orders.yml`、`metrics/gmv.yml`、`dimensions/*.yml`、`synonyms/business_terms.yml`、`schema/*.schema.json` → `semantic/_legacy/` | 否（文件内容未改，仅位置；零代码引用） |
| lint | `make lint` 新增第 4 项 `[authority]`：`semantic/` 下语义定义文件仅在 `ossie/`、`synonyms/`、`policies/`（`_*` 归档区豁免） | — |
| AGENTS.md | §4 目录职责表更新为现实结构；§7.3 YAML 校验条款改指向 `make lint`（原指向已废弃的 `semantic/schema/*.schema.json`） | — |

## 2. 影响面（诚实声明）

- **指标/维度口径定义未变**：ossie 模型一字未动，`ai_context.synonyms` 注记不变；
  本迁移改的是**解析器英文措辞表的物理位置**（代码 → 配置）。
- **受影响的角色与流程**：
  - 新场景接入：追加英文措辞从"改 planner 代码"变为"改 `en_us.yml`"（B7 前提）；
  - 语义层评审：词典与模型的一致性靠契约测试锁定（条数基线 / 键必须在模型内 /
    措辞不与模型注记重叠，唯一例外中英同形的 `AOV`），不再靠"常量在同一个文件里"；
  - CI：`[authority]` 新检查会在 `semantic/` 下新增非权威目录的 YAML 时直接报红。
- **不受影响**：行级策略、Guard 链条与安全默认值、黄金集标注、历史报告。

## 3. 验证（数字全部来自脚本产物）

| 命令 | 结果 |
|---|---|
| `make lint` | 4 项全绿（ossie 2 文件 / governance 2 文件 / gold 89 条 / authority 零违规） |
| `make test` | `Ran 481 tests OK (skipped=14)` |
| `python -m eval.runner --dry` | finance `plan_acc 65/65`、`clarify 5/5`（zh 57/57+5/5、en 8/8）；retail `18/18`、`1/1`（zh 13/13+1/1、en 5/5）；两域 `exec_errors 0` |
| dry summary vs `eval/reports/b933e20.json` | 非 EX 维度逐域逐语言**完全相等**（EX 列 dry 下不执行，故 `n/a(首轮锚定)`，不参与比对） |

## 4. 复现命令

```bash
make lint && make test
.venv/bin/python -m eval.runner --dry        # 双域 summary 对照 b933e20
.venv/bin/python -m pytest tests/test_locale_synonyms.py tests/test_semantic_lint.py -q
```
