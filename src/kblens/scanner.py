"""Phase 1: Scan directory structure and discover components."""

from __future__ import annotations

import fnmatch
import logging
from pathlib import Path

from .models import (
    BINARY_EXTENSIONS,
    DOCUMENT_EXTENSIONS,
    SUPPORTED_EXTENSIONS,
    Component,
    Config,
)

logger = logging.getLogger("kblens.scanner")


AUTO_DETECT_SAMPLE_LIMIT = 2000
DEFAULT_FALLBACK_EXTENSIONS = {".h", ".hpp", ".cpp", ".cc"}

# Directories that should never be treated as packages or components
_SKIP_DIRS = frozenset(
    {
        "__pycache__",
        "node_modules",
        ".git",
        ".svn",
        ".hg",
        "__pypackages__",
        ".tox",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "venv",
        ".venv",
        "env",
        ".env",
    }
)


def resolve_include_extensions(config: Config) -> set[str]:
    """Resolve which file extensions to include.

    - ``"auto"``: walk source dirs and collect all SUPPORTED_EXTENSIONS found.
      For document sources, DOCUMENT_EXTENSIONS are used instead.
    - explicit list: parse ``["*.h", "*.cpp"]`` → ``{".h", ".cpp"}``.
    """
    if config.include_extensions == "auto":
        detected: set[str] = set()
        for source in config.source_dirs:
            src_path = Path(source.path)
            if not src_path.is_dir():
                continue
            # Choose the right extension set based on source type
            valid_exts = DOCUMENT_EXTENSIONS if source.type == "document" else SUPPORTED_EXTENSIONS
            count = 0
            for f in src_path.rglob("*"):
                if f.is_file():
                    ext = f.suffix.lower()
                    if ext in valid_exts:
                        detected.add(ext)
                    count += 1
                    # Stop early once we've sampled enough files and found at least one extension
                    if count >= AUTO_DETECT_SAMPLE_LIMIT and detected:
                        break
        return detected if detected else DEFAULT_FALLBACK_EXTENSIONS
    else:
        # Manual list: ["*.h", "*.cpp", ".h", "h"] → {".h", ".cpp"}
        result: set[str] = set()
        for e in config.include_extensions:
            ext = e.lstrip("*")
            if not ext.startswith("."):
                ext = f".{ext}"
            result.add(ext)
        return result


def _matches_exclude(rel_path: str, exclude_patterns: list[str]) -> bool:
    """Check if a relative path matches any exclude pattern."""
    # Normalize to forward slashes
    rel = rel_path.replace("\\", "/")
    for pattern in exclude_patterns:
        if fnmatch.fnmatch(rel, pattern):
            return True
        # Also check just the filename
        if fnmatch.fnmatch(rel.split("/")[-1], pattern):
            return True
    return False


def is_code_file(file_path: Path, include_exts: set[str]) -> bool:
    """Check whether a file should be included."""
    ext = file_path.suffix.lower()
    if ext in BINARY_EXTENSIONS:
        return False
    return ext in include_exts


def _is_skippable_dir(name: str) -> bool:
    """Check if a directory name should be skipped entirely."""
    return name.startswith(".") or name in _SKIP_DIRS


def count_code_files(
    directory: Path,
    include_exts: set[str],
    exclude_patterns: list[str],
    base_path: Path | None = None,
) -> tuple[int, int]:
    """Count code files and total lines in a directory.

    Returns (file_count, total_lines).
    """
    if base_path is None:
        base_path = directory
    file_count = 0
    total_lines = 0
    try:
        for f in directory.rglob("*"):
            if not f.is_file():
                continue
            rel = str(f.relative_to(base_path))
            if _matches_exclude(rel, exclude_patterns):
                continue
            if is_code_file(f, include_exts):
                file_count += 1
                try:
                    with open(f, "rb") as fh:
                        total_lines += sum(1 for _ in fh)
                except (OSError, PermissionError):
                    pass
    except (OSError, PermissionError):
        pass
    return file_count, total_lines


def _has_direct_code_files(
    directory: Path,
    include_exts: set[str],
    exclude_patterns: list[str],
) -> bool:
    """Check if directory has code files directly (not recursively)."""
    try:
        for f in directory.iterdir():
            if f.is_file() and is_code_file(f, include_exts):
                rel = f.name
                if not _matches_exclude(rel, exclude_patterns):
                    return True
    except (OSError, PermissionError):
        pass
    return False


def _count_direct_code_files(
    directory: Path,
    include_exts: set[str],
    exclude_patterns: list[str],
) -> tuple[int, int]:
    """Count code files directly in a directory (not recursively).

    Returns (file_count, total_lines).
    """
    file_count = 0
    total_lines = 0
    try:
        for f in directory.iterdir():
            if not f.is_file():
                continue
            if not is_code_file(f, include_exts):
                continue
            if _matches_exclude(f.name, exclude_patterns):
                continue
            file_count += 1
            try:
                with open(f, "rb") as fh:
                    total_lines += sum(1 for _ in fh)
            except (OSError, PermissionError):
                pass
    except (OSError, PermissionError):
        pass
    return file_count, total_lines


