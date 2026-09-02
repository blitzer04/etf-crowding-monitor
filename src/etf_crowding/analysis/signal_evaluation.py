"""Evaluate standalone price signals and publish reproducible local bundles."""

from __future__ import annotations

import hashlib
import json
import math
import shutil
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Literal, cast

import exchange_calendars as xcals  # type: ignore[import-untyped]
import numpy as np
import pandas as pd
import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.parquet as pq  # type: ignore[import-untyped]
from pandas.api.types import (
    is_any_real_numeric_dtype,
    is_bool_dtype,
    is_float_dtype,
    is_integer_dtype,
    is_object_dtype,
    is_unsigned_integer_dtype,
)

from etf_crowding import __version__
from etf_crowding.config import ETFDefinition, load_etf_universe
from etf_crowding.data.prices import (
    DEFAULT_PRICE_FILENAME,
    DEFAULT_PRICE_START_DATE,
    PriceDownloadCallable,
    PriceDownloadResult,
    TickerDownloadStatus,
    download_price_history,
    persist_price_history,
    validate_price_retrieval_statuses,
)
from etf_crowding.data.validation import (
    CANONICAL_PRICE_COLUMNS,
    PRICE_VALUE_COLUMNS,
    PriceDataValidationError,
    validate_price_data,
)
from etf_crowding.paths import (
    get_processed_data_dir,
    get_project_root,
    get_snapshot_data_dir,
)
from etf_crowding.signals import (
    MOMENTUM_OUTPUT_COLUMNS,
    VOLATILITY_OUTPUT_COLUMNS,
    calculate_momentum,
    calculate_volatility,
)

XNYS_CALENDAR_NAME = "XNYS"
DEFAULT_SIGNAL_EVALUATION_DIRNAME = "signal_evaluations"
EvaluationMode = Literal["offline", "refresh"]
EvaluationInstant = str | datetime | pd.Timestamp

COVERAGE_COLUMNS = (
    "ticker",
    "name",
    "category",
    "acquisition_status",
    "acquisition_error",
    "acquisition_rows_received",
    "acquisition_retrieved_at",
    "input_first_retrieved_at",
    "input_last_retrieved_at",
    "request_start",
    "request_end_exclusive",
    "target_session",
    "first_canonical_date",
    "last_canonical_date",
    "expected_xnys_observation_count",
    "present_xnys_observation_count",
    "missing_canonical_count",
    "missing_canonical_dates",
    "missing_adjusted_close_count",
    "missing_adjusted_close_dates",
    "first_adjusted_close_date",
    "last_adjusted_close_date",
    "target_price_row_present",
    "target_adjusted_close_present",
    "price_staleness_sessions",
    "momentum_first_raw_date",
    "momentum_last_raw_date",
    "momentum_first_normalized_date",
    "momentum_last_normalized_date",
    "momentum_target_raw_eligible",
    "momentum_target_normalized_eligible",
    "momentum_target_raw",
    "momentum_target_simple_return_pct",
    "momentum_target_percentile",
    "momentum_target_status",
    "momentum_target_normalization_status",
    "momentum_target_reference_count",
    "momentum_raw_staleness_sessions",
    "momentum_normalized_staleness_sessions",
    "volatility_first_raw_date",
    "volatility_last_raw_date",
    "volatility_first_normalized_date",
    "volatility_last_normalized_date",
    "volatility_target_raw_eligible",
    "volatility_target_normalized_eligible",
    "volatility_target_raw",
    "volatility_target_annualized_pct",
    "volatility_target_percentile",
    "volatility_target_status",
    "volatility_target_normalization_status",
    "volatility_target_reference_count",
    "volatility_raw_staleness_sessions",
    "volatility_normalized_staleness_sessions",
)

DEPENDENCE_COLUMNS = (
    "scope",
    "estimator",
    "ticker",
    "signal_date",
    "pair_count",
    "first_signal_date",
    "last_signal_date",
    "included_tickers",
    "universe_status",
    "status",
    "estimate",
)

_MOMENTUM_NUMERIC_COLUMNS = (
    "start_adjusted_close",
    "end_adjusted_close",
    "raw_momentum",
    "simple_return_pct",
    "momentum_percentile",
    "normalization_reference_count",
    "interior_missing_row_count",
    "interior_missing_adjusted_close_count",
)
_VOLATILITY_NUMERIC_COLUMNS = (
    "raw_annualized_volatility",
    "annualized_volatility_pct",
    "volatility_percentile",
    "normalization_reference_count",
    "missing_row_count",
    "missing_adjusted_close_count",
)
_COVERAGE_NUMERIC_COLUMNS = (
    "acquisition_rows_received",
    "expected_xnys_observation_count",
    "present_xnys_observation_count",
    "missing_canonical_count",
    "missing_adjusted_close_count",
    "price_staleness_sessions",
    "momentum_target_raw",
    "momentum_target_simple_return_pct",
    "momentum_target_percentile",
    "momentum_target_reference_count",
    "momentum_raw_staleness_sessions",
    "momentum_normalized_staleness_sessions",
    "volatility_target_raw",
    "volatility_target_annualized_pct",
    "volatility_target_percentile",
    "volatility_target_reference_count",
    "volatility_raw_staleness_sessions",
    "volatility_normalized_staleness_sessions",
)
_DEPENDENCE_NUMERIC_COLUMNS = ("pair_count", "estimate")

_ARTIFACT_FILENAMES = {
    "input_prices": "input_prices.parquet",
    "coverage": "coverage.parquet",
    "momentum": "momentum.parquet",
    "volatility": "volatility.parquet",
    "dependence": "dependence.parquet",
}
_DATE_LIST_TYPE = pa.list_(pa.field("element", pa.timestamp("ns"), nullable=True))
_STRING_LIST_TYPE = pa.list_(pa.field("element", pa.string(), nullable=True))
_UTC_TIMESTAMP_TYPE = pa.timestamp("ns", tz="UTC")
_TIMESTAMP_TYPE = pa.timestamp("ns")


class SignalEvaluationError(ValueError):
    """Indicate that a standalone signal evaluation cannot be completed safely."""


@dataclass(frozen=True, slots=True)
class EvaluationTarget:
    """Describe the captured evaluation instant and its resolved XNYS target.

    Attributes:
        captured_at: One UTC instant captured before any optional acquisition.
        target_session: Latest XNYS session closed by ``captured_at``.
        request_start: Inclusive canonical price-history start.
        request_end: Exclusive provider end, one calendar day after the target.
    """

    captured_at: pd.Timestamp
    target_session: pd.Timestamp
    request_start: date
    request_end: date


@dataclass(frozen=True, slots=True)
class SignalEvaluation:
    """Hold validated standalone signal outputs and coverage diagnostics.

    Attributes:
        mode: ``offline`` or explicitly requested ``refresh`` operation.
        target: Captured evaluation timing contract.
        input_prices: Exact canonical slice passed to both signal APIs.
        coverage: One diagnostic row per configured ETF.
        momentum: Unmodified native Momentum output.
        volatility: Unmodified native Volatility output.
        dependence: Exact-date descriptive Pearson and Spearman diagnostics.
        acquisition_statuses: Optional statuses from the refresh batch.
        universe: Immutable configured ETF definitions in configuration order.
    """

    mode: EvaluationMode
    target: EvaluationTarget
    input_prices: pd.DataFrame
    coverage: pd.DataFrame
    momentum: pd.DataFrame
    volatility: pd.DataFrame
    dependence: pd.DataFrame
    acquisition_statuses: tuple[TickerDownloadStatus, ...]
    universe: tuple[ETFDefinition, ...]


@dataclass(frozen=True, slots=True)
class SignalEvaluationRun:
    """Describe a completed evaluation and its transactionally published bundle.

    Attributes:
        evaluation: In-memory validated evaluation result.
        bundle_path: New non-overwriting local run directory.
        manifest: Validated manifest loaded from the published bundle.
    """

    evaluation: SignalEvaluation
    bundle_path: Path
    manifest: Mapping[str, object]


def _utc_now() -> pd.Timestamp:
    return pd.Timestamp.now(tz="UTC")


def _normalize_utc_instant(
    value: EvaluationInstant | None,
    *,
    name: str,
) -> pd.Timestamp:
    instant = pd.Timestamp(_utc_now() if value is None else value)
    if pd.isna(instant) or instant.tzinfo is None:
        raise SignalEvaluationError(f"{name} must be a valid timezone-aware instant.")
    return instant.tz_convert("UTC")


