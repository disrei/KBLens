"""Phase 3: Smart packing — group AST entries into LLM-friendly batches."""

from __future__ import annotations

from .models import ASTEntry, AggGroup, Batch, Component, PackResult, PackingConfig


def _entry_effective_tokens(entry: ASTEntry, chars_per_token: float) -> int:
    """Return a conservative token estimate for packing decisions.

    AST-derived token counts are fast but can under-estimate large C++ files.
    Character-based estimates are safer but can over-split.  Use the larger
    of the two so packing stays under context without exploding batch counts.
    """
    char_estimate = max(1, int(len(entry.content) / chars_per_token))
    return max(entry.tokens, char_estimate)


# ---------------------------------------------------------------------------
# Affinity grouping
# ---------------------------------------------------------------------------


def group_by_nearest_parent(dirs: list[str]) -> dict[str, list[str]]:
    """Group directories by their nearest parent that is also in the set.

    Example:
        Input:  [core/session, core/session/routing, core/messages]
        Output: {"core/session": [core/session, core/session/routing],
                 "core/messages": [core/messages]}
    """
    dir_set = set(dirs)
    groups: dict[str, list[str]] = {}

    for d in sorted(dirs):
        parent = d
        assigned = False
        while "/" in parent:
            parent = parent.rsplit("/", 1)[0]
            if parent in dir_set:
                groups.setdefault(parent, [])
                if d not in groups[parent]:
                    groups[parent].append(d)
                assigned = True
                break
        if not assigned:
            groups.setdefault(d, [])
            if d not in groups[d]:
                groups[d].append(d)

    # Ensure parent key is itself in its own list
    for key in list(groups.keys()):
        if key not in groups[key]:
            groups[key].insert(0, key)

    return groups


# ---------------------------------------------------------------------------
# Splitting / merging helpers
# ---------------------------------------------------------------------------


def _split_directory_entries(
    d: str,
    ast_map: dict[str, ASTEntry],
    budget: int,
    chars_per_token: float = 3.0,
) -> list[Batch]:
    """Split a single directory that exceeds *budget* into file-level batches.

    Each batch stores its exact file paths in ``entry_keys`` so the summarizer
    can pick the right entries even though they share the same directory.

    When a single file still exceeds *budget*, its content is split at line
    boundaries and stored as synthetic entries (``rel_path + "^^chunk_N"``)
    in *ast_map* so the summarizer can consume them incrementally.
    """
    entries = [
        (rel, e, _entry_effective_tokens(e, chars_per_token))
        for rel, e in ast_map.items()
        if (e.dir or ".") == d
    ]
    entries.sort(key=lambda item: item[2], reverse=True)

    batches: list[Batch] = []
    current_keys: list[str] = []
    current_tokens = 0

    for rel, e, t in entries:
        if t <= budget:
            if current_tokens + t > budget and current_keys:
                batches.append(
                    Batch(dirs=[d], tokens=current_tokens, entry_keys=list(current_keys))
                )
                current_keys, current_tokens = [], 0
            current_keys.append(rel)
            current_tokens += t
        else:
            # Single file exceeds budget — split at line boundaries
            if current_keys:
                batches.append(
                    Batch(dirs=[d], tokens=current_tokens, entry_keys=list(current_keys))
                )
                current_keys, current_tokens = [], 0
            _split_entry_into_chunks(rel, e, ast_map, budget, batches, d, chars_per_token)

    if current_keys:
        batches.append(
            Batch(dirs=[d], tokens=current_tokens, entry_keys=list(current_keys))
        )

    return batches


