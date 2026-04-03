"""Phase 3: Smart packing — group AST entries into LLM-friendly batches."""

from __future__ import annotations

from .models import ASTEntry, AggGroup, Batch, Component, PackResult, PackingConfig


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


def _split_group(
    dirs: list[str],
    dir_stats: dict[str, int],
    budget: int,
) -> list[Batch]:
    """Split a group of directories into batches within budget."""
    batches: list[Batch] = []
    current: list[str] = []
    tokens = 0

    for d in sorted(dirs):
        t = dir_stats.get(d, 0)
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
                subs = _split_group(sub_dirs, dir_stats, budget)
                indices = list(range(len(batches), len(batches) + len(subs)))
                for s in subs:
                    s.group_key = key
                batches.extend(subs)
                if len(subs) > 1:
                    agg_groups.append(AggGroup(parent=key, batch_indices=indices))
    else:
        # Can't sub-group further, just split linearly
        subs = _split_group(dirs, dir_stats, budget)
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
) -> PackResult:
    """Pack AST entries into batches for LLM consumption.

    Returns a PackResult with batches and aggregation groups.
    """
    if packing is None:
        packing = PackingConfig()

    budget = packing.token_budget
    min_tokens = packing.token_min

    # 1. Group tokens by directory
    dir_stats: dict[str, int] = {}
    for entry in ast_map.values():
        d = entry.dir or "."
        dir_stats[d] = dir_stats.get(d, 0) + entry.tokens

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
            subs = _split_group(dirs, dir_stats, budget)
            indices = list(range(len(batches), len(batches) + len(subs)))
            for s in subs:
                s.group_key = key
            batches.extend(subs)
            if len(subs) > 1:
                agg_groups.append(AggGroup(parent=key, batch_indices=indices))
        else:
            _recursive_pack(dirs, dir_stats, budget, batches, agg_groups, key)

    # 5. Merge tiny batches
    _merge_tiny_batches(batches, min_tokens, budget)

    # 6. Rebuild agg_groups from actual batch positions after merging.
    #    _merge_tiny_batches may pop/reorder batches, invalidating the
    #    indices recorded earlier. Rebuild by scanning group_key.
    agg_groups = _rebuild_agg_groups(batches)

    return PackResult(batches=batches, aggregation_groups=agg_groups)
