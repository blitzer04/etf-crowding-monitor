"""Synchronize temporary process-global yfinance runtime configuration."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from threading import RLock

import yfinance as yf  # type: ignore[import-untyped]

_YFINANCE_RUNTIME_LOCK = RLock()


@contextmanager
def yfinance_exception_visibility() -> Iterator[None]:
    """Expose yfinance exceptions while preserving its exact prior setting.

    yfinance exception visibility is process-global, so all project adapters
    must serialize the full interval during which they temporarily disable
    exception hiding.

    Yields:
        Control while yfinance exception hiding is disabled.
    """

    with _YFINANCE_RUNTIME_LOCK:
        previous_hide_exceptions = yf.config.debug.hide_exceptions
        yf.config.debug.hide_exceptions = False
        try:
            yield
        finally:
            yf.config.debug.hide_exceptions = previous_hide_exceptions
