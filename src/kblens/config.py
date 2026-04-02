"""Configuration loading with layered override support.

Lookup order for config files:
  1. Explicit path via --config (if provided)
  2. ./kblens.yaml in current working directory
  3. ~/.config/kblens/config.yaml (user global config)

Each level can have a .local.yaml sibling that overrides it:
  kblens.yaml + kblens.local.yaml
  config.yaml   + config.local.yaml

Priority: environment variable > .local.yaml > base yaml
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml

from .models import Config, LLMConfig, PackingConfig, SourceDir

# Defaults live in the dataclass definitions in models.py — no duplication here.
_LLM_DEFAULTS = LLMConfig()
_PACKING_DEFAULTS = PackingConfig()
_CONFIG_DEFAULTS = Config()

# Default config search locations (in priority order)
USER_CONFIG_DIR = Path.home() / ".config" / "kblens"
USER_CONFIG_FILE = USER_CONFIG_DIR / "config.yaml"
LOCAL_CONFIG_FILE = Path("kblens.yaml")


class ConfigError(Exception):
    """Configuration error."""


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge *override* into *base* (mutates base)."""
    for key, value in override.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def _load_yaml(path: Path) -> dict:
    """Load a YAML file, returning empty dict if file doesn't exist."""
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _expand_path(p: str) -> str:
    """Expand ~ and environment variables in a path string."""
    return str(Path(os.path.expandvars(os.path.expanduser(p))).resolve())


def _parse_source_dirs(raw: list[dict] | None) -> list[SourceDir]:
    if not raw:
        return []
    result = []
    for item in raw:
        raw_path = item.get("path", "")
        if not raw_path:
            continue
        p = _expand_path(raw_path)
        n = item.get("name", "") or Path(p).name
        result.append(SourceDir(path=p, name=n))
    return result


def _parse_llm(raw: dict | None) -> LLMConfig:
    if not raw:
        return LLMConfig()
    d = _LLM_DEFAULTS
    return LLMConfig(
        model=raw.get("model", d.model),
        api_base=raw.get("api_base"),
        api_key=raw.get("api_key"),
        api_key_env=raw.get("api_key_env"),
        temperature=raw.get("temperature", d.temperature),
        max_concurrent=raw.get("max_concurrent", d.max_concurrent),
        max_concurrent_components=raw.get("max_concurrent_components", d.max_concurrent_components),
    )


def _parse_packing(raw: dict | None) -> PackingConfig:
    if not raw:
        return PackingConfig()
    d = _PACKING_DEFAULTS
    return PackingConfig(
        token_budget=raw.get("token_budget", d.token_budget),
        token_min=raw.get("token_min", d.token_min),
        token_max=raw.get("token_max", d.token_max),
        component_split_threshold=raw.get("component_split_threshold", d.component_split_threshold),
    )


def _resolve_api_key(llm: LLMConfig) -> str:
    """Resolve LLM API key from env var or config."""
    if llm.api_key_env:
        val = os.environ.get(llm.api_key_env)
        if val:
            return val
    val = os.environ.get("KBLENS_LLM_KEY")
    if val:
        return val
    if llm.api_key:
        return llm.api_key
    return ""


def find_config_file(explicit_path: str | None = None) -> Path | None:
    """Find the config file to use, searching default locations.

    Returns the path to the config file, or None if not found.
    """
    if explicit_path:
        p = Path(explicit_path)
        if p.exists():
            return p
        return None

    # Search in priority order
    if LOCAL_CONFIG_FILE.exists():
        return LOCAL_CONFIG_FILE.resolve()
    if USER_CONFIG_FILE.exists():
        return USER_CONFIG_FILE

    return None


