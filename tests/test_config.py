"""Tests for ETF universe configuration loading and validation."""

import os
import subprocess
import sys
from importlib.resources import files
from pathlib import Path
from zipfile import ZipFile

import pytest

from etf_crowding.config import ETFDefinition, load_etf_universe

EXPECTED_TICKERS = {
    "ARKK",
    "DIA",
    "ICLN",
    "IGV",
    "IWM",
    "KRE",
    "LIT",
    "QQQ",
    "SMH",
    "SOXX",
    "SPY",
    "TAN",
    "VGT",
    "VTI",
    "XBI",
    "XLB",
    "XLE",
    "XLF",
    "XLI",
    "XLK",
    "XLP",
    "XLV",
    "XLY",
    "XOP",
}


def _write_config(tmp_path: Path, contents: str) -> Path:
    config_path = tmp_path / "etf_universe.yaml"
    config_path.write_text(contents, encoding="utf-8")
    return config_path


def test_explicit_path_etf_universe_loads_successfully(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path,
        """
etfs:
  - ticker: TEST
    name: Test Market ETF
    category: Test Category
""",
    )

    universe = load_etf_universe(config_path)

    assert universe == (
        ETFDefinition(ticker="TEST", name="Test Market ETF", category="Test Category"),
    )


def test_default_universe_contains_all_expected_etfs() -> None:
    universe = load_etf_universe()

    assert len(universe) == 24
    assert {entry.ticker for entry in universe} == EXPECTED_TICKERS


def test_default_universe_tickers_are_unique() -> None:
    tickers = [entry.ticker for entry in load_etf_universe()]

    assert len(tickers) == len(set(tickers))


def test_packaged_etf_universe_resource_exists() -> None:
    resource = files("etf_crowding.resources").joinpath("etf_universe.yaml")

    assert resource.is_file()


def test_default_universe_loads_from_isolated_zip_package(tmp_path: Path) -> None:
    package_dir = Path(__file__).parents[1] / "src" / "etf_crowding"
    archive_path = tmp_path / "installed_package.zip"
    packaged_files = (
        Path("__init__.py"),
        Path("config.py"),
        Path("resources/__init__.py"),
        Path("resources/etf_universe.yaml"),
    )

    with ZipFile(archive_path, "w") as archive:
        for relative_path in packaged_files:
            archive.write(
                package_dir / relative_path,
                Path("etf_crowding") / relative_path,
            )

    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(archive_path)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from etf_crowding.config import load_etf_universe; "
                "print(len(load_etf_universe()))"
            ),
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "24"


def test_duplicate_tickers_are_rejected_case_insensitively(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path,
        """
etfs:
  - ticker: SPY
    name: First ETF
    category: Broad Market
  - ticker: spy
    name: Duplicate ETF
    category: Broad Market
""",
    )

    with pytest.raises(ValueError, match="Duplicate ETF ticker 'spy'"):
        load_etf_universe(config_path)


def test_duplicate_ticker_mapping_keys_are_rejected(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path,
        """
etfs:
  - ticker: SPY
    ticker: QQQ
    name: Duplicate Key ETF
    category: Broad Market
""",
    )

    with pytest.raises(ValueError, match=r"duplicate key 'ticker'"):
        load_etf_universe(config_path)


def test_duplicate_top_level_etfs_keys_are_rejected(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path,
        """
etfs:
  - ticker: SPY
    name: First ETF
    category: Broad Market
etfs:
  - ticker: QQQ
    name: Second ETF
    category: Broad Market
""",
    )

    with pytest.raises(ValueError, match=r"duplicate key 'etfs'"):
        load_etf_universe(config_path)


@pytest.mark.parametrize("missing_field", ["ticker", "name", "category"])
def test_missing_required_fields_are_rejected(
    tmp_path: Path, missing_field: str
) -> None:
    fields = {
        "ticker": "SPY",
        "name": "SPDR S&P 500 ETF Trust",
        "category": "Broad Market",
    }
    fields.pop(missing_field)
    entry_lines = "\n".join(f"    {key}: {value}" for key, value in fields.items())
    config_path = _write_config(tmp_path, f"etfs:\n  -\n{entry_lines}\n")

    with pytest.raises(ValueError, match=rf"non-empty string '{missing_field}'"):
        load_etf_universe(config_path)


@pytest.mark.parametrize("field", ["ticker", "name", "category"])
def test_blank_required_fields_are_rejected(tmp_path: Path, field: str) -> None:
    values = {
        "ticker": "SPY",
        "name": "SPDR S&P 500 ETF Trust",
        "category": "Broad Market",
    }
    values[field] = "  "
    entry_lines = "\n".join(f"    {key}: '{value}'" for key, value in values.items())
    config_path = _write_config(tmp_path, f"etfs:\n  -\n{entry_lines}\n")

    with pytest.raises(ValueError, match=rf"non-empty string '{field}'"):
        load_etf_universe(config_path)


def test_malformed_yaml_structure_is_rejected(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path,
        """
- ticker: SPY
  name: SPDR S&P 500 ETF Trust
  category: Broad Market
""",
    )

    with pytest.raises(ValueError, match="top-level mapping"):
        load_etf_universe(config_path)


def test_invalid_yaml_is_rejected(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path, "etfs: [")

    with pytest.raises(ValueError, match="Invalid YAML"):
        load_etf_universe(config_path)
