# U.S. ETF Crowding & Overheating Risk Monitor

An early-stage quantitative-finance research project for monitoring potential
crowding and overheating across a curated set of U.S.-listed equity ETFs. A
local-only Streamlit viewer presents verified standalone price-signal research,
while reusable methodology and bundle validation remain in the tested Python
package. No public application has been approved or deployed.

## Research question

> Can ETF creation/redemption activity, momentum, concentration, and volatility
> be combined to identify crowded or potentially overheated areas of the U.S.
> equity ETF market?

This project is intended to be a risk-monitoring framework, not a
crash-prediction model. Elevated indicators would describe unusual conditions
that merit attention; they would not establish that a reversal is imminent.

## Why ETF crowding matters

ETFs can concentrate investor demand in the same securities, industries, or
themes. Strong creation activity, persistent price strength, concentrated
holdings, and changing volatility may together reveal areas where positioning
has become unusually one-sided. No single factor is conclusive, so the planned
monitor will keep the inputs visible and document their assumptions.

## Planned ETF universe

The initial universe contains 24 ETFs defined in the canonical packaged resource
[`src/etf_crowding/resources/etf_universe.yaml`](src/etf_crowding/resources/etf_universe.yaml).
The loader also accepts an explicit external configuration path for controlled
tests or user-supplied universes.

| Category | Tickers |
| --- | --- |
| Broad Market | SPY, QQQ, IWM, DIA, VTI |
| Technology / Growth | XLK, VGT, SMH, SOXX, IGV |
| Financials | XLF, KRE |
| Energy | XLE, XOP |
| Healthcare | XLV, XBI |
| Consumer | XLY, XLP |
| Industrials / Materials | XLI, XLB |
| Thematic | ARKK, TAN, ICLN, LIT |

This is a curated present-day research universe, not a reconstruction of every
ETF that was investable at each historical date.

## Factor methodology and status

The monitor's research design considers four interpretable factors:

1. **Flow:** a creation/redemption flow proxy when derived from changes in
   shares outstanding, clearly distinguished from reported fund flows.
2. **Momentum:** an implemented XNYS-aligned, per-ETF percentile of trailing
   252-session adjusted-price log returns.
3. **Concentration:** a deferred current-only measure whose preferred future
   raw metric is direct-long-equity economic-entity HHI.
4. **Volatility:** an implemented XNYS-aligned, per-ETF percentile of
   21-session annualized adjusted-price return dispersion.

Flow is deferred from both historical and current scores until a data source
passes the unchanged event-time acceptance specification. It will not be
represented by zero, a neutral component, stale Yahoo data, stitched issuer-page
observations, or automatically redistributed weights. No historical or current
composite has been finalized: inputs, weights, thresholds, interpretation, and
naming remain undecided. In particular, an indicator containing Momentum and
Volatility but no direct positioning or flow evidence will not be called a
Crowding Score merely because those components are available.

Concentration is deferred pending a production-eligible holdings and
economic-entity-mapping architecture. It remains current-only, and current
holdings will never be applied retroactively. Its preferred future raw
methodology is direct-long-equity economic-entity HHI, but no Concentration
calculation is implemented. Eligibility thresholds, freshness rules,
normalization, and cross-sectional percentiles remain unapproved. No composite,
component weights, thresholds, risk classes, or Crowding Score has been defined.

Definitions, alignment rules, and limitations are documented in
[`docs/methodology.md`](docs/methodology.md). The dated evidence behind the Flow
decision is recorded separately in
[`docs/flow-data-source-feasibility.md`](docs/flow-data-source-feasibility.md).
The official-source feasibility work supporting the Concentration deferral is
recorded in
[`docs/concentration-data-source-feasibility.md`](docs/concentration-data-source-feasibility.md).
The controlled 2026-08-25 price refresh and independent artifact audit are
recorded in
[`docs/price-signal-empirical-audit-2026-08-25.md`](docs/price-signal-empirical-audit-2026-08-25.md).

