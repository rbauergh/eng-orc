"""Layered configuration: model defaults ← ~/.eng-orc/config.yaml ← ENGORC__* env vars.

The config file is written by `orc init` (from a commented template) and is the
single place the user tunes servers, model roles, budgets, and memory backends.
Environment overrides use double-underscore paths, e.g.
ENGORC__SERVER__BASE_URL=http://127.0.0.1:9292/v1
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from .fsio import read_yaml

ENV_PREFIX = "ENGORC__"
HOME_ENV = "ENGORC_HOME"


def resolve_home() -> Path:
    return Path(os.environ.get(HOME_ENV, "~/.eng-orc")).expanduser().resolve()


class ServerConfig(BaseModel):
    base_url: str = "http://127.0.0.1:9292/v1"  # llama-swap OpenAI-compatible endpoint
    control_url: str = "http://127.0.0.1:9292"  # llama-swap root (running/unload/health)
    api_key: str = "eng-orc"  # llama-swap ignores it; kept for OpenAI-client compat
    embeddings_url: str | None = None  # defaults to base_url when unset
    connect_timeout: float = 10.0
    request_timeout: float = 900.0  # a 12 GB card generating 3k tokens can take minutes
    max_retries: int = 3


class RoleModel(BaseModel):
    """How one logical role (coder/planner/utility) maps onto a served model."""

    model: str
    context_window: int = 16384
    max_output_tokens: int = 3072
    temperature: float = 0.2
    top_p: float = 0.95
    thinking: bool = False  # model emits <think> blocks that must be stripped
    supports_schema: bool = True  # server enforces response_format json_schema
    extra_body: dict = Field(default_factory=dict)  # merged into every request,
    # e.g. {"chat_template_kwargs": {"enable_thinking": false}} to pin a Qwen3.x
    # mode so one resident model serves both coder (off) and planner (on) roles


class EmbedderModel(BaseModel):
    model: str = "embed"
    batch_size: int = 16
    max_chars: int = 6000  # guard against oversized chunks blowing the embed ctx


class ModelsConfig(BaseModel):
    # Context windows are set to the models' EFFECTIVE reasoning range for
    # quantized 7-30B (NoLiMa/RULER-style findings), not their advertised max.
    profile: str = "balanced-12gb"
    coder: RoleModel = Field(default_factory=lambda: RoleModel(model="coder", context_window=16384))
    planner: RoleModel = Field(
        default_factory=lambda: RoleModel(model="planner", context_window=12288, thinking=True)
    )
    utility: RoleModel = Field(
        default_factory=lambda: RoleModel(
            model="utility", context_window=8192, max_output_tokens=1024, temperature=0.1
        )
    )
    embedder: EmbedderModel = Field(default_factory=EmbedderModel)
    # Additional named models (review panelists, experiments). Referenced by
    # name anywhere a model role is accepted, e.g. review.panel entries.
    extra: dict[str, RoleModel] = Field(default_factory=dict)

    def for_role(self, role: str) -> RoleModel:
        if role in self.extra:
            return self.extra[role]
        try:
            value = getattr(self, role)
        except AttributeError as exc:
            raise KeyError(f"unknown model role: {role}") from exc
        if not isinstance(value, RoleModel):
            raise KeyError(f"{role} is not a chat model role")
        return value


class RunConfig(BaseModel):
    clarification_budget: int = 2  # blocking questions an agent may ask per phase
    max_attempts_per_item: int = 3
    max_turns_coder: int = 40
    max_turns_oneshot_tools: int = 12  # read-only exploration before charter/design
    review_required: bool = True
    compact_after_turns: int = 14  # tool-loop turns before history compaction
    # Model roles the implementer rotates to once the primary coder's attempts
    # on an item have failed (fresh weights bring fresh priors to stuck
    # problems). With max_attempts_per_item=3 and one fallback, attempts run:
    # coder, coder, fallback. Rotation only ever happens BETWEEN attempts —
    # within an attempt one consistent author owns the work.
    coder_fallbacks: list[str] = Field(default_factory=list)
    # How many times the planner may autonomously triage an exhausted item
    # (diagnose from the evidence, then revise/split/drop/retry it) before
    # the question escalates to the user.
    triage_rounds: int = 2
    max_tool_output_chars: int = 6000
    shell_timeout: float = 300.0
    verify_timeout: float = 600.0
    max_steps_per_project_visit: int = 1  # scheduler fairness: steps before rotating projects


class PanelReviewer(BaseModel):
    """One seat on the review panel: which model looks through which lens."""

    model_role: str = "coder"  # coder | planner | utility | any models.extra name
    lens: str = "correctness"  # see engorc.agents.roles.REVIEW_LENSES


class ReviewConfig(BaseModel):
    """Multi-model code review before sign-off.

    Every completed work item is judged by each panelist independently;
    blocking findings are unioned and the item only integrates when EVERY
    panelist approves. Weight diversity (different models) plus lens
    diversity (different failure modes) is how small local models approach
    big-model review quality — configure as many seats as you can afford
    swaps for. An unreachable panelist is skipped with a journal warning,
    never wedging the pipeline.
    """

    panel: list[PanelReviewer] = Field(default_factory=lambda: [PanelReviewer()])


class MemoryConfig(BaseModel):
    backend: Literal["auto", "letta", "local", "off"] = "auto"
    letta_base_url: str = "http://127.0.0.1:8283"
    letta_token: str | None = None
    letta_agent: str = "orc-librarian"
    letta_model_handle: str | None = None  # e.g. "openai/utility"; None = configure in Letta
    letta_embedding_handle: str | None = None
    timeout: float = 30.0
    recall_top_k: int = 5
    curate_with_agent: bool = True  # let the librarian agent digest wrap-up summaries


class IndexConfig(BaseModel):
    enabled: bool = True
    ignore: list[str] = Field(
        default_factory=lambda: [
            ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "__pycache__",
            "dist", "build", "target", ".idea", ".vscode", ".pytest_cache",
            ".ruff_cache", ".eng-orc", "*.lock", "*.min.*",
        ]
    )
    max_file_kb: int = 384
    chunk_lines: int = 60
    chunk_overlap_lines: int = 10
    max_chars_per_chunk: int = 3000
    top_k: int = 8
    repomap_tokens: int = 1600


class SchedulerConfig(BaseModel):
    poll_seconds: float = 5.0  # idle wait in `orc run --watch`
    gpu_lock_timeout: float = 3600.0


class Config(BaseModel):
    home: Path = Field(default_factory=resolve_home)

    # One canonical home path (symlinks resolved) keeps every derived path —
    # projects, workrooms, indexes — mutually comparable with relative_to().
    @field_validator("home", mode="after")
    @classmethod
    def _canonical_home(cls, value: Path) -> Path:
        return value.expanduser().resolve()
    server: ServerConfig = Field(default_factory=ServerConfig)
    models: ModelsConfig = Field(default_factory=ModelsConfig)
    run: RunConfig = Field(default_factory=RunConfig)
    review: ReviewConfig = Field(default_factory=ReviewConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    index: IndexConfig = Field(default_factory=IndexConfig)
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    log_level: str = "info"

    @property
    def projects_dir(self) -> Path:
        return self.home / "projects"

    @property
    def gpu_lock_path(self) -> Path:
        return self.home / "locks" / "gpu.lock"

    @property
    def config_path(self) -> Path:
        return self.home / "config.yaml"


def _deep_merge(base: dict, override: dict) -> dict:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _coerce(raw: str) -> Any:
    lowered = raw.lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    if lowered in ("null", "none", ""):
        return None
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        return raw


def _env_overrides() -> dict:
    overrides: dict = {}
    for key, raw in os.environ.items():
        if not key.startswith(ENV_PREFIX):
            continue
        path = [part.lower() for part in key[len(ENV_PREFIX):].split("__") if part]
        if not path:
            continue
        node = overrides
        for part in path[:-1]:
            node = node.setdefault(part, {})
        node[path[-1]] = _coerce(raw)
    return overrides


def load_config(home: Path | None = None) -> Config:
    home = home or resolve_home()
    data: dict = {"home": str(home)}
    file_data = read_yaml(home / "config.yaml", default={})
    if isinstance(file_data, dict):
        data = _deep_merge(data, file_data)
        data["home"] = str(home)  # the file cannot relocate its own home
    data = _deep_merge(data, _env_overrides())
    data.pop("home_env", None)
    return Config.model_validate(data)


@lru_cache(maxsize=1)
def get_config() -> Config:
    return load_config()


def reset_config_cache() -> None:
    get_config.cache_clear()


CONFIG_TEMPLATE = """\
# eng-orc configuration. Environment overrides: ENGORC__SECTION__KEY=value
# (e.g. ENGORC__SERVER__BASE_URL=http://127.0.0.1:9292/v1)

