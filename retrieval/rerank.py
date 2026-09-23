"""元数据重排（Rerank，Day 24）：对召回候选按三个信号重排。

信号与口径（确定性、可审计、固定排序规则不调参）
---------------------------------------------
1. **同义词置信度**（synonym）：问句命中的该指标同义词覆盖字符数 / 问句有效
   字符数。编码“问句直接用了指标名/同义词”的强度——词法召回只看 token 共现，
   无法区分命中主名与命中长描述。
2. **指标热度**（popularity）：外部注入的频次表（生产为查询日志统计；本项目
   评测为 gold 问句留一计数——评测脚本排除当前查询，避免用目标自身做证据）。
3. **owner 优先级**（owner）：ATLAS 治理扩展声明 owner → 显式优先级表。
   当前指标均为 finance@atlas.local（最高档 1.0），故该项为常数、
   不改变排序——属“治理完备度”信号，多 owner 场景才产生区分度（诚实声明）。

排序规则（词典序分层）
----------------------
按 (synonym, popularity, owner, 原召回序) 逐层降序裁决：**查询相关信号做主键，
全局信号只做同层裁决**。加权线性混合版本曾实测 Recall@1 34/44——热度分对
top-20 内恒在的热门指标（total_trade_value 留一 6 次）是系统性加成，能压过
同义词命中但冷门的正确指标；词典序避免“与查询无关的加分项覆盖查询证据”。
（两版对比见 eval/reports/retrieval-rerank-7d48dcb.linear-weighted.json 与
 retrieval-rerank-7d48dcb.json，数字均实测。）

用法::

    reranker = MetaReranker(model, popularity=hotness)  # hotness: {metric: count}
    top5 = reranker.rerank(recall_top20, question)[:5]
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Protocol

from agent.compiler import SemanticModel

_log = logging.getLogger(__name__)

# owner → 优先级（0~1），显式配置。当前唯一 owner 最高档 1.0；
# 未注册治理块的指标 owner="" → 0.0（治理信号缺失不加分）。
OWNER_PRIORITY: Mapping[str, float] = {"finance@atlas.local": 1.0}

# 热度饱和阈值：频次 ≥ 10 视为饱和（当前 44 条语料上限 5，留一后 ≤ 5）
POPULARITY_CAP = 10


def synonym_confidence(question: str, synonyms: Sequence[str]) -> float:
    """问句对同义词集的字符覆盖比例（命中字符并集去重 / 问句有效字符数）。

    相互包含的同义词（"成交量" ⊂ "总成交量"）只贡献并集字符，不重复计分。
    """
    hits = [s for s in synonyms if s and s in question]
    if not hits:
        return 0.0
    covered = len({ch for s in hits for ch in s})
    q_len = max(1, sum(1 for ch in question if not ch.isspace()))
    return min(covered / q_len, 1.0)


def _popularity_norm(count: int) -> float:
    """热度归一化：min(count / 饱和阈值, 1)，保证热门指标不垄断分数。"""
    return min(count / POPULARITY_CAP, 1.0)


# 提示词外置路径（AGENTS.md §7.4：禁止内联在 Python 代码里）。
_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "agent" / "prompts"
_JEV_PROMPT_FILE = _PROMPTS_DIR / "jev_decision.yaml"
# 进程内缓存：重排是每问句调用，避免反复读盘；内容随 git 版本变更而变。
_state_template_cache: str | None = None


def _load_rerank_state_template() -> str:
    """读 `rerank_state_template`（进程内缓存）；缺失即抛错，不静默兜底。

    提示词是判别质量的一部分，配置缺失须显式暴露而非用代码内硬编码凑数
    （AGENTS.md §7.4）。此处不做 try/except：调用方 `_apply_chooser` 已把
    任何异常收敛为退回确定性排序，不会打断链路。
    """
    global _state_template_cache
    if _state_template_cache is None:
        import yaml

        doc = yaml.safe_load(_JEV_PROMPT_FILE.read_text(encoding="utf-8"))
        _state_template_cache = str(doc["rerank_state_template"])
    return _state_template_cache


class MetricChooser(Protocol):
    """判别式重排接缝（ADR-0030 ⑤）：从候选里选一个，附校准置信度。

    由 `agent.jev_engine.JevEngine` 满足（其 `choose()` 签名一致）。此处只依赖
    协议而非具体实现——重排层**不 import Jev**，引擎可插拔（测试注入 fake，
    生产不启用时传 `None`）。
    """

    def choose(self, state: str, question: str, options: tuple[str, ...]) -> object:
        """返回含 `.choice` 与 `.confidence` 的判别结果；失败须抛异常。"""
        ...


# 采用 Jev 判别结果的最低置信度（ADR-0030 ④：低置信不覆盖原逻辑）。
# 阈值是**工程选择**而非实测最优值——须由绑定快照 sha 的评测确定后回填
# （见 ADR-0030 验证方式；当前值属待验证口径，不得当作已调优参数引用）。
JEV_CONFIDENCE_THRESHOLD = 0.9


class MetaReranker:
    """在召回排名内按元数据信号重排的确定性重排器。

    参数
    ----
    model      : SemanticModel（提供 metric_synonyms / metric_owners）。
    popularity : 指标 → 热度频次的只读映射；不传或缺失的指标按 0 计。
                 评测口径见 retrieval_eval（gold 留一计数，排除当前查询）。
    chooser    : 可选判别引擎（ADR-0030 ⑤，如 `JevEngine`）。传 `None`（默认）
                 时行为与 ADR-0030 之前**逐字一致**。注入后仅高置信判别会覆盖
                 名次，任何异常/低置信都退回下方确定性词典序（可切换语义）。
    """

    def __init__(
        self,
        model: SemanticModel,
        popularity: Mapping[str, int] | None = None,
        chooser: MetricChooser | None = None,
        confidence_threshold: float = JEV_CONFIDENCE_THRESHOLD,
    ) -> None:
        self._model = model
        self._popularity = dict(popularity or {})
        self._chooser = chooser
        self._threshold = confidence_threshold

    def signal_score(self, metric: str, question: str) -> tuple[float, float, float]:
        """返回 (syn, pop, own) 三个信号分（各 0~1，供分析与调试）。"""
        syn = synonym_confidence(question, self._model.metric_synonyms.get(metric, ()))
        pop = _popularity_norm(self._popularity.get(metric, 0))
        owner = self._model.metric_owners.get(metric, "")
        own = OWNER_PRIORITY.get(owner, 0.0) if owner else 0.0
        return syn, pop, own

    def rerank(self, ranked: Sequence[str], question: str) -> list[str]:
        """在候选排名内重排：判别引擎优先（若注入），否则确定性词典序。

        判别路径（ADR-0030 ⑤）只把**高置信**的选中项提到首位，其余保持确定性
        序——即 Jev 提供的是「首选建议 + 置信度」，不是整体名次重写。任何异常
        （端点失败/超时/原语不符/越界选项）或置信度低于阈值 → **完全退回**
        下方词典序（可切换语义：Jev 只建议，原逻辑兜底）。

        确定性词典序：主键同义词置信度（逐查询证据），次键热度、再次 owner
        （全局偏好，仅在同层裁决）；完全同分保持原召回序（确定性）。
        """
        ordered = self._deterministic_order(ranked, question)
        return self._apply_chooser(ordered, question)

    def _deterministic_order(self, ranked: Sequence[str], question: str) -> list[str]:
        """既有词典序重排（ADR-0030 前行为，逐字保留——无条件可用路径）。"""
        scored: list[tuple[float, float, float, int, str]] = []
        for idx, doc in enumerate(ranked):
            syn, pop, own = self.signal_score(doc, question)
            scored.append((syn, pop, own, idx, doc))
        scored.sort(key=lambda item: (-item[0], -item[1], -item[2], item[3]))
        return [doc for _, _, _, _, doc in scored]

    def _apply_chooser(self, ordered: list[str], question: str) -> list[str]:
        """判别引擎裁决（层级 B）：高置信选中项提前，否则原序返回。

        **fail-open 到确定性逻辑**：任何失败都不抛给调用方——重排处在一个
        可有可无的增益位，让能力失败打断整条问数链路是本末倒置（对比
        `narrative` 的 fail-closed：那里是发货文本的正确性问题，必须硬闸）。
        """
        if self._chooser is None or len(ordered) < 2:
            return ordered  # 未注入引擎 / 单候选无需判别
        try:
            decision = self._chooser.choose(
                state=self._build_state(ordered, question),
                question="metric",
                options=tuple(ordered),
            )
        except Exception:  # noqa: BLE001 - 端点失败/契约破裂 → 退回确定性序
            _log.debug("Jev 判别失败，退回确定性重排", exc_info=True)
            return ordered

        chosen = getattr(decision, "choice", None)
        confidence = float(getattr(decision, "confidence", 0.0))
        if chosen is None or confidence < self._threshold or chosen not in ordered:
            return ordered  # 低置信/未知选项 → 不覆盖（原逻辑胜出）
        return [chosen, *(doc for doc in ordered if doc != chosen)]

    def _build_state(self, candidates: Sequence[str], question: str) -> str:
        """构造 Jev 的 state 文本（**schema-only**，ADR-0030 ②）。

        只含问句与候选项的名称/同义词/说明——**绝不含业务结果数值**。
        提示词模板从 `agent/prompts/jev_decision.yaml` 读取（禁止代码内联，
        AGENTS.md §7.4）。
        """
        lines: list[str] = []
        for name in candidates:
            syns = "、".join(self._model.metric_synonyms.get(name, ())) or "—"
            desc = self._model.metric_descriptions.get(name, "")
            lines.append(f"{name} = {syns} = {desc}")
        template = _load_rerank_state_template()
        return template.replace("__QUESTION__", question).replace(
            "__CANDIDATES__", "\n".join(lines)
        )