Historical daily price ingestion/persistence, historical shares-outstanding
ingestion/persistence, and the standalone Momentum and Volatility components
are implemented. This describes pipeline and calculation availability, not
empirical suitability of the current shares source for Flow scoring. The
creation/redemption flow proxy and Concentration component remain deferred.
Holdings ingestion, Concentration calculations, composite scores, and backtests
are not implemented. One dated current-vintage
Momentum/Volatility evaluation has been completed and independently audited;
it is exploratory evidence, not a point-in-time backtest or a composite result.
The local dashboard only reads a separately selected, fully verified evaluation
bundle and does not create a score or refresh data.

## Planned project architecture

```text
data/raw/               Source data preserved in its original form
data/processed/         Validated and transformed research datasets
data/snapshots/         Point-in-time snapshots when available
docs/                   Methodology, definitions, and limitations
notebooks/              Exploratory analysis only
scripts/                Command-line data and research workflows
src/etf_crowding/       Reusable code and packaged configuration resources
tests/                  Automated tests
app/                    Streamlit presentation layer
```

The Streamlit presentation layer calls the read-only consumer API in
`src/etf_crowding/`; financial logic is not duplicated in `app/`.

### Repository paths

Local scripts can resolve the repository's data and documentation directories
through `etf_crowding.paths`. In a verified source checkout, the module recognizes
the root from the expected `src/etf_crowding` layout and root `pyproject.toml`.
For installed deployments, set `ETF_CROWDING_PROJECT_ROOT` to an existing project
directory before requesting repository-only paths. The module raises a clear
error when neither condition is met; it never treats `site-packages` as a data
root and never creates directories automatically.

The packaged ETF universe is independent of repository path resolution and
continues to load through `importlib.resources` in installed distributions.

## Price-data pipeline

Day 2 adds an offline-tested historical daily price pipeline backed by
`yfinance` and Yahoo Finance. The command-line workflow loads all tickers through
the canonical packaged ETF-universe configuration, requests each ticker
independently, normalizes successful responses, validates the combined history,
and writes the canonical Parquet dataset to
`data/processed/etf_prices_daily.parquet`.

Run an update from the repository root after installing the project:

```text
.venv\Scripts\python.exe scripts\update_prices.py
```

The default date window starts on `2018-01-01` and uses the current
America/New_York calendar date as the exclusive end bound, matching the
documented `yfinance` interface. Therefore, the default request includes only
observations strictly before the current U.S. market calendar date. It does not
attempt to determine market-close status or consult a trading calendar. Explicit
end dates preserve the same exclusive semantics. Explicit bounds and a
controlled destination are also available:

```text
.venv\Scripts\python.exe scripts\update_prices.py --start 2024-01-01 --end 2024-02-01 --output data/processed/etf_prices_daily.parquet
```

Every usable normalized observation returned for a ticker must fall within the
requested inclusive-start, exclusive-end interval. A response containing any
out-of-window date is rejected for that ticker rather than silently trimmed.
Raw chart timestamps are converted using the response's exchange timezone, and
the resulting market dates are normalized without a timezone-induced date
shift. Any injected non-empty provider DataFrame must expose a pandas
`DatetimeIndex`; numeric or other malformed indexes are rejected instead of
being coerced into dates. Supplied `Open`, `High`, `Low`, `Close`, `Adj Close`,
and `Volume` columns must already have real numeric pandas dtypes before
normalization. Boolean, complex, string, object, datetime, timedelta,
categorical, and other non-real-numeric provider dtypes are rejected for that
ticker rather than coerced into plausible market values.
Provider exceptions are surfaced through yfinance's supported
`config.debug.hide_exceptions` setting so failures remain distinguishable from a
genuinely empty history. The pipeline restores the prior setting after each
request.

