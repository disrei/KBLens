"""Document format conversion: various formats → Markdown text.

Uses two backends:

1. **Built-in** — ``.md`` and ``.txt`` files are read directly (fast path).
2. **markitdown** — PDF, DOCX, PPTX, XLSX, XLS, HTML, CSV, EPUB, etc.
   Install with: ``pip install markitdown``
   For full format support: ``pip install 'markitdown[all]'``

markitdown is lazily imported so that kblens can still function for
code-only workflows even when markitdown is not installed.  When a
document source encounters a non-.md/.txt file without markitdown,
a clear error with install instructions is shown.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger("kblens.doc_convert")

# Singleton MarkItDown instance (created on first use)
_markitdown_instance = None
_markitdown_available: bool | None = None


class UnsupportedFormatError(Exception):
    """Raised when a document format conversion fails."""


def _get_markitdown():
    """Lazily import and instantiate MarkItDown.

    Returns the singleton instance, or None if markitdown is not installed.
    """
    global _markitdown_instance, _markitdown_available
    if _markitdown_available is not None:
        return _markitdown_instance

    try:
        from markitdown import MarkItDown

        _markitdown_instance = MarkItDown()
        _markitdown_available = True
        return _markitdown_instance
    except ImportError:
        _markitdown_available = False
        logger.debug("markitdown not installed — only .md/.txt supported natively")
        return None


def convert_to_markdown(file_path: Path) -> str:
    """Convert a document file to Markdown text.

    For ``.md`` and ``.txt`` files, reads directly (fast path).
    For all other formats, delegates to ``markitdown``.

    Args:
        file_path: Path to the document file.

    Returns:
        Markdown text content.

    Raises:
        UnsupportedFormatError: If the format conversion fails or
            markitdown is not installed.
        OSError: If the file cannot be read.
    """
    ext = file_path.suffix.lower()

    # Fast path: read text files directly (avoid markitdown overhead)
    if ext in _DIRECT_READ_EXTS:
        return file_path.read_text(encoding="utf-8", errors="replace")

    # All other formats: markitdown
    return _convert_via_markitdown(file_path)


# Extensions that are read directly (no conversion needed)
_DIRECT_READ_EXTS: set[str] = {".md", ".txt"}


def _convert_via_markitdown(file_path: Path) -> str:
    """Convert a file using the markitdown library."""
    mid = _get_markitdown()

    if mid is None:
        raise UnsupportedFormatError(
            f"Cannot convert '{file_path.suffix}' files — markitdown is not installed.\n"
            f"Install with:  pip install markitdown\n"
            f"Full support:  pip install 'markitdown[all]'"
        )

    try:
        result = mid.convert(str(file_path))
        text = result.text_content
        if not text or not text.strip():
            logger.warning("markitdown returned empty content for %s", file_path)
            return ""
        return text
    except Exception as e:
        logger.warning("markitdown failed on %s: %s", file_path, e)
        raise UnsupportedFormatError(
            f"Failed to convert {file_path.name}: {e}"
        ) from e
