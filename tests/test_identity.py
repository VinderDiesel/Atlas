#!/usr/bin/env python3
"""`data/identity.py` 契约测试（ADR-0019 判据 4 + 判据 5(a)~(e) + 文档判据第 1 条，无 DB）。

断言口径分三层：

1. **行为**（判据 4）：`git_short_sha()` 的 `ATLAS_GIT_SHA` 优先级，以及
   「失败必须抛、不得静默降级」——降级语义按代价 ⑤ 属调用点，不属共享函数。
2. **结构**（判据 5a/5b/5e）：全仓 `.py` 检索，生产代码里 HEAD 解析逻辑与
   `SNAPSHOT_DIR` 定义**各只允许 1 处**。
3. **记录与文件系统一致**（决策 ⑥ 文档判据第 1 条）：`data/snapshots/README.md`
   的演进表必须覆盖盘上每一份 meta——见 `TestSnapshotLedgerCoverage`。

结构断言按**行为特征**（常量 `_REV_PARSE` 所指的 HEAD 解析命令）而非函数名检索：
ADR-0019 初稿按 `def git_short_sha` 检索，漏掉了名为 `head_sha` 的第 11 份生产
副本，「副本归零」的判据因此可在只做一半合并时也通过。**判据必须比被它约束的
实现更宽**。同理，本文件的散文里也不得写出该命令字面量——自身命中会让 5(a) 的
等于断言变成假失败，而这条纪律由断言本身执行，不靠自觉。

本文件自身含被检索的字符串，故用拼接构造常量、并把测试目录单独按白名单
（`_ALLOWED_TEST_ORACLES`）判定，不让它污染生产侧断言。
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from data.identity import REPO_ROOT, SNAPSHOT_DIR, git_short_sha

# 分写拼接，避免本文件被自身的仓库级检索命中（若字面量出现在此处，
# "生产/测试命中集"断言会永真或永假，取决于写法，两者都是废判据）
_REV_PARSE = "rev" + "-parse"

# ADR-0019 决策 ② 末段裁定：测试侧保留 2 处**独立预言机**，不合并。
# 合并会让断言变成同义反复（产物值与被断言值同源），故它们允许继续含 _REV_PARSE。
_ALLOWED_TEST_ORACLES = frozenset({"tests/test_export_dbt.py", "tests/test_demo_e2e.py"})

# 判据 5(b) 的检索口径：**路径构造形态**，不是变量名。ADR-0019 初版的 5(b) 按
# `^SNAPSHOT_DIR =` 检索，漏掉了 serving/ 里三处内联写法（直接把目录拼在调用行），
# 与前两次按命名检索的漏检同型。正则覆盖三种实际形态（此处不写字面量示例，
# 否则本文件的注释会自造命中）：data 与 snapshots 分作两段字符串、两段合为
# 一段、以及带 f 前缀再拼文件名的形态。三者都要求「路径除法 + 以 data 打头的
# 字符串」这一构造特征，因此不会误报错误提示文案与 docstring 里的目录名说明。
_SNAP_DIR_NAME = "snapsh" + "ots"
_SNAPSHOT_PATH_RE = re.compile(
    r'/\s*f?"data"\s*/\s*f?"' + _SNAP_DIR_NAME + r'"|/\s*f?"data/' + _SNAP_DIR_NAME
)
# ADR-0019 决策 ① 的运行时/评测边界：这两处直接点名**固定 sha** 的 meta 文件，
# 属评测/验收链路（N6 要求绑死快照，正是它们该写死 sha 的理由），不受 5(b) 约束。
_ALLOWED_FIXED_SNAPSHOT = frozenset({"eval/api_acceptance.py", "eval/e2e_acceptance.py"})

_SKIP_DIR_PARTS = (".venv", "__pycache__", "node_modules", ".git")


def _rel(p: Path) -> str:
    return p.relative_to(REPO_ROOT).as_posix()


def _py_sources() -> list[Path]:
    """全仓可检索的 .py 文件（排除虚拟环境/缓存/容器内依赖目录）。"""
    return sorted(
        p
        for p in REPO_ROOT.rglob("*.py")
        if not (set(p.relative_to(REPO_ROOT).parts) & set(_SKIP_DIR_PARTS))
    )


def _files_containing(token: str) -> set[str]:
    return {
        _rel(p) for p in _py_sources() if token in p.read_text(encoding="utf-8", errors="replace")
    }


def _git_worktree() -> bool:
    """当前是否处于 git 工作树（决定结构断言的期望集是否含 identity.py 之外的真实调用）。"""
    proc = subprocess.run(
        ["git", _REV_PARSE, "--is-inside-work-tree"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0


def _git_shallow_clone() -> bool:
    """当前是否为浅克隆（shallow clone），浅克隆下历史 commit 不可见。"""
    shallow_file = REPO_ROOT / ".git" / "shallow"
    return shallow_file.exists()


class TestGitShortShaBehavior(unittest.TestCase):
    """判据 4：env 优先，且证明注入路径上 git 未被调用。"""

    def test_env_injection_short_circuits_git(self) -> None:
        """设 ATLAS_GIT_SHA → 返回注入值，且 subprocess.run 一次都不被调用。

        用 side_effect 抛异常来**证明未被调用**：若实现先跑 git 再读 env，
        本例会以 CalledProcessError 失败，而不是"碰巧返回同样的值"。
        """
        with (
            mock.patch.dict("os.environ", {"ATLAS_GIT_SHA": "abc1234"}),
            mock.patch(
                "data.identity.subprocess.run",
                side_effect=AssertionError("注入身份生效时不得调用 git"),
            ),
        ):
            self.assertEqual(git_short_sha(), "abc1234")

    def test_env_value_is_stripped(self) -> None:
        """注入值两端空白应被忽略（compose/ARG 常见带空格的传值）。"""
        with mock.patch.dict("os.environ", {"ATLAS_GIT_SHA": "  abc1234  "}):
            self.assertEqual(git_short_sha(), "abc1234")

    def test_without_env_runs_git_with_repo_cwd(self) -> None:
        """未设 env → 走 git，且 cwd 必须是仓库根（子目录调用时相对 git 目录会漂移）。"""
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="  f00dbad\n")
        with (
            mock.patch.dict("os.environ", {}, clear=True),
            mock.patch("data.identity.subprocess.run", return_value=completed) as run,
        ):
            self.assertEqual(git_short_sha(), "f00dbad")
        args = run.call_args
        self.assertEqual(args.args[0], ["git", _REV_PARSE, "--short", "HEAD"])
        self.assertEqual(args.kwargs["cwd"], REPO_ROOT)
        self.assertTrue(args.kwargs["check"], "check=True 才会在非 git 环境抛错，而非静默返回空串")

    def test_git_failure_propagates(self) -> None:
        """共享函数不得内置降级：非 git 环境必须抛（降级是调用点的责任，代价 ⑤）。"""
        err = subprocess.CalledProcessError(returncode=128, cmd=["git"])
        with (
            mock.patch.dict("os.environ", {}, clear=True),
            mock.patch("data.identity.subprocess.run", side_effect=err),
            self.assertRaises(subprocess.CalledProcessError),
        ):
            git_short_sha()


class TestSnapshotDirAuthority(unittest.TestCase):
    """判据 5(b) 的正向面：合并后 SNAPSHOT_DIR 仍指向同一目录。"""

    def test_points_at_repo_snapshots_dir(self) -> None:
        self.assertEqual(SNAPSHOT_DIR, REPO_ROOT / "data" / _SNAP_DIR_NAME)
        self.assertTrue(SNAPSHOT_DIR.is_dir(), "目录不存在会让所有 meta 读取静默返回空")


class TestCopyZeroProduction(unittest.TestCase):
    """判据 5(a)：生产代码里 HEAD 解析逻辑只允许 `data/identity.py` 一处。"""

    @unittest.skipUnless(_git_worktree(), "需要 git 工作树才能定位仓库根")
    def test_rev_parse_hits_are_identity_plus_allowed_oracles(self) -> None:
        hits = _files_containing(_REV_PARSE)
        production = {h for h in hits if not h.startswith("tests/")}
        self.assertEqual(
            production,
            {"data/identity.py"},
            f"生产代码出现未合并的 sha 副本：{sorted(production - {'data/identity.py'})}",
        )
        # 测试侧命中必须恰好是裁定的 2 处独立预言机（多一处说明有人新增副本，
        # 少一处说明预言机被误删——两者都要失败，不能让 (a) 只看生产侧就通过）
        tests = {h for h in hits if h.startswith("tests/")}
        self.assertEqual(
            tests,
            set(_ALLOWED_TEST_ORACLES),
            f"测试侧命中集与裁定不符：{sorted(tests ^ set(_ALLOWED_TEST_ORACLES))}",
        )


class TestSnapshotDirCopyZero(unittest.TestCase):
    """判据 5(b)：生产代码里**构造快照目录 Path** 的位置只允许 `data/identity.py`。

    检索按构造形态（见 `_SNAPSHOT_PATH_RE` 上方注释），不按变量名：初版按
    `^SNAPSHOT_DIR =` 检索，serving/ 三处把目录直接拼在调用行的写法全部漏掉，
    于是「副本归零」可以在只做一半时被满足——与前两次按命名检索的漏检同型。

    测试侧不做等于断言：测试读固定快照 meta 是 N6 允许的既有资产，数量会随
    用例增加而增长，写成精确等于会把「新增一个测试」变成「必须改判据」。测试侧
    的约束由 5(a)（HEAD 解析命令命中集）与 5(e)（预言机注释）覆盖。
    """

    def _hits(self) -> set[str]:
        return {
            _rel(p)
            for p in _py_sources()
            if _SNAPSHOT_PATH_RE.search(p.read_text(encoding="utf-8", errors="replace"))
        }

    def test_production_constructs_snapshot_dir_only_in_identity(self) -> None:
        production = {h for h in self._hits() if not h.startswith("tests/")}
        allowed = {"data/identity.py"} | _ALLOWED_FIXED_SNAPSHOT
        self.assertEqual(
            production,
            allowed,
            f"出现新的快照目录路径副本：{sorted(production - allowed)}；"
            f"若例外已消失，白名单需同步收缩：{sorted(allowed - production)}",
        )


class TestBackwardCompatReexport(unittest.TestCase):
    """判据 5(c)：`from eval.runner import …` 仍可用，且是同一个对象。"""

    def test_eval_runner_reexports_identity_objects(self) -> None:
        from data import identity
        from eval.runner import SNAPSHOT_DIR as runner_dir
        from eval.runner import git_short_sha as runner_sha

        self.assertIs(
            runner_sha,
            identity.git_short_sha,
            "re-export 必须是同一函数对象；包一层会绕开 env 优先逻辑，重新制造口径分裂",
        )
        self.assertEqual(runner_dir, identity.SNAPSHOT_DIR)

    def test_reexport_is_explicit_for_type_checkers(self) -> None:
        """re-export 必须是 `X as X` 显式形式，否则 mypy strict 下消费方全报 attr-defined。

        这条不是风格问题：`make test` 不跑 mypy。实测在只有运行时断言（`assertIs`）
        的版本上，本文件 11 例与全量 565 例**全部通过**，而 `mypy` 已报 12 条错误——
        即「契约测试全绿」与「契约成立」之间有一整个类型层的盲区。
        """
        src = (REPO_ROOT / "eval" / "runner.py").read_text(encoding="utf-8")
        for name in ("SNAPSHOT_DIR", "git_short_sha"):
            explicit = f"from data.identity import {name} as {name}"
            self.assertTrue(
                explicit in src,
                f"eval/runner.py 里 {name} 的再导出必须是显式 `as` 形式"
                "（mypy strict 默认 --no-implicit-reexport）",
            )


class TestExportDbtTolerance(unittest.TestCase):
    """判据 5(d)：export_dbt 的 `"unknown"` 容错已移到调用点。"""

    def test_no_rev_parse_copy_in_export_dbt(self) -> None:
        src = (REPO_ROOT / "semantic" / "export_dbt.py").read_text(encoding="utf-8")
        # 用 bool 断言而非 assertNotIn(token, src)：后者失败时会把整个文件内容
        # 当作 haystack 打进报告（实测 28KB 噪声），淹没真正的差集
        self.assertFalse(
            _REV_PARSE in src,
            "semantic/export_dbt.py 仍有自己的 HEAD 解析副本，应删除并改 import data.identity",
        )

    def test_non_git_env_still_exports_with_unknown(self) -> None:
        """让真实的 `git_short_sha()` 抛错，断言 main() 仍成功且 sha="unknown"。

        patch 的是 `data.identity.subprocess.run`（底层），不是 export_dbt 里的名字：
        这样同时验证「共享函数确实抛」与「调用点确实容错」两端。
        """
        from semantic.export_dbt import main

        err = subprocess.CalledProcessError(returncode=128, cmd=["git"])
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.dict("os.environ", {}, clear=True),
            mock.patch("data.identity.subprocess.run", side_effect=err),
        ):
            out = Path(tmp) / "dbt.yml"
            report = Path(tmp) / "report.json"
            rc = main(
                [
                    "--in",
                    str(REPO_ROOT / "semantic" / "ossie" / "atlas_finance.ossie.yaml"),
                    "--out",
                    str(out),
                    "--report",
                    str(report),
                ]
            )
            self.assertEqual(rc, 0, "非 git 环境导出必须仍然成功（原语义）")
            self.assertEqual(json.loads(report.read_text(encoding="utf-8"))["sha"], "unknown")


class TestIndependentOraclesDocumented(unittest.TestCase):
    """判据 5(e)：2 处独立预言机必须带显式注释，且不得 import data.identity。"""

    def test_oracles_carry_comment_and_stay_independent(self) -> None:
        for rel in sorted(_ALLOWED_TEST_ORACLES):
            src = (REPO_ROOT / rel).read_text(encoding="utf-8")
            self.assertTrue(
                "独立预言机" in src,
                f"{rel} 的 _head_sha() 须注明它是刻意保留的独立预言机，"
                "否则后人会按「副本归零」把它当漏删副本修掉，静默削弱断言",
            )
            self.assertTrue(
                "from data.identity import" not in src,
                f"{rel} 一旦 import data.identity，独立预言机身份即失效",
            )


class TestSnapshotLedgerCoverage(unittest.TestCase):
    """0019 决策 ⑥ 的**文档判据第 1 条**（演进表覆盖）落地为断言，不再靠人工点数。

    为什么必须自动化：本批要消除的就是「人工记录与文件系统漂移」——`data/snapshots/
    README.md` 的演进表实测漏记 8 份 meta，而它正是决策 ① 第 3 级回退（按
    `created_at` 取最新）人类排查时唯一的入口。写在 ADR 里的「文档判据」若没有断言，
    下一次 `make seed` 之后照样漂。

    方向是**单向集合包含**（meta ⊆ README），反向不成立也不取消：README 里的 7 位 hex
    有两种身份（快照名 / commit 短 sha），故反向按**行为**判定——「无同名 meta 的 token
    必须是本仓一个真实 commit」。不选「把 commit sha 列成白名单」：枚举集会随每次加出处
    注释而增长，增长本身需要人来批准，等于把纪律又交回自觉（0019 判据 5(a) 的
    「改测试不放宽判据」同型处置）。
    """

    def _metas(self) -> set[str]:
        # 取文件名首段而不是 [:7]：短 sha 位数由 git 决定，硬切 7 位会在 8 位短 sha
        # 出现时把同一份 meta 认成两个不同 token
        return {p.name.split(".", 1)[0] for p in SNAPSHOT_DIR.glob("*.meta.json")}

    def _readme_tokens(self) -> set[str]:
        text = (SNAPSHOT_DIR / "README.md").read_text(encoding="utf-8")
        # `{7,}` 而不是恰好 7：与上面 `_metas()` 的「文件名首段」同口径——短 sha 位数
        # 由 git 决定，若哪天是 8 位，恰好 7 的正则会把真快照认成「不认识的 token」
        return set(re.findall(r"(?<![0-9a-zA-Z])[0-9a-f]{7,}(?![0-9a-f])", text))

    @staticmethod
    def _is_real_commit(sha: str) -> bool:
        """该 7 位 hex 是否本仓的真实 commit（`--verify` 顺带保证唯一性）。"""
        proc = subprocess.run(
            ["git", _REV_PARSE, "--verify", "--quiet", f"{sha}^{{commit}}"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        return proc.returncode == 0

    def test_every_locked_meta_is_documented(self) -> None:
        missing = self._metas() - self._readme_tokens()
        self.assertEqual(
            missing,
            set(),
            f"data/snapshots/README.md 漏记 {len(missing)} 份已锁快照：{sorted(missing)}",
        )

    @unittest.skipUnless(_git_worktree(), "反向判定要查 commit 是否存在")
    @unittest.skipIf(_git_shallow_clone(), "浅克隆下历史 commit 不可见，跳过 commit 验证")
    def test_documented_hex_is_snapshot_or_real_commit(self) -> None:
        unknown = sorted(
            t for t in self._readme_tokens() - self._metas() if not self._is_real_commit(t)
        )
        self.assertEqual(
            unknown,
            [],
            "README 出现第三种身份的 7 位 hex：既无同名 meta，也不是本仓 commit"
            f"——{unknown}（写错的快照名、或外仓 sha 都会落在这里）",
        )

    def test_group_sizes_stated_in_readme_match_recomputation(self) -> None:
        """演进表里的「N 份」必须是重算得出的数（N1：文档数字只能来自脚本产物）。

        指纹口径与 README 的声明一致：`row_counts` + `snapshot_ids` 全等 → 同数据多锁。
        取数方式按**同一行内**「X 表」之后紧邻的「N 份」配对（而不是全文找
        「，N 份」）：一句话里并列两组的写法很自然（本节开头就是），全文找会把
        另一组的份数算进来，配对才是无歧义的口径。
        """
        groups: dict[str, int] = {}
        for path in SNAPSHOT_DIR.glob("*.meta.json"):
            meta = json.loads(path.read_text(encoding="utf-8"))
            tables = sum(len(t) for t in meta.get("row_counts", {}).values())
            digest = hashlib.md5(
                json.dumps(
                    {
                        "row_counts": meta.get("row_counts"),
                        "snapshot_ids": meta.get("snapshot_ids"),
                    },
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest()
            key = f"{digest}:{tables}"
            groups[key] = groups.get(key, 0) + 1
        text = (SNAPSHOT_DIR / "README.md").read_text(encoding="utf-8")
        # 断言前先确认「表数」足以命名这些组：若同表数出现两个指纹组，README 现在的
        # 分组口径就不够用了，必须显式失败让人去改口径，而不是各自去找「N 份」
        labels = [key.rsplit(":", 1)[1] for key in groups]
        self.assertEqual(
            len(labels),
            len(set(labels)),
            f"指纹组数 {len(labels)} 但表数只有 {sorted(set(labels))} 种——"
            "README 的分组口径需扩充（同表数的两份不同指纹不能共用一句「N 份」）",
        )
        stated: dict[str, set[int]] = {}
        for line in text.splitlines():
            for hit in re.finditer(r"(\d+) 表", line):
                after = re.search(r"(\d+) 份", line[hit.end() :])
                if after:
                    stated.setdefault(hit.group(1), set()).add(int(after.group(1)))
        for key, size in sorted(groups.items()):
            tables = key.rsplit(":", 1)[1]
            with self.subTest(表数=tables, 实测份数=size):
                found = stated.get(tables, set())
                # 既要求**至少写一次**（漏写 = 读者无从知道组大小），又要求
                # **写几处都一致**（两处不一致 = 有一处过期，正是漂掉 8 份的形态）
                self.assertTrue(found, f"README 没有任何一行在讲 {tables} 表组的份数")
                self.assertEqual(
                    found,
                    {size},
                    f"{tables} 表组实测 {size} 份，README 写了 {sorted(found)}"
                    "——文档数字只能来自重算（AGENTS.md N1）",
                )


if __name__ == "__main__":
    unittest.main()
