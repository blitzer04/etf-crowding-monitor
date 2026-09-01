from __future__ import annotations

import ast
import hashlib
import json
import math
import re
import shutil
import tomllib
from datetime import UTC, datetime, timedelta
from functools import cache
from pathlib import Path
from typing import cast

import exchange_calendars as xcals  # type: ignore[import-untyped]
import pandas as pd
import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

import etf_crowding.analysis.signal_evaluation as evaluation_module
from app.signal_dashboard import (
    _CHART_CONFIG,
    _DEPENDENCE_DATE_DURABLE_KEY_PREFIX,
    _DEPENDENCE_DATE_WIDGET_KEY,
    _ETF_DETAIL_TICKER_DURABLE_KEY_PREFIX,
    _ETF_DETAIL_TICKER_WIDGET_KEY,
    CHART_BACKGROUND_COLOR,
    HEADING_ANCHORS,
    NEUTRAL_LINE_COLOR,
    VIEW_NAMES,
    _bundle_scoped_state_key,
    _contrast_ratio,
    _dependence_display,
    build_adjusted_price_figure,
    build_dependence_figure,
    build_overview_tables,
    build_staleness_table,
)
from app.streamlit_app import _FRAGMENT_CLEAR_SCRIPT_TEMPLATE
from etf_crowding.analysis import (
    VerifiedSignalEvaluationBundle,
    evaluate_price_signals,
    load_signal_evaluation_bundle,
    publish_signal_evaluation_bundle,
    resolve_evaluation_target,
)
from etf_crowding.analysis.signal_bundle import ArtifactName
from etf_crowding.config import load_etf_universe

RETRIEVED_AT = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
GIT_HEAD = "b" * 40
RUN_ID = "20260824T120002123456Z"
SECOND_RUN_ID = "20260825T120003123456Z"
APP_PATH = Path(__file__).parents[1] / "app" / "streamlit_app.py"


@cache
def _sessions(count: int) -> pd.DatetimeIndex:
    calendar = xcals.get_calendar("XNYS", start="2018-01-01", end="2027-12-31")
    return calendar.sessions[:count]


def _prices() -> pd.DataFrame:
    sessions = _sessions(600)
    rows: list[dict[str, object]] = []
    for ticker_position, ticker in enumerate(("SPY", "QQQ", "SOXX")):
        ticker_sessions = sessions if ticker == "SPY" else sessions[:-1]
        for position, session in enumerate(ticker_sessions):
            price = (
                100.0
                + 8.0 * ticker_position
                + (0.02 + ticker_position / 1000) * position
                + (0.5 + ticker_position / 10)
                * math.sin(position / (6.0 + ticker_position))
            )
            rows.append(
                {
                    "date": session,
                    "ticker": ticker,
                    "open": price,
                    "high": price + 1,
                    "low": price - 1,
                    "close": price,
                    "adjusted_close": price,
                    "volume": 2_000_000 + position,
                    "retrieved_at": pd.Timestamp(RETRIEVED_AT),
                }
            )
    return pd.DataFrame(rows)


def _target(session: pd.Timestamp) -> evaluation_module.EvaluationTarget:
    calendar = xcals.get_calendar(
        "XNYS", start=session - timedelta(days=10), end=session + timedelta(days=10)
    )
    return resolve_evaluation_target(calendar.session_close(session))


@pytest.fixture(scope="module")
def source_project(tmp_path_factory: pytest.TempPathFactory) -> Path:
    project = tmp_path_factory.mktemp("streamlit-project")
    output_root = project / "data" / "processed" / "signal_evaluations"
    prices = _prices()
    evaluation = evaluate_price_signals(
        prices,
        load_etf_universe(),
        _target(_sessions(600)[-1]),
    )
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(
            evaluation_module, "_git_state", lambda root: (GIT_HEAD, True)
        )
        publish_signal_evaluation_bundle(
            evaluation,
            output_root,
            command_arguments=("--prices", "synthetic-app.parquet"),
            creation_time="2026-08-24T12:00:02.123456Z",
            repository_root=project,
        )
        publish_signal_evaluation_bundle(
            evaluation,
            output_root,
            command_arguments=("--prices", "synthetic-app.parquet"),
            creation_time="2026-08-25T12:00:03.123456Z",
            repository_root=project,
        )
    finally:
        monkeypatch.undo()
    return project


@pytest.fixture
def project_root(tmp_path: Path, source_project: Path) -> Path:
    project = tmp_path / "project"
    shutil.copytree(source_project, project)
    return project


def _app(project: Path, monkeypatch: pytest.MonkeyPatch) -> AppTest:
    monkeypatch.setenv("ETF_CROWDING_PROJECT_ROOT", str(project))

    def forbidden_export_primitive(*args: object, **kwargs: object) -> None:
        raise AssertionError("export-capable Streamlit primitive invoked")

    for name in ("dataframe", "data_editor", "download_button"):
        monkeypatch.setattr(st, name, forbidden_export_primitive)
    return AppTest.from_file(APP_PATH, default_timeout=120)


def _select_valid_run(app: AppTest) -> AppTest:
    app.run()
    assert list(app.exception) == []
    next(
        element
        for element in app.selectbox
        if element.label == "Verified run candidate"
    ).set_value(RUN_ID)
    app.run()
    assert list(app.exception) == []
    return app


