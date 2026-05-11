from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from kblens import cli
from kblens.models import Component, ComponentResult, Config, PackingConfig, SourceDir
from kblens.writer import write_component_incremental


def test_write_component_incremental_rewrites_doc_asset_refs(tmp_path: Path) -> None:
    source_root = tmp_path / "docs"
    comp_dir = source_root / "OVR_Home" / "GAME_CONTENT"
    images_dir = comp_dir / "_images"
    images_dir.mkdir(parents=True)
    (images_dir / "diagram.png").write_bytes(b"png")

    config = Config(
        output_dir=str(tmp_path / "kb"),
        source_dirs=[SourceDir(path=str(source_root), name="ovr-confluence", type="document")],
        packing=PackingConfig(component_split_threshold=200),
    )
    component = Component(
        source_name="ovr-confluence",
        package_name="OVR_Home",
        name="GAME_CONTENT",
        path=comp_dir,
        file_count=1,
        total_lines=20,
    )
    result = ComponentResult(
        component=component,
        overview="# GAME_CONTENT",
        submodule_ast={
            "page.md": "## Original Content\n\n![diagram](__kblens_asset__/_images/diagram.png)",
        },
        detected_language="markdown",
        batch_count=1,
    )

    write_component_incremental(config, result)

    output_md = tmp_path / "kb" / "ovr-confluence" / "OVR_Home" / "GAME_CONTENT.md"
    text = output_md.read_text(encoding="utf-8")
    assert "_images/diagram.png" in text
    assert "../__assets/GAME_CONTENT/_images/diagram.png" in text or "__assets/GAME_CONTENT/_images/diagram.png" in text
    asset_file = (
        tmp_path
        / "kb"
        / "ovr-confluence"
        / "OVR_Home"
        / "__assets"
        / "GAME_CONTENT"
        / "_images"
        / "diagram.png"
    )
    assert asset_file.is_file()


def test_migrate_doc_assets_rewrites_existing_kb(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    source_root = tmp_path / "confluence_docs"
    comp_dir = source_root / "OVR_Home" / "GAME_CONTENT"
    images_dir = comp_dir / "_images"
    images_dir.mkdir(parents=True)
    (images_dir / "legacy.png").write_bytes(b"png")

    output_root = tmp_path / "kb"
    source_output = output_root / "ovr-confluence"
    package_dir = source_output / "ovr-confluence" / "OVR_Home"
    package_dir.mkdir(parents=True)
    (source_output / "INDEX.md").write_text("# INDEX\n", encoding="utf-8")
    (source_output / "_meta.json").write_text(
        '{"components": {"ovr-confluence/OVR_Home/GAME_CONTENT": {"path": "%s"}}}'
        % str(comp_dir).replace("\\", "\\\\"),
        encoding="utf-8",
    )
    legacy_md = package_dir / "GAME_CONTENT.md"
    legacy_md.write_text("![legacy](_images/legacy.png)\n", encoding="utf-8")

    config_path = tmp_path / "kblens.yaml"
    config_path.write_text(
        (
            "version: 1\n"
            "output_dir: \"%s\"\n"
            "sources:\n"
            "  - path: \"%s\"\n"
            "    name: \"ovr-confluence\"\n"
            "    type: \"document\"\n"
        )
        % (str(output_root).replace("\\", "/"), str(source_root).replace("\\", "/")),
        encoding="utf-8",
    )

    _console_fh = open(Path(tmp_path / "console.txt"), "w", encoding="utf-8")
    monkeypatch.setattr(cli, "console", cli.Console(file=_console_fh))
    result = runner.invoke(
        cli.app,
        ["migrate-doc-assets", "--config", str(config_path), "--source", "ovr-confluence"],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    updated = legacy_md.read_text(encoding="utf-8")
    assert "__assets/GAME_CONTENT/_images/legacy.png" in updated
    asset_file = package_dir / "__assets" / "GAME_CONTENT" / "_images" / "legacy.png"
    assert asset_file.is_file()
