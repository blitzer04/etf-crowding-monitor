"""Download, normalize, and persist canonical daily ETF price history."""

from __future__ import annotations

import errno
import logging
import os
import shutil
import sys
import tempfile
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import BinaryIO, Literal, cast
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf  # type: ignore[import-untyped]
from pandas.api.types import is_any_real_numeric_dtype
from yfinance import utils as yf_utils  # type: ignore[import-untyped]
from yfinance.const import _BASE_URL_  # type: ignore[import-untyped]

from etf_crowding.data.numeric_dtypes import (
    NumericDtypeHarmonizationError,
    build_lossless_real_numeric_series,
    cast_real_numeric_series_losslessly,
    harmonize_real_numeric_series,
)
from etf_crowding.data.validation import (
    CANONICAL_PRICE_COLUMNS,
    PRICE_VALUE_COLUMNS,
    PriceDataValidationError,
    deduplicate_price_data,
    validate_price_data,
)
from etf_crowding.data.yfinance_runtime import yfinance_exception_visibility
from etf_crowding.paths import get_snapshot_data_dir

DEFAULT_PRICE_START_DATE = date(2018, 1, 1)
DEFAULT_PRICE_FILENAME = "etf_prices_daily.parquet"
_US_MARKET_TIME_ZONE_NAME = "America/New_York"
_PRICE_HISTORY_LOCK_TIMEOUT_SECONDS = 30.0
_PRICE_HISTORY_LOCK_POLL_SECONDS = 0.05

DownloadStatus = Literal["success", "empty", "failed"]
PriceDownloadCallable = Callable[[str, date, date], pd.DataFrame | None]

LOGGER = logging.getLogger(__name__)

_PROVIDER_FIELD_NAMES = {
    "open": "open",
    "high": "high",
    "low": "low",
    "close": "close",
    "adj close": "adjusted_close",
    "volume": "volume",
}
_REQUIRED_PROVIDER_FIELDS = {"open", "high", "low", "close", "volume"}
_RAW_QUOTE_FIELDS = {
    "Open": "open",
    "High": "high",
    "Low": "low",
    "Close": "close",
    "Volume": "volume",
}


class PriceNormalizationError(ValueError):
    """Indicate that provider output cannot be normalized safely."""


@dataclass(frozen=True, slots=True)
class TickerDownloadStatus:
    """Describe the outcome of one ticker's provider request.

    Attributes:
        ticker: Requested ETF ticker.
        status: One of ``success``, ``empty``, or ``failed``.
        rows_received: Number of usable canonical rows returned.
        first_date: Earliest returned trading date, when available.
        last_date: Latest returned trading date, when available.
        retrieved_at: UTC pandas timestamp when a successful or empty ticker
            response returned. Failed outcomes have no successful retrieval
            timestamp.
        query_start: Inclusive provider request date.
        query_end: Exclusive provider request date.
        returned_dates: Exact canonical dates returned by this ticker response.
            Empty and failed outcomes have no returned dates.
        error: Provider or normalization error for failed requests.
    """

    ticker: str
    status: DownloadStatus
    rows_received: int
    first_date: date | None
    last_date: date | None
    retrieved_at: pd.Timestamp | None
    query_start: date
    query_end: date
    returned_dates: tuple[date, ...]
    error: str | None = None


@dataclass(frozen=True, slots=True)
class PriceDownloadResult:
    """Hold canonical price rows and structured per-ticker outcomes.

    Attributes:
        prices: Successful observations in canonical long format.
        statuses: Outcome for every requested ticker, in request order.
        retrieved_at: Latest successful or empty ticker-response timestamp, or
            ``None`` if no such response was observed.
    """

    prices: pd.DataFrame
    statuses: tuple[TickerDownloadStatus, ...]
    retrieved_at: pd.Timestamp | None

    @property
    def successful_tickers(self) -> tuple[str, ...]:
        """Return tickers with at least one usable row."""

        return tuple(item.ticker for item in self.statuses if item.status == "success")

    @property
    def empty_tickers(self) -> tuple[str, ...]:
        """Return tickers for which the provider returned no usable rows."""

        return tuple(item.ticker for item in self.statuses if item.status == "empty")

    @property
    def failed_tickers(self) -> tuple[str, ...]:
        """Return tickers whose provider request or normalization failed."""

        return tuple(item.ticker for item in self.statuses if item.status == "failed")


@dataclass(frozen=True, slots=True)
class PricePersistenceResult:
    """Describe a successful canonical price-history write.

    Attributes:
        prices: Validated latest source vintage written to the canonical file.
        revised_row_count: Overlapping rows replaced by a changed incoming
            source vintage, including rows completed with previously missing
            market fields.
        revised_tickers: Tickers affected by accepted source-vintage revisions.
        snapshot_path: Immutable snapshot of the superseded canonical file, or
            ``None`` when no overlapping source values changed.
    """

    prices: pd.DataFrame
    revised_row_count: int
    revised_tickers: tuple[str, ...]
    snapshot_path: Path | None


def _empty_price_data() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": pd.Series(dtype="datetime64[ns]"),
            "ticker": pd.Series(dtype="string"),
            "open": pd.Series(dtype="float64"),
            "high": pd.Series(dtype="float64"),
            "low": pd.Series(dtype="float64"),
            "close": pd.Series(dtype="float64"),
            "adjusted_close": pd.Series(dtype="float64"),
            "volume": pd.Series(dtype="float64"),
            "retrieved_at": pd.Series(dtype="datetime64[ns, UTC]"),
        }
    )


