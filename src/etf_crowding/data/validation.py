"""Validation rules for canonical daily ETF price data."""

from __future__ import annotations

import numpy as np
import pandas as pd
from pandas.api.types import is_any_real_numeric_dtype, is_datetime64_dtype

CANONICAL_PRICE_COLUMNS = (
    "date",
    "ticker",
    "open",
    "high",
    "low",
    "close",
    "adjusted_close",
    "volume",
    "retrieved_at",
)
PRICE_KEY_COLUMNS = ("ticker", "date")
PRICE_VALUE_COLUMNS = (
    "open",
    "high",
    "low",
    "close",
    "adjusted_close",
    "volume",
)
POSITIVE_PRICE_COLUMNS = ("open", "high", "low", "close", "adjusted_close")


class PriceDataValidationError(ValueError):
    """Indicate that price data violate the canonical data contract."""


def _validate_columns(data: pd.DataFrame) -> None:
    duplicate_columns = data.columns[data.columns.duplicated()].unique().tolist()
    if duplicate_columns:
        raise PriceDataValidationError(
            f"Price data contain duplicate column labels: {duplicate_columns}."
        )

    missing_columns = [
        column for column in CANONICAL_PRICE_COLUMNS if column not in data.columns
    ]
    if missing_columns:
        raise PriceDataValidationError(
            f"Price data are missing required columns: {missing_columns}."
        )

    unexpected_columns = [
        column for column in data.columns if column not in CANONICAL_PRICE_COLUMNS
    ]
    if unexpected_columns:
        raise PriceDataValidationError(
            f"Price data contain unexpected columns: {unexpected_columns}."
        )


def _key_examples(data: pd.DataFrame, invalid: pd.Series, limit: int = 5) -> str:
    examples = data.loc[invalid, list(PRICE_KEY_COLUMNS)].head(limit)
    return examples.to_dict(orient="records").__repr__()


def _numeric_columns(data: pd.DataFrame) -> dict[str, pd.Series]:
    numeric_columns: dict[str, pd.Series] = {}

    for column in PRICE_VALUE_COLUMNS:
        values = data[column]
        if not is_any_real_numeric_dtype(values.dtype):
            raise PriceDataValidationError(
                f"Column '{column}' must have a real numeric dtype; "
                f"received {values.dtype}."
            )

        finite_values = pd.Series(
            np.isfinite(values), index=data.index, dtype="boolean"
        ).fillna(False)
        non_finite = values.notna() & ~finite_values.astype(bool)
        if non_finite.any():
            raise PriceDataValidationError(
                f"Column '{column}' contains non-finite values at "
                f"{_key_examples(data, non_finite)}."
            )

        numeric_columns[column] = values

    return numeric_columns


def _validate_ohlc(data: pd.DataFrame, numeric_columns: dict[str, pd.Series]) -> None:
    open_prices = numeric_columns["open"]
    high_prices = numeric_columns["high"]
    low_prices = numeric_columns["low"]
    close_prices = numeric_columns["close"]

    consistency_checks = {
        "high >= open": high_prices.ge(open_prices)
        | high_prices.isna()
        | open_prices.isna(),
        "high >= close": high_prices.ge(close_prices)
        | high_prices.isna()
        | close_prices.isna(),
        "high >= low": high_prices.ge(low_prices)
        | high_prices.isna()
        | low_prices.isna(),
        "low <= open": low_prices.le(open_prices)
        | low_prices.isna()
        | open_prices.isna(),
        "low <= close": low_prices.le(close_prices)
        | low_prices.isna()
        | close_prices.isna(),
    }

    for rule, valid in consistency_checks.items():
        invalid = ~valid
        if invalid.any():
            raise PriceDataValidationError(
                f"OHLC consistency rule '{rule}' failed at "
                f"{_key_examples(data, invalid)}."
            )


