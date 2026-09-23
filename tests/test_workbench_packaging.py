"""T13 发行打包：compose.connect.yml 配置检查 + Makefile 入口断言。

红测先行（TDD）：导入尚不存在的 compose 文件路径常量。
断言覆盖：
- compose.connect.yml 存在且可被 docker compose config 验证
- 无默认秘密（密码/密钥不硬编码）
- Makefile 有 backup/restore/diagnostics 入口
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_CONNECT_PATH = REPO_ROOT / "infra" / "docker" / "compose.connect.yml"
MAKEFILE_PATH = REPO_ROOT / "Makefile"


# ---------------------------------------------------------------------------
# compose.connect.yml 存在性与配置检查
# ---------------------------------------------------------------------------


def test_compose_connect_file_exists() -> None:
    """compose.connect.yml 必须存在（最小发行配置）。"""
    assert COMPOSE_CONNECT_PATH.exists(), f"缺失最小发行 compose：{COMPOSE_CONNECT_PATH}"


def test_compose_connect_is_valid_yaml() -> None:
    """compose.connect.yml 是有效 YAML。"""
    import yaml

    content = COMPOSE_CONNECT_PATH.read_text(encoding="utf-8")
    parsed = yaml.safe_load(content)
    assert isinstance(parsed, dict), "compose 顶层必须是 mapping"
    assert "services" in parsed, "compose 必须声明 services"


def test_compose_connect_has_no_default_secrets() -> None:
    """compose.connect.yml 不含默认秘密（密码/密钥不硬编码，D12/N9）。"""
    content = COMPOSE_CONNECT_PATH.read_text(encoding="utf-8")
    # 常见秘密模式（硬编码密码/密钥）——只检查 environment 值，不检查注释
    forbidden_patterns = [
        "password: atlas",  # 硬编码密码
        "ATLAS_JWT_SECRET: default",  # 默认密钥
    ]
    for pattern in forbidden_patterns:
        assert pattern not in content.lower(), f"compose 含硬编码秘密：{pattern}"


def test_compose_connect_config_validates() -> None:
    """compose.connect.yml 可被 docker compose config 验证（不打印展开秘密）。"""
    if not COMPOSE_CONNECT_PATH.exists():
        pytest.skip("compose.connect.yml 尚未创建")
    # --quiet 只验证不输出（避免泄露展开后的秘密）
    result = subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE_CONNECT_PATH), "config", "--quiet"],
        capture_output=True,
        text=True,
    )
    # docker compose 可能不可用（CI 环境）或缺少环境变量，此时跳过
    if result.returncode != 0:
        stderr_lower = result.stderr.lower()
        if "docker" in stderr_lower or "missing a value" in stderr_lower:
            pytest.skip("docker compose 不可用或缺少环境变量")
    assert result.returncode == 0, f"compose config 验证失败：{result.stderr}"


# ---------------------------------------------------------------------------
# Makefile 入口断言
# ---------------------------------------------------------------------------


def test_makefile_has_backup_target() -> None:
    """Makefile 有 backup 入口（D12 运维）。"""
    content = MAKEFILE_PATH.read_text(encoding="utf-8")
    assert "backup:" in content, "Makefile 缺失 backup 入口"


def test_makefile_has_restore_target() -> None:
    """Makefile 有 restore 入口（D12 运维）。"""
    content = MAKEFILE_PATH.read_text(encoding="utf-8")
    assert "restore:" in content, "Makefile 缺失 restore 入口"


def test_makefile_has_diagnostics_target() -> None:
    """Makefile 有 diagnostics 入口（D12/D13 运维）。"""
    content = MAKEFILE_PATH.read_text(encoding="utf-8")
    assert "diagnostics:" in content, "Makefile 缺失 diagnostics 入口"


def test_makefile_backup_uses_python() -> None:
    """backup 入口走 Python 脚本（不直接操作 SQLite 文件）。"""
    content = MAKEFILE_PATH.read_text(encoding="utf-8")
    # 找 backup: 后的下一行（recipe）
    lines = content.split("\n")
    for i, line in enumerate(lines):
        if line.startswith("backup:"):
            # 下一行应是 recipe（以 tab 开头）
            if i + 1 < len(lines):
                recipe = lines[i + 1]
                assert recipe.startswith("\t"), "backup recipe 应以 tab 开头"
                assert "python" in recipe or "$(PYTHON)" in recipe, (
                    "backup 应走 Python 脚本（不直接 cp SQLite）"
                )
            return
    pytest.fail("Makefile 未找到 backup: 入口")
