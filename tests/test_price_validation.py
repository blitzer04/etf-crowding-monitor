"""Tests for canonical ETF price validation and persistence safeguards."""

import errno
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal

import numpy as np
import pandas as pd
import pyarrow as pa
import pytest
from pandas.api.types import is_any_real_numeric_dtype

import etf_crowding.data.prices as price_module
from etf_crowding.data.prices import TickerDownloadStatus, persist_price_history
from etf_crowding.data.validation import (
    PRICE_VALUE_COLUMNS,
    PriceDataValidationError,
    deduplicate_price_data,
    validate_price_data,
)

RETRIEVED_AT = datetime(2026, 8, 13, 12, 30, tzinfo=UTC)
NEW_RETRIEVED_AT = pd.Timestamp("2026-08-14T12:30:00Z")
SNAPSHOT_TIME = datetime(2026, 8, 14, 13, 0, tzinfo=UTC)
ARROW_DECIMAL_DTYPE = pd.ArrowDtype(pa.decimal128(20, 2))


def _canonical_prices() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": pd.to_datetime(["2024-01-02", "2024-01-03"]),
            "ticker": pd.Series(["SPY", "SPY"], dtype="string"),
            "open": [100.0, 102.0],
            "high": [103.0, 105.0],
            "low": [99.0, 101.0],
            "close": [102.0, 104.0],
            "adjusted_close": [101.5, 103.5],
            "volume": [1_000_000.0, 1_200_000.0],
            "retrieved_at": pd.to_datetime([RETRIEVED_AT, RETRIEVED_AT], utc=True),
        }
    )


def _with_duplicate_column_label(prices: pd.DataFrame, column: str) -> pd.DataFrame:
    return pd.concat([prices, prices[[column]].copy()], axis="columns")


def _duplicate_prices_with_malformed_ticker(value: object) -> pd.DataFrame:
    duplicated = pd.concat(
        [_canonical_prices().iloc[[0]], _canonical_prices().iloc[[0]]],
        ignore_index=True,
    )
    ticker_values = np.empty(2, dtype=object)
    ticker_values[:] = [value, value]
    duplicated["ticker"] = pd.Series(ticker_values, dtype="object")
    return duplicated


def _appended_price_row(
    date_value: str,
    *,
    retrieved_at: pd.Timestamp = NEW_RETRIEVED_AT,
) -> pd.DataFrame:
    row = _canonical_prices().iloc[[0]].copy()
    row["date"] = pd.Timestamp(date_value)
    row["retrieved_at"] = retrieved_at
    return row


def _price_row_with_numeric_dtype(
    date_value: str,
    dtype: str,
    *,
    retrieved_at: pd.Timestamp | datetime = RETRIEVED_AT,
    adjusted_close: object = 101.5,
) -> pd.DataFrame:
    row = _canonical_prices().iloc[[0]].copy()
    row["date"] = pd.Timestamp(date_value)
    values: dict[str, object] = {
        "open": 100.0,
        "high": 103.0,
        "low": 99.0,
        "close": 102.0,
        "adjusted_close": adjusted_close,
        "volume": 1_000_000.0,
    }
    for column, value in values.items():
        row[column] = pd.Series([value], index=row.index, dtype=dtype)
    row["retrieved_at"] = retrieved_at
    return row


def _large_integer_price_row(
    date_value: str,
    value: int,
    dtype: str,
    *,
    retrieved_at: pd.Timestamp | datetime = RETRIEVED_AT,
) -> pd.DataFrame:
    row = _canonical_prices().iloc[[0]].copy()
    row["date"] = pd.Timestamp(date_value)
    for column in PRICE_VALUE_COLUMNS:
        row[column] = pd.Series([value], index=row.index, dtype=dtype)
    row["retrieved_at"] = retrieved_at
    return row


def _retrieval_status(
    ticker: str,
    status: Literal["success", "empty", "failed"],
    returned_dates: tuple[str, ...] = (),
    *,
    start: str = "2024-01-01",
    end: str = "2024-01-05",
    retrieved_at: datetime | None = RETRIEVED_AT,
) -> TickerDownloadStatus:
    resolved_dates = tuple(date.fromisoformat(value) for value in returned_dates)
    return TickerDownloadStatus(
        ticker=ticker,
        status=status,
        rows_received=len(resolved_dates),
        first_date=min(resolved_dates) if resolved_dates else None,
        last_date=max(resolved_dates) if resolved_dates else None,
        retrieved_at=None if status == "failed" else retrieved_at,
        error="synthetic failure" if status == "failed" else None,
        query_start=date.fromisoformat(start),
        query_end=date.fromisoformat(end),
        returned_dates=resolved_dates,
    )


def _price_row_with_decimal(value: Decimal | None) -> pd.DataFrame:
    row = _canonical_prices().iloc[[0]].copy()
    row["open"] = pd.Series([value], index=row.index, dtype=ARROW_DECIMAL_DTYPE)
    return row


def _install_tracking_transaction_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, int | bool]:
    state: dict[str, int | bool] = {
        "active": False,
        "entries": 0,
        "releases": 0,
    }

    @contextmanager
    def tracking_lock(output_path: Path) -> Iterator[None]:
        del output_path
        assert state["active"] is False
        state["active"] = True
        state["entries"] = int(state["entries"]) + 1
        try:
            yield
        finally:
            state["active"] = False
            state["releases"] = int(state["releases"]) + 1

    monkeypatch.setattr(price_module, "_price_history_transaction_lock", tracking_lock)
    return state


def test_exact_duplicate_observations_are_deduplicated() -> None:
    original = _canonical_prices().iloc[[0]]
    duplicate = original.copy()
    duplicate["retrieved_at"] = pd.Timestamp("2026-08-14T00:00:00Z")
    duplicated_data = pd.concat([original, duplicate], ignore_index=True)

    deduplicated = deduplicate_price_data(duplicated_data)

    assert len(deduplicated) == 1
    assert deduplicated["retrieved_at"].iloc[0] == pd.Timestamp("2026-08-14T00:00:00Z")


def test_conflicting_duplicate_observations_are_rejected() -> None:
    original = _canonical_prices().iloc[[0]]
    conflict = original.copy()
    conflict["close"] = 102.5
    duplicated_data = pd.concat([original, conflict], ignore_index=True)

    with pytest.raises(PriceDataValidationError, match="Conflicting market values"):
        deduplicate_price_data(duplicated_data)


def test_unhashable_object_market_values_are_rejected_before_deduplication() -> None:
    duplicated = pd.concat(
        [_canonical_prices().iloc[[0]], _canonical_prices().iloc[[0]]],
        ignore_index=True,
    )
    values = np.empty(2, dtype=object)
    values[:] = [[100.0], np.array([100.0])]
    duplicated["open"] = pd.Series(values, dtype="object")

    with pytest.raises(
        PriceDataValidationError, match=r"'open'.*real numeric dtype.*object"
    ):
        deduplicate_price_data(duplicated)


@pytest.mark.parametrize(
    "malformed_ticker",
    [["SPY"], np.array(["SPY"])],
    ids=["list", "numpy-array"],
)
def test_malformed_ticker_is_rejected_before_price_duplicate_hashing(
    malformed_ticker: object,
) -> None:
    invalid = _duplicate_prices_with_malformed_ticker(malformed_ticker)

    with pytest.raises(
        PriceDataValidationError, match="Ticker values must be non-empty strings"
    ):
        deduplicate_price_data(invalid)


@pytest.mark.parametrize(
    ("column", "invalid_value"),
    [
        ("open", 0.0),
        ("high", -1.0),
        ("low", 0.0),
        ("close", -1.0),
        ("adjusted_close", 0.0),
    ],
)
def test_zero_or_negative_prices_are_rejected(
    column: str, invalid_value: float
) -> None:
    prices = _canonical_prices()
    prices.loc[0, column] = invalid_value

    with pytest.raises(PriceDataValidationError, match=rf"Column '{column}'"):
        validate_price_data(prices)


def test_negative_volume_is_rejected() -> None:
    prices = _canonical_prices()
    prices.loc[0, "volume"] = -1.0

    with pytest.raises(PriceDataValidationError, match="must be non-negative"):
        validate_price_data(prices)


@pytest.mark.parametrize(
    ("column", "invalid_value", "expected_rule"),
    [
        ("high", 98.0, "high >= open"),
        ("low", 101.0, "low <= open"),
        ("close", 104.0, "high >= close"),
    ],
)
def test_ohlc_consistency_violations_are_rejected(
    column: str, invalid_value: float, expected_rule: str
) -> None:
    prices = _canonical_prices().iloc[[0]].copy()
    prices.loc[prices.index[0], column] = invalid_value

    with pytest.raises(PriceDataValidationError, match=expected_rule):
        validate_price_data(prices)


