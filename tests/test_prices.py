"""Tests for ETF price download orchestration and yfinance normalization."""

from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

import etf_crowding.data.prices as price_module
import scripts.update_prices as update_prices_script
from etf_crowding.data.prices import (
    PriceNormalizationError,
    download_price_history,
    get_default_price_end_date,
    normalize_yfinance_output,
    persist_price_history,
)
from etf_crowding.data.validation import (
    CANONICAL_PRICE_COLUMNS,
    PRICE_VALUE_COLUMNS,
    PriceDataValidationError,
)

RETRIEVED_AT = datetime(2026, 8, 13, 12, 30, tzinfo=UTC)
PROVIDER_FIELDS = ("Open", "High", "Low", "Close", "Adj Close", "Volume")


def test_runtime_yfinance_version_matches_pinned_provider_contract() -> None:
    assert price_module.yf.__version__ == "1.5.2"


def _single_ticker_frame(
    *,
    index: pd.DatetimeIndex | None = None,
    include_adjusted_close: bool = True,
) -> pd.DataFrame:
    resolved_index = (
        index
        if index is not None
        else pd.DatetimeIndex(["2024-01-02", "2024-01-03"], name="Date")
    )
    row_count = len(resolved_index)
    frame = pd.DataFrame(
        {
            "Open": [100.0, 102.0][:row_count],
            "High": [103.0, 105.0][:row_count],
            "Low": [99.0, 101.0][:row_count],
            "Close": [102.0, 104.0][:row_count],
            "Adj Close": [101.5, 103.5][:row_count],
            "Volume": [1_000_000, 1_200_000][:row_count],
        },
        index=resolved_index,
    )
    if not include_adjusted_close:
        frame = frame.drop(columns="Adj Close")
    return frame


def _raw_chart_payload(
    *,
    dates: tuple[str, ...] = ("2024-01-02", "2024-01-03"),
    quote_values: dict[str, list[object]] | None = None,
    adjusted_close_values: list[object] | None = None,
    include_adjusted_close: bool = True,
    symbol: object = "SPY",
) -> dict[str, object]:
    row_count = len(dates)
    resolved_quote_values = quote_values or {
        "open": [100.0, 102.0][:row_count],
        "high": [103.0, 105.0][:row_count],
        "low": [99.0, 101.0][:row_count],
        "close": [102.0, 104.0][:row_count],
        "volume": [1_000_000.0, 1_200_000.0][:row_count],
    }
    result: dict[str, object] = {
        "meta": {
            "symbol": symbol,
            "exchangeTimezoneName": "America/New_York",
        },
        "timestamp": [
            int(pd.Timestamp(value, tz="America/New_York").timestamp())
            for value in dates
        ],
        "indicators": {"quote": [resolved_quote_values]},
    }
    if include_adjusted_close:
        indicators = result["indicators"]
        assert isinstance(indicators, dict)
        indicators["adjclose"] = [
            {
                "adjclose": adjusted_close_values
                if adjusted_close_values is not None
                else [101.5, 103.5][:row_count]
            }
        ]
    return {"chart": {"result": [result], "error": None}}


def _install_raw_yfinance_ticker(
    monkeypatch: pytest.MonkeyPatch,
    payload: object,
    *,
    payload_sequence: list[object] | None = None,
    request_error: Exception | None = None,
    captured: dict[str, object] | None = None,
) -> None:
    capture = captured if captured is not None else {}
    responses = iter(payload_sequence if payload_sequence is not None else [payload])

    class FakeResponse:
        text = ""

        def __init__(self, response_payload: object) -> None:
            self._payload = response_payload

        def json(self) -> object:
            return self._payload

    class FakeData:
        def _request(self, response_payload: object, **kwargs: object) -> FakeResponse:
            capture.update(kwargs)
            capture["hide_exceptions_during_request"] = (
                price_module.yf.config.debug.hide_exceptions
            )
            if request_error is not None:
                raise request_error
            return FakeResponse(response_payload)

        def get(self, **kwargs: object) -> FakeResponse:
            capture["request_method"] = "get"
            capture["get_call_count"] = int(capture.get("get_call_count", 0)) + 1
            request_calls = capture.setdefault("request_calls", [])
            assert isinstance(request_calls, list)
            request_calls.append(dict(kwargs))
            return self._request(next(responses), **kwargs)

        def cache_get(self, **kwargs: object) -> FakeResponse:
            capture["request_method"] = "cache_get"
            capture["cache_get_call_count"] = (
                int(capture.get("cache_get_call_count", 0)) + 1
            )
            raise AssertionError(f"cache_get must not be used: {kwargs}")

    class FakeTicker:
        def __init__(self, ticker: str) -> None:
            capture["ticker"] = ticker
            self._data = FakeData()

        def _get_ticker_tz(self, timeout: int) -> str:
            capture["timezone_timeout"] = timeout
            return "America/New_York"

    monkeypatch.setattr(price_module.yf, "Ticker", FakeTicker)


def _multi_ticker_frame(field_level_first: bool = True) -> pd.DataFrame:
    index = pd.DatetimeIndex(["2024-01-03", "2024-01-02"], name="Date")
    tickers = ("SPY", "QQQ")
    columns = pd.MultiIndex.from_product(
        [PROVIDER_FIELDS, tickers], names=["Price", "Ticker"]
    )
    values: list[list[float]] = []
    for day_offset in (1.0, 0.0):
        row: list[float] = []
        for field, ticker in columns:
            ticker_offset = 20.0 if ticker == "QQQ" else 0.0
            base_values = {
                "Open": 100.0,
                "High": 103.0,
                "Low": 99.0,
                "Close": 102.0,
                "Adj Close": 101.5,
                "Volume": 1_000_000.0,
            }
            row.append(base_values[field] + ticker_offset + day_offset)
        values.append(row)

    frame = pd.DataFrame(values, index=index, columns=columns)
    if field_level_first:
        return frame
    return frame.swaplevel(0, 1, axis=1).sort_index(axis=1)


def test_flat_provider_output_is_normalized_to_canonical_schema() -> None:
    normalized = normalize_yfinance_output(
        _single_ticker_frame(), ["SPY"], RETRIEVED_AT
    )

    assert tuple(normalized.columns) == CANONICAL_PRICE_COLUMNS
    assert normalized["ticker"].tolist() == ["SPY", "SPY"]
    assert normalized["close"].tolist() == [102.0, 104.0]
    assert normalized["adjusted_close"].tolist() == [101.5, 103.5]
    assert normalized["retrieved_at"].nunique() == 1
    assert normalized["retrieved_at"].iloc[0] == pd.Timestamp(RETRIEVED_AT)


