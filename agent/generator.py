"""LLM 路由生成器（Day 31）：问句 → Plan 候选（Generator 角色的 Plan 级实现）。

设计原则（与 AGENTS.md 对齐）
-----------------------------
- **LLM 只做路由候选**：输出 Plan JSON（metric/dimensions/time/top_n），SQL 一律由
  确定性 Compiler 生成——LLM 不触碰 SQL 语法，Guard 只兜底 Compiler 产物（最小攻击面）。
- **候选必须过确定性校验**：指标注册检查 / 维度字段存在 / 时间格式 / 结构约束，
  任一不过即 refuse，不猜测（AGENTS.md 决策优先级 4：确定性优先）。
- 直接生成 SQL 的生成器（Spider 对照 / LoRA 微调候选）不在本文件范围内。

引擎
----
- engine="openai"：OpenAI 兼容 chat/completions（读 .env 的 OPENAI_API_KEY /
  OPENAI_BASE_URL；模型名 = 构造参数 > .env 的 OPENAI_MODEL_NAME > 默认
  gpt-4o-mini）。usage 与墙钟延迟随结果返回。
- engine="stub"：确定性假引擎（内部走 Planner），用于评测链路契约测试——
  不消耗 token，产出的数字不代表任何 LLM 能力（评测报告须显式标注）。

接口
----
    gen = Generator(SemanticModel(), engine="openai")
    result = gen.generate("2013 年总交易额是多少？")
    # result: GenerationResult(plan=Plan|None, refusal=GenerationRefusal|None,
    #                         usage={...}, latency_ms=float)
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from agent.compiler import OrderSpec, Plan, SemanticModel, TimeSpec
from agent.planner import ClarificationRequest, Planner
from agent.tools.schema_linker import SchemaLinker

load_dotenv()  # AGENTS.md 第 13 节：密钥只走环境变量/.env

PROMPT_FILE = Path(__file__).resolve().parent / "prompts" / "generator_plan.yaml"
_DEFAULT_MODEL = "gpt-4o-mini"

# 时间格式正则（与 planner.py 口径一致：year/quarter YYYYQn/month YYYYMM/date ISO）
_Q_RE = re.compile(r"^\d{4}Q[1-4]$")
_M_RE = re.compile(r"^\d{6}$")
_D_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_Y_RE = re.compile(r"^\d{4}$")


def validate_plan_json(model: SemanticModel, obj: dict[str, Any]) -> tuple[Plan | None, str | None]:
    """Plan JSON → (合法 Plan, None)；不合法 → (None, 具体原因)。

    Generator 推理入口与 SFT 训练语料构造（lora.build_pairs）共用同一道确定性
    校验：训练目标与推理输出口径一致，杜绝「训练语料合法但推理永远拒」的错位
    （Day 37 架构对齐；规则变更必须同时覆盖两端测试）。
    """
    metric = obj.get("metric")
    if not isinstance(metric, str) or metric not in model.metrics:
        return None, f"metric 未注册或缺失：{metric!r}"

    dims_raw = obj.get("dimensions") or []
    dims: list[str] = []
    for dim in dims_raw:
        if not isinstance(dim, str):
            return None, f"dimension 非字符串：{dim!r}"
        found = model.find_field(dim)
        if found is None or found[1].is_time:
            return None, f"dimension 不存在或是时间字段：{dim!r}"
        dims.append(dim)

    # 与 Planner/_parse_dimensions 同口径（AGENTS.md 术语：Dimension）：Planner 命中序 =
    # dimension_synonyms 注册序（gold 标注 expected_dimensions 同源），而 LLM 输出维度
    # 顺序自由。不规范化则同集合不同序 → 编译 SQL 列序不同 → EX hash 不一致
    # （gold-146 实测 2026-09-03：Branch/Tier 与 Tier/Branch 语义等价但 hash 不同）。
    # 兜底：无同义词维度（Planner 不可达、gold 不会含）保持原相对序排末尾。
    dim_order = {name: i for i, name in enumerate(model.dimension_synonyms)}
    dims.sort(key=lambda d: dim_order.get(d, len(dim_order)))

    time_spec = _validate_time_json(obj.get("time"))
    if obj.get("time") is not None and time_spec is None:
        return None, f"time 格式不支持：{obj.get('time')!r}"

    top_n = obj.get("top_n")
    if top_n is not None and (
        not isinstance(top_n, int) or isinstance(top_n, bool) or not 1 <= top_n <= 1000
    ):
        return None, f"top_n 非法：{top_n!r}"

    # 与 Planner._parse_top_n 口径一致：前 N 名 → 按指标降序 + limit=N；
    # 否则无排序 + 默认 limit 100（EX 锚定 hash 依赖此口径）
    order_by: tuple[OrderSpec, ...] = ()
    limit = 100
    if top_n is not None:
        order_by = (OrderSpec(metric, desc=True),)
        limit = top_n
    return (
        Plan(
            metric=metric,
            dimensions=tuple(dims),
            time=time_spec,
            filters=(),
            order_by=order_by,
            limit=limit,
        ),
        None,
    )


def _validate_time_json(raw: object) -> TimeSpec | None:
    """time 字段 → TimeSpec；形态不对返回 None。"""
    if raw is None or not isinstance(raw, dict):
        return None
    granularity = raw.get("granularity")
    value = raw.get("value")
    if granularity == "year" and isinstance(value, (int, str)) and str(value).isdigit():
        return TimeSpec("year", int(value))
    if granularity == "quarter" and isinstance(value, str):
        normalized = value.upper()
        if _Q_RE.match(normalized):
            return TimeSpec("quarter", normalized)
    if granularity == "month" and isinstance(value, (int, str)) and str(value).isdigit():
        return TimeSpec("month", int(value))
    if granularity == "date" and isinstance(value, str) and _D_RE.match(value):
        return TimeSpec("date", value)
    return None


@dataclass(frozen=True)
class GenerationRefusal:
    """生成器拒答：无法确定性路由到注册指标（不猜）。"""

    question: str
    reason: str


@dataclass
class GenerationResult:
    """一次生成的结果：Plan 与 refusal 恰有一个非空。"""

    question: str
    plan: Plan | None = None
    refusal: GenerationRefusal | None = None
    usage: dict[str, int] = field(default_factory=dict)  # prompt/completion tokens
    latency_ms: float = 0.0
    raw: str = ""  # LLM 原文（报告留痕用，不落盘密钥）

    @property
    def refused(self) -> bool:
        return self.refusal is not None


class Generator:
    """问句 → Plan 候选的 LLM 路由器（确定性校验兜底）。"""

    def __init__(
        self,
        model: SemanticModel,
        engine: str = "openai",
        model_name: str | None = None,
    ) -> None:
        if engine not in ("openai", "stub"):
            raise ValueError(f"未知引擎：{engine}（支持 openai / stub）")
        self.model = model
        self.engine = engine
        self.model_name = model_name or os.environ.get("OPENAI_MODEL_NAME") or _DEFAULT_MODEL
        self._planner = Planner(model)  # stub 引擎与确定性校验复用
        self._linker = SchemaLinker(model)  # RAG 上下文检索（确定性）
        self._prompt = self._load_prompt()
        self._metrics_text = self._metrics_block()
        self._dims_info = self._dimension_info()

    # -- 公开接口 ----------------------------------------------------------

    def generate(self, question: str, k: int = 5) -> GenerationResult:
        """路由生成：问句 → Plan 候选；无法确定 → GenerationRefusal。"""
        started = time.perf_counter()
        if self.engine == "stub":
            result = self._generate_stub(question)
        else:
            result = self._generate_llm(question, k=k)
        result.latency_ms = round((time.perf_counter() - started) * 1000, 1)
        return result

    # -- 内部实现 ----------------------------------------------------------

    def _load_prompt(self) -> dict[str, str]:
        """读 prompts/generator_plan.yaml（版本管理，禁止代码内联）。"""
        doc = yaml.safe_load(PROMPT_FILE.read_text(encoding="utf-8"))
        return {
            "system": str(doc["system_template"]),
            "user": str(doc["user_template"]),
        }

    def _metrics_block(self) -> str:
        """指标清单文本：name = 同义词 = 说明（prompt 的权威清单块）。"""
        lines: list[str] = []
        for name in self.model.metrics:
            syns = "、".join(self.model.metric_synonyms.get(name, ())) or "—"
            desc = self.model.metric_descriptions.get(name, "")
            lines.append(f"{name} = {syns} = {desc}")
        return "\n".join(lines)

    def _dimension_info(self) -> str:
        """维度字段与含义（dim_* 表非时间字段，含同义词与所属表）。"""
        lines: list[str] = []
        for ds_name, ds in self.model.datasets.items():
            if not ds_name.startswith("dim_"):
                continue
            for f in ds.fields.values():
                if f.is_time or not f.synonyms:
                    continue
                lines.append(f"{f.name}（{ds_name}，用户说『{f.synonyms[0]}』即此字段）")
        return "\n".join(lines)

    def _candidates_text(self, question: str, k: int) -> str:
        """RAG 上下文：SchemaLinker top-k 候选的文档（问句相关指标）。"""
        linked = self._linker.link(question, k=k)
        lines = [f"候选{idx}：{name}" for idx, name in enumerate(linked.candidates, start=1)]
        if linked.dims:
            lines.append(f"问句检出的分组维度（参考）：{', '.join(linked.dims)}")
        return "\n".join(lines)

    def _generate_stub(self, question: str) -> GenerationResult:
        """确定性 stub：Planner 结果序列化为 Plan（评测链路契约验证用）。

        注意：stub 数字不代表 LLM 能力；stub 用于在无 LLM 端点时验证
        generator→compiler→guard→execute 评测链路与口径正确。
        """
        result = self._planner.plan(question)
        if isinstance(result, ClarificationRequest):
            return GenerationResult(
                question=question,
                refusal=GenerationRefusal(question, "；".join(result.reasons)),
            )
        return GenerationResult(question=question, plan=result)

    def _generate_llm(self, question: str, k: int) -> GenerationResult:
        """调 OpenAI 兼容端点，解析并校验响应。"""
        user = (
            self._prompt["user"]
            .replace("__METRICS__", self._metrics_text)
            .replace("__QUESTION__", question)
            .replace("__CANDIDATES__", self._candidates_text(question, k))
        )
        system = (
            self._prompt["system"]
            .replace("__METRICS__", self._metrics_text)
            .replace("__DIM_INFO__", self._dims_info)
        )
        raw, usage = self._chat(system, user)
        return self._parse_response(raw, question, usage)

    def _chat(self, system: str, user: str) -> tuple[str, dict[str, int]]:
        """OpenAI 兼容 chat/completions；返回 (content, usage)。"""
        api_key = os.environ.get("OPENAI_API_KEY", "")
        base = os.environ.get("OPENAI_BASE_URL", "").rstrip("/") or "https://api.openai.com/v1"
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY 未配置（.env）；stub 引擎不调用网络")
        payload = {
            "model": self.model_name,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.0,  # 路由任务确定性优先：零温度单次采样
            "max_tokens": 800,  # 2026-09-03 实测：300 对 TopN+RAG 候选问句不足，
            # 6/7 拒答源于输出截断（completion=300/300）而非语义拒答 → 提高上限
        }
        req = urllib.request.Request(
            f"{base}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            body: dict[str, Any] = json.loads(resp.read().decode("utf-8"))
        content = str(body["choices"][0]["message"]["content"])
        usage = {
            "prompt_tokens": int(body.get("usage", {}).get("prompt_tokens", 0)),
            "completion_tokens": int(body.get("usage", {}).get("completion_tokens", 0)),
        }
        return content, usage

    def _parse_response(self, raw: str, question: str, usage: dict[str, int]) -> GenerationResult:
        """解析 LLM 输出 → 校验 → Plan | Refusal（任何不满足都 refuse，不猜）。"""
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as exc:
            return GenerationResult(
                question=question,
                refusal=GenerationRefusal(question, f"输出非合法 JSON：{exc}"),
                usage=usage,
                raw=raw,
            )
        if not isinstance(obj, dict):
            return GenerationResult(
                question=question,
                refusal=GenerationRefusal(question, "输出不是 JSON 对象"),
                usage=usage,
                raw=raw,
            )
        if obj.get("refuse") is True:
            return GenerationResult(
                question=question,
                refusal=GenerationRefusal(question, str(obj.get("reason", "自述无法确定"))),
                usage=usage,
                raw=raw,
            )
        plan, reason = self._validate_plan(obj)
        if plan is None:
            return GenerationResult(
                question=question,
                refusal=GenerationRefusal(question, f"输出未通过确定性校验：{reason}"),
                usage=usage,
                raw=raw,
            )
        return GenerationResult(question=question, plan=plan, usage=usage, raw=raw)

    # -- 确定性校验（防 LLM 编造的唯一关卡） -------------------------------

    def _validate_plan(self, obj: dict[str, Any]) -> tuple[Plan | None, str | None]:
        """LLM 输出 → (合法 Plan, None)；不合法 → (None, 具体原因)。"""
        return validate_plan_json(self.model, obj)
