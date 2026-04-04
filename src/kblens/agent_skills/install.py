"""Install and uninstall bundled KBLens skills for supported agents."""

from __future__ import annotations

import shutil

from .registry import AGENT_SKILL_TARGETS, AgentSkillTarget, source_skill_dir


def is_installed(target: AgentSkillTarget) -> bool:
    """Return whether the bundled skill is installed for *target*."""
    if not target.install_dir:
        return False
    return (target.install_dir / "SKILL.md").exists()


def install_targets(targets: list[AgentSkillTarget], force: bool = False) -> dict[str, str]:
    """Install the bundled skill into all auto-installable *targets*."""
    src_dir = source_skill_dir()
    results: dict[str, str] = {}

    for target in targets:
        if not target.install_dir:
            results[target.key] = "manual"
            continue
        if target.install_dir.exists():
            if not force:
                results[target.key] = "exists"
                continue
            shutil.rmtree(target.install_dir)
        target.install_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src_dir, target.install_dir)
        results[target.key] = "installed"

    return results


def uninstall_targets(targets: list[AgentSkillTarget]) -> dict[str, str]:
    """Remove the bundled skill from all auto-installable *targets*."""
    results: dict[str, str] = {}

    for target in targets:
        if not target.install_dir:
            results[target.key] = "manual"
            continue
        if not target.install_dir.exists():
            results[target.key] = "missing"
            continue
        shutil.rmtree(target.install_dir)
        results[target.key] = "removed"

    return results


def skill_status_rows() -> list[dict[str, str]]:
    """Build status rows for CLI presentation."""
    rows: list[dict[str, str]] = []
    for target in AGENT_SKILL_TARGETS:
        install_path = str(target.install_dir) if target.install_dir else "manual-only"
        rows.append(
            {
                "key": target.key,
                "name": target.display_name,
                "installed": "yes" if is_installed(target) else "no",
                "auto": "yes" if target.supports_auto_install else "no",
                "path": install_path,
                "manual_help": target.manual_help,
            }
        )
    return rows