def _split_entry_into_chunks(
    rel: str,
    entry: ASTEntry,
    ast_map: dict[str, ASTEntry],
    budget: int,
    batches: list[Batch],
    d: str,
    chars_per_token: float = 3.0,
) -> None:
    """Split a single oversized AST entry into multiple smaller entries.

    Chunks are stored in *ast_map* with keys like ``path^^chunk_0`` so
    ``_build_batch_content`` can resolve them via ``entry_keys``.
    The original entry is removed from *ast_map*.
    """
    content = entry.content
    lines = content.split("\n")
    if len(lines) <= 1:
        # Can't split — create one oversized batch and let pre-flight handle it
        batches.append(
            Batch(dirs=[d], tokens=_entry_effective_tokens(entry, chars_per_token), entry_keys=[rel])
        )
        return

    # Greedy chunking: fill chunks to ~budget tokens
    chunks: list[list[str]] = [[]]
    chunk_chars = 0
    limit = int(budget * chars_per_token)

    for line in lines:
        line_len = len(line) + 1  # +1 for newline
        if chunk_chars + line_len > limit and chunks[-1]:
            chunks.append([])
            chunk_chars = 0
        chunks[-1].append(line)
        chunk_chars += line_len

    for i, chunk_lines in enumerate(chunks):
        chunk_key = f"{rel}^^chunk_{i}"
        chunk_text = "\n".join(chunk_lines)
        chunk_tokens = max(1, int(len(chunk_text) / chars_per_token))
        ast_map[chunk_key] = ASTEntry(
            rel_path=entry.rel_path,
            dir=entry.dir,
            content=chunk_text,
            tokens=chunk_tokens,
            language=entry.language,
        )
        batches.append(
            Batch(dirs=[d], tokens=chunk_tokens, entry_keys=[chunk_key])
        )

    # Remove the original oversized entry
    ast_map.pop(rel, None)


def _split_group(
    dirs: list[str],
    dir_stats: dict[str, int],
    budget: int,
    ast_map: dict[str, ASTEntry] | None = None,
    chars_per_token: float = 3.0,
) -> list[Batch]:
    """Split a group of directories into batches within budget.

    When *ast_map* is provided and a single directory exceeds *budget*, the
    directory is split at the file level using per-batch ``entry_keys``.
    """
    batches: list[Batch] = []
    current: list[str] = []
    tokens = 0

    for d in sorted(dirs):
        t = dir_stats.get(d, 0)
        if t > budget and ast_map:
            if current:
                batches.append(Batch(dirs=list(current), tokens=tokens))
                current, tokens = [], 0
            batches.extend(_split_directory_entries(d, ast_map, budget, chars_per_token))
            continue
        if tokens + t > budget and current:
            batches.append(Batch(dirs=list(current), tokens=tokens))
            current, tokens = [], 0
        current.append(d)
        tokens += t

    if current:
        batches.append(Batch(dirs=list(current), tokens=tokens))

    return batches


def _merge_tiny_batches(batches: list[Batch], min_tokens: int, budget: int) -> None:
    """Merge batches smaller than min_tokens with neighbors (in place)."""
    i = 0
    while i < len(batches):
        if batches[i].tokens < min_tokens and len(batches) > 1:
            # Try merge with previous
            if i > 0 and batches[i - 1].tokens + batches[i].tokens <= budget:
                batches[i - 1].merge(batches.pop(i))
                continue
            # Try merge with next
            if i < len(batches) - 1 and batches[i + 1].tokens + batches[i].tokens <= budget:
                batches[i + 1].merge_front(batches.pop(i))
                continue
        i += 1


def _rebuild_agg_groups(batches: list[Batch]) -> list[AggGroup]:
    """Rebuild aggregation groups from batch group_key after merging.

    A group_key that appears on more than one batch means those batches
    need to be aggregated together in Phase 5a.
    """
    from collections import defaultdict

    key_to_indices: dict[str, list[int]] = defaultdict(list)
    for i, b in enumerate(batches):
        if b.group_key:
            key_to_indices[b.group_key].append(i)
    return [
        AggGroup(parent=key, batch_indices=indices)
        for key, indices in key_to_indices.items()
        if len(indices) > 1
    ]


