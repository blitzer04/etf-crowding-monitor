"""Download, normalize, and persist historical ETF shares outstanding."""

from __future__ import annotations

import errno
import logging
import math
import os
import shutil
import sys
import tempfile
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from numbers import Real
from pathlib import Path
from typing import BinaryIO, Literal

import pandas as pd
import yfinance as yf  # type: ignore[import-untyped]
from pandas.api.types import is_any_real_numeric_dtype
from yfinance import utils as yf_utils  # type: ignore[import-untyped]
from yfinance.const import _BASE_URL_  # type: ignore[import-untyped]

from etf_crowding.data.numeric_dtypes import (
    NumericDtypeHarmonizationError,
    build_lossless_real_numeric_series,
    harmonize_real_numeric_series,
)
from etf_crowding.data.share_validation import (
    CANONICAL_SHARE_COLUMNS,
    ShareDataValidationError,
    deduplicate_share_data,
    validate_share_data,
)
from etf_crowding.data.yfinance_runtime import yfinance_exception_visibility
from etf_crowding.paths import get_snapshot_data_dir

DEFAULT_SHARES_FILENAME = "etf_shares_outstanding.parquet"
_SHARES_HISTORY_LOCK_TIMEOUT_SECONDS = 30.0
_SHARES_HISTORY_LOCK_POLL_SECONDS = 0.05

ShareDownloadStatusValue = Literal["success", "empty", "failed"]
ShareDownloadCallable = Callable[[str, date | None, date | None], pd.Series | None]

LOGGER = logging.getLogger(__name__)


class SharesNormalizationError(ValueError):
    """Indicate that provider shares output cannot be normalized safely."""


@dataclass(frozen=True, slots=True)
class ShareDownloadStatus:
    """Describe one ticker's shares-outstanding provider outcome.

    Attributes:
        ticker: Requested ETF ticker.
        status: One of ``success``, ``empty``, or ``failed``.
        rows_received: Number of canonical source observations returned.
        first_date: Earliest returned source observation date, when available.
        last_date: Latest returned source observation date, when available.
        retrieved_at: UTC time when a successful or empty response returned.
            Failed outcomes have no successful retrieval timestamp.
        error: Provider or normalization error for failed outcomes.
    """

    ticker: str
    status: ShareDownloadStatusValue
    rows_received: int
    first_date: date | None
    last_date: date | None
    retrieved_at: datetime | None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class ShareDownloadResult:
    """Hold canonical shares rows and structured per-ticker outcomes.

    Attributes:
        shares: Successful source observations in canonical long format.
        statuses: Outcome for every requested ticker, in request order.
        retrieved_at: Latest successful or empty response timestamp, or
            ``None`` if no such response was observed.
    """

    shares: pd.DataFrame
    statuses: tuple[ShareDownloadStatus, ...]
    retrieved_at: datetime | None

    @property
    def successful_tickers(self) -> tuple[str, ...]:
        """Return tickers with at least one dated source observation."""

        return tuple(item.ticker for item in self.statuses if item.status == "success")

    @property
    def empty_tickers(self) -> tuple[str, ...]:
        """Return tickers for which the provider returned no dated history."""

        return tuple(item.ticker for item in self.statuses if item.status == "empty")

    @property
    def failed_tickers(self) -> tuple[str, ...]:
        """Return tickers whose provider request or normalization failed."""

        return tuple(item.ticker for item in self.statuses if item.status == "failed")


@dataclass(frozen=True, slots=True)
class SharePersistenceResult:
    """Describe a successful canonical shares-history write.

    Attributes:
        shares: Validated latest source vintage written to the canonical file.
        revised_row_count: Overlapping rows replaced by a changed newer source
            vintage, including missing-to-present completions.
        revised_tickers: Tickers affected by accepted source-vintage revisions.
        snapshot_path: Exact superseded canonical snapshot, or ``None`` when no
            overlapping shares value changed.
    """

    shares: pd.DataFrame
    revised_row_count: int
    revised_tickers: tuple[str, ...]
    snapshot_path: Path | None


