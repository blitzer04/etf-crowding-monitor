"""Calculate the approved ETF Volatility percentile on XNYS sessions."""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from decimal import Decimal, DecimalException, localcontext
from typing import cast

import exchange_calendars as xcals  # type: ignore[import-untyped]
import numpy as np
import pandas as pd

from etf_crowding.data.validation import validate_price_data

XNYS_CALENDAR_NAME = "XNYS"
VOLATILITY_RETURN_WINDOW_SESSIONS = 21
VOLATILITY_ANNUALIZATION_SESSIONS = 252
VOLATILITY_NORMALIZATION_WINDOW_SESSIONS = 756
VOLATILITY_MINIMUM_PRIOR_OBSERVATIONS = 252
_REQUIRED_CALENDAR_PREHISTORY_SESSIONS = (
    VOLATILITY_RETURN_WINDOW_SESSIONS + VOLATILITY_NORMALIZATION_WINDOW_SESSIONS - 1
)
_CALENDAR_CONSTRUCTION_PREHISTORY_DAYS = 6 * 366
_DECIMAL_RETURN_PRECISION = 80

VOLATILITY_OUTPUT_COLUMNS = (
    "ticker",
    "signal_date",
    "window_start_date",
    "window_end_date",
    "raw_annualized_volatility",
    "annualized_volatility_pct",
    "volatility_percentile",
    "normalization_reference_count",
    "first_prospective_session",
    "window_eligible",
    "window_status",
    "missing_row_count",
    "missing_row_dates",
    "missing_adjusted_close_count",
    "missing_adjusted_close_dates",
)

_DATE_COLUMNS = (
    "signal_date",
    "window_start_date",
    "window_end_date",
    "first_prospective_session",
)
_FLOAT_COLUMNS = (
    "raw_annualized_volatility",
    "annualized_volatility_pct",
    "volatility_percentile",
)
_COUNT_COLUMNS = (
    "normalization_reference_count",
    "missing_row_count",
    "missing_adjusted_close_count",
)

type DateInput = str | date | datetime | pd.Timestamp
type NumericScalar = int | float


class VolatilityDataValidationError(ValueError):
    """Indicate that canonical prices cannot satisfy the Volatility contract."""


def _parse_evaluation_date(value: DateInput, name: str) -> pd.Timestamp:
    try:
        timestamp = pd.Timestamp(value)
    except (TypeError, ValueError) as error:
        raise VolatilityDataValidationError(
            f"{name} must be a valid timezone-naive date."
        ) from error
    if pd.isna(timestamp) or timestamp.tzinfo is not None:
        raise VolatilityDataValidationError(
            f"{name} must be a valid timezone-naive date."
        )
    if timestamp != timestamp.normalize():
        raise VolatilityDataValidationError(
            f"{name} must not contain an intraday time."
        )
    return timestamp


def _python_numeric(value: object) -> NumericScalar | None:
    if value is None or value is pd.NA:
        return None
    if isinstance(value, (int, np.integer)) and not isinstance(value, (bool, np.bool_)):
        return int(value)
    if isinstance(value, (float, np.floating)):
        converted = float(value)
        return None if math.isnan(converted) else converted
    raise VolatilityDataValidationError(
        "Canonical adjusted-close values must be supported real numeric scalars."
    )


def _empty_volatility_output() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "ticker": pd.Series(dtype="string"),
            "signal_date": pd.Series(dtype="datetime64[ns]"),
            "window_start_date": pd.Series(dtype="datetime64[ns]"),
            "window_end_date": pd.Series(dtype="datetime64[ns]"),
            "raw_annualized_volatility": pd.Series(dtype="Float64"),
            "annualized_volatility_pct": pd.Series(dtype="Float64"),
            "volatility_percentile": pd.Series(dtype="Float64"),
            "normalization_reference_count": pd.Series(dtype="Int64"),
            "first_prospective_session": pd.Series(dtype="datetime64[ns]"),
            "window_eligible": pd.Series(dtype="boolean"),
            "window_status": pd.Series(dtype="string"),
            "missing_row_count": pd.Series(dtype="Int64"),
            "missing_row_dates": pd.Series(dtype="object"),
            "missing_adjusted_close_count": pd.Series(dtype="Int64"),
            "missing_adjusted_close_dates": pd.Series(dtype="object"),
        },
        columns=VOLATILITY_OUTPUT_COLUMNS,
    )