The provider boundary is currently inspected and tested specifically against
`yfinance==1.5.2`. Its public `Ticker.history()` output does not retain all
source missingness needed here, so a small isolated adapter uses the pinned
yfinance-owned uncached `Ticker._data.get` chart transport for every
source-vintage retrieval. This preserves yfinance's session, authentication,
cookie/crumb, retry, error, and rate-limit handling without introducing a
project HTTP client. The adapter deliberately bypasses yfinance's in-process
response cache because it provides neither a source retrieval timestamp nor a
TTL suitable for the latest-vintage policy. Repeated identical historical
requests may therefore make another provider request; this is an intentional
correctness-over-performance choice. For normal live ingestion, the project
captures `retrieved_at` immediately after each successful ticker response
returns and before canonical normalization. Tickers in one batch may therefore
have different timestamps. This remains a client-side retrieval timestamp, not
a Yahoo server-side revision timestamp. A timezone-aware explicit
`retrieved_at` is available as a deterministic caller override, but is not the
normal concurrent live-ingestion policy. This is a deliberate private-interface
dependency. Provider upgrades require source review and offline integration-test
updates before changing that exact pin.

Before assigning the requested ticker to raw chart values, the adapter requires
the scalar identifier at `chart.result[0].meta.symbol` and compares it with the
uppercase symbol convention used by yfinance 1.5.2. A missing, malformed, or
conflicting identifier rejects that ticker response; it is never silently
relabeled and no heuristic alias mapping is used.

### Canonical daily price schema

| Column | Definition |
| --- | --- |
| `date` | Timezone-naive, normalized market observation date |
| `ticker` | Configured ETF ticker |
| `open` | Yahoo/yfinance source historical daily open |
| `high` | Yahoo/yfinance source historical daily high |
| `low` | Yahoo/yfinance source historical daily low |
| `close` | Yahoo/yfinance source historical daily close |
| `adjusted_close` | Raw chart adjusted-close value when supplied for that observation |
| `volume` | Raw chart daily volume, preserving source null versus explicit zero |
| `retrieved_at` | UTC client-side timestamp captured after that ticker response returns |

Canonical column labels must be unique. Every canonical market column (`open`,
`high`, `low`, `close`, `adjusted_close`, and `volume`) must already use a
supported dense real numeric integer or floating pandas dtype, including
supported NumPy, nullable pandas, and PyArrow-backed representations. Pandas
SparseDtype and PyArrow decimal are currently unsupported. Boolean, complex,
string, object, datetime, timedelta, categorical, sparse, decimal, and other
unsupported dtypes are rejected before persistence; they are never converted
into plausible market values.

Raw JSON numeric arrays are materialized per field without DataFrame or NumPy
float inference. Integers, floats, and nulls are combined only when one supported
representation preserves every source value and missing state exactly. The
established normalized price output remains `float64`; each raw market Series
must round-trip exactly through that conversion. An integer such as
`9_007_199_254_740_993` is retained exactly at the raw boundary but rejected
before canonical assignment because binary `float64` cannot represent it
exactly. It is never silently rounded. Explicit zero and null remain distinct.

The adapter reads Yahoo chart quote arrays before yfinance's high-level history
transformations, so no `Adj Close / Close` automatic OHLC adjustment or back
adjustment is applied, preserving the Day 2 `auto_adjust=False` and
`back_adjust=False` methodology. These Yahoo/yfinance source historical OHLC
values must not be described as guaranteed nominal or raw historical tape
prices: Yahoo's source series may already reflect split-related historical
adjustments or later provider revisions.

yfinance 1.5.2's high-level history processing can synthesize `Adj Close` from
`Close` when the raw adjusted-close indicator is absent and can replace missing
volume with zero. Canonical missingness is therefore derived from the raw Yahoo
chart values, not solely from the post-processed history DataFrame.
`adjusted_close` is populated only when the raw adjusted-close indicator supplies
that observation; it is never fabricated from `close`. A raw volume null remains
missing, while an explicit raw zero remains zero. Future total-return or momentum
calculations should continue to use appropriately adjusted prices. Day 2 does
not implement those calculations.

Individual market fields may remain missing and are never forward-filled or
backfilled. A ticker/date row with all six market fields missing has no usable
market observation. Raw dated rows remain visible through normalization and are
rejected rather than filled, zeroed, or silently dropped. One ticker's empty
response or provider failure does not discard
successful tickers: the CLI reports a partial batch clearly and persists only
valid observations. If every ticker is empty or failed, the command exits with
an error and does not replace existing history.

