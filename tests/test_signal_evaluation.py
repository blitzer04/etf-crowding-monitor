from __future__ import annotations

import hashlib
import json
import math
import subprocess
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from functools import cache
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import exchange_calendars as xcals  # type: ignore[import-untyped]
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

import etf_crowding.analysis.signal_evaluation as evaluation_module
import scripts.evaluate_price_signals as evaluation_script
from etf_crowding.analysis import (
    COVERAGE_COLUMNS,
    DEPENDENCE_COLUMNS,
    SignalEvaluationError,
    calculate_dependence_diagnostics,
    evaluate_price_signals,
    publish_signal_evaluation_bundle,
    resolve_evaluation_target,
    run_signal_evaluation,
    validate_signal_evaluation,
)
from etf_crowding.config import ETFDefinition, load_etf_universe
from etf_crowding.data.prices import (
    DownloadStatus,
    PriceDownloadResult,
    TickerDownloadStatus,
)
from etf_crowding.data.validation import PriceDataValidationError

RETRIEVED_AT = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
GIT_HEAD = "a" * 40


class _DateTuple(tuple[date, ...]):
    pass


@cache
def _xnys_sessions(count: int) -> pd.DatetimeIndex:
    calendar = xcals.get_calendar("XNYS", start="2018-01-01", end="2027-12-31")
    return calendar.sessions[:count]


def _canonical_prices(
    sessions: pd.DatetimeIndex,
    tickers: tuple[str, ...] = ("SPY",),
) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for ticker_position, ticker in enumerate(tickers):
        for session_position, session in enumerate(sessions):
            price = (
                100.0
                + 5.0 * ticker_position
                + 0.03 * session_position
                + 0.7 * math.sin(session_position / (6.0 + ticker_position % 3))
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
                    "volume": 1_000_000.0 + session_position,
                    "retrieved_at": pd.Timestamp(RETRIEVED_AT),
                }
            )
    return pd.DataFrame(rows)


def _target_for_session(session: pd.Timestamp) -> evaluation_module.EvaluationTarget:
    calendar = xcals.get_calendar(
        "XNYS", start=session - timedelta(days=10), end=session + timedelta(days=10)
    )
    return resolve_evaluation_target(calendar.session_close(session))


def _configured_universe() -> tuple[ETFDefinition, ...]:
    universe = load_etf_universe()
    assert len(universe) == 24
    return universe


@pytest.fixture(scope="module")
def complete_evaluation() -> evaluation_module.SignalEvaluation:
    sessions = _xnys_sessions(505)
    return evaluate_price_signals(
        _canonical_prices(sessions),
        _configured_universe(),
        _target_for_session(sessions[-1]),
    )


@pytest.mark.parametrize(
    ("instant", "expected_target"),
    [
        ("2024-11-25T20:59:59Z", "2024-11-22"),
        ("2024-11-25T21:00:00Z", "2024-11-25"),
        ("2024-11-30T12:00:00Z", "2024-11-29"),
        ("2024-11-28T18:00:00Z", "2024-11-27"),
        ("2024-11-29T17:59:59Z", "2024-11-27"),
        ("2024-11-29T18:00:00Z", "2024-11-29"),
        ("2018-12-05T20:00:00Z", "2018-12-04"),
        ("2025-01-09T20:00:00Z", "2025-01-08"),
    ],
)
def test_target_resolution_uses_scheduled_xnys_close(
    instant: str, expected_target: str
) -> None:
    target = resolve_evaluation_target(instant)

    assert target.captured_at.tzinfo is UTC
    assert target.target_session == pd.Timestamp(expected_target)
    assert target.request_start == date(2018, 1, 1)
    assert target.request_end == target.target_session.date() + timedelta(days=1)


def test_target_resolution_requires_timezone_aware_instant() -> None:
    with pytest.raises(SignalEvaluationError, match="timezone-aware"):
        resolve_evaluation_target("2024-11-29T18:00:00")


def test_nanosecond_evaluation_instant_survives_target_and_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instant = "2024-11-29T18:00:00.123456789Z"
    target = resolve_evaluation_target(instant)
    assert target.captured_at == pd.Timestamp(instant)
    assert target.captured_at.nanosecond == 789
    assert target.target_session == pd.Timestamp("2024-11-29")
    prices = _canonical_prices(pd.DatetimeIndex([target.target_session]))
    evaluation = evaluate_price_signals(
        prices,
        (ETFDefinition("SPY", "SPY", "Test"),),
        target,
    )
    monkeypatch.setattr(evaluation_module, "_git_state", lambda root: (GIT_HEAD, False))

    result = publish_signal_evaluation_bundle(
        evaluation,
        tmp_path / "bundles",
        creation_time="2026-08-24T12:00:00.123456789Z",
        repository_root=tmp_path,
    )
    loaded = json.loads(
        (result.bundle_path / "manifest.json").read_text(encoding="utf-8")
    )

    assert result.evaluation.target.captured_at == pd.Timestamp(instant)
    assert loaded["captured_utc_reference_instant"] == instant


def test_canonical_retrieval_nanoseconds_survive_evaluation_and_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = _xnys_sessions(30)
    target = _target_for_session(sessions[-1])
    first_retrieved_at = pd.Timestamp("2024-11-29T18:01:02.123456789Z")
    last_retrieved_at = pd.Timestamp("2024-11-29T18:01:02.987654321Z")
    prices = _canonical_prices(sessions)
    prices["retrieved_at"] = pd.Series(
        [first_retrieved_at] * (len(prices) - 1) + [last_retrieved_at],
        dtype="datetime64[ns, UTC]",
    )
    original = prices.copy(deep=True)
    definition = ETFDefinition("SPY", "SPY", "Test")
    status = TickerDownloadStatus(
        ticker="SPY",
        status="success",
        rows_received=1,
        first_date=sessions[-1].date(),
        last_date=sessions[-1].date(),
        retrieved_at=last_retrieved_at,
        query_start=target.request_start,
        query_end=target.request_end,
        returned_dates=(sessions[-1].date(),),
        error=None,
    )

    evaluation = evaluate_price_signals(
        prices,
        (definition,),
        target,
        mode="refresh",
        acquisition_statuses=(status,),
    )
    pd.testing.assert_frame_equal(prices, original, check_exact=True)
    coverage = evaluation.coverage.iloc[0]
    assert coverage["input_first_retrieved_at"] == first_retrieved_at
    assert coverage["input_last_retrieved_at"] == last_retrieved_at
    assert coverage["acquisition_retrieved_at"] == last_retrieved_at
    assert evaluation.acquisition_statuses[0].retrieved_at == last_retrieved_at
    for value in (
        coverage["input_first_retrieved_at"],
        coverage["input_last_retrieved_at"],
        coverage["acquisition_retrieved_at"],
    ):
        assert isinstance(value, pd.Timestamp)
        assert str(value.tz) == "UTC"

    monkeypatch.setattr(evaluation_module, "_git_state", lambda root: (GIT_HEAD, False))
    result = publish_signal_evaluation_bundle(
        evaluation,
        tmp_path / "bundles",
        creation_time="2026-08-24T12:00:10.123456789Z",
        repository_root=tmp_path,
    )
    reloaded_input = pd.read_parquet(result.bundle_path / "input_prices.parquet")
    reloaded_coverage = pd.read_parquet(result.bundle_path / "coverage.parquet")
    manifest = json.loads(
        (result.bundle_path / "manifest.json").read_text(encoding="utf-8")
    )
    ticker_metadata = manifest["ticker_metadata"][0]

    assert reloaded_input["retrieved_at"].min() == first_retrieved_at
    assert reloaded_input["retrieved_at"].max() == last_retrieved_at
    assert reloaded_coverage.loc[0, "input_first_retrieved_at"] == first_retrieved_at
    assert reloaded_coverage.loc[0, "input_last_retrieved_at"] == last_retrieved_at
    assert reloaded_coverage.loc[0, "acquisition_retrieved_at"] == last_retrieved_at
    assert ticker_metadata["input_first_retrieved_at"] == (
        "2024-11-29T18:01:02.123456789Z"
    )
    assert ticker_metadata["input_last_retrieved_at"] == (
        "2024-11-29T18:01:02.987654321Z"
    )
    assert ticker_metadata["retrieved_at"] == "2024-11-29T18:01:02.987654321Z"
    pd.testing.assert_frame_equal(prices, original, check_exact=True)


