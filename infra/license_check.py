"""许可证与第三方内容守卫（ADR-0023 决策 ⑦）。

做什么
    把「`.gitignore` 是唯一防线」升级为 6 条可执行断言，任一失败 exit 1：
    ① `data/raw` 与 `data/fibo/{fibo-src,vendor}` 在 Git 索引中零文件
    ② `LICENSE` 首个非空行含 `Apache License`，且 `NOTICE` 存在且非空
    ③ `pyproject.toml` 的 license/classifier 与 `LICENSE` 文本三方一致
    ④ 已安装分发的 GPL/LGPL/AGPL 嫌疑集 ⊆ 白名单（显式常量，P0a 落地后为空集）
    ⑤ `scripts/tpcds_kit_sf01.patch`（若存在）文件头含 EULA 声明块 + 全大写 legend
    ⑥ `--report` 产出 `exports/dependency-licenses.json`（name/version/license/引入路径）

参数
    --report    追加产出 `exports/dependency-licenses.json`——已安装分发清单，
                与 `NOTICE` 指向的机器产物同物（`NOTICE` 只指向它，不复制它）。
                经 make 调用用变量触发：`make license-check REPORT=1`
                （GNU make 拒绝未知长选项 `--report`，实测见 Makefile 注释）。

退出码
    0  全部断言通过
    1  任一断言失败——守卫不得静默通过，断言内部异常按失败处理

异常
    不向外抛异常：每条断言自捕获并转为 FAIL 详情（错误信息必须进日志）。
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata as md
import json
import re
import subprocess
import tomllib
from collections.abc import Callable, Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LICENSE_PATH = REPO_ROOT / "LICENSE"
NOTICE_PATH = REPO_ROOT / "NOTICE"
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"
TPC_PATCH_PATH = REPO_ROOT / "scripts" / "tpcds_kit_sf01.patch"
REPORT_PATH = REPO_ROOT / "exports" / "dependency-licenses.json"

# Apache-2.0 规范全文（含 APPENDIX）的 sha256。P0a 实测：与 opentelemetry 分发的
# 规范副本逐字节一致。钉住它防「LICENSE 再次被改写而 pyproject/README 不知情」。
CANONICAL_APACHE_SHA256 = "c71d239df91726fc519c6eb72d318ec65820627232b2f796219e87dcf35d0ab4"
APACHE_SPDX = "Apache-2.0"
APACHE_CLASSIFIER = "License :: OSI Approved :: Apache Software License"

# GPL 扫描：`(?<![A-Za-z])` 防匹配更长标识符中的片段；`A?L?` 覆盖 GPL/LGPL/AGPL。
# 大小写敏感——许可证标识符惯用大写（如 `GPLv2`、`LGPL-3.0-only`）。
COPYLEFT_RE = re.compile(r"(?<![A-Za-z])(A?L?GPL)")

# copyleft 白名单：P0a 落地后应为显式空集。若未来被迫引入 copyleft 依赖，必须在此
# 逐包登记（包名 + 理由注释）——不允许「忽略全部 copyleft」的宽松形态（决策 ⑦ 断言 4）。
COPYLEFT_WHITELIST: frozenset[str] = frozenset()

# TPC-DS patch 声明块必须含的标记（决策 ⑤：EULA 声明块 + 条款 9(b) 全大写 legend）。
TPC_HEADER_MARKERS = (
    "USER LICENSE AGREEMENT VERSION 2.2",
    "不适用本仓 Apache-2.0",
    "THE TPC SOFTWARE IS AVAILABLE WITHOUT CHARGE FROM TPC.",
)


def _norm(name: str) -> str:
    """PEP 503 归一化（大小写与 `-_.` 折叠），用于跨元数据比对包名。"""
    return re.sub(r"[-_.]+", "-", name).lower()


def installed_distributions() -> dict[str, md.Distribution]:
    """已安装分发，键 = PEP 503 归一化名（重复名保留首个出现）。"""
    dists: dict[str, md.Distribution] = {}
    for dist in md.distributions():
        dists.setdefault(_norm(dist.metadata["Name"] or "?"), dist)
    return dists


def _license_candidates(meta: md.PackageMetadata) -> list[tuple[str, str]]:
    """GPL 扫描的候选串（ADR-0023 同口径：expression → 单行 License → classifiers）。

    短值规则：`License` 字段含换行（全文型，常含 bundled 组件披露）时跳过——
    numpy 全文含 `GPL-3.0-or-later`（libgfortran）、pandas 全文含 `GPL-compatible`，
    均非本包许可却会误报（P0a 实测，短值规则是必要防线）。
    """
    out: list[tuple[str, str]] = []
    expr = meta.get("License-Expression")
    if expr and expr.strip():
        out.append(("License-Expression", expr.strip()))
    lic = meta.get("License")
    if lic and lic.strip() and "\n" not in lic.strip():
        out.append(("License", lic.strip()))
    for classifier in meta.get_all("Classifier") or []:
        if classifier.startswith("License ::"):
            out.append(("Classifier", classifier))
    return out


def display_license(meta: md.PackageMetadata) -> str:
    """报告用许可证取值链：expression → 单行 License → classifier（去前缀）。

    多行 License 跳过展示——首行常是版权行（numpy 实测为 `Copyright (c) …`）
    或授权模板句，展示无意义；四类多行包均有 classifier 兜底（P0a 实测）。
    """
    expr = meta.get("License-Expression")
    if expr and expr.strip():
        return " ".join(expr.split())
    lic = meta.get("License")
    if lic and lic.strip() and "\n" not in lic.strip():
        return " ".join(lic.split())
    for classifier in meta.get_all("Classifier") or []:
        if classifier.startswith("License :: OSI Approved :: "):
            return classifier.removeprefix("License :: OSI Approved :: ").strip()
    for classifier in meta.get_all("Classifier") or []:
        if classifier.startswith("License :: "):
            return classifier.removeprefix("License :: ").strip()
    return "UNKNOWN"


def _requires_names(meta: md.PackageMetadata) -> list[str]:
    """从 `Requires-Dist` 串取归一化包名（PEP 508 名称止于空白/分号/括号/比较符）。"""
    names: list[str] = []
    for req in meta.get_all("Requires-Dist") or []:
        token = re.split(r"[\s;(\[<>=!~]", req.strip(), maxsplit=1)[0]
        if token:
            names.append(_norm(token))
    return names


def _reverse_parents(dists: dict[str, md.Distribution]) -> dict[str, list[str]]:
    """归一化名 → 已安装父包名列表（`Requires-Dist` 反查，与背景段反向依赖表同口径）。"""
    parents: dict[str, list[str]] = {}
    for key, dist in dists.items():
        if key == "atlas":
            continue
        for dep in _requires_names(dist.metadata):
            parents.setdefault(dep, []).append(dist.metadata["Name"] or key)
    return parents


def check_restricted_paths() -> tuple[bool, str]:
    """断言 ①：受限路径在 Git 索引中零文件（`.gitignore` 防线升级为断言）。"""
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--", "data/raw", "data/fibo/fibo-src", "data/fibo/vendor"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return False, f"git ls-files 失败（exit {result.returncode}）：{result.stderr.strip()}"
    files = [line for line in result.stdout.splitlines() if line.strip()]
    if files:
        sample = "、".join(files[:3])
        return (
            False,
            f"索引含 {len(files)} 个受限路径文件（前 3：{sample}）——专有 EULA 内容不得入库",
        )
    return True, "data/raw 与 data/fibo/{fibo-src,vendor} 索引零文件（git ls-files --cached 口径）"


def check_license_notice() -> tuple[bool, str]:
    """断言 ②：LICENSE 首个非空行含 `Apache License` + NOTICE 存在且非空。"""
    if not LICENSE_PATH.is_file():
        return False, "LICENSE 缺失"
    first = next(
        (
            line.strip()
            for line in LICENSE_PATH.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ),
        "",
    )
    if "Apache License" not in first:
        return False, f"LICENSE 首个非空行不含 Apache License：{first[:60]!r}"
    if not NOTICE_PATH.is_file() or not NOTICE_PATH.read_text(encoding="utf-8").strip():
        return False, "NOTICE 缺失或为空"
    return True, f"LICENSE 首行 = {first!r}；NOTICE 存在（{NOTICE_PATH.stat().st_size} 字节）"


def check_metadata_consistency() -> tuple[bool, str]:
    """断言 ③：pyproject 的 license/classifier 与 LICENSE 文本三方一致（防再次分叉）。"""
    digest = hashlib.sha256(LICENSE_PATH.read_bytes()).hexdigest()
    if digest != CANONICAL_APACHE_SHA256:
        return False, f"LICENSE sha256 漂移：{digest}（预期规范全文 {CANONICAL_APACHE_SHA256}）"
    with open(PYPROJECT_PATH, "rb") as fh:
        project = tomllib.load(fh).get("project", {})
    license_field = project.get("license", {})
    text = license_field.get("text", "") if isinstance(license_field, dict) else ""
    if text != APACHE_SPDX:
        return False, f"pyproject project.license.text = {text!r}（预期 {APACHE_SPDX!r}）"
    if APACHE_CLASSIFIER not in project.get("classifiers", []):
        return False, f"pyproject 缺 classifier {APACHE_CLASSIFIER!r}"
    return (
        True,
        f"LICENSE 字节钉（{digest[:12]}…）+ pyproject license/classifier 三方一致（{APACHE_SPDX}）",
    )


def check_no_copyleft_installed() -> tuple[bool, str]:
    """断言 ④：已安装分发的 copyleft 嫌疑集 ⊆ 白名单（显式常量，现为空集）。"""
    dists = installed_distributions()
    suspects: list[tuple[str, str, str, str]] = []
    for key, dist in sorted(dists.items()):
        for source, value in _license_candidates(dist.metadata):
            if COPYLEFT_RE.search(value):
                suspects.append(
                    (
                        dist.metadata["Name"] or key,
                        dist.metadata["Version"] or "?",
                        source,
                        value[:80],
                    )
                )
    extra = [s for s in suspects if _norm(s[0]) not in COPYLEFT_WHITELIST]
    if extra:
        lines = "；".join(f"{n} {v}（{src}: {val}）" for n, v, src, val in extra[:5])
        return False, f"发现 {len(extra)} 个未白名单 copyleft 分发：{lines}"
    return True, (
        f"copyleft 嫌疑集 ∅ ⊆ 白名单（扫描 {len(dists)} 个分发；短值规则跳过全文型 License 字段）"
    )


def check_tpcds_patch_header() -> tuple[bool, str]:
    """断言 ⑤：TPC patch 文件头声明块（EULA 版本 + carve-out + 全大写 legend）。"""
    if not TPC_PATCH_PATH.is_file():
        return True, "patch 不存在（已改写为运行时脚本则按 ADR 决策 ⑤ 自动跳过）"
    header_lines: list[str] = []
    for line in TPC_PATCH_PATH.read_text(encoding="utf-8").splitlines():
        if line.startswith("diff --git "):
            break
        header_lines.append(line)
    header = "\n".join(header_lines)
    missing = [marker for marker in TPC_HEADER_MARKERS if marker not in header]
    if missing:
        return False, f"patch 文件头缺声明：{missing}"
    return True, f"patch 文件头含 EULA 声明块与全大写 legend（{len(header_lines)} 行）"


def write_dependency_report() -> tuple[bool, str]:
    """断言 ⑥：产出 exports/dependency-licenses.json（沿 exports/ 机器产物入库惯例）。"""
    dists = installed_distributions()
    if "atlas" not in dists:
        return False, "atlas 分发不在当前环境——报告须由项目 .venv 生成（make 走 $(PYTHON)）"
    direct = set(_requires_names(dists["atlas"].metadata))
    parents = _reverse_parents(dists)
    packages = []
    for key, dist in sorted(dists.items()):
        if key == "atlas":
            introduced = "-"
        elif key in direct:
            introduced = "atlas"
        else:
            introduced = " / ".join(sorted(set(parents.get(key, [])))) or "-"
        packages.append(
            {
                "name": dist.metadata["Name"] or key,
                "version": dist.metadata["Version"] or "?",
                "license": display_license(dist.metadata),
                "引入路径": introduced,
            }
        )
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(
        json.dumps(
            {
                "generated_by": "infra/license_check.py --report（ADR-0023 决策 ⑦）",
                "package_count": len(packages),
                "packages": packages,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return True, f"exports/dependency-licenses.json 已产出（{len(packages)} 条 = 实测已安装分发数）"


CHECKS: list[tuple[str, Callable[[], tuple[bool, str]]]] = [
    ("受限路径索引零文件", check_restricted_paths),
    ("LICENSE 首行 + NOTICE 存在", check_license_notice),
    ("pyproject 与 LICENSE 三方一致", check_metadata_consistency),
    ("copyleft 嫌疑集 ⊆ 白名单", check_no_copyleft_installed),
    ("TPC patch 文件头声明块", check_tpcds_patch_header),
]


def main(argv: Sequence[str] | None = None) -> int:
    """入口：跑 5 条常驻断言，`--report` 时追加第 6 条（产出依赖清单）。"""
    parser = argparse.ArgumentParser(description="许可证与第三方内容守卫（ADR-0023 决策 ⑦）")
    parser.add_argument(
        "--report",
        action="store_true",
        help="追加产出 exports/dependency-licenses.json（make 侧用 REPORT=1 触发）",
    )
    args = parser.parse_args(argv)

    print("== license-check（ADR-0023 决策 ⑦）")
    failures: list[str] = []
    for index, (title, check) in enumerate(CHECKS, 1):
        try:
            ok, detail = check()
        except Exception as exc:  # 断言内部异常按失败处理——守卫不得静默通过
            ok, detail = False, f"断言执行异常：{exc!r}"
        print(f"[{index}/{len(CHECKS)}] {'PASS' if ok else 'FAIL'}  {title}：{detail}")
        if not ok:
            failures.append(title)

    if args.report:
        try:
            ok, detail = write_dependency_report()
        except Exception as exc:
            ok, detail = False, f"报告产出异常：{exc!r}"
        print(f"[report] {'PASS' if ok else 'FAIL'}  依赖清单：{detail}")
        if not ok:
            failures.append("--report")

    if failures:
        print(f"== license-check: FAIL（{len(failures)} 项：{'、'.join(failures)}）")
        return 1
    print(
        f"== license-check: PASS（{len(CHECKS)}/{len(CHECKS)}"
        + ("，含 --report" if args.report else "")
        + "）"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
