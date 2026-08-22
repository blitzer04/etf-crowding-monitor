"""Tests for the approved XNYS-aligned Momentum component."""

from __future__ import annotations

import math
from decimal import Decimal, localcontext
from fractions import Fraction
from importlib.metadata import version

import exchange_calendars as xcals  # type: ignore[import-untyped]
import numpy as np
import pandas as pd
import pytest

from etf_crowding.signals.momentum import (
    MOMENTUM_OUTPUT_COLUMNS,
    MomentumDataValidationError,
    _stable_log_return,
    _stable_simple_return_percentage,
    calculate_momentum,
)

RETRIEVED_AT = pd.Timestamp("2026-08-22T00:00:00Z")


def _xnys_sessions(count: int) -> pd.DatetimeIndex:
    calendar = xcals.get_calendar("XNYS", start="2018-01-01", end="2026-12-31")
    return calendar.sessions[:count]


def _canonical_prices(
    sessions: pd.DatetimeIndex,
    adjusted_close: list[float | None] | np.ndarray,
    *,
    ticker: str = "SPY",
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
            "adjusted_close": pd.Series(values, dtype="Float64"),
            "volume": pd.Series([np.nan] * len(sessions), dtype="float64"),
            "retrieved_at": pd.Series(
                [RETRIEVED_AT] * len(sessions), dtype="datetime64[ns, UTC]"
            ),
        }
    )


def _prices_with_raw_returns(
    sessions: pd.DatetimeIndex,
    raw_returns: dict[int, float],
    *,
    ticker: str = "SPY",
) -> pd.DataFrame:
    adjusted_close = np.ones(len(sessions), dtype=np.float64)
    for position in range(252, len(sessions)):
        adjusted_close[position] = adjusted_close[position - 252] * math.exp(
            raw_returns.get(position, 0.0)
        )
    return _canonical_prices(sessions, adjusted_close, ticker=ticker)


def _row_at(result: pd.DataFrame, signal_date: pd.Timestamp) -> pd.Series:
    return result.loc[result["signal_date"].eq(signal_date)].iloc[0]


def _observation_for_float_endpoints(
    start_price: float,
    end_price: float,
) -> pd.Series:
    sessions = _xnys_sessions(253)
    adjusted_close = [start_price] * len(sessions)
    adjusted_close[-1] = end_price
    prices = _canonical_prices(sessions, adjusted_close)
    return _row_at(calculate_momentum(prices), sessions[-1])


def _exact_numeric_fraction(value: int | float) -> Fraction:
    if isinstance(value, int):
        return Fraction(value)
    return Fraction.from_float(value)


def _high_precision_log_return(
    start_price: int | float,
    end_price: int | float,
) -> float:
    with localcontext() as context:
        context.prec = 120
        start = (
            Decimal(start_price)
            if isinstance(start_price, int)
            else Decimal.from_float(start_price)
        )
        end = (
            Decimal(end_price)
            if isinstance(end_price, int)
            else Decimal.from_float(end_price)
        )
        return float(end.ln() - start.ln())


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


def test_exact_252_session_endpoint_return_and_display_value() -> None:
    sessions = _xnys_sessions(254)
    adjusted_close = [100.0] * 253
    adjusted_close[-1] = 110.0
    prices = _canonical_prices(sessions[:253], adjusted_close)

    result = calculate_momentum(prices)
    observation = _row_at(result, sessions[252])

    assert observation["endpoint_start_date"] == sessions[0]
    assert observation["endpoint_end_date"] == sessions[252]
    assert observation["start_adjusted_close"] == 100.0
    assert observation["end_adjusted_close"] == 110.0
    assert observation["raw_momentum"] == pytest.approx(math.log(1.1))
    assert observation["simple_return_pct"] == pytest.approx(10.0)
    assert observation["simple_return_status"] == "available"
    assert observation["endpoint_eligible"]
    assert observation["normalization_reference_count"] == 0
    assert pd.isna(observation["momentum_percentile"])
    assert observation["first_prospective_session"] == sessions[253]