def test_evaluation_preserves_exact_24_etf_coverage_and_eligibility_dates(
    complete_evaluation: evaluation_module.SignalEvaluation,
) -> None:
    coverage = complete_evaluation.coverage
    sessions = _xnys_sessions(505)

    assert tuple(coverage.columns) == COVERAGE_COLUMNS
    assert coverage["ticker"].tolist() == [
        definition.ticker for definition in _configured_universe()
    ]
    assert len(coverage) == 24
    spy = coverage.loc[coverage["ticker"].eq("SPY")].iloc[0]
    assert spy["first_canonical_date"] == sessions[0]
    assert spy["last_canonical_date"] == sessions[-1]
    assert spy["expected_xnys_observation_count"] == 505
    assert spy["present_xnys_observation_count"] == 505
    assert spy["missing_canonical_dates"] == ()
    assert spy["momentum_first_raw_date"] == sessions[252]
    assert spy["momentum_first_normalized_date"] == sessions[504]
    assert spy["momentum_last_raw_date"] == sessions[-1]
    assert spy["momentum_last_normalized_date"] == sessions[-1]
    assert spy["volatility_first_raw_date"] == sessions[21]
    assert spy["volatility_first_normalized_date"] == sessions[273]
    assert spy["volatility_last_raw_date"] == sessions[-1]
    assert spy["volatility_last_normalized_date"] == sessions[-1]
    assert spy["price_staleness_sessions"] == 0
    assert spy["momentum_raw_staleness_sessions"] == 0
    assert spy["momentum_normalized_staleness_sessions"] == 0
    assert spy["volatility_raw_staleness_sessions"] == 0
    assert spy["volatility_normalized_staleness_sessions"] == 0


def test_entirely_absent_etf_remains_missing_coverage_row(
    complete_evaluation: evaluation_module.SignalEvaluation,
) -> None:
    absent = complete_evaluation.coverage.loc[
        complete_evaluation.coverage["ticker"].eq("QQQ")
    ].iloc[0]

    assert absent["acquisition_status"] == "not_requested"
    assert absent["present_xnys_observation_count"] == 0
    assert absent["missing_canonical_count"] == 505
    assert not absent["target_price_row_present"]
    assert not absent["target_adjusted_close_present"]
    assert pd.isna(absent["price_staleness_sessions"])
    assert not absent["momentum_target_raw_eligible"]
    assert not absent["momentum_target_normalized_eligible"]
    assert pd.isna(absent["momentum_target_raw"])
    assert pd.isna(absent["momentum_target_percentile"])
    assert absent["momentum_target_status"] == "ticker_unavailable"
    assert absent["volatility_target_status"] == "ticker_unavailable"


def test_missing_target_row_is_stale_diagnostic_not_current_value() -> None:
    sessions = _xnys_sessions(506)
    prices = _canonical_prices(sessions)
    prices = prices.loc[prices["date"].ne(sessions[-1])].reset_index(drop=True)

    result = evaluate_price_signals(
        prices, _configured_universe(), _target_for_session(sessions[-1])
    )
    spy = result.coverage.loc[result.coverage["ticker"].eq("SPY")].iloc[0]
    momentum_target = result.momentum.loc[
        result.momentum["ticker"].eq("SPY")
        & result.momentum["signal_date"].eq(sessions[-1])
    ].iloc[0]
    volatility_target = result.volatility.loc[
        result.volatility["ticker"].eq("SPY")
        & result.volatility["signal_date"].eq(sessions[-1])
    ].iloc[0]

    assert spy["missing_canonical_dates"] == (sessions[-1],)
    assert spy["price_staleness_sessions"] == 1
    assert spy["momentum_raw_staleness_sessions"] == 1
    assert spy["momentum_normalized_staleness_sessions"] == 1
    assert spy["volatility_raw_staleness_sessions"] == 1
    assert spy["volatility_normalized_staleness_sessions"] == 1
    assert not spy["momentum_target_raw_eligible"]
    assert not spy["volatility_target_raw_eligible"]
    assert momentum_target["endpoint_status"] == "missing_end_row"
    assert volatility_target["window_status"] == "missing_price_rows"
    assert volatility_target["missing_row_dates"] == (sessions[-1],)


def test_missing_target_adjusted_close_preserves_native_diagnostics() -> None:
    sessions = _xnys_sessions(505)
    prices = _canonical_prices(sessions)
    prices.loc[prices["date"].eq(sessions[-1]), "adjusted_close"] = pd.NA

    result = evaluate_price_signals(
        prices, _configured_universe(), _target_for_session(sessions[-1])
    )
    spy = result.coverage.loc[result.coverage["ticker"].eq("SPY")].iloc[0]
    momentum_target = result.momentum.loc[
        result.momentum["signal_date"].eq(sessions[-1])
    ].iloc[0]
    volatility_target = result.volatility.loc[
        result.volatility["signal_date"].eq(sessions[-1])
    ].iloc[0]

    assert spy["missing_adjusted_close_dates"] == (sessions[-1],)
    assert spy["price_staleness_sessions"] == 1
    assert momentum_target["endpoint_status"] == "missing_end_adjusted_close"
    assert volatility_target["window_status"] == "missing_adjusted_close"
    assert volatility_target["missing_adjusted_close_dates"] == (sessions[-1],)
    assert pd.isna(spy["momentum_target_raw"])
    assert pd.isna(spy["volatility_target_raw"])


