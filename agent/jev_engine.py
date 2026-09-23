"""Jev System One 决策引擎接入（ADR-0030）：判别式后端 + 置信度透传 + 确定性兜底。

定位（与 ADR-0029/0030 对齐）
------------------------------
Jev 是 TypeSafe AI 的 System One 型模型：**不生成文本**，只对预设判别项输出
强类型决策原语（Choice 选项+概率 / Score 标定打分 / Noul 陈述为真 0~1 概率）
并附**校准置信度**。它的适用域是「答案范围已知的判断」，不是生成。

三条不可协商的约束（ADR-0030 ②③④）
-----------------------------------
1. **数据出境**：Jev 当前仅有托管 API（无本地部署、未公开权重），因此按
   `Backend.JEV` 对待并继承云的全部出境约束——**只承载 schema-only 判断**
   （prompt 内无业务结果数值）。`narrative`（result-bearing）由
   `resolve_llm_backend` 硬钉在自托管 chat_completions，**绝不路由到本模块**。
2. **fail-closed**：端点失败/超时/非 JSON/未知原语 → **抛异常**，由调用点回落
   其原有确定性逻辑（承 ADR-0029 ③ 心智；本模块不返回半成品结果）。
3. **置信度不得改造**：校准是 Jev 的核心价值，本模块原样透传，不加权、不缩放、
   不二次归一。调用方按阈值分流（高→静默采用，中→保留原逻辑，低→放弃）。

与 `narrative.ChatClient` 的关系
--------------------------------
**不共用协议**。`ChatClient.complete(system, user, model)` 是 chat 语义；
Jev 是 `decide(state, questions, model)` 的判别语义，输出结构完全不同
（ADR-0030 ①：引擎类型与后端形态正交，不为统一而统一）。

用法::

    client = HttpSystemOneClient("https://api.example.com", api_key=...)
    engine = JevEngine(client)
    decision = engine.choose(
        state="问句：今年总成交金额是多少",
        question="metric",
        options=("total_trade_value", "avg_trade_value"),
    )
    if decision and decision.confidence >= 0.9:
        ...  # 采用 decision.choice
    else:
        ...  # 退回 MetaReranker 的确定性词典序

本模块**不涉及任何 SQL 生成或执行**（N3）；不产出任何未实测数字（N1）。
"""

from __future__ import annotations

import json
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Protocol

# 判别原语（ADR-0030 ③）：Jev 只回这三类结构，未知原语视为端点契约破裂。
PRIMITIVE_CHOICE = "choice"
PRIMITIVE_SCORE = "score"
PRIMITIVE_NOUL = "noul"
_KNOWN_PRIMITIVES = frozenset({PRIMITIVE_CHOICE, PRIMITIVE_SCORE, PRIMITIVE_NOUL})

_TIMEOUT_S = 10  # 判别是「快判断」：慢于此与其等，不如回落确定性逻辑
_MAX_OPTIONS = 255  # Jev 单选择字段基数上限（官方口径）；超出须先打分再选


@dataclass(frozen=True)
class JevDecision:
    """一次判别结果的不可变快照（置信度原样透传，ADR-0030 ③）。

    字段按原语取用：
    - Choice → `choice` + `probs`（各选项概率）+ `confidence`（选中项概率）
    - Score  → `scores`（标定打分）+ `confidence`
    - Noul   → `probability`（陈述为真的 0~1）+ `confidence`

    `confidence` 的语义是「模型有多确定」，**不是「答案有多大概率正确」**
    （ADR-0029/0030 共同口径）——不得当作准确率使用或报告。
    """

    primitive: str
    confidence: float
    choice: str | None = None
    probability: float | None = None
    probs: dict[str, float] = field(default_factory=dict)
    scores: dict[str, float] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)  # 端点原文（审计留痕；不含密钥）

    def __post_init__(self) -> None:
        # 置信度落在 [0,1]：越界即端点契约破裂，宁可显式失败也不让脏值流下去。
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"Jev 置信度越界 [0,1]：{self.confidence!r}（端点契约破裂）")


class SystemOneClient(Protocol):
    """System One 判别客户端协议（测试注入 fake，生产用 `HttpSystemOneClient`）。"""

    def decide(
        self, state: str, questions: dict[str, Any], model: str
    ) -> tuple[dict[str, Any], dict[str, int]]:
        """返回 (decisions, usage)。失败/超时须抛异常，由上层回落（不静默返回空）。"""
        ...


class HttpSystemOneClient:
    """Jev 托管端点客户端：`POST {base_url}/v1/systemone`（ADR-0030 ③）。

    仅用可移植子集字段 `model` / `state` / `questions`（承 ADR-0029 ① 精神：
    换线格式不等于放弃可移植子集纪律）。响应只读 `decisions` 与 `usage`。
    """

    def __init__(self, base_url: str, api_key: str = "") -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    def decide(
        self, state: str, questions: dict[str, Any], model: str
    ) -> tuple[dict[str, Any], dict[str, int]]:
        """同步判别；非 2xx / 结构异常抛 `RuntimeError`（上层回落确定性逻辑）。"""
        payload = {"model": model, "state": state, "questions": questions}
        headers = {"Content-Type": "application/json"}
        if self.api_key:  # N9：密钥走请求头，绝不入 query 或日志
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(
            f"{self.base_url}/v1/systemone",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            started = time.perf_counter()
            with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:  # noqa: S310 - 受管端点
                body: dict[str, Any] = json.loads(resp.read().decode("utf-8"))
            latency_ms = int(round((time.perf_counter() - started) * 1000))
        except Exception as exc:  # noqa: BLE001 - 网络/超时/非 JSON 一律显式失败
            raise RuntimeError(f"Jev 端点调用失败：{type(exc).__name__}") from exc

        decisions = body.get("decisions")
        if not isinstance(decisions, dict):
            raise RuntimeError("Jev 响应缺 decisions 块（端点契约破裂）")
        u = body.get("usage", {})
        usage: dict[str, int] = {
            "prompt_tokens": int(u.get("prompt_tokens", 0)),
            "completion_tokens": int(u.get("completion_tokens", 0)),
            "latency_ms": latency_ms,
        }
        return decisions, usage


def _as_float(value: Any, field_name: str) -> float:
    """严格转 float：bool 与非数值一律拒绝（避免 True→1.0 静默污染置信度）。"""
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"Jev 字段 {field_name} 非数值：{value!r}")
    return float(value)


