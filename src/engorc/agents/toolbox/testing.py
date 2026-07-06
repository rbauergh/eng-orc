"""Test execution: the deterministic verifier and the agent-facing run_tests tool.

Verification never involves an LLM. A work item is "verified" when every one
of its verify_commands exits 0 (falling back to the auto-detected project
test command). The report is structured so routing decisions read exit codes,
not prose.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from ...util import truncate_tail
from .base import Tool, ToolContext, ToolResult
from .shell import run_command


class CommandResult(BaseModel):
    command: str
    exit_code: int
    tail: str = ""


class VerifyReport(BaseModel):
    passed: bool
    results: list[CommandResult] = Field(default_factory=list)

    def summary(self) -> str:
        if not self.results:
            return "no verification commands ran"
        lines = []
        for result in self.results:
            mark = "PASS" if result.exit_code == 0 else f"FAIL({result.exit_code})"
            lines.append(f"[{mark}] {result.command}")
        return "\n".join(lines)

    def failure_detail(self, max_chars: int = 3000) -> str:
        for result in self.results:
            if result.exit_code != 0:
                return truncate_tail(result.tail, max_chars)
        return ""


def detect_test_command(workroom: Path) -> str | None:
    """Best-effort default when a work item declares no verify_commands."""
    if (workroom / "pyproject.toml").exists() or (workroom / "pytest.ini").exists():
        return "python3 -m pytest -q --color=no"
    if any(workroom.glob("tests/test_*.py")) or any(workroom.glob("test_*.py")):
        return "python3 -m pytest -q --color=no"
    if (workroom / "package.json").exists():
        text = (workroom / "package.json").read_text(encoding="utf-8", errors="replace")
        if '"test"' in text:
            return "npm test --silent"
    if (workroom / "Cargo.toml").exists():
        return "cargo test --quiet"
    if (workroom / "go.mod").exists():
        return "go test ./..."
    if (workroom / "Makefile").exists():
        makefile = (workroom / "Makefile").read_text(encoding="utf-8", errors="replace")
        if "\ntest:" in makefile or makefile.startswith("test:"):
            return "make test"
    return None


def run_verification(
    ctx: ToolContext,
    verify_commands: list[str] | None = None,
    timeout: float | None = None,
) -> VerifyReport:
    commands = [c for c in (verify_commands or []) if c.strip()]
    if not commands:
        detected = detect_test_command(ctx.workroom)
        if detected:
            commands = [detected]
    if not commands:
        return VerifyReport(passed=False, results=[
            CommandResult(command="(none)", exit_code=1, tail="no verify commands and no test setup detected")
        ])
    timeout = timeout or ctx.config.run.verify_timeout
    report = VerifyReport(passed=True)
    for command in commands:
        result = run_command(ctx, command, timeout)
        exit_code = int(result.data.get("exit_code", 1)) if result.data else (0 if result.ok else 1)
        report.results.append(
            CommandResult(command=command, exit_code=exit_code, tail=truncate_tail(result.output, 4000))
        )
        if exit_code != 0:
            report.passed = False
            break  # later commands usually depend on earlier ones
    return report


def run_tests(ctx: ToolContext, args: dict, payload: str) -> ToolResult:
    verify_commands = None
    if ctx.extras.get("verify_commands"):
        verify_commands = list(ctx.extras["verify_commands"])
    report = run_verification(ctx, verify_commands)
    detail = report.failure_detail()
    output = report.summary() + (f"\n\n{detail}" if detail else "")
    return ToolResult(ok=report.passed, output=output, data={"passed": report.passed})


TESTING_TOOLS = [
    Tool(
        name="run_tests",
        summary="Run this task's verification commands (or the project test suite).",
        args_doc="{}",
        payload_doc="",
        handler=run_tests,
    ),
]
