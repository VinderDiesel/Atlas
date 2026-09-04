# Atlas — Trustworthy AI Question-Answering over Data

> **FIBO-anchored semantic layer + deterministic NL2SQL + explainable Data Agent**
> for the finance domain (TPC-DI) and, since 2026-09-04, retail (TPC-DS SF0.1).
>
> Status: **under active development**.
> Every number in this repo comes from reproducible script artifacts, not marketing.
> See [EVAL_REPORT.md](EVAL_REPORT.md) for the machine-generated evaluation summary.
>
> Full Chinese documentation (the canonical one): [README.md](README.md).
> This file is a maintained **subset** (quick start / architecture / evaluation
> reproduction / the two domains / known-limitations digest) with links back to the
> Chinese original — the two files must not drift into parallel versions.

---

## 1. One-liner

A trustworthy AI-to-data platform: a **unified semantic layer** (Apache Ossie +
Atlas governance extensions) where one business word has exactly one definition,
a **deterministic planner/compiler** turns questions into read-only SQL, and a
**Guard** enforces read-only + row-level policies before execution on Apache Doris
over Apache Iceberg.

Design pillars (details in [README.md §1](README.md)):

- **Determinism first** — in-domain questions go through Planner → Compiler → Guard,
  zero LLM tokens; LLM/RAG is a comparison strategy, not the default (measured: 44/44
  Plan Acc both ways, cost differs by orders of magnitude).
- **Git is the single source of truth** — semantic YAML, prompts, gold set, adapters
  are all versioned; any behavior traces back to a commit.
- **Evaluation is binding** — every number is bound to a locked data snapshot
  (`data/snapshots/<sha>.meta.json`) and a report JSON under `eval/reports/`.

## 2. Architecture (condensed)

```text
Users (Chat UI / Notebook / BI / API)
  → API Gateway + audit + budget + read-only SQL firewall + OTel
  → Data Agent (LangGraph state machine; deterministic tools first:
     plan → execute → explain; clarify / candidate / handoff branches)
  → Semantic retrieval (Milvus + graph) · Metric registry (YAML/Git) · Compiler
  → Semantic Compiler: FIBO concept / metric / dimension / time / filter → SQL
  → Apache Doris 4.1 (OLAP, native Iceberg catalog)
  → Apache Iceberg V2 on MinIO · Apache Polaris (REST catalog + object-level RBAC)
  → PostgreSQL · Milvus · NetworkX
  → Evaluation/feedback loop: gold set, Execution EX, judge, corrections, SFT
```

Full diagram: [README.md §2](README.md).

## 3. Two semantic domains

| Domain | Semantic model | Data | Datasets | Gold samples |
|---|---|---|---|---|
| **finance** (primary) | `semantic/ossie/atlas_finance.ossie.yaml` | TPC-DI Batch1, 2012-07-07~2017-07-07 (measured), 8 DWD tables | accounts/brokers/customers/ securities/trades/holdings/cash | 70 (62 zh + 8 en), all anchored |
| **retail** (second main domain since 2026-09-04) | `semantic/ossie/atlas_retail.ossie.yaml` | TPC-DS SF0.1 via `make seed-retail`, sales window 1998~2002 (measured), 4 DWD tables | date/item/store/store_sales | 19 (14 zh incl. 1 ambiguous + 5 en), all anchored |

Retail "promotion" history (audit note): an early ruling said retail samples were
"historical comparison only, no longer added" because **no data existed**; once
TPC-DS SF0.1 was loaded (`make seed-retail`, 2026-09-04) the ruling's reason
disappeared and retail became the second main evaluation domain (overturn note in
[eval/gold/README.md](eval/gold/README.md), following the ADR overturn-conditions
process).

Language is a **sample attribute** (`lang_en` tag), not a domain axis: reports
break down per domain then per language (zh/en), never mixed.

## 4. 30-minute quick start

Prerequisites: Docker Compose (≥ 2.20), Python 3.11, uv or pip, Git ≥ 2.40,
≥ 16 GB RAM recommended (Doris FE+BE; fallback path in ADR-0004).

```bash
git clone <your-repo-url> atlas && cd atlas

make install        # 1) dependencies
make up             # 2) Apache stack: Polaris / Doris / Iceberg+MinIO / Milvus / Grafana
make seed           # 3) TPC-DI Batch1 → 17 ODS tables → lock snapshot
make dwd            # 3b) 8 idempotent DWD tables in Doris
make lint-ossie     # 4) semantic layer validation (Ossie schema)
make lint-governance
make plan Q="2013 年第二季度总交易额是多少？"   # 5) full chain, one question
make compile
make eval           # evaluation report (must match the locked snapshot)
make report && cat EVAL_REPORT.md   # 6) machine-generated summary
make export         # 7) export to dbt MetricFlow YAML
```

