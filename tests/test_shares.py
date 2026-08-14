"""Tests for ETF shares-outstanding provider ingestion and normalization."""

from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pandas as pd
import pytest

import etf_crowding.data.shares as shares_module
import scripts.update_shares as update_shares_script
from etf_crowding.data.numeric_dtypes import is_supported_canonical_numeric_dtype
from etf_crowding.data.share_validation import CANONICAL_SHARE_COLUMNS
from etf_crowding.data.shares import (
    SharesNormalizationError,
    download_shares_history,
    normalize_yfinance_shares_output,
)

RETRIEVED_AT = datetime(2026, 8, 14, 10, 0, tzinfo=UTC)


def _provider_series(
    values: list[object] | None = None,
    *,
    dates: tuple[str, ...] = ("2024-01-03", "2024-01-02"),
    dtype: str | None = None,
    timezone: str = "America/New_York",
) -> pd.Series:
    resolved_values = values if values is not None else [101_000_000, 100_000_000]
    index = pd.DatetimeIndex(dates, tz=timezone, name="Date")
    return pd.Series(resolved_values, index=index, dtype=dtype, name="shares_out")


def _raw_shares_payload(
    *,
    dates: tuple[str, ...] = ("2024-01-02", "2024-01-03"),
    values: list[object] | None = None,
    include_shares: bool = True,
) -> dict[str, object]:
    result: dict[str, object] = {
        "timestamp": [int(pd.Timestamp(value, tz="UTC").timestamp()) for value in dates]
    }
    if include_shares:
        result["shares_out"] = (
            values if values is not None else [100_000_000, 101_000_000]
        )
    return {
        "finance": {"error": None},
        "timeseries": {"result": [result]},
    }


def _install_raw_yfinance_ticker(
    monkeypatch: pytest.MonkeyPatch,
    payloads: list[object],
    *,
    captured: dict[str, object] | None = None,
    request_error: Exception | None = None,
    exchange_timezones: dict[str, str] | None = None,
) -> None:
    capture = captured if captured is not None else {}
    responses = iter(payloads)

    class FakeResponse:
        def __init__(self, payload: object) -> None:
            self._payload = payload

        def json(self) -> object:
            return self._payload

    class FakeData:
        def get(self, **kwargs: object) -> FakeResponse:
            capture["get_call_count"] = int(capture.get("get_call_count", 0)) + 1
            calls = capture.setdefault("request_calls", [])
            assert isinstance(calls, list)
            calls.append(dict(kwargs))
            capture["hide_exceptions_during_request"] = (
                shares_module.yf.config.debug.hide_exceptions
            )
            if request_error is not None:
                raise request_error
            return FakeResponse(next(responses))

        def cache_get(self, **kwargs: object) -> FakeResponse:
            capture["cache_get_call_count"] = (
                int(capture.get("cache_get_call_count", 0)) + 1
            )
            raise AssertionError(f"cache_get must not be used: {kwargs}")

    class FakeTicker:
        def __init__(self, ticker: str) -> None:
            capture["ticker"] = ticker
            self._ticker = ticker
            self._data = FakeData()

        def _get_ticker_tz(self, timeout: int) -> str:
            capture["timezone_timeout"] = timeout
            if exchange_timezones is None:
                return "America/New_York"
            return exchange_timezones[self._ticker]

    monkeypatch.setattr(shares_module.yf, "Ticker", FakeTicker)


def _captured_query_periods(captured: dict[str, object]) -> list[tuple[str, int, int]]:
    request_calls = captured["request_calls"]
    assert isinstance(request_calls, list)
    periods: list[tuple[str, int, int]] = []
    for request in request_calls:
        assert isinstance(request, dict)
        parameters = parse_qs(urlparse(str(request["url"])).query)
        periods.append(
            (
                parameters["symbol"][0],
                int(parameters["period1"][0]),
                int(parameters["period2"][0]),
            )
        )
    return periods


def test_runtime_yfinance_version_matches_inspected_shares_contract() -> None:
    assert shares_module.yf.__version__ == "1.5.2"