@pytest.mark.parametrize(
    ("malformed_values", "expected_dtype_fragment"),
    [
        pytest.param(pd.Series([True, False], dtype="bool"), "bool", id="bool"),
        pytest.param(
            pd.Series([True, pd.NA], dtype="boolean"),
            "boolean",
            id="nullable-boolean",
        ),
        pytest.param(
            pd.Series(["100.0", "102.0"], dtype="string"),
            "string",
            id="numeric-string",
        ),
        pytest.param(
            pd.Series(pd.to_datetime(["2024-01-02", "2024-01-03"])),
            "datetime64",
            id="datetime",
        ),
        pytest.param(
            pd.Series(pd.to_timedelta([1, 2], unit="D")),
            "timedelta64",
            id="timedelta",
        ),
        pytest.param(
            pd.Series([100.0, 102.0], dtype="object"),
            "object",
            id="object-numeric-values",
        ),
    ],
)
def test_malformed_provider_market_dtypes_are_rejected_before_casting(
    malformed_values: pd.Series,
    expected_dtype_fragment: str,
) -> None:
    provider_data = _single_ticker_frame()
    malformed_values.index = provider_data.index
    provider_data["Open"] = malformed_values

    with pytest.raises(PriceNormalizationError) as error_info:
        normalize_yfinance_output(provider_data, ["SPY"], RETRIEVED_AT)

    error_message = str(error_info.value)
    assert "field 'Open' (open)" in error_message
    assert "real numeric dtype" in error_message
    actual_dtype = str(provider_data["Open"].dtype)
    assert expected_dtype_fragment in actual_dtype
    assert f"received {actual_dtype}" in error_message


def test_complex64_open_is_rejected_before_casting() -> None:
    provider_data = _single_ticker_frame()
    provider_data["Open"] = pd.Series(
        [100.0 + 1.0j, 102.0 - 1.0j],
        index=provider_data.index,
        dtype="complex64",
    )

    with pytest.raises(PriceNormalizationError) as error_info:
        normalize_yfinance_output(provider_data, ["SPY"], RETRIEVED_AT)

    assert "field 'Open' (open)" in str(error_info.value)
    assert "real numeric dtype" in str(error_info.value)
    assert "received complex64" in str(error_info.value)


def test_complex128_close_with_zero_imaginary_part_is_still_rejected() -> None:
    provider_data = _single_ticker_frame()
    provider_data["Close"] = pd.Series(
        [102.0 + 0.0j, 104.0 + 0.0j],
        index=provider_data.index,
        dtype="complex128",
    )

    with pytest.raises(PriceNormalizationError) as error_info:
        normalize_yfinance_output(provider_data, ["SPY"], RETRIEVED_AT)

    assert "field 'Close' (close)" in str(error_info.value)
    assert "real numeric dtype" in str(error_info.value)
    assert "received complex128" in str(error_info.value)


def test_float64_provider_market_columns_remain_valid() -> None:
    provider_data = _single_ticker_frame().astype(
        {field: "float64" for field in PROVIDER_FIELDS}
    )

    normalized = normalize_yfinance_output(provider_data, ["SPY"], RETRIEVED_AT)

    assert len(normalized) == 2
    assert all(
        str(normalized[field].dtype) == "float64" for field in PRICE_VALUE_COLUMNS
    )


def test_int64_provider_market_columns_remain_valid() -> None:
    provider_data = (
        _single_ticker_frame()
        .round()
        .astype({field: "int64" for field in PROVIDER_FIELDS})
    )

    normalized = normalize_yfinance_output(provider_data, ["SPY"], RETRIEVED_AT)

    assert len(normalized) == 2
    assert all(str(provider_data[field].dtype) == "int64" for field in PROVIDER_FIELDS)
    assert all(
        str(normalized[field].dtype) == "float64" for field in PRICE_VALUE_COLUMNS
    )


def test_nullable_float64_and_int64_provider_columns_remain_valid() -> None:
    provider_data = _single_ticker_frame()
    for field in PROVIDER_FIELDS[:-1]:
        provider_data[field] = pd.array(provider_data[field], dtype="Float64")
    provider_data["Volume"] = pd.array(provider_data["Volume"], dtype="Int64")
    provider_data.loc[provider_data.index[0], "Adj Close"] = pd.NA

    assert str(provider_data["Open"].dtype) == "Float64"
    assert str(provider_data["Volume"].dtype) == "Int64"

    normalized = normalize_yfinance_output(provider_data, ["SPY"], RETRIEVED_AT)

    assert len(normalized) == 2
    assert pd.isna(normalized.loc[0, "adjusted_close"])
    assert all(
        str(normalized[field].dtype) == "float64" for field in PRICE_VALUE_COLUMNS
    )


def test_price_download_batch_uses_one_stable_dense_numeric_representation() -> None:
    dtypes = {"SPY": "int64[pyarrow]", "QQQ": "Float64"}

    def mixed_backend_download(ticker: str, start: date, end: date) -> pd.DataFrame:
        del start, end
        provider_data = _single_ticker_frame().round()
        for field in PROVIDER_FIELDS:
            provider_data[field] = pd.Series(
                provider_data[field].tolist(),
                index=provider_data.index,
                dtype=dtypes[ticker],
            )
        return provider_data

    result = download_price_history(
        ["SPY", "QQQ"],
        start="2024-01-01",
        end="2024-02-01",
        downloader=mixed_backend_download,
        retrieved_at=RETRIEVED_AT,
    )

    assert set(result.successful_tickers) == {"SPY", "QQQ"}
    assert all(
        str(result.prices[column].dtype) == "float64" for column in PRICE_VALUE_COLUMNS
    )


def test_price_batch_concat_preserves_exact_large_float64_integer() -> None:
    exactly_representable_integer = 2**53

    def mixed_numeric_download(ticker: str, start: date, end: date) -> pd.DataFrame:
        del start, end
        provider_data = _single_ticker_frame(
            index=pd.DatetimeIndex(["2024-01-02"], name="Date")
        )
        if ticker == "SPY":
            provider_data["Volume"] = pd.Series(
                [exactly_representable_integer],
                index=provider_data.index,
                dtype="Int64",
            )
        return provider_data

    result = download_price_history(
        ["SPY", "QQQ"],
        start="2024-01-01",
        end="2024-01-04",
        downloader=mixed_numeric_download,
        retrieved_at=RETRIEVED_AT,
    )

    spy_volume = result.prices.loc[result.prices["ticker"].eq("SPY"), "volume"].iloc[0]
    assert spy_volume == exactly_representable_integer
    assert int(spy_volume) == exactly_representable_integer


def test_single_ticker_multiindex_output_is_supported() -> None:
    provider_data = _single_ticker_frame()
    provider_data.columns = pd.MultiIndex.from_product(
        [provider_data.columns, ["SPY"]], names=["Price", "Ticker"]
    )

    normalized = normalize_yfinance_output(provider_data, ["SPY"], RETRIEVED_AT)

    assert len(normalized) == 2
    assert normalized["ticker"].unique().tolist() == ["SPY"]


@pytest.mark.parametrize("field_level_first", [True, False])
def test_multi_ticker_output_supports_both_yfinance_level_orders(
    field_level_first: bool,
) -> None:
    normalized = normalize_yfinance_output(
        _multi_ticker_frame(field_level_first), ["SPY", "QQQ"], RETRIEVED_AT
    )

    assert normalized[["ticker", "date"]].to_records(index=False).tolist() == [
        ("QQQ", pd.Timestamp("2024-01-02")),
        ("QQQ", pd.Timestamp("2024-01-03")),
        ("SPY", pd.Timestamp("2024-01-02")),
        ("SPY", pd.Timestamp("2024-01-03")),
    ]


