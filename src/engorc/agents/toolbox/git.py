"""Git tools scoped to the workroom. Commits are the integrator's job;
push is intentionally absent (publishing stays a human decision)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from ...util import truncate_middle
from .base import Tool, ToolContext, ToolResult


def _git(workroom: Path, *argv: str, timeout: float = 60) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            ["git", "-C", str(workroom), *argv],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, f"git failed: {exc}"
    return proc.returncode, ((proc.stdout or "") + (proc.stderr or "")).strip()


def ensure_repo(workroom: Path) -> bool:
    """Initialize a repo (with a sane identity fallback) so history exists from
    the first attempt. Returns True when the workroom is a git repo."""
    code, _ = _git(workroom, "rev-parse", "--git-dir")
    if code == 0:
        return True
    code, out = _git(workroom, "init", "-b", "main")
    if code != 0:
        return False
    for key, value in (("user.name", "eng-orc"), ("user.email", "eng-orc@local")):
        check_code, _ = _git(workroom, "config", key)
        if check_code != 0:
            _git(workroom, "config", key, value)
    return True


def git_status(ctx: ToolContext, args: dict, payload: str) -> ToolResult:
    code, out = _git(ctx.workroom, "status", "--short", "--branch")
    return ToolResult(ok=code == 0, output=out or "clean")


def git_diff(ctx: ToolContext, args: dict, payload: str) -> ToolResult:
    argv = ["diff", "--stat", "--patch"]
    if args.get("staged"):
        argv.insert(1, "--cached")
    code, out = _git(ctx.workroom, *argv)
    return ToolResult(ok=code == 0, output=truncate_middle(out or "no changes", 8000))


def git_log(ctx: ToolContext, args: dict, payload: str) -> ToolResult:
    n = str(min(int(args.get("n", 10)), 50))
    code, out = _git(ctx.workroom, "log", "--oneline", "-n", n)
    return ToolResult(ok=code == 0, output=out or "no commits yet")


def commit_all(workroom: Path, message: str) -> tuple[bool, str]:
    """Stage everything and commit; used by the integrator phase."""
    if not ensure_repo(workroom):
        return False, "workroom is not a git repository and could not be initialized"
    _git(workroom, "add", "-A")
    code, out = _git(workroom, "status", "--porcelain")
    if code == 0 and not out.strip():
        return True, "nothing to commit"
    code, out = _git(workroom, "commit", "-m", message)
    if code != 0:
        return False, out
    _, sha = _git(workroom, "rev-parse", "--short", "HEAD")
    return True, sha


def head_sha(workroom: Path) -> str:
    code, out = _git(workroom, "rev-parse", "--short", "HEAD")
    return out if code == 0 else ""


def diff_since(workroom: Path, ref: str) -> str:
    if not ref:
        code, out = _git(workroom, "diff", "HEAD")
        if code == 0 and out:
            return out
        code, out = _git(workroom, "show", "--stat", "--patch", "HEAD")
        return out if code == 0 else ""
    code, out = _git(workroom, "diff", f"{ref}..HEAD")
    base = out if code == 0 else ""
    code, dirty = _git(workroom, "diff", "HEAD")
    if code == 0 and dirty:
        base = base + "\n" + dirty if base else dirty
    return base


GIT_TOOLS = [
    Tool(
        name="git_status",
        summary="Working-tree status (short form).",
        args_doc="{}",
        payload_doc="",
        handler=git_status,
    ),
    Tool(
        name="git_diff",
        summary="Diff of uncommitted changes (add {\"staged\": true} for the index).",
        args_doc="{}",
        payload_doc="",
        handler=git_diff,
    ),
    Tool(
        name="git_log",
        summary="Recent commits, one line each.",
        args_doc='{"n": 10}',
        payload_doc="",
        handler=git_log,
    ),
]
