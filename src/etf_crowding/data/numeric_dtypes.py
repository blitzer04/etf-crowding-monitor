"""Losslessly harmonize validated canonical numeric storage representations."""

from __future__ import annotations

from typing import Any, Literal

import numpy as np
import pandas as pd
from pandas.api.extensions import ExtensionDtype
from pandas.api.types import (
    is_any_real_numeric_dtype,
    is_float_dtype,
    is_integer_dtype,
    is_unsigned_integer_dtype,
)

type NumericCandidateDtype = Literal["Int64", "UInt64", "Float64"]
type SeriesDtype = ExtensionDtype | np.dtype[Any]


class NumericDtypeHarmonizationError(ValueError):
    """Indicate that numeric representations have no supported lossless union."""


def is_supported_canonical_numeric_dtype(dtype: SeriesDtype) -> bool:
    """Return whether a dtype is a supported canonical numeric representation.

    Canonical financial columns support dense pandas, NumPy, and
    PyArrow-backed integer or floating dtypes. Sparse and PyArrow decimal
    dtypes are deliberately excluded even when pandas classifies them as real
    numeric, because the canonical persistence and validation layers do not
    support those extension representations.

    Args:
        dtype: Candidate pandas Series dtype.

    Returns:
        True for supported dense integer or floating dtypes; otherwise False.
    """

    return (
        not isinstance(dtype, pd.SparseDtype)
        and is_any_real_numeric_dtype(dtype)
        and (is_integer_dtype(dtype) or is_float_dtype(dtype))
    )


def _round_trip_preserves_values(
    original: pd.Series,
    converted: pd.Series,
) -> bool:
    try:
        restored = converted.astype(original.dtype)
    except (OverflowError, TypeError, ValueError):
        return False
    return original.reset_index(drop=True).equals(restored.reset_index(drop=True))


def _concat_preserves_values(left: pd.Series, right: pd.Series) -> bool:
    try:
        combined = pd.concat([left, right], ignore_index=True)
    except (OverflowError, TypeError, ValueError):
        return False
    if not is_supported_canonical_numeric_dtype(combined.dtype):
        return False

    left_combined = combined.iloc[: len(left)]
    right_combined = combined.iloc[len(left) :]
    return _round_trip_preserves_values(
        left, left_combined
    ) and _round_trip_preserves_values(right, right_combined)


def _candidate_dtypes(
    left_dtype: SeriesDtype,
    right_dtype: SeriesDtype,
) -> tuple[NumericCandidateDtype, ...]:
    integer_dtypes = (is_integer_dtype(left_dtype), is_integer_dtype(right_dtype))
    float_dtypes = (is_float_dtype(left_dtype), is_float_dtype(right_dtype))

    if all(integer_dtypes):
        unsigned_dtypes = (
            is_unsigned_integer_dtype(left_dtype),
            is_unsigned_integer_dtype(right_dtype),
        )
        if all(unsigned_dtypes):
            return ("UInt64",)
        if any(unsigned_dtypes):
            return ("Int64", "UInt64")
        return ("Int64",)
    if all(float_dtypes):
        return ("Float64",)
    if any(integer_dtypes) and any(float_dtypes):
        return ("Float64", "Int64", "UInt64")
    return ()


def harmonize_real_numeric_series(
    left: pd.Series,
    right: pd.Series,
) -> tuple[pd.Series, pd.Series]:
    """Return representations that pandas can concatenate without value loss.

    The inputs must already use supported dense canonical integer or floating
    dtypes. Existing compatible representations are retained. Otherwise, the
    function tries pandas nullable 64-bit numeric representations and accepts a
    cast only when casting back to each original dtype reproduces every value
    and missing state exactly.

    Args:
        left: First validated real-numeric Series.
        right: Second validated real-numeric Series.

    Returns:
        The original or losslessly cast Series in one common numeric dtype.

    Raises:
        NumericDtypeHarmonizationError: If either input is not a supported
            dense canonical integer or floating dtype, or no supported common
            representation preserves both inputs exactly.
    """

    if not is_supported_canonical_numeric_dtype(
        left.dtype
    ) or not is_supported_canonical_numeric_dtype(right.dtype):
        raise NumericDtypeHarmonizationError(
            "expected supported dense canonical integer or floating dtypes, "
            f"received {left.dtype} and {right.dtype}"
        )
    if _concat_preserves_values(left, right):
        return left, right

    for candidate_dtype in _candidate_dtypes(left.dtype, right.dtype):
        try:
            converted_left = left.astype(candidate_dtype)
            converted_right = right.astype(candidate_dtype)
        except (OverflowError, TypeError, ValueError):
            continue
        if _round_trip_preserves_values(
            left, converted_left
        ) and _round_trip_preserves_values(right, converted_right):
            return converted_left, converted_right

    raise NumericDtypeHarmonizationError(
        f"dtypes {left.dtype} and {right.dtype} have no supported common "
        "representation that preserves all values exactly"
    )