def _parse_date(value: str | date, name: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{name} must use YYYY-MM-DD format: {value!r}.") from error


def get_default_price_end_date(reference_time: datetime | None = None) -> date:
    """Return the current America/New_York date for the exclusive end bound.

    Using the U.S. market calendar date prevents a machine in a timezone ahead
    of New York from requesting the still-open U.S. trading date by default.
    This is a calendar-date rule only; it does not infer market-close status or
    consult a trading calendar.

    Args:
        reference_time: Optional timezone-aware instant for deterministic tests.
            Defaults to the current instant in UTC.

    Returns:
        The calendar date at that instant in America/New_York.

    Raises:
        ValueError: If an explicitly supplied reference time is timezone-naive.
    """

    current_time = reference_time if reference_time is not None else datetime.now(UTC)
    if current_time.tzinfo is None or current_time.utcoffset() is None:
        raise ValueError("reference_time must be timezone-aware.")
    return current_time.astimezone(ZoneInfo(_US_MARKET_TIME_ZONE_NAME)).date()


def _retrieval_timestamp(value: datetime | pd.Timestamp) -> pd.Timestamp:
    if type(value) is not datetime and not isinstance(value, pd.Timestamp):
        raise ValueError("retrieved_at must be a datetime or pandas Timestamp.")
    timestamp = pd.Timestamp(value)
    if pd.isna(timestamp):
        raise ValueError("retrieved_at must be a valid timestamp.")
    if timestamp.tzinfo is None:
        raise ValueError("retrieved_at must be timezone-aware.")
    if str(timestamp.tz) != "UTC":
        raise ValueError("retrieved_at must use the UTC timezone.")
    return timestamp


def _utc_now() -> pd.Timestamp:
    return pd.Timestamp.now(tz="UTC")


def _format_price_download_failure(error: Exception) -> str:
    error_type_name = type(error).__name__.strip() or "Exception"
    error_message = str(error).strip()
    return f"{error_type_name}: {error_message}" if error_message else error_type_name


def _normalize_tickers(tickers: Sequence[str]) -> tuple[str, ...]:
    normalized: list[str] = []
    seen: set[str] = set()
    for ticker in tickers:
        if not isinstance(ticker, str) or not ticker.strip():
            raise ValueError("Every requested ticker must be a non-empty string.")
        clean_ticker = ticker.strip()
        ticker_key = clean_ticker.casefold()
        if ticker_key in seen:
            raise ValueError(f"Duplicate requested ticker: {clean_ticker!r}.")
        seen.add(ticker_key)
        normalized.append(clean_ticker)

    if not normalized:
        raise ValueError("At least one ticker must be requested.")
    return tuple(normalized)


def _canonical_provider_field(label: object) -> str | None:
    normalized_label = " ".join(str(label).strip().casefold().replace("_", " ").split())
    return _PROVIDER_FIELD_NAMES.get(normalized_label)


def _provider_field_level(columns: pd.MultiIndex) -> int:
    candidate_levels: list[int] = []
    for level in range(columns.nlevels):
        canonical_names = {
            field
            for label in columns.get_level_values(level)
            if (field := _canonical_provider_field(label)) is not None
        }
        if _REQUIRED_PROVIDER_FIELDS.issubset(canonical_names):
            candidate_levels.append(level)

    if len(candidate_levels) != 1:
        raise PriceNormalizationError(
            "Could not identify one yfinance field level containing Open, High, "
            "Low, Close, and Volume."
        )
    return candidate_levels[0]


def _normalize_trading_dates(index: pd.Index) -> pd.DatetimeIndex:
    if not isinstance(index, pd.DatetimeIndex):
        raise PriceNormalizationError(
            "Non-empty yfinance daily history must use a pandas DatetimeIndex; "
            f"received {type(index).__name__}."
        )

    trading_dates = index.copy()
    if trading_dates.hasnans:
        raise PriceNormalizationError("yfinance returned missing or invalid dates.")
    if trading_dates.tz is not None:
        # Removing timezone metadata without conversion preserves the provider's
        # market observation date and avoids shifting it across calendar days.
        trading_dates = trading_dates.tz_localize(None)
    return trading_dates.normalize()


def _normalize_single_ticker(
    provider_data: pd.DataFrame,
    ticker: str,
    retrieved_at: datetime | pd.Timestamp,
) -> pd.DataFrame:
    renamed_columns: dict[object, str] = {}
    canonical_names: list[str] = []
    for column in provider_data.columns:
        canonical_name = _canonical_provider_field(column)
        if canonical_name is not None:
            renamed_columns[column] = canonical_name
            canonical_names.append(canonical_name)

    duplicate_fields = {
        field for field in canonical_names if canonical_names.count(field) > 1
    }
    if duplicate_fields:
        raise PriceNormalizationError(
            f"yfinance returned duplicate price fields: {sorted(duplicate_fields)}."
        )

    available_fields = set(canonical_names)
    missing_fields = sorted(_REQUIRED_PROVIDER_FIELDS - available_fields)
    if missing_fields:
        raise PriceNormalizationError(
            f"yfinance output for {ticker} is missing fields: {missing_fields}."
        )

    for provider_field, canonical_field in renamed_columns.items():
        provider_values = provider_data[provider_field]
        provider_dtype = provider_values.dtype
        if not is_any_real_numeric_dtype(provider_dtype):
            raise PriceNormalizationError(
                f"yfinance field {provider_field!r} ({canonical_field}) for {ticker} "
                "must have a real numeric dtype before normalization; "
                f"received {provider_dtype}."
            )

    source = provider_data.rename(columns=renamed_columns).reset_index(drop=True)
    normalized = pd.DataFrame(
        {
            "date": _normalize_trading_dates(provider_data.index),
            "ticker": pd.Series([ticker] * len(source), dtype="string"),
            "retrieved_at": pd.Series(
                [pd.Timestamp(retrieved_at)] * len(source),
                dtype="datetime64[ns, UTC]",
            ),
        }
    )

    for field in PRICE_VALUE_COLUMNS:
        if field == "adjusted_close" and field not in source.columns:
            normalized[field] = float("nan")
            continue
        try:
            normalized[field] = cast_real_numeric_series_losslessly(
                source[field], "float64"
            )
        except NumericDtypeHarmonizationError as error:
            raise PriceNormalizationError(
                f"yfinance field {field!r} for {ticker} cannot be represented "
                "losslessly as canonical float64 values."
            ) from error

    normalized = normalized.loc[:, list(CANONICAL_PRICE_COLUMNS)]
    normalized = deduplicate_price_data(normalized)
    normalized = normalized.sort_values(
        ["ticker", "date"], kind="mergesort"
    ).reset_index(drop=True)
    validate_price_data(normalized)
    return normalized


def _empty_raw_chart_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            field: pd.Series(dtype="float64")
            for field in ("Open", "High", "Low", "Close", "Adj Close", "Volume")
        },
        index=pd.DatetimeIndex([], name="Date"),
    )


def _chart_result(payload: object, ticker: str) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise PriceNormalizationError(
            f"Yahoo chart response for {ticker} must be a JSON object."
        )

    chart = payload.get("chart")
    if not isinstance(chart, dict):
        raise PriceNormalizationError(
            f"Yahoo chart response for {ticker} is missing the 'chart' object."
        )

    provider_error = chart.get("error")
    if provider_error is not None:
        if isinstance(provider_error, dict):
            description = provider_error.get("description") or provider_error.get(
                "code"
            )
        else:
            description = provider_error
        raise PriceNormalizationError(
            f"Yahoo chart response for {ticker} reported an error: {description!r}."
        )

    results = chart.get("result")
    if not isinstance(results, list):
        raise PriceNormalizationError(
            f"Yahoo chart response for {ticker} is missing the 'result' list."
        )
    if not results:
        raise PriceNormalizationError(
            f"Yahoo chart response for {ticker} has no result object from which "
            "provider identity can be established."
        )
    if len(results) != 1 or not isinstance(results[0], dict):
        raise PriceNormalizationError(
            f"Yahoo chart response for {ticker} must contain one result object."
        )
    return cast(dict[str, object], results[0])


