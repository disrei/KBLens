"""Confluence page crawler: recursively fetch pages and save as Markdown.

Usage:
    python confluence_crawler.py "https://confluence.ubisoft.com/display/ovr/HOW+TO+SPAWN+NPCS"
    python confluence_crawler.py "https://confluence.ubisoft.com/display/ovr/HOW+TO+SPAWN+NPCS" --depth 3
    python confluence_crawler.py "https://confluence.ubisoft.com/display/ovr/HOW+TO+SPAWN+NPCS" -o ./my_docs

Requires: pip install requests markitdown
"""

from __future__ import annotations

import argparse
import concurrent.futures
import getpass
import html as html_module
import json
import re
import shutil
import sys
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

import requests

# ---------------------------------------------------------------------------
# Confluence API helpers
# ---------------------------------------------------------------------------


def parse_confluence_url(url: str) -> tuple[str, str, str]:
    """Parse a Confluence page URL into (base_url, space_key, title).

    Supports:
      - /display/SPACE/Page+Title
      - /pages/viewpage.action?pageId=12345  (returns pageId as title)
    """
    parsed = urlparse(url)
    base_url = f"{parsed.scheme}://{parsed.hostname}"
    if parsed.port and parsed.port not in (80, 443):
        base_url += f":{parsed.port}"

    path = parsed.path

    # /display/SPACE/Title+Here
    m = re.match(r"/display/([^/]+)/(.+)", path)
    if m:
        space_key = m.group(1)
        title = unquote(m.group(2).replace("+", " "))
        return base_url, space_key, title

    # /pages/viewpage.action?pageId=XXXXX
    if "pageId" in (parsed.query or ""):
        m2 = re.search(r"pageId=(\d+)", parsed.query)
        if m2:
            return base_url, "", m2.group(1)

    print(f"[ERROR] Cannot parse Confluence URL: {url}")
    sys.exit(1)


