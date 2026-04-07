"""KBLens CLI — progressive-disclosure code knowledge base generator."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import traceback
from collections import defaultdict
from pathlib import Path

# Suppress litellm's noisy startup logs (SSL timeout warnings, model cost map fetches)
# before importing anything that triggers litellm initialization.
os.environ.setdefault("LITELLM_LOG", "ERROR")
logging.getLogger("LiteLLM").setLevel(logging.ERROR)
logging.getLogger("litellm").setLevel(logging.ERROR)

import typer
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.table import Table
from rich.text import Text

from . import __version__
from .agent_skills import (
    AGENT_SKILL_TARGETS,
    detect_targets,
    get_target,
    install_targets,
    skill_status_rows,
    uninstall_targets,
)
from .ast_extract import phase2_extract_ast
from .config import (
    CONFIG_TEMPLATE,
    ConfigError,
    USER_CONFIG_DIR,
    USER_CONFIG_FILE,
    find_config_file,
    load_config,
    require_api_key,
)
from .models import (
    ComponentResult,
    Config,
    PackageResult,
)
from .packer import phase3_pack
from .progress import ProgressLog
from .scanner import phase1_scan, resolve_include_extensions
from .summarizer import (
    LEAF_PROMPT,
    _build_batch_content,
    _compute_leaf_max_tokens,
    _llm_call,
    phase5a_aggregate,
    phase5b_component,
    phase5c_package,
    phase5d_index,
)
from .writer import (
    build_component_meta,
    build_meta,
    cleanup_deleted_components,
    compute_source_hash,
    is_component_done,
    load_meta,
    save_meta,
    save_meta_component,
    save_meta_failed,
    strip_ast_section,
    write_component_incremental,
    write_knowledge_base,
)

app = typer.Typer(
    name="kblens",
    help="Progressive-disclosure code knowledge base generator.",
    no_args_is_help=True,
)
console = Console()

skill_app = typer.Typer(
    name="skill",
    help="Install the bundled KBLens agent skill into supported coding agents.",
    no_args_is_help=True,
)
app.add_typer(skill_app, name="skill")

# Components with fewer AST tokens than this skip LLM and get a static summary.
MIN_AST_TOKENS_FOR_LLM = 50


def _render_skill_install_results(results: dict[str, str]) -> None:
    for target in AGENT_SKILL_TARGETS:
        if target.key not in results:
            continue
        status = results[target.key]
        if status == "installed":
            console.print(f"[green]Installed[/green] {target.display_name}")
        elif status == "exists":
            console.print(
                f"[yellow]Already installed[/yellow] {target.display_name} "
                f"(use --force to overwrite)"
            )
        elif status == "removed":
            console.print(f"[green]Removed[/green] {target.display_name}")
        elif status == "missing":
            console.print(f"[yellow]Not installed[/yellow] {target.display_name}")
        elif status == "manual":
            console.print(f"[yellow]Manual setup required[/yellow] {target.display_name}")
            console.print(f"  {target.manual_help}")


def _print_skill_setup_guidance() -> None:
    console.print()
    console.print("Skill setup:")
    detected = detect_targets()
    if not detected:
        console.print("  No supported coding agent detected.")
        console.print("  Install the KBLens skill manually into one of these locations:")
        for target in AGENT_SKILL_TARGETS:
            console.print(f"    - {target.display_name}: {target.manual_help}")
        return

    auto_targets = [target for target in detected if target.supports_auto_install]
    manual_targets = [target for target in detected if not target.supports_auto_install]

    console.print("  Detected agents: " + ", ".join(target.display_name for target in detected))
    if auto_targets:
        console.print("  Run [cyan]kblens skill install[/cyan] to install the bundled skill.")
    if manual_targets:
        for target in manual_targets:
            console.print(f"  {target.display_name}: {target.manual_help}")


@skill_app.command("install")
def skill_install(
    tool: list[str] = typer.Option(
        None,
        "--tool",
        "-t",
        help="Install for a specific tool key. Repeat to install multiple times.",
    ),
    force: bool = typer.Option(False, "--force", "-f", help="Overwrite an existing install."),
) -> None:
    """Install the bundled KBLens skill into supported coding agents."""
    selected = tool or []
    targets = []

    if selected:
        for key in selected:
            target = get_target(key)
            if target is None:
                valid = ", ".join(target.key for target in AGENT_SKILL_TARGETS)
                console.print(f"[red]Unknown tool:[/red] {key}")
                console.print(f"Valid tool keys: {valid}")
                raise typer.Exit(1)
            targets.append(target)
    else:
        targets = detect_targets()
        if not targets:
            console.print("[yellow]No supported coding agent detected.[/yellow]")
            _print_skill_setup_guidance()
            raise typer.Exit(1)

    results = install_targets(targets, force=force)
    _render_skill_install_results(results)


@skill_app.command("status")
def skill_status() -> None:
    """Show installation status for the bundled KBLens skill."""
    detected_keys = {target.key for target in detect_targets()}
    table = Table(title="KBLens Skill Status")
    table.add_column("Tool", style="cyan")
    table.add_column("Detected")
    table.add_column("Installed")
    table.add_column("Auto install")
    table.add_column("Path / Notes")

    for row in skill_status_rows():
        detected = "yes" if row["key"] in detected_keys else "no"
        path_or_help = row["path"] if row["auto"] == "yes" else row["manual_help"]
        table.add_row(row["name"], detected, row["installed"], row["auto"], path_or_help)

    console.print(table)


@skill_app.command("uninstall")
def skill_uninstall(
    tool: list[str] = typer.Option(
        None,
        "--tool",
        "-t",
        help="Uninstall from a specific tool key. Repeat to uninstall multiple times.",
    ),
) -> None:
    """Remove the bundled KBLens skill from supported coding agents."""
    selected = tool or []
    targets = []

    if selected:
        for key in selected:
            target = get_target(key)
            if target is None:
                valid = ", ".join(target.key for target in AGENT_SKILL_TARGETS)
                console.print(f"[red]Unknown tool:[/red] {key}")
                console.print(f"Valid tool keys: {valid}")
                raise typer.Exit(1)
            targets.append(target)
    else:
        targets = [target for target in AGENT_SKILL_TARGETS if target.supports_auto_install]

    results = uninstall_targets(targets)
    _render_skill_install_results(results)


# ---------------------------------------------------------------------------
# Live dashboard
# ---------------------------------------------------------------------------


class _DashboardState:
    """Mutable state rendered by the Rich Live panel."""

    def __init__(self) -> None:
        self.phase: str = ""
        self.total_components: int = 0
        self.done_components: int = 0
        self.skipped_components: int = 0
        self.changed_components: int = 0
        self.new_components: int = 0
        self.deleted_components: int = 0
        self.failed_components: int = 0
        self.active_components: list[str] = []
        self.total_batches: int = 0
        self.llm_calls: int = 0
        self.total_in_tokens: int = 0
        self.total_out_tokens: int = 0
        self.errors: int = 0
        self.events: list[str] = []
        self.packages_done: int = 0
        self.total_packages: int = 0
        self.dirty_packages: int = 0
        self.finished: bool = False
        self._lock = asyncio.Lock()

    def add_event(self, text: str) -> None:
        self.events.append(text)
        if len(self.events) > 30:
            self.events = self.events[-30:]


def _build_dashboard(ds: _DashboardState, progress: Progress) -> Group:
    tbl = Table.grid(padding=(0, 2))
    tbl.add_column(style="bold cyan", width=14)
    tbl.add_column()

    if ds.total_components > 0:
        pct = ds.done_components * 100 // ds.total_components
        comp_str = f"{ds.done_components} / {ds.total_components} ({pct}%)"
        detail_parts: list[str] = []
        if ds.skipped_components:
            detail_parts.append(f"{ds.skipped_components} unchanged")
        if ds.changed_components:
            detail_parts.append(f"{ds.changed_components} changed")
        if ds.new_components:
            detail_parts.append(f"{ds.new_components} new")
        if ds.deleted_components:
            detail_parts.append(f"{ds.deleted_components} deleted")
        if ds.failed_components:
            detail_parts.append(f"{ds.failed_components} retrying")
        if detail_parts:
            comp_str += f"  [dim]({', '.join(detail_parts)})[/dim]"
    else:
        comp_str = "---"

    tbl.add_row("Phase", ds.phase)
    tbl.add_row("Components", comp_str)
    tbl.add_row("LLM calls", str(ds.llm_calls))
    tbl.add_row("Tokens", f"{ds.total_in_tokens:,} in  /  {ds.total_out_tokens:,} out")
    if ds.dirty_packages > 0 or ds.total_packages > 0:
        tbl.add_row("Packages", f"{ds.dirty_packages} dirty / {ds.total_packages} total")
    if ds.active_components:
        active = ", ".join(c.split("/")[-1] for c in ds.active_components[:4])
        if len(ds.active_components) > 4:
            active += f"  (+{len(ds.active_components) - 4})"
        tbl.add_row("Active", active)
    if ds.errors:
        tbl.add_row("Errors", f"[red]{ds.errors}[/red]")

    log_lines = "\n".join(ds.events[-12:])
    log_text = Text(log_lines, style="dim")

    return Group(
        Panel(tbl, title="[bold]KBLens Generate[/bold]", border_style="cyan"),
        progress,
        Panel(log_text, title="[dim]Event Log[/dim]", border_style="dim", height=16),
    )


def _refresh_live(live_ref: list, ds: _DashboardState, progress: Progress) -> None:
    if live_ref:
        live_ref[0].update(_build_dashboard(ds, progress))


# ---------------------------------------------------------------------------
# Change detection helpers
# ---------------------------------------------------------------------------


def _classify_components(
    components: list,
    existing_meta: dict,
    include_exts: set[str] | None = None,
    exclude_patterns: list[str] | None = None,
) -> tuple[set[str], set[str], set[str], set[str], set[str]]:
    """Classify components into: unchanged, changed, new, deleted, failed.

    Returns (unchanged_keys, changed_keys, new_keys, deleted_keys, failed_keys).
    """
    current_keys = {comp.key for comp in components}
    meta_keys = set(existing_meta.get("components", {}).keys())

    unchanged_keys: set[str] = set()
    changed_keys: set[str] = set()
    new_keys: set[str] = set()
    failed_keys: set[str] = set()

    for comp in components:
        meta_entry = existing_meta.get("components", {}).get(comp.key)
        if meta_entry is None:
            new_keys.add(comp.key)
        elif meta_entry.get("status") in ("failed", "partial"):
            failed_keys.add(comp.key)
        elif is_component_done(existing_meta, comp.key, comp.path, include_exts, exclude_patterns):
            unchanged_keys.add(comp.key)
        else:
            changed_keys.add(comp.key)

    deleted_keys = meta_keys - current_keys

    return unchanged_keys, changed_keys, new_keys, deleted_keys, failed_keys


def _compute_dirty_packages(
    components: list,
    changed_keys: set[str],
    new_keys: set[str],
    deleted_keys: set[str],
    failed_keys: set[str],
) -> set[str]:
    """Determine which packages need their L1 overview regenerated."""
    dirty: set[str] = set()

    # Changed, new, or retried components dirty their package
    for comp in components:
        if comp.key in changed_keys or comp.key in new_keys or comp.key in failed_keys:
            dirty.add(f"{comp.source_name}/{comp.package_name}")

    # Deleted components also dirty their former package
    for dk in deleted_keys:
        parts = dk.split("/", 2)
        if len(parts) >= 2:
            dirty.add(f"{parts[0]}/{parts[1]}")

    return dirty


# ---------------------------------------------------------------------------
# kblens generate
# ---------------------------------------------------------------------------


@app.command()
def generate(
    config_path: str = typer.Option(
        None, "--config", "-c", help="Config file path (default: auto-detect)"
    ),
    source: str = typer.Option(
        None, "--source", "-s", help="Only generate for this source name (default: all)"
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Only scan and report statistics"),
) -> None:
    """Generate the full knowledge base (supports resume from checkpoint)."""
    try:
        config = load_config(config_path)
    except Exception as e:
        console.print(f"[red]Error loading config:[/red] {e}")
        raise typer.Exit(1)

    if not dry_run:
        try:
            require_api_key(config)
        except ConfigError as e:
            console.print(f"[red]{e}[/red]")
            raise typer.Exit(1)

    if not config.source_dirs:
        console.print("[red]No source directories configured. Check 'sources' in config.[/red]")
        raise typer.Exit(1)

    # Filter sources if --source is specified
    sources_to_run = config.source_dirs
    if source:
        sources_to_run = [s for s in config.source_dirs if s.name == source]
        if not sources_to_run:
            available = ", ".join(s.name for s in config.source_dirs)
            console.print(f"[red]Source '{source}' not found. Available: {available}[/red]")
            raise typer.Exit(1)

    console.print(f"[bold]KBLens v{__version__}[/bold]")
    if config.project:
        console.print(f"Project:        {config.project}")
    console.print(f"Sources:        {', '.join(s.name for s in sources_to_run)}")
    console.print(f"LLM model:      {config.llm.model}")
    console.print(
        f"Concurrency:    {config.llm.max_concurrent_components} components, "
        f"{config.llm.max_concurrent} LLM calls"
    )
    console.print()

    # Run pipeline for each source independently
    for src_idx, src_dir in enumerate(sources_to_run):
        if len(sources_to_run) > 1:
            console.print(
                f"[bold magenta]=== Source {src_idx + 1}/{len(sources_to_run)}: "
                f"{src_dir.name} ===[/bold magenta]"
            )
            console.print()

        # Build a single-source config for this iteration
        src_config = Config(
            version=config.version,
            project=config.project,
            output_dir=str(Path(config.output_dir) / src_dir.name),
            source_dirs=[src_dir],
            include_extensions=config.include_extensions,
            exclude_patterns=config.exclude_patterns,
            llm=config.llm,
            packing=config.packing,
            summary_language=config.summary_language,
        )

        _generate_one_source(src_config, dry_run)

        if len(sources_to_run) > 1:
            console.print()


def _generate_one_source(config: Config, dry_run: bool) -> None:
    """Run the full generation pipeline for a single source."""
    plog = ProgressLog(config.output_dir)
    include_exts = resolve_include_extensions(config)

    src_name = config.source_dirs[0].name if config.source_dirs else "unknown"
    console.print(f"[bold cyan]Source:[/bold cyan] {src_name}")
    console.print(f"File extensions: {sorted(include_exts)}")
    console.print(f"Output:         {config.output_dir}")
    console.print()

    # ---- Phase 1: Scan ----
    plog.phase_start("1-scan")
    console.print("[bold cyan]Phase 1:[/bold cyan] Scanning directory structure...")
    components = phase1_scan(config, include_exts)
    total_files = sum(c.file_count for c in components)
    total_lines = sum(c.total_lines for c in components)
    console.print(
        f"  Found [green]{len(components)}[/green] components, "
        f"{total_files:,} files, {total_lines:,} lines"
    )
    plog.scan_done(len(components), total_files, total_lines)
    plog.phase_done("1-scan")

    if not components:
        console.print("[yellow]No components found. Check source_dirs in config.[/yellow]")
        return

    # ---- Change detection ----
    existing_meta = load_meta(config.output_dir)
    unchanged_keys, changed_keys, new_keys, deleted_keys, failed_keys = _classify_components(
        components, existing_meta, include_exts, config.exclude_patterns
    )
    skip_keys = unchanged_keys  # Only truly unchanged components are skipped

    # Report change summary
    to_process_count = len(changed_keys) + len(new_keys) + len(failed_keys)
    if unchanged_keys or changed_keys or deleted_keys or failed_keys:
        parts: list[str] = []
        if unchanged_keys:
            parts.append(f"{len(unchanged_keys)} unchanged")
        if changed_keys:
            parts.append(f"[yellow]{len(changed_keys)} changed[/yellow]")
        if new_keys:
            parts.append(f"[green]{len(new_keys)} new[/green]")
        if failed_keys:
            parts.append(f"[red]{len(failed_keys)} retrying[/red]")
        if deleted_keys:
            parts.append(f"[red]{len(deleted_keys)} deleted[/red]")
        console.print(f"  Change detection: {', '.join(parts)}")

    # ---- Cleanup deleted components ----
    if deleted_keys:
        deleted_list = cleanup_deleted_components(
            config.output_dir, {c.key for c in components}, existing_meta
        )
        for dk in deleted_list:
            plog.component_deleted(dk)
        console.print(f"  [red]Cleaned up {len(deleted_list)} deleted component(s)[/red]")

    # ---- Compute dirty packages ----
    dirty_packages = _compute_dirty_packages(
        components, changed_keys, new_keys, deleted_keys, failed_keys
    )
    if dirty_packages:
        console.print(
            f"  Dirty packages: [yellow]{len(dirty_packages)}[/yellow] "
            f"({', '.join(sorted(p.split('/')[-1] for p in dirty_packages))})"
        )

    # ---- Phase 2: AST extraction (only for non-skipped) ----
    plog.phase_start("2-ast")
    console.print("[bold cyan]Phase 2:[/bold cyan] Extracting AST skeletons...")
    ast_data: dict[str, dict] = {}
    total_entries = 0
    to_extract = [c for c in components if c.key not in skip_keys]
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(bar_width=30),
        MofNCompleteColumn(),
        TextColumn("{task.fields[current]}"),
        TimeElapsedColumn(),
        console=console,
        transient=True,
    ) as prog:
        task = prog.add_task("  Extracting", total=len(to_extract), current="")
        for i, comp in enumerate(to_extract):
            short = comp.key.split("/")[-1]
            prog.update(task, current=f"[dim]{short} ({comp.file_count} files)[/dim]")
            ast_map = phase2_extract_ast(comp, config, include_exts)
            ast_data[comp.key] = ast_map
            total_entries += len(ast_map)
            prog.update(task, advance=1)
    console.print(
        f"  Extracted [green]{total_entries}[/green] AST entries from {len(to_extract)} components"
    )
    plog.ast_done(total_entries)
    plog.phase_done("2-ast")

    # ---- Phase 3: Packing (only for non-skipped) ----
    plog.phase_start("3-pack")
    console.print("[bold cyan]Phase 3:[/bold cyan] Smart packing...")
    pack_data: dict[str, object] = {}
    total_batches = 0
    agg_count = 0
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(bar_width=30),
        MofNCompleteColumn(),
        TextColumn("{task.fields[current]}"),
        TimeElapsedColumn(),
        console=console,
        transient=True,
    ) as prog:
        task = prog.add_task("  Packing", total=len(to_extract), current="")
        for comp in to_extract:
            short = comp.key.split("/")[-1]
            prog.update(task, current=f"[dim]{short}[/dim]")
            pack_result = phase3_pack(comp, ast_data[comp.key], config.packing)
            pack_data[comp.key] = pack_result
            total_batches += len(pack_result.batches)
            agg_count += len(pack_result.aggregation_groups)
            prog.update(task, advance=1)
    console.print(
        f"  [green]{total_batches}[/green] batches, [green]{agg_count}[/green] aggregation groups"
    )
    plog.pack_done(total_batches, agg_count)
    plog.phase_done("3-pack")

    # ---- Dry-run ----
    if dry_run:
        _print_dry_run_summary(config, components, ast_data, pack_data, include_exts, total_batches)
        return

    # ---- Phase 4-5: LLM (concurrent with live dashboard) ----
    plog.phase_start("4-5-llm", f"{total_batches} batches, {to_process_count} to process")
    console.print()

    all_comp_results, all_pkg_results, index_md = asyncio.run(
        _run_summarization_live(
            config,
            components,
            ast_data,
            pack_data,
            skip_keys,
            existing_meta,
            plog,
            include_exts,
            dirty_packages,
            changed_keys,
            new_keys,
            failed_keys,
            deleted_keys,
        )
    )
    plog.phase_done("4-5-llm")

    # ---- Phase 6: Write package overviews + INDEX ----
    plog.phase_start("6-write")
    console.print("[bold cyan]Phase 6:[/bold cyan] Writing package overviews + INDEX...")
    final_meta = load_meta(config.output_dir)
    write_knowledge_base(config, index_md, all_pkg_results, final_meta)
    plog.phase_done("6-write")

    total_in = final_meta["total_tokens"]["input"]
    total_out = final_meta["total_tokens"]["output"]
    plog.finished(
        len(final_meta["components"]), final_meta.get("total_summaries", 0), total_in, total_out
    )

    console.print()
    console.print(f"[bold green]Done![/bold green] Knowledge base -> {config.output_dir}")
    console.print(f"  Components: {len(final_meta['components'])}")
    console.print(f"  Tokens:     {total_in:,} in / {total_out:,} out")


# ---------------------------------------------------------------------------
# Concurrent summarization with incremental persistence
# ---------------------------------------------------------------------------


async def _process_one_component(
    idx: int,
    comp,
    pack_data: dict,
    ast_data: dict,
    config: Config,
    comp_semaphore: asyncio.Semaphore,
    llm_semaphore: asyncio.Semaphore,
    ds: _DashboardState,
    plog: ProgressLog,
    live_ref: list,
    progress: Progress,
    comp_task,
    include_exts: set[str] | None = None,
) -> ComponentResult | None:
    """Process one component: Phase 4 + 5a + 5b, then write incrementally."""
    from .models import BatchSummary

    async with comp_semaphore:
        pack_result = pack_data[comp.key]
        ast_map = ast_data[comp.key]
        n_batches = len(pack_result.batches)

        async with ds._lock:
            ds.active_components.append(comp.key)
            ds.add_event(f"START {comp.key} ({n_batches}b)")
        plog.component_start(comp.key, idx + 1, ds.total_components, n_batches)
        _refresh_live(live_ref, ds, progress)

        # ---- Skip components with no extractable AST (e.g. C#-only, config-only) ----
        total_ast_tokens = sum(e.tokens for e in ast_map.values())
        if total_ast_tokens < MIN_AST_TOKENS_FOR_LLM:
            # Record skipped status so next run doesn't treat it as "new"
            from datetime import datetime, timezone

            save_meta_component(
                config.output_dir,
                comp.key,
                {
                    "path": str(comp.path),
                    "status": "skipped",
                    "reason": "ast_tokens_below_threshold",
                    "ast_tokens": total_ast_tokens,
                    "source_hash": compute_source_hash(
                        comp.path, include_exts, config.exclude_patterns
                    ),
                    "last_updated": datetime.now(timezone.utc).isoformat(),
                },
                config.llm.model,
            )
            async with ds._lock:
                ds.active_components = [c for c in ds.active_components if c != comp.key]
                ds.skipped_components += 1
                ds.done_components += 1
                ds.add_event(f"SKIP {comp.key} (no extractable AST)")
            progress.update(comp_task, completed=ds.done_components)
            _refresh_live(live_ref, ds, progress)
            return None

        # ---- Phase 4: leaf summaries (batches concurrent via llm_semaphore) ----
        detected_langs = {e.language for e in ast_map.values() if e.language}
        lang_str = ", ".join(sorted(detected_langs)) or "unknown"

        async def _do_batch(batch):
            async with llm_semaphore:
                ast_content, dir_tree = _build_batch_content(batch, ast_map)
                prompt = LEAF_PROMPT.format(
                    package_name=comp.package_name,
                    component_name=comp.name,
                    file_count=comp.file_count,
                    total_lines=comp.total_lines,
                    detected_languages=lang_str,
                    dir_tree=dir_tree,
                    ast_content=ast_content,
                    summary_language=config.summary_language,
                )
                max_out = _compute_leaf_max_tokens(batch.tokens)
                text, in_tok, out_tok = await _llm_call(prompt, config, max_tokens=max_out)
                return BatchSummary(
                    batch=batch,
                    summary=text,
                    ast_content=ast_content,
                    input_tokens=in_tok,
                    output_tokens=out_tok,
                )

        try:
            summaries = list(await asyncio.gather(*(_do_batch(b) for b in pack_result.batches)))
        except Exception as e:
            plog.error(f"Phase 4 failed: {e}", comp.key)
            # Record failure in meta so it can be retried next run
            save_meta_failed(
                config.output_dir,
                comp.key,
                str(e),
                comp_path=str(comp.path),
                llm_model=config.llm.model,
            )
            async with ds._lock:
                ds.errors += 1
                ds.active_components = [c for c in ds.active_components if c != comp.key]
                ds.done_components += 1
                ds.add_event(f"ERROR {comp.key}: {e}")
            progress.update(comp_task, completed=ds.done_components)
            _refresh_live(live_ref, ds, progress)
            return None

        for s in summaries:
            async with ds._lock:
                ds.llm_calls += 1
            plog.llm_call("leaf", comp.key, s.input_tokens, s.output_tokens)

        # ---- Phase 5a: aggregate fragments ----
        submodule_summaries: dict[str, str] = {}
        submodule_ast: dict[str, str] = {}
        total_in = sum(s.input_tokens for s in summaries)
        total_out = sum(s.output_tokens for s in summaries)

        if pack_result.aggregation_groups:
            try:
                aggregated = await phase5a_aggregate(
                    pack_result.aggregation_groups, summaries, config
                )
            except Exception as e:
                plog.error(f"Phase 5a failed: {e}", comp.key)
                async with ds._lock:
                    ds.errors += 1
                    ds.add_event(f"ERROR 5a {comp.key}: {e}")
                aggregated = {}
            for parent, (text, in_t, out_t) in aggregated.items():
                submodule_summaries[parent] = text
                total_in += in_t
                total_out += out_t
                async with ds._lock:
                    ds.llm_calls += 1
                plog.llm_call("aggregate", parent, in_t, out_t)
            # Collect AST content: merge all batches in each agg group
            agg_indices = set()
            for ag in pack_result.aggregation_groups:
                agg_indices.update(ag.batch_indices)
                parts = [
                    summaries[i].ast_content for i in ag.batch_indices if summaries[i].ast_content
                ]
                if parts:
                    submodule_ast[ag.parent] = "\n\n".join(parts)
            for i, s in enumerate(summaries):
                if i not in agg_indices:
                    key = s.batch.group_key or f"batch_{i}"
                    submodule_summaries[key] = s.summary
                    if s.ast_content:
                        submodule_ast[key] = s.ast_content
        else:
            for i, s in enumerate(summaries):
                key = s.batch.group_key or f"batch_{i}"
                submodule_summaries[key] = s.summary
                if s.ast_content:
                    submodule_ast[key] = s.ast_content

        # ---- Phase 5b: component overview (skip for single batch w/o aggregation) ----
        skip_phase5b = n_batches == 1 and not pack_result.aggregation_groups
        if skip_phase5b and submodule_summaries:
            # Use the single leaf summary directly as overview
            single_key = next(iter(submodule_summaries))
            overview = f"# {comp.name}\n\n{submodule_summaries[single_key]}"
            in_t = out_t = 0
            async with ds._lock:
                ds.add_event(f"SKIP 5b {comp.key} (single batch)")
        else:
            try:
                overview, in_t, out_t = await phase5b_component(comp, submodule_summaries, config)
            except Exception as e:
                plog.error(f"Phase 5b failed: {e}", comp.key)
                async with ds._lock:
                    ds.errors += 1
                    ds.add_event(f"ERROR 5b {comp.key}: {e}")
                overview = f"# {comp.name}\n\n*Summary generation failed.*"
                in_t = out_t = 0
            total_in += in_t
            total_out += out_t
            async with ds._lock:
                ds.llm_calls += 1
            plog.llm_call("component", comp.key, in_t, out_t)

        # Detect primary language from AST entries for code block highlighting
        detected_lang = next(iter(sorted(detected_langs)), "cpp")

        cr = ComponentResult(
            component=comp,
            overview=overview,
            submodule_summaries=submodule_summaries,
            submodule_ast=submodule_ast,
            detected_language=detected_lang,
            batch_count=n_batches,
            total_input_tokens=total_in,
            total_output_tokens=total_out,
        )

        # ---- Incremental persist ----
        write_component_incremental(config, cr)
        save_meta_component(
            config.output_dir,
            comp.key,
            build_component_meta(cr, include_exts, config.exclude_patterns),
            config.llm.model,
        )

        async with ds._lock:
            ds.active_components = [c for c in ds.active_components if c != comp.key]
            ds.done_components += 1
            ds.total_in_tokens += total_in
            ds.total_out_tokens += total_out
            ds.add_event(f"DONE {comp.key} ({total_in:,}in/{total_out:,}out)")

        plog.component_done(comp.key, ds.done_components, ds.total_components, total_in, total_out)
        progress.update(comp_task, completed=ds.done_components)
        _refresh_live(live_ref, ds, progress)

        return cr


async def _run_summarization_live(
    config: Config,
    components: list,
    ast_data: dict,
    pack_data: dict,
    skip_keys: set[str],
    existing_meta: dict,
    plog: ProgressLog,
    include_exts: set[str] | None,
    dirty_packages: set[str],
    changed_keys: set[str],
    new_keys: set[str],
    failed_keys: set[str],
    deleted_keys: set[str],
) -> tuple[list[ComponentResult], dict[str, PackageResult], str | None]:
    """Run Phase 4-5 with concurrent components and live dashboard."""

    ds = _DashboardState()
    ds.total_components = len(components)
    ds.skipped_components = len(skip_keys)
    ds.done_components = len(skip_keys)
    ds.changed_components = len(changed_keys)
    ds.new_components = len(new_keys)
    ds.deleted_components = len(deleted_keys)
    ds.failed_components = len(failed_keys)

    to_process = [c for c in components if c.key not in skip_keys]

    pkg_set: set[str] = set()
    for c in components:
        pkg_set.add(f"{c.source_name}/{c.package_name}")
    ds.total_packages = len(pkg_set)
    ds.dirty_packages = len(dirty_packages)

    progress = Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(bar_width=30),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
        console=console,
    )
    comp_task = progress.add_task(
        "Components", total=ds.total_components, completed=ds.skipped_components
    )
    dirty_pkg_count = len(dirty_packages)
    pkg_task = progress.add_task(
        "Packages", total=dirty_pkg_count if dirty_pkg_count else 1, visible=False
    )

    llm_semaphore = asyncio.Semaphore(config.llm.max_concurrent)
    comp_semaphore = asyncio.Semaphore(config.llm.max_concurrent_components)

    all_component_results: list[ComponentResult] = []
    live_ref: list[Live] = []

    with Live(_build_dashboard(ds, progress), console=console, refresh_per_second=2) as live:
        live_ref.append(live)

        ds.phase = "4-5  Components"
        if skip_keys:
            ds.add_event(f"Resumed: {len(skip_keys)} unchanged (skipped)")
        if changed_keys:
            ds.add_event(f"Changed: {len(changed_keys)} components to regenerate")
        if new_keys:
            ds.add_event(f"New: {len(new_keys)} components to generate")
        if failed_keys:
            ds.add_event(f"Retrying: {len(failed_keys)} previously failed")
        if deleted_keys:
            ds.add_event(f"Deleted: {len(deleted_keys)} components cleaned up")
        _refresh_live(live_ref, ds, progress)

        # ---- Launch all component tasks concurrently ----
        tasks = [
            _process_one_component(
                i,
                c,
                pack_data,
                ast_data,
                config,
                comp_semaphore,
                llm_semaphore,
                ds,
                plog,
                live_ref,
                progress,
                comp_task,
                include_exts,
            )
            for i, c in enumerate(to_process)
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for r in results:
            if isinstance(r, ComponentResult):
                all_component_results.append(r)
            elif isinstance(r, Exception):
                tb = "".join(traceback.format_exception(type(r), r, r.__traceback__))
                logging.getLogger("kblens.cli").error(
                    "Unhandled exception in component task:\n%s", tb
                )
                async with ds._lock:
                    ds.errors += 1
                    ds.add_event(f"EXCEPTION: {r}")

        # ---- Build pkg_groups (only for dirty packages) ----
        # We need component overviews for ALL components in dirty packages,
        # including unchanged ones read from disk.
        pkg_groups: dict[str, dict[str, ComponentResult]] = defaultdict(dict)
        for cr in all_component_results:
            c = cr.component
            pkg_groups[f"{c.source_name}/{c.package_name}"][c.name] = cr

        # ---- Phase 5c: package overviews (only dirty packages) ----
        if dirty_packages:
            ds.phase = "5c  Packages"
            ds.active_components = []
            progress.update(pkg_task, visible=True, total=len(dirty_packages))
            _refresh_live(live_ref, ds, progress)

            all_pkg_results: dict[str, PackageResult] = {}
            package_overviews: dict[str, tuple[str, str]] = {}

            pkg_idx = 0
            for pkg_key in sorted(dirty_packages):
                parts = pkg_key.split("/", 1)
                if len(parts) < 2:
                    continue
                source_name, pkg_name = parts

                # Build comp_overviews from results + disk (for skipped components)
                comp_dict = pkg_groups.get(pkg_key, {})
                ds.add_event(f"Package: {pkg_name} ({len(comp_dict)} new/changed)")
                _refresh_live(live_ref, ds, progress)

                comp_overviews: dict[str, tuple[str, int]] = {
                    name: (cr.overview, cr.component.file_count) for name, cr in comp_dict.items()
                }
                # Include unchanged components by reading their .md from disk
                for comp in components:
                    full_pkg_key = f"{comp.source_name}/{comp.package_name}"
                    if full_pkg_key == pkg_key and comp.name not in comp_overviews:
                        md_path = (
                            Path(config.output_dir)
                            / comp.source_name
                            / comp.package_name
                            / f"{comp.name.replace('/', '_')}.md"
                        )
                        if md_path.exists():
                            txt = md_path.read_text(encoding="utf-8")
                            txt = strip_ast_section(txt)
                            comp_overviews[comp.name] = (txt, comp.file_count)
                        else:
                            # Component exists but has no .md
                            meta_entry = existing_meta.get("components", {}).get(comp.key, {})
                            status = meta_entry.get("status", "")
                            if status == "failed":
                                comp_overviews[comp.name] = (
                                    f"*Generation failed: {meta_entry.get('error', 'unknown')}*",
                                    comp.file_count,
                                )
                            elif status == "skipped":
                                reason = meta_entry.get("reason", "insufficient AST content")
                                comp_overviews[comp.name] = (
                                    f"*Skipped: {reason} ({comp.file_count} files)*",
                                    comp.file_count,
                                )

                try:
                    pkg_overview, in_t, out_t = await phase5c_package(
                        pkg_name, comp_overviews, config
                    )
                except Exception as e:
                    plog.error(f"Phase 5c failed: {e}", pkg_name)
                    async with ds._lock:
                        ds.errors += 1
                        ds.add_event(f"ERROR 5c {pkg_name}: {e}")
                    pkg_overview = f"# {pkg_name}\n\n*Summary generation failed.*"
                    in_t = out_t = 0

                async with ds._lock:
                    ds.llm_calls += 1
                    ds.total_in_tokens += in_t
                    ds.total_out_tokens += out_t
                plog.package_done(pkg_name)

                pkg_result = PackageResult(
                    name=pkg_name,
                    source_name=source_name,
                    overview=pkg_overview,
                    components=list(comp_dict.values()),
                )
                all_pkg_results[pkg_key] = pkg_result
                package_overviews[pkg_key] = (pkg_overview, source_name)

                pkg_idx += 1
                ds.packages_done = pkg_idx
                progress.update(pkg_task, completed=pkg_idx)
                _refresh_live(live_ref, ds, progress)

            # ---- Phase 5d: INDEX.md (only if any package changed) ----
            ds.phase = "5d  INDEX.md"
            ds.add_event("Generating INDEX.md")
            _refresh_live(live_ref, ds, progress)

            # Build full package_overviews including unchanged packages from disk
            for pkg_key in pkg_set:
                if pkg_key in dirty_packages:
                    continue  # Already in package_overviews
                parts = pkg_key.split("/", 1)
                if len(parts) < 2:
                    continue
                source_name, pkg_name = parts
                md_path = Path(config.output_dir) / source_name / f"{pkg_name}.md"
                if md_path.exists():
                    txt = md_path.read_text(encoding="utf-8")
                    # Strip leading '# ...' title line so format matches LLM raw output
                    # from dirty packages (which have no title prefix).
                    if txt.startswith("# "):
                        txt = txt.split("\n", 1)[1].lstrip("\n") if "\n" in txt else ""
                    package_overviews[pkg_key] = (txt, source_name)

            try:
                index_md, in_t, out_t = await phase5d_index(package_overviews, config)
            except Exception as e:
                plog.error(f"Phase 5d failed: {e}", "INDEX")
                async with ds._lock:
                    ds.errors += 1
                    ds.add_event(f"ERROR 5d: {e}")
                index_md = "# Knowledge Base Index\n\n*Generation failed.*"
                in_t = out_t = 0

            async with ds._lock:
                ds.llm_calls += 1
                ds.total_in_tokens += in_t
                ds.total_out_tokens += out_t
            plog.index_done()
        else:
            # No dirty packages — skip L1 + L0
            all_pkg_results = {}
            index_md = None  # Signal to write_knowledge_base to skip INDEX.md
            ds.add_event("No dirty packages — skipping L1/L0 generation")

        ds.phase = "DONE"
        ds.finished = True
        ds.add_event("Generation complete")
        _refresh_live(live_ref, ds, progress)

    return all_component_results, all_pkg_results, index_md


# ---------------------------------------------------------------------------
# Dry-run
# ---------------------------------------------------------------------------


def _print_dry_run_summary(
    config: Config,
    components: list,
    ast_data: dict,
    pack_data: dict,
    include_exts: set[str],
    total_batches: int,
) -> None:
    console.print()
    console.print("[bold]--- Dry Run Summary ---[/bold]")
    console.print()

    total_files = sum(c.file_count for c in components)
    total_lines = sum(c.total_lines for c in components)
    total_ast = sum(len(ast_data.get(c.key, {})) for c in components)
    est_tokens = sum(
        b.tokens for c in components for b in getattr(pack_data.get(c.key), "batches", [])
    )

    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column(style="bold")
    table.add_column()
    table.add_row("Components", str(len(components)))
    table.add_row("Total files", f"{total_files:,}")
    table.add_row("Total lines", f"{total_lines:,}")
    table.add_row("AST entries", f"{total_ast:,}")
    table.add_row("File extensions", str(sorted(include_exts)))
    table.add_row("Batches", str(total_batches))
    table.add_row("Est. LLM input tokens", f"{est_tokens:,}")
    table.add_row("Est. LLM calls", f"~{total_batches + len(components) + 20}")
    table.add_row("Output directory", config.output_dir)
    table.add_row("LLM model", config.llm.model)
    console.print(table)

    console.print()
    console.print("[bold]Top 10 largest components:[/bold]")
    top = sorted(components, key=lambda c: c.file_count, reverse=True)[:10]
    top_table = Table(box=None, padding=(0, 1))
    top_table.add_column("Component", style="cyan")
    top_table.add_column("Files", justify="right")
    top_table.add_column("Lines", justify="right")
    top_table.add_column("Batches", justify="right")
    for c in top:
        pr = pack_data.get(c.key)
        batches = len(pr.batches) if pr else 0
        top_table.add_row(c.key, str(c.file_count), f"{c.total_lines:,}", str(batches))
    console.print(top_table)


# ---------------------------------------------------------------------------
# kblens monitor
# ---------------------------------------------------------------------------


@app.command()
def monitor(
    config_path: str = typer.Option(
        None, "--config", "-c", help="Config file path (default: auto-detect)"
    ),
    source: str = typer.Option(
        None, "--source", "-s", help="Source name to monitor (auto-detect if only one)"
    ),
    follow: bool = typer.Option(True, "--follow/--no-follow", "-f", help="Continuously follow"),
) -> None:
    """Monitor a running generate process in real time."""
    try:
        config = load_config(config_path)
    except Exception as e:
        console.print(f"[red]Error loading config:[/red] {e}")
        raise typer.Exit(1)

    # Find the progress file in the correct source subdirectory
    if source:
        progress_path = Path(config.output_dir) / source / "_progress.jsonl"
    else:
        # Auto-detect: find the most recently modified _progress.jsonl
        candidates = list(Path(config.output_dir).glob("*/_progress.jsonl"))
        if len(candidates) == 1:
            progress_path = candidates[0]
        elif len(candidates) > 1:
            # Pick the most recently modified
            progress_path = max(candidates, key=lambda p: p.stat().st_mtime)
            console.print(
                f"[dim]Multiple sources found, monitoring most recent: "
                f"{progress_path.parent.name}[/dim]"
            )
            console.print(f"[dim]Use --source <name> to pick a specific one.[/dim]")
        else:
            progress_path = Path(config.output_dir) / "_progress.jsonl"
    if not progress_path.exists():
        console.print(
            f"[yellow]No progress file found at {progress_path}[/yellow]\n"
            "Start a generate first, then run monitor in another terminal."
        )
        raise typer.Exit(1)

    console.print(f"[bold]KBLens Monitor[/bold]  ({progress_path})")
    console.print()

    state = _ExtMonitorState()
    with open(progress_path, "r", encoding="utf-8") as f:
        for line in f:
            _ext_process_line(line, state)
    _ext_render(state)

    if not follow:
        return

    try:
        with open(progress_path, "r", encoding="utf-8") as f:
            f.seek(0, 2)
            while True:
                line = f.readline()
                if line:
                    if _ext_process_line(line, state):
                        _ext_render(state)
                else:
                    if state.finished:
                        console.print("\n[bold green]Generation complete.[/bold green]")
                        break
                    time.sleep(0.5)
    except KeyboardInterrupt:
        console.print("\n[dim]Monitor stopped.[/dim]")


class _ExtMonitorState:
    def __init__(self) -> None:
        self.phase: str = ""
        self.total_components: int = 0
        self.done_components: int = 0
        self.total_batches: int = 0
        self.llm_calls: int = 0
        self.total_in_tokens: int = 0
        self.total_out_tokens: int = 0
        self.errors: list[str] = []
        self.packages_done: int = 0
        self.elapsed: float = 0
        self.finished: bool = False
        self.events: list[str] = []


def _ext_process_line(line: str, s: _ExtMonitorState) -> bool:
    line = line.strip()
    if not line:
        return False
    try:
        e = json.loads(line)
    except json.JSONDecodeError:
        return False
    ev = e.get("event", "")
    s.elapsed = e.get("elapsed", s.elapsed)
    if ev == "phase_start":
        s.phase = e.get("phase", "")
        s.events.append(f"Phase {s.phase}")
        return True
    elif ev == "scan_done":
        s.total_components = e.get("components", 0)
        return True
    elif ev == "pack_done":
        s.total_batches = e.get("batches", 0)
        return True
    elif ev == "comp_start":
        s.events.append(
            f"[{e.get('index', 0)}/{e.get('total', 0)}] {e.get('key', '')} ({e.get('batches', 0)}b)"
        )
        return True
    elif ev == "comp_done":
        s.done_components = max(s.done_components, e.get("index", s.done_components))
        s.total_in_tokens += e.get("in_tokens", 0)
        s.total_out_tokens += e.get("out_tokens", 0)
        return True
    elif ev == "comp_deleted":
        s.events.append(f"DELETED: {e.get('key', '')}")
        return True
    elif ev == "comp_changed":
        s.events.append(f"CHANGED: {e.get('key', '')}")
        return True
    elif ev == "llm_call":
        s.llm_calls += 1
        return False
    elif ev == "pkg_done":
        s.packages_done += 1
        s.events.append(f"Package: {e.get('name', '')}")
        return True
    elif ev == "index_done":
        s.events.append("INDEX.md generated")
        return True
    elif ev == "error":
        s.errors.append(e.get("message", ""))
        s.events.append(f"ERROR: {e.get('message', '')}")
        return True
    elif ev == "finished":
        s.finished = True
        s.events.append("FINISHED")
        return True
    elif ev == "phase_done":
        return True
    return False


def _ext_render(s: _ExtMonitorState) -> None:
    elapsed_str = _fmt_elapsed(s.elapsed)
    if s.total_components > 0:
        pct = s.done_components * 100 // s.total_components
        comp_str = f"{s.done_components}/{s.total_components} ({pct}%)"
    else:
        comp_str = "---"
    eta_str = "---"
    if s.done_components > 0 and s.total_components > s.done_components:
        per = s.elapsed / s.done_components
        eta_str = _fmt_elapsed((s.total_components - s.done_components) * per)
    console.print(f"  [bold]Phase:[/bold]       {s.phase}")
    console.print(f"  [bold]Components:[/bold]  {comp_str}")
    console.print(f"  [bold]LLM calls:[/bold]   {s.llm_calls}")
    console.print(
        f"  [bold]Tokens:[/bold]      {s.total_in_tokens:,} in / {s.total_out_tokens:,} out"
    )
    console.print(f"  [bold]Elapsed:[/bold]     {elapsed_str}")
    console.print(f"  [bold]ETA:[/bold]         {eta_str}")
    if s.errors:
        console.print(f"  [bold red]Errors:[/bold red]      {len(s.errors)}")
    console.print()
    for ev in s.events[-8:]:
        console.print(f"  {ev}")
    console.print()


def _fmt_elapsed(seconds: float) -> str:
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h > 0:
        return f"{h}h {m:02d}m {sec:02d}s"
    return f"{m}m {sec:02d}s"


# ---------------------------------------------------------------------------
# kblens status / version
# ---------------------------------------------------------------------------


@app.command()
def status(
    config_path: str = typer.Option(
        None, "--config", "-c", help="Config file path (default: auto-detect)"
    ),
) -> None:
    """Show knowledge base status."""
    try:
        config = load_config(config_path)
    except Exception as e:
        console.print(f"[red]Error loading config:[/red] {e}")
        raise typer.Exit(1)

    console.print(f"[bold]KBLens v{__version__}[/bold]")
    console.print(f"Output root: {config.output_dir}")
    console.print()

    grand_total = 0
    grand_done = 0
    grand_failed = 0
    grand_skipped = 0
    grand_in = 0
    grand_out = 0

    for src in config.source_dirs:
        src_output = str(Path(config.output_dir) / src.name)
        meta = load_meta(src_output)
        comps = meta.get("components", {})
        total = len(comps)
        done = sum(1 for c in comps.values() if c.get("status") not in ("failed", "skipped"))
        failed = sum(1 for c in comps.values() if c.get("status") == "failed")
        skipped = sum(1 for c in comps.values() if c.get("status") == "skipped")
        tokens = meta.get("total_tokens", {})
        in_tok = tokens.get("input", 0)
        out_tok = tokens.get("output", 0)

        console.print(f"[bold cyan]{src.name}[/bold cyan]  ({src.path})")
        console.print(f"  Components:   {total} ({done} done, {skipped} skipped, {failed} failed)")
        console.print(f"  Last updated: {meta.get('generated_at', 'never') or 'never'}")
        console.print(f"  LLM model:    {meta.get('llm_model', 'N/A') or 'N/A'}")
        console.print(f"  Tokens:       {in_tok:,} in / {out_tok:,} out")
        console.print()

        grand_total += total
        grand_done += done
        grand_failed += failed
        grand_skipped += skipped
        grand_in += in_tok
        grand_out += out_tok

    if len(config.source_dirs) > 1:
        console.print(
            f"[bold]Total:[/bold] {grand_total} components "
            f"({grand_done} done, {grand_skipped} skipped, {grand_failed} failed), "
            f"{grand_in:,} in / {grand_out:,} out"
        )


# ---------------------------------------------------------------------------
# kblens init
# ---------------------------------------------------------------------------


@app.command()
def init(
    output: str = typer.Option(
        None,
        "--output",
        "-o",
        help="Where to write config (default: ~/.config/kblens/config.yaml)",
    ),
) -> None:
    """Create a configuration file interactively."""
    target = Path(output) if output else USER_CONFIG_FILE

    if target.exists():
        overwrite = typer.confirm(f"Config already exists at {target}. Overwrite?", default=False)
        if not overwrite:
            console.print("[yellow]Aborted.[/yellow]")
            raise typer.Exit(0)

    console.print(f"[bold]KBLens v{__version__} — Configuration Setup[/bold]")
    console.print()

    # Source directory
    source_path = typer.prompt(
        "Source directory to scan (absolute path)",
        default="/path/to/your/source",
    )
    source_name = typer.prompt(
        "Short name for this source",
        default=Path(source_path).name if source_path != "/path/to/your/source" else "my_project",
    )

    # Output directory
    output_dir = typer.prompt(
        "Output directory for knowledge base",
        default="~/kblens_kb",
    )

    # LLM settings
    llm_model = typer.prompt("LLM model name", default="gpt-4o-mini")
    llm_api_base = typer.prompt(
        "LLM API base URL",
        default="https://api.openai.com/v1",
    )

    # Summary language
    summary_language = typer.prompt("Summary language (en/zh/...)", default="en")

    # Generate config content
    content = CONFIG_TEMPLATE.format(
        output_dir=output_dir,
        source_path=source_path,
        source_name=source_name,
        llm_model=llm_model,
        llm_api_base=llm_api_base,
        summary_language=summary_language,
    )

    # Write
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")

    console.print()
    console.print(f"[bold green]Config written to:[/bold green] {target}")
    console.print()
    console.print("Next steps:")
    console.print(f"  1. Edit [cyan]{target}[/cyan] to set your API key")
    console.print(f"     (or set KBLENS_LLM_KEY environment variable)")
    console.print(f"  2. Run [cyan]kblens generate[/cyan] to build the knowledge base")
    console.print(f"  3. Run [cyan]kblens generate --dry-run[/cyan] to preview first")
    _print_skill_setup_guidance()


@app.command()
def version() -> None:
    """Show version."""
    console.print(f"KBLens {__version__}")


if __name__ == "__main__":
    app()
