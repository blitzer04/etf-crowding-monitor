"""Calculate the approved ETF Momentum percentile on XNYS sessions."""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from fractions import Fraction
from typing import cast

import exchange_calendars as xcals  # type: ignore[import-untyped]
import numpy as np
import pandas as pd

from etf_crowding.data.numeric_dtypes import (
    NumericDtypeHarmonizationError,
    build_lossless_real_numeric_series,
)
from etf_crowding.data.validation import validate_price_data

XNYS_CALENDAR_NAME = "XNYS"
MOMENTUM_LAG_SESSIONS = 252
MOMENTUM_NORMALIZATION_WINDOW_SESSIONS = 756
MOMENTUM_MINIMUM_PRIOR_OBSERVATIONS = 252
_REQUIRED_CALENDAR_PREHISTORY_SESSIONS = (
    MOMENTUM_LAG_SESSIONS + MOMENTUM_NORMALIZATION_WINDOW_SESSIONS - 1
)
_CALENDAR_CONSTRUCTION_PREHISTORY_DAYS = 6 * 366
_MAX_FLOAT64 = float(np.finfo(np.float64).max)
_SIMPLE_RETURN_AVAILABLE = "available"
_SIMPLE_RETURN_ENDPOINT_INELIGIBLE = "endpoint_ineligible"
_SIMPLE_RETURN_EXCEEDS_FLOAT64 = "exceeds_float64_range"

MOMENTUM_OUTPUT_COLUMNS = (
    "ticker",
    "signal_date",
    "endpoint_start_date",
    "endpoint_end_date",
    "start_adjusted_close",
    "end_adjusted_close",
    "raw_momentum",
    "simple_return_pct",
    "simple_return_status",
    "momentum_percentile",
    "normalization_reference_count",
    "first_prospective_session",
    "endpoint_eligible",
    "endpoint_status",
    "interior_missing_row_count",
    "interior_missing_row_dates",
    "interior_missing_adjusted_close_count",
    "interior_missing_adjusted_close_dates",
)

_DATE_COLUMNS = (
    "signal_date",
    "endpoint_start_date",
    "endpoint_end_date",
    "first_prospective_session",
)
_FLOAT_COLUMNS = (
    "raw_momentum",
    "simple_return_pct",
    "momentum_percentile",
)
_COUNT_COLUMNS = (
    "normalization_reference_count",
    "interior_missing_row_count",
    "interior_missing_adjusted_close_count",
)

type DateInput = str | date | datetime | pd.Timestamp
type NumericScalar = int | float


class MomentumDataValidationError(ValueError):
    """Indicate that canonical prices cannot satisfy the Momentum contract."""


def _parse_evaluation_date(value: DateInput, name: str) -> pd.Timestamp:
    try:
        timestamp = pd.Timestamp(value)
    except (TypeError, ValueError) as error:
        raise MomentumDataValidationError(
            f"{name} must be a valid timezone-naive date."
        ) from error
    if pd.isna(timestamp) or timestamp.tzinfo is not None:
        raise MomentumDataValidationError(
            f"{name} must be a valid timezone-naive date."
        )
    if timestamp != timestamp.normalize():
        raise MomentumDataValidationError(f"{name} must not contain an intraday time.")
    return timestamp


def _python_numeric(value: object) -> NumericScalar | None:
    if value is None or value is pd.NA:
        return None
    if isinstance(value, (int, np.integer)) and not isinstance(value, (bool, np.bool_)):
        return int(value)
    if isinstance(value, (float, np.floating)):
        converted = float(value)
        return None if math.isnan(converted) else converted
    raise MomentumDataValidationError(
        "Canonical adjusted-close values must be supported real numeric scalars."
    )


def _empty_momentum_output() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "ticker": pd.Series(dtype="string"),
            "signal_date": pd.Series(dtype="datetime64[ns]"),
            "endpoint_start_date": pd.Series(dtype="datetime64[ns]"),
            "endpoint_end_date": pd.Series(dtype="datetime64[ns]"),
            "start_adjusted_close": pd.Series(dtype="Float64"),
            "end_adjusted_close": pd.Series(dtype="Float64"),
            "raw_momentum": pd.Series(dtype="Float64"),
            "simple_return_pct": pd.Series(dtype="Float64"),
            "simple_return_status": pd.Series(dtype="string"),
            "momentum_percentile": pd.Series(dtype="Float64"),
            "normalization_reference_count": pd.Series(dtype="Int64"),
            "first_prospective_session": pd.Series(dtype="datetime64[ns]"),
            "endpoint_eligible": pd.Series(dtype="boolean"),
            "endpoint_status": pd.Series(dtype="string"),
            "interior_missing_row_count": pd.Series(dtype="Int64"),
            "interior_missing_row_dates": pd.Series(dtype="object"),
            "interior_missing_adjusted_close_count": pd.Series(dtype="Int64"),
            "interior_missing_adjusted_close_dates": pd.Series(dtype="object"),
        },
        columns=MOMENTUM_OUTPUT_COLUMNS,
    )


