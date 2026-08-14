"""Update the canonical historical ETF shares-outstanding dataset."""

from __future__ import annotations

import argparse
import logging
from collections.abc import Sequence
from pathlib import Path

from etf_crowding.config import load_etf_universe
from etf_crowding.data.shares import (
    DEFAULT_SHARES_FILENAME,
    download_shares_history,
    persist_shares_history,
)
from etf_crowding.paths import get_processed_data_dir, get_snapshot_data_dir

LOGGER = logging.getLogger(__name__)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Download and update historical ETF shares outstanding."
    )
    parser.add_argument(
        "--start",
        help=(
            "Optional provider query start in YYYY-MM-DD format. If omitted, "
            "yfinance 1.5.2 queries 548 days before its resolved end."
        ),
    )
    parser.add_argument(
        "--end",
        help=(
            "Optional provider query end in YYYY-MM-DD format. If omitted, "
            "one batch reference instant is converted to each ticker's "
            "exchange timezone."
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        help=(
            "Optional Parquet destination. Defaults to "
            "data/processed/etf_shares_outstanding.parquet."
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the shares update and return a process exit status.

    Partial valid batches are persisted without deleting history for failed,
    empty, or unrequested tickers. An entirely failed or empty batch exits
    without replacing existing canonical history.

    Args:
        argv: Optional command-line arguments for testing.

    Returns:
        Zero after a full or clearly reported partial update; one when no dated
        observations are available or persistence fails.
    """

    parser = _build_parser()
    arguments = parser.parse_args(argv)
    output_path = arguments.output or (
        get_processed_data_dir() / DEFAULT_SHARES_FILENAME
    )

    universe = load_etf_universe()
    tickers = tuple(definition.ticker for definition in universe)
    LOGGER.info("Requesting historical shares outstanding for %d ETFs.", len(tickers))

    try:
        result = download_shares_history(
            tickers=tickers,
            start=arguments.start,
            end=arguments.end,
        )
    except ValueError as error:
        LOGGER.error("Invalid shares update request: %s", error)
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

    if result.shares.empty:
        LOGGER.error(
            "No dated shares observations were downloaded; existing output was "
            "not replaced."
        )
        return 1

    if result.empty_tickers or result.failed_tickers:
        LOGGER.warning("Persisting a partial batch containing valid tickers only.")

    try:
        persistence = persist_shares_history(
            result.shares,
            output_path,
            snapshot_dir=get_snapshot_data_dir() / "shares",
        )
    except (OSError, ValueError) as error:
        LOGGER.error("Shares history was not updated: %s", error)
        return 1

    if persistence.revised_row_count:
        LOGGER.warning(
            "Accepted shares source-vintage revisions: %d rows across %d tickers (%s).",
            persistence.revised_row_count,
            len(persistence.revised_tickers),
            ", ".join(persistence.revised_tickers),
        )
        LOGGER.info("Superseded canonical snapshot: %s", persistence.snapshot_path)

    LOGGER.info("Rows written: %d.", len(persistence.shares))
    LOGGER.info("Output: %s", output_path.resolve())
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    raise SystemExit(main())
