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
            encoding="utf-8",
            errors="replace",  # diffs can carry raw binary bytes: git's text
            timeout=timeout,   # heuristic misses NUL-free binaries like WAV
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, f"git failed: {exc}"
    return proc.returncode, ((proc.stdout or "") + (proc.stderr or "")).strip()


GITIGNORE_SEED = """\
.venv/
__pycache__/
*.pyc
.pytest_cache/
.ruff_cache/
dist/
build/
*.egg-info/
node_modules/
.DS_Store
"""

_IGNORED_TRACKED = (".venv", "__pycache__", ".pytest_cache", ".ruff_cache",
                    "dist", "build", "node_modules")


def ensure_repo(workroom: Path) -> bool:
    """Initialize a repo (with a sane identity fallback) so history exists from
    the first attempt, and seed a .gitignore so build artifacts and the
    project venv never reach commits or review diffs. Returns True when the
    workroom is a git repo."""
    code, _ = _git(workroom, "rev-parse", "--git-dir")
    if code != 0:
        code, out = _git(workroom, "init", "-b", "main")
        if code != 0:
            return False
        for key, value in (("user.name", "eng-orc"), ("user.email", "eng-orc@local")):
            check_code, _ = _git(workroom, "config", key)
            if check_code != 0:
                _git(workroom, "config", key, value)
    gitignore = workroom / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(GITIGNORE_SEED, encoding="utf-8")
        # ignoring only stops FUTURE adds — junk already tracked (a committed
        # .venv) must be untracked and the change committed on its own, so it
        # never appears inside a work item's review diff
        _git(workroom, "rm", "-r", "--cached", "--quiet", "--ignore-unmatch",
             *_IGNORED_TRACKED)
        commit_all(workroom, "chore: ignore build artifacts and the project venv")
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


def workroom_dirty(workroom: Path) -> bool:
    """Uncommitted changes present (respects .gitignore)."""
    code, out = _git(workroom, "status", "--porcelain")
    return code == 0 and bool(out.strip())


def commit_all(workroom: Path, message: str) -> tuple[bool, str]:
    """Stage everything and commit; used by the integrator phase."""
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


# git's well-known empty-tree object: the diff base when HEAD is unborn
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


def diff_since(workroom: Path, ref: str) -> str:
    """Everything that changed since ref, INCLUDING brand-new files.

    `git diff` ignores untracked paths — and new work is mostly new files —
    so stage first (harmless: integration commits `git add -A` anyway), and
    diff against the empty tree when the repo has no commits yet."""
    if not ensure_repo(workroom):
        return ""
    _git(workroom, "add", "-A")
    base = ref or EMPTY_TREE
    code, out = _git(workroom, "diff", base)
    return out if code == 0 else ""


def tracked_files(workroom: Path, limit: int = 200) -> list[str]:
    _git(workroom, "add", "-A")
    code, out = _git(workroom, "ls-files")
    if code != 0:
        return []
    files = [line for line in out.splitlines() if line.strip()]
    return files[:limit]


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