@pytest.mark.parametrize(
    ("dtype", "start_price", "end_price"),
    [
        ("Int64", 2**53, 2**53 + 1),
        ("UInt64", 2**63 + 1, 2**63 + 2),
    ],
)
def test_exact_integer_endpoint_difference_survives_return_calculation(
    dtype: str,
    start_price: int,
    end_price: int,
) -> None:
    sessions = _xnys_sessions(253)
    prices = _canonical_prices(sessions, [100.0] * len(sessions))
    endpoint_prices = [start_price] * len(sessions)
    endpoint_prices[-1] = end_price
    prices["adjusted_close"] = pd.Series(endpoint_prices, dtype=dtype)

    observation = _row_at(calculate_momentum(prices), sessions[-1])
    expected_relative_change = 1.0 / start_price

    assert observation["start_adjusted_close"] == start_price
    assert observation["end_adjusted_close"] == end_price
    assert observation["raw_momentum"] == pytest.approx(
        math.log1p(expected_relative_change), rel=1e-15
    )
    assert observation["raw_momentum"] != 0.0
    assert observation["simple_return_pct"] == pytest.approx(
        100.0 * expected_relative_change, rel=1e-15
    )
    assert observation["simple_return_pct"] != 0.0
    assert observation["simple_return_status"] == "available"
    assert observation["endpoint_eligible"]


def test_large_decline_avoids_near_negative_one_relative_cancellation() -> None:
    start_price = 1.0
    end_price = 1.49 * 2.0**-53

    observation = _observation_for_float_endpoints(start_price, end_price)

    assert observation["raw_momentum"] == pytest.approx(-36.33802444971973, rel=1e-15)
    assert observation["raw_momentum"] == pytest.approx(
        _high_precision_log_return(start_price, end_price), rel=1e-15
    )
    assert observation["endpoint_eligible"]


@pytest.mark.parametrize(
    ("start_price", "end_price"),
    [
        (1.0, float(np.nextafter(2.0**-54, math.inf))),
        (float(np.nextafter(0.0, 1.0)), float(np.finfo(np.float64).max)),
        (float(np.finfo(np.float64).max), float(np.nextafter(0.0, 1.0))),
        (8.0, 4.0),
        (8.0, float(np.nextafter(4.0, 0.0))),
        (8.0, float(np.nextafter(4.0, math.inf))),
        (4.0, 8.0),
        (4.0, float(np.nextafter(8.0, 0.0))),
        (4.0, float(np.nextafter(8.0, math.inf))),
        (100.0, 110.0),
        (110.0, 100.0),
    ],
    ids=[
        "immediately-above-relative-collapse",
        "severe-positive-ratio",
        "severe-negative-ratio",
        "lower-band-boundary",
        "below-lower-band-boundary",
        "above-lower-band-boundary",
        "upper-band-boundary",
        "below-upper-band-boundary",
        "above-upper-band-boundary",
        "ordinary-positive-return",
        "ordinary-negative-return",
    ],
)
def test_hybrid_log_return_matches_high_precision_endpoint_formula(
    start_price: float,
    end_price: float,
) -> None:
    raw_momentum = _stable_log_return(start_price, end_price)
    expected = _high_precision_log_return(start_price, end_price)

    assert raw_momentum == pytest.approx(
        expected,
        rel=2e-15,
        abs=2.0 * math.ulp(expected),
    )


