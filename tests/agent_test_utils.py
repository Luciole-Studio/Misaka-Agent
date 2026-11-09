"""Translated from pi's ``packages/agent/test/utils/{calculate,get-current-time,wait-for-tick}.ts`` (v1.1.0).

MISAKA fork: ``new Function(`return ${expression}`)`` becomes a small arithmetic evaluator (numbers, ``+ - * / %``,
``**`` and parentheses), which is all the tests hand it.
"""

from __future__ import annotations

import ast
import asyncio
import operator
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from misaka.agent.types import AgentTool, AgentToolResult
from misaka.ai.types import TextContent, Usage
from misaka.utils.js import jsString

_OPERATORS: dict[type, Any] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}


def _evaluate(node: ast.AST) -> Any:
    if isinstance(node, ast.Expression):
        return _evaluate(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, int | float):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPERATORS:
        return _OPERATORS[type(node.op)](_evaluate(node.left), _evaluate(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPERATORS:
        return _OPERATORS[type(node.op)](_evaluate(node.operand))
    raise SyntaxError("Unexpected token")


def calculate(expression: str) -> AgentToolResult:
    try:
        result = _evaluate(ast.parse(expression, mode="eval"))
        return AgentToolResult(content=[TextContent(text=f"{expression} = {jsString(result)}")], details=None)
    except Exception as e:
        raise RuntimeError(str(e) or type(e).__name__) from e


calculateSchema = {
    "type": "object",
    "properties": {"expression": {"type": "string", "description": "The mathematical expression to evaluate"}},
    "required": ["expression"],
}


async def _executeCalculate(_toolCallId: str, args: dict[str, Any], *_rest: Any) -> AgentToolResult:
    return calculate(args["expression"])


calculateTool = AgentTool(
    label="Calculator",
    name="calculate",
    description="Evaluate mathematical expressions",
    parameters=calculateSchema,
    execute=_executeCalculate,
)


def createCalculateToolWithUsage(usage: Usage) -> AgentTool:
    async def execute(_toolCallId: str, args: dict[str, Any], *_rest: Any) -> AgentToolResult:
        result = calculate(args["expression"])
        result.usage = usage
        return result

    return calculateTool.model_copy(update={"execute": execute})


async def getCurrentTime(timezone: str | None = None) -> AgentToolResult:
    date = datetime.now(UTC)
    if timezone:
        try:
            timeStr = date.astimezone(ZoneInfo(timezone)).strftime("%A, %B %d, %Y at %I:%M:%S %p %Z")
            return AgentToolResult(
                content=[TextContent(text=timeStr)], details={"utcTimestamp": int(date.timestamp() * 1000)}
            )
        except (ZoneInfoNotFoundError, ValueError) as _e:
            raise RuntimeError(f"Invalid timezone: {timezone}. Current UTC time: {date.isoformat()}") from _e
    timeStr = date.astimezone().strftime("%A, %B %d, %Y at %I:%M:%S %p %Z")
    return AgentToolResult(content=[TextContent(text=timeStr)], details={"utcTimestamp": int(date.timestamp() * 1000)})


getCurrentTimeSchema = {
    "type": "object",
    "properties": {
        "timezone": {"type": "string", "description": "Optional timezone (e.g., 'America/New_York', 'Europe/London')"}
    },
}


async def _executeGetCurrentTime(_toolCallId: str, args: dict[str, Any], *_rest: Any) -> AgentToolResult:
    return await getCurrentTime(args.get("timezone"))


getCurrentTimeTool = AgentTool(
    label="Current Time",
    name="get_current_time",
    description="Get the current date and time",
    parameters=getCurrentTimeSchema,
    execute=_executeGetCurrentTime,
)


async def waitForTick() -> None:
    """``new Promise((resolve) => setTimeout(resolve, 0))``."""
    await asyncio.sleep(0)
