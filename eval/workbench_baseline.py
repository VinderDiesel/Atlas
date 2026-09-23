"""ADR-0031 T01：只读收集代码与历史证据，不执行 SQL、网络或模型。"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from data.identity import REPO_ROOT, SNAPSHOT_DIR, git_full_sha

SHA_RE = re.compile(r"[0-9a-f]{7,40}\Z")
MAX_EVIDENCE_BYTES = 16 * 1024 * 1024


class EvidenceModel(BaseModel):
    """证据只接受显式字段，避免将未知原文一并转存。"""

    model_config = ConfigDict(extra="forbid", frozen=True)


class FileEvidence(EvidenceModel):
    path: str
    digest: str


class HistoricalEvidence(FileEvidence):
    code_sha: str
    verification: Literal["not_run"] = "not_run"


class SnapshotEvidence(FileEvidence):
    snapshot_sha: str
    verification: Literal["not_run"] = "not_run"


class InventoryIssue(EvidenceModel):
    path: str
    reason: Literal["missing_code_sha", "invalid_json", "unsafe_path", "invalid_snapshot"]


class CapabilityEvidence(EvidenceModel):
    evidence_id: str
    sources: tuple[FileEvidence, ...]
    boundary: str
    runtime_verification: Literal["not_run"] = "not_run"


class StrategyBaseline(EvidenceModel):
    strategy: Literal["rules", "rules_llm", "sft", "rl"]
    status: Literal["not_run"] = "not_run"
    metrics: None = None
    reason: str = "本次仅清点证据，未运行该策略"


class BaselineReport(EvidenceModel):
    """清单报告不是评测报告；Schema 禁止承载当前效果数字。"""

    schema_version: Literal[1] = 1
    report_type: Literal["workbench_baseline"] = "workbench_baseline"
    created_at: datetime
    code_sha: str
    worktree_status: tuple[str, ...]
    worktree_digest: str
    current_metrics: None = None
    data_verified: Literal[False] = False
    historical_evidence: tuple[HistoricalEvidence, ...]
    snapshots: tuple[SnapshotEvidence, ...]
    issues: tuple[InventoryIssue, ...]
    capabilities: tuple[CapabilityEvidence, ...]
    strategies: tuple[StrategyBaseline, ...]


# 仅记录既有代码的检查入口和核验边界；文件存在不提升为服务接线或真链成功。
CAPABILITY_SOURCES: tuple[tuple[str, tuple[str, ...], str], ...] = (
    (
        "E02",
        ("agent/graph.py", "agent/compiler.py", "agent/security/sql_guard.py"),
        "确定性链的代码证据，当前运行效果未复测",
    ),
    (
        "E05",
        ("serving/api.py", "agent/factory.py", "agent/generator.py"),
        "候选门控不等于默认工厂完整候选链；本清单不证明 Tools 循环",
    ),
    (
        "E06",
        ("serving/api.py", "agent/factory.py", "observability/otel.py"),
        "旧分析流 compute-then-stream 不是真实进度；checkpoint 不是运行目录",
    ),
    (
        "E08",
        ("serving/auth.py", "tests/test_idp.py"),
        "RS256/JWKS 组件不等于 OIDC 浏览器登录或 HTTP 接线已验收",
    ),
    (
        "E11",
        ("agent/generator.py", "agent/graph.py", "agent/prompts/generator_plan.yaml"),
        "旧候选 filters 简化、隐式二次检索及全量清单，不能称公平 top-K 对照",
    ),
)


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _git(repo: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, timeout=30
    ).stdout


def _safe_file(repo: Path, path: Path) -> bool:
    relative = path.relative_to(repo)
    return (
        not any(
            (repo / Path(*relative.parts[:i])).is_symlink()
            for i in range(1, len(relative.parts) + 1)
        )
        and path.is_file()
    )


def _read_json(repo: Path, path: Path) -> tuple[dict[str, object] | None, str]:
    if not _safe_file(repo, path) or path.stat().st_size > MAX_EVIDENCE_BYTES:
        return None, ""
    raw = path.read_bytes()
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeError):
        return None, _digest(raw)
    return (data if isinstance(data, dict) else None), _digest(raw)


def inventory_reports(
    repo: Path,
) -> tuple[tuple[HistoricalEvidence, ...], tuple[InventoryIssue, ...]]:
    """清点 repo 下历史报告的身份/摘要；不复制效果或原文，坏文件记录原因。

    返回有效身份清单和问题清单；文件读取失败抛 OSError，不静默当成功。
    """
    evidence: list[HistoricalEvidence] = []
    issues: list[InventoryIssue] = []
    for path in sorted((repo / "eval" / "reports").glob("*.json")):
        name = path.relative_to(repo).as_posix()
        data, digest = _read_json(repo, path)
        if data is None:
            issues.append(
                InventoryIssue(path=name, reason="invalid_json" if digest else "unsafe_path")
            )
            continue
        if data.get("report_type") == "workbench_baseline":
            continue
        sha = data.get("code_sha", data.get("sha"))
        if not isinstance(sha, str) or not SHA_RE.fullmatch(sha):
            issues.append(InventoryIssue(path=name, reason="missing_code_sha"))
            continue
        evidence.append(HistoricalEvidence(path=name, digest=digest, code_sha=sha))
    return tuple(evidence), tuple(issues)


def _snapshots(repo: Path) -> tuple[tuple[SnapshotEvidence, ...], tuple[InventoryIssue, ...]]:
    evidence: list[SnapshotEvidence] = []
    issues: list[InventoryIssue] = []
    for path in sorted((repo / SNAPSHOT_DIR.relative_to(REPO_ROOT)).glob("*.meta.json")):
        name = path.relative_to(repo).as_posix()
        data, digest = _read_json(repo, path)
        sha = data.get("sha") if data else None
        if not isinstance(sha, str) or not SHA_RE.fullmatch(sha) or path.name != f"{sha}.meta.json":
            issues.append(InventoryIssue(path=name, reason="invalid_snapshot"))
            continue
        evidence.append(SnapshotEvidence(path=name, digest=digest, snapshot_sha=sha))
    return tuple(evidence), tuple(issues)


def _capabilities(repo: Path) -> tuple[CapabilityEvidence, ...]:
    items = []
    for evidence_id, paths, boundary in CAPABILITY_SOURCES:
        sources = tuple(
            FileEvidence(path=p, digest=_digest((repo / p).read_bytes()))
            for p in paths
            if _safe_file(repo, repo / p)
        )
        items.append(
            CapabilityEvidence(evidence_id=evidence_id, sources=sources, boundary=boundary)
        )
    return tuple(items)


def collect_baseline(repo: Path, output: Path) -> BaselineReport:
    """收集指定 Git 仓库的证据身份，排他创建 output JSON 并返回报告。

    不加载 .env、不访问服务、不验证数据、不将历史指标当成当前实测。
    抛 FileExistsError 防止覆盖既有证据；Git/IO 失败直接报错，不伪造身份。
    """
    repo = repo.resolve(strict=True)
    if output.exists() or output.is_symlink():
        raise FileExistsError(output)
    code_sha = git_full_sha(repo)
    status = _git(repo, "status", "--porcelain=v1", "--untracked-files=all")
    tree = hashlib.sha256(_git(repo, "diff", "HEAD", "--binary"))
    tree.update(status)
    for raw_path in _git(repo, "ls-files", "--others", "--exclude-standard", "-z").split(b"\0"):
        if not raw_path:
            continue
        path = repo / raw_path.decode()
        tree.update(raw_path)
        if _safe_file(repo, path):
            tree.update(hashlib.sha256(path.read_bytes()).digest())
    historical, issues = inventory_reports(repo)
    snapshots, snapshot_issues = _snapshots(repo)
    report = BaselineReport(
        created_at=datetime.now(UTC),
        code_sha=code_sha,
        worktree_status=tuple(status.decode().splitlines()),
        worktree_digest=tree.hexdigest(),
        historical_evidence=historical,
        snapshots=snapshots,
        issues=issues + snapshot_issues,
        capabilities=_capabilities(repo),
        strategies=tuple(StrategyBaseline(strategy=s) for s in ("rules", "rules_llm", "sft", "rl")),
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as handle:
        handle.write(report.model_dump_json(indent=2) + "\n")
    return report


def main(argv: list[str] | None = None) -> int:
    """CLI 收集清单：--repo 指定仓库，--output 必填；失败返回 2，无网络。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        report = collect_baseline(args.repo, args.output)
    except (OSError, subprocess.SubprocessError):
        print("基线采集失败：请检查 Git、输入目录与输出路径（不会覆盖既有报告）")
        return 2
    print(f"清单已写入 {args.output}，代码 {report.code_sha}；业务评测 not_run")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
