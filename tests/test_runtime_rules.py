"""解析规则按内容隔离（ADR-0031 D04）：两个 release 不串用同义词/形态。

口径：
- `RuntimeBundle.locale_rules()` 从制品固定字节装配解析规则；同内容共享缓存
  （内容寻址），不同内容互不影响；
- `Planner(..., rules=...)` 消费显式规则；缺省仍用仓库默认词典（旧 CLI/测试
  行为不变），装载 bundle 规则不污染全局默认；
- 英文 `ref` 指向**本 bundle 的**中文形态对象，不落到进程全局缓存；
- 本测试只证明隔离与按内容装配，不表示任何发布已审核或可用。
"""

from __future__ import annotations

from pathlib import Path

import yaml

from agent.planner import ClarificationRequest, Plan, Planner
from tests.workbench_support import bundle_files, write_bundle

MODEL_PATH = "semantic/ossie/atlas_finance.ossie.yaml"


def _bundle_with_extra_en_synonym(tmp_path: Path, word: str):
    """真实制品 + en 词典追加 total_trade_tax 一个英文措辞。"""
    from agent.runtime.bundle import load_bundle

    files = bundle_files()
    doc = yaml.safe_load(files["semantic/synonyms/en_us.yml"])
    doc["metric_synonyms"]["total_trade_tax"] = ["trade tax", word]
    files["semantic/synonyms/en_us.yml"] = yaml.safe_dump(doc, allow_unicode=True).encode()
    return load_bundle(tmp_path, write_bundle(tmp_path, files))


def _planner(bundle, *, rules=None) -> Planner:
    model = bundle.semantic_model(MODEL_PATH)
    if rules is None:
        return Planner(model)
    return Planner(model, rules=rules)


def test_two_bundles_do_not_share_metric_synonyms(tmp_path: Path) -> None:
    from agent.runtime.bundle import load_bundle

    real = load_bundle(tmp_path, write_bundle(tmp_path, bundle_files()))
    variant = _bundle_with_extra_en_synonym(tmp_path, "tax burden")
    planner_real = _planner(real, rules=real.locale_rules())
    planner_variant = _planner(variant, rules=variant.locale_rules())

    hit = planner_variant.plan("tax burden in 2013", "en")
    assert isinstance(hit, Plan)
    assert hit.metric == "total_trade_tax"
    miss = planner_real.plan("tax burden in 2013", "en")
    assert isinstance(miss, ClarificationRequest)
    assert miss.kind == "unmatched"
    # 交错复核：先跑变体不使真实制品命中，再跑真实制品不使变体失效
    assert isinstance(planner_variant.plan("tax burden in 2013", "en"), Plan)
    assert planner_real.plan("tax burden in 2013", "en").kind == "unmatched"


def test_two_bundles_do_not_share_zh_patterns(tmp_path: Path) -> None:
    from agent.runtime.bundle import load_bundle

    real = load_bundle(tmp_path, write_bundle(tmp_path, bundle_files()))
    files = bundle_files()
    doc = yaml.safe_load(files["semantic/synonyms/patterns_zh_cn.yml"])
    doc["followup"]["prefixes"] = [w for w in doc["followup"]["prefixes"] if w != "换成"]
    files["semantic/synonyms/patterns_zh_cn.yml"] = yaml.safe_dump(
        doc, allow_unicode=True, sort_keys=False
    ).encode()
    variant = load_bundle(tmp_path, write_bundle(tmp_path, files))
    planner_real = _planner(real, rules=real.locale_rules())
    planner_variant = _planner(variant, rules=variant.locale_rules())
    prev = Plan(
        metric="total_trade_tax", dimensions=(), time=None, filters=(), order_by=(), limit=100
    )

    assert isinstance(planner_real.followup("换成 2014 年", prev, "zh"), Plan)
    assert planner_variant.followup("换成 2014 年", prev, "zh") is None
    # 变体仍消费自己词典里的其余前缀（证明是减词版，不是空词典）
    assert isinstance(planner_variant.followup("改成 2014 年", prev, "zh"), Plan)
    # 交错复核
    assert isinstance(planner_real.followup("换成 2014 年", prev, "zh"), Plan)
    assert planner_variant.followup("换成 2014 年", prev, "zh") is None


def test_bundle_rules_do_not_leak_into_default_path(tmp_path: Path) -> None:
    from agent.compiler import load_locale_synonyms

    variant = _bundle_with_extra_en_synonym(tmp_path, "tax burden")
    planner_with = _planner(variant, rules=variant.locale_rules())
    assert isinstance(planner_with.plan("tax burden in 2013", "en"), Plan)

    # 装载变体规则不改变缺省规则：新 Planner 不携带 rules 时仍用仓库词典
    planner_default = _planner(variant)
    assert isinstance(planner_default.plan("tax burden in 2013", "en"), ClarificationRequest)
    assert load_locale_synonyms("en_us")["metric_synonyms"]["total_trade_tax"] == ("trade tax",)


def test_rules_cached_by_content_not_by_instance(tmp_path: Path) -> None:
    from agent.runtime.bundle import load_bundle

    release_id = write_bundle(tmp_path, bundle_files())
    first = load_bundle(tmp_path, release_id)
    second = load_bundle(tmp_path, release_id)
    assert first.locale_rules() is second.locale_rules()

    variant = _bundle_with_extra_en_synonym(tmp_path, "tax burden")
    assert variant.locale_rules() is not first.locale_rules()


def test_en_ref_points_to_own_bundle_zh_patterns(tmp_path: Path) -> None:
    from agent.planner import default_locale_rules
    from agent.runtime.bundle import load_bundle

    files = bundle_files()
    doc = yaml.safe_load(files["semantic/synonyms/patterns_zh_cn.yml"])
    for item in doc["time"]["patterns"]:
        if item["kind"] == "iso_date":
            item["pattern"] = r"(?:\d{4})-(?:\d{2})-(?:\d{2})"
    files["semantic/synonyms/patterns_zh_cn.yml"] = yaml.safe_dump(
        doc, allow_unicode=True, sort_keys=False
    ).encode()
    bundle = load_bundle(tmp_path, write_bundle(tmp_path, files))
    rules = bundle.locale_rules()

    own_zh = dict(rules.zh_time_patterns)
    own_en = dict(rules.en_time_patterns)
    assert own_en["iso_date"] is own_zh["iso_date"]
    default = default_locale_rules()
    assert own_zh["iso_date"] is not dict(default.zh_time_patterns)["iso_date"]
    # 隔离的 ref 仍保持中英共享语义：同一 ISO 日期写法可被本 bundle 的 en 解析
    planner = Planner(bundle.semantic_model(MODEL_PATH), rules=rules)
    plan = planner.plan("trade tax in 2013-07-05", "en")
    assert isinstance(plan, Plan)
    assert plan.time is not None and plan.time.granularity == "date"