The canonical Parquet file represents the latest successfully validated
Yahoo/yfinance source vintage available to the pipeline. For each persisted
ticker/date row, `retrieved_at` is the latest client-side retrieval time at
which the project observed the currently stored six market fields. It is not a
Yahoo publication or revision timestamp. A later identical observation keeps
all market values unchanged while advancing only this provenance watermark; it
is not a source-value revision. A newer whole-row source vintage may complete
previously missing fields or revise existing market values. The pipeline does
not infer whether a revision arose from a dividend, split, other corporate
action, Yahoo correction, or another provider change.

For a changed overlapping ticker/date row, the incoming `retrieved_at` must be
later than the canonical row's provenance watermark. An older incoming vintage
is rejected; different values with the same timestamp are also rejected as
inconsistent. An older or equal identical observation leaves the watermark
unchanged, so it can never move backward.

Before revised overlapping rows replace the canonical values, the exact
superseded canonical Parquet is preserved under `data/snapshots/prices/` with a
collision-safe UTC timestamp. The revision is reported by row count and affected
ticker. If snapshot preservation fails, the canonical file remains unchanged.
Advancing only `retrieved_at` for identical source values atomically rewrites the
canonical file but creates no revision snapshot because no financial value was
superseded.
An incoming overlap that loses a previously available market value is rejected
for manual review rather than erasing data or mixing fields from different
source vintages. Non-requested, empty, or failed tickers retain their existing
history. The production updater passes each ticker's success status, exact
inclusive-start/exclusive-end request window, returned dates, and retrieval
timestamp into the same persistence transaction. While holding the output lock,
the transaction compares only existing ticker/date rows inside each successful
window with that response's returned dates. If a previously stored in-window
date vanishes, the entire update is rejected for manual review, the canonical
file remains unchanged, no revision snapshot is created, and the affected keys
are reported. Vanished rows are not auto-deleted. Dates outside coverage and
history for empty, failed, or unrequested tickers remain untouched; weekends,
holidays, and other nonexistent calendar rows are never inferred. A
deterministic adjacent lock file serializes each canonical output's complete
existing-file read, validation, coverage comparison, merge, required snapshot,
and atomic replacement transaction. Lock acquisition times out clearly after
30 seconds; unrelated custom output paths use independent locks. The lock file
is only coordination metadata and is not a market dataset. Generated processed
data and snapshots are ignored by Git.

Market-data coverage, field availability, adjustment calculations, revisions,
rate limits, and service continuity depend on yfinance and Yahoo Finance. This
third-party dataset should not be treated as an exchange-authoritative record.

## Shares-outstanding data pipeline

Day 3 adds an offline-tested historical ETF shares-outstanding ingestion layer.
It records source observations only; it does not calculate fund flows or a
creation/redemption flow proxy. Run the updater from the repository root:

```text
.venv\Scripts\python.exe scripts\update_shares.py
```

The canonical output is
`data/processed/etf_shares_outstanding.parquet`. Optional `--start`, `--end`,
and `--output` arguments control provider query bounds and the destination. In
the inspected yfinance 1.5.2 implementation, an omitted start resolves to 548
days before its exchange-local end time. For reproducible multi-ticker batches,
the project captures one client-side reference instant when `--end` is omitted,
then converts that same instant independently to each ticker's exchange
timezone before constructing its bounds. A run crossing exchange-local midnight
therefore does not use a different wall-clock instant for later tickers. The
implementation sends `period1` and `period2` to the provider but does not
document their exact response inclusivity. Day 3 therefore does not label these
bounds as inclusive/exclusive and does not silently trim returned source
observations.

