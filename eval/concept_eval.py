#!/usr/bin/env python3
"""概念一致率评测（ADR-0032 M1-T5 真版：标注统计 + matcher 三分）。

为什么存在
----------
ADR-0007 待办 1（概念映射准确率实测值进入 EVAL_REPORT，与 EX / Plan Acc 分开）
的关闭证据面。M0-T2 预实现版冻结了"标注面"统计口径（样本/术语/角色/概念去重/
registry 覆盖）；本真版接入 `agent.concept_index` 确定性 matcher，产出三分一致率。

口径（诚实）
----------
- 扫描 `eval/gold/*/gold-*.json`（与 `semantic/lint.py` 同 glob）；条数随人工
  标注演进，以当日输出为准（不写死样本数）。
- `registry_coverage` 取 distinct IRI 口径：gold 使用的概念 IRI 与
  `data/fibo/iri_registry.json`（`concepts` 短名 → IRI 字典的值集）对比。
  `not_in_registry` 非零 = M0-T2 停批条件：清单登记在产物 `missing`，
  不自行修 IRI（属 ADR-0007 已定校验面）。
- `consistency` 三分（每标注一条；匹配语义 = M1-T3 `match_terms`，locale 按
  `detect_locale`）：hit = 标注术语被概念层命中且概念一致（IRI→短名经索引
  `iri` 反查；同 term 多命中任一正确即 hit）；mismatch = 命中但概念不一致；
  miss = 未命中 / 标注概念不在索引。**rate = hit/标注总数**，不折算任何加权。
- 无层（`ATLAS_CONCEPT_LAYER=off` 或索引缺失 → loader 返回 None）时全部计
  miss（0 命中基线，本体对照）；`layer_enabled` 字段如实标记，防 0 命中被
  误读为"概念层全错"。
- 输出 `eval/reports/concept-<sha>.json`（绑定 commit sha）。

用法（从仓库根执行，纯读文件，无需 Doris）：
    uv run python -m eval.concept_eval
"""

from __future__ import annotations

import glob
import json
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from agent.concept_index import ConceptIndex, detect_locale, load_concept_index, match_terms
from data.identity import git_short_sha

REPO = Path(__file__).resolve().parent.parent
REPORTS_DIR = REPO / "eval" / "reports"
REGISTRY_PATH = REPO / "data" / "fibo" / "iri_registry.json"
TZ = timezone(timedelta(hours=8))


def load_registry_iris(path: Path = REGISTRY_PATH) -> set[str]:
    """iri_registry.json → 权威 IRI 集（concepts 短名 → IRI 字典的值集）。"""
    doc = json.loads(path.read_text(encoding="utf-8"))
    return set(doc["concepts"].values())


def load_samples() -> list[dict[str, Any]]:
    """gold 样本（glob 口径与 semantic/lint.py 的 check_gold 一致）。"""
    pattern = str(REPO / "eval" / "gold" / "*" / "gold-*.json")
    return [json.loads(Path(p).read_text(encoding="utf-8")) for p in sorted(glob.glob(pattern))]


def summarize(samples: list[dict[str, Any]], registry_iris: set[str]) -> dict[str, Any]:
    """标注统计（纯函数；samples 为解析后的 gold dict 列表，仅消费 fibo_concepts）。

    参数
    ----
    samples : 完整 gold 样本列表。
    registry_iris : 权威 IRI 集（load_registry_iris 的产物）。

    返回
    ----
    统计 dict：样本级/术语级计数、角色分布（键排序保证产物重建确定性）、
    distinct 概念数与 registry 覆盖（in_registry + not_in_registry ==
    concepts_distinct；missing = not_in_registry 的排序清单）。
    """
    samples_annotated = 0
    roles: Counter[str] = Counter()
    concepts: set[str] = set()
    terms_total = 0
    for sample in samples:
        annotations = sample.get("fibo_concepts") or []
        if annotations:
            samples_annotated += 1
        for ann in annotations:
            terms_total += 1
            roles[str(ann["role"])] += 1
            concepts.add(str(ann["concept"]))
    not_in = sorted(concepts - registry_iris)
    return {
        "samples_total": len(samples),
        "samples_annotated": samples_annotated,
        "terms_total": terms_total,
        "terms_by_role": dict(sorted(roles.items())),
        "concepts_distinct": len(concepts),
        "registry_coverage": {
            "in_registry": len(concepts) - len(not_in),
            "not_in_registry": len(not_in),
            "missing": not_in,
        },
    }


