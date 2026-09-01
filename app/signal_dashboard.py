"""Presentation helpers for the verified local signal-bundle dashboard."""

from __future__ import annotations

import sys
from collections.abc import Mapping
from importlib.metadata import PackageNotFoundError, version
from inspect import signature
from typing import Any, Literal, cast

import numpy as np
import pandas as pd
import plotly.graph_objects as go  # type: ignore[import-untyped]
import streamlit as st

from etf_crowding.analysis import VerifiedSignalEvaluationBundle

MOMENTUM_COLOR = "#0072B2"
VOLATILITY_COLOR = "#D55E00"
CHART_BACKGROUND_COLOR = "#0E1117"
NEUTRAL_LINE_COLOR = "#D9D9D9"
PRICE_COLOR = NEUTRAL_LINE_COLOR
DEPENDENCE_COLOR = NEUTRAL_LINE_COLOR
MISSING_TEXT = "Missing"
VIEW_NAMES = (
    "Overview",
    "ETF detail",
    "Dependence diagnostics",
    "Provenance and limitations",
)

_ETF_DETAIL_TICKER_DURABLE_KEY_PREFIX = "signal_dashboard_etf_detail_ticker_selection"
_ETF_DETAIL_TICKER_WIDGET_KEY = "_signal_dashboard_etf_detail_ticker_selection"
_DEPENDENCE_DATE_DURABLE_KEY_PREFIX = "signal_dashboard_dependence_date_selection"
_DEPENDENCE_DATE_WIDGET_KEY = "_signal_dashboard_dependence_date_selection"

HEADING_ANCHORS = {
    "verified_context": "verified-run-context",
    "sidebar_context": "sidebar-fixed-run-context",
    "overview": "overview-view",
    "overview_momentum": "overview-momentum",
    "overview_momentum_status": "overview-momentum-status",
    "overview_volatility": "overview-volatility",
    "overview_volatility_status": "overview-volatility-status",
    "detail": "etf-detail-view",
    "detail_staleness": "etf-detail-staleness",
    "detail_price": "etf-detail-adjusted-price",
    "detail_momentum": "etf-detail-momentum",
    "detail_volatility": "etf-detail-volatility",
    "dependence": "dependence-diagnostics-view",
    "dependence_etf": "dependence-per-etf",
    "dependence_session": "dependence-per-session",
    "provenance": "provenance-limitations-view",
    "provenance_packages": "provenance-evaluation-packages",
    "provenance_artifacts": "provenance-artifacts",
    "provenance_runtime": "provenance-viewer-runtime",
    "provenance_limitations": "provenance-interpretation-limitations",
}

_CHART_CONFIG: dict[str, object] = {
    "displayModeBar": False,
    "displaylogo": False,
    "modeBarButtonsToRemove": ["toImage"],
    "responsive": True,
}


def _bundle_scoped_state_key(
    prefix: str, bundle: VerifiedSignalEvaluationBundle
) -> str:
    return f"{prefix}:{bundle.content_sha256}"


def _store_temporary_widget_value(durable_key: str, temporary_widget_key: str) -> None:
    st.session_state[durable_key] = st.session_state[temporary_widget_key]


def _prepare_ticker_widget_state(
    bundle: VerifiedSignalEvaluationBundle, tickers: tuple[str, ...]
) -> str:
    durable_key = _bundle_scoped_state_key(
        _ETF_DETAIL_TICKER_DURABLE_KEY_PREFIX, bundle
    )
    saved_value = st.session_state.get(durable_key)
    if type(saved_value) is not str or saved_value not in tickers:
        saved_value = tickers[0]
        st.session_state[durable_key] = saved_value
    st.session_state[_ETF_DETAIL_TICKER_WIDGET_KEY] = saved_value
    return durable_key


def _prepare_dependence_date_widget_state(
    bundle: VerifiedSignalEvaluationBundle,
    available_dates: tuple[pd.Timestamp, ...],
) -> str:
    durable_key = _bundle_scoped_state_key(_DEPENDENCE_DATE_DURABLE_KEY_PREFIX, bundle)
    saved_value = st.session_state.get(durable_key)
    if type(saved_value) is not pd.Timestamp or saved_value not in available_dates:
        saved_value = available_dates[-1]
        st.session_state[durable_key] = saved_value
    st.session_state[_DEPENDENCE_DATE_WIDGET_KEY] = saved_value
    return durable_key


def _contrast_ratio(foreground: str, background: str) -> float:
    """Return the WCAG relative-luminance contrast ratio for two hex colors."""

    def luminance(color: str) -> float:
        channels = tuple(int(color[index : index + 2], 16) / 255 for index in (1, 3, 5))
        linear = tuple(
            channel / 12.92
            if channel <= 0.04045
            else ((channel + 0.055) / 1.055) ** 2.4
            for channel in channels
        )
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    lighter, darker = sorted(
        (luminance(foreground), luminance(background)), reverse=True
    )
    return (lighter + 0.05) / (darker + 0.05)


