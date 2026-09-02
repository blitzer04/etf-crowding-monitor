"""Regression tests for the read-only hosted CI workflow."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

WORKFLOW_PATH = Path(__file__).parents[1] / ".github" / "workflows" / "ci.yml"
CHECKOUT_SHA = "3d3c42e5aac5ba805825da76410c181273ba90b1"
SETUP_PYTHON_SHA = "5fda3b95a4ea91299a34e894583c3862153e4b97"


class GitHubActionsLoader(yaml.SafeLoader):
    """Load YAML 1.2-style booleans without converting the Actions `on` key."""


GitHubActionsLoader.yaml_implicit_resolvers = {
    key: [
        (tag, pattern) for tag, pattern in resolvers if tag != "tag:yaml.org,2002:bool"
    ]
    for key, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
GitHubActionsLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool",
    re.compile(r"^(?:true|false)$", re.IGNORECASE),
    list("tTfF"),
)


def _load_workflow() -> tuple[dict[str, Any], str]:
    text = WORKFLOW_PATH.read_text(encoding="utf-8")
    workflow = yaml.load(text, Loader=GitHubActionsLoader)
    assert isinstance(workflow, dict)
    return workflow, text


def _quality_job(workflow: dict[str, Any]) -> dict[str, Any]:
    jobs = workflow["jobs"]
    assert set(jobs) == {"quality"}
    return jobs["quality"]


def _all_mapping_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return {str(key) for key in value} | {
            nested_key
            for nested_value in value.values()
            for nested_key in _all_mapping_keys(nested_value)
        }
    if isinstance(value, list):
        return {nested_key for item in value for nested_key in _all_mapping_keys(item)}
    return set()


def test_ci_triggers_and_permissions_are_exactly_read_only() -> None:
    workflow, _ = _load_workflow()

    assert workflow["name"] == "CI"
    assert workflow["on"] == {
        "pull_request": {"branches": ["main"]},
        "push": {"branches": ["main"]},
        "workflow_dispatch": None,
    }
    assert not {
        "schedule",
        "pull_request_target",
        "workflow_run",
    } & set(workflow["on"])
    assert workflow["permissions"] == {"contents": "read"}
    assert "permissions" not in _quality_job(workflow)
    assert "environment" not in _all_mapping_keys(workflow)


def test_ci_matrix_runtime_and_timeout_contract() -> None:
    workflow, _ = _load_workflow()
    job = _quality_job(workflow)

    assert workflow["env"] == {"PYTHONDONTWRITEBYTECODE": "1"}
    assert job["runs-on"] == "${{ matrix.os }}"
    assert job["timeout-minutes"] == 30
    assert job["strategy"] == {
        "fail-fast": False,
        "matrix": {"os": ["windows-latest", "ubuntu-latest"]},
    }


def test_ci_job_and_required_steps_cannot_be_skipped_or_fail_open() -> None:
    workflow, _ = _load_workflow()
    job = _quality_job(workflow)

    assert set(workflow) == {"name", "on", "permissions", "env", "jobs"}
    assert "if" not in job
    assert "continue-on-error" not in job
    assert set(job) == {"runs-on", "timeout-minutes", "strategy", "steps"}
    steps = job["steps"]
    assert steps
    for step in steps:
        assert "if" not in step
        assert "continue-on-error" not in step
        has_uses = "uses" in step
        has_run = "run" in step
        assert has_uses is not has_run
        if has_uses:
            assert set(step) == {"name", "uses", "with"}
        else:
            assert set(step) == {"name", "run"}


def test_ci_actions_are_official_immutable_releases() -> None:
    workflow, text = _load_workflow()
    steps = _quality_job(workflow)["steps"]
    action_steps = [step for step in steps if "uses" in step]
    expected = {
        "actions/checkout": (CHECKOUT_SHA, "v7.0.1"),
        "actions/setup-python": (SETUP_PYTHON_SHA, "v7.0.0"),
    }

    assert len(action_steps) == len(expected)
    for step in action_steps:
        match = re.fullmatch(
            r"(actions/(?:checkout|setup-python))@([0-9a-f]{40})",
            step["uses"],
        )
        assert match is not None
        identity, sha = match.groups()
        expected_sha, version = expected[identity]
        assert sha == expected_sha
        version_pattern = (
            rf"^\s*uses:\s*{re.escape(step['uses'])}\s+#\s+"
            rf"{re.escape(version)}\s*$"
        )
        assert re.search(version_pattern, text, re.MULTILINE)

    checkout_step = next(
        step for step in action_steps if step["uses"].startswith("actions/checkout@")
    )
    setup_step = next(
        step
        for step in action_steps
        if step["uses"].startswith("actions/setup-python@")
    )
    assert checkout_step["with"] == {"persist-credentials": False}
    assert setup_step["with"] == {"python-version": "3.12"}


def test_ci_commands_are_exact_offline_quality_gates() -> None:
    workflow, text = _load_workflow()
    steps = _quality_job(workflow)["steps"]
    commands = [step["run"] for step in steps if "run" in step]

    assert commands == [
        'python -m pip install -e ".[dev]"',
        "python -m pip check",
        "python -m ruff check --no-cache .",
        "python -m ruff format --check --no-cache .",
        (
            "python -m mypy --no-incremental --cache-dir "
            '"${{ runner.temp }}/mypy" src/etf_crowding app'
        ),
        (
            "python -m pytest -W error -q -p no:cacheprovider --basetemp "
            '"${{ runner.temp }}/pytest"'
        ),
    ]

    command_text = "\n".join(commands).lower()
    prohibited = (
        "${{ secrets.",
        "yfinance",
        "yahoo",
        "update_prices",
        "update_shares",
        "evaluate_price_signals",
        "--refresh",
        "streamlit",
        "browser",
        "playwright",
        "deploy",
        "upload-artifact",
        "download-artifact",
        "curl",
        "wget",
        "data/",
        "data\\",
        ".parquet",
        "signal_evaluations",
        "snapshot",
        "bundle",
    )
    assert all(token not in command_text for token in prohibited)
    assert re.search(r"\bgit(?:\.exe)?\s+", command_text) is None
    assert "secrets" not in _all_mapping_keys(workflow)
    assert "${{ secrets." not in text.lower()
