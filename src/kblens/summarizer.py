"""Phase 4-5: LLM-based summarization.

Phase 4: Generate leaf summaries (one per batch).
Phase 5: Aggregate summaries upward (fragments -> component -> package -> INDEX).
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any

import litellm

from .models import (
    ASTEntry,
    AggGroup,
    Batch,
    BatchSummary,
    Component,
    ComponentResult,
    Config,
    PackResult,
    PackageResult,
)

logger = logging.getLogger("kblens.summarizer")

# ---------------------------------------------------------------------------
# LLM max_tokens per phase (central place to tune output length)
# ---------------------------------------------------------------------------

LEAF_MAX_TOKENS = 1000
FRAGMENT_AGG_MAX_TOKENS = 500
COMPONENT_MAX_TOKENS = 800
PACKAGE_MAX_TOKENS = 1000
INDEX_MAX_TOKENS = 2000

# Text truncation limits for context passed to higher-level prompts
BRIEF_DEFAULT_CHARS = 600
BRIEF_TABLE_CHARS = 200
BRIEF_INDEX_CHARS = 150

# ---------------------------------------------------------------------------
# Prompt templates  (keep concise — every token costs money)
# ---------------------------------------------------------------------------

LEAF_PROMPT = """\
Package: {package_name}
Component: {component_name} ({file_count} files, {total_lines} lines)
Languages: {detected_languages}

Directories: {dir_tree}

```
{ast_content}
```

Summarize this code (in {summary_language}, max 400 words, Markdown).
Use these exact headings:

## Responsibility
(1-2 sentences)

## Key Types and Relationships
(classes, structs, enums and how they relate)
IMPORTANT: For each enum, list ALL its values. For event/data structs, list ALL their fields. These details are critical for searchability.

## Free Functions and System Functions
(list ALL non-member functions with their full signatures — these are critical for searchability)

## Main Public Interfaces
(key class methods and their signatures)

## Source Files
(list the source file paths from the `// --- path ---` markers above, grouped by role)

