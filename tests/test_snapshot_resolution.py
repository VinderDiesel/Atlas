#!/usr/bin/env python3
"""运行时快照三级解析的契约测试（ADR-0019 决策 ①，判据 1~3，无 DB）。

覆盖范围：`data/identity.resolve_runtime_snapshot()` 的优先级、回显字段与
`created_at` 断言；决策 ① 末句 / 决策 ⑤ 的 **N6 边界**（评测侧不走本函数、且评测
口径特征仍在）；代价 ⑧ 待办的 **四处接入**（`agent/cli.py` + 三个 `*_verify` 工具的
预算确实来自解析结果）。**契约用例全部在临时目录构造 meta，不读真实
`data/snapshots/`**（判据 1 末句）：读真实目录会让本文件随每次 `make seed` 变化而
失败，且会把「HEAD 无 meta」这一当前事实误当成契约锁死。

HEAD 由 `ATLAS_GIT_SHA` 注入控制（判据 4 已验证该通道优先于 git），因此本文件
不需要 git 工作树，也不会真的执行 git。

一处刻意的**行为收紧**（已回填为 0019 决策 ① 实施裁定 1）：ADR 判据 1 把
`bound_to_head` 定义为 `source != "latest"`，于是 `source="env"` 恒为 True。
但决策 ③ 给出的补救手段正是「用 ATLAS_SNAPSHOT_SHA 指定含这些表的快照」——
即显式指定一个**不等于 HEAD** 的 sha，此时 `bound_to_head=True` 是一句谎话，
而这个字段是 ADR-0018 决策 ③ 的 UI 警示信号（诚实性红线）。实现改为按
`快照 sha == HEAD sha` 比较：对 head/latest 两级与原公式逐字等价，只有 env
级由「恒 True」变成「仅在确实等于 HEAD 时 True」。见
`test_env_sha_differing_from_head_is_not_bound`。
"""

from __future__ import annotations

import importlib
import io
import json
import re
import subprocess
import tempfile
import tokenize
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from data.identity import (
    REPO_ROOT,
    RuntimeSnapshot,
    SnapshotUnavailable,
    resolve_runtime_snapshot,
)

_HEAD = "ATLAS_GIT_SHA"
_SNAP = "ATLAS_SNAPSHOT_SHA"

# 与真实 meta 同格式（定长 ISO 8601 + +08:00，AGENTS.md §7.3）；data/snapshot.py:41
# 的 TZ 就是这个偏移，格式假设破了要在这里报，而不是静默选错快照
_T0 = "2026-09-01T00:00:00+08:00"
_EARLY = "2026-09-04T12:54:14+08:00"  # 真实 dc4f350 的值（字典序最大但时间较早）
_LATE = "2026-09-09T11:56:23+08:00"  # 真实 a11d779 的值（created_at 最新）


def _env(**kv: str | None) -> Any:
    """构造隔离环境：clear=True 保证不受宿主 .env 影响，值为 None 的键不写入。"""
    preserved = {k: v for k, v in kv.items() if v is not None}
    return mock.patch.dict("os.environ", preserved, clear=True)