def _is_missing(value: object) -> bool:
    if value is None or value is pd.NA or value is pd.NaT:
        return True
    return isinstance(value, (float, np.floating)) and bool(np.isnan(value))


def _date_text(value: object) -> str:
    if _is_missing(value):
        return MISSING_TEXT
    return pd.Timestamp(cast(Any, value)).strftime("%Y-%m-%d")


def _instant_text(value: object) -> str:
    if _is_missing(value):
        return MISSING_TEXT
    return pd.Timestamp(cast(Any, value)).isoformat().replace("+00:00", "Z")


def _number_text(value: object, *, decimals: int = 4) -> str:
    if _is_missing(value):
        return MISSING_TEXT
    return f"{float(cast(float | int, value)):,.{decimals}f}"


def _percent_text(value: object) -> str:
    if _is_missing(value):
        return MISSING_TEXT
    return f"{float(cast(float | int, value)):,.2f}%"


def _percentile_text(value: object) -> str:
    if _is_missing(value):
        return MISSING_TEXT
    return f"{float(cast(float | int, value)):,.2f} / 100"


def _integer_text(value: object) -> str:
    if _is_missing(value):
        return MISSING_TEXT
    return f"{int(cast(float | int, value)):,}"


def _bool_text(value: object) -> str:
    if _is_missing(value):
        return MISSING_TEXT
    return "Yes" if bool(value) else "No"


def _missing_with_status(value: object, status: object) -> str:
    if _is_missing(value):
        return f"{MISSING_TEXT} ({status})"
    return _number_text(value)


def _status_text(value: object) -> str:
    return MISSING_TEXT if _is_missing(value) else str(value)


def _date_list_text(value: object) -> str:
    if value is None or value is pd.NA:
        return "None"
    dates = cast(tuple[pd.Timestamp, ...], value)
    if not dates:
        return "None"
    return ", ".join(_date_text(item) for item in dates)


def _ticker_list_text(value: object) -> str:
    if value is None or value is pd.NA:
        return "None"
    tickers = cast(tuple[str, ...], value)
    return ", ".join(tickers) if tickers else "None"


def _base_overview(coverage: pd.DataFrame) -> pd.DataFrame:
    result = coverage.loc[:, ["ticker", "name", "category", "target_session"]].copy()
    result.columns = ["Ticker", "Name", "Category", "Target session"]
    result["Target session"] = result["Target session"].map(_date_text)
    return result


