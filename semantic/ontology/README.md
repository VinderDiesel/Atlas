# 概念索引产物（semantic/ontology · ADR-0032）

> 本目录存放**概念层运行时产物**：FIBO 闭包 + `fibo_alignment` 映射 + 人工别名
> 在**构建期**一次性编译为静态索引（AOT，ADR-0032 ④），入库提交；运行时纯 JSON
> 查表、不解析本体（rdflib 仅构建期，`--with` 注入）。
> **产物必须与构建器/源文件的每次变更同 commit 提交**——审计链：谁生成、何时生成、
> 源自哪一版（头部三元绑定）。

## 产物

| 文件 | 内容 | 生成命令 |
|---|---|---|
| `atlas_finance.concept_index.json` | 29 个 registry 概念 / 31 条映射全量落位 / 术语三源合并 | `make ontology-build` |

产物 schema（v1）：

```json
{"schema_version": 1, "generated_at": "<ISO8601+08:00>",
 "fibo_commit": "119fa8c091aa4beece7d22aefa6fe138021a4355",
 "commons_release": "20250801",
 "source_semantic_sha": "<atlas_finance.ossie.yaml sha256>",
 "concepts": {"<registry 短名>": {"iri": "...", "label": "...", "definition": "|null",
   "terms": [{"text": "...", "lang": "en|zh", "source": "S1|S2|S3", "via": "<对象键|null>"}],
   "parents": [{"concept": "<短名>", "transitive": false}],
   "children": [{"concept": "<短名>", "transitive": false}],
   "objects": [{"object": "...", "match_type": "...", "confidence": "...", "note": "..."}]}}}
```

- 术语三源：**S1** = FIBO 本体注解（`rdfs:label` / `cmns-av:synonym` /
  `cmns-av:abbreviation` / `skos:prefLabel`；定义自 `skos:definition`，不参与匹配）；
  **S2** = 对齐对象 `ai_context.synonyms`（`via` 记对象键 provenance）；
  **S3** = 人工别名 `semantic/synonyms/concepts_zh_cn.yml`（M1-T2 才建，当前为空）。
- 层级为 registry 集合内**归纳子图**：跨过未注册中间类的边打 `transitive: true`，
  未注册中间类不物化（ADR-0032 ②）。
- `atlas_retail` 无 `fibo_alignment` 映射 → **不生成产物文件**，检索缝天然走降级路径。
- registry 外 IRI → 构建器**响亮失败**（先跑 `check_iris.py` 入 registry 再重建）。

## 来源三元绑定（审计链）

| 项 | 当前值 | 说明 |
|---|---|---|
| `fibo_commit` | `119fa8c091aa4beece7d22aefa6fe138021a4355` | FIBO sparse checkout 锁定 commit（获取方式见 `data/fibo/README.md`） |
| `commons_release` | `20250801` | OMG Commons 版本段 |
| `source_semantic_sha` | 见产物头部 | `semantic/ossie/atlas_finance.ossie.yaml` 的 sha256——**改语义模型不重建 = lint 红**（ADR-0032 ⑤ 两级漂移防线第 1 条） |

## 复现

```bash
make ontology-build            # 重建产物（构建期 rdflib==7.6.0 注入，不污染主依赖）
make ontology-build CHECK=1    # 重建到内存并与入库产物比对（剔除 generated_at），
                               # 漂移时打印差异字段路径并以 exit 1 失败
```

前置：`data/fibo/fibo-src`（FIBO sparse checkout）与 `data/fibo/vendor`（OMG 依赖）
就位，获取方式见 `data/fibo/README.md`「复现」节。

## 漂移防线（两级）

1. **repo 文件派生内容 CI 严格可查**：`source_semantic_sha`、S1 来源登记、S2/S3 术语、
   `objects` 反向索引——源文件在库，`make ontology-build CHECK=1` 可在任何有 FIBO
   闭包的环境重建比对；
2. **闭包派生部分**（`label` / `definition` / 层级）：**诚实声明——`fibo-src` 不入库，
   CI 查不了这两类**，只能本地 `make ontology-build CHECK=1` 全量比对
   （ADR-0032「代价与限制」第 3 条）。

## FIBO 版本升级流程

1. bump 锁：`data/fibo/fibo-src` 更新到新 commit（sparse checkout 方式见
   `data/fibo/README.md`），同步更新其锁定版本表；
2. `uv run --with rdflib==7.6.0 python data/fibo/check_iris.py`
   —— 29 条权威 IRI 应全部仍存在；**若有缺失 → 停批排查，禁改 registry 掩盖**；
3. `make ontology-build` 重建产物（新 `fibo_commit` 写入头部）；
4. 对第 3 步产生的**漂移 diff 逐条评审**（label / definition / 层级变化是否符合
   升级预期），再 `make ontology-build CHECK=1` 确认重建确定性；
5. `make lint && make test` 全绿后，产物 + `data/fibo/README.md` 锁定表 +（如有）
   registry 更新**同一提交**，在提交说明记录新 commit。

## 与既有资产的关系

- 构建器：`data/fibo/build_concept_index.py`（置闭包工具旁，复用
  `check_iris.load_graph` 的 SEEDS 闭包加载；纯函数层可无 rdflib 单测）；
- 运行时消费：`agent/concept_index.py`（M1-T3 交付，尚未实施；镜像
  `agent/value_domain.py` 的位置逻辑）——**无索引文件 = 全缝旁路**（零行为差异）；
- 别名 curation loop：`make paraphrase`（dev 集）失败 → `eval/failures/` →
  人工提出别名 → 写 `concepts_*.yml` → `make ontology-build` → `make lint` → 复跑；
  **holdout 一律不碰、只在最终报告**（ADR-0032 ⑤）。
