"""Explicit repository paths for local scripts and application workflows."""

import os
from pathlib import Path

PROJECT_ROOT_ENV_VAR = "ETF_CROWDING_PROJECT_ROOT"
_MODULE_PATH = Path(__file__).resolve()


def _source_checkout_root(module_path: Path) -> Path | None:
    candidate = module_path.parents[2]
    expected_module = candidate / "src" / "etf_crowding" / "paths.py"
    project_marker = candidate / "pyproject.toml"

    if project_marker.is_file() and expected_module.resolve() == module_path.resolve():
        return candidate
    return None


def get_project_root() -> Path:
    """Return the explicitly configured or verified source-checkout root.

    The ``ETF_CROWDING_PROJECT_ROOT`` environment variable takes precedence.
    Without it, the function recognizes only the repository's expected
    ``src/etf_crowding`` layout with a root ``pyproject.toml`` marker. Installed
    package locations are never treated as repository roots.

    Returns:
        The resolved project root.

    Raises:
        RuntimeError: If the environment override is empty, does not identify a
            directory, or no source-checkout root can be verified.
    """

    configured_root = os.getenv(PROJECT_ROOT_ENV_VAR)
    if configured_root is not None:
        if not configured_root.strip():
            raise RuntimeError(f"{PROJECT_ROOT_ENV_VAR} must not be empty.")

        project_root = Path(configured_root).expanduser().resolve()
        if not project_root.is_dir():
            raise RuntimeError(
                f"{PROJECT_ROOT_ENV_VAR} does not identify an existing directory: "
                f"'{project_root}'."
            )
        return project_root

    source_root = _source_checkout_root(_MODULE_PATH)
    if source_root is not None:
        return source_root

    raise RuntimeError(
        "Project root is unavailable outside a verified source checkout. "
        f"Set {PROJECT_ROOT_ENV_VAR} to an existing project directory."
    )


def get_data_dir() -> Path:
    """Return the configured project's data directory.

    Returns:
        The ``data`` directory below the resolved project root.

    Raises:
        RuntimeError: If the project root cannot be resolved.
    """

    return get_project_root() / "data"


def get_raw_data_dir() -> Path:
    """Return the configured project's raw-data directory.

    Returns:
        The ``data/raw`` directory below the resolved project root.

    Raises:
        RuntimeError: If the project root cannot be resolved.
    """

    return get_data_dir() / "raw"


def get_processed_data_dir() -> Path:
    """Return the configured project's processed-data directory.

    Returns:
        The ``data/processed`` directory below the resolved project root.

    Raises:
        RuntimeError: If the project root cannot be resolved.
    """

    return get_data_dir() / "processed"


def get_snapshot_data_dir() -> Path:
    """Return the configured project's snapshot-data directory.

    Returns:
        The ``data/snapshots`` directory below the resolved project root.

    Raises:
        RuntimeError: If the project root cannot be resolved.
    """

    return get_data_dir() / "snapshots"


def get_docs_dir() -> Path:
    """Return the configured project's documentation directory.

    Returns:
        The ``docs`` directory below the resolved project root.

    Raises:
        RuntimeError: If the project root cannot be resolved.
    """

    return get_project_root() / "docs"