def validate_price_data(data: pd.DataFrame) -> None:
    """Validate a canonical daily ETF price dataset.

    Missing market values are allowed because provider observations may be
    legitimately incomplete. Market columns must already use real numeric pandas
    dtypes; the validator never coerces strings or complex values. Column labels
    must be unique. Present price values must be finite and positive, volume must
    be finite and non-negative, and available OHLC fields must be internally
    consistent. Date columns must already use the canonical datetime dtypes. The
    function never fills or repairs observations.

    Args:
        data: Candidate data in the canonical daily price schema.

    Raises:
        PriceDataValidationError: If schema, keys, values, timestamps, or OHLC
            relationships violate the canonical data contract.
    """

    _validate_columns(data)

    if isinstance(data["date"].dtype, pd.DatetimeTZDtype) or not is_datetime64_dtype(
        data["date"].dtype
    ):
        raise PriceDataValidationError(
            "Column 'date' must have a timezone-naive datetime64 dtype."
        )

    retrieval_dtype = data["retrieved_at"].dtype
    if not isinstance(retrieval_dtype, pd.DatetimeTZDtype):
        raise PriceDataValidationError(
            "Column 'retrieved_at' must have a timezone-aware datetime64 dtype."
        )
    if str(retrieval_dtype.tz) != "UTC":
        raise PriceDataValidationError("Retrieval timestamps must use UTC.")

    numeric_columns = _numeric_columns(data)
    if data.empty:
        return

    invalid_ticker = data["ticker"].map(
        lambda value: not isinstance(value, str) or not value.strip()
    )
    if invalid_ticker.any():
        raise PriceDataValidationError("Ticker values must be non-empty strings.")

    if data["date"].isna().any():
        raise PriceDataValidationError("Price dates must not be missing or invalid.")
    if data["date"].ne(data["date"].dt.normalize()).any():
        raise PriceDataValidationError(
            "Price dates must be normalized to midnight without intraday times."
        )

    duplicate_keys = data.duplicated(list(PRICE_KEY_COLUMNS), keep=False)
    if duplicate_keys.any():
        raise PriceDataValidationError(
            "Price data contain duplicate ticker/date pairs at "
            f"{_key_examples(data, duplicate_keys)}."
        )

    if data["retrieved_at"].isna().any():
        raise PriceDataValidationError(
            "Retrieval timestamps must not be missing or invalid."
        )

    no_market_values = data[list(PRICE_VALUE_COLUMNS)].isna().all(axis=1)
    if no_market_values.any():
        raise PriceDataValidationError(
            "Every ticker/date row must contain at least one market value; "
            f"invalid rows: {_key_examples(data, no_market_values)}."
        )

    for column in POSITIVE_PRICE_COLUMNS:
        invalid_price = numeric_columns[column].notna() & numeric_columns[column].le(0)
        if invalid_price.any():
            raise PriceDataValidationError(
                f"Column '{column}' must be positive where present; invalid rows: "
                f"{_key_examples(data, invalid_price)}."
            )

    invalid_volume = numeric_columns["volume"].notna() & numeric_columns["volume"].lt(0)
    if invalid_volume.any():
        raise PriceDataValidationError(
            "Column 'volume' must be non-negative where present; invalid rows: "
            f"{_key_examples(data, invalid_volume)}."
        )

    _validate_ohlc(data, numeric_columns)


def deduplicate_price_data(data: pd.DataFrame) -> pd.DataFrame:
    """Remove identical ticker/date observations and reject conflicts.

    Market-value equality is assessed across raw OHLC, adjusted close, and
    volume. Retrieval time is provenance for the download rather than a market
    value, so the first row is retained when market values agree.

    Args:
        data: Price observations containing the canonical columns.

    Returns:
        A copy with at most one observation for each ticker/date pair.

    Raises:
        PriceDataValidationError: If duplicate keys have different market
            values or the canonical schema is malformed.
    """

    _validate_columns(data)
    duplicate_keys = data.duplicated(list(PRICE_KEY_COLUMNS), keep=False)
    if not duplicate_keys.any():
        return data.copy()

    duplicate_rows = data.loc[duplicate_keys]
    conflicting_keys: list[object] = []
    grouped_rows = duplicate_rows.groupby(
        list(PRICE_KEY_COLUMNS), dropna=False, sort=False
    )
    for key, group in grouped_rows:
        has_conflict = any(
            group[column].nunique(dropna=False) > 1 for column in PRICE_VALUE_COLUMNS
        )
        if has_conflict:
            conflicting_keys.append(key)

    if conflicting_keys:
        raise PriceDataValidationError(
            "Conflicting market values found for duplicate ticker/date pairs: "
            f"{conflicting_keys[:5]}."
        )

    return data.drop_duplicates(list(PRICE_KEY_COLUMNS), keep="first").copy()