def resolve_evaluation_target(
    evaluation_instant: EvaluationInstant | None = None,
) -> EvaluationTarget:
    """Resolve the latest completed XNYS session from one UTC instant.

    No post-close grace period is imposed. Provider availability does not move
    the target backward; missing target data remains missing in the evaluation.

    Args:
        evaluation_instant: Optional timezone-aware instant. Defaults to one
            current UTC capture and is normalized to UTC.

    Returns:
        The captured instant, target session, and fixed request bounds.

    Raises:
        SignalEvaluationError: If the instant is invalid or predates the
            configured Day 8 price-history start.
    """

    captured_at = _normalize_utc_instant(evaluation_instant, name="evaluation_instant")
    captured_timestamp = pd.Timestamp(captured_at)
    local_date = captured_timestamp.tz_convert("America/New_York").date()
    calendar = xcals.get_calendar(
        XNYS_CALENDAR_NAME,
        start=pd.Timestamp(local_date) - timedelta(days=370),
        end=pd.Timestamp(local_date) + timedelta(days=370),
    )
    completed = calendar.closes.loc[calendar.closes.le(captured_timestamp)]
    if completed.empty:
        raise SignalEvaluationError(
            "The XNYS calendar did not contain a session completed by the "
            "evaluation instant."
        )
    target_session = pd.Timestamp(completed.index[-1])
    if target_session < pd.Timestamp(DEFAULT_PRICE_START_DATE):
        raise SignalEvaluationError(
            "The resolved target session precedes the Day 8 history start "
            f"{DEFAULT_PRICE_START_DATE.isoformat()}."
        )
    return EvaluationTarget(
        captured_at=captured_at,
        target_session=target_session,
        request_start=DEFAULT_PRICE_START_DATE,
        request_end=target_session.date() + timedelta(days=1),
    )


def _validate_evaluation_target(target: EvaluationTarget) -> None:
    if not isinstance(target.captured_at, pd.Timestamp):
        raise SignalEvaluationError(
            "EvaluationTarget.captured_at must be a timezone-aware UTC pandas "
            "Timestamp so nanosecond precision is preserved."
        )
    if target.captured_at.tzinfo is None or str(target.captured_at.tz) != "UTC":
        raise SignalEvaluationError(
            "EvaluationTarget.captured_at must use the UTC timezone."
        )
    if (
        not isinstance(target.target_session, pd.Timestamp)
        or target.target_session.tzinfo is not None
        or pd.isna(target.target_session)
        or target.target_session != target.target_session.normalize()
    ):
        raise SignalEvaluationError(
            "EvaluationTarget.target_session must be a normalized timezone-naive "
            "pandas Timestamp."
        )
    expected = resolve_evaluation_target(target.captured_at)
    if (
        target.target_session != expected.target_session
        or target.request_start != expected.request_start
        or target.request_end != expected.request_end
    ):
        raise SignalEvaluationError(
            "Evaluation target, request start, and exclusive request end do not "
            "match the captured UTC instant and pinned XNYS schedule."
        )


def _validate_universe(
    universe: Sequence[ETFDefinition],
) -> tuple[ETFDefinition, ...]:
    definitions = tuple(universe)
    if not definitions:
        raise SignalEvaluationError("The evaluation universe must not be empty.")
    tickers = [definition.ticker for definition in definitions]
    if len(set(tickers)) != len(tickers):
        raise SignalEvaluationError(
            "The evaluation universe contains duplicate tickers."
        )
    return definitions


def _evaluation_sessions(target: EvaluationTarget) -> pd.DatetimeIndex:
    _validate_evaluation_target(target)
    calendar = xcals.get_calendar(
        XNYS_CALENDAR_NAME,
        start=pd.Timestamp(target.request_start) - timedelta(days=370),
        end=target.target_session + timedelta(days=370),
    )
    if target.target_session not in calendar.sessions:
        raise SignalEvaluationError("target_session must be an XNYS regular session.")
    return pd.DatetimeIndex(
        calendar.sessions[
            (calendar.sessions >= pd.Timestamp(target.request_start))
            & (calendar.sessions <= target.target_session)
        ]
    )


def _prepare_input_prices(
    canonical_prices: pd.DataFrame,
    universe: Sequence[ETFDefinition],
    target: EvaluationTarget,
) -> pd.DataFrame:
    validate_price_data(canonical_prices)
    tickers = {definition.ticker for definition in universe}
    prepared = canonical_prices.loc[
        canonical_prices["ticker"].isin(tickers)
        & canonical_prices["date"].ge(pd.Timestamp(target.request_start))
        & canonical_prices["date"].le(target.target_session),
        list(CANONICAL_PRICE_COLUMNS),
    ].copy()
    if prepared.empty:
        raise SignalEvaluationError(
            "No configured ETF price observations are available between "
            f"{target.request_start.isoformat()} and "
            f"{target.target_session.date().isoformat()}."
        )
    prepared = prepared.sort_values(["ticker", "date"], kind="mergesort").reset_index(
        drop=True
    )
    validate_price_data(prepared)
    return prepared


def _staleness_sessions(
    latest_date: pd.Timestamp | None,
    sessions: pd.DatetimeIndex,
) -> int | None:
    if latest_date is None:
        return None
    position = int(sessions.searchsorted(latest_date))
    if position >= len(sessions) or sessions[position] != latest_date:
        raise SignalEvaluationError(
            f"Staleness date {latest_date.date().isoformat()} is not an XNYS session."
        )
    return len(sessions) - 1 - position


def _first_last_present_dates(
    frame: pd.DataFrame,
    value_column: str,
) -> tuple[pd.Timestamp | None, pd.Timestamp | None]:
    eligible = frame.loc[frame[value_column].notna(), "signal_date"]
    if eligible.empty:
        return None, None
    return pd.Timestamp(eligible.iloc[0]), pd.Timestamp(eligible.iloc[-1])


def _target_observation(
    frame: pd.DataFrame,
    ticker: str,
    target_session: pd.Timestamp,
) -> pd.Series | None:
    rows = frame.loc[
        frame["ticker"].eq(ticker) & frame["signal_date"].eq(target_session)
    ]
    if len(rows) > 1:
        raise SignalEvaluationError(
            f"Signal output contains duplicate target rows for {ticker}."
        )
    return None if rows.empty else rows.iloc[0]


def _optional_float(value: object) -> float | None:
    if value is None or value is pd.NA:
        return None
    if not isinstance(value, (int, float, np.integer, np.floating)) or isinstance(
        value, (bool, np.bool_)
    ):
        raise SignalEvaluationError("A signal diagnostic is not a numeric scalar.")
    converted = float(value)
    if math.isnan(converted):
        return None
    if not math.isfinite(converted):
        raise SignalEvaluationError("A present signal diagnostic is non-finite.")
    return converted


def _normalization_status(
    row: pd.Series | None,
    *,
    raw_column: str,
    percentile_column: str,
) -> str:
    if row is None:
        return "ticker_unavailable"
    if pd.isna(row[raw_column]):
        return "raw_ineligible"
    if pd.isna(row[percentile_column]):
        return "insufficient_reference_history"
    return "eligible"


def _acquisition_price_view(
    prices: pd.DataFrame,
    statuses: Sequence[TickerDownloadStatus],
) -> pd.DataFrame:
    """Select only canonical rows explicitly claimed by successful statuses."""

    acquisition_keys: set[tuple[str, pd.Timestamp]] = set()
    for status in statuses:
        if (
            not isinstance(status, TickerDownloadStatus)
            or type(status.ticker) is not str
            or status.status != "success"
            or type(status.returned_dates) is not tuple
        ):
            continue
        acquisition_keys.update(
            (status.ticker, pd.Timestamp(returned_date))
            for returned_date in status.returned_dates
            if type(returned_date) is date
        )

    if not acquisition_keys:
        return prices.iloc[0:0].copy()
    present_keys = zip(prices["ticker"].astype(str), prices["date"], strict=True)
    included = pd.Series(
        [
            (ticker, pd.Timestamp(price_date)) in acquisition_keys
            for ticker, price_date in present_keys
        ],
        index=prices.index,
        dtype=bool,
    )
    return prices.loc[included, list(CANONICAL_PRICE_COLUMNS)].copy()


def _validated_status_map(
    mode: EvaluationMode,
    prices: pd.DataFrame,
    universe: Sequence[ETFDefinition],
    target: EvaluationTarget,
    statuses: Sequence[TickerDownloadStatus],
) -> dict[str, TickerDownloadStatus]:
    status_tuple = tuple(statuses)
    if not status_tuple:
        if mode == "refresh":
            raise SignalEvaluationError(
                "Refresh acquisition provenance must cover every configured ETF "
                "exactly once."
            )
        return {}

    acquisition_view = _acquisition_price_view(prices, status_tuple)
    try:
        validate_price_retrieval_statuses(
            acquisition_view,
            status_tuple,
            expected_tickers=tuple(definition.ticker for definition in universe),
            expected_query_start=target.request_start,
            expected_query_end=target.request_end,
        )
    except PriceDataValidationError as error:
        raise SignalEvaluationError(
            "Acquisition provenance violates the canonical price-retrieval "
            "status contract."
        ) from error
    return {status.ticker: status for status in status_tuple}


