"""KBLens knowledge base viewer — local HTTP server with live Markdown rendering.

Serves the generated knowledge base as a browsable HTML site with:
- Left sidebar: collapsible tree navigation built from _meta.json + directory scan
- Right content: Markdown rendered to HTML with syntax-highlighted code blocks
- Single-page app: clicking navigation fetches rendered HTML fragments via API

Usage:
    kblens serve --kb ./output_code --kb ./output_docs --port 9753
"""

from __future__ import annotations

import html as _html_mod
import json
import logging
import re
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Any

logger = logging.getLogger("kblens.server")

# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------

_md_renderer = None


def _get_md():
    """Lazy-init markdown-it-py with common plugins."""
    global _md_renderer
    if _md_renderer is not None:
        return _md_renderer

    from markdown_it import MarkdownIt

    md = MarkdownIt("commonmark", {"html": True, "typographer": False})
    # Enable tables, strikethrough, linkify-like features
    md.enable(["table", "strikethrough"])

    _md_renderer = md
    return md


def _highlight_code(code: str, lang: str) -> str:
    """Highlight code block using pygments. Returns raw HTML."""
    try:
        from pygments import highlight
        from pygments.lexers import get_lexer_by_name, TextLexer
        from pygments.formatters import HtmlFormatter

        try:
            lexer = get_lexer_by_name(lang, stripall=True)
        except Exception:
            lexer = TextLexer(stripall=True)
        formatter = HtmlFormatter(nowrap=True, cssclass="highlight")
        return highlight(code, lexer, formatter)
    except Exception:
        return _html_mod.escape(code)


def render_markdown(text: str) -> str:
    """Convert Markdown text to HTML with syntax highlighting."""
    md = _get_md()

    # We'll do a two-pass approach:
    # 1. Render with markdown-it
    # 2. Post-process fenced code blocks for pygments highlighting
    html = md.render(text)

    # markdown-it outputs fenced code as <pre><code class="language-xxx">...</code></pre>
    # We replace the inner content with pygments-highlighted HTML
    def _replace_code_block(match: re.Match) -> str:
        lang = match.group(1) or ""
        raw_code = _html_mod.unescape(match.group(2))
        if lang:
            highlighted = _highlight_code(raw_code, lang)
            return f'<pre><code class="language-{lang} highlight">{highlighted}</code></pre>'
        else:
            return match.group(0)  # no language, leave as-is

    html = re.sub(
        r'<pre><code class="language-(\w+)">(.*?)</code></pre>',
        _replace_code_block,
        html,
        flags=re.DOTALL,
    )

    return html


# ---------------------------------------------------------------------------
# Navigation tree builder
# ---------------------------------------------------------------------------


def _posix(p: Path | str) -> str:
    """Convert path to forward-slash string (safe for URLs on Windows)."""
    return str(p).replace("\\", "/")