def _coerce_output_dtypes(rows: list[dict[str, object]]) -> pd.DataFrame:
    if not rows:
        return _empty_volatility_output()

    output = pd.DataFrame(rows, columns=VOLATILITY_OUTPUT_COLUMNS)
    output["ticker"] = output["ticker"].astype("string")
    output["window_eligible"] = output["window_eligible"].astype("boolean")
    output["window_status"] = output["window_status"].astype("string")
    for column in _DATE_COLUMNS:
        output[column] = pd.to_datetime(output[column])
    for column in _FLOAT_COLUMNS:
        output[column] = pd.array(output[column].tolist(), dtype="Float64")
    for column in _COUNT_COLUMNS:
        output[column] = pd.array(output[column].tolist(), dtype="Int64")
    return output.sort_values(["ticker", "signal_date"], kind="mergesort").reset_index(
        drop=True
    )


def _optional_float(value: object) -> float | None:
    if value is None or value is pd.NA:
        return None
    if isinstance(value, (float, np.floating)):
        return float(value)
    raise VolatilityDataValidationError(
        "Volatility output float columns contain an invalid scalar."
    )


def _required_count(value: object, column: str) -> int:
    if isinstance(value, (int, np.integer)) and not isinstance(value, (bool, np.bool_)):
        return int(value)
    raise VolatilityDataValidationError(
        f"Volatility output column '{column}' contains an invalid count scalar."
    )


def _validate_diagnostic_dates(
    dates: object,
    expected_count: int,
    *,
    column: str,
    calendar_positions: dict[pd.Timestamp, int],
    window_start_position: int,
    window_end_position: int,
) -> tuple[pd.Timestamp, ...]:
    if not isinstance(dates, tuple):
        raise VolatilityDataValidationError(
            f"Volatility output column '{column}' must contain tuples."
        )
    if len(dates) != expected_count:
        raise VolatilityDataValidationError(
            f"Volatility output column '{column}' disagrees with its count."
        )
    if any(not isinstance(value, pd.Timestamp) for value in dates):
        raise VolatilityDataValidationError(
            f"Volatility output column '{column}' must contain pandas timestamps."
        )

    typed_dates = cast(tuple[pd.Timestamp, ...], dates)
    if any(
        value.tzinfo is not None or value != value.normalize() for value in typed_dates
    ):
        raise VolatilityDataValidationError(
            f"Volatility output column '{column}' must contain timezone-naive dates."
        )
    if tuple(sorted(typed_dates)) != typed_dates or len(set(typed_dates)) != len(
        typed_dates
    ):
        raise VolatilityDataValidationError(
            f"Volatility output column '{column}' must be uniquely date-sorted."
        )
    if any(
        (position := calendar_positions.get(value)) is None
        or position < window_start_position
        or position > window_end_position
        for value in typed_dates
    ):
        raise VolatilityDataValidationError(
            f"Volatility output column '{column}' must contain only exact XNYS "
            "sessions in its 22-price window."
        )
    return typed_dates


