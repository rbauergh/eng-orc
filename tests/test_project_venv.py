"""Per-project dependency sandboxes: agent commands run in the workroom venv."""

import sys
from pathlib import Path

from engorc.agents.toolbox import ALL_TOOLS, ToolContext
from engorc.agents.toolbox.shell import _subprocess_env, ensure_project_venv
from engorc.config import Config, IndexConfig, MemoryConfig, RunConfig
from engorc.events import Journal
from engorc.registry import Registry


def _venv_config(tmp_path, enabled: bool) -> Config:
    return Config(
        home=tmp_path / "home",
        memory=MemoryConfig(backend="local"),
        index=IndexConfig(enabled=False),
        run=RunConfig(project_venvs=enabled),
    )


def _ctx(config) -> ToolContext:
    project = Registry(config).create("venv mission", title="V")
    return ToolContext(project=project, config=config,
                       journal=Journal(project.root / "journal"))


def test_env_prefers_the_project_venv_when_present(tmp_path):
    ctx = _ctx(_venv_config(tmp_path, enabled=True))
    fake_bin = ctx.workroom / ".venv" / "bin"
    fake_bin.mkdir(parents=True)
    (fake_bin / "python3").write_text("")
    env = _subprocess_env(ctx)
    assert env["PATH"].startswith(str(fake_bin))
    assert env["VIRTUAL_ENV"] == str(fake_bin.parent)


def test_env_falls_back_to_orc_interpreter_without_a_venv(tmp_path):
    ctx = _ctx(_venv_config(tmp_path, enabled=False))
    env = _subprocess_env(ctx)
    assert env["PATH"].startswith(str(Path(sys.executable).parent))
    assert "VIRTUAL_ENV" not in env


def test_commands_actually_run_inside_the_created_venv(tmp_path, monkeypatch):
    import engorc.agents.toolbox.shell as shell_module

    # seeding hits the network; venv creation itself is offline and is the
    # part under test
    monkeypatch.setattr(shell_module, "_seed_project_venv", lambda python: True)
    ctx = _ctx(_venv_config(tmp_path, enabled=True))
    bin_dir = ensure_project_venv(ctx)
    assert bin_dir is not None and bin_dir.exists()
    result = ALL_TOOLS["run"].run(ctx, {}, "python3 -c 'import sys; print(sys.prefix)'")
    assert result.ok
    assert str(ctx.workroom / ".venv") in result.output
    # idempotent: a second ensure reuses the same venv
    assert ensure_project_venv(ctx) == bin_dir