def build_nav_tree(source_dir: Path) -> list[dict[str, Any]]:
    """Build navigation tree for a single source from directory structure.

    Returns a list of tree nodes:
    [
        {"label": "INDEX", "path": "INDEX.md", "type": "file"},
        {"label": "package-name", "type": "package", "children": [
            {"label": "component", "path": "pkg/comp.md", "type": "file"},
            {"label": "component (dir)", "type": "component", "children": [
                {"label": "leaf", "path": "pkg/comp/leaf.md", "type": "file"},
            ]}
        ]}
    ]
    """
    tree: list[dict[str, Any]] = []

    # INDEX.md is always the first entry
    index_path = source_dir / "INDEX.md"
    if index_path.exists():
        tree.append({"label": "INDEX", "path": "INDEX.md", "type": "index"})

    # The packages directory is source_dir / {source_name} (same name as source dir)
    # But there might be multiple source names; scan all subdirectories that contain .md files
    # Actually: the structure is source_dir/{source_name}/  where source_name dirs contain packages
    # Let's scan for all directories that contain .md files at depth 1 as packages

    # Find the packages root — it's the subdirectory that contains package .md files
    # Convention: source_dir/{source_name}/ is the packages root
    packages_roots = []
    for child in sorted(source_dir.iterdir()):
        if child.is_dir() and not child.name.startswith("_"):
            # Check if it contains .md files (package overviews)
            if any(child.glob("*.md")):
                packages_roots.append(child)

    # Also check _root-like dirs
    for child in sorted(source_dir.iterdir()):
        if child.is_dir() and child.name.startswith("_") and child.name != "__pycache__":
            if any(child.glob("*.md")):
                packages_roots.append(child)

    # In practice there's usually exactly one packages root (same name as source)
    # But we handle multiple for robustness
    for pkg_root in packages_roots:
        rel_root = pkg_root.relative_to(source_dir)

        # Each .md file at this level is a package overview
        # Each subdirectory at this level is a package's component directory
        packages: dict[str, dict[str, Any]] = {}

        # Collect package overview files
        for md_file in sorted(pkg_root.glob("*.md")):
            pkg_name = md_file.stem
            if pkg_name not in packages:
                packages[pkg_name] = {
                    "label": pkg_name,
                    "type": "package",
                    "overview_path": _posix(rel_root / md_file.name),
                    "children": [],
                }

        # Collect component directories and their files
        for subdir in sorted(pkg_root.iterdir()):
            if not subdir.is_dir():
                continue
            pkg_name = subdir.name
            if pkg_name not in packages:
                packages[pkg_name] = {
                    "label": pkg_name,
                    "type": "package",
                    "overview_path": None,
                    "children": [],
                }
            pkg_node = packages[pkg_name]

            # Files directly in the component dir are component files
            for md_file in sorted(subdir.glob("*.md")):
                comp_name = md_file.stem
                rel_path = _posix(rel_root / subdir.name / md_file.name)

                # Check if there's a subdirectory with the same name (large component)
                comp_subdir = subdir / comp_name
                if comp_subdir.is_dir() and any(comp_subdir.glob("*.md")):
                    # Large component with leaf files
                    comp_node: dict[str, Any] = {
                        "label": comp_name,
                        "type": "component",
                        "overview_path": rel_path,
                        "children": [],
                    }
                    for leaf_file in sorted(comp_subdir.glob("*.md")):
                        leaf_rel = _posix(rel_root / subdir.name / comp_name / leaf_file.name)
                        comp_node["children"].append(
                            {"label": leaf_file.stem, "path": leaf_rel, "type": "leaf"}
                        )
                    pkg_node["children"].append(comp_node)
                else:
                    # Simple component — single file
                    pkg_node["children"].append(
                        {"label": comp_name, "path": rel_path, "type": "component"}
                    )

            # Also check for directories that don't have a matching .md
            for comp_subdir in sorted(subdir.iterdir()):
                if not comp_subdir.is_dir():
                    continue
                # Skip if already handled above
                if (subdir / f"{comp_subdir.name}.md").exists():
                    continue
                if any(comp_subdir.glob("*.md")):
                    comp_node = {
                        "label": comp_subdir.name,
                        "type": "component",
                        "overview_path": None,
                        "children": [],
                    }
                    for leaf_file in sorted(comp_subdir.glob("*.md")):
                        leaf_rel = _posix(
                            rel_root / subdir.name / comp_subdir.name / leaf_file.name
                        )
                        comp_node["children"].append(
                            {"label": leaf_file.stem, "path": leaf_rel, "type": "leaf"}
                        )
                    pkg_node["children"].append(comp_node)

        # Add packages to tree
        for pkg_name in sorted(packages.keys()):
            tree.append(packages[pkg_name])

    return tree


# ---------------------------------------------------------------------------
# Multi-source discovery
# ---------------------------------------------------------------------------


def discover_sources(output_dir: Path) -> list[dict[str, Any]]:
    """Discover all source directories under output_dir.

    Each source directory contains INDEX.md and _meta.json.
    Returns [{"name": "source-name", "path": Path, "meta": {...}}, ...]
    """
    sources = []
    for child in sorted(output_dir.iterdir()):
        if not child.is_dir():
            continue
        meta_file = child / "_meta.json"
        index_file = child / "INDEX.md"
        if meta_file.exists() and index_file.exists():
            try:
                meta = json.loads(meta_file.read_text(encoding="utf-8"))
            except Exception:
                meta = {}
            sources.append({"name": child.name, "path": child, "meta": meta})
    return sources