def _raw_indicator_values(
    container: dict[str, object],
    field: str,
    expected_length: int,
    ticker: str,
) -> list[object]:
    values = container.get(field)
    if not isinstance(values, list) or len(values) != expected_length:
        raise PriceNormalizationError(
            f"Yahoo chart field {field!r} for {ticker} must be a list with "
            f"{expected_length} values."
        )

    invalid_positions = [
        position
        for position, value in enumerate(values)
        if value is not None
        and (isinstance(value, bool) or not isinstance(value, (int, float)))
    ]
    if invalid_positions:
        raise PriceNormalizationError(
            f"Yahoo chart field {field!r} for {ticker} contains non-numeric "
            f"values at positions {invalid_positions[:5]}."
        )
    return values


def _raw_chart_to_provider_frame(payload: object, ticker: str) -> pd.DataFrame:
    """Preserve source-level Yahoo quote missingness in a yfinance-like frame."""

    result = _chart_result(payload, ticker)

    meta = result.get("meta")
    if not isinstance(meta, dict):
        raise PriceNormalizationError(
            f"Yahoo chart result for {ticker} is missing the 'meta' object."
        )
    provider_symbol = meta.get("symbol")
    if (
        not isinstance(provider_symbol, str)
        or not provider_symbol
        or provider_symbol != provider_symbol.strip()
    ):
        raise PriceNormalizationError(
            f"Yahoo chart result for {ticker} lacks a valid 'symbol' identifier."
        )
    expected_symbol = ticker.upper()
    if provider_symbol != expected_symbol:
        raise PriceNormalizationError(
            f"Yahoo chart result identifies symbol {provider_symbol!r}, not "
            f"requested symbol {expected_symbol!r}."
        )
    exchange_timezone = meta.get("exchangeTimezoneName")
    if not isinstance(exchange_timezone, str) or not exchange_timezone:
        raise PriceNormalizationError(
            f"Yahoo chart result for {ticker} lacks an exchange timezone."
        )

    indicators = result.get("indicators")
    if not isinstance(indicators, dict):
        raise PriceNormalizationError(
            f"Yahoo chart result for {ticker} is missing the 'indicators' object."
        )
    quotes = indicators.get("quote")
    if (
        not isinstance(quotes, list)
        or len(quotes) != 1
        or not isinstance(quotes[0], dict)
    ):
        raise PriceNormalizationError(
            f"Yahoo chart result for {ticker} lacks one quote indicator object."
        )
    quote = cast(dict[str, object], quotes[0])

    timestamps = result.get("timestamp")
    if timestamps is None or timestamps == []:
        return _empty_raw_chart_frame()
    if not isinstance(timestamps, list):
        raise PriceNormalizationError(
            f"Yahoo chart timestamps for {ticker} must be a list."
        )
    invalid_timestamps = [
        position
        for position, value in enumerate(timestamps)
        if isinstance(value, bool) or not isinstance(value, int)
    ]
    if invalid_timestamps:
        raise PriceNormalizationError(
            f"Yahoo chart timestamps for {ticker} contain non-integer epoch "
            f"values at positions {invalid_timestamps[:5]}."
        )

    frame_values = {
        output_field: _raw_indicator_values(
            quote, source_field, len(timestamps), ticker
        )
        for output_field, source_field in _RAW_QUOTE_FIELDS.items()
    }

    adjusted_close = indicators.get("adjclose")
    if adjusted_close is not None:
        if (
            not isinstance(adjusted_close, list)
            or len(adjusted_close) != 1
            or not isinstance(adjusted_close[0], dict)
        ):
            raise PriceNormalizationError(
                f"Yahoo chart result for {ticker} has a malformed adjusted-close "
                "indicator."
            )
        frame_values["Adj Close"] = _raw_indicator_values(
            cast(dict[str, object], adjusted_close[0]),
            "adjclose",
            len(timestamps),
            ticker,
        )

    try:
        market_index = pd.DatetimeIndex(
            pd.to_datetime(timestamps, unit="s", utc=True)
        ).tz_convert(exchange_timezone)
    except (OverflowError, TypeError, ValueError) as error:
        raise PriceNormalizationError(
            f"Yahoo chart timestamps for {ticker} cannot be converted to valid "
            "market datetimes."
        ) from error
    market_index.name = "Date"
    provider_columns: dict[str, pd.Series] = {}
    for field, values in frame_values.items():
        try:
            provider_columns[field] = build_lossless_real_numeric_series(
                values, name=field
            )
        except NumericDtypeHarmonizationError as error:
            raise PriceNormalizationError(
                f"Yahoo chart field {field!r} for {ticker} cannot be represented "
                "losslessly as a supported raw numeric Series."
            ) from error
    provider_frame = pd.DataFrame(provider_columns)
    provider_frame.index = market_index
    return cast(
        pd.DataFrame, yf_utils.fix_Yahoo_dst_issue(provider_frame, interval="1d")
    )


def _request_raw_yfinance_chart(
    ticker: str, start_date: date, end_date: date
) -> object:
    """Request the pinned yfinance 1.5.2 raw chart payload.

    yfinance 1.5.2 exposes no public Ticker API for the raw indicator arrays.
    This boundary deliberately uses its private Ticker timezone lookup, date
    parser, chart URL constant, and owned uncached ``_data.get`` transport. That
    transport retains the package's session, cookies, crumb, retry, error, and
    rate-limit behavior. Its non-expiring in-process response cache is bypassed
    so ``retrieved_at`` can describe the response fetched for the current
    source observation; private objects never leave this function.
    """

    ticker_client = yf.Ticker(ticker)
    timezone_lookup = getattr(ticker_client, "_get_ticker_tz", None)
    data_client = getattr(ticker_client, "_data", None)
    if not callable(timezone_lookup) or data_client is None:
        raise PriceNormalizationError(
            "The pinned yfinance 1.5.2 raw chart adapter requires "
            "Ticker._get_ticker_tz and Ticker._data."
        )

    direct_get = getattr(data_client, "get", None)
    if not callable(direct_get):
        raise PriceNormalizationError(
            "The pinned yfinance 1.5.2 raw chart adapter requires Ticker._data.get."
        )

    exchange_timezone = timezone_lookup(timeout=10)
    if not isinstance(exchange_timezone, str) or not exchange_timezone:
        raise PriceNormalizationError(
            f"yfinance could not resolve an exchange timezone for {ticker}."
        )

    start_timestamp = int(
        yf_utils._parse_user_dt(start_date, exchange_timezone).timestamp()
    )
    end_timestamp = int(
        yf_utils._parse_user_dt(end_date, exchange_timezone).timestamp()
    )
    parameters = {
        "period1": start_timestamp,
        "period2": end_timestamp,
        "interval": "1d",
        "includePrePost": False,
        "events": "div,splits,capitalGains",
    }
    url = f"{_BASE_URL_}/v8/finance/chart/{ticker}"

    # A fresh source-vintage retrieval must not reuse an LRU-cached response and
    # then label that older payload with a new client-side retrieval timestamp.
    response = direct_get(url=url, params=parameters, timeout=10)
    if response is None:
        raise PriceNormalizationError(
            f"yfinance returned no chart response object for {ticker}."
        )
    response_text = getattr(response, "text", "")
    if isinstance(response_text, str) and "Will be right back" in response_text:
        raise PriceNormalizationError("Yahoo Finance is temporarily unavailable.")
    response_json = getattr(response, "json", None)
    if not callable(response_json):
        raise PriceNormalizationError(
            f"yfinance returned a chart response without JSON access for {ticker}."
        )
    return response_json()


