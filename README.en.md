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
| **finance** (primary) | `semantic/ossie/atlas_finance.ossie.yaml` | TPC-DI Batch1, 2012-07-07~2017-07-07 (measured), 8 DWD tables | accounts/brokers/customers/ securities/trades/holdings/cash | 79 files (71 zh + 8 en); **66 anchored**, 13 pending (measured 2026-09-14 — see KL #31) |
| **retail** (second main domain since 2026-09-04) | `semantic/ossie/atlas_retail.ossie.yaml` | TPC-DS SF0.1 via `make seed-retail`, sales window 1998~2002 (measured), 4 DWD tables | date/item/store/store_sales | 27 files (22 zh + 5 en); **18 anchored**, 9 pending (measured 2026-09-14 — see KL #31) |

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
Node.js ≥ 20 (optional — frontend console only: `make ui-check` / `make ui-build`;
not needed for the Python pipeline),
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

The gold set (`eval/gold/finance/` and `eval/gold/retail/`, **106 samples** measured
2026-09-14, plus 13 `pp-*.json` paraphrase samples that are *not* part of the gold
schema — see KL #31) is
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
- **Report currency (measured 2026-09-14)**: of the 48 artifacts in `eval/reports/`,
  7 are per-domain main reports; the newest non-dry one is `5d1e22b.json`
  (2026-09-09T11:41:32+08:00, finance 71 + retail 20 = 91). **No report covers the
  current 106 samples.** The mechanistic cause is registered in ADR-0017 cost ③
  (`enforce` sits outside the `try` in `eval/runner.py`, so a Guard rejection aborts
  the whole round); a report covering 106 is only possible after P-1 acceptance
  criteria 3/4 land. The following is a **historical reference** (numbers are what
  that report actually contains; not rewritten):
- Terminal verification at snapshot `b933e20` (full run, zero regression):
  finance 70 — zh 57/57 Plan Acc + 5/5 clarify, en 8/8; retail 19 — zh 13/13 +
  1/1 clarify, en 5/5; EX 65/65 + 18/18, 0 execution errors. Report:
  `eval/reports/b933e20.json` (per-domain sections, not mixed).
- Deterministic-coverage analysis: zero-LLM coverage per domain — finance 70/70,
  retail 19/19 (report `eval/reports/baseline-compiler-b933e20.json`, see
  [docs/baseline-compiler.md](docs/baseline-compiler.md)); the RAG+LLM strategy
  measured 44/44 identical in the gold-50 era (report `rag-llm-openai-7d48dcb.json`,
  historical comparison, not mixed with the current run).

## 6. Serving API (contract v2: `/api/v1` prefix + governance plane)

HTTP face of the same deterministic chain (ADR-0012; URL contract v2 = ADR-0022,
in `serving/api.py` + `serving/governance.py`). **Hard cut**: all business and
governance paths live under the `/api/v1` prefix (only the root `/health` is kept
as the probe contract, dual-mounted with identical body); old paths return 404
with no compatibility window. Governance endpoints read Git files / evaluation
artifacts only — **no DB, no Agent construction** (the business face returns 503
while governance stays 200 when the snapshot is missing). `engine=stub` default.

| Endpoint | Prefix | Auth | Bucket | Request | Response |
|---|---|---|---|---|---|
| `GET /health` | root + `/api/v1` (dual-mount, same body) | public | — | — | **8 keys**: `status`, `head_sha`, `snapshot_sha`, `snapshot_source`, `snapshot_bound_to_head`, `snapshot_created_at`, `snapshot_tables`, `boot_id` (liveness + **actual binding** + process identity; `degraded` with `null` binding keys when nothing can be bound — still HTTP 200). Authoritative key set: ADR-0019 decision ⑥ + ADR-0020 decision ⑦ |
| `POST /plan` | `/api/v1` | Bearer | business | `{question, model?}` (≤500 chars) | `{kind: "plan", plan}` or `{kind: "clarify", clarification}` (ambiguity → 200) |
| `POST /compile` | `/api/v1` | Bearer | business | Plan JSON (`metric/dimensions/time/filters/order_by/limit`, incl. `model?`) | `{sql}` (Doris read-only dialect); malformed / compile error → 422 |
| `POST /ask` | `/api/v1` | Bearer | business | `{question, session_id?, model?}` | full TurnResult (kind ∈ answer/clarify/blocked/error; Decimal→str precision, datetime→ISO8601) + `snapshot_sha` / `snapshot_bound_to_head` echoed; snapshot-binding failure → 503 (original cause in the message) |
| `POST /plan/execute` | `/api/v1` | Bearer | business | Plan JSON (same shape as `/compile`) + `session_id?` + `question?` (display-only, defaults to the plan's normalized text) | same shape as `/ask` (kind ∈ answer/blocked/error — never `clarify`, no Planner involved; **invalid plan → 200 + `kind="error"`**, not 422 not 500). No `session_id` → one-shot thread, no session state; with one → same session space as `/ask` (identity-fingerprint 422 applies) and the plan becomes the completion baseline for follow-up fragments (ADR-0022 cost ⑥) |
| `POST /analyze` | `/api/v1` | Bearer | business | `{question, session_id?, model?}` (AskBody identical to `/ask`; identity comes only from Bearer claims; falls back to a plain ask when there is no analysis intent, ADR-0026 decision ③) | Same field set as `/ask` plus a top-level `analysis` key (ADR-0026 decision ⑥): a **17-key projection** (schema_version / intent / status / metric / dimension / baseline / current / filters / snapshot_sha / semantic_sha256 / recipe_version / totals / items / steps / reason_code / text / elapsed_ms), keys always present, `null` as a whole for non-analysis turns (intent fallback or clarify). `analysis.status` ∈ ok/unavailable/blocked/error (all terminal — clarify rounds project the whole object as null and carry no status), machine-readable cause in `reason_code`; `analysis.steps[]` in the fixed role order (baseline_total → current_total → current_by_dimension → baseline_by_dimension) carry role/kind/sql — success steps also carry columns/rows/latency_ms, failed steps blank sql/columns/rows and keep only role/kind/latency_ms/reason_code (rejected SQL never leaves the service). totals (baseline/current/delta) and items values are always **Decimal strings** or null; when synthesis is unavailable, totals=null and items=[]. The parent turn never masquerades as a single-SQL result (sql/explanation null, rows empty, row_count=0). **Analysis is fixed four-step template compilation — no LLM in the loop**; the 422 session-identity-conflict rule is the same as `/ask`; snapshot-binding failure → 503 from the same source |
| `GET /governance/models` | `/api/v1` | Bearer | governance | — | `{kind: "governance.models", count, sources, items}` — semantic model list |
| `GET /governance/metrics` | `/api/v1` | Bearer | governance | `?model?` (finance default) | `{kind: "governance.metrics", …}` — metric list (governance extension + FIBO alignment) |
| `GET /governance/dimensions` | `/api/v1` | Bearer | governance | `?model?` | `{kind: "governance.dimensions", …}` — dimension list (physical column + value-domain status) |
| `GET /governance/synonyms` | `/api/v1` | Bearer | governance | `?locale?` (zh_cn default) | `{kind: "governance.synonyms", …}` — locale dictionaries (incl. empty-placeholder flag) |
| `GET /governance/values` | `/api/v1` | Bearer | governance | — | `{kind: "governance.values", …}` — value-domain registry (incl. skipped + reason); drill-down `GET /governance/values/{item}` |
| `GET /governance/policies` | `/api/v1` | Bearer | governance | — | `{kind: "governance.policies", …}` — row policies + role directory |
| `GET /governance/reports` | `/api/v1` | Bearer | governance | — | `{kind: "governance.reports", …}` — report index; drill-down `GET /governance/reports/{name}` |
| `GET /governance/snapshots` | `/api/v1` | Bearer | governance | — | `{kind: "governance.snapshots", …}` — snapshot list (created_at desc + latest flag) |

> The 14 rows fold the 17 OpenAPI paths (dual `/health` = 1 row; the `values` /
> `reports` collection+drill pairs = 1 row each; `POST /api/v1/analyze` added
> since 0026); the path set is pinned by
> `tests/test_api_contract_v2.py` (`EXPECTED_PATHS`, 17 literals since 0026). All governance
> collections share the `{kind, count, sources, items}` envelope and are GET-only.

- **Multi-model routing (2026-09-05)**: request body `model` field selects the
  semantic domain — `finance` (default, backward compatible) or `retail`; unknown
  value → 422. Session keys are isolated per model.
- **Service-face hardening (2026-09-05, ADR-0011 annotation)**: `/api/v1/ask` and
  `/api/v1/plan/execute` push the verified Bearer claims down as the row-level
  identity (`agent.ask(identity=…)` → Guard Policy injection; `/api/v1/plan`
  `/api/v1/compile` have no execution face and stay identity-free). Visible signals:
  - `explanation.policy_effect` — 「行级策略已生效（角色 X，策略 Y）」: role and
    policy name only, **never the condition value**;
  - a `session_id` is bound to the first request's identity fingerprint — changing
    identity on the same session → **422「会话身份冲突」**;
  - per-token **two independent buckets** (ADR-0022 decision ⑥): business
    (`plan`/`compile`/`ask`/`plan/execute`/`analyze`) vs governance (`/api/v1/governance/*`)
    — exceeding either → **429 + `Retry-After`**, the bucket named in `detail`
    (`/health` public and 401 paths are exempt).
- **Hardening env**: `ATLAS_AUDIT_DISABLED=1` turns the audit JSONL off
  (`serving/audit/`, one line per business/governance request incl. 429/422
  rejections, no SQL); `ATLAS_RATE_LIMIT_MAX` / `ATLAS_RATE_LIMIT_WINDOW_SECONDS`
  override the **placeholder** 60/min business-bucket default; the governance
  bucket has its own `ATLAS_GOVERNANCE_RATE_LIMIT_MAX` /
  `ATLAS_GOVERNANCE_RATE_LIMIT_WINDOW_SECONDS` (**placeholder** 240/min — a
  computed rationale: ~10 requests per governance navigation). 0 disables either.
  Same placeholders documented in `.env.example`.
- `/health` (dual-mounted at the root and under `/api/v1`, identical body) is
  public and returns **8 keys** — `status`, `head_sha`, `snapshot_sha`,
  `snapshot_source`, `snapshot_bound_to_head`, `snapshot_created_at`, `snapshot_tables`,
  `boot_id` (ADR-0019 decision ⑥ + ADR-0020 decision ⑦). When nothing can be bound the
  status is `degraded` and the binding keys are `null` — same key set, HTTP still 200
  (probe semantics).
- **Snapshot-binding visibility** (ADR-0019 decision ①/⑥): the runtime resolves the
  snapshot in three steps (`ATLAS_SNAPSHOT_SHA` → HEAD → newest locked), so it may
  legitimately bind to a snapshot **other than the code HEAD**; it then reports
  `snapshot_bound_to_head=false` with `snapshot_source` = `env`/`latest` on
  `/health` (dual-mount), `/api/v1/ask` and `atlas ask` (CLI prints to stderr to
  keep stdout machine-readable).
  Numbers from such a turn must **not** be quoted next to that snapshot's evaluation
  numbers, nor written into a report named after the code HEAD. `make eval` is
  unaffected: it still binds strictly to HEAD and re-verifies the data fingerprint.
- Snapshot-binding failure → 503 on `/api/v1/ask` (the message carries the original
  cause).
- Boundaries (KL #28): with `ATLAS_CHECKPOINT_DB` set, sessions / turns /
  identity fingerprints persist in the SQLite checkpoint (ADR-0020); still
  in-process are the rate-limit buckets, the audit JSONL append and the SQLite
  single-writer — uvicorn must run `workers=1`; the audit JSONL is a local
  file, non-tamper-proof (export before production); no IdP yet — tokens are
  locally signed HS256 (0011 decision 4 stays open). Verified by `make api-verify`
  A1-A9 (`eval/reports/api-acceptance-95cba68.json`: full-chain EX / ambiguity /
  auth / dual-mounted health / role-difference / session identity-conflict 422 /
  retail dual-tier incl. domain-scoped policy name + cross-domain rejection
  before Guard / governance 8-collection sweep A8 / `/plan/execute` real chain A9).

```bash
make serve    # uvicorn 127.0.0.1:8000 (single process)
curl -H "Authorization: Bearer $(make token)" \
  http://127.0.0.1:8000/api/v1/plan -d '{"question":"What were total sales in 1999?","model":"retail"}'

# Multi-step analysis (ADR-0026): absolute dual periods + explicit dimension
curl -H "Authorization: Bearer $(make token)" \
  http://127.0.0.1:8000/api/v1/analyze \
  -d '{"question":"分析 2013Q4 相对 2013Q3 的佣金收入按分支的变化贡献"}'
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
15. Row-level security boundaries: Polaris is object-level only (no row-level — measured); geographic/category roles measured across both domains via rls-verify (finance diff-set 4 — broker tier honoured since ADR-0021, measured brokerid predicate; retail diff-set 2 — region_manager TN equals hq results because SF0.1 has a single state, stated honestly; the category_analyst restriction carries the difference).
16. Derived-metric numeric backing closed (gold-156~162 anchored, snapshot 30b8344).
17. Negative average cash balance is a data property, not a bug (户均现金余额).
18. Retrieval regression after corpus growth 15→20 metrics — attribution and follow-up documented.
19. RAG+LLM measured equal accuracy with real token cost; LoRA rows blocked (no GPU) — honestly marked, not estimated.
20. No official text2sql benchmark yet (BIRD finance comparison blocked until a text2sql generator is available).
21. `make ask` and dashboard behavior boundaries (evaluation numbers do not appear in Grafana).
22. `make train` blocked on this machine (no CUDA); training path not yet measured.
23/24. Observability: QPS panels have no series by design (deterministic chain, 0 tokens).
25. Session memory defaults to the in-process MemorySaver (lost on restart); with `ATLAS_CHECKPOINT_DB` set it persists in the SQLite checkpoint (ADR-0020).
26. dbt MetricFlow export is three-state, lossy by design: agg 14 / ratio 3 / unmapped 3 (SUM(a*b)) with reasons registered per item.
27. FIBO L2 mapping covers 19/20 metrics; `total_trade_tax` gap registered as a to-do (needs a broader concept-closure).
28. HTTP API v1 boundaries (see §6 above).
29. Unsupported filter shapes (free-form two-metric comparison etc.) → clarification, never a guess.
30. Dimension value domains are snapshot-state, not real-time (ADR-0016, batch B4).
31. Gold-set counting corrected; the paraphrase set has **no structural gate**; plus a repo-wide inventory of stale counts (measured 2026-09-14): 120 JSON = 106 gold + 13 `pp-*` + `schema.json`; all 13 `pp-*` files fail the gold schema, so their exclusion from `validate_gold.py`'s glob is structurally required, not a defect. The real debt is on the paraphrase side (`eval/paraphrase_eval.py` uses `.get()`, so a misspelled key silently becomes `None` and is misattributed to the Planner; `--domain` is written into the report but never selects a model). The `base` field of the 13 `pp-*` files is only a grouping label, never resolved to a sample file, and 4 of them name a non-existent `gold-comm` — so `by_base` carries a key that traces back to nothing. Stale gold-set counts elsewhere are inventoried in two groups: **A = present-tense claims to fix** (`AGENTS.md:63`/`:271` — **fixed 2026-09-14 under user authorization**: both now defer to `make lint` instead of hardcoding a count, so future growth cannot make them stale again; still requires its own `contract` commit per AGENTS.md §14; `docs/atlas_query_test_cases.md:226`; three outreach drafts; live comments in `airflow/yaml_jobs/04_semantic_publish_eval.yaml:23-24`; `eval/spider/README.md:12`; the **status line** of `infra/adr/0010-eval-methodology.md:4`) and **B = dated records that must not be rewritten** (rewriting them would forge history, N1). Full text: [README.md §10 #31](README.md).
32. Frontend adds a second toolchain without lifting any backend limit (ADR-0018, batch P0b, 2026-09-16; backfilled into this digest in the P1 batch): `workers=1` still holds (rate-limit buckets, audit writes and the SQLite single writer remain in-process); `uv sync` is no longer enough — node lives in nvm and is invisible to non-interactive shells, so all scripted calls must go through `make ui-*` (which fails loudly, never silently skips); a fresh clone has no `frontend/dist` (locally gitignored) — the API still starts, but the browser has no UI until `make ui-build`. Full text: [README.md §10 #32](README.md).
33. Frontend P1 workbench — four honesty boundaries (ADR-0018 P1 batch, 2026-09-16): (a) truncation is only ever stated as **possible** (`row_count == limit` shows "may have been truncated by the Plan limit"; the definitive flag is not in the 0022 contract), while the 500-row table render cap is a stated frontend constant — the full-data exits are `atlas query --format json` (same read-only gateway) or a narrower `limit`; (b) **zero telemetry** — no reporting endpoint, no SDK, errors rendered verbatim; (c) P1 ships panel 1 only (**time-point note, corrected from P2, 2026-09-16**: at P1's delivery point every deep path rendered the same workbench; P2 has since landed the `/ask`, `/sessions` and `/governance/{6 sub-pages}` routes, while reports/snapshots and charts remain P3 — the SPA 200-HTML fallback is the 0018 criterion-7 capability, separate from routing); (d) the Bearer token is memory-only and lost on refresh (`make token ROLE=…` is the only issuance path). Full text: [README.md §10 #33](README.md).
34. Frontend P2 governance console — four honesty boundaries (ADR-0018 P2 batch, 2026-09-16): (a) **the session timeline can only list sessions of the current run** (no data source for cross-restart, cross-tab history): the 0022 contract has no `/governance/sessions`-style endpoint, and the 0020 SQLite checkpoint is the agent's internal state store, not a queryable session directory — listing history would require a new read-only endpoint plus a ruling on its access scope (sessions contain raw questions, a sensitive surface), **not ruled on**; (b) **all governance endpoints require Bearer** (measured 2026-09-16: no-token request to `/api/v1/governance/policies` → 401) — the role matrix is unreachable without a token, so first activation *and* every post-refresh state require pasting a `make token` output first; (c) **the dev signing channel exists only under `make ui-dev`**: `POST /__dev/sign` is a vite dev-only middleware (`spawn uv run --env-file .env` calling `serving.auth.sign_token`, byte-for-byte the same as `make token`; the vite process never reads the secret), while `make serve-dev` and containers have no such endpoint and the panel degrades to "copy the make command + paste" — no second signing path is fabricated; (d) **the 6 governance sub-pages fire 6 concurrent requests on mount** (models / metrics / dimensions / synonyms / values / policies; tab switches do not refetch), `reports` / `snapshots` and the ReportDrawer belong to P3, and this batch of 6 is the premise of the 0022 decision-⑥ 240/min governance bucket — it must **not** be merged or lazily loaded to cut the request count. (**P3 time-point correction, 2026-09-16**: the `reports` / `snapshots` sub-pages and the ReportDrawer have since landed with P3; the governance plane's final form of 8 concurrent requests is now factual; the rest of this entry stands unchanged.) Full text: [README.md §10 #34](README.md).
35. Chart wiring has landed (P3, ADR-0025, 2026-09-16) with three remaining capability boundaries: (a) **the time-axis column is authoritative only when declared by the compiler** — for `yoy`/`pop`/`cumulative` the x-axis rides on `Compiler.emitted_time_column` (into `render_chart`'s `time_columns` argument; the frontend never infers); the non-compiler path (`--llm` candidate chain) falls back to `_TIME_COLUMN_NAMES` and, on a hit, the spec `note` marks the provenance level (same source as the P3 append to KL #21); (b) **`yoy`/`pop` render the current period only — no multi-series comparison** (comparison values live in the data table and `note`; adding series would overturn ADR-0025 decision ⑥, a breaking spec change); (c) **chart data is always ≤ 200 points** (`MAX_CATEGORIES`; oversize degrades to a table with a note — not new in this batch). Division vs #21: #21 records rendering semantics (spec-level determinism, no pixels); this entry records wiring status and capability boundaries (ADR-0025 ruled 2026-09-14; wiring landed with P3, 2026-09-16) — the two must not be read merged. Full text: [README.md §10 #35](README.md).
36. Multi-step analysis rejects non-additive combinations outright — no approximation (ADR-0026, 2026-09-16): a metric/dimension combo without registered additive eligibility goes `analysis.status=unavailable` (`reason_code=missing_eligibility`) before any SQL runs, driven by per-snapshot eligibility evidence (`data/snapshots/<sha>.analysis.json`); the accepted scenario is attribution-001 (commission revenue by branch, 2013Q4 vs 2013Q3). Full text: [README.md §10 #36](README.md).
37. Synthesis truncation never yields possibly-incomplete numbers (ADR-0026 decision ⑤): when Budget row/group caps trigger `possible_truncation`, synthesis goes `unavailable` (totals=null, items=[], fixed wording) — refusing on unavailability is design behavior, not a defect. Full text: [README.md §10 #37](README.md).
38. Multi-step analysis is validated on fixed snapshots only (mutable-data boundary): analysis evaluation and eligibility evidence are bound to a snapshot sha (measured anchor this batch: `7c966e9`); data changes require re-anchoring the sha and rebuilding the eligibility evidence (`*.analysis.json`), and numbers across snapshots are not comparable (same discipline as #30). Full text: [README.md §10 #38](README.md).
39. No task-level hard timeout: Budget caps rows/tables/groups only — there is no task-level deadline or cancellation; `elapsed_ms`/`latency_ms` faithfully record executed sub-SQL time and are not a latency promise. Full text: [README.md §10 #39](README.md).
40. No dedicated analysis UI: the capability ships via the HTTP API (`POST /api/v1/analyze`); the frontend has no analysis surface (not to be confused with the P2/P3 governance panels, which read Git files and evaluation artifacts only). Full text: [README.md §10 #40](README.md).

## 9. Where to go next

- [EVAL_REPORT.md](EVAL_REPORT.md) — machine-generated evaluation summary (bound to current HEAD).
- [eval/gold/README.md](eval/gold/README.md) — gold-set structure, language tags, batch history, ruling-overturn audit note.
- [infra/adr/](infra/adr/) — 26 numbered ADRs, each with overturn conditions (`ls infra/adr/0*.md | wc -l`; 25 accepted as of 2026-09-14, when 0024/0025 were confirmed by the user; 0026 multi-step task planning remains **proposed** — its implementation is receipted inside the ADR, pending the user's decision confirmation). Implementation status (2026-09-16): 0017/0019/0020 (P-1), 0021 (P-2sec), 0022 (P-2api), 0023 (P0a), 0018's P0b + P1 + P2 + P3 batches, 0025's chart wiring, and 0026's fixed four-step contribution analysis (single template + initial combo, delivered via `POST /api/v1/analyze` — dynamic drill-down, business causality, anomaly detection and LLM narrative remain undelivered) are implemented; 0024 (Cube exporter, independent line) remains the only pending line. The mandated batch order is in closing note 3 of `infra/adr/0018-frontend-console.md`, and the per-batch work cards plus receipts are in `docs/design/dev-plan-0017-0025.md`.
- [docs/design/](docs/design/) — implementation-level design pages (ADR-0015 lexicon extraction; frontend console plan; batch-execution dev plan for ADR-0017~0025).
- [docs/GLOSSARY.md](docs/GLOSSARY.md), [docs/release-notes-v0.1.md](docs/release-notes-v0.1.md).
- Chinese canonical README: [README.md](README.md).

## 10. License

- **Code**: `Apache-2.0` (full text in [`LICENSE`](LICENSE); copyright and third-party notices in [`NOTICE`](NOTICE)).
- **Data & ontologies** (none of these are redistributed by Atlas; obtain each yourself under its original terms):
  - TPC-DI source data: TPC benchmark terms; generated locally through PDGF (BANKMARK EULA) — see `data/raw/gen_tpcdi.sh`.
  - TPC-DS kit (SF0.1): TPC EULA v2.2; clone instructions in `scripts/setup_tpcds.sh`.
  - FIBO (FND+FBC+BE): MIT License (Copyright 2020 EDM Council); FIBO is a trademark of EDM Council; clone instructions in `data/fibo/README.md`.
  - OMG Commons / LCC: RDF content is downloaded by the user (`data/fibo/vendor/` is not committed); its license terms are **not retained in this repository** — confirm with OMG before use. Only IRI identifier strings are committed.
  - BIRD finance: public academic benchmark, historical reference only (ADR-0014).
- **Dependencies**: declared in `pyproject.toml` / `uv.lock`; the GPLv2 / LGPL-3.0 database drivers were removed in P0a (ADR-0023). Machine-generated inventory: `make license-check REPORT=1` → [`exports/dependency-licenses.json`](exports/dependency-licenses.json) (GNU make rejects the `--report` long option, hence the variable form; `python -m infra.license_check --report` works as-is at script level).
- **Third-party tools & assets**:
  - `scripts/tpcds_kit_sf01.patch`: contains 48 lines of TPC-DS kit source (16 removed + 32 context) under TPC EULA v2.2; **not** Apache-2.0 (declaration block in the file header; `NOTICE` §2).
  - Remotion (`docs/outreach-video/`): source-available, two-tier — Free License for individuals / for-profit organizations with up to 3 employees / non-profits; otherwise a Company License is required. `node_modules` and rendered videos are not committed (`NOTICE` §3).
  - Committed media (`docs/contact-wechat.png`, `docs/outreach-wechat-assets/*`, etc.) is self-produced, no third-party assets.

**Version boundary**: no `LICENSE` file exists in any historical tag (v0.1.0 / v0.1.1 / v0.1.3); the MIT text entered the trunk via `b547489` (merged in `14f210a`) and was replaced by the Apache-2.0 full text in the P0a batch (2026-09-16). Git history is not rewritten — use it as the authoritative reference.

**TPC performance-result constraint** (TPC-DS kit EULA 4.c): timing/row-count figures in this README are Atlas' own pipeline measurements, not TPC tool benchmark results; if dsdgen/dsqgen performance figures are ever published, one of the three disclosures required by 4.c must be attached.