# ---------------------------------------------------------------------------
# CSS & JS (embedded)
# ---------------------------------------------------------------------------

PYGMENTS_CSS = ""


def _get_pygments_css() -> str:
    """Generate pygments CSS for code highlighting."""
    global PYGMENTS_CSS
    if PYGMENTS_CSS:
        return PYGMENTS_CSS
    try:
        from pygments.formatters import HtmlFormatter

        PYGMENTS_CSS = HtmlFormatter(style="github-dark").get_style_defs(".highlight")
    except Exception:
        PYGMENTS_CSS = ""
    return PYGMENTS_CSS


PAGE_CSS = """
:root {
    --bg: #0d1117;
    --bg-secondary: #161b22;
    --bg-tertiary: #21262d;
    --text: #e6edf3;
    --text-secondary: #8b949e;
    --text-muted: #6e7681;
    --border: #30363d;
    --accent: #58a6ff;
    --accent-hover: #79c0ff;
    --link: #58a6ff;
    --success: #3fb950;
    --warning: #d29922;
    --sidebar-width: 280px;
    --header-height: 48px;
}

* { margin: 0; padding: 0; box-sizing: border-box; }

body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans",
                 Helvetica, Arial, sans-serif;
    background: var(--bg);
    color: var(--text);
    line-height: 1.6;
    overflow: hidden;
    height: 100vh;
}

/* --- Header --- */
.header {
    height: var(--header-height);
    background: var(--bg-secondary);
    border-bottom: 1px solid var(--border);
    display: flex;
    align-items: center;
    padding: 0 16px;
    gap: 12px;
    position: fixed;
    top: 0; left: 0; right: 0;
    z-index: 100;
}
.header-logo {
    font-weight: 600;
    font-size: 15px;
    color: var(--text);
    white-space: nowrap;
}
.header-logo span { color: var(--accent); }
.header-sep {
    color: var(--text-muted);
    font-size: 18px;
    font-weight: 300;
}
.header-project {
    color: var(--text-secondary);
    font-size: 14px;
    flex: 1;
}
/* --- Layout --- */
.layout {
    display: flex;
    position: fixed;
    top: var(--header-height);
    left: 0; right: 0; bottom: 0;
}

/* --- Sidebar --- */
.sidebar {
    width: var(--sidebar-width);
    min-width: var(--sidebar-width);
    background: var(--bg-secondary);
    border-right: 1px solid var(--border);
    overflow-y: auto;
    padding: 12px 0;
    font-size: 13px;
}
.sidebar::-webkit-scrollbar { width: 6px; }
.sidebar::-webkit-scrollbar-track { background: transparent; }
.sidebar::-webkit-scrollbar-thumb { background: var(--bg-tertiary); border-radius: 3px; }

.nav-item {
    display: flex;
    align-items: center;
    padding: 4px 12px 4px 16px;
    cursor: pointer;
    color: var(--text-secondary);
    text-decoration: none;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    user-select: none;
    gap: 6px;
    border-left: 2px solid transparent;
    transition: all 0.15s;
}
.nav-item:hover {
    background: var(--bg-tertiary);
    color: var(--text);
}
.nav-item.active {
    background: rgba(88, 166, 255, 0.1);
    color: var(--accent);
    border-left-color: var(--accent);
}
.nav-item .icon {
    flex-shrink: 0;
    font-size: 14px;
    width: 18px;
    text-align: center;
}
.nav-item .label {
    overflow: hidden;
    text-overflow: ellipsis;
}

.nav-group { margin-bottom: 2px; }
.nav-group-header {
    display: flex;
    align-items: center;
    padding: 4px 12px 4px 16px;
    cursor: pointer;
    color: var(--text);
    font-weight: 500;
    font-size: 13px;
    gap: 6px;
    user-select: none;
    transition: all 0.15s;
}
.nav-group-header:hover { background: var(--bg-tertiary); }
.nav-group-header .toggle {
    font-size: 10px;
    transition: transform 0.15s;
    color: var(--text-muted);
    width: 14px;
    text-align: center;
    flex-shrink: 0;
}
.nav-group-header .toggle.open { transform: rotate(90deg); }
.nav-group-header .icon { font-size: 14px; width: 18px; text-align: center; flex-shrink: 0; }
.nav-group-children { display: none; }
.nav-group-children.open { display: block; }

/* Indentation levels */
.depth-0 { padding-left: 16px; }
.depth-1 { padding-left: 32px; }
.depth-2 { padding-left: 48px; }
.depth-3 { padding-left: 64px; }

/* --- Content --- */
.content {
    flex: 1;
    overflow-y: auto;
    padding: 32px 48px;
    max-width: 100%;
}
.content::-webkit-scrollbar { width: 8px; }
.content::-webkit-scrollbar-track { background: transparent; }
.content::-webkit-scrollbar-thumb { background: var(--bg-tertiary); border-radius: 4px; }

/* --- Markdown rendering --- */
.md-body {
    max-width: 900px;
    margin: 0 auto;
}
.md-body h1 {
    font-size: 28px;
    font-weight: 600;
    padding-bottom: 8px;
    border-bottom: 1px solid var(--border);
    margin-bottom: 16px;
}
.md-body h2 {
    font-size: 22px;
    font-weight: 600;
    margin-top: 32px;
    margin-bottom: 12px;
    padding-bottom: 6px;
    border-bottom: 1px solid var(--border);
}
.md-body h3 {
    font-size: 18px;
    font-weight: 600;
    margin-top: 24px;
    margin-bottom: 8px;
}
.md-body h4, .md-body h5, .md-body h6 {
    font-size: 15px;
    font-weight: 600;
    margin-top: 20px;
    margin-bottom: 6px;
}
.md-body p {
    margin-bottom: 12px;
}
.md-body a {
    color: var(--link);
    text-decoration: none;
}
.md-body a:hover { text-decoration: underline; }
.md-body ul, .md-body ol {
    margin-bottom: 12px;
    padding-left: 24px;
}
.md-body li { margin-bottom: 4px; }
.md-body li > ul, .md-body li > ol { margin-bottom: 0; }
.md-body blockquote {
    border-left: 3px solid var(--accent);
    padding: 4px 16px;
    margin: 12px 0;
    color: var(--text-secondary);
    background: var(--bg-secondary);
    border-radius: 0 6px 6px 0;
}
.md-body code {
    background: var(--bg-tertiary);
    border-radius: 4px;
    padding: 2px 6px;
    font-size: 0.9em;
    font-family: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace;
}
.md-body pre {
    background: var(--bg-secondary);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 16px;
    margin: 12px 0;
    overflow-x: auto;
    font-size: 13px;
    line-height: 1.5;
}
.md-body pre code {
    background: none;
    padding: 0;
    border-radius: 0;
    font-size: inherit;
    color: var(--text);
}
.md-body table {
    width: 100%;
    border-collapse: collapse;
    margin: 12px 0;
    font-size: 14px;
}
.md-body table th {
    background: var(--bg-secondary);
    font-weight: 600;
    text-align: left;
    padding: 8px 12px;
    border: 1px solid var(--border);
}
.md-body table td {
    padding: 8px 12px;
    border: 1px solid var(--border);
}
.md-body table tr:nth-child(even) { background: rgba(255,255,255,0.02); }
.md-body hr {
    border: none;
    border-top: 2px solid var(--border);
    margin: 24px 0;
}
.md-body img {
    max-width: 100%;
    border-radius: 6px;
    margin: 8px 0;
}
.md-body strong { font-weight: 600; }
.md-body em { font-style: italic; color: var(--text-secondary); }

/* --- Source header in sidebar --- */
.source-header {
    padding-left: 12px !important;
    font-size: 14px; font-weight: 600; color: var(--accent);
    margin-top: 8px; padding-top: 8px;
    border-top: 1px solid var(--border);
}
.nav-group:first-child .source-header { border-top: none; margin-top: 0; }

/* --- Loading / empty states --- */
.loading {
    display: flex;
    align-items: center;
    justify-content: center;
    height: 200px;
    color: var(--text-muted);
    font-size: 14px;
}
"""

