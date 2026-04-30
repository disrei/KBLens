"""Phase 2b: Extract document sections from Markdown files.

Replaces ``ast_extract`` for document sources.  Splits Markdown content by
heading level into sections, each producing an ``ASTEntry`` that the rest of
the pipeline (packer → summarizer → writer) can consume unchanged.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from .doc_convert import UnsupportedFormatError, convert_to_markdown
from .models import (
    ASTEntry,
    Component,
    Config,
    DOCUMENT_EXTENSIONS,
)
from .scanner import _matches_exclude

logger = logging.getLogger("kblens.section_extract")

# Maximum file size to process (same 1 MB limit as ast_extract)
MAX_DOC_FILE_SIZE = 1_048_576

# Regex matching Markdown headings: "# Heading", "## Heading", etc.
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)

# Regex matching Markdown image references: ![alt](path) or ![alt](path "title")
_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class Section:
    """A section of a Markdown document, defined by a heading."""

    heading: str  # the heading text (without '#' prefix)
    level: int  # heading level (1-6)
    content: str  # full content including heading line
    line_start: int  # 1-indexed line number where section starts
    line_end: int  # 1-indexed line number where section ends (inclusive)


# ---------------------------------------------------------------------------
# Section splitting
# ---------------------------------------------------------------------------


def parse_sections(md_text: str, level: int = 2) -> list[Section]:
    """Split Markdown text into sections by heading level.

    Splits on headings at ``level`` or above (i.e. ``level=2`` splits on
    ``#`` and ``##``).  Each section includes its heading and all content
    until the next heading of the same or higher level.

    If the document has no headings at or above ``level``, the entire
    document is returned as a single section.

    A "preamble" section (content before the first matching heading) is
    included if non-empty.
    """
    lines = md_text.split("\n")
    # Find all heading positions and their levels
    headings: list[tuple[int, int, str]] = []  # (line_idx, level, text)
    for i, line in enumerate(lines):
        m = _HEADING_RE.match(line)
        if m:
            h_level = len(m.group(1))
            h_text = m.group(2).strip()
            if h_level <= level:
                headings.append((i, h_level, h_text))

    if not headings:
        # No matching headings — return entire document as one section
        content = md_text.strip()
        if not content:
            return []
        return [
            Section(
                heading="(document)",
                level=0,
                content=content,
                line_start=1,
                line_end=len(lines),
            )
        ]

    sections: list[Section] = []

    # Preamble: content before the first heading
    first_heading_line = headings[0][0]
    if first_heading_line > 0:
        preamble = "\n".join(lines[:first_heading_line]).strip()
        if preamble:
            sections.append(
                Section(
                    heading="(preamble)",
                    level=0,
                    content=preamble,
                    line_start=1,
                    line_end=first_heading_line,
                )
            )

    # Build sections from headings
    for idx, (line_idx, h_level, h_text) in enumerate(headings):
        # Section content runs from this heading to the next heading (exclusive)
        if idx + 1 < len(headings):
            end_line = headings[idx + 1][0]
        else:
            end_line = len(lines)

        content = "\n".join(lines[line_idx:end_line]).strip()
        if content:
            sections.append(
                Section(
                    heading=h_text,
                    level=h_level,
                    content=content,
                    line_start=line_idx + 1,
                    line_end=end_line,
                )
            )

    return sections


# ---------------------------------------------------------------------------
# Image handling
# ---------------------------------------------------------------------------


def process_images(content: str, handling: str = "reference") -> str:
    """Process image references in Markdown content.

    Args:
        content: Markdown text.
        handling: ``"reference"`` keeps images as ``[Image: alt](path)``;
                  ``"ignore"`` removes image references entirely.

    Returns:
        Processed Markdown text.
    """
    if handling == "ignore":
        return _IMAGE_RE.sub("", content)

    # handling == "reference": convert ![alt](path) → [Image: alt](path)
    def _replace_image(m: re.Match) -> str:
        alt = m.group(1).strip() or Path(m.group(2)).stem
        path = m.group(2)
        return f"[Image: {alt}]({path})"

    return _IMAGE_RE.sub(_replace_image, content)


# ---------------------------------------------------------------------------
# Token estimation (same logic as ast_extract)
# ---------------------------------------------------------------------------


def estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 chars per token."""
    return max(1, len(text) // 4)


# ---------------------------------------------------------------------------
# Anchor generation
# ---------------------------------------------------------------------------


def _make_anchor(heading: str) -> str:
    """Generate a URL-friendly anchor from a heading string."""
    # Lowercase, replace spaces and special chars with hyphens
    anchor = re.sub(r"[^\w\s-]", "", heading.lower())
    anchor = re.sub(r"[\s_]+", "-", anchor.strip())
    return anchor


# ---------------------------------------------------------------------------
# Phase 2b entry point
# ---------------------------------------------------------------------------


def phase2_extract_docs(
    component: Component,
    config: Config,
    include_exts: set[str],
    section_level: int = 2,
    image_handling: str = "reference",
) -> dict[str, ASTEntry]:
    """Extract document sections from all document files in a component.

    This is the document equivalent of ``phase2_extract_ast``.  Each section
    of each document file becomes an ``ASTEntry`` with:

    - ``rel_path``: ``"filename.md#section-anchor"``
    - ``dir``: ``"filename.md"`` (groups sections by file for the packer)
    - ``content``: full section Markdown text (with images processed)
    - ``tokens``: estimated token count
    - ``language``: ``"markdown"``

    Returns:
        dict mapping ``rel_path`` to ``ASTEntry``, compatible with Phase 3.
    """
    ast_map: dict[str, ASTEntry] = {}
    comp_path = component.path

    # Collect all document files
    doc_files: list[Path] = []
    for f in sorted(comp_path.rglob("*")):
        if not f.is_file():
            continue
        if f.stat().st_size > MAX_DOC_FILE_SIZE:
            logger.warning("Skipping oversized document (>1MB): %s", f)
            continue
        ext = f.suffix.lower()
        if ext not in include_exts:
            continue
        rel = str(f.relative_to(comp_path)).replace("\\", "/")
        if _matches_exclude(rel, config.exclude_patterns):
            continue
        doc_files.append(f)

    for f in doc_files:
        rel_file = str(f.relative_to(comp_path)).replace("\\", "/")
        dir_key = rel_file  # group by file for packer

        # Convert to Markdown
        try:
            md_text = convert_to_markdown(f)
        except UnsupportedFormatError as e:
            logger.warning("Skipping %s: %s", f, e)
            continue
        except OSError as e:
            logger.warning("Cannot read %s: %s", f, e)
            continue

        if not md_text.strip():
            continue

        # Process images
        md_text = process_images(md_text, handling=image_handling)

        # Split into sections
        sections = parse_sections(md_text, level=section_level)
        if not sections:
            continue

        if len(sections) == 1:
            # Single section — use file path directly (no anchor)
            sec = sections[0]
            content = sec.content
            tokens = estimate_tokens(content)
            if tokens > 0:
                ast_map[rel_file] = ASTEntry(
                    rel_path=rel_file,
                    dir=dir_key,
                    content=content,
                    tokens=tokens,
                    language="markdown",
                )
        else:
            # Multiple sections — each gets its own entry with anchor
            for sec in sections:
                anchor = _make_anchor(sec.heading)
                entry_key = f"{rel_file}#{anchor}"

                # Deduplicate: if anchor collision, append line number
                if entry_key in ast_map:
                    entry_key = f"{rel_file}#{anchor}-L{sec.line_start}"

                content = sec.content
                tokens = estimate_tokens(content)
                if tokens > 0:
                    ast_map[entry_key] = ASTEntry(
                        rel_path=entry_key,
                        dir=dir_key,
                        content=content,
                        tokens=tokens,
                        language="markdown",
                    )

    return ast_map