def _validate_volatility_output(
    output: pd.DataFrame,
    *,
    calendar_sessions: pd.DatetimeIndex,
) -> None:
    if tuple(output.columns) != VOLATILITY_OUTPUT_COLUMNS:
        raise VolatilityDataValidationError(
            "Volatility output columns do not match the derived-data contract."
        )
    if output.empty:
        raise VolatilityDataValidationError(
            "The nonempty Volatility output validator received an empty result."
        )

    if (
        not isinstance(calendar_sessions, pd.DatetimeIndex)
        or calendar_sessions.empty
        or calendar_sessions.tz is not None
        or not calendar_sessions.is_monotonic_increasing
        or calendar_sessions.has_duplicates
        or not calendar_sessions.equals(calendar_sessions.normalize())
    ):
        raise VolatilityDataValidationError(
            "Volatility output validation requires a normalized, timezone-naive, "
            "strictly increasing XNYS session index."
        )
    calendar_positions = {
        session: position for position, session in enumerate(calendar_sessions)
    }

    expected_dtypes = {
        "ticker": "string",
        "signal_date": "datetime64[ns]",
        "window_start_date": "datetime64[ns]",
        "window_end_date": "datetime64[ns]",
        "raw_annualized_volatility": "Float64",
        "annualized_volatility_pct": "Float64",
        "volatility_percentile": "Float64",
        "normalization_reference_count": "Int64",
        "first_prospective_session": "datetime64[ns]",
        "window_eligible": "boolean",
        "window_status": "string",
        "missing_row_count": "Int64",
        "missing_row_dates": "object",
        "missing_adjusted_close_count": "Int64",
        "missing_adjusted_close_dates": "object",
    }
    invalid_dtypes = {
        column: str(output[column].dtype)
        for column, expected in expected_dtypes.items()
        if str(output[column].dtype) != expected
    }
    if invalid_dtypes:
        raise VolatilityDataValidationError(
            f"Volatility output contains unexpected dtypes: {invalid_dtypes}."
        )

    if not output.index.equals(pd.RangeIndex(len(output))):
        raise VolatilityDataValidationError(
            "Volatility output must use a deterministic zero-based index."
        )
    expected_order = output.sort_values(
        ["ticker", "signal_date"], kind="mergesort"
    ).reset_index(drop=True)
    if not output.equals(expected_order):
        raise VolatilityDataValidationError(
            "Volatility output must be sorted by ticker and signal date."
        )
    duplicate_keys = output.duplicated(["ticker", "signal_date"], keep=False)
    if duplicate_keys.any():
        raise VolatilityDataValidationError(
            "Volatility output contains duplicate ticker/signal-date keys."
        )
    if output["ticker"].isna().any() or any(
        not value.strip() for value in output["ticker"].astype(str)
    ):
        raise VolatilityDataValidationError(
            "Volatility output tickers must be nonempty strings."
        )
    if output[list(_DATE_COLUMNS)].isna().any().any():
        raise VolatilityDataValidationError(
            "Volatility output date fields must not be missing."
        )

    for column in _FLOAT_COLUMNS:
        present_values = output[column].dropna().to_numpy(dtype=np.float64)
        if not np.isfinite(present_values).all():
            raise VolatilityDataValidationError(
                f"Volatility output column '{column}' contains a non-finite value."
            )
    if output["raw_annualized_volatility"].dropna().lt(0).any():
        raise VolatilityDataValidationError(
            "Raw annualized Volatility must not be negative."
        )
    if output["annualized_volatility_pct"].dropna().lt(0).any():
        raise VolatilityDataValidationError(
            "Displayed annualized Volatility must not be negative."
        )
    percentiles = output["volatility_percentile"].dropna()
    if percentiles.lt(0).any() or percentiles.gt(100).any():
        raise VolatilityDataValidationError(
            "Volatility percentiles must lie within [0, 100]."
        )

    for column in _COUNT_COLUMNS:
        if output[column].isna().any() or output[column].lt(0).any():
            raise VolatilityDataValidationError(
                f"Volatility output column '{column}' must contain nonnegative counts."
            )
    if (
        output["normalization_reference_count"]
        .gt(VOLATILITY_NORMALIZATION_WINDOW_SESSIONS - 1)
        .any()
    ):
        raise VolatilityDataValidationError(
            "Volatility normalization reference count exceeds its session window."
        )

    for row in output.itertuples(index=False):
        signal_date = cast(pd.Timestamp, row.signal_date)
        window_start = cast(pd.Timestamp, row.window_start_date)
        window_end = cast(pd.Timestamp, row.window_end_date)
        prospective_session = cast(pd.Timestamp, row.first_prospective_session)
        signal_position = calendar_positions.get(signal_date)
        if signal_position is None:
            raise VolatilityDataValidationError(
                "Volatility output signal dates must be XNYS regular sessions."
            )
        if window_end != signal_date:
            raise VolatilityDataValidationError(
                "Volatility output window end must equal its signal date."
            )
        window_start_position = signal_position - VOLATILITY_RETURN_WINDOW_SESSIONS
        if (
            window_start_position < 0
            or window_start != calendar_sessions[window_start_position]
        ):
            raise VolatilityDataValidationError(
                "Volatility output window start must be exactly 21 XNYS sessions "
                "before its signal date."
            )
        prospective_position = signal_position + 1
        if (
            prospective_position >= len(calendar_sessions)
            or prospective_session != calendar_sessions[prospective_position]
        ):
            raise VolatilityDataValidationError(
                "Volatility prospective-use session must be the next XNYS regular "
                "session after the signal date."
            )

        missing_row_count = _required_count(row.missing_row_count, "missing_row_count")
        missing_adjusted_count = _required_count(
            row.missing_adjusted_close_count, "missing_adjusted_close_count"
        )
        missing_row_dates = _validate_diagnostic_dates(
            row.missing_row_dates,
            missing_row_count,
            column="missing_row_dates",
            calendar_positions=calendar_positions,
            window_start_position=window_start_position,
            window_end_position=signal_position,
        )
        missing_adjusted_dates = _validate_diagnostic_dates(
            row.missing_adjusted_close_dates,
            missing_adjusted_count,
            column="missing_adjusted_close_dates",
            calendar_positions=calendar_positions,
            window_start_position=window_start_position,
            window_end_position=signal_position,
        )
        if set(missing_row_dates).intersection(missing_adjusted_dates):
            raise VolatilityDataValidationError(
                "Missing-row and missing-adjusted-close diagnostics overlap."
            )

        eligible = bool(row.window_eligible)
        expected_eligible = missing_row_count == 0 and missing_adjusted_count == 0
        expected_issues: list[str] = []
        if missing_row_count:
            expected_issues.append("missing_price_rows")
        if missing_adjusted_count:
            expected_issues.append("missing_adjusted_close")
        expected_status = "eligible" if expected_eligible else "|".join(expected_issues)
        if eligible != expected_eligible or row.window_status != expected_status:
            raise VolatilityDataValidationError(
                "Volatility window eligibility and status are inconsistent."
            )

        raw_volatility = _optional_float(row.raw_annualized_volatility)
        display_volatility = _optional_float(row.annualized_volatility_pct)
        percentile = _optional_float(row.volatility_percentile)
        reference_count = _required_count(
            row.normalization_reference_count, "normalization_reference_count"
        )
        if not eligible:
            if any(
                value is not None
                for value in (raw_volatility, display_volatility, percentile)
            ):
                raise VolatilityDataValidationError(
                    "An ineligible Volatility window contains a derived value."
                )
            continue

        if raw_volatility is None or display_volatility is None:
            raise VolatilityDataValidationError(
                "An eligible Volatility window is missing its raw or display value."
            )
        if display_volatility != 100.0 * raw_volatility:
            raise VolatilityDataValidationError(
                "Displayed Volatility does not equal 100 times raw Volatility."
            )
        if reference_count < VOLATILITY_MINIMUM_PRIOR_OBSERVATIONS:
            if percentile is not None:
                raise VolatilityDataValidationError(
                    "Volatility percentile is present without sufficient history."
                )
        elif percentile is None:
            raise VolatilityDataValidationError(
                "Eligible Volatility with sufficient history lacks a percentile."
            )


