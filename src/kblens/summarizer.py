"""Phase 4-5: LLM-based summarization.

Phase 4: Generate leaf summaries (one per batch).
Phase 5: Aggregate summaries upward (fragments -> component -> package -> INDEX).
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
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
LEAF_MAX_TOKENS_FLOOR = 120
LEAF_MAX_TOKENS_CEILING = 700
LEAF_OUTPUT_RATIO = 0.10  # LLM only writes summaries, not signatures

FRAGMENT_AGG_MAX_TOKENS = 500
COMPONENT_MAX_TOKENS = 900
PACKAGE_MAX_TOKENS = 900
INDEX_MAX_TOKENS = 1200


def _compute_leaf_max_tokens(batch_input_tokens: int) -> int:
    """Dynamically compute output token budget based on batch input size.

    Since the LLM now only generates summaries (Responsibility, Key Types,
    Dependencies) and raw AST signatures are appended directly by the writer,
    the output budget can be much smaller.
    """
    computed = int(batch_input_tokens * LEAF_OUTPUT_RATIO) + 80
    return max(LEAF_MAX_TOKENS_FLOOR, min(computed, LEAF_MAX_TOKENS_CEILING))


# ---------------------------------------------------------------------------
# Smart token budgeting (character-based heuristic, no tokenizer dependency)
# ---------------------------------------------------------------------------

_SAFETY_MARGIN = 0.15  # reserve 15% of context for tokenizer variance + padding
_SYSTEM_PROMPT_ESTIMATE = 200  # conservative estimate of system prompt tokens
_TEMPLATE_OVERHEAD_ESTIMATE = 600  # conservative estimate of prompt template tokens

# Runtime calibration: each successful LLM call feeds back its actual
# prompt_tokens so KBLens learns the model's true chars/token ratio.
# Starts with the config value then converges via exponential moving average.
_runtime_cpt: float | None = None
_runtime_cpt_alpha = 0.3  # EMA smoothing factor (higher = faster adaptation)


def _get_chars_per_token(config: Config) -> float:
    """Return the current best estimate of chars-per-token for the model.

    Prefers the runtime-calibrated value (learned from actual LLM
    responses) over the static config default once enough data is
    available.
    """
    return _runtime_cpt if _runtime_cpt is not None else config.packing.estimate_chars_per_token


def _calibrate_cpt(prompt_chars: int, actual_tokens: int) -> None:
    """Update the running chars-per-token estimate from a real response."""
    global _runtime_cpt
    if actual_tokens <= 0 or prompt_chars <= 0:
        return
    observed = prompt_chars / actual_tokens
    if _runtime_cpt is None:
        _runtime_cpt = observed
    else:
        _runtime_cpt = (
            _runtime_cpt_alpha * observed + (1.0 - _runtime_cpt_alpha) * _runtime_cpt
        )


def _estimate_tokens(text: str, chars_per_token: float = 3.0) -> int:
    """Rough token estimate: characters / *chars_per_token*.

    Most tokenizers average 3-4 chars per token for English text; C++
    source code tends to be slightly denser.  Using a lower value (e.g.
    3.0) over-estimates, which is the safe direction for pre-flight
    context checks.
    """
    return max(1, int(len(text) / chars_per_token))


def _compute_code_budget(context_size: int, output_max: int) -> int:
    """How many tokens of source code / summary text can we safely pack?

    Returns the budget available for AST content or pre-written summaries
    after deducting system prompt, template overhead, output budget, and
    a 15 % safety margin.
    """
    safe_context = int(context_size * (1.0 - _SAFETY_MARGIN))
    budget = safe_context - _SYSTEM_PROMPT_ESTIMATE - _TEMPLATE_OVERHEAD_ESTIMATE - output_max
    return max(400, budget)  # floor: at least enough for a tiny batch


def _truncate_text_for_context(
    text: str,
    context_size: int,
    output_max: int,
    chars_per_token: float = 3.0,
) -> str:
    """Truncate *text* so that ``tokens(text) + system + output_max`` fits in context.

    Used for aggregate prompts (component, package, INDEX) where the input
    is pre-written summaries that may grow very large.

    Truncation strategy: keep sections (delimited by ``\\n\\n### ``) from the
    back toward the front until the budget is filled, then drop the rest
    with a note.
    """
    budget = _compute_code_budget(context_size, output_max)
    current_tokens = _estimate_tokens(text, chars_per_token)
    if current_tokens <= budget:
        return text

    # Split into top-level sections (package/component boundaries)
    sections = re.split(r"\n(?=###?\s)", text)
    char_budget = int(budget * chars_per_token)
    if len(sections) <= 1:
        # Can't split meaningfully — hard cut
        return text[:char_budget] + "\n\n*[truncated: input too large for context window]*"

    kept: list[str] = []
    kept_tokens = 0
    for sec in reversed(sections):
        t = _estimate_tokens(sec, chars_per_token)
        if kept_tokens + t <= budget:
            kept.insert(0, sec)
            kept_tokens += t
        else:
            break

    if not kept:
        return sections[-1][:char_budget] + "\n\n*[truncated]*"

    result = "\n".join(kept).strip()
    if len(kept) < len(sections):
        result = f"*[{len(sections) - len(kept)} section(s) omitted to fit context window]*\n\n" + result
    return result


def _truncate_prompt_to_context(
    prompt: str,
    system: str,
    context_size: int,
    max_tokens: int,
    chars_per_token: float,
) -> str:
    """Trim a prompt body so the full request fits inside the context window.

    Keeps the *tail* of the prompt, which usually contains the most specific
    AST/source payload, and prepends a truncation note when material was dropped.
    This is a generic fallback used when either pre-flight estimation or the
    provider reports context overflow.
    """
    system_tokens = _estimate_tokens(system, chars_per_token)
    safe_context = int(context_size * (1.0 - _SAFETY_MARGIN))
    allowed_prompt_tokens = safe_context - system_tokens - max_tokens
    if allowed_prompt_tokens <= 0:
        return prompt

    current_prompt_tokens = _estimate_tokens(prompt, chars_per_token)
    if current_prompt_tokens <= allowed_prompt_tokens:
        return prompt

    allowed_chars = int(allowed_prompt_tokens * chars_per_token)
    if allowed_chars <= 0:
        return prompt

    note = "*[truncated to fit context]*\n\n"
    tail_chars = max(0, allowed_chars - len(note))
    if tail_chars <= 0:
        return note[:allowed_chars]
    return note + prompt[-tail_chars:]


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
Prefer omission over guessing. If evidence is insufficient, say so explicitly.

Use these exact headings:

## Responsibility
(1-2 sentences: what this code does and why it exists)

## Key Types and Relationships
(classes, structs, enums and relationships that are explicit in the AST above; if none are explicit, write "Not enough information in AST excerpt.")

## Source Files
(list only the source file paths from the `// --- path ---` markers above; do not infer roles that are not explicit)

## Dependencies
(only list #include paths or types from other components visible above)

RULES:
- Be factual. Only describe what is visible in the AST above. Do NOT invent files, classes, or dependencies.
- Do NOT infer product domains, architecture layers, ownership, or runtime behavior from names alone.
- Do NOT use speculative language such as "likely", "probably", "appears to", "suggests", or "acts as".
- If a section lacks evidence, write "Not enough information in AST excerpt.".
- If no #include is visible, write "No explicit dependencies visible in AST excerpt."
- Do NOT list individual function signatures — they are preserved separately from the raw AST."""

DOC_LEAF_PROMPT = """\
Package: {package_name}
Component: {component_name} ({file_count} files)

Documents: {dir_tree}

```markdown
{ast_content}
```

Summarize this documentation in {summary_language}, using Markdown. Be concise.
The original document content will be appended separately — focus on the summary.
Prefer omission over guessing. If evidence is insufficient, say so explicitly.

Use these exact headings:

## Topic Summary
(2-3 sentences: what this documentation covers and its purpose)

## Key Concepts and Definitions
(important terms, concepts, or entities defined or explained in the text)

## Actionable Information
(steps, commands, configurations, or reference data that a reader would look up)

## Related Topics
(connections to other documents or topics mentioned or implied)

RULES:
- Be factual. Only describe what is visible in the document above.
- Do NOT infer unstated system architecture, business domains, or component responsibilities from titles alone.
- Do NOT use speculative language such as "likely", "probably", "appears to", or "suggests".
- If a section lacks evidence, write "Not enough information in document excerpt.".
- If the document contains images, note their alt text or filenames as visual references.
- Do NOT reproduce the full document text — it is preserved separately."""

DOC_COMPONENT_PROMPT = """\
Component: {component_name} ({file_count} files)

Document section details:
{submodule_text}

Write a component overview ({summary_language}, max 220 words, Markdown) using these exact headings:

## Purpose
(1-2 sentences, only if explicit in the section details)

## Structure
(describe only the document/section organization explicitly shown above)

## Key Topics
(bullet list of topics explicitly named above)

## Cross-References
(only explicit references between documents/sections; otherwise write "No explicit cross-references stated.")

RULES:
- Only use information from the section details above.
- Do NOT invent content or fill gaps from filenames alone.
- Do NOT use speculative language.
- If a section lacks evidence, write "Not enough information in section details."."""

FRAGMENT_AGG_PROMPT = """\
Partial summaries of `{parent}`:

{fragments}

Merge into one summary ({summary_language}, max 160 words, Markdown) using these exact headings:

## Responsibility

## Explicit Relationships

## Key API Names

RULES:
- Only use information repeated or directly stated in the fragments above.
- Do NOT infer new architecture, domains, workflows, or dependencies.
- Do NOT use speculative language.
- If relationships are not explicit, write "No explicit relationships stated in fragments."."""

COMPONENT_PROMPT = """\
Component: {component_name} ({file_count} files, {total_lines} lines)

Submodule details:
{submodule_text}

Write a component overview ({summary_language}, max 220 words, Markdown) using these exact headings:

## Purpose

## Explicit Structure

## Key API Names

## Dependencies

RULES:
- Only use information from the submodule details above.
- Do NOT invent content.
- Do NOT speculate about interactions, domain intent, or hidden architecture.
- Do NOT use speculative language.
- List API names only if they are explicitly named above.
- If structure or dependencies are not explicit, write "Not enough information in submodule details."."""

PACKAGE_PROMPT = """\
Package: {package_name}

{component_sections}

Write a package overview ({summary_language}, max 260 words, Markdown) using these exact headings:

## Package Purpose

## Components
(bullet list: `- <component>: <one factual sentence>`)

## Explicit Cross-Component Dependencies
(only dependencies explicitly stated in component summaries; otherwise write "No explicit cross-component dependencies stated.")

## Navigation Guide
(bullet list: `- For <task>, see <component>` only when supported by the component summaries)

RULES:
- Only use information from the component summaries above.
- Do NOT group by inferred domain.
- Do NOT infer business area, subsystem intent, or architecture from package/component names alone.
- Do NOT introduce examples or entities that are not present in the summaries.
- Do NOT use speculative language."""

INDEX_PROMPT = """\
{package_sections}

Write a knowledge base index ({summary_language}, Markdown) using these exact headings:

## Project Overview
(2-3 short sentences based only on repeated themes across package summaries)

## Packages
(Markdown table: `| Package | Description |` using links of the form `[source/package](source/package.md)`)

## Explicit Shared Patterns
(bullet list of patterns only if multiple package summaries explicitly mention them; otherwise write "No explicit shared patterns stated across package summaries.")

## How to Navigate This Knowledge Base
(bullet list with factual navigation tips derived from package summaries)

RULES:
- Only use information from the package summaries above.
- Do NOT infer a project-wide architecture from package names alone.
- Do NOT invent sample entities, domains, workflows, or dependency directions.
- Do NOT use speculative language."""


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


# litellm provider prefix list (subset of frequently used providers).
# When a model name like "custom-org/deepseek-v4" has a '/' but the prefix
# is NOT in this set, KBLens auto-wraps it as "openai/custom-org/deepseek-v4"
# so litellm routes via the OpenAI adapter while the API receives the full
# original identifier.  Provider names are lowercased for matching.
_KNOWN_LITELLM_PREFIXES: set[str] = {
    "openai",
    "anthropic",
    "azure",
    "azure_ai",
    "cohere",
    "huggingface",
    "ollama",
    "ollama_chat",
    "mistral",
    "groq",
    "deepseek",
    "minimax",
    "openrouter",
    "replicate",
    "bedrock",
    "vertex_ai",
    "together_ai",
    "fireworks_ai",
    "anyscale",
    "voyage",
    "databricks",
    "cloudflare",
    "jina_ai",
    "friendliai",
    "deepinfra",
    "perplexity",
    "clarifai",
    "maritalk",
    "xai",
    "nvidia_nim",
    "sambanova",
    "watsonx",
    "predibase",
    "gemini",
}
"""Known litellm provider prefixes.  The set is maintained manually so we don't
have to parse litellm's internal registry at import time."""


def _normalize_model_for_litellm(model: str, api_base: str | None, provider: str | None = None) -> tuple[str, bool]:
    """Normalize model name for litellm.

    litellm uses ``provider/model`` notation to route requests to the correct
    adapter (e.g. ``openai/gpt-4o``, ``anthropic/claude-3-opus``).

    When a model name already has a ``/`` but the prefix is **not** a known
    litellm provider, the function wraps the whole string with ``openai/``
    so that litellm routes via the OpenAI-compatible adapter while the API
    still receives the original, full identifier (e.g.
    ``openai/my-org/my-model`` → API sees ``my-org/my-model``).

    Returns:
        (normalized_model, was_inferred): The model name to use and whether prefix was auto-added.
    """
    if provider:
        parts = model.split("/", 1)
        if len(parts) == 2 and parts[0] == provider:
            return model, False
        return f"{provider}/{model}", True

    if "/" in model:
        prefix = model.split("/", 1)[0].lower()
        if prefix not in _KNOWN_LITELLM_PREFIXES and api_base:
            return f"openai/{model}", True
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
        config.llm.model, config.llm.api_base, config.llm.provider
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
    if config.llm.extra_body:
        kwargs["extra_body"] = config.llm.extra_body

    # ---- Pre-flight context check ----
    cpt = _get_chars_per_token(config)
    estimated_input = _estimate_tokens(system, cpt) + _estimate_tokens(prompt, cpt)
    total_estimate = estimated_input + max_tokens
    if total_estimate > config.llm.context_size:
        logger.warning(
            "Prompt estimate %d tokens (system=%d + prompt=%d + max_out=%d) exceeds context_size=%d; truncating before request",
            total_estimate,
            _estimate_tokens(system, cpt),
            _estimate_tokens(prompt, cpt),
            max_tokens,
            config.llm.context_size,
        )
        prompt = _truncate_prompt_to_context(
            prompt,
            system,
            config.llm.context_size,
            max_tokens,
            cpt,
        )
        kwargs["messages"][1]["content"] = prompt

    last_exc: Exception | None = None
    _thinking_warned = False
    for attempt in range(1, LLM_MAX_RETRIES + 1):
        try:
            response = await litellm.acompletion(**kwargs)
            text = response.choices[0].message.content or ""
            if not text.strip():
                # Check if the model produced reasoning_content instead of content
                # (thinking models like Qwen3.5, DeepSeek-R1, etc.)
                msg = response.choices[0].message
                reasoning = (
                    getattr(msg, "reasoning_content", None)
                    or (hasattr(msg, "model_extra") and msg.model_extra.get("reasoning_content"))
                )
                if reasoning and not _thinking_warned:
                    _thinking_warned = True
                    logger.warning(
                        "Model returned empty content but had reasoning_content. "
                        "Treating this as an empty final answer rather than using internal reasoning as summary. "
                        "Disable thinking if your provider supports it, or use a non-reasoning model variant."
                    )
                if not text:
                    raise ValueError("LLM returned empty content")
            finish_reason = getattr(response.choices[0], "finish_reason", None)
            if finish_reason == "length":
                logger.warning(
                    "LLM output truncated (hit max_tokens=%d). Output may be incomplete.",
                    max_tokens,
                )
            usage = response.usage
            in_tok = usage.prompt_tokens if usage else 0
            out_tok = usage.completion_tokens if usage else 0
            # Calibrate chars-per-token from this actual response
            prompt_chars = len(system) + len(prompt)
            if in_tok > 0 and prompt_chars > 0:
                _calibrate_cpt(prompt_chars, in_tok)
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
            # Context overflow: truncate prompt and retry once
            is_overflow = (
                "exceeds" in err_str and "context" in err_str
            ) or "prompt too long" in err_str
            if is_overflow and attempt <= 2:
                cpt = _get_chars_per_token(config)
                new_prompt = _truncate_prompt_to_context(
                    prompt,
                    system,
                    config.llm.context_size,
                    max_tokens,
                    cpt,
                )
                if new_prompt != prompt:
                    prompt = new_prompt
                    kwargs["messages"][1]["content"] = prompt
                    logger.warning(
                        "Truncated prompt to %d chars (%d estimated tokens) after context overflow",
                        len(prompt),
                        _estimate_tokens(prompt, cpt),
                    )
                    continue
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
    separator_format: str = "code",
) -> tuple[str, str]:
    """Build the AST content and dir tree for a batch.

    Args:
        separator_format: ``"code"`` uses ``// --- path ---`` separators;
                          ``"doc"`` uses ``### From: path`` separators.
    """
    batch_dirs = set(batch.dirs)
    dir_tree = ", ".join(sorted(batch_dirs)) if batch_dirs else "(root)"

    ast_lines: list[str] = []
    if batch.entry_keys:
        # File-level split: include only the exact entry keys listed.
        for rel_path in batch.entry_keys:
            entry = ast_map.get(rel_path)
            if entry is None:
                continue
            if separator_format == "doc":
                ast_lines.append(f"### From: {rel_path}")
            else:
                ast_lines.append(f"// --- {rel_path} ---")
            ast_lines.append(entry.content)
            ast_lines.append("")
    else:
        for rel_path, entry in sorted(ast_map.items()):
            d = entry.dir or "."
            if d in batch_dirs or (d == "" and ("" in batch_dirs or "." in batch_dirs)):
                if separator_format == "doc":
                    ast_lines.append(f"### From: {rel_path}")
                else:
                    ast_lines.append(f"// --- {rel_path} ---")
                ast_lines.append(entry.content)
                ast_lines.append("")

    return "\n".join(ast_lines), dir_tree