The inspected public interface is `Ticker.get_shares_full(start=None,
end=None)`. Its yfinance 1.5.2 source requests the Yahoo
fundamentals-timeseries `shares_out` field and constructs a pandas `Series`
indexed by exchange-local, timezone-aware timestamps. The detailed schema is
not documented by a method docstring and remains version-specific. To prevent a
cached old response from receiving a new retrieval timestamp, the project uses
the same pinned path through yfinance-owned uncached `Ticker._data.get`. This
retains yfinance session, cookie/crumb, configured retry, HTTP-error, and
rate-limit handling without adding a project HTTP client. The non-expiring
`cache_get` response cache is deliberately bypassed.

Before assigning the requested ticker, the raw parser requires the single
provider symbol in `timeseries.result[0].meta.symbol` and compares it with
yfinance 1.5.2's uppercase requested-symbol convention. Missing, malformed, or
conflicting identity metadata rejects the response without alias heuristics.

### Canonical shares-outstanding schema

| Column | Definition |
| --- | --- |
| `date` | Source observation date, normalized to timezone-naive midnight without converting the exchange-local date |
| `ticker` | Requested ETF ticker |
| `shares_outstanding` | Provider `shares_out` observation in shares |
| `retrieved_at` | UTC client-side timestamp captured after that ticker response returns |

The pipeline preserves only the dates returned by the source. It does not
assume the observations are daily, expand them onto the price or trading
calendar, infer weekend or holiday records, interpolate, forward-fill, or
backfill. Dated null `shares_out` values are retained because the inspected
yfinance method constructs its Series from timestamp/value pairs without
dropping nulls. A response with no dated history is reported as empty instead.

Provider shares values must already use real numeric pandas dtypes. Canonical
shares values must use a supported dense integer or floating dtype, including
supported NumPy, nullable pandas, and PyArrow-backed representations. Pandas
SparseDtype and PyArrow decimal are currently unsupported. Boolean, complex,
string, object, datetime, timedelta, and categorical dtypes are also rejected
rather than converted into plausible observations. Zero, negative, and infinite
present values are invalid. Missing values within a valid nullable integer or
floating Series remain missing. Canonical column labels must be unique, and
duplicate ticker/date values must be identical to deduplicate; conflicting
duplicates are invalid.

The raw shares parser constructs integer and floating source values separately
before applying the shared lossless numeric harmonization policy. This prevents
a mixed large-integer/float payload from passing through constructor inference.
Integer/null values preserve both the integer and missing state; exactly
representable integer/float combinations remain valid; incompatible combinations
raise a domain error before canonical normalization.

The canonical Parquet represents the latest successfully validated source
vintage observed by this project. For each persisted ticker/date row,
`retrieved_at` is the latest client-side retrieval time at which the project
observed the currently stored shares value; it is not a Yahoo publication or
revision timestamp. A later identical observation advances only that watermark,
without changing shares or creating a revision snapshot. Older or equal
identical observations cannot move it backward. A newer changed value or
missing-to-present completion replaces the whole row only after an exact
snapshot of the superseded canonical file is published under
`data/snapshots/shares/`. Stale revisions, different values with the same
`retrieved_at`, and present-to-missing value loss are rejected. An adjacent
per-output lock serializes the complete read, validation, merge, snapshot, and
atomic replacement transaction with a 30-second timeout. Partial batches retain
history for failed, empty, and unrequested tickers; an entirely empty or failed
batch cannot replace canonical history.

Shares observations intentionally remain sparse, and the inspected pinned
fundamentals-timeseries implementation does not establish exact response-bound
inclusivity. The price disappearance rule is therefore not copied to shares:
absence of a sparse shares date is not treated as proof that an observation
vanished inside confirmed coverage.

No live Yahoo request was used to implement or test Day 3. A separate read-only
audit on 2026-08-21 later found that the observed shares histories could not
support the approved Flow methodology across the configured universe. That
dated provider-feasibility result does not alter the implemented ingestion
contract and is documented in
[`docs/flow-data-source-feasibility.md`](docs/flow-data-source-feasibility.md).
The adapter and its offline contract tests remain pinned to yfinance 1.5.2 and
require renewed inspection on a provider upgrade.

## Momentum component