def test_normal_provider_series_is_normalized_to_canonical_schema() -> None:
    normalized = normalize_yfinance_shares_output(
        _provider_series(), "SPY", RETRIEVED_AT
    )

    assert tuple(normalized.columns) == CANONICAL_SHARE_COLUMNS
    assert normalized["date"].tolist() == [
        pd.Timestamp("2024-01-02"),
        pd.Timestamp("2024-01-03"),
    ]
    assert normalized["ticker"].tolist() == ["SPY", "SPY"]
    assert normalized["shares_outstanding"].tolist() == [
        100_000_000,
        101_000_000,
    ]
    assert (normalized["retrieved_at"] == pd.Timestamp(RETRIEVED_AT)).all()


def test_source_local_dates_are_preserved_without_timezone_shift() -> None:
    source = _provider_series(
        [100_000_000],
        dates=("2024-01-02 23:30",),
        timezone="America/New_York",
    )

    normalized = normalize_yfinance_shares_output(source, "SPY", RETRIEVED_AT)

    assert normalized["date"].tolist() == [pd.Timestamp("2024-01-02")]


def test_timezone_naive_provider_index_is_rejected() -> None:
    source = pd.Series(
        [100_000_000],
        index=pd.DatetimeIndex(["2024-01-02"]),
        dtype="int64",
    )

    with pytest.raises(SharesNormalizationError, match="timezone-aware"):
        normalize_yfinance_shares_output(source, "SPY", RETRIEVED_AT)


def test_non_datetime_provider_index_is_rejected() -> None:
    source = pd.Series([100_000_000], index=pd.Index([1]), dtype="int64")

    with pytest.raises(SharesNormalizationError, match="DatetimeIndex"):
        normalize_yfinance_shares_output(source, "SPY", RETRIEVED_AT)


@pytest.mark.parametrize(
    ("source", "dtype_fragment"),
    [
        pytest.param(_provider_series([True, False], dtype="bool"), "bool", id="bool"),
        pytest.param(
            _provider_series([True, pd.NA], dtype="boolean"),
            "boolean",
            id="nullable-bool",
        ),
        pytest.param(
            _provider_series([1 + 2j, 2 + 0j], dtype="complex128"),
            "complex128",
            id="complex",
        ),
        pytest.param(
            _provider_series(["100", "101"], dtype="string"),
            "string",
            id="string",
        ),
        pytest.param(
            _provider_series([100, 101], dtype="object"),
            "object",
            id="object",
        ),
        pytest.param(
            _provider_series(
                list(pd.to_datetime(["2024-01-02", "2024-01-03"])),
                dtype="datetime64[ns]",
            ),
            "datetime64",
            id="datetime",
        ),
        pytest.param(
            _provider_series(
                list(pd.to_timedelta([1, 2], unit="D")),
                dtype="timedelta64[ns]",
            ),
            "timedelta64",
            id="timedelta",
        ),
        pytest.param(
            _provider_series([100, 101], dtype="category"),
            "category",
            id="categorical",
        ),
    ],
)
def test_malformed_provider_shares_dtypes_are_rejected_before_casting(
    source: pd.Series,
    dtype_fragment: str,
) -> None:
    with pytest.raises(
        SharesNormalizationError,
        match=rf"shares_out.*real numeric dtype.*{dtype_fragment}",
    ):
        normalize_yfinance_shares_output(source, "SPY", RETRIEVED_AT)


@pytest.mark.parametrize("dtype", ["float64", "int64", "Float64", "Int64"])
def test_real_numeric_provider_dtypes_remain_valid(dtype: str) -> None:
    source = _provider_series([100_000_000, 101_000_000], dtype=dtype)

    normalized = normalize_yfinance_shares_output(source, "SPY", RETRIEVED_AT)

    assert str(normalized["shares_outstanding"].dtype) == dtype


def test_dated_missing_shares_are_preserved_including_all_missing_response() -> None:
    source = _provider_series([pd.NA, pd.NA], dtype="Float64")

    normalized = normalize_yfinance_shares_output(source, "SPY", RETRIEVED_AT)

    assert len(normalized) == 2
    assert normalized["shares_outstanding"].isna().all()


def test_download_sorts_tickers_and_dates_deterministically() -> None:
    result = download_shares_history(
        ["SPY", "QQQ"],
        downloader=lambda ticker, start, end: _provider_series(),
        retrieved_at=RETRIEVED_AT,
    )

    assert list(zip(result.shares["ticker"], result.shares["date"], strict=True)) == [
        ("QQQ", pd.Timestamp("2024-01-02")),
        ("QQQ", pd.Timestamp("2024-01-03")),
        ("SPY", pd.Timestamp("2024-01-02")),
        ("SPY", pd.Timestamp("2024-01-03")),
    ]