def test_missing_distinct_adjusted_close_remains_missing() -> None:
    normalized = normalize_yfinance_output(
        _single_ticker_frame(include_adjusted_close=False), ["SPY"], RETRIEVED_AT
    )

    assert normalized["adjusted_close"].isna().all()
    assert normalized["close"].notna().all()


def test_missing_requested_ticker_returns_no_fabricated_rows() -> None:
    provider_data = _single_ticker_frame()
    provider_data.columns = pd.MultiIndex.from_product(
        [provider_data.columns, ["SPY"]], names=["Price", "Ticker"]
    )

    normalized = normalize_yfinance_output(provider_data, ["QQQ"], RETRIEVED_AT)

    assert normalized.empty
    assert tuple(normalized.columns) == CANONICAL_PRICE_COLUMNS


def test_timezone_removal_preserves_market_observation_date() -> None:
    market_index = pd.DatetimeIndex(
        ["2024-01-02 00:00", "2024-01-03 00:00"],
        tz="America/New_York",
        name="Date",
    )

    normalized = normalize_yfinance_output(
        _single_ticker_frame(index=market_index), ["SPY"], RETRIEVED_AT
    )

    assert normalized["date"].dt.tz is None
    assert normalized["date"].tolist() == [
        pd.Timestamp("2024-01-02"),
        pd.Timestamp("2024-01-03"),
    ]


def test_timezone_naive_datetime_index_is_accepted() -> None:
    normalized = normalize_yfinance_output(
        _single_ticker_frame(index=pd.DatetimeIndex(["2024-01-02"], name="Date")),
        ["SPY"],
        RETRIEVED_AT,
    )

    assert normalized["date"].tolist() == [pd.Timestamp("2024-01-02")]
    assert normalized["date"].dt.tz is None


@pytest.mark.parametrize(
    "malformed_index",
    [
        pd.RangeIndex(2),
        pd.Index([0, 1], dtype="int64"),
    ],
)
def test_non_datetime_provider_index_is_rejected(
    malformed_index: pd.Index,
) -> None:
    provider_data = _single_ticker_frame()
    provider_data.index = malformed_index

    with pytest.raises(
        PriceNormalizationError, match="must use a pandas DatetimeIndex"
    ):
        normalize_yfinance_output(provider_data, ["SPY"], RETRIEVED_AT)


def test_range_index_cannot_create_unix_epoch_observation() -> None:
    provider_data = _single_ticker_frame(
        index=pd.DatetimeIndex(["2024-01-02"], name="Date")
    )
    provider_data.index = pd.RangeIndex(1)

    with pytest.raises(PriceNormalizationError, match="received RangeIndex"):
        normalize_yfinance_output(provider_data, ["SPY"], RETRIEVED_AT)


def test_output_is_sorted_deterministically_by_ticker_then_date() -> None:
    normalized = normalize_yfinance_output(
        _multi_ticker_frame(), ["SPY", "QQQ"], RETRIEVED_AT
    )

    expected = normalized.sort_values(["ticker", "date"], kind="mergesort")
    pd.testing.assert_frame_equal(normalized, expected.reset_index(drop=True))


def test_default_end_uses_new_york_date_at_asia_timezone_boundary() -> None:
    asia_next_day = datetime(2026, 8, 14, 0, 30, tzinfo=ZoneInfo("Asia/Taipei"))

    default_end = get_default_price_end_date(asia_next_day)

    assert default_end == date(2026, 8, 13)


def test_default_clock_is_consulted_only_after_downloader_returns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    def fake_download(ticker: str, start: date, end: date) -> pd.DataFrame:
        del ticker, start, end
        events.append("downloader-return")
        return _single_ticker_frame()

    def fake_utc_now() -> datetime:
        assert events == ["downloader-return"]
        events.append("clock")
        return RETRIEVED_AT

    monkeypatch.setattr(price_module, "_utc_now", fake_utc_now)

    result = download_price_history(
        ["SPY"],
        start="2024-01-01",
        end="2024-01-04",
        downloader=fake_download,
    )

    assert events == ["downloader-return", "clock"]
    assert result.statuses[0].retrieved_at == RETRIEVED_AT
    assert (result.prices["retrieved_at"] == pd.Timestamp(RETRIEVED_AT)).all()


def test_ticker_responses_receive_distinct_post_response_timestamps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_timestamp = datetime(2026, 8, 13, 12, 30, tzinfo=UTC)
    second_timestamp = datetime(2026, 8, 13, 12, 31, tzinfo=UTC)
    timestamps = iter([first_timestamp, second_timestamp])
    monkeypatch.setattr(price_module, "_utc_now", lambda: next(timestamps))

    result = download_price_history(
        ["SPY", "QQQ"],
        start="2024-01-01",
        end="2024-01-04",
        downloader=lambda ticker, start, end: _single_ticker_frame(),
    )

    assert [status.retrieved_at for status in result.statuses] == [
        first_timestamp,
        second_timestamp,
    ]
    assert (
        result.prices.loc[result.prices["ticker"].eq("SPY"), "retrieved_at"]
        == pd.Timestamp(first_timestamp)
    ).all()
    assert (
        result.prices.loc[result.prices["ticker"].eq("QQQ"), "retrieved_at"]
        == pd.Timestamp(second_timestamp)
    ).all()
    assert result.retrieved_at == second_timestamp


def test_later_observed_revision_is_accepted_by_persistence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_timestamp = datetime(2026, 8, 13, 12, 30, tzinfo=UTC)
    second_timestamp = datetime(2026, 8, 13, 12, 31, tzinfo=UTC)
    timestamps = iter([first_timestamp, second_timestamp])
    response_count = 0

    def fake_download(ticker: str, start: date, end: date) -> pd.DataFrame:
        nonlocal response_count
        del ticker, start, end
        response_count += 1
        frame = _single_ticker_frame()
        if response_count == 2:
            frame["Adj Close"] += 0.25
        return frame

    monkeypatch.setattr(price_module, "_utc_now", lambda: next(timestamps))
    first_result = download_price_history(
        ["SPY"],
        start="2024-01-01",
        end="2024-01-04",
        downloader=fake_download,
    )
    second_result = download_price_history(
        ["SPY"],
        start="2024-01-01",
        end="2024-01-04",
        downloader=fake_download,
    )
    output_path = tmp_path / "etf_prices_daily.parquet"
    persist_price_history(first_result.prices, output_path)

    persistence = persist_price_history(
        second_result.prices,
        output_path,
        snapshot_dir=tmp_path / "snapshots" / "prices",
        snapshot_time=datetime(2026, 8, 13, 12, 32, tzinfo=UTC),
    )

    assert persistence.revised_row_count == 2
    assert persistence.prices["adjusted_close"].tolist() == [101.75, 103.75]
    assert (persistence.prices["retrieved_at"] == second_timestamp).all()