`etf_crowding.signals.calculate_momentum` calculates the approved standalone
Momentum percentile entirely in memory from canonical prices. It uses the
version-pinned `exchange-calendars==4.13.2` XNYS regular-session calendar, exact
`adjusted_close` endpoints 252 sessions apart, and eligible prior raw returns
from `d_{t-755}` through `d_{t-1}`. At least 252 prior observations are required,
and ties use the midrank empirical percentile.

Return arithmetic preserves exact integer endpoint differences before floating
conversion and uses a log-domain fallback for extreme valid endpoint ratios. If
the optional positive display percentage exceeds finite Float64 range, the raw
log return remains eligible while `simple_return_pct` remains missing and
`simple_return_status` reports `exceeds_float64_range`.

Missing endpoint rows or adjusted prices remain `NaN`; `close` is never used as
a fallback. Interior missingness is reported without shifting the endpoints.
The output records endpoint values, raw and display returns, display-return
status, reference count, first prospective-use session, endpoint status, and
exact interior-missingness diagnostics. It is not persisted and does not modify
canonical prices.

Momentum is labeled as of the current XNYS close and is first prospectively
usable on the next XNYS session. Its history uses the current canonical price
vintage and is exploratory rather than a point-in-time backtest. It is not a
Crowding Score, and it does not replace deferred Flow or define composite
weights, thresholds, or missing-component behavior.

Canonical dates inside a Momentum calculation scope must be XNYS sessions.
Before any future `exchange-calendars` upgrade is approved, the complete XNYS
session-label set from `2018-01-01` through the evaluation end must be compared
with the pinned version and every difference reviewed.

## Volatility component

`etf_crowding.signals.calculate_volatility` calculates the approved standalone
Volatility percentile entirely in memory from canonical prices. It uses exactly
21 adjacent-XNYS adjusted-close log returns, requiring the complete 22-price
chain ending on the signal date. Raw Volatility is their annualized sample
standard deviation with `ddof=1` and fixed `sqrt(252)` annualization; the
optional display value is expressed in percentage points.

Any missing canonical row or missing `adjusted_close` in the chain invalidates
the current raw result. The output records exact window dates, raw and display
Volatility, percentile, reference count, first prospective-use session, window
status, and exact missing-row and missing-adjusted-close diagnostics. Its
derived contract is validated before return. The calculation does not fill,
shift, mutate, or persist canonical prices and does not persist derived output.

Normalization is per ETF, uses eligible prior observations from `d_{t-755}`
through `d_{t-1}`, excludes the current observation, requires at least 252
eligible prior values, and uses the midrank empirical percentile. Volatility is
labeled as of the current XNYS close and is first prospectively usable on the
next XNYS session. Its history uses the current adjusted-price vintage and is
exploratory rather than a point-in-time backtest. It is not a Crowding Score and
does not replace deferred Flow or define a composite, weights, or thresholds.

## Standalone signal evaluation workflow

Day 8 adds an offline-first package workflow for evaluating the implemented
standalone Momentum and Volatility components across the configured 24 ETFs.
Reusable coverage, staleness, dependence, schema, and bundle logic lives in
`etf_crowding.analysis`; the command-line script is a thin adapter.

Offline mode is the default and requires an existing canonical price file:

```text
.venv\Scripts\python.exe scripts\evaluate_price_signals.py
```

If that file is absent, the command fails without creating a valid-looking
empty evaluation. A live Yahoo price refresh is possible only through the
explicit `--refresh` option and requires separate authorization before use:

```text
.venv\Scripts\python.exe scripts\evaluate_price_signals.py --refresh
```

The refresh path captures one UTC reference instant at nanosecond precision
before acquisition and resolves the latest XNYS session whose scheduled close is
not later than that instant. It requests history from `2018-01-01` through the
calendar day after that target as an exclusive end and reuses the existing
independent per-ticker acquisition and atomic canonical persistence APIs. It adds
no project-level retry and preserves validated partial batches. Provider
availability never moves the target backward: missing target inputs produce
missing current results, while older price, raw-signal, and normalized-signal
dates remain separately labeled stale diagnostics.

