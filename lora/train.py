#!/usr/bin/env python3
"""LoRA 训练入口（Day 37/38，ADR-0008）：合规语料 → QLoRA → sql_v1 adapter。

职责与红线
----------
- **前置检查失败即 exit 2**：ML 依赖缺失 / 无 GPU / 语料为空或低于 min_samples
  时不启动训练（不装样子、不烧钱）；输出明确恢复指引。
- 训练目标 = 合法 Plan JSON（ADR-0008 决策 1），语料由 lora/build_pairs.py
  mode=plan 构造并校验（与 agent/generator 推理同口径，防「语料合法但推理
  永远拒」）。
- 权重落 `lora/weights/<adapter>/`（gitignore 不入库）；入库的是配置与
  train-report.json（rank/alpha/样本数/耗时/GPU 型号——所有数字由脚本产出）。
- **训练分支未在 GPU 实测**（本机无 GPU，2026-09-03）：代码按最小实现写，
  参数完全来自 configs/<adapter>.yaml；实测数字待 GPU 就绪后由本脚本产出，
  在此之前任何训练结果数字都不得写入文档（AGENTS.md N1）。

用法（从仓库根执行；GPU 机上先 `uv sync --extra ml`）：
    uv run python -m lora.train --adapter sql_v1 --data lora/data/pairs.jsonl

数据格式（jsonl，每行）：
    {"question": "...", "answer": "<合法 Plan JSON 字符串>"}
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

from eval.runner import git_short_sha

LORA_DIR = Path(__file__).resolve().parent
TZ = timezone(timedelta(hours=8))

# 训练模板（与 configs/sql_v1.yaml template.prompt 一致，双处同步防漂移）
_PROMPT_TEMPLATE = "问题：{question}\n输出合法的计划 JSON（只输出一个 JSON，不要解释）："


class _Blocked(Exception):
    """前置检查不满足：携带恢复指引。"""


def _require_module(name: str) -> None:
    """训练依赖存在性检查（延迟导入：日常环境不装 ml extra 也不崩）。"""
    if importlib.util.find_spec(name) is None:
        raise _Blocked(
            f"缺少 ML 依赖 {name!r}——在 GPU 机上执行 `uv sync --extra ml` "
            "（torch/transformers/peft/bitsandbytes/vllm）后重试"
        )


def check_preconditions(data_path: Path, cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """数据/依赖/GPU 前置检查 → 合规样本；任一不满足 raise _Blocked。"""
    if not data_path.exists():
        raise _Blocked(
            f"语料不存在：{data_path}\n先运行 `uv run python -m lora.build_pairs`"
            "（approved 样本人工确认后）生成合规语料；当前空语料是设计结论（Day 36）"
        )
    rows: list[dict[str, Any]] = []
    for line in data_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    if not rows:
        raise _Blocked(f"语料为空：{data_path}（空语料不训练；待失败样本飞轮驱动 Day 41）")

    # 双保险：语料构造器已按 mode=plan 过滤，此处抽查全量行仍为合法 Plan JSON
    from agent.compiler import SemanticModel
    from agent.generator import validate_plan_json

    model = SemanticModel()
    for row in rows:
        q = str(row.get("question", "")).strip()
        a = str(row.get("answer", "")).strip()
        try:
            obj = json.loads(a)
        except json.JSONDecodeError as exc:
            raise _Blocked(f"语料行 answer 非 JSON（question={q[:30]!r}）：{exc}") from None
        plan, reason = validate_plan_json(model, obj)
        if plan is None:
            raise _Blocked(
                f"语料行 answer 未过 validate_plan_json（question={q[:30]!r}）：{reason}"
            )

    min_samples = int(cfg["data"]["min_samples"])
    if len(rows) < min_samples:
        raise _Blocked(
            f"语料 {len(rows)} 条 < min_samples {min_samples}——样本太少训练无统计意义，"
            "拒绝启动（防止浪费 GPU 按量费用）；先积累失败样本（Day 41 飞轮）"
        )

    _require_module("torch")
    _require_module("transformers")
    _require_module("peft")
    _require_module("bitsandbytes")

    import torch

    if not torch.cuda.is_available():
        raise _Blocked(
            "无可用 CUDA GPU（torch.cuda.is_available()=False）。本机为 mac 开发环境，"
            "训练需 24G 单卡（QLoRA 7B，ADR-0008）；恢复 = 云上按量 GPU 机执行本命令"
        )
    return rows


def _run_training(
    cfg: dict[str, Any], rows: list[dict[str, Any]], data_path: Path, sha: str
) -> dict[str, Any]:
    """QLoRA 训练主流程（未实测路径，ADR-0008 代价登记；参数全来自 config）。

    训练目标 = Plan JSON 生成（question → answer 的 SFT）：文本 = prompt 模板 +
    question + answer；loss 只计算 answer 部分（question 区 label=-100）。
    """
    # 延迟导入（train 分支才需要）
    import torch
    from peft import LoraConfig, get_peft_model
    from transformers import (
        AutoModelForCausalLM,
        AutoTokenizer,
        BitsAndBytesConfig,
        Trainer,
        TrainingArguments,
    )

    lora_cfg = cfg["lora"]
    quant_cfg = cfg["quantization"]
    train_cfg = cfg["training"]
    template = str(cfg["template"]["prompt"])
    out_dir = Path(str(cfg["output"]["weights_dir"]))
    out_dir.mkdir(parents=True, exist_ok=True)

    bnb = BitsAndBytesConfig(
        load_in_4bit=quant_cfg["load_in_4bit"],
        bnb_4bit_quant_type=str(quant_cfg["bnb_4bit_quant_type"]),
        bnb_4bit_compute_dtype=getattr(torch, str(quant_cfg["bnb_4bit_compute_dtype"])),
    )
    tokenizer = AutoTokenizer.from_pretrained(str(cfg["base_model"]), trust_remote_code=False)
    model = AutoModelForCausalLM.from_pretrained(
        str(cfg["base_model"]), quantization_config=bnb, device_map="auto"
    )
    peft_cfg = LoraConfig(
        r=int(lora_cfg["r"]),
        lora_alpha=int(lora_cfg["alpha"]),
        lora_dropout=float(lora_cfg["dropout"]),
        target_modules=[str(m) for m in lora_cfg["target_modules"]],
        task_type=str(lora_cfg["task_type"]),
    )
    model = get_peft_model(model, peft_cfg)

    max_len = int(train_cfg["max_length"])

    def encode(question: str, answer: str) -> dict[str, list[int]]:
        """question → input_ids（labels -100）；answer → 正常 labels。"""
        prompt = template.format(question=question)
        full = prompt + "\n" + answer
        enc = tokenizer(full, truncation=True, max_length=max_len)
        prompt_len = len(tokenizer(prompt, truncation=True, max_length=max_len)["input_ids"])
        input_ids = enc["input_ids"]
        labels = input_ids.copy()
        for i in range(min(prompt_len, len(labels))):
            labels[i] = -100
        return {"input_ids": input_ids, "attention_mask": enc["attention_mask"], "labels": labels}

    dataset = [encode(str(r["question"]), str(r["answer"])) for r in rows]
    training_args = TrainingArguments(
        output_dir=str(out_dir),
        num_train_epochs=float(train_cfg["epochs"]),
        learning_rate=float(train_cfg["learning_rate"]),
        per_device_train_batch_size=int(train_cfg["batch_size"]),
        gradient_accumulation_steps=int(train_cfg["grad_accum_steps"]),
        warmup_ratio=float(train_cfg["warmup_ratio"]),
        lr_scheduler_type=str(train_cfg["lr_scheduler"]),
        seed=int(train_cfg["seed"]),
        logging_steps=int(train_cfg["logging_steps"]),
        save_strategy="epoch",
        report_to=[],
        remove_unused_columns=False,
    )
    trainer = Trainer(
        model=model,
        args=training_args,
        train_dataset=dataset,  # type: ignore[arg-type]  # list[dict] 转 HF Dataset 由 Trainer 处理
    )
    started = time.perf_counter()
    trainer.train()
    elapsed_s = round(time.perf_counter() - started, 1)

    # 保存 adapter + 配置副本 + 训练报告（权重不入库，报告是入库资产）
    model.save_pretrained(str(out_dir))
    (out_dir / "adapter_config.yaml").write_text(
        yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    report = {
        "sha": sha,
        "adapter": cfg["output"]["adapter_name"],
        "base_model": cfg["base_model"],
        "data_source": str(data_path),
        "samples": len(dataset),
        "lora": {"r": lora_cfg["r"], "alpha": lora_cfg["alpha"], "dropout": lora_cfg["dropout"]},
        "training": {
            "epochs": train_cfg["epochs"],
            "learning_rate": train_cfg["learning_rate"],
            "effective_batch": int(train_cfg["batch_size"]) * int(train_cfg["grad_accum_steps"]),
            "elapsed_s": elapsed_s,
            "gpu": torch.cuda.get_device_name(0) if torch.cuda.is_available() else "none",
        },
        "finished_at": datetime.now(TZ).isoformat(timespec="seconds"),
    }
    (out_dir / "train-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"[done] adapter → {out_dir}；报告 → {out_dir / 'train-report.json'}")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adapter", default="sql_v1")
    parser.add_argument("--data", type=Path, default=LORA_DIR / "data" / "pairs.jsonl")
    args = parser.parse_args()

    cfg_path = LORA_DIR / "configs" / f"{args.adapter}.yaml"
    if not cfg_path.exists():
        print(f"[blocked] 配置不存在：{cfg_path}", file=sys.stderr)
        return 2
    cfg: dict[str, Any] = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))

    try:
        rows = check_preconditions(args.data, cfg)
    except _Blocked as exc:
        print(f"[blocked] {exc}", file=sys.stderr)
        return 2

    sha = git_short_sha()
    report = _run_training(cfg, rows, args.data, sha)
    print(
        "[next] vLLM serve adapter 后跑 LoRA 行评测（见 configs/sql_v1.yaml 尾部）："
        f"make rag-eval ENGINE=openai；对比表 make compare RAG_ENGINE=openai\n"
        f"[report] samples={report['samples']} elapsed_s={report['training']['elapsed_s']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