def test_orchestration_invokes_public_signal_apis_without_mutating_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = _xnys_sessions(30)
    prices = _canonical_prices(sessions)
    original = prices.copy(deep=True)
    calls = {"momentum": 0, "volatility": 0}
    original_momentum = evaluation_module.calculate_momentum
    original_volatility = evaluation_module.calculate_volatility

    def momentum_spy(*args: object, **kwargs: object) -> pd.DataFrame:
        calls["momentum"] += 1
        return original_momentum(*args, **kwargs)  # type: ignore[arg-type]

    def volatility_spy(*args: object, **kwargs: object) -> pd.DataFrame:
        calls["volatility"] += 1
        return original_volatility(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(evaluation_module, "calculate_momentum", momentum_spy)
    monkeypatch.setattr(evaluation_module, "calculate_volatility", volatility_spy)

    evaluate_price_signals(
        prices,
        (ETFDefinition("SPY", "SPY", "Test"),),
        _target_for_session(sessions[-1]),
    )

    assert calls == {"momentum": 1, "volatility": 1}
    pd.testing.assert_frame_equal(prices, original)


def _manual_percentiles(
    universe: tuple[ETFDefinition, ...],
    sessions: pd.DatetimeIndex,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    momentum_rows: list[dict[str, object]] = []
    volatility_rows: list[dict[str, object]] = []
    for ticker_position, definition in enumerate(universe):
        for session_position, session in enumerate(sessions):
            momentum_value = float(2 * ticker_position + session_position)
            volatility_value = float(3 * ticker_position + 2 * session_position)
            momentum_rows.append(
                {
                    "ticker": definition.ticker,
                    "signal_date": session,
                    "momentum_percentile": momentum_value,
                }
            )
            volatility_rows.append(
                {
                    "ticker": definition.ticker,
                    "signal_date": session,
                    "volatility_percentile": volatility_value,
                }
            )
    return pd.DataFrame(momentum_rows), pd.DataFrame(volatility_rows)


def test_dependence_uses_exact_dates_and_labels_full_universe() -> None:
    universe = _configured_universe()
    sessions = _xnys_sessions(3)
    momentum, volatility = _manual_percentiles(universe, sessions)

    result = calculate_dependence_diagnostics(momentum, volatility, universe, sessions)

    assert tuple(result.columns) == DEPENDENCE_COLUMNS
    assert set(result["scope"]) == {"per_etf", "per_session"}
    assert not any("pvalue" in column.lower() for column in result.columns)
    assert "pooled" not in set(result["scope"])
    per_etf = result.loc[result["scope"].eq("per_etf")]
    assert per_etf["ticker"].drop_duplicates().tolist() == [
        definition.ticker for definition in universe
    ]
    assert per_etf["pair_count"].eq(3).all()
    assert per_etf["status"].eq("available").all()
    per_session = result.loc[result["scope"].eq("per_session")]
    assert per_session["pair_count"].eq(24).all()
    assert per_session["universe_status"].eq("full_universe").all()
    assert per_session["included_tickers"].iloc[0] == tuple(
        definition.ticker for definition in universe
    )


def test_dependence_reports_incomplete_insufficient_and_constant_inputs() -> None:
    universe = _configured_universe()
    sessions = _xnys_sessions(3)
    momentum, volatility = _manual_percentiles(universe, sessions)
    volatility = volatility.loc[
        ~(volatility["ticker"].eq("SPY") & volatility["signal_date"].eq(sessions[0]))
    ].reset_index(drop=True)
    volatility.loc[volatility["ticker"].eq("QQQ"), "volatility_percentile"] = 7.0

    result = calculate_dependence_diagnostics(momentum, volatility, universe, sessions)
    spy = result.loc[result["scope"].eq("per_etf") & result["ticker"].eq("SPY")]
    qqq = result.loc[result["scope"].eq("per_etf") & result["ticker"].eq("QQQ")]
    first_session = result.loc[
        result["scope"].eq("per_session") & result["signal_date"].eq(sessions[0])
    ]

    assert spy["pair_count"].eq(2).all()
    assert spy["status"].eq("insufficient_pairs").all()
    assert spy["estimate"].isna().all()
    assert qqq["status"].eq("constant_input").all()
    assert qqq["estimate"].isna().all()
    assert first_session["pair_count"].eq(23).all()
    assert first_session["universe_status"].eq("incomplete_universe").all()
    assert "SPY" not in first_session["included_tickers"].iloc[0]


def test_dependence_does_not_pair_different_dates() -> None:
    universe = (ETFDefinition("SPY", "SPY", "Test"),)
    sessions = _xnys_sessions(4)
    momentum = pd.DataFrame(
        {
            "ticker": ["SPY"] * 3,
            "signal_date": sessions[:3],
            "momentum_percentile": [10.0, 20.0, 30.0],
        }
    )
    volatility = pd.DataFrame(
        {
            "ticker": ["SPY"] * 3,
            "signal_date": sessions[1:4],
            "volatility_percentile": [15.0, 25.0, 35.0],
        }
    )

    result = calculate_dependence_diagnostics(momentum, volatility, universe, sessions)

    per_etf = result.loc[result["scope"].eq("per_etf")]
    assert per_etf["pair_count"].eq(2).all()
    assert per_etf["status"].eq("insufficient_pairs").all()


def _nullable_unmasked_nan(values: list[float]) -> pd.Series:
    array = pd.array(values, dtype="Float64")
    array._data[1] = np.nan
    assert not array._mask[1]
    return pd.Series(array)


def test_dependence_rejects_nullable_unmasked_nan_before_three_pair_count() -> None:
    universe = (ETFDefinition("SPY", "SPY", "Test"),)
    sessions = _xnys_sessions(3)
    momentum = pd.DataFrame(
        {
            "ticker": ["SPY"] * 3,
            "signal_date": sessions,
            "momentum_percentile": _nullable_unmasked_nan([10.0, 20.0, 30.0]),
        }
    )
    volatility = pd.DataFrame(
        {
            "ticker": ["SPY"] * 3,
            "signal_date": sessions,
            "volatility_percentile": pd.array([15.0, 25.0, 35.0], dtype="Float64"),
        }
    )

    with pytest.raises(
        SignalEvaluationError,
        match="Momentum dependence input column 'momentum_percentile'.*non-finite",
    ):
        calculate_dependence_diagnostics(momentum, volatility, universe, sessions)


def test_dependence_rejects_unmasked_nan_before_full_universe_classification() -> None:
    universe = _configured_universe()
    session = _xnys_sessions(1)
    momentum, volatility = _manual_percentiles(universe, session)
    values = pd.array(volatility["volatility_percentile"], dtype="Float64")
    values._data[5] = np.nan
    assert not values._mask[5]
    volatility["volatility_percentile"] = values

    with pytest.raises(
        SignalEvaluationError,
        match="Volatility dependence input column 'volatility_percentile'.*non-finite",
    ):
        calculate_dependence_diagnostics(momentum, volatility, universe, session)


@pytest.mark.parametrize(
    ("component", "value", "message"),
    [
        ("momentum", math.inf, "Momentum dependence.*momentum_percentile"),
        ("momentum", -math.inf, "Momentum dependence.*momentum_percentile"),
        ("volatility", math.inf, "Volatility dependence.*volatility_percentile"),
        ("volatility", -math.inf, "Volatility dependence.*volatility_percentile"),
    ],
)
def test_dependence_rejects_present_infinity_in_either_component(
    component: str,
    value: float,
    message: str,
) -> None:
    universe = (ETFDefinition("SPY", "SPY", "Test"),)
    sessions = _xnys_sessions(3)
    momentum = pd.DataFrame(
        {
            "ticker": ["SPY"] * 3,
            "signal_date": sessions,
            "momentum_percentile": pd.array([10.0, 20.0, 30.0], dtype="Float64"),
        }
    )
    volatility = pd.DataFrame(
        {
            "ticker": ["SPY"] * 3,
            "signal_date": sessions,
            "volatility_percentile": pd.array([15.0, 25.0, 35.0], dtype="Float64"),
        }
    )
    column = (
        "momentum_percentile" if component == "momentum" else "volatility_percentile"
    )
    frame = momentum if component == "momentum" else volatility
    frame.loc[1, column] = value

    with pytest.raises(SignalEvaluationError, match=message):
        calculate_dependence_diagnostics(momentum, volatility, universe, sessions)


def test_dependence_masked_missing_uses_one_exact_finite_pair_population() -> None:
    universe = (ETFDefinition("SPY", "SPY", "Test"),)
    sessions = _xnys_sessions(4)
    momentum = pd.DataFrame(
        {
            "ticker": ["SPY"] * 4,
            "signal_date": sessions,
            "momentum_percentile": pd.array([10.0, pd.NA, 30.0, 40.0], dtype="Float64"),
        }
    )
    volatility = pd.DataFrame(
        {
            "ticker": ["SPY"] * 4,
            "signal_date": sessions,
            "volatility_percentile": pd.array(
                [15.0, 25.0, 35.0, 45.0], dtype="Float64"
            ),
        }
    )

    result = calculate_dependence_diagnostics(momentum, volatility, universe, sessions)
    per_etf = result.loc[result["scope"].eq("per_etf")]
    missing_session = result.loc[
        result["scope"].eq("per_session") & result["signal_date"].eq(sessions[1])
    ]

    assert per_etf["pair_count"].eq(3).all()
    assert per_etf["included_tickers"].map(lambda value: value == ("SPY",)).all()
    assert per_etf["status"].eq("available").all()
    assert per_etf["estimate"].notna().all()
    assert missing_session["pair_count"].eq(0).all()
    assert missing_session["included_tickers"].map(lambda value: value == ()).all()
    assert missing_session["status"].eq("insufficient_pairs").all()
    assert missing_session["universe_status"].eq("incomplete_universe").all()


def _naive_dependence_reference(
    momentum: pd.DataFrame,
    volatility: pd.DataFrame,
    universe: tuple[ETFDefinition, ...],
    sessions: pd.DatetimeIndex,
) -> pd.DataFrame:
    paired = momentum.loc[
        momentum["momentum_percentile"].notna(),
        ["ticker", "signal_date", "momentum_percentile"],
    ].merge(
        volatility.loc[
            volatility["volatility_percentile"].notna(),
            ["ticker", "signal_date", "volatility_percentile"],
        ],
        on=["ticker", "signal_date"],
        how="inner",
        validate="one_to_one",
        sort=False,
    )
    tickers = [definition.ticker for definition in universe]
    ticker_order = {ticker: position for position, ticker in enumerate(tickers)}
    rows: list[dict[str, object]] = []

    def result_for(group: pd.DataFrame, estimator: str) -> tuple[str, float | None]:
        if len(group) < 3:
            return "insufficient_pairs", None
        first = group["momentum_percentile"]
        second = group["volatility_percentile"]
        if first.nunique() < 2 or second.nunique() < 2:
            return "constant_input", None
        return "available", float(first.corr(second, method=estimator))

    for ticker in tickers:
        group = paired.loc[paired["ticker"].eq(ticker)].sort_values(
            "signal_date", kind="mergesort"
        )
        first_date = None if group.empty else group["signal_date"].iloc[0]
        last_date = None if group.empty else group["signal_date"].iloc[-1]
        for estimator in ("pearson", "spearman"):
            status, estimate = result_for(group, estimator)
            rows.append(
                {
                    "scope": "per_etf",
                    "estimator": estimator,
                    "ticker": ticker,
                    "signal_date": None,
                    "pair_count": len(group),
                    "first_signal_date": first_date,
                    "last_signal_date": last_date,
                    "included_tickers": () if group.empty else (ticker,),
                    "universe_status": "not_applicable",
                    "status": status,
                    "estimate": estimate,
                }
            )

    configured = set(tickers)
    for signal_date in sessions:
        group = paired.loc[paired["signal_date"].eq(signal_date)].copy()
        group["_ticker_order"] = group["ticker"].map(ticker_order)
        group = group.sort_values("_ticker_order", kind="mergesort")
        included = tuple(group["ticker"].astype(str))
        universe_status = (
            "full_universe"
            if len(included) == len(tickers) and set(included) == configured
            else "incomplete_universe"
        )
        for estimator in ("pearson", "spearman"):
            status, estimate = result_for(group, estimator)
            rows.append(
                {
                    "scope": "per_session",
                    "estimator": estimator,
                    "ticker": None,
                    "signal_date": signal_date,
                    "pair_count": len(group),
                    "first_signal_date": signal_date if len(group) else None,
                    "last_signal_date": signal_date if len(group) else None,
                    "included_tickers": included,
                    "universe_status": universe_status,
                    "status": status,
                    "estimate": estimate,
                }
            )
    return pd.DataFrame(rows, columns=DEPENDENCE_COLUMNS)


def test_grouped_dependence_matches_naive_reference_for_interleaved_edge_cases() -> (
    None
):
    universe = _configured_universe()
    sessions = _xnys_sessions(5)
    momentum, volatility = _manual_percentiles(universe, sessions)
    volatility = volatility.loc[
        ~(volatility["ticker"].eq("SPY") & volatility["signal_date"].eq(sessions[1]))
    ].reset_index(drop=True)
    momentum.loc[
        momentum["ticker"].eq("QQQ") & momentum["signal_date"].eq(sessions[2]),
        "momentum_percentile",
    ] = pd.NA
    momentum.loc[momentum["signal_date"].eq(sessions[3]), "momentum_percentile"] = 50.0
    volatility.loc[
        volatility["signal_date"].eq(sessions[0]), "volatility_percentile"
    ] = volatility.loc[
        volatility["signal_date"].eq(sessions[0]), "volatility_percentile"
    ].round(-1)
    momentum = momentum.sample(frac=1.0, random_state=17).reset_index(drop=True)
    volatility = volatility.sample(frac=1.0, random_state=29).reset_index(drop=True)

    expected = _naive_dependence_reference(momentum, volatility, universe, sessions)
    actual = calculate_dependence_diagnostics(momentum, volatility, universe, sessions)

    pd.testing.assert_frame_equal(actual, expected, check_exact=True)


def _status(
    ticker: str,
    status: str,
    target: evaluation_module.EvaluationTarget,
    returned_dates: tuple[date, ...] = (),
    *,
    error: str | None = None,
) -> TickerDownloadStatus:
    rows_received = len(returned_dates)
    retrieved_at = None if status == "failed" else pd.Timestamp(RETRIEVED_AT)
    return TickerDownloadStatus(
        ticker=ticker,
        status=cast(DownloadStatus, status),
        rows_received=rows_received,
        first_date=returned_dates[0] if returned_dates else None,
        last_date=returned_dates[-1] if returned_dates else None,
        retrieved_at=retrieved_at,
        query_start=target.request_start,
        query_end=target.request_end,
        returned_dates=returned_dates,
        error=error,
    )


@pytest.fixture(scope="module")
def refresh_evaluation() -> evaluation_module.SignalEvaluation:
    sessions = _xnys_sessions(30)
    target = _target_for_session(sessions[-1])
    universe = _configured_universe()
    statuses = tuple(
        _status(
            definition.ticker,
            (
                "success"
                if definition.ticker == "SPY"
                else "empty"
                if definition.ticker == "QQQ"
                else "failed"
            ),
            target,
            (
                tuple(session.date() for session in sessions)
                if definition.ticker == "SPY"
                else ()
            ),
            error=(
                "synthetic failure" if definition.ticker not in {"SPY", "QQQ"} else None
            ),
        )
        for definition in universe
    )
    return evaluate_price_signals(
        _canonical_prices(sessions),
        universe,
        target,
        mode="refresh",
        acquisition_statuses=statuses,
    )


def test_refresh_statuses_preserve_success_empty_failed_and_absent_etfs() -> None:
    sessions = _xnys_sessions(30)
    target = _target_for_session(sessions[-1])
    universe = _configured_universe()
    statuses = []
    for definition in universe:
        if definition.ticker == "SPY":
            statuses.append(
                _status(
                    definition.ticker,
                    "success",
                    target,
                    tuple(session.date() for session in sessions),
                )
            )
        elif definition.ticker == "QQQ":
            statuses.append(_status(definition.ticker, "empty", target))
        else:
            statuses.append(
                _status(
                    definition.ticker,
                    "failed",
                    target,
                    error="synthetic failure",
                )
            )

    result = evaluate_price_signals(
        _canonical_prices(sessions),
        universe,
        target,
        mode="refresh",
        acquisition_statuses=statuses,
    )

    assert len(result.coverage) == 24
    assert result.coverage.set_index("ticker").loc["SPY", "acquisition_status"] == (
        "success"
    )
    assert result.coverage.set_index("ticker").loc["QQQ", "acquisition_status"] == (
        "empty"
    )
    assert result.coverage.set_index("ticker").loc["IWM", "acquisition_status"] == (
        "failed"
    )
    assert (
        result.coverage.set_index("ticker").loc["IWM", "present_xnys_observation_count"]
        == 0
    )


def test_complete_mixed_acquisition_population_and_offline_absence_are_valid(
    refresh_evaluation: evaluation_module.SignalEvaluation,
    complete_evaluation: evaluation_module.SignalEvaluation,
) -> None:
    validate_signal_evaluation(refresh_evaluation)
    validate_signal_evaluation(complete_evaluation)

    statuses = refresh_evaluation.acquisition_statuses
    assert len(statuses) == 24
    assert {status.status for status in statuses} == {"success", "empty", "failed"}
    assert complete_evaluation.mode == "offline"
    assert complete_evaluation.acquisition_statuses == ()


def _malformed_acquisition_statuses(
    evaluation: evaluation_module.SignalEvaluation,
    case: str,
) -> tuple[TickerDownloadStatus, ...]:
    statuses = list(evaluation.acquisition_statuses)
    success_position = next(
        position
        for position, status in enumerate(statuses)
        if status.status == "success"
    )
    empty_position = next(
        position for position, status in enumerate(statuses) if status.status == "empty"
    )
    failed_position = next(
        position
        for position, status in enumerate(statuses)
        if status.status == "failed"
    )
    success = statuses[success_position]
    empty = statuses[empty_position]
    failed = statuses[failed_position]

    if case == "success_zero_rows":
        statuses[success_position] = replace(success, rows_received=0)
    elif case == "unsupported_complete_status":
        statuses[success_position] = replace(
            success, status=cast(DownloadStatus, "complete")
        )
    elif case == "non_string_status":
        statuses[success_position] = replace(success, status=cast(DownloadStatus, 1))
    elif case == "empty_query_window":
        statuses[success_position] = replace(success, query_end=success.query_start)
    elif case == "reversed_query_window":
        statuses[success_position] = replace(success, query_start=success.query_end)
    elif case == "wrong_request_start":
        statuses[success_position] = replace(
            success, query_start=success.query_start + timedelta(days=1)
        )
    elif case == "wrong_exclusive_end":
        statuses[success_position] = replace(
            success, query_end=success.query_end + timedelta(days=1)
        )
    elif case == "mixed_query_bounds":
        statuses[failed_position] = replace(
            failed, query_start=failed.query_start + timedelta(days=1)
        )
    elif case == "failed_five_rows":
        statuses[failed_position] = replace(failed, rows_received=5)
    elif case == "failed_retrieval_timestamp":
        statuses[failed_position] = replace(failed, retrieved_at=RETRIEVED_AT)
    elif case == "failed_missing_error":
        statuses[failed_position] = replace(failed, error=None)
    elif case == "failed_whitespace_error":
        statuses[failed_position] = replace(failed, error=" failure ")
    elif case == "success_count_mismatch":
        statuses[success_position] = replace(
            success, rows_received=success.rows_received + 1
        )
    elif case == "success_first_date_mismatch":
        statuses[success_position] = replace(
            success, first_date=success.returned_dates[1]
        )
    elif case == "success_last_date_mismatch":
        statuses[success_position] = replace(
            success, last_date=success.returned_dates[-2]
        )
    elif case == "success_returned_date_mismatch":
        returned_dates = (success.query_start,) + success.returned_dates[1:]
        statuses[success_position] = replace(
            success,
            returned_dates=returned_dates,
            first_date=returned_dates[0],
            last_date=returned_dates[-1],
        )
    elif case == "success_retrieval_timestamp_mismatch":
        statuses[success_position] = replace(
            success,
            retrieved_at=pd.Timestamp(RETRIEVED_AT) + pd.Timedelta(nanoseconds=1),
        )
    elif case == "empty_with_rows":
        statuses[empty_position] = replace(empty, rows_received=1)
    elif case == "empty_with_dates":
        statuses[empty_position] = replace(
            empty,
            rows_received=1,
            first_date=empty.query_start,
            last_date=empty.query_start,
            returned_dates=(empty.query_start,),
        )
    elif case == "failed_with_dates":
        statuses[failed_position] = replace(
            failed,
            rows_received=1,
            first_date=failed.query_start,
            last_date=failed.query_start,
            returned_dates=(failed.query_start,),
        )
    elif case == "duplicate_ticker":
        statuses[failed_position] = replace(failed, ticker=success.ticker)
    elif case == "missing_ticker":
        statuses.pop()
    elif case == "extra_ticker":
        statuses.append(_status("EXTRA", "empty", evaluation.target))
    elif case == "casefold_duplicate_ticker":
        statuses[failed_position] = replace(failed, ticker=success.ticker.casefold())
    elif case == "whitespace_ticker":
        statuses[failed_position] = replace(failed, ticker=f" {failed.ticker}")
    elif case == "non_string_ticker":
        statuses[failed_position] = replace(failed, ticker=cast(str, 7))
    elif case == "boolean_rows_received":
        statuses[success_position] = replace(success, rows_received=cast(int, True))
    elif case == "floating_rows_received":
        statuses[success_position] = replace(success, rows_received=cast(int, 1.0))
    elif case == "datetime_query_start":
        statuses[success_position] = replace(
            success,
            query_start=cast(
                date,
                datetime.combine(success.query_start, datetime.min.time()),
            ),
        )
    elif case == "list_returned_dates":
        statuses[success_position] = replace(
            success,
            returned_dates=cast(tuple[date, ...], list(success.returned_dates)),
        )
    elif case == "tuple_subclass_returned_dates":
        statuses[success_position] = replace(
            success,
            returned_dates=cast(tuple[date, ...], _DateTuple(success.returned_dates)),
        )
    elif case == "non_string_error":
        statuses[failed_position] = replace(failed, error=cast(str | None, 5))
    else:
        raise AssertionError(f"Unhandled acquisition mutation: {case}")
    return tuple(statuses)


@pytest.mark.parametrize(
    "case",
    [
        "success_zero_rows",
        "unsupported_complete_status",
        "non_string_status",
        "empty_query_window",
        "reversed_query_window",
        "wrong_request_start",
        "wrong_exclusive_end",
        "mixed_query_bounds",
        "failed_five_rows",
        "failed_retrieval_timestamp",
        "failed_missing_error",
        "failed_whitespace_error",
        "success_count_mismatch",
        "success_first_date_mismatch",
        "success_last_date_mismatch",
        "success_returned_date_mismatch",
        "success_retrieval_timestamp_mismatch",
        "empty_with_rows",
        "empty_with_dates",
        "failed_with_dates",
        "duplicate_ticker",
        "missing_ticker",
        "extra_ticker",
        "casefold_duplicate_ticker",
        "whitespace_ticker",
        "non_string_ticker",
        "boolean_rows_received",
        "floating_rows_received",
        "datetime_query_start",
        "list_returned_dates",
        "tuple_subclass_returned_dates",
        "non_string_error",
    ],
)
def test_acquisition_metadata_is_rejected_unchanged_at_validation_and_publication(
    case: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    refresh_evaluation: evaluation_module.SignalEvaluation,
) -> None:
    malformed_statuses = _malformed_acquisition_statuses(refresh_evaluation, case)
    malformed = replace(refresh_evaluation, acquisition_statuses=malformed_statuses)
    frame_snapshots = {
        name: getattr(malformed, name).copy(deep=True)
        for name in (
            "input_prices",
            "coverage",
            "momentum",
            "volatility",
            "dependence",
        )
    }
    status_snapshot = malformed.acquisition_statuses
    monkeypatch.setattr(evaluation_module, "_git_state", lambda root: (GIT_HEAD, False))
    output_root = tmp_path / "bundles"

    with pytest.raises(SignalEvaluationError, match="Acquisition provenance"):
        validate_signal_evaluation(malformed)
    with pytest.raises(SignalEvaluationError, match="Acquisition provenance"):
        publish_signal_evaluation_bundle(
            malformed,
            output_root,
            creation_time="2026-08-24T12:00:09Z",
            repository_root=tmp_path,
        )

    assert malformed.acquisition_statuses == status_snapshot
    assert not output_root.exists()
    assert list(tmp_path.rglob("manifest.json")) == []
    for name, snapshot in frame_snapshots.items():
        pd.testing.assert_frame_equal(
            getattr(malformed, name), snapshot, check_exact=True
        )


def test_offline_missing_canonical_file_fails_before_bundle_creation(
    tmp_path: Path,
) -> None:
    output_root = tmp_path / "bundles"

    with pytest.raises(SignalEvaluationError, match="does not exist"):
        run_signal_evaluation(
            evaluation_instant="2024-11-29T18:00:00Z",
            price_path=tmp_path / "missing.parquet",
            output_root=output_root,
        )

    assert not output_root.exists()


def test_refresh_translates_shared_status_validation_error_with_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    refresh_evaluation: evaluation_module.SignalEvaluation,
) -> None:
    malformed_statuses = _malformed_acquisition_statuses(
        refresh_evaluation, "success_zero_rows"
    )
    input_prices = refresh_evaluation.input_prices.copy(deep=True)
    input_snapshot = input_prices.copy(deep=True)
    monkeypatch.setattr(
        evaluation_module,
        "download_price_history",
        lambda **kwargs: PriceDownloadResult(
            prices=input_prices,
            statuses=malformed_statuses,
            retrieved_at=pd.Timestamp(RETRIEVED_AT),
        ),
    )
    price_path = tmp_path / "prices.parquet"
    output_root = tmp_path / "bundles"

    with pytest.raises(SignalEvaluationError, match="Acquisition provenance") as error:
        run_signal_evaluation(
            refresh=True,
            evaluation_instant=refresh_evaluation.target.captured_at,
            price_path=price_path,
            output_root=output_root,
            universe=refresh_evaluation.universe,
        )

    assert isinstance(error.value.__cause__, PriceDataValidationError)
    pd.testing.assert_frame_equal(input_prices, input_snapshot, check_exact=True)
    assert not price_path.exists()
    assert not output_root.exists()


def test_refresh_rejects_inconsistent_aggregate_retrieval_timestamp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    refresh_evaluation: evaluation_module.SignalEvaluation,
) -> None:
    input_prices = refresh_evaluation.input_prices.copy(deep=True)
    input_snapshot = input_prices.copy(deep=True)
    monkeypatch.setattr(
        evaluation_module,
        "download_price_history",
        lambda **kwargs: PriceDownloadResult(
            prices=input_prices,
            statuses=refresh_evaluation.acquisition_statuses,
            retrieved_at=(pd.Timestamp(RETRIEVED_AT) + pd.Timedelta(nanoseconds=1)),
        ),
    )
    price_path = tmp_path / "prices.parquet"
    output_root = tmp_path / "bundles"

    with pytest.raises(SignalEvaluationError, match="aggregate retrieved_at"):
        run_signal_evaluation(
            refresh=True,
            evaluation_instant=refresh_evaluation.target.captured_at,
            price_path=price_path,
            output_root=output_root,
            universe=refresh_evaluation.universe,
        )

    pd.testing.assert_frame_equal(input_prices, input_snapshot, check_exact=True)
    assert not price_path.exists()
    assert not output_root.exists()


