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


VENV_DIR = ".venv"


def project_venv_bin(workroom: Path) -> Path | None:
    bin_dir = workroom / VENV_DIR / "bin"
    if (bin_dir / "python3").exists() or (bin_dir / "python").exists():
        return bin_dir
    return None


def _seed_project_venv(python: Path) -> bool:
    """pip + pytest into a fresh project venv (needs the network once)."""
    try:
        proc = subprocess.run(
            [str(python), "-m", "pip", "install", "-q", "--upgrade", "pip", "pytest"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=300,
        )
        return proc.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


_FOREIGN_MARKERS = ("package.json", "Cargo.toml", "go.mod", "pom.xml", "build.gradle")


def _clearly_not_python(workroom: Path) -> bool:
    """Skip venv clutter in attached non-Python repos: another ecosystem's
    manifest present and not a single .py file. Greenfield (no files at all)
    still gets a venv — the first Python file may be minutes away."""
    if not any((workroom / marker).exists() for marker in _FOREIGN_MARKERS):
        return False
    for path in workroom.rglob("*.py"):
        if ".venv" not in path.parts:
            return False
    return True


def ensure_project_venv(ctx: ToolContext) -> Path | None:
    """The project's dependency sandbox: agents `pip install` into it, and
    verification runs against it, so nothing ever lands in orc's own env
    (or trips PEP 668 on the system python)."""
    if not ctx.config.run.project_venvs:
        return None
    existing = project_venv_bin(ctx.workroom)
    if existing is not None:
        return existing
    if _clearly_not_python(ctx.workroom):
        return None
    import venv as _venv

    from ...obs.console import log

    try:
        _venv.EnvBuilder(with_pip=True).create(str(ctx.workroom / VENV_DIR))
    except Exception as exc:
        log.warn(f"could not create the project venv ({exc}); commands fall back to orc's env")
        return None
    bin_dir = project_venv_bin(ctx.workroom)
    if bin_dir is None:
        return None
    python = bin_dir / "python3" if (bin_dir / "python3").exists() else bin_dir / "python"
    if _seed_project_venv(python):
        log.info(f"project venv ready at {ctx.workroom / VENV_DIR} (pip + pytest seeded)")
    else:
        log.warn("project venv created but seeding pip/pytest failed (offline?) — "
                 "agents can `pip install pytest` themselves once the network is back")
    return bin_dir


def _subprocess_env(ctx: ToolContext) -> dict:
    """Agent commands run inside the PROJECT's venv when it exists: pip
    installs stay project-local. Without one (disabled/failed), fall back to
    orc's own interpreter dir so `python3 -m pytest` still resolves."""
    env = os.environ.copy()
    env.pop("VIRTUAL_ENV", None)
    project_bin = project_venv_bin(ctx.workroom) if ctx.config.run.project_venvs else None
    if project_bin is not None:
        lead = str(project_bin)
        env["VIRTUAL_ENV"] = str(project_bin.parent)
    else:
        lead = str(Path(sys.executable).parent)
    path = env.get("PATH", "")
    if not path.startswith(lead):
        env["PATH"] = f"{lead}{os.pathsep}{path}" if path else lead
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
            encoding="utf-8",
            errors="replace",  # commands may emit raw binary bytes
            timeout=timeout,
            env=_subprocess_env(ctx),
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