def _write_meta(
    directory: Path,
    sha: str,
    created_at: str | None = _T0,
    *,
    sha_in_content: str | None = None,
) -> Path:
    """写一份最小合法 meta（文件名 = sha.meta.json）。

    `sha_in_content` 仅供「文件名与内容 sha 不一致」的失效用例改写，正常用例不传。
    `created_at=None` 是**删掉该键**（不是写成 null），对应「缺字段」这条断言。
    """
    payload: dict[str, object] = {"sha": sha_in_content if sha_in_content else sha}
    if created_at is not None:
        payload["created_at"] = created_at
    path = directory / f"{sha}.meta.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _rel(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def _code_only(path: Path) -> str:
    """读源码并**抹掉注释与字符串字面量**，只留代码 token。

    结构检索需要它，否则会出现两种废判据：
    ① 散文误报——三个验证工具的 docstring 现在刻意保留改造前的写法与函数名作归因
      证据（N2 要求说清「改了什么」），按原文检索会把说明性文字当成代码使用；
    ② 为绕开 ① 而改用「行首是 import」之类的窄式，则退化成按写法检索——0019 判据 5
      的三次漏检教训是「检索口径小于裁定口径」，宁可抹掉散文也不要缩小代码检索面。

    无法解析的源码原样返回：判据宁可误报（多一条待人工确认）也不漏报。
    """
    source = path.read_text(encoding="utf-8", errors="replace")
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return source
    line_starts = [0]
    for line in source.splitlines(keepends=True):
        line_starts.append(line_starts[-1] + len(line))
    chars = list(source)
    for tok in tokens:
        if tok.type not in (tokenize.COMMENT, tokenize.STRING):
            continue
        lo = line_starts[tok.start[0] - 1] + tok.start[1]
        hi = line_starts[tok.end[0] - 1] + tok.end[1]
        chars[lo : min(hi, len(chars))] = [" "] * (min(hi, len(chars)) - lo)
    return "".join(chars)


class _TempSnapshotDir(unittest.TestCase):
    """共享的临时目录脚手架。"""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.snap_dir = Path(tmp.name)


class TestExplicitEnvLevel(_TempSnapshotDir):
    """判据 1 第一级：ATLAS_SNAPSHOT_SHA（显式指定，命中即止）。"""

    def test_env_hit_returns_source_env(self) -> None:
        _write_meta(self.snap_dir, "abc1234", _LATE)
        with _env(**{_SNAP: "abc1234", _HEAD: "fffffff"}):
            snap = resolve_runtime_snapshot(self.snap_dir)
        self.assertEqual(snap.source, "env")
        self.assertEqual(snap.sha, "abc1234")
        self.assertEqual(snap.meta["created_at"], _LATE)
        self.assertIsInstance(snap, RuntimeSnapshot)

    def test_env_hit_without_meta_raises_and_does_not_fall_back(self) -> None:
        """指定的 sha 无 meta → 响亮失败，**不回退**。

        目录里同时放了「HEAD 有 meta」与「有最新 meta」两份合法文件，让回退
        成为可能——若实现偷偷回退，本例会拿到 source=head/latest 而失败。
        理由（决策 ①）：显式意图被静默改写是比不可用更坏的失效模式。
        """
        _write_meta(self.snap_dir, "abc1234", _LATE)
        _write_meta(self.snap_dir, "fffffff", _EARLY)
        with (
            _env(**{_SNAP: "deadbee", _HEAD: "fffffff"}),
            self.assertRaises(SnapshotUnavailable) as ctx,
        ):
            resolve_runtime_snapshot(self.snap_dir)
        message = str(ctx.exception)
        self.assertIn(_SNAP, message, "错误消息须点名环境变量，否则用户不知道该查哪里")
        self.assertIn("deadbee", message)

    def test_empty_string_env_is_treated_as_unset(self) -> None:
        """空值 = 未指定（与 git_short_sha 对 ATLAS_GIT_SHA 的 strip 处理对称）。

        不是宽容：`.env.example` 的每一行都以 `KEY=` 结尾，若把空串当显式指定，
        则任何照抄 example 的人都会得到一个「指定了不存在的快照」错误。
        """
        _write_meta(self.snap_dir, "fffffff", _EARLY)
        with _env(**{_SNAP: "", _HEAD: "fffffff"}):
            snap = resolve_runtime_snapshot(self.snap_dir)
        self.assertEqual(snap.source, "head")

    def test_env_sha_differing_from_head_is_not_bound(self) -> None:
        """env 指定的 sha ≠ HEAD → bound_to_head 必须为 False（对 ADR 原公式的收紧）。"""
        _write_meta(self.snap_dir, "abc1234", _LATE)
        _write_meta(self.snap_dir, "fffffff", _EARLY)
        with _env(**{_SNAP: "abc1234", _HEAD: "fffffff"}):
            snap = resolve_runtime_snapshot(self.snap_dir)
        self.assertEqual(snap.source, "env")
        self.assertFalse(snap.bound_to_head)

    def test_env_sha_equal_to_head_is_bound(self) -> None:
        """同一 sha 的两种来源：env 与 HEAD 相等时确实绑在 HEAD 上。"""
        _write_meta(self.snap_dir, "abc1234", _LATE)
        with _env(**{_SNAP: "abc1234", _HEAD: "abc1234"}):
            snap = resolve_runtime_snapshot(self.snap_dir)
        self.assertEqual(snap.source, "env")
        self.assertTrue(snap.bound_to_head)