def test_offline_mode_never_calls_price_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = _xnys_sessions(30)
    price_path = tmp_path / "prices.parquet"
    _canonical_prices(sessions).to_parquet(price_path, index=False)
    monkeypatch.setattr(
        evaluation_module,
        "download_price_history",
        lambda **kwargs: pytest.fail(f"offline provider call: {kwargs}"),
    )
    monkeypatch.setattr(evaluation_module, "_git_state", lambda root: (GIT_HEAD, False))

    result = run_signal_evaluation(
        evaluation_instant=_target_for_session(sessions[-1]).captured_at,
        price_path=price_path,
        output_root=tmp_path / "bundles",
        creation_time="2026-08-24T12:00:00Z",
        repository_root=tmp_path,
        downloader=lambda ticker, start, end: pytest.fail("downloader called"),
        universe=_configured_universe(),
    )

    assert result.evaluation.mode == "offline"
    assert len(result.evaluation.coverage) == 24
    assert result.bundle_path.is_dir()


def test_refresh_uses_explicit_bounds_and_persists_valid_partial_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = _xnys_sessions(30)
    target = _target_for_session(sessions[-1])
    prices = _canonical_prices(sessions)
    universe = _configured_universe()
    statuses = tuple(
        _status(
            definition.ticker,
            "success" if definition.ticker == "SPY" else "empty",
            target,
            (
                tuple(session.date() for session in sessions)
                if definition.ticker == "SPY"
                else ()
            ),
        )
        for definition in universe
    )
    captured: dict[str, object] = {}

    def fake_download(**kwargs: object) -> PriceDownloadResult:
        captured.update(kwargs)
        return PriceDownloadResult(
            prices=prices,
            statuses=statuses,
            retrieved_at=pd.Timestamp(RETRIEVED_AT),
        )

    monkeypatch.setattr(evaluation_module, "download_price_history", fake_download)
    monkeypatch.setattr(evaluation_module, "_git_state", lambda root: (GIT_HEAD, True))
    price_path = tmp_path / "prices.parquet"
    result = run_signal_evaluation(
        refresh=True,
        evaluation_instant=target.captured_at,
        price_path=price_path,
        output_root=tmp_path / "bundles",
        command_arguments=("--refresh",),
        universe=universe,
        snapshot_dir=tmp_path / "snapshots",
        creation_time="2026-08-24T12:00:01Z",
        repository_root=tmp_path,
    )

    assert captured["tickers"] == tuple(definition.ticker for definition in universe)
    assert captured["start"] == date(2018, 1, 1)
    assert captured["end"] == sessions[-1].date() + timedelta(days=1)
    assert captured["downloader"] is None
    assert price_path.is_file()
    assert result.evaluation.mode == "refresh"
    assert len(result.evaluation.coverage) == 24
    assert result.manifest["worktree_dirty"] is True
    assert result.manifest["command_arguments"] == ["--refresh"]


