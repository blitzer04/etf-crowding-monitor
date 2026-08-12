"""Tests for explicit repository path resolution."""

from pathlib import Path

import pytest

import etf_crowding.paths as project_paths


def test_source_checkout_path_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(project_paths.PROJECT_ROOT_ENV_VAR, raising=False)
    expected_root = Path(__file__).resolve().parents[1]

    assert project_paths.get_project_root() == expected_root
    assert project_paths.get_data_dir() == expected_root / "data"
    assert project_paths.get_raw_data_dir() == expected_root / "data" / "raw"
    assert project_paths.get_processed_data_dir() == (
        expected_root / "data" / "processed"
    )
    assert project_paths.get_snapshot_data_dir() == (
        expected_root / "data" / "snapshots"
    )
    assert project_paths.get_docs_dir() == expected_root / "docs"


def test_project_root_environment_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured_root = tmp_path / "configured-project"
    configured_root.mkdir()
    monkeypatch.setenv(project_paths.PROJECT_ROOT_ENV_VAR, str(configured_root))

    assert project_paths.get_project_root() == configured_root.resolve()
    assert project_paths.get_data_dir() == configured_root.resolve() / "data"


def test_installed_layout_does_not_create_fake_repository_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installed_module = tmp_path / "Lib" / "site-packages" / "etf_crowding" / "paths.py"
    installed_module.parent.mkdir(parents=True)
    installed_module.touch()
    (tmp_path / "Lib" / "pyproject.toml").touch()
    monkeypatch.delenv(project_paths.PROJECT_ROOT_ENV_VAR, raising=False)
    monkeypatch.setattr(project_paths, "_MODULE_PATH", installed_module)

    with pytest.raises(RuntimeError, match="Project root is unavailable"):
        project_paths.get_project_root()

    with pytest.raises(RuntimeError, match="Project root is unavailable"):
        project_paths.get_data_dir()


def test_invalid_project_root_override_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing_root = tmp_path / "missing"
    monkeypatch.setenv(project_paths.PROJECT_ROOT_ENV_VAR, str(missing_root))

    with pytest.raises(RuntimeError, match="does not identify an existing directory"):
        project_paths.get_project_root()