def extract_marked_source_paths(ast_content: str, separator_format: str = "code") -> list[str]:
    """Extract source paths from batch content separators."""
    paths: list[str] = []
    for raw_line in ast_content.splitlines():
        line = raw_line.strip()
        if separator_format == "doc":
            prefix = "### From: "
            if line.startswith(prefix):
                paths.append(line[len(prefix) :].strip())
        else:
            if line.startswith("// --- ") and line.endswith(" ---"):
                paths.append(line[7:-4].strip())
    # Keep order stable while deduplicating.
    return list(dict.fromkeys(p for p in paths if p))


def summary_has_required_headings(text: str, is_doc: bool = False) -> bool:
    """Check whether a summary includes all required top-level headings."""
    required = (
        [
            "## Topic Summary",
            "## Key Concepts and Definitions",
            "## Actionable Information",
            "## Related Topics",
        ]
        if is_doc
        else [
            "## Responsibility",
            "## Key Types and Relationships",
            "## Source Files",
            "## Dependencies",
        ]
    )
    return all(h in text for h in required)


def summary_looks_truncated(text: str, is_doc: bool = False) -> bool:
    """Heuristic detection for visibly incomplete model output."""
    stripped = text.rstrip()
    if not stripped:
        return True
    if not summary_has_required_headings(stripped, is_doc=is_doc):
        return True
    last_line = stripped.splitlines()[-1].rstrip()
    if not last_line:
        return False
    if last_line.endswith(("`", "(", "[", ":", "-")):
        return True
    if stripped.count("`") % 2 == 1:
        return True
    return False


