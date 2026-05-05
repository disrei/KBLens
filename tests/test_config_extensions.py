from __future__ import annotations

from pathlib import Path

from kblens.config import load_config
from kblens.models import Config, SourceDir
from kblens.scanner import resolve_include_extensions


def test_load_config_parses_ignore_extensions(tmp_path: Path) -> None:
    config_path = tmp_path / "kblens.yaml"
    config_path.write_text(
        """
version: 1
sources:
  - path: ./src
    name: demo
include_extensions: auto
ignore_extensions:
  - cs
  - "*.MD"
""".strip(),
        encoding="utf-8",
    )
    (tmp_path / "src").mkdir()

    cfg = load_config(config_path)

    assert cfg.ignore_extensions == [".cs", ".md"]


def test_resolve_include_extensions_applies_ignore_extensions(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.cpp").write_text("int main() { return 0; }", encoding="utf-8")
    (src / "b.h").write_text("#pragma once", encoding="utf-8")
    (src / "c.cs").write_text("public class Demo {}", encoding="utf-8")

    cfg = Config(
        source_dirs=[SourceDir(path=str(src), name="demo")],
        include_extensions="auto",
        ignore_extensions=[".cs"],
    )

    assert resolve_include_extensions(cfg) == {".cpp", ".h"}


def test_resolve_include_extensions_applies_ignore_extensions_to_manual_list(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()

    cfg = Config(
        source_dirs=[SourceDir(path=str(src), name="demo")],
        include_extensions=["*.cpp", "cs", ".h"],
        ignore_extensions=["cs"],
    )

    assert resolve_include_extensions(cfg) == {".cpp", ".h"}
