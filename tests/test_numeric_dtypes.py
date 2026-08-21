"""Tests for the supported canonical numeric representation boundary."""

from decimal import Decimal

import numpy as np
import pandas as pd
import pyarrow as pa
import pytest
from pandas.api.types import is_any_real_numeric_dtype

from etf_crowding.data.numeric_dtypes import (
    NumericDtypeHarmonizationError,
    build_lossless_real_numeric_series,
    cast_real_numeric_series_losslessly,
    harmonize_real_numeric_series,
    is_supported_canonical_numeric_dtype,
)


@pytest.mark.parametrize(
    "dtype",
    [
        "int64",
        "uint64",
        "float64",
        "Int64",
        "UInt64",
        "Float64",
        "int64[pyarrow]",
        "uint64[pyarrow]",
        "double[pyarrow]",
    ],
)
def test_integer_and_floating_numeric_dtypes_are_supported(dtype: str) -> None:
    assert is_supported_canonical_numeric_dtype(pd.Series([1], dtype=dtype).dtype)


def test_arrow_decimal_cannot_be_harmonized_into_canonical_data() -> None:
    decimal_dtype = pd.ArrowDtype(pa.decimal128(20, 2))
    left = pd.Series([Decimal("100.00")], dtype=decimal_dtype)
    right = pd.Series([Decimal("101.00")], dtype=decimal_dtype)

    assert is_any_real_numeric_dtype(decimal_dtype)
    assert not is_supported_canonical_numeric_dtype(decimal_dtype)
    with pytest.raises(
        NumericDtypeHarmonizationError,
        match=r"supported dense canonical integer or floating dtypes.*decimal128",
    ):
        harmonize_real_numeric_series(left, right)


@pytest.mark.parametrize(
    "values",
    [
        pd.Series([100], dtype=pd.SparseDtype("int64", 0)),
        pd.Series([100.25], dtype=pd.SparseDtype("float64", 0.0)),
        pd.Series([pd.NA], dtype=pd.SparseDtype("float64", float("nan"))),
    ],
    ids=["sparse-int64", "sparse-float64", "sparse-missing"],
)
def test_sparse_numeric_dtypes_cannot_be_harmonized(
    values: pd.Series,
) -> None:
    assert is_any_real_numeric_dtype(values.dtype)
    assert not is_supported_canonical_numeric_dtype(values.dtype)
    with pytest.raises(
        NumericDtypeHarmonizationError,
        match=r"supported dense canonical integer or floating dtypes.*Sparse",
    ):
        harmonize_real_numeric_series(values, values)


def test_lossless_builder_preserves_large_integer_null_and_zero() -> None:
    large_integer = 9_007_199_254_740_993

    result = build_lossless_real_numeric_series([large_integer, None, 0])

    assert str(result.dtype) == "Int64"
    assert result.iloc[0] == large_integer
    assert pd.isna(result.iloc[1])
    assert result.iloc[2] == 0


def test_lossless_builder_preserves_unsigned_integer_above_int64() -> None:
    large_unsigned_integer = 2**63 + 1

    result = build_lossless_real_numeric_series([large_unsigned_integer, None])

    assert str(result.dtype) == "UInt64"
    assert result.iloc[0] == large_unsigned_integer
    assert pd.isna(result.iloc[1])


@pytest.mark.parametrize(
    ("values", "expected_dtype"),
    [
        pytest.param([2**53 + 1, 2**63], "UInt64", id="uint64-transition"),
        pytest.param(
            [2**53 + 1, 2**63 - 1],
            "Int64",
            id="signed-upper-bound",
        ),
        pytest.param([0, 2**64 - 1], "UInt64", id="uint64-full-domain"),
    ],
)
def test_lossless_builder_materializes_integer_boundaries_without_float_inference(
    values: list[int],
    expected_dtype: str,
) -> None:
    result = build_lossless_real_numeric_series(values)

    assert str(result.dtype) == expected_dtype
    assert result.tolist() == values


def test_lossless_builder_preserves_missing_with_uint64_boundary() -> None:
    result = build_lossless_real_numeric_series([None, 2**63])

    assert str(result.dtype) == "UInt64"
    assert pd.isna(result.iloc[0])
    assert result.iloc[1] == 2**63