@pytest.mark.parametrize(
    ("tickers", "dtypes"),
    [
        (
            ["SPY", "QQQ"],
            {"SPY": "int64[pyarrow]", "QQQ": "Int64"},
        ),
        (
            ["SPY", "QQQ"],
            {"SPY": "Int64", "QQQ": "int64[pyarrow]"},
        ),
        (
            ["QQQ", "SPY"],
            {"SPY": "int64[pyarrow]", "QQQ": "Int64"},
        ),
    ],
    ids=["arrow-then-pandas", "pandas-then-arrow", "reversed-ticker-order"],
)
def test_download_harmonizes_mixed_integer_ticker_backends_losslessly(
    tickers: list[str],
    dtypes: dict[str, str],
) -> None:
    values = {
        "SPY": 9_007_199_254_740_993,
        "QQQ": 9_007_199_254_740_995,
    }

    result = download_shares_history(
        tickers,
        downloader=lambda ticker, start, end: _provider_series(
            [values[ticker]], dates=("2024-01-02",), dtype=dtypes[ticker]
        ),
        retrieved_at=RETRIEVED_AT,
    )

    values_by_ticker = dict(
        zip(
            result.shares["ticker"],
            result.shares["shares_outstanding"],
            strict=True,
        )
    )
    assert values_by_ticker == values
    assert is_supported_canonical_numeric_dtype(
        result.shares["shares_outstanding"].dtype
    )
    assert result.shares["shares_outstanding"].dtype != object
    assert (result.shares["retrieved_at"] == pd.Timestamp(RETRIEVED_AT)).all()
    assert set(result.successful_tickers) == {"SPY", "QQQ"}


@pytest.mark.parametrize(
    "dtypes",
    [
        {"SPY": "double[pyarrow]", "QQQ": "Float64"},
        {"SPY": "Float64", "QQQ": "double[pyarrow]"},
        {"SPY": "float64", "QQQ": "Float64"},
    ],
    ids=["arrow-then-pandas", "pandas-then-arrow", "numpy-then-pandas"],
)
def test_download_harmonizes_mixed_floating_ticker_backends_losslessly(
    dtypes: dict[str, str],
) -> None:
    values = {"SPY": 100.25, "QQQ": 101.5}

    result = download_shares_history(
        ["SPY", "QQQ"],
        downloader=lambda ticker, start, end: _provider_series(
            [values[ticker]], dates=("2024-01-02",), dtype=dtypes[ticker]
        ),
        retrieved_at=RETRIEVED_AT,
    )

    values_by_ticker = dict(
        zip(
            result.shares["ticker"],
            result.shares["shares_outstanding"],
            strict=True,
        )
    )
    assert values_by_ticker == values
    assert is_supported_canonical_numeric_dtype(
        result.shares["shares_outstanding"].dtype
    )
    assert result.shares["shares_outstanding"].dtype != object


def test_download_preserves_missing_values_across_mixed_ticker_backends() -> None:
    values: dict[str, object] = {"SPY": pd.NA, "QQQ": 100}
    dtypes = {"SPY": "int64[pyarrow]", "QQQ": "Int64"}

    result = download_shares_history(
        ["SPY", "QQQ"],
        downloader=lambda ticker, start, end: _provider_series(
            [values[ticker]], dates=("2024-01-02",), dtype=dtypes[ticker]
        ),
        retrieved_at=RETRIEVED_AT,
    )

    spy_value = result.shares.loc[
        result.shares["ticker"].eq("SPY"), "shares_outstanding"
    ].iloc[0]
    qqq_value = result.shares.loc[
        result.shares["ticker"].eq("QQQ"), "shares_outstanding"
    ].iloc[0]
    assert pd.isna(spy_value)
    assert qqq_value == 100
    assert is_supported_canonical_numeric_dtype(
        result.shares["shares_outstanding"].dtype
    )


def test_download_rejects_ticker_dtypes_without_lossless_union() -> None:
    values = {"SPY": 9_007_199_254_740_993, "QQQ": 100.25}
    dtypes = {"SPY": "Int64", "QQQ": "Float64"}

    with pytest.raises(
        SharesNormalizationError,
        match=r"cannot be combined losslessly.*Int64.*Float64",
    ):
        download_shares_history(
            ["SPY", "QQQ"],
            downloader=lambda ticker, start, end: _provider_series(
                [values[ticker]], dates=("2024-01-02",), dtype=dtypes[ticker]
            ),
            retrieved_at=RETRIEVED_AT,
        )


