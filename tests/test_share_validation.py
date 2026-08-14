"""Tests for canonical ETF shares validation and persistence safeguards."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pytest
from pandas.api.types import is_any_real_numeric_dtype

import etf_crowding.data.shares as shares_module
from etf_crowding.data.share_validation import (
    ShareDataValidationError,
    deduplicate_share_data,
    validate_share_data,
)
from etf_crowding.data.shares import persist_shares_history

RETRIEVED_AT = datetime(2026, 8, 14, 10, 0, tzinfo=UTC)
NEW_RETRIEVED_AT = pd.Timestamp("2026-08-14T10:01:00Z")
SNAPSHOT_TIME = datetime(2026, 8, 14, 10, 2, tzinfo=UTC)
ARROW_DECIMAL_DTYPE = pd.ArrowDtype(pa.decimal128(20, 2))


def _canonical_shares() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": pd.to_datetime(["2024-01-02", "2024-01-03"]),
            "ticker": pd.Series(["SPY", "SPY"], dtype="string"),
            "shares_outstanding": pd.Series([100_000_000, 101_000_000], dtype="Int64"),
            "retrieved_at": pd.to_datetime([RETRIEVED_AT, RETRIEVED_AT], utc=True),
        }
    )


def _appended_share_row(
    date_value: str,
    *,
    retrieved_at: pd.Timestamp = NEW_RETRIEVED_AT,
) -> pd.DataFrame:
    row = _canonical_shares().iloc[[0]].copy()
    row["date"] = pd.Timestamp(date_value)
    row["retrieved_at"] = retrieved_at
    return row


def _share_row_with_numeric_dtype(
    date_value: str,
    value: object,
    dtype: str,
    *,
    retrieved_at: pd.Timestamp | datetime = RETRIEVED_AT,
) -> pd.DataFrame:
    row = _canonical_shares().iloc[[0]].copy()
    row["date"] = pd.Timestamp(date_value)
    row["shares_outstanding"] = pd.Series([value], index=row.index, dtype=dtype)
    row["retrieved_at"] = retrieved_at
    return row


def _share_row_with_decimal(value: Decimal | None) -> pd.DataFrame:
    row = _canonical_shares().iloc[[0]].copy()
    row["shares_outstanding"] = pd.Series(
        [value], index=row.index, dtype=ARROW_DECIMAL_DTYPE
    )
    return row


def _with_duplicate_column_label(shares: pd.DataFrame, column: str) -> pd.DataFrame:
    return pd.concat([shares, shares[[column]].copy()], axis="columns")


def _duplicate_shares_with_malformed_ticker(value: object) -> pd.DataFrame:
    duplicated = pd.concat(
        [_canonical_shares().iloc[[0]], _canonical_shares().iloc[[0]]],
        ignore_index=True,
    )
    ticker_values = np.empty(2, dtype=object)
    ticker_values[:] = [value, value]
    duplicated["ticker"] = pd.Series(ticker_values, dtype="object")
    return duplicated


def test_valid_canonical_shares_are_accepted() -> None:
    validate_share_data(_canonical_shares())


def test_required_and_unexpected_schema_are_rejected() -> None:
    missing = _canonical_shares().drop(columns="shares_outstanding")
    unexpected = _canonical_shares().assign(extra=1)

    with pytest.raises(ShareDataValidationError, match="missing required columns"):
        validate_share_data(missing)
    with pytest.raises(ShareDataValidationError, match="unexpected columns"):
        validate_share_data(unexpected)


@pytest.mark.parametrize("column", ["date", "ticker", "shares_outstanding"])
def test_duplicate_canonical_column_labels_are_rejected(column: str) -> None:
    invalid = _with_duplicate_column_label(_canonical_shares(), column)

    with pytest.raises(
        ShareDataValidationError, match=rf"duplicate column labels.*{column}"
    ):
        validate_share_data(invalid)


def test_blank_ticker_is_rejected() -> None:
    invalid = _canonical_shares()
    invalid.loc[0, "ticker"] = " "

    with pytest.raises(ShareDataValidationError, match="non-empty strings"):
        validate_share_data(invalid)


@pytest.mark.parametrize(
    "malformed_ticker",
    [["SPY"], np.array(["SPY"])],
    ids=["list", "numpy-array"],
)
def test_malformed_ticker_is_rejected_before_duplicate_hashing(
    malformed_ticker: object,
) -> None:
    invalid = _duplicate_shares_with_malformed_ticker(malformed_ticker)

    with pytest.raises(
        ShareDataValidationError, match="Ticker values must be non-empty strings"
    ):
        deduplicate_share_data(invalid)


def test_date_must_be_naive_normalized_datetime() -> None:
    string_date = _canonical_shares()
    string_date["date"] = string_date["date"].astype(str)
    with pytest.raises(ShareDataValidationError, match="timezone-naive datetime64"):
        validate_share_data(string_date)

    timezone_date = _canonical_shares()
    timezone_date["date"] = timezone_date["date"].dt.tz_localize("UTC")
    with pytest.raises(ShareDataValidationError, match="timezone-naive datetime64"):
        validate_share_data(timezone_date)

    intraday = _canonical_shares()
    intraday.loc[0, "date"] = pd.Timestamp("2024-01-02T12:00:00")
    with pytest.raises(ShareDataValidationError, match="normalized to midnight"):
        validate_share_data(intraday)


def test_missing_date_and_retrieval_timestamp_are_rejected() -> None:
    missing_date = _canonical_shares()
    missing_date.loc[0, "date"] = pd.NaT
    with pytest.raises(ShareDataValidationError, match="dates must not be missing"):
        validate_share_data(missing_date)

    missing_retrieval = _canonical_shares()
    missing_retrieval.loc[0, "retrieved_at"] = pd.NaT
    with pytest.raises(
        ShareDataValidationError, match="timestamps must not be missing"
    ):
        validate_share_data(missing_retrieval)


def test_retrieval_timestamp_must_be_timezone_aware_utc() -> None:
    naive = _canonical_shares()
    naive["retrieved_at"] = naive["retrieved_at"].dt.tz_localize(None)
    with pytest.raises(ShareDataValidationError, match="timezone-aware datetime64"):
        validate_share_data(naive)

    non_utc = _canonical_shares()
    non_utc["retrieved_at"] = non_utc["retrieved_at"].dt.tz_convert("America/New_York")
    with pytest.raises(ShareDataValidationError, match="must use UTC"):
        validate_share_data(non_utc)


@pytest.mark.parametrize(
    ("values", "dtype", "dtype_fragment"),
    [
        pytest.param([True, False], "bool", "bool", id="bool"),
        pytest.param([True, pd.NA], "boolean", "boolean", id="nullable-bool"),
        pytest.param([1 + 2j, 2 + 0j], "complex128", "complex128", id="complex"),
        pytest.param(["100", "101"], "string", "string", id="string"),
        pytest.param([100, 101], "object", "object", id="object"),
        pytest.param(
            pd.to_datetime(["2024-01-02", "2024-01-03"]),
            "datetime64[ns]",
            "datetime64",
            id="datetime",
        ),
        pytest.param(
            pd.to_timedelta([1, 2], unit="D"),
            "timedelta64[ns]",
            "timedelta64",
            id="timedelta",
        ),
        pytest.param([100, 101], "category", "category", id="categorical"),
    ],
)
def test_non_real_numeric_canonical_dtypes_are_rejected(
    values: object,
    dtype: str,
    dtype_fragment: str,
) -> None:
    invalid = _canonical_shares()
    invalid["shares_outstanding"] = pd.Series(values, dtype=dtype)

    with pytest.raises(
        ShareDataValidationError,
        match=rf"real numeric dtype.*{dtype_fragment}",
    ):
        validate_share_data(invalid)


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
def test_supported_real_numeric_dtypes_are_accepted(dtype: str) -> None:
    valid = _canonical_shares()
    valid["shares_outstanding"] = pd.Series([100, 101], dtype=dtype)

    validate_share_data(valid)


@pytest.mark.parametrize(
    "value",
    [Decimal("100.00"), None],
    ids=["finite-positive", "missing"],
)
def test_arrow_decimal_shares_are_rejected_cleanly(
    value: Decimal | None,
) -> None:
    invalid = _share_row_with_decimal(value)

    with pytest.raises(
        ShareDataValidationError,
        match=r"unsupported real numeric dtype.*decimal128\(20, 2\)\[pyarrow\]",
    ):
        validate_share_data(invalid)


def test_arrow_decimal_shares_are_rejected_before_deduplication() -> None:
    decimal_row = _share_row_with_decimal(Decimal("100.00"))
    duplicated = pd.concat([decimal_row, decimal_row], ignore_index=True)

    with pytest.raises(
        ShareDataValidationError,
        match=r"unsupported real numeric dtype.*decimal128\(20, 2\)\[pyarrow\]",
    ):
        deduplicate_share_data(duplicated)


def test_decimal_share_persistence_leaves_canonical_unchanged(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "etf_shares_outstanding.parquet"
    persist_shares_history(_canonical_shares(), output_path)
    canonical_bytes = output_path.read_bytes()
    invalid = _share_row_with_decimal(Decimal("100.00"))
    invalid["date"] = pd.Timestamp("2024-01-04")
    invalid["retrieved_at"] = NEW_RETRIEVED_AT

    with pytest.raises(
        ShareDataValidationError,
        match=r"unsupported real numeric dtype.*decimal128\(20, 2\)\[pyarrow\]",
    ):
        persist_shares_history(invalid, output_path)

    assert output_path.read_bytes() == canonical_bytes


def test_existing_decimal_share_parquet_fails_cleanly_without_mutation(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "etf_shares_outstanding.parquet"
    decimal_canonical = _share_row_with_decimal(Decimal("100.00"))
    decimal_canonical.to_parquet(output_path, index=False)
    canonical_bytes = output_path.read_bytes()
    assert pd.read_parquet(output_path)["shares_outstanding"].dtype == object

    with pytest.raises(
        ShareDataValidationError,
        match=r"real numeric dtype.*received object",
    ):
        persist_shares_history(_appended_share_row("2024-01-04"), output_path)

    assert output_path.read_bytes() == canonical_bytes


@pytest.mark.parametrize(
    ("values", "sparse_dtype"),
    [
        ([100, 101], pd.SparseDtype("int64", 0)),
        ([100.25, 101.5], pd.SparseDtype("float64", 0.0)),
        ([100.25, pd.NA], pd.SparseDtype("float64", float("nan"))),
    ],
    ids=["sparse-int64", "sparse-float64", "sparse-missing"],
)
def test_sparse_share_dtypes_are_rejected_cleanly(
    values: list[object],
    sparse_dtype: pd.SparseDtype,
) -> None:
    invalid = _canonical_shares()
    invalid["shares_outstanding"] = pd.Series(values, dtype=sparse_dtype)

    with pytest.raises(
        ShareDataValidationError,
        match=r"unsupported real numeric dtype.*Sparse",
    ):
        validate_share_data(invalid)


def test_sparse_shares_are_rejected_before_duplicate_processing() -> None:
    duplicated = pd.concat(
        [_canonical_shares().iloc[[0]], _canonical_shares().iloc[[0]]],
        ignore_index=True,
    )
    duplicated["shares_outstanding"] = pd.Series(
        [100, 100], dtype=pd.SparseDtype("int64", 0)
    )

    with pytest.raises(
        ShareDataValidationError,
        match=r"unsupported real numeric dtype.*Sparse",
    ):
        deduplicate_share_data(duplicated)


def test_sparse_share_persistence_leaves_canonical_unchanged(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "etf_shares_outstanding.parquet"
    persist_shares_history(_canonical_shares(), output_path)
    canonical_bytes = output_path.read_bytes()
    invalid = _appended_share_row("2024-01-04")
    invalid["shares_outstanding"] = pd.Series(
        [100], index=invalid.index, dtype=pd.SparseDtype("int64", 0)
    )

    with pytest.raises(
        ShareDataValidationError,
        match=r"unsupported real numeric dtype.*Sparse",
    ):
        persist_shares_history(invalid, output_path)

    assert output_path.read_bytes() == canonical_bytes


@pytest.mark.parametrize(
    ("invalid_value", "message"),
    [
        pytest.param(0.0, "must be positive", id="zero"),
        pytest.param(-1.0, "must be positive", id="negative"),
        pytest.param(float("inf"), "non-finite", id="positive-infinity"),
        pytest.param(float("-inf"), "non-finite", id="negative-infinity"),
    ],
)
def test_invalid_present_shares_values_are_rejected(
    invalid_value: float,
    message: str,
) -> None:
    invalid = _canonical_shares()
    invalid["shares_outstanding"] = invalid["shares_outstanding"].astype("Float64")
    invalid.loc[0, "shares_outstanding"] = invalid_value

    with pytest.raises(ShareDataValidationError, match=message):
        validate_share_data(invalid)


def test_dated_missing_shares_are_valid_and_not_filled() -> None:
    shares = _canonical_shares()
    shares["shares_outstanding"] = pd.Series([pd.NA, pd.NA], dtype="Float64")

    validate_share_data(shares)

    assert shares["shares_outstanding"].isna().all()


def test_exact_duplicate_observations_are_deduplicated_deterministically() -> None:
    original = _canonical_shares().iloc[[0]]
    duplicate = original.copy()
    duplicate["retrieved_at"] = NEW_RETRIEVED_AT
    duplicated = pd.concat([original, duplicate], ignore_index=True)

    result = deduplicate_share_data(duplicated)

    assert len(result) == 1
    assert result["retrieved_at"].iloc[0] == NEW_RETRIEVED_AT


def test_conflicting_duplicate_observations_are_rejected() -> None:
    original = _canonical_shares().iloc[[0]]
    conflict = original.copy()
    conflict["shares_outstanding"] = 100_500_000
    duplicated = pd.concat([original, conflict], ignore_index=True)

    with pytest.raises(ShareDataValidationError, match="Conflicting shares values"):
        deduplicate_share_data(duplicated)


@pytest.mark.parametrize(
    "malformed_value",
    [[100_000_000], np.array([100_000_000])],
    ids=["list", "numpy-array"],
)
def test_unhashable_object_shares_are_rejected_before_duplicate_hashing(
    malformed_value: object,
) -> None:
    duplicated = pd.concat(
        [_canonical_shares().iloc[[0]], _canonical_shares().iloc[[0]]],
        ignore_index=True,
    )
    values = np.empty(2, dtype=object)
    values[:] = [malformed_value, malformed_value]
    duplicated["shares_outstanding"] = pd.Series(values, dtype="object")

    with pytest.raises(
        ShareDataValidationError,
        match=r"'shares_outstanding'.*real numeric dtype.*object",
    ):
        deduplicate_share_data(duplicated)


def test_validate_rejects_duplicate_ticker_date_keys() -> None:
    duplicated = pd.concat(
        [_canonical_shares().iloc[[0]], _canonical_shares().iloc[[0]]],
        ignore_index=True,
    )

    with pytest.raises(ShareDataValidationError, match="duplicate ticker/date"):
        validate_share_data(duplicated)


def test_persistence_round_trip_uses_custom_canonical_path(tmp_path: Path) -> None:
    output_path = tmp_path / "custom" / "etf_shares_outstanding.parquet"

    result = persist_shares_history(_canonical_shares(), output_path)
    reloaded = pd.read_parquet(output_path)

    pd.testing.assert_frame_equal(reloaded, result.shares)
    assert result.revised_row_count == 0
    assert result.snapshot_path is None
    validate_share_data(reloaded)


def test_persistence_accepts_all_missing_dated_observations(tmp_path: Path) -> None:
    output_path = tmp_path / "etf_shares_outstanding.parquet"
    shares = _canonical_shares()
    shares["shares_outstanding"] = pd.Series([pd.NA, pd.NA], dtype="Float64")

    result = persist_shares_history(shares, output_path)

    assert len(result.shares) == 2
    assert result.shares["shares_outstanding"].isna().all()


def test_empty_canonical_input_is_rejected_without_output(tmp_path: Path) -> None:
    output_path = tmp_path / "etf_shares_outstanding.parquet"
    empty = _canonical_shares().iloc[0:0].copy()

    with pytest.raises(ValueError, match="No shares observations"):
        persist_shares_history(empty, output_path)

    assert not output_path.exists()


def test_repeated_persistence_does_not_duplicate_rows(tmp_path: Path) -> None:
    output_path = tmp_path / "etf_shares_outstanding.parquet"
    shares = _canonical_shares()

    persist_shares_history(shares, output_path)
    result = persist_shares_history(shares, output_path)

    assert len(result.shares) == 2
    assert result.revised_row_count == 0
    assert result.snapshot_path is None


def test_advanced_share_watermark_blocks_older_revision_and_snapshots_later_one(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "etf_shares_outstanding.parquet"
    snapshot_dir = tmp_path / "snapshots" / "shares"
    canonical = _canonical_shares()
    persist_shares_history(canonical, output_path)

    watermark_time = pd.Timestamp("2026-08-14T10:02:00Z")
    identical = canonical.iloc[[0]].copy()
    identical["retrieved_at"] = watermark_time
    watermark_result = persist_shares_history(
        identical,
        output_path,
        snapshot_dir=snapshot_dir,
        snapshot_time=SNAPSHOT_TIME,
    )

    watermark_row = watermark_result.shares.loc[
        watermark_result.shares["date"].eq(pd.Timestamp("2024-01-02"))
    ]
    assert watermark_row["shares_outstanding"].iloc[0] == 100_000_000
    assert watermark_row["retrieved_at"].iloc[0] == watermark_time
    assert watermark_result.revised_row_count == 0
    assert watermark_result.snapshot_path is None
    assert not snapshot_dir.exists()
    watermarked_file = output_path.read_bytes()

    stale_revision = canonical.iloc[[0]].copy()
    stale_revision["shares_outstanding"] = 100_500_000
    stale_revision["retrieved_at"] = NEW_RETRIEVED_AT
    with pytest.raises(ShareDataValidationError, match="older than the canonical"):
        persist_shares_history(
            stale_revision,
            output_path,
            snapshot_dir=snapshot_dir,
            snapshot_time=SNAPSHOT_TIME,
        )

    assert output_path.read_bytes() == watermarked_file
    assert not snapshot_dir.exists()

    later_revision_time = pd.Timestamp("2026-08-14T10:03:00Z")
    later_revision = canonical.iloc[[0]].copy()
    later_revision["shares_outstanding"] = 100_750_000
    later_revision["retrieved_at"] = later_revision_time
    revised_result = persist_shares_history(
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
    revised_row = revised_result.shares.loc[
        revised_result.shares["date"].eq(pd.Timestamp("2024-01-02"))
    ]
    assert revised_row["shares_outstanding"].iloc[0] == 100_750_000
    assert revised_row["retrieved_at"].iloc[0] == later_revision_time


def test_older_and_equal_identical_shares_do_not_move_watermark_backward(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "etf_shares_outstanding.parquet"
    canonical = _canonical_shares()
    watermark_time = pd.Timestamp("2026-08-14T10:02:00Z")
    canonical["retrieved_at"] = watermark_time
    persist_shares_history(canonical, output_path)

    older = _canonical_shares().iloc[[0]].copy()
    older_result = persist_shares_history(older, output_path)
    equal = canonical.iloc[[0]].copy()
    equal_result = persist_shares_history(equal, output_path)

    for result in (older_result, equal_result):
        retained = result.shares.loc[
            result.shares["date"].eq(pd.Timestamp("2024-01-02"))
        ]
        assert retained["retrieved_at"].iloc[0] == watermark_time
        assert result.revised_row_count == 0
        assert result.snapshot_path is None


def test_missing_share_watermark_blocks_older_missing_to_present_completion(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "etf_shares_outstanding.parquet"
    snapshot_dir = tmp_path / "snapshots" / "shares"
    existing = _canonical_shares()
    existing["shares_outstanding"] = pd.Series([pd.NA, 101_000_000], dtype="Float64")
    persist_shares_history(existing, output_path)

    watermark_time = pd.Timestamp("2026-08-14T10:02:00Z")
    identical_missing = existing.iloc[[0]].copy()
    identical_missing["retrieved_at"] = watermark_time
    watermark_result = persist_shares_history(identical_missing, output_path)
    watermarked_row = watermark_result.shares.loc[
        watermark_result.shares["date"].eq(pd.Timestamp("2024-01-02"))
    ]
    assert pd.isna(watermarked_row["shares_outstanding"].iloc[0])
    assert watermarked_row["retrieved_at"].iloc[0] == watermark_time
    watermarked_file = output_path.read_bytes()

    older_completion = _canonical_shares().iloc[[0]].copy()
    older_completion["retrieved_at"] = NEW_RETRIEVED_AT
    with pytest.raises(ShareDataValidationError, match="older than the canonical"):
        persist_shares_history(
            older_completion,
            output_path,
            snapshot_dir=snapshot_dir,
            snapshot_time=SNAPSHOT_TIME,
        )

    assert output_path.read_bytes() == watermarked_file
    assert not snapshot_dir.exists()


def test_newer_revision_snapshots_exact_superseded_canonical(tmp_path: Path) -> None:
    output_path = tmp_path / "etf_shares_outstanding.parquet"
    snapshot_dir = tmp_path / "snapshots" / "shares"
    shares = _canonical_shares()
    persist_shares_history(shares, output_path)
    original_bytes = output_path.read_bytes()
    revision = shares.iloc[[0]].copy()
    revision["shares_outstanding"] = 100_500_000
    revision["retrieved_at"] = NEW_RETRIEVED_AT

    result = persist_shares_history(
        revision,
        output_path,
        snapshot_dir=snapshot_dir,
        snapshot_time=SNAPSHOT_TIME,
    )

    assert result.revised_row_count == 1
    assert result.revised_tickers == ("SPY",)
    assert result.snapshot_path is not None
    assert result.snapshot_path.read_bytes() == original_bytes
    revised_row = result.shares.loc[
        result.shares["date"].eq(pd.Timestamp("2024-01-02"))
    ]
    assert revised_row["shares_outstanding"].iloc[0] == 100_500_000
    assert revised_row["retrieved_at"].iloc[0] == NEW_RETRIEVED_AT


def test_missing_to_present_completion_is_a_newer_revision(tmp_path: Path) -> None:
    output_path = tmp_path / "etf_shares_outstanding.parquet"
    existing = _canonical_shares()
    existing["shares_outstanding"] = pd.Series([pd.NA, 101_000_000], dtype="Float64")
    persist_shares_history(existing, output_path)
    incoming = existing.iloc[[0]].copy()
    incoming.loc[incoming.index[0], "shares_outstanding"] = 100_000_000
    incoming["retrieved_at"] = NEW_RETRIEVED_AT

    result = persist_shares_history(
        incoming,
        output_path,
        snapshot_dir=tmp_path / "snapshots" / "shares",
        snapshot_time=SNAPSHOT_TIME,
    )

    assert result.revised_row_count == 1
    assert result.shares["shares_outstanding"].notna().all()


def test_stale_revision_is_rejected_without_side_effects(tmp_path: Path) -> None:
    output_path = tmp_path / "etf_shares_outstanding.parquet"
    snapshot_dir = tmp_path / "snapshots" / "shares"
    existing = _canonical_shares()
    existing["retrieved_at"] = NEW_RETRIEVED_AT
    persist_shares_history(existing, output_path)
    original_bytes = output_path.read_bytes()
    stale = _canonical_shares().iloc[[0]].copy()
    stale["shares_outstanding"] = 100_500_000

    with pytest.raises(ShareDataValidationError, match="older than the canonical"):
        persist_shares_history(
            stale,
            output_path,
            snapshot_dir=snapshot_dir,
            snapshot_time=SNAPSHOT_TIME,
        )

    assert output_path.read_bytes() == original_bytes
    assert not snapshot_dir.exists()


def test_same_vintage_conflict_is_rejected(tmp_path: Path) -> None:
    output_path = tmp_path / "etf_shares_outstanding.parquet"
    persist_shares_history(_canonical_shares(), output_path)
    conflict = _canonical_shares().iloc[[0]].copy()
    conflict["shares_outstanding"] = 100_500_000

    with pytest.raises(ShareDataValidationError, match="same retrieved_at"):
        persist_shares_history(conflict, output_path)


def test_incoming_value_loss_is_rejected_without_modifying_canonical(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "etf_shares_outstanding.parquet"
    snapshot_dir = tmp_path / "snapshots" / "shares"
    existing = _canonical_shares()
    persist_shares_history(existing, output_path)
    original_bytes = output_path.read_bytes()
    incoming = existing.iloc[[0]].copy()
    incoming["shares_outstanding"] = pd.Series(
        [pd.NA], index=incoming.index, dtype="Float64"
    )
    incoming["retrieved_at"] = NEW_RETRIEVED_AT

    with pytest.raises(ShareDataValidationError, match="loses previously available"):
        persist_shares_history(
            incoming,
            output_path,
            snapshot_dir=snapshot_dir,
            snapshot_time=SNAPSHOT_TIME,
        )

    assert output_path.read_bytes() == original_bytes
    assert not snapshot_dir.exists()


def test_partial_update_preserves_other_ticker_history(tmp_path: Path) -> None:
    output_path = tmp_path / "etf_shares_outstanding.parquet"
    spy = _canonical_shares().iloc[[0]].copy()
    qqq = spy.copy()
    qqq["ticker"] = "QQQ"
    qqq["shares_outstanding"] = 80_000_000
    persist_shares_history(pd.concat([spy, qqq], ignore_index=True), output_path)
    incoming_spy = _appended_share_row("2024-01-04")

    result = persist_shares_history(incoming_spy, output_path)

    assert set(result.shares["ticker"]) == {"SPY", "QQQ"}
    assert len(result.shares.loc[result.shares["ticker"].eq("QQQ")]) == 1
    assert len(result.shares.loc[result.shares["ticker"].eq("SPY")]) == 2


def test_duplicate_labels_fail_before_creating_or_modifying_canonical(
    tmp_path: Path,
) -> None:
    new_output = tmp_path / "new" / "shares.parquet"
    invalid = _with_duplicate_column_label(_canonical_shares(), "shares_outstanding")
    with pytest.raises(ShareDataValidationError, match="duplicate column labels"):
        persist_shares_history(invalid, new_output)
    assert not new_output.exists()

    output_path = tmp_path / "shares.parquet"
    persist_shares_history(_canonical_shares(), output_path)
    original_bytes = output_path.read_bytes()
    with pytest.raises(ShareDataValidationError, match="duplicate column labels"):
        persist_shares_history(invalid, output_path)
    assert output_path.read_bytes() == original_bytes


def test_malformed_duplicate_shares_fail_before_persistence_mutation(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "shares.parquet"
    persist_shares_history(_canonical_shares(), output_path)
    canonical_bytes = output_path.read_bytes()
    invalid = pd.concat(
        [_canonical_shares().iloc[[0]], _canonical_shares().iloc[[0]]],
        ignore_index=True,
    )
    invalid["shares_outstanding"] = pd.Series(
        [[100_000_000], [100_000_000]], dtype="object"
    )

    with pytest.raises(
        ShareDataValidationError,
        match=r"'shares_outstanding'.*real numeric dtype.*object",
    ):
        persist_shares_history(invalid, output_path)

    assert output_path.read_bytes() == canonical_bytes


def test_malformed_existing_parquet_fails_cleanly_before_duplicate_hashing(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "shares.parquet"
    malformed = pd.concat(
        [_canonical_shares().iloc[[0]], _canonical_shares().iloc[[0]]],
        ignore_index=True,
    )
    malformed["shares_outstanding"] = pd.Series(
        [[100_000_000], [100_000_000]], dtype="object"
    )
    malformed.to_parquet(output_path, index=False)
    malformed_bytes = output_path.read_bytes()

    with pytest.raises(
        ShareDataValidationError,
        match=r"'shares_outstanding'.*real numeric dtype.*object",
    ):
        persist_shares_history(_appended_share_row("2024-01-04"), output_path)

    assert output_path.read_bytes() == malformed_bytes


def test_malformed_incoming_ticker_does_not_modify_canonical(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "shares.parquet"
    persist_shares_history(_canonical_shares(), output_path)
    canonical_bytes = output_path.read_bytes()
    invalid = _duplicate_shares_with_malformed_ticker(["SPY"])

    with pytest.raises(
        ShareDataValidationError, match="Ticker values must be non-empty strings"
    ):
        persist_shares_history(invalid, output_path)

    assert output_path.read_bytes() == canonical_bytes


def test_malformed_existing_ticker_parquet_fails_without_type_error(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "shares.parquet"
    malformed = _duplicate_shares_with_malformed_ticker(["SPY"])
    malformed.to_parquet(output_path, index=False)
    malformed_bytes = output_path.read_bytes()

    with pytest.raises(
        ShareDataValidationError, match="Ticker values must be non-empty strings"
    ):
        persist_shares_history(_appended_share_row("2024-01-04"), output_path)

    assert output_path.read_bytes() == malformed_bytes


def test_atomic_write_failure_leaves_existing_canonical_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_path = tmp_path / "etf_shares_outstanding.parquet"
    persist_shares_history(_canonical_shares(), output_path)
    original_bytes = output_path.read_bytes()

    def fail_write(data: pd.DataFrame, path: Path) -> None:
        del data, path
        raise OSError("synthetic canonical write failure")

    monkeypatch.setattr(shares_module, "_write_parquet_atomically", fail_write)

    with pytest.raises(OSError, match="synthetic canonical write failure"):
        persist_shares_history(_appended_share_row("2024-01-04"), output_path)

    assert output_path.read_bytes() == original_bytes


def test_atomic_writer_cleans_partial_temporary_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_path = tmp_path / "etf_shares_outstanding.parquet"
    persist_shares_history(_canonical_shares(), output_path)
    original_bytes = output_path.read_bytes()

    def fail_to_parquet(
        data: pd.DataFrame,
        path: Path,
        *,
        index: bool,
    ) -> None:
        del data, index
        Path(path).write_bytes(b"partial temporary parquet")
        raise OSError("synthetic parquet serialization failure")

    monkeypatch.setattr(pd.DataFrame, "to_parquet", fail_to_parquet)

    with pytest.raises(OSError, match="synthetic parquet serialization failure"):
        persist_shares_history(_appended_share_row("2024-01-04"), output_path)

    assert output_path.read_bytes() == original_bytes
    assert list(output_path.parent.glob("*.tmp.parquet")) == []


def test_lock_is_released_after_validation_failure(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "etf_shares_outstanding.parquet"
    invalid = _canonical_shares()
    invalid["shares_outstanding"] = invalid["shares_outstanding"].astype(str)

    with pytest.raises(ShareDataValidationError, match="real numeric dtype"):
        persist_shares_history(invalid, output_path)

    persist_shares_history(_canonical_shares(), output_path)
    assert output_path.exists()


def test_snapshot_failure_leaves_existing_canonical_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_path = tmp_path / "etf_shares_outstanding.parquet"
    shares = _canonical_shares()
    persist_shares_history(shares, output_path)
    original_bytes = output_path.read_bytes()
    revision = shares.iloc[[0]].copy()
    revision["shares_outstanding"] = 100_500_000
    revision["retrieved_at"] = NEW_RETRIEVED_AT

    def fail_snapshot(*args: object, **kwargs: object) -> Path:
        del args, kwargs
        raise OSError("synthetic snapshot failure")

    monkeypatch.setattr(shares_module, "_snapshot_existing_share_file", fail_snapshot)

    with pytest.raises(OSError, match="synthetic snapshot failure"):
        persist_shares_history(
            revision,
            output_path,
            snapshot_dir=tmp_path / "snapshots" / "shares",
            snapshot_time=SNAPSHOT_TIME,
        )

    assert output_path.read_bytes() == original_bytes


def test_lock_covers_read_validation_snapshot_and_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_path = tmp_path / "etf_shares_outstanding.parquet"
    shares = _canonical_shares()
    persist_shares_history(shares, output_path)
    revision = shares.iloc[[0]].copy()
    revision["shares_outstanding"] = 100_500_000
    revision["retrieved_at"] = NEW_RETRIEVED_AT
    state = {"active": False}
    events: list[str] = []
    original_read = shares_module.pd.read_parquet
    original_snapshot = shares_module._snapshot_existing_share_file
    original_write = shares_module._write_parquet_atomically

    @contextmanager
    def tracking_lock(path: Path) -> Iterator[None]:
        assert path == output_path
        state["active"] = True
        events.append("lock")
        try:
            yield
        finally:
            events.append("unlock")
            state["active"] = False

    def tracked_read(path: Path) -> pd.DataFrame:
        assert state["active"]
        events.append("read")
        return original_read(path)

    def tracked_snapshot(path: Path, destination: Path, timestamp: datetime) -> Path:
        assert state["active"]
        events.append("snapshot")
        return original_snapshot(path, destination, timestamp)

    def tracked_write(data: pd.DataFrame, path: Path) -> None:
        assert state["active"]
        events.append("write")
        original_write(data, path)

    monkeypatch.setattr(
        shares_module, "_shares_history_transaction_lock", tracking_lock
    )
    monkeypatch.setattr(shares_module.pd, "read_parquet", tracked_read)
    monkeypatch.setattr(
        shares_module, "_snapshot_existing_share_file", tracked_snapshot
    )
    monkeypatch.setattr(shares_module, "_write_parquet_atomically", tracked_write)

    persist_shares_history(
        revision,
        output_path,
        snapshot_dir=tmp_path / "snapshots" / "shares",
        snapshot_time=SNAPSHOT_TIME,
    )

    assert events[0] == "lock"
    assert events[-1] == "unlock"
    assert events.index("read") < events.index("snapshot") < events.index("write")


def test_concurrent_attempt_is_blocked_then_rereads_completed_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_path = tmp_path / "etf_shares_outstanding.parquet"
    persist_shares_history(_canonical_shares(), output_path)
    first_writer = _appended_share_row("2024-01-04")
    second_writer = _appended_share_row("2024-01-05")
    state = {"active": False, "blocked": 0, "triggered": False}
    observed_dates: list[set[pd.Timestamp]] = []
    original_read = shares_module.pd.read_parquet
    original_write = shares_module._write_parquet_atomically

    @contextmanager
    def exclusive_lock(path: Path) -> Iterator[None]:
        assert path == output_path
        if state["active"]:
            state["blocked"] = int(state["blocked"]) + 1
            raise TimeoutError("synthetic shares lock contention")
        state["active"] = True
        try:
            yield
        finally:
            state["active"] = False

    def tracked_read(path: Path) -> pd.DataFrame:
        existing = original_read(path)
        observed_dates.append(set(existing["date"]))
        return existing

    def interleaved_write(data: pd.DataFrame, path: Path) -> None:
        if not state["triggered"]:
            state["triggered"] = True
            with pytest.raises(TimeoutError, match="synthetic shares lock contention"):
                persist_shares_history(second_writer, output_path)
        original_write(data, path)

    monkeypatch.setattr(
        shares_module, "_shares_history_transaction_lock", exclusive_lock
    )
    monkeypatch.setattr(shares_module.pd, "read_parquet", tracked_read)
    monkeypatch.setattr(shares_module, "_write_parquet_atomically", interleaved_write)

    persist_shares_history(first_writer, output_path)
    persist_shares_history(second_writer, output_path)

    canonical = pd.read_parquet(output_path)
    assert state["blocked"] == 1
    assert pd.Timestamp("2024-01-04") not in observed_dates[0]
    assert pd.Timestamp("2024-01-04") in observed_dates[1]
    assert set(canonical["date"]) == {
        pd.Timestamp("2024-01-02"),
        pd.Timestamp("2024-01-03"),
        pd.Timestamp("2024-01-04"),
        pd.Timestamp("2024-01-05"),
    }


def test_custom_output_paths_use_independent_adjacent_locks(tmp_path: Path) -> None:
    first_output = tmp_path / "first" / "shares.parquet"
    second_output = tmp_path / "second" / "shares.parquet"
    first_lock = shares_module._shares_history_lock_path(first_output)
    second_lock = shares_module._shares_history_lock_path(second_output)

    assert first_lock != second_lock
    with shares_module._shares_history_transaction_lock(first_output):
        with shares_module._shares_history_transaction_lock(second_output):
            assert first_lock.exists()
            assert second_lock.exists()


@pytest.mark.parametrize(
    ("existing_dtype", "incoming_dtype"),
    [
        ("int64[pyarrow]", "Int64"),
        ("Int64", "int64[pyarrow]"),
        ("uint64[pyarrow]", "UInt64"),
    ],
)
def test_mixed_integer_share_backends_append_without_precision_loss(
    tmp_path: Path,
    existing_dtype: str,
    incoming_dtype: str,
) -> None:
    output_path = tmp_path / "etf_shares_outstanding.parquet"
    large_integer = 9_007_199_254_740_993
    existing = _share_row_with_numeric_dtype(
        "2024-01-02", large_integer, existing_dtype
    )
    incoming = _share_row_with_numeric_dtype(
        "2024-01-03",
        large_integer + 2,
        incoming_dtype,
        retrieved_at=NEW_RETRIEVED_AT,
    )

    persist_shares_history(existing, output_path)
    result = persist_shares_history(incoming, output_path)
    reloaded = pd.read_parquet(output_path)

    assert is_any_real_numeric_dtype(result.shares["shares_outstanding"].dtype)
    assert result.shares["shares_outstanding"].dtype != object
    assert result.shares["shares_outstanding"].tolist() == [
        large_integer,
        large_integer + 2,
    ]
    assert reloaded["shares_outstanding"].tolist() == [
        large_integer,
        large_integer + 2,
    ]
    validate_share_data(reloaded)


def test_mixed_share_backends_preserve_missing_values(tmp_path: Path) -> None:
    output_path = tmp_path / "etf_shares_outstanding.parquet"
    existing = _share_row_with_numeric_dtype("2024-01-02", pd.NA, "int64[pyarrow]")
    incoming = _share_row_with_numeric_dtype(
        "2024-01-03", pd.NA, "Int64", retrieved_at=NEW_RETRIEVED_AT
    )

    persist_shares_history(existing, output_path)
    result = persist_shares_history(incoming, output_path)

    assert is_any_real_numeric_dtype(result.shares["shares_outstanding"].dtype)
    assert result.shares["shares_outstanding"].isna().all()


@pytest.mark.parametrize(
    ("existing_dtype", "incoming_dtype"),
    [
        ("double[pyarrow]", "Float64"),
        ("Float64", "double[pyarrow]"),
        ("float64", "Float64"),
    ],
)
def test_mixed_floating_share_backends_append_without_object_fallback(
    tmp_path: Path,
    existing_dtype: str,
    incoming_dtype: str,
) -> None:
    output_path = tmp_path / "etf_shares_outstanding.parquet"
    existing = _share_row_with_numeric_dtype("2024-01-02", 100.25, existing_dtype)
    incoming = _share_row_with_numeric_dtype(
        "2024-01-03", 101.5, incoming_dtype, retrieved_at=NEW_RETRIEVED_AT
    )

    persist_shares_history(existing, output_path)
    result = persist_shares_history(incoming, output_path)

    assert is_any_real_numeric_dtype(result.shares["shares_outstanding"].dtype)
    assert result.shares["shares_outstanding"].dtype != object
    assert result.shares["shares_outstanding"].tolist() == [100.25, 101.5]


def test_mixed_share_representation_advances_watermark_then_revises_normally(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "etf_shares_outstanding.parquet"
    snapshot_dir = tmp_path / "snapshots" / "shares"
    large_integer = 9_007_199_254_740_993
    watermark_time = pd.Timestamp("2026-08-14T10:02:00Z")
    revision_time = pd.Timestamp("2026-08-14T10:03:00Z")
    existing = _share_row_with_numeric_dtype(
        "2024-01-02", large_integer, "int64[pyarrow]"
    )
    persist_shares_history(existing, output_path)

    identical = _share_row_with_numeric_dtype(
        "2024-01-02",
        large_integer,
        "Int64",
        retrieved_at=watermark_time,
    )
    watermark_result = persist_shares_history(
        identical,
        output_path,
        snapshot_dir=snapshot_dir,
        snapshot_time=SNAPSHOT_TIME,
    )

    assert watermark_result.revised_row_count == 0
    assert watermark_result.snapshot_path is None
    assert not snapshot_dir.exists()
    assert watermark_result.shares["shares_outstanding"].iloc[0] == large_integer
    assert watermark_result.shares["retrieved_at"].iloc[0] == watermark_time
    watermarked_file = output_path.read_bytes()

    revision = _share_row_with_numeric_dtype(
        "2024-01-02",
        large_integer + 2,
        "int64[pyarrow]",
        retrieved_at=revision_time,
    )
    revised_result = persist_shares_history(
        revision,
        output_path,
        snapshot_dir=snapshot_dir,
        snapshot_time=SNAPSHOT_TIME,
    )

    assert revised_result.revised_row_count == 1
    assert revised_result.snapshot_path is not None
    assert revised_result.snapshot_path.read_bytes() == watermarked_file
    assert revised_result.shares["shares_outstanding"].iloc[0] == large_integer + 2
    assert revised_result.shares["retrieved_at"].iloc[0] == revision_time


def test_incompatible_valid_share_representations_fail_without_precision_loss(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "etf_shares_outstanding.parquet"
    existing = _share_row_with_numeric_dtype(
        "2024-01-02", 9_007_199_254_740_993, "Int64"
    )
    persist_shares_history(existing, output_path)
    canonical_bytes = output_path.read_bytes()
    incoming = _share_row_with_numeric_dtype(
        "2024-01-03", 100.25, "Float64", retrieved_at=NEW_RETRIEVED_AT
    )

    with pytest.raises(
        ShareDataValidationError, match="Cannot losslessly combine canonical"
    ):
        persist_shares_history(incoming, output_path)

    assert output_path.read_bytes() == canonical_bytes