server:
  base_url: http://127.0.0.1:9292/v1     # llama-swap OpenAI-compatible endpoint
  control_url: http://127.0.0.1:9292     # llama-swap root, used for health/running/unload
  request_timeout: 900.0

models:
  profile: balanced-12gb                 # matches server/profiles/<name>.yaml
  coder:      {model: coder,   context_window: 24576, max_output_tokens: 3072, temperature: 0.2}
  planner:    {model: planner, context_window: 16384, max_output_tokens: 4096, thinking: true}
  utility:    {model: utility, context_window: 8192,  max_output_tokens: 1024, temperature: 0.1}
  embedder:   {model: embed,   batch_size: 16}

run:
  clarification_budget: 2                # blocking questions per phase before assuming-and-proceeding
  max_attempts_per_item: 3
  max_turns_coder: 40
  review_required: true

review:
  # Every panelist reviews each completed item; ALL must approve to sign off.
  # Add seats (and models.extra definitions) for more independent eyes —
  # each distinct model costs one swap per completed item.
  panel:
    - {model_role: coder, lens: correctness}
    # - {model_role: second-opinion, lens: adversarial}   # defined under models.extra

memory:
  backend: auto                          # auto = Letta when reachable, local SQLite otherwise
  letta_base_url: http://127.0.0.1:8283
  letta_agent: orc-librarian

index:
  enabled: true
  top_k: 8
  repomap_tokens: 1600

log_level: info
"""