def build_overview_tables(
    bundle: VerifiedSignalEvaluationBundle,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Build separate formatted Momentum and Volatility overview tables.

    Args:
        bundle: Fully verified fixed-run bundle.

    Returns:
        Momentum and Volatility display frames in configured universe order.
    """

    coverage = bundle.to_pandas("coverage")
    momentum = _base_overview(coverage)
    momentum["Raw Momentum"] = [
        _missing_with_status(value, status)
        for value, status in zip(
            coverage["momentum_target_raw"],
            coverage["momentum_target_status"],
            strict=True,
        )
    ]
    momentum["252-session return"] = coverage["momentum_target_simple_return_pct"].map(
        _percent_text
    )
    momentum["Own-history percentile"] = coverage["momentum_target_percentile"].map(
        _percentile_text
    )
    momentum["Endpoint status"] = coverage["momentum_target_status"].map(_status_text)
    momentum["Normalization status"] = coverage[
        "momentum_target_normalization_status"
    ].map(_status_text)
    momentum["Endpoint eligible"] = coverage["momentum_target_raw_eligible"].map(
        _bool_text
    )
    momentum["Normalized eligible"] = coverage[
        "momentum_target_normalized_eligible"
    ].map(_bool_text)
    momentum["Reference count"] = coverage["momentum_target_reference_count"].map(
        _integer_text
    )
    momentum["Price stale sessions"] = coverage["price_staleness_sessions"].map(
        _integer_text
    )
    momentum["Raw stale sessions"] = coverage["momentum_raw_staleness_sessions"].map(
        _integer_text
    )
    momentum["Percentile stale sessions"] = coverage[
        "momentum_normalized_staleness_sessions"
    ].map(_integer_text)
    momentum["Acquisition status"] = coverage["acquisition_status"].map(_status_text)

    volatility = _base_overview(coverage)
    volatility["Raw annualized Volatility"] = [
        _missing_with_status(value, status)
        for value, status in zip(
            coverage["volatility_target_raw"],
            coverage["volatility_target_status"],
            strict=True,
        )
    ]
    volatility["Annualized Volatility"] = coverage[
        "volatility_target_annualized_pct"
    ].map(_percent_text)
    volatility["Own-history percentile"] = coverage["volatility_target_percentile"].map(
        _percentile_text
    )
    volatility["Window status"] = coverage["volatility_target_status"].map(_status_text)
    volatility["Normalization status"] = coverage[
        "volatility_target_normalization_status"
    ].map(_status_text)
    volatility["Window eligible"] = coverage["volatility_target_raw_eligible"].map(
        _bool_text
    )
    volatility["Normalized eligible"] = coverage[
        "volatility_target_normalized_eligible"
    ].map(_bool_text)
    volatility["Reference count"] = coverage["volatility_target_reference_count"].map(
        _integer_text
    )
    volatility["Price stale sessions"] = coverage["price_staleness_sessions"].map(
        _integer_text
    )
    volatility["Raw stale sessions"] = coverage[
        "volatility_raw_staleness_sessions"
    ].map(_integer_text)
    volatility["Percentile stale sessions"] = coverage[
        "volatility_normalized_staleness_sessions"
    ].map(_integer_text)
    volatility["Acquisition status"] = coverage["acquisition_status"].map(_status_text)
    return momentum, volatility


def build_current_percentile_figure(
    coverage: pd.DataFrame,
    component: Literal["momentum", "volatility"],
) -> go.Figure:
    """Build one neutral current own-history percentile chart.

    Args:
        coverage: Verified coverage frame in configured order.
        component: Standalone signal to plot.

    Returns:
        A Plotly bar chart that omits missing values without substituting them.
    """

    if component == "momentum":
        value_column = "momentum_target_percentile"
        status_column = "momentum_target_normalization_status"
        title = "Current Momentum own-history percentile"
        color = MOMENTUM_COLOR
    else:
        value_column = "volatility_target_percentile"
        status_column = "volatility_target_normalization_status"
        title = "Current Volatility own-history percentile"
        color = VOLATILITY_COLOR
    present = coverage.loc[coverage[value_column].notna()]
    figure = go.Figure(
        go.Bar(
            x=present["ticker"],
            y=present[value_column].astype(float),
            marker_color=color,
            customdata=present[[status_column]],
            hovertemplate=(
                "Ticker: %{x}<br>Percentile: %{y:.2f} / 100"
                "<br>Status: %{customdata[0]}<extra></extra>"
            ),
        )
    )
    figure.update_layout(
        title=title,
        xaxis_title="Configured ETF order",
        yaxis_title="Percentile within that ETF's own trailing reference history",
        showlegend=False,
        margin={"l": 20, "r": 20, "t": 55, "b": 20},
    )
    figure.update_yaxes(range=[0, 100])
    return figure


def _history_figure(
    frame: pd.DataFrame,
    *,
    date_column: str,
    value_column: str,
    title: str,
    yaxis_title: str,
    color: str,
    hover_format: str,
    line_dash: str = "solid",
) -> go.Figure:
    figure = go.Figure(
        go.Scatter(
            x=frame[date_column],
            y=frame[value_column],
            mode="lines",
            line={"color": color, "dash": line_dash, "width": 2},
            connectgaps=False,
            hovertemplate=f"Date: %{{x|%Y-%m-%d}}<br>Value: {hover_format}<extra></extra>",
        )
    )
    figure.update_layout(
        title=title,
        xaxis_title="Observation date",
        yaxis_title=yaxis_title,
        showlegend=False,
        margin={"l": 20, "r": 20, "t": 55, "b": 20},
        paper_bgcolor=CHART_BACKGROUND_COLOR,
        plot_bgcolor=CHART_BACKGROUND_COLOR,
        font={"color": "#FAFAFA"},
    )
    return figure


def build_adjusted_price_figure(
    prices: pd.DataFrame, missing_dates: tuple[pd.Timestamp, ...], ticker: str
) -> go.Figure:
    """Build adjusted-price history with explicit null rows for known gaps.

    Args:
        prices: Verified canonical rows for one ETF.
        missing_dates: Verified missing canonical XNYS dates for that ETF.
        ticker: Selected ticker label.

    Returns:
        A line figure with gap-connection disabled.
    """

    gap_rows = pd.DataFrame(
        {"date": pd.DatetimeIndex(missing_dates), "adjusted_close": pd.NA}
    )
    history = pd.concat(
        [prices.loc[:, ["date", "adjusted_close"]], gap_rows], ignore_index=True
    ).sort_values("date", kind="mergesort")
    return _history_figure(
        history,
        date_column="date",
        value_column="adjusted_close",
        title=f"{ticker} adjusted-price history",
        yaxis_title="Adjusted close",
        color=PRICE_COLOR,
        hover_format="%{y:,.4f}",
    )


def _render_table(frame: pd.DataFrame) -> None:
    if "hide_index" in signature(st.table).parameters:
        st.table(frame, hide_index=True)
    else:  # Compatibility with supported signatures lacking hide_index.
        st.table(frame)


def _render_figure(figure: go.Figure) -> None:
    if "width" in signature(st.plotly_chart).parameters:
        st.plotly_chart(figure, width="stretch", config=_CHART_CONFIG)
    else:  # Compatibility with supported signatures lacking width.
        st.plotly_chart(figure, use_container_width=True, config=_CHART_CONFIG)


def render_overview(bundle: VerifiedSignalEvaluationBundle) -> None:
    """Render configured-order standalone current-signal overviews."""

    st.header("Overview", anchor=HEADING_ANCHORS["overview"])
    st.write(
        "All rows refer to the fixed target session in the selected verified run. "
        "Percentiles are each ETF's own trailing-history normalization, not a new "
        "cross-sectional factor. Static tables preserve the configured 24-ETF "
        "universe order; no ranking or export is provided."
    )
    coverage = bundle.to_pandas("coverage")
    momentum, volatility = build_overview_tables(bundle)

    st.subheader(
        "Momentum — standalone 252-session signal",
        anchor=HEADING_ANCHORS["overview_momentum"],
    )
    st.caption(
        "Purpose: show current Momentum values and statuses separately from "
        "Volatility. Missing values retain their verified reason."
    )
    _render_figure(build_current_percentile_figure(coverage, "momentum"))
    _render_table(
        momentum.loc[
            :,
            [
                "Ticker",
                "Name",
                "Category",
                "Target session",
                "Raw Momentum",
                "252-session return",
                "Own-history percentile",
                "Endpoint status",
                "Normalization status",
            ],
        ]
    )
    st.subheader(
        "Momentum eligibility, reference, and freshness",
        anchor=HEADING_ANCHORS["overview_momentum_status"],
    )
    _render_table(
        momentum.loc[
            :,
            [
                "Ticker",
                "Endpoint eligible",
                "Normalized eligible",
                "Reference count",
                "Price stale sessions",
                "Raw stale sessions",
                "Percentile stale sessions",
                "Acquisition status",
            ],
        ]
    )

    st.subheader(
        "Volatility — standalone annualized 21-return signal",
        anchor=HEADING_ANCHORS["overview_volatility"],
    )
    st.caption(
        "Purpose: show current Volatility values and statuses separately from "
        "Momentum. Missing values retain their verified reason."
    )
    _render_figure(build_current_percentile_figure(coverage, "volatility"))
    _render_table(
        volatility.loc[
            :,
            [
                "Ticker",
                "Name",
                "Category",
                "Target session",
                "Raw annualized Volatility",
                "Annualized Volatility",
                "Own-history percentile",
                "Window status",
                "Normalization status",
            ],
        ]
    )
    st.subheader(
        "Volatility eligibility, reference, and freshness",
        anchor=HEADING_ANCHORS["overview_volatility_status"],
    )
    _render_table(
        volatility.loc[
            :,
            [
                "Ticker",
                "Window eligible",
                "Normalized eligible",
                "Reference count",
                "Price stale sessions",
                "Raw stale sessions",
                "Percentile stale sessions",
                "Acquisition status",
            ],
        ]
    )


def _session_count_text(value: object) -> str:
    if _is_missing(value):
        return MISSING_TEXT
    count = int(cast(float | int, value))
    unit = "session" if count == 1 else "sessions"
    return f"{count:,} XNYS {unit}"


def build_staleness_table(row: pd.Series) -> pd.DataFrame:
    """Build the five-field current-vintage display for one verified ETF row.

    Args:
        row: One verified coverage row.

    Returns:
        Static display rows that keep zero and missing staleness explicit.
    """

    return pd.DataFrame(
        {
            "Measure": [
                "Price staleness",
                "Momentum raw staleness",
                "Momentum normalized staleness",
                "Volatility raw staleness",
                "Volatility normalized staleness",
            ],
            "Staleness": [
                _session_count_text(row["price_staleness_sessions"]),
                _session_count_text(row["momentum_raw_staleness_sessions"]),
                _session_count_text(row["momentum_normalized_staleness_sessions"]),
                _session_count_text(row["volatility_raw_staleness_sessions"]),
                _session_count_text(row["volatility_normalized_staleness_sessions"]),
            ],
            "Verified status": [
                _status_text(row["acquisition_status"]),
                _status_text(row["momentum_target_status"]),
                _status_text(row["momentum_target_normalization_status"]),
                _status_text(row["volatility_target_status"]),
                _status_text(row["volatility_target_normalization_status"]),
            ],
        }
    )


def _render_staleness_warning(row: pd.Series) -> None:
    stale_fields = {
        "price": row["price_staleness_sessions"],
        "Momentum raw": row["momentum_raw_staleness_sessions"],
        "Momentum normalized": row["momentum_normalized_staleness_sessions"],
        "Volatility raw": row["volatility_raw_staleness_sessions"],
        "Volatility normalized": row["volatility_normalized_staleness_sessions"],
    }
    stale = [
        f"{label}: {_session_count_text(value)}"
        for label, value in stale_fields.items()
        if not _is_missing(value) and int(value) > 0
    ]
    missing = [label for label, value in stale_fields.items() if _is_missing(value)]
    if stale:
        st.warning("Stale at the fixed target — " + "; ".join(stale))
    if missing:
        st.warning("No current vintage available for: " + ", ".join(missing) + ".")


def _latest_row(frame: pd.DataFrame, target: pd.Timestamp) -> pd.Series | None:
    rows = frame.loc[frame["signal_date"].eq(target)]
    return None if rows.empty else rows.iloc[0]


def _momentum_observation_table(row: pd.Series | None) -> pd.DataFrame:
    if row is None:
        return pd.DataFrame(
            [{"Observation": "Current Momentum", "Value": MISSING_TEXT}]
        )
    return pd.DataFrame(
        {
            "Field": [
                "Observation date",
                "252-session endpoint start",
                "252-session endpoint end",
                "First prospective-use session",
                "Endpoint eligible",
                "Endpoint status",
                "Raw Momentum",
                "Simple return",
                "Own-history percentile",
                "Normalization reference count",
                "Interior missing rows",
                "Interior missing-row dates",
                "Interior missing adjusted closes",
                "Interior missing-adjusted-close dates",
            ],
            "Value": [
                _date_text(row["signal_date"]),
                _date_text(row["endpoint_start_date"]),
                _date_text(row["endpoint_end_date"]),
                _date_text(row["first_prospective_session"]),
                _bool_text(row["endpoint_eligible"]),
                _status_text(row["endpoint_status"]),
                _number_text(row["raw_momentum"]),
                _percent_text(row["simple_return_pct"]),
                _percentile_text(row["momentum_percentile"]),
                _integer_text(row["normalization_reference_count"]),
                _integer_text(row["interior_missing_row_count"]),
                _date_list_text(row["interior_missing_row_dates"]),
                _integer_text(row["interior_missing_adjusted_close_count"]),
                _date_list_text(row["interior_missing_adjusted_close_dates"]),
            ],
        }
    )


def _volatility_observation_table(row: pd.Series | None) -> pd.DataFrame:
    if row is None:
        return pd.DataFrame(
            [{"Observation": "Current Volatility", "Value": MISSING_TEXT}]
        )
    return pd.DataFrame(
        {
            "Field": [
                "Observation date",
                "21-return window start",
                "21-return window end",
                "First prospective-use session",
                "Window eligible",
                "Window status",
                "Raw annualized Volatility",
                "Annualized Volatility",
                "Own-history percentile",
                "Normalization reference count",
                "Missing rows in window",
                "Missing-row dates",
                "Missing adjusted closes in window",
                "Missing-adjusted-close dates",
            ],
            "Value": [
                _date_text(row["signal_date"]),
                _date_text(row["window_start_date"]),
                _date_text(row["window_end_date"]),
                _date_text(row["first_prospective_session"]),
                _bool_text(row["window_eligible"]),
                _status_text(row["window_status"]),
                _number_text(row["raw_annualized_volatility"]),
                _percent_text(row["annualized_volatility_pct"]),
                _percentile_text(row["volatility_percentile"]),
                _integer_text(row["normalization_reference_count"]),
                _integer_text(row["missing_row_count"]),
                _date_list_text(row["missing_row_dates"]),
                _integer_text(row["missing_adjusted_close_count"]),
                _date_list_text(row["missing_adjusted_close_dates"]),
            ],
        }
    )


def render_etf_detail(bundle: VerifiedSignalEvaluationBundle) -> None:
    """Render price and separate signal histories for one configured ETF."""

    st.header("ETF detail", anchor=HEADING_ANCHORS["detail"])
    tickers = tuple(definition.ticker for definition in bundle.universe)
    durable_ticker_key = _prepare_ticker_widget_state(bundle, tickers)
    labels = {
        definition.ticker: f"{definition.ticker} — {definition.name}"
        for definition in bundle.universe
    }
    ticker = st.selectbox(
        "Configured ETF",
        tickers,
        format_func=labels.__getitem__,
        key=_ETF_DETAIL_TICKER_WIDGET_KEY,
        on_change=_store_temporary_widget_value,
        args=(durable_ticker_key, _ETF_DETAIL_TICKER_WIDGET_KEY),
    )
    coverage = bundle.to_pandas("coverage")
    coverage_row = coverage.loc[coverage["ticker"].eq(ticker)].iloc[0]
    prices = bundle.to_pandas("input_prices")
    prices = prices.loc[prices["ticker"].eq(ticker)].reset_index(drop=True)
    momentum = bundle.to_pandas("momentum")
    momentum = momentum.loc[momentum["ticker"].eq(ticker)].reset_index(drop=True)
    volatility = bundle.to_pandas("volatility")
    volatility = volatility.loc[volatility["ticker"].eq(ticker)].reset_index(drop=True)
    target = bundle.target.target_session

    st.write(f"**{labels[ticker]}** · {coverage_row['category']}")
    st.caption(
        f"Exact target observation date: {_date_text(target)}. Values first become "
        "prospectively usable on the separately displayed following XNYS session."
    )
    if coverage_row["acquisition_status"] != "success":
        st.warning(
            "Acquisition status: "
            f"{coverage_row['acquisition_status']}; reason: "
            f"{_status_text(coverage_row['acquisition_error'])}."
        )
    st.subheader(
        "Current-vintage staleness",
        anchor=HEADING_ANCHORS["detail_staleness"],
    )
    st.caption(
        "Zero is shown explicitly for a fresh target-session value. Positive "
        "counts refer to an older observation and missing remains missing."
    )
    _render_table(build_staleness_table(coverage_row))
    _render_staleness_warning(coverage_row)

    st.subheader("Adjusted-price history", anchor=HEADING_ANCHORS["detail_price"])
    st.caption(
        "Purpose: display the verified adjusted-close input vintage. Known missing "
        "sessions and missing adjusted closes remain gaps; no values are filled."
    )
    missing_dates = cast(
        tuple[pd.Timestamp, ...], coverage_row["missing_canonical_dates"]
    )
    _render_figure(build_adjusted_price_figure(prices, missing_dates, ticker))
    price_diagnostics = pd.DataFrame(
        {
            "Field": [
                "First canonical date",
                "Last canonical date",
                "Expected XNYS observations",
                "Present canonical observations",
                "Missing canonical rows",
                "Missing canonical dates",
                "Missing adjusted closes",
                "Missing-adjusted-close dates",
                "Target price row present",
                "Target adjusted close present",
            ],
            "Value": [
                _date_text(coverage_row["first_canonical_date"]),
                _date_text(coverage_row["last_canonical_date"]),
                _integer_text(coverage_row["expected_xnys_observation_count"]),
                _integer_text(coverage_row["present_xnys_observation_count"]),
                _integer_text(coverage_row["missing_canonical_count"]),
                _date_list_text(coverage_row["missing_canonical_dates"]),
                _integer_text(coverage_row["missing_adjusted_close_count"]),
                _date_list_text(coverage_row["missing_adjusted_close_dates"]),
                _bool_text(coverage_row["target_price_row_present"]),
                _bool_text(coverage_row["target_adjusted_close_present"]),
            ],
        }
    )
    _render_table(price_diagnostics)

    st.subheader("Momentum — standalone", anchor=HEADING_ANCHORS["detail_momentum"])
    st.caption(
        "Purpose: show the raw 252-session Momentum series and its own-history "
        "percentile separately. Gaps are not connected."
    )
    _render_figure(
        _history_figure(
            momentum,
            date_column="signal_date",
            value_column="raw_momentum",
            title=f"{ticker} raw 252-session Momentum",
            yaxis_title="Raw Momentum (ratio)",
            color=MOMENTUM_COLOR,
            hover_format="%{y:,.6f}",
        )
    )
    _render_figure(
        _history_figure(
            momentum,
            date_column="signal_date",
            value_column="momentum_percentile",
            title=f"{ticker} Momentum own-history percentile",
            yaxis_title="Percentile / 100",
            color=MOMENTUM_COLOR,
            hover_format="%{y:,.2f} / 100",
        )
    )
    _render_table(_momentum_observation_table(_latest_row(momentum, target)))

    st.subheader("Volatility — standalone", anchor=HEADING_ANCHORS["detail_volatility"])
    st.caption(
        "Purpose: show annualized 21-return Volatility and its own-history "
        "percentile separately. Gaps are not connected."
    )
    _render_figure(
        _history_figure(
            volatility,
            date_column="signal_date",
            value_column="annualized_volatility_pct",
            title=f"{ticker} annualized 21-return Volatility",
            yaxis_title="Annualized Volatility (%)",
            color=VOLATILITY_COLOR,
            hover_format="%{y:,.2f}%",
        )
    )
    _render_figure(
        _history_figure(
            volatility,
            date_column="signal_date",
            value_column="volatility_percentile",
            title=f"{ticker} Volatility own-history percentile",
            yaxis_title="Percentile / 100",
            color=VOLATILITY_COLOR,
            hover_format="%{y:,.2f} / 100",
        )
    )
    _render_table(_volatility_observation_table(_latest_row(volatility, target)))


def _dependence_display(frame: pd.DataFrame) -> pd.DataFrame:
    display = frame.copy()
    for column in ("signal_date", "first_signal_date", "last_signal_date"):
        display[column] = display[column].map(_date_text)
    display["included_tickers"] = display["included_tickers"].map(_ticker_list_text)
    display["estimate"] = display["estimate"].map(
        lambda value: _number_text(value, decimals=6)
    )
    display["pair_count"] = display["pair_count"].map(_integer_text)
    return display.rename(
        columns={
            "scope": "Scope",
            "estimator": "Estimator",
            "ticker": "Ticker",
            "signal_date": "Signal date",
            "pair_count": "Pair count",
            "first_signal_date": "First paired date",
            "last_signal_date": "Last paired date",
            "included_tickers": "Included ticker population",
            "universe_status": "Universe status",
            "status": "Estimator status",
            "estimate": "Estimate",
        }
    )


def build_dependence_figure(
    per_session: pd.DataFrame, estimator: Literal["pearson", "spearman"]
) -> go.Figure:
    """Build one descriptive per-session dependence history chart.

    Args:
        per_session: Verified per-session dependence rows.
        estimator: Pearson or Spearman estimator to display.

    Returns:
        A neutral high-contrast line chart with estimator-specific dash style.
    """

    estimator_rows = per_session.loc[per_session["estimator"].eq(estimator)]
    figure = _history_figure(
        estimator_rows,
        date_column="signal_date",
        value_column="estimate",
        title=f"Per-session {estimator.title()} estimate",
        yaxis_title="Descriptive correlation estimate",
        color=DEPENDENCE_COLOR,
        hover_format="%{y:,.6f}",
        line_dash="solid" if estimator == "pearson" else "dash",
    )
    figure.update_yaxes(range=[-1, 1])
    return figure


def render_dependence(bundle: VerifiedSignalEvaluationBundle) -> None:
    """Render secondary descriptive dependence diagnostics."""

    st.header("Dependence diagnostics", anchor=HEADING_ANCHORS["dependence"])
    st.info(
        "Secondary descriptive diagnostics only. Pearson and Spearman estimates "
        "describe exact paired Momentum-percentile and Volatility-percentile "
        "observations. They do not justify combining the signals and are not "
        "predictive or causal evidence."
    )
    dependence = bundle.to_pandas("dependence")
    per_etf = dependence.loc[dependence["scope"].eq("per_etf")]
    per_session = dependence.loc[dependence["scope"].eq("per_session")]

    st.subheader(
        "Per-ETF time-series dependence",
        anchor=HEADING_ANCHORS["dependence_etf"],
    )
    st.caption(
        "Purpose: report each estimator's exact paired-date count and range for "
        "each ETF, including insufficient-pair and constant-input statuses."
    )
    selected_ticker = st.selectbox(
        "ETF dependence ticker",
        [definition.ticker for definition in bundle.universe],
    )
    _render_table(
        _dependence_display(per_etf.loc[per_etf["ticker"].eq(selected_ticker)])
    )

    st.subheader(
        "Per-session configured-universe dependence",
        anchor=HEADING_ANCHORS["dependence_session"],
    )
    st.caption(
        "Purpose: report each session's exact included population, pair count, "
        "estimator status, and full/partial universe label."
    )
    for estimator in ("pearson", "spearman"):
        _render_figure(build_dependence_figure(per_session, estimator))
    available_dates = tuple(
        pd.Timestamp(value) for value in per_session["signal_date"].drop_duplicates()
    )
    if not available_dates:
        st.info("No verified per-session dependence rows are available.")
        return
    durable_date_key = _prepare_dependence_date_widget_state(bundle, available_dates)
    selected_date = st.selectbox(
        "Dependence signal date",
        available_dates,
        format_func=_date_text,
        key=_DEPENDENCE_DATE_WIDGET_KEY,
        on_change=_store_temporary_widget_value,
        args=(durable_date_key, _DEPENDENCE_DATE_WIDGET_KEY),
    )
    selected_session = per_session.loc[per_session["signal_date"].eq(selected_date)]
    _render_table(_dependence_display(selected_session))


def _mapping(value: object) -> Mapping[str, object]:
    return cast(Mapping[str, object], value)


def _runtime_versions() -> pd.DataFrame:
    packages = ("streamlit", "plotly", "pandas", "pyarrow")
    rows: list[dict[str, str]] = [{"Package": "python", "Version": sys.version}]
    for package in packages:
        try:
            package_version = version(package)
        except PackageNotFoundError:
            package_version = "Unavailable"
        rows.append({"Package": package, "Version": package_version})
    return pd.DataFrame(rows)


def render_provenance(bundle: VerifiedSignalEvaluationBundle) -> None:
    """Render fixed-run provenance, verification evidence, and limitations."""

    st.header("Provenance and limitations", anchor=HEADING_ANCHORS["provenance"])
    manifest = bundle.manifest
    run_metadata = pd.DataFrame(
        {
            "Field": [
                "Run ID",
                "Target XNYS session",
                "Captured evaluation instant",
                "Request start",
                "Request end (exclusive)",
                "Evaluation mode",
                "Git commit",
                "Pre-run worktree dirty",
                "Effective universe definitions SHA-256",
                "Bundle verification",
                "Combined six-file snapshot SHA-256",
                "Manifest computed SHA-256",
            ],
            "Value": [
                bundle.run_id,
                _date_text(bundle.target.target_session),
                _instant_text(bundle.target.captured_at),
                bundle.target.request_start.isoformat(),
                bundle.target.request_end.isoformat(),
                str(manifest["mode"]),
                str(manifest["git_head"]),
                _bool_text(manifest["worktree_dirty"]),
                str(manifest["universe_config_sha256"]),
                "Verified from exact snapshotted bytes",
                bundle.content_sha256,
                bundle.manifest_sha256,
            ],
        }
    )
    _render_table(run_metadata)
    st.caption(
        "The stored manifest field `universe_config_sha256` is the SHA-256 of the "
        "deterministic JSON serialization of the effective parsed ETF definitions "
        "in configured order, not the raw YAML resource bytes."
    )

    st.subheader(
        "Evaluation package versions",
        anchor=HEADING_ANCHORS["provenance_packages"],
    )
    package_versions = _mapping(manifest["package_versions"])
    _render_table(
        pd.DataFrame(
            [
                {"Package": package, "Version": str(package_versions[package])}
                for package in sorted(package_versions)
            ]
        )
    )

    st.subheader(
        "Artifact hashes, schemas, and verification",
        anchor=HEADING_ANCHORS["provenance_artifacts"],
    )
    artifacts = _mapping(manifest["artifacts"])
    artifact_rows: list[dict[str, object]] = []
    schema_rows: list[dict[str, object]] = []
    for artifact in (
        "input_prices",
        "coverage",
        "momentum",
        "volatility",
        "dependence",
    ):
        raw_metadata = artifacts[artifact]
        metadata = _mapping(raw_metadata)
        schema = cast(tuple[Mapping[str, object], ...], metadata["schema"])
        artifact_rows.append(
            {
                "Filename": metadata["filename"],
                "Rows": _integer_text(metadata["row_count"]),
                "Schema": f"{len(schema):,} fields; matched manifest",
                "Expected SHA-256": metadata["sha256"],
                "Computed SHA-256": bundle.artifact_sha256[artifact],
                "Verification result": "Verified",
            }
        )
        schema_rows.extend(
            {
                "Artifact": artifact,
                "Field": field["name"],
                "Arrow type": field["type"],
                "Nullable": _bool_text(field["nullable"]),
            }
            for field in schema
        )
    artifact_rows.append(
        {
            "Filename": "manifest.json",
            "Rows": "Not applicable",
            "Schema": "Not applicable",
            "Expected SHA-256": "Not applicable — manifest has no self-hash",
            "Computed SHA-256": bundle.manifest_sha256,
            "Verification result": "Structure verified; digest computed; unsigned",
        }
    )
    _render_table(pd.DataFrame(artifact_rows))
    st.warning(
        "The manifest's computed SHA-256 is not internally self-authenticating: "
        "the unsigned manifest and all artifacts could be replaced together."
    )
    with st.expander("Verified Arrow schemas"):
        _render_table(pd.DataFrame(schema_rows))

    st.subheader(
        "Viewer runtime versions", anchor=HEADING_ANCHORS["provenance_runtime"]
    )
    _render_table(_runtime_versions())

    st.subheader(
        "Interpretation and use limitations",
        anchor=HEADING_ANCHORS["provenance_limitations"],
    )
    st.markdown(
        """
- **Fixed local run, not live:** the viewer never refreshes data and never treats an older run as current. The selected target date remains visible.
- **Current-vintage history:** historical adjusted prices and derived signals use the provider vintage captured by this run, not archived as-of vintages.
- **Not a point-in-time backtest:** the bundle does not prove every historical value was available in the same form on that historical date.
- **Survivorship bias:** the configured 24-ETF universe is the current curated universe applied across history.
- **Provider revisions:** the provider may revise historical adjusted prices; this fixed bundle records only its captured input vintage.
- **Rights unresolved:** redistribution and public display rights for provider data and derived displays remain unresolved. This local viewer grants no publication right.
- **Descriptive only:** Momentum, Volatility, and dependence diagnostics are standalone historical descriptions, not predictions, causes, recommendations, or evidence for combination.
- **Missing remains missing:** absent, failed, empty, stale, or ineligible observations are not replaced with zero, neutrality, another component, or an older value.
- **Deferred components:** Flow and Concentration remain deferred and unavailable in this dashboard.
        """
    )
    st.warning(
        "No composite, weights, thresholds, risk classes, missing-component "
        "reweighting, or Crowding Score exists. The local viewer also provides "
        "no traffic-light label or predictive signal."
    )


def render_verified_context(bundle: VerifiedSignalEvaluationBundle) -> None:
    """Render persistent fixed-run context after successful verification."""

    target_text = _date_text(bundle.target.target_session)
    captured_text = _instant_text(bundle.target.captured_at)
    st.success("Verification status: VERIFIED — all six exact file snapshots passed.")
    st.subheader("Verified run context", anchor=HEADING_ANCHORS["verified_context"])
    st.markdown(f"**Selected run:** {bundle.run_id}")
    st.markdown(f"**Fixed target date:** {target_text}")
    st.markdown(f"**Captured evaluation instant:** {captured_text}")
    st.info(
        "Local offline viewer · fixed run, not live · Momentum and Volatility "
        "remain separate standalone historical signals."
    )
    st.sidebar.subheader("Fixed run context", anchor=HEADING_ANCHORS["sidebar_context"])
    st.sidebar.markdown(f"**Selected run:** {bundle.run_id}")
    st.sidebar.markdown(f"**Fixed target date:** {target_text}")
    st.sidebar.markdown(f"**Captured evaluation instant:** {captured_text}")
    st.sidebar.caption("Verified fixed run; local and offline; not live market data.")
