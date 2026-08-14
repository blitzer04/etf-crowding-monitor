"""Tests for the supported canonical numeric representation boundary."""

from decimal import Decimal

import pandas as pd
import pyarrow as pa
import pytest
from pandas.api.types import is_any_real_numeric_dtype

from etf_crowding.data.numeric_dtypes import (
    NumericDtypeHarmonizationError,
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
