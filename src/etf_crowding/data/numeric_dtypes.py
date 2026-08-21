"""Losslessly harmonize validated canonical numeric storage representations."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal, cast

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
type ExactRealValue = (
    tuple[int, int] | Literal["negative-infinity", "positive-infinity"]
)


class NumericDtypeHarmonizationError(ValueError):
    """Indicate that numeric representations have no supported lossless union."""


def _integer_storage_dtype(values: Sequence[int]) -> NumericCandidateDtype:
    minimum = min(values)
    maximum = max(values)
    if -(2**63) <= minimum and maximum <= 2**63 - 1:
        return "Int64"
    if 0 <= minimum and maximum <= 2**64 - 1:
        return "UInt64"
    raise NumericDtypeHarmonizationError(
        "integer values fall outside supported signed and unsigned 64-bit storage"
    )


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


def _is_missing_scalar(value: object) -> bool:
    missing = pd.isna(cast(Any, value))
    return isinstance(missing, (bool, np.bool_)) and bool(missing)


def _exact_real_value(value: object) -> ExactRealValue:
    """Return a non-wrapping representation of one validated numeric scalar."""

    if isinstance(value, (bool, np.bool_)):
        raise TypeError("boolean values are not supported real numeric scalars")
    if isinstance(value, (int, np.integer)):
        return (int(value), 1)
    if isinstance(value, (float, np.floating)):
        try:
            numerator, denominator = value.as_integer_ratio()
        except (OverflowError, ValueError):
            return "positive-infinity" if value > 0 else "negative-infinity"
        return (int(numerator), int(denominator))
    raise TypeError(f"unsupported real numeric scalar {type(value).__name__}")


def _values_are_preserved_exactly(
    original: Sequence[object] | pd.Series,
    converted: pd.Series,
) -> bool:
    if len(original) != len(converted):
        return False

    for original_value, converted_value in zip(original, converted, strict=True):
        original_missing = _is_missing_scalar(original_value)
        converted_missing = _is_missing_scalar(converted_value)
        if original_missing or converted_missing:
            if original_missing != converted_missing:
                return False
            continue
        try:
            if _exact_real_value(original_value) != _exact_real_value(converted_value):
                return False
        except TypeError:
            return False
    return True


def _materialize_python_integer_series(values: Sequence[int]) -> pd.Series:
    storage_dtype = _integer_storage_dtype(values)
    buffer_dtype = np.dtype("int64" if storage_dtype == "Int64" else "uint64")
    try:
        integer_buffer = np.fromiter(
            values,
            dtype=buffer_dtype,
            count=len(values),
        )
    except (OverflowError, TypeError, ValueError) as error:
        raise NumericDtypeHarmonizationError(
            f"integer values cannot be materialized as {storage_dtype}"
        ) from error

    # Compare with the untouched Python integers before pandas sees the buffer.
    if [int(value) for value in integer_buffer] != list(values):
        raise NumericDtypeHarmonizationError(
            f"integer materialization as {storage_dtype} changed a source value"
        )
    result = pd.Series(pd.array(integer_buffer, dtype=storage_dtype))
    if not _values_are_preserved_exactly(values, result):
        raise NumericDtypeHarmonizationError(
            f"pandas materialization as {storage_dtype} changed a source value"
        )
    return result


def _materialize_python_float_series(values: Sequence[float]) -> pd.Series:
    floating_buffer = np.fromiter(values, dtype=np.dtype("float64"), count=len(values))
    result = pd.Series(pd.array(floating_buffer, dtype="Float64"))
    if not _values_are_preserved_exactly(values, result):
        raise NumericDtypeHarmonizationError(
            "pandas materialization as Float64 changed a source value"
        )
    return result


def _conversion_preserves_values(
    original: pd.Series,
    converted: pd.Series,
) -> bool:
    return _values_are_preserved_exactly(original, converted)


def cast_real_numeric_series_losslessly(
    values: pd.Series,
    target_dtype: NumericCandidateDtype | Literal["float64"],
) -> pd.Series:
    """Cast a supported numeric Series only when every value remains exact.

    Args:
        values: Source Series using a supported dense numeric dtype.
        target_dtype: Supported destination representation.

    Returns:
        Values cast to the requested dtype with value and missingness preserved.

    Raises:
        NumericDtypeHarmonizationError: If the source dtype is unsupported or
            conversion changes any value or missing state.
    """

    if not is_supported_canonical_numeric_dtype(values.dtype):
        raise NumericDtypeHarmonizationError(
            f"expected a supported dense canonical numeric dtype, received {values.dtype}"
        )
    try:
        converted = values.astype(target_dtype)
    except (OverflowError, TypeError, ValueError) as error:
        raise NumericDtypeHarmonizationError(
            f"dtype {values.dtype} cannot be converted to {target_dtype}"
        ) from error
    if not _conversion_preserves_values(values, converted):
        raise NumericDtypeHarmonizationError(
            f"conversion from {values.dtype} to {target_dtype} does not preserve "
            "every value and missing state exactly"
        )
    return converted


def build_lossless_real_numeric_series(
    values: Sequence[object],
    *,
    name: str | None = None,
) -> pd.Series:
    """Build a dense numeric Series without lossy constructor inference.

    Python integers and floats are first materialized separately so pandas
    cannot promote a large integer through binary float before the lossless
    representation policy sees it. Nulls remain nulls. Mixed integer/float
    inputs use the same exact harmonization policy as canonical persistence.

    Args:
        values: Python ``int``, ``float``, or ``None`` source values.
        name: Optional Series name.

    Returns:
        A supported nullable integer or floating Series in original order.

    Raises:
        NumericDtypeHarmonizationError: If a value is unsupported, outside
            64-bit numeric storage, or cannot share a lossless representation.
    """

    integer_positions: list[int] = []
    integer_values: list[int] = []
    floating_positions: list[int] = []
    floating_values: list[float] = []
    for position, value in enumerate(values):
        if value is None:
            continue
        if isinstance(value, bool):
            raise NumericDtypeHarmonizationError(
                f"boolean value at position {position} is not a real financial value"
            )
        if isinstance(value, int):
            integer_positions.append(position)
            integer_values.append(value)
            continue
        if isinstance(value, float):
            floating_positions.append(position)
            floating_values.append(value)
            continue
        raise NumericDtypeHarmonizationError(
            f"unsupported value type {type(value).__name__} at position {position}"
        )

    integer_series: pd.Series | None = None
    if integer_values:
        integer_series = _materialize_python_integer_series(integer_values)
    floating_series: pd.Series | None = None
    if floating_values:
        floating_series = _materialize_python_float_series(floating_values)

    if integer_series is not None and floating_series is not None:
        integer_series, floating_series = harmonize_real_numeric_series(
            integer_series, floating_series
        )

    positioned_series: list[pd.Series] = []
    if integer_series is not None:
        positioned_integers = integer_series.copy()
        positioned_integers.index = pd.Index(integer_positions)
        positioned_series.append(positioned_integers)
    if floating_series is not None:
        positioned_floats = floating_series.copy()
        positioned_floats.index = pd.Index(floating_positions)
        positioned_series.append(positioned_floats)

    if positioned_series:
        ordered = cast(
            pd.Series,
            pd.concat(positioned_series).reindex(pd.RangeIndex(len(values))),
        )
    else:
        ordered = pd.Series(index=pd.RangeIndex(len(values)), dtype="Float64")
    ordered.name = name
    if not _values_are_preserved_exactly(values, ordered):
        raise NumericDtypeHarmonizationError(
            "ordered numeric materialization changed a source value or missing state"
        )
    return ordered


def _concat_preserves_values(left: pd.Series, right: pd.Series) -> bool:
    try:
        combined = pd.concat([left, right], ignore_index=True)
    except (OverflowError, TypeError, ValueError):
        return False
    if not is_supported_canonical_numeric_dtype(combined.dtype):
        return False

    left_combined = combined.iloc[: len(left)]
    right_combined = combined.iloc[len(left) :]
    return _conversion_preserves_values(
        left, left_combined
    ) and _conversion_preserves_values(right, right_combined)


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
    cast only when exact mathematical scalar comparison preserves every value
    and missing state. This comparison never casts signed and unsigned values
    back across domains, so modular wraparound cannot establish equality.

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
        if _conversion_preserves_values(
            left, converted_left
        ) and _conversion_preserves_values(right, converted_right):
            return converted_left, converted_right

    raise NumericDtypeHarmonizationError(
        f"dtypes {left.dtype} and {right.dtype} have no supported common "
        "representation that preserves all values exactly"
    )
