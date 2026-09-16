# ADR-0023：项目许可证统一为 Apache-2.0——三方矛盾消除、copyleft 依赖清理与第三方内容登记

- 日期：2026-09-14
- 状态：accepted（决策经用户确认；落地批次 **P0a**，须在 P-2api（ADR-0022）之后、
  P0b（ADR-0018 工程边界）之前）
- 相关：
  - `LICENSE`（当前 MIT 全文，实测 1087 字节，`:3` 为 `Copyright (c) 2026 HyperionS`）
  - `pyproject.toml:13`（`license = { text = "Apache-2.0" }`）、`:9`（version 0.1.4）、
    `:74-76`（`requires = ["setuptools>=64"]`）、`:29`（mysql-connector-python）、
    `:30`（psycopg[binary]）
  - `atlas.egg-info/PKG-INFO:5/:8/:42`（打包产物同时携带 Apache-2.0 元数据与 MIT 文件）
  - `README.md` §12「许可与声明」（撰写本 ADR 时为 `:754-757`，**2026-09-14 复测已漂到 `:858-861`**，仅 2 行；以 `grep -n "^## 12" README.md` 定位）、§7.1 数据表 FIBO/OMG 行（原记 `:441`，复测 `:446`）
    —— **行号漂移说明**：README 在各批次持续增行，本 ADR 内所有 `README.md:<行号>` 均已于 2026-09-14 按复测值更正；但 P0a 本身会再改 §12，故落地时**一律以章节锚点 + 定位命令为准**，不得直接套用本 ADR 的行号
  - `README.en.md`（**零** license 章节；撰写本 ADR 时 `:215` 的「11 ADRs」亦已过期，已于本 ADR
    撰写批次修正；2026-09-14 复测该行已漂到 `:227`，见决策 ⑥）
  - ADR-0018（决策 ① 前端 6 包许可证表有 5 个 ⬜ 待 P0b 回填，需项目基准；P0b 判据 1）
  - ADR-0012（备选方案表已预设「MIT 许可兼容 **Apache-2.0 项目**」——基准先于本 ADR 被引用）
  - AGENTS.md N7（不引入雇主真实数据/代码）、§5（技术栈锁定 Apache 全栈）、§9（数字纪律）
  - 代码事实源：`eval/runner.py:40`（全仓唯一 `import mysql.connector`）、
    `:172-195`（全仓唯一 `connect()` 调用）、`:153-169`（`result_hash` / `_scalar`）
  - 第三方内容：`scripts/tpcds_kit_sf01.patch`（102 行，入库）、`scripts/setup_tpcds.sh:37-53/:88`、
    `data/fibo/smoke_fibo.py:44-52`、`docs/outreach-video/remotion/package.json`

---

## 背景

### 同一项目当前对外有**三个互不相同**的许可证声明

实测（2026-09-14，`head`/`grep`/`importlib.metadata` 三路交叉）：

| 事实源 | 实测内容 | 对消费者的效力 |
|---|---|---|
| `LICENSE`（仓库根） | **MIT License** 全文，`Copyright (c) 2026 HyperionS` | 惯例上是**权威**声明；clone 后第一眼看到的是它 |
| `pyproject.toml:13` | `license = { text = "Apache-2.0" }` | 进 PyPI/wheel 元数据 |
| 已安装分发元数据 | `License: Apache-2.0`、`License-Expression: None`、**无任何 `License ::` classifier** | `importlib.metadata.distribution("atlas")` 实测 |
| `README.md:860`（原记 `:756`） | 「代码：`Apache-2.0`」 | 人类读者的声明 |
| `README.en.md` | **零命中**（`grep -i license` = 0） | 英文读者拿不到任何许可信息 |
| `NOTICE` / `COPYING` | **不存在**（`ls` 实测） | Apache-2.0 §4(d) 的分发义务无从履行 |

三处说 Apache-2.0、一处说 MIT、一处不说。**这不是"文档滞后"，是同一件作品同时挂了两份
互不相同的授权文本**——MIT 与 Apache-2.0 的义务集不同（后者含专利授权条款 §3、
NOTICE 传递义务 §4(d)、修改声明义务 §4(b)），使用者无法判断该遵守哪一套。按
AGENTS.md N2 的口径，`README.md:860` 与 `pyproject.toml:13` 中至少有一处是
"把未成立的写成已成立"。

### 打包产物把这个矛盾**固化进分发件**

`atlas.egg-info/PKG-INFO` 实测：

```
:5   License: Apache-2.0
:8   License-File: LICENSE
:42  Dynamic: license-file
```

`License-File: LICENSE` 是 setuptools 的自动收录：任何 sdist/wheel 都会把**MIT 全文**
打进包内，同时元数据声明 Apache-2.0。`infra/docker/api/Dockerfile:28-31`
（`COPY pyproject.toml uv.lock` → `uv sync --frozen --no-dev` → `COPY . .`）使镜像
同样携带这份矛盾。也就是说：矛盾不只存在于文档，已经存在于**可分发的产物**里。

### 依赖树里有 GPL 与 LGPL（108 包实测）

`.venv` 内实测 108 个已安装分发（`importlib.metadata.distributions()` 去重；
`ml` extras 的 torch/transformers/peft/bitsandbytes/vllm 均**未安装**，故这 108 包
= runtime + dev + 传递依赖）。许可证字段原始分布（`License-Expression` 优先，
其次 `License`，其次 `License ::` classifier）：

```
 47  MIT                15  BSD-3-Clause           14  Apache-2.0
  4  MIT License         3  Apache 2.0              2  PSF-2.0
  2  Modified BSD License  2  BSD License            2  LGPL-3.0-only
  2  BSD-2-Clause        1  MPL-2.0                 1  MIT-0
  1  Apache-2.0 OR BSD-3-Clause     1  Apache License, Version 2.0
  1  GNU GPLv2 (with FOSS License Exception)
  1  MPL-2.0 AND (Apache-2.0 OR MIT)   1  Apache-2.0 OR MIT
  1  Apache-2.0 OR BSD-2-Clause        1  Mozilla Public License 2.0 (MPL 2.0)
  1  Apache-2.0 AND BSD-2-Clause       1  3-Clause BSD License
  1  Apache Software License           1  Dual License      1  MIT OR Apache-2.0
```

copyleft 全集 **6 包**（反向依赖由 `Requires-Dist` 反查实测）：

| 包 | 版本 | 许可证 | 引入路径 | 全仓代码引用 |
|---|---|---|---|---|
| `mysql-connector-python` | 26.7.0 | **GNU GPLv2 (with FOSS License Exception)** | `pyproject.toml:29` **直接依赖** | **2 处**：`eval/runner.py:40` import + `:178` connect |
| `psycopg` | 3.3.5 | **LGPL-3.0-only** | `pyproject.toml:30` **直接依赖** | **0 处** |
| `psycopg-binary` | 3.3.5 | **LGPL-3.0-only** | `psycopg[binary]` extra | 0 处（随上一行） |
| `certifi` | 2026.7.22 | MPL-2.0 | `httpcore` / `httpx` / `requests` 传递 | 0 处（CA 束，运行期由 httpx 用） |
| `pathspec` | 1.1.1 | MPL-2.0 | `mypy` 传递（**dev-only**） | 0 处 |
| `orjson` | 3.12.0 | MPL-2.0 AND (Apache-2.0 OR MIT) | `langgraph-sdk` / `langsmith` / `pymilvus` / `mypy`(extra) | 0 处 |

`python-dateutil 2.9.0.post0` 的 `License: Dual License` 经 classifier 实测为
`BSD License` + `Apache Software License` 双许可（非 copyleft），不在此列。

**GPLv2 组件的实际暴露面**（这是本 ADR 最需要如实辨析的一段）：

