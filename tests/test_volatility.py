"""Tests for the approved XNYS-aligned Volatility component."""

from __future__ import annotations

import math
import statistics
from decimal import Decimal, localcontext
from functools import cache
from importlib.metadata import version

import exchange_calendars as xcals  # type: ignore[import-untyped]
import numpy as np
import pandas as pd
import pytest

import etf_crowding.signals.volatility as volatility_module
from etf_crowding.signals import (
    VOLATILITY_OUTPUT_COLUMNS,
    VolatilityDataValidationError,
    calculate_volatility,
)
from etf_crowding.signals.volatility import (
    _annualized_sample_volatility,
    _validate_volatility_output,
)

RETRIEVED_AT = pd.Timestamp("2026-08-23T00:00:00Z")


@cache
def _xnys_sessions(count: int) -> pd.DatetimeIndex:
    calendar = xcals.get_calendar("XNYS", start="2018-01-01", end="2027-12-31")
    return calendar.sessions[:count]


@cache
def _validation_sessions() -> pd.DatetimeIndex:
    calendar = xcals.get_calendar("XNYS", start="2017-01-01", end="2027-12-31")
    return calendar.sessions


def _exact_decimal(value: int | float) -> Decimal:
    return Decimal(value) if isinstance(value, int) else Decimal.from_float(value)


def _high_precision_volatility_from_returns(returns: list[float]) -> float:
    with localcontext() as context:
        context.prec = 120
        exact_returns = [Decimal.from_float(value) for value in returns]
        reference = exact_returns[0]
        translated = [value - reference for value in exact_returns]
        mean = sum(translated, Decimal(0)) / Decimal(len(translated))
        variance = sum((value - mean) ** 2 for value in translated) / Decimal(
            len(translated) - 1
        )
        return float((Decimal(252) * variance).sqrt())


def _high_precision_volatility_from_prices(prices: list[int | float]) -> float:
    with localcontext() as context:
        context.prec = 120
        exact_prices = [_exact_decimal(value) for value in prices]
        returns = [
            (end / start).ln() for start, end in zip(exact_prices, exact_prices[1:])
        ]
        reference = returns[0]
        translated = [value - reference for value in returns]
        mean = sum(translated, Decimal(0)) / Decimal(len(translated))
        variance = sum((value - mean) ** 2 for value in translated) / Decimal(
            len(translated) - 1
        )
        return float((Decimal(252) * variance).sqrt())


def _canonical_prices(
    sessions: pd.DatetimeIndex,
    adjusted_close: list[int | float | None] | np.ndarray,
    *,
    ticker: str = "SPY",
    adjusted_dtype: str = "Float64",
) -> pd.DataFrame:
    values = list(adjusted_close)
    assert len(sessions) == len(values)
    return pd.DataFrame(
        {
            "date": sessions,
            "ticker": pd.Series([ticker] * len(sessions), dtype="string"),
            "open": pd.Series([np.nan] * len(sessions), dtype="float64"),
            "high": pd.Series([np.nan] * len(sessions), dtype="float64"),
            "low": pd.Series([np.nan] * len(sessions), dtype="float64"),
            "close": pd.Series([100.0] * len(sessions), dtype="float64"),
            "adjusted_close": pd.Series(values, dtype=adjusted_dtype),
            "volume": pd.Series([np.nan] * len(sessions), dtype="float64"),
            "retrieved_at": pd.Series(
                [RETRIEVED_AT] * len(sessions), dtype="datetime64[ns, UTC]"
            ),
        }
    )


def _prices_from_daily_returns(
    sessions: pd.DatetimeIndex,
    daily_returns: list[float],
    *,
    ticker: str = "SPY",
) -> pd.DataFrame:
    assert len(sessions) == len(daily_returns) + 1
    adjusted_close = [100.0]
    for daily_return in daily_returns:
        adjusted_close.append(adjusted_close[-1] * math.exp(daily_return))
    return _canonical_prices(sessions, adjusted_close, ticker=ticker)


def _row_at(result: pd.DataFrame, signal_date: pd.Timestamp) -> pd.Series:
    return result.loc[result["signal_date"].eq(signal_date)].iloc[0]