def _parse_probs(raw: Any) -> dict[str, float]:
    """解析选项概率表；空表合法（端点可不回全分布）。"""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(f"Jev probs 非映射：{raw!r}")
    return {str(k): _as_float(v, f"probs[{k}]") for k, v in raw.items()}


class JevEngine:
    """把 Jev 的 Choice/Score/Noul 收敛为 Atlas 可消费的判别结果（ADR-0030 ③）。

    纯编排：不做重试、不做降级改写、不碰确定性逻辑——所有失败向上抛，
    由调用点决定回落（保持「Jev 只建议、原逻辑兜底」的可切换语义）。
    """

    def __init__(self, client: SystemOneClient, model: str) -> None:
        self._client = client
        self._model = model

    @property
    def model(self) -> str:
        return self._model

    def choose(self, state: str, question: str, options: tuple[str, ...]) -> JevDecision:
        """Choice 判别：从 `options` 中选一个（返回选中项 + 全分布 + 置信度）。

        Parameters
        ----------
        state : 非结构化上下文（**schema-only**：禁含业务结果数值，ADR-0030 ②）
        question : 判别项名（如 "metric"）
        options : 候选选项（基数上限 255，超出抛 `ValueError`）

        Raises
        ------
        ValueError
            选项数越界 / 端点返回的原语与请求不符 / 字段形态非法。
        RuntimeError
            端点调用失败（上层据此回落确定性逻辑）。
        """
        if not options:
            raise ValueError("Jev Choice 至少需要一个选项")
        if len(options) > _MAX_OPTIONS:
            raise ValueError(
                f"Jev 单选择字段基数上限 {_MAX_OPTIONS}，收到 {len(options)}（ADR-0030 ③）"
            )
        questions = {question: {"type": PRIMITIVE_CHOICE, "options": list(options)}}
        decisions, _ = self._client.decide(state, questions, self._model)

        node = decisions.get(question)
        if not isinstance(node, dict):
            raise ValueError(f"Jev 响应缺判别项 {question!r}（端点契约破裂）")
        primitive = str(node.get("type", ""))
        if primitive != PRIMITIVE_CHOICE:
            raise ValueError(f"Jev 原语不符：请求 {PRIMITIVE_CHOICE}，收到 {primitive!r}")

        choice = node.get("choice")
        if choice is not None and str(choice) not in options:
            # 越界选项 = 端点契约破裂。绝不静默接受（否则下游拿到未知指标名）。
            raise ValueError(f"Jev 返回越界选项 {choice!r}（不在请求候选中）")
        probs = _parse_probs(node.get("probs"))
        confidence = _as_float(node.get("confidence"), "confidence")
        return JevDecision(
            primitive=PRIMITIVE_CHOICE,
            confidence=confidence,
            choice=str(choice) if choice is not None else None,
            probs=probs,
            raw=node,
        )

    def score(self, state: str, question: str, levels: tuple[str, ...]) -> JevDecision:
        """Score 判别：按给定等级打分（返回各等级分 + 置信度）。"""
        if not levels:
            raise ValueError("Jev Score 至少需要一个等级")
        questions = {question: {"type": PRIMITIVE_SCORE, "levels": list(levels)}}
        decisions, _ = self._client.decide(state, questions, self._model)

        node = decisions.get(question)
        if not isinstance(node, dict):
            raise ValueError(f"Jev 响应缺判别项 {question!r}（端点契约破裂）")
        primitive = str(node.get("type", ""))
        if primitive != PRIMITIVE_SCORE:
            raise ValueError(f"Jev 原语不符：请求 {PRIMITIVE_SCORE}，收到 {primitive!r}")

        return JevDecision(
            primitive=PRIMITIVE_SCORE,
            confidence=_as_float(node.get("confidence"), "confidence"),
            scores=_parse_probs(node.get("scores")),
            raw=node,
        )

    def noul(self, state: str, statement: str) -> JevDecision:
        """Noul 判别：判断一项陈述为真的概率（0~1）。"""
        question = "statement"
        questions = {question: {"type": PRIMITIVE_NOUL, "statement": statement}}
        decisions, _ = self._client.decide(state, questions, self._model)

        node = decisions.get(question)
        if not isinstance(node, dict):
            raise ValueError(f"Jev 响应缺判别项 {question!r}（端点契约破裂）")
        primitive = str(node.get("type", ""))
        if primitive != PRIMITIVE_NOUL:
            raise ValueError(f"Jev 原语不符：请求 {PRIMITIVE_NOUL}，收到 {primitive!r}")

        probability = _as_float(node.get("probability"), "probability")
        if not 0.0 <= probability <= 1.0:
            raise ValueError(f"Jev Noul 概率越界 [0,1]：{probability!r}")
        return JevDecision(
            primitive=PRIMITIVE_NOUL,
            # Noul 的「真概率」本身即为校准输出；置信度与之一致（不另造第二个数）。
            confidence=probability,
            probability=probability,
            raw=node,
        )
