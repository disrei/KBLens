```
██╗  ██╗██████╗       ██╗     ███████╗███╗   ██╗███████╗
██║ ██╔╝██╔══██╗      ██║     ██╔════╝████╗  ██║██╔════╝
█████╔╝ ██████╔╝      ██║     █████╗  ██╔██╗ ██║███████╗
██╔═██╗ ██╔══██╗      ██║     ██╔══╝  ██║╚██╗██║╚════██║
██║  ██╗██████╔╝      ███████╗███████╗██║ ╚████║███████║
╚═╝  ╚═╝╚═════╝       ╚══════╝╚══════╝╚═╝  ╚═══╝╚══════╝
═══════════════════════════════════════════════════════════
           Knowledge Base Lens · Code Intelligence
═══════════════════════════════════════════════════════════
```

English | [中文](README_zh.md)

A progressive-disclosure code knowledge base generator for large codebases. KBLens uses tree-sitter to extract AST skeletons from C++, C#, Python, TypeScript, and JavaScript source files, packs them into LLM-friendly batches, and generates hierarchical Markdown summaries — giving AI coding assistants structured context about your codebase without reading every file.

## Why KBLens

When doing **vibe coding** — using AI assistants (Cursor, Copilot, OpenCode, etc.) to write and refactor code through natural language — the AI needs to understand your codebase's architecture. But large codebases (100K+ files) are too big for LLMs to consume directly. Without structured context, AI assistants either hallucinate or say "I don't know" when asked about internal systems.

KBLens solves this by generating a **three-layer knowledge base** from your actual source code:

```
L0  INDEX.md            Project overview + package directory
L1  packages/engine.md  Per-package component listing and architecture
L2  packages/engine/    Per-component: purpose, key types, public APIs, dependencies
```

This gives AI assistants a reliable, searchable reference — like an always-up-to-date architecture document generated from actual code. Point your AI tool at the knowledge base, and it can answer questions like "how does the physics system work?" or "what's the public API of SmartDrive?" without reading every source file.

## Key Features

- **AST-based extraction** — Uses tree-sitter to extract class/struct/enum/function signatures from C++, C#, Python, TypeScript, and JavaScript source files. No guessing, no hallucination.
- **Hybrid output** — LLM generates concise summaries (responsibility, types, dependencies), while raw AST signatures are appended directly to output files. Zero truncation, minimal LLM output tokens.
- **Hierarchical summaries** — Three levels of detail (project → package → component) with progressive disclosure. Ask about a package, get the overview. Ask about a class, get the details.
- **Incremental updates** — Only regenerates components whose source files changed. Tracks changes via file hash. A full run on 200+ components takes ~5 minutes; incremental runs take seconds.
- **Change detection** — Five-way classification (unchanged / changed / new / deleted / failed) with automatic cleanup of orphaned files and cascade updates to affected packages.
- **Multi-source projects** — One config file can define multiple source directories. Each source gets its own independent knowledge base with separate INDEX, metadata, and change tracking.
- **Concurrent generation** — Processes 8 components in parallel with 8 concurrent LLM calls. Includes exponential backoff retry (3 attempts) for transient failures.
- **Resume from interruption** — Progress is persisted after each component. Ctrl+C and re-run to continue where you left off.
- **Live dashboard** — Rich terminal UI showing real-time progress, active components, token usage, and error count.
- **Anti-hallucination prompts** — LLM prompts explicitly forbid speculative language and invented content. Dependencies are only listed when `#include` directives are visible in the AST.

## Prerequisites

- **Python 3.11+**
- **C compiler** — Required by tree-sitter for grammar compilation (GCC, Clang, or MSVC)
  - On Ubuntu/Debian: `sudo apt install build-essential`
  - On macOS: Xcode Command Line Tools (`xcode-select --install`)
  - On Windows: Visual Studio Build Tools or MinGW

## Installation

```bash
# From PyPI
pip install kblens

# Upgrade to latest version
pip install --upgrade kblens

# Or install from GitHub directly
pip install git+https://github.com/disrei/KBLens.git

# Or clone and install in development mode
git clone https://github.com/disrei/KBLens.git
cd kblens
pip install -e .

# Verify
kblens version
```

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
project: "my_engine"

output_dir: "~/kblens_kb/my_engine"

sources:
  - path: "/absolute/path/to/packages"
    name: "core"