def _single_target_result(
    prices: pd.DataFrame,
    signal_date: pd.Timestamp,
) -> pd.DataFrame:
    return calculate_volatility(
        prices,
        evaluation_start=signal_date,
        evaluation_end=signal_date,
    )


@cache
def _normalized_target_result() -> pd.DataFrame:
    sessions = _xnys_sessions(274)
    prices = _canonical_prices(sessions, [100.0] * len(sessions))
    return _single_target_result(prices, sessions[-1])


def _validate_output(result: pd.DataFrame) -> None:
    _validate_volatility_output(
        result,
        calendar_sessions=_validation_sessions(),
    )


def test_dependency_and_xnys_calendar_contract_are_pinned() -> None:
    assert version("exchange-calendars") == "4.13.2"
    calendar = xcals.get_calendar("XNYS", start="2018-01-01", end="2025-12-31")

    assert calendar.name == "XNYS"
    assert str(calendar.tz) == "America/New_York"
    assert not calendar.is_session("2024-11-28")  # Thanksgiving
    assert not calendar.is_session("2024-11-30")  # Weekend
    assert calendar.is_session("2024-11-29")  # Black Friday early close
    assert calendar.session_close("2024-11-29") == pd.Timestamp("2024-11-29T18:00:00Z")
    assert not calendar.is_session("2018-12-05")  # Bush national mourning
    assert not calendar.is_session("2025-01-09")  # Carter national mourning


def test_exact_21_return_window_and_hand_calculated_sample_volatility() -> None:
    sessions = _xnys_sessions(23)
    daily_returns = [0.01] * 20 + [0.03]
    prices = _prices_from_daily_returns(sessions[:22], daily_returns)

    result = _single_target_result(prices, sessions[21])
    observation = result.iloc[0]
    expected = math.sqrt(252.0) * statistics.stdev(daily_returns)

    assert observation["window_start_date"] == sessions[0]
    assert observation["window_end_date"] == sessions[21]
    assert observation["raw_annualized_volatility"] == pytest.approx(
        expected, rel=2e-12
    )
    assert observation["annualized_volatility_pct"] == pytest.approx(
        100.0 * expected, rel=2e-12
    )
    assert observation["window_eligible"]
    assert observation["window_status"] == "eligible"
    assert observation["first_prospective_session"] == sessions[22]


def test_annualized_sample_volatility_uses_ddof_one_and_stable_centering() -> None:
    returns = np.array([0.01] * 20 + [0.010000000001], dtype=np.float64)
    expected = math.sqrt(252.0) * statistics.stdev(returns.tolist())

    actual = _annualized_sample_volatility(returns)

    assert actual == pytest.approx(expected, rel=1e-12)
    assert actual > 0.0


def test_one_ulp_return_difference_preserves_exact_sample_dispersion() -> None:
    returns = [1.0] * 20 + [math.nextafter(1.0, math.inf)]
    expected = _high_precision_volatility_from_returns(returns)

    actual = _annualized_sample_volatility(np.array(returns, dtype=np.float64))

    assert expected == 7.691850745534255e-16
    # The production and 120-digit reference paths round only the final result
    # to Float64, so one final-result ULP is the relevant tolerance.
    assert abs(actual - expected) <= math.ulp(expected)
    assert actual > 0.0


def test_exact_stored_float_prices_do_not_collapse_distinct_log_returns() -> None:
    sessions = _xnys_sessions(22)
    adjusted_close = [math.exp(-10.5 + position) for position in range(22)]
    expected = _high_precision_volatility_from_prices(adjusted_close)
    prices = _canonical_prices(sessions, adjusted_close)

    observation = _single_target_result(prices, sessions[-1]).iloc[0]

    assert expected == 1.1660524795036886e-15
    assert abs(observation["raw_annualized_volatility"] - expected) <= math.ulp(
        expected
    )
    assert observation["raw_annualized_volatility"] > 0.0


def test_nearly_constant_distinct_stored_price_returns_remain_positive() -> None:
    sessions = _xnys_sessions(22)
    adjusted_close = [math.exp(-5.25 + 0.5 * position) for position in range(22)]
    expected = _high_precision_volatility_from_prices(adjusted_close)
    prices = _canonical_prices(sessions, adjusted_close)

    observation = _single_target_result(prices, sessions[-1]).iloc[0]

    assert expected > 0.0
    assert abs(observation["raw_annualized_volatility"] - expected) <= math.ulp(
        expected
    )


