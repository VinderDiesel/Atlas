"""MCP 风格工具服务（Day 45）：把确定性工具四件套以 MCP 工具形态暴露。

设计（任务书口径：工具描述不可信，需参数校验 + 作用域控制）
------------------------------------------------------------
- **MCP 风格**：list_tools（name/description/inputSchema）+ call_tool 的
  JSON-RPC 式调用与结果形态（{"content": [{"type": "text", "text": json}]}，
  错误走 isError 结果而非异常）——非完整 MCP SDK 实现（无 transport 层，
  同步本地调用；MVP 不需要，接入 stdio/SSE 时本层可直接挂 SDK）。
- **描述不可信**：description 只是给 LLM 看的提示词，可能被诱导或伪造；
  真实防线三层：① inputSchema JSON Schema 严格校验（未知键拒绝——
  additionalProperties: false，与 registry 白名单键一致）→ ② registry
  真身参数校验 → ③ Guard 只读执行（execute_readonly）。
- **作用域控制**：Scope 裁剪可见工具集（list_tools 只返回允许的工具，
  LLM 看不到被禁工具，避免诱导）+ 调用时二次拦截；execute_readonly 是
  最高风险工具（任意只读 SQL），默认不可见不可调，须显式 allow_execute_sql。
- **错误不裸抛**：调用结果统一 isError + error.type 分类
  （unknown_tool / scope_denied / validation_error / tool_error /
  internal_error），服务端不留堆栈（调用方是 LLM 编排，结果形态要稳定）。

已知边界（MVP，诚实声明）
------------------------
- 作用域粒度 = 工具级 + execute_readonly 总开关；表级/行级约束由 Guard
  budget 与 RowPolicy 承接（Day 45 不新增权限模型，见 AGENTS.md）。
- 无认证/审计协议层：调用留痕在 registry（DeterministicTools.calls），
  会话身份关联由上层（serving/auth，第 6 周未开工）负责。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from jsonschema import ValidationError, validate

from agent.tools.registry import DeterministicTools, ToolError

# 工具结果：MCP 风格（isError=False 为成功；错误时 error.type 分类）
ToolResult = dict[str, Any]

_EMPTY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}

_TIME_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "granularity": {
            "type": "string",
            "enum": ["year", "quarter", "month", "date"],
            "description": "时间粒度（dim_date 物理列约定，见 agent/compiler.py）",
        },
        "value": {
            "anyOf": [
                {"type": "integer"},
                {"type": "string", "minLength": 1},
            ],
            "description": "取值：year=2013 / quarter='2013Q2' / month=201307 / date='2017-07-07'",
        },
    },
    "required": ["granularity", "value"],
    "additionalProperties": False,
}

_FILTER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "column": {
            "type": "string",
            "minLength": 1,
            "description": (
                "过滤列：已注册字段（→ WHERE），或等于本 Plan 的 metric 名"
                "（→ 度量阈值 HAVING）；具体清单由 registry 校验"
            ),
        },
        "op": {
            "type": "string",
            "enum": ["=", "!=", "<", "<=", ">", ">="],
            "description": "比较操作符（与 Compiler 支持集一致）",
        },
        "value": {
            "anyOf": [
                {"type": "integer"},
                {"type": "number"},
                {"type": "string", "minLength": 1},
            ],
            "description": "标量比较值（不接受对象/数组/null/布尔）",
        },
    },
    "required": ["column", "op", "value"],
    "additionalProperties": False,
}

_COMPILE_PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "metric": {
            "type": "string",
            "description": "已注册指标名（先用 list_metrics 查询可用清单）",
        },
        "dimensions": {
            "type": "array",
            "items": {"type": "string"},
            "description": "分组维度字段（可缺省）",
        },
        "time": _TIME_SCHEMA,
        "filters": {
            "type": "array",
            "items": _FILTER_SCHEMA,
            "description": "过滤条件（可缺省）：维度等值/阈值→WHERE，column==metric 时→HAVING",
        },
        "order_by": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "column": {"type": "string", "description": "metric 或维度字段名"},
                    "desc": {"type": "boolean"},
                },
                "required": ["column"],
                "additionalProperties": False,
            },
            "description": "排序（可缺省）",
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": 1000,
            "description": "返回行数上限（默认 100）",
        },
    },
    "required": ["metric"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class ToolSpec:
    """工具元数据：description 仅供 LLM 参考（不可信），inputSchema 才是防线。"""

    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True)
class Scope:
    """调用作用域：谁可以调什么。

    allowed_tools    : None = 注册表全量（execute_readonly 除外）；否则 = 允许子集。
    allow_execute_sql: execute_readonly（任意只读 SQL）默认禁用，须显式放行。
    """

    allowed_tools: frozenset[str] | None = None
    allow_execute_sql: bool = False


_TOOLS = (
    ToolSpec(
        name="list_metrics",
        description=(
            "列出全部已注册指标目录（名称/描述/owner/同义词数），确定性输出。"
            "回答任何问数前若不确定口径，先查本目录。"
        ),
        input_schema=_EMPTY_SCHEMA,
    ),
    ToolSpec(
        name="describe_metric",
        description=(
            "查看单个指标的口径详情：表达式/同义词/owner/描述。"
            "参数 metric 必须来自 list_metrics 输出（大小写敏感）。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "metric": {"type": "string", "description": "已注册指标名"},
            },
            "required": ["metric"],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        name="compile_sql",
        description=(
            "把结构化 Plan（指标/维度/时间/过滤/排序/行数）确定性编译为只读 SQL。"
            "不执行；执行请用 execute_readonly。"
        ),
        input_schema=_COMPILE_PLAN_SCHEMA,
    ),
    ToolSpec(
        name="execute_readonly",
        description=(
            "对只读 SQL 过 Guard 后执行（白名单表 + LIMIT 注入，越权语句拒绝）。"
            "返回行与列。高风险工具：仅当调用方被显式授权。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "sql": {"type": "string", "minLength": 1, "description": "只读 SELECT"},
            },
            "required": ["sql"],
            "additionalProperties": False,
        },
    ),
)

_TOOLS_BY_NAME = {t.name: t for t in _TOOLS}


def _ok(data: dict[str, Any]) -> ToolResult:
    """成功结果：text 为 JSON（ensure_ascii=False，中文可读）。"""
    return {
        "isError": False,
        "content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False, default=str)}],
    }


def _fail(kind: str, message: str) -> ToolResult:
    """错误结果：稳定结构 + 分类，服务端不裸抛（LLM 编排友好）。"""
    return {
        "isError": True,
        "error": {"type": kind, "message": message},
        "content": [
            {
                "type": "text",
                "text": json.dumps(
                    {"error": {"type": kind, "message": message}},
                    ensure_ascii=False,
                    default=str,
                ),
            }
        ],
    }


class McpToolServer:
    """MCP 风格工具服务：元数据裁剪 + 参数校验 + 作用域拦截 + registry 转发。"""

    def __init__(self, tools: DeterministicTools, scope: Scope | None = None) -> None:
        self._tools = tools
        self._scope = scope or Scope()

    def list_tools(self) -> dict[str, Any]:
        """作用域内可见工具元数据（MCP list_tools 形态，被禁工具不出现）。"""
        return {
            "tools": [
                {
                    "name": spec.name,
                    "description": spec.description,
                    "inputSchema": spec.input_schema,
                }
                for spec in _TOOLS
                if self._permitted(spec.name)
            ]
        }

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> ToolResult:
        """按 MCP 风格调用工具；任何失败都以 isError 结果返回（不裸抛）。

        参数
        ----
        name      : 工具名（list_tools 输出）。
        arguments : 工具入参 dict（来自调用方，不可信；缺省 {}）。

        返回
        ----
        成功：{"isError": False, "content": [{"type": "text", "text": <json>}]}
        失败：isError=True + error.type ∈ unknown_tool / scope_denied /
              validation_error / tool_error / internal_error
        """
        spec = _TOOLS_BY_NAME.get(name)
        if spec is None:
            return _fail("unknown_tool", f"工具不存在：{name}")
        if not self._permitted(name):
            return _fail("scope_denied", f"工具 {name} 不在当前作用域（或被禁用）")
        args = arguments if isinstance(arguments, dict) else {}
        try:
            validate(args, spec.input_schema)
        except ValidationError as exc:
            return _fail("validation_error", f"参数校验失败：{exc.message}")
        try:
            result = self._dispatch(spec.name, args)
        except ToolError as exc:
            return _fail("tool_error", str(exc))
        except Exception as exc:  # noqa: BLE001 - 内部意外错误稳定返回，不留堆栈
            return _fail("internal_error", f"内部错误：{type(exc).__name__}: {exc}")
        return _ok(result)

    # -- 内部实现 ----------------------------------------------------------

    def _permitted(self, name: str) -> bool:
        if name == "execute_readonly":
            # 任意只读 SQL 是最高风险工具：默认禁用，显式 allow_execute_sql 才放行
            return self._scope.allow_execute_sql
        return self._scope.allowed_tools is None or name in self._scope.allowed_tools

    def _dispatch(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if name == "list_metrics":
            return self._tools.list_metrics()
        if name == "describe_metric":
            return self._tools.describe_metric(str(args["metric"]))
        if name == "compile_sql":
            return self._tools.compile_sql(args)
        if name == "execute_readonly":
            return self._tools.execute_readonly(str(args["sql"]))
        raise ToolError(f"未注册的调度目标：{name}")  # 理论不可达（name 已查 specs）