class TestHeadLevel(_TempSnapshotDir):
    """判据 1 第二级：HEAD 有 meta 时行为与改造前完全一致（可复现优先）。"""

    def test_head_meta_present_returns_source_head(self) -> None:
        _write_meta(self.snap_dir, "fffffff", _EARLY)
        _write_meta(self.snap_dir, "abc1234", _LATE)  # 更新的一份，但 HEAD 优先
        with _env(**{_HEAD: "fffffff"}):
            snap = resolve_runtime_snapshot(self.snap_dir)
        self.assertEqual(snap.source, "head")
        self.assertEqual(snap.sha, "fffffff")
        self.assertTrue(snap.bound_to_head)


class TestLatestLevel(_TempSnapshotDir):
    """判据 1 第三级 + 判据 2：HEAD 无 meta → 按 created_at 取最新，且不声称绑 HEAD。"""

    def test_head_without_meta_falls_back_to_latest(self) -> None:
        _write_meta(self.snap_dir, "abc1234", _LATE)
        _write_meta(self.snap_dir, "fffffff", _EARLY)
        with _env(**{_HEAD: "nosuch99"}):
            snap = resolve_runtime_snapshot(self.snap_dir)
        self.assertEqual(snap.source, "latest")
        self.assertEqual(snap.sha, "abc1234")
        self.assertFalse(snap.bound_to_head)

    def test_latest_key_is_created_at_not_filename(self) -> None:
        """判据 2：文件名倒序（`fff` > `000`）而 created_at 正序时必须选 created_at 大的。

        这条用例专门锁死口径 B（字典序）的回归——改造前 4 处工具实际选中
        `dc4f350`（2026-09-04）而非 `a11d779`（2026-09-09），即早 5 天的快照。
        """
        _write_meta(self.snap_dir, "fff0000", _EARLY)
        _write_meta(self.snap_dir, "0000aaa", _LATE)
        with _env(**{_HEAD: "nosuch99"}):
            snap = resolve_runtime_snapshot(self.snap_dir)
        self.assertEqual(snap.sha, "0000aaa", "按文件名取最新 = 口径 B 回归")

    def test_unresolvable_head_falls_back_to_latest(self) -> None:
        """HEAD 不可解析（无 .git 且未注入）→ 不崩，走第三级且如实标 unbound。

        与决策 ④ 的分工：构建期注入是部署纪律（响亮失败在 compose/Dockerfile），
        运行时解析到「最新已锁」是可达状态；这里断言的是**不谎报绑在 HEAD 上**。
        """
        _write_meta(self.snap_dir, "abc1234", _LATE)
        err = subprocess.CalledProcessError(returncode=128, cmd=["git"])
        with (
            _env(),
            mock.patch("data.identity.subprocess.run", side_effect=err),
        ):
            snap = resolve_runtime_snapshot(self.snap_dir)
        self.assertEqual(snap.source, "latest")
        self.assertFalse(snap.bound_to_head)


class TestNoSnapshotAtAll(_TempSnapshotDir):
    """判据 1 第四级：全无 meta → SnapshotUnavailable（保持 503 / exit 1 语义）。"""

    def test_empty_dir_raises(self) -> None:
        with _env(**{_HEAD: "fffffff"}), self.assertRaises(SnapshotUnavailable):
            resolve_runtime_snapshot(self.snap_dir)

    def test_non_meta_files_do_not_count(self) -> None:
        """目录里只有 README / 非 .meta.json 文件时仍算「无快照」，不得被选中。"""
        (self.snap_dir / "README.md").write_text("docs", encoding="utf-8")
        (self.snap_dir / "abc1234.json").write_text("{}", encoding="utf-8")
        with _env(**{_HEAD: "fffffff"}), self.assertRaises(SnapshotUnavailable):
            resolve_runtime_snapshot(self.snap_dir)


