"""真实会话 Agent 工厂：CLI（agent/cli.py）与 HTTP API（serving/api.py）共源。

create_live_agent() 原为 agent/cli.py 的 _live_agent（ADR-0012 提取为工厂）：
只读执行器走 eval/runner 同源（延迟 import——plan/compile 路径不触碰数据库，
本模块只在 ask 场景被调用）。快照绑定走 data.identity.resolve_runtime_snapshot
（ADR-0019 决策 ①：显式 ATLAS_SNAPSHOT_SHA > HEAD > 最新已锁），不可用时抛
SnapshotUnavailable——CLI 捕获后打印并 exit 1，HTTP 层捕获后转 503；不再用
SystemExit 中断（SystemExit 只属于 CLI 出口语义，HTTP 进程内不该裸奔）。

P7 多模型路由（2026-09-05）：model_path 显式注入语义模型 YAML，缺省 None =
金融默认（SemanticModel() 同源）——CLI ask 调用处零变化；HTTP API 按域传
零售路径（serving/api.py DOMAIN_MODEL_PATHS，finance/retail 双 agent 各自
单例懒建，会话状态按模型隔离）。
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

from langgraph.checkpoint.sqlite import SqliteSaver

from agent.graph import _CHECKPOINT_SERDE, DataAgent
from data.identity import RuntimeSnapshot, resolve_runtime_snapshot
from data.identity import SnapshotUnavailable as SnapshotUnavailable

if TYPE_CHECKING:  # 仅类型层：运行时依赖仍按下面的延迟 import 纪律走（决策 ②）
    from agent.compiler import SemanticModel
    from agent.security.sql_guard import Budget


def checkpoint_saver_from_env() -> SqliteSaver | None:
    """按 `ATLAS_CHECKPOINT_DB` 构造会话轨迹存储（ADR-0020 决策 ① 末段 + 决策 ③）。

    返回
    ----
    None : 变量未设 / 空串 / 纯空白 —— 图使用默认 `MemorySaver`，会话随重启消失。
    SqliteSaver : 指向该路径的持久化存储，表已建好（`setup()` 幂等，此处调一次）。

    为什么**不给默认路径**（与 ADR-0019 决策 ④ 同一条纪律）：把「是否落盘」写成隐式
    默认，等于让没打算持久化的部署（CI、评测、`atlas query`）在仓库里生成含业务数据的
    文件——checkpoint 里有 Guard 出口 SQL 与结果行，其敏感度等同数据库副本（N3 精神）。

    为什么自持连接：`SqliteSaver.from_conn_string()` 是 with 块语义（退出即关），而
    Agent 是进程生命周期单例（`serving/api.py` 按域懒建），两者必须同生共死。
    为什么 `serde` 走构造参数：实测 `SqliteSaver.__init__` 里的 `self.jsonplus_serde`
    是创建后从不使用的死属性，赋过去毫无效果；真正的读写走 `self.serde`（决策 ② 实测注
    第 6 条）。这里 import `_CHECKPOINT_SERDE` 而不是就地新建一份同内容 serializer——
    重建会让两处白名单各长各的，而退化是静默的。
    """
    raw = os.environ.get("ATLAS_CHECKPOINT_DB", "").strip()
    if not raw:
        # 判原始串而不是 `Path(raw)`：`Path("")` 会规范化成 `.`，「空 = 不持久化」的
        # 语义在 Path 上是不成立的（走下去还会得到 IsADirectoryError 这种误导性故障）
        return None
    path = Path(raw)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    saver = SqliteSaver(conn=conn, serde=_CHECKPOINT_SERDE)
    saver.setup()
    return saver


def _require_tables_in_snapshot(
    model: SemanticModel, budget: Budget, snapshot: RuntimeSnapshot
) -> None:
    """域-快照一致性启动校验（ADR-0019 决策 ③）。

    语义模型声明的每张表都必须在绑定快照的白名单里，否则该域**每一次**查询都会被
    Guard 拒绝，而拒绝消息（`表不在白名单内`）不指向根因（绑错快照）。本函数把这个
    失效提前到 agent 构造时，并直接列出缺失表。

    只查表集，**不查行数**（决策 ③ 末段）：`row_counts` 的绝对值不参与 Guard 判定，
    纳入启动校验会让「同数据多锁」的正常演进变成启动失败。

    Raises
    ------
    SnapshotUnavailable
        有缺失表。消息必须同时含 sha、绑定来源与两条出路（重锁 / 显式指定），
        否则用户拿到清单仍不知道该改哪里——判据 6 的后半段就是断言消息内容。
    """
    required = {ds.source for ds in model.datasets.values()}
    missing = sorted(required - set(budget.allowed_tables))
    if missing:
        raise SnapshotUnavailable(
            f"绑定快照 {snapshot.sha}（source={snapshot.source}）缺少语义模型 "
            f"{model.name} 所需的表：{missing}"
            "——请重锁快照（make seed）或用 ATLAS_SNAPSHOT_SHA 指定含这些表的快照"
        )


def create_live_agent(model_path: Path | None = None) -> DataAgent:
    """真实会话 Agent：真 Doris 执行器 + 运行时解析快照的预算与 meta。

    快照绑定走 `resolve_runtime_snapshot()`（ADR-0019 决策 ①）：显式
    `ATLAS_SNAPSHOT_SHA` > HEAD > 最新已锁。改造前这里是 HEAD 严格——HEAD 没有
    meta 就抛，于是「commit 之后、重锁之前」的窗口内 `/ask` 一律 503。

    绑定完成后先做域-快照一致性校验（决策 ③，见 `_require_tables_in_snapshot`），
    **通过后才构造** `DataAgent`：绑错快照属于配置错误，不该以「运行期逐次被 Guard
    拒绝」的形态暴露。

    Parameters
    ----------
    model_path : 语义模型 YAML（Path）；None = 金融默认（CLI 调用处零变化）。
        校验需要读模型的 datasets，因此 None 分支也在此处显式构造默认模型——
        交给 `DataAgent` 内部 `model or SemanticModel()` 兜底会让 CLI 的 ask 路径
        绕过校验（`model_path` 恰好是 None 的那条路径）。

    Raises
    ------
    SnapshotUnavailable
        显式指定的 sha 无 meta（不回退）、`data/snapshots/` 全无 meta，
        或绑定快照缺少该语义模型声明的表（决策 ③）。
        CLI 捕获后打印并 exit 1，HTTP 层捕获后转 503（ADR-0012）。

    会话持久化（ADR-0020 决策 ①）
    ----------------------------
    `ATLAS_CHECKPOINT_DB` 非空 → 该路径的 SQLite checkpointer（重启不失忆）；
    空/未设 → `MemorySaver`（历史行为）。判断只在 `checkpoint_saver_from_env()`
    一处，CLI 与 HTTP 因此同语义——它们是同一个工厂（ADR-0012 理由 1「共源」）。
    """
    from agent.compiler import SemanticModel
    from eval.runner import build_budget, execute_sql

    snapshot = resolve_runtime_snapshot()
    budget = build_budget(snapshot.meta)
    model = SemanticModel(model_path) if model_path is not None else SemanticModel()
    _require_tables_in_snapshot(model, budget, snapshot)
    # 传整个 snapshot（不是 snapshot.meta）：/ask 要回显 source 与 bound_to_head，
    # 而 meta 里没有这两项——若 API 层为拿到它们再解析一次，就成了「两次解析可能
    # 得到不同快照」的第二事实源（ADR-0019 决策 ②/⑥）
    return DataAgent(
        model=model,
        executor=execute_sql,
        budget=budget,
        snapshot=snapshot,
        checkpointer=checkpoint_saver_from_env(),
    )
