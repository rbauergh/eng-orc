from .base import Tool, ToolContext, ToolResult, render_tool_docs
from .control import CONTROL_TOOLS
from .fs import FS_TOOLS
from .git import GIT_TOOLS, ensure_repo
from .search import SEARCH_TOOLS
from .shell import SHELL_TOOLS, ensure_project_venv
from .testing import TESTING_TOOLS, run_verification

ALL_TOOLS: dict[str, Tool] = {
    tool.name: tool
    for tool in (*FS_TOOLS, *SEARCH_TOOLS, *SHELL_TOOLS, *GIT_TOOLS, *TESTING_TOOLS, *CONTROL_TOOLS)
}


def tools_named(*names: str) -> list[Tool]:
    return [ALL_TOOLS[name] for name in names]


__all__ = [
    "Tool",
    "ToolContext",
    "ToolResult",
    "render_tool_docs",
    "ALL_TOOLS",
    "tools_named",
    "ensure_repo",
    "ensure_project_venv",
    "run_verification",
]
