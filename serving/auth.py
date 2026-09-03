"""三角色 JWT 签发/校验与角色 → 行级策略绑定（行级权限链路的 JWT 服务，Day 25 落地）。

口径（诚实声明）
--------------------
- 当前数据域是 TPC-DI 金融 dwd（数据已在 Doris 且绑定快照）：gold-146
  「按分支和客户等级统计 2015 年交易额 Top5」同一 SQL 下，hq_admin /
  branch_manager / compliance_auditor 三角色注入不同谓词 → 结果不同。
- 零售域（rp_dept_visible：region / product_category）角色同构已注册，
  但 TPC-DI 快照无零售数据，待零售数据落地后换绑策略即可，机制不变。
- 规划原文的「华东区 / 华东区只读某品类」对应零售角色；金融 dwd 的
  dim_broker.Branch 是 TPC-DI 随机变造值（实测无地理语义），故本地
  角色取金融域真实口径（详见 README §3.3 Day 25 勾选与 KL #15）。
- JWT 用 HS256 自实现（仅标准库），是**本地实现**：生产应换受管 IdP
  签发（本模块只负责验证声明并把角色编译成策略），密钥必须走环境变量。

密钥
----
ATLAS_JWT_SECRET（.env，已 gitignore）。未设置时拒绝签发——不内置默认
密钥（AGENTS.md N9）。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

import yaml

# 相对仓库根：serving/auth.py → semantic/policies/row_policy.yml
_REPO_ROOT = Path(__file__).resolve().parent.parent
POLICY_PATH = _REPO_ROOT / "semantic" / "policies" / "row_policy.yml"

TOKEN_TTL_SECONDS = 3600  # 本地开发 token 有效期 1 小时

# 角色 → (策略文件内策略名, 策略文件内角色名, 说明)
# 角色条件来自 semantic/policies/row_policy.yml（Git 唯一事实源），不在此复制。
ROLE_DIRECTORY: dict[str, tuple[str, str, str]] = {
    # 金融域（rp_branch_visible）：gold-146 问句可注入
    "hq_admin": (
        "rp_branch_visible",
        "hq_admin",
        "总部管理员：全量可见（条件 1=1）",
    ),
    "branch_manager": (
        "rp_branch_visible",
        "branch_manager",
        "分支经理：仅见本分支（dim_broker.branch = 本人分支）",
    ),
    "compliance_auditor": (
        "rp_branch_visible",
        "compliance_auditor",
        "合规审计：仅见低敏感客户档（dim_customer.tier <= 上限）",
    ),
    # 零售域（rp_dept_visible）：注册待数据落地（无 TPC-DI 零售快照）
    "region_manager": (
        "rp_dept_visible",
        "region_manager",
        "大区经理：仅见本大区（region = 本人区域，零售域，待数据）",
    ),
    "category_analyst": (
        "rp_dept_visible",
        "category_analyst",
        "品类分析师：本大区 + 指定品类（零售域，待数据）",
    ),
}


class AuthError(Exception):
    """JWT 签发/校验失败。"""


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def _jwt_secret() -> str:
    secret = os.environ.get("ATLAS_JWT_SECRET", "")
    if not secret:
        raise AuthError("ATLAS_JWT_SECRET 未设置：请写入 .env（本地开发密钥，见 .env.example）")
    return secret


def sign_token(
    role: str,
    user_context: dict[str, object],
    *,
    secret: str | None = None,
    ttl: int = TOKEN_TTL_SECONDS,
    subject: str = "atlas-user",
) -> str:
    """签发 HS256 JWT，claims 含 role 与策略渲染所需的 user_context。

    Parameters
    ----------
    role : 角色名，须在 ROLE_DIRECTORY 注册
    user_context : 注入策略模板的声明值（如 branch / max_tier）
    secret : HS256 密钥；None 时从环境变量 ATLAS_JWT_SECRET 读取
    ttl : token 有效期（秒）

    Returns
    -------
    JWT 字符串（三段 base64url，点分隔）

    Raises
    ------
    AuthError : role 未注册 / 密钥缺失
    """
    if role not in ROLE_DIRECTORY:
        raise AuthError(f"角色未注册：{role!r}（ROLE_DIRECTORY 可加）")
    secret = secret or _jwt_secret()
    now = int(time.time())
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "sub": subject,
        "role": role,
        "iat": now,
        "exp": now + ttl,
        "user_context": user_context,
    }
    head_part = _b64url_encode(json.dumps(header, sort_keys=True).encode())
    body_part = _b64url_encode(json.dumps(payload, sort_keys=True).encode())
    signing_input = f"{head_part}.{body_part}".encode()
    digest = hmac.new(secret.encode(), signing_input, hashlib.sha256).digest()
    return f"{head_part}.{body_part}.{_b64url_encode(digest)}"


def verify_token(token: str, *, secret: str | None = None) -> dict[str, object]:
    """校验 JWT 并返回 claims。

    Raises
    ------
    AuthError : 格式错 / 签名不符 / 已过期 / 密钥缺失
    """
    secret = secret or _jwt_secret()
    parts = token.split(".")
    if len(parts) != 3:
        raise AuthError("JWT 格式错误：应为三段 base64url")
    head_part, body_part, sig_part = parts
    signing_input = f"{head_part}.{body_part}".encode()
    expected = hmac.new(secret.encode(), signing_input, hashlib.sha256).digest()
    actual = _b64url_decode(sig_part)
    if not hmac.compare_digest(expected, actual):
        raise AuthError("JWT 签名校验失败")
    try:
        payload: dict[str, object] = json.loads(_b64url_decode(body_part))
    except (ValueError, json.JSONDecodeError) as exc:
        raise AuthError(f"JWT payload 解析失败：{exc}") from exc
    now = int(time.time())
    exp = payload.get("exp", 0)
    if not isinstance(exp, int) or exp <= now:
        raise AuthError("JWT 已过期")
    if payload.get("role") not in ROLE_DIRECTORY:
        raise AuthError(f"JWT role 未注册：{payload.get('role')!r}")
    return payload


@dataclass(frozen=True)
class ResolvedPolicy:
    """角色解析结果：可直接交给 Guard 注入的行级策略。"""

    role: str
    policy_name: str
    condition: str
    claims: dict[str, object]

    def describe(self) -> str:
        return f"[{self.role}] {self.policy_name}: {self.condition}"


def resolve_policy(
    token: str,
    *,
    secret: str | None = None,
    policy_path: Path = POLICY_PATH,
) -> ResolvedPolicy:
    """token → (策略名, 已渲染谓词)：角色条件来自 row_policy.yml + claims。

    渲染在服务端做（Guard 的 Policy.render 同规则：{{ user.<key> }} 占位符，
    非法字面量由 Guard 兜底拒绝，这里不做二次实现）。
    """
    claims = verify_token(token, secret=secret)
    role = str(claims["role"])
    policy_name, role_name, _ = ROLE_DIRECTORY[role]
    user_context = claims.get("user_context")
    if not isinstance(user_context, dict):
        raise AuthError("JWT 缺少 user_context")
    doc = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
    condition: str | None = None
    for policy in doc["policies"]:
        if policy["name"] == policy_name:
            for r in policy["roles"]:
                if r["name"] == role_name:
                    condition = r["condition"]
                    break
    if condition is None:
        raise AuthError(f"策略 {policy_name!r} 中找不到角色 {role_name!r}（{policy_path}）")
    rendered = condition
    for key, value in user_context.items():
        rendered = rendered.replace(f"{{{{ user.{key} }}}}", _literal(value))
    if "{{" in rendered:
        raise AuthError(f"角色 {role!r} 条件存在未渲染占位符：{rendered}")
    return ResolvedPolicy(
        role=role, policy_name=policy_name, condition=rendered, claims=user_context
    )


def _literal(value: object) -> str:
    """把 claims 值渲染进策略模板（与 Guard 的 _escape_literal 同规则，重复校验无妨）。

    引号由模板负责；值只允许安全字符集，防注入（当前域值：无空格 branch / int tier）。
    """
    import re

    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    if not re.fullmatch(r"[A-Za-z0-9_\-.:]+", text):
        raise AuthError(f"claim 字面量含非法字符：{text!r}")
    return text
