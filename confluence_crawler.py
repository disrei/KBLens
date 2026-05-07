"""Confluence page crawler: recursively fetch pages and save as Markdown.

Usage:
    python confluence_crawler.py "https://confluence.ubisoft.com/display/ovr/HOW+TO+SPAWN+NPCS"
    python confluence_crawler.py "https://confluence.ubisoft.com/display/ovr/HOW+TO+SPAWN+NPCS" --depth 3
    python confluence_crawler.py "https://confluence.ubisoft.com/display/ovr/HOW+TO+SPAWN+NPCS" -o ./my_docs

Requires: pip install requests markitdown
"""

from __future__ import annotations

import argparse
import getpass
import html as html_module
import re
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

    def _get(self, endpoint: str, params: dict | None = None) -> dict:
        url = f"{self.base_url}/rest/api{endpoint}"
        resp = self.session.get(url, params=params, timeout=30)
        if resp.status_code == 401:
            print("[ERROR] Authentication failed. Check token/credentials.")
            sys.exit(1)
        resp.raise_for_status()
        return resp.json()

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

    def get_page_by_id(self, page_id: str) -> dict:
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
            results = data.get("results", [])
            children.extend(results)
            if len(results) < limit:
                break
            start += limit
        return children


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


def _clean_confluence_storage_html(html_content: str) -> str:
    """Remove Confluence-specific storage noise before HTML->Markdown conversion."""
    cleaned = html_content

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


def html_to_markdown(html_content: str, title: str = "") -> str:
    """Convert Confluence HTML storage format to Markdown."""
    global _markitdown_instance
    html_content = _clean_confluence_storage_html(html_content)

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
) -> int:
    """Recursively crawl a page and its children, saving as Markdown.

    Returns the number of pages saved.
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

    title = page.get("title", f"page_{page_id}")
    # Sanitize for Windows console (cp1252 can't handle zero-width spaces, emoji, etc.)
    safe_title = title.encode("cp1252", errors="replace").decode("cp1252")
    indent = "  " * depth
    print(f"{indent}[{depth}] {safe_title}")

    # Convert HTML → Markdown
    html_body = page.get("body", {}).get("storage", {}).get("value", "")
    if html_body:
        md_content = html_to_markdown(html_body, title)
    else:
        md_content = f"# {title}\n\n*(empty page)*"

    # Save to file
    safe_name = sanitize_filename(title)
    output_dir.mkdir(parents=True, exist_ok=True)
    md_path = output_dir / f"{safe_name}.md"
    md_path.write_text(md_content, encoding="utf-8")
    count = 1

    # Crawl children
    children = client.get_child_pages(page_id)
    if children:
        child_dir = output_dir / safe_name
        for child in children:
            child_id = child["id"]
            count += crawl_page(
                client, child_id, child_dir,
                depth=depth + 1, max_depth=max_depth, visited=visited,
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
    print()

    # Crawl
    output_dir = Path(args.output)
    count = crawl_page(client, page_id, output_dir, max_depth=args.depth)

    print()
    print(f"Done! Saved {count} page(s) to {output_dir}")


if __name__ == "__main__":
    main()
