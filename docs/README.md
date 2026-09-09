# docs/ 文档索引

> 2026-09-03 清理：移出内部执行记录（逐日任务清单 / 复盘 retro-* / 发布操作单 / blockers，
> 均为开发过程叙事，不面向仓库读者）。清理后本目录只保留两类资产——**对外读者可核验的证据
> 文档**与**发布/宣传素材**。被清理文档仍完整存在于 git 历史：
> `git log --all --oneline -- docs/逐日任务清单.md` 可取回。

## 文档清单

| 文件 | 用途 | 读者 |
|---|---|---|
| `README.md`（仓库根） | 项目唯一事实源：架构 / 快速开始 / 验证勾选 / 评测 / 限制 | 所有读者 |
| `release-notes-v0.1.md` | v0.1 发布说明（范围 / 复现 / 目录导览） | 发布读者 |
| `GLOSSARY.md` | 术语表（Metric / Measure / Plan / Guard 等，禁止混用） | 协作者 |
| `baseline-compiler.md` | 确定性编译器基线分析（零 LLM 覆盖 48/48 对照） | 评测读者 |
| `p1-acceptance.md` | P1 确定性主链验收（5 道 gates 明细） | 验证读者 |
| `e2e-acceptance.md` | Data Agent 端到端验收（5+1 场景，含 handoff） | 验证读者 |

## 证据链约定

- **数字只来自脚本产物**：`eval/reports/<git sha>.json` 与 `EVAL_REPORT.md` 是机器同源
  入口；本目录文档只转述、不发明数字（AGENTS.md §9）。
- **README §3.3** 为开发期逐日实测勾选记录（Day 编号 = 历史验证时间线，已封存不再逐日更新）；
  其引用的验收文档即上表 p1/e2e/baseline 三篇。
- **仓库根其他文档**（EVAL_REPORT.md / ADR 目录）不属 docs/，见 README §9 命令速查。

> 其中复盘类文档的部分结论（如 CI 依赖修复教训、评测方法修正）已沉淀进代码注释、
> 测试与 Known Limitations，不因文档移除而丢失。