def load_config(config_path: str | Path | None = None) -> Config:
    """Load configuration with two-layer merge.

    Layer 1 (global):  ~/.config/kblens/config.yaml  (shared LLM, packing, etc.)
    Layer 2 (project): ./kblens.yaml or explicit --config (project sources, output)

    If *config_path* is None, searches default locations:
      1. ./kblens.yaml  (project config in cwd)
      2. ~/.config/kblens/config.yaml  (global config)

    Each config file can have a .local.yaml sibling that overrides it.
    """
    # --- Layer 1: Global config (always loaded if it exists) ---
    global_base: dict = {}
    if USER_CONFIG_FILE.exists():
        global_base = _load_yaml(USER_CONFIG_FILE)
        global_local = USER_CONFIG_FILE.parent / "config.local.yaml"
        gl = _load_yaml(global_local)
        if gl:
            _deep_merge(global_base, gl)

    # --- Layer 2: Project config (overrides global) ---
    if config_path is not None:
        resolved = Path(config_path)
        if not resolved.exists():
            raise ConfigError(f"Config file not found: {resolved}")
    else:
        # Check for project config in cwd first
        if LOCAL_CONFIG_FILE.exists():
            resolved = LOCAL_CONFIG_FILE.resolve()
        elif global_base:
            # No project config, but global exists — use global only
            resolved = None
        else:
            raise ConfigError(
                "No configuration file found. Run 'kblens init' to create one,\n"
                f"or place kblens.yaml in the current directory.\n"
                f"  Searched: ./{LOCAL_CONFIG_FILE}, {USER_CONFIG_FILE}"
            )

    if resolved is not None:
        project_base = _load_yaml(resolved)
        # Look for .local.yaml sibling
        local_path = resolved.parent / (resolved.stem + ".local.yaml")
        local = _load_yaml(local_path)
        if local:
            _deep_merge(project_base, local)
        # Merge: project overrides global
        merged = _deep_merge(global_base, project_base)
    else:
        merged = global_base

    d = _CONFIG_DEFAULTS
    llm_cfg = _parse_llm(merged.get("llm"))
    # Support both "sources" and "source_dirs" keys
    raw_sources = merged.get("sources") or merged.get("source_dirs")
    cfg = Config(
        version=merged.get("version", d.version),
        project=merged.get("project", ""),
        output_dir=_expand_path(merged.get("output_dir", d.output_dir)),
        source_dirs=_parse_source_dirs(raw_sources),
        include_extensions=merged.get("include_extensions", d.include_extensions),
        exclude_patterns=merged.get("exclude_patterns", d.exclude_patterns),
        llm=llm_cfg,
        packing=_parse_packing(merged.get("packing")),
        summary_language=merged.get("summary_language", d.summary_language),
    )

    cfg.llm._resolved_api_key = _resolve_api_key(cfg.llm)
    return cfg


def require_api_key(config: Config) -> None:
    """Raise ConfigError if no API key is available."""
    if not config.llm._resolved_api_key:
        raise ConfigError(
            "LLM API key not found. Set it via:\n"
            "  1. llm.api_key in config file (or .local.yaml)\n"
            "  2. Environment variable (llm.api_key_env in config)\n"
            "  3. KBLENS_LLM_KEY environment variable"
        )


# ---------------------------------------------------------------------------
# Config file template for `kblens init`
# ---------------------------------------------------------------------------

CONFIG_TEMPLATE = """\
# KBLens Configuration
# Generated by: kblens init
#
# Sensitive values (API keys) can go in a sibling file:
#   config.local.yaml (same directory, gitignored)
version: 1

# Where to write the generated knowledge base
output_dir: "{output_dir}"

# Source directories to scan
source_dirs:
  - path: "{source_path}"
    name: "{source_name}"

# "auto" = detect from source; or explicit: ["*.h", "*.cpp"]
include_extensions: "auto"

exclude_patterns:
  - "*/test/*"
  - "*/tests/*"
  - "*/mock/*"
  - "*/mocks/*"
  - "*_test.*"
  - "*.test.*"
  - "**/node_modules/**"
  - "**/__pycache__/**"
  - "**/vendor/**"
  - "**/third_party/**"

llm:
  model: "{llm_model}"
  api_base: "{llm_api_base}"
  # api_key: "your-key-here"  # or use api_key_env / KBLENS_LLM_KEY
  temperature: 0.2
  max_concurrent: 8
  max_concurrent_components: 8

packing:
  token_budget: 8000
  token_min: 1000
  token_max: 24000
  component_split_threshold: 200

summary_language: "{summary_language}"
"""