llm:
  model: "gpt-4o-mini"
  # api_key: "your-api-key"     # see "API Key Security" below
  temperature: 0.2
  max_concurrent: 8
  max_concurrent_components: 8

summary_language: "en"
```

### 2. Preview

```bash
kblens generate --dry-run
```

This scans your source, extracts AST, and reports statistics without calling the LLM.

### 3. Generate

```bash
kblens generate
```

For a project with ~200 components, expect ~5 minutes and ~400K input tokens.

### 4. Use

The generated knowledge base is a directory of Markdown files. You can:

- **Browse directly** — Open `INDEX.md` and navigate through the hierarchy
- **Search with grep** — Find any class, function, or concept across all summaries
- **Integrate with AI tools** — Point your coding assistant's skill/tool at the knowledge base directory (see [AI Assistant Integration](#ai-assistant-integration) below)

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
  - path: "/absolute/path/to/src"     # Absolute path
    name: "core"                      # Short name (used as subdirectory)

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

## CLI Reference

```
kblens generate                    # Generate all sources
kblens generate --source core      # Generate only the "core" source
kblens generate --dry-run          # Preview without LLM calls
kblens generate --config ./my.yaml # Use specific config file
kblens status                      # Show knowledge base status
kblens monitor                     # Monitor a running generation
kblens init                        # Interactive config setup
kblens version                     # Show version
```

## Output Structure

For a project with two sources:

```
~/kblens_kb/my_project/
├── core/                           # Source: core
│   ├── INDEX.md                    # L0: package directory with links
│   ├── _meta.json                  # Component status, hashes, token counts
│   ├── _progress.jsonl             # Generation event log
│   └── core/                       # packages (same name as source)
│       ├── engine.md               # L1: engine package overview
│       ├── engine/
│       │   ├── SoundSystem.md      # L2: component overview
│       │   ├── SoundSystem/        # Leaf batch files (large components)
│       │   │   ├── src_reverb.md
│       │   │   └── src_voice.md
│       │   └── Physics.md
│       ├── gameplay.md
│       └── gameplay/
│           └── ...
└── tools/                          # Source: tools
    ├── INDEX.md
    └── tools/
        └── ...
```

### Markdown Format

Each L2 component file follows a two-section structure:

**Section 1: LLM-generated summary** (concise, architecture-focused)

```markdown
## Responsibility
One-to-two sentence description of what this component does.

## Key Types and Relationships
Classes, structs, enums and how they relate.

## Source Files
List of source file paths grouped by role.

## Dependencies
Explicit #include paths or "No explicit dependencies visible in AST excerpt."
```

**Section 2: Raw AST signatures** (appended after a `---` separator)

```markdown
---

## Complete API Signatures

​```cpp
// --- path/to/file.h ---
class MyClass {
    void MyMethod(int param);
};
​```
```

This hybrid approach keeps LLM output costs low (summaries only) while preserving complete, untruncated API signatures directly from tree-sitter extraction. The raw signatures are never passed to higher-level LLM prompts — they are only written to the final Markdown files.

## How It Works

KBLens runs a six-phase pipeline for each source:

1. **Scan** — Walk the directory tree, discover components (package/subdir pairs), count files and lines
2. **AST Extract** — Parse source files with tree-sitter, extract class/struct/enum/function skeletons and `#include` directives
3. **Pack** — Group AST entries into token-budgeted batches, create aggregation groups for large components
4. **Leaf Summarize** — Send each batch to the LLM for a focused summary; raw AST signatures are preserved separately for direct inclusion in output files (Phase 4)
5. **Aggregate** — Merge summaries upward: fragments → component overview → package overview → INDEX. Higher-level prompts receive only concise summaries (no raw signatures), keeping token costs low (Phase 5a-5d)
6. **Write** — Persist Markdown files (summary + appended AST signatures) and update `_meta.json` incrementally

### Incremental Behavior

KBLens is designed for daily use in active development. Just re-run `kblens generate` after code changes — it will figure out what needs updating.

On subsequent runs:

- **Unchanged components** are skipped entirely (hash match based on file path + mtime + size)
- **Changed components** are regenerated, and their package's L1 overview is updated
- **New components** are generated and added to the package overview
- **Deleted components** have their `.md` files and metadata cleaned up
- **Failed components** (from previous timeout/errors) are automatically retried
- **Skipped components** (< 50 AST tokens) are recorded in metadata to avoid re-scanning
- **L0 INDEX** is regenerated only if any package changed

