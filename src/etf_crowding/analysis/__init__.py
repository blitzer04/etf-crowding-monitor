"""Reusable workflows for evaluating implemented standalone ETF signals."""

from etf_crowding.analysis.signal_evaluation import (
    COVERAGE_COLUMNS,
    DEPENDENCE_COLUMNS,
    EvaluationTarget,
    SignalEvaluation,
    SignalEvaluationError,
    SignalEvaluationRun,
    calculate_dependence_diagnostics,
    evaluate_price_signals,
    publish_signal_evaluation_bundle,
    resolve_evaluation_target,
    run_signal_evaluation,
    validate_signal_evaluation,
)

__all__ = [
    "COVERAGE_COLUMNS",
    "DEPENDENCE_COLUMNS",
    "EvaluationTarget",
    "SignalEvaluation",
    "SignalEvaluationError",
    "SignalEvaluationRun",
    "calculate_dependence_diagnostics",
    "evaluate_price_signals",
    "publish_signal_evaluation_bundle",
    "resolve_evaluation_target",
    "run_signal_evaluation",
    "validate_signal_evaluation",
]