## Dependencies
(only list #include paths or types from other components visible above)

IMPORTANT: Every function name visible in the AST MUST appear in the summary. Do not summarize multiple functions as a group — list each one individually with its signature.
Be factual. Only describe what is visible in the AST above. Do NOT invent files, classes, or dependencies not shown. If no #include is visible, write "No explicit dependencies visible in AST excerpt.".
When mentioning specific functions or types, include the source file path where they are defined."""

FRAGMENT_AGG_PROMPT = """\
Partial summaries of `{parent}`:

{fragments}

Merge into one summary ({summary_language}, max 250 words, Markdown):
1. Responsibilities  2. Collaboration  3. Key interfaces

Only use information from the fragments above."""

COMPONENT_PROMPT = """\
Component: {component_name} ({file_count} files, {total_lines} lines)

Submodule details:
{submodule_text}

Write a component overview ({summary_language}, max 400 words, Markdown):
1. Purpose (1-2 sentences)
2. Architecture (how submodules relate)
3. Key public API summary
4. Dependencies (only those explicitly mentioned in the submodule details)

Only use information from the submodule details above. Do NOT invent content.
Do NOT speculate about possible interactions — only state relationships that are explicitly documented above."""

PACKAGE_PROMPT = """\
Package: {package_name}

| Component | Purpose | Files |
|---|---|---|
{component_table}

Write a package overview ({summary_language}, max 500 words, Markdown):
1. Package purpose
2. Components grouped by domain
3. Known cross-component dependencies (only from explicit references in the component summaries — do NOT speculate about possible interactions)
4. Navigation guide: "For X, see Y"

Only use information from the table above. Be factual and avoid speculative language like "likely", "probably", "could", or "may"."""

INDEX_PROMPT = """\
| Package | Source | Purpose |
|---|---|---|
{package_table}

Write a knowledge base index ({summary_language}, Markdown):
1. Project overview (2-3 sentences)
2. Package table with descriptions (use `[package_name](packages/package_name.md)` links)
3. High-level architecture
4. How to navigate this knowledge base

Only use information from the table above."""


# ---------------------------------------------------------------------------
# LLM wrapper with retry
# ---------------------------------------------------------------------------

# Retry settings
LLM_MAX_RETRIES = 3
LLM_RETRY_BASE_DELAY = 5.0  # seconds
LLM_RETRY_MAX_DELAY = 30.0  # seconds

# Exception types worth retrying (timeout, rate-limit, server errors)
_RETRYABLE_STRINGS = ("timeout", "rate_limit", "rate limit", "429", "500", "502", "503", "504")


def _is_retryable(exc: Exception) -> bool:
    """Determine if an LLM exception is worth retrying."""
    exc_str = str(exc).lower()
    cls_name = type(exc).__name__.lower()
    if any(k in cls_name for k in ("timeout", "ratelimit", "internalserver", "serviceunavailable")):
        return True
    return any(s in exc_str for s in _RETRYABLE_STRINGS)


async def _llm_call(
    prompt: str,
    config: Config,
    max_tokens: int = LEAF_MAX_TOKENS,
    system: str = "You are a concise code documentation writer. Be factual and brief.",
) -> tuple[str, int, int]:
    """Call LLM via litellm with exponential back-off retry.

    Returns (response_text, input_tokens, output_tokens).
    Retries up to LLM_MAX_RETRIES times on transient errors (timeout, 429, 5xx).
    """
    kwargs: dict[str, Any] = {
        "model": config.llm.model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": max_tokens,
        "temperature": config.llm.temperature,
    }
    if config.llm.api_base:
        kwargs["api_base"] = config.llm.api_base
    if config.llm._resolved_api_key:
        kwargs["api_key"] = config.llm._resolved_api_key

    last_exc: Exception | None = None
    for attempt in range(1, LLM_MAX_RETRIES + 1):
        try:
            response = await litellm.acompletion(**kwargs)
            text = response.choices[0].message.content or ""
            usage = response.usage
            in_tok = usage.prompt_tokens if usage else 0
            out_tok = usage.completion_tokens if usage else 0
            return text, in_tok, out_tok
        except Exception as e:
            last_exc = e
            if attempt < LLM_MAX_RETRIES and _is_retryable(e):
                delay = min(
                    LLM_RETRY_BASE_DELAY * (2 ** (attempt - 1)) + random.uniform(0, 1),
                    LLM_RETRY_MAX_DELAY,
                )
                logger.warning(
                    "LLM call failed (attempt %d/%d), retrying in %.1fs: %s",
                    attempt,
                    LLM_MAX_RETRIES,
                    delay,
                    e,
                )
                await asyncio.sleep(delay)
            else:
                logger.error(
                    "LLM call failed (attempt %d/%d, non-retryable): %s",
                    attempt,
                    LLM_MAX_RETRIES,
                    e,
                )
                raise
    # Should not reach here, but just in case
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("LLM call failed: no attempts were made (LLM_MAX_RETRIES=0?)")


# ---------------------------------------------------------------------------
# Phase 4: Leaf summaries
# ---------------------------------------------------------------------------


def _build_batch_content(
    batch: Batch,
    ast_map: dict[str, ASTEntry],
) -> tuple[str, str]:
    """Build the AST content and dir tree for a batch."""
    batch_dirs = set(batch.dirs)
    dir_tree = ", ".join(sorted(batch_dirs)) if batch_dirs else "(root)"

    ast_lines: list[str] = []
    for rel_path, entry in sorted(ast_map.items()):
        d = entry.dir or "."
        if d in batch_dirs or (d == "" and ("" in batch_dirs or "." in batch_dirs)):
            ast_lines.append(f"// --- {rel_path} ---")
            ast_lines.append(entry.content)
            ast_lines.append("")

    return "\n".join(ast_lines), dir_tree


async def phase4_generate(
    component: Component,
    pack_result: PackResult,
    ast_map: dict[str, ASTEntry],
    config: Config,
) -> list[BatchSummary]:
    """Generate leaf summaries for all batches (Phase 4)."""
    semaphore = asyncio.Semaphore(config.llm.max_concurrent)
    detected_langs = set()
    for entry in ast_map.values():
        if entry.language:
            detected_langs.add(entry.language)
    lang_str = ", ".join(sorted(detected_langs)) or "unknown"

    async def process(batch: Batch) -> BatchSummary:
        async with semaphore:
            ast_content, dir_tree = _build_batch_content(batch, ast_map)
            prompt = LEAF_PROMPT.format(
                package_name=component.package_name,
                component_name=component.name,
                file_count=component.file_count,
                total_lines=component.total_lines,
                detected_languages=lang_str,
                dir_tree=dir_tree,
                ast_content=ast_content,
                summary_language=config.summary_language,
            )
            text, in_tok, out_tok = await _llm_call(
                prompt,
                config,
                max_tokens=LEAF_MAX_TOKENS,
            )
            return BatchSummary(
                batch=batch,
                summary=text,
                input_tokens=in_tok,
                output_tokens=out_tok,
            )

    results = await asyncio.gather(*(process(b) for b in pack_result.batches))
    return list(results)


# ---------------------------------------------------------------------------
# Phase 5a: Fragment aggregation
# ---------------------------------------------------------------------------


async def phase5a_aggregate(
    agg_groups: list[AggGroup],
    summaries: list[BatchSummary],
    config: Config,
) -> dict[str, tuple[str, int, int]]:
    """Merge fragment summaries when a parent was split."""
    results: dict[str, tuple[str, int, int]] = {}

    for group in agg_groups:
        fragments = "\n\n".join(
            f"### Fragment {i + 1}\n{summaries[idx].summary}"
            for i, idx in enumerate(group.batch_indices)
            if idx < len(summaries)
        )
        prompt = FRAGMENT_AGG_PROMPT.format(
            parent=group.parent,
            fragments=fragments,
            summary_language=config.summary_language,
        )
        text, in_tok, out_tok = await _llm_call(
            prompt,
            config,
            max_tokens=FRAGMENT_AGG_MAX_TOKENS,
        )
        results[group.parent] = (text, in_tok, out_tok)

    return results


# ---------------------------------------------------------------------------
# Phase 5b: Component overview
# ---------------------------------------------------------------------------


def _brief(text: str, max_chars: int = BRIEF_DEFAULT_CHARS) -> str:
    """Truncate text to approximately max_chars, cutting at sentence boundary."""
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    cut = text[:max_chars].rfind(". ")
    if cut > max_chars // 2:
        return text[: cut + 1]
    return text[:max_chars] + "..."


# Max total chars for submodule text passed to Phase 5b prompt.
# If sum of all summaries exceeds this, each is truncated proportionally.
PHASE5B_MAX_CONTEXT_CHARS = 12000


async def phase5b_component(
    component: Component,
    submodule_summaries: dict[str, str],
    config: Config,
) -> tuple[str, int, int]:
    """Generate component overview from submodule summaries."""
    # Calculate total length, decide per-submodule budget
    total_len = sum(len(s) for s in submodule_summaries.values())
    n = len(submodule_summaries)
    if total_len > PHASE5B_MAX_CONTEXT_CHARS and n > 0:
        per_sub = PHASE5B_MAX_CONTEXT_CHARS // n
    else:
        per_sub = 0  # 0 = no truncation

    text_parts = []
    for name, summary in sorted(submodule_summaries.items()):
        text = _brief(summary, per_sub) if per_sub > 0 else summary
        text_parts.append(f"### {name}\n{text}")
    submodule_text = "\n\n".join(text_parts)

    prompt = COMPONENT_PROMPT.format(
        component_name=component.name,
        file_count=component.file_count,
        total_lines=component.total_lines,
        submodule_text=submodule_text,
        summary_language=config.summary_language,
    )
    return await _llm_call(prompt, config, max_tokens=COMPONENT_MAX_TOKENS)


# ---------------------------------------------------------------------------
# Phase 5c: Package overview
# ---------------------------------------------------------------------------


async def phase5c_package(
    pkg_name: str,
    component_overviews: dict[str, tuple[str, int]],
    config: Config,
) -> tuple[str, int, int]:
    """Generate package overview from component overviews."""
    rows = []
    for name, (overview, fc) in sorted(component_overviews.items()):
        rows.append(f"| {name} | {_brief(overview, BRIEF_TABLE_CHARS)} | {fc} |")
    table = "\n".join(rows)

    prompt = PACKAGE_PROMPT.format(
        package_name=pkg_name,
        component_table=table,
        summary_language=config.summary_language,
    )
    return await _llm_call(prompt, config, max_tokens=PACKAGE_MAX_TOKENS)


# ---------------------------------------------------------------------------
# Phase 5d: Global INDEX
# ---------------------------------------------------------------------------


async def phase5d_index(
    package_overviews: dict[str, tuple[str, str]],
    config: Config,
) -> tuple[str, int, int]:
    """Generate the global INDEX.md."""
    rows = []
    for name, (overview, source_name) in sorted(package_overviews.items()):
        rows.append(f"| {name} | {source_name} | {_brief(overview, BRIEF_INDEX_CHARS)} |")
    table = "\n".join(rows)

    prompt = INDEX_PROMPT.format(
        package_table=table,
        summary_language=config.summary_language,
    )
    return await _llm_call(prompt, config, max_tokens=INDEX_MAX_TOKENS)
