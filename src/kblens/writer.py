"""Phase 6: Write Markdown output files and metadata.

Supports both full writes and incremental (per-component) writes for
resume-from-checkpoint behaviour.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__
from .models import (
    ComponentResult,
    Config,
    MetaInfo,
    PackageResult,
)

logger = logging.getLogger("kblens.writer")


def _ensure_dir(path: Path) -> None:
    """Create directory (and parents) if needed."""
    path.mkdir(parents=True, exist_ok=True)


def _write_file(path: Path, content: str) -> None:
    """Write text to file, creating parent dirs as needed."""
    _ensure_dir(path.parent)
    path.write_text(content, encoding="utf-8")


def _leaf_output_name(sub_name: str) -> str:
    """Return a stable Markdown filename for a leaf summary."""
    safe = sub_name.replace("/", "_").replace("\\", "_")
    return safe if safe.lower().endswith(".md") else f"{safe}.md"


# ---------------------------------------------------------------------------
# Full write (used at the end for package overviews + INDEX)
# ---------------------------------------------------------------------------


def write_knowledge_base(
    config: Config,
    index_md: str | None,
    packages: dict[str, PackageResult],
    meta_dict: dict[str, Any],
) -> None:
    """Write package overviews, INDEX.md, and final _meta.json.

    If *index_md* is None the INDEX.md file is left unchanged on disk.
    """
    out = Path(config.output_dir)
    _ensure_dir(out)

    # INDEX.md (skip if None — no dirty packages)
    if index_md is not None:
        _write_file(out / "INDEX.md", index_md)

    # Per-package overview
    for pkg_key, pkg_data in packages.items():
        source_dir = out / pkg_data.source_name
        _write_file(source_dir / f"{pkg_data.name}.md", pkg_data.overview)

    # Final _meta.json (merge with incremental data already on disk)
    save_meta(config.output_dir, meta_dict)


# ---------------------------------------------------------------------------
# Incremental write — called after each component is done
# ---------------------------------------------------------------------------


def _append_ast_section(summary: str, ast_text: str, language: str = "cpp") -> str:
    """Append raw content section to a leaf summary.

    For code (language != "markdown"), wraps in a fenced code block under
    "Complete API Signatures".  For documents (language == "markdown"),
    appends the original content directly under "Original Content".
    """
    if not ast_text or not ast_text.strip():
        return summary
    if language == "markdown":
        return summary + "\n\n---\n\n## Original Content\n\n" + ast_text.strip()
    return (
        summary
        + f"\n\n---\n\n## Complete API Signatures\n\n```{language}\n"
        + ast_text.strip()
        + "\n```"
    )


def strip_ast_section(text: str) -> str:
    """Remove the appended AST signatures section from .md content.

    The AST section is separated by a ``\\n---\\n`` marker added by
    ``_append_ast_section``.  Everything from the **first** marker onward
    is stripped.  For small components whose .md contains multiple
    sub-module sections (also ``---``-delimited), this effectively keeps
    only the top-level overview — which is the intended input for
    higher-level (package / index) LLM prompts.
    """
    marker = "\n---\n"
    idx = text.find(marker)
    if idx >= 0:
        return text[:idx].rstrip()
    return text


_ASSET_DIRS = frozenset({"_images"})
_ASSET_PLACEHOLDER_PREFIX = "__kblens_asset__/"
_MARKDOWN_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")


def _rewrite_asset_placeholders(text: str, md_path: Path, asset_dir: Path) -> str:
    """Resolve internal asset placeholders to paths relative to *md_path*."""
    marker = f"({_ASSET_PLACEHOLDER_PREFIX}"
    if marker not in text:
        return text

    rel_prefix = os.path.relpath(asset_dir, md_path.parent).replace("\\", "/")
    if rel_prefix == ".":
        rel_prefix = ""

    def _replace(match: re.Match[str]) -> str:
        rel_asset = match.group(1)
        target = f"{rel_prefix}/{rel_asset}" if rel_prefix else rel_asset
        return f"({target})"

    return re.sub(r"\(__kblens_asset__/([^)]+)\)", _replace, text)


def rewrite_legacy_asset_refs(text: str, md_path: Path, asset_dir: Path) -> str:
    """Rewrite legacy ``_images/...`` refs to the component asset directory."""
    rel_prefix = os.path.relpath(asset_dir, md_path.parent).replace("\\", "/")
    if rel_prefix == ".":
        rel_prefix = ""

    def _replace(match: re.Match[str]) -> str:
        alt = match.group(1)
        raw_path = match.group(2).strip().replace("\\", "/")
        if not raw_path.startswith("_images/"):
            return match.group(0)
        target = f"{rel_prefix}/{raw_path}" if rel_prefix else raw_path
        return f"![{alt}]({target})"

    return _MARKDOWN_IMAGE_RE.sub(_replace, text)


def _component_asset_dir(
    pkg_comp_dir: Path,
    comp_name_safe: str,
    batch_count: int,
    file_count: int,
    threshold: int,
) -> Path:
    """Return the component-scoped asset directory in KB output."""
    if batch_count <= 1 and file_count < threshold:
        return pkg_comp_dir / "__assets" / comp_name_safe
    return pkg_comp_dir / comp_name_safe / "__assets"


def _copy_component_assets(source_path: Path, asset_dir: Path) -> None:
    """Copy component-local asset directories into a component-scoped asset root.

    This keeps image references in Markdown resolvable.  Only copies
    directories listed in ``_ASSET_DIRS`` that exist directly under
    *source_path*.
    """
    if asset_dir.exists():
        shutil.rmtree(asset_dir)

    copied = False
    for dirname in _ASSET_DIRS:
        src = source_path / dirname
        if not src.is_dir():
            continue
        dst = asset_dir / dirname
        shutil.copytree(src, dst)
        logger.debug("Copied %s → %s", src, dst)
        copied = True

    if not copied and asset_dir.exists():
        shutil.rmtree(asset_dir)


def migrate_component_assets(
    source_path: Path,
    package_dir: Path,
    comp_name_safe: str,
    markdown_paths: list[Path],
) -> int:
    """Migrate legacy component markdown and assets to component-scoped layout.

    Returns the number of rewritten markdown files.
    """
    is_large = (package_dir / comp_name_safe).is_dir()
    asset_dir = (
        package_dir / comp_name_safe / "__assets"
        if is_large
        else package_dir / "__assets" / comp_name_safe
    )
    _copy_component_assets(source_path, asset_dir)

    rewritten = 0
    for md_path in markdown_paths:
        if not md_path.is_file():
            continue
        original = md_path.read_text(encoding="utf-8")
        updated = rewrite_legacy_asset_refs(original, md_path, asset_dir)
        if updated != original:
            md_path.write_text(updated, encoding="utf-8")
            rewritten += 1
    return rewritten


def write_component_incremental(config: Config, cr: ComponentResult) -> None:
    """Write a single component's Markdown immediately after it is generated.

    This ensures progress is persisted even if the process is interrupted.
    """
    out = Path(config.output_dir)
    comp = cr.component
    pkg_comp_dir = out / comp.source_name / comp.package_name
    comp_name_safe = comp.name.replace("/", "_")
    threshold = config.packing.component_split_threshold
    asset_dir = _component_asset_dir(
        pkg_comp_dir,
        comp_name_safe,
        cr.batch_count,
        comp.file_count,
        threshold,
    )

    def _write_component_file(path: Path, content: str) -> None:
        _write_file(path, _rewrite_asset_placeholders(content, path, asset_dir))

    if cr.batch_count <= 1 and comp.file_count < threshold:
        # Small component -> single .md
        content = cr.overview
        if cr.skipped_reason and not cr.submodule_summaries and not cr.submodule_ast:
            _write_component_file(pkg_comp_dir / f"{comp_name_safe}.md", content)
            if comp.path.is_dir():
                _copy_component_assets(comp.path, asset_dir)
            return
        # Only append submodule summaries if Phase 5b was NOT skipped
        # (when Phase 5b is skipped, overview already contains the leaf content)
        if cr.submodule_summaries and len(cr.submodule_summaries) > 1:
            for sub_name, sub_text in sorted(cr.submodule_summaries.items()):
                # Skip if sub_name is empty or just "." - it's a placeholder
                if sub_name and sub_name != ".":
                    if not sub_text.strip():
                        logger.warning("Skipping empty submodule summary: %s", sub_name)
                        continue
                    combined = _append_ast_section(
                        sub_text, cr.submodule_ast.get(sub_name, ""), cr.detected_language
                    )
                    content += f"\n\n---\n\n## {sub_name}\n\n{combined}"
        elif cr.submodule_ast:
            # Single batch, Phase 5b skipped — append AST to the overview
            single_key = next(iter(cr.submodule_ast), "")
            if single_key:
                content = _append_ast_section(
                    content, cr.submodule_ast[single_key], cr.detected_language
                )
        else:
            # No AST content available (should not happen in normal flow)
            if cr.submodule_summaries:
                logger.warning(
                    "Component %s has summaries but no AST content to append",
                    comp.key,
                )
        _write_component_file(pkg_comp_dir / f"{comp_name_safe}.md", content)
    else:
        # Large component -> overview + submodule directory
        _write_component_file(pkg_comp_dir / f"{comp_name_safe}.md", cr.overview)
        if cr.submodule_summaries:
            sub_dir = pkg_comp_dir / comp_name_safe
            for sub_name, sub_text in sorted(cr.submodule_summaries.items()):
                if not sub_text.strip():
                    logger.warning("Skipping empty submodule summary: %s", sub_name)
                    continue
                combined = _append_ast_section(
                    sub_text, cr.submodule_ast.get(sub_name, ""), cr.detected_language
                )
                _write_component_file(sub_dir / _leaf_output_name(sub_name), combined)

    # Copy asset directories (_images/, etc.) from source to KB output so
    # that image references in the Markdown remain resolvable.
    if comp.path.is_dir():
        _copy_component_assets(comp.path, asset_dir)


# Lock for thread-safe meta updates (asyncio tasks may write concurrently)
_meta_lock = threading.Lock()


def save_meta_component(
    output_dir: str | Path,
    comp_key: str,
    comp_meta: dict[str, Any],
    llm_model: str = "",
) -> None:
    """Atomically update _meta.json with one component's metadata.

    Called right after ``write_component_incremental`` so that a subsequent
    ``generate`` run can detect which components are already done.
    """
    with _meta_lock:
        meta = load_meta(output_dir)
        meta["components"][comp_key] = comp_meta
        meta["total_components"] = len(meta["components"])
        if llm_model:
            meta["llm_model"] = llm_model
        _recompute_meta_aggregates(meta)
        meta["generated_at"] = datetime.now(timezone.utc).isoformat()
        save_meta(output_dir, meta)


def save_meta_failed(
    output_dir: str | Path,
    comp_key: str,
    error_msg: str,
    comp_path: str = "",
    llm_model: str = "",
) -> None:
    """Record a failed component in _meta.json so it can be retried later.

    A subsequent ``generate`` run sees ``status: "failed"`` and will
    re-process this component even if the source hash hasn't changed.
    """
    with _meta_lock:
        meta = load_meta(output_dir)
        meta["components"][comp_key] = {
            "path": comp_path,
            "status": "failed",
            "error": error_msg,
            "last_updated": datetime.now(timezone.utc).isoformat(),
        }
        meta["total_components"] = len(meta["components"])
        if llm_model:
            meta["llm_model"] = llm_model
        _recompute_meta_aggregates(meta)
        meta["generated_at"] = datetime.now(timezone.utc).isoformat()
        save_meta(output_dir, meta)


def _recompute_meta_aggregates(meta: dict[str, Any]) -> None:
    """Recompute total_tokens and total_summaries from component records."""
    total_in = 0
    total_out = 0
    total_summaries = 0
    for comp in meta.get("components", {}).values():
        tokens = comp.get("tokens", {})
        total_in += tokens.get("input", 0)
        total_out += tokens.get("output", 0)
        # Count this component as a summary if it's not failed
        if comp.get("status") != "failed":
            total_summaries += 1
    meta["total_tokens"] = {"input": total_in, "output": total_out}
    meta["total_summaries"] = total_summaries


# ---------------------------------------------------------------------------
# Checkpoint check — used at generate start to skip completed components
# ---------------------------------------------------------------------------


def is_component_done(
    meta: dict[str, Any],
    comp_key: str,
    comp_path: Path,
    include_exts: set[str] | None = None,
    exclude_patterns: list[str] | None = None,
) -> bool:
    """Check if a component was already generated and source hasn't changed.

    Returns False for components marked ``status: "failed"`` so they are
    retried automatically.
    """
    existing = meta.get("components", {}).get(comp_key)
    if not existing:
        return False
    # Failed or partial components should always be retried
    if existing.get("status") in ("failed", "partial"):
        return False
    old_hash = existing.get("source_hash", "")
    if not old_hash:
        return False
    current_hash = compute_source_hash(comp_path, include_exts, exclude_patterns)
    if old_hash == current_hash:
        return True

    # Fast hash changed. If we have a stored content hash, confirm whether the
    # underlying file bytes actually changed before forcing a regeneration.
    old_content_hash = existing.get("content_hash", "")
    if not old_content_hash:
        return False

    current_content_hash = compute_source_content_hash(comp_path, include_exts, exclude_patterns)
    return old_content_hash == current_content_hash


# ---------------------------------------------------------------------------
# Cleanup deleted components
# ---------------------------------------------------------------------------


def cleanup_deleted_components(
    output_dir: str | Path,
    current_keys: set[str],
    meta: dict[str, Any],
) -> list[str]:
    """Remove .md files and meta entries for components no longer in source.

    Returns list of deleted component keys.
    """
    out = Path(output_dir)
    meta_keys = set(meta.get("components", {}).keys())
    deleted_keys = meta_keys - current_keys

    for dk in sorted(deleted_keys):
        # Remove from meta
        meta["components"].pop(dk, None)
        logger.info("Cleaned up deleted component: %s", dk)

        # Remove .md file and submodule directory
        parts = dk.split("/", 2)  # source/package/name
        if len(parts) < 3:
            continue
        source_name, pkg_name, comp_name = parts
        comp_name_safe = comp_name.replace("/", "_")
        md_path = out / source_name / pkg_name / f"{comp_name_safe}.md"
        if md_path.exists():
            md_path.unlink()
            logger.info("  Removed: %s", md_path)
        sub_dir = out / source_name / pkg_name / comp_name_safe
        if sub_dir.is_dir():
            shutil.rmtree(sub_dir)
            logger.info("  Removed dir: %s", sub_dir)

        # Clean up empty package directory
        pkg_dir = out / source_name / pkg_name
        if pkg_dir.is_dir() and not any(pkg_dir.iterdir()):
            pkg_dir.rmdir()
            logger.info("  Removed empty package dir: %s", pkg_dir)

    if deleted_keys:
        meta["total_components"] = len(meta["components"])
        _recompute_meta_aggregates(meta)
        save_meta(output_dir, meta)

    return sorted(deleted_keys)


# ---------------------------------------------------------------------------
# Meta helpers
# ---------------------------------------------------------------------------


def load_meta(output_dir: str | Path) -> dict[str, Any]:
    """Load existing _meta.json or return empty structure."""
    meta_path = Path(output_dir) / "_meta.json"
    if meta_path.exists():
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {
        "generated_at": "",
        "generator_version": __version__,
        "config_hash": "",
        "llm_model": "",
        "total_components": 0,
        "total_summaries": 0,
        "total_tokens": {"input": 0, "output": 0},
        "components": {},
    }


def save_meta(output_dir: str | Path, meta: dict[str, Any]) -> None:
    """Write _meta.json to disk."""
    _write_file(
        Path(output_dir) / "_meta.json",
        json.dumps(meta, indent=2, ensure_ascii=False),
    )


def compute_source_hash(
    comp_path: Path,
    include_exts: set[str] | None = None,
    exclude_patterns: list[str] | None = None,
) -> str:
    """Compute a fast hash based on file relative-path + mtime + size.

    When *include_exts* is given, only files with matching extensions are
    included in the hash.  When *exclude_patterns* is given, files matching
    any pattern are skipped (consistent with scan/AST extraction).
    """
    from .scanner import _matches_exclude

    parts: list[str] = []
    try:
        for f in sorted(comp_path.rglob("*")):
            if f.is_file():
                if include_exts and f.suffix.lower() not in include_exts:
                    continue
                rel = str(f.relative_to(comp_path)).replace("\\", "/")
                if exclude_patterns and _matches_exclude(rel, exclude_patterns):
                    continue
                stat = f.stat()
                parts.append(f"{rel}:{stat.st_mtime:.0f}:{stat.st_size}")
    except (OSError, PermissionError):
        pass
    return hashlib.md5("\n".join(parts).encode()).hexdigest()


def compute_source_content_hash(
    comp_path: Path,
    include_exts: set[str] | None = None,
    exclude_patterns: list[str] | None = None,
) -> str:
    """Compute a stable hash based on file relative-path + file content."""
    from .scanner import _matches_exclude

    digest = hashlib.md5()
    try:
        for f in sorted(comp_path.rglob("*")):
            if f.is_file():
                if include_exts and f.suffix.lower() not in include_exts:
                    continue
                rel = str(f.relative_to(comp_path)).replace("\\", "/")
                if exclude_patterns and _matches_exclude(rel, exclude_patterns):
                    continue
                digest.update(rel.encode("utf-8"))
                digest.update(b"\0")
                with open(f, "rb") as fh:
                    while True:
                        chunk = fh.read(1024 * 1024)
                        if not chunk:
                            break
                        digest.update(chunk)
                digest.update(b"\0")
    except (OSError, PermissionError):
        pass
    return digest.hexdigest()


def build_component_meta(
    cr: ComponentResult,
    include_exts: set[str] | None = None,
    exclude_patterns: list[str] | None = None,
) -> dict[str, Any]:
    """Build the _meta.json entry for a single component."""
    comp = cr.component
    # Detect if any submodule summary is empty — mark as partial so it gets retried
    has_empty = (
        any(not text.strip() for text in cr.submodule_summaries.values())
        if cr.submodule_summaries
        else False
    )
    status = "partial" if has_empty else "done"
    if cr.skipped_reason:
        return {
            "path": str(comp.path),
            "status": "skipped",
            "reason": cr.skipped_reason,
            "file_count": comp.file_count,
            "total_lines": comp.total_lines,
            "batch_count": 0,
            "tokens": {
                "input": cr.total_input_tokens,
                "output": cr.total_output_tokens,
            },
            "last_updated": datetime.now(timezone.utc).isoformat(),
            "source_hash": compute_source_hash(comp.path, include_exts, exclude_patterns),
            "content_hash": compute_source_content_hash(
                comp.path, include_exts, exclude_patterns
            ),
        }
    if has_empty:
        logger.warning("Component %s has empty submodule summaries, marking as partial", comp.key)
    return {
        "path": str(comp.path),
        "status": status,
        "file_count": comp.file_count,
        "total_lines": comp.total_lines,
        "batch_count": cr.batch_count,
        "tokens": {
            "input": cr.total_input_tokens,
            "output": cr.total_output_tokens,
        },
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "source_hash": compute_source_hash(comp.path, include_exts, exclude_patterns),
        "content_hash": compute_source_content_hash(comp.path, include_exts, exclude_patterns),
    }


# Legacy compat — build_meta from full list (still used for final summary)
def build_meta(
    config: Config,
    components: list[ComponentResult],
) -> MetaInfo:
    """Build MetaInfo from generation results."""
    meta = MetaInfo(
        generated_at=datetime.now(timezone.utc).isoformat(),
        generator_version=__version__,
        llm_model=config.llm.model,
        total_components=len(components),
    )

    total_in = 0
    total_out = 0
    total_summaries = 0

    for cr in components:
        comp = cr.component
        meta.components[comp.key] = build_component_meta(cr)
        total_in += cr.total_input_tokens
        total_out += cr.total_output_tokens
        total_summaries += 1 + len(cr.submodule_summaries)

    meta.total_tokens = {"input": total_in, "output": total_out}
    meta.total_summaries = total_summaries

    return meta
