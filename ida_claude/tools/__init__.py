"""
Tool registry system.

Tools are registered using the @tool decorator and automatically
made available to the agent loop.
"""

import inspect
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, ParamSpec, TypeVar

__all__ = ["tool", "get_tools", "get_tool", "to_claude_format", "execute", "ToolDef"]

P = ParamSpec("P")
R = TypeVar("R")


@dataclass
class ToolDef:
    """Definition of a tool that Claude can call."""

    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema
    handler: Callable[..., Any]


# Global registry
_TOOLS: dict[str, ToolDef] = {}


def tool(
    name: str,
    description: str,
    parameters: dict[str, Any] | None = None,
):
    """
    Decorator to register a function as a tool.

    Args:
        name: Tool name (what Claude sees)
        description: What the tool does
        parameters: JSON Schema for parameters (auto-generated if not provided)

    Example:
        @tool(
            name="get_function",
            description="Get the decompiled code of a function",
        )
        def get_function(ea: str = None, name: str = None) -> dict:
            ...
    """

    def decorator(func: Callable[P, R]) -> Callable[P, R]:
        # Auto-generate schema from function signature if not provided
        schema = parameters
        if schema is None:
            schema = _generate_schema(func)

        _TOOLS[name] = ToolDef(
            name=name,
            description=description,
            parameters=schema,
            handler=func,
        )
        return func

    return decorator


def _generate_schema(func: Callable) -> dict[str, Any]:
    """Generate JSON Schema from function signature."""
    sig = inspect.signature(func)
    hints = getattr(func, "__annotations__", {})

    properties = {}
    required = []

    for param_name, param in sig.parameters.items():
        if param_name in ("self", "cls"):
            continue

        prop: dict[str, Any] = {}

        # Get type hint
        hint = hints.get(param_name)
        if hint is str:
            prop["type"] = "string"
        elif hint is int:
            prop["type"] = "integer"
        elif hint is bool:
            prop["type"] = "boolean"
        elif hint is float:
            prop["type"] = "number"
        elif hint is list:
            prop["type"] = "array"
        elif hint is dict:
            prop["type"] = "object"
        else:
            prop["type"] = "string"  # Default

        properties[param_name] = prop

        # Check if required (no default value)
        if param.default is inspect.Parameter.empty:
            required.append(param_name)

    return {
        "type": "object",
        "properties": properties,
        "required": required,
    }


def get_tools() -> dict[str, ToolDef]:
    """Get all registered tools."""
    return _TOOLS.copy()


def get_tool(name: str) -> ToolDef | None:
    """Get a specific tool by name."""
    return _TOOLS.get(name)


def to_claude_format() -> list[dict]:
    """Convert all tools to Claude's expected format."""
    return [
        {
            "name": t.name,
            "description": t.description,
            "input_schema": t.parameters,
        }
        for t in _TOOLS.values()
    ]


def execute(name: str, input: dict[str, Any]) -> Any:
    """
    Execute a tool by name with given input.

    Args:
        name: Tool name
        input: Tool arguments

    Returns:
        Tool result (will be JSON serialized for Claude)

    Raises:
        KeyError: If tool not found
        Exception: Whatever the tool raises
    """
    tool_def = _TOOLS.get(name)
    if not tool_def:
        raise KeyError(f"Unknown tool: {name}")

    return tool_def.handler(**input)


from . import ida  # noqa: F401, E402