@pytest.mark.parametrize(
    "sparse_dtype",
    [
        pd.SparseDtype("int64", 0),
        pd.SparseDtype("float64", float("nan")),
    ],
    ids=["sparse-int64", "sparse-float64"],
)
def test_sparse_provider_ticker_is_failed_before_batch_concat(
    sparse_dtype: pd.SparseDtype,
) -> None:
    result = download_shares_history(
        ["SPY"],
        downloader=lambda ticker, start, end: pd.Series(
            [100],
            index=pd.DatetimeIndex(["2024-01-02"], tz="America/New_York"),
            dtype=sparse_dtype,
            name="shares_out",
        ),
        retrieved_at=RETRIEVED_AT,
    )

    assert result.failed_tickers == ("SPY",)
    assert result.shares.empty
    assert "unsupported real numeric dtype Sparse" in (result.statuses[0].error or "")


def test_empty_ticker_response_is_reported_without_rows() -> None:
    result = download_shares_history(
        ["SPY"],
        downloader=lambda ticker, start, end: None,
        retrieved_at=RETRIEVED_AT,
    )

    assert result.empty_tickers == ("SPY",)
    assert result.failed_tickers == ()
    assert result.shares.empty
    assert result.statuses[0].retrieved_at == RETRIEVED_AT


def test_failed_ticker_does_not_discard_valid_ticker() -> None:
    def fake_download(
        ticker: str, start: date | None, end: date | None
    ) -> pd.Series | None:
        del start, end
        if ticker == "QQQ":
            raise ConnectionError("synthetic provider outage")
        return _provider_series()

    result = download_shares_history(
        ["SPY", "QQQ"],
        downloader=fake_download,
        retrieved_at=RETRIEVED_AT,
    )

    assert result.successful_tickers == ("SPY",)
    assert result.failed_tickers == ("QQQ",)
    assert result.shares["ticker"].unique().tolist() == ["SPY"]
    assert result.statuses[1].retrieved_at is None
    assert result.statuses[1].error == "ConnectionError: synthetic provider outage"


def test_malformed_ticker_does_not_discard_valid_ticker() -> None:
    def fake_download(
        ticker: str, start: date | None, end: date | None
    ) -> pd.Series | None:
        del start, end
        if ticker == "QQQ":
            return pd.Series(
                ["100", "101"],
                index=pd.DatetimeIndex(
                    ["2024-01-02", "2024-01-03"], tz="America/New_York"
                ),
                dtype="string",
            )
        return _provider_series()

    result = download_shares_history(
        ["SPY", "QQQ"],
        downloader=fake_download,
        retrieved_at=RETRIEVED_AT,
    )

    assert result.successful_tickers == ("SPY",)
    assert result.failed_tickers == ("QQQ",)
    assert "received string" in (result.statuses[1].error or "")
    assert not result.shares["ticker"].eq("QQQ").any()


def test_non_series_provider_response_is_failed_not_empty() -> None:
    result = download_shares_history(
        ["SPY"],
        downloader=lambda ticker, start, end: pd.DataFrame(),  # type: ignore[return-value]
        retrieved_at=RETRIEVED_AT,
    )

    assert result.failed_tickers == ("SPY",)
    assert result.empty_tickers == ()
    assert "must be a pandas Series" in (result.statuses[0].error or "")


def test_clock_is_consulted_after_downloader_returns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    def fake_download(ticker: str, start: date | None, end: date | None) -> pd.Series:
        del ticker, start, end
        events.append("download-return")
        return _provider_series()

    def fake_clock() -> datetime:
        assert events == ["download-return"]
        events.append("clock")
        return RETRIEVED_AT

    monkeypatch.setattr(shares_module, "_utc_now", fake_clock)

    result = download_shares_history(["SPY"], downloader=fake_download)

    assert events == ["download-return", "clock"]
    assert (result.shares["retrieved_at"] == pd.Timestamp(RETRIEVED_AT)).all()


