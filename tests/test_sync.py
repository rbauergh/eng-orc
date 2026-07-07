"""orc sync's merge semantics: profile owns models/review, user run-tuning survives."""

import yaml

from engorc.config import apply_profile_to_config, repo_root


def test_apply_profile_replaces_models_and_merges_run(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({
        "server": {"base_url": "http://custom:1/v1"},
        "models": {"profile": "old", "coder": {"model": "stale"}},
        "run": {"clarification_budget": 5, "coder_fallbacks": ["old-fallback"]},
        "log_level": "debug",
    }))
    profile_path = tmp_path / "orc-models.yaml"
    profile_path.write_text(yaml.safe_dump({
        "models": {"profile": "new", "coder": {"model": "coder"}},
        "review": {"panel": [{"model_role": "coder", "lens": "correctness"}]},
        "run": {"coder_fallbacks": ["coder-fallback"]},
    }))

    applied = apply_profile_to_config(profile_path, config_path)
    assert applied == {"models": True, "review": True, "run": True}
    # a second run is a no-op and says so — "sync didn't change anything"
    # must be a visible fact, not a guess
    assert apply_profile_to_config(profile_path, config_path) == {
        "models": False, "review": False, "run": False,
    }
    merged = yaml.safe_load(config_path.read_text())
    assert merged["models"]["profile"] == "new"            # profile owns models
    assert merged["review"]["panel"][0]["lens"] == "correctness"
    assert merged["run"]["coder_fallbacks"] == ["coder-fallback"]  # profile key applied
    assert merged["run"]["clarification_budget"] == 5      # user tuning survives
    assert merged["server"]["base_url"] == "http://custom:1/v1"    # untouched sections survive
    assert merged["log_level"] == "debug"


def test_repo_root_finds_the_checkout():
    root = repo_root()
    assert root is not None
    assert (root / "server" / "profiles" / "balanced-12gb" / "orc-models.yaml").exists()
