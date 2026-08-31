# Price and Signal Second-Vintage Operational Audit — 2026-08-31

## Scope and evidence boundary

This note records the controlled Day 11 price refresh, the second immutable
Momentum/Volatility evaluation bundle, the comparison with the Day 9 vintage,
and the two-run local dashboard acceptance completed on 2026-08-31. The audit
used the repository's public bundle consumer and read-only aggregate comparisons
of the canonical Parquet, its preserved pre-refresh snapshot, and both complete
six-file bundles. No provider was contacted and no refresh, signal calculation,
or persistence operation was run during this documentation pass.

The Markdown files are tracked repository documentation. The canonical market
data, snapshot, and evaluation bundles discussed below are local Git-ignored
artifacts and are not published by this note. Exact provider price values are
intentionally omitted. These results describe current-vintage revision behavior;
they are not trading, predictive, causal, or performance evidence.

## Controlled execution

The operational record contains exactly one refresh batch covering exactly the
24 configured tickers. There were zero project-level retries and no second
batch. The fixed execution identity was:

- evaluation instant: `2026-08-31T07:55:45.674456Z`;
- target XNYS session: `2026-08-28`;
- request interval: `[2018-01-01, 2026-08-29)`; and
- run ID: `20260831T075657153526Z`.

All 24 ticker acquisitions returned `success`, with 2,176 rows received for
each ticker. Here, acquisition success means that the historical response passed
the acquisition contract. It does not mean that every field on the target row,
or either target-session signal, is available.

## Canonical and snapshot outcome

The refreshed canonical price population contains 52,224 rows for 24 ETFs from
2018-01-02 through 2026-08-28. Relative to the preserved prior canonical
population, 96 `(date, ticker)` keys were added and zero keys were removed.

The sole price snapshot is
`data/snapshots/prices/etf_prices_daily_20260831T075636968830Z.parquet`. Its
SHA-256 is
`65d3929b04755bcd6dd400e70fd99ba3d2cbdd3435f70ba79e34764ca6f9b0e9`
and its byte size is 1,863,762. Those values exactly match the prior canonical
file recorded in the Day 9 audit, establishing a byte-for-byte preserved
pre-refresh canonical snapshot. The refresh completed normally, so no rollback
path was needed.

## Historical-vintage revisions

The prior and current canonical vintages share 52,128 identical keys. On those
keys, 31,241 rows have at least one changed market field:

| Field | Changed overlap rows |
| --- | ---: |
| `open` | 0 |
| `high` | 0 |
| `low` | 0 |
| `close` | 0 |
| `adjusted_close` | 31,234 |
| `volume` | 7 |

The adjusted-close revisions occur across all 24 ETFs. They are very small,
mixed-sign, float32-quantized differences. This pattern is consistent with
provider-side recomputation or rounding, but the audit does not prove that
cause. All seven volume revisions occur on 2026-08-24. They are consistent with
late source finalization, but that explanation also remains inference rather
than a proven source-side cause.

## Signal-version effects

Across the 52,128 signal keys shared by both vintages:

- Momentum raw values changed on 36,769 keys, while eight Momentum percentile
  values changed.
- Volatility raw values changed on 44,685 keys, while 90 Volatility percentile
  values changed.
- Eligibility flags, component statuses, normalization reference counts, and
  missing-row and missing-adjusted-close diagnostics did not change on the
  identical keys.

These are current-vintage revision diagnostics. They do not demonstrate a
trading result, predictive value, causal effect, or performance outcome.

## Target-session missingness

All 24 target rows for 2026-08-28 are present and contain `open`, `high`, `low`,
and `volume`. All 24 lack `close` and `adjusted_close`. Consequently, all target
Momentum and Volatility raw values, displayed percentages, and percentiles are
missing. Momentum reports `missing_end_adjusted_close` for every ETF, and
Volatility reports `missing_adjusted_close` for every ETF.

The latest eligible date remains 2026-08-27 for all 24 ETFs. Each of the five
coverage measures—price, Momentum raw, Momentum normalized, Volatility raw, and
Volatility normalized—reports one XNYS session of staleness. The workflow did
not move the declared target backward, substitute the last eligible observation,
fill the absent prices, or manufacture neutral values.

At the target session, the dependence diagnostic has zero exact Momentum/
Volatility pairs. Both estimators report `incomplete_universe` and
`insufficient_pairs`, with missing Pearson and Spearman estimates rather than a
substituted result.

## Bundle verification

The public consumer independently loaded and fully verified all six artifacts
in each bundle. Inventory contained exactly the two direct-child run directories
listed below and no temporary, partial, invalid, rejected, or quarantined bundle
directory. Candidate directory discovery was not treated as verification.

| Run ID | Target | Captured UTC instant | Combined content digest |
| --- | --- | --- | --- |
| `20260825T132408764517Z` | 2026-08-24 | `2026-08-25T13:18:23.141747Z` | `b14306f7576ad44731c2a1ef30e84e4bf7b7079c612e00a764a1e53d6e4280fa` |
| `20260831T075657153526Z` | 2026-08-28 | `2026-08-31T07:55:45.674456Z` | `ecfb43756ae319757fee4422ae31570e8a8a8fe7073e16abb25269d54c11dc8d` |

