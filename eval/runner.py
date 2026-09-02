#!/usr/bin/env python3
"""评测执行器：Plan Acc + EX（Atlas 主评测，README 第 5 节）。

链路（确定性优先，AGENTS.md 第 10 节优先级）：
    gold 样本 → Planner → Plan（Plan Acc：metric/dimensions/time 与标注一致）
              → 歧义样本必须返回 ClarificationRequest（反问，不猜）
              → Compiler → SQL → Guard（只读红线 + 表白名单=快照内表）
              → Doris 执行 → 结果 sha256（EX：与锚定 result_hash 一致）
              → eval/reports/<git sha>.json

诚实与可复现口径（与 data/snapshots/README.md、eval/gold/README.md 绑定）：
- 评测只认当前 HEAD：启动时以 `data/snapshot.py --check` 复核数据指纹，
  漂移（行数/snapshot id 变化）即拒绝出报告——数字必须绑定锁定快照。
- 金融段（gold-1xx）为主评测；零售段（gold-0xx）为历史对照，跳过并计数，
  不与金融段混报。
- result_hash = 编译 SQL（过 Guard 后）执行结果的 sha256。gold JSON 中
  占位符（<待执行后填写>）在首次执行时锚定回填并提示 commit；此后比对即 EX。
- 样本 SQL 必须确定性（单行聚合或带 ORDER BY）：多行无排序结果在并发
  执行下不稳定，hash 会抖动（gold 设计原则见 eval/gold/README.md）。

用法（从仓库根执行）：
    uv run python -m eval.runner           # 评测当前 HEAD（自动复核快照）
    uv run python -m eval.runner --dry     # 只跑 Plan Acc，不执行 SQL 不写报告
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import mysql.connector

from agent.compiler import Compiler, Plan, SemanticModel, TimeSpec
from agent.planner import ClarificationRequest, Planner
from agent.security.sql_guard import Budget, enforce

REPO_ROOT = Path(__file__).resolve().parent.parent
GOLD_DIR = REPO_ROOT / "eval" / "gold"
REPORT_DIR = REPO_ROOT / "eval" / "reports"
SNAPSHOT_DIR = REPO_ROOT / "data" / "snapshots"

TZ = timezone(timedelta(hours=8))  # 契约要求：时间戳显式 +08:00
# 占位符（AGENTS.md 9.3：未完成数字一律 <待填写>，评测锚定前不得编造）
PLACEHOLDER_HASH = "<待执行后填写>"
PLACEHOLDER_SHA = "<待锁定后填写>"
MAX_ROWS = 10_000  # Guard 强制行数上限（gold SQL 自带更小 LIMIT，不受影响）


def git_short_sha() -> str:
    """当前 HEAD 短 sha：报告文件名与快照绑定键。"""
    out = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip()


def verify_snapshot() -> None:
    """复核数据指纹与已锁快照一致（漂移即拒绝评测）。

    复用 data/snapshot.py --check（全表行数 + snapshot id 现场测量），
    不在此重复实现，避免两处口径漂移。退出码 0=一致，2=漂移/未锁。
    """
    proc = subprocess.run(
        [sys.executable, "-m", "data.snapshot", "--check"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        print(
            f"[error] 快照复核未通过（数据漂移或未锁定）：\n{proc.stderr.strip()}", file=sys.stderr
        )
        raise SystemExit(2)
    print(f"[snapshot] {proc.stdout.strip()}")


def load_gold_finance() -> list[dict[str, Any]]:
    """加载金融段样本（gold-1xx）；零售段（0xx）跳过并计数。"""
    samples: list[dict[str, Any]] = []
    for path in sorted(GOLD_DIR.glob("gold-*.json")):
        sample: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        if sample["id"].startswith("gold-1"):
            sample["_file"] = str(path)
            samples.append(sample)
    return samples


def time_key(time: TimeSpec | None) -> str | None:
    """TimeSpec → 与 gold expected_time 可比的规范字符串（year=2013 亦为 "2013"）。"""
    return None if time is None else str(time.value)


def plan_acc(plan: Plan, gold: dict[str, Any]) -> bool:
    """Plan Acc：metric/dimensions/time 三键与人工标注一致。"""
    expected_time = gold.get("expected_time")
    return (
        plan.metric == gold.get("expected_metric")
        and plan.dimensions == tuple(gold.get("expected_dimensions", []))
        and time_key(plan.time) == expected_time
    )


def result_hash(rows: list[tuple[Any, ...]]) -> str:
    """执行结果 → sha256（列序 = SELECT 序，行序 = 返回序，NULL 固定表示）。

    标量规约：Decimal/int/str 用 str（decimal 尾零稳定，实测 Doris SUM(decimal)
    返回 "344129059.3500" 恒定）；bytes 用 hex；float 用 repr（样本不使用，
    float 表示不稳定，若引入需另行规约）。
    """
    payload = "\n".join("\t".join("NULL" if v is None else _scalar(v) for v in row) for row in rows)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _scalar(value: Any) -> str:
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, float):
        return repr(value)
    return str(value)


def execute_sql(sql: str) -> tuple[list[tuple[Any, ...]], list[str]]:
    """在 Doris 执行只读 SQL，返回 (rows, columns)。

    连接参数走环境变量（AGENTS.md 第 13 节，本地开发默认 root 空密码，
    .env 可覆盖：DORIS_HOST/DORIS_PORT/DORIS_USER/DORIS_PASSWORD）。
    """
    conn = mysql.connector.connect(
        host=os.environ.get("DORIS_HOST", "127.0.0.1"),
        port=int(os.environ.get("DORIS_PORT", "9030")),
        user=os.environ.get("DORIS_USER", "root"),
        password=os.environ.get("DORIS_PASSWORD", ""),
        connection_timeout=15,
    )
    try:
        cursor = conn.cursor()
        try:
            cursor.execute(sql)
            columns = [desc[0] for desc in (cursor.description or [])]
            rows = [tuple(row) for row in cursor.fetchall()]
        finally:
            cursor.close()
    finally:
        conn.close()
    return rows, columns


def build_budget(snapshot_meta: dict[str, Any]) -> Budget:
    """Guard 预算：方言=doris，表白名单 = 锁定快照内的全部表。

    评测 SQL 只允许触碰已锁快照的表——未来新增表未入快照前不允许被评测，
    防止评测对象漂移（与 data/snapshots/README.md 规则一致）。
    """
    allowed = {
        f"atlas.{ns}.{table}"
        for ns, tables in snapshot_meta["row_counts"].items()
        for table in tables
    }
    return Budget(dialect="doris", max_rows=MAX_ROWS, allowed_tables=frozenset(allowed))


def anchor_hash(gold_path: str, sample: dict[str, Any], hash_value: str, sha: str) -> None:
    """锚定 result_hash 与 snapshot_sha 回填 gold JSON（占位符 → 实测值）。"""
    sample["result_hash"] = hash_value
    sample["snapshot_sha"] = sha
    path = Path(gold_path)
    payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    payload["result_hash"] = hash_value
    payload["snapshot_sha"] = sha
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def anchor_snapshot_sha(gold_path: str, sha: str) -> None:
    """歧义样本无执行结果：仅回填 snapshot_sha（result_hash 保持 null）。"""
    path = Path(gold_path)
    payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    payload["snapshot_sha"] = sha
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def evaluate(
    planner: Planner,
    compiler: Compiler,
    gold: dict[str, Any],
    budget: Budget,
    sha: str,
    dry: bool,
) -> dict[str, Any]:
    """评测单个样本，返回结构化结果（口径见模块 docstring）。"""
    question = str(gold["question"])
    result: dict[str, Any] = {
        "id": gold["id"],
        "question": question,
        "ambiguous": bool(gold.get("ambiguous", False)),
    }
    plan = planner.plan(question)
    if isinstance(plan, ClarificationRequest):
        # 歧义样本：反问 = PASS；非歧义样本反问 = FAIL（不猜，但标注说应可解析）
        result["clarify_ok"] = bool(gold.get("ambiguous", False))
        result["clarify_reasons"] = list(plan.reasons)
        # 歧义样本同样绑定评测快照（无执行结果，只锚 sha，result_hash 保持 null）
        if not dry and gold.get("snapshot_sha") in (None, "", PLACEHOLDER_SHA):
            anchor_snapshot_sha(str(gold["_file"]), sha)
        return result

    result["plan_ok"] = plan_acc(plan, gold)
    if dry:
        return result

    # 编译 → Guard → 执行（顺序不可调换，红线 N3）
    sql, _ = compiler.compile(plan)
    guarded, _ = enforce(sql, budget=budget)
    try:
        rows, columns = execute_sql(guarded)
    except Exception as exc:  # noqa: BLE001 - 执行失败记录到报告，不中断整轮
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result
    result["sql"] = guarded
    result["columns"] = columns
    result["row_count"] = len(rows)

    digest = result_hash(rows)
    stored = gold.get("result_hash")
    if stored in (None, "", PLACEHOLDER_HASH):
        if not dry:
            anchor_hash(str(gold["_file"]), gold, digest, sha)
        result["ex"] = "anchored"
        result["hash"] = digest
    elif stored == digest:
        result["ex"] = "pass"
        result["hash"] = digest
    else:
        result["ex"] = "fail"
        result["hash"] = digest
        result["expected_hash"] = stored
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry", action="store_true", help="只跑 Plan Acc，不执行 SQL 不写报告")
    args = parser.parse_args()

    if not args.dry:
        verify_snapshot()
    sha = git_short_sha()

    model = SemanticModel()
    planner = Planner(model)
    compiler = Compiler(model)
    snapshot_meta: dict[str, Any] = {}
    if not args.dry:
        snapshot_meta = json.loads((SNAPSHOT_DIR / f"{sha}.meta.json").read_text(encoding="utf-8"))
    budget = build_budget(snapshot_meta) if not args.dry else Budget(dialect="doris")

    samples = load_gold_finance()
    retail_count = len(list(GOLD_DIR.glob("gold-*.json"))) - len(samples)
    results = [evaluate(planner, compiler, g, budget, sha, args.dry) for g in samples]

    # 汇总（只输出实测计数，不做任何推断）
    non_ambiguous = [r for r in results if not r["ambiguous"]]
    ambiguous = [r for r in results if r["ambiguous"]]
    plan_ok = sum(1 for r in non_ambiguous if r.get("plan_ok"))
    clarify_ok = sum(1 for r in ambiguous if r.get("clarify_ok"))
    ex_pass = sum(1 for r in non_ambiguous if r.get("ex") == "pass")
    ex_fail = sum(1 for r in non_ambiguous if r.get("ex") == "fail")
    ex_anchored = sum(1 for r in non_ambiguous if r.get("ex") == "anchored")
    errors = sum(1 for r in results if "error" in r)

    summary = {
        "finance_total": len(samples),
        "retail_skipped": retail_count,
        "plan_acc": f"{plan_ok}/{len(non_ambiguous)}",
        "clarify": f"{clarify_ok}/{len(ambiguous)}",
        "ex": f"{ex_pass}/{ex_pass + ex_fail}" if ex_pass + ex_fail else "n/a(首轮锚定)",
        "ex_anchored": ex_anchored,
        "exec_errors": errors,
    }
    report = {
        "sha": sha,
        "created_at": datetime.now(TZ).isoformat(timespec="seconds"),
        "dry": args.dry,
        "summary": summary,
        "samples": results,
        "notes": (
            "口径：Plan Acc=metric/dimensions/time 与标注一致；EX=编译 SQL（过 Guard）"
            "执行结果 sha256 与锚定 result_hash 一致；歧义样本反问=pass；"
            "result_hash 锚定回填 gold JSON 后需 git commit（换快照须重锚定，见 "
            "data/snapshots/README.md）。"
        ),
    }

    if args.dry:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    target = REPORT_DIR / f"{sha}.json"
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[done] 报告已写入: {target.relative_to(REPO_ROOT)}")
    print(f"[summary] {json.dumps(summary, ensure_ascii=False)}")
    anchored = [r["id"] for r in results if r.get("ex") == "anchored"]
    if anchored:
        print(f"[anchor] 以下样本 result_hash 已锚定（gold JSON 已回填，请 commit）: {anchored}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
