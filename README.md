```
██╗  ██╗██████╗       ██╗     ███████╗███╗   ██╗███████╗
██║ ██╔╝██╔══██╗      ██║     ██╔════╝████╗  ██║██╔════╝
█████╔╝ ██████╔╝      ██║     █████╗  ██╔██╗ ██║███████╗
██╔═██╗ ██╔══██╗      ██║     ██╔══╝  ██║╚██╗██║╚════██║
██║  ██╗██████╔╝      ███████╗███████╗██║ ╚████║███████║
╚═╝  ╚═╝╚═════╝       ╚══════╝╚══════╝╚═╝  ╚═══╝╚══════╝
═══════════════════════════════════════════════════════════
     Knowledge Base Lens · Code & Document Intelligence
═══════════════════════════════════════════════════════════
```

English | [中文](README_zh.md)

A progressive-disclosure knowledge base generator for large codebases and document collections. KBLens uses tree-sitter to extract AST skeletons from source code, and markitdown to convert documents from various formats (PDF, DOCX, PPTX, HTML, etc.) to Markdown. Both are packed into LLM-friendly batches and summarized into hierarchical Markdown — giving AI assistants structured context without reading every file.

## Why KBLens

When doing **vibe coding** — using AI assistants (Cursor, Copilot, OpenCode, etc.) to write and refactor code through natural language — the AI needs to understand your codebase's architecture. But large codebases (100K+ files) are too big for LLMs to consume directly. Without structured context, AI assistants either hallucinate or say "I don't know" when asked about internal systems.

The same problem applies to **document collections** — internal wikis, technical docs, design specs, API references. They contain critical knowledge but are scattered across formats and too large for LLMs to ingest as-is.

KBLens solves both by generating a **three-layer knowledge base** from your actual source code and documents:

```
L0  INDEX.md            Project overview + package directory
L1  packages/engine.md  Per-package component listing and architecture
L2  packages/engine/    Per-component: purpose, key types, public APIs, dependencies
```

This gives AI assistants a reliable, searchable reference — like an always-up-to-date architecture document generated from actual code and docs. Point your AI tool at the knowledge base, and it can answer questions like "how does the physics system work?" or "what's the configuration reference for deployment?" without reading every source file.

## Key Features