def test_all_unusable_refresh_writes_neither_canonical_nor_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = _xnys_sessions(2)
    target = _target_for_session(sessions[-1])
    empty_prices = _canonical_prices(sessions).iloc[0:0]
    statuses = tuple(
        _status(
            definition.ticker,
            "failed",
            target,
            error="synthetic failure",
        )
        for definition in _configured_universe()
    )
    monkeypatch.setattr(
        evaluation_module,
        "download_price_history",
        lambda **kwargs: PriceDownloadResult(empty_prices, statuses, None),
    )
    price_path = tmp_path / "prices.parquet"
    output_root = tmp_path / "bundles"

    with pytest.raises(SignalEvaluationError, match="no usable"):
        run_signal_evaluation(
            refresh=True,
            evaluation_instant=target.captured_at,
            price_path=price_path,
            output_root=output_root,
            universe=_configured_universe(),
        )

    assert not price_path.exists()
    assert not output_root.exists()


def test_cli_defaults_offline_and_requires_refresh_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[dict[str, object]] = []
    target = resolve_evaluation_target("2024-11-29T18:00:00Z")

    def fake_run(**kwargs: object) -> SimpleNamespace:
        captured.append(kwargs)
        return SimpleNamespace(
            evaluation=SimpleNamespace(
                mode="refresh" if kwargs["refresh"] else "offline",
                target=target,
            ),
            bundle_path=Path("synthetic-bundle"),
        )

    monkeypatch.setattr(evaluation_script, "run_signal_evaluation", fake_run)

    assert evaluation_script.main([]) == 0
    assert (
        evaluation_script.main(
            ["--refresh", "--evaluation-instant", "2024-11-29T18:00:00Z"]
        )
        == 0
    )
    assert captured[0]["refresh"] is False
    assert captured[1]["refresh"] is True
    assert captured[1]["command_arguments"] == [
        "--refresh",
        "--evaluation-instant",
        "2024-11-29T18:00:00Z",
    ]


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _mutate_evaluation(
    evaluation: evaluation_module.SignalEvaluation,
    case: str,
) -> evaluation_module.SignalEvaluation:
    input_prices = evaluation.input_prices.copy(deep=True)
    coverage = evaluation.coverage.copy(deep=True)
    momentum = evaluation.momentum.copy(deep=True)
    volatility = evaluation.volatility.copy(deep=True)
    dependence = evaluation.dependence.copy(deep=True)

    if case == "duplicate_momentum_key":
        momentum = pd.concat([momentum, momentum.iloc[[0]]], ignore_index=True)
        momentum = momentum.sort_values(
            ["ticker", "signal_date"], kind="mergesort"
        ).reset_index(drop=True)
    elif case == "volatility_infinity":
        position = volatility["raw_annualized_volatility"].first_valid_index()
        assert position is not None
        volatility.loc[position, "raw_annualized_volatility"] = math.inf
    elif case == "coverage_infinity":
        position = coverage["momentum_target_raw"].first_valid_index()
        assert position is not None
        coverage.loc[position, "momentum_target_raw"] = math.inf
    elif case == "dependence_infinity":
        dependence.loc[0, "estimate"] = math.inf
    elif case == "nullable_unmasked_nan":
        position = momentum["momentum_percentile"].first_valid_index()
        assert position is not None
        array_position = momentum.index.get_loc(position)
        momentum["momentum_percentile"].array._data[array_position] = np.nan
        assert not momentum["momentum_percentile"].array._mask[array_position]
    elif case == "contradictory_target_price_flags":
        position = coverage["ticker"].eq("SPY").idxmax()
        coverage.loc[position, "target_adjusted_close_present"] = False
    elif case == "incorrect_included_tickers":
        position = dependence[
            dependence["scope"].eq("per_etf") & dependence["ticker"].eq("SPY")
        ].index[0]
        dependence.at[position, "included_tickers"] = ("QQQ",)
    elif case == "incorrect_pair_count":
        dependence.loc[0, "pair_count"] += 1
    elif case == "false_full_universe":
        position = dependence[dependence["scope"].eq("per_session")].index[0]
        dependence.loc[position, "universe_status"] = "full_universe"
    elif case == "invalid_coverage_date":
        position = coverage["ticker"].eq("SPY").idxmax()
        coverage.loc[position, "first_canonical_date"] = (
            evaluation.target.target_session + timedelta(days=1)
        )
    elif case == "invalid_coverage_count":
        coverage.loc[0, "missing_canonical_count"] = -1
    elif case == "signal_after_target":
        momentum.loc[momentum.index[-1], "signal_date"] = (
            evaluation.target.target_session + timedelta(days=1)
        )
    elif case == "completely_empty_artifacts":
        input_prices = input_prices.iloc[0:0].copy()
        coverage = coverage.iloc[0:0].copy()
        momentum = momentum.iloc[0:0].copy()
        volatility = volatility.iloc[0:0].copy()
        dependence = dependence.iloc[0:0].copy()
    else:
        raise AssertionError(f"Unhandled mutation case: {case}")

    return replace(
        evaluation,
        input_prices=input_prices,
        coverage=coverage,
        momentum=momentum,
        volatility=volatility,
        dependence=dependence,
    )