def test_required_schema_is_enforced() -> None:
    prices = _canonical_prices().drop(columns="adjusted_close")

    with pytest.raises(PriceDataValidationError, match="missing required columns"):
        validate_price_data(prices)


@pytest.mark.parametrize("column", ["close", "date", "ticker"])
def test_duplicate_canonical_column_labels_are_rejected(column: str) -> None:
    invalid = _with_duplicate_column_label(_canonical_prices(), column)

    with pytest.raises(
        PriceDataValidationError, match=rf"duplicate column labels.*{column}"
    ):
        validate_price_data(invalid)


def test_blank_ticker_is_rejected() -> None:
    prices = _canonical_prices()
    prices.loc[0, "ticker"] = " "

    with pytest.raises(PriceDataValidationError, match="non-empty strings"):
        validate_price_data(prices)


def test_legitimate_missing_market_values_are_preserved() -> None:
    prices = _canonical_prices()
    prices.loc[0, ["open", "adjusted_close", "volume"]] = float("nan")

    validate_price_data(prices)

    assert prices.loc[0, ["open", "adjusted_close", "volume"]].isna().all()


def test_row_with_all_market_fields_missing_is_rejected() -> None:
    prices = _canonical_prices().iloc[[0]].copy()
    prices.loc[:, list(PRICE_VALUE_COLUMNS)] = float("nan")

    with pytest.raises(
        PriceDataValidationError,
        match=r"at least one market value.*'ticker': 'SPY'.*2024-01-02",
    ):
        validate_price_data(prices)


def test_nullable_row_with_all_market_fields_missing_is_rejected() -> None:
    prices = _canonical_prices().iloc[[0]].copy()
    for column in PRICE_VALUE_COLUMNS:
        dtype = "Int64" if column == "volume" else "Float64"
        prices[column] = pd.Series([pd.NA], index=prices.index, dtype=dtype)

    with pytest.raises(PriceDataValidationError, match="at least one market value"):
        validate_price_data(prices)


def test_row_with_only_adjusted_close_present_is_valid() -> None:
    prices = _canonical_prices().iloc[[0]].copy()
    prices.loc[:, list(PRICE_VALUE_COLUMNS)] = float("nan")
    prices.loc[prices.index[0], "adjusted_close"] = 101.5

    validate_price_data(prices)

    assert prices[list(PRICE_VALUE_COLUMNS)].notna().sum(axis=1).iloc[0] == 1


def test_row_with_only_volume_present_is_valid() -> None:
    prices = _canonical_prices().iloc[[0]].copy()
    prices.loc[:, list(PRICE_VALUE_COLUMNS)] = float("nan")
    prices.loc[prices.index[0], "volume"] = 1_000_000.0

    validate_price_data(prices)

    assert prices[list(PRICE_VALUE_COLUMNS)].notna().sum(axis=1).iloc[0] == 1


def test_persistence_rejects_direct_all_market_missing_row(tmp_path: Path) -> None:
    output_path = tmp_path / "etf_prices_daily.parquet"
    prices = _canonical_prices().iloc[[0]].copy()
    prices.loc[:, list(PRICE_VALUE_COLUMNS)] = float("nan")

    with pytest.raises(PriceDataValidationError, match="at least one market value"):
        persist_price_history(prices, output_path)

    assert not output_path.exists()


def test_persistence_round_trip_uses_canonical_parquet(tmp_path: Path) -> None:
    output_path = tmp_path / "nested" / "etf_prices_daily.parquet"

    result = persist_price_history(_canonical_prices(), output_path)
    reloaded = pd.read_parquet(output_path)

    pd.testing.assert_frame_equal(reloaded, result.prices)
    assert result.revised_row_count == 0
    assert result.snapshot_path is None
    validate_price_data(reloaded)


def test_lock_covers_read_validation_snapshot_and_canonical_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_path = tmp_path / "etf_prices_daily.parquet"
    snapshot_dir = tmp_path / "snapshots" / "prices"
    prices = _canonical_prices()
    persist_price_history(prices, output_path)
    revision = prices.iloc[[0]].copy()
    revision["adjusted_close"] = 101.75
    revision["retrieved_at"] = NEW_RETRIEVED_AT

    state = {"active": False}
    events: list[str] = []
    original_read = price_module.pd.read_parquet
    original_validate = price_module.validate_price_data
    original_snapshot = price_module._snapshot_existing_price_file
    original_write = price_module._write_parquet_atomically

    @contextmanager
    def tracking_lock(locked_output_path: Path) -> Iterator[None]:
        assert locked_output_path == output_path
        assert state["active"] is False
        state["active"] = True
        events.append("lock-acquired")
        try:
            yield
        finally:
            events.append("lock-released")
            state["active"] = False

    def tracked_read(path: Path) -> pd.DataFrame:
        assert state["active"] is True
        events.append("existing-read")
        return original_read(path)

    def tracked_validate(data: pd.DataFrame) -> None:
        assert state["active"] is True
        events.append("validation")
        original_validate(data)

    def tracked_snapshot(
        canonical_path: Path,
        destination: Path,
        timestamp: datetime,
    ) -> Path:
        assert state["active"] is True
        events.append("snapshot")
        return original_snapshot(canonical_path, destination, timestamp)

    def tracked_write(data: pd.DataFrame, path: Path) -> None:
        assert state["active"] is True
        events.append("canonical-replacement")
        original_write(data, path)

    monkeypatch.setattr(price_module, "_price_history_transaction_lock", tracking_lock)
    monkeypatch.setattr(price_module.pd, "read_parquet", tracked_read)
    monkeypatch.setattr(price_module, "validate_price_data", tracked_validate)
    monkeypatch.setattr(price_module, "_snapshot_existing_price_file", tracked_snapshot)
    monkeypatch.setattr(price_module, "_write_parquet_atomically", tracked_write)

    persist_price_history(
        revision,
        output_path,
        snapshot_dir=snapshot_dir,
        snapshot_time=SNAPSHOT_TIME,
    )

    assert events[0] == "lock-acquired"
    assert events[-1] == "lock-released"
    assert events.index("existing-read") < events.index("snapshot")
    assert events.index("snapshot") < events.index("canonical-replacement")
    assert events.count("validation") >= 3


def test_second_persistence_attempt_cannot_enter_while_first_holds_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_path = tmp_path / "etf_prices_daily.parquet"
    persist_price_history(_canonical_prices(), output_path)
    first_writer = _appended_price_row("2024-01-04")
    second_writer = _appended_price_row("2024-01-05")
    state = {"active": False, "blocked": 0, "triggered": False, "reads": 0}
    original_read = price_module.pd.read_parquet
    original_write = price_module._write_parquet_atomically

    @contextmanager
    def exclusive_lock(locked_output_path: Path) -> Iterator[None]:
        assert locked_output_path == output_path
        if state["active"]:
            state["blocked"] = int(state["blocked"]) + 1
            raise TimeoutError("synthetic lock contention")
        state["active"] = True
        try:
            yield
        finally:
            state["active"] = False

    def tracked_read(path: Path) -> pd.DataFrame:
        state["reads"] = int(state["reads"]) + 1
        return original_read(path)

    def interleaved_write(data: pd.DataFrame, path: Path) -> None:
        if not state["triggered"]:
            state["triggered"] = True
            with pytest.raises(TimeoutError, match="synthetic lock contention"):
                persist_price_history(second_writer, output_path)
        original_write(data, path)

    monkeypatch.setattr(price_module, "_price_history_transaction_lock", exclusive_lock)
    monkeypatch.setattr(price_module.pd, "read_parquet", tracked_read)
    monkeypatch.setattr(price_module, "_write_parquet_atomically", interleaved_write)

    first_result = persist_price_history(first_writer, output_path)

    assert state["blocked"] == 1
    assert state["reads"] == 1
    assert pd.Timestamp("2024-01-04") in set(first_result.prices["date"])
    assert pd.Timestamp("2024-01-05") not in set(first_result.prices["date"])