def _empty_share_data() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": pd.Series(dtype="datetime64[ns]"),
            "ticker": pd.Series(dtype="string"),
            "shares_outstanding": pd.Series(dtype="Float64"),
            "retrieved_at": pd.Series(dtype="datetime64[ns, UTC]"),
        }
    )


def _parse_optional_date(value: str | date | None, name: str) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{name} must use YYYY-MM-DD format: {value!r}.") from error


def _retrieval_timestamp(value: datetime | pd.Timestamp) -> datetime:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        raise ValueError("retrieved_at must be timezone-aware.")
    return timestamp.tz_convert("UTC").to_pydatetime()


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _query_reference_time() -> datetime:
    return datetime.now(UTC)


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


def _normalize_provider_dates(index: pd.Index) -> pd.DatetimeIndex:
    if not isinstance(index, pd.DatetimeIndex):
        raise SharesNormalizationError(
            "The yfinance shares response must use a pandas DatetimeIndex; "
            f"received {type(index).__name__}."
        )
    if index.tz is None:
        raise SharesNormalizationError(
            "The yfinance shares response index must be timezone-aware."
        )
    if index.hasnans:
        raise SharesNormalizationError(
            "The yfinance shares response contains missing or invalid dates."
        )

    # yfinance 1.5.2 localizes shares timestamps to the exchange timezone.
    # Removing that timezone without conversion preserves its source date.
    return index.tz_localize(None).normalize()


def normalize_yfinance_shares_output(
    provider_data: pd.Series | None,
    ticker: str,
    retrieved_at: datetime | pd.Timestamp,
) -> pd.DataFrame:
    """Normalize one yfinance shares Series without filling observations.

    The installed yfinance 1.5.2 ``Ticker.get_shares_full`` implementation
    returns a Series indexed by exchange-local, timezone-aware timestamps.
    Dated missing values are retained because that implementation does not drop
    null timestamp/value pairs. No price-calendar alignment is performed.

    Args:
        provider_data: One ticker's yfinance-compatible shares Series or
            ``None`` for genuine no-history output.
        ticker: Requested ETF ticker.
        retrieved_at: Timezone-aware client retrieval timestamp.

    Returns:
        Canonical shares observations sorted by ticker and source date.

    Raises:
        SharesNormalizationError: If the provider schema, dates, or shares dtype
            do not match the inspected yfinance contract.
        ShareDataValidationError: If normalized observations violate the
            canonical data contract.
        ValueError: If ``retrieved_at`` is timezone-naive.
    """

    timestamp = _retrieval_timestamp(retrieved_at)
    if provider_data is None:
        return _empty_share_data()
    if not isinstance(provider_data, pd.Series):
        raise SharesNormalizationError(
            "The yfinance shares response must be a pandas Series; "
            f"received {type(provider_data).__name__}."
        )
    if not is_any_real_numeric_dtype(provider_data.dtype):
        raise SharesNormalizationError(
            "Provider field 'shares_out' must have a real numeric dtype; "
            f"received {provider_data.dtype}."
        )
    if provider_data.empty:
        return _empty_share_data()

    observation_dates = _normalize_provider_dates(provider_data.index)
    normalized = pd.DataFrame(
        {
            "date": pd.Series(observation_dates, dtype="datetime64[ns]"),
            "ticker": pd.Series([ticker] * len(provider_data), dtype="string"),
            "shares_outstanding": provider_data.reset_index(drop=True),
            "retrieved_at": pd.Series(
                pd.to_datetime([timestamp] * len(provider_data), utc=True)
            ),
        }
    )
    normalized = deduplicate_share_data(normalized)
    normalized = normalized.sort_values(
        ["ticker", "date"], kind="mergesort"
    ).reset_index(drop=True)
    normalized = normalized.loc[:, list(CANONICAL_SHARE_COLUMNS)]
    validate_share_data(normalized)
    return normalized


