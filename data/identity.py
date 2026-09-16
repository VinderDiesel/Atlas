#!/usr/bin/env python3
"""仓库/快照身份的单一事实源（ADR-0019 决策 ①②）。

只允许 stdlib（os / subprocess / json / pathlib / dataclasses / datetime）：本模块
被 `data/`、`eval/`、`metadata/`、`semantic/`、`serving/` 五个方向引用，任何重依赖都会
顺着 import 污染验证工具与 `make lint` 路径——ADR-0019 决策 ② 的归属表实测过另两个
候选（留在 `eval/runner.py` 会拉入 pymysql 与语义层；放 `data/snapshot.py` 会
拉入 pyarrow + pyiceberg），均因过重被否。`data/__init__.py` 实测为空文件，故
`import data.identity` 不触发 loader。

为什么必须单一：合并前生产代码有 11 份相同的 `git rev-parse --short HEAD` 实现，
只有 1 份认 `ATLAS_GIT_SHA`。同一进程内可以出现两个不同的「当前 sha」（`/health`
走认注入的那份，`rls_verify` 走自己不认注入的副本），ADR-0011 要求的
「验证工具与业务面同口径」不成立。

`ATLAS_GIT_SHA` 是容器身份通道（镜像内无 `.git`，见 `infra/docker/api/Dockerfile`
的 `ENV ATLAS_GIT_SHA=${GIT_SHA}`），是身份标识不是密钥，不触及 N9。
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent

# 快照 meta 目录：5 份副本合并到此（ADR-0019 决策 ②）。锚变量名曾分 REPO_ROOT/REPO
# 两种，值相等但改名时只改一处就会静默漂移——这正是副本该归零的理由。
SNAPSHOT_DIR = REPO_ROOT / "data" / "snapshots"


def git_short_sha() -> str:
    """当前 HEAD 短 sha：报告文件名与快照绑定键。

    环境变量 ATLAS_GIT_SHA 优先（容器/无 .git 环境的身份注入，见
    infra/docker/api/Dockerfile 与 docker-compose atlas-api）；未设置时走
    git 命令（本地/CI 行为不变）。

    Returns
    -------
    str
        注入值原样返回（已 strip）；否则返回 `git rev-parse --short HEAD` 的输出。

    Raises
    ------
    subprocess.CalledProcessError
        未注入 ATLAS_GIT_SHA 且 git 命令失败（非 git 仓库 / 无 HEAD）。
        需要容错的调用方**自行在调用点捕获**——ADR-0019 代价 ⑤ 明确降级语义
        不进本函数：若这里直接返回 "unknown"，15+ 处消费方（含评测报告命名）
        会静默拿到一个假身份，比抛错更坏。
    """
    injected = os.environ.get("ATLAS_GIT_SHA", "").strip()
    if injected:
        return injected
    out = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip()


def git_short_sha_or_none() -> str | None:
    """HEAD 短 sha，解析失败返回 `None`（不抛）。

    「能拿到就带上、拿不到就明说不知道」这个捕获写在两处就会漂移（`resolve_runtime_snapshot`
    的降级分支与 `/health` 的 `head_sha` 键必须对同一个 HEAD 说话，否则
    `snapshot_bound_to_head` 与 `sha == head_sha` 可以不一致）。异常类型集合与
    `git_short_sha` 的 `Raises` 段逐字对应——新增可抛类型时只改这里。
    """
    try:
        return git_short_sha()
    except (subprocess.CalledProcessError, OSError):
        return None


# ---------------------------------------------------------------------------
# 运行时快照解析（ADR-0019 决策 ①）
# ---------------------------------------------------------------------------

# 显式指定快照的环境变量。身份标识而非密钥，不触及 N9（ADR-0019 约束节）。
SNAPSHOT_SHA_ENV = "ATLAS_SNAPSHOT_SHA"

_META_SUFFIX = ".meta.json"

# 三级来源（决策 ① 优先级，从高到低，命中即止）
SOURCE_ENV = "env"
SOURCE_HEAD = "head"
SOURCE_LATEST = "latest"


class SnapshotUnavailable(Exception):
    """运行时绑定不到已锁快照 meta（HTTP 层转 503、CLI exit 1，见 ADR-0012）。

    为什么定义在这里而不是 `agent/factory.py`：本模块的解析函数要抛它，而
    factory 顶层 `from agent.graph import DataAgent` 会拉入 langgraph——反向
    import 会把图编排依赖塞进 `make lint` 与全部验证工具，正是决策 ② 否决
    「留在 `eval/runner.py`」时用的同一条理由。`Exception` 是 builtin，
    不破本模块「仅 stdlib」约束。裁定全文见 0019 决策 ① 末段。
    """


@dataclass(frozen=True)
class RuntimeSnapshot:
    """一次运行时快照解析的结果（决策 ① 的回显契约）。

    Attributes
    ----------
    sha : 实际绑定快照的短 sha（等于 `data/snapshots/<sha>.meta.json` 的文件名 stem）
    meta : 该 meta 的内容（表白名单预算与 data_range 的来源）
    source : 绑定来源，`env` / `head` / `latest` 之一
    bound_to_head : 是否确实绑在当前 HEAD 上。`False` 时运行时数字与该 sha 的
        评测数字不可互引（代价 ③），是必须向前端回显的信号（决策 ⑥）。
    """

    sha: str
    meta: dict[str, Any]
    source: str
    bound_to_head: bool
    # 本次解析看到的 HEAD（`None` = 无法解析，容器内无 .git 且未注入身份）。带上它是
    # 为了 `/health` 的 `head_sha` 与 `snapshot_bound_to_head` **出自同一次比较**：
    # 端点自己再调一次 `git_short_sha()` 就是两个 HEAD 来源，两次读取之间提交一次
    # 就能让响应自相矛盾（决策 ② 的口径）。默认 None 让既有的手工构造（测试注入）
    # 不破。
    head: str | None = None

    def describe(self) -> str:
        """一行绑定回显，供 CLI 与各验证工具共用。

        为什么放进 dataclass 而不是各调用点各写一遍：本 ADR 消除的就是「同一件事
        有多个写法」，回显格式若有 4 份，改一处就会让另外 3 处的输出静默分叉
        （代价 ③ 说非 HEAD 绑定的唯一约束是回显，格式分叉等于约束打折）。
        `bound_to_head` 用小写 true/false 与 /health 的 JSON 字段同形（决策 ⑥）。
        """
        return (
            f"sha={self.sha} source={self.source} "
            f"bound_to_head={str(self.bound_to_head).lower()} "
            f"created_at={self.meta.get('created_at')}"
        )

    @property
    def table_count(self) -> int:
        """快照内表数：`/health` 的 `snapshot_tables` 键（决策 ⑥）。

        口径必须等于 Guard 白名单大小——回显 29 而实际放行 25 就是误导。白名单由
        `eval.runner.build_budget` 从同一份 `meta["row_counts"]` 展开，两处各写一次
        推导（本属性只要数量，build_budget 要名字集合，强行共用会把 Guard 的
        `atlas.{ns}.{table}` 命名规则搬进「仅 stdlib」的身份模块），因此相等关系由
        `tests/test_identity_echo.py` 对真实 17 份 meta 逐份断言，而不是靠注释保证。

        Raises
        ------
        SnapshotUnavailable
            `row_counts` 缺失或形态不是「命名空间 → 表 → 行数」。报错而不是返回 0：
            静默给 0 会让回显说「这张快照没有表」，比说不了更坏。
        """
        counts = self.meta.get("row_counts")
        if not isinstance(counts, dict):
            raise SnapshotUnavailable(f"快照 meta {self.sha} 缺 row_counts——无法回显表数")
        flat = [ns for ns, tables in counts.items() if not isinstance(tables, dict)]
        if flat:
            raise SnapshotUnavailable(
                f"快照 meta {self.sha} 的 row_counts 形态不是「命名空间 → 表 → 行数」："
                f"{flat}（口径见 data/snapshots/README.md）"
            )
        return sum(len(tables) for tables in counts.values())


def _meta_path(directory: Path, sha: str) -> Path:
    return directory / f"{sha}{_META_SUFFIX}"


def _checked_created_at(meta: dict[str, Any], path: Path) -> datetime:
    """created_at 断言（决策 ① + 代价 ④）：非空、可解析、后缀必须是 `+08:00`。

    报错而不是跳过：`max(created_at)` 一旦被坏值污染就会静默选中另一份快照，
    比「不可用」更坏。仅断言「可解析」挡不住时区漂移（UTC 一样能解析且排序正确），
    故后缀断言独立存在——这是代价 ④ 明写的补偿措施。
    """
    raw = meta.get("created_at")
    text = raw.strip() if isinstance(raw, str) else ""
    if not text:
        raise SnapshotUnavailable(f"快照 meta {path.name} 缺 created_at——无法按时间取最新")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise SnapshotUnavailable(
            f"快照 meta {path.name} 的 created_at 不可解析：{raw!r}（{exc}）"
        ) from exc
    if not text.endswith("+08:00"):
        raise SnapshotUnavailable(
            f"快照 meta {path.name} 的 created_at 未显式声明 +08:00：{raw!r}"
            "（AGENTS.md §7.3 要求显式时区）"
        )
    return parsed


def _load_meta(path: Path) -> tuple[datetime, dict[str, Any]]:
    """读一份 meta，校验两个身份来源一致 + created_at 合法。

    文件名 stem 与内容 `sha` 必须相等：实测 `data/snapshots/` 现有 17 份全部一致
    （2026-09-14），所以这条断言零行为风险；留着不校验的话，`/health` 回显的 sha
    与 meta 自述可以互相矛盾而无人报错——正是决策 ② 要消除的「两个来源」。
    """
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SnapshotUnavailable(f"快照 meta {path.name} 无法读取或解析：{exc}") from exc
    if not isinstance(loaded, dict):
        raise SnapshotUnavailable(f"快照 meta {path.name} 顶层不是对象")
    stem = path.name[: -len(_META_SUFFIX)]
    content_sha = str(loaded.get("sha", ""))
    if content_sha != stem:
        raise SnapshotUnavailable(
            f"快照 meta 文件名与内容自述的 sha 不一致：文件名 {stem!r} vs sha {content_sha!r}"
        )
    return _checked_created_at(loaded, path), loaded


def resolve_runtime_snapshot(snapshot_dir: Path | None = None) -> RuntimeSnapshot:
    """运行时（非评测）快照绑定：显式 > HEAD > 最新已锁（ADR-0019 决策 ①）。

    与评测路径的分工：`eval/runner.py` 的 `main()` 保持 HEAD 严格 +
    `verify_snapshot()` 指纹复核，**不调用本函数**（N6）。本函数只服务
    `/ask`、`atlas ask`、`atlas query` 与各 `*_verify` 工具的「白名单预算取自哪份
    快照」，数据源仍限 `data/snapshots/` 内的已锁 meta。

    Parameters
    ----------
    snapshot_dir : 快照 meta 目录；None = `SNAPSHOT_DIR`。可注入是为了让契约测试
        在临时目录构造失效场景，不碰真实快照（判据 1 末句）。

    Returns
    -------
    RuntimeSnapshot
        `source` ∈ {`env`, `head`, `latest`}；`bound_to_head` 按「解析出的 sha 是否
        等于当前 HEAD」比较得出。对 head/latest 两级这等价于判据 1 的原公式
        （`source != "latest"`）；差别只在 env 级——原公式让「显式指定一个不等于
        HEAD 的 sha」也报 `True`，而决策 ③ 的补救手段正是这么用，于是 UI 警示
        信号会谎报。已作为 0019 决策 ① 实施裁定 1 回填。

    Raises
    ------
    SnapshotUnavailable
        `ATLAS_SNAPSHOT_SHA` 指定了但无对应 meta（**不回退**：显式意图被静默改写
        是更坏的失效模式）；或该目录无 meta 可供绑定。
    """
    base = SNAPSHOT_DIR if snapshot_dir is None else snapshot_dir
    # HEAD 解析失败不等于「无快照」：镜像内没有 .git，身份靠 ATLAS_GIT_SHA 注入，
    # 缺失即构建失败属决策 ④ 的职责。留 None 表示「无法声称绑定 HEAD」，
    # bound_to_head 于是取 False——对诚实性信号，保守的一端是不声称。
    head = git_short_sha_or_none()

    explicit = os.environ.get(SNAPSHOT_SHA_ENV, "").strip()
    if explicit:
        path = _meta_path(base, explicit)
        if not path.is_file():
            raise SnapshotUnavailable(
                f"{SNAPSHOT_SHA_ENV} 指定的快照 {explicit} 在 {base} 无 meta——"
                "显式指定不回退（决策 ①）：请核对该 sha，或取消该变量以改用 HEAD"
            )
        _, meta = _load_meta(path)
        sha, source = explicit, SOURCE_ENV
    elif head is not None and _meta_path(base, head).is_file():
        _, meta = _load_meta(_meta_path(base, head))
        sha, source = head, SOURCE_HEAD
    else:
        # sorted 只为确定性：两份 created_at 完全相同的 meta 必须每次都选中同一份。
        # 比较用解析后的 datetime 而非定长字符串，顺带挡住「时区不同但仍可解析」
        # 造成的错选（代价 ④ 的残留风险由后缀断言 + 这里的时间序共同兜住）。
        candidates = [_load_meta(p) for p in sorted(base.glob(f"*{_META_SUFFIX}"))]
        if not candidates:
            raise SnapshotUnavailable(
                f"{base} 无锁定快照 meta（当前 HEAD={head or '不可解析'}）——"
                "请先 make seed 锁定快照（AGENTS.md N6）"
            )
        _, meta = max(candidates, key=lambda item: item[0])
        sha, source = str(meta["sha"]), SOURCE_LATEST
    return RuntimeSnapshot(
        sha=sha,
        meta=meta,
        source=source,
        bound_to_head=head is not None and sha == head,
        head=head,
    )
