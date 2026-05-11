from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from kblens import cli
from kblens.config import load_raw_config


runner = CliRunner()


def _invoke_in_cwd(monkeypatch, cwd: Path):
    monkeypatch.chdir(cwd)
    return runner.invoke(cli.app, ["add-kb"], catch_exceptions=False, env={})


def test_add_kb_uses_local_project_config_for_generation(
    tmp_path: Path, monkeypatch
) -> None:
    project_dir = tmp_path / "demo"
    project_dir.mkdir()
    (project_dir / "src").mkdir()

    home_dir = tmp_path / "home"
    user_config_dir = home_dir / ".config" / "kblens"
    user_config_dir.mkdir(parents=True)
    user_config = user_config_dir / "config.yaml"
    user_config.write_text(
        """
version: 1
output_dir: /global-output
sources:
  - path: /existing/project
    name: existing
llm:
  api_key: global-key
""".strip(),
        encoding="utf-8",
    )

    (project_dir / "kblens.yaml").write_text(
        f"""
version: 1
output_dir: {str(project_dir / 'local-output')}
sources:
  - path: {project_dir}
    name: local-source
llm:
  api_key: local-key
summary_language: zh
""".strip(),
        encoding="utf-8",
    )

    monkeypatch.setattr(cli, "USER_CONFIG_DIR", user_config_dir)
    monkeypatch.setattr(cli, "USER_CONFIG_FILE", user_config)
    monkeypatch.setattr(cli, "_set_kb_env_var", lambda output_dir: None)
    monkeypatch.setattr(cli, "require_api_key", lambda config: None)
    monkeypatch.setattr(cli.Path, "home", staticmethod(lambda: home_dir))

    captured: dict[str, object] = {}

    def fake_run_generate(config, source, dry_run):
        captured["config"] = config
        captured["source"] = source
        captured["dry_run"] = dry_run

    monkeypatch.setattr(cli, "_run_generate", fake_run_generate)

    result = _invoke_in_cwd(monkeypatch, project_dir)

    assert result.exit_code == 0
    assert captured["source"] == "local-source"
    assert captured["dry_run"] is False

    config = captured["config"]
    assert config.output_dir == str((project_dir / "local-output").resolve())
    assert config.summary_language == "zh"
    assert len(config.source_dirs) == 1
    assert config.source_dirs[0].name == "local-source"
    assert Path(config.source_dirs[0].path) == project_dir.resolve()

    saved = load_raw_config(user_config)
    saved_sources = saved["sources"]
    assert any(Path(item["path"]).resolve() == project_dir.resolve() for item in saved_sources)


def test_add_kb_existing_source_keeps_existing_name_and_updates(
    tmp_path: Path, monkeypatch
) -> None:
    project_dir = tmp_path / "repo"
    project_dir.mkdir()

    home_dir = tmp_path / "home"
    user_config_dir = home_dir / ".config" / "kblens"
    user_config_dir.mkdir(parents=True)
    user_config = user_config_dir / "config.yaml"
    user_config.write_text(
        f"""
version: 1
output_dir: {str((home_dir / 'kb').resolve())}
sources:
  - path: {project_dir}
    name: tracked-src
llm:
  api_key: test-key
""".strip(),
        encoding="utf-8",
    )

    monkeypatch.setattr(cli, "USER_CONFIG_DIR", user_config_dir)
    monkeypatch.setattr(cli, "USER_CONFIG_FILE", user_config)
    monkeypatch.setattr(cli, "_set_kb_env_var", lambda output_dir: None)
    monkeypatch.setattr(cli, "require_api_key", lambda config: None)
    monkeypatch.setattr(cli.Path, "home", staticmethod(lambda: home_dir))

    captured: dict[str, object] = {}

    def fake_run_generate(config, source, dry_run):
        captured["config"] = config
        captured["source"] = source
        captured["dry_run"] = dry_run

    monkeypatch.setattr(cli, "_run_generate", fake_run_generate)

    result = _invoke_in_cwd(monkeypatch, project_dir)

    assert result.exit_code == 0
    assert "already exists" in result.stdout
    assert captured["source"] == "tracked-src"
    assert captured["dry_run"] is False

    config = captured["config"]
    assert len(config.source_dirs) == 1
    assert config.source_dirs[0].name == "tracked-src"
    assert Path(config.source_dirs[0].path) == project_dir.resolve()

    saved = load_raw_config(user_config)
    assert sum(1 for item in saved["sources"] if item["name"] == "tracked-src") == 1
