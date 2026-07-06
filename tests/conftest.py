import os

os.environ.setdefault("ENGORC__LOG_LEVEL", "error")

import pytest

from engorc.config import Config, IndexConfig, MemoryConfig


@pytest.fixture()
def config(tmp_path) -> Config:
    return Config(
        home=tmp_path / "home",
        memory=MemoryConfig(backend="local"),
        index=IndexConfig(enabled=False),
    )


@pytest.fixture()
def project(config):
    from engorc.registry import Registry

    registry = Registry(config)
    return registry.create("Test mission: build a widget", title="Widget")
