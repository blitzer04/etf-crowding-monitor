"""Run the offline-first standalone Momentum and Volatility evaluation."""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

from etf_crowding.analysis import SignalEvaluationError, run_signal_evaluation

LOGGER = logging.getLogger(__name__)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Evaluate standalone Momentum and Volatility from canonical prices."
        )
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help=(
            "Explicitly request a 24-ETF Yahoo price refresh before evaluation. "
            "Offline canonical-file evaluation is the default."
        ),
    )
    parser.add_argument(
        "--evaluation-instant",
        help=(
            "Optional timezone-aware ISO-8601 instant used to resolve the latest "
            "completed XNYS session."
        ),
    )
    parser.add_argument(
        "--prices",
        type=Path,
        help="Optional canonical price Parquet path.",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        help=(
            "Optional run-bundle root. Defaults to data/processed/signal_evaluations/."
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the thin CLI adapter and return a process exit status.

    Args:
        argv: Optional arguments for tests. Defaults to process arguments.

    Returns:
        Zero after a validated bundle is published, otherwise one.
    """

    supplied_arguments = list(sys.argv[1:] if argv is None else argv)
    arguments = _build_parser().parse_args(supplied_arguments)
    try:
        result = run_signal_evaluation(
            refresh=arguments.refresh,
            evaluation_instant=arguments.evaluation_instant,
            price_path=arguments.prices,
            output_root=arguments.output_root,
            command_arguments=supplied_arguments,
        )
    except (OSError, SignalEvaluationError, ValueError) as error:
        LOGGER.error("Signal evaluation failed: %s", error)
        return 1

    LOGGER.info("Mode: %s", result.evaluation.mode)
    LOGGER.info(
        "Target XNYS session: %s",
        result.evaluation.target.target_session.date().isoformat(),
    )
    LOGGER.info("Bundle: %s", result.bundle_path)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    raise SystemExit(main())