def _resolve_yfinance_query_window(
    start_date: date | None,
    end_date: date | None,
    exchange_timezone: str,
    query_reference_time: datetime | None,
) -> tuple[pd.Timestamp, pd.Timestamp]:
    if end_date is not None:
        end = yf_utils._parse_user_dt(end_date, exchange_timezone)
    else:
        if query_reference_time is None or query_reference_time.tzinfo is None:
            raise ValueError(
                "An omitted shares query end requires a timezone-aware batch "
                "reference time."
            )
        end = pd.Timestamp(query_reference_time).tz_convert(exchange_timezone)
    start = (
        yf_utils._parse_user_dt(start_date, exchange_timezone)
        if start_date is not None
        else end - pd.Timedelta(days=548)
    )
    if start >= end:
        raise ValueError("start must be earlier than end for the provider query.")
    return start.floor("D"), end.ceil("D")


def _request_raw_yfinance_shares(
    ticker: str,
    start_date: date | None,
    end_date: date | None,
    query_reference_time: datetime | None = None,
) -> tuple[object, str]:
    """Request the pinned yfinance 1.5.2 shares-outstanding payload.

    The public ``Ticker.get_shares_full`` method uses a non-expiring response
    cache and suppresses some failures as ``None``. This isolated adapter uses
    the same pinned fundamentals-timeseries path and yfinance-owned uncached
    transport so each source vintage has a fresh response while yfinance keeps
    responsibility for sessions, cookie/crumb handling, configured retries,
    HTTP errors, and rate limiting.
    """

    ticker_client = yf.Ticker(ticker)
    timezone_lookup = getattr(ticker_client, "_get_ticker_tz", None)
    data_client = getattr(ticker_client, "_data", None)
    if not callable(timezone_lookup) or data_client is None:
        raise SharesNormalizationError(
            "The pinned yfinance 1.5.2 shares adapter requires "
            "Ticker._get_ticker_tz and Ticker._data."
        )

    direct_get = getattr(data_client, "get", None)
    if not callable(direct_get):
        raise SharesNormalizationError(
            "The pinned yfinance 1.5.2 shares adapter requires Ticker._data.get."
        )

    exchange_timezone = timezone_lookup(timeout=10)
    if not isinstance(exchange_timezone, str) or not exchange_timezone:
        raise SharesNormalizationError(
            f"yfinance could not resolve an exchange timezone for {ticker}."
        )

    start, end = _resolve_yfinance_query_window(
        start_date,
        end_date,
        exchange_timezone,
        query_reference_time,
    )
    url_base = (
        f"{_BASE_URL_}/ws/fundamentals-timeseries/v1/finance/timeseries/"
        f"{ticker}?symbol={ticker}"
    )
    url = f"{url_base}&period1={int(start.timestamp())}&period2={int(end.timestamp())}"
    response = direct_get(url=url, timeout=30)
    if response is None:
        raise SharesNormalizationError(
            f"yfinance returned no shares response object for {ticker}."
        )
    response_json = getattr(response, "json", None)
    if not callable(response_json):
        raise SharesNormalizationError(
            f"yfinance returned a shares response without JSON for {ticker}."
        )
    return response_json(), exchange_timezone