def _exact_decimal(
    value: object,
) -> Decimal:
    if isinstance(value, Decimal):
        converted = value
    elif isinstance(value, (int, np.integer)) and not isinstance(
        value, (bool, np.bool_)
    ):
        converted = Decimal(int(value))
    elif isinstance(value, (float, np.floating)):
        converted = Decimal.from_float(float(value))
    else:
        raise VolatilityDataValidationError(
            "Volatility arithmetic received an unsupported numeric scalar."
        )
    if not converted.is_finite():
        raise VolatilityDataValidationError(
            "Volatility arithmetic requires finite numeric values."
        )
    return converted


def _high_precision_log_return(
    start_price: NumericScalar,
    end_price: NumericScalar,
) -> Decimal:
    try:
        with localcontext() as context:
            # Exact Float64 ratios can differ by roughly 1e-32 and exact UInt64
            # ratios by roughly 1e-39. Eighty decimal digits retain those source
            # distinctions plus ample guard precision through log and variance.
            context.prec = _DECIMAL_RETURN_PRECISION
            start = _exact_decimal(start_price)
            end = _exact_decimal(end_price)
            raw_return = (end / start).ln()
    except (DecimalException, OverflowError, ValueError) as error:
        raise VolatilityDataValidationError(
            "Valid positive Volatility endpoints produced an invalid log return."
        ) from error
    if not raw_return.is_finite():
        raise VolatilityDataValidationError(
            "Valid positive Volatility endpoints produced a non-finite log return."
        )
    return raw_return


