#!/usr/bin/env python3
"""LoRA 推理入口（High2：让生成式路径在无 GPU 时也可端到端演练）。

设计
----
- **有 adapter**：若配置了 vLLM 服务的 LoRA 端点（ATLAS_LORA_ENDPOINT），调用它
  生成 Plan 候选 → 确定性校验（agent/generator.validate_plan_json）→ Compiler 出 SQL。
  与 agent/generator 同口径，LLM 只做 Plan 候选，SQL 仍由编译器生成。
- **无 adapter（shadow / 兜底）**：未配置端点时，回落到确定性编译器
  （Planner→Plan→Compiler）作为「影子」输出，并标记 generator_used=False。这保证
  生成式链路在开发机（无 GPU）也能跑通「问句→Plan→SQL」全路径、可被集成测试覆盖，
  真实 LoRA 推理只在 GPU 环境启用（不在此机烧钱）。

不引入 torch/transformers 依赖：本模块只负责路由与兜底，训练在 lora/train.py。

用法::
    gen = LoRAGenerator(SemanticModel())
    result = gen.generate("2013 年第二季度总交易额")
    # result.sql 即编译产物；result.generator_used 区分真·LoRA / 影子兜底
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
from dataclasses import dataclass, field

from agent.compiler import Compiler, Plan, SemanticModel
from agent.generator import validate_plan_json
from agent.planner import ClarificationRequest, Planner


@dataclass
class InferenceResult:
    """一次 LoRA 推理结果（与 agent/generator.GenerationResult 同语义，简化版）。"""

    question: str
    plan: Plan | None = None
    sql: str | None = None
    notes: list[str] = field(default_factory=list)
    generator_used: bool = False  # True = 真·LoRA 端点；False = 确定性影子兜底
    refusal: str | None = None


class LoRAGenerator:
    """问句 → Plan → SQL 的 LoRA 推理路由（影子兜底，无需 GPU）。"""

    def __init__(self, model: SemanticModel, endpoint: str | None = None) -> None:
        self.model = model
        self.planner = Planner(model)
        self.compiler = Compiler(model)
        self.endpoint = endpoint or os.environ.get("ATLAS_LORA_ENDPOINT")

    def generate(self, question: str) -> InferenceResult:
        if self.endpoint:
            return self._generate_adapter(question)
        return self._shadow(question)

    # -- 影子兜底（确定性编译器，CPU 可跑） ----------------------------------
    def _shadow(self, question: str) -> InferenceResult:
        plan = self.planner.plan(question)
        if isinstance(plan, ClarificationRequest):
            return InferenceResult(
                question=question,
                generator_used=False,
                refusal="；".join(plan.reasons),
                notes=["影子兜底：确定性编译器未解析，原样澄清（不猜测）"],
            )
        sql, notes = self.compiler.compile(plan)
        return InferenceResult(
            question=question,
            plan=plan,
            sql=sql,
            notes=list(notes) + ["影子兜底：未配置 LoRA 端点，使用确定性编译器"],
            generator_used=False,
        )

    # -- 真·LoRA 端点（vLLM OpenAI 兼容 chat/completions） -------------------
    def _generate_adapter(self, question: str) -> InferenceResult:
        endpoint = self.endpoint
        assert endpoint is not None
        model_name = os.environ.get("ATLAS_LORA_MODEL_NAME", "atlas-sql-v1")
        payload = {
            "model": model_name,
            "messages": [{"role": "user", "content": question}],
            "temperature": 0.0,
            "max_tokens": 800,
        }
        req = urllib.request.Request(
            f"{endpoint.rstrip('/')}/v1/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310 - 受管推理端点
                body = json.loads(resp.read().decode("utf-8"))
            raw = str(body["choices"][0]["message"]["content"])
        except Exception as exc:  # noqa: BLE001 - 端点不可用 → 回落影子，不中断
            shadow = self._shadow(question)
            shadow.notes.append(f"LoRA 端点调用失败（{exc!r}），回落影子兜底")
            return shadow

        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as exc:
            shadow = self._shadow(question)
            shadow.notes.append(f"LoRA 输出非合法 JSON（{exc}），回落影子兜底")
            return shadow
        if obj.get("refuse") is True:
            return InferenceResult(
                question=question,
                generator_used=True,
                refusal=str(obj.get("reason", "自述无法确定")),
            )
        plan, reason = validate_plan_json(self.model, obj)
        if plan is None:
            shadow = self._shadow(question)
            shadow.notes.append(f"LoRA 输出未过确定性校验（{reason}），回落影子兜底")
            return shadow
        sql, notes = self.compiler.compile(plan)
        return InferenceResult(
            question=question,
            plan=plan,
            sql=sql,
            notes=list(notes),
            generator_used=True,
        )


def main() -> int:
    import argparse

    from eval.runner import git_short_sha

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("question", help="自然语言问句")
    parser.add_argument("--adapter", default="sql_v1")
    args = parser.parse_args()

    model = SemanticModel()
    gen = LoRAGenerator(model)
    started = time.perf_counter()
    result = gen.generate(args.question)
    elapsed = round((time.perf_counter() - started) * 1000, 1)
    print(
        json.dumps(
            {
                "question": result.question,
                "generator_used": result.generator_used,
                "plan": None
                if result.plan is None
                else {
                    "metric": result.plan.metric,
                    "dimensions": list(result.plan.dimensions),
                    "time": None if result.plan.time is None else str(result.plan.time.value),
                },
                "sql": result.sql,
                "refusal": result.refusal,
                "notes": result.notes,
                "latency_ms": elapsed,
                "sha": git_short_sha(),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