1. 它是**唯一的 Doris 驱动**。`import mysql.connector` 在全部入库 Python 里只出现
   1 次（`eval/runner.py:40`），`connect()` 只调用 1 次（`:178`）——单一收敛点；
   但 `execute_sql` 被 **9 个文件**直接 import（`agent/cli.py`、`agent/factory.py`、
   `data/value_profile.py`、`eval/api_acceptance.py`、`eval/e2e_acceptance.py`、
   `serving/metrics_verify.py`、`serving/p1_acceptance.py`、`serving/rls_verify.py`、
   `tests/test_demo_e2e.py`），`eval.runner` 被 **19 个文件** import，对应
   **11 个 make 目标**（`query` / `ask` / `serve` / `api-verify` / `e2e` / `eval` /
   `profile-values` / `demo` / `rls-verify` / `metrics-verify` / `p1-verify`）。
   所以「只有 2 处用法」说的是**代码收敛度**，不是**影响面**——影响面是全执行链。
2. `dist-info/LICENSE.txt` 首段实测确认 GPLv2 + additional permissions，且
   `:33` 声明「this Connector is also subject to the **Universal FOSS Exception,
   Version 1.0**」，其全文在同文件 `:418-473`（该 `LICENSE.txt` 共 8125 行，
   `:477` 起是第三方组件许可汇编）。该例外条款 iii 的文本要点：
   若双方之间**只复制 Interfaces 而不复制其他代码**，则「use and/or distribution
   of the Software with Other FOSS shall not be deemed to require that the Other
   FOSS be licensed under the license of the Software」，并明确举例包含
   「statically or dynamically linking … and distributing the resulting
   combination under different licenses for the respective portions thereof」；
   "Other FOSS" 定义为「以 OSI 批准和/或 FSF 认定为自由的许可证、附带完整对应源码
   分发」的软件——Apache-2.0 属 OSI 批准。
3. 据此**文本级读解**（不是法律意见，本 ADR 不做法律判定）：Atlas 只 `import` 并
   调用 Connector 的接口、未把 Connector 代码复制进仓库，落在条款 iii 描述的情形内，
   GPL 传染**可能不成立**。但该读解留下三项持续义务与两项不确定性：
   - 义务：组合件必须"各部分按各自许可证"分发 → 需要一份能承载归属的 NOTICE
     （当前**不存在**）；不得对 Connector 部分主张 Apache-2.0；镜像/lockfile 每次
     变更都需重新确认形态未变（例：若将来把 Connector 代码 vendored 进仓库，
     条款 iii 前提立刻消失）。
   - 不确定性 A：例外条款是 Oracle 单方文本（Version 1.0），上游可改版；
   - 不确定性 B：条款末段明确「Nothing in this additional permission grants any
     right to distribute any portion of the Software on terms other than those of
     the Software License」——`Dockerfile:29` 的镜像**确实分发** Connector 本体，
     该部分仍是 GPLv2，镜像因此是混合许可产物。
4. 与之对照，替换成本实测极低：`uv pip install --dry-run pymysql` →
   `Resolved 1 package in 213ms` / `+ pymysql==1.2.0`（**零传递依赖**），
   代码侧改动 = 1 处 import + 1 处参数名（`connection_timeout=15` →
   `connect_timeout=15`，其余 DB-API 调用同构：`cursor()` / `execute()` /
   `description` / `fetchall()` / `close()`）。

### LGPL-3.0 依赖是**死代码**

`psycopg[binary]>=3.2`（`pyproject.toml:30`，注释写「PostgreSQL（Polaris / Atlas
元数据）」）在全部入库文件中**零代码引用**：`grep -rn psycopg` 排除 `.venv`/`uv.lock`
后只命中 `pyproject.toml:30`、`atlas.egg-info/PKG-INFO:17`、`atlas.egg-info/requires.txt:9`
（三者都是它自己的声明）。但 `psycopg-binary` 会随 `uv sync --no-dev` 进镜像，
实测其 `.dylibs/` 携带 `libpq.5.dylib` / `libssl.3.dylib` / `libgssapi_krb5` 等
**LGPL 二进制**——即"没有任何一行代码用它，却分发着它的 copyleft 二进制"。
Polaris 的连接面在 MVP 阶段从未落地（ADR-0004 记录 Polaris 为孵化器项目、
降级路径是 Iceberg hadoop catalog）。

### 入库的第三方内容分四类，其中一类是专有 EULA、一类含**逐字 TPC 源码**

`git ls-files` 实测：入库 489 文件；`data/raw/` **0 文件入库**（`.gitignore:21`）、
`data/fibo/fibo-src/` 与 `data/fibo/vendor/` **0 文件入库**（`.gitignore:27/:28`，
`git check-ignore -v` 三个路径逐个命中）、`docs/outreach-video/remotion/node_modules`
由**局部** `docs/outreach-video/.gitignore:1` 忽略（根 `.gitignore` **无** `node_modules`
条目，`grep` 实测 0 命中）。入库侧无任何 per-file 许可头（`git grep` 全仓
`SPDX-License-Identifier|Copyright (c)|Licensed under the Apache` 只命中
`LICENSE:3` 与 `data/fibo/README.md:99` 两处）。

| 第三方内容 | 许可证（实测文件） | 是否入库 | 当前合规依据 | 残留问题 |
|---|---|---|---|---|
| **FIBO 本体**（FND+FBC+BE） | `data/fibo/fibo-src/LICENSE` = `The MIT License (MIT)` / `Copyright (c) 2020 Enterprise Data Management Council` | ❌ 仅 6 个自写脚本 + `iri_registry.json` 入库 | `.gitignore:27`；`data/fibo/README.md:91` 给出 clone 指令；`:99` 已声明 MIT 与「FIBO 为 EDM Council 商标」 | 与 `README.md:446`（原记 `:441`）一致 ✅；商标声明只在小 README 里，主 README §12 未提 |
| **OMG Commons / LCC** | `data/fibo/vendor/{Commons,LCC}` 下**无任何许可文件**（`find -iname "*licen*"` = 0），全是 `.rdf` | ❌ 0 文件入库 | `.gitignore:28`；入库侧只有 **IRI 字符串**（`iri_registry.json` 中 `omg.org`/`spec.edmcouncil.org` 命中 29 处；`smoke_fibo.py:44-52` 把 IRI 映射到 `vendor/` 本地路径） | `README.md:446/:861`（原记 `:441/:757`）写「OMG 规范（**研究用途**）」——仓内**无许可文本可佐证**该定性，属无证据声明 |
| **TPC-DS kit**（`gregrahn/tpcds-kit` @ `5a3a817`，v2.10.0） | `data/raw/tpcds-kit/EULA.txt` = TPC END USER LICENSE AGREEMENT **VERSION 2.2**（专有，非 OSI） | ❌ kit 本体 0 文件入库；⚠️ **`scripts/tpcds_kit_sf01.patch` 入库** | `setup_tpcds.sh:53` 由使用者自行 `git clone`、`:88` 再 apply patch | 见下方专段 |
| **PDGF**（Parallel Data Generation Framework，TPC-DI 数据生成） | `data/raw/tools/Tools/PDGF/LICENSE.txt` = **BANKMARK UG (HAFTUNGSBESCHRAENKT) END-USER LICENSE AGREEMENT**（专有 EULA）；同目录另有 `THIRD-PARTY-LICENSE.txt`（列 log4j-1.2.15 / javassist / commons-lang3 / commons-net-3.3 等 Apache-2.0 组件）与 `pdgf.jar` | ❌ 0 文件入库 | `.gitignore:21` 是**唯一防线**（无 lint、无 CI 断言） | 一旦有人 `git add -f data/raw/`，专有 jar 与 EULA 直接进公开仓 |
| **TPC-DI 源数据** | `data/raw/tpcdi/`（Batch1/2/3 + audit csv）下**无任何许可文件**（`find` = 0） | ❌ 0 文件入库 | `.gitignore:21` | `README.md:445`（原记 `:440`）写「公开基准，**注册下载**」——许可条款同样在仓内无文本佐证 |
| **Remotion** | `node_modules/remotion/LICENSE.md` 实读 = **两层 source-available**（非 OSI）：Free License 适用于「an individual / a for-profit organization with up to 3 employees / a non-profit / 评估期」，否则需 Company License；并禁止「copy or modify Remotion code for the purpose of selling, renting, licensing, relicensing, or sublicensing your own derivate of Remotion」 | ⚠️ **`package.json` + `package-lock.json` + `src/*.tsx` + `out/E0.mp4` 等 17 文件入库**；`node_modules` 由局部 gitignore 排除 | 实装版本实测 `4.0.523`（声明 `^4.0.0`）；当前身份 = 个人 → 落在 Free License 资格内；`out/E0.mp4` 是**渲染产物**（Free License 明确允许「creating videos and images」，含商业用途） | 资格是**身份相关**的：转为 >3 人营利组织即需购买 Company License；`package-lock.json` 入库意味着他人 `npm ci` 会拉取 source-available 组件，但项目许可证文本对此**只字未提** |
| **自产媒体** | `docs/contact-wechat.png`、`docs/outreach-wechat-assets/*.png`（20+）、`covers/cover-E0.png`、`remotion/out/E0.mp4` | ✅ 入库 | 自产，无第三方素材 | 无 |

