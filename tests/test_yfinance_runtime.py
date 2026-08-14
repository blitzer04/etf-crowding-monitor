"""Tests for synchronized temporary yfinance runtime configuration."""

from collections.abc import Callable
from datetime import date, datetime
from threading import Event, Thread

import pandas as pd
import pytest
import yfinance as yf  # type: ignore[import-untyped]

import etf_crowding.data.prices as price_module
import etf_crowding.data.shares as shares_module
import etf_crowding.data.yfinance_runtime as runtime_module
from etf_crowding.data.yfinance_runtime import yfinance_exception_visibility


def _run_and_capture(
    operation: Callable[[], None], errors: list[BaseException]
) -> None:
    try:
        operation()
    except BaseException as error:  # pragma: no cover - asserted in the caller
        errors.append(error)


def _join_thread(thread: Thread) -> None:
    thread.join(timeout=5)
    assert not thread.is_alive(), f"thread {thread.name!r} did not finish"


def _assert_runtime_lock_available_to_another_thread() -> None:
    acquisition_results: list[bool] = []

    def try_acquire() -> None:
        acquired = runtime_module._YFINANCE_RUNTIME_LOCK.acquire(blocking=False)
        acquisition_results.append(acquired)
        if acquired:
            runtime_module._YFINANCE_RUNTIME_LOCK.release()

    thread = Thread(target=try_acquire, name="runtime-lock-probe")
    thread.start()
    _join_thread(thread)
    assert acquisition_results == [True]


@pytest.mark.parametrize("initial_value", [True, False])
def test_exception_visibility_restores_exact_value_after_success(
    monkeypatch: pytest.MonkeyPatch, initial_value: bool
) -> None:
    monkeypatch.setattr(yf.config.debug, "hide_exceptions", initial_value)

    with yfinance_exception_visibility():
        assert yf.config.debug.hide_exceptions is False

    assert yf.config.debug.hide_exceptions is initial_value


@pytest.mark.parametrize("initial_value", [True, False])
def test_exception_visibility_restores_exact_value_after_failure(
    monkeypatch: pytest.MonkeyPatch, initial_value: bool
) -> None:
    monkeypatch.setattr(yf.config.debug, "hide_exceptions", initial_value)

    with pytest.raises(RuntimeError, match="synthetic provider failure"):
        with yfinance_exception_visibility():
            assert yf.config.debug.hide_exceptions is False
            raise RuntimeError("synthetic provider failure")

    assert yf.config.debug.hide_exceptions is initial_value


def test_price_and_shares_adapters_import_the_same_runtime_context() -> None:
    assert (
        price_module.yfinance_exception_visibility
        is runtime_module.yfinance_exception_visibility
    )
    assert (
        shares_module.yfinance_exception_visibility
        is runtime_module.yfinance_exception_visibility
    )
    assert not hasattr(price_module, "_YFINANCE_RUNTIME_LOCK")
    assert not hasattr(shares_module, "_YFINANCE_RUNTIME_LOCK")


def test_adapters_release_runtime_lock_before_payload_normalization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(yf.config.debug, "hide_exceptions", True)
    price_payload = object()
    shares_payload = object()
    normalizers_called: list[str] = []

    def fake_price_request(ticker: str, start_date: date, end_date: date) -> object:
        del ticker, start_date, end_date
        assert yf.config.debug.hide_exceptions is False
        return price_payload

    def fake_price_normalizer(payload: object, ticker: str) -> pd.DataFrame:
        del ticker
        assert payload is price_payload
        assert yf.config.debug.hide_exceptions is True
        _assert_runtime_lock_available_to_another_thread()
        normalizers_called.append("price")
        return pd.DataFrame()

    def fake_shares_request(
        ticker: str,
        start_date: date | None,
        end_date: date | None,
        query_reference_time: datetime | None,
    ) -> tuple[object, str]:
        del ticker, start_date, end_date, query_reference_time
        assert yf.config.debug.hide_exceptions is False
        return shares_payload, "America/New_York"

    def fake_shares_normalizer(
        payload: object, ticker: str, timezone: str
    ) -> pd.Series:
        del ticker, timezone
        assert payload is shares_payload
        assert yf.config.debug.hide_exceptions is True
        _assert_runtime_lock_available_to_another_thread()
        normalizers_called.append("shares")
        return pd.Series(dtype="Int64")

    monkeypatch.setattr(price_module, "_request_raw_yfinance_chart", fake_price_request)
    monkeypatch.setattr(
        price_module, "_raw_chart_to_provider_frame", fake_price_normalizer
    )
    monkeypatch.setattr(
        shares_module, "_request_raw_yfinance_shares", fake_shares_request
    )
    monkeypatch.setattr(
        shares_module, "_raw_shares_to_provider_series", fake_shares_normalizer
    )

    price_module._download_yfinance_ticker("SPY", date(2024, 1, 1), date(2024, 1, 3))
    shares_module._download_yfinance_ticker("QQQ", None, None)

    assert normalizers_called == ["price", "shares"]