def _all_text(app: AppTest) -> str:
    values: list[str] = []
    for element_type in (
        app.title,
        app.header,
        app.subheader,
        app.caption,
        app.markdown,
        app.info,
        app.warning,
        app.error,
        app.success,
    ):
        values.extend(str(element.value) for element in element_type)
    return "\n".join(values)


def test_blank_initial_state_displays_no_financial_values(
    project_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _app(project_root, monkeypatch).run()

    assert list(app.exception) == []
    assert app.selectbox[0].value == ""
    assert len(app.table) == 0
    assert len(app.get("plotly_chart")) == 0
    text = _all_text(app)
    assert "local and offline" in text
    assert "No run is selected automatically" in text
    assert "not live market data" in text
    assert "no financial values appear before verification" in text
    assert (
        "Momentum and Volatility are standalone descriptive price diagnostics." in text
    )
    assert len(app.radio) == 0

    app.session_state[f"{_ETF_DETAIL_TICKER_DURABLE_KEY_PREFIX}:unverified"] = "SOXX"
    app.session_state[f"{_DEPENDENCE_DATE_DURABLE_KEY_PREFIX}:unverified"] = (
        pd.Timestamp("2020-03-16")
    )
    app.run()

    assert list(app.exception) == []
    assert len(app.table) == 0
    assert len(app.get("plotly_chart")) == 0
    assert len(app.radio) == 0


def test_valid_overview_has_24_configured_rows_and_separate_components(
    project_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _select_valid_run(_app(project_root, monkeypatch))

    assert app.radio[0].value == "Overview"
    assert len(app.table) == 4
    tables = [cast(pd.DataFrame, element.value) for element in app.table]
    momentum = next(frame for frame in tables if "Raw Momentum" in frame)
    volatility = next(frame for frame in tables if "Raw annualized Volatility" in frame)
    momentum_status = next(frame for frame in tables if "Endpoint eligible" in frame)
    volatility_status = next(frame for frame in tables if "Window eligible" in frame)
    expected = [definition.ticker for definition in load_etf_universe()]
    for frame in (momentum, volatility, momentum_status, volatility_status):
        assert frame["Ticker"].tolist() == expected
        assert len(frame) == 24
    assert "Raw Momentum" in momentum
    assert "Raw annualized Volatility" not in momentum
    assert "Raw annualized Volatility" in volatility
    assert "Raw Momentum" not in volatility
    assert set(momentum).union(momentum_status) == {
        "Ticker",
        "Name",
        "Category",
        "Target session",
        "Raw Momentum",
        "252-session return",
        "Own-history percentile",
        "Endpoint status",
        "Normalization status",
        "Endpoint eligible",
        "Normalized eligible",
        "Reference count",
        "Price stale sessions",
        "Raw stale sessions",
        "Percentile stale sessions",
        "Acquisition status",
    }
    assert set(volatility).union(volatility_status) == {
        "Ticker",
        "Name",
        "Category",
        "Target session",
        "Raw annualized Volatility",
        "Annualized Volatility",
        "Own-history percentile",
        "Window status",
        "Normalization status",
        "Window eligible",
        "Normalized eligible",
        "Reference count",
        "Price stale sessions",
        "Raw stale sessions",
        "Percentile stale sessions",
        "Acquisition status",
    }
    assert "Missing (ticker_unavailable)" in momentum["Raw Momentum"].tolist()
    assert (
        "Missing (ticker_unavailable)"
        in volatility["Raw annualized Volatility"].tolist()
    )
    assert "no ranking or export" in _all_text(app)
    assert list(app.exception) == []


def test_selection_change_and_changed_artifact_clear_verified_state(
    project_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _select_valid_run(_app(project_root, monkeypatch))
    root = project_root / "data" / "processed" / "signal_evaluations"
    bundle = load_signal_evaluation_bundle(root, RUN_ID)
    app.session_state[
        _bundle_scoped_state_key(_ETF_DETAIL_TICKER_DURABLE_KEY_PREFIX, bundle)
    ] = "SOXX"
    app.session_state[
        _bundle_scoped_state_key(_DEPENDENCE_DATE_DURABLE_KEY_PREFIX, bundle)
    ] = pd.Timestamp("2020-03-16")
    coverage = root / RUN_ID / "coverage.parquet"
    coverage.write_bytes(coverage.read_bytes() + b"modified-after-verification")

    app.radio[0].set_value("ETF detail")
    app.run()

    assert list(app.exception) == []
    assert "failed closed" in _all_text(app)
    assert len(app.table) == 0
    assert len(app.get("plotly_chart")) == 0
    assert "signal_dashboard_verified_bundle" not in app.session_state
    assert not any(element.label == "Configured ETF" for element in app.selectbox)
    assert not any(
        element.label == "Dependence signal date" for element in app.selectbox
    )


def test_invalid_candidate_never_falls_back_to_valid_run(
    project_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    invalid_run = "20260825T120002123456Z"
    invalid = project_root / "data" / "processed" / "signal_evaluations" / invalid_run
    invalid.mkdir()
    app = _app(project_root, monkeypatch).run()
    next(
        element
        for element in app.selectbox
        if element.label == "Verified run candidate"
    ).set_value(invalid_run)
    app.run()

    assert list(app.exception) == []
    assert "failed closed" in _all_text(app)
    assert len(app.table) == 0
    assert len(app.get("plotly_chart")) == 0
    assert app.selectbox[0].value == invalid_run


def test_etf_detail_shows_stale_missing_diagnostics_and_unfilled_gaps(
    project_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _select_valid_run(_app(project_root, monkeypatch))
    app.radio[0].set_value("ETF detail")
    app.run()
    next(
        element for element in app.selectbox if element.label == "Configured ETF"
    ).set_value("QQQ")
    app.run()

    text = _all_text(app)
    assert list(app.exception) == []
    assert "Stale at the fixed target" in text
    assert "Adjusted-price history" in text
    assert "Momentum" in text and "standalone" in text
    assert "Volatility" in text
    assert len(app.get("plotly_chart")) == 5
    current_tables = [cast(pd.DataFrame, element.value) for element in app.table]
    values = "\n".join(frame.to_string(index=False) for frame in current_tables)
    staleness = next(frame for frame in current_tables if "Staleness" in frame)
    assert staleness["Measure"].tolist() == [
        "Price staleness",
        "Momentum raw staleness",
        "Momentum normalized staleness",
        "Volatility raw staleness",
        "Volatility normalized staleness",
    ]
    assert "1 XNYS session" in staleness["Staleness"].tolist()
    assert "missing_end_row" in values
    assert "missing_price_rows" in values
    assert "Missing" in values

    root = project_root / "data" / "processed" / "signal_evaluations"
    bundle = load_signal_evaluation_bundle(root, RUN_ID)
    prices = bundle.to_pandas("input_prices")
    qqq_prices = prices.loc[prices["ticker"].eq("QQQ")]
    coverage = bundle.to_pandas("coverage").set_index("ticker").loc["QQQ"]
    figure = build_adjusted_price_figure(
        qqq_prices,
        cast(tuple[pd.Timestamp, ...], coverage["missing_canonical_dates"]),
        "QQQ",
    )
    assert figure.data[0].connectgaps is False
    assert pd.isna(figure.data[0].y[-1])
    assert figure.layout.yaxis.title.text == "Adjusted close"
    assert figure.layout.plot_bgcolor == CHART_BACKGROUND_COLOR
    assert figure.data[0].line.color == NEUTRAL_LINE_COLOR
    assert figure.data[0].hovertemplate is not None
    assert "$" not in figure.data[0].hovertemplate
    assert "USD" not in figure.data[0].hovertemplate
    assert "%{y:,.4f}" in figure.data[0].hovertemplate


def test_view_local_defaults_and_same_view_changes_use_verified_bundle_options(
    project_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = project_root / "data" / "processed" / "signal_evaluations"
    bundle = load_signal_evaluation_bundle(root, RUN_ID)
    latest_date = (
        bundle.to_pandas("dependence")
        .loc[lambda frame: frame["scope"].eq("per_session"), "signal_date"]
        .iloc[-1]
    )
    app = _select_valid_run(_app(project_root, monkeypatch))

    app.radio[0].set_value("ETF detail")
    app.run()
    configured_etf = next(
        element for element in app.selectbox if element.label == "Configured ETF"
    )
    assert configured_etf.value == "SPY"
    configured_etf.set_value("SOXX")
    app.run()
    assert list(app.exception) == []
    assert (
        next(
            element for element in app.selectbox if element.label == "Configured ETF"
        ).value
        == "SOXX"
    )

    app.radio[0].set_value("Dependence diagnostics")
    app.run()
    dependence_date = next(
        element
        for element in app.selectbox
        if element.label == "Dependence signal date"
    )
    assert dependence_date.options[dependence_date.index] == pd.Timestamp(
        latest_date
    ).strftime("%Y-%m-%d")
    dependence_date.select_index(dependence_date.options.index("2020-03-16"))
    app.run()

    assert list(app.exception) == []
    selected_date = next(
        element
        for element in app.selectbox
        if element.label == "Dependence signal date"
    )
    assert selected_date.options[selected_date.index] == "2020-03-16"
    displayed_session = next(
        cast(pd.DataFrame, element.value)
        for element in app.table
        if set(cast(pd.DataFrame, element.value)["Scope"]) == {"per_session"}
    )
    assert set(displayed_session["Signal date"]) == {"2020-03-16"}


def test_etf_and_dependence_date_survive_repeated_cross_view_navigation(
    project_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = project_root / "data" / "processed" / "signal_evaluations"
    bundle = load_signal_evaluation_bundle(root, RUN_ID)
    soxx_coverage = (
        bundle.to_pandas("coverage")
        .loc[lambda frame: frame["ticker"].eq("SOXX")]
        .iloc[0]
    )
    expected_staleness = build_staleness_table(soxx_coverage)
    app = _select_valid_run(_app(project_root, monkeypatch))

    def assert_soxx_detail() -> None:
        assert list(app.exception) == []
        assert (
            next(
                element
                for element in app.selectbox
                if element.label == "Configured ETF"
            ).value
            == "SOXX"
        )
        assert "**SOXX —" in _all_text(app)
        displayed_staleness = next(
            cast(pd.DataFrame, element.value)
            for element in app.table
            if "Staleness" in cast(pd.DataFrame, element.value)
        )
        pd.testing.assert_frame_equal(displayed_staleness, expected_staleness)

    def assert_selected_dependence_date() -> None:
        assert list(app.exception) == []
        date_widget = next(
            element
            for element in app.selectbox
            if element.label == "Dependence signal date"
        )
        assert date_widget.options[date_widget.index] == "2020-03-16"
        displayed_session = next(
            cast(pd.DataFrame, element.value)
            for element in app.table
            if set(cast(pd.DataFrame, element.value)["Scope"]) == {"per_session"}
        )
        assert set(displayed_session["Signal date"]) == {"2020-03-16"}

    app.radio[0].set_value("ETF detail")
    app.run()
    next(
        element for element in app.selectbox if element.label == "Configured ETF"
    ).set_value("SOXX")
    app.run()
    assert_soxx_detail()

    app.radio[0].set_value("Dependence diagnostics")
    app.run()
    date_widget = next(
        element
        for element in app.selectbox
        if element.label == "Dependence signal date"
    )
    date_widget.select_index(date_widget.options.index("2020-03-16"))
    app.run()
    assert_selected_dependence_date()

    for _ in range(2):
        for view in ("Overview", "Provenance and limitations"):
            app.radio[0].set_value(view)
            app.run()
            assert list(app.exception) == []

        app.radio[0].set_value("ETF detail")
        app.run()
        assert_soxx_detail()

        app.radio[0].set_value("Dependence diagnostics")
        app.run()
        assert_selected_dependence_date()

    assert (
        next(
            element
            for element in app.selectbox
            if element.label == "Verified run candidate"
        ).value
        == RUN_ID
    )


def test_invalid_durable_selections_repair_before_widget_creation(
    project_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = project_root / "data" / "processed" / "signal_evaluations"
    bundle = load_signal_evaluation_bundle(root, RUN_ID)
    ticker_key = _bundle_scoped_state_key(_ETF_DETAIL_TICKER_DURABLE_KEY_PREFIX, bundle)
    date_key = _bundle_scoped_state_key(_DEPENDENCE_DATE_DURABLE_KEY_PREFIX, bundle)
    latest_date = pd.Timestamp(
        bundle.to_pandas("dependence")
        .loc[lambda frame: frame["scope"].eq("per_session"), "signal_date"]
        .iloc[-1]
    )
    app = _select_valid_run(_app(project_root, monkeypatch))

    for invalid_ticker in ("NOT_CONFIGURED", ["SOXX"]):
        app.session_state[ticker_key] = invalid_ticker
        app.radio[0].set_value("ETF detail")
        app.run()
        assert list(app.exception) == []
        assert (
            next(
                element
                for element in app.selectbox
                if element.label == "Configured ETF"
            ).value
            == "SPY"
        )
        assert app.session_state[ticker_key] == "SPY"

    for invalid_date in ("2020-03-16", pd.Timestamp("1900-01-01")):
        app.session_state[date_key] = invalid_date
        app.radio[0].set_value("Dependence diagnostics")
        app.run()
        assert list(app.exception) == []
        date_widget = next(
            element
            for element in app.selectbox
            if element.label == "Dependence signal date"
        )
        assert date_widget.options[date_widget.index] == latest_date.strftime(
            "%Y-%m-%d"
        )
        assert app.session_state[date_key] == latest_date


def test_durable_selections_are_isolated_by_verified_bundle_digest(
    project_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = project_root / "data" / "processed" / "signal_evaluations"
    first_bundle = load_signal_evaluation_bundle(root, RUN_ID)
    second_bundle = load_signal_evaluation_bundle(root, SECOND_RUN_ID)
    assert first_bundle.content_sha256 != second_bundle.content_sha256
    first_ticker_key = _bundle_scoped_state_key(
        _ETF_DETAIL_TICKER_DURABLE_KEY_PREFIX, first_bundle
    )
    first_date_key = _bundle_scoped_state_key(
        _DEPENDENCE_DATE_DURABLE_KEY_PREFIX, first_bundle
    )
    second_ticker_key = _bundle_scoped_state_key(
        _ETF_DETAIL_TICKER_DURABLE_KEY_PREFIX, second_bundle
    )
    second_date_key = _bundle_scoped_state_key(
        _DEPENDENCE_DATE_DURABLE_KEY_PREFIX, second_bundle
    )
    second_latest_date = pd.Timestamp(
        second_bundle.to_pandas("dependence")
        .loc[lambda frame: frame["scope"].eq("per_session"), "signal_date"]
        .iloc[-1]
    )
    app = _select_valid_run(_app(project_root, monkeypatch))

    app.radio[0].set_value("ETF detail")
    app.run()
    next(
        element for element in app.selectbox if element.label == "Configured ETF"
    ).set_value("SOXX")
    app.run()

    app.radio[0].set_value("Dependence diagnostics")
    app.run()
    first_date_widget = next(
        element
        for element in app.selectbox
        if element.label == "Dependence signal date"
    )
    first_date_widget.select_index(first_date_widget.options.index("2020-03-16"))
    app.run()
    assert app.session_state[first_ticker_key] == "SOXX"
    assert app.session_state[first_date_key] == pd.Timestamp("2020-03-16")

    next(
        element
        for element in app.selectbox
        if element.label == "Verified run candidate"
    ).set_value(SECOND_RUN_ID)
    app.run()
    assert list(app.exception) == []
    second_date_widget = next(
        element
        for element in app.selectbox
        if element.label == "Dependence signal date"
    )
    assert second_date_widget.options[second_date_widget.index] == (
        second_latest_date.strftime("%Y-%m-%d")
    )
    assert app.session_state[second_date_key] == second_latest_date

    app.radio[0].set_value("ETF detail")
    app.run()
    assert list(app.exception) == []
    assert (
        next(
            element for element in app.selectbox if element.label == "Configured ETF"
        ).value
        == "SPY"
    )
    assert app.session_state[second_ticker_key] == "SPY"

    next(
        element
        for element in app.selectbox
        if element.label == "Verified run candidate"
    ).set_value(RUN_ID)
    app.run()
    assert list(app.exception) == []
    assert (
        next(
            element for element in app.selectbox if element.label == "Configured ETF"
        ).value
        == "SOXX"
    )

    app.radio[0].set_value("Dependence diagnostics")
    app.run()
    restored_date_widget = next(
        element
        for element in app.selectbox
        if element.label == "Dependence signal date"
    )
    assert restored_date_widget.options[restored_date_widget.index] == "2020-03-16"


def test_staleness_table_keeps_fresh_stale_and_missing_explicit(
    project_root: Path,
) -> None:
    root = project_root / "data" / "processed" / "signal_evaluations"
    coverage = load_signal_evaluation_bundle(root, RUN_ID).to_pandas("coverage")
    by_ticker = coverage.set_index("ticker")

    fresh = build_staleness_table(by_ticker.loc["SPY"])
    stale = build_staleness_table(by_ticker.loc["QQQ"])
    missing = build_staleness_table(by_ticker.loc["IWM"])

    assert fresh["Staleness"].tolist() == ["0 XNYS sessions"] * 5
    assert stale["Staleness"].tolist() == [
        "1 XNYS session",
        "1 XNYS session",
        "1 XNYS session",
        "1 XNYS session",
        "1 XNYS session",
    ]
    assert missing["Staleness"].tolist() == ["Missing"] * 5


def test_dependence_view_retains_statuses_counts_dates_and_populations(
    project_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _select_valid_run(_app(project_root, monkeypatch))
    original_to_pandas = VerifiedSignalEvaluationBundle.to_pandas

    def with_all_estimator_statuses(
        bundle: VerifiedSignalEvaluationBundle,
        artifact: str,
    ) -> pd.DataFrame:
        frame = original_to_pandas(bundle, cast(ArtifactName, artifact))
        if artifact == "dependence":
            for ticker, estimator, status, estimate, pair_count in (
                ("SPY", "pearson", "insufficient_pairs", pd.NA, 2),
                ("SPY", "spearman", "constant_input", pd.NA, 10),
                ("QQQ", "pearson", "eligible", 0.125, 10),
                ("QQQ", "spearman", "eligible", 0.125, 10),
            ):
                mask = (
                    frame["scope"].eq("per_etf")
                    & frame["ticker"].eq(ticker)
                    & frame["estimator"].eq(estimator)
                )
                frame.loc[mask, "status"] = status
                frame.loc[mask, "estimate"] = estimate
                frame.loc[mask, "pair_count"] = pair_count
        return frame

    monkeypatch.setattr(
        VerifiedSignalEvaluationBundle, "to_pandas", with_all_estimator_statuses
    )
    app.radio[0].set_value("Dependence diagnostics")
    app.run()

    assert list(app.exception) == []
    assert len(app.table) == 2
    tables = [cast(pd.DataFrame, element.value) for element in app.table]
    per_etf = next(frame for frame in tables if set(frame["Scope"]) == {"per_etf"})
    per_session = next(
        frame for frame in tables if set(frame["Scope"]) == {"per_session"}
    )
    required = {
        "Estimator",
        "Pair count",
        "Estimator status",
        "Universe status",
        "Included ticker population",
    }
    assert required.issubset(per_etf.columns)
    assert required.issubset(per_session.columns)
    assert per_etf["Ticker"].tolist() == ["SPY", "SPY"]
    assert set(per_etf["Estimator status"]) == {
        "insufficient_pairs",
        "constant_input",
    }
    assert set(per_session["Estimator"]) == {"pearson", "spearman"}
    assert len(per_session) == 2
    next(
        element for element in app.selectbox if element.label == "ETF dependence ticker"
    ).set_value("QQQ")
    app.run()
    qqq_table = next(
        cast(pd.DataFrame, element.value)
        for element in app.table
        if set(cast(pd.DataFrame, element.value)["Scope"]) == {"per_etf"}
    )
    assert set(qqq_table["Estimator status"]) == {"eligible"}
    text = _all_text(app).lower()
    assert "descriptive diagnostics only" in text
    assert "predictive or causal evidence" in text
    assert "p-value" not in text

    statuses = pd.DataFrame(
        {
            "scope": ["per_etf"] * 3,
            "estimator": ["pearson"] * 3,
            "ticker": ["SPY", "QQQ", "IWM"],
            "signal_date": [pd.NaT] * 3,
            "pair_count": [2, 10, 10],
            "first_signal_date": [pd.NaT] * 3,
            "last_signal_date": [pd.NaT] * 3,
            "included_tickers": [(), (), ()],
            "universe_status": ["not_applicable"] * 3,
            "status": ["insufficient_pairs", "constant_input", "eligible"],
            "estimate": [pd.NA, pd.NA, 0.125],
        }
    )
    display = _dependence_display(statuses)
    assert display["Estimator status"].tolist() == [
        "insufficient_pairs",
        "constant_input",
        "eligible",
    ]


def test_provenance_view_exposes_required_fixed_run_evidence(
    project_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _select_valid_run(_app(project_root, monkeypatch))
    app.radio[0].set_value("Provenance and limitations")
    app.run()

    assert list(app.exception) == []
    text = _all_text(app)
    tables = "\n".join(
        cast(pd.DataFrame, element.value).to_string(index=False)
        for element in app.table
    )
    combined = text + "\n" + tables
    for required in (
        RUN_ID,
        GIT_HEAD,
        "Target XNYS session",
        "Captured evaluation instant",
        "Request end (exclusive)",
        "Pre-run worktree dirty",
        "Effective universe definitions SHA-256",
        "The stored manifest field `universe_config_sha256` is the SHA-256 of the "
        "deterministic JSON serialization of the effective parsed ETF definitions "
        "in configured order, not the raw YAML resource bytes.",
        "Verified from exact snapshotted bytes",
        "Manifest computed SHA-256",
        "Current-vintage history",
        "Not a point-in-time backtest",
        "Survivorship bias",
        "Provider revisions",
        "Rights unresolved",
        "Descriptive only",
        "Viewer runtime versions",
        "not internally self-authenticating",
        "unsigned manifest and all artifacts could be replaced together",
        "Flow and Concentration remain deferred and unavailable",
        "No composite, weights, thresholds, risk classes, missing-component "
        "reweighting, or Crowding Score exists",
    ):
        assert required in combined
    assert "Universe-config SHA-256" not in combined
    artifact_table = next(
        cast(pd.DataFrame, element.value)
        for element in app.table
        if "Expected SHA-256" in cast(pd.DataFrame, element.value)
    )
    assert artifact_table["Filename"].tolist() == [
        "input_prices.parquet",
        "coverage.parquet",
        "momentum.parquet",
        "volatility.parquet",
        "dependence.parquet",
        "manifest.json",
    ]
    assert set(artifact_table.iloc[:5]["Verification result"]) == {"Verified"}
    assert (
        artifact_table.iloc[:5]["Expected SHA-256"]
        == artifact_table.iloc[:5]["Computed SHA-256"]
    ).all()
    manifest_row = artifact_table.iloc[5]
    assert manifest_row["Rows"] == "Not applicable"
    assert manifest_row["Schema"] == "Not applicable"
    assert "no self-hash" in manifest_row["Expected SHA-256"]
    assert len(manifest_row["Computed SHA-256"]) == 64
    assert manifest_row["Verification result"] == (
        "Structure verified; digest computed; unsigned"
    )


def test_presentation_contract_has_no_combined_output_fields(
    project_root: Path,
) -> None:
    root = project_root / "data" / "processed" / "signal_evaluations"
    bundle = load_signal_evaluation_bundle(root, RUN_ID)
    momentum, volatility = build_overview_tables(bundle)
    columns = {column.lower() for column in (*momentum.columns, *volatility.columns)}
    forbidden = {
        "composite",
        "crowding score",
        "risk class",
        "traffic light",
        "grade",
        "weight",
        "p-value",
    }
    assert columns.isdisjoint(forbidden)
    assert not any(
        "momentum" in column and "volatility" in column for column in columns
    )


def test_dashboard_source_uses_only_static_tables_and_anchored_headings() -> None:
    source_path = APP_PATH.with_name("signal_dashboard.py")
    source = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    forbidden_calls: list[str] = []
    heading_calls: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr in {"dataframe", "data_editor", "download_button"}:
            forbidden_calls.append(node.func.attr)
        if node.func.attr in {"header", "subheader"}:
            heading_calls.append(node)

    assert forbidden_calls == []
    assert heading_calls
    assert all(
        any(keyword.arg == "anchor" for keyword in call.keywords)
        for call in heading_calls
    )
    assert len(HEADING_ANCHORS) == len(set(HEADING_ANCHORS.values()))
    assert all(f'HEADING_ANCHORS["{key}"]' in source for key in HEADING_ANCHORS)
    assert {
        HEADING_ANCHORS["overview"],
        HEADING_ANCHORS["detail"],
        HEADING_ANCHORS["dependence"],
        HEADING_ANCHORS["provenance"],
    } == {
        "overview-view",
        "etf-detail-view",
        "dependence-diagnostics-view",
        "provenance-limitations-view",
    }


def test_view_selection_state_is_session_local_and_uses_temporary_widget_keys() -> None:
    dashboard_source = APP_PATH.with_name("signal_dashboard.py").read_text(
        encoding="utf-8"
    )
    entrypoint_source = APP_PATH.read_text(encoding="utf-8")
    combined_source = dashboard_source + "\n" + entrypoint_source

    assert _ETF_DETAIL_TICKER_WIDGET_KEY.startswith("_")
    assert _DEPENDENCE_DATE_WIDGET_KEY.startswith("_")
    assert _ETF_DETAIL_TICKER_DURABLE_KEY_PREFIX != _ETF_DETAIL_TICKER_WIDGET_KEY
    assert _DEPENDENCE_DATE_DURABLE_KEY_PREFIX != _DEPENDENCE_DATE_WIDGET_KEY
    assert "st.session_state" in dashboard_source
    for forbidden in (
        "query_params",
        "experimental_get_query_params",
        "experimental_set_query_params",
        "localStorage",
        "sessionStorage",
        "st.cache_data",
        "st.cache_resource",
    ):
        assert forbidden not in combined_source


def test_fragment_clear_script_is_trusted_and_history_neutral() -> None:
    source = APP_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    assignment = next(
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name)
            and target.id == "_FRAGMENT_CLEAR_SCRIPT_TEMPLATE"
            for target in node.targets
        )
    )

    assert isinstance(assignment.value, ast.Constant)
    assert assignment.value.value == _FRAGMENT_CLEAR_SCRIPT_TEMPLATE
    assert (
        "history.replaceState(\n"
        "    null,\n"
        '    "",\n'
        "    window.location.pathname + window.location.search,\n"
        ");"
    ) in _FRAGMENT_CLEAR_SCRIPT_TEMPLATE
    assert _FRAGMENT_CLEAR_SCRIPT_TEMPLATE.count("history.replaceState(") == 1
    assert "pushState" not in _FRAGMENT_CLEAR_SCRIPT_TEMPLATE
    assert "location.hash =" not in _FRAGMENT_CLEAR_SCRIPT_TEMPLATE
    assert "{transition_nonce:d}" in _FRAGMENT_CLEAR_SCRIPT_TEMPLATE
    assert _FRAGMENT_CLEAR_SCRIPT_TEMPLATE.strip().startswith(
        '<script data-transition-nonce="'
    )
    assert _FRAGMENT_CLEAR_SCRIPT_TEMPLATE.strip().endswith("</script>")
    assert "<iframe" not in source.lower()
    assert "frameElement" not in source
    assert "tabindex" not in _FRAGMENT_CLEAR_SCRIPT_TEMPLATE.lower()
    assert "streamlit.components.v1" not in source
    assert "components.html" not in source
    assert "unsafe_allow_html" not in source

    html_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "st"
        and node.func.attr == "html"
    ]
    assert len(html_calls) == 1
    javascript_keyword = next(
        keyword
        for keyword in html_calls[0].keywords
        if keyword.arg == "unsafe_allow_javascript"
    )
    assert isinstance(javascript_keyword.value, ast.Constant)
    assert javascript_keyword.value.value is True
    for external_name in (
        "run_id",
        "selected_run",
        "query",
        "ticker",
        "provider",
        "manifest",
        "view",
    ):
        assert external_name not in _FRAGMENT_CLEAR_SCRIPT_TEMPLATE.lower()


def test_streamlit_dependency_floor_matches_javascript_api_requirement() -> None:
    project_root = APP_PATH.parents[1]
    pyproject = tomllib.loads(
        (project_root / "pyproject.toml").read_text(encoding="utf-8")
    )
    project = cast(dict[str, object], pyproject["project"])
    dependencies = cast(list[str], project["dependencies"])
    pyproject_constraints = [
        dependency
        for dependency in dependencies
        if dependency.casefold().startswith("streamlit")
    ]
    requirement_constraints = [
        line.partition("#")[0].strip()
        for line in (project_root / "requirements.txt")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.partition("#")[0].strip().casefold().startswith("streamlit")
    ]

    assert pyproject_constraints == requirement_constraints
    assert len(pyproject_constraints) == 1
    match = re.fullmatch(r"streamlit>=([0-9]+(?:\.[0-9]+)*)", pyproject_constraints[0])
    assert match is not None
    assert tuple(int(part) for part in match.group(1).split(".")) >= (1, 52)


def test_fragment_clear_runs_once_per_actual_top_level_view_change(
    project_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rendered_scripts: list[tuple[str, bool]] = []

    def capture_html(
        body: str,
        *,
        width: str = "stretch",
        unsafe_allow_javascript: bool = False,
    ) -> None:
        assert width == "stretch"
        rendered_scripts.append((body, unsafe_allow_javascript))

    monkeypatch.setattr(st, "html", capture_html)
    app = _select_valid_run(_app(project_root, monkeypatch))
    assert rendered_scripts == []

    for count, view in enumerate(
        (
            "ETF detail",
            "Dependence diagnostics",
            "Provenance and limitations",
            "Overview",
            "ETF detail",
        ),
        start=1,
    ):
        app.radio[0].set_value(view)
        app.run()
        assert list(app.exception) == []
        assert app.radio[0].value == view
        assert len(rendered_scripts) == count
        assert rendered_scripts[-1] == (
            _FRAGMENT_CLEAR_SCRIPT_TEMPLATE.format(transition_nonce=count),
            True,
        )

    payloads = [payload for payload, _ in rendered_scripts]
    assert len(payloads) == len(set(payloads)) == 5
    for payload in payloads:
        for external_value in (
            RUN_ID,
            *VIEW_NAMES,
            "SPY",
            "QQQ",
            "provider",
            "manifest",
            "qa=fragment",
        ):
            assert external_value not in payload

    app.run()
    assert list(app.exception) == []
    assert len(rendered_scripts) == 5
    assert app.radio[0].value == "ETF detail"

    configured_etf = next(
        element for element in app.selectbox if element.label == "Configured ETF"
    )
    configured_etf.set_value("QQQ")
    app.run()
    assert list(app.exception) == []
    assert len(rendered_scripts) == 5
    assert (
        next(
            element for element in app.selectbox if element.label == "Configured ETF"
        ).value
        == "QQQ"
    )

    app.radio[0].set_value("Dependence diagnostics")
    app.run()
    assert list(app.exception) == []
    assert len(rendered_scripts) == 6
    assert rendered_scripts[-1] == (
        _FRAGMENT_CLEAR_SCRIPT_TEMPLATE.format(transition_nonce=6),
        True,
    )
    dependence_ticker = next(
        element for element in app.selectbox if element.label == "ETF dependence ticker"
    )
    dependence_date = next(
        element
        for element in app.selectbox
        if element.label == "Dependence signal date"
    )
    dependence_ticker.set_value("QQQ")
    selected_date_label = dependence_date.options[0]
    dependence_date.select_index(0)
    app.run()
    assert list(app.exception) == []
    assert len(rendered_scripts) == 6
    assert (
        next(
            element
            for element in app.selectbox
            if element.label == "ETF dependence ticker"
        ).value
        == "QQQ"
    )
    selected_date = next(
        element
        for element in app.selectbox
        if element.label == "Dependence signal date"
    )
    assert selected_date.options[selected_date.index] == selected_date_label
    assert (
        next(
            element
            for element in app.selectbox
            if element.label == "Verified run candidate"
        ).value
        == RUN_ID
    )


def test_plotly_contract_disables_export_and_uses_contrasting_neutral_line() -> None:
    assert _CHART_CONFIG["displayModeBar"] is False
    assert _CHART_CONFIG["displaylogo"] is False
    assert "toImage" in cast(list[str], _CHART_CONFIG["modeBarButtonsToRemove"])

    measured_ratio = _contrast_ratio(NEUTRAL_LINE_COLOR, CHART_BACKGROUND_COLOR)
    assert NEUTRAL_LINE_COLOR == "#D9D9D9"
    assert CHART_BACKGROUND_COLOR == "#0E1117"
    assert measured_ratio == pytest.approx(13.3883, abs=0.0001)
    assert measured_ratio >= 4.5

    dependence = pd.DataFrame(
        {
            "signal_date": pd.to_datetime(["2026-08-24", "2026-08-24"]),
            "estimator": ["pearson", "spearman"],
            "estimate": [0.125, 0.25],
        }
    )
    pearson = build_dependence_figure(dependence, "pearson")
    spearman = build_dependence_figure(dependence, "spearman")
    for figure in (pearson, spearman):
        assert figure.data[0].line.color == NEUTRAL_LINE_COLOR
        assert figure.layout.plot_bgcolor == CHART_BACKGROUND_COLOR
    assert pearson.data[0].line.dash == "solid"
    assert spearman.data[0].line.dash == "dash"


def test_verified_context_preserves_complete_values_in_page_and_sidebar(
    project_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle_root = project_root / "data" / "processed" / "signal_evaluations"
    bundle = load_signal_evaluation_bundle(bundle_root, RUN_ID)
    target_text = bundle.target.target_session.strftime("%Y-%m-%d")
    captured_text = bundle.target.captured_at.isoformat().replace("+00:00", "Z")
    app = _select_valid_run(_app(project_root, monkeypatch))

    all_text = _all_text(app)
    sidebar_text = "\n".join(
        str(element.value)
        for element_type in (
            app.sidebar.subheader,
            app.sidebar.markdown,
            app.sidebar.caption,
        )
        for element in element_type
    )
    for required in (RUN_ID, target_text, captured_text):
        assert all_text.count(required) >= 2
        assert required in sidebar_text
    assert len(app.metric) == 0


def test_every_view_renders_without_export_capable_primitives(
    project_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _select_valid_run(_app(project_root, monkeypatch))

    for view in (
        "Overview",
        "ETF detail",
        "Dependence diagnostics",
        "Provenance and limitations",
    ):
        app.radio[0].set_value(view)
        app.run()
        assert list(app.exception) == []


def test_synthetic_bundle_hashes_are_stable_during_apptest(
    project_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle_path = project_root / "data" / "processed" / "signal_evaluations" / RUN_ID
    before = {
        path.name: (
            path.stat().st_size,
            path.stat().st_mtime_ns,
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        for path in bundle_path.iterdir()
    }
    app = _select_valid_run(_app(project_root, monkeypatch))
    app.radio[0].set_value("Provenance and limitations")
    app.run()
    after = {
        path.name: (
            path.stat().st_size,
            path.stat().st_mtime_ns,
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        for path in bundle_path.iterdir()
    }

    assert list(app.exception) == []
    assert after == before


def test_manifest_is_synthetic_and_contains_no_provider_payload_copy(
    source_project: Path,
) -> None:
    manifest_path = (
        source_project
        / "data"
        / "processed"
        / "signal_evaluations"
        / RUN_ID
        / "manifest.json"
    )
    manifest = cast(
        dict[str, object], json.loads(manifest_path.read_text(encoding="utf-8"))
    )
    assert manifest["command_arguments"] == ["--prices", "synthetic-app.parquet"]