PAGE_JS = """
let currentPath = null;
let currentSource = null;

async function init() {
    const resp = await fetch('/api/sources');
    const sources = await resp.json();
    const sidebar = document.getElementById('sidebar-nav');
    sidebar.innerHTML = '';

    // Load all source trees in parallel
    const treeResults = await Promise.all(
        sources.map(async s => {
            const r = await fetch('/api/tree?source=' + encodeURIComponent(s.name));
            return { name: s.name, tree: await r.json() };
        })
    );

    // Build sidebar: each source is a top-level group
    treeResults.forEach(({ name, tree }) => {
        const srcGroup = document.createElement('div');
        srcGroup.className = 'nav-group';

        const srcHeader = document.createElement('div');
        srcHeader.className = 'nav-group-header depth-0 source-header';

        const toggle = document.createElement('span');
        toggle.className = 'toggle open';
        toggle.textContent = '\\u25B6';

        const icon = document.createElement('span');
        icon.className = 'icon';
        icon.textContent = '\\u{1F4DA}';

        const label = document.createElement('span');
        label.className = 'label';
        label.textContent = name;
        label.title = name;

        srcHeader.appendChild(toggle);
        srcHeader.appendChild(icon);
        srcHeader.appendChild(label);

        const srcChildren = document.createElement('div');
        srcChildren.className = 'nav-group-children open';

        tree.forEach(node => {
            srcChildren.appendChild(buildNavNode(node, name, 1));
        });

        srcHeader.addEventListener('click', () => {
            srcChildren.classList.toggle('open');
            toggle.classList.toggle('open');
        });

        srcGroup.appendChild(srcHeader);
        srcGroup.appendChild(srcChildren);
        sidebar.appendChild(srcGroup);
    });

    // Auto-load first source INDEX
    if (treeResults.length > 0) {
        await loadPage('INDEX.md', treeResults[0].name);
    }
}

function buildNavNode(node, sourceName, depth) {
    // Leaf-like nodes: index, simple component, leaf
    if (node.type === 'index'
        || (node.type === 'component' && !node.children)
        || node.type === 'leaf') {
        const el = document.createElement('div');
        el.className = 'nav-item depth-' + depth;
        el.dataset.path = node.path;
        el.dataset.source = sourceName;

        const icon = document.createElement('span');
        icon.className = 'icon';
        icon.textContent = node.type === 'index' ? '\\u{1F3E0}' : '\\u{1F4C4}';

        const label = document.createElement('span');
        label.className = 'label';
        label.textContent = node.label;
        label.title = node.label;

        el.appendChild(icon);
        el.appendChild(label);
        el.addEventListener('click', () => loadPage(node.path, sourceName));
        return el;
    }

    // Group nodes: package or component with children
    const group = document.createElement('div');
    group.className = 'nav-group';

    const header = document.createElement('div');
    header.className = 'nav-group-header depth-' + depth;

    const toggle = document.createElement('span');
    toggle.className = 'toggle';
    toggle.textContent = '\\u25B6';

    const icon = document.createElement('span');
    icon.className = 'icon';
    icon.textContent = node.type === 'package' ? '\\u{1F4E6}' : '\\u{1F4C1}';

    const label = document.createElement('span');
    label.className = 'label';
    label.textContent = node.label;
    label.title = node.label;

    header.appendChild(toggle);
    header.appendChild(icon);
    header.appendChild(label);

    const childrenContainer = document.createElement('div');
    childrenContainer.className = 'nav-group-children';

    if (node.overview_path) {
        const ov = document.createElement('div');
        ov.className = 'nav-item depth-' + (depth + 1);
        ov.dataset.path = node.overview_path;
        ov.dataset.source = sourceName;
        const oIcon = document.createElement('span');
        oIcon.className = 'icon';
        oIcon.textContent = '\\u{1F4CB}';
        const oLabel = document.createElement('span');
        oLabel.className = 'label';
        oLabel.textContent = 'Overview';
        ov.appendChild(oIcon);
        ov.appendChild(oLabel);
        ov.addEventListener('click', () => loadPage(node.overview_path, sourceName));
        childrenContainer.appendChild(ov);
    }

    if (node.children) {
        node.children.forEach(child => {
            childrenContainer.appendChild(buildNavNode(child, sourceName, depth + 1));
        });
    }

    header.addEventListener('click', () => {
        childrenContainer.classList.toggle('open');
        toggle.classList.toggle('open');
    });

    group.appendChild(header);
    group.appendChild(childrenContainer);
    return group;
}

async function loadPage(path, sourceName) {
    currentPath = path;
    currentSource = sourceName;

    // Update active state
    document.querySelectorAll('.nav-item').forEach(el => el.classList.remove('active'));
    document.querySelectorAll('.nav-item').forEach(el => {
        if (el.dataset.path === path && el.dataset.source === sourceName) {
            el.classList.add('active');
            // Expand parent groups
            let parent = el.parentElement;
            while (parent) {
                if (parent.classList.contains('nav-group-children')) {
                    parent.classList.add('open');
                    const hdr = parent.previousElementSibling;
                    if (hdr) {
                        const t = hdr.querySelector('.toggle');
                        if (t) t.classList.add('open');
                    }
                }
                parent = parent.parentElement;
            }
        }
    });

    const content = document.getElementById('content');
    content.innerHTML = '<div class="loading">Loading...</div>';

    try {
        const url = '/api/page?source=' + encodeURIComponent(sourceName)
                  + '&path=' + encodeURIComponent(path);
        const resp = await fetch(url);
        if (!resp.ok) throw new Error('Failed to load page');
        const data = await resp.json();
        content.innerHTML = '<div class="md-body">' + data.html + '</div>';
    } catch (e) {
        content.innerHTML = '<div class="loading">Error: ' + e.message + '</div>';
    }
}

document.addEventListener('DOMContentLoaded', init);
"""