def test_explicit_retrieved_at_is_exact_override_and_must_be_aware(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        shares_module,
        "_utc_now",
        lambda: (_ for _ in ()).throw(AssertionError("clock must not run")),
    )
    result = download_shares_history(
        ["SPY"],
        downloader=lambda ticker, start, end: _provider_series(),
        retrieved_at=RETRIEVED_AT,
    )
    assert result.retrieved_at == RETRIEVED_AT
    assert (result.shares["retrieved_at"] == pd.Timestamp(RETRIEVED_AT)).all()

    with pytest.raises(ValueError, match="retrieved_at must be timezone-aware"):
        download_shares_history(
            ["SPY"],
            downloader=lambda ticker, start, end: _provider_series(),
            retrieved_at=datetime(2026, 8, 14, 10, 0),
        )


def test_provider_query_bounds_are_forwarded_without_local_filtering() -> None:
    captured: dict[str, object] = {}

    def fake_download(ticker: str, start: date | None, end: date | None) -> pd.Series:
        captured.update(ticker=ticker, start=start, end=end)
        return _provider_series(
            [99_000_000],
            dates=("2023-12-31",),
        )

    result = download_shares_history(
        ["SPY"],
        start="2024-01-01",
        end="2024-02-01",
        downloader=fake_download,
        retrieved_at=RETRIEVED_AT,
    )

    assert captured == {
        "ticker": "SPY",
        "start": date(2024, 1, 1),
        "end": date(2024, 2, 1),
    }
    assert result.shares["date"].tolist() == [pd.Timestamp("2023-12-31")]


def test_omitted_bounds_use_one_batch_reference_and_keep_response_timestamps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    _install_raw_yfinance_ticker(
        monkeypatch,
        [_raw_shares_payload(), _raw_shares_payload()],
        captured=captured,
    )
    reference_times = iter(
        [
            datetime(2026, 8, 15, 3, 59, tzinfo=UTC),
            datetime(2026, 8, 16, 3, 59, tzinfo=UTC),
        ]
    )
    query_clock_calls = 0

    def query_clock() -> datetime:
        nonlocal query_clock_calls
        query_clock_calls += 1
        return next(reference_times)

    response_times = iter(
        [
            datetime(2026, 8, 15, 3, 59, 30, tzinfo=UTC),
            datetime(2026, 8, 15, 4, 0, 30, tzinfo=UTC),
        ]
    )
    monkeypatch.setattr(shares_module, "_query_reference_time", query_clock)
    monkeypatch.setattr(shares_module, "_utc_now", lambda: next(response_times))

    result = download_shares_history(["SPY", "QQQ"])

    assert query_clock_calls == 1
    periods = _captured_query_periods(captured)
    assert periods[0][1:] == periods[1][1:]
    local_reference = pd.Timestamp("2026-08-15T03:59:00Z").tz_convert(
        "America/New_York"
    )
    assert periods[0][1] == int(
        (local_reference - pd.Timedelta(days=548)).floor("D").timestamp()
    )
    assert periods[0][2] == int(local_reference.ceil("D").timestamp())
    assert [status.retrieved_at for status in result.statuses] == [
        datetime(2026, 8, 15, 3, 59, 30, tzinfo=UTC),
        datetime(2026, 8, 15, 4, 0, 30, tzinfo=UTC),
    ]


def test_same_batch_reference_is_converted_per_timezone_and_order_independent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    _install_raw_yfinance_ticker(
        monkeypatch,
        [_raw_shares_payload()] * 4,
        captured=captured,
        exchange_timezones={"SPY": "America/New_York", "EWJ": "Asia/Tokyo"},
    )
    batch_reference = datetime(2026, 8, 15, 0, 30, tzinfo=UTC)
    query_clock_calls = 0

    def query_clock() -> datetime:
        nonlocal query_clock_calls
        query_clock_calls += 1
        return batch_reference

    monkeypatch.setattr(shares_module, "_query_reference_time", query_clock)

    download_shares_history(
        ["SPY", "EWJ"],
        retrieved_at=RETRIEVED_AT,
    )
    download_shares_history(
        ["EWJ", "SPY"],
        retrieved_at=RETRIEVED_AT,
    )

    assert query_clock_calls == 2
    periods = _captured_query_periods(captured)
    first_order = {ticker: (start, end) for ticker, start, end in periods[:2]}
    reversed_order = {ticker: (start, end) for ticker, start, end in periods[2:]}
    assert first_order == reversed_order
    for ticker, timezone in {
        "SPY": "America/New_York",
        "EWJ": "Asia/Tokyo",
    }.items():
        local_reference = pd.Timestamp(batch_reference).tz_convert(timezone)
        assert first_order[ticker] == (
            int((local_reference - pd.Timedelta(days=548)).floor("D").timestamp()),
            int(local_reference.ceil("D").timestamp()),
        )