def test_second_writer_rereads_first_writers_canonical_update(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_path = tmp_path / "etf_prices_daily.parquet"
    persist_price_history(_canonical_prices(), output_path)
    observed_existing_dates: list[set[pd.Timestamp]] = []
    original_read = price_module.pd.read_parquet

    def tracked_read(path: Path) -> pd.DataFrame:
        existing = original_read(path)
        observed_existing_dates.append(set(existing["date"]))
        return existing

    monkeypatch.setattr(price_module.pd, "read_parquet", tracked_read)

    persist_price_history(_appended_price_row("2024-01-04"), output_path)
    persist_price_history(_appended_price_row("2024-01-05"), output_path)

    assert len(observed_existing_dates) == 2
    assert pd.Timestamp("2024-01-04") not in observed_existing_dates[0]
    assert pd.Timestamp("2024-01-04") in observed_existing_dates[1]


def test_serialized_append_writers_do_not_lose_either_row(tmp_path: Path) -> None:
    output_path = tmp_path / "etf_prices_daily.parquet"
    persist_price_history(_canonical_prices(), output_path)
    first_writer = _appended_price_row("2024-01-04")
    second_writer = _appended_price_row("2024-01-05")

    persist_price_history(first_writer, output_path)
    persist_price_history(second_writer, output_path)

    canonical = pd.read_parquet(output_path)
    assert set(canonical["date"]) == {
        pd.Timestamp("2024-01-02"),
        pd.Timestamp("2024-01-03"),
        pd.Timestamp("2024-01-04"),
        pd.Timestamp("2024-01-05"),
    }


def test_serialized_revision_then_append_preserves_both_writers(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "etf_prices_daily.parquet"
    snapshot_dir = tmp_path / "snapshots" / "prices"
    prices = _canonical_prices()
    persist_price_history(prices, output_path)
    revision = prices.iloc[[0]].copy()
    revision["adjusted_close"] = 101.75
    revision["retrieved_at"] = NEW_RETRIEVED_AT

    persist_price_history(
        revision,
        output_path,
        snapshot_dir=snapshot_dir,
        snapshot_time=SNAPSHOT_TIME,
    )
    persist_price_history(_appended_price_row("2024-01-04"), output_path)

    canonical = pd.read_parquet(output_path)
    revised_row = canonical.loc[canonical["date"].eq(pd.Timestamp("2024-01-02"))]
    assert revised_row["adjusted_close"].iloc[0] == 101.75
    assert pd.Timestamp("2024-01-04") in set(canonical["date"])


def test_lock_is_released_after_incoming_validation_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_path = tmp_path / "etf_prices_daily.parquet"
    state = _install_tracking_transaction_lock(monkeypatch)
    invalid = _canonical_prices()
    invalid["close"] = invalid["close"].astype(str)

    with pytest.raises(PriceDataValidationError, match="real numeric dtype"):
        persist_price_history(invalid, output_path)

    assert state == {"active": False, "entries": 1, "releases": 1}
    persist_price_history(_canonical_prices(), output_path)
    assert state == {"active": False, "entries": 2, "releases": 2}


def test_lock_is_released_after_canonical_write_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_path = tmp_path / "etf_prices_daily.parquet"
    prices = _canonical_prices()
    persist_price_history(prices, output_path)
    original_file = output_path.read_bytes()
    original_write = price_module._write_parquet_atomically
    state = _install_tracking_transaction_lock(monkeypatch)

    def fail_write(data: pd.DataFrame, path: Path) -> None:
        del data, path
        raise OSError("synthetic canonical write failure")

    monkeypatch.setattr(price_module, "_write_parquet_atomically", fail_write)

    with pytest.raises(OSError, match="synthetic canonical write failure"):
        persist_price_history(_appended_price_row("2024-01-04"), output_path)

    assert output_path.read_bytes() == original_file
    assert state == {"active": False, "entries": 1, "releases": 1}
    monkeypatch.setattr(price_module, "_write_parquet_atomically", original_write)
    persist_price_history(_appended_price_row("2024-01-04"), output_path)
    assert state == {"active": False, "entries": 2, "releases": 2}


def test_lock_timeout_does_not_modify_canonical_or_snapshots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_path = tmp_path / "etf_prices_daily.parquet"
    snapshot_dir = tmp_path / "snapshots" / "prices"
    persist_price_history(_canonical_prices(), output_path)
    original_file = output_path.read_bytes()
    attempts = 0

    def fail_lock(lock_file: object) -> None:
        nonlocal attempts
        del lock_file
        attempts += 1
        raise OSError(errno.EACCES, "synthetic lock contention")

    monkeypatch.setattr(price_module, "_try_lock_file", fail_lock)
    monkeypatch.setattr(price_module, "_PRICE_HISTORY_LOCK_TIMEOUT_SECONDS", 0.0)

    with pytest.raises(TimeoutError, match="Timed out after 0 seconds") as error_info:
        persist_price_history(
            _appended_price_row("2024-01-04"),
            output_path,
            snapshot_dir=snapshot_dir,
        )

    assert isinstance(error_info.value.__cause__, OSError)
    assert attempts == 1
    assert output_path.read_bytes() == original_file
    assert not snapshot_dir.exists()


def test_custom_output_paths_use_independent_adjacent_locks(tmp_path: Path) -> None:
    first_output = tmp_path / "first" / "prices.parquet"
    second_output = tmp_path / "second" / "prices.parquet"
    first_lock = price_module._price_history_lock_path(first_output)
    second_lock = price_module._price_history_lock_path(second_output)

    assert first_lock != second_lock
    assert first_lock.parent == first_output.parent.resolve()
    assert second_lock.parent == second_output.parent.resolve()
    with price_module._price_history_transaction_lock(first_output):
        with price_module._price_history_transaction_lock(second_output):
            assert first_lock.exists()
            assert second_lock.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows lock smoke test")
def test_windows_lock_blocks_another_process_without_sleeping(tmp_path: Path) -> None:
    output_path = tmp_path / "etf_prices_daily.parquet"
    child_code = "\n".join(
        [
            "import sys",
            "from pathlib import Path",
            "sys.path.insert(0, str(Path.cwd() / 'src'))",
            "import etf_crowding.data.prices as prices",
            "prices._PRICE_HISTORY_LOCK_TIMEOUT_SECONDS = 0.1",
            "try:",
            "    with prices._price_history_transaction_lock(Path(sys.argv[1])):",
            "        raise SystemExit(2)",
            "except TimeoutError:",
            "    raise SystemExit(0)",
        ]
    )

    with price_module._price_history_transaction_lock(output_path):
        child = subprocess.run(
            [sys.executable, "-c", child_code, str(output_path)],
            cwd=Path.cwd(),
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )

    assert child.returncode == 0, child.stderr


def test_repeated_persistence_does_not_duplicate_rows(tmp_path: Path) -> None:
    output_path = tmp_path / "etf_prices_daily.parquet"
    prices = _canonical_prices()
    persist_price_history(prices, output_path)
    repeated = prices.copy()
    repeated["retrieved_at"] = pd.Timestamp("2026-08-14T00:00:00Z")

    snapshot_dir = tmp_path / "snapshots" / "prices"
    result = persist_price_history(
        repeated,
        output_path,
        snapshot_dir=snapshot_dir,
        snapshot_time=SNAPSHOT_TIME,
    )

    assert len(result.prices) == 2
    assert not result.prices.duplicated(["ticker", "date"]).any()
    assert (result.prices["retrieved_at"] == pd.Timestamp("2026-08-14T00:00:00Z")).all()
    assert result.revised_row_count == 0
    assert result.snapshot_path is None
    assert not snapshot_dir.exists()


def test_advanced_price_watermark_blocks_older_revision_and_snapshots_later_one(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "etf_prices_daily.parquet"
    snapshot_dir = tmp_path / "snapshots" / "prices"
    canonical = _canonical_prices()
    persist_price_history(canonical, output_path)

    watermark_time = pd.Timestamp("2026-08-15T12:30:00Z")
    identical = canonical.iloc[[0]].copy()
    identical["retrieved_at"] = watermark_time
    watermark_result = persist_price_history(
        identical,
        output_path,
        snapshot_dir=snapshot_dir,
        snapshot_time=SNAPSHOT_TIME,
    )

    watermark_row = watermark_result.prices.loc[
        watermark_result.prices["date"].eq(pd.Timestamp("2024-01-02"))
    ]
    assert (
        watermark_row[list(PRICE_VALUE_COLUMNS)].iloc[0].tolist()
        == canonical[list(PRICE_VALUE_COLUMNS)].iloc[0].tolist()
    )
    assert watermark_row["retrieved_at"].iloc[0] == watermark_time
    assert watermark_result.revised_row_count == 0
    assert watermark_result.snapshot_path is None
    assert not snapshot_dir.exists()
    watermarked_file = output_path.read_bytes()

    stale_revision = canonical.iloc[[0]].copy()
    stale_revision["adjusted_close"] = 101.75
    stale_revision["retrieved_at"] = NEW_RETRIEVED_AT
    with pytest.raises(PriceDataValidationError, match="older than the canonical"):
        persist_price_history(
            stale_revision,
            output_path,
            snapshot_dir=snapshot_dir,
            snapshot_time=SNAPSHOT_TIME,
        )

    assert output_path.read_bytes() == watermarked_file
    assert not snapshot_dir.exists()

    later_revision_time = pd.Timestamp("2026-08-16T12:30:00Z")
    later_revision = canonical.iloc[[0]].copy()
    later_revision[["open", "high", "low", "close", "adjusted_close", "volume"]] = [
        50.0,
        52.0,
        49.0,
        51.0,
        50.5,
        2_000_000.0,
    ]
    later_revision["retrieved_at"] = later_revision_time
    revised_result = persist_price_history(
        later_revision,
        output_path,
        snapshot_dir=snapshot_dir,
        snapshot_time=SNAPSHOT_TIME,
    )

    assert revised_result.revised_row_count == 1
    assert revised_result.snapshot_path is not None
    assert revised_result.snapshot_path.read_bytes() == watermarked_file
    snapshotted = pd.read_parquet(revised_result.snapshot_path)
    snapshotted_row = snapshotted.loc[
        snapshotted["date"].eq(pd.Timestamp("2024-01-02"))
    ]
    assert snapshotted_row["retrieved_at"].iloc[0] == watermark_time
    revised_row = revised_result.prices.loc[
        revised_result.prices["date"].eq(pd.Timestamp("2024-01-02"))
    ]
    assert (
        revised_row[list(PRICE_VALUE_COLUMNS)].iloc[0].tolist()
        == later_revision[list(PRICE_VALUE_COLUMNS)].iloc[0].tolist()
    )
    assert revised_row["retrieved_at"].iloc[0] == later_revision_time


def test_older_and_equal_identical_prices_do_not_move_watermark_backward(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "etf_prices_daily.parquet"
    canonical = _canonical_prices()
    watermark_time = pd.Timestamp("2026-08-15T12:30:00Z")
    canonical["retrieved_at"] = watermark_time
    persist_price_history(canonical, output_path)

    older = _canonical_prices().iloc[[0]].copy()
    older_result = persist_price_history(older, output_path)
    equal = canonical.iloc[[0]].copy()
    equal_result = persist_price_history(equal, output_path)

    for result in (older_result, equal_result):
        retained = result.prices.loc[
            result.prices["date"].eq(pd.Timestamp("2024-01-02"))
        ]
        assert retained["retrieved_at"].iloc[0] == watermark_time
        assert result.revised_row_count == 0
        assert result.snapshot_path is None


def test_equal_retrieved_at_and_identical_values_remain_a_no_op(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "etf_prices_daily.parquet"
    snapshot_dir = tmp_path / "snapshots" / "prices"
    prices = _canonical_prices()
    persist_price_history(prices, output_path)

    result = persist_price_history(
        prices.iloc[[0]].copy(),
        output_path,
        snapshot_dir=snapshot_dir,
        snapshot_time=SNAPSHOT_TIME,
    )

    assert result.revised_row_count == 0
    assert result.snapshot_path is None
    assert not snapshot_dir.exists()
    pd.testing.assert_frame_equal(result.prices, prices)


def test_adjusted_close_revision_snapshots_exact_superseded_vintage(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "etf_prices_daily.parquet"
    snapshot_dir = tmp_path / "snapshots" / "prices"
    prices = _canonical_prices()
    persist_price_history(prices, output_path)
    original_file = output_path.read_bytes()
    revision = prices.iloc[[0]].copy()
    revision["adjusted_close"] = 101.75
    revision["retrieved_at"] = NEW_RETRIEVED_AT

    result = persist_price_history(
        revision,
        output_path,
        snapshot_dir=snapshot_dir,
        snapshot_time=SNAPSHOT_TIME,
    )

    assert result.revised_row_count == 1
    assert result.revised_tickers == ("SPY",)
    assert result.snapshot_path is not None
    assert result.snapshot_path.read_bytes() == original_file
    pd.testing.assert_frame_equal(pd.read_parquet(result.snapshot_path), prices)

    canonical = pd.read_parquet(output_path)
    revised_row = canonical.loc[canonical["date"].eq(pd.Timestamp("2024-01-02"))]
    assert revised_row["adjusted_close"].iloc[0] == 101.75
    assert revised_row["retrieved_at"].iloc[0] == NEW_RETRIEVED_AT


def test_older_overlapping_revision_is_rejected_without_side_effects(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "etf_prices_daily.parquet"
    snapshot_dir = tmp_path / "snapshots" / "prices"
    canonical = _canonical_prices()
    canonical["retrieved_at"] = NEW_RETRIEVED_AT
    persist_price_history(canonical, output_path)
    canonical_bytes = output_path.read_bytes()
    stale_revision = canonical.iloc[[0]].copy()
    stale_revision["adjusted_close"] = 101.75
    stale_revision["retrieved_at"] = pd.Timestamp(RETRIEVED_AT)

    with pytest.raises(
        PriceDataValidationError,
        match=r"older than the canonical.*\('SPY', Timestamp\('2024-01-02",
    ):
        persist_price_history(
            stale_revision,
            output_path,
            snapshot_dir=snapshot_dir,
            snapshot_time=SNAPSHOT_TIME,
        )

    assert output_path.read_bytes() == canonical_bytes
    assert not snapshot_dir.exists()


def test_equal_retrieved_at_with_different_values_is_rejected(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "etf_prices_daily.parquet"
    snapshot_dir = tmp_path / "snapshots" / "prices"
    canonical = _canonical_prices()
    persist_price_history(canonical, output_path)
    canonical_bytes = output_path.read_bytes()
    inconsistent = canonical.iloc[[0]].copy()
    inconsistent["adjusted_close"] = 101.75

    with pytest.raises(
        PriceDataValidationError,
        match=r"same retrieved_at.*\('SPY', Timestamp\('2024-01-02",
    ):
        persist_price_history(
            inconsistent,
            output_path,
            snapshot_dir=snapshot_dir,
            snapshot_time=SNAPSHOT_TIME,
        )

    assert output_path.read_bytes() == canonical_bytes
    assert not snapshot_dir.exists()


def test_ohlc_revision_is_accepted_as_generic_source_vintage_revision(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "etf_prices_daily.parquet"
    prices = _canonical_prices()
    persist_price_history(prices, output_path)
    revision = prices.iloc[[0]].copy()
    revision[["open", "high", "low", "close"]] = [50.0, 51.5, 49.5, 51.0]
    revision["volume"] = 2_000_000.0
    revision["retrieved_at"] = NEW_RETRIEVED_AT

    result = persist_price_history(
        revision,
        output_path,
        snapshot_dir=tmp_path / "snapshots" / "prices",
        snapshot_time=SNAPSHOT_TIME,
    )

    assert result.revised_row_count == 1
    revised_row = result.prices.loc[
        result.prices["date"].eq(pd.Timestamp("2024-01-02"))
    ]
    assert revised_row[["open", "high", "low", "close"]].iloc[0].tolist() == [
        50.0,
        51.5,
        49.5,
        51.0,
    ]
    assert revised_row["retrieved_at"].iloc[0] == NEW_RETRIEVED_AT


def test_snapshot_names_are_timestamped_and_collision_safe(tmp_path: Path) -> None:
    output_path = tmp_path / "etf_prices_daily.parquet"
    snapshot_dir = tmp_path / "snapshots" / "prices"
    prices = _canonical_prices()
    persist_price_history(prices, output_path)
    first_revision = prices.iloc[[0]].copy()
    first_revision["adjusted_close"] = 101.75
    first_revision["retrieved_at"] = NEW_RETRIEVED_AT

    first_result = persist_price_history(
        first_revision,
        output_path,
        snapshot_dir=snapshot_dir,
        snapshot_time=SNAPSHOT_TIME,
    )
    assert first_result.snapshot_path is not None
    first_snapshot_bytes = first_result.snapshot_path.read_bytes()
    second_revision = first_revision.copy()
    second_revision["adjusted_close"] = 101.8
    second_revision["retrieved_at"] = pd.Timestamp("2026-08-15T12:30:00Z")

    second_result = persist_price_history(
        second_revision,
        output_path,
        snapshot_dir=snapshot_dir,
        snapshot_time=SNAPSHOT_TIME,
    )

    assert second_result.snapshot_path is not None
    assert first_result.snapshot_path.name == (
        "etf_prices_daily_20260814T130000000000Z.parquet"
    )
    assert second_result.snapshot_path.name == (
        "etf_prices_daily_20260814T130000000000Z_001.parquet"
    )
    assert first_result.snapshot_path.read_bytes() == first_snapshot_bytes
    snapshotted_first_revision = pd.read_parquet(second_result.snapshot_path)
    assert snapshotted_first_revision.loc[0, "adjusted_close"] == 101.75


def test_incoming_values_may_complete_a_previous_missing_source_row(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "etf_prices_daily.parquet"
    existing = _canonical_prices()
    existing.loc[0, "adjusted_close"] = float("nan")
    persist_price_history(existing, output_path)
    incoming = _canonical_prices().iloc[[0]].copy()
    incoming["retrieved_at"] = NEW_RETRIEVED_AT

    result = persist_price_history(
        incoming,
        output_path,
        snapshot_dir=tmp_path / "snapshots" / "prices",
        snapshot_time=SNAPSHOT_TIME,
    )

    assert result.revised_row_count == 1
    completed_row = result.prices.loc[
        result.prices["date"].eq(pd.Timestamp("2024-01-02"))
    ]
    assert completed_row["adjusted_close"].iloc[0] == 101.5
    assert completed_row["retrieved_at"].iloc[0] == NEW_RETRIEVED_AT


def test_nullable_float_missing_to_present_is_detected_as_completion(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "etf_prices_daily.parquet"
    existing = _canonical_prices()
    existing["adjusted_close"] = pd.Series([pd.NA, 103.5], dtype="Float64")
    persist_price_history(existing, output_path)
    incoming = existing.iloc[[0]].copy()
    incoming.loc[incoming.index[0], "adjusted_close"] = 101.5
    incoming["retrieved_at"] = NEW_RETRIEVED_AT

    result = persist_price_history(
        incoming,
        output_path,
        snapshot_dir=tmp_path / "snapshots" / "prices",
        snapshot_time=SNAPSHOT_TIME,
    )

    assert result.revised_row_count == 1
    completed_row = result.prices.loc[
        result.prices["date"].eq(pd.Timestamp("2024-01-02"))
    ]
    assert completed_row["adjusted_close"].iloc[0] == 101.5
    assert completed_row["retrieved_at"].iloc[0] == NEW_RETRIEVED_AT


def test_nullable_float_both_missing_remains_identical(tmp_path: Path) -> None:
    output_path = tmp_path / "etf_prices_daily.parquet"
    snapshot_dir = tmp_path / "snapshots" / "prices"
    existing = _canonical_prices()
    existing["adjusted_close"] = pd.Series([pd.NA, 103.5], dtype="Float64")
    persist_price_history(existing, output_path)
    incoming = existing.iloc[[0]].copy()
    incoming["retrieved_at"] = NEW_RETRIEVED_AT

    result = persist_price_history(
        incoming,
        output_path,
        snapshot_dir=snapshot_dir,
        snapshot_time=SNAPSHOT_TIME,
    )

    assert result.revised_row_count == 0
    assert result.snapshot_path is None
    assert not snapshot_dir.exists()
    retained_row = result.prices.loc[
        result.prices["date"].eq(pd.Timestamp("2024-01-02"))
    ]
    assert pd.isna(retained_row["adjusted_close"].iloc[0])
    assert retained_row["retrieved_at"].iloc[0] == NEW_RETRIEVED_AT


def test_advanced_missing_price_watermark_rejects_older_completion(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "etf_prices_daily.parquet"
    snapshot_dir = tmp_path / "snapshots" / "prices"
    existing = _canonical_prices()
    existing["adjusted_close"] = pd.Series([pd.NA, 103.5], dtype="Float64")
    persist_price_history(existing, output_path)

    watermark_time = pd.Timestamp("2026-08-15T12:30:00Z")
    identical_missing = existing.iloc[[0]].copy()
    identical_missing["retrieved_at"] = watermark_time
    persist_price_history(identical_missing, output_path)
    watermarked_file = output_path.read_bytes()

    older_completion = _canonical_prices().iloc[[0]].copy()
    older_completion["retrieved_at"] = NEW_RETRIEVED_AT
    with pytest.raises(PriceDataValidationError, match="older than the canonical"):
        persist_price_history(
            older_completion,
            output_path,
            snapshot_dir=snapshot_dir,
            snapshot_time=SNAPSHOT_TIME,
        )

    assert output_path.read_bytes() == watermarked_file
    assert not snapshot_dir.exists()


def test_nullable_numeric_present_to_missing_triggers_value_loss_rejection(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "etf_prices_daily.parquet"
    existing = _canonical_prices()
    existing["adjusted_close"] = pd.Series([101.5, 103.5], dtype="Float64")
    persist_price_history(existing, output_path)
    canonical_bytes = output_path.read_bytes()
    incoming = existing.iloc[[0]].copy()
    incoming.loc[incoming.index[0], "adjusted_close"] = pd.NA
    incoming["retrieved_at"] = NEW_RETRIEVED_AT

    with pytest.raises(PriceDataValidationError, match="loses previously available"):
        persist_price_history(
            incoming,
            output_path,
            snapshot_dir=tmp_path / "snapshots" / "prices",
            snapshot_time=SNAPSHOT_TIME,
        )

    assert output_path.read_bytes() == canonical_bytes


def test_incoming_value_loss_is_rejected_without_modifying_canonical_file(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "etf_prices_daily.parquet"
    snapshot_dir = tmp_path / "snapshots" / "prices"
    prices = _canonical_prices()
    persist_price_history(prices, output_path)
    original_file = output_path.read_bytes()
    incomplete = prices.iloc[[0]].copy()
    incomplete["adjusted_close"] = float("nan")
    incomplete["retrieved_at"] = NEW_RETRIEVED_AT

    with pytest.raises(PriceDataValidationError, match="loses previously available"):
        persist_price_history(
            incomplete,
            output_path,
            snapshot_dir=snapshot_dir,
            snapshot_time=SNAPSHOT_TIME,
        )

    assert output_path.read_bytes() == original_file
    assert not snapshot_dir.exists()


def test_failed_snapshot_preservation_leaves_canonical_file_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_path = tmp_path / "etf_prices_daily.parquet"
    prices = _canonical_prices()
    persist_price_history(prices, output_path)
    original_file = output_path.read_bytes()
    revision = prices.iloc[[0]].copy()
    revision["adjusted_close"] = 101.75
    revision["retrieved_at"] = NEW_RETRIEVED_AT
    original_snapshot = price_module._snapshot_existing_price_file
    state = _install_tracking_transaction_lock(monkeypatch)

    def fail_snapshot(*args: object, **kwargs: object) -> Path:
        del args, kwargs
        raise OSError("synthetic snapshot failure")

    monkeypatch.setattr(price_module, "_snapshot_existing_price_file", fail_snapshot)

    with pytest.raises(OSError, match="synthetic snapshot failure"):
        persist_price_history(
            revision,
            output_path,
            snapshot_dir=tmp_path / "snapshots" / "prices",
            snapshot_time=SNAPSHOT_TIME,
        )

    assert output_path.read_bytes() == original_file
    pd.testing.assert_frame_equal(pd.read_parquet(output_path), prices)
    assert state == {"active": False, "entries": 1, "releases": 1}
    monkeypatch.setattr(
        price_module, "_snapshot_existing_price_file", original_snapshot
    )
    persist_price_history(_appended_price_row("2024-01-04"), output_path)
    assert state == {"active": False, "entries": 2, "releases": 2}


def test_partial_ticker_batch_preserves_unrequested_ticker_history(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "etf_prices_daily.parquet"
    existing = _canonical_prices()
    qqq_history = existing.iloc[[0]].copy()
    qqq_history["ticker"] = "QQQ"
    qqq_history[["open", "high", "low", "close", "adjusted_close"]] += 20.0
    existing = pd.concat([existing, qqq_history], ignore_index=True)
    persist_price_history(existing, output_path)
    incoming_spy = _canonical_prices().iloc[[0]].copy()
    incoming_spy["date"] = pd.Timestamp("2024-01-04")
    incoming_spy["retrieved_at"] = NEW_RETRIEVED_AT

    result = persist_price_history(incoming_spy, output_path)

    assert set(result.prices["ticker"]) == {"SPY", "QQQ"}
    assert len(result.prices.loc[result.prices["ticker"].eq("QQQ")]) == 1
    assert len(result.prices.loc[result.prices["ticker"].eq("SPY")]) == 3
    assert result.revised_row_count == 0


def test_invalid_incoming_revision_cannot_modify_or_snapshot_canonical_file(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "etf_prices_daily.parquet"
    snapshot_dir = tmp_path / "snapshots" / "prices"
    prices = _canonical_prices()
    persist_price_history(prices, output_path)
    original_file = output_path.read_bytes()
    invalid = prices.iloc[[0]].copy()
    invalid["close"] = -1.0
    invalid["retrieved_at"] = NEW_RETRIEVED_AT

    with pytest.raises(PriceDataValidationError, match="must be positive"):
        persist_price_history(
            invalid,
            output_path,
            snapshot_dir=snapshot_dir,
            snapshot_time=SNAPSHOT_TIME,
        )

    assert output_path.read_bytes() == original_file
    assert not snapshot_dir.exists()


def test_numeric_string_market_column_is_rejected_before_persistence(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "etf_prices_daily.parquet"
    invalid = _canonical_prices()
    invalid["close"] = invalid["close"].astype(str)

    with pytest.raises(
        PriceDataValidationError, match="'close' must have a real numeric dtype"
    ):
        persist_price_history(invalid, output_path)

    assert not output_path.exists()


def test_persistence_rejects_unhashable_duplicate_market_data_without_type_error(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "etf_prices_daily.parquet"
    persist_price_history(_canonical_prices(), output_path)
    canonical_bytes = output_path.read_bytes()
    invalid = pd.concat(
        [_canonical_prices().iloc[[0]], _canonical_prices().iloc[[0]]],
        ignore_index=True,
    )
    invalid["close"] = pd.Series([[102.0], [102.0]], dtype="object")

    with pytest.raises(
        PriceDataValidationError, match=r"'close'.*real numeric dtype.*object"
    ):
        persist_price_history(invalid, output_path)

    assert output_path.read_bytes() == canonical_bytes


def test_malformed_incoming_price_ticker_does_not_modify_canonical(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "etf_prices_daily.parquet"
    persist_price_history(_canonical_prices(), output_path)
    canonical_bytes = output_path.read_bytes()
    invalid = _duplicate_prices_with_malformed_ticker(["SPY"])

    with pytest.raises(
        PriceDataValidationError, match="Ticker values must be non-empty strings"
    ):
        persist_price_history(invalid, output_path)

    assert output_path.read_bytes() == canonical_bytes


def test_malformed_existing_price_ticker_parquet_fails_without_type_error(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "etf_prices_daily.parquet"
    malformed = _duplicate_prices_with_malformed_ticker(["SPY"])
    malformed.to_parquet(output_path, index=False)
    malformed_bytes = output_path.read_bytes()

    with pytest.raises(
        PriceDataValidationError, match="Ticker values must be non-empty strings"
    ):
        persist_price_history(_appended_price_row("2024-01-04"), output_path)

    assert output_path.read_bytes() == malformed_bytes


def test_boolean_market_column_is_rejected() -> None:
    invalid = _canonical_prices()
    invalid["volume"] = pd.Series([True, False], dtype="boolean")

    with pytest.raises(
        PriceDataValidationError, match="'volume' must have a real numeric dtype"
    ):
        validate_price_data(invalid)


def test_object_numeric_looking_market_column_is_rejected() -> None:
    invalid = _canonical_prices()
    invalid["open"] = pd.Series([100.0, 102.0], dtype="object")

    with pytest.raises(
        PriceDataValidationError, match="'open' must have a real numeric dtype"
    ):
        validate_price_data(invalid)


def test_complex64_market_column_is_rejected_before_validation_casting() -> None:
    invalid = _canonical_prices()
    invalid["open"] = pd.Series([100.0 + 1.0j, 102.0 + 2.0j], dtype="complex64")

    with pytest.raises(
        PriceDataValidationError, match=r"'open'.*real numeric dtype.*complex64"
    ):
        validate_price_data(invalid)


def test_complex128_market_column_with_imaginary_value_is_rejected() -> None:
    invalid = _canonical_prices()
    invalid["close"] = pd.Series([102.0 + 1.0j, 104.0 + 3.0j], dtype="complex128")

    with pytest.raises(
        PriceDataValidationError, match=r"'close'.*real numeric dtype.*complex128"
    ):
        validate_price_data(invalid)


def test_complex_dtype_with_zero_imaginary_parts_is_still_rejected() -> None:
    invalid = _canonical_prices()
    invalid["adjusted_close"] = pd.Series(
        [101.5 + 0.0j, 103.5 + 0.0j], dtype="complex128"
    )

    with pytest.raises(
        PriceDataValidationError,
        match=r"'adjusted_close'.*real numeric dtype.*complex128",
    ):
        validate_price_data(invalid)


def test_persistence_rejects_complex_data_before_parquet_write(tmp_path: Path) -> None:
    output_path = tmp_path / "etf_prices_daily.parquet"
    invalid = _canonical_prices()
    invalid["close"] = pd.Series([102.0 + 1.0j, 104.0 + 3.0j], dtype="complex128")

    with pytest.raises(PriceDataValidationError, match="real numeric dtype"):
        persist_price_history(invalid, output_path)

    assert not output_path.exists()


def test_persistence_rejects_duplicate_labels_without_modifying_canonical(
    tmp_path: Path,
) -> None:
    new_output_path = tmp_path / "new" / "etf_prices_daily.parquet"
    invalid = _with_duplicate_column_label(_canonical_prices(), "close")

    with pytest.raises(
        PriceDataValidationError, match=r"duplicate column labels.*close"
    ):
        persist_price_history(invalid, new_output_path)

    assert not new_output_path.exists()

    output_path = tmp_path / "etf_prices_daily.parquet"
    persist_price_history(_canonical_prices(), output_path)
    canonical_bytes = output_path.read_bytes()

    with pytest.raises(
        PriceDataValidationError, match=r"duplicate column labels.*close"
    ):
        persist_price_history(invalid, output_path)

    assert output_path.read_bytes() == canonical_bytes


def test_string_date_column_is_rejected_before_persistence(tmp_path: Path) -> None:
    output_path = tmp_path / "etf_prices_daily.parquet"
    invalid = _canonical_prices()
    invalid["date"] = invalid["date"].dt.strftime("%Y-%m-%d")

    with pytest.raises(
        PriceDataValidationError, match="'date' must have a timezone-naive datetime64"
    ):
        persist_price_history(invalid, output_path)

    assert not output_path.exists()


def test_string_retrieved_at_column_is_rejected_before_persistence(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "etf_prices_daily.parquet"
    invalid = _canonical_prices()
    invalid["retrieved_at"] = invalid["retrieved_at"].astype(str)

    with pytest.raises(
        PriceDataValidationError,
        match="'retrieved_at' must have a timezone-aware datetime64",
    ):
        persist_price_history(invalid, output_path)

    assert not output_path.exists()


@pytest.mark.parametrize(
    "dtype",
    [
        "int64",
        "uint64",
        "float64",
        "Int64",
        "UInt64",
        "Float64",
        "int64[pyarrow]",
        "uint64[pyarrow]",
        "double[pyarrow]",
    ],
)
def test_supported_canonical_price_numeric_dtypes_are_accepted(dtype: str) -> None:
    prices = _canonical_prices()
    values_by_column = {
        "open": [100, 102],
        "high": [103, 105],
        "low": [99, 101],
        "close": [102, 104],
        "adjusted_close": [101, 103],
        "volume": [1_000_000, 1_200_000],
    }
    for column, values in values_by_column.items():
        prices[column] = pd.Series(values, dtype=dtype)

    validate_price_data(prices)


@pytest.mark.parametrize(
    "value",
    [Decimal("100.00"), None],
    ids=["finite-positive", "missing"],
)
def test_arrow_decimal_price_field_is_rejected_cleanly(
    value: Decimal | None,
) -> None:
    invalid = _price_row_with_decimal(value)

    with pytest.raises(
        PriceDataValidationError,
        match=r"'open'.*unsupported real numeric dtype.*decimal128\(20, 2\)",
    ):
        validate_price_data(invalid)


def test_arrow_decimal_price_is_rejected_before_deduplication() -> None:
    decimal_row = _price_row_with_decimal(Decimal("100.00"))
    duplicated = pd.concat([decimal_row, decimal_row], ignore_index=True)

    with pytest.raises(
        PriceDataValidationError,
        match=r"'open'.*unsupported real numeric dtype.*decimal128\(20, 2\)",
    ):
        deduplicate_price_data(duplicated)


def test_decimal_price_persistence_leaves_canonical_unchanged(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "etf_prices_daily.parquet"
    persist_price_history(_canonical_prices(), output_path)
    canonical_bytes = output_path.read_bytes()
    invalid = _price_row_with_decimal(Decimal("100.00"))
    invalid["date"] = pd.Timestamp("2024-01-04")
    invalid["retrieved_at"] = NEW_RETRIEVED_AT

    with pytest.raises(
        PriceDataValidationError,
        match=r"'open'.*unsupported real numeric dtype.*decimal128\(20, 2\)",
    ):
        persist_price_history(invalid, output_path)

    assert output_path.read_bytes() == canonical_bytes


def test_existing_decimal_price_parquet_fails_cleanly_without_mutation(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "etf_prices_daily.parquet"
    decimal_canonical = _price_row_with_decimal(Decimal("100.00"))
    decimal_canonical.to_parquet(output_path, index=False)
    canonical_bytes = output_path.read_bytes()
    assert pd.read_parquet(output_path)["open"].dtype == object

    with pytest.raises(
        PriceDataValidationError,
        match=r"'open'.*real numeric dtype.*received object",
    ):
        persist_price_history(_appended_price_row("2024-01-04"), output_path)

    assert output_path.read_bytes() == canonical_bytes


@pytest.mark.parametrize(
    ("values", "sparse_dtype"),
    [
        ([100, 102], pd.SparseDtype("int64", 0)),
        ([100.0, 102.0], pd.SparseDtype("float64", 0.0)),
        ([100.0, pd.NA], pd.SparseDtype("float64", float("nan"))),
    ],
    ids=["sparse-int64", "sparse-float64", "sparse-missing"],
)
def test_sparse_price_dtypes_are_rejected_cleanly(
    values: list[object],
    sparse_dtype: pd.SparseDtype,
) -> None:
    invalid = _canonical_prices()
    invalid["open"] = pd.Series(values, dtype=sparse_dtype)

    with pytest.raises(
        PriceDataValidationError,
        match=r"'open'.*unsupported real numeric dtype.*Sparse",
    ):
        validate_price_data(invalid)


def test_sparse_prices_are_rejected_before_duplicate_processing() -> None:
    duplicated = pd.concat(
        [_canonical_prices().iloc[[0]], _canonical_prices().iloc[[0]]],
        ignore_index=True,
    )
    duplicated["open"] = pd.Series([100, 100], dtype=pd.SparseDtype("int64", 0))

    with pytest.raises(
        PriceDataValidationError,
        match=r"'open'.*unsupported real numeric dtype.*Sparse",
    ):
        deduplicate_price_data(duplicated)


def test_sparse_price_persistence_leaves_canonical_unchanged(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "etf_prices_daily.parquet"
    persist_price_history(_canonical_prices(), output_path)
    canonical_bytes = output_path.read_bytes()
    invalid = _appended_price_row("2024-01-04")
    invalid["open"] = pd.Series(
        [100], index=invalid.index, dtype=pd.SparseDtype("int64", 0)
    )

    with pytest.raises(
        PriceDataValidationError,
        match=r"'open'.*unsupported real numeric dtype.*Sparse",
    ):
        persist_price_history(invalid, output_path)

    assert output_path.read_bytes() == canonical_bytes


def test_supported_numeric_dtypes_survive_parquet_round_trip(tmp_path: Path) -> None:
    output_path = tmp_path / "etf_prices_daily.parquet"
    prices = _canonical_prices()
    prices["open"] = pd.Series([100.0, 102.0], dtype="float64")
    prices["high"] = pd.Series([103.0, 105.0], dtype="Float64")
    prices["low"] = pd.Series([99, 101], dtype="int64")
    prices["close"] = pd.Series([102.0, 104.0], dtype="Float64")
    prices["adjusted_close"] = pd.Series([101.5, pd.NA], dtype="Float64")
    prices["volume"] = pd.Series([1_000_000, 1_200_000], dtype="Int64")

    validate_price_data(prices)
    result = persist_price_history(prices, output_path)
    reloaded = pd.read_parquet(output_path)

    assert tuple(reloaded.columns) == tuple(prices.columns)
    assert str(reloaded["date"].dtype) == str(prices["date"].dtype)
    assert str(reloaded["retrieved_at"].dtype) == str(prices["retrieved_at"].dtype)
    assert str(reloaded["open"].dtype) == "float64"
    assert str(reloaded["high"].dtype) == "Float64"
    assert str(reloaded["low"].dtype) == "int64"
    assert str(reloaded["close"].dtype) == "Float64"
    assert str(reloaded["adjusted_close"].dtype) == "Float64"
    assert str(reloaded["volume"].dtype) == "Int64"
    pd.testing.assert_frame_equal(reloaded, result.prices)
    validate_price_data(reloaded)


@pytest.mark.parametrize(
    ("existing_dtype", "incoming_dtype"),
    [
        ("double[pyarrow]", "Float64"),
        ("Float64", "double[pyarrow]"),
        ("float64", "Float64"),
    ],
)
def test_mixed_floating_price_backends_append_without_object_fallback(
    tmp_path: Path,
    existing_dtype: str,
    incoming_dtype: str,
) -> None:
    output_path = tmp_path / "etf_prices_daily.parquet"
    existing = _price_row_with_numeric_dtype("2024-01-02", existing_dtype)
    incoming = _price_row_with_numeric_dtype(
        "2024-01-03", incoming_dtype, retrieved_at=NEW_RETRIEVED_AT
    )

    persist_price_history(existing, output_path)
    result = persist_price_history(incoming, output_path)
    reloaded = pd.read_parquet(output_path)

    for column in PRICE_VALUE_COLUMNS:
        assert is_any_real_numeric_dtype(result.prices[column].dtype)
        assert result.prices[column].dtype != object
    assert result.prices["close"].tolist() == [102.0, 102.0]
    validate_price_data(reloaded)


def test_mixed_integer_price_backends_preserve_large_values_exactly(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "etf_prices_daily.parquet"
    large_integer = 9_007_199_254_740_993
    existing = _large_integer_price_row("2024-01-02", large_integer, "int64[pyarrow]")
    incoming = _large_integer_price_row(
        "2024-01-03",
        large_integer + 2,
        "Int64",
        retrieved_at=NEW_RETRIEVED_AT,
    )

    persist_price_history(existing, output_path)
    result = persist_price_history(incoming, output_path)

    for column in PRICE_VALUE_COLUMNS:
        assert is_any_real_numeric_dtype(result.prices[column].dtype)
        assert result.prices[column].tolist() == [large_integer, large_integer + 2]


def test_mixed_price_representation_preserves_missing_watermark_and_revisions(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "etf_prices_daily.parquet"
    snapshot_dir = tmp_path / "snapshots" / "prices"
    watermark_time = pd.Timestamp("2026-08-15T12:30:00Z")
    revision_time = pd.Timestamp("2026-08-16T12:30:00Z")
    existing = _price_row_with_numeric_dtype(
        "2024-01-02", "double[pyarrow]", adjusted_close=pd.NA
    )
    persist_price_history(existing, output_path)

    identical = _price_row_with_numeric_dtype(
        "2024-01-02",
        "Float64",
        adjusted_close=pd.NA,
        retrieved_at=watermark_time,
    )
    watermark_result = persist_price_history(
        identical,
        output_path,
        snapshot_dir=snapshot_dir,
        snapshot_time=SNAPSHOT_TIME,
    )

    assert watermark_result.revised_row_count == 0
    assert watermark_result.snapshot_path is None
    assert not snapshot_dir.exists()
    assert pd.isna(watermark_result.prices["adjusted_close"].iloc[0])
    assert watermark_result.prices["retrieved_at"].iloc[0] == watermark_time
    watermarked_file = output_path.read_bytes()

    revision = _price_row_with_numeric_dtype(
        "2024-01-02",
        "double[pyarrow]",
        adjusted_close=pd.NA,
        retrieved_at=revision_time,
    )
    revision["close"] = pd.Series(
        [102.5], index=revision.index, dtype="double[pyarrow]"
    )
    revised_result = persist_price_history(
        revision,
        output_path,
        snapshot_dir=snapshot_dir,
        snapshot_time=SNAPSHOT_TIME,
    )

    assert revised_result.revised_row_count == 1
    assert revised_result.snapshot_path is not None
    assert revised_result.snapshot_path.read_bytes() == watermarked_file
    assert revised_result.prices["close"].iloc[0] == 102.5
    assert pd.isna(revised_result.prices["adjusted_close"].iloc[0])
    assert revised_result.prices["retrieved_at"].iloc[0] == revision_time


@pytest.mark.parametrize(
    "row_order",
    [pytest.param([0, 1], id="ascending"), pytest.param([1, 0], id="reversed")],
)
def test_successful_coverage_date_matching_is_independent_of_incoming_row_order(
    tmp_path: Path,
    row_order: list[int],
) -> None:
    output_path = tmp_path / "etf_prices_daily.parquet"
    existing = _canonical_prices()
    persist_price_history(existing, output_path)
    incoming = existing.iloc[row_order].reset_index(drop=True)
    incoming["retrieved_at"] = NEW_RETRIEVED_AT

    result = persist_price_history(
        incoming,
        output_path,
        retrieval_statuses=(
            _retrieval_status(
                "SPY",
                "success",
                ("2024-01-02", "2024-01-03"),
                retrieved_at=NEW_RETRIEVED_AT.to_pydatetime(),
            ),
        ),
    )

    assert result.prices["date"].tolist() == [
        pd.Timestamp("2024-01-02"),
        pd.Timestamp("2024-01-03"),
    ]
    assert result.prices["retrieved_at"].eq(NEW_RETRIEVED_AT).all()


def test_coverage_validation_does_not_hide_conflicting_duplicate_date(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "etf_prices_daily.parquet"
    incoming = _canonical_prices()
    conflict = incoming.iloc[[0]].copy()
    conflict["close"] = 102.5
    duplicated = pd.concat([incoming, conflict], ignore_index=True)

    with pytest.raises(PriceDataValidationError, match="Conflicting market values"):
        persist_price_history(
            duplicated,
            output_path,
            retrieval_statuses=(
                _retrieval_status("SPY", "success", ("2024-01-02", "2024-01-03")),
            ),
        )


@pytest.mark.parametrize(
    ("row_order", "returned_dates"),
    [
        pytest.param([0], ("2024-01-02", "2024-01-03"), id="missing-date"),
        pytest.param([0, 1], ("2024-01-02",), id="extra-date"),
    ],
)
def test_coverage_dates_must_exactly_match_incoming_rows(
    tmp_path: Path,
    row_order: list[int],
    returned_dates: tuple[str, ...],
) -> None:
    output_path = tmp_path / "etf_prices_daily.parquet"
    incoming = _canonical_prices().iloc[row_order].reset_index(drop=True)

    with pytest.raises(
        PriceDataValidationError,
        match="coverage dates.*do not exactly match",
    ):
        persist_price_history(
            incoming,
            output_path,
            retrieval_statuses=(_retrieval_status("SPY", "success", returned_dates),),
        )


def test_coverage_ticker_must_exactly_match_incoming_rows(tmp_path: Path) -> None:
    output_path = tmp_path / "etf_prices_daily.parquet"
    incoming = _canonical_prices()
    incoming["ticker"] = "QQQ"

    with pytest.raises(
        PriceDataValidationError,
        match="coverage tickers do not exactly match",
    ):
        persist_price_history(
            incoming,
            output_path,
            retrieval_statuses=(
                _retrieval_status("SPY", "success", ("2024-01-02", "2024-01-03")),
            ),
        )


def test_coverage_timestamp_must_match_incoming_rows(tmp_path: Path) -> None:
    output_path = tmp_path / "etf_prices_daily.parquet"

    with pytest.raises(
        PriceDataValidationError,
        match="coverage timestamp.*does not match",
    ):
        persist_price_history(
            _canonical_prices(),
            output_path,
            retrieval_statuses=(
                _retrieval_status(
                    "SPY",
                    "success",
                    ("2024-01-02", "2024-01-03"),
                    retrieved_at=NEW_RETRIEVED_AT.to_pydatetime(),
                ),
            ),
        )


def test_in_coverage_vanished_price_rejects_transaction_without_snapshot(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "etf_prices_daily.parquet"
    snapshot_dir = tmp_path / "snapshots" / "prices"
    existing = _canonical_prices()
    persist_price_history(existing, output_path)
    canonical_bytes = output_path.read_bytes()
    incoming = existing.iloc[[0]].copy()

    with pytest.raises(
        PriceDataValidationError,
        match=r"vanished.*\('SPY', '2024-01-03'\).*manual review",
    ):
        persist_price_history(
            incoming,
            output_path,
            retrieval_statuses=(_retrieval_status("SPY", "success", ("2024-01-02",)),),
            snapshot_dir=snapshot_dir,
            snapshot_time=SNAPSHOT_TIME,
        )

    assert output_path.read_bytes() == canonical_bytes
    assert not snapshot_dir.exists()


def test_all_vanished_price_keys_are_reported_deterministically(tmp_path: Path) -> None:
    output_path = tmp_path / "etf_prices_daily.parquet"
    spy = _canonical_prices()
    qqq = spy.copy()
    qqq["ticker"] = "QQQ"
    existing = pd.concat([qqq, spy], ignore_index=True)
    persist_price_history(existing, output_path)
    incoming_spy = _appended_price_row("2024-01-04")
    incoming_qqq = incoming_spy.copy()
    incoming_qqq["ticker"] = "QQQ"
    incoming = pd.concat([incoming_spy, incoming_qqq], ignore_index=True)

    with pytest.raises(PriceDataValidationError) as error_info:
        persist_price_history(
            incoming,
            output_path,
            retrieval_statuses=(
                _retrieval_status(
                    "SPY",
                    "success",
                    ("2024-01-04",),
                    retrieved_at=NEW_RETRIEVED_AT.to_pydatetime(),
                ),
                _retrieval_status(
                    "QQQ",
                    "success",
                    ("2024-01-04",),
                    retrieved_at=NEW_RETRIEVED_AT.to_pydatetime(),
                ),
            ),
        )

    error_message = str(error_info.value)
    expected_keys = [
        "('QQQ', '2024-01-02')",
        "('QQQ', '2024-01-03')",
        "('SPY', '2024-01-02')",
        "('SPY', '2024-01-03')",
    ]
    assert all(key in error_message for key in expected_keys)
    assert [error_message.index(key) for key in expected_keys] == sorted(
        error_message.index(key) for key in expected_keys
    )


def test_existing_price_outside_successful_coverage_is_preserved(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "etf_prices_daily.parquet"
    existing = _canonical_prices().iloc[[0]].copy()
    persist_price_history(existing, output_path)
    incoming = _appended_price_row("2024-01-03")

    result = persist_price_history(
        incoming,
        output_path,
        retrieval_statuses=(
            _retrieval_status(
                "SPY",
                "success",
                ("2024-01-03",),
                start="2024-01-03",
                end="2024-01-04",
                retrieved_at=NEW_RETRIEVED_AT.to_pydatetime(),
            ),
        ),
    )

    assert result.prices["date"].tolist() == [
        pd.Timestamp("2024-01-02"),
        pd.Timestamp("2024-01-03"),
    ]


@pytest.mark.parametrize("non_success_status", ["failed", "empty"])
def test_failed_or_empty_ticker_does_not_trigger_disappearance(
    tmp_path: Path,
    non_success_status: str,
) -> None:
    output_path = tmp_path / "etf_prices_daily.parquet"
    spy = _canonical_prices()
    qqq = spy.copy()
    qqq["ticker"] = "QQQ"
    existing = pd.concat([spy, qqq], ignore_index=True)
    persist_price_history(existing, output_path)

    result = persist_price_history(
        spy,
        output_path,
        retrieval_statuses=(
            _retrieval_status("SPY", "success", ("2024-01-02", "2024-01-03")),
            _retrieval_status("QQQ", non_success_status),
        ),
    )

    assert set(result.prices["ticker"]) == {"SPY", "QQQ"}
    assert len(result.prices.loc[result.prices["ticker"].eq("QQQ")]) == 2


def test_unrequested_ticker_is_untouched_by_successful_coverage(tmp_path: Path) -> None:
    output_path = tmp_path / "etf_prices_daily.parquet"
    spy = _canonical_prices()
    qqq = spy.copy()
    qqq["ticker"] = "QQQ"
    persist_price_history(pd.concat([spy, qqq], ignore_index=True), output_path)

    result = persist_price_history(
        spy,
        output_path,
        retrieval_statuses=(
            _retrieval_status("SPY", "success", ("2024-01-02", "2024-01-03")),
        ),
    )

    assert len(result.prices.loc[result.prices["ticker"].eq("QQQ")]) == 2


def test_identical_successful_refresh_with_coverage_advances_provenance(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "etf_prices_daily.parquet"
    existing = _canonical_prices()
    persist_price_history(existing, output_path)
    incoming = existing.copy()
    incoming["retrieved_at"] = NEW_RETRIEVED_AT

    result = persist_price_history(
        incoming,
        output_path,
        retrieval_statuses=(
            _retrieval_status(
                "SPY",
                "success",
                ("2024-01-02", "2024-01-03"),
                retrieved_at=NEW_RETRIEVED_AT.to_pydatetime(),
            ),
        ),
    )

    assert result.revised_row_count == 0
    assert result.snapshot_path is None
    assert result.prices["retrieved_at"].eq(NEW_RETRIEVED_AT).all()


def test_genuine_revision_with_complete_coverage_keeps_snapshot_policy(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "etf_prices_daily.parquet"
    snapshot_dir = tmp_path / "snapshots" / "prices"
    existing = _canonical_prices()
    persist_price_history(existing, output_path)
    canonical_bytes = output_path.read_bytes()
    revision = existing.copy()
    revision["adjusted_close"] += 0.25
    revision["retrieved_at"] = NEW_RETRIEVED_AT

    result = persist_price_history(
        revision,
        output_path,
        retrieval_statuses=(
            _retrieval_status(
                "SPY",
                "success",
                ("2024-01-02", "2024-01-03"),
                retrieved_at=NEW_RETRIEVED_AT.to_pydatetime(),
            ),
        ),
        snapshot_dir=snapshot_dir,
        snapshot_time=SNAPSHOT_TIME,
    )

    assert result.revised_row_count == 2
    assert result.snapshot_path is not None
    assert result.snapshot_path.read_bytes() == canonical_bytes


def test_later_canonical_change_cannot_bypass_locked_disappearance_check(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "etf_prices_daily.parquet"
    returned_row = _appended_price_row("2024-01-03", retrieved_at=NEW_RETRIEVED_AT)
    stale_coverage = (
        _retrieval_status(
            "SPY",
            "success",
            ("2024-01-03",),
            retrieved_at=NEW_RETRIEVED_AT.to_pydatetime(),
        ),
    )
    persist_price_history(returned_row, output_path)
    concurrent_row = _canonical_prices().iloc[[0]].copy()
    persist_price_history(concurrent_row, output_path)
    canonical_after_concurrent_write = output_path.read_bytes()

    with pytest.raises(
        PriceDataValidationError,
        match=r"\('SPY', '2024-01-02'\)",
    ):
        persist_price_history(
            returned_row,
            output_path,
            retrieval_statuses=stale_coverage,
        )

    assert output_path.read_bytes() == canonical_after_concurrent_write
