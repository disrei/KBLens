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


def html_to_markdown(html_content: str, title: str = "") -> str:
    """Convert Confluence HTML storage format to Markdown."""
    global _markitdown_instance

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
            return text
    except ImportError:
        pass
    except Exception as e:
        print(f"  [WARN] markitdown failed: {e}, falling back to basic conversion")

    # Fallback: basic HTML tag stripping
    return _basic_html_to_md(html_content, title)


def _basic_html_to_md(html: str, title: str = "") -> str:
    """Very basic HTML → Markdown fallback (no external deps)."""
    import html as html_module

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
    indent = "  " * depth
    print(f"{indent}[{depth}] {title}")

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