Optional — retail domain data (TPC-DS SF0.1):

```bash
make seed-retail    # dsdgen bootstrap → dim_date/dim_item/dim_store/store_sales → dwd
make eval           # per-domain sections (finance + retail), never mixed
make demo           # end-to-end demo tests (12 bilingual questions + 2 RLS identities)
```

Environment honesty: reproduction needs the Docker stack up (`docker compose up`);
evaluation additionally requires the current HEAD snapshot to be locked
(`data/snapshots/<sha>.meta.json` present — the snapshot tool refuses to overwrite
a stale one).

## 5. Evaluation — how numbers are produced

The gold set (`eval/gold/finance/` and `eval/gold/retail/`, 89 samples total) is
**manually labeled** — not auto-generated. Each sample pins `expected_metric /
expected_dimensions / expected_time / expected_sql / result_hash / snapshot_sha`.

Chain: `gold question → Planner → Plan (Plan Acc) → Compiler → Guard (read-only +
table whitelist = snapshot tables) → Doris execution → sha256 of rows`, compared
against the anchored `result_hash` (= EX pass). Ambiguous samples must return a
clarification question instead of a guess.

- Evaluation only trusts the current HEAD: the runner re-checks the data snapshot
  fingerprint and refuses to produce a report on drift.
- First execution auto-anchors: placeholder `result_hash` gets backfilled with the
  measured sha256 (bound to `snapshot_sha`); later runs compare.
- Reports: `eval/reports/<sha>.json`, per-domain sections; `EVAL_REPORT.md` is
  machine-generated from them (`make report`) — no hand-written numbers, every cell
  has a `source` column (AGENTS.md N1).
- Latest terminal verification (snapshot `b933e20`, full run, zero regression):
  finance 70 — zh 57/57 Plan Acc + 5/5 clarify, en 8/8; retail 19 — zh 13/13 +
  1/1 clarify, en 5/5; EX 65/65 + 18/18, 0 execution errors. Report:
  `eval/reports/b933e20.json` (per-domain sections, not mixed).
- Deterministic-coverage analysis: zero-LLM coverage per domain — finance 70/70,
  retail 19/19 (report `eval/reports/baseline-compiler-b933e20.json`, see
  [docs/baseline-compiler.md](docs/baseline-compiler.md)); the RAG+LLM strategy
  measured 44/44 identical in the gold-50 era (report `rag-llm-openai-7d48dcb.json`,
  historical comparison, not mixed with the current run).

## 6. Serving API (v1)

HTTP face of the same deterministic chain (ADR-0012): `/plan /compile /ask` with
Bearer JWT auth (`make token`), `engine=stub` default.

- **Multi-model routing (2026-09-05)**: request body `model` field selects the
  semantic domain — `finance` (default, backward compatible) or `retail`; unknown
  value → 422. Session keys are isolated per model.
- **Service-face hardening (2026-09-05, ADR-0011 annotation)**: `/ask` pushes the
  verified Bearer claims down as the row-level identity (`agent.ask(identity=…)`
  → Guard Policy injection; `/plan` `/compile` have no execution face and stay
  identity-free). Visible signals:
  - `explanation.policy_effect` — 「行级策略已生效（角色 X，策略 Y）」: role and
    policy name only, **never the condition value**;
  - a `session_id` is bound to the first request's identity fingerprint — changing
    identity on the same session → **422「会话身份冲突」**;
  - per-token shared-bucket rate limit → **429 + `Retry-After`** (`/health` public
    and 401 paths are exempt).
- **Hardening env**: `ATLAS_AUDIT_DISABLED=1` turns the business-audit JSONL off
  (`serving/audit/`, one line per request, no SQL); `ATLAS_RATE_LIMIT_MAX` /
  `ATLAS_RATE_LIMIT_WINDOW_SECONDS` override the **placeholder** 60/min default
  (0 disables). Same placeholders documented in `.env.example`.