def _validate_download_result_retrieval_metadata(
    result: PriceDownloadResult,
) -> None:
    observed = [
        pd.Timestamp(status.retrieved_at)
        for status in result.statuses
        if status.retrieved_at is not None
    ]
    expected = max(observed) if observed else None
    actual = result.retrieved_at
    if expected is None:
        if actual is not None:
            raise SignalEvaluationError(
                "Acquisition aggregate retrieved_at must be missing when every "
                "ticker request failed."
            )
        return
    if (
        not isinstance(actual, pd.Timestamp)
        or pd.isna(actual)
        or actual.tzinfo is None
        or str(actual.tz) != "UTC"
        or actual != expected
    ):
        raise SignalEvaluationError(
            "Acquisition aggregate retrieved_at does not exactly match the latest "
            "validated ticker retrieval timestamp."
        )


def _build_coverage(
    prices: pd.DataFrame,
    universe: Sequence[ETFDefinition],
    target: EvaluationTarget,
    momentum: pd.DataFrame,
    volatility: pd.DataFrame,
    *,
    status_by_ticker: Mapping[str, TickerDownloadStatus],
) -> pd.DataFrame:
    sessions = _evaluation_sessions(target)
    expected_dates = set(sessions)
    rows: list[dict[str, object]] = []

    for definition in universe:
        ticker = definition.ticker
        ticker_prices = prices.loc[prices["ticker"].eq(ticker)].sort_values(
            "date", kind="mergesort"
        )
        present_dates = set(pd.DatetimeIndex(ticker_prices["date"]))
        off_calendar_dates = present_dates.difference(expected_dates)
        if off_calendar_dates:
            formatted = sorted(value.date().isoformat() for value in off_calendar_dates)
            raise SignalEvaluationError(
                f"Canonical prices for {ticker} contain non-XNYS dates: {formatted}."
            )
        missing_dates = tuple(sorted(expected_dates.difference(present_dates)))
        missing_adjusted_dates = tuple(
            pd.DatetimeIndex(
                ticker_prices.loc[ticker_prices["adjusted_close"].isna(), "date"]
            )
        )
        adjusted_rows = ticker_prices.loc[ticker_prices["adjusted_close"].notna()]
        first_canonical = (
            None if ticker_prices.empty else pd.Timestamp(ticker_prices["date"].iloc[0])
        )
        last_canonical = (
            None
            if ticker_prices.empty
            else pd.Timestamp(ticker_prices["date"].iloc[-1])
        )
        first_adjusted = (
            None if adjusted_rows.empty else pd.Timestamp(adjusted_rows["date"].iloc[0])
        )
        last_adjusted = (
            None
            if adjusted_rows.empty
            else pd.Timestamp(adjusted_rows["date"].iloc[-1])
        )
        target_price = ticker_prices.loc[
            ticker_prices["date"].eq(target.target_session)
        ]
        target_row_present = not target_price.empty
        target_adjusted_present = bool(
            target_row_present and target_price["adjusted_close"].notna().iloc[0]
        )

        ticker_momentum = momentum.loc[momentum["ticker"].eq(ticker)]
        momentum_first_raw, momentum_last_raw = _first_last_present_dates(
            ticker_momentum, "raw_momentum"
        )
        momentum_first_normalized, momentum_last_normalized = _first_last_present_dates(
            ticker_momentum, "momentum_percentile"
        )
        momentum_target = _target_observation(momentum, ticker, target.target_session)
        momentum_raw_eligible = bool(
            momentum_target is not None and not pd.isna(momentum_target["raw_momentum"])
        )
        momentum_normalized_eligible = bool(
            momentum_target is not None
            and not pd.isna(momentum_target["momentum_percentile"])
        )

        ticker_volatility = volatility.loc[volatility["ticker"].eq(ticker)]
        volatility_first_raw, volatility_last_raw = _first_last_present_dates(
            ticker_volatility, "raw_annualized_volatility"
        )
        volatility_first_normalized, volatility_last_normalized = (
            _first_last_present_dates(ticker_volatility, "volatility_percentile")
        )
        volatility_target = _target_observation(
            volatility, ticker, target.target_session
        )
        volatility_raw_eligible = bool(
            volatility_target is not None
            and not pd.isna(volatility_target["raw_annualized_volatility"])
        )
        volatility_normalized_eligible = bool(
            volatility_target is not None
            and not pd.isna(volatility_target["volatility_percentile"])
        )

        acquisition = status_by_ticker.get(ticker)
        input_first_retrieved_at = (
            None
            if ticker_prices.empty
            else pd.Timestamp(ticker_prices["retrieved_at"].min())
        )
        input_last_retrieved_at = (
            None
            if ticker_prices.empty
            else pd.Timestamp(ticker_prices["retrieved_at"].max())
        )
        rows.append(
            {
                "ticker": ticker,
                "name": definition.name,
                "category": definition.category,
                "acquisition_status": (
                    "not_requested" if acquisition is None else acquisition.status
                ),
                "acquisition_error": None if acquisition is None else acquisition.error,
                "acquisition_rows_received": (
                    None if acquisition is None else acquisition.rows_received
                ),
                "acquisition_retrieved_at": (
                    None
                    if acquisition is None or acquisition.retrieved_at is None
                    else pd.Timestamp(acquisition.retrieved_at)
                ),
                "input_first_retrieved_at": input_first_retrieved_at,
                "input_last_retrieved_at": input_last_retrieved_at,
                "request_start": pd.Timestamp(target.request_start),
                "request_end_exclusive": pd.Timestamp(target.request_end),
                "target_session": target.target_session,
                "first_canonical_date": first_canonical,
                "last_canonical_date": last_canonical,
                "expected_xnys_observation_count": len(sessions),
                "present_xnys_observation_count": len(ticker_prices),
                "missing_canonical_count": len(missing_dates),
                "missing_canonical_dates": missing_dates,
                "missing_adjusted_close_count": len(missing_adjusted_dates),
                "missing_adjusted_close_dates": missing_adjusted_dates,
                "first_adjusted_close_date": first_adjusted,
                "last_adjusted_close_date": last_adjusted,
                "target_price_row_present": target_row_present,
                "target_adjusted_close_present": target_adjusted_present,
                "price_staleness_sessions": _staleness_sessions(
                    last_adjusted, sessions
                ),
                "momentum_first_raw_date": momentum_first_raw,
                "momentum_last_raw_date": momentum_last_raw,
                "momentum_first_normalized_date": momentum_first_normalized,
                "momentum_last_normalized_date": momentum_last_normalized,
                "momentum_target_raw_eligible": momentum_raw_eligible,
                "momentum_target_normalized_eligible": (momentum_normalized_eligible),
                "momentum_target_raw": (
                    None
                    if momentum_target is None
                    else _optional_float(momentum_target["raw_momentum"])
                ),
                "momentum_target_simple_return_pct": (
                    None
                    if momentum_target is None
                    else _optional_float(momentum_target["simple_return_pct"])
                ),
                "momentum_target_percentile": (
                    None
                    if momentum_target is None
                    else _optional_float(momentum_target["momentum_percentile"])
                ),
                "momentum_target_status": (
                    "ticker_unavailable"
                    if momentum_target is None
                    else str(momentum_target["endpoint_status"])
                ),
                "momentum_target_normalization_status": _normalization_status(
                    momentum_target,
                    raw_column="raw_momentum",
                    percentile_column="momentum_percentile",
                ),
                "momentum_target_reference_count": (
                    None
                    if momentum_target is None
                    else int(momentum_target["normalization_reference_count"])
                ),
                "momentum_raw_staleness_sessions": _staleness_sessions(
                    momentum_last_raw, sessions
                ),
                "momentum_normalized_staleness_sessions": _staleness_sessions(
                    momentum_last_normalized, sessions
                ),
                "volatility_first_raw_date": volatility_first_raw,
                "volatility_last_raw_date": volatility_last_raw,
                "volatility_first_normalized_date": volatility_first_normalized,
                "volatility_last_normalized_date": volatility_last_normalized,
                "volatility_target_raw_eligible": volatility_raw_eligible,
                "volatility_target_normalized_eligible": (
                    volatility_normalized_eligible
                ),
                "volatility_target_raw": (
                    None
                    if volatility_target is None
                    else _optional_float(volatility_target["raw_annualized_volatility"])
                ),
                "volatility_target_annualized_pct": (
                    None
                    if volatility_target is None
                    else _optional_float(volatility_target["annualized_volatility_pct"])
                ),
                "volatility_target_percentile": (
                    None
                    if volatility_target is None
                    else _optional_float(volatility_target["volatility_percentile"])
                ),
                "volatility_target_status": (
                    "ticker_unavailable"
                    if volatility_target is None
                    else str(volatility_target["window_status"])
                ),
                "volatility_target_normalization_status": _normalization_status(
                    volatility_target,
                    raw_column="raw_annualized_volatility",
                    percentile_column="volatility_percentile",
                ),
                "volatility_target_reference_count": (
                    None
                    if volatility_target is None
                    else int(volatility_target["normalization_reference_count"])
                ),
                "volatility_raw_staleness_sessions": _staleness_sessions(
                    volatility_last_raw, sessions
                ),
                "volatility_normalized_staleness_sessions": _staleness_sessions(
                    volatility_last_normalized, sessions
                ),
            }
        )

    coverage = pd.DataFrame(rows, columns=COVERAGE_COLUMNS)
    _validate_coverage(coverage, universe, len(sessions))
    return coverage