class ConfluenceClient:
    """Simple Confluence REST API client."""

    def __init__(self, base_url: str, token: str | None = None,
                 username: str | None = None, password: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers["Accept"] = "application/json"
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"
        elif username and password:
            self.session.auth = (username, password)

    def _get(self, endpoint: str, params: dict | None = None) -> dict | None:
        url = f"{self.base_url}/rest/api{endpoint}"
        try:
            resp = self.session.get(url, params=params, timeout=(10, 25))
            if resp.status_code == 401:
                print("[ERROR] Authentication failed. Check token/credentials.")
                sys.exit(1)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            print(f"  [WARN] API request failed: {endpoint}: {e}", file=sys.stderr)
            return None

    def get_page_by_title(self, space_key: str, title: str) -> dict | None:
        """Find a page by space key and title."""
        data = self._get("/content", params={
            "spaceKey": space_key,
            "title": title,
            "expand": "body.storage,children.page,version",
            "limit": 1,
        })
        results = data.get("results", [])
        return results[0] if results else None

    def get_page_by_id(self, page_id: str) -> dict | None:
        """Get a page by its ID."""
        return self._get(f"/content/{page_id}", params={
            "expand": "body.storage,children.page,version,ancestors",
        })

    def get_child_pages(self, page_id: str) -> list[dict]:
        """Get all direct child pages of a page."""
        children = []
        start = 0
        limit = 50
        while True:
            data = self._get(f"/content/{page_id}/child/page", params={
                "start": start,
                "limit": limit,
                "expand": "version",
            })
            if data is None:
                break  # API error, return what we have
            results = data.get("results", [])
            children.extend(results)
            if len(results) < limit:
                break
            start += limit
        return children

    def get_attachments(self, page_id: str) -> list[dict]:
        """Get all attachments for a page."""
        attachments = []
        start = 0
        limit = 50
        while True:
            data = self._get(f"/content/{page_id}/child/attachment", params={
                "start": start,
                "limit": limit,
            })
            if data is None:
                break  # API error, return what we have
            results = data.get("results", [])
            attachments.extend(results)
            if len(results) < limit:
                break
            start += limit
        return attachments

    def download_attachment(self, url: str) -> bytes | None:
        """Download an attachment by URL. Returns bytes or None."""
        try:
            resp = self.session.get(url, timeout=20)
            resp.raise_for_status()
            return resp.content
        except Exception:
            # Silent skip — too many 500s from Confluence for thumbnails
            return None


# ---------------------------------------------------------------------------
# HTML → Markdown conversion
# ---------------------------------------------------------------------------

_markitdown_instance = None

_NOISE_LINE_PATTERNS = [
    re.compile(r"^index\\?_cards", re.IGNORECASE),
    re.compile(r"^plugin\\?_pagetree", re.IGNORECASE),
    re.compile(r"^gifview\b", re.IGNORECASE),
    re.compile(r"^center\d*barre$", re.IGNORECASE),
    re.compile(r"^you don't have anything to do with the div below", re.IGNORECASE),
    re.compile(r"^[A-Za-z0-9_]+INLINE.*$"),
    re.compile(r"^[A-Za-z0-9_]+usefultips.*$", re.IGNORECASE),
    re.compile(r"^\d+%$"),
    re.compile(r"^color:\s*#?[0-9a-f]{3,8};?$", re.IGNORECASE),
    re.compile(r"^\]\]>$"),
    re.compile(r"^plugin_pagetree_expandcollapse.*$", re.IGNORECASE),
    re.compile(r"^display:\s*none;?$", re.IGNORECASE),
    re.compile(r"^#title-text.*$", re.IGNORECASE),
    re.compile(r"^#main-header.*$", re.IGNORECASE),
    re.compile(r"^\.page-metadata.*$", re.IGNORECASE),
    re.compile(r"^a,\s*$", re.IGNORECASE),
    re.compile(r"^a:visited.*$", re.IGNORECASE),
    re.compile(r"^a:focus.*$", re.IGNORECASE),
    re.compile(r"^a:hover.*$", re.IGNORECASE),
    re.compile(r"^a:active.*$", re.IGNORECASE),
    re.compile(r"^a\.blogHeading.*$", re.IGNORECASE),
    re.compile(r"^font-size:\s*.*$", re.IGNORECASE),
    re.compile(r"^font-family:\s*.*$", re.IGNORECASE),
    re.compile(r"^letter-spacing:\s*.*$", re.IGNORECASE),
]


def _convert_ac_images(html_content: str, page_id: str = "", base_url: str = "") -> str:
    """Convert Confluence <ac:image> tags to standard <img> tags.

    Confluence stores images in two forms:
      1. Attachment: <ac:image><ri:attachment ri:filename="x.png"/></ac:image>
      2. External URL: <ac:image><ri:url ri:value="https://..."/></ac:image>

    This converts them to <img src="..." alt="..."> so markitdown can handle them.
    """
    def _replace_image(m: re.Match) -> str:
        inner = m.group(1)

        # Try attachment: ri:filename="..."
        fname_m = re.search(r'ri:filename="([^"]+)"', inner)
        if fname_m:
            filename = html_module.unescape(fname_m.group(1))
            # Build download URL: /download/attachments/{pageId}/{filename}
            if page_id:
                src = f"{base_url}/download/attachments/{page_id}/{quote(filename)}"
            else:
                src = filename
            alt = Path(filename).stem
            return f'<img src="{src}" alt="{alt}" />'

        # Try external URL: ri:value="..."
        url_m = re.search(r'ri:value="([^"]+)"', inner)
        if url_m:
            src = html_module.unescape(url_m.group(1))
            alt = Path(urlparse(src).path).stem or "image"
            return f'<img src="{src}" alt="{alt}" />'

        return ""  # unknown image format — drop

    # Match <ac:image ...> ... </ac:image> (may have attributes on ac:image itself)
    html_content = re.sub(
        r"<ac:image\b[^>]*>(.*?)</ac:image>",
        _replace_image,
        html_content,
        flags=re.IGNORECASE | re.DOTALL,
    )
    # Also handle self-closing: <ac:image ... />
    html_content = re.sub(
        r"<ac:image\b[^/]*/\s*>",
        "",
        html_content,
        flags=re.IGNORECASE,
    )
    return html_content


def _clean_confluence_storage_html(
    html_content: str, page_id: str = "", base_url: str = "",
) -> str:
    """Remove Confluence-specific storage noise before HTML->Markdown conversion."""
    # Convert ac:image to standard <img> BEFORE stripping ri:attachment nodes.
    cleaned = _convert_ac_images(html_content, page_id, base_url)

    # Strip CDATA blocks (CSS, inline scripts) — entire block is Confluence noise.
    cleaned = re.sub(r"<!\[CDATA\[.*?\]\]>", "", cleaned, flags=re.DOTALL)

    # Remove style/script blocks entirely.
    cleaned = re.sub(r"<style\b[^>]*>.*?</style>", "", cleaned, flags=re.IGNORECASE | re.DOTALL)
    cleaned = re.sub(r"<script\b[^>]*>.*?</script>", "", cleaned, flags=re.IGNORECASE | re.DOTALL)

    # Iterative unwrap: each pass strips one layer of macros, handles nesting.
    _NOISE_MACRO_NAMES = (
        r"pagetree|children|excerpt-include|contentbylabel|details|toc|widget|html|css|style"
        r"|multiexcerpt-include|html-bobswift"
    )
    _LAYOUT_MACRO_NAMES = r"section|column|bgcolor|align|banner"

    prev = None
    while prev != cleaned:
        prev = cleaned

        # Delete noise macros (outermost layer each pass).
        cleaned = re.sub(
            r"<ac:structured-macro\b[^>]*ac:name=\"(?:" + _NOISE_MACRO_NAMES
            + r")\"[^>]*>.*?</ac:structured-macro>",
            "",
            cleaned,
            flags=re.IGNORECASE | re.DOTALL,
        )

        # Unwrap layout macros (keep inner content).
        cleaned = re.sub(
            r"<ac:structured-macro\b[^>]*ac:name=\"(?:" + _LAYOUT_MACRO_NAMES
            + r")\"[^>]*>(.*?)</ac:structured-macro>",
            r"\1",
            cleaned,
            flags=re.IGNORECASE | re.DOTALL,
        )

    # Remove macro body wrappers (keep inner content).
    cleaned = re.sub(r"</?ac:rich-text-body[^>]*>", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"</?ac:plain-text-body[^>]*>", "", cleaned, flags=re.IGNORECASE)

    # Unwrap layout container tags (keep inner content).
    cleaned = re.sub(r"</?ac:layout(?:-section|-cell)?\b[^>]*>", "", cleaned, flags=re.IGNORECASE)

    # Unwrap any remaining ac:structured-macro tags (keep inner content).
    cleaned = re.sub(r"</?ac:structured-macro\b[^>]*>", "", cleaned, flags=re.IGNORECASE)

    # Strip ac:parameter tags (always metadata, not content).
    cleaned = re.sub(
        r"<ac:parameter\b[^>]*>.*?</ac:parameter>", "", cleaned,
        flags=re.IGNORECASE | re.DOTALL,
    )

    # Remove placeholder / attachment nodes (these produce no meaningful text).
    cleaned = re.sub(
        r"<(?:ac:placeholder|ri:attachment|ri:url)\b[^>]*>.*?</(?:ac:placeholder|ri:attachment|ri:url)>",
        "",
        cleaned,
        flags=re.IGNORECASE | re.DOTALL,
    )

    # Remove empty paragraphs / cursor targets.
    cleaned = re.sub(r"<p\b[^>]*>\s*(?:<br\s*/?>\s*)*</p>", "", cleaned, flags=re.IGNORECASE)

    return cleaned


def _extract_navigation_fallback(html_content: str, title: str = "") -> str:
    """Extract a navigation-style Markdown page from noisy storage HTML."""
    # Extract links.
    links = re.findall(
        r'<a\b[^>]*href="([^"]+)"[^>]*>(.*?)</a>', html_content,
        flags=re.DOTALL | re.IGNORECASE,
    )
    bullets: list[str] = []
    seen: set[tuple[str, str]] = set()
    for href, label_html in links:
        label = re.sub(r"<[^>]+>", "", label_html)
        label = html_module.unescape(label).strip()
        href = html_module.unescape(href).strip()
        if not label or len(label) > 200:
            continue
        if label.lower() == title.lower():
            continue
        key = (label, href)
        if key in seen:
            continue
        seen.add(key)
        bullets.append(f"- [{label}]({href})")
        if len(bullets) >= 60:
            break

    # Extract headings from HTML.
    headings: list[tuple[int, str]] = []
    for level in range(1, 7):
        for m in re.finditer(
            rf"<(?:h{level}|H{level})\b[^>]*>(.*?)</(?:h{level}|H{level})>",
            html_content, re.DOTALL,
        ):
            h_text = re.sub(r"<[^>]+>", "", m.group(1))
            h_text = html_module.unescape(h_text).strip()
            if h_text and len(h_text) > 2 and len(h_text) < 200 and h_text.lower() != title.lower():
                headings.append((level, h_text))

    lines = [f"# {title}"] if title else []
    lines.append("")
    lines.append("*(Navigation-style page with limited standalone text content.)*")

    if headings:
        lines.append("")
        lines.append("## Sections")
        for level, h_text in headings:
            lines.append(f"{'  ' * (level - 1)}- {h_text}")

    if bullets:
        lines.append("")
        lines.append("## Links")
        lines.extend(bullets)
    elif not headings:
        lines.append("")
        lines.append("*(No structured content could be extracted.)*")

    return "\n".join(lines).strip()


def _looks_like_noise_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    if any(pat.match(stripped) for pat in _NOISE_LINE_PATTERNS):
        return True
    # CSS-like blocks
    if stripped.startswith((".", "#")) and "{" in stripped:
        return True
    if stripped in {"}", "{", "center", "count"}:
        return True
    if stripped.endswith("{") or stripped.endswith("}"):
        return True
    # Confluence noise tokens
    if any(token in stripped for token in (
        "display:none", "page-metadata", "likes-and-labels-container",
        "#footer", "#comments-section", "auto-cursor-target",
        "plugin_pagetree_expandcollapse", "data-darkreader-inline-color",
    )):
        return True
    # Long attachment URLs
    if stripped.startswith("https://confluence.ubisoft.com/download/attachments/") and len(stripped) > 120:
        return True
    # Pure CSS property lines
    if re.match(r"^[a-z-]+\s*:\s*[^;]*;?\s*$", stripped, re.IGNORECASE) and not re.search(r"\b[a-zA-Z]{3,}\b", stripped):
        return True
    # Font/style declarations
    if stripped.startswith("font-") or stripped.startswith("color:") or stripped.startswith("span"):
        return True
    return False


def _has_substantive_content(text: str, title: str = "") -> bool:
    """Check if page text has meaningful content beyond title and metadata annotations."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    title_line = f"# {title}" if title else None
    substantive = [
        l for l in lines
        if l != title_line
        and not l.startswith("*(")
        and not l == "---"
    ]
    total_chars = sum(len(l) for l in substantive)
    return len(substantive) >= 3 and total_chars >= 40


def _postprocess_markdown(text: str, title: str = "") -> str:
    """Clean common Confluence macro/CSS residue after Markdown conversion."""
    # Strip CSS comments early.
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)

    lines = text.splitlines()
    cleaned_lines: list[str] = []
    skipping_css = False

    for raw_line in lines:
        line = raw_line.rstrip()
        stripped = line.strip()

        # Detect CSS block open: `.foo {` or `#bar {`
        if stripped.startswith((".", "#")) and stripped.endswith("{"):
            skipping_css = True
            continue
        if skipping_css:
            if stripped == "}":
                skipping_css = False
            continue

        if _looks_like_noise_line(line):
            continue

        cleaned_lines.append(line)

    cleaned = "\n".join(cleaned_lines)
    # Collapse empty lines
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()

    # Keep a single top-level title if present.
    if title:
        title_heading = f"# {title}"
        cleaned = re.sub(rf"^(?:#\s+{re.escape(title)}\s*)+", title_heading + "\n\n", cleaned)

    if cleaned and title and cleaned == title_heading:
        return cleaned

    return cleaned.strip()


def html_to_markdown(
    html_content: str, title: str = "",
    page_id: str = "", base_url: str = "",
) -> str:
    """Convert Confluence HTML storage format to Markdown."""
    global _markitdown_instance
    html_content = _clean_confluence_storage_html(html_content, page_id, base_url)

    # Wrap in a basic HTML document for markitdown
    full_html = f"<html><head><title>{title}</title></head><body>{html_content}</body></html>"

    # Try markitdown first
    try:
        if _markitdown_instance is None:
            from markitdown import MarkItDown
            _markitdown_instance = MarkItDown()

        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".html", mode="w", encoding="utf-8",
                                         delete=False) as f:
            f.write(full_html)
            tmp_path = f.name

        result = _markitdown_instance.convert(tmp_path)
        Path(tmp_path).unlink(missing_ok=True)

        text = result.text_content or ""
        if text.strip():
            cleaned = _postprocess_markdown(text, title)
            if _has_substantive_content(cleaned, title):
                return cleaned
    except ImportError:
        pass
    except Exception as e:
        import sys as _sys
        print(f"  [WARN] markitdown failed: {e}", file=_sys.stderr)

    # Fallback: basic HTML tag stripping
    cleaned = _postprocess_markdown(_basic_html_to_md(html_content, title), title)
    if _has_substantive_content(cleaned, title):
        return cleaned
    return _extract_navigation_fallback(html_content, title)


def _basic_html_to_md(html: str, title: str = "") -> str:
    """Very basic HTML → Markdown fallback (no external deps)."""
    text = html
    # Headers
    for i in range(1, 7):
        text = re.sub(rf"<h{i}[^>]*>(.*?)</h{i}>", rf"{'#' * i} \1\n", text, flags=re.DOTALL)
    # Bold / italic
    text = re.sub(r"<(?:strong|b)>(.*?)</(?:strong|b)>", r"**\1**", text, flags=re.DOTALL)
    text = re.sub(r"<(?:em|i)>(.*?)</(?:em|i)>", r"*\1*", text, flags=re.DOTALL)
    # Code
    text = re.sub(r"<code>(.*?)</code>", r"`\1`", text, flags=re.DOTALL)
    # Links
    text = re.sub(r'<a[^>]+href="([^"]*)"[^>]*>(.*?)</a>', r"[\2](\1)", text, flags=re.DOTALL)
    # Lists
    text = re.sub(r"<li[^>]*>(.*?)</li>", r"- \1\n", text, flags=re.DOTALL)
    # Line breaks and paragraphs
    text = re.sub(r"<br\s*/?>", "\n", text)
    text = re.sub(r"<p[^>]*>(.*?)</p>", r"\1\n\n", text, flags=re.DOTALL)
    # Strip remaining tags
    text = re.sub(r"<[^>]+>", "", text)
    # Unescape HTML entities
    text = html_module.unescape(text)
    # Clean up whitespace
    text = re.sub(r"\n{3,}", "\n\n", text)

    if title:
        text = f"# {title}\n\n{text}"
    return text.strip()


# ---------------------------------------------------------------------------
# Recursive crawler
# ---------------------------------------------------------------------------


_IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".bmp", ".ico"})


def _download_single_image(args: tuple) -> tuple[str, str] | None:
    """Download one image URL.  Returns (original_url, local_rel_path) or None."""
    client, full_url, page_id, filename_encoded, images_dir = args
    filename = unquote(filename_encoded)
    ext = Path(filename).suffix.lower()
    if ext not in _IMAGE_EXTS:
        return None

    safe_fname = sanitize_filename(Path(filename).stem) + ext
    local_path = images_dir / safe_fname
    if local_path.exists():
        return (full_url, f"_images/{safe_fname}")

    data = client.download_attachment(full_url)
    if data is None:
        return None

    local_path.write_bytes(data)
    return (full_url, f"_images/{safe_fname}")


def _download_page_images(
    client: ConfluenceClient,
    page_id: str,
    md_content: str,
    output_dir: Path,
) -> str:
    """Download images referenced in md_content concurrently.

    Attachment images are downloaded to ``output_dir/_images/``.
    Returns the updated Markdown content with local image paths.
    """
    att_pattern = re.compile(
        r"!\[([^\]]*)\]\(([^)]+/download/attachments/" + re.escape(page_id) + r"/([^)]+))\)"
    )
    matches = att_pattern.findall(md_content)
    if not matches:
        return md_content

    images_dir = output_dir / "_images"
    images_dir.mkdir(parents=True, exist_ok=True)

    # Deduplicate by URL before launching concurrent downloads.
    seen_urls: set[str] = set()
    work = []
    for alt, full_url, filename_encoded in matches:
        if full_url not in seen_urls:
            seen_urls.add(full_url)
            work.append((client, full_url, page_id, filename_encoded, images_dir))

    downloaded: dict[str, str] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = pool.map(_download_single_image, work)

    for r in results:
        if r is not None:
            orig_url, local_rel = r
            downloaded[orig_url] = local_rel

    # Rewrite URLs in Markdown.
    for orig_url, local_rel in downloaded.items():
        md_content = md_content.replace(orig_url, local_rel)

    if downloaded:
        print(f"    [images] {len(downloaded)}/{len(matches)} saved")

    return md_content


# ---------------------------------------------------------------------------
# Incremental crawl support
# ---------------------------------------------------------------------------


def _load_crawl_meta(output_dir: Path) -> dict[str, dict]:
    """Load ``_crawl_meta.json`` from output directory."""
    meta_path = output_dir / "_crawl_meta.json"
    if not meta_path.is_file():
        return {}
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_crawl_meta(output_dir: Path, meta: dict[str, dict]) -> None:
    """Write ``_crawl_meta.json`` to output directory."""
    output_dir.mkdir(parents=True, exist_ok=True)
    meta_path = output_dir / "_crawl_meta.json"
    tmp = meta_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(meta_path)


def _clean_stale_pages(output_dir: Path, old_meta: dict[str, dict],
                       new_ids: set[str]) -> int:
    """Remove .md files and _images dirs for pages no longer in the tree."""
    removed = 0
    for page_id, info in old_meta.items():
        if page_id in new_ids:
            continue
        path_str = info.get("path", "")
        if not path_str:
            continue
        md_path = output_dir / path_str
        if md_path.is_file():
            md_path.unlink()
            removed += 1
            print(f"  [stale] removed {path_str}")
        # Also remove sibling _images dir
        imgs = md_path.with_name("_images")
        if imgs.is_dir():
            shutil.rmtree(imgs)
    return removed


def sanitize_filename(name: str) -> str:
    """Make a string safe for use as a filename."""
    name = re.sub(r'[<>:"/\\|?*]', "_", name)
    name = re.sub(r"\s+", "_", name)
    return name[:200]  # limit length


def crawl_page(
    client: ConfluenceClient,
    page_id: str,
    output_dir: Path,
    depth: int = 0,
    max_depth: int = 10,
    visited: set | None = None,
    download_images: bool = True,
    crawl_meta: dict[str, dict] | None = None,
) -> int:
    """Recursively crawl a page and its children, saving as Markdown.

    When ``crawl_meta`` is provided (incremental mode), pages whose
    version has not changed are skipped — conversion and image download
    are bypassed — but children are still recursed.

    Returns the number of pages newly written (not skipped).
    """
    if visited is None:
        visited = set()

    if page_id in visited:
        return 0
    visited.add(page_id)

    if depth > max_depth:
        return 0

    # Fetch page content
    try:
        page = client.get_page_by_id(page_id)
    except Exception as e:
        print(f"  [ERROR] Failed to fetch page {page_id}: {e}")
        return 0

    if page is None:
        print(f"  [WARN] Page {page_id} returned None from API")
        return 0

    title = page.get("title", f"page_{page_id}")
    safe_title = title.encode("cp1252", errors="replace").decode("cp1252")
    indent = "  " * depth
    safe_name = sanitize_filename(title)
    md_path = output_dir / f"{safe_name}.md"

    # --- incremental skip logic ---
    version = page.get("version", {}).get("number", 0)
    if crawl_meta is not None:
        prev = crawl_meta.get(page_id)
        if prev and prev.get("version") == version and md_path.is_file():
            print(f"{indent}[{depth}] {safe_title} (unchanged)")
            # Still record for stale check.
            crawl_meta[page_id] = {"title": title, "version": version,
                                   "path": str(md_path.relative_to(output_dir))}
            count = 0
            # Recurse children even though this page didn't change.
            children = client.get_child_pages(page_id)
            if children:
                child_dir = output_dir / safe_name
                for child in children:
                    child_id = child["id"]
                    count += crawl_page(
                        client, child_id, child_dir,
                        depth=depth + 1, max_depth=max_depth, visited=visited,
                        download_images=download_images, crawl_meta=crawl_meta,
                    )
            return count

    print(f"{indent}[{depth}] {safe_title}")

    # --- full processing ---
    html_body = page.get("body", {}).get("storage", {}).get("value", "")
    if html_body:
        md_content = html_to_markdown(
            html_body, title, page_id=page_id, base_url=client.base_url,
        )
    else:
        md_content = f"# {title}\n\n*(empty page)*"

    output_dir.mkdir(parents=True, exist_ok=True)
    if download_images and html_body:
        md_content = _download_page_images(client, page_id, md_content, output_dir)

    md_path.write_text(md_content, encoding="utf-8")
    count = 1

    # Record in crawl meta.
    if crawl_meta is not None:
        crawl_meta[page_id] = {"title": title, "version": version,
                               "path": str(md_path.relative_to(output_dir))}

    # Crawl children
    children = client.get_child_pages(page_id)
    if children:
        child_dir = output_dir / safe_name
        for child in children:
            child_id = child["id"]
            count += crawl_page(
                client, child_id, child_dir,
                depth=depth + 1, max_depth=max_depth, visited=visited,
                download_images=download_images,
                crawl_meta=crawl_meta,
            )

    return count


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="Crawl Confluence pages recursively and save as Markdown."
    )
    parser.add_argument("url", help="Confluence page URL to start crawling from")
    parser.add_argument("-o", "--output", default="./confluence_docs",
                        help="Output directory (default: ./confluence_docs)")
    parser.add_argument("-d", "--depth", type=int, default=10,
                        help="Maximum recursion depth (default: 10)")
    parser.add_argument("--token", default=None,
                        help="Confluence Personal Access Token (recommended)")
    parser.add_argument("-u", "--username", default=None,
                        help="Confluence username (prompts if not given)")
    parser.add_argument("-p", "--password", default=None,
                        help="Confluence password (prompts if not given)")
    parser.add_argument("--no-images", action="store_true",
                        help="Skip downloading attachment images")
    parser.add_argument("--incremental", action="store_true",
                        help="Incremental: skip pages whose version hasn't changed")
    args = parser.parse_args()

    # Parse URL
    base_url, space_key, title_or_id = parse_confluence_url(args.url)
    print(f"Confluence: {base_url}")
    print(f"Space: {space_key or '(by page ID)'}")
    print(f"Page: {title_or_id}")
    print()

    # Get credentials
    token = args.token
    if not token:
        username = args.username or input("Username: ")
        password = args.password or getpass.getpass("Password: ")
    else:
        username = password = None
    print()

    # Connect
    client = ConfluenceClient(base_url, token=token, username=username, password=password)

    # Find the starting page
    if space_key:
        print(f"Looking up page '{title_or_id}' in space '{space_key}'...")
        page = client.get_page_by_title(space_key, title_or_id)
        if not page:
            print(f"[ERROR] Page not found: '{title_or_id}' in space '{space_key}'")
            sys.exit(1)
        page_id = page["id"]
    else:
        page_id = title_or_id

    print(f"Found page ID: {page_id}")
    print(f"Output: {args.output}")
    print(f"Max depth: {args.depth}")
    if args.incremental:
        print("Mode: incremental (skipping unchanged pages)")
    print()

    # Crawl
    output_dir = Path(args.output)
    crawl_meta: dict[str, dict] | None = None

    if args.incremental:
        old_meta = _load_crawl_meta(output_dir)
        crawl_meta = {}  # will be populated during crawl

    count = crawl_page(
        client, page_id, output_dir, max_depth=args.depth,
        download_images=not args.no_images,
        crawl_meta=crawl_meta,
    )

    # Post-crawl: save meta and clean stale pages
    if crawl_meta is not None and count >= 0:
        stale = _clean_stale_pages(output_dir, old_meta, set(crawl_meta.keys()))
        if stale:
            print(f"  Cleaned {stale} stale page(s)")
        _save_crawl_meta(output_dir, crawl_meta)

    print()
    print(f"Done! Saved {count} page(s) to {output_dir}")


if __name__ == "__main__":
    main()