def _raw_shares_to_provider_series(
    payload: object,
    ticker: str,
    exchange_timezone: str,
) -> pd.Series | None:
    if not isinstance(payload, dict):
        raise SharesNormalizationError(
            f"Malformed yfinance shares payload for {ticker}: expected an object."
        )

    finance = payload.get("finance")
    if isinstance(finance, dict) and finance.get("error") is not None:
        error = finance["error"]
        raise SharesNormalizationError(
            f"Yahoo shares request failed for {ticker}: {error}."
        )

    timeseries = payload.get("timeseries")
    if not isinstance(timeseries, dict):
        raise SharesNormalizationError(
            f"Malformed yfinance shares payload for {ticker}: missing timeseries."
        )
    results = timeseries.get("result")
    if not isinstance(results, list):
        raise SharesNormalizationError(
            f"Malformed yfinance shares payload for {ticker}: invalid result list."
        )
    if not results:
        raise SharesNormalizationError(
            f"Malformed yfinance shares payload for {ticker}: empty result list."
        )
    result = results[0]
    if not isinstance(result, dict):
        raise SharesNormalizationError(
            f"Malformed yfinance shares payload for {ticker}: invalid result."
        )
    metadata = result.get("meta")
    if not isinstance(metadata, dict):
        raise SharesNormalizationError(
            f"Malformed yfinance shares payload for {ticker}: missing result metadata."
        )
    provider_symbols = metadata.get("symbol")
    if (
        not isinstance(provider_symbols, list)
        or len(provider_symbols) != 1
        or not isinstance(provider_symbols[0], str)
        or not provider_symbols[0]
        or provider_symbols[0] != provider_symbols[0].strip()
    ):
        raise SharesNormalizationError(
            f"Malformed yfinance shares payload for {ticker}: result metadata "
            "must contain one valid symbol identifier."
        )
    provider_symbol = provider_symbols[0]
    expected_symbol = ticker.upper()
    if provider_symbol != expected_symbol:
        raise SharesNormalizationError(
            f"Yahoo shares result identifies symbol {provider_symbol!r}, not "
            f"requested symbol {expected_symbol!r}."
        )
    if "shares_out" not in result:
        return None

    timestamps = result.get("timestamp")
    shares_values = result["shares_out"]
    if not isinstance(timestamps, list) or not isinstance(shares_values, list):
        raise SharesNormalizationError(
            f"Malformed yfinance shares payload for {ticker}: timestamp and "
            "shares_out must be lists."
        )
    if len(timestamps) != len(shares_values):
        raise SharesNormalizationError(
            f"Malformed yfinance shares payload for {ticker}: timestamp and "
            "shares_out lengths differ."
        )

    for timestamp in timestamps:
        if (
            isinstance(timestamp, bool)
            or not isinstance(timestamp, Real)
            or not math.isfinite(float(timestamp))
        ):
            raise SharesNormalizationError(
                f"Malformed yfinance shares timestamp for {ticker}: {timestamp!r}."
            )
    for value in shares_values:
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, Real)
        ):
            raise SharesNormalizationError(
                f"Malformed yfinance shares_out value for {ticker}: {value!r}."
            )

    try:
        provider_index = pd.to_datetime(timestamps, unit="s").tz_localize(
            exchange_timezone
        )
    except (TypeError, ValueError) as error:
        raise SharesNormalizationError(
            f"Malformed yfinance shares dates for {ticker}: {error}."
        ) from error

    try:
        provider_values = build_lossless_real_numeric_series(
            shares_values, name="shares_out"
        )
    except NumericDtypeHarmonizationError as error:
        raise SharesNormalizationError(
            f"Yahoo shares_out values for {ticker} cannot be represented "
            "losslessly as one supported raw numeric Series."
        ) from error
    provider_values.index = provider_index
    return provider_values.sort_index(kind="mergesort")


def _download_yfinance_ticker(
    ticker: str,
    start_date: date | None,
    end_date: date | None,
    *,
    query_reference_time: datetime | None = None,
) -> pd.Series | None:
    with yfinance_exception_visibility():
        payload, exchange_timezone = _request_raw_yfinance_shares(
            ticker,
            start_date,
            end_date,
            query_reference_time,
        )
    return _raw_shares_to_provider_series(payload, ticker, exchange_timezone)


def _concat_share_frames_losslessly(
    frames: Sequence[pd.DataFrame],
) -> pd.DataFrame:
    """Concatenate validated ticker frames without numeric representation loss."""

    combined = frames[0].copy()
    for frame in frames[1:]:
        try:
            combined_values, frame_values = harmonize_real_numeric_series(
                combined["shares_outstanding"], frame["shares_outstanding"]
            )
        except NumericDtypeHarmonizationError as error:
            raise SharesNormalizationError(
                "Successful ticker shares representations cannot be combined "
                "losslessly: shares_outstanding dtypes "
                f"{combined['shares_outstanding'].dtype} and "
                f"{frame['shares_outstanding'].dtype}."
            ) from error

        harmonized_combined = combined.copy()
        harmonized_frame = frame.copy()
        harmonized_combined["shares_outstanding"] = combined_values
        harmonized_frame["shares_outstanding"] = frame_values
        combined = pd.concat([harmonized_combined, harmonized_frame], ignore_index=True)
    return combined