def _validate_coverage(
    coverage: pd.DataFrame,
    universe: Sequence[ETFDefinition],
    expected_session_count: int,
) -> None:
    if tuple(coverage.columns) != COVERAGE_COLUMNS:
        raise SignalEvaluationError("Coverage output columns violate the contract.")
    expected_tickers = [definition.ticker for definition in universe]
    if coverage["ticker"].tolist() != expected_tickers:
        raise SignalEvaluationError(
            "Coverage output must retain the configured universe order exactly."
        )
    if coverage["ticker"].duplicated().any():
        raise SignalEvaluationError("Coverage output contains duplicate ETFs.")
    records = cast(list[dict[str, object]], coverage.to_dict(orient="records"))
    for row in records:
        ticker = cast(str, row["ticker"])
        expected_count = cast(int, row["expected_xnys_observation_count"])
        present_count = cast(int, row["present_xnys_observation_count"])
        missing_count = cast(int, row["missing_canonical_count"])
        missing_dates = cast(Sequence[object], row["missing_canonical_dates"])
        missing_adjusted_count = cast(int, row["missing_adjusted_close_count"])
        missing_adjusted_dates = cast(
            Sequence[object], row["missing_adjusted_close_dates"]
        )
        if expected_count != expected_session_count:
            raise SignalEvaluationError("Coverage expected-session counts disagree.")
        if present_count + missing_count != expected_session_count:
            raise SignalEvaluationError(
                f"Coverage counts do not reconcile for {ticker}."
            )
        if len(missing_dates) != missing_count:
            raise SignalEvaluationError(
                f"Missing canonical dates do not reconcile for {ticker}."
            )
        if len(missing_adjusted_dates) != missing_adjusted_count:
            raise SignalEvaluationError(
                f"Missing adjusted-close dates do not reconcile for {ticker}."
            )


def _validate_present_finite_columns(
    frame: pd.DataFrame,
    columns: Sequence[str],
    *,
    component: str,
) -> None:
    duplicate_columns = frame.columns[frame.columns.duplicated()].unique().tolist()
    if duplicate_columns:
        raise SignalEvaluationError(
            f"{component} contains duplicate column labels: {duplicate_columns}."
        )
    missing_columns = [column for column in columns if column not in frame.columns]
    if missing_columns:
        raise SignalEvaluationError(
            f"{component} is missing required numeric columns: {missing_columns}."
        )
    for column in columns:
        values = frame[column]
        if is_object_dtype(values.dtype):
            for value in values.array:
                if value is None or value is pd.NA:
                    continue
                if not isinstance(
                    value, (int, float, np.integer, np.floating)
                ) or isinstance(value, (bool, np.bool_)):
                    raise SignalEvaluationError(
                        f"{component} input column '{column}' must contain only "
                        "real numeric or missing scalars."
                    )
                if not math.isfinite(float(value)):
                    raise SignalEvaluationError(
                        f"{component} input column '{column}' contains a present "
                        "non-finite value."
                    )
            continue
        if is_bool_dtype(values.dtype) or not is_any_real_numeric_dtype(values.dtype):
            raise SignalEvaluationError(
                f"{component} input column '{column}' must use a real numeric dtype."
            )
        # Nullable floating arrays can contain NaN in an unmasked storage slot.
        # dropna() removes only values declared missing by the dtype's mask, so
        # every remaining scalar must be finite before populations are formed.
        present = values.dropna().to_numpy(dtype=np.float64)
        invalid_positions = np.flatnonzero(~np.isfinite(present))
        if invalid_positions.size:
            raise SignalEvaluationError(
                f"{component} input column '{column}' contains a present "
                "non-finite value."
            )


def _correlation_result(
    first: np.ndarray,
    second: np.ndarray,
    estimator: Literal["pearson", "spearman"],
) -> tuple[str, float | None]:
    if len(first) < 3:
        return "insufficient_pairs", None
    if np.unique(first).size < 2 or np.unique(second).size < 2:
        return "constant_input", None
    estimate = float(pd.Series(first).corr(pd.Series(second), method=estimator))
    if not math.isfinite(estimate):
        raise SignalEvaluationError(
            f"{estimator.title()} correlation produced a non-finite estimate."
        )
    return "available", estimate


def calculate_dependence_diagnostics(
    momentum: pd.DataFrame,
    volatility: pd.DataFrame,
    universe: Sequence[ETFDefinition],
    sessions: pd.DatetimeIndex,
) -> pd.DataFrame:
    """Calculate exact-date descriptive dependence for standalone percentiles.

    Only rows with both native normalized percentiles present are paired. The
    function reports Pearson and Spearman estimates by ETF and by XNYS session,
    requires at least three nonconstant pairs, and calculates no pooled result
    or p-value.

    Args:
        momentum: Native output from ``calculate_momentum``.
        volatility: Native output from ``calculate_volatility``.
        universe: Configured ETFs in canonical order.
        sessions: XNYS evaluation sessions to report cross-sectionally.

    Returns:
        A deterministic DataFrame following ``DEPENDENCE_COLUMNS``.

    Raises:
        SignalEvaluationError: If keys are duplicated or a computed estimate is
            non-finite.
    """

    definitions = _validate_universe(universe)
    _validate_present_finite_columns(
        momentum,
        ("momentum_percentile",),
        component="Momentum dependence",
    )
    _validate_present_finite_columns(
        volatility,
        ("volatility_percentile",),
        component="Volatility dependence",
    )
    momentum_pairs = momentum.loc[
        momentum["momentum_percentile"].notna(),
        ["ticker", "signal_date", "momentum_percentile"],
    ]
    volatility_pairs = volatility.loc[
        volatility["volatility_percentile"].notna(),
        ["ticker", "signal_date", "volatility_percentile"],
    ]
    if momentum_pairs.duplicated(["ticker", "signal_date"]).any() or (
        volatility_pairs.duplicated(["ticker", "signal_date"]).any()
    ):
        raise SignalEvaluationError("Signal outputs contain duplicate dependence keys.")
    paired = momentum_pairs.merge(
        volatility_pairs,
        on=["ticker", "signal_date"],
        how="inner",
        validate="one_to_one",
        sort=False,
    )
    configured_tickers = [definition.ticker for definition in definitions]
    paired = paired.loc[paired["ticker"].isin(configured_tickers)].copy()
    ticker_order = {
        ticker: position for position, ticker in enumerate(configured_tickers)
    }
    rows: list[dict[str, object]] = []

    estimators: tuple[Literal["pearson", "spearman"], ...] = (
        "pearson",
        "spearman",
    )
    for ticker in configured_tickers:
        ticker_pairs = paired.loc[paired["ticker"].eq(ticker)].sort_values(
            "signal_date", kind="mergesort"
        )
        first = ticker_pairs["momentum_percentile"].to_numpy(dtype=np.float64)
        second = ticker_pairs["volatility_percentile"].to_numpy(dtype=np.float64)
        first_date = (
            None
            if ticker_pairs.empty
            else pd.Timestamp(ticker_pairs["signal_date"].iloc[0])
        )
        last_date = (
            None
            if ticker_pairs.empty
            else pd.Timestamp(ticker_pairs["signal_date"].iloc[-1])
        )
        for estimator in estimators:
            status, estimate = _correlation_result(first, second, estimator)
            rows.append(
                {
                    "scope": "per_etf",
                    "estimator": estimator,
                    "ticker": ticker,
                    "signal_date": None,
                    "pair_count": len(ticker_pairs),
                    "first_signal_date": first_date,
                    "last_signal_date": last_date,
                    "included_tickers": () if ticker_pairs.empty else (ticker,),
                    "universe_status": "not_applicable",
                    "status": status,
                    "estimate": estimate,
                }
            )

    configured_set = set(configured_tickers)
    paired_by_session = paired.assign(
        _ticker_order=paired["ticker"].map(ticker_order)
    ).sort_values(["signal_date", "_ticker_order"], kind="mergesort")
    session_groups = {
        cast(pd.Timestamp, signal_date): group.drop(columns="_ticker_order")
        for signal_date, group in paired_by_session.groupby(
            "signal_date", sort=False, observed=True
        )
    }
    for signal_date in sessions:
        session_pairs = session_groups.get(signal_date)
        if session_pairs is None:
            session_pairs = paired.iloc[0:0]
        included_tickers = tuple(session_pairs["ticker"].astype(str))
        full_universe = (
            len(included_tickers) == len(configured_tickers)
            and set(included_tickers) == configured_set
        )
        first = session_pairs["momentum_percentile"].to_numpy(dtype=np.float64)
        second = session_pairs["volatility_percentile"].to_numpy(dtype=np.float64)
        for estimator in estimators:
            status, estimate = _correlation_result(first, second, estimator)
            rows.append(
                {
                    "scope": "per_session",
                    "estimator": estimator,
                    "ticker": None,
                    "signal_date": signal_date,
                    "pair_count": len(session_pairs),
                    "first_signal_date": (signal_date if len(session_pairs) else None),
                    "last_signal_date": signal_date if len(session_pairs) else None,
                    "included_tickers": included_tickers,
                    "universe_status": (
                        "full_universe" if full_universe else "incomplete_universe"
                    ),
                    "status": status,
                    "estimate": estimate,
                }
            )

    dependence = pd.DataFrame(rows, columns=DEPENDENCE_COLUMNS)
    _validate_dependence(dependence, configured_tickers, sessions)
    return dependence


