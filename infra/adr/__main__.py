"""ADR 脚手架（`make adr TITLE="xxx"` 的实现，AGENTS.md §11 第 1 步）。

为什么需要它：§11 要求"无 ADR 则先写 ADR 模板"，但 `infra/adr/` 只存放 .md，
`python -m infra.adr` 会被 PEP 420 当成 namespace package 而报
"No module named infra.adr.__main__"——流程第一步的工具本身是坏的，于是新决策
长期不留档（实证：ADR-0017 被 9 处代码引用却不存在，时间智能功能先于决策记录
落地）。本模块把编号分配与模板填充机械化，消除"手写编号会撞号/漏号"的借口。

模板七段沿用 infra/adr/README.md 与 ADR-0011/0012 的实际结构（README 强调 ADR 的
价值在于"为什么没选另一个"与"什么情况下应该推翻"，故后者单独成段）。

用法：
    make adr TITLE="维度值域注册"                 # 中文标题，需同时给 SLUG
    make adr TITLE="维度值域注册" SLUG="value-domain"
    python -m infra.adr new "标题" --slug value-domain
    python -m infra.adr list                      # 现有编号与标题
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import date
from pathlib import Path

ADR_DIR = Path(__file__).resolve().parent
# 既有文件名惯例：0016-dimension-value-domain.md（四位编号 + 英文短横线 slug）
ADR_FILE = re.compile(r"^(\d{4})-([a-z0-9-]+)\.md$")

TEMPLATE = """# ADR-{num}：{title}

- 日期：{today}
- 状态：proposed
- 相关：<链接到代码 / 评测报告 / 其他 ADR>

---

## 背景

<为什么要做这个决策，约束是什么。约束要引 AGENTS.md 的红线条目与既有 ADR，
不重复论述——ADR 之间靠引用连成网。>

## 备选方案

| 方案 | 优势 | 劣势 |
|---|---|---|
| **<选定方案>** | | |
| <被否方案 1> | | |
| <被否方案 2> | | |

## 决策

<选了什么。多项子决策用 ①②③ 分节（沿 ADR-0016 惯例），每节自成一段可独立
推翻的粒度。>

### ①

### ②

## 理由

<为什么选它，关键权衡是什么。这一段是评审时最常被追问的，不写"因为更好"。>

## 代价与限制

<这个决策带来什么问题。**必须是实测或代码级可验证的事实**，不写"可能有性能
影响"这类无法证伪的话（AGENTS.md N1/N2）。>

## 什么情况下应该推翻

<触发条件要可观测：出现某个需求 / 某个维护成本持续高 / 某个上游项目到达某
版本。写不出触发条件说明决策边界没想清。>

## 验证方式

<如何证明这个决策是对的，什么数据能证伪。优先指向可执行入口（make 目标、
tests/ 契约测试、eval/reports/ 产物），不写"人工检查"。>
"""


def existing() -> list[tuple[int, str, str]]:
    """已存在的 ADR：[(编号, slug, 标题首行)]，按编号升序。

    标题从文件首行 `# ADR-XXXX：<标题>` 提取，取不到则空串（不猜）。
    """
    out: list[tuple[int, str, str]] = []
    for path in sorted(ADR_DIR.glob("*.md")):
        m = ADR_FILE.match(path.name)
        if not m:
            continue
        first = path.read_text(encoding="utf-8").splitlines()[0] if path.stat().st_size else ""
        title = first.split("：", 1)[1].strip() if "：" in first else ""
        out.append((int(m.group(1)), m.group(2), title))
    return out


def next_number() -> int:
    """下一个可用编号（现有最大 +1；目录无 ADR 时从 1 起）。"""
    nums = [n for n, _, _ in existing()]
    return (max(nums) + 1) if nums else 1


def _slug_from(title: str) -> str:
    """从标题提取英文 slug；纯中文标题提不出则返回空串（由调用方要求显式 SLUG）。

    不做翻译——自动译名会与人工命名漂移，且翻译质量无法验证。
    """
    words = re.findall(r"[A-Za-z][A-Za-z0-9]*", title)
    return "-".join(w.lower() for w in words)


def new(title: str, slug: str | None = None) -> Path:
    """生成新 ADR 模板文件，返回路径。

    Parameters
    ----------
    title : 决策标题（中文可），写进首行 `# ADR-XXXX：<标题>`。
    slug : 文件名英文 slug；None 时从标题的 ASCII 词提取，提不出则报错。

    Raises
    ------
    SystemExit
        标题为空 / slug 无法确定 / 目标文件已存在（拒绝覆盖既有决策记录）。
    """
    title = title.strip()
    if not title:
        raise SystemExit("[error] TITLE 为空：make adr TITLE=\"决策标题\"")
    slug = (slug or _slug_from(title)).strip().lower()
    if not slug:
        raise SystemExit(
            "[error] 无法从纯中文标题推断文件名 slug（不做自动翻译）："
            '请显式给出，如 make adr TITLE="维度值域注册" SLUG="value-domain"'
        )
    if not re.fullmatch(r"[a-z0-9-]+", slug):
        raise SystemExit(f"[error] slug 只允许小写字母/数字/短横线，收到：{slug}")
    num = next_number()
    path = ADR_DIR / f"{num:04d}-{slug}.md"
    if path.exists():
        raise SystemExit(f"[error] {path.name} 已存在，拒绝覆盖既有决策记录")
    path.write_text(
        TEMPLATE.format(num=f"{num:04d}", title=title, today=date.today().isoformat()),
        encoding="utf-8",
    )
    return path


def main(argv: list[str] | None = None) -> int:
    """CLI 入口：`new <title> [--slug S]` 或 `list`。"""
    parser = argparse.ArgumentParser(prog="python -m infra.adr", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_new = sub.add_parser("new", help="生成下一个编号的 ADR 模板")
    p_new.add_argument("title", help="决策标题（中文可）")
    p_new.add_argument("--slug", default=None, help="文件名英文 slug（纯中文标题必填）")
    sub.add_parser("list", help="列出现有 ADR 编号与标题")
    args = parser.parse_args(argv)

    if args.cmd == "list":
        rows = existing()
        if not rows:
            print("(infra/adr/ 下无 ADR)")
            return 0
        for num, slug, title in rows:
            print(f"{num:04d}  {slug:42s} {title}")
        print(f"\n下一个编号：{next_number():04d}")
        return 0

    path = new(args.title, args.slug)
    print(f"[ok] 已生成 {path.relative_to(ADR_DIR.parent.parent)}")
    print("     状态为 proposed：填完七段后改 accepted，并在 commit message 说明影响面")
    return 0


if __name__ == "__main__":
    sys.exit(main())
