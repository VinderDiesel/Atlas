"""lora.candidates 契约测试（ADR-0027 T2）：反馈 → 候选归纳（纯函数，无 I/O/LLM）。

覆盖：
- 同义词候选：misunderstood/wrong_metric + corrected_term → synonym candidate
- 值域别名候选：wrong_value + corrected_term → value_alias candidate
- 无 corrected_term → 不产候选（纯 kind 不够）
- 混合输入 → 两类候选各自正确
- write_candidates 只落 _candidates/ 目录，不写权威源
- 空候选列表 → 不写任何文件

设计约束（0027 决策 ②）：
- 归纳器是**离线纯函数**，不读盘不调 LLM
- 候选只落 _candidates/ 非权威区，绝不写 synonyms/*.yml 或 values/*.json
- 确定性：同输入 → 同输出，无随机性
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _pending(
    kind: str,
    question: str,
    metric: str | None = None,
    corrected_term: str = "",
    source: str = "test-feedback",
) -> dict:
    """构造统一 pending 形态记录（ADR-0027 决策 ④ sample 包裹）。"""
    sample: dict = {"question": question, "kind": kind}
    if metric is not None:
        sample["metric"] = metric
    if corrected_term:
        sample["corrected_term"] = corrected_term
    return {"status": "pending_review", "source": source, "sample": sample}


class TestInduceCandidates(unittest.TestCase):
    """纯函数 induce_candidates 测试（不触碰文件系统）。"""

    def test_synonym_candidate_from_misunderstood(self) -> None:
        """misunderstood + corrected_term → 同义词候选（建议该措辞归入指定 metric）。"""
        from lora.candidates import induce_candidates

        records = [_pending("misunderstood", "手续费是多少", metric="commission_revenue", corrected_term="手续费")]
        result = induce_candidates(records)
        self.assertEqual(len(result.synonyms), 1)
        self.assertEqual(result.synonyms[0].term, "手续费")
        self.assertEqual(result.synonyms[0].suggest_for_metric, "commission_revenue")
        self.assertEqual(result.synonyms[0].source, "test-feedback")
        self.assertEqual(result.value_aliases, [])

    def test_synonym_candidate_from_wrong_metric(self) -> None:
        """wrong_metric + corrected_term → 同义词候选。"""
        from lora.candidates import induce_candidates

        records = [_pending("wrong_metric", "收入", metric="commission_revenue", corrected_term="总交易额")]
        result = induce_candidates(records)
        self.assertEqual(len(result.synonyms), 1)
        self.assertEqual(result.synonyms[0].term, "总交易额")
        self.assertEqual(result.synonyms[0].suggest_for_metric, "commission_revenue")

    def test_value_alias_candidate_from_wrong_value(self) -> None:
        """wrong_value + corrected_term → 值域别名候选。"""
        from lora.candidates import induce_candidates

        records = [_pending("wrong_value", "男性员工数", metric="headcount", corrected_term="男")]
        result = induce_candidates(records)
        self.assertEqual(len(result.value_aliases), 1)
        self.assertEqual(result.value_aliases[0].term, "男")
        self.assertEqual(result.value_aliases[0].source, "test-feedback")
        self.assertEqual(result.synonyms, [])

    def test_no_candidate_without_corrected_term(self) -> None:
        """无 corrected_term → 不产任何候选（纯 kind 不够，需要用户给出正确措辞）。"""
        from lora.candidates import induce_candidates

        records = [_pending("misunderstood", "问东答西", metric="some_metric")]
        result = induce_candidates(records)
        self.assertEqual(result.synonyms, [])
        self.assertEqual(result.value_aliases, [])

    def test_other_kind_ignored(self) -> None:
        """other/wrong_time 等 kind 不产候选（无明确术语归属信号）。"""
        from lora.candidates import induce_candidates

        records = [
            _pending("other", "其他问题", corrected_term="某术语"),
            _pending("wrong_time", "时间范围错", corrected_term="2024"),
        ]
        result = induce_candidates(records)
        self.assertEqual(result.synonyms, [])
        self.assertEqual(result.value_aliases, [])

    def test_mixed_produces_both_types(self) -> None:
        """混合输入：misunderstood + wrong_value → 各产一类候选。"""
        from lora.candidates import induce_candidates

        records = [
            _pending("misunderstood", "手续费", metric="commission_revenue", corrected_term="佣金", source="s1"),
            _pending("wrong_value", "男性", metric="headcount", corrected_term="M", source="s2"),
            _pending("wrong_metric", "收入", metric="total_trade_value"),  # 无 corrected_term
        ]
        result = induce_candidates(records)
        self.assertEqual(len(result.synonyms), 1)
        self.assertEqual(result.synonyms[0].term, "佣金")
        self.assertEqual(len(result.value_aliases), 1)
        self.assertEqual(result.value_aliases[0].term, "M")

    def test_empty_input(self) -> None:
        """空输入 → 空输出。"""
        from lora.candidates import induce_candidates

        result = induce_candidates([])
        self.assertEqual(result.synonyms, [])
        self.assertEqual(result.value_aliases, [])


class TestWriteCandidates(unittest.TestCase):
    """write_candidates 文件落盘测试（临时目录，验证只写 _candidates/）。"""

    def test_writes_to_candidates_dir(self) -> None:
        """候选文件落入 synonyms/_candidates/ 和 values/_candidates/。"""
        from lora.candidates import CandidateSet, SynonymCandidate, ValueAliasCandidate, write_candidates

        with tempfile.TemporaryDirectory() as tmp:
            semantic = Path(tmp) / "semantic"
            (semantic / "synonyms" / "_candidates").mkdir(parents=True)
            (semantic / "values" / "_candidates").mkdir(parents=True)

            candidates = CandidateSet(
                synonyms=[SynonymCandidate(term="手续费", suggest_for_metric="commission_revenue", source="f1", question="q")],
                value_aliases=[ValueAliasCandidate(term="M", source="f2", question="q")],
            )
            write_candidates(candidates, semantic_dir=semantic)

            syn_files = list((semantic / "synonyms" / "_candidates").glob("*.json"))
            val_files = list((semantic / "values" / "_candidates").glob("*.json"))
            self.assertEqual(len(syn_files), 1)
            self.assertEqual(len(val_files), 1)

            syn_data = json.loads(syn_files[0].read_text(encoding="utf-8"))
            self.assertEqual(syn_data["term"], "手续费")
            self.assertEqual(syn_data["suggest_for_metric"], "commission_revenue")

    def test_no_write_when_empty(self) -> None:
        """空候选 → 不写任何文件。"""
        from lora.candidates import CandidateSet, write_candidates

        with tempfile.TemporaryDirectory() as tmp:
            semantic = Path(tmp) / "semantic"
            (semantic / "synonyms" / "_candidates").mkdir(parents=True)
            (semantic / "values" / "_candidates").mkdir(parents=True)

            write_candidates(CandidateSet(synonyms=[], value_aliases=[]), semantic_dir=semantic)

            syn_files = list((semantic / "synonyms" / "_candidates").glob("*.json"))
            val_files = list((semantic / "values" / "_candidates").glob("*.json"))
            self.assertEqual(len(syn_files), 0)
            self.assertEqual(len(val_files), 0)

    def test_never_writes_authoritative_sources(self) -> None:
        """铁律（0027 决策 ①）：write_candidates 绝不写 synonyms/*.yml 或 values/*.json。"""
        from lora.candidates import CandidateSet, SynonymCandidate, write_candidates

        with tempfile.TemporaryDirectory() as tmp:
            semantic = Path(tmp) / "semantic"
            (semantic / "synonyms" / "_candidates").mkdir(parents=True)
            (semantic / "values" / "_candidates").mkdir(parents=True)

            candidates = CandidateSet(
                synonyms=[SynonymCandidate(term="test", suggest_for_metric="m", source="s", question="q")],
                value_aliases=[],
            )
            write_candidates(candidates, semantic_dir=semantic)

            # 权威源文件未被创建
            self.assertFalse((semantic / "synonyms" / "zh_cn.yml").exists())
            self.assertFalse((semantic / "synonyms" / "en_us.yml").exists())
            # values/ 下无新 json（_candidates/ 内的不算权威源）
            val_root_jsons = list((semantic / "values").glob("*.json"))
            self.assertEqual(len(val_root_jsons), 0)


class TestCandidateGenerationLintStable(unittest.TestCase):
    """判据 3（0027）：候选生成前后 make lint 输出逐字一致。

    在真实 _candidates/ 目录放入候选 JSON，验证 check_semantic_authority
    与 check_value_profiles 结果不变（_* 隔离已由 T1.2 在 fixture 级覆盖，
    此处补一层真实路径的端到端断言）。
    """

    def test_lint_unchanged_with_real_candidates(self) -> None:
        """真实 _candidates/ 放入 JSON 后，权威校验结果不变。"""
        from semantic.lint import check_semantic_authority, check_value_profiles

        syn_candidate = REPO / "semantic" / "synonyms" / "_candidates" / "_test_lint_stable.json"
        val_candidate = REPO / "semantic" / "values" / "_candidates" / "_test_lint_stable.json"

        errors_before = check_semantic_authority()
        values_before = check_value_profiles()

        try:
            syn_candidate.write_text('{"term":"测试"}\n', encoding="utf-8")
            val_candidate.write_text('{"term":"V"}\n', encoding="utf-8")

            errors_after = check_semantic_authority()
            values_after = check_value_profiles()
        finally:
            syn_candidate.unlink(missing_ok=True)
            val_candidate.unlink(missing_ok=True)

        self.assertEqual(errors_before, errors_after)
        self.assertEqual(values_before, values_after)


if __name__ == "__main__":
    unittest.main()