@pytest.mark.parametrize("value", [-(2**63) - 1, 2**64])
def test_lossless_builder_rejects_values_outside_64_bit_domains(value: int) -> None:
    with pytest.raises(
        NumericDtypeHarmonizationError,
        match="outside supported signed and unsigned 64-bit storage",
    ):
        build_lossless_real_numeric_series([value])


def test_lossless_builder_accepts_exact_integer_float_combination() -> None:
    result = build_lossless_real_numeric_series([100_000_000, 100_000_000.5, None])

    assert str(result.dtype) == "Float64"
    assert result.iloc[:2].tolist() == [100_000_000.0, 100_000_000.5]
    assert pd.isna(result.iloc[2])


def test_lossless_builder_rejects_inexact_integer_float_combination() -> None:
    with pytest.raises(
        NumericDtypeHarmonizationError,
        match="no supported common representation.*preserves all values exactly",
    ):
        build_lossless_real_numeric_series([9_007_199_254_740_993, 100.5])


@pytest.mark.parametrize(
    "values",
    [
        pytest.param([2**64 - 1, 1.0], id="uint64-max"),
        pytest.param([2**63 + 1, 1.0], id="above-int64"),
    ],
)
def test_lossless_builder_uses_uint64_for_exact_integral_float_union(
    values: list[int | float],
) -> None:
    result = build_lossless_real_numeric_series(values)

    assert str(result.dtype) == "UInt64"
    assert result.tolist() == [int(value) for value in values]


def test_lossless_builder_rejects_uint64_and_fraction_without_exact_union() -> None:
    with pytest.raises(
        NumericDtypeHarmonizationError,
        match="no supported common representation.*preserves all values exactly",
    ):
        build_lossless_real_numeric_series([2**63 + 1, 0.5])


def test_signed_integer_and_large_integral_float_do_not_use_wraparound() -> None:
    result = build_lossless_real_numeric_series([-1, float(2**63)])

    assert str(result.dtype) == "Float64"
    assert result.tolist() == [-1.0, float(2**63)]


def test_harmonization_preserves_uint64_max_instead_of_wrapping_to_int64() -> None:
    unsigned = pd.Series(
        np.array([2**64 - 1], dtype=np.uint64),
        dtype="UInt64",
    )
    integral_float = pd.Series([1.0], dtype="Float64")

    harmonized_unsigned, harmonized_float = harmonize_real_numeric_series(
        unsigned, integral_float
    )

    assert str(harmonized_unsigned.dtype) == "UInt64"
    assert str(harmonized_float.dtype) == "UInt64"
    assert harmonized_unsigned.tolist() == [2**64 - 1]
    assert harmonized_float.tolist() == [1]


@pytest.mark.parametrize(
    "value",
    [True, 1 + 0j, Decimal("100.0"), "100", [100], np.array([100])],
    ids=["bool", "complex", "decimal", "string", "list", "array"],
)
def test_lossless_builder_rejects_unsupported_source_values(value: object) -> None:
    with pytest.raises(NumericDtypeHarmonizationError):
        build_lossless_real_numeric_series([value])


def test_float64_cast_requires_exact_value_and_missing_round_trip() -> None:
    exact = pd.Series([2**53, pd.NA, 0], dtype="Int64")
    converted = cast_real_numeric_series_losslessly(exact, "float64")

    assert converted.tolist()[:1] == [float(2**53)]
    assert pd.isna(converted.iloc[1])
    assert converted.iloc[2] == 0.0

    inexact = pd.Series([2**53 + 1], dtype="Int64")
    with pytest.raises(
        NumericDtypeHarmonizationError,
        match="does not preserve every value and missing state exactly",
    ):
        cast_real_numeric_series_losslessly(inexact, "float64")


@pytest.mark.parametrize(
    ("source", "target_dtype"),
    [
        pytest.param(
            pd.Series(
                np.array([2**64 - 1], dtype=np.uint64),
                dtype="UInt64",
            ),
            "Int64",
            id="uint64-max-to-int64",
        ),
        pytest.param(
            pd.Series([-1], dtype="Int64"),
            "UInt64",
            id="negative-int64-to-uint64",
        ),
    ],
)
def test_cross_signed_cast_rejects_modular_wraparound(
    source: pd.Series,
    target_dtype: str,
) -> None:
    with pytest.raises(
        NumericDtypeHarmonizationError,
        match="does not preserve every value and missing state exactly",
    ):
        cast_real_numeric_series_losslessly(source, target_dtype)  # type: ignore[arg-type]
