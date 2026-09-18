"""接地叙述模块（ADR-0029 ③④）：确定性 TurnPayload → 受限 LLM 叙述 → 数字校验 → 回落。

不变量（守 N1/N3/②/③）
----------------------
- **只在确定性结果算完后运行**：输入是冻结的 TurnPayload，本模块不查询、不产 SQL、
  不参与执行（N3：叙述不新增任何执行通道）。
- **数字硬闸**：LLM 文本先过 `verify_grounded`；出现 payload 里没有的新数字 →
  丢弃 LLM 文本、回落确定性模板（`analysis.text`），`grounded=false` 文本**永不发货**。
- **fail-closed**：决策为 `FALLBACK_TEMPLATE`、端点调用失败、非 JSON、超时——一律回落
  模板，绝不改道云（后端由 `resolve_llm_backend` 钉死为自托管）。
- **因果软约束**：定性/因果表达无法客观验真，靠提示词约束 + 响应 `grounded/model/tier`
  标签让调用方知情（诚实边界，见 ADR-0029 ④）。

后端线格式统一 OpenAI 兼容 `chat/completions`，只用可移植子集（①）。
"""

from __future__ import annotations

import json
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import yaml

from agent.llm_policy import BackendDecision, LlmAction
from agent.narrative_guard import verify_grounded

PROMPT_FILE = Path(__file__).resolve().parent / "prompts" / "narrative.yaml"
_MAX_TOKENS = 400  # 一到三句叙述足够；有界输出（配合可移植子集，①）
_TIMEOUT_S = 30  # 叙述非关键路径：端点慢/挂 → 超时回落模板，不拖垮确定性结果


class ChatClient(Protocol):
    """OpenAI 兼容补全客户端协议（测试注入 fake，生产用 `OpenAICompatClient`）。"""

    def complete(self, system: str, user: str, model: str) -> tuple[str, dict[str, int]]:
        """返回 (文本, usage)。失败/超时须抛异常，由上层回落（不静默返回空）。"""
        ...


@dataclass
class NarrativeResult:
    """一次叙述的结果；`fallback=True` 表示发的是确定性模板而非 LLM 文本。"""

    text: str
    grounded: bool
    fallback: bool
    model: str = ""
    tier: str = "none"  # cloud|self_hosted|none（none=模板回落）
    reason_code: str | None = None
    usage: dict[str, int] = field(default_factory=dict)
    violations: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, Any]:
        """响应 `narrative` 块（字段稳定；`grounded` 只对 LLM 文本为真）。"""
        return {
            "text": self.text,
            "model": self.model,
            "tier": self.tier,
            "grounded": self.grounded,
            "fallback": self.fallback,
            "reason_code": self.reason_code,
        }


class OpenAICompatClient:
    """OpenAI 兼容 `chat/completions` 客户端——仅可移植子集（①，禁 response_format 等）。"""

    def __init__(self, base_url: str, api_key: str = "") -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    def complete(self, system: str, user: str, model: str) -> tuple[str, dict[str, int]]:
        """同步补全；非 2xx / 结构异常抛 `RuntimeError`（上层回落模板）。"""
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.0,  # 叙述亦零温采样：可复现、不炫技（确定性优先）
            "max_tokens": _MAX_TOKENS,
        }
        headers = {"Content-Type": "application/json"}
        if self.api_key:  # N9：密钥走请求头，绝不入 query
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        started = time.perf_counter()
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:  # noqa: S310 - 受管端点
            body: dict[str, Any] = json.loads(resp.read().decode("utf-8"))
        content = str(body["choices"][0]["message"]["content"])
        u = body.get("usage", {})
        usage: dict[str, int] = {
            "prompt_tokens": int(u.get("prompt_tokens", 0)),
            "completion_tokens": int(u.get("completion_tokens", 0)),
            "latency_ms": int(round((time.perf_counter() - started) * 1000)),
        }
        return content, usage


def _load_prompt() -> dict[str, str]:
    doc = yaml.safe_load(PROMPT_FILE.read_text(encoding="utf-8"))
    return {"system": str(doc["system_template"]), "user": str(doc["user_template"])}


def _facts_block(payload: dict[str, Any]) -> str:
    """把确定性事实序列化为提示词事实块——**唯一数字来源**（其数字必属 ALLOW）。

    有分析产物时只喂 `analysis`（17 键，含 totals/items/text）以束紧 token；否则退化
    喂核心结果字段。不含 sql/密钥等无关项。
    """
    analysis = payload.get("analysis")
    source = analysis if analysis else {k: payload.get(k) for k in ("metric", "columns", "rows")}
    return json.dumps(source, ensure_ascii=False, default=str)


def default_template(payload: dict[str, Any]) -> str:
    """确定性回落文本：优先分析自带的 `analysis.text`（0026 已算），否则空串。"""
    analysis = payload.get("analysis") or {}
    text = analysis.get("text") if isinstance(analysis, dict) else None
    return str(text) if text else ""


def _fallback(
    payload: dict[str, Any],
    template: Any,
    reason_code: str,
    violations: tuple[str, ...] = (),
    usage: dict[str, int] | None = None,
) -> NarrativeResult:
    text = template(payload)
    return NarrativeResult(
        text=text,
        grounded=False,
        fallback=True,
        model="",
        tier="none",
        reason_code=reason_code,
        violations=violations,
        # 成本不因回落而消失：LLM 已跑的 token（如 ungrounded）据实携带（ADR-0029 ⑦）。
        usage=usage or {},
    )


def synthesize_narrative(
    payload: dict[str, Any],
    decision: BackendDecision,
    *,
    client: ChatClient,
    template: Any = default_template,
) -> NarrativeResult:
    """按策略决策生成叙述（③④）。纯编排：所有失败路径都回落确定性模板。

    Parameters
    ----------
    payload : 确定性 TurnPayload（已序列化形态；数字来源 + 校验 ALLOW 来源）
    decision : `resolve_llm_backend` 的输出（USE_BACKEND 用其 endpoint；FALLBACK_TEMPLATE 直接回落）
    client : OpenAI 兼容补全客户端（测试注入 fake）
    template : 回落用的确定性模板函数（默认取 `analysis.text`）

    Returns
    -------
    NarrativeResult；`grounded=True` 仅当 LLM 文本全部数字可追溯。
    """
    if decision.action is LlmAction.FALLBACK_TEMPLATE:
        return _fallback(payload, template, decision.reason_code or "self_hosted_unavailable")
    if decision.endpoint is None:  # 理论不达（USE_BACKEND 必带 endpoint）；防御性回落
        return _fallback(payload, template, "llm_not_configured")

    prompt = _load_prompt()
    user = prompt["user"].replace("__FACTS__", _facts_block(payload))
    try:
        text, usage = client.complete(prompt["system"], user, decision.endpoint.model_name)
    except Exception:  # noqa: BLE001 - 端点失败/超时/结构异常 → 回落模板，绝不升级云（③）
        return _fallback(payload, template, "endpoint_error")

    verdict = verify_grounded(text, payload)
    if not verdict.grounded:  # 出现 payload 外的新数字 → 丢 LLM 文本发模板（N1）
        # 文本虽被丢弃，但模型确已消耗 token——成本据实携带，不因回落而漏计（⑦）。
        return _fallback(payload, template, "ungrounded", verdict.violations, usage=usage)

    return NarrativeResult(
        text=text.strip(),
        grounded=True,
        fallback=False,
        model=decision.endpoint.model_name,
        tier=decision.tier,
        usage=usage,
    )
