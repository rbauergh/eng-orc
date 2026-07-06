"""Console output: one shared rich logger for the CLI and orchestrator.

Verbosity follows config.log_level (debug|info|warn|error). Agent activity
is rendered as compact single lines so a long build loop stays readable in
a terminal scrollback.
"""

from __future__ import annotations

import os

from rich.console import Console

_LEVELS = {"debug": 10, "info": 20, "warn": 30, "error": 40}


class Log:
    def __init__(self) -> None:
        self.console = Console(highlight=False)
        self.level = _LEVELS.get(os.environ.get("ENGORC__LOG_LEVEL", "info").lower(), 20)

    def set_level(self, name: str) -> None:
        self.level = _LEVELS.get(name.lower(), 20)

    def debug(self, message: str) -> None:
        if self.level <= 10:
            self.console.print(f"[dim]· {message}[/dim]")

    def info(self, message: str) -> None:
        if self.level <= 20:
            self.console.print(message)

    def success(self, message: str) -> None:
        if self.level <= 20:
            self.console.print(f"[green]✓[/green] {message}")

    def warn(self, message: str) -> None:
        if self.level <= 30:
            self.console.print(f"[yellow]⚠ {message}[/yellow]")

    def error(self, message: str) -> None:
        self.console.print(f"[red]✗ {message}[/red]")

    def agent(self, role: str, message: str) -> None:
        if self.level <= 20:
            self.console.print(f"[bold cyan]{role}[/bold cyan] {message}")

    def rule(self, title: str = "") -> None:
        if self.level <= 20:
            self.console.rule(title)


log = Log()
