from __future__ import annotations

import hashlib
import json
import math
import shutil
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime, timedelta
from functools import cache
from pathlib import Path
from typing import cast

import exchange_calendars as xcals  # type: ignore[import-untyped]
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

import etf_crowding.analysis.signal_bundle as bundle_module
import etf_crowding.analysis.signal_evaluation as evaluation_module
import etf_crowding.data.prices as prices_module
import etf_crowding.signals.momentum as momentum_module
import etf_crowding.signals.volatility as volatility_module
from etf_crowding.analysis import (
    SignalBundleDiscoveryError,
    SignalBundleIntegrityError,
    SignalBundleSelectionError,
    SignalBundleValidationError,
    discover_signal_evaluation_runs,
    evaluate_price_signals,
    load_signal_evaluation_bundle,
    publish_signal_evaluation_bundle,
    resolve_evaluation_target,
)
from etf_crowding.config import ETFDefinition, load_etf_universe
from etf_crowding.data.prices import DownloadStatus, TickerDownloadStatus

RETRIEVED_AT = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
ACQUIRED_AT = pd.Timestamp("2026-08-24T12:00:00.123456789Z")
RETAINED_AT = pd.Timestamp("2026-08-23T11:59:59.987654321Z")
GIT_HEAD = "a" * 40
RUN_ID = "20260824T120002123456Z"
REAL_DAY_9_RUN_ID = "20260825T132408764517Z"


@cache
def _sessions(count: int) -> pd.DatetimeIndex:
    calendar = xcals.get_calendar("XNYS", start="2018-01-01", end="2027-12-31")
    return calendar.sessions[:count]


def _target(session: pd.Timestamp) -> evaluation_module.EvaluationTarget:
    calendar = xcals.get_calendar(
        "XNYS", start=session - timedelta(days=10), end=session + timedelta(days=10)
    )
    return resolve_evaluation_target(calendar.session_close(session))


def _prices(
    sessions: pd.DatetimeIndex,
    tickers: tuple[str, ...] = ("SPY",),
) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for ticker_position, ticker in enumerate(tickers):
        for session_position, session in enumerate(sessions):
            price = (
                100.0
                + 4.0 * ticker_position
                + (0.018 + ticker_position / 100_000) * session_position
                + (0.4 + ticker_position / 100)
                * math.sin(session_position / (5.0 + ticker_position % 4))
            )
            rows.append(
                {
                    "date": session,
                    "ticker": ticker,
                    "open": price,
                    "high": price + 1.0,
                    "low": price - 1.0,
                    "close": price,
                    "adjusted_close": price,
                    "volume": 1_000_000 + session_position,
                    "retrieved_at": pd.Timestamp(RETRIEVED_AT),
                }
            )
    return pd.DataFrame(rows)


def _publish(
    output_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    prices: pd.DataFrame | None = None,
    mode: str = "offline",
    acquisition_statuses: tuple[TickerDownloadStatus, ...] | None = None,
) -> Path:
    universe = load_etf_universe()
    input_prices = _prices(_sessions(505)) if prices is None else prices
    evaluation_arguments: dict[str, object] = {
        "mode": cast(evaluation_module.EvaluationMode, mode)
    }
    if acquisition_statuses is not None:
        evaluation_arguments["acquisition_statuses"] = acquisition_statuses
    evaluation = evaluate_price_signals(
        input_prices,
        universe,
        _target(pd.Timestamp(input_prices["date"].max())),
        **evaluation_arguments,
    )
    monkeypatch.setattr(evaluation_module, "_git_state", lambda root: (GIT_HEAD, False))
    result = publish_signal_evaluation_bundle(
        evaluation,
        output_root,
        command_arguments=("--prices", "synthetic.parquet"),
        creation_time="2026-08-24T12:00:02.123456Z",
        repository_root=output_root.parent,
    )
    return result.bundle_path