def test_explicit_end_bypasses_automatic_query_reference_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    _install_raw_yfinance_ticker(
        monkeypatch,
        [_raw_shares_payload()],
        captured=captured,
    )
    monkeypatch.setattr(
        shares_module,
        "_query_reference_time",
        lambda: (_ for _ in ()).throw(
            AssertionError("query-reference clock must not run")
        ),
    )

    download_shares_history(
        ["SPY"],
        end="2024-02-01",
        retrieved_at=RETRIEVED_AT,
    )

    _, period1, period2 = _captured_query_periods(captured)[0]
    explicit_end = pd.Timestamp("2024-02-01", tz="America/New_York")
    assert period1 == int(
        (explicit_end - pd.Timedelta(days=548)).floor("D").timestamp()
    )
    assert period2 == int(explicit_end.ceil("D").timestamp())


def test_explicit_start_and_omitted_end_share_one_batch_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    _install_raw_yfinance_ticker(
        monkeypatch,
        [_raw_shares_payload(), _raw_shares_payload()],
        captured=captured,
    )
    batch_reference = datetime(2026, 8, 15, 3, 59, tzinfo=UTC)
    query_clock_calls = 0

    def query_clock() -> datetime:
        nonlocal query_clock_calls
        query_clock_calls += 1
        return batch_reference

    monkeypatch.setattr(shares_module, "_query_reference_time", query_clock)

    download_shares_history(
        ["SPY", "QQQ"],
        start="2024-01-01",
        retrieved_at=RETRIEVED_AT,
    )

    assert query_clock_calls == 1
    periods = _captured_query_periods(captured)
    expected_start = int(pd.Timestamp("2024-01-01", tz="America/New_York").timestamp())
    expected_end = int(
        pd.Timestamp(batch_reference)
        .tz_convert("America/New_York")
        .ceil("D")
        .timestamp()
    )
    assert [(start, end) for _, start, end in periods] == [
        (expected_start, expected_end),
        (expected_start, expected_end),
    ]


def test_raw_adapter_uses_uncached_yfinance_transport_and_exact_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    _install_raw_yfinance_ticker(
        monkeypatch,
        [_raw_shares_payload()],
        captured=captured,
    )
    monkeypatch.setattr(shares_module.yf.config.debug, "hide_exceptions", True)

    result = download_shares_history(
        ["SPY"],
        start="2024-01-01",
        end="2024-02-01",
        retrieved_at=RETRIEVED_AT,
    )

    assert result.successful_tickers == ("SPY",)
    assert captured["ticker"] == "SPY"
    assert captured["timezone_timeout"] == 10
    assert captured["get_call_count"] == 1
    assert captured.get("cache_get_call_count", 0) == 0
    request_calls = captured["request_calls"]
    assert isinstance(request_calls, list)
    request = request_calls[0]
    assert request["timeout"] == 30
    url = str(request["url"])
    assert "/ws/fundamentals-timeseries/v1/finance/timeseries/SPY" in url
    assert "symbol=SPY" in url
    expected_start = int(pd.Timestamp("2024-01-01", tz="America/New_York").timestamp())
    expected_end = int(pd.Timestamp("2024-02-01", tz="America/New_York").timestamp())
    assert f"period1={expected_start}" in url
    assert f"period2={expected_end}" in url
    assert captured["hide_exceptions_during_request"] is False
    assert shares_module.yf.config.debug.hide_exceptions is True


def test_identical_raw_requests_fetch_twice_and_observe_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    _install_raw_yfinance_ticker(
        monkeypatch,
        [
            _raw_shares_payload(values=[100_000_000, 101_000_000]),
            _raw_shares_payload(values=[100_500_000, 101_500_000]),
        ],
        captured=captured,
    )

    first = download_shares_history(
        ["SPY"],
        start="2024-01-01",
        end="2024-02-01",
        retrieved_at=RETRIEVED_AT,
    )
    second_timestamp = datetime(2026, 8, 14, 10, 1, tzinfo=UTC)
    second = download_shares_history(
        ["SPY"],
        start="2024-01-01",
        end="2024-02-01",
        retrieved_at=second_timestamp,
    )

    assert first.shares["shares_outstanding"].tolist() == [100_000_000, 101_000_000]
    assert second.shares["shares_outstanding"].tolist() == [100_500_000, 101_500_000]
    assert (second.shares["retrieved_at"] == pd.Timestamp(second_timestamp)).all()
    assert captured["get_call_count"] == 2
    assert captured.get("cache_get_call_count", 0) == 0
    request_calls = captured["request_calls"]
    assert isinstance(request_calls, list)
    assert request_calls[0] == request_calls[1]