def download_shares_history(
    tickers: Sequence[str],
    start: str | date | None = None,
    end: str | date | None = None,
    *,
    downloader: ShareDownloadCallable | None = None,
    retrieved_at: datetime | pd.Timestamp | None = None,
) -> ShareDownloadResult:
    """Download historical shares outstanding without failing the whole batch.

    ``start`` and ``end`` are provider query bounds only. The installed
    yfinance implementation does not document their response inclusivity, so
    Day 3 does not filter or expand returned source observations. When ``end``
    is omitted, one batch reference instant is converted independently to each
    ticker's exchange timezone. When ``start`` is also omitted, yfinance's
    inspected policy queries the 548 days preceding that ticker-local end.

    Args:
        tickers: ETF tickers to request independently.
        start: Optional provider query start date.
        end: Optional provider query end date.
        downloader: Optional compatible callable for offline tests.
        retrieved_at: Optional timezone-aware deterministic timestamp override.
            The live default timestamps each ticker after its downloader returns.

    Returns:
        Canonical source observations and a status for every requested ticker.

    Raises:
        ValueError: If tickers, query bounds, or retrieval time are invalid.
    """

    requested_tickers = _normalize_tickers(tickers)
    start_date = _parse_optional_date(start, "start")
    end_date = _parse_optional_date(end, "end")
    if start_date is not None and end_date is not None and start_date >= end_date:
        raise ValueError("start must be earlier than end for the provider query.")

    timestamp_override = (
        _retrieval_timestamp(retrieved_at) if retrieved_at is not None else None
    )
    query_reference_time = _query_reference_time() if end_date is None else None
    statuses: list[ShareDownloadStatus] = []
    successful_frames: list[pd.DataFrame] = []

    for ticker in requested_tickers:
        try:
            if downloader is None:
                provider_data = _download_yfinance_ticker(
                    ticker,
                    start_date,
                    end_date,
                    query_reference_time=query_reference_time,
                )
            else:
                provider_data = downloader(ticker, start_date, end_date)
            response_timestamp = timestamp_override or _utc_now()
            normalized = normalize_yfinance_shares_output(
                provider_data, ticker, response_timestamp
            )
        except Exception as error:  # Provider libraries expose varied failures.
            error_message = f"{type(error).__name__}: {error}"
            LOGGER.warning("Shares download failed for %s: %s", ticker, error_message)
            statuses.append(
                ShareDownloadStatus(
                    ticker=ticker,
                    status="failed",
                    rows_received=0,
                    first_date=None,
                    last_date=None,
                    retrieved_at=None,
                    error=error_message,
                )
            )
            continue

        if normalized.empty:
            LOGGER.warning("Shares download returned no dated history for %s.", ticker)
            statuses.append(
                ShareDownloadStatus(
                    ticker=ticker,
                    status="empty",
                    rows_received=0,
                    first_date=None,
                    last_date=None,
                    retrieved_at=response_timestamp,
                )
            )
            continue

        successful_frames.append(normalized)
        statuses.append(
            ShareDownloadStatus(
                ticker=ticker,
                status="success",
                rows_received=len(normalized),
                first_date=normalized["date"].min().date(),
                last_date=normalized["date"].max().date(),
                retrieved_at=response_timestamp,
            )
        )

    if successful_frames:
        shares = _concat_share_frames_losslessly(successful_frames)
        shares = deduplicate_share_data(shares)
        shares = shares.sort_values(["ticker", "date"], kind="mergesort").reset_index(
            drop=True
        )
        validate_share_data(shares)
    else:
        shares = _empty_share_data()

    observed_timestamps = [
        status.retrieved_at for status in statuses if status.retrieved_at is not None
    ]
    return ShareDownloadResult(
        shares=shares,
        statuses=tuple(statuses),
        retrieved_at=max(observed_timestamps) if observed_timestamps else None,
    )


def _shares_history_lock_path(output_path: Path) -> Path:
    resolved_output_path = output_path.resolve(strict=False)
    return resolved_output_path.with_name(f".{resolved_output_path.name}.lock")