**TPC-DS patch 专段（本 ADR 发现的最具体的一项暴露）**：

`scripts/tpcds_kit_sf01.patch` 入库 102 行，实测构成：

```
新增行 41 / 删除行 16 / 上下文行 32   →  其中 48 行（16 删除 + 32 上下文）是逐字 TPC 源码
涉及文件：tools/r_params.c（:1）、tools/scaling.c（:27）
版权/许可声明：grep -c -i "copyright|TPC|license" = 0   →  零声明保留
```

对照 EULA v2.2 实测条款：

- 4.b「You may modify the Software」→ 改是允许的 ✅
- 6「You may not remove the copyright notice … and **You must apply the notice if
  You extract part of the Materials not bearing a notice**」→ patch 抽取了 48 行
  不带声明的 TPC 源码却未附任何声明 ⚠️
- 8「Any portion of the Materials **merged into or integrated with** other software
  … will continue to be subject to the terms and conditions of this Agreement」
- 9 分发修改版需同时满足：(a) 许可证至少提供 EULA 全部保护与条件、(b) 在标签与
  许可证顶部以**全大写、≥12pt、不弱于其他印刷**附上
  「THE TPC SOFTWARE IS AVAILABLE WITHOUT CHARGE FROM TPC.」、(c) 保留全部
  版权/专利/商标/归属声明、(d) 不得收费
- 4.c 公开性能结果需满足三条件之一：(1) 作为 TPC Benchmark Result、
  (2)「as part of an academic or research effort that **does not imply or state a
  marketing position**」、(3) 明确标识不可与 TPC Benchmark Result 比较

**本 ADR 不判定** 48 行 unified diff 上下文是否构成条款 9 意义上的"分发修改版软件"
（这是法律判断，超出 ADR 权限）；本 ADR 只登记两点事实：① 该文件当前随仓库以
Apache-2.0（`pyproject.toml:13`）对外授权，而 Apache-2.0 无法满足 9(a)~(c) 中任何
一项；② 存在一条**不依赖该法律判断**的零风险路径（见决策 ⑤），故不必等判断结论。

另注 4.c：`README.md` 内 TPC-DS/TPC-DI 相关的行数、耗时类数字（如
`:180` 的 FE 内存 5.5G→1.0G）都是 **Atlas 自身链路的实测**，不是 dsdgen/dsqgen 的
benchmark 性能结果，故不落在 4.c 的"performance results produced while using the
Software"内；但若将来在 README 写 dsdgen 生成速率/耗时，即需按 4.c(2) 或 (3) 加
显式标识——这条约束进决策 ⑤ 的登记表。

### 前端批次需要项目许可证基准

ADR-0018 决策 ① 列出 6 个前端包的许可证表，其中只有 `react`（MIT，读
`node_modules/react/LICENSE` 首行实测）是 ✅，其余 5 个（typescript / vite / antd /
recharts / vitest）是 ⬜「官方声明，待安装后复核」，其 P0b 判据 1 明写「未回填不得
声称许可证评估完成」。在项目自身许可证三方矛盾的情况下回填第三方包许可证**没有意义**：
兼容性判断需要一个确定的基准（Apache-2.0 与 MIT 对专利授权、NOTICE 传递的要求不同）。
故 P0a 必须先于 P0b（ADR-0018 落地注记第 3 条已同步此排序）。

---

## 备选方案

| 方案 | 优势 | 劣势 |
|---|---|---|
| **统一为 Apache-2.0**（选定） | `pyproject.toml:13` 与 `README.md:860` 已如此声明，改动集中在 `LICENSE` 一个文件 + 新增 NOTICE；与 AGENTS.md §5「Apache 全栈」定位一致；含专利授权条款 §3（对"金融可信"定位是实质增益）；ADR-0012 备选方案表已预设此基准 | 需新增 NOTICE 并履行 §4(b) 修改声明义务；对第三方内容需做文件级 carve-out（TPC patch / Remotion） |
| 统一为 MIT（改 `pyproject.toml:13` + `README.md:860` 两处） | 改动量最小；`LICENSE` 现状即 MIT，无需新增 NOTICE | 与 AGENTS.md §5 的 Apache 定位相悖；需回改 ADR-0012 备选方案表里"Apache-2.0 项目"的既有表述；无专利授权条款；已发布的 0.1.4 分发元数据（`License: Apache-2.0`）成为历史错误声明 |
| 保持现状不动 | 零工作量 | 同一作品两份授权文本 + 分发件自相矛盾（PKG-INFO 元数据 Apache / 内含 LICENSE 文件 MIT），且 README.en.md 对英文读者零披露；这是 N2 型债务，会随每次发版复制 |
| 双许可 `Apache-2.0 OR MIT` | 消费者可自选，兼容性最宽 | 需同时履行两套义务（含 MIT 的声明保留 + Apache 的 NOTICE）；PEP 639 `license-expression` 才支持 OR 表达式，而当前 `license = { text = … }` 形态与 setuptools>=64 基线不支持（见决策 ②）；对个人自用项目是纯粹的复杂度增加 |
| 保留 `mysql-connector-python`，依 Universal FOSS Exception 合规 | 不动执行链，零回归风险 | 需先建 NOTICE + 每portion许可声明；例外条款是 Oracle 单方文本可改版；镜像形态每次变更需重新判定；为**2 处代码**维护一套持续合规机制，与"简洁"优先级（AGENTS.md §10 第 5 条）不符 |
| 保留 `psycopg[binary]` 备用 | 将来接 Polaris/Postgres 时不用再装 | 全仓零引用却分发 LGPL 二进制（`libpq`/`libssl`/`libgssapi_krb5`）；真要接 Postgres 时版本约束也该重新评估，"提前装着"不产生任何价值 |

---

## 决策

### ① 项目许可证 = Apache-2.0：`LICENSE` 全文替换 + 新增 `NOTICE`

- `LICENSE` 替换为 Apache License 2.0 全文（文末 APPENDIX 段「How to apply the
  Apache License to your work」的 boilerplate **不填入项目名**——Apache 惯例是
  该段作为使用说明保留原样，归属信息放 `NOTICE`），版权行
  `Copyright 2026 HyperionS`（沿用 `LICENSE:3` 的既有权利人，不改署名主体）。
