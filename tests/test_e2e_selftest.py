"""The crown jewel: the complete mission lifecycle, GPU-less, with a cold
restart in the middle. This is the same scenario `orc selftest` ships to
users; if it is green here, the orchestrator works on this machine."""

import pytest

from engorc.selftest import run_selftest


@pytest.mark.timeout(180)
def test_full_mission_lifecycle_with_resume(tmp_path):
    report = run_selftest(home=tmp_path / "home", keep=True)
    failures = [f"{c.name}: {c.detail}" for c in report.checks if not c.ok]
    assert report.ok, "\n".join(failures)
