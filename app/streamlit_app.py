"""Local-only Streamlit entry point for verified signal-evaluation bundles."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import streamlit as st

from app.signal_dashboard import (
    VIEW_NAMES,
    render_dependence,
    render_etf_detail,
    render_overview,
    render_provenance,
    render_verified_context,
)
from etf_crowding.analysis import (
    DEFAULT_SIGNAL_BUNDLE_DIRNAME,
    SignalBundleError,
    VerifiedSignalEvaluationBundle,
    discover_signal_evaluation_runs,
    load_signal_evaluation_bundle,
)
from etf_crowding.paths import get_processed_data_dir

_VERIFIED_STATE_KEY = "signal_dashboard_verified_bundle"
_SELECTION_STATE_KEY = "signal_dashboard_selected_run"
_LAST_VIEW_STATE_KEY = "signal_dashboard_last_view"
_FRAGMENT_TRANSITION_NONCE_STATE_KEY = "signal_dashboard_fragment_transition_nonce"

# The integer nonce is generated only by this application. It makes each trusted
# script payload distinct without interpolating view, run, or artifact data.
_FRAGMENT_CLEAR_SCRIPT_TEMPLATE = """
<script data-transition-nonce="{transition_nonce:d}">
history.replaceState(
    null,
    "",
    window.location.pathname + window.location.search,
);
</script>
"""


def _bundle_root() -> Path:
    return get_processed_data_dir() / DEFAULT_SIGNAL_BUNDLE_DIRNAME


def _clear_verified_state() -> None:
    st.session_state.pop(_VERIFIED_STATE_KEY, None)


def _clear_fragment_on_view_change(view: str) -> None:
    previous_view = st.session_state.get(_LAST_VIEW_STATE_KEY)
    if previous_view is not None and previous_view != view:
        transition_nonce = (
            cast(
                int,
                st.session_state.get(_FRAGMENT_TRANSITION_NONCE_STATE_KEY, 0),
            )
            + 1
        )
        st.session_state[_FRAGMENT_TRANSITION_NONCE_STATE_KEY] = transition_nonce
        st.html(
            _FRAGMENT_CLEAR_SCRIPT_TEMPLATE.format(transition_nonce=transition_nonce),
            unsafe_allow_javascript=True,
        )
    st.session_state[_LAST_VIEW_STATE_KEY] = view


def main() -> None:
    """Run the fail-closed local signal dashboard."""

    st.set_page_config(
        page_title="Local ETF Signal Bundle Viewer",
        page_icon=None,
        layout="wide",
        initial_sidebar_state="expanded",
    )
    st.title("Local ETF Signal Bundle Viewer")
    st.caption(
        "Read-only, local, offline inspection of one explicitly selected fixed run. "
        "No provider request, refresh, export, or persistence is available."
    )

    root = _bundle_root()
    try:
        inventory = discover_signal_evaluation_runs(root)
    except SignalBundleError as error:
        _clear_verified_state()
        st.error(f"Bundle discovery failed: {error}")
        st.info(
            "No financial values are displayed. Create or restore the local bundle "
            "outside this viewer, then reload the app."
        )
        return

    options = ("", *inventory.run_ids)
    selected_run = st.sidebar.selectbox(
        "Verified run candidate",
        options,
        index=0,
        format_func=lambda value: "Select a local run…" if not value else value,
        help=(
            "Discovery lists directory names only. A run is not trusted until the "
            "six-file snapshot passes complete consumer verification."
        ),
    )
    prior_selection = st.session_state.get(_SELECTION_STATE_KEY)
    if selected_run != prior_selection:
        _clear_verified_state()
        st.session_state[_SELECTION_STATE_KEY] = selected_run

    if inventory.temporary_names or inventory.quarantined_names:
        st.sidebar.caption(
            f"Excluded in-progress directories: {len(inventory.temporary_names)} · "
            f"excluded quarantined directories: {len(inventory.quarantined_names)}"
        )
    if not selected_run:
        _clear_verified_state()
        st.info(
            "This viewer is local and offline. No run is selected automatically; "
            "select a run ID explicitly. The dashboard is not live market data, "
            "and no financial values appear before verification. Momentum and "
            "Volatility are standalone descriptive price diagnostics."
        )
        return

    previous = cast(
        VerifiedSignalEvaluationBundle | None,
        st.session_state.get(_VERIFIED_STATE_KEY),
    )
    try:
        with st.spinner("Snapshotting and verifying all six local bundle files…"):
            bundle = load_signal_evaluation_bundle(
                root, selected_run, previous=previous
            )
    except SignalBundleError as error:
        _clear_verified_state()
        st.error(f"Selected bundle failed closed: {error}")
        st.info(
            "No financial values are displayed. The viewer never repairs, fills, "
            "quarantines, replaces, or falls back to another run."
        )
        return

    st.session_state[_VERIFIED_STATE_KEY] = bundle
    render_verified_context(bundle)
    view = st.sidebar.radio("View", VIEW_NAMES, index=0)
    _clear_fragment_on_view_change(view)
    if view == "Overview":
        render_overview(bundle)
    elif view == "ETF detail":
        render_etf_detail(bundle)
    elif view == "Dependence diagnostics":
        render_dependence(bundle)
    else:
        render_provenance(bundle)


if __name__ == "__main__":
    main()