def _build_page_html(project_name: str) -> str:
    """Build the full single-page HTML shell."""
    pygments_css = _get_pygments_css()
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>KBLens · {project_name}</title>
<style>
{PAGE_CSS}
{pygments_css}
</style>
</head>
<body>
<div class="header">
    <div class="header-logo"><span>KB</span>Lens</div>
    <span class="header-sep">/</span>
    <div class="header-project">{project_name}</div>
</div>
<div class="layout">
    <nav class="sidebar" id="sidebar-nav">
        <div class="loading">Loading...</div>
    </nav>
    <main class="content" id="content">
        <div class="loading">Loading...</div>
    </main>
</div>
<script>
{PAGE_JS}
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# HTTP Request Handler
# ---------------------------------------------------------------------------


class KBLensHandler(BaseHTTPRequestHandler):
    """Handle requests for the knowledge base viewer."""

    source_map: dict[str, Path]  # {source_name: source_dir} set by serve()
    project_name: str  # set by serve()

    def log_message(self, format: str, *args: Any) -> None:
        """Suppress default access log; use logger instead."""
        logger.debug(format, *args)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = dict(urllib.parse.parse_qsl(parsed.query))

        if path == "/" or path == "/index.html":
            self._serve_html_shell()
        elif path == "/api/sources":
            self._serve_sources()
        elif path == "/api/tree":
            self._serve_tree(query)
        elif path == "/api/page":
            self._serve_page(query)
        else:
            self._serve_404()

    def _send_json(self, data: Any, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: str, status: int = 200) -> None:
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_404(self) -> None:
        self._send_json({"error": "Not found"}, 404)

    def _serve_html_shell(self) -> None:
        html = _build_page_html(self.project_name)
        self._send_html(html)

    def _serve_sources(self) -> None:
        result = []
        for name, src_path in sorted(self.source_map.items()):
            meta: dict[str, Any] = {}
            meta_file = src_path / "_meta.json"
            if meta_file.exists():
                try:
                    meta = json.loads(meta_file.read_text(encoding="utf-8"))
                except Exception:
                    pass
            result.append({
                "name": name,
                "components": meta.get("total_components", 0),
                "model": meta.get("llm_model", ""),
                "generated_at": meta.get("generated_at", ""),
            })
        self._send_json(result)

    def _serve_tree(self, query: dict[str, str]) -> None:
        source_name = query.get("source", "")
        source_dir = self.source_map.get(source_name)
        if not source_dir or not source_dir.is_dir():
            self._send_json({"error": f"Source not found: {source_name}"}, 404)
            return
        tree = build_nav_tree(source_dir)
        self._send_json(tree)

    def _serve_page(self, query: dict[str, str]) -> None:
        source_name = query.get("source", "")
        rel_path = query.get("path", "")

        if not source_name or not rel_path:
            self._send_json({"error": "Missing source or path"}, 400)
            return

        source_dir = self.source_map.get(source_name)
        if not source_dir:
            self._send_json({"error": f"Source not found: {source_name}"}, 404)
            return

        # Security: prevent path traversal
        try:
            safe_path = Path(rel_path)
            if safe_path.is_absolute() or ".." in safe_path.parts:
                self._send_json({"error": "Invalid path"}, 400)
                return
        except Exception:
            self._send_json({"error": "Invalid path"}, 400)
            return

        file_path = source_dir / safe_path
        if not file_path.exists() or not file_path.is_file():
            self._send_json({"error": f"File not found: {rel_path}"}, 404)
            return

        # Ensure file is within source_dir (resolved path check)
        try:
            file_path.resolve().relative_to(source_dir.resolve())
        except ValueError:
            self._send_json({"error": "Access denied"}, 403)
            return

        text = file_path.read_text(encoding="utf-8")
        html = render_markdown(text)
        self._send_json({"html": html, "path": rel_path})


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def discover_all_sources(output_dirs: list[Path]) -> dict[str, Path]:
    """Discover sources across multiple output directories.

    Returns {source_name: source_dir_path}. If names collide, later dirs win.
    """
    source_map: dict[str, Path] = {}
    for output_dir in output_dirs:
        if not output_dir.is_dir():
            continue
        for s in discover_sources(output_dir):
            source_map[s["name"]] = s["path"]
    return source_map


