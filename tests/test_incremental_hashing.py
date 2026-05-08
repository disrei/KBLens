from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import os

from kblens.writer import compute_source_content_hash, compute_source_hash, is_component_done


def test_compute_source_hash_ignores_mtime_only_changes(tmp_path: Path) -> None:
    comp = tmp_path / "component"
    comp.mkdir()
    source = comp / "example.cpp"
    source.write_text("int answer() { return 42; }\n", encoding="utf-8")

    before = compute_source_hash(comp, include_exts={".cpp"})

    stat = source.stat()
    os.utime(source, (stat.st_atime + 2, stat.st_mtime + 2))
    after = compute_source_hash(comp, include_exts={".cpp"})

    assert source.stat().st_mtime > stat.st_mtime
    assert before != after


def test_compute_source_hash_changes_when_file_content_changes(tmp_path: Path) -> None:
    comp = tmp_path / "component"
    comp.mkdir()
    source = comp / "example.cpp"
    source.write_text("int answer() { return 42; }\n", encoding="utf-8")

    before = compute_source_hash(comp, include_exts={".cpp"})

    source.write_text("int answer() { return 7; }\n", encoding="utf-8")
    after = compute_source_hash(comp, include_exts={".cpp"})

    assert before != after


def test_compute_source_content_hash_ignores_mtime_only_changes(tmp_path: Path) -> None:
    comp = tmp_path / "component"
    comp.mkdir()
    source = comp / "example.cpp"
    source.write_text("int answer() { return 42; }\n", encoding="utf-8")

    before = compute_source_content_hash(comp, include_exts={".cpp"})

    stat = source.stat()
    os.utime(source, (stat.st_atime + 2, stat.st_mtime + 2))
    after = compute_source_content_hash(comp, include_exts={".cpp"})

    assert source.stat().st_mtime > stat.st_mtime
    assert before == after


def test_is_component_done_uses_content_hash_as_second_factor(tmp_path: Path) -> None:
    comp = tmp_path / "component"
    comp.mkdir()
    source = comp / "example.cpp"
    source.write_text("int answer() { return 42; }\n", encoding="utf-8")

    old_source_hash = compute_source_hash(comp, include_exts={".cpp"})
    old_content_hash = compute_source_content_hash(comp, include_exts={".cpp"})

    stat = source.stat()
    os.utime(source, (stat.st_atime + 2, stat.st_mtime + 2))

    meta = {
        "components": {
            "demo/component": {
                "status": "done",
                "source_hash": old_source_hash,
                "content_hash": old_content_hash,
                "last_updated": datetime.now(timezone.utc).isoformat(),
            }
        }
    }

    assert is_component_done(meta, "demo/component", comp, include_exts={".cpp"})