- 新增 `NOTICE`（仓库根），承载 Apache-2.0 §4(d) 要求的归属传递。内容限于
  **不可推导的事实**：项目版权行、FIBO/EDM Council 归属 + 商标声明、
  TPC-DS patch 的 EULA carve-out（决策 ⑤）、Remotion source-available 声明、
  依赖许可证清单的**生成方式**（指向 `exports/dependency-licenses.json`，
  不在 NOTICE 里硬列 108 包——硬列必然随 lockfile 漂移成假信息）。
- 替换后 MIT 全文从仓库消失。历史 commit 里的 MIT 文本不追溯（Git 历史即事实，
  不做改写；`README` §12 注明"0.1.4 之前 `LICENSE` 曾为 MIT，自 P0a 起统一为
  Apache-2.0"，让版本边界可查）。

### ② `pyproject.toml` 补 `License ::` classifier；PEP 639 `license-expression` 本批**不迁**

- 补 `classifiers = ["License :: OSI Approved :: Apache Software License"]`
  （当前实测为**零** classifier，`importlib.metadata` 取不到任何 License 分类）。
- `license = { text = "Apache-2.0" }` **保持不动**。PEP 639 的
  `license-expression = "Apache-2.0"` 形态需要 setuptools ≥ 77（上游发布说明，
  **未在本机实测**），而 `pyproject.toml:75` 基线是 `setuptools>=64`；抬构建基线
  与本批"消除矛盾"的目标无关，故记为代价 ⑥，另批处理。
- 附带事实：`.venv` 内 `setuptools` / `wheel` / `pip` **均未安装**（`uv` 管理，
  构建走隔离环境），故抬基线不需要本地预装，但也不意味着本批要动它。

### ③ 删除 `psycopg[binary]>=3.2`：消除 LGPL-3.0 与 2 个死包

`pyproject.toml:30` 整行删除 + `uv lock` 重新求解。依据：全仓**零代码引用**
（实测见背景段），Polaris 连接面在 MVP 从未落地（ADR-0004 已记录 Polaris 为
孵化器项目 + hadoop catalog 降级路径）。删除后 `psycopg` 与 `psycopg-binary`
两个 LGPL-3.0-only 分发同时出树，镜像内不再含 `libpq`/`libgssapi_krb5` 等
LGPL 二进制。将来真要接 Postgres 时按当时版本重新声明（并优先评估纯 Python
实现或 `psycopg[c]` 之外的形态），不做"提前占位"。

### ④ `mysql-connector-python` → PyMySQL：消除 GPLv2，判据是 **84 条锚定 hash 逐条不变**

- `pyproject.toml:29` 改为 PyMySQL；`eval/runner.py:40` 改 import、
  `:178-184` 的 `connection_timeout=15` 改 `connect_timeout=15`，其余调用
  （`cursor()` / `execute()` / `description` / `fetchall()` / `close()`）DB-API 同构。
- **唯一验收判据是可复现性，不是"能连上"**：`eval/gold/` 实测 **119** 个样本文件
  （106 份 `gold-*.json` + 13 份 `pp-*.json` 改写样本），按锚定状态分五类
  （2026-09-14 逐文件实测，脚本判据：`result_hash` 是否 64 位十六进制 +
  `snapshot_sha` 是否不含 `<` + `ambiguous` 布尔值）：

  | 类 | 数量 | 定义 | P-1 能否 EX 锚定 |
  |---|---|---|---|
  | A | **84** | 真值 `result_hash` + 实测 `snapshot_sha`（finance 66 + retail 18） | 已锚定 |
  | B | **13** | 非歧义 + 双占位符（gold-172~178、gold-072~077） | ✅ 能 |
  | C | **7** | `ambiguous: true` + `result_hash: null` + 实测 sha（gold-104/121/122/148/155、gold-047/068） | ❌ 设计上不执行 |
  | D | **2** | `ambiguous: true` + 双占位符（gold-179、gold-078） | ❌ 设计上不执行 |
  | E | **13** | `pp-*.json` 改写样本（无 `result_hash` 字段，不属 gold schema） | ❌ 不涉执行 |

  **迁移后这 84 条（A 类）必须逐条 hash 不变**——`result_hash` 由
  `eval/runner.py:153-161` 对 `str(value)` 拼接后取 sha256，而 `:156-158` 的
  docstring 明写「decimal 尾零稳定，实测 Doris SUM(decimal) 返回 `"344129059.3500"`
  恒定」，该实测是在 mysql-connector-python 下做的。若 PyMySQL 对 DECIMAL 的
  scale/类型处理有任何差异（返回 `str` 而非 `Decimal`、或尾零不同），hash 全变、
  EX 全红。
- **分母随批次顺序变动，必须显式登记**：上述 **84** 是本 ADR 撰写时（2026-09-14）
  的实测值。但批次唯一合法序（ADR-0018 落地注记 3）是 P-1 → … → P0a，而
  ADR-0017 判据 4 要求 P-1 把双占位样本锚定回填。故 P0a 开工时锚定样本数
  **应已从 84 增至 97**（= A 84 + B 13）。本判据的**真正分母不是写死的
  数字，而是公式**：「同时具备真值 `result_hash`（64 位十六进制）与实测
  `snapshot_sha` 的样本数」，P0a 开工当日重测并写入报告；若重测值 ≠ 97，
  必须先归因（P-1 是否部分失败）再跑驱动对比，**不得直接拿 84 当分母**
  （那会把 P-1 新锚定的 13 条排除在驱动回归比对之外，恰好漏掉时间智能
  这批最复杂的窗口函数 SQL——它们是 DECIMAL/数值类型差异最可能暴露的地方）。
  下文与判据 5、证伪条件里出现的「84 条」均按本公式读。
- **纠错（同日二次实测）：分母是 97 而不是初稿的 99**。初稿把「`snapshot_sha`
  为占位符的样本数」（= B 13 + D 2 = **15**，与 ADR-0017 判据 4 的「15 条」、
  代价 ④ 的「finance 8 + retail 7」同源）直接当成「P-1 可 EX 锚定的样本数」。
  实测否定该等式：D 类的 gold-179（「交易额同比」）与 gold-078（「销售额同比」）
  均为 `ambiguous: true` + tags `['comparison','clarify']`，属 ADR-0017 决策 ②
  「触发词命中但时间未解析 → 澄清，不猜」的载体，**返回澄清、无结果行、
  设计上永远不会有 `result_hash`**——其 `<待执行后填写>` 占位符是一个
  **永不可能被兑现的承诺**。故 15 可锚定 = 13（B）+ 2（D，仅能回填 sha）。
- **方法论教训（比数字本身重要）**：本条初稿已经遵循了「写成公式 + 撰写时
  实测值 + 重测时点」的纪律，**公式是对的，代入值仍错了**。原因是实测时只验了
  「占位符个数」而未验「这些样本按定义能否产生被占位的值」。故纪律需补一句：
  **实测值必须按公式的定义逐条验证归属，不得用另一个看似等价的计数代替**。
  本 ADR 内遵该纪律重测的三处：锚定分母（84 → 97，非 99）、可锚定增量
  （15 → 13）、`pp-*.json` 是否属 gold schema（否，13/13 实测失败，见 README KL #31）。
- 若 P-1 未按预期完成锚定（即 P0a 开工当日重测仍为 84），那 13 条 B 类样本
  **无法参与本判据**（没有可比对的锚定值）。此时要么先补
  锚定（真跑一次 `make eval` 回填），要么在报告中显式标注为"未锚定，不参与
  驱动对比"——不得用"其余都过了"掩盖这 13 条的空白（N1）。C/D 类共 9 条
  歧义样本则**永远不进分母**，不得把它们的 `result_hash: null` 计作失败。
