"""接地数值校验器（ADR-0029 ④）：叙述中的每个业务数字必须可追溯到确定性 payload。

这是 N1（不编数字）红线的**客观关卡**——纯函数、无网络、可逐例单测。

规则
----
- `ALLOW` 只从**已算完的确定性 payload** 生成（数值叶 + 时间值里的 4 位年份）；
  LLM 无权扩充它。
- 从叙述文本抽数字 token（支持千分位、小数、负号、百分、中文万/亿倍数）；
  token 边界的**前后瞻只挡 ASCII 字母数字**（模型名 `gpt-4o`、sha 片段 `7c966e9`、
  `2013Q4` 等结构性串不参与事实校验），CJK 句读字符不阻断。
- 判决：token 归一为「值 + 其显示精度」后，若存在 ALLOW 中某值按该精度四舍五入
  （`ROUND_HALF_UP`）恰等于 token 值，则视为引用该值（容显示级舍入）；否则越界。
- 百分另存分数变体（`12.5%` → 12.5 与 0.125），兼容 payload 两种比率记法。

**诚实边界**：只兜「数字与新数字」；定性/因果表达无法客观验真，不在此校验（见 ADR-0029 ④）。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

# 数值字符串（可选符号 + 千分位/普通 + 可选小数）整串匹配，用于 payload 叶解析
_STRICT_NUM_RE = re.compile(r"^[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?$")
# 文本中的数字 token：前瞻非 ASCII 字母/数字/点/下划线，后瞻非 ASCII 字母/数字
_NUM_CORE = r"[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
_SUFFIX = r"(?:\s*[万亿])?\s*%?"
_TOKEN_RE = re.compile(rf"(?<![0-9A-Za-z._]){_NUM_CORE}{_SUFFIX}(?![0-9A-Za-z])")
# 从时间/文本串里抽 4 位年份（结构性数字，须在 payload 元数据中出现才可用）
_YEAR_RE = re.compile(r"(?:19|20)\d{2}")

_ONE = Decimal(1)


@dataclass(frozen=True)
class GroundedVerdict:
    """校验判决：`grounded` 为真表示全部数字可追溯；否则 `violations` 列出越界 token。"""

    grounded: bool
    violations: tuple[str, ...]


def _to_decimal(raw: str) -> Decimal | None:
    """把（可能带千分位的）数值字符串转 Decimal；非纯数值返回 None。"""
    s = raw.strip().replace(",", "")
    if not _STRICT_NUM_RE.match(raw.strip()):
        return None
    try:
        return Decimal(s)
    except ArithmeticError:  # Decimal 解析失败（理论不达，防御）
        return None


def _collect(node: object, allowed: set[Decimal]) -> None:
    """递归遍历 payload：数值叶入 ALLOW；字符串叶再抽年份。bool 排除（非业务数）。"""
    if isinstance(node, bool):  # bool 是 int 子类，必须先挡（否则 True→1）
        return
    if isinstance(node, int):
        allowed.add(Decimal(node))
    elif isinstance(node, float):
        allowed.add(Decimal(repr(node)))  # repr 保精度，避免二进制噪声
    elif isinstance(node, Decimal):
        allowed.add(node)
    elif isinstance(node, str):
        exact = _to_decimal(node)
        if exact is not None:
            allowed.add(exact)
        for year in _YEAR_RE.findall(node):
            allowed.add(Decimal(year))
    elif isinstance(node, Mapping):
        for value in node.values():
            _collect(value, allowed)
    elif isinstance(node, (list, tuple)):
        for value in node:
            _collect(value, allowed)


def build_allowed_numbers(payload: Mapping[str, object]) -> frozenset[Decimal]:
    """从确定性 payload 构造允许集（Decimal 规范化值）。纯函数。"""
    allowed: set[Decimal] = set()
    _collect(payload, allowed)
    return frozenset(allowed)


def extract_number_tokens(text: str) -> list[str]:
    """抽取叙述文本中的数字 token（含 万/亿/% 后缀）。模型名/sha 内数字被边界排除。"""
    return [m.group() for m in _TOKEN_RE.finditer(text)]


def _token_candidates(token: str) -> list[tuple[Decimal, Decimal]]:
    """token → [(绝对值, 显示精度)]。处理千分位、万/亿倍数、百分（含分数变体）。"""
    raw = token.strip()
    scale = _ONE
    if raw.endswith("万"):
        scale = Decimal(10000)
    elif raw.endswith("亿"):
        scale = Decimal(100000000)

    core = raw
    for suffix in ("万", "亿", "%"):
        core = core.replace(suffix, "")
    core = core.replace(" ", "").replace(",", "")
    if core in ("", "+", "-"):
        return []
    try:
        mantissa = Decimal(core)
    except ArithmeticError:
        return []

    exp = mantissa.as_tuple().exponent  # 小数位数（整数的 exponent 为 0/负；NaN 类为字面量）
    decimals = max(-exp, 0) if isinstance(exp, int) else 0
    precision = scale / (_ONE.scaleb(decimals))  # 该 token 能分辨的最小单位
    value = mantissa * scale

    candidates: list[tuple[Decimal, Decimal]] = [(value, precision)]
    if raw.endswith("%"):  # 百分亦按分数解释（兼容 payload 存 0-1 比率）
        candidates.append((value / Decimal(100), precision / Decimal(100)))
    return candidates


def _round_to(value: Decimal, precision: Decimal) -> Decimal:
    """把 value 舍入到 precision 的最近倍数（ROUND_HALF_UP，显示级舍入）。"""
    if precision <= 0:
        return value
    steps = (value / precision).to_integral_value(rounding=ROUND_HALF_UP)
    return steps * precision


def _is_grounded(token: str, allowed: frozenset[Decimal]) -> bool:
    """单个 token 是否可追溯到 ALLOW（任一候选值×精度命中即可）。"""
    for value, precision in _token_candidates(token):
        for a in allowed:
            if _round_to(a, precision) == value:
                return True
    return False


def verify_grounded(text: str, payload: Mapping[str, object]) -> GroundedVerdict:
    """校验叙述文本所有数字是否接地于 payload。空文本/无数字 → grounded=True。

    Parameters
    ----------
    text : 待发布的 LLM 叙述文本
    payload : 确定性 TurnPayload（已序列化形态）

    Returns
    -------
    GroundedVerdict：任一数字不可追溯 → grounded=False 且 violations 收集越界 token。
    """
    allowed = build_allowed_numbers(payload)
    violations = tuple(t for t in extract_number_tokens(text) if not _is_grounded(t, allowed))
    return GroundedVerdict(grounded=not violations, violations=violations)
