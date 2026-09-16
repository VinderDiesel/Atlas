#!/usr/bin/env python3
"""容器身份不给默认值、构建期注入（ADR-0019 决策 ④，工作项 5；不需守护进程）。

失效形态（0019 背景节实测复现）：`Dockerfile` 写 `ARG GIT_SHA=b933e20`、
`docker-compose.yml` 写 `${GIT_SHA:-7d48dcb}` —— **两处默认值不同**，生效的是
compose 的 `7d48dcb`（组 2，无零售表），而 Dockerfile 的注释声称生效的是
`b933e20`。任何硬编码 sha 默认值都会随 HEAD 前进变成谎言（N2）。

决策 ④ 的三件套，本文件逐条对应：

1. 默认值删除 → 断言 `ARG GIT_SHA` 无 `=`，且**身份注入链上的三个部署配置文件**
   （Dockerfile / compose / Makefile）内都不存在任何 7 位 hex 字面量：默认值与
   注释里的 sha 是同一个缺陷的两种形态，都会随 HEAD 前进过期（N2）；
2. **构建期**响亮失败 → 把 Dockerfile 里那行守卫脚本**原样取出来用 sh 执行**
   （不是抄一份），空身份必须非零退出且消息点名 `GIT_SHA`；
3. 注入点收到 `make` → 断言求值点在 Makefile 侧（`:=` + `$(shell …)`）且已
   **export**（compose 是 make 的子进程，不 export 就等于没注入——只能实测区分），
   并断言 make 求出的值 == 运行时权威 `git_short_sha()`（身份同源）。

   本文件**不出现**那句 git 命令的字面量，连 docstring 也不行：0019 判据 5(a) 按
   命令关键词全文检索 `.py` 并断言命中集恰为「1 生产 + 2 独立预言机」，写了就多出一项。
   那是该判据的**误报**方向（与本批前四次「漏报」同型而反向），登记见 0019 判据 5(a)。

另一组断言锁住**没有做**的事，防后人「顺手加严」：

- compose 用 `${GIT_SHA:?}` 看起来更严格，但实测（docker compose 29.5.2）它会让
  `ps` / `down` / `config` 全部失败——「停服需要身份」是新造的运维陷阱，
  故必填校验只能落在构建期；本批用 `docker compose ps` 在**无身份**下必须成功
  来锁死这个选择；
- `ATLAS_SNAPSHOT_SHA` 是运行时可选项（决策 ① 的第三级回退依赖它可留空），
  不得跟 `GIT_SHA` 一起改成必填。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = REPO_ROOT / "infra" / "docker" / "api" / "Dockerfile"
COMPOSE = REPO_ROOT / "docker-compose.yml"
MAKEFILE = REPO_ROOT / "Makefile"

# 决策 ④ 点名要清除的两个硬编码身份（背景节：注释说 b933e20、生效 7d48dcb）
KNOWN_SHAS = ("b933e20", "7d48dcb")


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _run_instructions(text: str) -> list[str]:
    """按 Dockerfile 语义还原 RUN 脚本（合并 `\\` 续行），供逐行定位与执行。"""
    scripts: list[str] = []
    buf: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if buf:
            if line.endswith("\\"):
                buf.append(line[:-1].strip())
                continue
            buf.append(line)
            scripts.append(" ".join(p for p in buf if p))
            buf = []
            continue
        if line.startswith("RUN "):
            body = line[4:]
            if body.endswith("\\"):
                buf = [body[:-1].strip()]
            else:
                scripts.append(body.strip())
    return scripts


class TestDockerfileHasNoIdentityDefault(unittest.TestCase):
    """决策 ④ 第 1 步：删默认值，且注释里也不留 sha（文档判据第 2 条）。"""

    def test_arg_git_sha_declares_no_default(self) -> None:
        decls = [
            ln.strip()
            for ln in _text(DOCKERFILE).splitlines()
            if re.match(r"^\s*ARG\s+GIT_SHA\b", ln)
        ]
        self.assertTrue(decls, "Dockerfile 里必须仍有 ARG GIT_SHA 声明（注入通道不能删）")
        for decl in decls:
            self.assertNotIn(
                "=",
                decl,
                f"{decl!r} 仍带默认值——硬编码 sha 会随 HEAD 前进变成谎言（N2）",
            )

    def test_no_sha_literal_anywhere_in_dockerfile(self) -> None:
        """通用式而非枚举：注释里的 sha 与 ARG 默认值里的 sha 是同一个缺陷的两种形态。"""
        tokens = re.findall(r"(?<![0-9a-zA-Z])[0-9a-f]{7}(?![0-9a-f])", _text(DOCKERFILE))
        self.assertEqual(
            tokens, [], f"Dockerfile 仍含 7 位 hex 字面量 {tokens}（sha 只能来自注入）"
        )

    def test_no_sha_literal_in_compose_and_makefile(self) -> None:
        """把上一条的通用式推到链路其余两个文件（收口时补）。

        起因：`Makefile:158` 的注释写着「锁定快照（b933e20 meta…）」，而它描述的
        sha 其实写死在 `eval/api_acceptance.py` 里——注释与常量各说各话，正是决策 ④
        要删的「撒谎注释」形态。只扫 Dockerfile 会让这条纪律在同一个注入链上留三个
        出口，因此这里按通用式扫 compose 与 Makefile（Makefile 那处即本条清掉的）。
        """
        for path in (COMPOSE, MAKEFILE):
            with self.subTest(config=path.name):
                tokens = re.findall(r"(?<![0-9a-zA-Z])[0-9a-f]{7}(?![0-9a-f])", _text(path))
                self.assertEqual(
                    tokens, [], f"{path.name} 仍含 7 位 hex 字面量 {tokens}（历史值归 ADR）"
                )

    def test_build_guard_rejects_empty_identity(self) -> None:
        """把文件里那行守卫**原样执行**：抄一份到测试里就等于测我的副本。"""
        guards = [s for s in _run_instructions(_text(DOCKERFILE)) if "GIT_SHA" in s]
        self.assertEqual(len(guards), 1, f"构建期守卫必须恰好一处，实测 {len(guards)} 处：{guards}")
        empty = subprocess.run(
            ["sh", "-c", guards[0]],
            capture_output=True,
            text=True,
            env={**os.environ, "GIT_SHA": ""},
        )
        self.assertNotEqual(empty.returncode, 0, "空身份必须让构建失败（响亮失败优于静默错绑）")
        self.assertIn(
            "GIT_SHA",
            empty.stdout + empty.stderr,
            "失败消息不点名 GIT_SHA，用户就不知道该传什么",
        )
        ok = subprocess.run(
            ["sh", "-c", guards[0]],
            capture_output=True,
            text=True,
            env={**os.environ, "GIT_SHA": "abc1234"},
        )
        self.assertEqual(ok.returncode, 0, f"给了身份就不该失败：{ok.stderr}")

    def test_guard_precedes_dependency_layers(self) -> None:
        """失败要快：守卫排在 `uv sync` 之后意味着每次误构建先装几分钟依赖。"""
        lines = _text(DOCKERFILE).splitlines()

        def _first(pattern: str) -> int:
            return next(
                (i for i, ln in enumerate(lines) if re.search(pattern, ln)),
                len(lines),
            )

        guard = _first(r"^RUN\s+test -n")
        deps = _first(r"^RUN\s+uv sync")
        self.assertLess(guard, deps, "身份守卫必须在依赖安装层之前（fail fast，别先花几分钟装包）")


class TestComposePassesThroughWithoutDefault(unittest.TestCase):
    """决策 ④ 第 2 步：compose 透传身份，但自己不给值。"""

    def _git_sha_arg_line(self) -> str:
        lines = [
            ln.strip()
            for ln in _text(COMPOSE).splitlines()
            if re.match(r"^\s*GIT_SHA:", ln) and not ln.strip().startswith("#")
        ]
        self.assertEqual(len(lines), 1, f"compose 里 GIT_SHA 构建参数应恰好一处：{lines}")
        return lines[0]

    def test_git_sha_has_no_hardcoded_default(self) -> None:
        line = self._git_sha_arg_line()
        self.assertIsNone(
            re.search(r"\$\{GIT_SHA:?-[^}]", line),
            f"{line!r} 仍有非空默认值——就是背景节那处静默错绑的形态",
        )
        for sha in KNOWN_SHAS:
            self.assertNotIn(sha, line)

    def test_compose_text_free_of_stale_sha_comments(self) -> None:
        """注释也清：compose:199 那句「默认 b933e20」与生效值从来不符（N2）。"""
        hits = [
            f"{i + 1}: {ln.strip()}"
            for i, ln in enumerate(_text(COMPOSE).splitlines())
            for sha in KNOWN_SHAS
            if sha in ln
        ]
        self.assertEqual(hits, [], f"compose 仍提及写死的 sha（注释与默认值都会过期）：{hits}")

    def test_snapshot_sha_remains_optional(self) -> None:
        """反向保护：`ATLAS_SNAPSHOT_SHA` 不得跟身份一起被改成必填。

        留空 = 按 HEAD > 最新已锁解析（决策 ① 第三级）；改成必填会强迫每次启动
        都显式选快照，与本 ADR 的解析规则直接冲突。
        """
        lines = [
            ln.strip()
            for ln in _text(COMPOSE).splitlines()
            if "ATLAS_SNAPSHOT_SHA" in ln and not ln.strip().startswith("#")
        ]
        self.assertEqual(lines, ["ATLAS_SNAPSHOT_SHA: ${ATLAS_SNAPSHOT_SHA:-}"], f"实测 {lines}")


class TestMakefileIsTheInjectionPoint(unittest.TestCase):
    """决策 ④ 第 3 步：注入点收在 make。"""

    def test_identity_assignment_is_make_side(self) -> None:
        """赋值必须发生在 make 侧（`$(shell …)` 求值），不是写死的字符串。

        只要求 `$(shell`，不锁赋值运算符：`:=` 与 `?=` 都满足决策 ④（前者只允许
        命令行覆盖，后者额外允许 env 覆盖）——把运算符写进判据就是把风格当契约。
        """
        hits = [
            f"{i + 1}: {ln.strip()}"
            for i, ln in enumerate(_text(MAKEFILE).splitlines())
            if re.match(r"^GIT_SHA\s*[:?]=\s*\$\(shell", ln.strip())
        ]
        self.assertEqual(len(hits), 1, f"身份求值点应恰好一处，实测 {hits}")

    def test_make_identity_equals_runtime_authority(self) -> None:
        """make 求出的身份必须等于运行时权威 HEAD —— 同源才是决策 ②/④ 的交汇点。

        比「文件里有没有那句命令」强得多：两者一旦分叉（例如在 `.env` 里塞
        `ATLAS_GIT_SHA` 冒充 HEAD），构建期注入的身份就和运行时解析用的身份不是
        一个东西，正是本决策要修的「身份与能力不匹配」的又一形态。

        比对时清掉 `ATLAS_GIT_SHA`：`git_short_sha()` 认它（决策 ② 的注入通道），
        而 make 侧只认真实 HEAD，留着会让两边「合理地不一致」而误红。
        """
        env = {k: v for k, v in os.environ.items() if k not in ("GIT_SHA", "ATLAS_GIT_SHA")}
        with tempfile.TemporaryDirectory() as tmp:
            probe = Path(tmp) / "probe.mk"
            probe.write_text('print-identity:\n\t@echo "$$GIT_SHA"\n')
            proc = subprocess.run(
                ["make", "-s", "-f", "Makefile", "-f", str(probe), "print-identity"],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                env=env,
            )
        from data.identity import git_short_sha

        self.assertEqual(proc.returncode, 0, proc.stderr[:400])
        self.assertEqual(
            proc.stdout.strip(),
            git_short_sha(),
            "make 注入的身份与运行时权威解析不同源",
        )

    def test_identity_exported_to_child_processes(self) -> None:
        """`export` 与否测不出差别，除非真的看子进程环境。

        做法：临时追加一个只打印环境的 probe makefile，与真实 Makefile 同读
        （GNU Make 3.81 无 `--eval`，故用多 `-f`）。
        """
        with tempfile.TemporaryDirectory() as tmp:
            probe = Path(tmp) / "probe.mk"
            probe.write_text("print-identity:\n\t@env | grep '^GIT_SHA=' || echo MISSING\n")
            proc = subprocess.run(
                ["make", "-s", "-f", "Makefile", "-f", str(probe), "print-identity"],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                env={k: v for k, v in os.environ.items() if k != "GIT_SHA"},
            )
        line = proc.stdout.strip()
        self.assertNotEqual(
            line, "MISSING", f"compose 是 make 的子进程，不 export 就等于没注入：{proc}"
        )
        value = line.split("=", 1)[1]
        self.assertRegex(value, r"^[0-9a-f]{7,40}$", f"身份不像 git 短 sha：{line!r}")


@unittest.skipUnless(shutil.which("docker"), "无 docker CLI，跳过 compose 解析行为验证")
class TestComposeResolutionBehaviour(unittest.TestCase):
    """行为预言机：`docker compose config` 在客户端做变量插值，不需要守护进程。"""

    def _compose(self, *args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["docker", "compose", *args],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            env={**os.environ, **env},
        )

    def _resolved_git_sha(self, proc: subprocess.CompletedProcess[str]) -> str:
        match = re.search(r"^ +GIT_SHA: (.*)$", proc.stdout, re.MULTILINE)
        self.assertIsNotNone(match, f"渲染结果里没有 GIT_SHA 构建参数：{proc.stderr[:400]}")
        return match.group(1).strip().strip("\"'")

    def test_unset_identity_resolves_empty_not_a_stale_sha(self) -> None:
        proc = self._compose("config", env={"GIT_SHA": ""})
        self.assertEqual(proc.returncode, 0, proc.stderr[:400])
        self.assertEqual(
            self._resolved_git_sha(proc),
            "",
            "不注入必须得到空值（交给构建守卫拒绝），而不是某个写死的 sha",
        )

    def test_explicit_identity_is_honoured(self) -> None:
        proc = self._compose("config", env={"GIT_SHA": "abc1234"})
        self.assertEqual(proc.returncode, 0, proc.stderr[:400])
        self.assertEqual(self._resolved_git_sha(proc), "abc1234")

    def test_stop_and_inspect_work_without_identity(self) -> None:
        """本条锁住「不用 `${GIT_SHA:?}`」这个选择本身（实测：`:?` 会连累 ps/down）."""
        proc = self._compose("ps", env={"GIT_SHA": ""})
        self.assertEqual(
            proc.returncode,
            0,
            f"停服/查看不该需要构建身份——把必填放进 compose 会造出新运维陷阱：{proc.stderr[:400]}",
        )


if __name__ == "__main__":
    unittest.main()