def _coerce_output_dtypes(rows: list[dict[str, object]]) -> pd.DataFrame:
    if not rows:
        return _empty_momentum_output()

    endpoint_price_columns = ("start_adjusted_close", "end_adjusted_close")
    output_data: dict[str, object] = {}
    for column in MOMENTUM_OUTPUT_COLUMNS:
        values = [row[column] for row in rows]
        if column in endpoint_price_columns:
            try:
                output_data[column] = build_lossless_real_numeric_series(
                    values, name=column
                )
            except NumericDtypeHarmonizationError as error:
                raise MomentumDataValidationError(
                    f"Momentum output column '{column}' has no lossless numeric "
                    "representation."
                ) from error
        else:
            output_data[column] = values

    output = pd.DataFrame(output_data, columns=MOMENTUM_OUTPUT_COLUMNS)
    output["ticker"] = output["ticker"].astype("string")
    output["endpoint_status"] = output["endpoint_status"].astype("string")
    output["simple_return_status"] = output["simple_return_status"].astype("string")
    output["endpoint_eligible"] = output["endpoint_eligible"].astype("boolean")
    for column in _DATE_COLUMNS:
        output[column] = pd.to_datetime(output[column])
    for column in _FLOAT_COLUMNS:
        output[column] = pd.array(output[column].tolist(), dtype="Float64")
    for column in _COUNT_COLUMNS:
        output[column] = pd.array(output[column].tolist(), dtype="Int64")
    return output.sort_values(["ticker", "signal_date"], kind="mergesort").reset_index(
        drop=True
    )


def _endpoint_state(
    adjusted_close_by_date: dict[pd.Timestamp, NumericScalar | None],
    start_date: pd.Timestamp,
    end_date: pd.Timestamp,
) -> tuple[bool, str, NumericScalar | None, NumericScalar | None]:
    issues: list[str] = []
    start_row_present = start_date in adjusted_close_by_date
    end_row_present = end_date in adjusted_close_by_date
    start_price = adjusted_close_by_date.get(start_date)
    end_price = adjusted_close_by_date.get(end_date)

    if not start_row_present:
        issues.append("missing_start_row")
    elif start_price is None:
        issues.append("missing_start_adjusted_close")
    if not end_row_present:
        issues.append("missing_end_row")
    elif end_price is None:
        issues.append("missing_end_adjusted_close")

    if issues:
        return False, "|".join(issues), start_price, end_price
    return True, "eligible", start_price, end_price


def _stable_relative_change(
    start_price: NumericScalar,
    end_price: NumericScalar,
) -> float:
    if isinstance(start_price, int) != isinstance(end_price, int):
        relative_change, _ = _exact_mixed_relative_change(start_price, end_price)
        try:
            return float(relative_change)
        except OverflowError:
            return math.inf if relative_change > 0 else -math.inf

    # Python integer subtraction is exact, so an integer endpoint difference is
    # preserved before the one unavoidable conversion to a floating return.
    difference = end_price - start_price
    try:
        return float(difference / start_price)
    except OverflowError:
        return math.inf if difference > 0 else -math.inf


def _exact_fraction(value: NumericScalar) -> Fraction:
    if isinstance(value, int):
        return Fraction(value)
    return Fraction.from_float(value)


def _exact_mixed_relative_change(
    start_price: NumericScalar,
    end_price: NumericScalar,
) -> tuple[Fraction, bool]:
    start_exact = _exact_fraction(start_price)
    end_exact = _exact_fraction(end_price)
    difference = end_exact - start_exact
    if difference >= 0:
        endpoints_are_close = difference <= start_exact
    else:
        endpoints_are_close = -difference <= end_exact
    return difference / start_exact, endpoints_are_close