class TestCreatedAtAssertions(_TempSnapshotDir):
    """判据 3：created_at 非空 + 可解析 + 时区后缀必须是 +08:00，异常即报错。"""

    def _expect_unavailable(self, sha: str, created_at: str | None) -> str:
        _write_meta(self.snap_dir, sha, created_at)
        with _env(**{_HEAD: "nosuch99"}), self.assertRaises(SnapshotUnavailable) as ctx:
            resolve_runtime_snapshot(self.snap_dir)
        return str(ctx.exception)

    def test_missing_created_at_raises(self) -> None:
        message = self._expect_unavailable("abc1234", None)
        self.assertIn("created_at", message)

    def test_unparseable_created_at_raises(self) -> None:
        self.assertIn("created_at", self._expect_unavailable("abc1234", "2026/09/09"))

    def test_created_at_without_offset_raises(self) -> None:
        """无时区后缀（naive）能解析，但格式契约要求显式时区 → 必须拒。"""
        self.assertIn("created_at", self._expect_unavailable("abc1234", "2026-09-09T11:56:23"))

    def test_created_at_with_other_offset_raises(self) -> None:
        """UTC 后缀仍可解析且排序正确，但违反 §7.3 的显式 +08:00 约定 → 拒。

        代价 ④ 的补偿断言正是这一条：只在「不可解析」时报错挡不住时区漂移。
        """
        utc = "2026-09-09T11:56:23+00:00"
        self.assertIn("created_at", self._expect_unavailable("abc1234", utc))

    def test_bad_created_at_on_head_meta_still_raises(self) -> None:
        """第二级命中也要断言：/health 会回显 snapshot_created_at，坏值不得外流。"""
        _write_meta(self.snap_dir, "fffffff", "yesterday")
        with _env(**{_HEAD: "fffffff"}), self.assertRaises(SnapshotUnavailable):
            resolve_runtime_snapshot(self.snap_dir)

    def test_bad_created_at_on_unselected_meta_does_not_block_explicit_env(self) -> None:
        """未参与选择与回显的 meta 格式坏 → 不影响本次解析（断言只管被读到与被比较的）。

        反过来断言会要求全目录洁净，等于让一份手误的历史 meta 冻结所有查询；
        校验发生在「读它」的时刻，与评测侧 verify_snapshot 的全量复核分工不同。
        """
        _write_meta(self.snap_dir, "abc1234", "yesterday")
        _write_meta(self.snap_dir, "fffffff", _EARLY)
        with _env(**{_SNAP: "fffffff", _HEAD: "fffffff"}):
            snap = resolve_runtime_snapshot(self.snap_dir)
        self.assertEqual(snap.sha, "fffffff")

    def test_bad_created_at_in_latest_scan_raises(self) -> None:
        """第三级要比较全部候选 → 任一条坏值即报错（否则可能静默选错）。"""
        _write_meta(self.snap_dir, "abc1234", _LATE)
        _write_meta(self.snap_dir, "fffffff", "yesterday")
        with _env(**{_HEAD: "nosuch99"}), self.assertRaises(SnapshotUnavailable):
            resolve_runtime_snapshot(self.snap_dir)


class TestMetaIntegrity(_TempSnapshotDir):
    """文件名与 meta 内 `sha` 字段必须一致（实测真实 17 份全部一致，故零行为风险）。"""

    def test_filename_and_content_sha_mismatch_raises(self) -> None:
        """两个身份来源不一致时响亮失败，而不是挑一个看起来对的。

        决策 ② 消除的正是「同一件事有两个来源」这类漂移；若这里静默取文件名，
        `/health` 回显的 sha 与 meta 内容自述可以互相矛盾而无人报错。
        """
        _write_meta(self.snap_dir, "abc1234", _LATE, sha_in_content="fffffff")
        with (
            _env(**{_SNAP: "abc1234", _HEAD: "fffffff"}),
            self.assertRaises(SnapshotUnavailable) as ctx,
        ):
            resolve_runtime_snapshot(self.snap_dir)
        self.assertIn("abc1234", str(ctx.exception))