def _validate_requested_date_window(
    normalized: pd.DataFrame,
    ticker: str,
    start_date: date,
    end_date: date,
) -> None:
    if normalized.empty:
        return

    outside_window = normalized["date"].lt(pd.Timestamp(start_date)) | normalized[
        "date"
    ].ge(pd.Timestamp(end_date))
    if outside_window.any():
        offending_dates = (
            normalized.loc[outside_window, "date"]
            .drop_duplicates()
            .sort_values()
            .dt.strftime("%Y-%m-%d")
            .head(5)
            .tolist()
        )
        raise PriceNormalizationError(
            f"yfinance output for {ticker} contains dates outside the requested "
            f"[{start_date.isoformat()}, {end_date.isoformat()}) window: "
            f"{offending_dates}."
        )


def normalize_yfinance_output(
    provider_data: pd.DataFrame | None,
    requested_tickers: Sequence[str],
    retrieved_at: datetime | pd.Timestamp,
) -> pd.DataFrame:
    """Convert current yfinance daily output layouts to canonical long format.

    The normalizer accepts flat single-ticker history and two-level yfinance
    download columns in either field/ticker or ticker/field order. It maps a
    distinct ``Adj Close`` field to ``adjusted_close`` but leaves that canonical
    field missing when the provider does not supply it. It does not substitute
    source ``Close`` or fill any market values.

    Args:
        provider_data: DataFrame from the raw chart adapter or a compatible
            current yfinance layout.
        requested_tickers: Tickers expected in the response.
        retrieved_at: Timezone-aware timestamp shared by this batch.

    Returns:
        Canonical daily observations sorted by ticker and date.

    Raises:
        PriceNormalizationError: If a non-empty response has an unsupported or
            incomplete layout.
        PriceDataValidationError: If normalized market values fail validation.
        ValueError: If tickers or retrieval time are invalid.
    """

    tickers = _normalize_tickers(requested_tickers)
    batch_timestamp = _retrieval_timestamp(retrieved_at)
    if provider_data is None or provider_data.empty:
        return _empty_price_data()

    normalized_frames: list[pd.DataFrame] = []
    if not isinstance(provider_data.columns, pd.MultiIndex):
        if len(tickers) != 1:
            raise PriceNormalizationError(
                "Flat yfinance columns can only be assigned to one requested ticker."
            )
        normalized_frames.append(
            _normalize_single_ticker(provider_data, tickers[0], batch_timestamp)
        )
    else:
        if provider_data.columns.nlevels != 2:
            raise PriceNormalizationError(
                "Expected yfinance columns to have exactly two levels."
            )
        field_level = _provider_field_level(provider_data.columns)
        ticker_level = 1 - field_level
        ticker_labels = provider_data.columns.get_level_values(ticker_level).unique()

        for ticker in tickers:
            matching_labels = [
                label
                for label in ticker_labels
                if str(label).strip().casefold() == ticker.casefold()
            ]
            if not matching_labels:
                continue
            if len(matching_labels) > 1:
                raise PriceNormalizationError(
                    f"yfinance returned ambiguous column labels for {ticker}."
                )
            ticker_data = provider_data.xs(
                matching_labels[0],
                axis=1,
                level=ticker_level,
                drop_level=True,
            )
            if isinstance(ticker_data, pd.Series):
                ticker_data = ticker_data.to_frame()
            normalized_frames.append(
                _normalize_single_ticker(ticker_data, ticker, batch_timestamp)
            )

    if not normalized_frames:
        return _empty_price_data()

    normalized = pd.concat(normalized_frames, ignore_index=True)
    normalized = deduplicate_price_data(normalized)
    normalized = normalized.sort_values(
        ["ticker", "date"], kind="mergesort"
    ).reset_index(drop=True)
    validate_price_data(normalized)
    return normalized


def _download_yfinance_ticker(
    ticker: str, start_date: date, end_date: date
) -> pd.DataFrame | None:
    with yfinance_exception_visibility():
        # yfinance 1.5.2 uses this public config setting to surface provider
        # exceptions. Raw arrays are requested before its history processing can
        # synthesize adjusted close, zero-fill volume, or discard empty rows.
        payload = _request_raw_yfinance_chart(ticker, start_date, end_date)
    return _raw_chart_to_provider_frame(payload, ticker)


