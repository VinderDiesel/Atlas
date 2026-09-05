"""同义改写鲁棒性评测测试（Planner-only，不需 Doris）。

锁定两类行为：
1. 注册同义词改写 → Plan Acc 通过（稳定性基线）。
2. OOV 口吻 → Planner 拒绝解析（诚实拒答，计为 drop）；这些样本确认当前泛化缺口，
   若未来补了同义词使其通过，应同步更新 gold/paraphrase 的预期与 Known Limitations。
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from agent.compiler import SemanticModel
from agent.planner import ClarificationRequest, Planner

REPO_ROOT = Path(__file__).resolve().parent.parent
GOLD = REPO_ROOT / "eval" / "gold" / "paraphrase"
MODEL = SemanticModel()


def _load(qid: str) -> dict:
    return json.loads((GOLD / f"{qid}.json").read_text(encoding="utf-8"))


def _plan_ok(sample: dict) -> bool:
    plan = Planner(MODEL).plan(sample["question"])
    if isinstance(plan, ClarificationRequest):
        return False
    return (
        plan.metric == sample["expected_metric"]
        and tuple(plan.dimensions) == tuple(sample["expected_dimensions"])
        and (None if plan.time is None else str(plan.time.value)) == sample["expected_time"]
    )


class TestParaphraseStable(unittest.TestCase):
    """注册同义词改写应稳定解析（不掉落）。"""

    STABLE = [
        "pp-101-1",
        "pp-101-2",
        "pp-101-3",
        "pp-101-4",
        "pp-101-6",
        "pp-dim-1",
        "pp-dim-2",
        "pp-comm-1",
        "pp-comm-2",
    ]

    def test_stable_synonyms_resolve(self) -> None:
        for qid in self.STABLE:
            with self.subTest(qid=qid):
                self.assertTrue(_plan_ok(_load(qid)), f"{qid} 注册同义词改写未被解析")


class TestParaphraseGap(unittest.TestCase):
    """OOV 口吻当前无法解析（诚实拒答 = drop，记录泛化缺口）。

    这些用例是「已知弱点」的回归锁：若 Planner 后续支持这些措辞，测试会翻转，
    届时需同步更新 gold/paraphrase 预期并修订 README Known Limitations。
    """

    GAP = ["pp-101-5", "pp-dim-3", "pp-comm-3", "pp-comm-4"]

    def test_oov_untrained_phrasing_rejected(self) -> None:
        for qid in self.GAP:
            with self.subTest(qid=qid):
                plan = Planner(MODEL).plan(_load(qid)["question"])
                self.assertIsInstance(plan, ClarificationRequest)


if __name__ == "__main__":
    unittest.main()