- **Dual mode** — Processes both code (via tree-sitter AST extraction) and documents (via markitdown conversion + section splitting) through the same pipeline
- **AST-based code extraction** — Uses tree-sitter to extract class/struct/enum/function signatures from C++, C#, Python, TypeScript, and JavaScript source files. No guessing, no hallucination.
- **Document format support** — PDF, DOCX, PPTX, XLSX, HTML, CSV, EPUB, and more via [markitdown](https://github.com/microsoft/markitdown). Documents are converted to Markdown, split by heading level, and summarized.
- **Hybrid output** — LLM generates concise summaries, while raw content (AST signatures for code, original text for documents) is appended directly. Zero truncation, minimal LLM output tokens.
- **Hierarchical summaries** — Three levels of detail (project → package → component) with progressive disclosure. Ask about a package, get the overview. Ask about a class or document section, get the details.
- **Incremental updates** — Only regenerates components whose source files changed. Tracks changes via file hash. A full run on 200+ components takes ~5 minutes; incremental runs take seconds.
- **Local LLM support** — Works with local models via llama.cpp, Ollama, or any OpenAI-compatible API. Includes thinking-mode detection for models like Qwen3.5 and DeepSeek-R1.
- **Change detection** — Five-way classification (unchanged / changed / new / deleted / failed) with automatic cleanup of orphaned files and cascade updates to affected packages.
- **Multi-source projects** — One config file can define multiple source directories with different types (code or document). Each source gets its own independent knowledge base.
- **Concurrent generation** — Processes 8 components in parallel with 8 concurrent LLM calls. Includes exponential backoff retry (3 attempts) for transient failures.
- **Browser viewer** — Built-in `kblens serve` command starts a local HTTP server to browse the knowledge base in your browser with syntax-highlighted code, Markdown rendering, and a tree navigation sidebar. Supports viewing multiple knowledge bases (code + docs) simultaneously.
- **Resume from interruption** — Progress is persisted after each component. Ctrl+C and re-run to continue where you left off.
- **Live dashboard** — Rich terminal UI showing real-time progress, active components, token usage, and error count.

## Prerequisites

- **Python 3.11+**
- **C compiler** — Required by tree-sitter for grammar compilation (GCC, Clang, or MSVC)
  - On Ubuntu/Debian: `sudo apt install build-essential`
  - On macOS: Xcode Command Line Tools (`xcode-select --install`)
  - On Windows: Visual Studio Build Tools or MinGW

## Installation

```bash
# From PyPI (code knowledge base only)
pip install kblens

# With document format support (PDF, DOCX, PPTX, HTML, etc.)
pip install 'kblens[docs]'

# With full document format support (all markitdown backends)
pip install 'kblens[docs-all]'

# Upgrade to latest version
pip install --upgrade kblens

# Or install from GitHub directly
pip install git+https://github.com/disrei/KBLens.git

# Or clone and install in development mode
git clone https://github.com/disrei/KBLens.git
cd kblens
pip install -e .            # code only
pip install -e ".[docs]"    # + document support

# Verify
kblens version
```

### Install Extras

| Extra | Command | What It Adds |
|-------|---------|--------------|
| (none) | `pip install kblens` | Code KB (C++, C#, Python, TS/JS) + documents (.md, .txt only) |
| `docs` | `pip install 'kblens[docs]'` | + PDF, DOCX, PPTX, XLSX, HTML, CSV, EPUB via markitdown |
| `docs-all` | `pip install 'kblens[docs-all]'` | + all markitdown optional backends |
| `dev` | `pip install 'kblens[dev]'` | + pytest, ruff |

## Quick Start

### 1. Create a configuration

```bash
kblens init
```

This walks you through creating `~/.config/kblens/config.yaml` with your source paths and LLM settings.

Or create it manually:

```yaml
# ~/.config/kblens/config.yaml
version: 1
project: "my_project"

output_dir: "~/kblens_kb/my_project"

sources:
  # Code source — uses AST extraction
  - path: "/path/to/src"
    name: "source-code"

  # Document source — uses markitdown + section splitting
  - path: "/path/to/docs"
    name: "project-docs"
    type: "document"

llm:
  model: "gpt-4o-mini"
  # api_key: "your-api-key"     # see "API Key Security" below
  temperature: 0.2

summary_language: "en"
```

### 2. Preview

```bash
kblens generate --dry-run
```

This scans your source, extracts AST / document sections, and reports statistics without calling the LLM.

### 3. Generate

```bash
kblens generate
```

For a project with ~200 components, expect ~5 minutes and ~400K input tokens.

### 4. Use

The generated knowledge base is a directory of Markdown files. You can:

- **Browse in browser** — Run `kblens serve` to open a local viewer with syntax highlighting, tree navigation, and Markdown rendering
- **Browse directly** — Open `INDEX.md` and navigate through the hierarchy
- **Search with grep** — Find any class, function, or concept across all summaries
- **Integrate with AI tools** — Point your coding assistant's skill/tool at the knowledge base directory (see [AI Assistant Integration](#ai-assistant-integration) below)

#### Browser Viewer

Each `kblens generate` run **appends** its output directory to the `KBLENS_KB_PATH` environment variable. Run multiple generations (e.g., one for code, one for docs), then a single `kblens serve` shows everything together.

```bash
# After running generate for each knowledge base, just:
kblens serve

# Or explicitly specify directories:
kblens serve --kb ~/kblens_kb/my_project

# Browse multiple knowledge bases (code + docs) together
kblens serve --kb ~/kblens_kb/code_output --kb ~/kblens_kb/doc_output

# Use a specific config file to locate the output directory
kblens serve --config kblens.yaml
```

The viewer starts a local HTTP server (default port 9753) with:
- **Left sidebar** — Collapsible tree showing all sources, packages, and components
- **Right content** — Markdown rendered with GitHub-dark styling and syntax-highlighted code blocks
- **Multi-source** — All KBs from `KBLENS_KB_PATH` and `--kb` flags are merged; all sources appear in the sidebar

## Document Knowledge Base

KBLens can generate knowledge bases from document collections — technical docs, wikis, design specs, API references, etc.

### Supported Formats

| Format | Extensions | Requirement |
|--------|-----------|-------------|
| Markdown | `.md` | Built-in (no extra deps) |
| Plain text | `.txt` | Built-in |
| PDF | `.pdf` | `pip install 'kblens[docs]'` |
| Word | `.docx`, `.doc` | `pip install 'kblens[docs]'` |
| PowerPoint | `.pptx` | `pip install 'kblens[docs]'` |
| Excel | `.xlsx`, `.xls` | `pip install 'kblens[docs]'` |
| HTML | `.html`, `.htm` | `pip install 'kblens[docs]'` |
| CSV | `.csv` | `pip install 'kblens[docs]'` |
| EPUB | `.epub` | `pip install 'kblens[docs]'` |
| Jupyter | `.ipynb` | `pip install 'kblens[docs]'` |

Format conversion is powered by [markitdown](https://github.com/microsoft/markitdown) (Microsoft).

### How It Works (Documents)

The document pipeline replaces the AST extraction phase with:

1. **Convert** — Non-Markdown files are converted to Markdown via markitdown
2. **Section Extract** — Markdown is split by heading level (default: `##`) into sections
3. **Image Handling** — Image references are preserved as `[Image: alt text](path)` for searchability

The rest of the pipeline (packing, LLM summarization, aggregation, writing) is shared with the code path.

### Document Source Configuration

```yaml
sources:
  - path: "/path/to/docs"
    name: "project-docs"
    type: "document"            # Required: tells KBLens to use document pipeline
    section_level: 2            # Split on ## headings (default: 2)
    image_handling: "reference"  # Keep image refs (default: "reference", or "ignore")
```

### Document Output Format

Each leaf node in the document knowledge base has two sections:

```markdown
# Component Name

## Topic Summary
What this documentation covers and its purpose.

## Key Concepts and Definitions
Important terms, entities, and definitions.

## Actionable Information
Steps, commands, configurations, reference data.

## Related Topics
Connections to other documents.

---

## Original Content

### From: filename.md#section-heading
(Complete original text preserved for precise retrieval)
```

The LLM summary enables navigation and topic matching, while the original content below the `---` separator allows precise retrieval and direct quoting.

## Using with Local LLMs

KBLens works well with locally deployed LLMs for privacy-sensitive or cost-free usage.

### Recommended Setup

```bash
# Example: llama.cpp with Qwen3.5-9B
llama-server -m model.gguf -c 65536 --n-gpu-layers 99 --flash-attn on \
  -b 2048 -ub 512 --port 8080 --cache-type-k q8_0 --cache-type-v q8_0 -np 1
```

### Configuration for Local LLMs

```yaml
llm:
  model: "openai/your-model-name"
  api_base: "http://localhost:8080/v1"
  api_key: "not-needed"
  temperature: 0.2
  max_concurrent: 1               # Serial execution for local LLMs
  max_concurrent_components: 1

packing:
  token_budget: 20000             # Larger batches = fewer LLM calls
```

### Thinking Model Support

Models with built-in "thinking mode" (Qwen3.5, DeepSeek-R1, etc.) may output reasoning tokens instead of actual content by default. KBLens automatically detects this and shows a fix:

```
LLM returned empty content but has reasoning_content — the model is in
'thinking mode'. Disable thinking in your kblens config:

  llm:
    extra_body:
      chat_template_kwargs:
        enable_thinking: false
```

Add the suggested `extra_body` configuration to disable thinking mode:

```yaml
llm:
  model: "openai/Qwen3.5-9B"
  api_base: "http://localhost:8080/v1"
  api_key: "not-needed"
  extra_body:
    chat_template_kwargs:
      enable_thinking: false
```

The `extra_body` field passes arbitrary parameters to the LLM API, making it compatible with any server-specific options.

## API Key Security

**Never commit API keys to version control.** Use one of these methods:

1. **Environment variable** (recommended):
   ```bash
   export KBLENS_LLM_KEY=sk-your-key-here
   ```

2. **Local config override** — Create a `.local.yaml` sibling next to your config file:
   ```yaml
   # ~/.config/kblens/config.local.yaml (gitignored)
   llm:
     api_key: "sk-your-key-here"
   ```

3. **Config key_env reference** — Point to any environment variable:
   ```yaml
   llm:
     api_key_env: "MY_OPENAI_KEY"
   ```

## Configuration

KBLens uses a two-layer config system:

| Layer | Location | Purpose |
|-------|----------|---------|
| Global | `~/.config/kblens/config.yaml` | Shared LLM settings, packing parameters |
| Project | `./kblens.yaml` in project root | Project-specific sources and output |

Project config overrides global config. Each layer can have a `.local.yaml` sibling for sensitive values (API keys).

### Config Reference

```yaml
version: 1
project: "my_project"                # Project name (displayed in CLI)

output_dir: "~/kblens_kb/my_project"  # Knowledge base output root

sources:                              # Source directories to scan
  - path: "/path/to/src"             # Absolute path
    name: "core"                     # Short name (used as subdirectory)
    # type: "code"                   # Default: "code" (AST extraction)

  - path: "/path/to/docs"
    name: "docs"
    type: "document"                 # Document pipeline (markitdown + sections)
    section_level: 2                 # Split on H2 headings (default: 2)
    image_handling: "reference"      # "reference" (keep) or "ignore" (remove)

include_extensions: "auto"            # "auto" or explicit list: [".h", ".cpp"]

exclude_patterns:                     # Glob patterns to skip
  - "*/test/*"
  - "*_test.*"

llm:
  model: "gpt-4o-mini"               # Any litellm-compatible model
  api_base: "https://api.openai.com/v1"
  api_key: "sk-..."                   # Or use api_key_env / KBLENS_LLM_KEY
  temperature: 0.2
  max_concurrent: 8                   # Concurrent LLM calls
  max_concurrent_components: 8        # Concurrent component pipelines
  extra_body:                         # Extra params passed to LLM API
    chat_template_kwargs:             # Example: disable thinking mode
      enable_thinking: false

packing:
  token_budget: 8000                  # Target tokens per batch
  token_min: 1000                     # Minimum batch size
  token_max: 24000                    # Maximum batch size
  component_split_threshold: 200      # File count threshold for splitting

summary_language: "en"                # Language for generated summaries
```

### Environment Variables

| Variable | Purpose |
|----------|---------|
| `KBLENS_LLM_KEY` | LLM API key (overrides config) |
| `KBLENS_KB_PATH` | Accumulated automatically by each `kblens generate` run; used by AI skills and `kblens serve` to locate all KBs. Supports multiple paths separated by `;` (Windows) or `:` (Unix). |

## CLI Reference

```
kblens generate                    # Generate all sources
kblens generate --source core      # Generate only the "core" source
kblens generate --dry-run          # Preview without LLM calls
kblens generate --config ./my.yaml # Use specific config file
kblens serve                       # Browse KB in browser (auto-detect from env)
kblens serve --kb ./output         # Browse a specific KB directory
kblens serve --kb ./code --kb ./docs  # Browse multiple KBs together
kblens serve --port 8080           # Use a custom port
kblens status                      # Show knowledge base status
kblens monitor                     # Monitor a running generation
kblens init                        # Interactive config setup
kblens version                     # Show version
```

## Output Structure

For a project with a code source and a document source:

```
~/kblens_kb/my_project/
├── source-code/                    # Source: code
│   ├── INDEX.md                    # L0: package directory with links
│   ├── _meta.json                  # Component status, hashes, token counts
│   └── source-code/
│       ├── engine.md               # L1: engine package overview
│       ├── engine/
│       │   ├── SoundSystem.md      # L2: component (summary + AST signatures)
│       │   └── Physics.md
│       └── gameplay.md
├── project-docs/                   # Source: documents
│   ├── INDEX.md
│   ├── _meta.json
│   └── project-docs/
│       ├── api.md                  # L1: api package overview
│       ├── api/
│       │   └── api.md              # L2: component (summary + original content)
│       └── guides.md
```

### Code Output Format

```markdown
## Responsibility
What this component does.

## Key Types and Relationships
Classes, structs, enums and how they relate.

## Source Files
File paths grouped by role.

## Dependencies
Explicit #include paths.

---

## Complete API Signatures

​```cpp
class MyClass { void MyMethod(int param); };
​```
```

### Document Output Format

```markdown
## Topic Summary
What this documentation covers.

## Key Concepts and Definitions
Important terms and entities.

## Actionable Information
Steps, commands, configurations.

## Related Topics
Connections to other documents.

---

## Original Content

### From: filename.md#section-heading
(Complete original text)
```

## How It Works

KBLens runs a six-phase pipeline for each source:

1. **Scan** — Walk the directory tree, discover components (package/subdir pairs), count files and lines
2. **Extract** — For code: parse with tree-sitter, extract AST skeletons. For documents: convert formats via markitdown, split by heading level into sections.
3. **Pack** — Group entries into token-budgeted batches, create aggregation groups for large components
4. **Leaf Summarize** — Send each batch to the LLM for a focused summary; raw content (AST or original text) is preserved separately (Phase 4)
5. **Aggregate** — Merge summaries upward: fragments → component overview → package overview → INDEX (Phase 5a-5d)
6. **Write** — Persist Markdown files (summary + appended raw content) and update `_meta.json` incrementally

### Incremental Behavior

KBLens is designed for daily use in active development. Just re-run `kblens generate` after code or document changes — it will figure out what needs updating.

On subsequent runs:

- **Unchanged components** are skipped entirely (hash match based on file path + mtime + size)
- **Changed components** are regenerated, and their package's L1 overview is updated
- **New components** are generated and added to the package overview
- **Deleted components** have their `.md` files and metadata cleaned up
- **Failed components** (from previous timeout/errors) are automatically retried
- **Skipped components** (< 50 AST tokens) are recorded in metadata to avoid re-scanning
- **L0 INDEX** is regenerated only if any package changed

## Language Support

### Code Languages

- **C++** (`.h`, `.hpp`, `.cpp`, `.cc`, `.cxx`) — classes, structs, enums, free functions, templates, supplementary `.cpp` extraction
- **C#** (`.cs`) — classes, structs, interfaces, records, enums, delegates, generics with constraints, attributes, XML doc comments
- **Python** (`.py`, `.pyi`) — classes with public methods, module-level functions, type-annotated constants, decorators, docstrings, `__all__`
- **TypeScript** (`.ts`, `.tsx`) — classes, interfaces, type aliases, enums, exported functions, arrow functions, access modifiers
- **JavaScript** (`.js`, `.jsx`, `.mjs`, `.cjs`) — classes, exported functions, constants

### Document Formats

See [Supported Formats](#supported-formats) above.

### Directory Layout

KBLens supports two layout styles:

- **Deep layout** (C++ engine style): `source/package/component/src/*.h` — three directory levels
- **Flat layout** (Python package style): `source/package/*.py` — package directory contains code files directly

Both are auto-detected during scanning. Document sources use the same layout detection.

### Roadmap

Planned languages:

- [x] C++
- [x] C#
- [x] Python
- [x] TypeScript / JavaScript
- [x] Document knowledge base (PDF, DOCX, PPTX, HTML, etc.)
- [ ] Java / Kotlin
- [ ] Rust
- [ ] Go

## AI Assistant Integration

KBLens generates Markdown knowledge bases that can be queried by AI coding assistants. An [OpenCode](https://opencode.ai) skill template is included in `skills/kblens-kb/SKILL.md`.

### OpenCode Setup

```bash
# Auto-install skill
kblens skill install

# Or manually
mkdir -p ~/.config/opencode/skills/kblens-kb
cp skills/kblens-kb/SKILL.md ~/.config/opencode/skills/kblens-kb/
```

The skill automatically reads `KBLENS_KB_PATH` (set after each `kblens generate`) to find the knowledge base.

### Other AI Tools

The knowledge base is plain Markdown files. You can integrate it with any AI tool that supports file-based context:

- Add the knowledge base directory as a reference path
- Use grep/search to find relevant `.md` files
- The three-layer hierarchy (INDEX → package → component) provides natural progressive disclosure

## Notes

- The knowledge base uses **absolute paths** in `_meta.json` for change tracking. If you move your source code directory, regenerate the knowledge base with `kblens generate`.
- **Hybrid output mode**: LLM only generates concise summaries (~400 tokens per batch). Raw content (AST signatures or document text) is appended directly, so it is never truncated or hallucinated.
- LLM model compatibility: KBLens uses [litellm](https://github.com/BerriAI/litellm) under the hood, so any model supported by litellm will work (OpenAI, Anthropic, local Ollama, llama.cpp, etc.).
- For **local LLM** users: set `max_concurrent: 1` and increase `token_budget` (e.g., 20000) to minimize the number of serial LLM calls.

## License

MIT — see [LICENSE](LICENSE).