def _try_lock_file(lock_file: BinaryIO) -> None:
    if sys.platform == "win32":
        import msvcrt

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
def _shares_history_transaction_lock(output_path: Path) -> Iterator[None]:
    """Hold an adjacent cross-process lock for one canonical shares output."""

    lock_path = _shares_history_lock_path(output_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + _SHARES_HISTORY_LOCK_TIMEOUT_SECONDS

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
                        f"{_SHARES_HISTORY_LOCK_TIMEOUT_SECONDS:g} seconds waiting "
                        f"for the shares-history lock for {output_path}."
                    ) from error
                time.sleep(min(_SHARES_HISTORY_LOCK_POLL_SECONDS, remaining_seconds))

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


def _snapshot_existing_share_file(
    output_path: Path,
    snapshot_dir: Path,
    snapshot_time: datetime,
) -> Path:
    """Atomically publish an exact, non-overwriting canonical shares snapshot."""

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


def _merge_share_vintages(
    existing: pd.DataFrame,
    incoming: pd.DataFrame,
) -> tuple[pd.DataFrame, int, tuple[str, ...]]:
    """Merge validated shares source vintages without losing observations."""

    key_columns = ["ticker", "date"]
    existing_by_key = existing.set_index(key_columns, drop=False)
    incoming_by_key = incoming.set_index(key_columns, drop=False)
    overlap_keys = existing_by_key.index.intersection(incoming_by_key.index, sort=False)

    if overlap_keys.empty:
        return pd.concat([existing, incoming], ignore_index=True), 0, ()

    existing_overlap = existing_by_key.loc[overlap_keys]
    incoming_overlap = incoming_by_key.loc[overlap_keys]
    existing_missing = existing_overlap["shares_outstanding"].isna()
    incoming_missing = incoming_overlap["shares_outstanding"].isna()
    loses_existing_values = ~existing_missing & incoming_missing
    if loses_existing_values.any():
        affected_keys = list(overlap_keys[loses_existing_values])[:5]
        raise ShareDataValidationError(
            "Incoming source vintage loses previously available shares values "
            f"for ticker/date pairs: {affected_keys}. Manual review is required."
        )

    both_missing = existing_missing & incoming_missing
    both_present = ~existing_missing & ~incoming_missing
    equal_when_present = pd.Series(False, index=overlap_keys, dtype=bool)
    if both_present.any():
        comparisons = existing_overlap.loc[both_present, "shares_outstanding"].eq(
            incoming_overlap.loc[both_present, "shares_outstanding"]
        )
        equal_when_present.loc[both_present] = comparisons.fillna(False).astype(bool)
    identical_values = both_missing | (both_present & equal_when_present)
    revised_keys = overlap_keys[~identical_values]
    existing_overlap_times = existing_by_key.loc[overlap_keys, "retrieved_at"]
    incoming_overlap_times = incoming_by_key.loc[overlap_keys, "retrieved_at"]
    newer_identical_keys = overlap_keys[
        identical_values & incoming_overlap_times.gt(existing_overlap_times)
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
                retained_existing.loc[:, list(CANONICAL_SHARE_COLUMNS)],
                new_rows.loc[:, list(CANONICAL_SHARE_COLUMNS)],
            ],
            ignore_index=True,
        )
        return combined, 0, ()

    existing_revision_times = existing_by_key.loc[revised_keys, "retrieved_at"]
    incoming_revision_times = incoming_by_key.loc[revised_keys, "retrieved_at"]
    stale_revisions = incoming_revision_times.lt(existing_revision_times)
    if stale_revisions.any():
        affected_keys = list(revised_keys[stale_revisions])[:5]
        raise ShareDataValidationError(
            "Incoming shares source vintage is older than the canonical source "
            f"vintage for ticker/date pairs: {affected_keys}."
        )

    same_vintage_conflicts = incoming_revision_times.eq(existing_revision_times)
    if same_vintage_conflicts.any():
        affected_keys = list(revised_keys[same_vintage_conflicts])[:5]
        raise ShareDataValidationError(
            "Different shares values claim the same retrieved_at source vintage "
            f"for ticker/date pairs: {affected_keys}."
        )

    revised_rows = incoming_by_key.loc[revised_keys, list(CANONICAL_SHARE_COLUMNS)]
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
            retained_existing.loc[:, list(CANONICAL_SHARE_COLUMNS)],
            revised_rows,
            new_rows.loc[:, list(CANONICAL_SHARE_COLUMNS)],
        ],
        ignore_index=True,
    )
    revised_tickers = tuple(sorted(revised_rows["ticker"].astype(str).unique()))
    return combined, len(revised_rows), revised_tickers


