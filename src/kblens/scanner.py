"""Phase 1: Scan directory structure and discover components."""

from __future__ import annotations

import fnmatch
import logging
from pathlib import Path

from .models import (
    BINARY_EXTENSIONS,
    SUPPORTED_EXTENSIONS,
    Component,
    Config,
)

logger = logging.getLogger("kblens.scanner")


AUTO_DETECT_SAMPLE_LIMIT = 2000
DEFAULT_FALLBACK_EXTENSIONS = {".h", ".hpp", ".cpp", ".cc"}


def resolve_include_extensions(config: Config) -> set[str]:
    """Resolve which file extensions to include.

    - ``"auto"``: walk source dirs and collect all SUPPORTED_EXTENSIONS found.
    - explicit list: parse ``["*.h", "*.cpp"]`` → ``{".h", ".cpp"}``.
    """
    if config.include_extensions == "auto":
        detected: set[str] = set()
        for source in config.source_dirs:
            src_path = Path(source.path)
            if not src_path.is_dir():
                continue
            count = 0
            for f in src_path.rglob("*"):
                if f.is_file():
                    ext = f.suffix.lower()
                    if ext in SUPPORTED_EXTENSIONS:
                        detected.add(ext)
                    count += 1
                    # Only stop early after enough files AND all common C++ exts found
                    if count >= AUTO_DETECT_SAMPLE_LIMIT and detected >= {".h", ".cpp"}:
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


def phase1_scan(config: Config, include_exts: set[str] | None = None) -> list[Component]:
    """Scan source directories and return a list of components.

    Discovery hierarchy: source_dir / package / component.
    """
    if include_exts is None:
        include_exts = resolve_include_extensions(config)

    components: list[Component] = []

    for source in config.source_dirs:
        src_path = Path(source.path)
        if not src_path.is_dir():
            logger.warning("Source directory not found, skipping: %s", src_path)
            continue

        # Packages = direct children of source dir
        try:
            packages = sorted(
                p for p in src_path.iterdir() if p.is_dir() and not p.name.startswith(".")
            )
        except (OSError, PermissionError):
            continue

        for pkg_path in packages:
            pkg_name = pkg_path.name

            # Components = direct children of package
            _SKIP_DIRS = {"__pycache__", "node_modules", ".git", "__pypackages__"}
            try:
                comp_dirs = sorted(
                    c
                    for c in pkg_path.iterdir()
                    if c.is_dir() and not c.name.startswith(".") and c.name not in _SKIP_DIRS
                )
            except (OSError, PermissionError):
                continue

            if comp_dirs:
                # Standard 3-level layout: source/package/component/
                for comp_dir in comp_dirs:
                    # Check for sub-projects within the component
                    subprojects = detect_subprojects(comp_dir)

                    for sp_path in subprojects:
                        fc, tl = count_code_files(
                            sp_path, include_exts, config.exclude_patterns, sp_path
                        )
                        if fc == 0:
                            continue

                        # Name: if sub-project, use parent/child naming
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
            else:
                # Flat layout fallback: package dir itself contains code files
                # (common in Python projects: src/mypackage/*.py)
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