@pytest.fixture(scope="module")
def source_bundle(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("verified-bundle") / "bundles"
    monkeypatch = pytest.MonkeyPatch()
    try:
        return _publish(root, monkeypatch)
    finally:
        monkeypatch.undo()


@pytest.fixture
def bundle_root(tmp_path: Path, source_bundle: Path) -> Path:
    root = tmp_path / "bundles"
    shutil.copytree(source_bundle.parent, root)
    return root


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _filesystem_snapshot(bundle_path: Path) -> dict[str, tuple[int, int, str]]:
    return {
        path.name: (path.stat().st_size, path.stat().st_mtime_ns, _hash(path))
        for path in bundle_path.iterdir()
    }


def _manifest(bundle_path: Path) -> dict[str, object]:
    return cast(
        dict[str, object],
        json.loads((bundle_path / "manifest.json").read_text(encoding="utf-8")),
    )


def _write_manifest(bundle_path: Path, manifest: dict[str, object]) -> None:
    (bundle_path / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _replace_artifact(
    bundle_path: Path,
    artifact: str,
    transform: Callable[[pa.Table], pa.Table],
) -> None:
    manifest = _manifest(bundle_path)
    artifacts = cast(dict[str, dict[str, object]], manifest["artifacts"])
    metadata = artifacts[artifact]
    artifact_path = bundle_path / cast(str, metadata["filename"])
    table = transform(pq.read_table(artifact_path))
    pq.write_table(table, artifact_path)
    metadata["row_count"] = table.num_rows
    metadata["sha256"] = _hash(artifact_path)
    metadata["schema"] = [
        {"name": field.name, "nullable": field.nullable, "type": str(field.type)}
        for field in table.schema
    ]
    if artifact == "input_prices":
        manifest["input_row_count"] = table.num_rows
        manifest["input_sha256"] = metadata["sha256"]
    _write_manifest(bundle_path, manifest)


def _replace_column_value(
    table: pa.Table,
    column: str,
    row: int,
    value: object,
) -> pa.Table:
    index = table.column_names.index(column)
    values = table.column(column).to_pylist()
    values[row] = value
    field = table.schema.field(column)
    return table.set_column(index, field, pa.array(values, type=field.type))


def _mutate_acquisition_metadata(
    bundle_path: Path,
    ticker: str,
    *,
    manifest_updates: Mapping[str, object],
    coverage_updates: Mapping[str, object] | None = None,
) -> None:
    if coverage_updates:

        def transform(table: pa.Table) -> pa.Table:
            row = table.column("ticker").to_pylist().index(ticker)
            mutated = table
            for column, value in coverage_updates.items():
                mutated = _replace_column_value(mutated, column, row, value)
            return mutated

        _replace_artifact(bundle_path, "coverage", transform)

    manifest = _manifest(bundle_path)
    metadata = cast(list[dict[str, object]], manifest["ticker_metadata"])
    row = next(item for item in metadata if item["ticker"] == ticker)
    row.update(manifest_updates)
    _write_manifest(bundle_path, manifest)


def _replace_input_provenance(
    bundle_path: Path,
    ticker: str,
    row_positions: tuple[int, ...],
    retrieved_at: pd.Timestamp,
    *,
    replacement_dates: tuple[pd.Timestamp, ...] | None = None,
) -> None:
    if replacement_dates is not None:
        assert len(replacement_dates) == len(row_positions)

    def transform(table: pa.Table) -> pa.Table:
        ticker_rows = [
            row
            for row, value in enumerate(table.column("ticker").to_pylist())
            if value == ticker
        ]
        mutated = table
        for mutation_position, ticker_position in enumerate(row_positions):
            row = ticker_rows[ticker_position]
            mutated = _replace_column_value(mutated, "retrieved_at", row, retrieved_at)
            if replacement_dates is not None:
                mutated = _replace_column_value(
                    mutated,
                    "date",
                    row,
                    replacement_dates[mutation_position],
                )
        return mutated

    _replace_artifact(bundle_path, "input_prices", transform)
    input_prices = pq.read_table(bundle_path / "input_prices.parquet").to_pandas()
    ticker_retrievals = input_prices.loc[
        input_prices["ticker"].eq(ticker), "retrieved_at"
    ]
    first_retrieved_at = pd.Timestamp(ticker_retrievals.min())
    last_retrieved_at = pd.Timestamp(ticker_retrievals.max())
    _mutate_acquisition_metadata(
        bundle_path,
        ticker,
        manifest_updates={
            "input_first_retrieved_at": first_retrieved_at.isoformat().replace(
                "+00:00", "Z"
            ),
            "input_last_retrieved_at": last_retrieved_at.isoformat().replace(
                "+00:00", "Z"
            ),
        },
        coverage_updates={
            "input_first_retrieved_at": first_retrieved_at,
            "input_last_retrieved_at": last_retrieved_at,
        },
    )


def _assert_public_rejection_is_read_only(
    bundle_root: Path,
    *,
    match: str,
    previous: bundle_module.VerifiedSignalEvaluationBundle | None = None,
) -> None:
    bundle_path = bundle_root / RUN_ID
    before_files = _filesystem_snapshot(bundle_path)
    before_children = tuple(sorted(path.name for path in bundle_root.iterdir()))
    previous_state = (
        None
        if previous is None
        else (
            previous.content_sha256,
            previous.manifest_sha256,
            dict(previous.artifact_sha256),
            previous.to_pandas("input_prices"),
            previous.to_pandas("coverage"),
        )
    )

    with pytest.raises(SignalBundleValidationError, match=match):
        load_signal_evaluation_bundle(bundle_root, RUN_ID, previous=previous)

    assert _filesystem_snapshot(bundle_path) == before_files
    assert tuple(sorted(path.name for path in bundle_root.iterdir())) == before_children
    if previous is not None and previous_state is not None:
        assert previous.content_sha256 == previous_state[0]
        assert previous.manifest_sha256 == previous_state[1]
        assert dict(previous.artifact_sha256) == previous_state[2]
        pd.testing.assert_frame_equal(
            previous.to_pandas("input_prices"), previous_state[3]
        )
        pd.testing.assert_frame_equal(previous.to_pandas("coverage"), previous_state[4])


def test_discovery_requires_safe_root_and_only_returns_candidate_names(
    tmp_path: Path,
) -> None:
    with pytest.raises(SignalBundleDiscoveryError):
        discover_signal_evaluation_runs(tmp_path / "missing")

    root = tmp_path / "bundles"
    root.mkdir()
    (root / RUN_ID).mkdir()
    (root / "20260825T120002123456Z").mkdir()
    (root / ".20260826T120002123456Z.tmp-123").mkdir()
    (root / "20260823T120002123456Z.invalid-abc").mkdir()
    (root / "not-a-run").mkdir()
    (root / "20260822T120002123456Z").write_text("not a directory")

    inventory = discover_signal_evaluation_runs(root)

    assert inventory.run_ids == ("20260825T120002123456Z", RUN_ID)
    assert inventory.temporary_names == (".20260826T120002123456Z.tmp-123",)
    assert inventory.quarantined_names == ("20260823T120002123456Z.invalid-abc",)
    assert inventory.rejected_names == ("20260822T120002123456Z", "not-a-run")


def test_discovery_and_selection_reject_reparse_points(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "bundles"
    candidate = root / RUN_ID
    candidate.mkdir(parents=True)
    original = bundle_module._path_is_reparse_point
    monkeypatch.setattr(
        bundle_module,
        "_path_is_reparse_point",
        lambda path: path == candidate or original(path),
    )

    assert discover_signal_evaluation_runs(root).run_ids == ()
    with pytest.raises(SignalBundleSelectionError, match="unsafe"):
        load_signal_evaluation_bundle(root, RUN_ID)


@pytest.mark.parametrize(
    "run_id",
    [
        "",
        "latest",
        "../20260824T120002123456Z",
        "20260824T120002Z",
        ".20260824T120002123456Z.tmp-123",
        "20260824T120002123456Z.invalid-abc",
        123,
    ],
)
def test_selection_requires_exact_direct_child_run_id(
    bundle_root: Path, run_id: object
) -> None:
    with pytest.raises(SignalBundleSelectionError):
        load_signal_evaluation_bundle(bundle_root, cast(str, run_id))


def test_verified_bundle_is_immutable_reusable_and_read_only(bundle_root: Path) -> None:
    bundle_path = bundle_root / RUN_ID
    before = _filesystem_snapshot(bundle_path)

    verified = load_signal_evaluation_bundle(bundle_root, RUN_ID)
    reused = load_signal_evaluation_bundle(bundle_root, RUN_ID, previous=verified)

    assert reused is verified
    assert verified.run_id == RUN_ID
    assert verified.bundle_path == bundle_path.resolve()
    assert verified.manifest_sha256 == _hash(bundle_path / "manifest.json")
    assert len(verified.universe) == 24
    assert verified.to_pandas("coverage")["ticker"].tolist() == [
        definition.ticker for definition in load_etf_universe()
    ]
    with pytest.raises(TypeError):
        cast(dict[str, object], verified.manifest)["run_id"] = "changed"
    frame = verified.to_pandas("coverage")
    frame.loc[0, "name"] = "caller mutation"
    assert verified.to_pandas("coverage").loc[0, "name"] != "caller mutation"
    assert _filesystem_snapshot(bundle_path) == before


def test_changed_bytes_are_never_hidden_by_previous_verified_state(
    bundle_root: Path,
) -> None:
    verified = load_signal_evaluation_bundle(bundle_root, RUN_ID)
    artifact = bundle_root / RUN_ID / "coverage.parquet"
    artifact.write_bytes(artifact.read_bytes() + b"changed")

    with pytest.raises(SignalBundleIntegrityError, match="hash"):
        load_signal_evaluation_bundle(bundle_root, RUN_ID, previous=verified)


def test_loader_does_not_call_forbidden_production_paths(
    bundle_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("forbidden production path called")

    for module, names in (
        (
            evaluation_module,
            (
                "evaluate_price_signals",
                "run_signal_evaluation",
                "publish_signal_evaluation_bundle",
            ),
        ),
        (momentum_module, ("calculate_momentum",)),
        (volatility_module, ("calculate_volatility",)),
        (
            prices_module,
            (
                "download_price_history",
                "persist_price_history",
                "load_price_history",
            ),
        ),
    ):
        for name in names:
            monkeypatch.setattr(module, name, forbidden, raising=False)

    assert load_signal_evaluation_bundle(bundle_root, RUN_ID).run_id == RUN_ID


@pytest.mark.parametrize(
    ("mutation", "error_type"),
    [
        ("missing", SignalBundleIntegrityError),
        ("extra", SignalBundleIntegrityError),
        ("directory", SignalBundleIntegrityError),
        ("truncated", SignalBundleIntegrityError),
        ("malformed_manifest", SignalBundleIntegrityError),
        ("duplicate_manifest_key", SignalBundleIntegrityError),
    ],
)
def test_incomplete_or_malformed_file_sets_fail_closed(
    bundle_root: Path,
    mutation: str,
    error_type: type[Exception],
) -> None:
    bundle_path = bundle_root / RUN_ID
    if mutation == "missing":
        (bundle_path / "coverage.parquet").unlink()
    elif mutation == "extra":
        (bundle_path / "extra.txt").write_text("extra")
    elif mutation == "directory":
        (bundle_path / "coverage.parquet").unlink()
        (bundle_path / "coverage.parquet").mkdir()
    elif mutation == "truncated":
        (bundle_path / "coverage.parquet").write_bytes(b"PAR1")
    elif mutation == "malformed_manifest":
        (bundle_path / "manifest.json").write_text("{")
    else:
        (bundle_path / "manifest.json").write_text('{"run_id":"a","run_id":"b"}')

    with pytest.raises(error_type):
        load_signal_evaluation_bundle(bundle_root, RUN_ID)


@pytest.mark.parametrize(
    "field",
    [
        "run_id",
        "git_head",
        "worktree_dirty",
        "universe_config_sha256",
        "target_xnys_session",
        "request_start",
        "request_end_exclusive",
        "captured_utc_reference_instant",
        "created_at",
        "mode",
        "ticker_metadata",
        "package_versions",
        "artifacts",
    ],
)
def test_manifest_contract_failures_are_rejected(bundle_root: Path, field: str) -> None:
    manifest = _manifest(bundle_root / RUN_ID)
    if field in {"worktree_dirty", "ticker_metadata", "package_versions", "artifacts"}:
        manifest[field] = "invalid"
    elif field == "git_head":
        manifest[field] = "bad"
    elif field == "run_id":
        manifest[field] = "20260825T120002123456Z"
    elif field == "universe_config_sha256":
        manifest[field] = "0" * 64
    elif field == "target_xnys_session":
        manifest[field] = "2020-01-02"
    elif field == "request_start":
        manifest[field] = "2018-01-02"
    elif field == "request_end_exclusive":
        manifest[field] = "2020-01-03"
    elif field == "captured_utc_reference_instant":
        manifest[field] = "2020-01-03T20:59:00Z"
    elif field == "created_at":
        manifest[field] = "2026-08-24T12:00:02Z"
    else:
        manifest[field] = "refresh"
    _write_manifest(bundle_root / RUN_ID, manifest)

    with pytest.raises(SignalBundleValidationError):
        load_signal_evaluation_bundle(bundle_root, RUN_ID)


@pytest.mark.parametrize(
    "metadata_field",
    ["filename", "row_count", "sha256", "schema"],
)
def test_artifact_manifest_mismatches_are_rejected(
    bundle_root: Path, metadata_field: str
) -> None:
    manifest = _manifest(bundle_root / RUN_ID)
    metadata = cast(
        dict[str, object], cast(dict[str, object], manifest["artifacts"])["coverage"]
    )
    replacements: dict[str, object] = {
        "filename": "momentum.parquet",
        "row_count": 23,
        "sha256": "0" * 64,
        "schema": [],
    }
    metadata[metadata_field] = replacements[metadata_field]
    _write_manifest(bundle_root / RUN_ID, manifest)

    with pytest.raises((SignalBundleIntegrityError, SignalBundleValidationError)):
        load_signal_evaluation_bundle(bundle_root, RUN_ID)


@pytest.mark.parametrize(
    ("artifact", "column", "row", "value"),
    [
        ("coverage", "ticker", 0, "QQQ"),
        ("coverage", "momentum_target_status", 0, "missing_endpoint_row"),
        ("coverage", "price_staleness_sessions", 0, 12),
        ("momentum", "endpoint_status", -1, "missing_endpoint_row"),
        ("volatility", "window_status", -1, "missing_window_row"),
        ("dependence", "pair_count", 0, 999),
        ("dependence", "universe_status", 0, "full_configured_universe"),
    ],
)
def test_rehashed_semantic_contradictions_are_rejected(
    bundle_root: Path,
    artifact: str,
    column: str,
    row: int,
    value: object,
) -> None:
    _replace_artifact(
        bundle_root / RUN_ID,
        artifact,
        lambda table: _replace_column_value(table, column, row, value),
    )

    with pytest.raises(SignalBundleValidationError):
        load_signal_evaluation_bundle(bundle_root, RUN_ID)


def test_rehashed_unsorted_and_duplicate_keys_are_rejected(bundle_root: Path) -> None:
    def reverse(table: pa.Table) -> pa.Table:
        return table.take(pa.array(list(reversed(range(table.num_rows)))))

    _replace_artifact(bundle_root / RUN_ID, "momentum", reverse)
    with pytest.raises(SignalBundleValidationError):
        load_signal_evaluation_bundle(bundle_root, RUN_ID)


def test_rehashed_incompatible_arrow_schema_is_rejected(bundle_root: Path) -> None:
    _replace_artifact(
        bundle_root / RUN_ID,
        "coverage",
        lambda table: table.drop(["category"]),
    )

    with pytest.raises(SignalBundleValidationError):
        load_signal_evaluation_bundle(bundle_root, RUN_ID)


def test_rehashed_unmasked_nonfinite_value_is_rejected(bundle_root: Path) -> None:
    def inject_nan(table: pa.Table) -> pa.Table:
        column = "momentum_percentile"
        index = table.column_names.index(column)
        values = table.column(column).to_pylist()
        row = next(
            position for position, value in enumerate(values) if value is not None
        )
        values[row] = float("nan")
        field = table.schema.field(column)
        array = pa.array(values, type=field.type, from_pandas=False)
        assert array[row].is_valid
        return table.set_column(index, field, array)

    _replace_artifact(bundle_root / RUN_ID, "momentum", inject_nan)
    with pytest.raises(SignalBundleValidationError):
        load_signal_evaluation_bundle(bundle_root, RUN_ID)


def test_rehashed_diagnostic_list_and_included_order_failures_are_rejected(
    bundle_root: Path,
) -> None:
    def alter_list(table: pa.Table) -> pa.Table:
        column = "interior_missing_row_dates"
        row = table.num_rows - 1
        return _replace_column_value(table, column, row, [pd.Timestamp("2019-01-02")])

    _replace_artifact(bundle_root / RUN_ID, "momentum", alter_list)
    with pytest.raises(SignalBundleValidationError):
        load_signal_evaluation_bundle(bundle_root, RUN_ID)


def test_rehashed_included_ticker_order_is_rejected(bundle_root: Path) -> None:
    def reverse_included(table: pa.Table) -> pa.Table:
        column = "included_tickers"
        values = table.column(column).to_pylist()
        row = 0
        values[row] = ["QQQ", "SPY"]
        index = table.column_names.index(column)
        field = table.schema.field(column)
        return table.set_column(index, field, pa.array(values, type=field.type))

    _replace_artifact(bundle_root / RUN_ID, "dependence", reverse_included)
    with pytest.raises(SignalBundleValidationError):
        load_signal_evaluation_bundle(bundle_root, RUN_ID)


def test_nullable_values_and_integers_above_float_exactness_are_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions = _sessions(2)
    large = 9_007_199_254_740_993
    prices = pd.DataFrame(
        {
            "date": sessions,
            "ticker": pd.Series(["SPY", "SPY"], dtype="string"),
            "open": pd.Series([large, large + 1], dtype="UInt64"),
            "high": pd.Series([large + 2, large + 3], dtype="UInt64"),
            "low": pd.Series([large - 2, large - 1], dtype="UInt64"),
            "close": pd.Series([large, large + 1], dtype="UInt64"),
            "adjusted_close": pd.Series([large, large + 1], dtype="UInt64"),
            "volume": pd.Series([large, pd.NA], dtype="UInt64"),
            "retrieved_at": pd.Series(
                [pd.Timestamp(RETRIEVED_AT)] * 2, dtype="datetime64[ns, UTC]"
            ),
        }
    )
    root = tmp_path / "bundles"
    _publish(root, monkeypatch, prices=prices)

    verified = load_signal_evaluation_bundle(root, RUN_ID)
    loaded = verified.to_pandas("input_prices")

    assert loaded.loc[0, "open"] == large
    assert loaded.loc[1, "volume"] is pd.NA


def _status(
    definition: ETFDefinition,
    status: DownloadStatus,
    target: evaluation_module.EvaluationTarget,
    returned_dates: tuple[date, ...] = (),
    retrieved_at: pd.Timestamp = pd.Timestamp(RETRIEVED_AT),
) -> TickerDownloadStatus:
    return TickerDownloadStatus(
        ticker=definition.ticker,
        status=status,
        rows_received=len(returned_dates),
        first_date=returned_dates[0] if returned_dates else None,
        last_date=returned_dates[-1] if returned_dates else None,
        retrieved_at=None if status == "failed" else retrieved_at,
        query_start=target.request_start,
        query_end=target.request_end,
        returned_dates=returned_dates,
        error="synthetic failure" if status == "failed" else None,
    )


@pytest.fixture(scope="module")
def source_refresh_bundle(tmp_path_factory: pytest.TempPathFactory) -> Path:
    sessions = _sessions(30)
    acquired_sessions = sessions[-5:]
    target = _target(sessions[-1])
    universe = load_etf_universe()
    input_prices = _prices(sessions, ("SPY", "QQQ", "IWM"))
    input_prices["retrieved_at"] = pd.Series(
        [RETAINED_AT] * len(input_prices), dtype="datetime64[ns, UTC]"
    )
    spy_acquired = input_prices["ticker"].eq("SPY") & input_prices["date"].isin(
        acquired_sessions
    )
    input_prices.loc[spy_acquired, "retrieved_at"] = ACQUIRED_AT
    acquired_dates = tuple(session.date() for session in acquired_sessions)
    statuses = tuple(
        _status(
            definition,
            cast(
                DownloadStatus,
                "success"
                if definition.ticker == "SPY"
                else "empty"
                if definition.ticker == "QQQ"
                else "failed",
            ),
            target,
            acquired_dates if definition.ticker == "SPY" else (),
            retrieved_at=ACQUIRED_AT,
        )
        for definition in universe
    )
    root = tmp_path_factory.mktemp("refresh-bundle") / "bundles"
    monkeypatch = pytest.MonkeyPatch()
    try:
        return _publish(
            root,
            monkeypatch,
            prices=input_prices,
            mode="refresh",
            acquisition_statuses=statuses,
        )
    finally:
        monkeypatch.undo()


@pytest.fixture
def refresh_bundle_root(tmp_path: Path, source_refresh_bundle: Path) -> Path:
    root = tmp_path / "bundles"
    shutil.copytree(source_refresh_bundle.parent, root)
    return root


def test_absent_empty_failed_and_success_acquisition_states_are_supported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions = _sessions(30)
    target = _target(sessions[-1])
    dates = tuple(session.date() for session in sessions)
    universe = load_etf_universe()
    statuses = tuple(
        _status(
            definition,
            cast(
                DownloadStatus,
                "success"
                if definition.ticker == "SPY"
                else "empty"
                if definition.ticker == "QQQ"
                else "failed",
            ),
            target,
            dates if definition.ticker == "SPY" else (),
        )
        for definition in universe
    )
    root = tmp_path / "bundles"
    _publish(
        root,
        monkeypatch,
        prices=_prices(sessions),
        mode="refresh",
        acquisition_statuses=statuses,
    )

    coverage = load_signal_evaluation_bundle(root, RUN_ID).to_pandas("coverage")
    indexed = coverage.set_index("ticker")

    assert indexed.loc["SPY", "acquisition_status"] == "success"
    assert indexed.loc["QQQ", "acquisition_status"] == "empty"
    assert indexed.loc["IWM", "acquisition_status"] == "failed"
    assert indexed.loc["IWM", "momentum_target_status"] == "ticker_unavailable"
    assert pd.isna(indexed.loc["IWM", "momentum_target_raw"])


def test_successful_acquisition_provenance_matches_canonical_rows(
    refresh_bundle_root: Path,
) -> None:
    verified = load_signal_evaluation_bundle(refresh_bundle_root, RUN_ID)
    coverage = verified.to_pandas("coverage").set_index("ticker")
    prices = verified.to_pandas("input_prices")
    acquired = prices.loc[
        prices["ticker"].eq("SPY") & prices["retrieved_at"].eq(ACQUIRED_AT)
    ]

    assert len(coverage) == 24
    assert coverage.loc["SPY", "acquisition_status"] == "success"
    assert coverage.loc["SPY", "acquisition_rows_received"] == 5
    assert len(acquired) == 5
    assert acquired["date"].min() == _sessions(30)[-5]
    assert acquired["date"].max() == _sessions(30)[-1]
    assert acquired["retrieved_at"].dtype == "datetime64[ns, UTC]"


def test_partial_acquisition_and_retained_history_remain_valid(
    refresh_bundle_root: Path,
) -> None:
    verified = load_signal_evaluation_bundle(refresh_bundle_root, RUN_ID)
    coverage = verified.to_pandas("coverage").set_index("ticker")
    prices = verified.to_pandas("input_prices")

    assert coverage.loc["SPY", "present_xnys_observation_count"] == 30
    assert coverage.loc["SPY", "acquisition_rows_received"] == 5
    assert (
        coverage.loc["SPY", "acquisition_retrieved_at"]
        == coverage.loc["QQQ", "acquisition_retrieved_at"]
        == ACQUIRED_AT
    )
    assert prices.loc[
        prices["ticker"].eq("SPY"), "retrieved_at"
    ].value_counts().to_dict() == {RETAINED_AT: 25, ACQUIRED_AT: 5}
    assert coverage.loc["QQQ", "acquisition_status"] == "empty"
    assert coverage.loc["QQQ", "present_xnys_observation_count"] == 30
    assert coverage.loc["QQQ", "acquisition_retrieved_at"] == ACQUIRED_AT
    assert prices.loc[
        prices["ticker"].eq("QQQ"), "retrieved_at"
    ].value_counts().to_dict() == {RETAINED_AT: 30}
    assert coverage.loc["IWM", "acquisition_status"] == "failed"
    assert coverage.loc["IWM", "present_xnys_observation_count"] == 30
    assert pd.isna(coverage.loc["IWM", "acquisition_retrieved_at"])
    assert prices.loc[
        prices["ticker"].eq("IWM"), "retrieved_at"
    ].value_counts().to_dict() == {RETAINED_AT: 30}


def test_empty_acquisition_rejects_coordinated_matching_canonical_row(
    refresh_bundle_root: Path,
) -> None:
    previous = load_signal_evaluation_bundle(refresh_bundle_root, RUN_ID)
    bundle_path = refresh_bundle_root / RUN_ID
    _replace_input_provenance(bundle_path, "QQQ", (0,), ACQUIRED_AT)
    manifest = _manifest(bundle_path)
    artifacts = cast(dict[str, dict[str, object]], manifest["artifacts"])
    input_metadata = artifacts["input_prices"]
    ticker_metadata = cast(list[dict[str, object]], manifest["ticker_metadata"])
    qqq_metadata = next(row for row in ticker_metadata if row["ticker"] == "QQQ")
    coverage = pq.read_table(bundle_path / "coverage.parquet").to_pandas()
    qqq_coverage = coverage.loc[coverage["ticker"].eq("QQQ")].iloc[0]

    assert input_metadata["sha256"] == _hash(bundle_path / "input_prices.parquet")
    assert manifest["input_sha256"] == input_metadata["sha256"]
    assert qqq_metadata["input_first_retrieved_at"] == (
        RETAINED_AT.isoformat().replace("+00:00", "Z")
    )
    assert qqq_metadata["input_last_retrieved_at"] == (
        ACQUIRED_AT.isoformat().replace("+00:00", "Z")
    )
    assert qqq_coverage["input_first_retrieved_at"] == RETAINED_AT
    assert qqq_coverage["input_last_retrieved_at"] == ACQUIRED_AT
    _assert_public_rejection_is_read_only(
        refresh_bundle_root,
        match="Empty acquisition provenance for QQQ claims zero canonical rows",
        previous=previous,
    )


def test_empty_acquisition_rejects_multiple_matching_canonical_rows(
    refresh_bundle_root: Path,
) -> None:
    _replace_input_provenance(refresh_bundle_root / RUN_ID, "QQQ", (0, 1), ACQUIRED_AT)

    _assert_public_rejection_is_read_only(
        refresh_bundle_root,
        match="but 2 matching rows exist",
    )


def test_empty_acquisition_rejects_matching_row_outside_request_window(
    refresh_bundle_root: Path,
) -> None:
    _replace_input_provenance(
        refresh_bundle_root / RUN_ID,
        "QQQ",
        (0,),
        ACQUIRED_AT,
        replacement_dates=(pd.Timestamp("2017-12-29"),),
    )

    _assert_public_rejection_is_read_only(
        refresh_bundle_root,
        match="Empty acquisition provenance for QQQ claims canonical rows .* outside",
    )


def test_empty_acquisition_timestamp_matching_is_nanosecond_exact(
    refresh_bundle_root: Path,
) -> None:
    one_nanosecond_earlier = ACQUIRED_AT - pd.Timedelta(1, unit="ns")
    _replace_input_provenance(
        refresh_bundle_root / RUN_ID,
        "QQQ",
        (0,),
        one_nanosecond_earlier,
    )

    verified = load_signal_evaluation_bundle(refresh_bundle_root, RUN_ID)
    prices = verified.to_pandas("input_prices")
    qqq_first = prices.loc[prices["ticker"].eq("QQQ")].iloc[0]

    assert qqq_first["retrieved_at"] == one_nanosecond_earlier
    assert qqq_first["retrieved_at"] != ACQUIRED_AT


def test_zero_row_success_remains_rejected(
    refresh_bundle_root: Path,
) -> None:
    _mutate_acquisition_metadata(
        refresh_bundle_root / RUN_ID,
        "SPY",
        manifest_updates={
            "rows_received": 0,
            "first_returned_date": None,
            "last_returned_date": None,
        },
        coverage_updates={"acquisition_rows_received": 0},
    )

    _assert_public_rejection_is_read_only(
        refresh_bundle_root,
        match="Successful acquisition metadata is invalid for SPY",
    )


def test_coordinated_success_row_count_contradiction_is_rejected_read_only(
    refresh_bundle_root: Path,
) -> None:
    _mutate_acquisition_metadata(
        refresh_bundle_root / RUN_ID,
        "SPY",
        manifest_updates={"rows_received": 1},
        coverage_updates={"acquisition_rows_received": 1},
    )

    _assert_public_rejection_is_read_only(
        refresh_bundle_root,
        match="Successful acquisition provenance for SPY claims 1 canonical rows",
    )


def test_coordinated_incorrect_first_returned_date_is_rejected(
    refresh_bundle_root: Path,
) -> None:
    wrong_first = _sessions(30)[-4].date().isoformat()
    _mutate_acquisition_metadata(
        refresh_bundle_root / RUN_ID,
        "SPY",
        manifest_updates={"first_returned_date": wrong_first},
    )

    _assert_public_rejection_is_read_only(
        refresh_bundle_root,
        match="Successful acquisition first returned date for SPY",
    )


def test_coordinated_incorrect_last_returned_date_is_rejected(
    refresh_bundle_root: Path,
) -> None:
    wrong_last = _sessions(30)[-2].date().isoformat()
    _mutate_acquisition_metadata(
        refresh_bundle_root / RUN_ID,
        "SPY",
        manifest_updates={"last_returned_date": wrong_last},
    )

    _assert_public_rejection_is_read_only(
        refresh_bundle_root,
        match="Successful acquisition last returned date for SPY",
    )


def test_coordinated_incorrect_retrieval_timestamp_is_rejected(
    refresh_bundle_root: Path,
) -> None:
    wrong_retrieved_at = ACQUIRED_AT - pd.Timedelta(1, unit="ns")
    _mutate_acquisition_metadata(
        refresh_bundle_root / RUN_ID,
        "SPY",
        manifest_updates={
            "retrieved_at": wrong_retrieved_at.isoformat().replace("+00:00", "Z")
        },
        coverage_updates={"acquisition_retrieved_at": wrong_retrieved_at},
    )

    _assert_public_rejection_is_read_only(
        refresh_bundle_root,
        match="Successful acquisition provenance for SPY claims 5 canonical rows",
    )


@pytest.mark.parametrize(
    ("ticker", "manifest_updates", "coverage_updates"),
    [
        (
            "QQQ",
            {"rows_received": 1},
            {"acquisition_rows_received": 1},
        ),
        (
            "IWM",
            {"retrieved_at": ACQUIRED_AT.isoformat().replace("+00:00", "Z")},
            {"acquisition_retrieved_at": ACQUIRED_AT},
        ),
    ],
)
def test_empty_and_failed_acquisition_contracts_remain_fail_closed(
    refresh_bundle_root: Path,
    ticker: str,
    manifest_updates: dict[str, object],
    coverage_updates: dict[str, object],
) -> None:
    _mutate_acquisition_metadata(
        refresh_bundle_root / RUN_ID,
        ticker,
        manifest_updates=manifest_updates,
        coverage_updates=coverage_updates,
    )

    _assert_public_rejection_is_read_only(
        refresh_bundle_root,
        match=f"{ticker}",
    )


def test_real_day_9_bundle_passes_public_consumer_unchanged() -> None:
    root = Path(__file__).parents[1] / "data" / "processed" / "signal_evaluations"
    bundle_path = root / REAL_DAY_9_RUN_ID
    if not bundle_path.is_dir():
        pytest.skip("Local Git-ignored Day 9 bundle is unavailable.")
    before_files = _filesystem_snapshot(bundle_path)
    before_children = tuple(sorted(path.name for path in root.iterdir()))

    verified = load_signal_evaluation_bundle(root, REAL_DAY_9_RUN_ID)

    assert verified.run_id == REAL_DAY_9_RUN_ID
    assert len(verified.to_pandas("coverage")) == 24
    assert _filesystem_snapshot(bundle_path) == before_files
    assert tuple(sorted(path.name for path in root.iterdir())) == before_children