def test_earlier_observed_revision_remains_stale_under_vintage_rule(
    tmp_path: Path,
) -> None:
    earlier_timestamp = datetime(2026, 8, 13, 12, 30, tzinfo=UTC)
    later_timestamp = datetime(2026, 8, 13, 12, 31, tzinfo=UTC)
    earlier = normalize_yfinance_output(
        _single_ticker_frame(), ["SPY"], earlier_timestamp
    )
    later_frame = _single_ticker_frame()
    later_frame["Adj Close"] += 0.25
    later = normalize_yfinance_output(later_frame, ["SPY"], later_timestamp)
    output_path = tmp_path / "etf_prices_daily.parquet"
    snapshot_dir = tmp_path / "snapshots" / "prices"
    persist_price_history(later, output_path)
    canonical_bytes = output_path.read_bytes()

    with pytest.raises(
        PriceDataValidationError, match="older than the canonical source vintage"
    ):
        persist_price_history(
            earlier,
            output_path,
            snapshot_dir=snapshot_dir,
            snapshot_time=datetime(2026, 8, 13, 12, 32, tzinfo=UTC),
        )

    assert output_path.read_bytes() == canonical_bytes
    assert not snapshot_dir.exists()


def test_explicit_retrieved_at_is_exact_override_without_clock_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_if_clock_is_consulted() -> datetime:
        raise AssertionError("automatic clock must not run for explicit override")

    monkeypatch.setattr(price_module, "_utc_now", fail_if_clock_is_consulted)

    result = download_price_history(
        ["SPY", "QQQ"],
        start="2024-01-01",
        end="2024-01-04",
        downloader=lambda ticker, start, end: _single_ticker_frame(),
        retrieved_at=RETRIEVED_AT,
    )

    assert [status.retrieved_at for status in result.statuses] == [
        RETRIEVED_AT,
        RETRIEVED_AT,
    ]
    assert result.retrieved_at == RETRIEVED_AT
    assert (result.prices["retrieved_at"] == pd.Timestamp(RETRIEVED_AT)).all()


def test_explicit_retrieved_at_must_be_timezone_aware_before_download() -> None:
    downloader_called = False

    def fake_download(ticker: str, start: date, end: date) -> pd.DataFrame:
        nonlocal downloader_called
        del ticker, start, end
        downloader_called = True
        return _single_ticker_frame()

    with pytest.raises(ValueError, match="retrieved_at must be timezone-aware"):
        download_price_history(
            ["SPY"],
            start="2024-01-01",
            end="2024-01-04",
            downloader=fake_download,
            retrieved_at=datetime(2026, 8, 13, 12, 30),
        )

    assert downloader_called is False