- PyMySQL 自身许可证**当前未在本机实测**（`uv` 缓存内无其 `METADATA`）：
  PyPI 声明为 MIT，但按 ADR-0018 P0b 判据 1 的同一纪律，**安装后必须读
  `pymysql-*.dist-info/METADATA` 与包内 LICENSE 复核并回填**，未回填不得声称
  "已消除 GPL"（判据 4）。
- 纯 Python 驱动的性能**未实测**，本 ADR 不做任何性能声明；若实测发现单轮
  评测耗时显著上升，按代价 ③ 处理（回退或换 `mysqlclient`——但后者是 GPL 系
  需另行评估，故首选是接受耗时）。

### ⑤ 第三方内容四类登记 + TPC-DS patch 的文件级 carve-out

- 在 `NOTICE` 与 `README` §12 建立**四类登记表**（代码 / 数据与本体 / 依赖 /
  第三方工具与素材），每类逐条给：许可证类型、是否入库、当前合规依据、
  推翻条件。背景段的表即初稿，P0a 落地时搬进 `NOTICE`。
- `scripts/tpcds_kit_sf01.patch`：
  - **立即做**（P0a 内）：文件头加声明块，写明「本文件含 TPC-DS kit
    （`gregrahn/tpcds-kit` @ `5a3a817`，v2.10.0）`tools/r_params.c` 与
    `tools/scaling.c` 的 48 行原始代码（16 删除 + 32 上下文），受 TPC END USER
    LICENSE AGREEMENT VERSION 2.2 约束，**不适用**本仓 Apache-2.0」，并附条款
    9(b) 要求的全大写 legend「THE TPC SOFTWARE IS AVAILABLE WITHOUT CHARGE
    FROM TPC.」；同时在 `NOTICE` 登记该 carve-out。
  - **目标态**（同批或紧邻批）：把 patch 改写为**运行时转换脚本**（对 clone 下来的
    树做等价编辑，不嵌入任何 TPC 源文件行），使入库内容含 **0 行 TPC 源码**，
    从而该问题不再依赖任何法律判断。转换脚本本身是 Atlas 自写代码，Apache-2.0 无争议。
- `data/raw/`（PDGF 专有 EULA + TPC-DI 数据 + tpcds-kit）与 `data/fibo/{fibo-src,
  vendor}`：**保持不入库**，并把这条从"惯例"升级为"断言"（决策 ⑦）。
  `README` §12 需明写：这些内容由使用者按 `data/fibo/README.md:91` 与
  `scripts/setup_tpcds.sh` 的指令自行获取，各自遵循原始许可；Atlas 不分发它们。
- Remotion：`NOTICE` 登记「`docs/outreach-video/remotion/` 使用 Remotion
  （source-available，两层许可：Free License 适用于个人 / ≤3 人营利组织 / 非营利；
  否则需 Company License）；实装 4.0.523；`node_modules` 不入库；入库的 17 个文件
  为 Atlas 自写的 TSX/配置与渲染产物 `out/E0.mp4`」，并把「转为 >3 人营利组织」
  列入推翻条件。
- OMG Commons/LCC 与 TPC-DI 的"研究用途"/"注册下载"定性：`README` §12 改为
  **如实措辞**——「OMG Commons/LCC 的 RDF 内容由使用者自行下载（`data/fibo/vendor/`
  不入库），其许可条款**未在本仓留存文本**，使用前需自行向 OMG 确认；本仓只入库
  IRI 标识符字符串」。把无证据的定性换成可核查的边界描述（N1/N2）。

### ⑥ `README` §12 扩为四类完整声明，`README.en.md` 补 §12

- `README.md` §12（复测 `:858-861`）现有 2 行（代码 / 数据）扩为四类 + 指向 `NOTICE`，
  并显式给出"许可证版本边界"（0.1.4 之前 `LICENSE` 为 MIT）。
- `README.en.md` **新增一节** License（当前 `grep -ic license README.en.md` = 0 命中），
  与中文版四类结构一致。**节号按英文 README 自身章节序顺延为 §10**（实测其章节为
  §1~§9，与中文版的 13 节不同构），不得照抄中文版的 §12。
- 同行的「11 ADRs」过期数字（N1）**已于 ADR 撰写批次（2026-09-14）先行修正**，
  不等 P0a：该行是可直接证伪的过期数字，故不写死计数而写
  **测量命令 + 状态拆分**。拆分本身也会漂移，故逐次记录而非改写：
  撰写本条时 `ls infra/adr/0*.md | wc -l` = **24**（ADR-0001~0023 accepted，
  ADR-0024 为 proposed）；同日 ADR-0025 建档后 = **25**；0024/0025 经用户确认后
  （2026-09-14 复测）= **25 篇全 accepted**。`README.en.md` 的 §9 索引行（撰写本条时
  为 `:215`，2026-09-14 复测已漂到 `:227`）已按此形态修正并标注
  「0017~0025 accepted 但未实现」（N2）；
  另补一行 `docs/design/` 索引（撰写本条时该目录 3 页，原本在 `docs/README.md` 与
  英文 README 均零收录；页数随新增设计页漂移，以 `ls docs/design/*.md | wc -l`
  为准）。**先行的理由**：该数字是本批次新增 7 篇 ADR 直接
  造成的过期，与许可证工作无依赖；留在 P0a 会让一个可立即证伪的假数字多存活
  一个批次。P0a 因此只需完成 §12 本身（判据 13 已同步收窄）。

### ⑦ `make license-check`：把「`.gitignore` 是唯一防线」变成可执行断言

新增 `infra/license_check.py` + Makefile 目标 `license-check`（并挂进 `make lint`
之后、`make test` 之前的独立目标，**不并入** `lint`——`lint` 的语义是语义层校验，
`Makefile:97-108` 三个子目标全是 `semantic/*`，混入许可证断言会破坏该目标的可预期性）。
断言集（全部可脚本化，任一失败即 `exit 1`）：

1. `git ls-files data/raw data/fibo/fibo-src data/fibo/vendor` 输出**为空**
   （当前实测 0 行）——把专有 EULA 与外部本体挡在仓外的**唯一防线**从
   `.gitignore:21/:27/:28` 三行文本升级为 CI 可判定的断言；
2. `LICENSE` 首个非空行匹配 `Apache License`，且 `NOTICE` 存在；
3. `pyproject.toml` 的 `license`/classifier 与 `LICENSE` 文件一致（防止再次分叉）；
4. 已安装分发中许可证含 `GPL`/`LGPL`/`AGPL` 的包集合 ⊆ **白名单**
   （P0a 落地后白名单应为**空集**；白名单写成显式常量并注明理由，
   不允许用"忽略全部 copyleft"的宽松形态）；
5. `scripts/tpcds_kit_sf01.patch` 若仍存在，则其文件头必须含 TPC EULA 声明块与
   全大写 legend（决策 ⑤ 的立即项）；改写为运行时脚本后此断言自动跳过。
6. `--report` 模式输出 `exports/dependency-licenses.json`（沿 `exports/` 既有惯例：
   `dbt_semantic_models.yml` + `metric-export-report.json` 都是机器产物入库），
   内容为 108 包的 `name/version/license/引入路径`，由 `importlib.metadata` 生成
   ——`NOTICE` 只指向它，不复制它。

目录职责：`infra/license_check.py` 需在 AGENTS.md §4 的 `infra/` 行补一句
（`license_check.py：许可证与第三方内容守卫`），该文件变更按 §14 走 `contract`
提交类型，与 P0a 的代码变更**分开提交**（§8 禁止混合类型）。

---