def _stable_log_return(
    start_price: NumericScalar,
    end_price: NumericScalar,
) -> float:
    if isinstance(start_price, int) != isinstance(end_price, int):
        exact_relative_change, endpoints_are_close = _exact_mixed_relative_change(
            start_price, end_price
        )
        relative_change = float(exact_relative_change) if endpoints_are_close else None
    else:
        difference = end_price - start_price
        if difference >= 0:
            endpoints_are_close = difference <= start_price
        else:
            endpoints_are_close = -difference <= end_price
        relative_change = (
            float(difference / start_price) if endpoints_are_close else None
        )

    if endpoints_are_close:
        assert relative_change is not None
        # For positive endpoints these comparisons are equivalent to the
        # inclusive ratio band 0.5 <= end / start <= 2.0 without calculating a
        # ratio that could overflow or underflow. Python integer subtraction is
        # exact, retaining small differences above the Float64 precision limit.
        raw_return = math.log1p(relative_change)
    else:
        # The log domain avoids ratio overflow, underflow, and cancellation when
        # a severe decline makes a relative change round close to negative one.
        raw_return = math.log(end_price) - math.log(start_price)

    if not math.isfinite(raw_return):
        raise MomentumDataValidationError(
            "Valid positive Momentum endpoints produced a non-finite log return."
        )
    return raw_return


def _stable_simple_return_percentage(
    start_price: NumericScalar,
    end_price: NumericScalar,
    raw_return: float,
) -> tuple[float | None, str]:
    relative_change = _stable_relative_change(start_price, end_price)
    relative_change_collapsed = relative_change == 0.0 and end_price != start_price
    if (
        not math.isfinite(relative_change)
        or relative_change <= -1.0
        or relative_change_collapsed
    ):
        try:
            relative_change = math.expm1(raw_return)
        except OverflowError:
            return None, _SIMPLE_RETURN_EXCEEDS_FLOAT64

    if not math.isfinite(relative_change) or relative_change > _MAX_FLOAT64 / 100.0:
        return None, _SIMPLE_RETURN_EXCEEDS_FLOAT64

    simple_return_pct = 100.0 * relative_change
    if not math.isfinite(simple_return_pct):
        return None, _SIMPLE_RETURN_EXCEEDS_FLOAT64
    return simple_return_pct, _SIMPLE_RETURN_AVAILABLE


def _raw_momentum_values(
    sessions: pd.DatetimeIndex,
    adjusted_close_by_date: dict[pd.Timestamp, NumericScalar | None],
) -> np.ndarray:
    values = np.full(len(sessions), np.nan, dtype=np.float64)
    for position in range(MOMENTUM_LAG_SESSIONS, len(sessions)):
        start_date = sessions[position - MOMENTUM_LAG_SESSIONS]
        end_date = sessions[position]
        eligible, _, start_price, end_price = _endpoint_state(
            adjusted_close_by_date, start_date, end_date
        )
        if eligible:
            assert start_price is not None and end_price is not None
            values[position] = _stable_log_return(start_price, end_price)
    return values


def _missing_position_indexes(
    sessions: pd.DatetimeIndex,
    session_positions: dict[pd.Timestamp, int],
    adjusted_close_by_date: dict[pd.Timestamp, NumericScalar | None],
) -> tuple[np.ndarray, np.ndarray]:
    row_present = np.zeros(len(sessions), dtype=bool)
    adjusted_close_missing = np.zeros(len(sessions), dtype=bool)
    for observation_date, adjusted_close in adjusted_close_by_date.items():
        position = session_positions[observation_date]
        row_present[position] = True
        if adjusted_close is None:
            adjusted_close_missing[position] = True
    return np.flatnonzero(~row_present), np.flatnonzero(adjusted_close_missing)


def _strict_interior_missing_dates(
    sessions: pd.DatetimeIndex,
    missing_positions: np.ndarray,
    start_position: int,
    end_position: int,
) -> tuple[int, tuple[pd.Timestamp, ...]]:
    first = int(np.searchsorted(missing_positions, start_position + 1, side="left"))
    stop = int(np.searchsorted(missing_positions, end_position, side="left"))
    interval_positions = missing_positions[first:stop]
    return len(interval_positions), tuple(sessions.take(interval_positions))


def _midrank_percentile(current: float, reference: np.ndarray) -> float:
    less_count = int(np.count_nonzero(reference < current))
    equal_count = int(np.count_nonzero(reference == current))
    return 100.0 * (less_count + 0.5 * equal_count) / len(reference)