@pytest.mark.parametrize(
    "case",
    [
        "duplicate_momentum_key",
        "volatility_infinity",
        "coverage_infinity",
        "dependence_infinity",
        "nullable_unmasked_nan",
        "contradictory_target_price_flags",
        "incorrect_included_tickers",
        "incorrect_pair_count",
        "false_full_universe",
        "invalid_coverage_date",
        "invalid_coverage_count",
        "signal_after_target",
        "completely_empty_artifacts",
    ],
)
def test_publication_rejects_unchanged_malformed_evaluation(
    case: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    complete_evaluation: evaluation_module.SignalEvaluation,
) -> None:
    malformed = _mutate_evaluation(complete_evaluation, case)
    snapshots = {
        name: getattr(malformed, name).copy(deep=True)
        for name in (
            "input_prices",
            "coverage",
            "momentum",
            "volatility",
            "dependence",
        )
    }
    monkeypatch.setattr(evaluation_module, "_git_state", lambda root: (GIT_HEAD, False))
    output_root = tmp_path / "bundles"

    with pytest.raises(SignalEvaluationError):
        publish_signal_evaluation_bundle(
            malformed,
            output_root,
            creation_time="2026-08-24T12:00:06Z",
            repository_root=tmp_path,
        )

    assert not output_root.exists()
    for name, snapshot in snapshots.items():
        pd.testing.assert_frame_equal(
            getattr(malformed, name), snapshot, check_exact=True
        )


