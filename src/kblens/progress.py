"""Progress logging for KBLens pipeline.

Writes structured JSON-lines to {output_dir}/_progress.jsonl,
allowing a separate `kblens monitor` process to tail and display progress.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class ProgressLog:
    """Structured progress logger that writes to a JSONL file."""

    def __init__(self, output_dir: str | Path):
        self._path = Path(output_dir) / "_progress.jsonl"
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._start_time = time.monotonic()
        # Truncate on new run
        self._path.write_text("", encoding="utf-8")

    def _write(self, entry: dict[str, Any]) -> None:
        entry["ts"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        entry["elapsed"] = round(time.monotonic() - self._start_time, 1)
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    # -- Phase transitions --
    def phase_start(self, phase: str, detail: str = "") -> None:
        self._write({"event": "phase_start", "phase": phase, "detail": detail})

    def phase_done(self, phase: str, detail: str = "") -> None:
        self._write({"event": "phase_done", "phase": phase, "detail": detail})

    # -- Scan / AST / Pack summaries --
    def scan_done(self, components: int, files: int, lines: int) -> None:
        self._write(
            {
                "event": "scan_done",
                "components": components,
                "files": files,
                "lines": lines,
            }
        )

    def ast_done(self, entries: int) -> None:
        self._write({"event": "ast_done", "entries": entries})

    def pack_done(self, batches: int, agg_groups: int) -> None:
        self._write({"event": "pack_done", "batches": batches, "agg_groups": agg_groups})

    # -- Component-level progress --
    def component_start(self, key: str, index: int, total: int, batches: int) -> None:
        self._write(
            {
                "event": "comp_start",
                "key": key,
                "index": index,
                "total": total,
                "batches": batches,
            }
        )

    def component_done(
        self,
        key: str,
        index: int,
        total: int,
        in_tokens: int,
        out_tokens: int,
    ) -> None:
        self._write(
            {
                "event": "comp_done",
                "key": key,
                "index": index,
                "total": total,
                "in_tokens": in_tokens,
                "out_tokens": out_tokens,
            }
        )

    # -- LLM call level --
    def llm_call(
        self,
        step: str,
        target: str,
        in_tokens: int,
        out_tokens: int,
    ) -> None:
        self._write(
            {
                "event": "llm_call",
                "step": step,
                "target": target,
                "in_tokens": in_tokens,
                "out_tokens": out_tokens,
            }
        )

    # -- Package / Index --
    def package_done(self, name: str) -> None:
        self._write({"event": "pkg_done", "name": name})

    def index_done(self) -> None:
        self._write({"event": "index_done"})

    # -- Errors --
    def error(self, message: str, target: str = "") -> None:
        self._write({"event": "error", "message": message, "target": target})

    # -- Component lifecycle --
    def component_deleted(self, key: str) -> None:
        self._write({"event": "comp_deleted", "key": key})

    def component_changed(self, key: str) -> None:
        self._write({"event": "comp_changed", "key": key})

    # -- Final --
    def finished(self, components: int, summaries: int, in_tok: int, out_tok: int) -> None:
        self._write(
            {
                "event": "finished",
                "components": components,
                "summaries": summaries,
                "in_tokens": in_tok,
                "out_tokens": out_tok,
            }
        )
