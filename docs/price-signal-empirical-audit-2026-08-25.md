# Price-signal empirical audit — 2026-08-25

## Scope and evidence boundary

Day 9 executed one controlled Yahoo/yfinance historical-price refresh and then
performed an independent offline audit of the resulting canonical data and
standalone Momentum and Volatility evaluation bundle. This note records the
verified operational facts and interpretation boundary; it does not change any
financial methodology.

This Markdown note is repository-tracked. The canonical Parquet, persistent
lock, and evaluation bundle under `data/` are local generated artifacts ignored
by Git and are not included in the repository. The results below describe the
audited local adjusted-price vintage only. They are not a point-in-time
historical reconstruction.

## Controlled refresh

- Evaluation target: `2026-08-24` XNYS session.
- Request interval: `[2018-01-01, 2026-08-25)`.
- Run ID: `20260825T132408764517Z`.
- One authorized `download_price_history` batch was executed.
- Exactly 24 configured ETF acquisition operations ran in configuration order.
- The project added no retry and issued no second batch. yfinance-managed
  ancillary transport activity means the number of raw HTTP transmissions must
  not be described as exactly 24.
- All 24 ETF operations succeeded. Each returned 2,172 rows spanning
  `2018-01-02` through `2026-08-24`, with no empty or failed ticker.

The configured acquisition population was `SPY`, `QQQ`, `IWM`, `DIA`, `VTI`,
`XLK`, `VGT`, `SMH`, `SOXX`, `IGV`, `XLF`, `KRE`, `XLE`, `XOP`, `XLV`, `XBI`,
`XLY`, `XLP`, `XLI`, `XLB`, `ARKK`, `TAN`, `ICLN`, and `LIT`.

## Local artifact identity

The audited canonical file contained 52,128 rows, zero duplicate ticker/date
keys, and zero missing `adjusted_close` values. Every ETF had exactly the 2,172
XNYS sessions in the audited interval. The canonical file and immutable bundle
input were semantically identical; their different byte hashes reflect the
documented physical timestamp and Arrow string representations, not different
financial values, missing masks, timestamps, keys, or row order.

| Local artifact | Rows | SHA-256 |
| --- | ---: | --- |
| `data/processed/etf_prices_daily.parquet` | 52,128 | `65d3929b04755bcd6dd400e70fd99ba3d2cbdd3435f70ba79e34764ca6f9b0e9` |
| `input_prices.parquet` | 52,128 | `5349c5656e5cc8e1e83fb0c9ac8bdd53014e167904a7e6d1e1019953c69ce019` |
| `coverage.parquet` | 24 | `74ea1cc71cfd80b79c32539996bfe299c09319f376e17b5f134d70802a7e1aa1` |
| `momentum.parquet` | 52,128 | `e86153e303048a2cf2524b37255677dd60c46f4ed49973bc2edb3f559da187d2` |
| `volatility.parquet` | 52,128 | `434b466ade6944fb996f4414e2c6d1554f0d3a050a416e5a17fffabc25131a4d` |
| `dependence.parquet` | 4,392 | `d4bff85ded9621618797cbd1c1b4e0709d4a9b6824c33c12484b980beb019e91` |
| `manifest.json` | — | `8184fd7f7af24811110c4d2c1670920bc3a62d5f6c3b231fb3d95bac8fc4e29d` |

The bundle contained only its six expected files. The manifest relationships,
current final-path hashes, schemas, row counts, universe hash, Git provenance,
request bounds, package versions, acquisition metadata, and creation time all
passed consumer-side verification. `coverage.parquet` has exactly **53
columns**.

## Standalone signal results

All 24 ETFs had eligible target-session Momentum and Volatility raw values and
percentiles. Each percentile used exactly 755 eligible prior observations from
`d_t-755` through `d_t-1`; the current observation was excluded. Price, raw
signal, and normalized-signal staleness were all zero XNYS sessions.

The independent audit recalculated the complete Momentum and Volatility outputs
in memory through the committed public APIs. Stored columns, keys, row order,
missing masks, eligibility, statuses, endpoint and window dates, raw and display
values, reference counts, percentiles, prospective sessions, and diagnostic
date collections matched the recomputed outputs. Representative high-precision
calculations also confirmed the approved 252-session Momentum and 21-return
sample-Volatility formulas without filling, clipping, or shifting inputs.

The full 24-ETF current-value table remains in the local bundle and is not
copied into repository documentation. These observations are standalone
historical-relative price diagnostics, not risk classes or a composite.

## Dependence diagnostics

The audit independently rebuilt the exact-date joins without using the stored
dependence calculations. It confirmed 1,668 paired dates per ETF from
`2020-01-03` through `2026-08-24`, 48 per-ETF estimator rows, and 4,344
per-session estimator rows. Pair counts, date bounds, included ticker sets,
statuses, universe labels, and estimates matched the stored results; the maximum
independent floating-point difference was `5.55e-16`.

Five sessions correctly produced `constant_input` rather than a correlation
estimate:

- `2020-03-11`;
- `2020-03-12`;
- `2020-03-16`;
- `2020-03-24`; and
- `2020-03-26`.

On each date, all 24 inputs were finite and eligible and every Volatility
percentile was exactly `100.0`; Momentum was not constant. The status therefore
followed mathematically from the data rather than missingness, serialization,
or grouping error.

These Pearson and Spearman estimates are descriptive. They do not establish
causality, predictive value, investor crowding, future returns, or suitability
for combining Momentum and Volatility.

## Data-quality and timing observations

The canonical volume for IWM on `2019-07-30` is `1,200`, compared with much
larger adjacent-session observations. It is an unresolved provider-source
observation, not an established error. It requires external corroboration before
future volume-based analysis and is not used by the implemented Momentum or
Volatility calculations.

For this run, the target closed at `2026-08-24T20:00:00Z`; the next XNYS session
opened at `2026-08-25T13:30:00Z`. All 24 provider retrievals completed before
that open, and the target-session outputs consistently identify `2026-08-25` as
their first prospective-use session. This verifies the operational label for
this run only. It does not establish when every historical provider observation
or adjusted-price vintage was available.

## Interpretation and unresolved rights

- Historical paths use the current adjusted-price vintage and may change after
  later provider revisions.
- This run is exploratory current-vintage analysis, not a point-in-time
  backtest.
- The configured present-day universe introduces survivorship bias.
- The local generated files are not automatically published or committed.
- Public redistribution of provider data and rights to display derived results
  remain separately unresolved.
- Flow and Concentration remain deferred.
- No composite, weights, thresholds, risk classes, missing-component
  reweighting, or Crowding Score is defined.