## 理由

1. **矛盾的成本高于统一的成本**：统一为 Apache-2.0 只需改 1 个文件（`LICENSE`）
   + 新增 1 个文件（`NOTICE`），因为 `pyproject.toml:13`、`README.md:860`、
   ADR-0012 备选方案表、AGENTS.md §5 已经**四处预设**了这个基准；反向统一到 MIT
   要改 4 处并让已发布的 0.1.4 元数据变成历史错误声明。选阻力最小且与项目定位
   一致的方向。
2. **专利授权条款对"金融可信"定位是实质增益**：Apache-2.0 §3 显式授予专利许可
   并在 §3 末段含专利诉讼终止条款，MIT 无对应内容。一个以"可解释、可审计、
   面向金融场景"为卖点的项目，其许可证不含专利授权是能力缺口而非中立选择。
3. **GPL 组件的替换是"2 处代码 vs 一套持续合规机制"的比较**：背景段已如实记录
   Universal FOSS Exception 条款 iii 的文本读解——传染**可能不成立**。但即便成立
   读解正确，它换来的是三项持续义务（NOTICE 承载、各portion分别授权、镜像形态
   逐次判定）加两项不确定性（Oracle 单方文本可改版、镜像确实分发 Connector 本体）。
   为 2 处代码维护这套机制，违反 AGENTS.md §10 第 5 条（简洁）与第 3 条
   （可复现——例外条款版本变了，历史结论就不可复现）。**注意：本决策的理由不是
   "GPL 禁止使用"，而是"合规维护成本 > 替换成本"**。
4. **删 `psycopg` 是纯收益**：零引用的直接依赖 + LGPL 二进制进镜像，删除不损失
   任何能力（Polaris 连接面从未落地），也不需要任何替代实现。这类"死依赖"
   留在树里的唯一后果就是让许可证盘点多一行需要解释的 copyleft。
5. **登记优于清理**：TPC patch、Remotion、PDGF、OMG 四项都**不能**通过"改许可证"
   解决（它们不是我们的作品），只能通过"如实登记 + 缩小入库面"解决。所以决策 ⑤
   的形态是登记表 + carve-out + 断言，而不是"统一为 Apache-2.0"这句话本身。
   这也是为什么本 ADR 的标题里"第三方内容登记"与"许可证统一"并列。
6. **P0a 必须先于 P0b**：ADR-0018 P0b 判据 1 要求回填 5 个前端包许可证，而
   "兼容"这个词需要一个基准。先定基准再回填，否则回填结论要返工一次。

---

## 代价与限制

① **`LICENSE` 变更是一次授权变更，不是文档修订**：MIT → Apache-2.0 对已获取
旧版本副本的使用者不追溯（他们仍可依 MIT 使用其已获得的副本），但**新分发**
一律按 Apache-2.0。`README` §12 必须写出版本边界（决策 ⑥），否则"这个项目是
什么许可证"这个问题对历史版本仍无答案。本 ADR 不评估既有下游（实测：无已知
下游消费者，项目为个人自用 + 公开演示）。

② **PyMySQL 迁移触碰的是全执行链，不是评测脚本**：`execute_sql` 是**唯一** Doris
驱动收敛点，被 9 个文件直接 import、19 个文件间接耦合、11 个 make 目标依赖。
所以"改 2 行"的代价描述只在代码行数意义上成立；**验证代价**是全链路的：
`make eval`（84 条锚定 hash）+ `make api-verify`（A1~A7）+ `make rls-verify`
+ `make p1-verify` + `make metrics-verify` + `make e2e` + `make demo` 全绿才算过。
任一目标 skip（无 DB / 无快照 / 无 `ATLAS_JWT_SECRET`）都必须在报告中显式标注，
不得以"未跑"冒充"通过"。

③ **迁移失败的退路是有损的**：若 PyMySQL 的 DECIMAL 形态导致 hash 变化，可选项
只有三个——(a) 在 `_scalar()` 里对 DECIMAL 做显式规范化（`Decimal` 量化到固定
scale）后重新锚定 84 条 hash（**改变既有锚定值**，需在 `semantic/migrations/`
记录变更理由与影响面）；(b) 回退到 mysql-connector-python 并按备选方案表第 5 行
建合规机制；(c) 换 `mysqlclient`（C 扩展，GPL 系，需另行评估——不推荐）。
本 ADR 预设 (a) 为退路，但 (a) 会让"锚定 hash"这个可信性资产出现一次人工变更，
必须走 migrations 留痕，不能静默重跑覆盖。

④ **15 条占位 sha 的 gold 样本是既存债务，但其中只有 13 条是真债务**（分类见
决策 ④ 的五类表）：`result_hash` = `<待执行后填写>` + `snapshot_sha` =
`<待锁定后填写>`（finance 8 + retail 7，均带 `comparison` tag，全是 ADR-0017
时间智能批次新增）。其中 **B 类 13 条**（非歧义，yoy/pop/cumulative）使
"84/97 已锚定"这个比例无法达到 100%，也违反 AGENTS.md N6（评测必须绑定固定
快照 sha）的字面要求；补锚定需要真跑 `make eval`（Doris + 已锁快照），
不属许可证批次的能力范围 → 记为遗留项，进 `README` Known Limitations
（不得删除或美化既有条目，N4）。

而 **D 类 2 条**（gold-179、gold-078）的债务**不是未锚定，而是偏离了既有样本
约定**（2026-09-14 实测，对照 C 类 7 条的写法）：

| 字段 | C 类既有约定（7 条） | D 类实际（2 条） | 判定 |
|---|---|---|---|
| `result_hash` | `null` | `"<待执行后填写>"` | ❌ 歧义样本返回澄清、无结果行，该占位符**永不可能兑现** |
| `snapshot_sha` | 实测 sha（`b47a6c1`/`92033c9`） | `"<待锁定后填写>"` | ❌ 澄清行为也需绑定快照才可复现 |
| tags | 同时含 `ambiguous` + `clarification` | `['comparison','clarify']` | ❌ 缺 `ambiguous` tag；且 `clarify` 在全集**仅这 2 条使用**（实测 tag 词表：`clarification` 7 次 vs `clarify` 2 次），是同一概念的两个词——AGENTS.md §3「禁止自创同义词」在数据层的同款问题 |

这 2 条的正确修法是**改字段而非跑评测**（`result_hash` → `null`、补 `ambiguous` tag、
`clarify` → `clarification`、sha 回填为澄清行为实测时的快照），属 `eval` 类型变更，
应在 P-1 批次随 ADR-0017 判据 4 一并做，**不得混进本许可证批次**（§8）。

另：`eval/gold/schema.json:35-36` 允许
`result_hash` 为 string、`snapshot_sha` 为 string，**占位符字符串能通过 schema
校验**——即 `validate_gold.py` 抓不到这类未锚定样本，这是校验口径的缺口。

⑤ **TPC-DS patch 的目标态改写有功能风险**：把 102 行 patch 改成运行时转换脚本，
需要该脚本在 `5a3a817` 之外的 kit 版本上仍能定位到同样的代码位置（patch 依赖
精确行号与上下文，脚本依赖模式匹配）。若匹配失败，`setup_tpcds.sh` 的 SF0.1
生成路径断裂（零售域装载前序）。故改写必须保留"匹配失败即响亮报错并给出
手工 patch 指令"的分支，不得静默跳过（沿 `setup_tpcds.sh:74` 对 bison 缺失的
既有处理风格）。

⑥ **PEP 639 迁移留下一个已知的元数据形态债**：`license = { text = … }` 是
setuptools 上游已标记为过时的形态（PEP 639 后被 `license-expression` 取代；
此句为**上游文档口径，未在本机实测**），
本批不迁意味着 `License-Expression` 字段继续为 `None`（实测），
依赖 SPDX 表达式的工具链（如许可证扫描器）读不到机器可判定的许可证。
迁移前提是抬 `pyproject.toml:75` 到 `setuptools>=77`（未在本机实测），另批处理。