def serve(
    output_dirs: list[Path],
    project_name: str = "KBLens",
    host: str = "127.0.0.1",
    port: int = 9753,
) -> None:
    """Start the knowledge base HTTP server.

    Args:
        output_dirs: List of root output directories, each may contain
                     source subdirectories with INDEX.md + _meta.json.
        project_name: Project name to display in the header.
        host: Bind address.
        port: Listen port.
    """
    source_map = discover_all_sources(output_dirs)
    if not source_map:
        dirs_str = ", ".join(str(d) for d in output_dirs)
        raise FileNotFoundError(
            f"No knowledge base sources found in: {dirs_str}. "
            f"Run 'kblens generate' first."
        )

    KBLensHandler.source_map = source_map
    KBLensHandler.project_name = project_name

    http_server = HTTPServer((host, port), KBLensHandler)

    from rich.console import Console

    console = Console()
    console.print()
    console.print("[bold]KBLens Knowledge Base Viewer[/bold]")
    console.print(f"  Project:  [cyan]{project_name}[/cyan]")
    console.print(f"  Sources:  {len(source_map)}")
    for name, spath in sorted(source_map.items()):
        console.print(f"    [dim]{name}[/dim]  {spath}")
    console.print()
    console.print(f"  [bold green]http://{host}:{port}[/bold green]")
    console.print()
    console.print("  Press [bold]Ctrl+C[/bold] to stop")
    console.print()

    try:
        http_server.serve_forever()
    except KeyboardInterrupt:
        console.print("\n[yellow]Server stopped.[/yellow]")
        http_server.server_close()