def _validate_dependence(
    dependence: pd.DataFrame,
    configured_tickers: Sequence[str],
    sessions: pd.DatetimeIndex,
) -> None:
    if tuple(dependence.columns) != DEPENDENCE_COLUMNS:
        raise SignalEvaluationError("Dependence output columns violate the contract.")
    if not set(dependence["scope"]).issubset({"per_etf", "per_session"}):
        raise SignalEvaluationError("Dependence output contains an invalid scope.")
    if not set(dependence["estimator"]).issubset({"pearson", "spearman"}):
        raise SignalEvaluationError("Dependence output contains an invalid estimator.")
    if dependence.duplicated(["scope", "estimator", "ticker", "signal_date"]).any():
        raise SignalEvaluationError("Dependence output contains duplicate keys.")
    expected_rows = 2 * (len(configured_tickers) + len(sessions))
    if len(dependence) != expected_rows:
        raise SignalEvaluationError(
            "Dependence output does not cover every required key."
        )
    available = dependence["status"].eq("available")
    if (
        dependence.loc[available, "estimate"].isna().any()
        or dependence.loc[~available, "estimate"].notna().any()
    ):
        raise SignalEvaluationError("Dependence status and estimates disagree.")
    if (dependence.loc[available, "pair_count"] < 3).any():
        raise SignalEvaluationError(
            "Dependence estimate is present with fewer than 3 pairs."
        )


def evaluate_price_signals(
    prices: pd.DataFrame,
    universe: Sequence[ETFDefinition],
    target: EvaluationTarget,
    *,
    mode: EvaluationMode = "offline",
    acquisition_statuses: Sequence[TickerDownloadStatus] = (),
) -> SignalEvaluation:
    """Evaluate native Momentum and Volatility over one canonical price vintage.

    The function never recalculates either financial formula. It calls the
    existing public signal APIs, retains their native outputs, preserves missing
    values, and adds only coverage, staleness, and descriptive exact-date
    dependence diagnostics.

    Args:
        prices: Canonical price data, potentially including dates outside the
            Day 8 evaluation scope.
        universe: Configured ETFs retained in coverage order.
        target: Captured XNYS timing and request bounds.
        mode: Offline or explicitly requested refresh mode.
        acquisition_statuses: Optional refresh statuses for every ETF.

    Returns:
        Validated in-memory evaluation outputs.

    Raises:
        SignalEvaluationError: If timing, universe, coverage, or derived
            diagnostics violate the evaluation contract.
        PriceDataValidationError: If canonical prices are invalid.
    """

    if mode not in {"offline", "refresh"}:
        raise SignalEvaluationError(f"Unsupported evaluation mode: {mode!r}.")
    _validate_evaluation_target(target)
    definitions = _validate_universe(universe)
    sessions = _evaluation_sessions(target)
    input_prices = _prepare_input_prices(prices, definitions, target)
    original = input_prices.copy(deep=True)
    status_by_ticker = _validated_status_map(
        mode,
        input_prices,
        definitions,
        target,
        acquisition_statuses,
    )

    momentum = calculate_momentum(input_prices, evaluation_end=target.target_session)
    volatility = calculate_volatility(
        input_prices, evaluation_end=target.target_session
    )
    pd.testing.assert_frame_equal(input_prices, original)
    if tuple(momentum.columns) != MOMENTUM_OUTPUT_COLUMNS:
        raise SignalEvaluationError("Momentum API returned an unexpected schema.")
    if tuple(volatility.columns) != VOLATILITY_OUTPUT_COLUMNS:
        raise SignalEvaluationError("Volatility API returned an unexpected schema.")

    coverage = _build_coverage(
        input_prices,
        definitions,
        target,
        momentum,
        volatility,
        status_by_ticker=status_by_ticker,
    )
    dependence = calculate_dependence_diagnostics(
        momentum, volatility, definitions, sessions
    )
    return SignalEvaluation(
        mode=mode,
        target=target,
        input_prices=input_prices,
        coverage=coverage,
        momentum=momentum,
        volatility=volatility,
        dependence=dependence,
        acquisition_statuses=tuple(acquisition_statuses),
        universe=definitions,
    )


def _assert_frame_exact(
    actual: pd.DataFrame,
    expected: pd.DataFrame,
    *,
    artifact: str,
) -> None:
    try:
        pd.testing.assert_frame_equal(
            actual,
            expected,
            check_dtype=True,
            check_exact=True,
            check_like=False,
            check_names=True,
        )
    except AssertionError as error:
        raise SignalEvaluationError(
            f"{artifact} does not match the validated evaluation contract."
        ) from error


def _validate_canonical_evaluation_input(
    prices: pd.DataFrame,
    universe: Sequence[ETFDefinition],
    target: EvaluationTarget,
) -> None:
    if not isinstance(prices, pd.DataFrame):
        raise SignalEvaluationError("Evaluation input prices must be a DataFrame.")
    if prices.empty:
        raise SignalEvaluationError(
            "A nonempty configured universe cannot publish an empty canonical input."
        )
    try:
        validate_price_data(prices)
    except ValueError as error:
        raise SignalEvaluationError(
            "Canonical evaluation input violates the price-data contract."
        ) from error
    if tuple(prices.columns) != CANONICAL_PRICE_COLUMNS:
        raise SignalEvaluationError(
            "Canonical evaluation input columns must use the exact contract order."
        )
    if not prices.index.equals(pd.RangeIndex(len(prices))):
        raise SignalEvaluationError(
            "Canonical evaluation input must use a deterministic zero-based index."
        )
    sorted_prices = prices.sort_values(
        ["ticker", "date"], kind="mergesort"
    ).reset_index(drop=True)
    if not prices.equals(sorted_prices):
        raise SignalEvaluationError(
            "Canonical evaluation input must be sorted by ticker and date."
        )
    configured_tickers = {definition.ticker for definition in universe}
    input_tickers = set(prices["ticker"].astype(str))
    unexpected_tickers = sorted(input_tickers.difference(configured_tickers))
    if unexpected_tickers:
        raise SignalEvaluationError(
            "Canonical evaluation input contains tickers outside the configured "
            f"universe: {unexpected_tickers}."
        )
    lower_bound = pd.Timestamp(target.request_start)
    invalid_bounds = prices["date"].lt(lower_bound) | prices["date"].gt(
        target.target_session
    )
    if invalid_bounds.any():
        raise SignalEvaluationError(
            "Canonical evaluation input contains dates outside the request start "
            "and target-session bounds."
        )
    sessions = _evaluation_sessions(target)
    invalid_sessions = pd.DatetimeIndex(prices["date"]).difference(sessions)
    if not invalid_sessions.empty:
        raise SignalEvaluationError(
            "Canonical evaluation input contains non-XNYS session dates."
        )