def test_identical_daily_returns_produce_valid_zero_volatility() -> None:
    sessions = _xnys_sessions(22)
    adjusted_close = [2**position for position in range(len(sessions))]
    prices = _canonical_prices(sessions, adjusted_close, adjusted_dtype="Int64")

    observation = _single_target_result(prices, sessions[-1]).iloc[0]

    assert observation["raw_annualized_volatility"] == 0.0
    assert observation["annualized_volatility_pct"] == 0.0
    assert observation["window_eligible"]
    assert pd.isna(observation["volatility_percentile"])


def test_ordinary_positive_and_negative_returns_match_exact_stored_prices() -> None:
    sessions = _xnys_sessions(22)
    requested_returns = [0.01, -0.015, 0.02, -0.005, 0.0, 0.012, -0.008] * 3
    prices = _prices_from_daily_returns(sessions, requested_returns)
    stored_prices = prices["adjusted_close"].astype("float64").tolist()
    expected = _high_precision_volatility_from_prices(stored_prices)

    observation = _single_target_result(prices, sessions[-1]).iloc[0]

    assert observation["raw_annualized_volatility"] == pytest.approx(
        expected,
        rel=2e-15,
        abs=math.ulp(expected),
    )


def test_first_raw_normalized_and_prospective_positions_and_dates() -> None:
    sessions = _xnys_sessions(275)
    prices = _canonical_prices(sessions[:274], [100.0] * 274)

    result = calculate_volatility(prices)
    first_raw = result.loc[result["raw_annualized_volatility"].notna()].iloc[0]
    first_normalized = result.loc[result["volatility_percentile"].notna()].iloc[0]

    assert first_raw["signal_date"] == sessions[21]
    assert first_raw["signal_date"] == pd.Timestamp("2018-02-01")
    assert first_normalized["signal_date"] == sessions[273]
    assert first_normalized["signal_date"] == pd.Timestamp("2019-02-04")
    assert first_normalized["normalization_reference_count"] == 252
    assert first_normalized["volatility_percentile"] == 50.0
    assert first_normalized["first_prospective_session"] == sessions[274]
    assert first_normalized["first_prospective_session"] == pd.Timestamp("2019-02-05")


def test_252_prior_minimum_preserves_raw_before_normalization() -> None:
    sessions = _xnys_sessions(274)
    prices = _canonical_prices(sessions, [100.0] * len(sessions))

    result = calculate_volatility(
        prices,
        evaluation_start=sessions[272],
        evaluation_end=sessions[273],
    )
    before = _row_at(result, sessions[272])
    first = _row_at(result, sessions[273])

    assert before["raw_annualized_volatility"] == 0.0
    assert before["normalization_reference_count"] == 251
    assert pd.isna(before["volatility_percentile"])
    assert first["raw_annualized_volatility"] == 0.0
    assert first["normalization_reference_count"] == 252
    assert first["volatility_percentile"] == 50.0


