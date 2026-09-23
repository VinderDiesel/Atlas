"""维度值域画像生成（ADR-0016 §①，`make profile-values`）

对语义模型**注册维度**（planner 可寻址的 dim_* 非时间字段，即
`SemanticModel.dimension_synonyms` 的键）的**编译器绑定列**，从当前锁定快照执行
`SELECT DISTINCT`，生成 `semantic/values/<model>.<field>.json`。

纪律（为什么必须这样跑）
----------------------
1. **只读，不写库**：每条 SQL 先过 `sql_guard.enforce`（AGENTS.md N3：执行前必过
   Guard），预算 = 锁定快照内的表白名单（与 `make eval` 同一个 `build_budget`，
   不另开口径）。生成器能碰的表和评测能碰的表严格一致。
2. **前后快照复核**：采集前后按已锁 meta 比对真实指纹；默认使用代码身份，
   也可显式指定快照。漂移或 meta 被改即拒绝写入，避免把漂移固化成"权威口径"。
3. **人工别名是唯一人工字段**：`aliases` 由人工追加，重新生成时原样保留，并校验
   别名目标仍在新值域内（悬空别名 → 响亮报错，不静默放行）。值本体一律机器生成。
4. **大基数跳过**：distinct 值数 > `--max-cardinality`（默认 200）的列记
   `status: skipped` + 原因，不注册 values——planner 对 skipped 列不做值校验
   （安全默认，见 `agent/value_domain.py`）。跳过也是信息：文件里留着实测基数。
5. **绑定列而非声明列**：值域按 `SemanticModel.find_field()` 的首匹配结果生成。
   同名字段跨数据集时（实测金融 `Status` 同时存在于 fact_trades 与 dim_account），
   绑定的是先出现的那个数据集——该事实如实记进 `bound_dataset` / `note`，
   本批不改编译器（ADR-0016 §④）。

用法
----
    make profile-values                 # 双域全部注册维度
    .venv/bin/python -m data.value_profile --domain retail --dry-run
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from agent.compiler import SemanticModel  # noqa: E402
from agent.security.sql_guard import Budget, enforce  # noqa: E402
from agent.value_domain import VALUES_DIR  # noqa: E402
from data.identity import SNAPSHOT_DIR, git_short_sha  # noqa: E402
from eval.runner import DOMAIN_MODELS, build_budget, execute_sql  # noqa: E402

TZ = timezone(timedelta(hours=8))  # 契约要求：时间戳显式 +08:00
DEFAULT_MAX_CARDINALITY = 200

# 标识符白名单：表/列名只接受 [A-Za-z_][A-Za-z0-9_]*。语义模型是可信配置，但
# 值域 SQL 要过 Guard 执行，任何非预期字符（空格/引号/点号以外的结构）都说明
# 上游声明有问题——在此处失败比在 Doris 侧失败可解释得多。
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

Executor = Callable[[str], list[tuple[Any, ...]]]


def _check_ident(value: str, what: str) -> str:
    if not _IDENT.match(value):
        raise ValueError(f"{what} {value!r} 不是可安全拼接的标识符（[A-Za-z_][A-Za-z0-9_]*）")
    return value


def _as_text(value: Any) -> str:
    """DISTINCT 结果 → 值域存储用的字符串表示（与 resolve 的 str(value) 同口径）。"""
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value)


def _run(sql: str, budget: Budget, executor: Executor) -> list[tuple[Any, ...]]:
    """Guard 校验后执行。返回行列表（不返回列名，调用方按位置取值）。"""
    safe_sql, _cost = enforce(sql, budget=budget)
    return list(executor(safe_sql))


def measure_column(
    source_table: str,
    column: str,
    budget: Budget,
    executor: Executor,
    max_cardinality: int,
) -> tuple[int, int, int, list[tuple[str, int]]]:
    """测量一列：(distinct 数, 总行数, NULL 行数, 非空值按 (频次降序, 值升序))。

    两条查询：先量基数决定是否注册，基数达标才取值+频次（大列不做无谓物化）。
    """
    table = ".".join(_check_ident(part, "表名片段") for part in source_table.split("."))
    col = _check_ident(column, "列名")
    counts = _run(
        f"SELECT COUNT(DISTINCT {col}), COUNT(*), COUNT({col}) FROM {table}",
        budget,
        executor,
    )
    distinct_count, row_count, non_null = (int(counts[0][0]), int(counts[0][1]), int(counts[0][2]))
    pairs: list[tuple[str, int]] = []
    if 0 < distinct_count <= max_cardinality:
        rows = _run(
            f"SELECT {col}, COUNT(*) FROM {table} WHERE {col} IS NOT NULL "
            f"GROUP BY {col} LIMIT {max_cardinality}",
            budget,
            executor,
        )
        pairs = sorted(
            ((_as_text(value), int(count)) for value, count in rows), key=lambda p: (-p[1], p[0])
        )
        if len(pairs) != distinct_count:
            # 频次查询被 LIMIT 截断 = 阈值形同虚设（阈值与 distinct 判定必须同源）
            raise RuntimeError(
                f"{source_table}.{column}：distinct={distinct_count} 但频次查询返回 {len(pairs)} 行"
            )
    return distinct_count, row_count, row_count - non_null, pairs


def _load_existing_aliases(path: Path, values: list[tuple[str, int]]) -> dict[str, str]:
    """保留人工别名，并校验别名目标仍在新值域内（悬空即报错）。"""
    if path.is_symlink():
        raise RuntimeError(f"值域文件不得是符号链接：{path}")
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8")).get("aliases") or {}
    aliases = {str(key): str(target) for key, target in raw.items()}
    known = {value for value, _ in values}
    dangling = {alias: target for alias, target in aliases.items() if target not in known}
    if dangling:
        raise RuntimeError(
            f"{path.name} 的人工别名指向已不存在的取值（快照已变，请复核后手工修正）：{dangling}"
        )
    return aliases


def build_profile(
    model: SemanticModel,
    field: str,
    sha: str,
    budget: Budget,
    executor: Executor,
    max_cardinality: int = DEFAULT_MAX_CARDINALITY,
    now: str | None = None,
    values_dir: Path = VALUES_DIR,
) -> dict[str, Any]:
    """单个「模型.维度字段」的值域 payload（不落盘，供测试与 dry-run 复用）。"""
    bound = model.find_field(field)
    if bound is None:  # dimension_synonyms 的键必然可查，出现即为模型加载缺陷
        raise RuntimeError(f"维度字段 {field} 在模型 {model.name} 中查不到绑定列")
    ds_name, fld = bound
    same_name = [ds.name for ds in model.datasets.values() if field in ds.fields]
    distinct_count, row_count, null_count, pairs = measure_column(
        model.datasets[ds_name].source, fld.physical, budget, executor, max_cardinality
    )
    registered = 0 < distinct_count <= max_cardinality
    note = (
        f"值域派生自锁定快照 {sha} 的 {model.datasets[ds_name].source}."
        f"{fld.physical}；DISTINCT 实测 {distinct_count} 个非空取值、"
        f"NULL 行数 {null_count}。"
    )
    if len(same_name) > 1:
        note += (
            f"同名字段出现在 {len(same_name)} 个数据集 {same_name}，编译器按 datasets "
            f"顺序绑定首个 {ds_name}（其余数据集的同名列不参与本值域）。"
        )
    if not registered:
        note += (
            f" distinct 值数超过阈值 {max_cardinality}，按 ADR-0016 §① 跳过注册："
            "planner 对该列不做值校验（值原样透传）。"
            if distinct_count > max_cardinality
            else " 该列在当前快照无非空取值，按 ADR-0016 §① 跳过注册。"
        )
    return {
        "model": model.name,
        "field": field,
        "status": "registered" if registered else "skipped",
        "snapshot_sha": sha,
        "generated_at": now or datetime.now(TZ).isoformat(timespec="seconds"),
        "bound_dataset": ds_name,
        "source_table": model.datasets[ds_name].source,
        "source_column": fld.physical,
        "distinct_count": distinct_count,
        "row_count": row_count,
        "null_count": null_count,
        "max_cardinality": max_cardinality,
        "skip_reason": None
        if registered
        else (
            f"distinct={distinct_count} 超过阈值 {max_cardinality}"
            if distinct_count > max_cardinality
            else "当前快照无该列的非空取值"
        ),
        "values": [{"value": value, "count": count} for value, count in pairs],
        "aliases": _load_existing_aliases(values_dir / f"{model.name}.{field}.json", pairs),
        "note": note,
    }


def generate(
    domains: list[str],
    max_cardinality: int,
    dry_run: bool,
    executor: Executor | None = None,
    values_dir: Path = VALUES_DIR,
    *,
    snapshot_sha: str | None = None,
    raw_dir: Path | None = None,
) -> list[dict[str, Any]]:
    """从已锁快照测量指定域的全部值域，返回 payload 列表。

    snapshot_sha 默认取代码身份；raw_dir 可指定共享原始数据目录。
    dry_run 仍执行前后指纹及别名校验，但不写文件。非法身份抛 ValueError，
    缺失快照、数据漂移或悬空别名抛 RuntimeError；SQL/IO 异常向上传播。
    所有列校验成功后才写入，不承诺多文件写入的文件系统事务性。
    """
    from data.snapshot import build_meta, fingerprint
    from eval.analysis_eval import verify_snapshot_fingerprint

    # 原为本模块私有 head_sha()（不认 ATLAS_GIT_SHA 的第 11 份副本，ADR-0019 决策 ②）；
    # 改用单一事实源后容器内注入身份可正常生成，不再因镜像无 .git 而抛错。
    sha = git_short_sha() if snapshot_sha is None else snapshot_sha
    if not re.fullmatch(r"[0-9a-f]{7,40}", sha):
        raise ValueError("snapshot_sha 必须是 7–40 位小写十六进制身份")
    meta_path = SNAPSHOT_DIR / f"{sha}.meta.json"
    if meta_path.is_symlink() or not meta_path.is_file():
        raise RuntimeError(f"快照必须是已锁定的普通 meta 文件：{meta_path}")
    locked_text = meta_path.read_text(encoding="utf-8")

    def verify_locked() -> None:
        ok, reason = verify_snapshot_fingerprint(
            sha,
            snapshots_dir=SNAPSHOT_DIR,
            measure=lambda: fingerprint(build_meta(raw_dir=raw_dir)),
        )
        if not ok:
            raise RuntimeError(f"快照复核未通过：{reason}")
        if meta_path.is_symlink() or meta_path.read_text(encoding="utf-8") != locked_text:
            raise RuntimeError("采集期间锁定快照 meta 被修改，拒绝生成值域")

    verify_locked()
    meta = json.loads(locked_text)
    budget = build_budget(meta)
    run: Executor = executor if executor is not None else (lambda sql: execute_sql(sql)[0])
    payloads: list[dict[str, Any]] = []
    for domain in domains:
        model = SemanticModel(DOMAIN_MODELS[domain])
        for field in model.dimension_synonyms:
            payload = build_profile(
                model,
                field,
                str(meta["sha"]),
                budget,
                run,
                max_cardinality,
                values_dir=values_dir,
            )
            payloads.append(payload)
    verify_locked()
    if not dry_run:
        values_dir.mkdir(parents=True, exist_ok=True)
        for payload in payloads:
            path = values_dir / f"{payload['model']}.{payload['field']}.json"
            path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
    return payloads


def main(argv: list[str] | None = None) -> int:
    """执行画像 CLI，返回退出码；参数错误或测量失败时拒绝写入。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--domain",
        choices=[*sorted(DOMAIN_MODELS), "all"],
        default="all",
        help="要画像的域（默认 all = 全部已注册域）",
    )
    parser.add_argument(
        "--max-cardinality",
        type=int,
        default=DEFAULT_MAX_CARDINALITY,
        help="超过该 distinct 值数的列跳过注册（默认 200）",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="只打印测量结果，不写 semantic/values/"
    )
    parser.add_argument("--snapshot-sha", help="指定已锁快照，默认使用当前代码身份")
    parser.add_argument("--raw-dir", type=Path, help="TPC-DI 原始数据目录，默认使用仓库目录")
    args = parser.parse_args(argv)
    if args.max_cardinality < 1:
        parser.error("--max-cardinality 必须 ≥ 1")
    domains = sorted(DOMAIN_MODELS) if args.domain == "all" else [args.domain]

    payloads = generate(
        domains,
        args.max_cardinality,
        args.dry_run,
        snapshot_sha=args.snapshot_sha,
        raw_dir=args.raw_dir,
    )
    if not payloads:  # 域内无注册维度（模型退化）——不是成功，是配置异常
        print(
            "[error] 未生成任何值域：域内没有可寻址维度（dimension_synonyms 为空）", file=sys.stderr
        )
        return 1
    registered = [p for p in payloads if p["status"] == "registered"]
    print(
        f"[profile] 快照 {payloads[0]['snapshot_sha']}｜阈值 {args.max_cardinality}｜"
        f"维度列 {len(payloads)}：注册 {len(registered)}，跳过 {len(payloads) - len(registered)}"
    )
    for p in sorted(payloads, key=lambda x: (x["model"], x["field"])):
        preview = ", ".join(item["value"] for item in p["values"][:6])
        print(
            f"  [{p['status']:>10}] {p['model']}.{p['field']} "
            f"({p['source_table']}.{p['source_column']}) "
            f"distinct={p['distinct_count']} nulls={p['null_count']}"
            + (f" values=[{preview}{' …' if len(p['values']) > 6 else ''}]" if p["values"] else "")
            + ("（dry-run，未写文件）" if args.dry_run else "")
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
