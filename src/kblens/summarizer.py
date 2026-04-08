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
    Config,
)

logger = logging.getLogger("kblens.summarizer")

# Track which model+api_base combinations have already been warned about prefix inference.
# This prevents logging the same warning multiple times during a single run.
_prefix_inference_warned: set[tuple[str, str | None]] = set()

# ---------------------------------------------------------------------------
# LLM max_tokens per phase (central place to tune output length)
# ---------------------------------------------------------------------------

# Leaf: dynamically computed — see _compute_leaf_max_tokens()
# Since LLM now only writes summaries (not signatures), output is much smaller.
LEAF_MAX_TOKENS_FLOOR = 400
LEAF_MAX_TOKENS_CEILING = 1500
LEAF_OUTPUT_RATIO = 0.15  # LLM only writes summaries, not signatures

FRAGMENT_AGG_MAX_TOKENS = 800
COMPONENT_MAX_TOKENS = 1500
PACKAGE_MAX_TOKENS = 1500
INDEX_MAX_TOKENS = 2000


def _compute_leaf_max_tokens(batch_input_tokens: int) -> int:
    """Dynamically compute output token budget based on batch input size.

    Since the LLM now only generates summaries (Responsibility, Key Types,
    Dependencies) and raw AST signatures are appended directly by the writer,
    the output budget can be much smaller.
    """
    computed = int(batch_input_tokens * LEAF_OUTPUT_RATIO) + 200
    return max(LEAF_MAX_TOKENS_FLOOR, min(computed, LEAF_MAX_TOKENS_CEILING))


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

Summarize this code in {summary_language}, using Markdown. Be concise.
The raw API signatures will be appended separately — do NOT list individual function signatures.

Use these exact headings:

## Responsibility
(1-2 sentences: what this code does and why it exists)

## Key Types and Relationships
(classes, structs, enums and how they relate to each other — focus on inheritance, composition, and collaboration patterns)

## Source Files
(list the source file paths from the `// --- path ---` markers above, grouped by role)