When acquisition statuses are present, the workflow validates their exact
ticker population, request bounds, outcome fields, returned dates, row counts,
and UTC retrieval timestamps against the canonical rows claimed by the batch.
Canonical and per-ticker retrieval timestamps retain nanosecond precision in
coverage artifacts and manifest metadata.

Each evaluation retains one coverage row for every configured ETF, including
empty, failed, and entirely absent tickers. The workflow calls the public
`calculate_momentum` and `calculate_volatility` APIs and retains their native
outputs. Pearson and Spearman correlations use only exact `(ticker,
signal_date)` pairs with both percentiles present. They are reported separately
by ETF and by session, require at least three nonconstant pairs, contain no
p-values or pooled estimate, and label session cross-sections as
`full_universe` only when all 24 ETFs are present.

Validated local run bundles are written under
`data/processed/signal_evaluations/<UTC run ID>/` without overwriting an existing
run. They contain immutable input prices, coverage, native Momentum, native
Volatility, descriptive dependence diagnostics, and a provenance manifest. Each
bundle is written in a sibling temporary directory, verified after Parquet
reload with explicit Arrow schemas and hashes, and published only after the
complete manifest passes validation. Before conversion, a holistic validator
reconciles the canonical input, native signals, coverage, and dependence without
repairing them. Git provenance is captured before any output path is created.
After the directory rename, every final-path artifact and the manifest are
reopened and revalidated; failures are quarantined rather than returned as valid
runs. Modifications after the function returns cannot be prevented, so consumers
must verify manifest hashes before use. Run bundles are generated local data;
they are not automatically published or committed.

Day 8.2 implemented and tested this workflow with synthetic data only. On
2026-08-25, Day 9 subsequently executed one separately authorized refresh and
independently audited the resulting current-vintage artifacts. The canonical
Parquet and run bundle are local Git-ignored data and are not part of the
repository; the tracked audit record is
[`docs/price-signal-empirical-audit-2026-08-25.md`](docs/price-signal-empirical-audit-2026-08-25.md).
Historical output uses that run's current adjusted-price vintage, not a
reconstruction of prior provider vintages. It is therefore exploratory, not a
point-in-time backtest, and the present-day configured universe retains
survivorship bias. Correlations are descriptive only and do not establish
causality, predictive value, crowding, or suitability for a composite.

### Local signal-bundle dashboard

Day 10 adds a local-only, read-only viewer for the existing six-file Day 9
signal-evaluation contract. It discovers direct-child run directory names but
starts with a blank selection and never silently selects the newest or an older
run. Before any financial value is displayed, the consumer snapshots the exact
six regular files, hashes them, parses those same bytes, and verifies the
manifest, schemas, row counts, configured universe hash and order, timing,
statuses, staleness, cross-artifact semantics, and dependence metadata.

Launch it from the repository root only when an existing local bundle is
already present:

```text
.venv\Scripts\python.exe -m streamlit run app\streamlit_app.py --server.address 127.0.0.1 --browser.gatherUsageStats false
```

The four sidebar views are Overview, ETF detail, Dependence diagnostics, and
Provenance and limitations. Momentum and Volatility always remain separate.
Missing and stale values remain visibly missing or stale with their source
status. Tables use static, non-exporting presentation and Plotly image export is
disabled; per-session dependence uses an explicit date filter and bounded static
rows. Provenance lists all six files, including the manifest's computed digest,
and states that the unsigned manifest is not internally self-authenticating. The
viewer has no provider access, refresh control, download/export, derived
persistence, shares, holdings, Flow, Concentration, composite, threshold, risk
class, or public-hosting path. Consumer verification is repeated against a new
six-file snapshot on every Streamlit rerun; a previously verified in-memory
object is reused only when the combined digest is unchanged. See
[`app/README.md`](app/README.md) for the operating contract.

## Concentration status

Day 7 approved methodology and completed official-source feasibility research;
it did not implement Concentration or retrieve holdings files. Concentration is
deferred until holdings and deterministic, versioned
security-to-issuer-to-parent mappings are production-eligible. Ticker and
company-name heuristics are not acceptable substitutes.