def validate_signal_evaluation(evaluation: SignalEvaluation) -> None:
    """Validate a complete evaluation without mutating or repairing it.

    The validator checks the canonical input boundary, rejects present non-finite
    derived values, regenerates native signals through their approved public
    calculators, and requires exact equality for coverage and dependence. This
    proves cross-artifact consistency without duplicating financial formulas.

    Args:
        evaluation: Candidate evaluation to validate before serialization or
            publication.

    Raises:
        SignalEvaluationError: If any input, native output, diagnostic, or
            cross-artifact relationship violates the Day 8 contract.
    """

    if not isinstance(evaluation, SignalEvaluation):
        raise SignalEvaluationError("Publication requires a SignalEvaluation result.")
    _validate_evaluation_target(evaluation.target)
    definitions = _validate_universe(evaluation.universe)
    _validate_canonical_evaluation_input(
        evaluation.input_prices, definitions, evaluation.target
    )

    expected_columns = {
        "Momentum": MOMENTUM_OUTPUT_COLUMNS,
        "Volatility": VOLATILITY_OUTPUT_COLUMNS,
        "coverage": COVERAGE_COLUMNS,
        "dependence": DEPENDENCE_COLUMNS,
    }
    frames = {
        "Momentum": evaluation.momentum,
        "Volatility": evaluation.volatility,
        "coverage": evaluation.coverage,
        "dependence": evaluation.dependence,
    }
    for component, columns in expected_columns.items():
        frame = frames[component]
        if not isinstance(frame, pd.DataFrame) or tuple(frame.columns) != columns:
            raise SignalEvaluationError(
                f"{component} columns do not match the evaluation contract."
            )

    _validate_present_finite_columns(
        evaluation.momentum,
        _MOMENTUM_NUMERIC_COLUMNS,
        component="Momentum",
    )
    _validate_present_finite_columns(
        evaluation.volatility,
        _VOLATILITY_NUMERIC_COLUMNS,
        component="Volatility",
    )
    _validate_present_finite_columns(
        evaluation.coverage,
        _COVERAGE_NUMERIC_COLUMNS,
        component="Coverage",
    )
    _validate_present_finite_columns(
        evaluation.dependence,
        _DEPENDENCE_NUMERIC_COLUMNS,
        component="Dependence",
    )

    try:
        expected = evaluate_price_signals(
            evaluation.input_prices,
            definitions,
            evaluation.target,
            mode=evaluation.mode,
            acquisition_statuses=evaluation.acquisition_statuses,
        )
    except SignalEvaluationError:
        raise
    except ValueError as error:
        raise SignalEvaluationError(
            "Native signal regeneration rejected the supplied evaluation input."
        ) from error

    _assert_frame_exact(
        evaluation.input_prices,
        expected.input_prices,
        artifact="Canonical input",
    )
    _assert_frame_exact(
        evaluation.momentum,
        expected.momentum,
        artifact="Momentum output",
    )
    _assert_frame_exact(
        evaluation.volatility,
        expected.volatility,
        artifact="Volatility output",
    )
    _assert_frame_exact(
        evaluation.coverage,
        expected.coverage,
        artifact="Coverage output",
    )
    _assert_frame_exact(
        evaluation.dependence,
        expected.dependence,
        artifact="Dependence output",
    )
    if evaluation.mode != expected.mode:
        raise SignalEvaluationError("Evaluation mode is internally inconsistent.")
    if evaluation.target != expected.target:
        raise SignalEvaluationError("Evaluation target is internally inconsistent.")
    if evaluation.universe != expected.universe:
        raise SignalEvaluationError("Evaluation universe is internally inconsistent.")
    if evaluation.acquisition_statuses != expected.acquisition_statuses:
        raise SignalEvaluationError(
            "Evaluation acquisition metadata are internally inconsistent."
        )


def _numeric_arrow_type(values: pd.Series) -> pa.DataType:
    dtype = values.dtype
    numpy_dtype = np.dtype(cast(Any, getattr(dtype, "numpy_dtype", dtype)))
    if is_integer_dtype(dtype):
        bit_width = numpy_dtype.itemsize * 8
        if is_unsigned_integer_dtype(dtype):
            return {
                8: pa.uint8(),
                16: pa.uint16(),
                32: pa.uint32(),
                64: pa.uint64(),
            }[bit_width]
        return {
            8: pa.int8(),
            16: pa.int16(),
            32: pa.int32(),
            64: pa.int64(),
        }[bit_width]
    if is_float_dtype(dtype):
        return pa.float32() if numpy_dtype.itemsize <= 4 else pa.float64()
    raise SignalEvaluationError(
        f"No deterministic Arrow numeric type exists for dtype {dtype}."
    )


def _arrow_array(
    values: Sequence[object] | pd.Series, data_type: pa.DataType
) -> pa.Array:
    prepared: Sequence[object]
    if pa.types.is_list(data_type):
        prepared = [
            None
            if value is None or value is pd.NA
            else list(cast(Sequence[object], value))
            for value in values
        ]
    elif isinstance(values, pd.Series):
        prepared = values.tolist()
    else:
        prepared = list(values)
    try:
        return pa.array(prepared, type=data_type, from_pandas=True, safe=True)
    except (pa.ArrowException, TypeError, ValueError) as error:
        raise SignalEvaluationError(
            f"Values cannot be represented losslessly as Arrow {data_type}."
        ) from error


def _table_from_fields(
    frame: pd.DataFrame,
    fields: Sequence[tuple[str, pa.DataType, bool]],
) -> pa.Table:
    expected_columns = tuple(name for name, _, _ in fields)
    if tuple(frame.columns) != expected_columns:
        raise SignalEvaluationError(
            f"Artifact columns differ from the explicit schema: {expected_columns}."
        )
    schema = pa.schema(
        [
            pa.field(name, data_type, nullable=nullable)
            for name, data_type, nullable in fields
        ]
    )
    arrays = [_arrow_array(frame[name], data_type) for name, data_type, _ in fields]
    for field, array in zip(schema, arrays, strict=True):
        if not field.nullable and array.null_count:
            raise SignalEvaluationError(
                f"Non-nullable artifact field {field.name!r} contains missing values."
            )
    table = pa.Table.from_arrays(arrays, schema=schema)
    table.validate(full=True)
    return table


def _input_price_table(prices: pd.DataFrame) -> pa.Table:
    validate_price_data(prices)
    fields: list[tuple[str, pa.DataType, bool]] = [
        ("date", _TIMESTAMP_TYPE, False),
        ("ticker", pa.string(), False),
    ]
    fields.extend(
        (column, _numeric_arrow_type(prices[column]), True)
        for column in PRICE_VALUE_COLUMNS
    )
    fields.append(("retrieved_at", _UTC_TIMESTAMP_TYPE, False))
    return _table_from_fields(prices.loc[:, list(CANONICAL_PRICE_COLUMNS)], fields)


def _momentum_table(momentum: pd.DataFrame) -> pa.Table:
    fields = [
        ("ticker", pa.string(), False),
        ("signal_date", _TIMESTAMP_TYPE, False),
        ("endpoint_start_date", _TIMESTAMP_TYPE, True),
        ("endpoint_end_date", _TIMESTAMP_TYPE, False),
        (
            "start_adjusted_close",
            _numeric_arrow_type(momentum["start_adjusted_close"]),
            True,
        ),
        (
            "end_adjusted_close",
            _numeric_arrow_type(momentum["end_adjusted_close"]),
            True,
        ),
        ("raw_momentum", pa.float64(), True),
        ("simple_return_pct", pa.float64(), True),
        ("simple_return_status", pa.string(), False),
        ("momentum_percentile", pa.float64(), True),
        ("normalization_reference_count", pa.int64(), False),
        ("first_prospective_session", _TIMESTAMP_TYPE, False),
        ("endpoint_eligible", pa.bool_(), False),
        ("endpoint_status", pa.string(), False),
        ("interior_missing_row_count", pa.int64(), True),
        ("interior_missing_row_dates", _DATE_LIST_TYPE, False),
        ("interior_missing_adjusted_close_count", pa.int64(), True),
        ("interior_missing_adjusted_close_dates", _DATE_LIST_TYPE, False),
    ]
    return _table_from_fields(momentum, fields)


def _volatility_table(volatility: pd.DataFrame) -> pa.Table:
    fields = [
        ("ticker", pa.string(), False),
        ("signal_date", _TIMESTAMP_TYPE, False),
        ("window_start_date", _TIMESTAMP_TYPE, False),
        ("window_end_date", _TIMESTAMP_TYPE, False),
        ("raw_annualized_volatility", pa.float64(), True),
        ("annualized_volatility_pct", pa.float64(), True),
        ("volatility_percentile", pa.float64(), True),
        ("normalization_reference_count", pa.int64(), False),
        ("first_prospective_session", _TIMESTAMP_TYPE, False),
        ("window_eligible", pa.bool_(), False),
        ("window_status", pa.string(), False),
        ("missing_row_count", pa.int64(), False),
        ("missing_row_dates", _DATE_LIST_TYPE, False),
        ("missing_adjusted_close_count", pa.int64(), False),
        ("missing_adjusted_close_dates", _DATE_LIST_TYPE, False),
    ]
    return _table_from_fields(volatility, fields)


def _coverage_table(coverage: pd.DataFrame) -> pa.Table:
    string_fields = {
        "ticker",
        "name",
        "category",
        "acquisition_status",
        "acquisition_error",
        "momentum_target_status",
        "momentum_target_normalization_status",
        "volatility_target_status",
        "volatility_target_normalization_status",
    }
    utc_fields = {
        "acquisition_retrieved_at",
        "input_first_retrieved_at",
        "input_last_retrieved_at",
    }
    date_fields = {
        column
        for column in COVERAGE_COLUMNS
        if column.endswith("_date")
        or column.endswith("_session")
        or column in {"request_start", "request_end_exclusive"}
    }
    list_fields = {"missing_canonical_dates", "missing_adjusted_close_dates"}
    bool_fields = {
        "target_price_row_present",
        "target_adjusted_close_present",
        "momentum_target_raw_eligible",
        "momentum_target_normalized_eligible",
        "volatility_target_raw_eligible",
        "volatility_target_normalized_eligible",
    }
    float_fields = {
        "momentum_target_raw",
        "momentum_target_simple_return_pct",
        "momentum_target_percentile",
        "volatility_target_raw",
        "volatility_target_annualized_pct",
        "volatility_target_percentile",
    }
    nonnullable_strings = {
        "ticker",
        "name",
        "category",
        "acquisition_status",
        "momentum_target_status",
        "momentum_target_normalization_status",
        "volatility_target_status",
        "volatility_target_normalization_status",
    }
    fields: list[tuple[str, pa.DataType, bool]] = []
    for column in COVERAGE_COLUMNS:
        if column in string_fields:
            fields.append((column, pa.string(), column not in nonnullable_strings))
        elif column in utc_fields:
            fields.append((column, _UTC_TIMESTAMP_TYPE, True))
        elif column in date_fields:
            nullable = column not in {
                "request_start",
                "request_end_exclusive",
                "target_session",
            }
            fields.append((column, _TIMESTAMP_TYPE, nullable))
        elif column in list_fields:
            fields.append((column, _DATE_LIST_TYPE, False))
        elif column in bool_fields:
            fields.append((column, pa.bool_(), False))
        elif column in float_fields:
            fields.append((column, pa.float64(), True))
        else:
            nullable = column in {
                "acquisition_rows_received",
                "price_staleness_sessions",
                "momentum_target_reference_count",
                "momentum_raw_staleness_sessions",
                "momentum_normalized_staleness_sessions",
                "volatility_target_reference_count",
                "volatility_raw_staleness_sessions",
                "volatility_normalized_staleness_sessions",
            }
            fields.append((column, pa.int64(), nullable))
    return _table_from_fields(coverage, fields)