## Dependencies
(only list #include paths or types from other components visible above)

RULES:
- Be factual. Only describe what is visible in the AST above. Do NOT invent files, classes, or dependencies.
- If no #include is visible, write "No explicit dependencies visible in AST excerpt."
- Do NOT list individual function signatures — they are preserved separately from the raw AST."""

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
3. Key public API summary (list important class/interface NAMES only — full signatures are preserved in leaf files)
4. Dependencies (only those explicitly mentioned in the submodule details)

Only use information from the submodule details above. Do NOT invent content.
Do NOT speculate about possible interactions — only state relationships that are explicitly documented above."""

PACKAGE_PROMPT = """\
Package: {package_name}

{component_sections}

Write a package overview ({summary_language}, max 500 words, Markdown):
1. Package purpose
2. Components grouped by domain
3. Known cross-component dependencies (only from explicit references in the component summaries — do NOT speculate about possible interactions)
4. Navigation guide: "For X, see Y"

Only use information from the component summaries above. Be factual and avoid speculative language like "likely", "probably", "could", or "may"."""

INDEX_PROMPT = """\
{package_sections}

Write a knowledge base index ({summary_language}, Markdown):
1. Project overview (2-3 sentences)
2. Package table with descriptions (use `[package_name](packages/package_name.md)` links)
3. High-level architecture
4. How to navigate this knowledge base

Only use information from the package summaries above."""


# ---------------------------------------------------------------------------
# LLM wrapper with retry
# ---------------------------------------------------------------------------

# Retry settings
LLM_MAX_RETRIES = 3
LLM_RETRY_BASE_DELAY = 5.0  # seconds
LLM_RETRY_MAX_DELAY = 30.0  # seconds

# Exception types worth retrying (timeout, rate-limit, server errors)
_RETRYABLE_STRINGS = (
    "timeout",
    "rate_limit",
    "rate limit",
    "429",
    "500",
    "502",
    "503",
    "504",
    "empty content",
)


def _is_retryable(exc: Exception) -> bool:
    """Determine if an LLM exception is worth retrying."""
    exc_str = str(exc).lower()
    cls_name = type(exc).__name__.lower()
    if any(k in cls_name for k in ("timeout", "ratelimit", "internalserver", "serviceunavailable")):
        return True
    return any(s in exc_str for s in _RETRYABLE_STRINGS)


def _normalize_model_for_litellm(model: str, api_base: str | None) -> tuple[str, bool]:
    """Normalize model name for litellm.

    litellm requires provider prefixes (e.g., 'openai/', 'anthropic/', 'minimax/').
    Many users only have the raw model ID from their provider's dashboard.

    This function auto-adds 'openai/' prefix when:
    - model has no '/' (no provider prefix)
    - api_base is set (suggests OpenAI-compatible endpoint)

    Returns:
        (normalized_model, was_inferred): The model name to use and whether prefix was auto-added.
    """
    if "/" in model:
        return model, False

    if not api_base:
        return model, False

    return f"openai/{model}", True


async def _llm_call(
    prompt: str,
    config: Config,
    max_tokens: int = 1000,
    system: str = "You are a concise code documentation writer. Be factual and brief.",
) -> tuple[str, int, int]:
    """Call LLM via litellm with exponential back-off retry.

    Returns (response_text, input_tokens, output_tokens).
    Retries up to LLM_MAX_RETRIES times on transient errors (timeout, 429, 5xx).
    """
    normalized_model, was_inferred = _normalize_model_for_litellm(
        config.llm.model, config.llm.api_base
    )

    if was_inferred:
        config_key = (config.llm.model, config.llm.api_base)
        if config_key not in _prefix_inference_warned:
            _prefix_inference_warned.add(config_key)
            logger.warning(
                "Model '%s' has no provider prefix. Assuming OpenAI-compatible API at %s. "
                "If this is wrong, set model = 'provider/model' explicitly "
                "(e.g., 'minimax/MiniMax-M2.1' or 'openai/gpt-4o-mini').",
                config.llm.model,
                config.llm.api_base,
            )

    kwargs: dict[str, Any] = {
        "model": normalized_model,
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
            if not text.strip():
                raise ValueError(
                    "LLM returned empty content (model may have refused or hit a content filter)"
                )
            finish_reason = getattr(response.choices[0], "finish_reason", None)
            if finish_reason == "length":
                logger.warning(
                    "LLM output truncated (hit max_tokens=%d). Output may be incomplete.",
                    max_tokens,
                )
            usage = response.usage
            in_tok = usage.prompt_tokens if usage else 0
            out_tok = usage.completion_tokens if usage else 0
            return text, in_tok, out_tok
        except Exception as e:
            last_exc = e
            err_str = str(e).lower()
            if was_inferred and ("provider" in err_str or "not provided" in err_str):
                logger.error(
                    "Model '%s' was auto-prefixed as 'openai/%s'. This may be incorrect. "
                    "Try setting model explicitly: 'openai/<model>' for OpenAI-compatible endpoints, "
                    "or '<provider>/<model>' for native LiteLLM providers "
                    "(e.g., 'minimax/MiniMax-M2.1', 'anthropic/claude-3-opus', 'deepseek/deepseek-chat').",
                    config.llm.model,
                    config.llm.model,
                )
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
            max_out = _compute_leaf_max_tokens(batch.tokens)
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
                max_tokens=max_out,
            )
            return BatchSummary(
                batch=batch,
                summary=text,
                ast_content=ast_content,
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


async def phase5b_component(
    component: Component,
    submodule_summaries: dict[str, str],
    config: Config,
) -> tuple[str, int, int]:
    """Generate component overview from submodule summaries."""
    text_parts = []
    for name, summary in sorted(submodule_summaries.items()):
        text_parts.append(f"### {name}\n{summary}")
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
    sections = []
    for name, (overview, fc) in sorted(component_overviews.items()):
        sections.append(f"### {name} ({fc} files)\n{overview}")
    component_sections = "\n\n".join(sections)

    prompt = PACKAGE_PROMPT.format(
        package_name=pkg_name,
        component_sections=component_sections,
        summary_language=config.summary_language,
    )
    # Scale output budget with number of components:
    # ~30 tokens per component row + 300 tokens fixed overhead
    n_components = len(component_overviews)
    max_out = max(PACKAGE_MAX_TOKENS, min(n_components * 30 + 300, 3000))
    return await _llm_call(prompt, config, max_tokens=max_out)


# ---------------------------------------------------------------------------
# Phase 5d: Global INDEX
# ---------------------------------------------------------------------------


async def phase5d_index(
    package_overviews: dict[str, tuple[str, str]],
    config: Config,
) -> tuple[str, int, int]:
    """Generate the global INDEX.md."""
    sections = []
    for name, (overview, source_name) in sorted(package_overviews.items()):
        sections.append(f"### {name} (source: {source_name})\n{overview}")
    package_sections = "\n\n".join(sections)

    prompt = INDEX_PROMPT.format(
        package_sections=package_sections,
        summary_language=config.summary_language,
    )
    return await _llm_call(prompt, config, max_tokens=INDEX_MAX_TOKENS)
