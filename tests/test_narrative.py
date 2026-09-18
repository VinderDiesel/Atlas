"""B2 · 叙述模块测试（ADR-0029 ③④）——所有失败路径必须回落确定性模板（N1）。"""

from __future__ import annotations

from agent.llm_policy import BackendDecision, LlmConfig, resolve_llm_backend
from agent.narrative import NarrativeResult, synthesize_narrative

_PAYLOAD = {
    "kind": "answer",
    "metric": "gmv",
    "analysis": {
        "totals": {"baseline": "5000000.00", "current": "6234567.89", "delta": "1234567.89"},
        "text": "确定性模板：两期差额 1234567.89。",
    },
}
_CAPS = frozenset({"candidate-fallback", "narrative"})
_CFG = LlmConfig.with_self_hosted(base_url="http://vllm:8000/v1", model_name="atlas-instruct")


class _FakeClient:
    """可编程 fake：返回预置文本或抛异常，并记录是否被调用（零网络）。"""

    def __init__(self, *, text: str = "", raises: bool = False) -> None:
        self._text = text
        self._raises = raises
        self.called = 0

    def complete(self, system: str, user: str, model: str) -> tuple[str, dict[str, int]]:
        self.called += 1
        if self._raises:
            raise RuntimeError("endpoint down")
        return self._text, {"prompt_tokens": 10, "completion_tokens": 5}


def _self_hosted_decision() -> BackendDecision:
    return resolve_llm_backend("narrative", _CAPS, _CFG, self_hosted_ok=True)


def test_grounded_llm_text_shipped() -> None:
    """LLM 引用 payload 既有数字（含格式化）→ grounded 发货，带 model/tier 溯源。"""
    client = _FakeClient(text="East 增长约 123.5 万元。")
    r = synthesize_narrative(_PAYLOAD, _self_hosted_decision(), client=client)
    assert isinstance(r, NarrativeResult)
    assert r.grounded is True and r.fallback is False
    assert r.tier == "self_hosted"
    assert r.model == "atlas-instruct"
    assert client.called == 1


def test_novel_number_falls_back_to_template() -> None:
    """LLM 冒出 payload 外的新数字 → 丢 LLM 文本、发确定性模板（N1 硬闸）。"""
    client = _FakeClient(text="总额高达 99 亿元。")  # 99亿 不在 payload
    r = synthesize_narrative(_PAYLOAD, _self_hosted_decision(), client=client)
    assert r.grounded is False and r.fallback is True
    assert r.tier == "none"
    assert r.reason_code == "ungrounded"
    assert r.text == _PAYLOAD["analysis"]["text"]  # 发的是模板
    assert r.violations  # 越界 token 记入审计


def test_endpoint_error_falls_back() -> None:
    """端点异常 → 回落模板，reason=endpoint_error（绝不升级云、不整链报错）。"""
    client = _FakeClient(raises=True)
    r = synthesize_narrative(_PAYLOAD, _self_hosted_decision(), client=client)
    assert r.fallback is True and r.reason_code == "endpoint_error"
    assert r.text == _PAYLOAD["analysis"]["text"]


def test_decision_fallback_never_calls_client() -> None:
    """策略已判 FALLBACK_TEMPLATE（缺自托管）→ 直接回落，一次都不碰客户端。"""
    down = resolve_llm_backend("narrative", _CAPS, _CFG, self_hosted_ok=False)
    client = _FakeClient(text="不该被用到")
    r = synthesize_narrative(_PAYLOAD, down, client=client)
    assert r.fallback is True and r.reason_code == "self_hosted_unavailable"
    assert client.called == 0


def test_to_payload_shape_has_provenance() -> None:
    """响应块稳定含 text/model/tier/grounded/fallback/reason_code。"""
    client = _FakeClient(text="差额 1234567.89。")
    r = synthesize_narrative(_PAYLOAD, _self_hosted_decision(), client=client)
    block = r.to_payload()
    assert set(block) == {"text", "model", "tier", "grounded", "fallback", "reason_code"}
    assert block["grounded"] is True