def test_default_cli_does_not_inject_batch_start_retrieved_at(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_arguments: dict[str, object] = {}

    def fake_download_price_history(**kwargs: object) -> SimpleNamespace:
        captured_arguments.update(kwargs)
        return SimpleNamespace(
            prices=pd.DataFrame(),
            successful_tickers=(),
            empty_tickers=("SPY",),
            failed_tickers=(),
        )

    monkeypatch.setattr(
        update_prices_script,
        "load_etf_universe",
        lambda: (SimpleNamespace(ticker="SPY"),),
    )
    monkeypatch.setattr(
        update_prices_script, "download_price_history", fake_download_price_history
    )

    exit_code = update_prices_script.main(
        [
            "--start",
            "2024-01-01",
            "--end",
            "2024-01-04",
            "--output",
            str(tmp_path / "prices.parquet"),
        ]
    )

    assert exit_code == 1
    assert "retrieved_at" not in captured_arguments


def test_provider_failure_has_no_successful_retrieval_timestamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_if_clock_is_consulted() -> datetime:
        raise AssertionError("failed provider request must not be timestamped")

    def fake_download(ticker: str, start: date, end: date) -> pd.DataFrame:
        del ticker, start, end
        raise TimeoutError("synthetic timeout")

    monkeypatch.setattr(price_module, "_utc_now", fail_if_clock_is_consulted)

    result = download_price_history(
        ["SPY"],
        start="2024-01-01",
        end="2024-01-04",
        downloader=fake_download,
    )

    assert result.failed_tickers == ("SPY",)
    assert result.statuses[0].retrieved_at is None
    assert result.retrieved_at is None


def test_empty_response_retains_post_response_retrieval_timestamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(price_module, "_utc_now", lambda: RETRIEVED_AT)

    result = download_price_history(
        ["SPY"],
        start="2024-01-01",
        end="2024-01-04",
        downloader=lambda ticker, start, end: pd.DataFrame(),
    )

    assert result.empty_tickers == ("SPY",)
    assert result.statuses[0].retrieved_at == RETRIEVED_AT
    assert result.retrieved_at == RETRIEVED_AT


def test_download_uses_injected_new_york_default_end_date() -> None:
    captured_end_dates: list[date] = []

    def fake_download(ticker: str, start: date, end: date) -> pd.DataFrame:
        del ticker, start
        captured_end_dates.append(end)
        return _single_ticker_frame()

    result = download_price_history(
        ["SPY"],
        start="2024-01-01",
        downloader=fake_download,
        retrieved_at=RETRIEVED_AT,
        default_end_reference_time=datetime(
            2026, 8, 14, 0, 30, tzinfo=ZoneInfo("Asia/Taipei")
        ),
    )

    assert result.successful_tickers == ("SPY",)
    assert captured_end_dates == [date(2026, 8, 13)]


def test_explicit_end_remains_exclusive_and_ignores_default_reference() -> None:
    captured_end_dates: list[date] = []

    def fake_download(ticker: str, start: date, end: date) -> pd.DataFrame:
        del ticker, start
        captured_end_dates.append(end)
        return _single_ticker_frame()

    download_price_history(
        ["SPY"],
        start="2024-01-01",
        end="2024-02-01",
        downloader=fake_download,
        retrieved_at=RETRIEVED_AT,
        default_end_reference_time=datetime(
            2026, 8, 14, 0, 30, tzinfo=ZoneInfo("Asia/Taipei")
        ),
    )

    assert captured_end_dates == [date(2024, 2, 1)]


def test_observation_exactly_on_inclusive_start_is_accepted() -> None:
    def fake_download(ticker: str, start: date, end: date) -> pd.DataFrame:
        del ticker, end
        return _single_ticker_frame(
            index=pd.DatetimeIndex([start.isoformat()], name="Date")
        )

    result = download_price_history(
        ["SPY"],
        start="2024-01-02",
        end="2024-01-04",
        downloader=fake_download,
        retrieved_at=RETRIEVED_AT,
    )

    assert result.successful_tickers == ("SPY",)
    assert result.prices["date"].tolist() == [pd.Timestamp("2024-01-02")]
    assert result.statuses[0].query_start == date(2024, 1, 2)
    assert result.statuses[0].query_end == date(2024, 1, 4)
    assert result.statuses[0].returned_dates == (date(2024, 1, 2),)


@pytest.mark.parametrize(
    ("offending_date", "expected_date"),
    [
        ("2024-01-01", "2024-01-01"),
        ("2024-01-04", "2024-01-04"),
        ("2024-01-05", "2024-01-05"),
    ],
)
def test_out_of_window_observation_fails_ticker_without_returning_rows(
    offending_date: str,
    expected_date: str,
) -> None:
    def fake_download(ticker: str, start: date, end: date) -> pd.DataFrame:
        del ticker, start, end
        return _single_ticker_frame(
            index=pd.DatetimeIndex([offending_date], name="Date")
        )

    result = download_price_history(
        ["SPY"],
        start="2024-01-02",
        end="2024-01-04",
        downloader=fake_download,
        retrieved_at=RETRIEVED_AT,
    )

    assert result.failed_tickers == ("SPY",)
    assert result.prices.empty
    assert result.statuses[0].error is not None
    assert expected_date in result.statuses[0].error
    assert "[2024-01-02, 2024-01-04)" in result.statuses[0].error


def test_out_of_window_ticker_fails_while_valid_ticker_succeeds() -> None:
    def fake_download(ticker: str, start: date, end: date) -> pd.DataFrame:
        del start, end
        returned_date = "2024-01-01" if ticker == "QQQ" else "2024-01-02"
        return _single_ticker_frame(
            index=pd.DatetimeIndex([returned_date], name="Date")
        )

    result = download_price_history(
        ["SPY", "QQQ"],
        start="2024-01-02",
        end="2024-01-04",
        downloader=fake_download,
        retrieved_at=RETRIEVED_AT,
    )

    assert result.successful_tickers == ("SPY",)
    assert result.failed_tickers == ("QQQ",)
    assert result.prices["ticker"].unique().tolist() == ["SPY"]
    assert result.prices["date"].ge(pd.Timestamp("2024-01-02")).all()
    assert result.prices["date"].lt(pd.Timestamp("2024-01-04")).all()


def test_mixed_valid_and_all_market_missing_rows_fail_ticker() -> None:
    def fake_download(ticker: str, start: date, end: date) -> pd.DataFrame:
        del ticker, start, end
        provider_data = _single_ticker_frame()
        provider_data.loc[provider_data.index[1], list(PROVIDER_FIELDS)] = float("nan")
        return provider_data

    result = download_price_history(
        ["SPY"],
        start="2024-01-01",
        end="2024-01-04",
        downloader=fake_download,
        retrieved_at=RETRIEVED_AT,
    )

    assert result.failed_tickers == ("SPY",)
    assert result.prices.empty
    assert result.statuses[0].error is not None
    assert "at least one market value" in result.statuses[0].error
    assert "2024-01-03" in result.statuses[0].error


def test_all_market_missing_ticker_fails_while_valid_ticker_succeeds() -> None:
    def fake_download(ticker: str, start: date, end: date) -> pd.DataFrame:
        del start, end
        provider_data = _single_ticker_frame()
        if ticker == "QQQ":
            provider_data.loc[:, list(PROVIDER_FIELDS)] = float("nan")
        return provider_data

    result = download_price_history(
        ["SPY", "QQQ"],
        start="2024-01-01",
        end="2024-01-04",
        downloader=fake_download,
        retrieved_at=RETRIEVED_AT,
    )

    assert result.successful_tickers == ("SPY",)
    assert result.failed_tickers == ("QQQ",)
    assert result.empty_tickers == ()
    assert result.prices["ticker"].unique().tolist() == ["SPY"]
    assert result.prices[list(PRICE_VALUE_COLUMNS)].notna().any(axis=1).all()


def test_empty_ticker_status_does_not_discard_successful_data() -> None:
    def fake_download(ticker: str, start: date, end: date) -> pd.DataFrame:
        del start, end
        if ticker == "QQQ":
            return pd.DataFrame()
        return _single_ticker_frame()

    result = download_price_history(
        ["SPY", "QQQ"],
        start="2024-01-01",
        end="2024-01-04",
        downloader=fake_download,
        retrieved_at=RETRIEVED_AT,
    )

    assert result.successful_tickers == ("SPY",)
    assert result.empty_tickers == ("QQQ",)
    assert result.failed_tickers == ()
    assert result.prices["ticker"].unique().tolist() == ["SPY"]


def test_failed_ticker_status_records_error_and_continues() -> None:
    def fake_download(ticker: str, start: date, end: date) -> pd.DataFrame:
        del start, end
        if ticker == "QQQ":
            raise ConnectionError("synthetic provider outage")
        return _single_ticker_frame()

    result = download_price_history(
        ["SPY", "QQQ"],
        start="2024-01-01",
        end="2024-01-04",
        downloader=fake_download,
        retrieved_at=RETRIEVED_AT,
    )

    assert result.successful_tickers == ("SPY",)
    assert result.failed_tickers == ("QQQ",)
    failed_status = result.statuses[1]
    assert failed_status.rows_received == 0
    assert failed_status.error == "ConnectionError: synthetic provider outage"
    assert len(result.prices) == 2


def test_malformed_provider_ticker_fails_while_valid_ticker_succeeds() -> None:
    def fake_download(ticker: str, start: date, end: date) -> pd.DataFrame:
        del start, end
        provider_data = _single_ticker_frame()
        if ticker == "QQQ":
            provider_data["Close"] = pd.Series(
                ["102.0", "104.0"], index=provider_data.index, dtype="string"
            )
        return provider_data

    result = download_price_history(
        ["SPY", "QQQ"],
        start="2024-01-01",
        end="2024-01-04",
        downloader=fake_download,
        retrieved_at=RETRIEVED_AT,
    )

    assert result.successful_tickers == ("SPY",)
    assert result.failed_tickers == ("QQQ",)
    assert result.statuses[1].error is not None
    assert "field 'Close' (close)" in result.statuses[1].error
    assert "received string" in result.statuses[1].error
    assert result.prices["ticker"].unique().tolist() == ["SPY"]


def test_complex_ticker_fails_without_rows_while_valid_ticker_succeeds() -> None:
    def fake_download(ticker: str, start: date, end: date) -> pd.DataFrame:
        del start, end
        provider_data = _single_ticker_frame()
        if ticker == "QQQ":
            provider_data["Close"] = pd.Series(
                [102.0 + 2.0j, 104.0 + 3.0j],
                index=provider_data.index,
                dtype="complex128",
            )
        return provider_data

    result = download_price_history(
        ["SPY", "QQQ"],
        start="2024-01-01",
        end="2024-01-04",
        downloader=fake_download,
        retrieved_at=RETRIEVED_AT,
    )

    assert result.successful_tickers == ("SPY",)
    assert result.failed_tickers == ("QQQ",)
    assert result.statuses[1].error is not None
    assert "field 'Close' (close)" in result.statuses[1].error
    assert "received complex128" in result.statuses[1].error
    assert result.prices["ticker"].unique().tolist() == ["SPY"]
    assert not result.prices["ticker"].eq("QQQ").any()


def test_malformed_provider_data_never_reaches_canonical_prices() -> None:
    def fake_download(ticker: str, start: date, end: date) -> pd.DataFrame:
        del ticker, start, end
        provider_data = _single_ticker_frame()
        provider_data["Volume"] = pd.Series(
            [True, False], index=provider_data.index, dtype="bool"
        )
        return provider_data

    result = download_price_history(
        ["SPY"],
        start="2024-01-01",
        end="2024-01-04",
        downloader=fake_download,
        retrieved_at=RETRIEVED_AT,
    )

    assert result.failed_tickers == ("SPY",)
    assert result.prices.empty
    assert tuple(result.prices.columns) == CANONICAL_PRICE_COLUMNS


def test_all_failed_batch_returns_empty_canonical_data() -> None:
    def fake_download(ticker: str, start: date, end: date) -> pd.DataFrame:
        del ticker, start, end
        raise TimeoutError("synthetic timeout")

    result = download_price_history(
        ["SPY"],
        start="2024-01-01",
        end="2024-01-04",
        downloader=fake_download,
        retrieved_at=RETRIEVED_AT,
    )

    assert result.prices.empty
    assert tuple(result.prices.columns) == CANONICAL_PRICE_COLUMNS
    assert result.failed_tickers == ("SPY",)


def test_yfinance_request_uses_supported_exception_config_and_restores_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_arguments: dict[str, object] = {}

    monkeypatch.setattr(price_module.yf.config.debug, "hide_exceptions", True)
    _install_raw_yfinance_ticker(
        monkeypatch, _raw_chart_payload(), captured=captured_arguments
    )

    result = download_price_history(
        ["SPY"],
        start="2024-01-01",
        end="2024-01-04",
        retrieved_at=RETRIEVED_AT,
    )

    assert result.successful_tickers == ("SPY",)
    assert captured_arguments["ticker"] == "SPY"
    assert str(captured_arguments["url"]).endswith("/v8/finance/chart/SPY")
    parameters = captured_arguments["params"]
    assert isinstance(parameters, dict)
    assert parameters["interval"] == "1d"
    assert parameters["includePrePost"] is False
    assert parameters["events"] == "div,splits,capitalGains"
    assert pd.Timestamp(parameters["period1"], unit="s", tz="UTC").tz_convert(
        "America/New_York"
    ).date() == date(2024, 1, 1)
    assert pd.Timestamp(parameters["period2"], unit="s", tz="UTC").tz_convert(
        "America/New_York"
    ).date() == date(2024, 1, 4)
    assert captured_arguments["request_method"] == "get"
    assert captured_arguments["get_call_count"] == 1
    assert captured_arguments.get("cache_get_call_count", 0) == 0
    assert captured_arguments["hide_exceptions_during_request"] is False
    assert price_module.yf.config.debug.hide_exceptions is True


def test_identical_historical_requests_use_uncached_transport_twice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    payload = _raw_chart_payload()
    _install_raw_yfinance_ticker(
        monkeypatch,
        payload,
        payload_sequence=[payload, payload],
        captured=captured,
    )

    first_result = download_price_history(
        ["SPY"],
        start="2024-01-01",
        end="2024-01-04",
        retrieved_at=RETRIEVED_AT,
    )
    second_result = download_price_history(
        ["SPY"],
        start="2024-01-01",
        end="2024-01-04",
        retrieved_at=datetime(2026, 8, 14, 12, 30, tzinfo=UTC),
    )

    assert first_result.successful_tickers == ("SPY",)
    assert second_result.successful_tickers == ("SPY",)
    assert captured["get_call_count"] == 2
    assert captured.get("cache_get_call_count", 0) == 0
    request_calls = captured["request_calls"]
    assert isinstance(request_calls, list)
    assert len(request_calls) == 2
    assert request_calls[0] == request_calls[1]


def test_second_identical_request_observes_revision_with_own_retrieved_at(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_payload = _raw_chart_payload()
    revised_payload = _raw_chart_payload(adjusted_close_values=[101.75, 103.75])
    captured: dict[str, object] = {}
    second_retrieved_at = datetime(2026, 8, 14, 12, 30, tzinfo=UTC)
    _install_raw_yfinance_ticker(
        monkeypatch,
        first_payload,
        payload_sequence=[first_payload, revised_payload],
        captured=captured,
    )

    first_result = download_price_history(
        ["SPY"],
        start="2024-01-01",
        end="2024-01-04",
        retrieved_at=RETRIEVED_AT,
    )
    second_result = download_price_history(
        ["SPY"],
        start="2024-01-01",
        end="2024-01-04",
        retrieved_at=second_retrieved_at,
    )

    assert first_result.prices["adjusted_close"].tolist() == [101.5, 103.5]
    assert second_result.prices["adjusted_close"].tolist() == [101.75, 103.75]
    assert (first_result.prices["retrieved_at"] == pd.Timestamp(RETRIEVED_AT)).all()
    assert (
        second_result.prices["retrieved_at"] == pd.Timestamp(second_retrieved_at)
    ).all()
    assert captured["get_call_count"] == 2
    assert captured.get("cache_get_call_count", 0) == 0


def test_raw_adapter_delegates_to_get_only_yfinance_data_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class FakeResponse:
        text = ""

        def json(self) -> object:
            return _raw_chart_payload()

    class GetOnlyData:
        def get(self, **kwargs: object) -> FakeResponse:
            captured.update(kwargs)
            return FakeResponse()

    class GetOnlyTicker:
        def __init__(self, ticker: str) -> None:
            captured["ticker"] = ticker
            self._data = GetOnlyData()

        def _get_ticker_tz(self, timeout: int) -> str:
            captured["timezone_timeout"] = timeout
            return "America/New_York"

    monkeypatch.setattr(price_module.yf, "Ticker", GetOnlyTicker)

    result = download_price_history(
        ["SPY"],
        start="2024-01-01",
        end="2024-01-04",
        retrieved_at=RETRIEVED_AT,
    )

    assert result.successful_tickers == ("SPY",)
    assert captured["ticker"] == "SPY"
    assert str(captured["url"]).endswith("/v8/finance/chart/SPY")
    assert captured["timeout"] == 10


def test_raw_chart_fixture_produces_real_numeric_market_dtypes() -> None:
    provider_data = price_module._raw_chart_to_provider_frame(
        _raw_chart_payload(), "SPY"
    )

    assert set(provider_data.columns) == set(PROVIDER_FIELDS)
    assert all(
        pd.api.types.is_any_real_numeric_dtype(provider_data[field].dtype)
        for field in PROVIDER_FIELDS
    )


def test_raw_chart_preserves_large_integer_before_canonical_float_cast() -> None:
    large_integer = 9_007_199_254_740_993
    payload = _raw_chart_payload(
        dates=("2024-01-02",),
        quote_values={
            "open": [100],
            "high": [103],
            "low": [99],
            "close": [102],
            "volume": [large_integer],
        },
        adjusted_close_values=[101.5],
    )

    provider_data = price_module._raw_chart_to_provider_frame(payload, "SPY")

    assert provider_data["Volume"].iloc[0] == large_integer
    assert int(provider_data["Volume"].iloc[0]) == large_integer


def test_raw_chart_uint64_buffer_preserves_each_original_python_integer() -> None:
    raw_volumes = [2**53 + 1, 2**63]
    payload = _raw_chart_payload(
        quote_values={
            "open": [100, 102],
            "high": [103, 105],
            "low": [99, 101],
            "close": [102, 104],
            "volume": raw_volumes,
        },
    )

    provider_data = price_module._raw_chart_to_provider_frame(payload, "SPY")

    assert str(provider_data["Volume"].dtype) == "UInt64"
    assert provider_data["Volume"].tolist() == raw_volumes


def test_large_inexact_volume_is_rejected_before_canonical_corruption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    large_integer = 9_007_199_254_740_993
    _install_raw_yfinance_ticker(
        monkeypatch,
        _raw_chart_payload(
            dates=("2024-01-02",),
            quote_values={
                "open": [100],
                "high": [103],
                "low": [99],
                "close": [102],
                "volume": [large_integer],
            },
            adjusted_close_values=[101.5],
        ),
    )

    result = download_price_history(
        ["SPY"],
        start="2024-01-01",
        end="2024-01-04",
        retrieved_at=RETRIEVED_AT,
    )

    assert result.failed_tickers == ("SPY",)
    assert result.prices.empty
    assert "cannot be represented losslessly as canonical float64" in (
        result.statuses[0].error or ""
    )


def test_exactly_representable_mixed_raw_values_remain_valid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_raw_yfinance_ticker(
        monkeypatch,
        _raw_chart_payload(
            quote_values={
                "open": [100, 102.5],
                "high": [103, 105.5],
                "low": [99, 101.5],
                "close": [102, 104.5],
                "volume": [1_000_000, 1_200_000.0],
            },
        ),
    )

    result = download_price_history(
        ["SPY"],
        start="2024-01-01",
        end="2024-01-04",
        retrieved_at=RETRIEVED_AT,
    )

    assert result.successful_tickers == ("SPY",)
    assert result.prices["open"].tolist() == [100.0, 102.5]
    assert result.prices["volume"].tolist() == [1_000_000.0, 1_200_000.0]


def test_raw_mixed_values_without_lossless_representation_are_rejected() -> None:
    payload = _raw_chart_payload(
        quote_values={
            "open": [9_007_199_254_740_993, 100.5],
            "high": [9_007_199_254_740_995, 103.5],
            "low": [9_007_199_254_740_991, 99.5],
            "close": [9_007_199_254_740_993, 102.5],
            "volume": [1_000_000, 1_200_000],
        },
    )

    with pytest.raises(
        PriceNormalizationError,
        match="cannot be represented losslessly",
    ):
        price_module._raw_chart_to_provider_frame(payload, "SPY")


def test_matching_chart_metadata_symbol_is_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_raw_yfinance_ticker(monkeypatch, _raw_chart_payload(symbol="SPY"))

    result = download_price_history(
        ["SPY"],
        start="2024-01-01",
        end="2024-01-04",
        retrieved_at=RETRIEVED_AT,
    )

    assert result.successful_tickers == ("SPY",)


def test_mismatched_chart_metadata_symbol_never_reaches_canonical_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_raw_yfinance_ticker(monkeypatch, _raw_chart_payload(symbol="QQQ"))

    result = download_price_history(
        ["SPY"],
        start="2024-01-01",
        end="2024-01-04",
        retrieved_at=RETRIEVED_AT,
    )

    assert result.failed_tickers == ("SPY",)
    assert result.prices.empty
    assert "identifies symbol 'QQQ', not requested symbol 'SPY'" in (
        result.statuses[0].error or ""
    )


@pytest.mark.parametrize("symbol", [None, 123, True, "", " SPY "])
def test_missing_or_malformed_chart_metadata_symbol_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    symbol: object,
) -> None:
    payload = _raw_chart_payload(symbol=symbol)
    if symbol is None:
        result_payload = payload["chart"]
        assert isinstance(result_payload, dict)
        results = result_payload["result"]
        assert isinstance(results, list)
        result = results[0]
        assert isinstance(result, dict)
        metadata = result["meta"]
        assert isinstance(metadata, dict)
        del metadata["symbol"]
    _install_raw_yfinance_ticker(monkeypatch, payload)

    result = download_price_history(
        ["SPY"],
        start="2024-01-01",
        end="2024-01-04",
        retrieved_at=RETRIEVED_AT,
    )

    assert result.failed_tickers == ("SPY",)
    assert result.prices.empty
    assert "valid 'symbol' identifier" in (result.statuses[0].error or "")


def test_yfinance_uppercase_symbol_contract_accepts_lowercase_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_raw_yfinance_ticker(monkeypatch, _raw_chart_payload(symbol="SPY"))

    result = download_price_history(
        ["spy"],
        start="2024-01-01",
        end="2024-01-04",
        retrieved_at=RETRIEVED_AT,
    )

    assert result.successful_tickers == ("spy",)
    assert result.prices["ticker"].unique().tolist() == ["spy"]


def test_partial_batch_identity_mismatch_does_not_contaminate_valid_ticker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_raw_yfinance_ticker(
        monkeypatch,
        _raw_chart_payload(symbol="SPY"),
        payload_sequence=[
            _raw_chart_payload(symbol="SPY"),
            _raw_chart_payload(symbol="SPY"),
        ],
    )

    result = download_price_history(
        ["SPY", "QQQ"],
        start="2024-01-01",
        end="2024-01-04",
        retrieved_at=RETRIEVED_AT,
    )

    assert result.successful_tickers == ("SPY",)
    assert result.failed_tickers == ("QQQ",)
    assert result.prices["ticker"].unique().tolist() == ["SPY"]
    assert not result.prices["ticker"].eq("QQQ").any()


def test_yfinance_exception_config_is_restored_after_provider_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    monkeypatch.setattr(price_module.yf.config.debug, "hide_exceptions", True)
    _install_raw_yfinance_ticker(
        monkeypatch,
        _raw_chart_payload(),
        request_error=ConnectionError("synthetic yfinance failure"),
        captured=captured,
    )

    result = download_price_history(
        ["SPY"],
        start="2024-01-01",
        end="2024-01-04",
        retrieved_at=RETRIEVED_AT,
    )

    assert captured["hide_exceptions_during_request"] is False
    assert captured["request_method"] == "get"
    assert captured.get("cache_get_call_count", 0) == 0
    assert price_module.yf.config.debug.hide_exceptions is True
    assert result.failed_tickers == ("SPY",)
    assert result.statuses[0].error == ("ConnectionError: synthetic yfinance failure")


def test_genuine_empty_yfinance_history_remains_empty_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(price_module.yf.config.debug, "hide_exceptions", True)
    _install_raw_yfinance_ticker(
        monkeypatch,
        {
            "chart": {
                "result": [
                    {
                        "meta": {
                            "symbol": "SPY",
                            "exchangeTimezoneName": "America/New_York",
                        },
                        "indicators": {"quote": [{}]},
                    }
                ],
                "error": None,
            }
        },
    )

    result = download_price_history(
        ["SPY"],
        start="2024-01-01",
        end="2024-01-04",
        retrieved_at=RETRIEVED_AT,
    )

    assert result.empty_tickers == ("SPY",)
    assert result.failed_tickers == ()
    assert price_module.yf.config.debug.hide_exceptions is True


def test_raw_payload_without_adjusted_close_preserves_missingness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_raw_yfinance_ticker(
        monkeypatch, _raw_chart_payload(include_adjusted_close=False)
    )

    result = download_price_history(
        ["SPY"],
        start="2024-01-01",
        end="2024-01-04",
        retrieved_at=RETRIEVED_AT,
    )

    assert result.successful_tickers == ("SPY",)
    assert result.prices["adjusted_close"].isna().all()
    assert result.prices["close"].tolist() == [102.0, 104.0]


def test_raw_adjusted_close_equal_to_close_remains_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_raw_yfinance_ticker(
        monkeypatch,
        _raw_chart_payload(adjusted_close_values=[102.0, 104.0]),
    )

    result = download_price_history(
        ["SPY"],
        start="2024-01-01",
        end="2024-01-04",
        retrieved_at=RETRIEVED_AT,
    )

    assert result.prices["adjusted_close"].tolist() == [102.0, 104.0]
    assert result.prices["adjusted_close"].notna().all()


def test_raw_adjusted_close_null_remains_missing_for_that_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_raw_yfinance_ticker(
        monkeypatch,
        _raw_chart_payload(adjusted_close_values=[None, 103.5]),
    )

    result = download_price_history(
        ["SPY"],
        start="2024-01-01",
        end="2024-01-04",
        retrieved_at=RETRIEVED_AT,
    )

    assert pd.isna(result.prices.loc[0, "adjusted_close"])
    assert result.prices.loc[1, "adjusted_close"] == 103.5


@pytest.mark.parametrize(
    ("raw_volume", "expected_missing", "expected_value"),
    [(None, True, None), (0.0, False, 0.0)],
)
def test_raw_volume_null_and_zero_remain_distinct(
    monkeypatch: pytest.MonkeyPatch,
    raw_volume: float | None,
    expected_missing: bool,
    expected_value: float | None,
) -> None:
    payload = _raw_chart_payload(dates=("2024-01-02",))
    chart = payload["chart"]
    assert isinstance(chart, dict)
    results = chart["result"]
    assert isinstance(results, list)
    raw_result = results[0]
    assert isinstance(raw_result, dict)
    indicators = raw_result["indicators"]
    assert isinstance(indicators, dict)
    quotes = indicators["quote"]
    assert isinstance(quotes, list)
    quote = quotes[0]
    assert isinstance(quote, dict)
    quote["volume"] = [raw_volume]
    _install_raw_yfinance_ticker(monkeypatch, payload)

    result = download_price_history(
        ["SPY"],
        start="2024-01-01",
        end="2024-01-04",
        retrieved_at=RETRIEVED_AT,
    )

    assert bool(pd.isna(result.prices.loc[0, "volume"])) is expected_missing
    if expected_value is not None:
        assert result.prices.loc[0, "volume"] == expected_value


def test_raw_fully_missing_dated_observation_fails_ticker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    null_quote = {field: [None] for field in ("open", "high", "low", "close", "volume")}
    _install_raw_yfinance_ticker(
        monkeypatch,
        _raw_chart_payload(
            dates=("2024-01-02",),
            quote_values=null_quote,
            adjusted_close_values=[None],
        ),
    )

    result = download_price_history(
        ["SPY"],
        start="2024-01-01",
        end="2024-01-04",
        retrieved_at=RETRIEVED_AT,
    )

    assert result.failed_tickers == ("SPY",)
    assert result.prices.empty
    assert result.statuses[0].error is not None
    assert "at least one market value" in result.statuses[0].error


def test_malformed_raw_chart_structure_fails_ticker_usefully(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_raw_yfinance_ticker(monkeypatch, {"chart": {"error": None}})

    result = download_price_history(
        ["SPY"],
        start="2024-01-01",
        end="2024-01-04",
        retrieved_at=RETRIEVED_AT,
    )

    assert result.failed_tickers == ("SPY",)
    assert result.statuses[0].error is not None
    assert "missing the 'result' list" in result.statuses[0].error


def test_pinned_raw_adapter_fails_clearly_without_expected_internal_interface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class IncompatibleTicker:
        def __init__(self, ticker: str) -> None:
            del ticker

    monkeypatch.setattr(price_module.yf, "Ticker", IncompatibleTicker)

    result = download_price_history(
        ["SPY"],
        start="2024-01-01",
        end="2024-01-04",
        retrieved_at=RETRIEVED_AT,
    )

    assert result.failed_tickers == ("SPY",)
    assert result.statuses[0].error is not None
    assert "requires Ticker._get_ticker_tz and Ticker._data" in (
        result.statuses[0].error
    )


def test_raw_chart_dates_preserve_market_timezone_and_sort_deterministically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _raw_chart_payload(dates=("2024-01-03", "2024-01-02"))
    _install_raw_yfinance_ticker(monkeypatch, payload)

    result = download_price_history(
        ["SPY"],
        start="2024-01-01",
        end="2024-01-04",
        retrieved_at=RETRIEVED_AT,
    )

    assert tuple(result.prices.columns) == CANONICAL_PRICE_COLUMNS
    assert result.prices["date"].dt.tz is None
    assert result.prices["date"].tolist() == [
        pd.Timestamp("2024-01-02"),
        pd.Timestamp("2024-01-03"),
    ]


def test_raw_chart_applies_pinned_yfinance_daily_dst_date_correction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _raw_chart_payload(dates=("2024-01-03",))
    chart = payload["chart"]
    assert isinstance(chart, dict)
    results = chart["result"]
    assert isinstance(results, list)
    raw_result = results[0]
    assert isinstance(raw_result, dict)
    raw_result["timestamp"] = [
        int(pd.Timestamp("2024-01-02 23:00", tz="America/New_York").timestamp())
    ]
    _install_raw_yfinance_ticker(monkeypatch, payload)

    result = download_price_history(
        ["SPY"],
        start="2024-01-01",
        end="2024-01-04",
        retrieved_at=RETRIEVED_AT,
    )

    assert result.successful_tickers == ("SPY",)
    assert result.prices["date"].tolist() == [pd.Timestamp("2024-01-03")]


def test_raw_chart_response_still_enforces_exclusive_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_raw_yfinance_ticker(monkeypatch, _raw_chart_payload(dates=("2024-01-04",)))

    result = download_price_history(
        ["SPY"],
        start="2024-01-01",
        end="2024-01-04",
        retrieved_at=RETRIEVED_AT,
    )

    assert result.failed_tickers == ("SPY",)
    assert result.prices.empty
    assert result.statuses[0].error is not None
    assert "[2024-01-01, 2024-01-04)" in result.statuses[0].error