def download_price_history(
    tickers: Sequence[str],
    start: str | date = DEFAULT_PRICE_START_DATE,
    end: str | date | None = None,
    *,
    downloader: PriceDownloadCallable | None = None,
    retrieved_at: datetime | pd.Timestamp | None = None,
    default_end_reference_time: datetime | None = None,
) -> PriceDownloadResult:
    """Download and normalize daily prices without failing the whole batch.

    Each ticker is requested independently so an empty or failed provider call
    can be reported without fabricating rows or discarding successful tickers.
    The date range uses yfinance semantics: ``start`` is inclusive and ``end``
    is exclusive. When ``end`` is omitted, the cutoff is the current calendar
    date in America/New_York, so only earlier U.S. market dates are requested.

    Args:
        tickers: ETF tickers to request.
        start: Inclusive first date in ``YYYY-MM-DD`` format or as a date.
        end: Exclusive end date. Defaults to the current America/New_York date.
        downloader: Optional compatible callable for offline tests.
        retrieved_at: Optional timezone-aware UTC override applied to every
            provider response for deterministic callers and tests. When omitted,
            each ticker is timestamped immediately after its downloader returns.
        default_end_reference_time: Optional timezone-aware instant used only to
            determine a default end date in deterministic tests. Explicit
            ``end`` values bypass this reference time.

    Returns:
        Canonical observations and a structured status for every ticker.

    Raises:
        ValueError: If tickers, date bounds, or retrieval time are invalid.
    """

    requested_tickers = _normalize_tickers(tickers)
    start_date = _parse_date(start, "start")
    end_date = (
        _parse_date(end, "end")
        if end is not None
        else get_default_price_end_date(default_end_reference_time)
    )
    if start_date >= end_date:
        raise ValueError("start must be earlier than the exclusive end date.")

    timestamp_override = (
        _retrieval_timestamp(retrieved_at) if retrieved_at is not None else None
    )
    download = downloader or _download_yfinance_ticker
    statuses: list[TickerDownloadStatus] = []
    successful_frames: list[pd.DataFrame] = []

    for ticker in requested_tickers:
        response_timestamp: pd.Timestamp | None = None
        try:
            provider_data = download(ticker, start_date, end_date)
            response_timestamp = timestamp_override or _utc_now()
            normalized = normalize_yfinance_output(
                provider_data, [ticker], response_timestamp
            )
            _validate_requested_date_window(normalized, ticker, start_date, end_date)
        except Exception as error:  # Provider libraries expose varied failures.
            error_message = _format_price_download_failure(error)
            LOGGER.warning("Price download failed for %s: %s", ticker, error_message)
            statuses.append(
                TickerDownloadStatus(
                    ticker=ticker,
                    status="failed",
                    rows_received=0,
                    first_date=None,
                    last_date=None,
                    retrieved_at=None,
                    query_start=start_date,
                    query_end=end_date,
                    returned_dates=(),
                    error=error_message,
                )
            )
            continue

        if normalized.empty:
            LOGGER.warning("Price download returned no usable data for %s.", ticker)
            statuses.append(
                TickerDownloadStatus(
                    ticker=ticker,
                    status="empty",
                    rows_received=0,
                    first_date=None,
                    last_date=None,
                    retrieved_at=response_timestamp,
                    query_start=start_date,
                    query_end=end_date,
                    returned_dates=(),
                )
            )
            continue

        returned_dates = tuple(normalized["date"].dt.date)
        successful_frames.append(normalized)
        statuses.append(
            TickerDownloadStatus(
                ticker=ticker,
                status="success",
                rows_received=len(normalized),
                first_date=returned_dates[0],
                last_date=returned_dates[-1],
                retrieved_at=response_timestamp,
                query_start=start_date,
                query_end=end_date,
                returned_dates=returned_dates,
            )
        )

    if successful_frames:
        prices = pd.concat(successful_frames, ignore_index=True)
        prices = deduplicate_price_data(prices)
        prices = prices.sort_values(["ticker", "date"], kind="mergesort").reset_index(
            drop=True
        )
        validate_price_data(prices)
    else:
        prices = _empty_price_data()

    observed_timestamps = [
        _retrieval_timestamp(status.retrieved_at)
        for status in statuses
        if status.retrieved_at is not None
    ]
    return PriceDownloadResult(
        prices=prices,
        statuses=tuple(statuses),
        retrieved_at=max(observed_timestamps) if observed_timestamps else None,
    )


def _price_history_lock_path(output_path: Path) -> Path:
    resolved_output_path = output_path.resolve(strict=False)
    return resolved_output_path.with_name(f".{resolved_output_path.name}.lock")


def _try_lock_file(lock_file: BinaryIO) -> None:
    if sys.platform == "win32":
        import msvcrt

        # msvcrt locks from the current position. Every process uses byte zero
        # and keeps this handle open for the full canonical transaction.
        lock_file.seek(0)
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        return

    import fcntl

    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_file(lock_file: BinaryIO) -> None:
    if sys.platform == "win32":
        import msvcrt

        lock_file.seek(0)
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