⑦ **`exports/dependency-licenses.json` 会随 lockfile 漂移**：108 包清单是
`uv.lock` 的函数，任何依赖变更都让入库的报告过期。缓解：`make license-check`
的 `--report` 是**生成**入口而非人工维护入口，且断言 4（copyleft 白名单）
针对**当前已安装**分发而非入库 JSON——即 JSON 过期不会造成假绿，只会造成
信息陈旧；`NOTICE` 因此只指向生成方式，不复制内容。

⑧ **Remotion 的合规状态与"我是谁"绑定**：Free License 资格取决于法律实体形态
（个人 / ≤3 人营利 / 非营利）。这不是代码可断言的事实，`make license-check`
无法覆盖，只能靠 `NOTICE` 登记 + 推翻条件（下节第 3 条）。若项目将来被用于
任何组织性用途，这一项需要**人工**重新判定，且判定结论无法自动化验证。

---

## 什么情况下应该推翻

1. **PyMySQL 迁移后 84 条锚定 hash 有任何一条变化且无法归因于数据变更** →
   决策 ④ 被证伪，按代价 ③ 走 (a)/(b)/(c)；若走 (b)，则本 ADR 关于"GPL 组件
   可低成本移除"的核心论据作废，需重新按备选方案表第 5 行建合规机制。
2. **Polaris 或任何 Postgres 元数据面真正落地**（ADR-0004 的降级路径未触发、
   Polaris 从孵化器毕业并接入）→ 决策 ③ 需重开：重新引入 Postgres 驱动时
   必须在该批次的 ADR 里做许可证评估，不得直接恢复 `psycopg[binary]`。
3. **项目主体从个人变为 >3 人营利组织**，或 `docs/outreach-video/` 的视频产出
   进入任何商业分发 → Remotion Free License 资格失效（需 Company License），
   决策 ⑤ 的登记项从"已合规"变为"待购买"；同时 PDGF/TPC EULA 的"个人使用"
   前提也需重新核对。
4. **需要把 `data/raw/` 或 `data/fibo/fibo-src/` 的任何内容入库**（例如为了让
   评测完全离线可复现而把 FIBO 本体入仓）→ 决策 ⑤ 与 ⑦ 的断言 1 直接冲突，
   必须先取得相应许可（FIBO 是 MIT 可入库但需保留声明；PDGF 是专有 EULA
   **不得**入库；TPC-DS kit 入库需满足 EULA 条款 9(a)~(d) 含全大写 legend）。
5. **setuptools 基线抬到 ≥77 或构建后端换为 hatchling/uv_build** → 决策 ② 的
   "本批不迁 PEP 639" 失去前提，应在同一批补 `license-expression = "Apache-2.0"`
   并删除 `license = { text = … }`（两者并存会被 setuptools 报错）。

---

## 验证方式

**P0a 批次判据（全部可脚本化）**：

1. `head -1 LICENSE` 输出含 `Apache License`；`wc -c LICENSE` 与 MIT 版的
   1087 字节不同（Apache-2.0 全文远长于此，可作粗断言）；`test -f NOTICE` 成立；
   `git grep -c "MIT License" -- LICENSE` = 0；
2. `importlib.metadata.distribution("atlas")` 的 `License` = `Apache-2.0`、
   `License :: OSI Approved :: Apache Software License` ∈ classifiers
   （实测当前为**空**）；`atlas.egg-info/PKG-INFO` 的 `License:` 与
   `License-File:` 指向的文本一致（不再出现"元数据 Apache / 内含 MIT"）；
3. `git grep -n "psycopg" -- "*.py"` = **0 命中**；`uv sync --frozen --no-dev` 后
   `importlib.metadata.distributions()` 中不含 `psycopg` / `psycopg-binary`；
   包总数由 108 降为 **<待实测后填写>**（删 2 包 + 可能的传递依赖变化，
   不预估数字，N1）；
4. `importlib.metadata.distributions()` 中许可证字段含 `GPL`/`LGPL`/`AGPL` 的
   包集合 = **空集**（`mysql-connector-python` 与 `psycopg*` 均出树）；
   `pymysql-*.dist-info/METADATA` 的许可证字段实测值 = **<待实测后填写>**
   （PyPI 声明 MIT，未复核前不得写入本 ADR）；MPL-2.0 三包
   （`certifi` / `pathspec` / `orjson`）保留并在 `NOTICE` 中登记为文件级 copyleft；
5. `make eval` 全绿，且 `eval/gold/` 中**全部锚定样本**（分母按决策 ④ 的公式
   当日重测；本 ADR 撰写时实测 **84** = finance 66 + retail 18，P-1 落地后
   预期 **97**，即 +13 条 B 类；C/D 类 9 条歧义样本永不进分母）的真值
   `result_hash` **逐条与迁移前一致**（`git diff eval/gold/`
   对这些文件**零改动**——这是本判据的强形态：不允许"重跑后重新锚定"来
   消除差异）；重测仍为双占位的样本在报告中显式标注为"未锚定，不参与驱动对比"；
6. 执行面全链路无回归：`make api-verify`（A1~A7）、`make rls-verify`、
   `make p1-verify`、`make metrics-verify`、`make e2e`、`make demo` 全绿；
   任一因环境 skip 的目标必须在产出报告中列出 skip 原因，不得计入"通过"；
7. `make license-check` 全绿：断言 1（`git ls-files data/raw data/fibo/fibo-src
   data/fibo/vendor` = 空）、断言 2（LICENSE/NOTICE）、断言 3（pyproject 与
   LICENSE 一致）、断言 4（copyleft 白名单 = 空集）、断言 5（patch 声明块）
   逐条独立可判定；**负向验证**：临时 `git add -f data/raw/tools/Tools/PDGF/pdgf.jar`
   → 断言 1 必须 `exit 1`（证明该守卫不是恒真），随后 `git reset` 复原；
8. `make license-check --report` 产出 `exports/dependency-licenses.json`，
   其条目数 = 实测已安装分发数，且每条含 `name` / `version` / `license` /
   `引入路径`（直接依赖标 `atlas`，传递依赖标父包名，与背景段的反向依赖表同口径）；
9. `scripts/tpcds_kit_sf01.patch`（若仍为 patch 形态）文件头含 TPC EULA 声明块
   与全大写 legend；若已改写为运行时脚本，则 `scripts/setup_tpcds.sh` 在
   **未 clone kit 的干净环境**下必须响亮报错（非静默跳过），且报错文本给出
   clone 指令（沿 `:27-30` 既有风格）；
10. `git check-ignore -v docs/outreach-video/remotion/node_modules` 命中
    `docs/outreach-video/.gitignore:1`；`git ls-files docs/outreach-video` =
    **17 个文件**（实测），其中不含任何 `node_modules/` 路径；
11. `make lint && make test` 全绿（`tests/` 下 `pytest --collect-only` 实测
    **565 例**，含 43 例 API 契约测试；`make test` 走的是
    `unittest discover -s tests`，`Makefile:233-234`）。本批不改任何测试断言，
    故不得出现"为了过而放宽断言"。

**文档判据**：

12. `README.md` §12「许可与声明」扩为四类（代码 / 数据与本体 / 依赖 / 第三方工具
    与素材）+ 指向 `NOTICE` + 许可证版本边界（0.1.4 之前为 MIT）；
    OMG 与 TPC-DI 的措辞由"研究用途"/"注册下载"改为如实的边界描述（决策 ⑤）；
    **定位以 `grep -n "^## 12" README.md` 为准，不套用撰写时行号**（本判据撰写时为
    `:754-757`，2026-09-14 复测为 `:858-861`，P0a 自身改动后会再次漂移）；
