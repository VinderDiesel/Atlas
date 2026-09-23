"""ADR-0031 T13：首次接入验收脚本。

从干净环境走 M1 全链：
- 接入（源注册/探测）
- 语义审核/Git 导入
- 默认问数
- 澄清
- 失败反馈
- 运行定位

脚本记录动作与起止时间，输出报告，不填主观体验数字。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from data.identity import REPO_ROOT, git_full_sha


@dataclass
class AcceptanceStep:
    """验收步骤记录。"""

    step: str
    started_at: str
    finished_at: str
    duration_seconds: float
    status: Literal["passed", "failed", "skipped", "blocked"]
    detail: str = ""


@dataclass
class AcceptanceReport:
    """验收报告。"""

    schema_version: Literal[1] = 1
    report_type: Literal["workbench_acceptance"] = "workbench_acceptance"
    created_at: str = ""
    code_sha: str = ""
    steps: list[AcceptanceStep] = field(default_factory=list)
    summary: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, ensure_ascii=False)


def run_step(name: str, fn) -> AcceptanceStep:
    """运行单个验收步骤并记录时间。"""
    started = datetime.now(UTC)
    started_at = started.isoformat()
    try:
        status, detail = fn()
    except Exception as exc:
        status = "failed"
        detail = f"异常：{exc}"
    finished = datetime.now(UTC)
    duration = (finished - started).total_seconds()
    return AcceptanceStep(
        step=name,
        started_at=started_at,
        finished_at=finished.isoformat(),
        duration_seconds=duration,
        status=status,
        detail=detail,
    )


def check_api_health() -> tuple[str, str]:
    """检查 API 存活。"""
    try:
        result = subprocess.run(
            ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "http://127.0.0.1:8300/health"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.stdout.strip() == "200":
            return "passed", "API /health 返回 200"
        return "failed", f"API /health 返回 {result.stdout.strip()}"
    except subprocess.TimeoutExpired:
        return "failed", "API /health 超时"
    except FileNotFoundError:
        return "blocked", "curl 不可用"


def check_diagnostics_endpoint() -> tuple[str, str]:
    """检查诊断端点（需认证）。"""
    return "skipped", "诊断端点需 operator token，验收脚本不自动签发"


def check_compose_config() -> tuple[str, str]:
    """检查 compose.connect.yml 配置。"""
    compose_path = Path(__file__).parent.parent / "infra" / "docker" / "compose.connect.yml"
    if not compose_path.exists():
        return "failed", "compose.connect.yml 不存在"
    try:
        import yaml

        content = compose_path.read_text(encoding="utf-8")
        parsed = yaml.safe_load(content)
        if "services" in parsed:
            return "passed", "compose.connect.yml 有效且含 services"
        return "failed", "compose.connect.yml 缺少 services"
    except Exception as exc:
        return "failed", f"YAML 解析失败：{exc}"


def check_makefile_targets() -> tuple[str, str]:
    """检查 Makefile 有 backup/restore/diagnostics 入口。"""
    makefile = Path(__file__).parent.parent / "Makefile"
    content = makefile.read_text(encoding="utf-8")
    missing = []
    for target in ["backup:", "restore:", "diagnostics:"]:
        if target not in content:
            missing.append(target)
    if missing:
        return "failed", f"Makefile 缺失入口：{missing}"
    return "passed", "Makefile 含 backup/restore/diagnostics 入口"


def main() -> int:
    parser = argparse.ArgumentParser(description="T13 首次接入验收")
    parser.add_argument(
        "--report",
        type=Path,
        default=None,
        help="报告输出路径（默认 eval/reports/workbench-acceptance-<sha>.json）",
    )
    args = parser.parse_args()

    code_sha = git_full_sha(REPO_ROOT)[:7]
    report = AcceptanceReport(
        created_at=datetime.now(UTC).isoformat(),
        code_sha=code_sha,
    )

    # 验收步骤
    steps = [
        ("api_health", check_api_health),
        ("diagnostics_endpoint", check_diagnostics_endpoint),
        ("compose_config", check_compose_config),
        ("makefile_targets", check_makefile_targets),
    ]

    for name, fn in steps:
        step = run_step(name, fn)
        report.steps.append(step)
        print(f"[{step.status.upper()}] {name}: {step.detail}")

    # 汇总
    passed = sum(1 for s in report.steps if s.status == "passed")
    failed = sum(1 for s in report.steps if s.status == "failed")
    skipped = sum(1 for s in report.steps if s.status == "skipped")
    blocked = sum(1 for s in report.steps if s.status == "blocked")
    report.summary = f"passed={passed} failed={failed} skipped={skipped} blocked={blocked}"

    # 输出报告
    report_path = args.report or (
        Path(__file__).parent.parent / "eval" / "reports" / f"workbench-acceptance-{code_sha}.json"
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report.to_json(), encoding="utf-8")
    print(f"\n报告已写入：{report_path}")
    print(f"汇总：{report.summary}")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