@pytest.mark.parametrize(
    ("start_price", "end_price"),
    [
        (2**53 + 1, float(2**53)),
        (float(2**53), 2**53 + 1),
        (2**53 + 1, float(2**53 + 2)),
        (float(2**53 + 2), 2**53 + 1),
        (2**63 - 1, float(2**63)),
        (float(2**63), 2**63 - 1),
        (2**63 + 1, float(2**63)),
        (float(2**63), 2**63 + 1),
        (2**64 - 1, float(2**64)),
        (float(2**64), 2**64 - 1),
        (8, 4.0),
        (8, float(np.nextafter(4.0, 0.0))),
        (8, float(np.nextafter(4.0, math.inf))),
        (4, 8.0),
        (4, float(np.nextafter(8.0, 0.0))),
        (4, float(np.nextafter(8.0, math.inf))),
        (1, float(np.finfo(np.float64).max)),
        (float(np.finfo(np.float64).max), 1),
    ],
    ids=[
        "large-int-to-adjacent-float",
        "adjacent-float-to-large-int",
        "large-int-to-next-float",
        "next-float-to-large-int",
        "signed-int64-boundary-up",
        "signed-int64-boundary-down",
        "uint64-above-signed-boundary-down",
        "uint64-above-signed-boundary-up",
        "uint64-maximum-to-next-float",
        "next-float-to-uint64-maximum",
        "mixed-lower-band-boundary",
        "mixed-below-lower-band-boundary",
        "mixed-above-lower-band-boundary",
        "mixed-upper-band-boundary",
        "mixed-below-upper-band-boundary",
        "mixed-above-upper-band-boundary",
        "mixed-severe-positive-ratio",
        "mixed-severe-negative-ratio",
    ],
)
def test_mixed_numeric_endpoints_preserve_raw_and_display_returns(
    start_price: int | float,
    end_price: int | float,
) -> None:
    raw_momentum = _stable_log_return(start_price, end_price)
    expected_raw = _high_precision_log_return(start_price, end_price)
    raw_tolerance = max(
        4.0 * math.ulp(expected_raw),
        5e-15 * abs(expected_raw),
    )

    assert math.isfinite(raw_momentum)
    assert raw_momentum != 0.0
    assert abs(raw_momentum - expected_raw) <= raw_tolerance

    simple_return_pct, simple_return_status = _stable_simple_return_percentage(
        start_price,
        end_price,
        raw_momentum,
    )
    start_exact = _exact_numeric_fraction(start_price)
    end_exact = _exact_numeric_fraction(end_price)
    expected_percentage = 100 * (end_exact - start_exact) / start_exact
    try:
        expected_simple_return = float(expected_percentage)
    except OverflowError:
        expected_simple_return = math.inf

    if math.isfinite(expected_simple_return):
        simple_tolerance = max(
            4.0 * math.ulp(expected_simple_return),
            5e-15 * abs(expected_simple_return),
        )
        assert simple_return_status == "available"
        assert simple_return_pct is not None
        assert abs(simple_return_pct - expected_simple_return) <= simple_tolerance
    else:
        assert simple_return_status == "exceeds_float64_range"
        assert simple_return_pct is None


def test_nearly_equal_large_float_endpoints_produce_nonzero_return() -> None:
    sessions = _xnys_sessions(253)
    end_price = np.finfo(np.float64).max
    start_price = np.nextafter(end_price, 0.0)
    adjusted_close = [start_price] * len(sessions)
    adjusted_close[-1] = end_price
    prices = _canonical_prices(sessions, adjusted_close)

    observation = _row_at(calculate_momentum(prices), sessions[-1])
    expected_relative_change = (end_price - start_price) / start_price

    assert observation["raw_momentum"] == pytest.approx(
        math.log1p(expected_relative_change)
    )
    assert observation["raw_momentum"] > 0.0
    assert observation["simple_return_pct"] == pytest.approx(
        100.0 * expected_relative_change
    )
    assert observation["simple_return_status"] == "available"


@pytest.mark.parametrize("increasing", [True, False])
def test_extreme_float_ratio_keeps_finite_usable_raw_return(
    increasing: bool,
) -> None:
    sessions = _xnys_sessions(505)
    smallest_positive = np.nextafter(0.0, 1.0)
    largest_finite = np.finfo(np.float64).max
    start_price, end_price = (
        (smallest_positive, largest_finite)
        if increasing
        else (largest_finite, smallest_positive)
    )
    adjusted_close = [start_price] * len(sessions)
    adjusted_close[-1] = end_price
    prices = _canonical_prices(sessions, adjusted_close)

    observation = _row_at(calculate_momentum(prices), sessions[-1])
    expected_raw = math.log(end_price) - math.log(start_price)

    assert observation["raw_momentum"] == pytest.approx(expected_raw)
    assert math.isfinite(observation["raw_momentum"])
    assert observation["endpoint_eligible"]
    assert observation["endpoint_status"] == "eligible"
    assert observation["normalization_reference_count"] == 252
    assert observation["momentum_percentile"] == (100.0 if increasing else 0.0)
    if increasing:
        assert pd.isna(observation["simple_return_pct"])
        assert observation["simple_return_status"] == "exceeds_float64_range"
    else:
        assert observation["simple_return_pct"] == -100.0
        assert observation["simple_return_status"] == "available"