class TestSnapshotUnavailableOwnership(_TempSnapshotDir):
    """异常类型归属裁定（决策 ① 末段）：定义在 identity，factory 显式 re-export。"""

    def test_factory_reexports_the_same_exception_object(self) -> None:
        """`from agent.factory import SnapshotUnavailable` 必须拿到同一个类。

        两个类型会让 `except` 漏成 500：HTTP 层只捕 factory 那个，identity 抛出的
        就逃到最外层。assertIs 而非 assertEqual——比的是身份不是名字。
        """
        from agent import factory

        self.assertIs(factory.SnapshotUnavailable, SnapshotUnavailable)

    def test_reexport_is_explicit_for_type_checkers(self) -> None:
        """必须是 `X as X` 显式再导出形式（同 0019 判据 5(c) 的 mypy 踩坑）。"""
        root = Path(__file__).resolve().parent.parent
        src = (root / "agent" / "factory.py").read_text(encoding="utf-8")
        self.assertTrue(
            "from data.identity import SnapshotUnavailable as SnapshotUnavailable" in src,
            "裸 import 在 mypy strict（--no-implicit-reexport）下不算再导出，"
            "三处消费方会报 attr-defined，而 make test 不跑 mypy，测不出来",
        )
        self.assertFalse(
            "class SnapshotUnavailable" in src,
            "异常类应只在 data/identity.py 定义一份（决策 ① 归属裁定）",
        )


class TestEvaluationPathUntouched(unittest.TestCase):
    """N6 边界（决策 ① 末句 + 决策 ⑤）：三级解析只服务运行时，评测口径不改。

    两条断言都是**结构**的，因为它们守的是「评测会不会被顺手放宽」这条只能靠
    代码位置成立的约束：判据 1~3 测的是解析函数本身，测不到「有人把 `eval/runner.py`
    改成调用它」。放宽一旦发生在评测侧，症状是 EX 数字绑到了会漂移的基准上——
    没有任何测试会变红，只有读者被误导（N6 + N1）。
    """

    def test_eval_dir_does_not_use_runtime_resolution(self) -> None:
        sources = sorted((REPO_ROOT / "eval").rglob("*.py"))
        self.assertTrue(sources, "eval/ 下找不到 .py，本断言会因检索集为空而假通过")
        # 检索 code-only：散文里出现函数名不算使用（见 `_code_only` 的理由）
        users = [_rel(p) for p in sources if "resolve_runtime_snapshot" in _code_only(p)]
        self.assertEqual(
            users,
            [],
            "评测链路引用了运行时解析函数：三级回退会把「最新已锁」带进评测基准，"
            "违反 AGENTS.md N6（评测必须绑固定快照 sha）",
        )

    def test_runner_still_reads_head_strictly_and_reverifies_fingerprint(self) -> None:
        """改造前 HEAD 严格 + 指纹复核的形态必须**仍在**（本 ADR 不动评测侧）。

        断言的是三条既有代码的特征而非新增行为：`{sha}.meta.json` 单文件定位
        （不做选择）、`git_short_sha()` 提供 sha、非 dry 路径先 `verify_snapshot()`。
        任何一条消失都意味着评测基准被改，而那不在本 ADR 范围内。
        """
        path = REPO_ROOT / "eval" / "runner.py"
        # 单文件定位形态含字符串字面量，只能在原文里找（_code_only 会抹掉字符串）
        self.assertIn('f"{sha}.meta.json"', path.read_text(encoding="utf-8"))
        code = _code_only(path)
        for token in ("git_short_sha()", "verify_snapshot()"):
            self.assertIn(token, code, f"eval/runner.py 的评测口径特征 {token!r} 消失")

    def test_runtime_resolution_does_not_verify_fingerprint(self) -> None:
        """反向边界：运行时解析**不做**指纹复核（备选方案第 3 行已被否决）。

        否决理由是每次问答都要全表 scan count，把毫秒级问答变成秒级；若将来有人
        「顺手加严」把 `verify_snapshot()` 接进 identity，本例会失败，逼他回到
        0019 备选方案表重新裁定。顺带守住依赖边界：`verify_snapshot` 属
        `eval/runner.py`，被 identity import 会拉入 mysql 驱动与语义层（决策 ②）。

        本例是 `_code_only` 的真实用例：identity 的 docstring 里写了「不调用本函数」
        与「`verify_snapshot()` 原样不动」，按原文检索会自造命中。
        """
        code = _code_only(REPO_ROOT / "data" / "identity.py")
        self.assertNotIn("verify_snapshot", code)
        self.assertIsNone(
            re.search(r"^\s*(?:from|import)\s+eval[\s.]", code, flags=re.MULTILINE),
            "identity 反向 import 评测模块即破「仅 stdlib」约束（决策 ② 归属表）",
        )


