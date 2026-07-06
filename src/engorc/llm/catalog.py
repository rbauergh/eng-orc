"""Agent-role → model-role resolution.

Agent roles are behavioral personas; model roles are VRAM-shaped slots
(coder / planner / utility / embedder) defined in config and mirrored by the
llama-swap profile. Grouping agents onto few model slots is what lets the
scheduler batch work by resident model and minimize swaps on a single GPU.
"""

from __future__ import annotations

from ..config import Config, RoleModel


def model_role_for_agent(agent_role: str) -> str:
    from ..agents.roles import ROLES  # roles.py owns the persona → slot mapping

    spec = ROLES.get(agent_role)
    if spec is not None:
        return spec.model_role
    return "utility" if agent_role in ("summarizer", "integrator") else "coder"


def model_for_agent(config: Config, agent_role: str) -> RoleModel:
    return config.models.for_role(model_role_for_agent(agent_role))


def chat_model_roles(config: Config) -> dict[str, RoleModel]:
    roles = {
        "coder": config.models.coder,
        "planner": config.models.planner,
        "utility": config.models.utility,
    }
    roles.update(config.models.extra)
    return roles