def test_bundle_has_explicit_round_tripped_schemas_and_complete_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    complete_evaluation: evaluation_module.SignalEvaluation,
) -> None:
    monkeypatch.setattr(evaluation_module, "_git_state", lambda root: (GIT_HEAD, True))
    result = publish_signal_evaluation_bundle(
        complete_evaluation,
        tmp_path / "bundles",
        command_arguments=("--prices", "synthetic.parquet"),
        creation_time="2026-08-24T12:00:02.123456Z",
        repository_root=tmp_path,
    )

    assert result.bundle_path.name == "20260824T120002123456Z"
    assert {path.name for path in result.bundle_path.iterdir()} == {
        "input_prices.parquet",
        "coverage.parquet",
        "momentum.parquet",
        "volatility.parquet",
        "dependence.parquet",
        "manifest.json",
    }
    manifest = json.loads(
        (result.bundle_path / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["git_head"] == GIT_HEAD
    assert manifest["worktree_dirty"] is True
    assert manifest["command_arguments"] == ["--prices", "synthetic.parquet"]
    assert manifest["mode"] == "offline"
    assert manifest["captured_utc_reference_instant"].endswith("Z")
    assert manifest["target_xnys_session"] == "2020-01-03"
    assert manifest["request_start"] == "2018-01-01"
    assert manifest["request_end_exclusive"] == "2020-01-04"
    assert manifest["package_versions"]["scipy"]
    assert len(manifest["ticker_metadata"]) == 24
    assert "manifest" not in manifest["artifacts"]
    assert "manifest.json" not in manifest["artifacts"]

    for metadata in manifest["artifacts"].values():
        artifact_path = result.bundle_path / metadata["filename"]
        table = pq.read_table(artifact_path)
        assert table.num_rows == metadata["row_count"]
        assert _file_sha256(artifact_path) == metadata["sha256"]
        assert [field["name"] for field in metadata["schema"]] == table.column_names
    assert manifest["input_sha256"] == manifest["artifacts"]["input_prices"]["sha256"]
    assert manifest["input_row_count"] == len(complete_evaluation.input_prices)

    momentum_schema = pq.read_schema(result.bundle_path / "momentum.parquet")
    volatility_schema = pq.read_schema(result.bundle_path / "volatility.parquet")
    coverage_schema = pq.read_schema(result.bundle_path / "coverage.parquet")
    assert pa.types.is_list(momentum_schema.field("interior_missing_row_dates").type)
    assert pa.types.is_timestamp(
        momentum_schema.field("interior_missing_row_dates").type.value_type
    )
    assert pa.types.is_list(volatility_schema.field("missing_row_dates").type)
    assert pa.types.is_timestamp(
        volatility_schema.field("missing_row_dates").type.value_type
    )
    assert pa.types.is_list(coverage_schema.field("missing_canonical_dates").type)

    coverage_table = pq.read_table(result.bundle_path / "coverage.parquet")
    qqq_row = coverage_table.column("ticker").to_pylist().index("QQQ")
    assert coverage_table.column("momentum_target_raw")[qqq_row].as_py() is None
    assert (
        coverage_table.column("volatility_target_percentile")[qqq_row].as_py() is None
    )


def test_bundle_preserves_large_nullable_integer_values_exactly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = _xnys_sessions(2)
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
                [pd.Timestamp(RETRIEVED_AT)] * 2,
                dtype="datetime64[ns, UTC]",
            ),
        }
    )
    evaluation = evaluate_price_signals(
        prices,
        (ETFDefinition("SPY", "SPY", "Test"),),
        _target_for_session(sessions[-1]),
    )
    monkeypatch.setattr(evaluation_module, "_git_state", lambda root: (GIT_HEAD, False))

    result = publish_signal_evaluation_bundle(
        evaluation,
        tmp_path / "bundles",
        creation_time="2026-08-24T12:00:03Z",
        repository_root=tmp_path,
    )
    input_table = pq.read_table(result.bundle_path / "input_prices.parquet")

    assert input_table.schema.field("adjusted_close").type == pa.uint64()
    assert input_table.column("adjusted_close").to_pylist() == [large, large + 1]
    assert input_table.column("volume").to_pylist() == [large, None]