13. `README.en.md` 新增一节 License（当前 `grep -ic license README.en.md` = **0**），
    四类结构与中文版一致。**节号不得照抄中文版的 §12**：英文 README 章节序为
    §1~§9（`grep -nE "^## " README.en.md`，2026-09-14 实测末节为 §9 Where to go next），
    故新节为 **§10**（本条初稿误写 §12，已于 2026-09-14 纠正）；
    同行的「11 ADRs」修正**已于 ADR 撰写批次完成**（决策 ⑥，实测 24 篇），本判据
    只验该新增节；复验时仍需 `grep -c "numbered ADRs" README.en.md` = 1 且其行的
    计数等于 `ls infra/adr/0*.md | wc -l`（防后续新增 ADR 时再度过期）；
14. `NOTICE` 存在且只含不可推导的事实（版权行 / FIBO+商标 / TPC patch carve-out /
    Remotion 两层许可 / 依赖清单生成方式），**不硬列** 108 包；
15. AGENTS.md §4 的 `infra/` 行补 `license_check.py` 职责（走 `contract` 提交，
    与 P0a 代码变更分开提交，§8）；`data/fibo/README.md:99` 的 FIBO MIT + 商标
    声明与 `NOTICE` 一致（不出现两份不同措辞的归属）。

**证伪条件**：若判据 5 无法达成（84 条 hash 有变化）且代价 ③ 的三条退路全部
不可接受，则决策 ④ 被推翻——此时项目必须回到"保留 GPLv2 驱动 + 依 Universal
FOSS Exception 建合规机制"的备选方案，并且 `NOTICE` 必须承载 Connector 的
GPLv2 归属与例外条款版本；本 ADR 理由 3 的成本比较需按实际投入重写。
若判据 7 的负向验证失败（`git add -f` 后断言 1 仍为绿），说明守卫实现读的是
工作区而非索引，决策 ⑦ 的核心价值（把 `.gitignore` 升级为断言）不成立，
必须改为 `git ls-files --cached` 口径重做。

---

## 落地注记：P0a 批次（2026-09-16；沿 ADR-0011 `:86` / ADR-0021 落地注记先例，不改裁定正文）

### 判据 1~15 兑现（证据 = 实跑产物，N1；全扫逐条回执见 `docs/design/dev-plan-0017-0025.md` §2.4）

| # | 兑现证据 |
|---|---|
| 1 | `head -1 LICENSE` = `Apache License`（11357 字节 ≠ MIT 版 1087）；`NOTICE` 存在（2807 字节）；`git grep -c "MIT License" -- LICENSE` = 0 |
| 2 | `importlib.metadata.distribution("atlas")`：`License` = `Apache-2.0`、Apache classifier ∈ classifiers；`atlas.egg-info/PKG-INFO` 的 `License-File` = `LICENSE` / `NOTICE`（元数据与文本不再矛盾） |
| 3 | `git grep -n "psycopg" -- "*.py"` = 0；runtime-only（`uv sync --frozen --no-dev`）90 分发中 psycopg 族零命中；包总数两口径见下「正文占位符说明」 |
| 4 | 全量环境（109 分发）copyleft 扫描 = 空集；MPL-2.0 三包（`certifi` / `pathspec` / `orjson`）在 `NOTICE` 登记为文件级 copyleft；`pymysql` 实测值见下 |
| 5 | `make eval` 全绿：分母公式重测 **97**（= A 84 + B 13，与预期一致）；finance 73/73 EX + 6/6 clarify、retail 24/24 + 3/3、双域 `exec_errors` 0 / `guard_blocked` 0；`git diff eval/gold/` **空**；9 条 C/D 类歧义样本以 `ambiguous` 形态不进分母 |
| 6 | 六执行面全绿且无 skip：`api-verify` 11/11、`rls-verify` 差异集 2、`p1-verify`、`metrics-verify`、`e2e` 7/7、`demo` 14/14 |
| 7 | `make license-check` 5/5；负向验证：`git add -f …pdgf.jar` → 脚本侧 `exit 1`（`make` 包装为 2）→ `git reset` 复原后 PASS |
| 8 | `REPORT=1` → `exports/dependency-licenses.json` 109 条四字段（`name` / `version` / `license` / `引入路径`，反查口径同背景段）；命令形态偏差见下 |
| 9 | `scripts/tpcds_kit_sf01.patch` 文件头含 EULA 声明块与全大写 legend（断言 5 实检 11 行） |
| 10 | `git check-ignore -v docs/outreach-video/remotion/node_modules` 命中 `docs/outreach-video/.gitignore:1`；`git ls-files docs/outreach-video` = 22（撰写时 17，见下） |
| 11 | `make lint` 5 项全过 + `make test` Ran 743（撰写时 565——不进断言强度，见下） |
| 12 | `grep -n "^## 12" README.md` = `:908`；四类完整声明 + 许可证版本边界 + OMG / TPC-DI 如实措辞 |
| 13 | README.en.md §10 License（`numbered ADRs` 行计数 25 = `ls infra/adr/0*.md`） |
| 14 | `NOTICE` 只含不可推导事实、不硬列包（MPL-2.0 三包登记为例外） |
| 15 | AGENTS.md §4 已补 `license_check.py` 职责（`contract` 独立提交）；`data/fibo/README.md` 措辞与 `NOTICE` 一致 |

### 其他落地事实

- **提交拆分（4 个）**：`0e8e531` `chore`（工作项 3/4/7 + 证据链产物）/
  `cdec901` `docs`（工作项 1/2/5/6）/ `8258015` `contract`（AGENTS.md §4）/
  本 `docs(planning)` 记账提交（本注记 + dev-plan §2.4 回执 + 状态同步）。
- **判据 8 命令形态**：GNU make 拒绝未知长选项（实测 `make license-check
  --report` → `unrecognized option`），Makefile 走本仓变量惯例 `REPORT=1`；
  脚本侧 `python -m infra.license_check --report` 原样保留判据的形态。
- **判据 10 文件数漂移**：`git ls-files docs/outreach-video` = 22（撰写时 17；
  归因 a517286 用户侧提交净 +5），check-ignore 断言不受影响。
- **判据 11**：新锁快照（见下）触发契约测试账本联动（主报告 13→14、首条 sha
  ccb4c8b→7c966e9），按该测试 docstring 明示程序更新常量 + 漂移登记——非放宽
  断言（先例 83457e7）；实测 743 例相对撰写时 565 的增长为期间批次新增所致。
- **快照与 values**：`make eval` 前重锁 7c966e9（同数据多锁，指纹与 ccb4c8b
  全一致），判据 5 的机械证据绑定此 sha（`eval/reports/7c966e9.json`）；21 个
  `semantic/values/*.json` 按 `make profile-values` 重绑定（值集不变）。

### 正文占位符说明（正文不改，由本注记回填）

- 判据 3 的「包总数由 108 降为 `<待实测后填写>`」：全量 dev 环境 **109**
  （含 atlas 自身）、runtime-only（`--no-dev`）**90**——两口径均登记，判据本体
  用 runtime-only 对照「删 2 包 + 传递依赖变化」的语义。
- 判据 4 的「`pymysql-*.dist-info/METADATA` 许可证字段实测值 = `<待实测后填写>`」：
  `License-Expression` = **MIT**。
- **遗留观察项**：`semantic/ossie/atlas_finance.ossie.yaml:5` 的「（公开注册下载）」
  为数据获取方式的事实描述（非许可定性）；判据 12 的措辞修正范围为 README §12
  （已改），此处登记不修。

