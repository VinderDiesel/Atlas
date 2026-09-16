"""治理面只读端点（ADR-0022 决策 ④⑤⑦）：8 个集合 + 2 个钻取，一律只读文件。

口径（诚实声明）
--------------------
- **只读 Git 文件 + 报告/快照产物目录，绝不触发 DB 与 Agent 构造**（决策 ④）：
  即使 `/api/v1/ask` 因 `{HEAD}.meta.json` 缺失返回 503，治理面板照常可用——
  那正是排查该故障时最需要看的画面（P-2api 判据 5 断言此隔离）。
- 复用既有解析器，不新写 YAML 解析：治理扩展走 `semantic.governance_validate.
  iter_payloads`（lint 的事实源解析器）；指标/维度走 `agent.compiler.SemanticModel`；
  角色目录走 `serving.auth.ROLE_DIRECTORY`。零新增事实源。
- 缓存只加在实测贵处（决策 ④）：ossie YAML safe_load（实测 23.4 ms/次）与
  iter_payloads 产物（23.7 ms/次）按 `(st_mtime_ns, st_size)` 单槽缓存，mtime
  变化即失效、语义层改动立刻可见（`workers=1` 故无跨进程失效问题）；JSON 侧
  （values ~1 ms / reports ~5 ms / snapshots）直读不缓存。报告索引只解析主报告
  body，非主报告仅取文件名 + size + mtime + 模式标签。
- 限流走**独立桶**（决策 ⑥：业务桶不受影响），每请求一行审计
  `kind="governance_read"`（session_id / row_count / latency_ms 恒 null；
  endpoint 含 query 的 model，其余参数不入审计）。
- 诚实性标志位随端点携带（决策 ⑦，六个陷阱逐条对应 ADR-0018 代价 ⑥）：
  `status: skipped` + `skip_reason`、`structured` + `pattern`、主报告 `dry` 透传、
  `empty_placeholder` + `authority_note`、`registered`、`is_latest_by_created_at`。
- 统一信封 `{kind, count, sources, items}`：`sources` 让每个面板显示数据来自哪个
  Git 文件（治理面的诚实性要求可溯源到唯一事实源，决策 ⑤）。
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, Depends, HTTPException, Request

from agent.compiler import SemanticModel
from data.identity import SNAPSHOT_DIR, git_short_sha_or_none
from semantic.governance_validate import iter_payloads
from serving.audit import AuditLog
from serving.auth import ROLE_DIRECTORY, BearerClaims
from serving.ratelimit import RateLimiter

GOV_PREFIX = "/api/v1/governance"
REPO_ROOT = Path(__file__).resolve().parent.parent
SYNONYMS_DIR = REPO_ROOT / "semantic" / "synonyms"
VALUES_DIR = REPO_ROOT / "semantic" / "values"
POLICY_PATH = REPO_ROOT / "semantic" / "policies" / "row_policy.yml"
REPORTS_DIR = REPO_ROOT / "eval" / "reports"
# 快照目录**不在此构造**（ADR-0019 判据 5(b)：生产代码只允许 data/identity.py
# 一处目录副本，tests/test_identity.py 全仓检索强制此约束）——单源导入。
SNAPSHOTS_DIR = SNAPSHOT_DIR

TZ = timezone(timedelta(hours=8))  # 时间戳显式 +08:00（与 audit / 快照同口径）

_LOCALES = ("zh_cn", "en_us")
# 权威源说明（决策 ⑦ 陷阱 4）：如实转述同义词表文件头注释的口径——zh_cn.yml
# 实测为空占位（两节皆 {}），其文件头原文：「当前为空占位，中文注记仍在语义模型里」
_AUTHORITY_NOTES = {
    "zh_cn": (
        "中文同义词表当前为空占位（empty_placeholder=true 表示「本表为空」而非"
        "「中文无同义词」）：中文注记的权威源在语义模型 semantic/ossie/*.ossie.yaml "
        "的 ai_context.synonyms——ADR-0015 避免双权威源，本表是其 locale 形态层补充。"
    ),
    "en_us": (
        "英文措辞为解析器 locale 形态层数据（从 planner 字面量外置，非口径定义）："
        "指标/维度口径的权威源仍是 semantic/ossie/*.ossie.yaml（ADR-0015）。"
    ),
}

# 主报告文件名口径（纯 sha；决策 ⑤ 端点 7）：非此形态 = 非主报告（不解析 body）
_MAIN_REPORT_RE = re.compile(r"^[0-9a-f]{7,}$")
_SHA_SEG_RE = re.compile(r"[0-9a-f]{7,}")

# ---------------------------------------------------------------------------
# 解析缓存（决策 ④：只缓存贵解析——ossie YAML safe_load 与 iter_payloads 产物；
# JSON 侧直读不缓存。单槽 key = 文件名，命中条件含 (st_mtime_ns, st_size)——文件
# 一改立刻失效，无需 TTL）
# ---------------------------------------------------------------------------

_YAML_CACHE: dict[str, tuple[tuple[int, int], dict[str, Any]]] = {}
_PAYLOAD_CACHE: dict[str, tuple[tuple[int, int], list[tuple[str, dict[str, Any], str | None]]]] = {}


def _stat_key(path: Path) -> tuple[int, int]:
    st = path.stat()
    return st.st_mtime_ns, st.st_size


def _yaml_doc(path: Path) -> dict[str, Any]:
    """ossie YAML → dict（safe_load 结果按 mtime+size 缓存）。"""
    key = _stat_key(path)
    hit = _YAML_CACHE.get(path.name)
    if hit is not None and hit[0] == key:
        return hit[1]
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    _YAML_CACHE[path.name] = (key, doc)
    return doc


def _payloads(path: Path) -> list[tuple[str, dict[str, Any], str | None]]:
    """iter_payloads 产物缓存（与 YAML 缓存独立：iter_payloads 内部自带一次
    safe_load，两条管线不共用中间结果——宁可多一次 23.7 ms 也不让缓存语义含糊）。"""
    key = _stat_key(path)
    hit = _PAYLOAD_CACHE.get(path.name)
    if hit is not None and hit[0] == key:
        return hit[1]
    payloads = list(iter_payloads(path))
    _PAYLOAD_CACHE[path.name] = (key, payloads)
    return payloads


def _rel(path: Path) -> str:
    """仓库内相对路径（sources 字段的展示口径）。"""
    return path.relative_to(REPO_ROOT).as_posix()


# ---------------------------------------------------------------------------
# 各端点数据构建（纯函数，可独立测试；不触碰网络与数据库）
# ---------------------------------------------------------------------------


def _model_payload(path: Path) -> dict[str, Any]:
    """文件内模型级 ATLAS payload（无则空 dict）。"""
    return next((data for _, data, name in _payloads(path) if name is None), {})


def _models_index(model_paths: dict[str, Path]) -> list[dict[str, Any]]:
    """端点 1：语义模型清单（计数 + 时间维声明 + 治理/血缘/新鲜度块）。"""
    items: list[dict[str, Any]] = []
    for domain, path in model_paths.items():
        semantic = SemanticModel(path, doc=_yaml_doc(path))
        payload = _model_payload(path)
        gov = payload.get("governance") or {}
        lineage = payload.get("lineage") or {}
        freshness = payload.get("freshness") or {}
        items.append(
            {
                "domain": domain,
                "model_name": semantic.name,
                "source_file": _rel(path),
                "datasets": len(semantic.datasets),
                "metrics": len(semantic.metrics),
                "relationships": len(semantic.relationships),
                "fields": sum(len(ds.fields) for ds in semantic.datasets.values()),
                "time_dimension": semantic.time_dimension,
                "policy": {"default_row_policy": semantic.default_row_policy},
                "governance": {
                    key: gov.get(key)
                    for key in ("owner", "version", "status", "review_cycle_days")
                },
                "lineage": {"source_tables": list(lineage.get("source_tables") or [])},
                "freshness": {
                    "schedule": freshness.get("schedule"),
                    "sla_minutes": freshness.get("sla_minutes"),
                },
            }
        )
    return items


def _metrics_index(domain: str, model_paths: dict[str, Path]) -> list[dict[str, Any]]:
    """端点 2：指标清单（表达式 + 治理扩展 + FIBO 对齐）。

    FIBO 映射的实测位置（2026-09-16）：指标级自定义扩展**不含** fibo_alignment，
    条目在**模型级** mappings 的 `metric:<指标名>` 键下（finance 实测 19/20 条，
    缺者如实返回空 mappings——不补造）。
    """
    path = model_paths[domain]
    semantic = SemanticModel(path, doc=_yaml_doc(path))
    payloads = _payloads(path)
    per_metric = {name: data for _, data, name in payloads if name is not None}
    model_payload = next((data for _, data, name in payloads if name is None), {})
    mappings = (model_payload.get("fibo_alignment") or {}).get("mappings") or {}
    items: list[dict[str, Any]] = []
    for name in semantic.metrics:
        data = per_metric.get(name) or {}
        gov = data.get("governance") or {}
        lineage = data.get("lineage") or {}
        quality = data.get("quality") or {}
        items.append(
            {
                "name": name,
                "expression": semantic.metrics[name],
                "description": semantic.metric_descriptions.get(name),
                "synonyms": list(semantic.metric_synonyms.get(name, ())),
                "owner": semantic.metric_owners.get(name) or None,
                "version": gov.get("version"),
                "status": gov.get("status"),
                "supersedes": gov.get("supersedes"),
                "lineage": {"source_columns": list(lineage.get("source_columns") or [])},
                "quality": {
                    "gold_test_cases": list(quality.get("gold_test_cases") or []),
                    "expected_value_snapshot_sha": quality.get("expected_value_snapshot_sha"),
                },
                "fibo_alignment": {
                    "mappings": {
                        key: entry
                        for key, entry in mappings.items()
                        if key == f"metric:{name}"
                    }
                },
            }
        )
    return items


def _value_status_map(model_name: str) -> dict[str, str]:
    """值域注册表 status 索引（field → registered|skipped）；无文件由调用方落 none。"""
    out: dict[str, str] = {}
    for path in VALUES_DIR.glob(f"{model_name}.*.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        status = data.get("status")
        if isinstance(status, str):
            out[str(data.get("field"))] = status
    return out


def _dimensions_index(domain: str, model_paths: dict[str, Path]) -> list[dict[str, Any]]:
    """端点 3：维度清单（物理列 + 时间标记 + 同义词 + 值域注册状态）。"""
    path = model_paths[domain]
    semantic = SemanticModel(path, doc=_yaml_doc(path))
    value_status = _value_status_map(semantic.name)
    items: list[dict[str, Any]] = []
    for ds in semantic.datasets.values():
        for field in ds.fields.values():
            items.append(
                {
                    "dataset": ds.name,
                    "field": field.name,
                    "physical": field.physical,
                    "is_time": field.is_time,
                    "synonyms": list(field.synonyms),
                    "value_domain": value_status.get(field.name, "none"),
                }
            )
    return items


def _synonyms_payload(locale: str) -> dict[str, Any]:
    """端点 4：locale 词典（两节 + 形态词分节原样 + 空占位标志与权威源说明）。"""
    if locale not in _LOCALES:
        raise HTTPException(
            status_code=422, detail=f"未知 locale：{locale!r}（可选 {'|'.join(_LOCALES)}）"
        )
    syn_doc = yaml.safe_load((SYNONYMS_DIR / f"{locale}.yml").read_text(encoding="utf-8")) or {}
    pat_doc = (
        yaml.safe_load((SYNONYMS_DIR / f"patterns_{locale}.yml").read_text(encoding="utf-8")) or {}
    )
    metric_syn = syn_doc.get("metric_synonyms") or {}
    dim_syn = syn_doc.get("dimension_synonyms") or {}
    return {
        "locale": locale,
        "metric_synonyms": metric_syn,
        "dimension_synonyms": dim_syn,
        "patterns": pat_doc,  # 分节原样（YAML 原文结构，不编译为正则）
        "empty_placeholder": not metric_syn and not dim_syn,
        "authority_note": _AUTHORITY_NOTES[locale],
    }


def _values_index() -> list[dict[str, Any]]:
    """端点 5：值域注册表清单（JSON 直读；values_count 为 values 数组长度派生）。"""
    items: list[dict[str, Any]] = []
    for path in sorted(VALUES_DIR.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        items.append(
            {
                "model": data.get("model"),
                "field": data.get("field"),
                "status": data.get("status"),
                "values_count": len(data.get("values") or []),
                "skip_reason": data.get("skip_reason"),
                "snapshot_sha": data.get("snapshot_sha"),
                "generated_at": data.get("generated_at"),
                "bound_dataset": data.get("bound_dataset"),
                "source_table": data.get("source_table"),
                "source_column": data.get("source_column"),
            }
        )
    return items


def _value_detail(item: str) -> dict[str, Any]:
    """钻取 1：完整 values 数组 + 别名（JSON 原文直读）。

    `status: skipped` 的条目返回 `values: []` + `skip_reason` 原文——阈值事实
    如实透传（ADR-0016 §①），不补任何空表说明。
    """
    if not re.fullmatch(r"[A-Za-z0-9_]+(\.[A-Za-z0-9_]+)?", item):
        raise HTTPException(status_code=404, detail=f"值域条目不存在：{item!r}")
    target = VALUES_DIR / f"{item}.json"
    if not target.is_file():
        raise HTTPException(status_code=404, detail=f"值域条目不存在：{item!r}")
    return json.loads(target.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def _policies_index(model_paths: dict[str, Path]) -> list[dict[str, Any]]:
    """端点 6：策略 → 角色目录（condition 模板原文 + 注册状态 + 域声明）。

    `condition` 是**模板原文**（含 `{{ user.X }}` 占位符，不含渲染值）——ADR-0011
    决策 5「被拒路径不外泄细节」不受影响（返回的是模板，不是某次被拒的谓词值）。
    """
    declared: dict[str, list[str]] = {}
    for domain, path in model_paths.items():
        semantic = SemanticModel(path, doc=_yaml_doc(path))
        if semantic.default_row_policy:
            declared.setdefault(semantic.default_row_policy, []).append(domain)
    doc = yaml.safe_load(POLICY_PATH.read_text(encoding="utf-8")) or {}
    items: list[dict[str, Any]] = []
    for policy in doc.get("policies") or []:
        roles: list[dict[str, Any]] = []
        for role in policy.get("roles") or []:
            name = str(role.get("name"))
            spec = ROLE_DIRECTORY.get(name)
            roles.append(
                {
                    "name": name,
                    "condition": role.get("condition"),
                    "description": role.get("description"),
                    "required_claims": list(spec.required_claims) if spec else [],
                    "list_claims": sorted(spec.list_claims) if spec else [],
                    # 诚实性标志位（决策 ⑦ 陷阱 5）：策略里声明但未注册的角色
                    # 不可签发（sign_token 拒），面板必须能显示这一差异
                    "registered": spec is not None,
                }
            )
        items.append(
            {
                "name": policy.get("name"),
                "description": policy.get("description"),
                "default_deny": policy.get("default_deny"),
                "declared_by_models": declared.get(str(policy.get("name")), []),
                "roles": roles,
            }
        )
    return items


def _report_pattern(stem: str) -> str:
    """文件名 → 模式标签：sha 段替换为 <sha>（决策 ⑤ 端点 7 的 18 种模式口径）。"""
    return _SHA_SEG_RE.sub("<sha>", stem)


def _reports_index() -> list[dict[str, Any]]:
    """端点 7：报告索引（只解析主报告 body；非主报告仅文件元信息 + 模式标签）。"""
    items: list[dict[str, Any]] = []
    for path in REPORTS_DIR.glob("*.json"):
        st = path.stat()
        structured = bool(_MAIN_REPORT_RE.fullmatch(path.stem))
        entry: dict[str, Any] = {
            "name": path.stem,
            "pattern": _report_pattern(path.stem),
            "structured": structured,
            "size_bytes": st.st_size,
            "mtime": datetime.fromtimestamp(st.st_mtime, TZ).isoformat(timespec="seconds"),
        }
        if structured:
            # 主报告才解析 body——既快，又天然满足 ADR-0018 代价 ⑥
            # 「不得伪造统一表头」（非主报告根本没有可伪的表头）
            data = json.loads(path.read_text(encoding="utf-8"))
            entry.update(
                {
                    "sha": data.get("sha"),
                    "created_at": data.get("created_at"),
                    # dry 透传（决策 ⑦ 陷阱 3）：dry 报告的 ex:"n/a" 不得被读成 EX=0
                    "dry": data.get("dry"),
                    "domains": data.get("domains"),
                }
            )
        items.append(entry)
    # 最新在前（mtime 降序；git checkout 后 mtime 可能相同，name 作稳定 tie-breaker）
    items.sort(key=lambda e: (str(e["mtime"]), str(e["name"])), reverse=True)
    return items


def _report_detail(name: str) -> dict[str, Any]:
    """钻取 2：主报告 7 键结构化；非主报告 {name, pattern, structured, raw}（降级展示）。"""
    if not re.fullmatch(r"[A-Za-z0-9_.\-]+", name) or ".." in name:
        raise HTTPException(status_code=404, detail=f"报告不存在：{name!r}")
    target = REPORTS_DIR / f"{name}.json"
    if not target.is_file():
        raise HTTPException(status_code=404, detail=f"报告不存在：{name!r}")
    data = json.loads(target.read_text(encoding="utf-8"))
    if _MAIN_REPORT_RE.fullmatch(name):
        return data  # type: ignore[no-any-return]
    return {"name": name, "pattern": _report_pattern(name), "structured": False, "raw": data}


def _snapshots_index() -> list[dict[str, Any]]:
    """端点 8：快照清单（created_at 降序 + 最新标志；字典序不可靠——决策 ⑦ 陷阱 6）。"""
    head = git_short_sha_or_none()
    items: list[dict[str, Any]] = []
    for path in SNAPSHOTS_DIR.glob("*.meta.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        row_counts = data.get("row_counts") or {}
        tables = sum(len(rc) for rc in row_counts.values() if isinstance(rc, dict))
        total_rows = sum(
            int(count)
            for rc in row_counts.values()
            if isinstance(rc, dict)
            for count in rc.values()
        )
        sha = str(data.get("sha") or path.name.removesuffix(".meta.json"))
        items.append(
            {
                "sha": sha,
                "created_at": data.get("created_at"),
                "source": data.get("source"),
                "data_range": data.get("data_range"),
                "raw_size_bytes": data.get("raw_size_bytes"),
                "table_count": tables,
                "row_counts": {
                    "namespaces": {
                        ns: sum(int(c) for c in rc.values())
                        for ns, rc in row_counts.items()
                        if isinstance(rc, dict)
                    },
                    "total_tables": tables,
                    "total_rows": total_rows,
                },
                "bound_to_head": sha == head,
                "is_latest_by_created_at": False,  # 排序后标首条
            }
        )
    items.sort(key=lambda e: str(e["created_at"] or ""), reverse=True)
    if items:
        items[0]["is_latest_by_created_at"] = True
    return items


def _envelope(kind: str, items: list[dict[str, Any]], sources: list[str]) -> dict[str, Any]:
    """统一信封（决策 ⑤；契约测试锁定键集）：count 为条目数，sources 为 Git 文件。"""
    return {"kind": kind, "count": len(items), "sources": sources, "items": items}


def _audit_endpoint(request: Request) -> str:
    """治理审计行 endpoint：完整路径 + 仅 model 查询参数（决策 ⑥，其余参数不入）。"""
    model = request.query_params.get("model")
    return f"{request.url.path}?model={model}" if model else request.url.path


# ---------------------------------------------------------------------------
# Router 工厂
# ---------------------------------------------------------------------------


def build_governance_router(
    *,
    audit: AuditLog,
    rate_limiter: RateLimiter,
    model_paths: dict[str, Path],
    require_model: Callable[[str], str],
) -> APIRouter:
    """构造治理面 router（`/api/v1/governance` 前缀；ADR-0022 决策 ①⑤）。

    参数
    ----
    audit        : 审计面（与业务面同一实例——每请求一行 kind=governance_read，
                   沿用同一个 ATLAS_AUDIT_DISABLED 开关，决策 ⑥ 不新增第二开关）。
    rate_limiter : **治理桶**限流器（独立于业务桶；决策 ⑥）。
    model_paths  : 域 → 语义模型 YAML（业务面同源注入，单源不复制域名册）。
    require_model: 域白名单校验（业务面 `_model_name` 同源注入；未知值 422）。

    返回
    ----
    APIRouter：10 条路由（8 集合 + 2 钻取），装配进业务 app（决策 ①：
    同进程同源——ADR-0018 已裁定永久零 CORS）。
    """
    router = APIRouter(prefix=GOV_PREFIX, tags=["governance"])

    def _require_gov_rate_limit(request: Request, claims: BearerClaims) -> None:
        """治理桶限流依赖（与业务桶同形：per-token、429 + Retry-After、审计行）。"""
        key = str(claims.get("sub") or claims.get("role") or "anonymous")
        allowed, retry_after = rate_limiter.check(key)
        if not allowed:
            audit.record(
                endpoint=_audit_endpoint(request),
                claims=claims,
                kind="rate_limited",
                status=429,
                bucket="governance",
            )
            raise HTTPException(
                status_code=429,
                detail="请求过于频繁（治理面），请稍后再试",
                headers={"Retry-After": str(retry_after)},
            )

    def _gov_audit(request: Request, claims: dict[str, object]) -> None:
        """治理面每请求一行（决策 ⑥）：无会话无行数，三键恒 null。"""
        audit.record(
            endpoint=_audit_endpoint(request),
            claims=claims,
            kind="governance_read",
            bucket="governance",
        )

    @router.get("/models", summary="语义模型清单（治理面只读）")
    def governance_models(
        request: Request,
        _claims: BearerClaims,
        _rl: None = Depends(_require_gov_rate_limit),
    ) -> dict[str, Any]:
        items = _models_index(model_paths)
        _gov_audit(request, _claims)
        return _envelope("governance.models", items, [_rel(p) for p in model_paths.values()])

    @router.get("/metrics", summary="指标清单（含治理扩展与 FIBO 对齐；model 选域）")
    def governance_metrics(
        request: Request,
        _claims: BearerClaims,
        _rl: None = Depends(_require_gov_rate_limit),
        model: str = "finance",
    ) -> dict[str, Any]:
        domain = require_model(model)
        items = _metrics_index(domain, model_paths)
        _gov_audit(request, _claims)
        return _envelope("governance.metrics", items, [_rel(model_paths[domain])])

    @router.get("/dimensions", summary="维度清单（物理列 + 值域注册状态；model 选域）")
    def governance_dimensions(
        request: Request,
        _claims: BearerClaims,
        _rl: None = Depends(_require_gov_rate_limit),
        model: str = "finance",
    ) -> dict[str, Any]:
        domain = require_model(model)
        items = _dimensions_index(domain, model_paths)
        _gov_audit(request, _claims)
        return _envelope("governance.dimensions", items, [_rel(model_paths[domain])])

    @router.get("/synonyms", summary="locale 词典（含空占位标志与权威源说明）")
    def governance_synonyms(
        request: Request,
        _claims: BearerClaims,
        _rl: None = Depends(_require_gov_rate_limit),
        locale: str = "zh_cn",
    ) -> dict[str, Any]:
        payload = _synonyms_payload(locale)
        _gov_audit(request, _claims)
        return _envelope(
            "governance.synonyms",
            [payload],
            [
                _rel(SYNONYMS_DIR / f"{locale}.yml"),
                _rel(SYNONYMS_DIR / f"patterns_{locale}.yml"),
            ],
        )

    @router.get("/values", summary="值域注册表清单（含 skipped 与 skip_reason）")
    def governance_values(
        request: Request,
        _claims: BearerClaims,
        _rl: None = Depends(_require_gov_rate_limit),
    ) -> dict[str, Any]:
        items = _values_index()
        _gov_audit(request, _claims)
        return _envelope("governance.values", items, [f"{_rel(VALUES_DIR)}/"])

    @router.get("/policies", summary="行级策略与角色目录（condition 模板原文）")
    def governance_policies(
        request: Request,
        _claims: BearerClaims,
        _rl: None = Depends(_require_gov_rate_limit),
    ) -> dict[str, Any]:
        items = _policies_index(model_paths)
        _gov_audit(request, _claims)
        return _envelope(
            "governance.policies",
            items,
            [_rel(POLICY_PATH), _rel(Path(__file__).resolve().parent / "auth.py")],
        )

    @router.get("/reports", summary="评测报告索引（主报告结构化，非主报告模式标签）")
    def governance_reports(
        request: Request,
        _claims: BearerClaims,
        _rl: None = Depends(_require_gov_rate_limit),
    ) -> dict[str, Any]:
        items = _reports_index()
        _gov_audit(request, _claims)
        return _envelope("governance.reports", items, [f"{_rel(REPORTS_DIR)}/"])

    @router.get("/snapshots", summary="数据快照清单（created_at 降序 + 最新标志）")
    def governance_snapshots(
        request: Request,
        _claims: BearerClaims,
        _rl: None = Depends(_require_gov_rate_limit),
    ) -> dict[str, Any]:
        items = _snapshots_index()
        _gov_audit(request, _claims)
        return _envelope("governance.snapshots", items, [f"{_rel(SNAPSHOTS_DIR)}/"])

    @router.get("/values/{item}", summary="值域钻取（完整 values + 别名）")
    def governance_value_detail(
        item: str,
        request: Request,
        _claims: BearerClaims,
        _rl: None = Depends(_require_gov_rate_limit),
    ) -> dict[str, Any]:
        payload = _value_detail(item)
        _gov_audit(request, _claims)
        return payload

    @router.get("/reports/{name}", summary="报告钻取（主报告结构化 / 非主报告 raw 降级）")
    def governance_report_detail(
        name: str,
        request: Request,
        _claims: BearerClaims,
        _rl: None = Depends(_require_gov_rate_limit),
    ) -> dict[str, Any]:
        payload = _report_detail(name)
        _gov_audit(request, _claims)
        return payload

    return router
