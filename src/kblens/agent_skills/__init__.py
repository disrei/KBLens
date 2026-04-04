"""Agent skill installation helpers."""

from .install import install_targets, skill_status_rows, uninstall_targets
from .registry import (
    AGENT_SKILL_TARGETS,
    AgentSkillTarget,
    detect_targets,
    get_target,
    source_skill_dir,
)

__all__ = [
    "AGENT_SKILL_TARGETS",
    "AgentSkillTarget",
    "detect_targets",
    "get_target",
    "install_targets",
    "skill_status_rows",
    "source_skill_dir",
    "uninstall_targets",
]
