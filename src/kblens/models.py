"""Data models for KBLens pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SUPPORTED_EXTENSIONS: set[str] = {
    # C/C++
    ".h",
    ".hpp",
    ".hxx",
    ".cpp",
    ".cc",
    ".cxx",
    ".c",
    # Python
    ".py",
    ".pyi",
    # TypeScript / JavaScript
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".mjs",
    ".cjs",
}

BINARY_EXTENSIONS: set[str] = {
    # Compiled
    ".o",
    ".obj",
    ".a",
    ".lib",
    ".so",
    ".dll",
    ".dylib",
    ".exe",
    ".pdb",
    ".class",
    ".jar",
    ".war",
    ".pyc",
    ".pyo",
    # Archives
    ".zip",
    ".tar",
    ".gz",
    ".bz2",
    ".7z",
    ".rar",
    # Media
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".bmp",
    ".ico",
    ".svg",
    ".mp3",
    ".wav",
    ".ogg",
    ".mp4",
    ".avi",
    ".mov",
    # Data / docs
    ".pdf",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".ppt",
    ".db",
    ".sqlite",
    ".dat",
    ".bin",
    # Fonts
    ".ttf",
    ".otf",
    ".woff",
    ".woff2",
    # Game / engine assets
    ".pak",
    ".asset",
    ".meta",
    ".fbx",
    ".uasset",
}

LANGUAGE_MAP: dict[str, list[str]] = {
    "cpp": [".h", ".hpp", ".cpp", ".cc", ".cxx", ".hxx"],
    "c": [".h", ".c"],
    "python": [".py", ".pyi"],
    "java": [".java"],
    "kotlin": [".kt", ".kts"],
    "go": [".go"],
    "rust": [".rs"],
    "typescript": [".ts", ".tsx"],
    "javascript": [".js", ".jsx", ".mjs"],
    "csharp": [".cs"],
    "swift": [".swift"],
    "ruby": [".rb"],
    "php": [".php"],
    "dart": [".dart"],
    "scala": [".scala"],
}

# ---------------------------------------------------------------------------
# Configuration models
# ---------------------------------------------------------------------------


@dataclass
class SourceDir:
    """One source directory to scan."""

    path: str
    name: str


@dataclass
class LLMConfig:
    """LLM connection settings."""

    model: str = "gpt-4o-mini"
    api_base: str | None = None
    api_key: str | None = None
    api_key_env: str | None = None
    temperature: float = 0.2
    max_concurrent: int = 8
    max_concurrent_components: int = 8
    # Resolved at runtime (not from YAML)
    _resolved_api_key: str = ""


@dataclass
class PackingConfig:
    """Token packing parameters."""

    token_budget: int = 8000
    token_min: int = 1000
    token_max: int = 24000
    component_split_threshold: int = 200


@dataclass
class Config:
    """Top-level configuration."""

    version: int = 1
    project: str = ""
    output_dir: str = "./kblens_kb"
    source_dirs: list[SourceDir] = field(default_factory=list)
    include_extensions: str | list[str] = "auto"
    exclude_patterns: list[str] = field(
        default_factory=lambda: [
            "*/test/*",
            "*/tests/*",
            "*/mock/*",
            "*/mocks/*",
            "*_test.*",
            "*.test.*",
            "**/node_modules/**",
            "**/__pycache__/**",
            "**/vendor/**",
            "**/third_party/**",
        ]
    )
    llm: LLMConfig = field(default_factory=LLMConfig)
    packing: PackingConfig = field(default_factory=PackingConfig)
    summary_language: str = "en"


# ---------------------------------------------------------------------------
# Pipeline data models
# ---------------------------------------------------------------------------


@dataclass
class Component:
    """A detected code component (package/subdir)."""

    source_name: str  # e.g. "packages"
    package_name: str  # e.g. "gameplay"
    name: str  # e.g. "AIDecisionLayer"
    path: Path  # absolute path on disk
    file_count: int = 0
    total_lines: int = 0

    @property
    def key(self) -> str:
        """Unique key: source/package/component."""
        return f"{self.source_name}/{self.package_name}/{self.name}"


@dataclass
class ASTEntry:
    """Extracted AST skeleton for one file."""

    rel_path: str  # relative path within component
    dir: str  # parent directory (relative)
    content: str  # extracted skeleton text
    tokens: int = 0  # estimated token count
    language: str = ""  # detected language id
    is_supplementary: bool = False  # True for ".cpp (extra)" entries


@dataclass
class Batch:
    """One LLM batch — a group of directories within token budget."""

    dirs: list[str]
    tokens: int
    group_key: str = ""

    def merge(self, other: Batch) -> None:
        """Merge another batch into this one (append)."""
        self.dirs.extend(other.dirs)
        self.tokens += other.tokens

    def merge_front(self, other: Batch) -> None:
        """Merge another batch before this one (prepend)."""
        self.dirs = other.dirs + self.dirs
        self.tokens += other.tokens


@dataclass
class AggGroup:
    """Tracks which batches belong to a split parent directory."""

    parent: str  # parent directory key
    batch_indices: list[int]  # indices into the batch list


@dataclass
class PackResult:
    """Output of Phase 3 packing."""

    batches: list[Batch] = field(default_factory=list)
    aggregation_groups: list[AggGroup] = field(default_factory=list)


@dataclass
class BatchSummary:
    """LLM output for one batch."""

    batch: Batch
    summary: str
    ast_content: str = ""  # raw AST text, appended to .md by writer
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class ComponentResult:
    """Complete processed result for one component."""

    component: Component
    overview: str = ""
    submodule_summaries: dict[str, str] = field(default_factory=dict)
    submodule_ast: dict[str, str] = field(default_factory=dict)  # raw AST per submodule
    detected_language: str = "cpp"  # for code block syntax highlighting
    batch_count: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0


@dataclass
class PackageResult:
    """Complete result for one package."""

    name: str
    source_name: str
    overview: str = ""
    components: list[ComponentResult] = field(default_factory=list)


@dataclass
class MetaInfo:
    """Generation metadata (_meta.json)."""

    generated_at: str = ""
    generator_version: str = ""
    config_hash: str = ""
    llm_model: str = ""
    total_components: int = 0
    total_summaries: int = 0
    total_tokens: dict[str, int] = field(default_factory=lambda: {"input": 0, "output": 0})
    components: dict[str, dict[str, Any]] = field(default_factory=dict)