def _annualized_sample_volatility(
    returns: Sequence[object] | np.ndarray,
) -> float:
    if len(returns) != VOLATILITY_RETURN_WINDOW_SESSIONS:
        raise VolatilityDataValidationError(
            "Volatility requires exactly 21 adjacent XNYS daily returns."
        )

    with localcontext() as context:
        context.prec = _DECIMAL_RETURN_PRECISION
        exact_returns = [_exact_decimal(value) for value in returns]
        reference = exact_returns[0]
        translated = [value - reference for value in exact_returns]
        if all(value.is_zero() for value in translated):
            return 0.0

        mean_translated = sum(translated, Decimal(0)) / Decimal(len(translated))
        centered_sum_of_squares = sum(
            (value - mean_translated) * (value - mean_translated)
            for value in translated
        )
        variance = centered_sum_of_squares / Decimal(len(translated) - 1)
        annualized_exact = (
            Decimal(VOLATILITY_ANNUALIZATION_SESSIONS) * variance
        ).sqrt()

    try:
        annualized = float(annualized_exact)
    except (OverflowError, ValueError) as error:
        raise VolatilityDataValidationError(
            "Eligible daily returns produced non-finite annualized volatility."
        ) from error
    if not math.isfinite(annualized) or (
        not annualized_exact.is_zero() and annualized == 0.0
    ):
        raise VolatilityDataValidationError(
            "Eligible daily returns produced non-finite or underflowed annualized "
            "volatility."
        )
    return annualized


def _daily_log_returns(
    sessions: pd.DatetimeIndex,
    adjusted_close_by_date: dict[pd.Timestamp, NumericScalar | None],
) -> list[Decimal | None]:
    returns: list[Decimal | None] = [None] * len(sessions)
    for position in range(1, len(sessions)):
        start_price = adjusted_close_by_date.get(sessions[position - 1])
        end_price = adjusted_close_by_date.get(sessions[position])
        if start_price is not None and end_price is not None:
            returns[position] = _high_precision_log_return(start_price, end_price)
    return returns


def _raw_volatility_values(daily_returns: Sequence[Decimal | None]) -> np.ndarray:
    values = np.full(len(daily_returns), np.nan, dtype=np.float64)
    window = VOLATILITY_RETURN_WINDOW_SESSIONS
    for position in range(window, len(daily_returns)):
        return_window = daily_returns[position - window + 1 : position + 1]
        if all(value is not None for value in return_window):
            values[position] = _annualized_sample_volatility(
                cast(Sequence[Decimal], return_window)
            )
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


def _inclusive_missing_dates(
    sessions: pd.DatetimeIndex,
    missing_positions: np.ndarray,
    start_position: int,
    end_position: int,
) -> tuple[int, tuple[pd.Timestamp, ...]]:
    first = int(np.searchsorted(missing_positions, start_position, side="left"))
    stop = int(np.searchsorted(missing_positions, end_position, side="right"))
    interval_positions = missing_positions[first:stop]
    return len(interval_positions), tuple(sessions.take(interval_positions))


def _midrank_percentile(current: float, reference: np.ndarray) -> float:
    less_count = int(np.count_nonzero(reference < current))
    equal_count = int(np.count_nonzero(reference == current))
    return 100.0 * (less_count + 0.5 * equal_count) / len(reference)