def _harmonize_share_numeric_dtypes(
    existing: pd.DataFrame,
    incoming: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    try:
        existing_values, incoming_values = harmonize_real_numeric_series(
            existing["shares_outstanding"], incoming["shares_outstanding"]
        )
    except NumericDtypeHarmonizationError as error:
        raise ShareDataValidationError(
            "Cannot losslessly combine canonical shares_outstanding dtypes "
            f"{existing['shares_outstanding'].dtype} and "
            f"{incoming['shares_outstanding'].dtype}: {error}."
        ) from error

    harmonized_existing = existing.copy()
    harmonized_incoming = incoming.copy()
    harmonized_existing["shares_outstanding"] = existing_values
    harmonized_incoming["shares_outstanding"] = incoming_values
    return harmonized_existing, harmonized_incoming


def persist_shares_history(
    incoming_data: pd.DataFrame,
    output_path: Path,
    *,
    snapshot_dir: Path | None = None,
    snapshot_time: datetime | None = None,
) -> SharePersistenceResult:
    """Merge the latest validated shares vintage in a serialized transaction.

    Identical overlaps retain the existing source value while advancing its
    provenance watermark to a later incoming retrieval time. A changed newer
    overlap replaces the entire row only after the exact superseded canonical
    Parquet is safely snapshotted. Incoming value loss is rejected. An adjacent
    cross-process lock covers existing-file read, validation, merge, snapshot,
    and atomic replace.

    Args:
        incoming_data: Newly retrieved canonical shares observations.
        output_path: Destination Parquet path.
        snapshot_dir: Superseded-vintage directory. Defaults to
            ``data/snapshots/shares`` only when a revision needs a snapshot.
        snapshot_time: Optional timezone-aware snapshot timestamp for tests.

    Returns:
        The written canonical dataset and source-revision metadata.

    Raises:
        ShareDataValidationError: If incoming, existing, or combined data are
            invalid, stale, inconsistent, or lose a previous shares value.
        ValueError: If no dated observations are available to persist.
        TimeoutError: If the per-output lock cannot be acquired in 30 seconds.
        OSError: If reading, snapshotting, or canonical replacement fails.
    """

    with _shares_history_transaction_lock(output_path):
        existing: pd.DataFrame | None = None
        if output_path.exists():
            existing = pd.read_parquet(output_path)
            existing = deduplicate_share_data(existing)
            validate_share_data(existing)

        incoming = deduplicate_share_data(incoming_data)
        validate_share_data(incoming)
        if incoming.empty:
            raise ValueError("No shares observations are available to persist.")

        revised_row_count = 0
        revised_tickers: tuple[str, ...] = ()
        snapshot_path: Path | None = None
        if existing is not None:
            existing, incoming = _harmonize_share_numeric_dtypes(existing, incoming)
            combined, revised_row_count, revised_tickers = _merge_share_vintages(
                existing, incoming
            )
        else:
            combined = incoming.copy()

        combined = combined.sort_values(
            ["ticker", "date"], kind="mergesort"
        ).reset_index(drop=True)
        combined = combined.loc[:, list(CANONICAL_SHARE_COLUMNS)]
        validate_share_data(combined)

        if revised_row_count:
            resolved_snapshot_dir = snapshot_dir
            if resolved_snapshot_dir is None:
                resolved_snapshot_dir = get_snapshot_data_dir() / "shares"
            snapshot_path = _snapshot_existing_share_file(
                output_path,
                resolved_snapshot_dir,
                _snapshot_timestamp(snapshot_time),
            )

        _write_parquet_atomically(combined, output_path)
        return SharePersistenceResult(
            shares=combined,
            revised_row_count=revised_row_count,
            revised_tickers=revised_tickers,
            snapshot_path=snapshot_path,
        )
