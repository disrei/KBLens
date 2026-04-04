"""Registry of supported agent skill targets."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AgentSkillTarget:
    """A supported agent skill installation target."""

    key: str
    display_name: str
    detect_commands: tuple[str, ...]
    detect_dirs: tuple[Path, ...]
    install_dir: Path | None
    manual_help: str

    @property
    def supports_auto_install(self) -> bool:
        return self.install_dir is not None


_HOME = Path.home()

AGENT_SKILL_TARGETS: tuple[AgentSkillTarget, ...] = (
    AgentSkillTarget(
        key="claude-code",
        display_name="Claude Code",
        detect_commands=("claude",),
        detect_dirs=(_HOME / ".claude",),
        install_dir=_HOME / ".claude" / "skills" / "kblens-kb",
        manual_help="Install manually to ~/.claude/skills/kblens-kb/",
    ),
    AgentSkillTarget(
        key="opencode",
        display_name="OpenCode",
        detect_commands=("opencode",),
        detect_dirs=(_HOME / ".config" / "opencode",),
        install_dir=_HOME / ".config" / "opencode" / "skills" / "kblens-kb",
        manual_help="Install manually to ~/.config/opencode/skills/kblens-kb/",
    ),
    AgentSkillTarget(
        key="gemini-cli",
        display_name="Gemini CLI",
        detect_commands=("gemini",),
        detect_dirs=(_HOME / ".gemini", _HOME / ".agents"),
        install_dir=_HOME / ".gemini" / "skills" / "kblens-kb",
        manual_help="Install manually to ~/.gemini/skills/kblens-kb/ or ~/.agents/skills/kblens-kb/",
    ),
    AgentSkillTarget(
        key="codex",
        display_name="OpenAI Codex",
        detect_commands=("codex",),
        detect_dirs=(_HOME / ".codex",),
        install_dir=None,
        manual_help=(
            "Codex was detected, but KBLens does not auto-install to a Codex user skill directory "
            "because the official path could not be verified. Use the Codex docs or install as a "
            "project skill under .codex/skills/kblens-kb/."
        ),
    ),
)


def get_target(key: str) -> AgentSkillTarget | None:
    """Return a target by key."""
    for target in AGENT_SKILL_TARGETS:
        if target.key == key:
            return target
    return None


def detect_targets() -> list[AgentSkillTarget]:
    """Detect locally installed agent tools by command or config dir."""
    detected: list[AgentSkillTarget] = []
    for target in AGENT_SKILL_TARGETS:
        command_found = any(shutil.which(cmd) for cmd in target.detect_commands)
        dir_found = any(path.exists() for path in target.detect_dirs)
        if command_found or dir_found:
            detected.append(target)
    return detected


def source_skill_dir() -> Path:
    """Return the built-in kblens-kb skill directory bundled with the package."""
    return Path(__file__).resolve().parent.parent / "resources" / "skills" / "kblens-kb"