def test_raw_null_shares_are_preserved_as_nullable_numeric(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_raw_yfinance_ticker(
        monkeypatch,
        [_raw_shares_payload(values=[None, 101_000_000])],
    )

    result = download_shares_history(
        ["SPY"],
        start="2024-01-01",
        end="2024-02-01",
        retrieved_at=RETRIEVED_AT,
    )

    assert result.successful_tickers == ("SPY",)
    assert pd.isna(result.shares["shares_outstanding"].iloc[0])
    assert result.shares["shares_outstanding"].iloc[1] == 101_000_000
    assert str(result.shares["shares_outstanding"].dtype) == "Int64"


def test_raw_explicit_zero_is_rejected_not_converted_to_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_raw_yfinance_ticker(
        monkeypatch,
        [_raw_shares_payload(values=[100_000_000, 0])],
    )

    result = download_shares_history(
        ["SPY"],
        start="2024-01-01",
        end="2024-02-01",
        retrieved_at=RETRIEVED_AT,
    )

    assert result.failed_tickers == ("SPY",)
    assert "must be positive" in (result.statuses[0].error or "")
    assert result.shares.empty


def test_raw_numeric_string_is_malformed_not_coerced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_raw_yfinance_ticker(
        monkeypatch,
        [_raw_shares_payload(values=["100000000", "101000000"])],
    )

    result = download_shares_history(
        ["SPY"],
        start="2024-01-01",
        end="2024-02-01",
        retrieved_at=RETRIEVED_AT,
    )

    assert result.failed_tickers == ("SPY",)
    assert "Malformed yfinance shares_out value" in (result.statuses[0].error or "")
    assert result.shares.empty


def test_raw_missing_shares_field_is_genuine_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_raw_yfinance_ticker(
        monkeypatch,
        [_raw_shares_payload(include_shares=False)],
    )

    result = download_shares_history(
        ["SPY"],
        start="2024-01-01",
        end="2024-02-01",
        retrieved_at=RETRIEVED_AT,
    )

    assert result.empty_tickers == ("SPY",)
    assert result.failed_tickers == ()


def test_raw_provider_error_and_malformed_payload_are_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_raw_yfinance_ticker(
        monkeypatch,
        [
            {"finance": {"error": {"code": "Bad Request"}}},
            {"timeseries": {"result": "invalid"}},
        ],
    )

    provider_error = download_shares_history(
        ["SPY"],
        start="2024-01-01",
        end="2024-02-01",
        retrieved_at=RETRIEVED_AT,
    )
    malformed = download_shares_history(
        ["SPY"],
        start="2024-01-01",
        end="2024-02-01",
        retrieved_at=RETRIEVED_AT,
    )

    assert provider_error.failed_tickers == ("SPY",)
    assert "Yahoo shares request failed" in (provider_error.statuses[0].error or "")
    assert malformed.failed_tickers == ("SPY",)
    assert "invalid result list" in (malformed.statuses[0].error or "")


def test_raw_empty_result_list_is_malformed_under_pinned_parser_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_raw_yfinance_ticker(
        monkeypatch,
        [{"finance": {"error": None}, "timeseries": {"result": []}}],
    )

    result = download_shares_history(
        ["SPY"],
        start="2024-01-01",
        end="2024-02-01",
        retrieved_at=RETRIEVED_AT,
    )

    assert result.failed_tickers == ("SPY",)
    assert result.empty_tickers == ()
    assert "empty result list" in (result.statuses[0].error or "")


def test_raw_transport_exception_is_failed_and_config_is_restored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    _install_raw_yfinance_ticker(
        monkeypatch,
        [_raw_shares_payload()],
        captured=captured,
        request_error=TimeoutError("synthetic provider timeout"),
    )
    monkeypatch.setattr(shares_module.yf.config.debug, "hide_exceptions", True)

    result = download_shares_history(
        ["SPY"],
        start="2024-01-01",
        end="2024-02-01",
        retrieved_at=RETRIEVED_AT,
    )

    assert result.failed_tickers == ("SPY",)
    assert "synthetic provider timeout" in (result.statuses[0].error or "")
    assert captured["hide_exceptions_during_request"] is False
    assert shares_module.yf.config.debug.hide_exceptions is True


def test_cli_partial_batch_uses_custom_output_without_timestamp_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    canonical = normalize_yfinance_shares_output(
        _provider_series(), "SPY", RETRIEVED_AT
    )

    def fake_download(**kwargs: object) -> SimpleNamespace:
        captured["download"] = kwargs
        return SimpleNamespace(
            shares=canonical,
            successful_tickers=("SPY",),
            empty_tickers=(),
            failed_tickers=("QQQ",),
        )

    def fake_persist(
        shares: pd.DataFrame,
        output_path: Path,
        *,
        snapshot_dir: Path,
    ) -> SimpleNamespace:
        captured["shares"] = shares
        captured["output_path"] = output_path
        captured["snapshot_dir"] = snapshot_dir
        return SimpleNamespace(
            shares=shares,
            revised_row_count=0,
            revised_tickers=(),
            snapshot_path=None,
        )

    monkeypatch.setattr(
        update_shares_script,
        "load_etf_universe",
        lambda: (SimpleNamespace(ticker="SPY"), SimpleNamespace(ticker="QQQ")),
    )
    monkeypatch.setattr(update_shares_script, "download_shares_history", fake_download)
    monkeypatch.setattr(update_shares_script, "persist_shares_history", fake_persist)
    output_path = tmp_path / "custom" / "shares.parquet"

    exit_code = update_shares_script.main(
        [
            "--start",
            "2024-01-01",
            "--end",
            "2024-02-01",
            "--output",
            str(output_path),
        ]
    )

    assert exit_code == 0
    download_arguments = captured["download"]
    assert isinstance(download_arguments, dict)
    assert download_arguments == {
        "tickers": ("SPY", "QQQ"),
        "start": "2024-01-01",
        "end": "2024-02-01",
    }
    assert "retrieved_at" not in download_arguments
    assert captured["output_path"] == output_path


def test_cli_all_empty_batch_does_not_replace_existing_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_path = tmp_path / "shares.parquet"
    output_path.write_bytes(b"existing canonical bytes")
    persist_called = False

    def fail_if_persisted(*args: object, **kwargs: object) -> None:
        nonlocal persist_called
        del args, kwargs
        persist_called = True

    monkeypatch.setattr(
        update_shares_script,
        "load_etf_universe",
        lambda: (SimpleNamespace(ticker="SPY"),),
    )
    monkeypatch.setattr(
        update_shares_script,
        "download_shares_history",
        lambda **kwargs: SimpleNamespace(
            shares=pd.DataFrame(),
            successful_tickers=(),
            empty_tickers=("SPY",),
            failed_tickers=(),
        ),
    )
    monkeypatch.setattr(
        update_shares_script, "persist_shares_history", fail_if_persisted
    )

    exit_code = update_shares_script.main(["--output", str(output_path)])

    assert exit_code == 1
    assert persist_called is False
    assert output_path.read_bytes() == b"existing canonical bytes"


def test_cli_all_failed_batch_does_not_replace_existing_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_path = tmp_path / "shares.parquet"
    output_path.write_bytes(b"existing canonical bytes")
    persist_called = False

    def fail_if_persisted(*args: object, **kwargs: object) -> None:
        nonlocal persist_called
        del args, kwargs
        persist_called = True

    monkeypatch.setattr(
        update_shares_script,
        "load_etf_universe",
        lambda: (SimpleNamespace(ticker="SPY"),),
    )
    monkeypatch.setattr(
        update_shares_script,
        "download_shares_history",
        lambda **kwargs: SimpleNamespace(
            shares=pd.DataFrame(),
            successful_tickers=(),
            empty_tickers=(),
            failed_tickers=("SPY",),
        ),
    )
    monkeypatch.setattr(
        update_shares_script, "persist_shares_history", fail_if_persisted
    )

    exit_code = update_shares_script.main(["--output", str(output_path)])

    assert exit_code == 1
    assert persist_called is False
    assert output_path.read_bytes() == b"existing canonical bytes"
