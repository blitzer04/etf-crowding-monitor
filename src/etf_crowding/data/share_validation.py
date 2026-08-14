"""Validation rules for canonical historical ETF shares outstanding."""

from __future__ import annotations

import numpy as np
import pandas as pd
from pandas.api.types import is_any_real_numeric_dtype, is_datetime64_dtype

from etf_crowding.data.numeric_dtypes import (
    is_supported_canonical_numeric_dtype,
)

CANONICAL_SHARE_COLUMNS = (
    "date",
    "ticker",
    "shares_outstanding",
    "retrieved_at",
)
SHARE_KEY_COLUMNS = ("ticker", "date")


class ShareDataValidationError(ValueError):
    """Indicate that shares data violate the canonical data contract."""


def _validate_columns(data: pd.DataFrame) -> None:
    duplicate_columns = data.columns[data.columns.duplicated()].unique().tolist()
    if duplicate_columns:
        raise ShareDataValidationError(
            f"Shares data contain duplicate column labels: {duplicate_columns}."
        )

    missing_columns = [
        column for column in CANONICAL_SHARE_COLUMNS if column not in data.columns
    ]
    if missing_columns:
        raise ShareDataValidationError(
            f"Shares data are missing required columns: {missing_columns}."
        )

    unexpected_columns = [
        column for column in data.columns if column not in CANONICAL_SHARE_COLUMNS
    ]
    if unexpected_columns:
        raise ShareDataValidationError(
            f"Shares data contain unexpected columns: {unexpected_columns}."
        )


def _key_examples(data: pd.DataFrame, invalid: pd.Series, limit: int = 5) -> str:
    examples = data.loc[invalid, list(SHARE_KEY_COLUMNS)].head(limit)
    return examples.to_dict(orient="records").__repr__()


def _prevalidate_share_structure_and_dtype(data: pd.DataFrame) -> pd.Series:
    _validate_columns(data)
    shares = data["shares_outstanding"]
    if not is_supported_canonical_numeric_dtype(shares.dtype):
        if is_any_real_numeric_dtype(shares.dtype):
            raise ShareDataValidationError(
                "Column 'shares_outstanding' uses unsupported real numeric dtype "
                f"{shares.dtype}; canonical shares support dense integer and "
                "floating pandas, NumPy, and PyArrow-backed dtypes only."
            )
        raise ShareDataValidationError(
            "Column 'shares_outstanding' must have a real numeric dtype using a "
            "supported dense integer or floating representation; "
            f"received {shares.dtype}."
        )
    return shares


def _validate_retrieval_timestamps(data: pd.DataFrame) -> None:
    retrieval_dtype = data["retrieved_at"].dtype
    if not isinstance(retrieval_dtype, pd.DatetimeTZDtype):
        raise ShareDataValidationError(
            "Column 'retrieved_at' must have a timezone-aware datetime64 dtype."
        )
    if str(retrieval_dtype.tz) != "UTC":
        raise ShareDataValidationError("Retrieval timestamps must use UTC.")
    if data["retrieved_at"].isna().any():
        raise ShareDataValidationError(
            "Retrieval timestamps must not be missing or invalid."
        )


def _validate_canonical_keys(data: pd.DataFrame) -> None:
    date_dtype = data["date"].dtype
    if isinstance(date_dtype, pd.DatetimeTZDtype) or not is_datetime64_dtype(
        date_dtype
    ):
        raise ShareDataValidationError(
            "Column 'date' must have a timezone-naive datetime64 dtype."
        )

    invalid_ticker = pd.Series(
        [
            not isinstance(value, str) or not value.strip()
            for value in data["ticker"].array
        ],
        index=data.index,
        dtype=bool,
    )
    if invalid_ticker.any():
        examples = data.loc[invalid_ticker, "ticker"].head(5).tolist()
        raise ShareDataValidationError(
            f"Ticker values must be non-empty strings; invalid values: {examples}."
        )

    if data["date"].isna().any():
        raise ShareDataValidationError("Share dates must not be missing or invalid.")
    if data["date"].ne(data["date"].dt.normalize()).any():
        raise ShareDataValidationError(
            "Share dates must be normalized to midnight without intraday times."
        )


def validate_share_data(data: pd.DataFrame) -> None:
    """Validate canonical historical ETF shares-outstanding observations.

    Dated missing share values are valid source observations and remain missing.
    Present values must use a supported dense integer or floating numeric dtype
    and be finite and positive. The validator never coerces, fills, or repairs
    observations. Sparse and PyArrow decimal dtypes are unsupported.

    Args:
        data: Candidate data in the canonical shares-outstanding schema.

    Raises:
        ShareDataValidationError: If schema, keys, values, dates, or provenance
            timestamps violate the canonical data contract.
    """

    shares = _prevalidate_share_structure_and_dtype(data)
    _validate_canonical_keys(data)
    _validate_retrieval_timestamps(data)

    finite_values = pd.Series(
        np.isfinite(shares), index=data.index, dtype="boolean"
    ).fillna(False)
    non_finite = shares.notna() & ~finite_values.astype(bool)
    if non_finite.any():
        raise ShareDataValidationError(
            "Column 'shares_outstanding' contains non-finite values at "
            f"{_key_examples(data, non_finite)}."
        )

    if data.empty:
        return

    duplicate_keys = data.duplicated(list(SHARE_KEY_COLUMNS), keep=False)
    if duplicate_keys.any():
        raise ShareDataValidationError(
            "Shares data contain duplicate ticker/date pairs at "
            f"{_key_examples(data, duplicate_keys)}."
        )

    invalid_shares = shares.notna() & shares.le(0)
    if invalid_shares.any():
        raise ShareDataValidationError(
            "Column 'shares_outstanding' must be positive where present; "
            f"invalid rows: {_key_examples(data, invalid_shares)}."
        )


def deduplicate_share_data(data: pd.DataFrame) -> pd.DataFrame:
    """Remove identical ticker/date observations and reject conflicts.

    Retrieval time is provenance rather than a source value. When duplicate
    keys have identical shares values, including two missing values, the row
    with the latest retrieval timestamp is retained as the provenance watermark.

    Args:
        data: Shares observations containing the canonical columns.

    Returns:
        A copy with at most one observation for each ticker/date pair.

    Raises:
        ShareDataValidationError: If duplicate keys have conflicting shares
            values or the canonical schema is malformed.
    """

    _prevalidate_share_structure_and_dtype(data)
    _validate_canonical_keys(data)
    _validate_retrieval_timestamps(data)
    duplicate_keys = data.duplicated(list(SHARE_KEY_COLUMNS), keep=False)
    if not duplicate_keys.any():
        return data.copy()

    duplicate_rows = data.loc[duplicate_keys]
    conflicting_keys: list[object] = []
    grouped_rows = duplicate_rows.groupby(
        list(SHARE_KEY_COLUMNS), dropna=False, sort=False
    )
    for key, group in grouped_rows:
        if group["shares_outstanding"].nunique(dropna=False) > 1:
            conflicting_keys.append(key)

    if conflicting_keys:
        raise ShareDataValidationError(
            "Conflicting shares values found for duplicate ticker/date pairs: "
            f"{conflicting_keys[:5]}."
        )

    input_order_column = "_share_input_order"
    ordered = data.assign(**{input_order_column: range(len(data))}).sort_values(
        ["retrieved_at", input_order_column], kind="mergesort"
    )
    deduplicated = ordered.drop_duplicates(
        list(SHARE_KEY_COLUMNS), keep="last"
    ).sort_values(input_order_column, kind="mergesort")
    return deduplicated.drop(columns=input_order_column).copy()