@contextmanager
def _price_history_transaction_lock(output_path: Path) -> Iterator[None]:
    """Hold an adjacent cross-process lock for one canonical output path."""

    lock_path = _price_history_lock_path(output_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + _PRICE_HISTORY_LOCK_TIMEOUT_SECONDS

    with lock_path.open("a+b") as lock_file:
        if sys.platform == "win32":
            lock_file.seek(0, os.SEEK_END)
            if lock_file.tell() == 0:
                lock_file.write(b"\0")
                lock_file.flush()

        while True:
            try:
                _try_lock_file(lock_file)
                break
            except OSError as error:
                contention_errnos = {errno.EACCES, errno.EAGAIN, errno.EDEADLK}
                if error.errno not in contention_errnos:
                    raise

                remaining_seconds = deadline - time.monotonic()
                if remaining_seconds <= 0:
                    raise TimeoutError(
                        "Timed out after "
                        f"{_PRICE_HISTORY_LOCK_TIMEOUT_SECONDS:g} seconds waiting "
                        f"for the price-history lock for {output_path}."
                    ) from error
                time.sleep(min(_PRICE_HISTORY_LOCK_POLL_SECONDS, remaining_seconds))

        try:
            yield
        finally:
            _unlock_file(lock_file)


def _write_parquet_atomically(data: pd.DataFrame, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{output_path.stem}.",
            suffix=".tmp.parquet",
            dir=output_path.parent,
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)

        data.to_parquet(temporary_path, index=False)
        os.replace(temporary_path, output_path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _snapshot_timestamp(value: datetime | None) -> datetime:
    timestamp = value if value is not None else datetime.now(UTC)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("snapshot_time must be timezone-aware.")
    return timestamp.astimezone(UTC)


def _snapshot_existing_price_file(
    output_path: Path,
    snapshot_dir: Path,
    snapshot_time: datetime,
) -> Path:
    """Atomically publish an exact, non-overwriting copy of canonical prices."""

    snapshot_dir.mkdir(parents=True, exist_ok=True)
    timestamp_label = snapshot_time.strftime("%Y%m%dT%H%M%S%fZ")
    temporary_path: Path | None = None

    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{output_path.stem}.{timestamp_label}.",
            suffix=".tmp.parquet",
            dir=snapshot_dir,
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            with output_path.open("rb") as canonical_file:
                shutil.copyfileobj(canonical_file, temporary_file)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())

        collision_number = 0
        while True:
            collision_suffix = (
                "" if collision_number == 0 else f"_{collision_number:03d}"
            )
            snapshot_path = snapshot_dir / (
                f"{output_path.stem}_{timestamp_label}{collision_suffix}"
                f"{output_path.suffix}"
            )
            try:
                # Linking publishes the completed temporary file atomically and
                # fails instead of overwriting an existing snapshot.
                os.link(temporary_path, snapshot_path)
            except FileExistsError:
                collision_number += 1
                continue

            temporary_path.unlink()
            temporary_path = None
            return snapshot_path
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _merge_price_vintages(
    existing: pd.DataFrame,
    incoming: pd.DataFrame,
) -> tuple[pd.DataFrame, int, tuple[str, ...]]:
    """Merge validated source vintages without mixing fields across rows."""

    key_columns = ["ticker", "date"]
    market_columns = list(PRICE_VALUE_COLUMNS)
    existing_by_key = existing.set_index(key_columns, drop=False)
    incoming_by_key = incoming.set_index(key_columns, drop=False)
    overlap_keys = existing_by_key.index.intersection(incoming_by_key.index, sort=False)

    if overlap_keys.empty:
        combined = pd.concat([existing, incoming], ignore_index=True)
        return combined, 0, ()

    existing_overlap = existing_by_key.loc[overlap_keys]
    incoming_overlap = incoming_by_key.loc[overlap_keys]
    existing_missing = existing_overlap[market_columns].isna()
    incoming_missing = incoming_overlap[market_columns].isna()
    loses_existing_values = (~existing_missing & incoming_missing).any(axis=1)
    if loses_existing_values.any():
        affected_keys = list(overlap_keys[loses_existing_values])[:5]
        raise PriceDataValidationError(
            "Incoming source vintage loses previously available market values "
            f"for ticker/date pairs: {affected_keys}. Manual review is required."
        )

    identical_fields = pd.Series(True, index=overlap_keys, dtype=bool)
    for column in market_columns:
        both_missing = existing_missing[column] & incoming_missing[column]
        both_present = ~existing_missing[column] & ~incoming_missing[column]
        equal_when_present = pd.Series(False, index=overlap_keys, dtype=bool)
        if both_present.any():
            present_comparison = existing_overlap.loc[both_present, column].eq(
                incoming_overlap.loc[both_present, column]
            )
            equal_when_present.loc[both_present] = present_comparison.fillna(
                False
            ).astype(bool)
        field_is_identical = both_missing | (both_present & equal_when_present)
        identical_fields &= field_is_identical.astype(bool)

    revised_keys = overlap_keys[~identical_fields]
    existing_overlap_times = existing_by_key.loc[overlap_keys, "retrieved_at"]
    incoming_overlap_times = incoming_by_key.loc[overlap_keys, "retrieved_at"]
    newer_identical_keys = overlap_keys[
        identical_fields & incoming_overlap_times.gt(existing_overlap_times)
    ]
    if revised_keys.empty:
        retained_existing = existing_by_key.copy()
        if not newer_identical_keys.empty:
            retained_existing.loc[newer_identical_keys, "retrieved_at"] = (
                incoming_by_key.loc[newer_identical_keys, "retrieved_at"].array
            )
        new_rows = incoming_by_key.loc[
            ~incoming_by_key.index.isin(existing_by_key.index)
        ]
        combined = pd.concat(
            [
                retained_existing.loc[:, list(CANONICAL_PRICE_COLUMNS)],
                new_rows.loc[:, list(CANONICAL_PRICE_COLUMNS)],
            ],
            ignore_index=True,
        )
        return combined, 0, ()

    existing_revision_times = existing_by_key.loc[revised_keys, "retrieved_at"]
    incoming_revision_times = incoming_by_key.loc[revised_keys, "retrieved_at"]
    stale_revisions = incoming_revision_times.lt(existing_revision_times)
    if stale_revisions.any():
        affected_keys = list(revised_keys[stale_revisions])[:5]
        raise PriceDataValidationError(
            "Incoming source vintage is older than the canonical source vintage "
            f"for ticker/date pairs: {affected_keys}."
        )

    same_vintage_conflicts = incoming_revision_times.eq(existing_revision_times)
    if same_vintage_conflicts.any():
        affected_keys = list(revised_keys[same_vintage_conflicts])[:5]
        raise PriceDataValidationError(
            "Different market values claim the same retrieved_at source vintage "
            f"for ticker/date pairs: {affected_keys}."
        )

    revised_rows = incoming_by_key.loc[revised_keys, list(CANONICAL_PRICE_COLUMNS)]
    new_rows = incoming_by_key.loc[~incoming_by_key.index.isin(existing_by_key.index)]
    retained_existing = existing_by_key.loc[
        ~existing_by_key.index.isin(revised_keys)
    ].copy()
    if not newer_identical_keys.empty:
        retained_existing.loc[newer_identical_keys, "retrieved_at"] = (
            incoming_by_key.loc[newer_identical_keys, "retrieved_at"].array
        )
    combined = pd.concat(
        [
            retained_existing.loc[:, list(CANONICAL_PRICE_COLUMNS)],
            revised_rows,
            new_rows.loc[:, list(CANONICAL_PRICE_COLUMNS)],
        ],
        ignore_index=True,
    )
    revised_tickers = tuple(sorted(revised_rows["ticker"].astype(str).unique()))
    return combined, len(revised_rows), revised_tickers


def _harmonize_price_numeric_dtypes(
    existing: pd.DataFrame,
    incoming: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    harmonized_existing = existing.copy()
    harmonized_incoming = incoming.copy()
    for column in PRICE_VALUE_COLUMNS:
        try:
            existing_values, incoming_values = harmonize_real_numeric_series(
                existing[column], incoming[column]
            )
        except NumericDtypeHarmonizationError as error:
            raise PriceDataValidationError(
                f"Cannot losslessly combine canonical '{column}' dtypes "
                f"{existing[column].dtype} and {incoming[column].dtype}: {error}."
            ) from error
        harmonized_existing[column] = existing_values
        harmonized_incoming[column] = incoming_values
    return harmonized_existing, harmonized_incoming


def validate_price_retrieval_statuses(
    incoming: pd.DataFrame,
    retrieval_statuses: Sequence[TickerDownloadStatus],
    *,
    expected_tickers: Sequence[str] | None = None,
    expected_query_start: date | None = None,
    expected_query_end: date | None = None,
) -> tuple[TickerDownloadStatus, ...]:
    """Validate retrieval coverage against its exact canonical acquisition rows.

    Args:
        incoming: Canonical rows attributed to the supplied retrieval statuses.
            It must contain exactly the rows claimed by successful statuses and
            no rows attributed to empty or failed statuses.
        retrieval_statuses: Per-ticker provider outcomes to validate.
        expected_tickers: Optional exact requested ticker population.
        expected_query_start: Optional required inclusive query start shared by
            every status.
        expected_query_end: Optional required exclusive query end shared by every
            status.

    Returns:
        Successful statuses in their supplied order.

    Raises:
        PriceDataValidationError: If status types, values, population, query
            bounds, or canonical row relationships are invalid.
    """

    statuses = tuple(retrieval_statuses)
    if (expected_query_start is None) != (expected_query_end is None):
        raise PriceDataValidationError(
            "Expected price retrieval query bounds must be supplied together."
        )
    if expected_query_start is not None and (
        type(expected_query_start) is not date
        or type(expected_query_end) is not date
        or expected_query_start >= cast(date, expected_query_end)
    ):
        raise PriceDataValidationError(
            "Expected price retrieval query bounds must be a nonempty date window."
        )

    expected_ticker_tuple: tuple[str, ...] | None = None
    if expected_tickers is not None:
        expected_ticker_tuple = tuple(expected_tickers)
        expected_keys: set[str] = set()
        for ticker in expected_ticker_tuple:
            if type(ticker) is not str or not ticker or ticker != ticker.strip():
                raise PriceDataValidationError(
                    "Expected price retrieval tickers must be non-empty exact "
                    "strings without surrounding whitespace."
                )
            ticker_key = ticker.casefold()
            if ticker_key in expected_keys:
                raise PriceDataValidationError(
                    f"Expected price retrieval tickers contain duplicate {ticker!r}."
                )
            expected_keys.add(ticker_key)

    seen_tickers: set[str] = set()
    successful_statuses: list[TickerDownloadStatus] = []
    common_query_window: tuple[date, date] | None = None
    for status in statuses:
        if not isinstance(status, TickerDownloadStatus):
            raise PriceDataValidationError(
                "Price retrieval coverage must contain TickerDownloadStatus values."
            )
        if (
            type(status.ticker) is not str
            or not status.ticker
            or status.ticker != status.ticker.strip()
        ):
            raise PriceDataValidationError(
                "Price retrieval coverage tickers must be non-empty strings "
                "without surrounding whitespace."
            )
        ticker_key = status.ticker.casefold()
        if ticker_key in seen_tickers:
            raise PriceDataValidationError(
                f"Price retrieval coverage contains duplicate ticker {status.ticker!r}."
            )
        seen_tickers.add(ticker_key)
        if type(status.query_start) is not date or type(status.query_end) is not date:
            raise PriceDataValidationError(
                f"Price retrieval coverage for {status.ticker} must use date bounds."
            )
        if status.query_start >= status.query_end:
            raise PriceDataValidationError(
                f"Price retrieval coverage for {status.ticker} has an invalid "
                "query window."
            )
        status_query_window = (status.query_start, status.query_end)
        if common_query_window is None:
            common_query_window = status_query_window
        elif status_query_window != common_query_window:
            raise PriceDataValidationError(
                "Price retrieval coverage must use one common query window for "
                "every ticker status."
            )
        if type(status.status) is not str:
            raise PriceDataValidationError(
                f"Price retrieval coverage for {status.ticker} must use a string "
                "status."
            )
        if type(status.rows_received) is not int:
            raise PriceDataValidationError(
                f"Price retrieval coverage for {status.ticker} must use an exact "
                "integer rows_received value."
            )
        if status.first_date is not None and type(status.first_date) is not date:
            raise PriceDataValidationError(
                f"Price retrieval coverage for {status.ticker} has an invalid "
                "first_date type."
            )
        if status.last_date is not None and type(status.last_date) is not date:
            raise PriceDataValidationError(
                f"Price retrieval coverage for {status.ticker} has an invalid "
                "last_date type."
            )
        if status.error is not None and type(status.error) is not str:
            raise PriceDataValidationError(
                f"Price retrieval coverage for {status.ticker} has an invalid "
                "error type."
            )
        if type(status.returned_dates) is not tuple or any(
            type(value) is not date for value in status.returned_dates
        ):
            raise PriceDataValidationError(
                f"Price retrieval coverage for {status.ticker} must contain a "
                "tuple of returned dates."
            )
        if status.returned_dates != tuple(sorted(set(status.returned_dates))):
            raise PriceDataValidationError(
                f"Price retrieval coverage for {status.ticker} must contain "
                "unique dates in ascending order."
            )
        if any(
            value < status.query_start or value >= status.query_end
            for value in status.returned_dates
        ):
            raise PriceDataValidationError(
                f"Price retrieval coverage for {status.ticker} contains returned "
                "dates outside its query window."
            )

        if status.status == "success":
            expected_first = status.returned_dates[0] if status.returned_dates else None
            expected_last = status.returned_dates[-1] if status.returned_dates else None
            if (
                status.rows_received <= 0
                or not status.returned_dates
                or status.rows_received != len(status.returned_dates)
                or status.first_date != expected_first
                or status.last_date != expected_last
                or status.error is not None
            ):
                raise PriceDataValidationError(
                    f"Successful price retrieval coverage for {status.ticker} is "
                    "internally inconsistent."
                )
            if not isinstance(status.retrieved_at, pd.Timestamp):
                raise PriceDataValidationError(
                    f"Price retrieval coverage for {status.ticker} must use a "
                    "pandas Timestamp for retrieved_at."
                )
            try:
                _retrieval_timestamp(status.retrieved_at)
            except (TypeError, ValueError) as error:
                raise PriceDataValidationError(
                    f"Price retrieval coverage for {status.ticker} has an invalid "
                    "retrieved_at timestamp."
                ) from error
            successful_statuses.append(status)
        elif status.status == "empty":
            if (
                status.rows_received != 0
                or status.first_date is not None
                or status.last_date is not None
                or status.returned_dates
                or status.error is not None
            ):
                raise PriceDataValidationError(
                    f"Empty price retrieval coverage for {status.ticker} is "
                    "internally inconsistent."
                )
            if not isinstance(status.retrieved_at, pd.Timestamp):
                raise PriceDataValidationError(
                    f"Price retrieval coverage for {status.ticker} must use a "
                    "pandas Timestamp for retrieved_at."
                )
            try:
                _retrieval_timestamp(status.retrieved_at)
            except (TypeError, ValueError) as error:
                raise PriceDataValidationError(
                    f"Price retrieval coverage for {status.ticker} has an invalid "
                    "retrieved_at timestamp."
                ) from error
        elif status.status == "failed":
            if (
                status.rows_received != 0
                or status.first_date is not None
                or status.last_date is not None
                or status.returned_dates
                or status.retrieved_at is not None
                or type(status.error) is not str
                or not status.error
                or status.error != status.error.strip()
            ):
                raise PriceDataValidationError(
                    f"Failed price retrieval coverage for {status.ticker} is "
                    "internally inconsistent."
                )
        else:
            raise PriceDataValidationError(
                f"Price retrieval coverage for {status.ticker} has unsupported "
                f"status {status.status!r}."
            )

    if expected_query_start is not None and common_query_window is not None:
        if common_query_window[0] != expected_query_start:
            raise PriceDataValidationError(
                "The common price retrieval query start does not match the "
                "required expected start."
            )
        if common_query_window[1] != expected_query_end:
            raise PriceDataValidationError(
                "The common price retrieval query end does not match the required "
                "expected end."
            )

    if expected_ticker_tuple is not None:
        actual_tickers = tuple(status.ticker for status in statuses)
        if len(actual_tickers) != len(expected_ticker_tuple) or set(
            actual_tickers
        ) != set(expected_ticker_tuple):
            raise PriceDataValidationError(
                "Price retrieval coverage tickers do not exactly match the "
                "expected ticker population."
            )

    validate_price_data(incoming)
    incoming_tickers = set(incoming["ticker"].array)
    successful_tickers = {status.ticker for status in successful_statuses}
    if incoming_tickers != successful_tickers:
        raise PriceDataValidationError(
            "Successful price retrieval coverage tickers do not exactly match "
            f"incoming canonical tickers: coverage={sorted(successful_tickers)}, "
            f"incoming={sorted(incoming_tickers)}."
        )

    for status in successful_statuses:
        ticker_rows = incoming.loc[incoming["ticker"].eq(status.ticker)]
        incoming_dates = tuple(
            ticker_rows["date"].sort_values(kind="mergesort").dt.date
        )
        if incoming_dates != status.returned_dates:
            raise PriceDataValidationError(
                f"Price retrieval coverage dates for {status.ticker} do not "
                "exactly match the incoming canonical rows."
            )
        try:
            status_timestamp = _retrieval_timestamp(
                cast(pd.Timestamp, status.retrieved_at)
            )
        except ValueError as error:
            raise PriceDataValidationError(
                f"Price retrieval coverage for {status.ticker} has an invalid "
                "retrieved_at timestamp."
            ) from error
        if (
            len(ticker_rows) != status.rows_received
            or not ticker_rows["retrieved_at"].eq(status_timestamp).all()
        ):
            raise PriceDataValidationError(
                f"Price retrieval coverage timestamp for {status.ticker} does not "
                "match the incoming canonical rows."
            )

    return tuple(successful_statuses)


def _reject_vanished_price_observations(
    existing: pd.DataFrame,
    successful_statuses: Sequence[TickerDownloadStatus],
) -> None:
    vanished_keys: list[tuple[str, str]] = []
    for status in sorted(successful_statuses, key=lambda item: item.ticker):
        in_coverage = (
            existing["ticker"].eq(status.ticker)
            & existing["date"].ge(pd.Timestamp(status.query_start))
            & existing["date"].lt(pd.Timestamp(status.query_end))
        )
        existing_dates = existing.loc[in_coverage, "date"]
        returned_dates = pd.DatetimeIndex(status.returned_dates)
        vanished_dates = existing_dates.loc[~existing_dates.isin(returned_dates)]
        vanished_keys.extend(
            (status.ticker, value.date().isoformat())
            for value in vanished_dates.sort_values()
        )

    if vanished_keys:
        raise PriceDataValidationError(
            "Previously stored price observations vanished from a later "
            "successful provider response inside confirmed coverage: "
            f"{vanished_keys}. The transaction was rejected for manual review; "
            "no rows were deleted."
        )


def persist_price_history(
    incoming_data: pd.DataFrame,
    output_path: Path,
    *,
    retrieval_statuses: Sequence[TickerDownloadStatus] | None = None,
    snapshot_dir: Path | None = None,
    snapshot_time: datetime | None = None,
) -> PricePersistenceResult:
    """Merge the latest validated source vintage in a serialized transaction.

    Identical overlapping observations retain the existing market values while
    advancing their provenance watermark to a later incoming retrieval time. A
    changed overlap replaces the entire row with the incoming source vintage
    only after the exact superseded canonical Parquet file is safely
    snapshotted. Incoming rows that lose previously available market fields are
    rejected so fields from different source vintages are never silently
    combined. When same-batch retrieval statuses are supplied, a successful
    ticker response that omits an existing observation inside its confirmed
    query window is rejected for manual review; the row is neither retained as
    a current response nor auto-deleted. Empty, failed, and unrequested tickers
    do not trigger this check. An adjacent cross-process lock serializes the
    complete read, coverage comparison, validation, merge, snapshot, and atomic
    canonical replacement for this output path.

    Args:
        incoming_data: Newly downloaded canonical observations.
        output_path: Destination Parquet path.
        retrieval_statuses: Optional structured outcomes from the same
            ``download_price_history`` result. Production refreshes pass these
            statuses to establish successful query coverage. When omitted, no
            absence claim can be made and no disappearance check is applied.
        snapshot_dir: Destination for superseded canonical vintages. Defaults to
            ``data/snapshots/prices`` through the existing project path helper
            and is resolved only when a revision requires a snapshot.
        snapshot_time: Optional timezone-aware snapshot timestamp for tests.

    Returns:
        The written dataset and structured source-revision metadata.

    Raises:
        PriceDataValidationError: If incoming, existing, or combined data are
            invalid or incoming overlaps lose existing market values.
        ValueError: If no incoming rows are available to persist.
        TimeoutError: If another process holds this output path's persistence
            lock for 30 seconds.
        OSError: If existing data cannot be read, a required snapshot cannot be
            preserved, or the canonical file cannot be written.
    """

    with _price_history_transaction_lock(output_path):
        existing: pd.DataFrame | None = None
        if output_path.exists():
            existing = pd.read_parquet(output_path)
            existing = deduplicate_price_data(existing)
            validate_price_data(existing)

        incoming = deduplicate_price_data(incoming_data)
        validate_price_data(incoming)
        if incoming.empty:
            raise ValueError("No price observations are available to persist.")
        successful_statuses: tuple[TickerDownloadStatus, ...] = ()
        if retrieval_statuses is not None:
            successful_statuses = validate_price_retrieval_statuses(
                incoming, retrieval_statuses
            )
        if existing is not None and successful_statuses:
            _reject_vanished_price_observations(existing, successful_statuses)

        revised_row_count = 0
        revised_tickers: tuple[str, ...] = ()
        snapshot_path: Path | None = None
        if existing is not None:
            existing, incoming = _harmonize_price_numeric_dtypes(existing, incoming)
            combined, revised_row_count, revised_tickers = _merge_price_vintages(
                existing, incoming
            )
        else:
            combined = incoming.copy()

        combined = combined.sort_values(
            ["ticker", "date"], kind="mergesort"
        ).reset_index(drop=True)
        combined = combined.loc[:, list(CANONICAL_PRICE_COLUMNS)]
        validate_price_data(combined)

        if revised_row_count:
            resolved_snapshot_dir = snapshot_dir
            if resolved_snapshot_dir is None:
                resolved_snapshot_dir = get_snapshot_data_dir() / "prices"
            snapshot_path = _snapshot_existing_price_file(
                output_path,
                resolved_snapshot_dir,
                _snapshot_timestamp(snapshot_time),
            )

        _write_parquet_atomically(combined, output_path)
        return PricePersistenceResult(
            prices=combined,
            revised_row_count=revised_row_count,
            revised_tickers=revised_tickers,
            snapshot_path=snapshot_path,
        )