def build_leaf_fallback_summary(ast_content: str, is_doc: bool = False) -> str:
    """Create a deterministic fallback summary when model output is incomplete."""
    paths = extract_marked_source_paths(ast_content, "doc" if is_doc else "code")
    if is_doc:
        path_lines = "\n".join(f"- {p}" for p in paths[:12]) or "- No extracted document sections listed."
        return (
            "## Topic Summary\n"
            "Not enough information in document excerpt to provide a reliable prose summary.\n\n"
            "## Key Concepts and Definitions\n"
            "Not enough information in document excerpt.\n\n"
            "## Actionable Information\n"
            f"{path_lines}\n\n"
            "## Related Topics\n"
            "Not enough information in document excerpt."
        )

    path_lines = "\n".join(f"- {p}" for p in paths[:12]) or "- No extractable source files listed."
    return (
        "## Responsibility\n"
        "Not enough information in AST excerpt to provide a reliable prose summary.\n\n"
        "## Key Types and Relationships\n"
        "Not enough information in AST excerpt.\n\n"
        "## Source Files\n"
        f"{path_lines}\n\n"
        "## Dependencies\n"
        "No explicit dependencies visible in AST excerpt."
    )


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
    submodule_text = _truncate_text_for_context(
        submodule_text, config.llm.context_size, COMPONENT_MAX_TOKENS,
        _get_chars_per_token(config),
    )

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

    # Keep package summaries short for smaller local models.
    n_components = len(component_overviews)
    max_out = max(PACKAGE_MAX_TOKENS, min(n_components * 18 + 180, 1600))
    component_sections = _truncate_text_for_context(
        component_sections, config.llm.context_size, max_out,
        _get_chars_per_token(config),
    )

    prompt = PACKAGE_PROMPT.format(
        package_name=pkg_name,
        component_sections=component_sections,
        summary_language=config.summary_language,
    )
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
    package_sections = _truncate_text_for_context(
        package_sections, config.llm.context_size, INDEX_MAX_TOKENS,
        _get_chars_per_token(config),
    )

    prompt = INDEX_PROMPT.format(
        package_sections=package_sections,
        summary_language=config.summary_language,
    )
    return await _llm_call(prompt, config, max_tokens=INDEX_MAX_TOKENS)