def _dependence_table(dependence: pd.DataFrame) -> pa.Table:
    fields = [
        ("scope", pa.string(), False),
        ("estimator", pa.string(), False),
        ("ticker", pa.string(), True),
        ("signal_date", _TIMESTAMP_TYPE, True),
        ("pair_count", pa.int64(), False),
        ("first_signal_date", _TIMESTAMP_TYPE, True),
        ("last_signal_date", _TIMESTAMP_TYPE, True),
        ("included_tickers", _STRING_LIST_TYPE, False),
        ("universe_status", pa.string(), False),
        ("status", pa.string(), False),
        ("estimate", pa.float64(), True),
    ]
    return _table_from_fields(dependence, fields)


def _artifact_frames(evaluation: SignalEvaluation) -> dict[str, pd.DataFrame]:
    return {
        "input_prices": evaluation.input_prices,
        "coverage": evaluation.coverage,
        "momentum": evaluation.momentum,
        "volatility": evaluation.volatility,
        "dependence": evaluation.dependence,
    }


def _artifact_tables(evaluation: SignalEvaluation) -> dict[str, pa.Table]:
    frames = _artifact_frames(evaluation)
    return {
        "input_prices": _input_price_table(frames["input_prices"]),
        "coverage": _coverage_table(frames["coverage"]),
        "momentum": _momentum_table(frames["momentum"]),
        "volatility": _volatility_table(frames["volatility"]),
        "dependence": _dependence_table(frames["dependence"]),
    }


def _validate_table_semantics(
    source: pd.DataFrame,
    table: pa.Table,
    *,
    artifact: str,
) -> None:
    if tuple(table.column_names) != tuple(source.columns) or table.num_rows != len(
        source
    ):
        raise SignalEvaluationError(
            f"{artifact} Arrow conversion changed columns or row count."
        )
    for column in source.columns:
        pandas_missing = source[column].isna().to_numpy(dtype=bool)
        arrow_missing = np.asarray(table[column].is_null().to_pylist(), dtype=bool)
        if not np.array_equal(pandas_missing, arrow_missing):
            raise SignalEvaluationError(
                f"{artifact} column '{column}' changed its missing-value mask at "
                "the pandas-to-Arrow boundary."
            )


def _write_verified_table(
    table: pa.Table,
    path: Path,
    *,
    source: pd.DataFrame,
    artifact: str,
) -> None:
    pq.write_table(table, path, version="2.6")
    reloaded = pq.read_table(path)
    reloaded.validate(full=True)
    if not table.equals(reloaded, check_metadata=True):
        raise SignalEvaluationError(
            f"Parquet round trip changed artifact values or schema for {path.name}."
        )
    _validate_table_semantics(source, reloaded, artifact=artifact)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as input_file:
        while chunk := input_file.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _schema_manifest(schema: pa.Schema) -> list[dict[str, object]]:
    return [
        {
            "name": field.name,
            "type": str(field.type),
            "nullable": field.nullable,
        }
        for field in schema
    ]