The preferred future raw metric is economic-entity HHI over eligible mapped
direct-long equity weights. HHI points, effective holdings, and top-10 weight
would be diagnostics of disclosed portfolio concentration. They would not
establish investor crowding, fund flows, positioning, liquidity stress,
valuation excess, overheating, crash risk, or future returns.

Cash, liabilities, collateral, derivatives, short positions, pooled funds, and
unknown instruments would remain outside the HHI numerator but visible in
reconciliation diagnostics. Missing, stale, partial, ambiguous, or unmappable
inputs would remain missing. The project has not approved universal numeric
coverage gates, a universal two-XNYS-session freshness rule, a cross-sectional
Concentration percentile, or a 22-ETF substitute for the configured universe.
See [`docs/methodology.md`](docs/methodology.md) for the locked methodology and
[`docs/concentration-data-source-feasibility.md`](docs/concentration-data-source-feasibility.md)
for the evidence boundary and unexecuted validation plan.

## Technology stack

- Python 3.12
- pandas, NumPy, SciPy, and statsmodels for data and statistical work
- exchange-calendars 4.13.2 for the pinned XNYS reference-session calendar
- yfinance 1.5.2 as the pinned historical price and shares-data interface
- PyYAML and PyArrow for configuration and data storage
- Plotly and Streamlit for the local verified-bundle viewer
- pytest, Ruff, and mypy for quality checks

## Current development status

Day 1 established the repository foundation and canonical ETF universe. Day 2
implements historical daily price ingestion. Day 3 implements historical
shares-outstanding ingestion, validation, incremental Parquet persistence, and
the command-line update workflow. Day 4 finalized the future Flow methodology,
audited source feasibility, and deferred Flow because no production-eligible
source exists. Subsequent phases implemented the standalone Momentum and
Volatility components and their audit diagnostics. Day 7 performed
Concentration methodology design and official-source feasibility research and
deferred implementation pending production-eligible holdings and
economic-entity mapping. Day 8 implements an offline-first standalone
Momentum/Volatility evaluation and reproducible local bundle workflow. Day 9
completed one controlled live price refresh and an independent read-only audit
of its local Git-ignored artifacts; the resulting analysis is current-vintage
and exploratory rather than a point-in-time backtest. Day 10 implements the
fail-closed local Streamlit viewer for an explicitly selected verified bundle.
Pipeline implementation must not be confused with source suitability. The
creation/redemption flow proxy, holdings ingestion, Concentration calculations,
composite scores, point-in-time backtests, production analysis case studies, and
public Streamlit deployment are not implemented.

## Data limitations

Limitations include survivorship bias from the present-day curated universe,
third-party price and shares-data availability and revisions, missing
observations, the empirically unsuitable Yahoo/yfinance shares coverage observed
on 2026-08-21, the difference between a future flow proxy and reported flows,
nonuniform issuer holdings contracts, unresolved economic-parent mapping and
display rights, unresolved public redistribution and derived-display rights,
and lack of historical point-in-time holdings. The Flow audit does not establish
the provider's undocumented internal cause. Missing data are not silently
invented or forward-filled. Each canonical price and shares row records a
client-side retrieval time, but Yahoo Finance does not provide an
exchange-authoritative publication timestamp for every historical observation.

## Installation

From the repository root, create a Python 3.12 virtual environment and install
the package with its development dependencies:

```text
python -m venv .venv
.venv\Scripts\activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

On macOS or Linux, activate the environment with
`source .venv/bin/activate`. Alternatively, `python -m pip install -r
requirements.txt` installs the same production and development dependency sets
without installing the local package.

## Testing commands

Run the configured quality gates from the repository root:

```text
ruff check .
ruff format --check .
mypy src/etf_crowding app
pytest
```

## Disclaimer

This project is for research and educational purposes only. It is not investment
advice, a recommendation to buy or sell any security, or a guarantee of future
results. Any eventual indicators will depend on data quality, methodological
choices, and assumptions that may fail in live markets.