#### Typical workflow

```bash
# First run: full generation (~5 min for 200 components)
kblens generate

# ... make code changes ...

# Subsequent run: only changed components regenerated (~seconds)
kblens generate

# Check what's in the knowledge base
kblens status
```

#### How change detection works

Each component's identity is a hash of `(relative_path, mtime, size)` for all code files. When you re-run `kblens generate`:

1. **Scan** discovers all current components
2. **Compare** each component's hash against `_meta.json`
3. Components with matching hash → skip. Mismatched or missing → regenerate.
4. Components in `_meta.json` but no longer on disk → delete their `.md` files
5. Only packages containing dirty components get their L1 overview regenerated
6. L0 INDEX regenerated only if any L1 changed

## Language Support

Currently supports:

- **C++** (`.h`, `.hpp`, `.cpp`, `.cc`, `.cxx`) — classes, structs, enums, free functions, templates, supplementary `.cpp` extraction
- **C#** (`.cs`) — classes, structs, interfaces, records, enums, delegates, generics with constraints, attributes, XML doc comments, properties, events; only public/protected/internal members are kept
- **Python** (`.py`, `.pyi`) — classes with public methods, module-level functions, type-annotated constants, decorators, docstrings, `__all__`; private names (`_prefixed`) are automatically skipped
- **TypeScript** (`.ts`, `.tsx`) — classes, interfaces, type aliases, enums, exported functions, arrow functions, access modifiers; `private` members and `_prefixed` names are skipped
- **JavaScript** (`.js`, `.jsx`, `.mjs`, `.cjs`) — classes, exported functions, constants; same extraction logic as TypeScript but without type annotations

The AST extraction, packing, and summarization pipeline is language-agnostic — only the tree-sitter parser and extraction logic is language-specific. Mixed-language projects (e.g., C++ engine with Python tooling) work out of the box.

Components with fewer than 50 AST tokens are excluded from LLM summarization.

### Directory Layout

KBLens supports two layout styles:

- **Deep layout** (C++ engine style): `source/package/component/src/*.h` — three directory levels
- **Flat layout** (Python package style): `source/package/*.py` — package directory contains code files directly

Both are auto-detected during scanning.

### Roadmap

Planned languages:

- [x] C++
- [x] C#
- [x] Python
- [x] TypeScript / JavaScript
- [ ] Java / Kotlin
- [ ] Rust
- [ ] Go

## AI Assistant Integration

KBLens generates Markdown knowledge bases that can be queried by AI coding assistants. An [OpenCode](https://opencode.ai) skill template is included in `skills/kblens-kb/SKILL.md`.

### OpenCode Setup

1. Copy the skill to your OpenCode config directory:

   ```bash
   # Linux / macOS
   mkdir -p ~/.config/opencode/skills/kblens-kb
   cp skills/kblens-kb/SKILL.md ~/.config/opencode/skills/kblens-kb/

   # Windows
   mkdir "%USERPROFILE%\.config\opencode\skills\kblens-kb"
   copy skills\kblens-kb\SKILL.md "%USERPROFILE%\.config\opencode\skills\kblens-kb\"
   ```

2. The skill automatically reads your `~/.config/kblens/config.yaml` to find the knowledge base location.

3. Ask your AI assistant questions about your codebase — it will search the knowledge base for answers.

### Other AI Tools

The knowledge base is plain Markdown files. You can integrate it with any AI tool that supports file-based context:

- Add the knowledge base directory as a reference path
- Use grep/search to find relevant `.md` files
- The three-layer hierarchy (INDEX → package → component) provides natural progressive disclosure

## Notes

- The knowledge base uses **absolute paths** in `_meta.json` for change tracking. If you move your source code directory, regenerate the knowledge base with `kblens generate`.
- **Hybrid output mode**: LLM only generates concise summaries (~400 tokens per batch). Raw AST signatures are appended directly from tree-sitter output, so they are never truncated or hallucinated. Higher-level prompts (package → INDEX) only receive summaries, not raw signatures, keeping aggregation costs low.
- LLM model compatibility: KBLens uses [litellm](https://github.com/BerriAI/litellm) under the hood, so any model supported by litellm will work (OpenAI, Anthropic, local Ollama, etc.).

## License

MIT — see [LICENSE](LICENSE).