def _universe_hash(universe: Sequence[ETFDefinition]) -> str:
    encoded = json.dumps(
        [asdict(definition) for definition in universe],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _package_versions() -> dict[str, str]:
    versions = {
        "etf-crowding-monitor": __version__,
        "python": sys.version.split()[0],
    }
    for package in (
        "exchange-calendars",
        "numpy",
        "pandas",
        "pyarrow",
        "scipy",
        "yfinance",
    ):
        try:
            versions[package] = version(package)
        except PackageNotFoundError:
            versions[package] = "unavailable"
    return versions


def _git_state(repository_root: Path) -> tuple[str, bool]:
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repository_root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=normal"],
            cwd=repository_root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as error:
        raise SignalEvaluationError("Git provenance could not be resolved.") from error
    if len(head) != 40:
        raise SignalEvaluationError("Git HEAD is not a full 40-character hash.")
    return head, bool(status.strip())


def _iso_date(value: date | pd.Timestamp | None) -> str | None:
    if value is None:
        return None
    return pd.Timestamp(value).date().isoformat()


def _iso_instant(value: datetime | pd.Timestamp | None) -> str | None:
    if value is None or pd.isna(value):
        return None
    instant = pd.Timestamp(value)
    if instant.tzinfo is None:
        raise SignalEvaluationError("Manifest instants must be timezone-aware.")
    return instant.tz_convert("UTC").isoformat().replace("+00:00", "Z")


def _ticker_metadata(evaluation: SignalEvaluation) -> list[dict[str, object]]:
    statuses = {status.ticker: status for status in evaluation.acquisition_statuses}
    rows: list[dict[str, object]] = []
    records = cast(
        list[dict[str, object]], evaluation.coverage.to_dict(orient="records")
    )
    for coverage_row in records:
        ticker = cast(str, coverage_row["ticker"])
        status = statuses.get(ticker)
        rows.append(
            {
                "ticker": ticker,
                "acquisition_status": (
                    "not_requested" if status is None else status.status
                ),
                "error": None if status is None else status.error,
                "rows_received": None if status is None else status.rows_received,
                "retrieved_at": _iso_instant(
                    cast(
                        datetime | pd.Timestamp | None,
                        coverage_row["acquisition_retrieved_at"],
                    )
                ),
                "query_start": (
                    None if status is None else status.query_start.isoformat()
                ),
                "query_end_exclusive": (
                    None if status is None else status.query_end.isoformat()
                ),
                "first_returned_date": (
                    None if status is None else _iso_date(status.first_date)
                ),
                "last_returned_date": (
                    None if status is None else _iso_date(status.last_date)
                ),
                "input_rows": int(
                    cast(int, coverage_row["present_xnys_observation_count"])
                ),
                "input_first_retrieved_at": _iso_instant(
                    cast(
                        datetime | pd.Timestamp | None,
                        coverage_row["input_first_retrieved_at"],
                    )
                ),
                "input_last_retrieved_at": _iso_instant(
                    cast(
                        datetime | pd.Timestamp | None,
                        coverage_row["input_last_retrieved_at"],
                    )
                ),
            }
        )
    return rows


def _run_id(creation_time: pd.Timestamp) -> str:
    return creation_time.strftime("%Y%m%dT%H%M%S%fZ")


def _validate_manifest(
    manifest_path: Path,
    expected: Mapping[str, object],
) -> dict[str, object]:
    loaded = cast(
        dict[str, object], json.loads(manifest_path.read_text(encoding="utf-8"))
    )
    if loaded != expected:
        raise SignalEvaluationError("The reloaded manifest differs from its source.")
    artifacts = cast(dict[str, dict[str, object]], loaded.get("artifacts"))
    if "manifest" in artifacts or "manifest.json" in artifacts:
        raise SignalEvaluationError("The manifest must not contain its own hash.")
    for metadata in artifacts.values():
        filename = str(metadata["filename"])
        path = manifest_path.parent / filename
        table = pq.read_table(path)
        if table.num_rows != metadata["row_count"]:
            raise SignalEvaluationError(f"Manifest row count failed for {filename}.")
        if _schema_manifest(table.schema) != metadata["schema"]:
            raise SignalEvaluationError(f"Manifest schema failed for {filename}.")
        if _sha256_file(path) != metadata["sha256"]:
            raise SignalEvaluationError(f"Manifest hash failed for {filename}.")
    return loaded


def _validate_bundle_at_path(
    bundle_path: Path,
    expected_manifest: Mapping[str, object],
    evaluation: SignalEvaluation,
    expected_tables: Mapping[str, pa.Table],
) -> dict[str, object]:
    manifest_path = bundle_path / "manifest.json"
    _validate_manifest(manifest_path, expected_manifest)
    frames = _artifact_frames(evaluation)
    for artifact_name, filename in _ARTIFACT_FILENAMES.items():
        reloaded = pq.read_table(bundle_path / filename)
        reloaded.validate(full=True)
        expected_table = expected_tables[artifact_name]
        if not expected_table.equals(reloaded, check_metadata=True):
            raise SignalEvaluationError(
                f"Final-path artifact {filename} differs from its validated source."
            )
        _validate_table_semantics(
            frames[artifact_name],
            reloaded,
            artifact=artifact_name,
        )
    validate_signal_evaluation(evaluation)
    # Reopen and rehash last so mutations during table or holistic validation
    # cannot escape the final publication check.
    return _validate_manifest(manifest_path, expected_manifest)


def _quarantine_invalid_bundle(final_path: Path) -> Path:
    quarantine_path = final_path.with_name(
        f"{final_path.name}.invalid-{uuid.uuid4().hex}"
    )
    final_path.rename(quarantine_path)
    return quarantine_path


def publish_signal_evaluation_bundle(
    evaluation: SignalEvaluation,
    output_root: Path,
    *,
    command_arguments: Sequence[str] = (),
    creation_time: EvaluationInstant | None = None,
    repository_root: Path | None = None,
) -> SignalEvaluationRun:
    """Publish one validated run bundle transactionally at directory level.

    Every Parquet file uses an explicit Arrow schema and is verified after
    reload. The manifest is written last inside a sibling temporary directory;
    only a fully validated directory is renamed to its final unique run ID.

    Args:
        evaluation: Validated in-memory signal evaluation.
        output_root: Parent directory for uniquely named run directories.
        command_arguments: Exact command arguments recorded for provenance.
        creation_time: Optional timezone-aware UTC-based run-ID source.
        repository_root: Git checkout used for commit and dirty-state metadata.

    Returns:
        The evaluation, published bundle path, and reloaded manifest.

    Raises:
        FileExistsError: If the UTC-based run directory already exists.
        SignalEvaluationError: If serialization, reload, provenance, or manifest
            validation fails.
        OSError: If the bundle cannot be written or published.
    """

    created_at = _normalize_utc_instant(creation_time, name="creation_time")
    run_id = _run_id(created_at)
    output_root = output_root.resolve()
    final_path = output_root / run_id
    if final_path.exists():
        raise FileExistsError(f"Signal evaluation run already exists: {final_path}.")

    # Provenance must describe the checkout before an unignored custom output
    # path can make that checkout dirty.
    resolved_repository_root = repository_root or get_project_root()
    git_head, worktree_dirty = _git_state(resolved_repository_root)
    validate_signal_evaluation(evaluation)
    frames = _artifact_frames(evaluation)
    tables = _artifact_tables(evaluation)

    output_root.mkdir(parents=True, exist_ok=True)
    temporary_path = Path(tempfile.mkdtemp(prefix=f".{run_id}.tmp-", dir=output_root))
    try:
        artifact_metadata: dict[str, dict[str, object]] = {}
        for artifact_name, filename in _ARTIFACT_FILENAMES.items():
            table = tables[artifact_name]
            artifact_path = temporary_path / filename
            _write_verified_table(
                table,
                artifact_path,
                source=frames[artifact_name],
                artifact=artifact_name,
            )
            artifact_metadata[artifact_name] = {
                "filename": filename,
                "schema": _schema_manifest(table.schema),
                "row_count": table.num_rows,
                "sha256": _sha256_file(artifact_path),
            }

        input_metadata = artifact_metadata["input_prices"]
        manifest: dict[str, object] = {
            "run_id": run_id,
            "git_head": git_head,
            "worktree_dirty": worktree_dirty,
            "command_arguments": list(command_arguments),
            "mode": evaluation.mode,
            "captured_utc_reference_instant": _iso_instant(
                evaluation.target.captured_at
            ),
            "target_xnys_session": _iso_date(evaluation.target.target_session),
            "request_start": evaluation.target.request_start.isoformat(),
            "request_end_exclusive": evaluation.target.request_end.isoformat(),
            "package_versions": _package_versions(),
            "universe_config_sha256": _universe_hash(evaluation.universe),
            "input_sha256": input_metadata["sha256"],
            "input_row_count": input_metadata["row_count"],
            "ticker_metadata": _ticker_metadata(evaluation),
            "artifacts": artifact_metadata,
            "created_at": _iso_instant(created_at),
        }
        manifest_path = temporary_path / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        _validate_manifest(manifest_path, manifest)
        temporary_path.rename(final_path)
        try:
            loaded_manifest = _validate_bundle_at_path(
                final_path,
                manifest,
                evaluation,
                tables,
            )
        except Exception as validation_error:
            try:
                quarantine_path = _quarantine_invalid_bundle(final_path)
            except OSError as quarantine_error:
                publication_error = SignalEvaluationError(
                    "Final-path bundle validation failed and the exact newly "
                    "published directory could not be quarantined."
                )
                publication_error.add_note(f"Quarantine failure: {quarantine_error!r}")
                raise publication_error from validation_error
            raise SignalEvaluationError(
                "Final-path bundle validation failed; the invalid run was "
                f"quarantined as {quarantine_path.name}."
            ) from validation_error
        return SignalEvaluationRun(
            evaluation=evaluation,
            bundle_path=final_path,
            manifest=loaded_manifest,
        )
    except Exception:
        shutil.rmtree(temporary_path, ignore_errors=True)
        raise


def _load_canonical_prices(path: Path) -> pd.DataFrame:
    if not path.is_file():
        raise SignalEvaluationError(
            f"Canonical price dataset does not exist: {path.resolve()}. "
            "Run with --refresh only after separately authorizing provider access."
        )
    try:
        prices = pd.read_parquet(path)
    except (OSError, ValueError) as error:
        raise SignalEvaluationError(
            f"Canonical price dataset could not be loaded: {path.resolve()}."
        ) from error
    validate_price_data(prices)
    return prices


def run_signal_evaluation(
    *,
    refresh: bool = False,
    evaluation_instant: EvaluationInstant | None = None,
    price_path: Path | None = None,
    output_root: Path | None = None,
    command_arguments: Sequence[str] = (),
    downloader: PriceDownloadCallable | None = None,
    universe: Sequence[ETFDefinition] | None = None,
    snapshot_dir: Path | None = None,
    creation_time: EvaluationInstant | None = None,
    repository_root: Path | None = None,
) -> SignalEvaluationRun:
    """Run the offline-first standalone signal evaluation workflow.

    Offline mode is the default and never calls a provider. Refresh mode must
    be selected explicitly; it uses the existing per-ticker downloader and
    atomic canonical persistence APIs with no orchestration-level retry.

    Args:
        refresh: Explicitly authorize the live acquisition path when true.
        evaluation_instant: Optional timezone-aware instant resolved once before
            any refresh. Defaults to one current UTC capture.
        price_path: Canonical Parquet path. Defaults to the standard processed
            price path.
        output_root: Bundle root. Defaults to
            ``data/processed/signal_evaluations``.
        command_arguments: Exact CLI or caller arguments stored in the manifest.
        downloader: Optional test-only compatible price downloader. It is never
            called in offline mode.
        universe: Optional validated definitions. Defaults to the packaged
            configured universe.
        snapshot_dir: Optional canonical revision-snapshot destination.
        creation_time: Optional deterministic run-ID timestamp.
        repository_root: Optional Git checkout for provenance.

    Returns:
        The completed in-memory evaluation and published local bundle.

    Raises:
        SignalEvaluationError: If input data or acquisition are unavailable.
        OSError: If canonical or bundle persistence fails.
    """

    target = resolve_evaluation_target(evaluation_instant)
    definitions = _validate_universe(universe or load_etf_universe())
    resolved_price_path = price_path or (
        get_processed_data_dir() / DEFAULT_PRICE_FILENAME
    )
    resolved_output_root = output_root or (
        get_processed_data_dir() / DEFAULT_SIGNAL_EVALUATION_DIRNAME
    )
    statuses: tuple[TickerDownloadStatus, ...] = ()
    mode: EvaluationMode = "refresh" if refresh else "offline"

    if refresh:
        tickers = tuple(definition.ticker for definition in definitions)
        result = download_price_history(
            tickers=tickers,
            start=target.request_start,
            end=target.request_end,
            downloader=downloader,
        )
        statuses = result.statuses
        _validated_status_map(
            mode,
            result.prices,
            definitions,
            target,
            statuses,
        )
        _validate_download_result_retrieval_metadata(result)
        if result.prices.empty:
            raise SignalEvaluationError(
                "The refresh returned no usable configured ETF observations; "
                "no canonical dataset or evaluation bundle was written."
            )
        persist_price_history(
            result.prices,
            resolved_price_path,
            retrieval_statuses=result.statuses,
            snapshot_dir=snapshot_dir or (get_snapshot_data_dir() / "prices"),
        )

    canonical_prices = _load_canonical_prices(resolved_price_path)
    evaluation = evaluate_price_signals(
        canonical_prices,
        definitions,
        target,
        mode=mode,
        acquisition_statuses=statuses,
    )
    return publish_signal_evaluation_bundle(
        evaluation,
        resolved_output_root,
        command_arguments=command_arguments,
        creation_time=creation_time,
        repository_root=repository_root,
    )