def normalize_index_links(index_md: str, package_overviews: dict[str, tuple[str, str]]) -> str:
    """Rewrite package links in INDEX.md to match actual on-disk layout.

    The writer stores package overviews at ``<output_dir>/<source>/<package>.md``.
    Models may still emit stale or inferred link targets. This function preserves
    link labels while normalizing targets deterministically from package keys.

    Tries the full ``source/package`` key first, then falls back to matching
    just the package name as label, so both ``[community/data]`` and ``[data]``
    are covered without hard-coding any particular naming convention.
    """

    text = index_md
    for pkg_key in sorted(package_overviews, key=len, reverse=True):
        parts = pkg_key.split("/", 1)
        if len(parts) != 2:
            continue
        source_name, pkg_name = parts
        target = f"{source_name}/{pkg_name}.md"
        replacement = f"[{pkg_key}]({target})"
        pattern = rf"\[{re.escape(pkg_key)}\]\([^)]*\)"
        text = re.sub(pattern, replacement, text)

    for pkg_key in sorted(package_overviews, key=len, reverse=True):
        parts = pkg_key.split("/", 1)
        if len(parts) != 2:
            continue
        source_name, pkg_name = parts
        target = f"{source_name}/{pkg_name}.md"
        pattern = rf"\[{re.escape(pkg_name)}\]\([^)]*\)"
        replacement = f"[{pkg_name}]({target})"
        text = re.sub(pattern, replacement, text)
    return text