- `/health` is public; snapshot-meta missing → 503 on `/ask`.
- Boundaries (KL #28): sessions / identity fingerprints / rate limit are all
  in-process — uvicorn must run `workers=1`; the audit JSONL is a local file,
  non-tamper-proof (export before production); no IdP yet — tokens are locally
  signed HS256 (0011 decision 4 stays open). Verified by `make api-verify`
  A5-A7 (`eval/reports/api-acceptance-4a547e7.json`: role-difference / session
  identity-conflict 422 / retail category restriction + cross-domain Guard block).

```bash
make serve    # uvicorn 127.0.0.1:8000 (single process)
curl -H "Authorization: Bearer $(make token)" \
  http://127.0.0.1:8000/plan -d '{"question":"What were total sales in 1999?","model":"retail"}'
```

## 7. Data-agent demo (tests, not scripts)

`make demo` runs `tests/test_demo_e2e.py` — the quick-start README as an executable
integration test: 12 bilingual end-to-end questions (finance + retail, one
DataAgent each) with deterministic assertions, plus 2 identity-bearing RLS cases
(region_manager TN / category_analyst 2 categories) through the same
`resolve_policy → Policy injection → Doris` path as `rls-verify`. Skips with a
reproduction hint when Doris or the locked snapshot is missing; the 2 RLS cases
skip alone when `ATLAS_JWT_SECRET` is unset.

## 8. Known limitations (digest)

Full text (Chinese, canonical, do not delete or beautify): [README.md §10](README.md).
Condensed:

1. Limited data scale (TPC-DI default; ~2.94M rows) — not comparable to real PB-scale finance.
2. Schema linking validated only on the registered 8-dataset domain; 1000-table scale is an unverified hypothesis.
3. Simplified row-level policy implementation — not tested against real IAM/audit/compliance.
4. MVP single-machine; no HA, no load testing.
5. Numbers discipline: any new number must come from script artifacts (AGENTS.md N1).
6. Apache Ossie 0.2.0.dev0 is DRAFT; incubation project (migration path in ADR-0002).
7. Governance fields (owner/lineage/freshness) are an Atlas extension, self-built.
8. Polaris/Doris add operational complexity (ADR-0004 fallback exists).
9. sqlglot instead of Calcite: no CBO (ADR-0005).
10. Charting/attribution minimal (deterministic only).
11. Planner is a deterministic rule engine: dimension parsing needs explicit grouping words; relative time is **design-unsupported** (returns clarification — fixed-snapshot evaluation would drift; ADR-0014 ③); filter supports dimension equality/exclusion and metric thresholds.
12. Retrieval corpus and queries share provenance (same-source validation); Milvus vectors are lexical (no embedding semantics).
13. Graph constraint is more conservative than the Compiler on multi-hop recalls.
14. Guard cross-table predicate join injection landed; remaining edge = reject when no legal join path exists.
15. Row-level security boundaries: Polaris is object-level only (no row-level — measured); geographic/category roles measured across both domains via rls-verify (finance diff-set 3; retail diff-set 2 — region_manager TN equals hq results because SF0.1 has a single state, stated honestly; the category_analyst restriction carries the difference).
16. Derived-metric numeric backing closed (gold-156~162 anchored, snapshot 30b8344).
17. Negative average cash balance is a data property, not a bug (户均现金余额).
18. Retrieval regression after corpus growth 15→20 metrics — attribution and follow-up documented.
19. RAG+LLM measured equal accuracy with real token cost; LoRA rows blocked (no GPU) — honestly marked, not estimated.
20. No official text2sql benchmark yet (BIRD finance comparison blocked until a text2sql generator is available).
21. `make ask` and dashboard behavior boundaries (evaluation numbers do not appear in Grafana).
22. `make train` blocked on this machine (no CUDA); training path not yet measured.
23/24. Observability: QPS panels have no series by design (deterministic chain, 0 tokens).
25. Session memory is in-process MemorySaver (lost on restart).
26. dbt MetricFlow export is three-state, lossy by design: agg 14 / ratio 3 / unmapped 3 (SUM(a*b)) with reasons registered per item.
27. FIBO L2 mapping covers 19/20 metrics; `total_trade_tax` gap registered as a to-do (needs a broader concept-closure).
28. HTTP API v1 boundaries (see §6 above).
29. Unsupported filter shapes (free-form two-metric comparison etc.) → clarification, never a guess.

## 9. Where to go next

- [EVAL_REPORT.md](EVAL_REPORT.md) — machine-generated evaluation summary (bound to current HEAD).
- [eval/gold/README.md](eval/gold/README.md) — gold-set structure, language tags, batch history, ruling-overturn audit note.
- [infra/adr/](infra/adr/) — 11 ADRs, each with overturn conditions.
- [docs/GLOSSARY.md](docs/GLOSSARY.md), [docs/release-notes-v0.1.md](docs/release-notes-v0.1.md).
- Chinese canonical README: [README.md](README.md).