def test_bundle_run_directory_never_overwrites(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    complete_evaluation: evaluation_module.SignalEvaluation,
) -> None:
    monkeypatch.setattr(evaluation_module, "_git_state", lambda root: (GIT_HEAD, False))
    creation_time = "2026-08-24T12:00:04Z"
    publish_signal_evaluation_bundle(
        complete_evaluation,
        tmp_path / "bundles",
        creation_time=creation_time,
        repository_root=tmp_path,
    )

    with pytest.raises(FileExistsError, match="already exists"):
        publish_signal_evaluation_bundle(
            complete_evaluation,
            tmp_path / "bundles",
            creation_time=creation_time,
            repository_root=tmp_path,
        )


def test_git_provenance_is_captured_before_unignored_output_creation(
    tmp_path: Path,
    complete_evaluation: evaluation_module.SignalEvaluation,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(
        ["git", "init", "--initial-branch=main"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Synthetic Test"],
        cwd=repository,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "synthetic@example.invalid"],
        cwd=repository,
        check=True,
    )
    (repository / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=repository, check=True)
    subprocess.run(
        ["git", "commit", "-m", "synthetic baseline"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    expected_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert not subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    result = publish_signal_evaluation_bundle(
        complete_evaluation,
        repository / "unignored-output" / "bundles",
        creation_time="2026-08-24T12:00:07Z",
        repository_root=repository,
    )

    assert result.manifest["git_head"] == expected_head
    assert result.manifest["worktree_dirty"] is False
    assert subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_final_path_race_is_detected_and_quarantined(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    complete_evaluation: evaluation_module.SignalEvaluation,
) -> None:
    monkeypatch.setattr(evaluation_module, "_git_state", lambda root: (GIT_HEAD, False))
    original_validation = evaluation_module._validate_bundle_at_path

    def mutate_then_validate(
        bundle_path: Path,
        expected_manifest: object,
        evaluation: evaluation_module.SignalEvaluation,
        expected_tables: object,
    ) -> dict[str, object]:
        with (bundle_path / "input_prices.parquet").open("ab") as artifact:
            artifact.write(b"synthetic-race")
        return original_validation(
            bundle_path,
            expected_manifest,
            evaluation,
            expected_tables,
        )

    monkeypatch.setattr(
        evaluation_module, "_validate_bundle_at_path", mutate_then_validate
    )
    output_root = tmp_path / "bundles"
    run_id = "20260824T120008000000Z"

    with pytest.raises(SignalEvaluationError, match="quarantined") as error:
        publish_signal_evaluation_bundle(
            complete_evaluation,
            output_root,
            creation_time="2026-08-24T12:00:08Z",
            repository_root=tmp_path,
        )

    assert error.value.__cause__ is not None
    assert not (output_root / run_id).exists()
    quarantined = list(output_root.glob(f"{run_id}.invalid-*"))
    assert len(quarantined) == 1
    assert (quarantined[0] / "manifest.json").is_file()


def test_bundle_failure_leaves_no_final_or_temporary_run_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    complete_evaluation: evaluation_module.SignalEvaluation,
) -> None:
    output_root = tmp_path / "bundles"

    def fail_write(
        table: pa.Table,
        path: Path,
        **kwargs: object,
    ) -> None:
        del table, path, kwargs
        raise OSError("synthetic artifact failure")

    monkeypatch.setattr(evaluation_module, "_write_verified_table", fail_write)
    monkeypatch.setattr(evaluation_module, "_git_state", lambda root: (GIT_HEAD, False))

    with pytest.raises(OSError, match="synthetic artifact failure"):
        publish_signal_evaluation_bundle(
            complete_evaluation,
            output_root,
            creation_time="2026-08-24T12:00:05Z",
            repository_root=tmp_path,
        )

    assert output_root.is_dir()
    assert list(output_root.iterdir()) == []


def test_output_contracts_contain_no_forbidden_component_or_score_fields(
    complete_evaluation: evaluation_module.SignalEvaluation,
) -> None:
    forbidden_terms = (
        "flow",
        "concentration",
        "composite",
        "weight",
        "risk_class",
        "reweight",
        "crowding",
        "pvalue",
    )
    output_columns = list(complete_evaluation.coverage.columns) + list(
        complete_evaluation.dependence.columns
    )

    assert not any(
        term in column.lower() for term in forbidden_terms for column in output_columns
    )
    assert set(complete_evaluation.dependence["scope"]) == {
        "per_etf",
        "per_session",
    }