class TestCallSitesUseResolvedSnapshot(unittest.TestCase):
    """代价 ⑧ 待办的落地判据：四处「列目录取最后一个」已换成解析函数（行为断言）。

    为什么不打字符串断言「源码里不再出现 `sorted(metas)[-1]`」：那类检索要么按
    命名（`metas[-1]`，改个变量名就漏检——0019 判据 5 已在同一批里三次踩过这个坑），
    要么被散文误报（三个工具的 docstring 现在**必须**保留改造前的写法作为归因
    证据）。改成断言可观测行为：**它们拿到的 meta 就是解析函数返回的那一份**——
    把解析函数换成一张真实目录里不存在的表，若某处仍在自己列目录，它会拿到真实
    meta 而被下面的等值断言抓住。

    `agent/cli.py` 一并列入，使「四处」在本文件内可一次性复验；它的退出码与
    stderr 回显另见 `tests/test_cli_query.py::TestLoadBudgetWiring`。
    """

    # (模块路径, 函数名)——对应 0019 背景节「口径 B 的四处」
    SITES = (
        ("agent.cli", "_load_budget"),
        ("serving.metrics_verify", "load_budget"),
        ("serving.rls_verify", "load_budget"),
        ("serving.p1_acceptance", "load_budget"),
    )

    def _snapshot(self) -> RuntimeSnapshot:
        """合成快照：sha 取一个字典序居中的值，表名带唯一标记。"""
        meta = {
            "sha": "0ab1cde",
            "created_at": _LATE,
            "row_counts": {"dwd": {"wire_probe_only"}},
        }
        return RuntimeSnapshot(sha="0ab1cde", meta=meta, source="env", bound_to_head=False)

    def test_each_site_returns_budget_from_the_resolved_meta(self) -> None:
        for mod_name, func_name in self.SITES:
            with self.subTest(site=f"{mod_name}.{func_name}"):
                module = importlib.import_module(mod_name)
                snap = self._snapshot()
                with mock.patch.object(module, "resolve_runtime_snapshot", return_value=snap):
                    budget, got = getattr(module, func_name)()
                self.assertIs(got, snap, "第二个返回值应是解析结果本身（报告要回显它）")
                self.assertEqual(
                    budget.allowed_tables,
                    frozenset({"atlas.dwd.wire_probe_only"}),
                    "白名单不等于解析出的那份 meta：该处仍在自己列目录选快照",
                )
                self.assertEqual(budget.dialect, "doris")

    def test_each_site_converts_unavailable_to_systemexit(self) -> None:
        """四处一致：快照不可用 → SystemExit（CLI/工具语义），消息原样带出。"""
        for mod_name, func_name in self.SITES:
            with self.subTest(site=f"{mod_name}.{func_name}"):
                module = importlib.import_module(mod_name)
                err = SnapshotUnavailable("无 meta 可供绑定：探针")
                with (
                    mock.patch.object(module, "resolve_runtime_snapshot", side_effect=err),
                    self.assertRaises(SystemExit) as ctx,
                ):
                    getattr(module, func_name)()
                self.assertIn("探针", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
