# AGENTS.md

## Project

KBLens — Python CLI tool that generates hierarchical Markdown knowledge bases from large codebases using tree-sitter AST extraction + LLM summarization. Installed via `pip install -e .`, invoked as `kblens`.

## Layout

```
src/kblens/           # All source code (setuptools src-layout)
  cli.py              # Typer CLI — the only entrypoint (app = typer.Typer)
  config.py           # Two-layer YAML config: global (~/.config/kblens/) + project (./kblens.yaml)
  models.py           # All dataclasses: Config, Component, ASTEntry, Batch, PackResult, etc.
  scanner.py          # Phase 1 — directory walk, component discovery, extension auto-detect
  ast_extract.py      # Phase 2 — tree-sitter parsing (C++, Python, TS/JS), 1200+ lines
  packer.py           # Phase 3 — group AST entries into token-budgeted batches
  summarizer.py       # Phase 4-5 — LLM calls via litellm, prompt templates, aggregation
  writer.py           # Phase 6 — write Markdown + _meta.json, incremental persistence
  progress.py         # JSONL progress log for resume + external monitor
  agent_skills/       # `kblens skill install` — auto-install SKILL.md into coding agents
  resources/skills/   # Bundled SKILL.md template (included in package via package_data)
skills/kblens-kb/     # Repo copy of the OpenCode skill template
tests/                # Empty — no tests exist yet
```

## Dev commands

```bash
pip install -e ".[dev]"          # Install in dev mode with pytest + ruff
ruff check src/                  # Lint (line-length 100, target py311)
ruff format src/                 # Format
pytest                           # Tests (currently empty)
python -m build                  # Build wheel + sdist into dist/
```

## Key architecture facts

- **Six-phase pipeline** runs per source: scan → AST extract → pack → leaf summarize → aggregate (fragments → component → package → INDEX) → write. The orchestrator is `_generate_one_source()` in `cli.py`.
- **All LLM calls** go through `summarizer._llm_call()` which wraps `litellm.acompletion()`. Model is configurable — anything litellm supports works.
- **Concurrency**: components process in parallel (semaphore-limited), each component's batches also run concurrently. Two separate semaphores: `max_concurrent_components` and `max_concurrent`.
- **Incremental**: `_meta.json` tracks per-component hashes (path + mtime + size). Unchanged components skip all phases. Failed components auto-retry on next run.
- **Config resolution order**: env var → `.local.yaml` sibling → base YAML. Both `sources` and `source_dirs` keys accepted for the source list.
- **Version** lives in both `src/kblens/__init__.py` (`__version__`) and `pyproject.toml` (`version`). Update both when bumping.

## Gotchas

- `kblens.local.yaml` and `kblens.yaml` are gitignored — they hold project-specific paths and API keys. Do **not** commit them.
- `dist/` may contain stale build artifacts. Rebuild with `python -m build` if needed. Clean `build/` and `src/kblens.egg-info/` before rebuilding to avoid version caching.
- tree-sitter requires a C compiler at install time (see README Prerequisites).
- `ast_extract.py` skips files > 1 MB (`MAX_FILE_SIZE`) to prevent tree-sitter freezes on giant generated files.
- Components with < 50 AST tokens (`MIN_AST_TOKENS_FOR_LLM` in cli.py) get a static "skipped" entry in metadata, no LLM call.
- litellm startup is noisy — suppressed via `LITELLM_LOG=ERROR` env var set in `cli.py` before any imports.

## Style

- Python 3.11+, dataclasses (no Pydantic), `from __future__ import annotations` everywhere.
- Ruff for lint+format, line length 100.
- Async with `asyncio.run()` for the LLM pipeline; rest is sync.
- Rich for terminal UI (live dashboard, progress bars, tables).