def _has_src_or_include(path: Path) -> bool:
    """Check if directory has its own src/ or include/ subdirectory with code."""
    for sub_name in ("src", "include", "Source", "Include"):
        sub = path / sub_name
        if sub.is_dir():
            # Check it has at least one file
            try:
                next(sub.rglob("*"))
                return True
            except StopIteration:
                pass
    return False


def detect_subprojects(comp_path: Path) -> list[Path]:
    """Detect independent sub-projects within a component.

    If multiple children each have their own src/ or include/,
    they are treated as separate sub-projects.
    Otherwise the component itself is one unit.
    """
    subprojects: list[Path] = []
    try:
        for child in sorted(comp_path.iterdir()):
            if child.is_dir() and _has_src_or_include(child):
                subprojects.append(child)
    except (OSError, PermissionError):
        pass
    # Only split if there are multiple; otherwise treat whole dir as one component
    if len(subprojects) > 1:
        return subprojects
    return [comp_path]


# ---------------------------------------------------------------------------
# Phase 1 entry point
# ---------------------------------------------------------------------------


def phase1_scan(config: Config, include_exts: set[str] | None = None) -> list[Component]:
    """Scan source directories and return a list of components.

    Uses a flexible discovery strategy that adapts to any project layout:

    1. Walk from each source root, looking for directories that contain code.
    2. Group discovered code directories into (package, component) pairs
       based on their relative path from the source root.
    3. For deep layouts (C++ engine: source/package/component/src/...),
       the first level is the package, the second+ levels are components.
    4. For flat layouts (Python: source/package/*.py), the package directory
       itself becomes a single component.
    5. Sub-project detection (multiple src/ children) is preserved for C++.

    This makes scanning project-structure-agnostic: any layout where code
    files live somewhere under the source root will be discovered.
    """
    if include_exts is None:
        include_exts = resolve_include_extensions(config)

    components: list[Component] = []

    for source in config.source_dirs:
        src_path = Path(source.path)
        if not src_path.is_dir():
            logger.warning("Source directory not found, skipping: %s", src_path)
            continue

        # Step 1: discover all directories that contain code files (recursively).
        # Each such directory is a candidate leaf. We then merge upward to form
        # meaningful (package, component) groupings.
        #
        # Strategy: enumerate direct children of source as "packages".
        # Within each package, find all code-bearing directories at any depth.

        try:
            top_dirs = sorted(
                p for p in src_path.iterdir() if p.is_dir() and not _is_skippable_dir(p.name)
            )
        except (OSError, PermissionError):
            continue

        # Source root may itself contain code files (e.g. src/index.ts, main.py)
        if _has_direct_code_files(src_path, include_exts, config.exclude_patterns):
            fc, tl = _count_direct_code_files(src_path, include_exts, config.exclude_patterns)
            if fc > 0:
                components.append(
                    Component(
                        source_name=source.name,
                        package_name="_root",
                        name="_root",
                        path=src_path,
                        file_count=fc,
                        total_lines=tl,
                    )
                )

        for pkg_path in top_dirs:
            pkg_name = pkg_path.name

            # Find child directories that are NOT skippable
            try:
                child_dirs = sorted(
                    c for c in pkg_path.iterdir() if c.is_dir() and not _is_skippable_dir(c.name)
                )
            except (OSError, PermissionError):
                child_dirs = []

            # Check if child dirs themselves contain code (deep layout)
            found_deep_components = False
            for comp_dir in child_dirs:
                # Sub-project detection for C++ (multiple src/ children)
                subprojects = detect_subprojects(comp_dir)

                for sp_path in subprojects:
                    fc, tl = count_code_files(
                        sp_path, include_exts, config.exclude_patterns, sp_path
                    )
                    if fc == 0:
                        continue

                    found_deep_components = True

                    if sp_path != comp_dir:
                        name = f"{comp_dir.name}/{sp_path.name}"
                    else:
                        name = comp_dir.name

                    components.append(
                        Component(
                            source_name=source.name,
                            package_name=pkg_name,
                            name=name,
                            path=sp_path,
                            file_count=fc,
                            total_lines=tl,
                        )
                    )

            # Package root entry files: always check if the package dir itself
            # has direct code files (e.g. __init__.py, index.ts, mod.rs).
            # These are often the public API entry point and should not be lost
            # even when sub-components exist.
            if found_deep_components:
                # Only count direct (non-recursive) files to avoid double-counting
                if _has_direct_code_files(pkg_path, include_exts, config.exclude_patterns):
                    fc, tl = _count_direct_code_files(
                        pkg_path, include_exts, config.exclude_patterns
                    )
                    if fc > 0:
                        components.append(
                            Component(
                                source_name=source.name,
                                package_name=pkg_name,
                                name=f"{pkg_name}._root",
                                path=pkg_path,
                                file_count=fc,
                                total_lines=tl,
                            )
                        )
            else:
                # No deep components — treat entire package as one flat component
                fc, tl = count_code_files(pkg_path, include_exts, config.exclude_patterns, pkg_path)
                if fc > 0:
                    components.append(
                        Component(
                            source_name=source.name,
                            package_name=pkg_name,
                            name=pkg_name,
                            path=pkg_path,
                            file_count=fc,
                            total_lines=tl,
                        )
                    )

    return components