def classify_annotation(question: str, term: str, concept_iri: str, index: ConceptIndex) -> str:
    """单条标注三分：hit / mismatch / miss（纯确定性，无 IO）。

    参数
    ----
    question : gold 样本问句。
    term : 标注术语（gold `fibo_concepts[].term`）。
    concept_iri : 标注概念 IRI（gold `fibo_concepts[].concept`）。
    index : 概念索引（调用方保证非 None；无层在 summarize_consistency 处理）。

    返回
    ----
    "hit" / "mismatch" / "miss"：先按问句 locale 跑 `match_terms` 并过滤术语
    原文，再经索引 `iri` 反查标注概念短名比对；反查失败（概念层未覆盖该概念，
    真实数据不触发——registry 全覆盖）与未命中同归 "miss"（与无层语义一致）。
    """
    hits = [h for h in match_terms(index, question, detect_locale(question)) if h.term == term]
    if not hits:
        return "miss"
    target = next(
        (name for name, entry in index.concepts.items() if entry.iri == concept_iri), None
    )
    if target is None:
        return "miss"
    return "hit" if any(hit.concept == target for hit in hits) else "mismatch"


def summarize_consistency(
    samples: list[dict[str, Any]], index: ConceptIndex | None
) -> dict[str, Any]:
    """一致性三分统计（纯函数；index=None = 无层 → 全 miss，0 命中基线）。

    返回
    ----
    `{"hit", "mismatch", "miss", "rate"}`；rate = "<hit>/<标注总数>"（分母为
    全部标注条数，含无层时——样本/标注面不随层的有无变化）。
    """
    counts = {"hit": 0, "mismatch": 0, "miss": 0}
    total = 0
    for sample in samples:
        for ann in sample.get("fibo_concepts") or []:
            total += 1
            if index is None:
                counts["miss"] += 1
                continue
            counts[classify_annotation(sample["question"], ann["term"], ann["concept"], index)] += 1
    return {**counts, "rate": f"{counts['hit']}/{total}"}


def build_report(
    samples: list[dict[str, Any]],
    index: ConceptIndex | None,
    sha: str,
    *,
    registry_iris: set[str],
) -> dict[str, Any]:
    """组装产物 dict（纯函数；不落盘，便于测试锁定 schema）。

    stage 恒为 "matcher"（脚本能力阶段）；layer_enabled 如实标记本次运行的
    层可用性（0 命中防误读，见模块 docstring）。
    """
    return {
        "eval": "concept-alignment",
        "stage": "matcher",
        "created_at": datetime.now(TZ).isoformat(timespec="seconds"),
        "sha": sha,
        "layer_enabled": index is not None,
        "stats": summarize(samples, registry_iris),
        "consistency": summarize_consistency(samples, index),
        "notes": (
            "三分口径：hit=标注术语被概念层命中且概念一致；mismatch=命中但概念不一致；"
            "miss=未命中或标注概念不在索引。rate = hit/标注总数，不折算任何加权；"
            "layer_enabled=false（无层）时全部计 miss（0 命中基线，本体对照）。"
        ),
    }


def main() -> int:
    sha = git_short_sha()
    samples = load_samples()
    index = load_concept_index()
    report = build_report(samples, index, sha, registry_iris=load_registry_iris())
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORTS_DIR / f"concept-{sha}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[done] {out.relative_to(REPO)}")
    print(json.dumps(report["stats"], ensure_ascii=False, indent=2))
    print(json.dumps(report["consistency"], ensure_ascii=False, indent=2))
    if report["stats"]["registry_coverage"]["not_in_registry"]:
        print(
            "[error] gold 标注含 registry 外 IRI（M0-T2 停批条件；清单见产物 missing，不自行修）",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
