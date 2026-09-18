"""B0/B2 · 接地数值校验器测试（ADR-0029 ④）——N1 命门的纯函数关卡。

契约：叙述里每个业务数字必须可追溯到确定性 payload（含格式化变体：千分位/定点/
百分/万·亿/显示级四舍五入）；出现 payload 里没有的新数字 → `grounded=false`。
定性/因果词不在硬校验内（诚实边界，见 §1 决策④）。payload 按**已序列化形态**喂
（`_jsonable` 后 Decimal → 数值字符串），与真实 TurnPayload 一致。
"""

from __future__ import annotations

from decimal import Decimal

from agent.narrative_guard import (
    build_allowed_numbers,
    extract_number_tokens,
    verify_grounded,
)

# 仿 /analyze 序列化后的 TurnPayload：Decimal 值为字符串，计数/耗时为整数
_PAYLOAD = {
    "kind": "answer",
    "row_count": 4,
    "latency_ms": 12,
    "columns": ["branch", "gmv"],
    "rows": [["East", "1234567.89"], ["West", "987654.32"]],
    "analysis": {
        "intent": "contribution",
        "baseline": {"granularity": "quarter", "value": "2013Q4"},
        "current": {"granularity": "quarter", "value": "2014Q1"},
        "totals": {"baseline": "5000000.00", "current": "6234567.89", "delta": "1234567.89"},
        "items": [
            {
                "value": "East",
                "baseline": "2000000.00",
                "current": "3234567.89",
                "delta": "1234567.89",
                "contribution_pct": "100.0",
            }
        ],
        "text": "确定性模板叙述",
        "elapsed_ms": 340,
    },
}


def test_allowed_collects_numeric_leaves_and_time_years() -> None:
    """ALLOW 收：数值叶（含数值字符串）+ 时间值里的 4 位年份；不收纯文本。"""
    allowed = build_allowed_numbers(_PAYLOAD)
    assert Decimal("1234567.89") in allowed  # rows Decimal 字符串
    assert Decimal("4") in allowed  # row_count
    assert Decimal("2013") in allowed  # baseline value "2013Q4" 的年份
    assert Decimal("2014") in allowed  # current value "2014Q1" 的年份


def test_exact_number_grounded() -> None:
    """逐字引用 payload 数字 → grounded。"""
    v = verify_grounded("East 分支合计 1234567.89 元。", _PAYLOAD)
    assert v.grounded is True
    assert v.violations == ()


def test_thousands_and_display_rounding_grounded() -> None:
    """千分位 + 显示级四舍五入（123.5万 / 1,234,568）都算引用同一值。"""
    assert verify_grounded("约 123.5 万元。", _PAYLOAD).grounded is True
    assert verify_grounded("达 1,234,568 元。", _PAYLOAD).grounded is True


def test_novel_number_rejected() -> None:
    """payload 里没有的新数字 → grounded=false，且违规 token 入 violations。"""
    v = verify_grounded("总计 9,999,999 元。", _PAYLOAD)
    assert v.grounded is False
    assert any("9999999" in t or "9,999,999" in t for t in v.violations)


def test_percent_fraction_variant_grounded() -> None:
    """贡献占比 100%（payload contribution_pct=100.0，亦含 1.0 分数形态）→ 放行。"""
    assert verify_grounded("贡献 100%。", _PAYLOAD).grounded is True


def test_qualitative_only_is_grounded() -> None:
    """纯定性/因果表达（无数字）恒 grounded——校验器只兜数字，不假装验因果。"""
    v = verify_grounded("增长主要由 East 分支驱动，表现亮眼。", _PAYLOAD)
    assert v.grounded is True


def test_year_in_payload_grounded_year_absent_rejected() -> None:
    """payload 含 2013/2014 → 可说；凭空 2099 → 拒。"""
    assert verify_grounded("对比 2013 与 2014。", _PAYLOAD).grounded is True
    assert verify_grounded("展望 2099。", _PAYLOAD).grounded is False


def test_model_name_digits_not_extracted() -> None:
    """模型名/版本号内数字与 sha 片段不参与事实校验（token 边界屏蔽相邻字母）。"""
    assert extract_number_tokens("model gpt-4o-mini sha 7c966e9") == []


def test_negative_and_zero_grounded() -> None:
    """负号与 0：payload 有 row_count=4 无 0；'0' 属新数字须拒，'-3.2' 须拒。"""
    assert verify_grounded("下降 0 项？", {"row_count": 4}).grounded is False
    assert verify_grounded("变化 -3.2。", {"delta": "-3.2"}).grounded is True
