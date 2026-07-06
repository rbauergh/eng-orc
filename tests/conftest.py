import os

os.environ.setdefault("ENGORC__LOG_LEVEL", "error")

import pytest

from engorc.config import Config, IndexConfig, MemoryConfig, RunConfig


@pytest.fixture()
def config(tmp_path) -> Config:
    return Config(
        home=tmp_path / "home",
        memory=MemoryConfig(backend="local"),
        index=IndexConfig(enabled=False),
        # real venv creation is exercised by its dedicated tests; everywhere
        # else it would just add seconds and a network dependency
        run=RunConfig(project_venvs=False),
    )


@pytest.fixture()
def project(config):
    from engorc.registry import Registry

    registry = Registry(config)
    return registry.create("Test mission: build a widget", title="Widget")