def calculate_volatility(
    prices: pd.DataFrame,
    *,
    evaluation_start: DateInput | None = None,
    evaluation_end: DateInput | None = None,
) -> pd.DataFrame:
    """Calculate per-ETF Volatility observations on the XNYS calendar.

    Raw Volatility at ``d_t`` is the annualized sample standard deviation of
    the 21 exact adjacent-session adjusted-close log returns ending at ``d_t``.
    Its percentile uses eligible prior raw values dated from ``d_{t-755}``
    through ``d_{t-1}``, requires at least 252 prior observations, and excludes
    the current value. Calendar alignment creates no prices and never mutates
    or persists the canonical input.

    Args:
        prices: Canonical daily ETF prices. The earliest in-scope canonical
            date defines the start of available calendar history.
        evaluation_start: Optional inclusive lower bound for returned signal
            dates. Defaults to the earliest in-scope canonical date.
        evaluation_end: Optional inclusive upper bound for returned signal
            dates. Defaults to the latest canonical date.

    Returns:
        A deterministic ticker/date-sorted DataFrame containing window,
        annualized-volatility, percentile, availability, eligibility, and
        missingness diagnostics. Results are in memory only.

    Raises:
        PriceDataValidationError: If ``prices`` violate the canonical contract.
        VolatilityDataValidationError: If evaluation bounds are invalid, an
            in-scope canonical date is not an XNYS session, or a valid window
            cannot be represented by the output contract.
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
            raise VolatilityDataValidationError(
                "evaluation_start must be on or before evaluation_end."
            )
        return _empty_volatility_output()

    canonical_start = pd.Timestamp(prices["date"].min())
    canonical_end = pd.Timestamp(prices["date"].max())
    start = (
        canonical_start if parsed_evaluation_start is None else parsed_evaluation_start
    )
    end = canonical_end if parsed_evaluation_end is None else parsed_evaluation_end
    if start < canonical_start:
        raise VolatilityDataValidationError(
            "evaluation_start cannot precede the earliest canonical price date."
        )
    if end < canonical_start:
        raise VolatilityDataValidationError(
            "evaluation_end cannot precede the earliest canonical price date."
        )
    if start > end:
        raise VolatilityDataValidationError(
            "evaluation_start must be on or before evaluation_end."
        )

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
        raise VolatilityDataValidationError(
            "Canonical price dates inside the Volatility calculation scope must "
            f"be XNYS sessions; invalid dates: {formatted_dates}."
        )

    canonical_start_position = int(all_sessions.searchsorted(canonical_start))
    history_start_position = (
        canonical_start_position - _REQUIRED_CALENDAR_PREHISTORY_SESSIONS
    )
    if history_start_position < 0:
        raise VolatilityDataValidationError(
            "The XNYS calendar could not supply the required Volatility prehistory."
        )
    history_end_position = int(all_sessions.searchsorted(end, side="right"))
    history_sessions = all_sessions[history_start_position:history_end_position]
    signal_sessions = history_sessions[
        (history_sessions >= start) & (history_sessions <= end)
    ]
    if signal_sessions.empty:
        raise VolatilityDataValidationError(
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
        daily_returns = _daily_log_returns(history_sessions, adjusted_close_by_date)
        raw_values = _raw_volatility_values(daily_returns)
        missing_row_positions, missing_adjusted_positions = _missing_position_indexes(
            history_sessions,
            history_positions,
            adjusted_close_by_date,
        )

        for signal_date in signal_sessions:
            position = history_positions[signal_date]
            window_start_position = position - VOLATILITY_RETURN_WINDOW_SESSIONS
            window_start_date = history_sessions[window_start_position]
            next_position = all_positions[signal_date] + 1
            prospective_session = all_sessions[next_position]
            missing_row_count, missing_row_dates = _inclusive_missing_dates(
                history_sessions,
                missing_row_positions,
                window_start_position,
                position,
            )
            missing_adjusted_count, missing_adjusted_dates = _inclusive_missing_dates(
                history_sessions,
                missing_adjusted_positions,
                window_start_position,
                position,
            )
            window_eligible = missing_row_count == 0 and missing_adjusted_count == 0
            issues: list[str] = []
            if missing_row_count:
                issues.append("missing_price_rows")
            if missing_adjusted_count:
                issues.append("missing_adjusted_close")
            window_status = "eligible" if window_eligible else "|".join(issues)

            raw_volatility = (
                float(raw_values[position])
                if np.isfinite(raw_values[position])
                else None
            )
            if window_eligible and raw_volatility is None:
                raise VolatilityDataValidationError(
                    "An eligible Volatility window did not produce a finite value."
                )

            reference_start = max(
                0, position - (VOLATILITY_NORMALIZATION_WINDOW_SESSIONS - 1)
            )
            reference = raw_values[reference_start:position]
            reference = reference[np.isfinite(reference)]
            reference_count = len(reference)
            volatility_percentile: float | None = None
            if (
                raw_volatility is not None
                and reference_count >= VOLATILITY_MINIMUM_PRIOR_OBSERVATIONS
            ):
                volatility_percentile = _midrank_percentile(raw_volatility, reference)

            rows.append(
                {
                    "ticker": ticker,
                    "signal_date": signal_date,
                    "window_start_date": window_start_date,
                    "window_end_date": signal_date,
                    "raw_annualized_volatility": raw_volatility,
                    "annualized_volatility_pct": (
                        None if raw_volatility is None else 100.0 * raw_volatility
                    ),
                    "volatility_percentile": volatility_percentile,
                    "normalization_reference_count": reference_count,
                    "first_prospective_session": prospective_session,
                    "window_eligible": window_eligible,
                    "window_status": window_status,
                    "missing_row_count": missing_row_count,
                    "missing_row_dates": missing_row_dates,
                    "missing_adjusted_close_count": missing_adjusted_count,
                    "missing_adjusted_close_dates": missing_adjusted_dates,
                }
            )

    output = _coerce_output_dtypes(rows)
    _validate_volatility_output(output, calendar_sessions=all_sessions)
    return output