def _recursive_pack(
    dirs: list[str],
    dir_stats: dict[str, int],
    budget: int,
    batches: list[Batch],
    agg_groups: list[AggGroup],
    parent_key: str,
    ast_map: dict[str, ASTEntry],
    chars_per_token: float = 3.0,
) -> None:
    """Recursively split very large groups."""
    # Try splitting by sub-groups first
    sub_groups = group_by_nearest_parent(dirs)
    if len(sub_groups) > 1:
        for key, sub_dirs in sub_groups.items():
            t = sum(dir_stats.get(d, 0) for d in sub_dirs)
            if t <= budget:
                batches.append(Batch(dirs=sub_dirs, tokens=t, group_key=key))
            else:
                subs = _split_group(sub_dirs, dir_stats, budget, ast_map, chars_per_token)
                indices = list(range(len(batches), len(batches) + len(subs)))
                for s in subs:
                    s.group_key = key
                batches.extend(subs)
                if len(subs) > 1:
                    agg_groups.append(AggGroup(parent=key, batch_indices=indices))
    else:
        # Can't sub-group further, just split linearly
        subs = _split_group(dirs, dir_stats, budget, ast_map, chars_per_token)
        indices = list(range(len(batches), len(batches) + len(subs)))
        for s in subs:
            s.group_key = parent_key
        batches.extend(subs)
        if len(subs) > 1:
            agg_groups.append(AggGroup(parent=parent_key, batch_indices=indices))


# ---------------------------------------------------------------------------
# Phase 3 entry point
# ---------------------------------------------------------------------------


def phase3_pack(
    component: Component,
    ast_map: dict[str, ASTEntry],
    packing: PackingConfig | None = None,
    context_size: int = 16384,
) -> PackResult:
    """Pack AST entries into batches for LLM consumption.

    When *packing.token_budget* is 0, the budget is derived automatically from
    *context_size* by reserving space for system prompt, template overhead,
    output tokens, and a 15 % safety margin.

    Returns a PackResult with batches and aggregation groups.
    """
    if packing is None:
        packing = PackingConfig()

    budget = packing.token_budget
    if budget <= 0:
        # Auto-derive: context_size * 0.85 - system(200) - template(600) - output(300)
        safe_ctx = int(context_size * 0.85)
        budget = max(400, safe_ctx - 200 - 600 - 300)
        # Clamp to token_max if token_max is set
        if packing.token_max > 0:
            budget = min(budget, packing.token_max)

    min_tokens = packing.token_min
    cpt = packing.estimate_chars_per_token

    # 1. Group tokens by directory
    dir_stats: dict[str, int] = {}
    for entry in ast_map.values():
        d = entry.dir or "."
        dir_stats[d] = dir_stats.get(d, 0) + _entry_effective_tokens(entry, cpt)

    if not dir_stats:
        return PackResult()

    # 2. If total fits in one batch, done
    total = sum(dir_stats.values())
    if total <= budget:
        # Use component name as group_key instead of "."
        safe_key = component.name.replace("/", "_").replace("\\", "_")
        return PackResult(
            batches=[Batch(dirs=list(dir_stats.keys()), tokens=total, group_key=safe_key)],
        )

    # 3. Group by affinity
    all_dirs = list(dir_stats.keys())
    groups = group_by_nearest_parent(all_dirs)

    # 4. Pack each group
    batches: list[Batch] = []
    agg_groups: list[AggGroup] = []

    for key, dirs in groups.items():
        t = sum(dir_stats.get(d, 0) for d in dirs)
        if t <= budget:
            batches.append(Batch(dirs=dirs, tokens=t, group_key=key))
        elif t <= packing.token_max:
            subs = _split_group(dirs, dir_stats, budget, ast_map, cpt)
            indices = list(range(len(batches), len(batches) + len(subs)))
            for s in subs:
                s.group_key = key
            batches.extend(subs)
            if len(subs) > 1:
                agg_groups.append(AggGroup(parent=key, batch_indices=indices))
        else:
            _recursive_pack(dirs, dir_stats, budget, batches, agg_groups, key, ast_map, cpt)

    # 5. Merge tiny batches
    _merge_tiny_batches(batches, min_tokens, budget)

    # 6. Rebuild agg_groups from actual batch positions after merging.
    #    _merge_tiny_batches may pop/reorder batches, invalidating the
    #    indices recorded earlier. Rebuild by scanning group_key.
    agg_groups = _rebuild_agg_groups(batches)

    return PackResult(batches=batches, aggregation_groups=agg_groups)
