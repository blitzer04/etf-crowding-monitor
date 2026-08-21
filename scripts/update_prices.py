"""Update the canonical daily ETF price-history dataset."""

from __future__ import annotations

import argparse
import logging
from collections.abc import Sequence
from pathlib import Path

from etf_crowding.config import load_etf_universe
from etf_crowding.data.prices import (
    DEFAULT_PRICE_FILENAME,
    DEFAULT_PRICE_START_DATE,
    download_price_history,
    persist_price_history,
)
from etf_crowding.paths import get_processed_data_dir, get_snapshot_data_dir

LOGGER = logging.getLogger(__name__)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Download and update canonical daily ETF prices from yfinance."
    )
    parser.add_argument(
        "--start",
        default=DEFAULT_PRICE_START_DATE.isoformat(),
        help="Inclusive start date in YYYY-MM-DD format (default: 2018-01-01).",
    )
    parser.add_argument(
        "--end",
        help=(
            "Exclusive end date in YYYY-MM-DD format (default: current "
            "America/New_York calendar date)."
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        help=(
            "Optional Parquet destination. Defaults to "
            "data/processed/etf_prices_daily.parquet."
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the price update and return a process exit status.

    A partial batch is persisted only when at least one ticker succeeded, and
    empty or failed tickers are reported explicitly. An all-failed or all-empty
    batch exits without replacing an existing canonical file.

    Args:
        argv: Optional command-line arguments for testing.

    Returns:
        Zero after a successful full or clearly reported partial update; one if
        no usable observations were downloaded or persistence failed.
    """

    parser = _build_parser()
    arguments = parser.parse_args(argv)
    output_path = arguments.output or (
        get_processed_data_dir() / DEFAULT_PRICE_FILENAME
    )

    universe = load_etf_universe()
    tickers = tuple(definition.ticker for definition in universe)
    LOGGER.info("Requesting daily prices for %d ETFs.", len(tickers))

    try:
        result = download_price_history(
            tickers=tickers,
            start=arguments.start,
            end=arguments.end,
        )
    except ValueError as error:
        LOGGER.error("Invalid price update request: %s", error)
        return 1

    LOGGER.info("Successful tickers: %d.", len(result.successful_tickers))
    if result.empty_tickers:
        LOGGER.warning(
            "Empty tickers (%d): %s.",
            len(result.empty_tickers),
            ", ".join(result.empty_tickers),
        )
    if result.failed_tickers:
        LOGGER.warning(
            "Failed tickers (%d): %s.",
            len(result.failed_tickers),
            ", ".join(result.failed_tickers),
        )

    if result.prices.empty:
        LOGGER.error(
            "No usable price observations were downloaded; existing output was "
            "not replaced."
        )
        return 1

    if result.empty_tickers or result.failed_tickers:
        LOGGER.warning("Persisting a partial batch containing only successful tickers.")

    try:
        persistence = persist_price_history(
            result.prices,
            output_path,
            retrieval_statuses=result.statuses,
            snapshot_dir=get_snapshot_data_dir() / "prices",
        )
    except (OSError, ValueError) as error:
        LOGGER.error("Price history was not updated: %s", error)
        return 1

    if persistence.revised_row_count:
        LOGGER.warning(
            "Accepted source-vintage revisions: %d rows across %d tickers (%s).",
            persistence.revised_row_count,
            len(persistence.revised_tickers),
            ", ".join(persistence.revised_tickers),
        )
        LOGGER.info("Superseded canonical snapshot: %s", persistence.snapshot_path)

    LOGGER.info("Rows written: %d.", len(persistence.prices))
    LOGGER.info("Output: %s", output_path.resolve())
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    raise SystemExit(main())
