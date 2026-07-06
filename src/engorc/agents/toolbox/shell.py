"""Shell execution: bash in the workroom with timeouts, output shaping,
and a denylist for the obviously catastrophic.

The command travels in the fenced payload (never JSON), runs in a fresh
subprocess (no persistent shell state — the prompt teaches `cmd1 && cmd2`),
and the observation keeps the head and tail of long output because the end
of a build/test log is where the truth lives.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

from .base import Tool, ToolContext, ToolResult

_DENYLIST = [
    re.compile(r"\brm\s+(-[a-zA-Z]*\s+)*(/|~)(\s|$)"),  # rm -rf / or ~
    re.compile(r"\bsudo\b"),
    re.compile(r"\b(shutdown|reboot|poweroff|halt)\b"),
    re.compile(r"\bmkfs\b"),
    re.compile(r"\bdd\s+[^|;]*of=/dev/"),
    re.compile(r":\(\)\s*\{.*\};\s*:"),  # forkbomb
    re.compile(r"\bgit\s+push\b"),  # publishing is the integrator/user's call, never an agent's
]


def _denied(command: str) -> str | None:
    for pattern in _DENYLIST:
        if pattern.search(command):
            return f"command blocked by policy: matches {pattern.pattern!r}"
    return None


def _subprocess_env() -> dict:
    """Commands must see the same python environment orc runs in: `python3`
    resolves to the venv interpreter even when orc was invoked by absolute
    path with no venv activated."""
    env = os.environ.copy()
    bin_dir = str(Path(sys.executable).parent)
    path = env.get("PATH", "")
    if not path.startswith(bin_dir):
        env["PATH"] = f"{bin_dir}{os.pathsep}{path}" if path else bin_dir
    return env


def run_command(ctx: ToolContext, command: str, timeout: float) -> ToolResult:
    reason = _denied(command)
    if reason:
        return ToolResult(ok=False, output=reason)
    try:
        proc = subprocess.run(
            ["bash", "-c", command],
            cwd=ctx.workroom,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_subprocess_env(),
        )
    except subprocess.TimeoutExpired:
        return ToolResult(ok=False, output=f"timed out after {timeout:.0f}s: {command[:200]}")
    output = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
    result = ToolResult(
        ok=proc.returncode == 0,
        output=f"exit code {proc.returncode}\n{output.strip()}" if output.strip() else f"exit code {proc.returncode}",
        data={"exit_code": proc.returncode},
    )
    return result


def run(ctx: ToolContext, args: dict, payload: str) -> ToolResult:
    command = payload.strip() or str(args.get("command", "")).strip()
    if not command:
        return ToolResult(ok=False, output="run needs the shell command in the fenced payload")
    timeout = min(float(args.get("timeout", ctx.config.run.shell_timeout)), 1800.0)
    return run_command(ctx, command, timeout)


SHELL_TOOLS = [
    Tool(
        name="run",
        summary="Run a bash command in the project root. Fresh shell each call — chain with && when state matters.",
        args_doc='{"timeout": 300}',
        payload_doc="the shell command(s)",
        handler=run,
        mutates=True,
    ),
]