@pytest.mark.parametrize("initial_value", [True, False])
def test_overlapping_contexts_are_serialized_and_restore_original_value(
    monkeypatch: pytest.MonkeyPatch, initial_value: bool
) -> None:
    monkeypatch.setattr(yf.config.debug, "hide_exceptions", initial_value)
    first_entered = Event()
    release_first = Event()
    second_started = Event()
    second_entered = Event()
    events: list[str] = []
    errors: list[BaseException] = []

    def first_operation() -> None:
        with yfinance_exception_visibility():
            events.append("first_entered")
            first_entered.set()
            if not release_first.wait(timeout=5):
                raise TimeoutError("first operation was not released")
            events.append("first_leaving")

    def second_operation() -> None:
        second_started.set()
        with yfinance_exception_visibility():
            events.append("second_entered")
            second_entered.set()

    first_thread = Thread(
        target=_run_and_capture,
        args=(first_operation, errors),
        name="first-yfinance-operation",
    )
    second_thread = Thread(
        target=_run_and_capture,
        args=(second_operation, errors),
        name="second-yfinance-operation",
    )

    first_thread.start()
    assert first_entered.wait(timeout=5)
    second_thread.start()
    assert second_started.wait(timeout=5)
    unexpectedly_acquired = runtime_module._YFINANCE_RUNTIME_LOCK.acquire(
        blocking=False
    )
    if unexpectedly_acquired:
        runtime_module._YFINANCE_RUNTIME_LOCK.release()
    try:
        assert not unexpectedly_acquired
        assert not second_entered.is_set()
    finally:
        release_first.set()
    _join_thread(first_thread)
    _join_thread(second_thread)

    assert errors == []
    assert events == ["first_entered", "first_leaving", "second_entered"]
    assert yf.config.debug.hide_exceptions is initial_value


def test_price_and_shares_provider_intervals_share_runtime_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(yf.config.debug, "hide_exceptions", True)
    price_entered = Event()
    release_price = Event()
    shares_started = Event()
    shares_entered = Event()
    events: list[str] = []
    errors: list[BaseException] = []
    price_payload = object()
    shares_payload = object()

    def fake_price_request(ticker: str, start_date: date, end_date: date) -> object:
        del ticker, start_date, end_date
        assert yf.config.debug.hide_exceptions is False
        events.append("price_provider_entered")
        price_entered.set()
        if not release_price.wait(timeout=5):
            raise TimeoutError("price provider operation was not released")
        events.append("price_provider_leaving")
        return price_payload

    def fake_shares_request(
        ticker: str,
        start_date: date | None,
        end_date: date | None,
        query_reference_time: datetime | None,
    ) -> tuple[object, str]:
        del ticker, start_date, end_date, query_reference_time
        assert yf.config.debug.hide_exceptions is False
        events.append("shares_provider_entered")
        shares_entered.set()
        return shares_payload, "America/New_York"

    def run_price_adapter() -> None:
        price_module._download_yfinance_ticker(
            "SPY", date(2024, 1, 1), date(2024, 1, 3)
        )

    def run_shares_adapter() -> None:
        shares_started.set()
        shares_module._download_yfinance_ticker("QQQ", None, None)

    monkeypatch.setattr(price_module, "_request_raw_yfinance_chart", fake_price_request)
    monkeypatch.setattr(
        price_module,
        "_raw_chart_to_provider_frame",
        lambda payload, ticker: pd.DataFrame() if payload is price_payload else None,
    )
    monkeypatch.setattr(
        shares_module, "_request_raw_yfinance_shares", fake_shares_request
    )
    monkeypatch.setattr(
        shares_module,
        "_raw_shares_to_provider_series",
        lambda payload, ticker, timezone: (
            pd.Series(dtype="Int64") if payload is shares_payload else None
        ),
    )

    price_thread = Thread(
        target=_run_and_capture,
        args=(run_price_adapter, errors),
        name="price-provider-operation",
    )
    shares_thread = Thread(
        target=_run_and_capture,
        args=(run_shares_adapter, errors),
        name="shares-provider-operation",
    )

    price_thread.start()
    assert price_entered.wait(timeout=5)
    shares_thread.start()
    assert shares_started.wait(timeout=5)
    unexpectedly_acquired = runtime_module._YFINANCE_RUNTIME_LOCK.acquire(
        blocking=False
    )
    if unexpectedly_acquired:
        runtime_module._YFINANCE_RUNTIME_LOCK.release()
    try:
        assert not unexpectedly_acquired
        assert not shares_entered.is_set()
    finally:
        release_price.set()
    _join_thread(price_thread)
    _join_thread(shares_thread)

    assert errors == []
    assert events == [
        "price_provider_entered",
        "price_provider_leaving",
        "shares_provider_entered",
    ]
    assert yf.config.debug.hide_exceptions is True