def test_early_signal_keeps_exact_pre_history_endpoint_date() -> None:
    sessions = _xnys_sessions(1)
    prices = _canonical_prices(sessions, [100.0])
    calendar = xcals.get_calendar("XNYS", start="2016-01-01", end="2018-12-31")
    signal_position = calendar.sessions.get_loc(sessions[0])
    expected_start = calendar.sessions[signal_position - 252]

    observation = _row_at(calculate_momentum(prices), sessions[0])

    assert observation["endpoint_start_date"] == expected_start
    assert observation["endpoint_status"] == "missing_start_row"
    assert pd.isna(observation["raw_momentum"])


def test_first_raw_normalized_and_prospective_positions_and_zero_variance() -> None:
    sessions = _xnys_sessions(506)
    prices = _canonical_prices(sessions, [100.0] * len(sessions))

    result = calculate_momentum(prices)

    first_raw = result.loc[result["raw_momentum"].notna()].iloc[0]
    first_normalized = result.loc[result["momentum_percentile"].notna()].iloc[0]
    before_normalized = _row_at(result, sessions[503])

    assert first_raw["signal_date"] == sessions[252]
    assert before_normalized["normalization_reference_count"] == 251
    assert pd.isna(before_normalized["momentum_percentile"])
    assert first_normalized["signal_date"] == sessions[504]
    assert first_normalized["normalization_reference_count"] == 252
    assert first_normalized["momentum_percentile"] == 50.0
    assert first_normalized["first_prospective_session"] == sessions[505]


def test_midrank_ties_use_exact_prior_population_and_exclude_current() -> None:
    sessions = _xnys_sessions(505)
    raw_returns: dict[int, float] = {}
    raw_returns.update({position: -0.1 for position in range(252, 352)})
    raw_returns.update({position: 0.0 for position in range(352, 404)})
    raw_returns.update({position: 0.1 for position in range(404, 504)})
    raw_returns[504] = 0.0
    prices = _prices_with_raw_returns(sessions, raw_returns)

    observation = _row_at(calculate_momentum(prices), sessions[504])

    assert observation["normalization_reference_count"] == 252
    assert observation["raw_momentum"] == 0.0
    assert observation["momentum_percentile"] == pytest.approx(50.0)


@pytest.mark.parametrize(
    ("current_raw", "expected_percentile"),
    [(2.0, 100.0), (-2.0, 0.0)],
)
def test_momentum_is_one_sided_and_raw_values_are_not_clipped(
    current_raw: float,
    expected_percentile: float,
) -> None:
    sessions = _xnys_sessions(505)
    prices = _prices_with_raw_returns(sessions, {504: current_raw})

    observation = _row_at(calculate_momentum(prices), sessions[504])

    assert observation["raw_momentum"] == pytest.approx(current_raw)
    assert observation["momentum_percentile"] == expected_percentile


def test_reference_window_is_exactly_d_t_minus_755_through_d_t_minus_1() -> None:
    sessions = _xnys_sessions(1101)
    current_position = 1100
    raw_returns = {
        current_position - 756: -0.5,
        current_position - 755: -0.1,
        current_position: 0.0,
    }
    prices = _prices_with_raw_returns(sessions, raw_returns)

    observation = _row_at(calculate_momentum(prices), sessions[current_position])
    expected_percentile = 100.0 * (1.0 + 0.5 * 754.0) / 755.0

    assert observation["normalization_reference_count"] == 755
    assert observation["momentum_percentile"] == pytest.approx(expected_percentile)