def calculate_momentum(
    prices: pd.DataFrame,
    *,
    evaluation_start: DateInput | None = None,
    evaluation_end: DateInput | None = None,
) -> pd.DataFrame:
    """Calculate per-ETF Momentum observations on the XNYS session calendar.

    Raw Momentum at ``d_t`` is the log adjusted-close return from the exact
    XNYS session ``d_{t-252}``. Its percentile uses eligible prior raw values
    dated from ``d_{t-755}`` through ``d_{t-1}``, requires at least 252 prior
    observations, and excludes the current value. Calendar alignment creates
    no price values and never mutates or persists the canonical input.

    Args:
        prices: Canonical daily ETF prices. The earliest in-scope canonical
            date defines the start of available calendar history.
        evaluation_start: Optional inclusive lower bound for returned signal
            dates. Defaults to the earliest in-scope canonical date.
        evaluation_end: Optional inclusive upper bound for returned signal
            dates. Defaults to the latest canonical date.

    Returns:
        A deterministic ticker/date-sorted DataFrame containing endpoint,
        return, percentile, availability, eligibility, and interior-missingness
        diagnostics. Results are in memory only.

    Raises:
        PriceDataValidationError: If ``prices`` violate the canonical contract.
        MomentumDataValidationError: If evaluation bounds are invalid, an
            in-scope canonical date is not an XNYS session, or a lossless
            derived output representation is unavailable.
    """

    validate_price_data(prices)
    parsed_evaluation_start = (
        None
        if evaluation_start is None
        else _parse_evaluation_date(evaluation_start, "evaluation_start")
    )
    parsed_evaluation_end = (
        None
        if evaluation_end is None
        else _parse_evaluation_date(evaluation_end, "evaluation_end")
    )
    if prices.empty:
        if (
            parsed_evaluation_start is not None
            and parsed_evaluation_end is not None
            and parsed_evaluation_start > parsed_evaluation_end
        ):
            raise MomentumDataValidationError(
                "evaluation_start must be on or before evaluation_end."
            )
        return _empty_momentum_output()

    canonical_start = pd.Timestamp(prices["date"].min())
    canonical_end = pd.Timestamp(prices["date"].max())
    start = (
        canonical_start if parsed_evaluation_start is None else parsed_evaluation_start
    )
    end = canonical_end if parsed_evaluation_end is None else parsed_evaluation_end
    if start < canonical_start:
        raise MomentumDataValidationError(
            "evaluation_start cannot precede the earliest canonical price date."
        )
    if end < canonical_start:
        raise MomentumDataValidationError(
            "evaluation_end cannot precede the earliest canonical price date."
        )
    if start > end:
        raise MomentumDataValidationError(
            "evaluation_start must be on or before evaluation_end."
        )

    # Explicit construction padding supplies the exact lag and normalization
    # labels before available prices and the next prospective session after the
    # evaluation end. It never extends the price or signal calculation scope.
    calendar = xcals.get_calendar(
        XNYS_CALENDAR_NAME,
        start=canonical_start - timedelta(days=_CALENDAR_CONSTRUCTION_PREHISTORY_DAYS),
        end=end + timedelta(days=370),
    )
    all_sessions = calendar.sessions
    calculation_prices = prices.loc[prices["date"].le(end)].copy()
    in_scope_dates = pd.DatetimeIndex(calculation_prices["date"].unique())
    scope_sessions = all_sessions[
        (all_sessions >= canonical_start) & (all_sessions <= end)
    ]
    off_calendar_dates = in_scope_dates.difference(scope_sessions)
    if not off_calendar_dates.empty:
        formatted_dates = [value.date().isoformat() for value in off_calendar_dates]
        raise MomentumDataValidationError(
            "Canonical price dates inside the Momentum calculation scope must "
            f"be XNYS sessions; invalid dates: {formatted_dates}."
        )

    canonical_start_position = int(all_sessions.searchsorted(canonical_start))
    history_start_position = (
        canonical_start_position - _REQUIRED_CALENDAR_PREHISTORY_SESSIONS
    )
    if history_start_position < 0:
        raise MomentumDataValidationError(
            "The XNYS calendar could not supply the required Momentum prehistory."
        )
    history_end_position = int(all_sessions.searchsorted(end, side="right"))
    history_sessions = all_sessions[history_start_position:history_end_position]
    signal_sessions = history_sessions[
        (history_sessions >= start) & (history_sessions <= end)
    ]
    if signal_sessions.empty:
        raise MomentumDataValidationError(
            "The evaluation range contains no XNYS regular sessions."
        )

    history_positions = {
        session: position for position, session in enumerate(history_sessions)
    }
    all_positions = {session: position for position, session in enumerate(all_sessions)}
    rows: list[dict[str, object]] = []

    tickers = sorted(calculation_prices["ticker"].astype(str).unique().tolist())
    for ticker in tickers:
        ticker_prices = calculation_prices.loc[
            calculation_prices["ticker"].astype(str).eq(ticker)
        ]
        adjusted_close_by_date = {
            pd.Timestamp(cast(DateInput, row.date)): _python_numeric(row.adjusted_close)
            for row in ticker_prices[["date", "adjusted_close"]].itertuples(index=False)
        }
        raw_values = _raw_momentum_values(history_sessions, adjusted_close_by_date)
        missing_row_positions, missing_adjusted_positions = _missing_position_indexes(
            history_sessions,
            history_positions,
            adjusted_close_by_date,
        )

        for signal_date in signal_sessions:
            position = history_positions[signal_date]
            next_position = all_positions[signal_date] + 1
            prospective_session = all_sessions[next_position]
            endpoint_start_date: pd.Timestamp | None = None
            start_price: NumericScalar | None = None
            end_price: NumericScalar | None = adjusted_close_by_date.get(signal_date)
            endpoint_eligible = False
            endpoint_status = "insufficient_calendar_history"
            missing_row_dates: tuple[pd.Timestamp, ...] = ()
            missing_adjusted_dates: tuple[pd.Timestamp, ...] = ()
            missing_row_count: int | None = None
            missing_adjusted_count: int | None = None

            if position >= MOMENTUM_LAG_SESSIONS:
                endpoint_start_date = history_sessions[position - MOMENTUM_LAG_SESSIONS]
                (
                    endpoint_eligible,
                    endpoint_status,
                    start_price,
                    end_price,
                ) = _endpoint_state(
                    adjusted_close_by_date, endpoint_start_date, signal_date
                )
                missing_row_count, missing_row_dates = _strict_interior_missing_dates(
                    history_sessions,
                    missing_row_positions,
                    position - MOMENTUM_LAG_SESSIONS,
                    position,
                )
                missing_adjusted_count, missing_adjusted_dates = (
                    _strict_interior_missing_dates(
                        history_sessions,
                        missing_adjusted_positions,
                        position - MOMENTUM_LAG_SESSIONS,
                        position,
                    )
                )

            raw_momentum = (
                float(raw_values[position])
                if np.isfinite(raw_values[position])
                else None
            )
            simple_return_pct: float | None = None
            simple_return_status = _SIMPLE_RETURN_ENDPOINT_INELIGIBLE
            if endpoint_eligible:
                assert start_price is not None and end_price is not None
                assert raw_momentum is not None
                simple_return_pct, simple_return_status = (
                    _stable_simple_return_percentage(
                        start_price,
                        end_price,
                        raw_momentum,
                    )
                )

            reference_start = max(
                0, position - (MOMENTUM_NORMALIZATION_WINDOW_SESSIONS - 1)
            )
            reference = raw_values[reference_start:position]
            reference = reference[np.isfinite(reference)]
            reference_count = len(reference)
            momentum_percentile: float | None = None
            if (
                raw_momentum is not None
                and reference_count >= MOMENTUM_MINIMUM_PRIOR_OBSERVATIONS
            ):
                momentum_percentile = _midrank_percentile(raw_momentum, reference)

            rows.append(
                {
                    "ticker": ticker,
                    "signal_date": signal_date,
                    "endpoint_start_date": endpoint_start_date,
                    "endpoint_end_date": signal_date,
                    "start_adjusted_close": start_price,
                    "end_adjusted_close": end_price,
                    "raw_momentum": raw_momentum,
                    "simple_return_pct": simple_return_pct,
                    "simple_return_status": simple_return_status,
                    "momentum_percentile": momentum_percentile,
                    "normalization_reference_count": reference_count,
                    "first_prospective_session": prospective_session,
                    "endpoint_eligible": endpoint_eligible,
                    "endpoint_status": endpoint_status,
                    "interior_missing_row_count": missing_row_count,
                    "interior_missing_row_dates": missing_row_dates,
                    "interior_missing_adjusted_close_count": missing_adjusted_count,
                    "interior_missing_adjusted_close_dates": missing_adjusted_dates,
                }
            )

    return _coerce_output_dtypes(rows)
