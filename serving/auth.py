"""JWT 签发/校验与角色 → 行级策略绑定（行级权限链路的 JWT 服务，Day 25 落地）。

HTTP 认证依赖（require_bearer / BearerClaims）也在此定义：业务面（serving/api.py）
与治理面（serving/governance.py）两个 router 共用同一份 401 语义（ADR-0022 决策 ①
的两个 router 若各写一份，401 文案与 WWW-Authenticate 头会漂移）。

二维事实源（ADR-0021 决策 ①）
----------------------------
- 域 → 策略：语义模型的 custom_extensions.policy.default_row_policy（ossie YAML）
- 策略 → 角色 → 条件：semantic/policies/row_policy.yml（Git 唯一事实源）
- 本模块 ROLE_DIRECTORY：角色注册 + claims 契约（**不含策略名**——策略由域决定）

口径（诚实声明）
--------------------
- 当前数据域是 TPC-DI 金融 dwd（数据已在 Doris 且绑定快照）：gold-146
  「按分支和客户等级统计 2015 年交易额 Top5」同一 SQL 下，hq_admin /
  branch_manager / broker / compliance_auditor 四角色注入不同谓词 → 结果不同。
- 零售域（rp_dept_visible）角色同构已注册；2026-09-04 TPC-DS SF0.1 零售
  数据落地后，region/product_category 未落地逻辑列对齐物理 dim_store.s_state /
  dim_item.i_category（P5），category_analyst 列表值经 sql_in 过滤器渲染，
  rls-verify 零售档实测（详见 eval/reports/rls-verify-<sha>.json）。
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
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import yaml
from fastapi import Depends, Header, HTTPException

# 相对仓库根：serving/auth.py → semantic/policies/row_policy.yml
_REPO_ROOT = Path(__file__).resolve().parent.parent
POLICY_PATH = _REPO_ROOT / "semantic" / "policies" / "row_policy.yml"

TOKEN_TTL_SECONDS = 3600  # 本地开发 token 有效期 1 小时

# 角色注册表：claims 契约 + 说明（**不含策略名**——域 → 策略见 ossie
# default_row_policy，ADR-0021 决策 ①；角色条件来自 semantic/policies/row_policy.yml
# （Git 唯一事实源），不在此复制）。required_claims / list_claims 与 YAML 条件模板
# 的占位符交叉校验（make lint，决策 ⑥）。


@dataclass(frozen=True)
class RoleSpec:
    """角色注册项：claims 契约 + 说明 + LLM 能力位。**不含策略名**——策略由域决定（决策 ①）。"""

    required_claims: tuple[str, ...]
    list_claims: frozenset[str] = frozenset()  # 需 sql_in 渲染的列表值键
    description: str = ""
    llm: frozenset[str] = frozenset()  # ADR-0029 ⑥：可请求的 LLM 意图集（取值同 LlmIntent）


# 角色 → LLM 能力映射（ADR-0029 ⑥「映射是配置」，用户 2026-09-18 定）：
# candidate-fallback=schema-only（低敏）全员；narrative=result-bearing（含结果数值、
# 强制自托管）收窄到分析/治理角色。branch_manager/broker 仅候选路由、无叙述。
_ROUTE = frozenset({"candidate-fallback"})
_ROUTE_NARRATE = frozenset({"candidate-fallback", "narrative"})

ROLE_DIRECTORY: dict[str, RoleSpec] = {
    # 金融域（rp_branch_visible）：gold-146 问句可注入
    "hq_admin": RoleSpec(
        (), frozenset(), "总部管理员：全量可见（条件 1=1，两域共用）", _ROUTE_NARRATE
    ),
    "branch_manager": RoleSpec(("branch",), frozenset(), "分支经理：仅见本分支", _ROUTE),
    "broker": RoleSpec(("brokerid",), frozenset(), "经纪人：仅见本人 brokerid 名下", _ROUTE),
    "compliance_auditor": RoleSpec(
        ("max_tier",), frozenset(), "合规审计：仅见 tier ≤ 上限的客户档", _ROUTE
    ),
    # 零售域（rp_dept_visible）：region/product_category 逻辑列已对齐物理
    # （dim_store.s_state / dim_item.i_category，2026-09-04 P5 实测，见
    # eval/reports/rls-verify-<sha>.json）；claims：region + categories（sql_in）
    "region_manager": RoleSpec(
        ("region",), frozenset(), "大区（州）经理：仅见本州门店", _ROUTE_NARRATE
    ),
    "category_analyst": RoleSpec(
        ("region", "categories"),
        frozenset({"categories"}),
        "品类分析师：本州 + 指定品类集",
        _ROUTE_NARRATE,
    ),
}


def caps_for_role(role: str) -> frozenset[str]:
    """角色 → 可请求的 LLM 意图集（ADR-0029 ⑥）。未注册角色 → 空集（拒绝一切 LLM 意图）。"""
    spec = ROLE_DIRECTORY.get(role)
    return spec.llm if spec is not None else frozenset()


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

    签发期即校验 claims 契约（ADR-0021 决策 ④）：缺 required_claims 任何键、
    或 list_claims 键的值不是非空列表 → AuthError——把「注定解析失败的 token」
    拦在签发时（前端角色切换器据此渲染表单，ADR-0018）。

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
    AuthError : role 未注册 / 缺必需 claims 键 / 列表值形态错 / 密钥缺失
    """
    spec = ROLE_DIRECTORY.get(role)
    if spec is None:
        raise AuthError(f"角色未注册：{role!r}（ROLE_DIRECTORY 可加）")
    missing = [key for key in spec.required_claims if key not in user_context]
    if missing:
        raise AuthError(f"角色 {role!r} 缺少必需 claims 键：{missing}（签发期拒绝）")
    for key in spec.list_claims:
        value = user_context.get(key)
        if not isinstance(value, list | tuple) or not value:
            raise AuthError(f"角色 {role!r} 的 claims {key!r} 需为非空列表：{value!r}")
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
    """校验 HS256 本地 JWT 并返回 claims。

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


# ---------------------------------------------------------------------------
# FastAPI 认证依赖（serving/api.py 业务面与 serving/governance.py 治理面共用，
# ADR-0022 决策 ①：认证语义单源，两个 router 只装配不复制）
# ---------------------------------------------------------------------------


def require_bearer(authorization: str | None = Header(default=None)) -> dict[str, object]:
    """Bearer JWT → claims。失败一律 401，不外泄校验细节（0011 口径）。"""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401, detail="缺少 Bearer 访问令牌", headers={"WWW-Authenticate": "Bearer"}
        )
    token = authorization.removeprefix("Bearer ").strip()
    try:
        return verify_token(token)
    except AuthError as exc:
        raise HTTPException(status_code=401, detail="访问令牌无效或已过期") from exc


BearerClaims = Annotated[dict[str, object], Depends(require_bearer)]


def claims_fingerprint(claims: dict[str, object]) -> str:
    """已验证 claims → 会话身份指纹（sha256 摘要，跨进程稳定）。

    用途（ADR-0020 决策 ⑥）：会话首轮把指纹写进 checkpoint，后续轮比对——
    同会话换身份（含重签 token）必须换 session_id。**全量 claims 参与**（含
    iat/exp）：重签 token 即新身份，口径偏严不偏松（与原进程内实现一致）。

    为什么是哈希而不是 claims 本体：指纹会落进 checkpoint 文件（磁盘/卷），
    存本体等于把身份细节留档（ADR-0011 不外泄）；校验只需要相等性，不需要原值。

    为什么固定 canonical JSON（sort_keys + 紧凑分隔符）：同一 claims 必须
    在任何进程/重启后得到同一摘要——序列化形态一变，老会话在重启后全成假冲突。
    """
    canonical = json.dumps(claims, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 外部 IdP 验证（RS256 / JWKS，生产部署钩子；本地 HS256 自签发仅开发用）
# ---------------------------------------------------------------------------


def _b64url_to_int(text: str) -> int:
    return int.from_bytes(_b64url_decode(text), "big")


def _jwk_rsa_public(jwk: dict[str, object]):
    """JWK（kty=RSA，含 n/e）→ RSA 公钥（lazy import cryptography）。"""
    try:
        from cryptography.hazmat.primitives.asymmetric import rsa
    except Exception as exc:  # noqa: BLE001 - 可选依赖，缺失即明确报错
        raise AuthError(
            "cryptography 未安装：外部 IdP（RS256/JWKS）验证需要它（uv sync --extra idp）"
        ) from exc
    if jwk.get("kty") != "RSA":
        raise AuthError(f"仅支持 RSA JWK，收到 kty={jwk.get('kty')!r}")
    n = _b64url_to_int(str(jwk["n"]))
    e = _b64url_to_int(str(jwk["e"]))
    return rsa.RSAPublicNumbers(e, n).public_key()


def verify_token_external(
    token: str,
    jwk: dict[str, object],
    *,
    audience: str | None = None,
    issuer: str | None = None,
) -> dict[str, object]:
    """用外部 IdP 的 RSA 公钥（JWK）验证 RS256 JWT，返回 claims。

    校验链：header.alg == RS256 → 签名（PKCS1v15 + SHA256）→ exp →
    aud（若配置）→ iss（若配置）→ role 注册。与本地 HS256 路径同口径，
    仅密钥来源不同（受管 IdP 而非 ATLAS_JWT_SECRET）。

    Raises
    ------
    AuthError : 任何校验失败
    """
    parts = token.split(".")
    if len(parts) != 3:
        raise AuthError("JWT 格式错误：应为三段 base64url")
    header: dict[str, object] = json.loads(_b64url_decode(parts[0]))
    if header.get("alg") != "RS256":
        raise AuthError(f"外部 IdP 仅支持 RS256，收到 alg={header.get('alg')!r}")
    signing_input = f"{parts[0]}.{parts[1]}".encode()
    sig = _b64url_decode(parts[2])
    public_key = _jwk_rsa_public(jwk)
    try:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        public_key.verify(sig, signing_input, padding.PKCS1v15(), hashes.SHA256())
    except Exception as exc:  # noqa: BLE001 - 签名不符/算法错误统一为 AuthError
        raise AuthError(f"RS256 签名校验失败：{exc}") from exc
    payload = json.loads(_b64url_decode(parts[1]))
    now = int(time.time())
    exp = payload.get("exp", 0)
    if not isinstance(exp, int) or exp <= now:
        raise AuthError("JWT 已过期")
    if audience is not None and payload.get("aud") != audience:
        raise AuthError(f"JWT audience 不符：{payload.get('aud')!r} != {audience!r}")
    if issuer is not None and payload.get("iss") != issuer:
        raise AuthError(f"JWT issuer 不符：{payload.get('iss')!r} != {issuer!r}")
    if payload.get("role") not in ROLE_DIRECTORY:
        raise AuthError(f"JWT role 未注册：{payload.get('role')!r}")
    return payload


class JwksResolver:
    """从 IdP 的 JWKS 端点按 kid 解析 RSA 公钥（lazy 拉取 + 内存缓存）。

    用法::
        resolver = JwksResolver("https://idp.example.com/.well-known/jwks.json")
        claims = verify_token_external(token, resolver.get_kid(kid), audience=AUD, issuer=ISS)
    """

    def __init__(self, jwks_url: str) -> None:
        self._url = jwks_url
        self._cache: dict[str, dict[str, object]] = {}

    def _fetch(self) -> dict[str, object]:
        import urllib.request

        with urllib.request.urlopen(self._url, timeout=10) as resp:  # noqa: S310 - 受管 IdP 端点
            return json.loads(resp.read().decode("utf-8"))

    def get_kid(self, kid: str) -> dict[str, object]:
        if kid not in self._cache:
            doc = self._fetch()
            keys = doc.get("keys", [])
            match = next((k for k in keys if k.get("kid") == kid), None)
            if match is None:
                raise AuthError(f"JWKS 中找不到 kid={kid!r}")
            self._cache[kid] = match
        return self._cache[kid]


@dataclass(frozen=True)
class ResolvedPolicy:
    """角色解析结果：可直接交给 Guard 注入的行级策略。"""

    role: str
    policy_name: str
    condition: str
    claims: dict[str, object]

    def describe(self) -> str:
        return f"[{self.role}] {self.policy_name}: {self.condition}"


def resolve_claims(
    claims: dict[str, object],
    *,
    policy_name: str,
    policy_path: Path = POLICY_PATH,
) -> ResolvedPolicy:
    """已验证 claims → (策略名, 已渲染谓词)：策略解析的纯函数内核。

    policy_name 由调用方按当前语义模型传入（`model.default_row_policy`——
    域 → 策略的唯一事实源在 ossie YAML，ADR-0021 决策 ①③；不再从
    ROLE_DIRECTORY 推断）。resolve_policy(token, policy_name=...) 在
    verify_token 之后委托本函数；DataAgent 身份注入（graph.ask identity）
    与 rls-verify/demo 同走本内核——角色条件来自 row_policy.yml + claims
    （Git 唯一事实源，不在此复制）。

    claims 须已过签名/有效期校验（verify_token 输出形态：role + user_context）；
    本函数只做角色注册查表、策略内角色存在性判定与占位符渲染。渲染在服务端做
    （Guard 的 Policy.render 同规则：{{ user.<key> }} 占位符，非法字面量由
    Guard 兜底拒绝，这里不做二次实现）。
    """
    role = claims.get("role")
    if not isinstance(role, str) or role not in ROLE_DIRECTORY:
        raise AuthError(f"claims role 未注册：{role!r}（ROLE_DIRECTORY 可加）")
    user_context = claims.get("user_context")
    if not isinstance(user_context, dict):
        raise AuthError("JWT 缺少 user_context")
    doc = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
    policy = next((p for p in doc["policies"] if p["name"] == policy_name), None)
    if policy is None:
        raise AuthError(f"策略不存在：{policy_name!r}（{policy_path}）")
    condition: str | None = None
    for r in policy["roles"]:
        if r["name"] == role:
            condition = r["condition"]
            break
    if condition is None:
        # 「用零售角色查金融域」由静默按一维表解析 → 显式拒绝（ADR-0021 决策 ③）
        raise AuthError(f"角色 {role!r} 不属于策略 {policy_name!r}（域不匹配）")
    rendered = condition
    for key, value in user_context.items():
        if isinstance(value, list | tuple):
            continue  # 列表值仅由下方 sql_in 过滤器渲染（_literal 拒非标量）
        rendered = rendered.replace(f"{{{{ user.{key} }}}}", _literal(value))
    # sql_in 过滤器：列表值 → ('a','b')（单项仍过 _literal 安全校验后包引号，
    # 不引入引号逃逸；值须为非空列表，防注入与空集语义陷阱）
    for match in re.finditer(r"\{\{ user\.([A-Za-z_][A-Za-z0-9_]*) \| sql_in \}\}", rendered):
        key = match.group(1)
        values = user_context.get(key)
        if not isinstance(values, list | tuple) or not values:
            raise AuthError(f"claim {key!r} 需为非空列表（sql_in 渲染）：{values!r}")
        rendered = rendered.replace(match.group(0), ", ".join(f"'{_literal(v)}'" for v in values))
    if "{{" in rendered:
        raise AuthError(f"角色 {role!r} 条件存在未渲染占位符：{rendered}")
    return ResolvedPolicy(
        role=role, policy_name=policy_name, condition=rendered, claims=user_context
    )


def resolve_policy(
    token: str,
    *,
    policy_name: str,
    secret: str | None = None,
    policy_path: Path = POLICY_PATH,
) -> ResolvedPolicy:
    """token → (策略名, 已渲染谓词)：verify_token 后委托 resolve_claims。

    policy_name 与 resolve_claims 同源（调用方按语义模型传入，ADR-0021
    决策 ③——签名破坏性变更，与 resolve_claims 同一提交内改完）。
    渲染在服务端做（Guard 的 Policy.render 同规则：{{ user.<key> }} 占位符，
    非法字面量由 Guard 兜底拒绝，这里不做二次实现）。
    """
    claims = verify_token(token, secret=secret)
    return resolve_claims(claims, policy_name=policy_name, policy_path=policy_path)


def roles_for_policy(
    policy_name: str,
    *,
    policy_path: Path = POLICY_PATH,
) -> tuple[str, ...]:
    """策略名 → 该策略下**已注册**的角色（按 row_policy.yml 声明顺序）。

    供前端角色切换器（ADR-0018）与帮助文本按域过滤（ADR-0021 决策 ⑤）；
    未注册角色不返回（该债务由 make lint 决策 ⑥ 拦截，本函数只暴露
    可签发的角色集合）。
    """
    doc = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
    for policy in doc["policies"]:
        if policy["name"] == policy_name:
            return tuple(r["name"] for r in policy["roles"] if r["name"] in ROLE_DIRECTORY)
    raise AuthError(f"策略不存在：{policy_name!r}（{policy_path}）")


def _literal(value: object) -> str:
    """把 claims 值渲染进策略模板（与 Guard 的 _escape_literal 同规则，重复校验无妨）。

    引号由模板负责；值只允许安全字符集，防注入（当前域值：无空格 branch / int tier）。
    """
    import re

    if isinstance(value, int | float):
        return str(value)
    text = str(value)
    if not re.fullmatch(r"[A-Za-z0-9_\-.:]+", text):
        raise AuthError(f"claim 字面量含非法字符：{text!r}")
    return text