@pytest.mark.parametrize(
    ("failure_kind", "expected_status"),
    [
        ("missing-start-row", "missing_start_row"),
        ("missing-end-row", "missing_end_row"),
        ("missing-start-adjusted", "missing_start_adjusted_close"),
        ("missing-end-adjusted", "missing_end_adjusted_close"),
    ],
)
def test_missing_endpoint_rows_or_adjusted_prices_remain_nan_without_fallback(
    failure_kind: str,
    expected_status: str,
) -> None:
    sessions = _xnys_sessions(301)
    prices = _canonical_prices(sessions, [100.0] * len(sessions))
    start_date = sessions[48]
    end_date = sessions[300]
    affected_date = start_date if "start" in failure_kind else end_date
    if failure_kind.endswith("row"):
        prices = prices.loc[prices["date"].ne(affected_date)].reset_index(drop=True)
    else:
        prices.loc[prices["date"].eq(affected_date), "adjusted_close"] = pd.NA
        assert prices.loc[prices["date"].eq(affected_date), "close"].iloc[0] == 100.0

    result = calculate_momentum(prices, evaluation_end=end_date)
    observation = _row_at(result, end_date)

    assert not observation["endpoint_eligible"]
    assert observation["endpoint_status"] == expected_status
    assert pd.isna(observation["raw_momentum"])
    assert pd.isna(observation["simple_return_pct"])
    assert observation["simple_return_status"] == "endpoint_ineligible"
    assert pd.isna(observation["momentum_percentile"])


def test_no_interior_gaps_have_zero_diagnostic_counts() -> None:
    sessions = _xnys_sessions(301)
    prices = _canonical_prices(sessions, [100.0] * len(sessions))

    observation = _row_at(calculate_momentum(prices), sessions[300])

    assert observation["interior_missing_row_count"] == 0
    assert observation["interior_missing_row_dates"] == ()
    assert observation["interior_missing_adjusted_close_count"] == 0
    assert observation["interior_missing_adjusted_close_dates"] == ()


def test_endpoint_gaps_are_excluded_from_strict_interior_diagnostics() -> None:
    sessions = _xnys_sessions(301)
    prices = _canonical_prices(sessions, [100.0] * len(sessions))
    start_date = sessions[48]
    end_date = sessions[300]
    prices = prices.loc[~prices["date"].isin([start_date, end_date])].reset_index(
        drop=True
    )

    observation = _row_at(calculate_momentum(prices, evaluation_end=end_date), end_date)

    assert observation["endpoint_status"] == "missing_start_row|missing_end_row"
    assert observation["interior_missing_row_count"] == 0
    assert observation["interior_missing_row_dates"] == ()
    assert observation["interior_missing_adjusted_close_count"] == 0
    assert observation["interior_missing_adjusted_close_dates"] == ()


def test_multiple_interior_gaps_are_diagnostic_without_shifting_endpoints() -> None:
    sessions = _xnys_sessions(301)
    prices = _canonical_prices(sessions, [100.0] * len(sessions))
    missing_row_dates = (sessions[100], sessions[150], sessions[200])
    missing_adjusted_dates = (sessions[101], sessions[151], sessions[201])
    prices = prices.loc[~prices["date"].isin(missing_row_dates)].reset_index(drop=True)
    prices.loc[prices["date"].isin(missing_adjusted_dates), "adjusted_close"] = pd.NA
    original = prices.copy(deep=True)

    observation = _row_at(calculate_momentum(prices), sessions[300])

    assert observation["endpoint_start_date"] == sessions[48]
    assert observation["endpoint_end_date"] == sessions[300]
    assert observation["raw_momentum"] == 0.0
    assert observation["endpoint_eligible"]
    assert observation["interior_missing_row_count"] == 3
    assert observation["interior_missing_row_dates"] == missing_row_dates
    assert observation["interior_missing_adjusted_close_count"] == 3
    assert (
        observation["interior_missing_adjusted_close_dates"] == missing_adjusted_dates
    )
    pd.testing.assert_frame_equal(prices, original)