def test_reference_window_is_exactly_d_t_minus_755_through_d_t_minus_1(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = _xnys_sessions(1101)
    prices = _canonical_prices(sessions, [100.0] * len(sessions))

    def controlled_raw_values(daily_returns: np.ndarray) -> np.ndarray:
        values = np.full(len(daily_returns), np.nan, dtype=np.float64)
        current = len(values) - 1
        values[current - 755 : current] = 0.0
        values[current - 756] = -0.5
        values[current - 755] = -0.1
        values[current] = 0.0
        return values

    monkeypatch.setattr(
        volatility_module, "_raw_volatility_values", controlled_raw_values
    )
    observation = _single_target_result(prices, sessions[-1]).iloc[0]
    expected = 100.0 * (1.0 + 0.5 * 754.0) / 755.0

    assert observation["normalization_reference_count"] == 755
    assert observation["volatility_percentile"] == pytest.approx(expected)


@pytest.mark.parametrize(
    ("reference_value", "current_value", "expected_percentile"),
    [(0.0, 1.0, 100.0), (1.0, 0.0, 0.0), (0.0, 0.0, 50.0)],
)
def test_zero_variance_reference_population_has_exact_boundaries(
    monkeypatch: pytest.MonkeyPatch,
    reference_value: float,
    current_value: float,
    expected_percentile: float,
) -> None:
    sessions = _xnys_sessions(274)
    prices = _canonical_prices(sessions, [100.0] * len(sessions))

    def controlled_raw_values(daily_returns: np.ndarray) -> np.ndarray:
        values = np.full(len(daily_returns), np.nan, dtype=np.float64)
        current = len(values) - 1
        values[current - 252 : current] = reference_value
        values[current] = current_value
        return values

    monkeypatch.setattr(
        volatility_module, "_raw_volatility_values", controlled_raw_values
    )
    observation = _single_target_result(prices, sessions[-1]).iloc[0]

    assert observation["normalization_reference_count"] == 252
    assert observation["raw_annualized_volatility"] == current_value
    assert observation["volatility_percentile"] == expected_percentile


def test_per_etf_isolation_sorting_and_input_immutability() -> None:
    sessions = _xnys_sessions(274)
    spy = _canonical_prices(sessions, [100.0] * len(sessions), ticker="SPY")
    qqq_values = [100.0] * len(sessions)
    qqq_values[-1] = 120.0
    qqq = _canonical_prices(sessions, qqq_values, ticker="QQQ")
    prices = pd.concat([spy, qqq], ignore_index=True).iloc[::-1].reset_index(drop=True)
    original = prices.copy(deep=True)

    result = _single_target_result(prices, sessions[-1])
    current = result.set_index("ticker")

    assert result["ticker"].tolist() == ["QQQ", "SPY"]
    assert current.loc["QQQ", "volatility_percentile"] == 100.0
    assert current.loc["SPY", "volatility_percentile"] == 50.0
    pd.testing.assert_frame_equal(prices, original)


@pytest.mark.parametrize("missing_position", [0, 10, 21])
def test_missing_price_anywhere_in_complete_chain_invalidates_window(
    missing_position: int,
) -> None:
    sessions = _xnys_sessions(22)
    prices = _canonical_prices(sessions, [100.0] * len(sessions))
    missing_date = sessions[missing_position]
    prices = prices.loc[prices["date"].ne(missing_date)].reset_index(drop=True)

    observation = _single_target_result(prices, sessions[-1]).iloc[0]

    assert observation["window_start_date"] == sessions[0]
    assert observation["window_end_date"] == sessions[-1]
    assert not observation["window_eligible"]
    assert observation["window_status"] == "missing_price_rows"
    assert observation["missing_row_count"] == 1
    assert observation["missing_row_dates"] == (missing_date,)
    assert observation["missing_adjusted_close_count"] == 0
    assert observation["missing_adjusted_close_dates"] == ()
    assert pd.isna(observation["raw_annualized_volatility"])
    assert pd.isna(observation["annualized_volatility_pct"])
    assert pd.isna(observation["volatility_percentile"])


@pytest.mark.parametrize("missing_position", [0, 10, 21])
def test_missing_adjusted_close_anywhere_invalidates_without_close_fallback(
    missing_position: int,
) -> None:
    sessions = _xnys_sessions(22)
    prices = _canonical_prices(sessions, [100.0] * len(sessions))
    missing_date = sessions[missing_position]
    prices.loc[prices["date"].eq(missing_date), "adjusted_close"] = pd.NA
    assert prices.loc[prices["date"].eq(missing_date), "close"].iloc[0] == 100.0

    observation = _single_target_result(prices, sessions[-1]).iloc[0]

    assert not observation["window_eligible"]
    assert observation["window_status"] == "missing_adjusted_close"
    assert observation["missing_row_count"] == 0
    assert observation["missing_row_dates"] == ()
    assert observation["missing_adjusted_close_count"] == 1
    assert observation["missing_adjusted_close_dates"] == (missing_date,)
    assert pd.isna(observation["raw_annualized_volatility"])


def test_missingness_classes_are_mutually_exclusive_and_deterministic() -> None:
    sessions = _xnys_sessions(22)
    prices = _canonical_prices(sessions, [100.0] * len(sessions))
    missing_row_date = sessions[5]
    missing_adjusted_date = sessions[6]
    prices = prices.loc[prices["date"].ne(missing_row_date)].reset_index(drop=True)
    prices.loc[prices["date"].eq(missing_adjusted_date), "adjusted_close"] = pd.NA

    observation = _single_target_result(prices, sessions[-1]).iloc[0]

    assert observation["window_status"] == ("missing_price_rows|missing_adjusted_close")
    assert observation["missing_row_dates"] == (missing_row_date,)
    assert observation["missing_adjusted_close_dates"] == (missing_adjusted_date,)
    assert set(observation["missing_row_dates"]).isdisjoint(
        observation["missing_adjusted_close_dates"]
    )


def test_missing_interior_price_is_not_reinterpreted_as_adjacent_nonmissing() -> None:
    sessions = _xnys_sessions(22)
    prices = _canonical_prices(sessions, np.linspace(100.0, 121.0, len(sessions)))
    missing_date = sessions[10]
    prices = prices.loc[prices["date"].ne(missing_date)].reset_index(drop=True)

    observation = _single_target_result(prices, sessions[-1]).iloc[0]

    assert observation["window_start_date"] == sessions[0]
    assert observation["window_end_date"] == sessions[-1]
    assert observation["missing_row_dates"] == (missing_date,)
    assert pd.isna(observation["raw_annualized_volatility"])


def test_off_xnys_canonical_date_inside_scope_is_rejected() -> None:
    sessions = pd.DatetimeIndex(["2018-12-03", "2018-12-04", "2018-12-06"])
    prices = _canonical_prices(sessions, [100.0, 100.0, 100.0])
    invalid = _canonical_prices(pd.DatetimeIndex(["2018-12-05"]), [100.0])
    prices = pd.concat([prices, invalid], ignore_index=True)

    with pytest.raises(
        VolatilityDataValidationError, match=r"XNYS sessions.*2018-12-05"
    ):
        calculate_volatility(prices)


def test_early_close_and_friday_timing_use_next_xnys_session() -> None:
    early_close_dates = pd.DatetimeIndex(
        ["2024-11-25", "2024-11-26", "2024-11-27", "2024-11-29"]
    )
    early_close_prices = _canonical_prices(
        early_close_dates, [100.0] * len(early_close_dates)
    )
    friday_dates = pd.DatetimeIndex(["2024-11-21", "2024-11-22"])
    friday_prices = _canonical_prices(friday_dates, [100.0] * len(friday_dates))
    special_closure_dates = pd.DatetimeIndex(["2018-12-03", "2018-12-04", "2018-12-06"])
    special_closure_prices = _canonical_prices(
        special_closure_dates,
        [100.0] * len(special_closure_dates),
    )

    early_close = _row_at(
        calculate_volatility(early_close_prices), pd.Timestamp("2024-11-29")
    )
    friday = _row_at(calculate_volatility(friday_prices), pd.Timestamp("2024-11-22"))
    special_closure = _row_at(
        calculate_volatility(special_closure_prices),
        pd.Timestamp("2018-12-04"),
    )

    assert early_close["first_prospective_session"] == pd.Timestamp("2024-12-02")
    assert friday["first_prospective_session"] == pd.Timestamp("2024-11-25")
    assert special_closure["first_prospective_session"] == pd.Timestamp("2018-12-06")


@pytest.mark.parametrize(
    "invalid_bound",
    [
        "not-a-date",
        pd.Timestamp("2024-01-02T00:00:00Z"),
        pd.Timestamp("2024-01-02T12:00:00"),
    ],
)
def test_nonempty_input_rejects_invalid_evaluation_bounds(
    invalid_bound: object,
) -> None:
    sessions = _xnys_sessions(2)
    prices = _canonical_prices(sessions, [100.0, 100.0])

    with pytest.raises(VolatilityDataValidationError, match="evaluation_start"):
        calculate_volatility(prices, evaluation_start=invalid_bound)  # type: ignore[arg-type]


def test_nonempty_input_rejects_reversed_evaluation_bounds() -> None:
    sessions = _xnys_sessions(3)
    prices = _canonical_prices(sessions, [100.0] * len(sessions))

    with pytest.raises(
        VolatilityDataValidationError,
        match="evaluation_start must be on or before evaluation_end",
    ):
        calculate_volatility(
            prices,
            evaluation_start=sessions[2],
            evaluation_end=sessions[1],
        )


def test_empty_input_with_omitted_or_one_valid_bound_is_typed() -> None:
    empty_prices = _canonical_prices(pd.DatetimeIndex([]), [])

    omitted = calculate_volatility(empty_prices)
    one_bound = calculate_volatility(empty_prices, evaluation_end="2024-01-02")

    for result in (omitted, one_bound):
        assert result.empty
        assert tuple(result.columns) == VOLATILITY_OUTPUT_COLUMNS
        assert str(result["ticker"].dtype) == "string"
        assert str(result["signal_date"].dtype) == "datetime64[ns]"
        assert str(result["raw_annualized_volatility"].dtype) == "Float64"
        assert str(result["window_eligible"].dtype) == "boolean"


@pytest.mark.parametrize(
    "invalid_bound",
    [
        "not-a-date",
        pd.Timestamp("2024-01-02T00:00:00Z"),
        pd.Timestamp("2024-01-02T12:00:00"),
    ],
)
def test_empty_input_rejects_invalid_supplied_bound(invalid_bound: object) -> None:
    empty_prices = _canonical_prices(pd.DatetimeIndex([]), [])

    with pytest.raises(VolatilityDataValidationError, match="evaluation_start"):
        calculate_volatility(empty_prices, evaluation_start=invalid_bound)  # type: ignore[arg-type]


def test_empty_input_rejects_reversed_valid_bounds() -> None:
    empty_prices = _canonical_prices(pd.DatetimeIndex([]), [])

    with pytest.raises(
        VolatilityDataValidationError,
        match="evaluation_start must be on or before evaluation_end",
    ):
        calculate_volatility(
            empty_prices,
            evaluation_start="2024-01-03",
            evaluation_end="2024-01-02",
        )


@pytest.mark.parametrize(
    ("adjusted_dtype", "start_value"),
    [
        ("Int64", 2**53 - 10),
        ("Int64", 2**53 + 101),
        ("UInt64", 2**63 + 101),
        ("int64[pyarrow]", 2**53 + 101),
        ("uint64[pyarrow]", 2**63 + 101),
    ],
)
def test_large_integer_adjusted_prices_produce_finite_eligible_output(
    adjusted_dtype: str,
    start_value: int,
) -> None:
    sessions = _xnys_sessions(22)
    values = [start_value + position for position in range(len(sessions))]
    prices = _canonical_prices(sessions, values, adjusted_dtype=adjusted_dtype)
    expected = _high_precision_volatility_from_prices(values)

    observation = _single_target_result(prices, sessions[-1]).iloc[0]

    assert observation["window_eligible"]
    assert math.isfinite(observation["raw_annualized_volatility"])
    assert abs(observation["raw_annualized_volatility"] - expected) <= math.ulp(
        expected
    )
    assert math.isfinite(observation["annualized_volatility_pct"])


def test_extreme_float_ratios_produce_finite_eligible_output() -> None:
    sessions = _xnys_sessions(22)
    smallest = float(np.nextafter(0.0, 1.0))
    largest = float(np.finfo(np.float64).max)
    values = [smallest if position % 2 == 0 else largest for position in range(22)]
    prices = _canonical_prices(sessions, values)
    expected = _high_precision_volatility_from_prices(values)

    observation = _single_target_result(prices, sessions[-1]).iloc[0]

    assert observation["window_eligible"]
    assert math.isfinite(observation["raw_annualized_volatility"])
    assert observation["raw_annualized_volatility"] > 0.0
    assert observation["raw_annualized_volatility"] == pytest.approx(
        expected,
        rel=2e-15,
    )
    assert math.isfinite(observation["annualized_volatility_pct"])


def test_eligible_window_cannot_return_a_nonfinite_derived_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = _xnys_sessions(22)
    prices = _canonical_prices(sessions, [100.0] * len(sessions))

    monkeypatch.setattr(
        volatility_module,
        "_annualized_sample_volatility",
        lambda returns: math.inf,
    )

    with pytest.raises(
        VolatilityDataValidationError,
        match="eligible Volatility window did not produce a finite value",
    ):
        _single_target_result(prices, sessions[-1])


def test_output_columns_dtypes_and_status_value_contract() -> None:
    sessions = _xnys_sessions(22)
    prices = _canonical_prices(sessions, [100.0] * len(sessions))

    result = _single_target_result(prices, sessions[-1])
    observation = result.iloc[0]

    assert tuple(result.columns) == VOLATILITY_OUTPUT_COLUMNS
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
    assert {column: str(result[column].dtype) for column in result} == expected_dtypes
    assert observation["window_eligible"]
    assert observation["window_status"] == "eligible"
    assert observation["annualized_volatility_pct"] == (
        100.0 * observation["raw_annualized_volatility"]
    )
    assert observation["normalization_reference_count"] == 0
    assert pd.isna(observation["volatility_percentile"])


@pytest.mark.parametrize(
    "column",
    [
        "raw_annualized_volatility",
        "annualized_volatility_pct",
        "volatility_percentile",
    ],
)
@pytest.mark.parametrize(
    "nonfinite_value",
    [
        pytest.param(np.nan, id="present-nan"),
        pytest.param(np.inf, id="positive-infinity"),
        pytest.param(-np.inf, id="negative-infinity"),
    ],
)
def test_internal_output_validator_rejects_present_nonfinite_float_values(
    column: str,
    nonfinite_value: float,
) -> None:
    malformed = _normalized_target_result().copy(deep=True)
    malformed[column] = pd.Series(
        pd.arrays.FloatingArray(
            np.array([nonfinite_value], dtype=np.float64),
            np.array([False], dtype=bool),
        )
    )

    assert str(malformed[column].dtype) == "Float64"
    assert not malformed[column].isna().iloc[0]
    assert not np.isfinite(float(malformed.at[0, column]))
    with pytest.raises(VolatilityDataValidationError) as error_info:
        _validate_output(malformed)

    assert column in str(error_info.value)
    assert "non-finite" in str(error_info.value)


def test_internal_output_validator_preserves_permitted_masked_missing_values() -> None:
    ordinary = _normalized_target_result().copy(deep=True)
    ordinary_before = ordinary.copy(deep=True)

    _validate_output(ordinary)

    pd.testing.assert_frame_equal(ordinary, ordinary_before)

    early_sessions = _xnys_sessions(22)
    early_prices = _canonical_prices(early_sessions, [100.0] * len(early_sessions))
    insufficient_history = _single_target_result(early_prices, early_sessions[-1])

    assert insufficient_history.at[0, "volatility_percentile"] is pd.NA
    _validate_output(insufficient_history)

    missing_date = early_sessions[0]
    ineligible_prices = early_prices.loc[
        early_prices["date"].ne(missing_date)
    ].reset_index(drop=True)
    ineligible = _single_target_result(ineligible_prices, early_sessions[-1])
    for column in (
        "raw_annualized_volatility",
        "annualized_volatility_pct",
        "volatility_percentile",
    ):
        assert ineligible.at[0, column] is pd.NA
    _validate_output(ineligible)


def test_internal_output_validator_rejects_schema_sorting_and_duplicate_keys() -> None:
    sessions = _xnys_sessions(23)
    prices = _canonical_prices(sessions, [100.0] * len(sessions))
    result = calculate_volatility(prices)

    _validate_output(result)
    with pytest.raises(VolatilityDataValidationError, match="columns"):
        _validate_output(result[result.columns[::-1]])
    with pytest.raises(VolatilityDataValidationError, match="sorted"):
        _validate_output(result.iloc[::-1].reset_index(drop=True))
    duplicated = (
        pd.concat([result, result.iloc[[-1]]], ignore_index=True)
        .sort_values(["ticker", "signal_date"], kind="mergesort")
        .reset_index(drop=True)
    )
    with pytest.raises(VolatilityDataValidationError, match="duplicate"):
        _validate_output(duplicated)


def test_internal_output_validator_rejects_numeric_and_status_contradictions() -> None:
    sessions = _xnys_sessions(274)
    prices = _canonical_prices(sessions, [100.0] * len(sessions))
    result = _single_target_result(prices, sessions[-1])

    display_mismatch = result.copy(deep=True)
    display_mismatch.loc[0, "annualized_volatility_pct"] = 1.0
    with pytest.raises(VolatilityDataValidationError, match="100 times"):
        _validate_output(display_mismatch)

    out_of_range = result.copy(deep=True)
    out_of_range.loc[0, "volatility_percentile"] = 101.0
    with pytest.raises(VolatilityDataValidationError, match=r"\[0, 100\]"):
        _validate_output(out_of_range)

    ineligible_with_value = result.copy(deep=True)
    ineligible_with_value.loc[0, "window_eligible"] = False
    ineligible_with_value.loc[0, "window_status"] = "missing_price_rows"
    ineligible_with_value.loc[0, "missing_row_count"] = 1
    ineligible_with_value.at[0, "missing_row_dates"] = (
        ineligible_with_value.loc[0, "window_start_date"],
    )
    with pytest.raises(VolatilityDataValidationError, match="ineligible"):
        _validate_output(ineligible_with_value)


def test_internal_output_validator_rejects_malformed_diagnostics() -> None:
    sessions = _xnys_sessions(22)
    prices = _canonical_prices(sessions, [100.0] * len(sessions))
    result = _single_target_result(prices, sessions[-1])

    wrong_type = result.copy(deep=True)
    wrong_type.at[0, "missing_row_dates"] = []
    with pytest.raises(VolatilityDataValidationError, match="tuples"):
        _validate_output(wrong_type)

    wrong_count = result.copy(deep=True)
    wrong_count.loc[0, "missing_row_count"] = 1
    with pytest.raises(VolatilityDataValidationError, match="disagrees"):
        _validate_output(wrong_count)

    overlap = result.copy(deep=True)
    diagnostic_date = overlap.loc[0, "window_start_date"]
    overlap.loc[0, "missing_row_count"] = 1
    overlap.loc[0, "missing_adjusted_close_count"] = 1
    overlap.at[0, "missing_row_dates"] = (diagnostic_date,)
    overlap.at[0, "missing_adjusted_close_dates"] = (diagnostic_date,)
    with pytest.raises(VolatilityDataValidationError, match="overlap"):
        _validate_output(overlap)


def test_internal_output_validator_rejects_false_calendar_relationships() -> None:
    sessions = _xnys_sessions(23)
    prices = _canonical_prices(sessions[:22], [100.0] * 22)
    result = _single_target_result(prices, pd.Timestamp("2018-02-01"))

    wrong_start = result.copy(deep=True)
    wrong_start.loc[0, "window_start_date"] = pd.Timestamp("2018-01-03")
    with pytest.raises(VolatilityDataValidationError, match="exactly 21 XNYS"):
        _validate_output(wrong_start)

    wrong_prospective = result.copy(deep=True)
    wrong_prospective.loc[0, "first_prospective_session"] = pd.Timestamp("2018-02-03")
    with pytest.raises(VolatilityDataValidationError, match="next XNYS"):
        _validate_output(wrong_prospective)

    wrong_end = result.copy(deep=True)
    wrong_end.loc[0, "window_end_date"] = sessions[20]
    with pytest.raises(VolatilityDataValidationError, match="window end"):
        _validate_output(wrong_end)

    for invalid_diagnostic_date in (
        pd.Timestamp("2018-01-13"),
        pd.Timestamp("2017-12-29"),
    ):
        wrong_diagnostic = result.copy(deep=True)
        wrong_diagnostic.loc[0, "window_eligible"] = False
        wrong_diagnostic.loc[0, "window_status"] = "missing_price_rows"
        wrong_diagnostic.loc[0, "raw_annualized_volatility"] = pd.NA
        wrong_diagnostic.loc[0, "annualized_volatility_pct"] = pd.NA
        wrong_diagnostic.loc[0, "volatility_percentile"] = pd.NA
        wrong_diagnostic.loc[0, "missing_row_count"] = 1
        wrong_diagnostic.at[0, "missing_row_dates"] = (invalid_diagnostic_date,)
        with pytest.raises(
            VolatilityDataValidationError,
            match="exact XNYS sessions in its 22-price window",
        ):
            _validate_output(wrong_diagnostic)


def test_output_contract_contains_no_forbidden_component_or_persistence_fields() -> (
    None
):
    forbidden_terms = (
        "flow",
        "composite",
        "crowding",
        "weight",
        "threshold",
        "persist",
    )

    assert not any(
        term in column.lower()
        for column in VOLATILITY_OUTPUT_COLUMNS
        for term in forbidden_terms
    )