The Day 9 bundle records evaluation Git commit
`2d31927c3e0caee2ab088497c3d69f0223834578`; the Day 11 bundle records
`26aa1f5ec1fb3071e2583cb4178896891b22524a`. Both record configured-universe
SHA-256
`3d740d4cdb2a387acfc22e6bc51499aa19e9c3d932afd9d28caba7efebb02705`.

| Run ID | Artifact | Rows | SHA-256 |
| --- | --- | ---: | --- |
| `20260825T132408764517Z` | `input_prices.parquet` | 52,128 | `5349c5656e5cc8e1e83fb0c9ac8bdd53014e167904a7e6d1e1019953c69ce019` |
| `20260825T132408764517Z` | `coverage.parquet` | 24 | `74ea1cc71cfd80b79c32539996bfe299c09319f376e17b5f134d70802a7e1aa1` |
| `20260825T132408764517Z` | `momentum.parquet` | 52,128 | `e86153e303048a2cf2524b37255677dd60c46f4ed49973bc2edb3f559da187d2` |
| `20260825T132408764517Z` | `volatility.parquet` | 52,128 | `434b466ade6944fb996f4414e2c6d1554f0d3a050a416e5a17fffabc25131a4d` |
| `20260825T132408764517Z` | `dependence.parquet` | 4,392 | `d4bff85ded9621618797cbd1c1b4e0709d4a9b6824c33c12484b980beb019e91` |
| `20260825T132408764517Z` | `manifest.json` | — | `8184fd7f7af24811110c4d2c1670920bc3a62d5f6c3b231fb3d95bac8fc4e29d` |
| `20260831T075657153526Z` | `input_prices.parquet` | 52,224 | `da56097e0bd842e0734c6d94235569dadc0ccbd21f528235d1da0864b2ccc647` |
| `20260831T075657153526Z` | `coverage.parquet` | 24 | `5d66fa826f29cb0a47208791d87a2a3f4c1b05f41efaa9759db25068899e0f0a` |
| `20260831T075657153526Z` | `momentum.parquet` | 52,224 | `b314bfe5bfc387e67f9987d7c2a26c5f8cccfe6c69c1ca7c8d04b0514eb01eef` |
| `20260831T075657153526Z` | `volatility.parquet` | 52,224 | `8efb965e7de85e581c045b384266e809838f09c191ef30d5715b57b1bfef5c9d` |
| `20260831T075657153526Z` | `dependence.parquet` | 4,400 | `f604df0b5d54e43a8de7f01b8cd553b63a3ffc09e5eb803a897ced2b8d3dd5f6` |
| `20260831T075657153526Z` | `manifest.json` | — | `ac2e806afc898e0b06913e8b1bea9f3a16ed098de62ac87848c9b1e81f1d1c69` |

Each run contributes exactly six artifact rows to the table, including
`manifest.json`. Row counts apply to the Parquet artifacts; the manifest is a
JSON provenance record rather than a tabular result.

## Two-run Browser acceptance

A read-only real-Browser acceptance exercised both verified bundles. The run
selector started blank, neither run was selected automatically, and no financial
content appeared before selection. Discovery presented the two candidates
newest first without implying that either had passed six-file verification.

After explicit selection, the Day 9 bundle showed its complete 2026-08-24 target
state with zero staleness, and the Day 11 bundle showed its valid-but-missing
2026-08-28 target state with the exact reasons and one-session staleness described
above. Chart gaps stopped at the 2026-08-27 eligible values rather than carrying
them to the target. Target-session dependence remained missing with the correct
pair counts and statuses.

Digest-scoped state isolation restored the Day 9 SOXX/date selection and the Day
11 IGV/date selection across four cross-run transitions. No label, value, date,
chart, target identity, or provenance from one bundle appeared under the other,
and no newest-run fallback occurred.

Keyboard operation covered Overview, ETF detail, Dependence diagnostics, and
Provenance and limitations. Heading permalinks and cross-view fragment clearing
preserved the pathname and test query string; same-view widget changes did not
clear the fragment. The dashboard also passed 1280×720 and 800×900 inspection,
responsive identity display, deterministic headings, contrast, separate
Momentum and Volatility presentation, solid/dashed dependence styles, tabular
alternatives, full provenance, and missing-reason display. No CSV/download or
Plotly image-export control was present. No rerun/reload loop, blank page,
iframe, accumulated hidden component, extra focus target, visible Streamlit
exception, or Browser-console error was observed. No actionable P1, P2, or P3
dashboard defect remained.

For each run, the provenance view showed exactly six artifact rows including
`manifest.json`, expected and computed hashes, schemas, row counts, the universe
hash, evaluation Git commit, target and request timing, and the unsigned-manifest
limitation. Flow and Concentration remained explicitly deferred; Momentum and
Volatility remained separate, with no composite or export surface.

## Limitations

- Raw provider response bytes and complete `returned_dates` were not retained.
- No authenticated acquisition identifier exists.
- The exact source-side cause of the missing final `close` and
  `adjusted_close` elements cannot be proven.
- The manifest is unsigned and is not externally self-authenticating.
- A residual local time-of-check/time-of-use risk remains after verification.
- Historical results use current adjusted-price vintages and are not
  point-in-time backtests.
- Present-day-universe survivorship bias remains.
- Redistribution and public-display rights remain unresolved.
- Flow and Concentration remain explicitly deferred.
- No composite, component weights, thresholds, risk classes,
  missing-component reweighting, or Crowding Score exists.