def test_off_xnys_canonical_date_inside_scope_is_rejected() -> None:
    sessions = pd.DatetimeIndex(["2018-12-03", "2018-12-04", "2018-12-06"])
    prices = _canonical_prices(sessions, [100.0, 100.0, 100.0])
    invalid = _canonical_prices(pd.DatetimeIndex(["2018-12-05"]), [100.0])
    prices = pd.concat([prices, invalid], ignore_index=True)

    with pytest.raises(MomentumDataValidationError, match=r"XNYS sessions.*2018-12-05"):
        calculate_momentum(prices)


def test_next_session_timing_skips_weekend_and_preserves_early_close_session() -> None:
    sessions = pd.DatetimeIndex(
        ["2024-11-25", "2024-11-26", "2024-11-27", "2024-11-29"]
    )
    prices = _canonical_prices(sessions, [100.0] * len(sessions))

    observation = _row_at(calculate_momentum(prices), pd.Timestamp("2024-11-29"))

    assert observation["signal_date"] == pd.Timestamp("2024-11-29")
    assert observation["first_prospective_session"] == pd.Timestamp("2024-12-02")


def test_output_is_sorted_for_multiple_etfs_and_input_is_not_mutated() -> None:
    sessions = _xnys_sessions(505)
    spy = _prices_with_raw_returns(sessions, {504: 2.0}, ticker="SPY")
    qqq = _prices_with_raw_returns(sessions, {504: -2.0}, ticker="QQQ")
    prices = pd.concat([spy, qqq], ignore_index=True).iloc[::-1].reset_index(drop=True)
    original = prices.copy(deep=True)

    result = calculate_momentum(prices)

    expected = result.sort_values(["ticker", "signal_date"], kind="mergesort")
    pd.testing.assert_frame_equal(result, expected.reset_index(drop=True))
    assert result["ticker"].drop_duplicates().tolist() == ["QQQ", "SPY"]
    current = result.loc[result["signal_date"].eq(sessions[504])].set_index("ticker")
    assert current.loc["QQQ", "momentum_percentile"] == 0.0
    assert current.loc["SPY", "momentum_percentile"] == 100.0
    pd.testing.assert_frame_equal(prices, original)


def test_empty_input_with_omitted_bounds_returns_typed_empty_output() -> None:
    empty_prices = _canonical_prices(pd.DatetimeIndex([]), [])

    result = calculate_momentum(empty_prices)

    assert result.empty
    assert tuple(result.columns) == MOMENTUM_OUTPUT_COLUMNS
    assert str(result["signal_date"].dtype) == "datetime64[ns]"
    assert str(result["simple_return_status"].dtype) == "string"


@pytest.mark.parametrize("bound_name", ["evaluation_start", "evaluation_end"])
def test_empty_input_validates_one_supplied_bound(bound_name: str) -> None:
    empty_prices = _canonical_prices(pd.DatetimeIndex([]), [])

    result = calculate_momentum(empty_prices, **{bound_name: "2024-01-02"})

    assert result.empty


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

    with pytest.raises(MomentumDataValidationError, match="evaluation_start"):
        calculate_momentum(empty_prices, evaluation_start=invalid_bound)  # type: ignore[arg-type]


def test_empty_input_rejects_reversed_valid_bounds() -> None:
    empty_prices = _canonical_prices(pd.DatetimeIndex([]), [])

    with pytest.raises(
        MomentumDataValidationError,
        match="evaluation_start must be on or before evaluation_end",
    ):
        calculate_momentum(
            empty_prices,
            evaluation_start="2024-01-03",
            evaluation_end="2024-01-02",
        )


def test_output_contract_contains_no_flow_composite_or_crowding_fields() -> None:
    assert tuple(
        calculate_momentum(_canonical_prices(_xnys_sessions(1), [100.0])).columns
    ) == (MOMENTUM_OUTPUT_COLUMNS)
    forbidden_terms = ("flow", "composite", "crowding")
    assert not any(
        term in column.lower()
        for column in MOMENTUM_OUTPUT_COLUMNS
        for term in forbidden_terms
    )
