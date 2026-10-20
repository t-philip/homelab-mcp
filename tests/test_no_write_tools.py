"""The read-only guarantee, checked over the real adapter set."""

from __future__ import annotations

from homelab_mcp.adapters import iter_declared_tools
from homelab_mcp.naming import tool_name_violation


def test_no_declared_tool_is_a_write_tool() -> None:
    offenders = {
        spec.name: reason
        for spec in iter_declared_tools()
        if (reason := tool_name_violation(spec.name)) is not None
    }
    assert offenders == {}


def test_declared_tool_names_are_unique() -> None:
    names = [spec.name for spec in iter_declared_tools()]
    assert len(names) == len(set(names)), f"duplicate tool names in {names}"


def test_every_declared_tool_has_a_description_and_async_handler() -> None:
    import inspect

    for spec in iter_declared_tools():
        assert spec.description.strip()
        assert inspect.iscoroutinefunction(spec.handler), spec.name
