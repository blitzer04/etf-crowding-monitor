# Local Streamlit Signal-Bundle Viewer

This directory contains the local-only presentation layer for one existing,
validated signal-evaluation bundle. The app does not calculate financial
signals. It calls the reusable consumer API in
`etf_crowding.analysis.signal_bundle`, which verifies the exact six snapshotted
files before the presentation layer receives any values.

## Launch

From the repository root, with a local bundle already present under
`data/processed/signal_evaluations/<UTC run ID>/`, run:

```text
.venv\Scripts\python.exe -m streamlit run app\streamlit_app.py --server.address 127.0.0.1 --browser.gatherUsageStats false
```

The viewer discovers candidate direct-child run names but defaults to a blank
selection. The user must select a run explicitly. Discovery does not establish
validity, and the app never falls back to another run. A selected run must pass
manifest, hash, schema, row-count, configured-universe, timing, status,
staleness, and cross-artifact semantic verification before any financial value
appears.

## Views

- **Overview:** separate configured-order Momentum and Volatility static tables
  and own-history percentile charts for all 24 ETFs. Wide content is split into
  value/status and eligibility/freshness tables without sorting or ranking.
- **ETF detail:** adjusted-price history, separate raw and normalized signal
  histories, exact observation/prospective-use dates, eligibility, reference
  counts, missing-input diagnostics, and staleness warnings. Chart gaps are not
  connected.
- **Dependence diagnostics:** secondary descriptive Pearson and Spearman output
  with explicit ETF and session-date filters and bounded static tables,
  including pair counts, dates, included populations, estimator status, and
  universe status. It contains no p-values or pooled estimate.
- **Provenance and limitations:** run timing, Git state, evaluation and viewer
  versions, the effective ordered parsed universe-definitions hash, exactly six
  artifact rows, schemas, expected and computed hashes, consumer verification
  status, and the current-vintage, point-in-time, survivorship,
  provider-revision, rights, and interpretation boundaries. The stored manifest
  field remains `universe_config_sha256`; it is the SHA-256 of the deterministic
  JSON serialization of the effective parsed ETF definitions in configured
  order, not the raw packaged or external YAML resource bytes. The manifest
  digest is computed for the exact snapshot but is not internally
  self-authenticating because the manifest is unsigned and could be replaced
  with all five Parquet artifacts.

## Operating boundary

The controlled manual price-refresh procedure, including authorization,
retention, failure, and post-run verification, is documented in
[`../docs/price-signal-operations.md`](../docs/price-signal-operations.md).

The app is a fixed-run viewer, not a live monitor. It has no provider call,
refresh button, export/download, public hosting, derived persistence, repair,
quarantine, or fallback behavior. It does not access volume, shares, holdings,
Flow, or Concentration. It does not create or imply a composite, Crowding Score,
risk class, traffic-light classification, threshold, weight, missing-component
reweighting, grade, causal result, or predictive signal.

All tables use non-exporting static presentation. Plotly mode bars and image
export are disabled while hover inspection remains available. No unsafe HTML or
client-side hiding is used to enforce this boundary.

Missing data remain explicitly missing with the verified status or reason.
Previously verified in-memory state is cleared when selection changes or
verification fails. On an unchanged selection, the app still snapshots and
hashes all six files; it reuses the immutable verified object only when the
combined digest of that new snapshot is unchanged. Failed validation is never
cached.

The dashboard is not approved for public display or redistribution. Provider
and derived-display rights remain unresolved.
