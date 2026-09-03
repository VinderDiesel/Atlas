# FIBO 本体子集（项目语义锚点 · 外部依赖锁定记录）

> **FIBO 是 Atlas 的主场景语义锚点**（ADR-0007，L2 概念对齐层）：
> “业务术语 → FIBO 概念 IRI → 语义字段 → SQL”的可解释链，以本目录的本体闭包为校验基准。
> 本目录存放 FIBO 本体子集与 OMG Commons/LCC 依赖，**仅本地使用，不入库**。
> 入库内容：本 README（锁定 sha）、`smoke_fibo.py`（复现脚本）及校验脚本。

## 锁定版本（2026-09-02 获取）

| 依赖 | 来源 | 锁定版本 | 获取方式 |
|---|---|---|---|
| FIBO 本体（FND + FBC + BE 域） | github.com/edmcouncil/fibo | commit `119fa8c091aa4beece7d22aefa6fe138021a4355`（master） | `git clone --depth 1 --filter=blob:none --sparse` + `git sparse-checkout set FND FBC BE` |
| OMG Commons（20 模块） | omg.org/spec/Commons | 版本 `20250801` | `https://www.omg.org/spec/Commons/20250801/<Module>.rdf` |
| OMG LCC（5 模块） | omg.org/spec/LCC | 无版本段 URL（跟随最新） | `https://www.omg.org/spec/LCC/<path>/<Module>.rdf` |

## 目录结构

```text
data/fibo/
├── README.md          # 本文件（入库）
├── smoke_fibo.py      # 冒烟验证脚本（入库，可复现，AllFND 闭包）
├── check_iris.py      # 候选概念 IRI 存在性验证（29 条权威清单）
├── probe_namespaces.py # 命名空间探查（找真实类名，不猜测）
├── validate_alignments.py # fibo_alignment 校验（ADR-0007 CI 校验点）
├── fibo-src/          # FIBO 仓库 sparse checkout（gitignore）
│   ├── FND/           # Foundations 域（1.4MB，入口 AllFND.rdf）
│   ├── FBC/           # Financial Business and Commerce 域（6.1MB，闭包所需）
│   ├── BE/            # Business Entities 域（1.1MB，闭包所需）
│   └── catalog-v001.xml  # IRI → 本地文件映射
└── vendor/            # OMG 外部依赖（gitignore）
    ├── Commons/       # 20 个模块（约 400KB）
    └── LCC/           # CountryRepresentation / ISO3166 / Subdivision / Languages
```

## 为什么需要 FND + FBC + BE

冒烟脚本（`smoke_fibo.py`）从 `AllFND.rdf` 入口做 owl:imports 闭包 BFS 加载，
实测闭包包含 FBC（ClientsAndAccounts 等）与 BE（FunctionalEntities 等）模块，
因此 sparse-checkout 需覆盖三个域。**若未来语义模型映射用到 SEC/MD/LOAN 域，
需 `git sparse-checkout add <域>` 后重跑冒烟脚本验证闭包完整性。**

**重要（2026-09-02 实测发现）**：`AllFND.rdf` 的 import 列表**不含**
`FND/TransactionsExt/` 与 FBC 域（它们只作为"被依赖方"被 import）。
交易/账户/证券概念（MarketTransaction、BrokerageAccount、Security 等）
必须显式加入扩展种子 `SEEDS`（见 `check_iris.py`）才能进入闭包。

## 关键事实（2026-09 实测）

1. **FIBO 2.0 基础概念已下沉至 OMG Commons**：`Party` 的正确 IRI 是
   `https://www.omg.org/spec/Commons/PartiesAndSituations/Party`（FIBO 自身
   Parties.rdf 引用 `cmns-pts:Party`）。设计映射时基础概念优先查 Commons。
2. **概念下沉不限于 Party**：`Person` 位于 FND/AgentsAndPeople/People（非 Commons
   PartiesAndSituations）；`FormalOrganization`/`LegalEntity`/`LegalPerson`/
   `OrganizationalSubUnit`/`Date` 均在 OMG Commons；类名前缀须以本体文件实际
   声明为准（如 `fibo-fnd-txn-mkt` 而非 `tx-mkt`），映射前必须探查验证。
3. **加载性能**：扩展种子闭包 1100+ 个 owl:Class、1.2s 完成（含 TransactionsExt 与
   FBC 域）；AllFND 基础闭包为 55 个文件、22,730 条三元组、0.3s（rdflib 7.6.0, Python 3.11）
4. **TBox 规模**：MVP 阶段完全可内存加载，无需推理引擎（符合 ADR-0007 L2 决策）

## 覆盖审计（2026-09-03 Day 54）

**方法**：以 validate_alignments.py 同口径（model 级 custom_extensions
fibo_alignment.mappings）对 8 datasets / 20 metrics 全量比对；新增概念先经
check_iris.py 实测存在后才入映射（诚实红线：不引用未经验证的 IRI）。

| 项 | 结果 |
|---|---|
| 映射总数 | 22 → **31**（8 datasets/11 字段/关系 + 20 metrics 中 19 条锚定） |
| 权威清单 | 26 → **29**（+UnitPrice / Holding / ScalarQuantity，本文件上方更新） |
| dataset 覆盖 | 8/8（本轮补 fact_holdings → Holding，FND/OwnershipAndControl） |
| metric 覆盖 | 19/20，唯一待办：**total_trade_tax** |

**total_trade_tax 待办理由**：锁定闭包（FND+FBC+BE 子集 + Commons 20250801）中
无贴切「交易税金额」类——候选仅 TaxIdentifier/TaxLot（税务治理概念，非金额）、
Fee（经纪服务费，政府税语义不贴切）。补映射需先扩展闭包域（如 FBC 税务模块）
并重跑冒烟验证，故登记待办、不硬补（2026-09-03 实测确认）。

## 语义模型映射校验（ADR-0007 CI 校验点）

```bash
# 概念 IRI 存在性回归保护（29 条权威清单，应输出 29/29）
.venv/bin/python check_iris.py
# 语义模型映射校验：governance schema + FIBO 闭包 IRI 存在性
.venv/bin/python validate_alignments.py semantic/ossie/atlas_finance.ossie.yaml
```

## 复现

```bash
# 1) 重建 FIBO sparse checkout
git clone --depth 1 --filter=blob:none --sparse https://github.com/edmcouncil/fibo.git fibo-src
cd fibo-src && git sparse-checkout set FND FBC BE && cd ..
# 2) 校验（应输出：✅ import 闭包完整，无缺失）
.venv/bin/python smoke_fibo.py
```

## 许可与署名

- FIBO：MIT License（Copyright 2020 EDM Council）；FIBO 为 EDM Council 商标
- OMG Commons / LCC：OMG 规范发布物，按 OMG 条款使用（研究用途）
- 本体 IRI 均保留原始命名空间，映射文件不改变本体内容
