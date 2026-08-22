# Methodology

## Scope and status

This document describes the implemented price and shares-outstanding data
foundations and intended research design for the U.S. ETF Crowding & Overheating
Risk Monitor. No flow proxy, empirical factor calculations, scores, thresholds,
backtests, or conclusions exist at this stage. Exact definitions, units,
transformations, date alignment, and missing-data rules will be specified and
tested before any factor is presented as a result.

Flow is deferred from both historical and current scores because the current
shares source did not pass the approved event-time acceptance specification.
This is a source-feasibility decision, not a change to that future specification
or to the implemented shares-ingestion contract. The dated empirical evidence
is recorded in [`flow-data-source-feasibility.md`](flow-data-source-feasibility.md).

The framework is intended to monitor unusual combinations of risk indicators.
It is not intended to predict crashes or provide a trading signal.

## Historical daily price data

### Source and request policy

Daily ETF price history is requested through `yfinance` from Yahoo Finance. The
pipeline reads daily raw chart quote and adjusted-close indicator arrays before
yfinance's high-level history transformations. It applies neither automatic
`Adj Close / Close` OHLC adjustment nor back adjustment, preserving the Day 2
`auto_adjust=False` and `back_adjust=False` methodology. Each ticker is requested
separately so an empty response or provider error can be identified without
discarding valid results for other ETFs.

The default request starts on `2018-01-01`. The start date is inclusive and the
end date is exclusive under the documented yfinance interface. The default end
is the current calendar date in America/New_York, so default downloads include
only observations strictly before the current U.S. market calendar date. This
rule does not attempt to determine whether the market has closed and does not
require a trading calendar. An explicit end date retains the same exclusive
semantics. Provider coverage may start later for a particular fund and may
contain gaps.

After normalization, every usable observation for a ticker must satisfy
`date >= start` and `date < end`. If any date falls outside that interval, the
entire ticker response is classified as failed rather than filtered. Non-empty
yfinance-style provider frames must use a pandas `DatetimeIndex`; numeric and
other non-datetime indexes are rejected without generic date coercion. Raw chart
timestamps are converted using the response's exchange timezone before the same
date normalization and interval validation. Before numeric casting, every
supplied `Open`, `High`, `Low`, `Close`, `Adj Close`, and `Volume` Series must
already use a real numeric pandas dtype. Boolean, complex, string, object,
datetime, timedelta, categorical, and other non-real-numeric dtypes fail that
ticker; numeric-looking values are not coerced across this provider boundary.
Provider exceptions are exposed through yfinance's supported exception-visibility
configuration, which is restored after each sequential ticker request, so a
provider or network failure remains distinct from a genuine empty history.
The integration is currently validated specifically against `yfinance==1.5.2`;
its public `Ticker.history()` result does not expose enough information to
recover source missingness. The isolated provider adapter therefore uses the
pinned private uncached `Ticker._data.get` chart transport and ticker-timezone
lookup for every source-vintage retrieval. This retains yfinance's chart
URL/date conversion plus session, authentication, cookie/crumb, retry, error,
and rate-limit handling without a project HTTP client. The adapter bypasses
yfinance's in-process response cache because the cached response has no source
retrieval timestamp or TTL suitable for determining the latest provider
vintage. Repeating identical historical bounds may therefore incur another
provider request; financial provenance correctness takes precedence over this
cache optimization. In normal live ingestion, `retrieved_at` is captured
immediately after each successful ticker response returns and before canonical
normalization. Tickers in the same batch can therefore have different
timestamps. It is a project client-side retrieval timestamp, not a Yahoo
server-side revision timestamp. A timezone-aware explicit `retrieved_at`
remains available as a deterministic caller override, not as the normal
concurrent live-ingestion policy. Provider upgrades require renewed source
review and offline tests before the exact pin changes.

The raw chart result supplies provider identity as the scalar
`chart.result[0].meta.symbol`. The adapter validates that identifier against the
uppercase requested-symbol convention in yfinance 1.5.2 before assigning the
requested ticker to any values. Missing, malformed, or conflicting identity is
a failed ticker response. No heuristic alias mapping is applied.

### Canonical fields and units

The processed dataset contains at most one row per `ticker` and `date` with
these fields:

| Field | Unit and interpretation |
| --- | --- |
| `date` | Timezone-naive market observation date, normalized to midnight |
| `ticker` | ETF ticker from the configured research universe |
| `open` | Yahoo/yfinance source historical open in quoted-currency units |
| `high` | Yahoo/yfinance source historical high in quoted-currency units |
| `low` | Yahoo/yfinance source historical low in quoted-currency units |
| `close` | Yahoo/yfinance source historical close in quoted-currency units |
| `adjusted_close` | Raw chart adjusted-close value in quoted-currency units when supplied for that observation |
| `volume` | Raw chart daily volume in shares, preserving null versus explicit zero |
| `retrieved_at` | UTC client-side timestamp captured after that ticker response returns |

Timezone metadata is removed from daily provider timestamps without converting
the timestamp to another timezone. This preserves the provider's market
observation date and avoids an unintended calendar-day shift. Output is sorted
deterministically by ticker and date.

### Source OHLC and adjusted close

The `open`, `high`, `low`, and `close` fields are Yahoo/yfinance source
historical OHLC taken from the raw quote arrays. The pipeline does not apply
yfinance's `Adj Close / Close` automatic adjustment or back adjustment. This does
not establish that the source OHLC are nominal, originally observed historical
tape prices. Yahoo's historical source series may already reflect split-related
historical adjustments or later provider revisions.

yfinance 1.5.2's high-level history processing can initialize `Adj Close` from
`Close` when the raw adjusted-close indicator is absent and can fill missing
volume with zero. Canonical missingness is therefore taken from the raw chart
arrays instead of inferred from the processed history DataFrame or numeric
equality. `adjusted_close` is present only when the raw adjusted-close indicator
contains a value for that observation; it is never fabricated from `close`. A
raw volume null remains missing, while an explicit raw zero remains zero. Future
total-return and momentum calculations should continue to use appropriately
adjusted prices where the adjustment methodology fits the calculation. No
return, momentum, volatility, or other signal is calculated in the Day 2
pipeline.

### Validation, missing data, and revisions

Present prices must be finite and positive, present volume must be finite and
non-negative, and available OHLC values must satisfy basic range relationships.
Canonical column labels must be unique, and all six market columns must use
supported dense real numeric integer or floating pandas dtypes from NumPy,
nullable pandas, or PyArrow-backed representations. Pandas SparseDtype and
PyArrow decimal are currently unsupported. Boolean, complex, string, object,
datetime, timedelta, categorical, sparse, decimal, and other unsupported dtypes
are invalid before Parquet serialization. Values are not inspected or coerced
to rescue an unsupported dtype.
Raw integers, floats, and nulls are first materialized per field without lossy
DataFrame or NumPy inference. A mixed representation is accepted only if every
value and missing state has an exact common numeric representation. The
normalized price contract remains `float64`, so every source value must also
round-trip exactly through `float64`; an inexact integer above `2**53` is
rejected before canonical assignment rather than rounded. Explicit zero remains
distinct from missing.
Individual missing market fields are preserved; observations are never
forward-filled, backfilled, or invented. Every ticker/date row must contain at
least one of `open`, `high`, `low`, `close`, `adjusted_close`, or `volume`. A row
with all six fields missing remains visible through normalization, is invalid,
and cannot enter canonical history. A response with no dated observations
remains a genuine empty response rather than a validation failure.

The canonical `data/processed/etf_prices_daily.parquet` file represents the
latest successfully validated Yahoo/yfinance source vintage available to this
pipeline. For each persisted ticker/date row, `retrieved_at` is the latest
client-side retrieval time at which the project observed the currently stored
six market fields. It is not a Yahoo publication or revision timestamp. Exact
duplicate observations within one incoming dataset retain the row with the
latest retrieval time; conflicting duplicates remain invalid. Incremental
persistence compares each validated incoming row with the existing row for the
same ticker/date:

- Identical market fields retain the existing financial values. A later
  incoming `retrieved_at` advances only the canonical provenance watermark; an
  older or equal timestamp leaves it unchanged. This is not a source-value
  revision.
- If incoming data complete previously missing market fields without losing any
  existing values, or if market values differ, the entire validated incoming
  row becomes the latest source vintage only when its row-level `retrieved_at`
  is later than the existing row. The pipeline records only that a source-vintage
  revision occurred; it does not infer a dividend, split, other corporate
  action, Yahoo correction, or other provider cause from price fields alone.
- A changed overlap with an older `retrieved_at` is rejected as stale. Different
  market values with the same `retrieved_at` are rejected as inconsistent
  observations claiming the same source vintage. Either rejection occurs before
  a snapshot or canonical write.
- If an incoming overlap loses any previously available market field, the update
  fails for manual review. Fields from different row vintages are never silently
  combined.

Before any changed overlapping rows supersede the canonical file, its exact
previous Parquet representation is atomically preserved under
`data/snapshots/prices/` using a collision-safe UTC timestamp. Snapshots are not
overwritten. Failure to preserve the snapshot aborts before the canonical file
changes. Accepted revisions are surfaced by revised-row count and affected
ticker, and revised canonical rows use the incoming response's `retrieved_at`.
Provenance-only watermark advancement atomically rewrites the canonical file but
does not create a snapshot because no market value is superseded. The validated
latest-vintage canonical write remains atomic. An adjacent cross-process file
lock, scoped to the canonical output path, covers the entire existing-file read,
validation, merge, snapshot, and replacement transaction. Acquisition fails
after a 30-second timeout rather than waiting indefinitely. The persistent lock
file is coordination metadata, not financial data; custom output paths do not
share a global lock.

The batch records successful, empty, and failed tickers separately. A partial
batch may persist valid ticker results only after the CLI reports that it is
partial; existing history for failed, empty, or otherwise unrequested tickers is
not deleted. An entirely empty or failed batch cannot replace an existing
canonical file.

The production updater carries the same batch's structured ticker outcomes into
persistence: status, inclusive-start/exclusive-end price request bounds, exact
returned dates, row count, and retrieval timestamp. Those fields must match the
incoming canonical rows exactly. While holding the output transaction lock and
after loading the current canonical file, persistence compares only existing
ticker/date keys inside each successful ticker's confirmed request window with
that response's returned dates. If any such existing key is absent, the update
is rejected for manual review, all affected keys are reported deterministically,
the canonical bytes remain unchanged, and no revision snapshot is created. The
row is not auto-deleted. Existing dates outside coverage and all history for
failed, empty, or unrequested tickers remain preserved. The check never invents
weekend, holiday, or arbitrary calendar observations.

## Historical shares outstanding

### Source and verified interface

Day 3 uses the public `Ticker.get_shares_full(start=None, end=None)` interface
found in the installed `yfinance==1.5.2` source. That implementation requests
the Yahoo fundamentals-timeseries path for its `shares_out` field and returns a
pandas `Series`. Its index is formed by parsing source timestamps and localizing
them to the ticker's exchange timezone. The method has no detailed docstring, so
the endpoint schema and parsing behavior are treated as private,
version-specific implementation details rather than a stable provider promise.

The high-level implementation calls yfinance's non-expiring `cache_get` and can
return `None` for both absent shares history and some suppressed request errors.
Those behaviors do not provide sufficient source-vintage or failure provenance.
The isolated Day 3 adapter therefore mirrors the inspected yfinance 1.5.2
fundamentals-timeseries request and parser while calling the yfinance-owned
uncached `Ticker._data.get` transport. This keeps yfinance responsible for its
session, authentication, cookie/crumb, configured retries, HTTP behavior, and
rate-limit errors. No independent project HTTP client or retry loop is used.

The fundamentals-timeseries result supplies identity in the single-symbol
collection at `timeseries.result[0].meta.symbol`. The raw parser validates that
identifier against yfinance 1.5.2's uppercase requested-symbol convention before
assigning the requested ticker. Missing, malformed, or conflicting identity
metadata fails the ticker response; no alias heuristic is used.

Tests use injected Series and raw payload fixtures only. No live Yahoo request
was made while implementing or testing Day 3. A separate read-only audit on
2026-08-21 subsequently evaluated source feasibility and found the observed
shares histories unsuitable for the approved Flow methodology across the
configured universe. That result does not alter the raw-data contract described
here and does not establish Yahoo's undocumented internal cause. Provider
upgrades require source inspection and contract-test review before the yfinance
pin or adapter changes.

### Query bounds and observation dates

The inspected yfinance implementation accepts optional `start` and `end`
arguments. The project preserves its inspected construction while resolving an
omitted end from one client-side batch reference instant, captured before any
ticker request. That same instant is converted independently into each ticker's
exchange timezone. This prevents request duration, ordering, or an intervening
exchange-local midnight from changing later tickers' wall-clock reference. When
`start` is omitted it remains 548 days before that ticker's resolved end. The
adapter floors the resolved start day, ceils the end day, and sends both as
`period1` and `period2`. The implementation neither documents exact
inclusive/exclusive response semantics nor filters the returned Series to a
locally enforced interval. Accordingly, Day 3 describes these values only as
provider query bounds, not as a canonical `[start, end)` rule, and does not
silently trim an observation that the provider returns.

The canonical `date` is the source observation date represented by yfinance's
exchange-local index. Day 3 removes the timezone without converting the instant
to another timezone, then normalizes the local calendar date to midnight. This
prevents an unintended calendar shift while preserving the installed
implementation's date interpretation.

The source Series is not assumed to be genuinely daily. Only returned source
dates are stored. Day 3 does not expand sparse observations onto trading days,
align them to ETF price history, create weekend or holiday records, interpolate,
forward-fill, or backfill. Any later creation/redemption flow-proxy methodology
must define its observation alignment independently and point in time.

### Canonical schema and validation

The processed file is
`data/processed/etf_shares_outstanding.parquet` with exactly these fields:

| Field | Unit and interpretation |
| --- | --- |
| `date` | Timezone-naive normalized source observation date |
| `ticker` | Requested ETF ticker |
| `shares_outstanding` | Provider `shares_out` observation, in shares |
| `retrieved_at` | UTC client-side retrieval timestamp captured after that ticker response returns |

`retrieved_at` is this project's observation time, not a Yahoo publication or
revision timestamp. Normal live retrieval timestamps each returned ticker
response separately. An explicit timezone-aware override exists for
deterministic offline use and is not the normal concurrent ingestion policy.

The installed yfinance method builds its Series from all paired timestamps and
`shares_out` values without dropping null values. Day 3 therefore treats a dated
null as meaningful source missingness and retains that row. A response with no
dated Series is instead classified as genuinely empty. Missing shares remain
missing even if every dated row in a ticker response is null; they are never
converted to zero or filled.

The provider boundary requires a real numeric pandas dtype for shares. The
canonical validator supports dense integer and floating dtypes from NumPy,
pandas nullable, and PyArrow-backed representations; pandas SparseDtype and
PyArrow decimal are currently unsupported. Boolean, complex, string, object,
datetime, timedelta, categorical, and other unsupported dtypes are rejected
before conversion or Parquet serialization. Present shares values must be
finite and strictly positive. Missing values within supported nullable integer
or floating dtypes are valid. Column labels must be unique, tickers nonblank,
dates normalized and valid, and retrieval timestamps timezone-aware UTC. Exact
duplicate ticker/date observations with the same shares value retain the row
with the latest retrieval timestamp deterministically; conflicting duplicate
values are invalid.

Raw Python integers and floats are materialized separately before the shared
lossless numeric harmonization policy combines them. This prevents pandas
constructor inference from converting a large integer through an inexact
binary float. Integer/null inputs retain the exact integer and missing state;
mixed values are accepted only when exact round-trips prove that one supported
representation preserves every input.

### Source vintages and persistence

The canonical file represents the latest successfully validated shares source
vintage observed by this project. For each persisted ticker/date row,
`retrieved_at` is the latest client-side retrieval time at which the project
observed the currently stored shares value; it remains neither a Yahoo
publication timestamp nor a Yahoo revision timestamp:

- Identical overlapping shares values retain the existing source value. A later
  incoming `retrieved_at` advances only the canonical provenance watermark; an
  older or equal timestamp leaves it unchanged.
- A changed shares value or missing-to-present completion replaces the entire
  row only when the incoming `retrieved_at` is later.
- A changed older vintage is rejected as stale. Different values with the same
  `retrieved_at` are rejected as inconsistent.
- An incoming missing value cannot erase a previously present shares value.
  That value loss is rejected for manual review rather than silently accepted.
- Failed, empty, and unrequested tickers retain their prior canonical history.

Before an accepted revision changes the canonical file, the exact superseded
Parquet is atomically published under `data/snapshots/shares/` with a
collision-safe UTC name. Snapshot failure leaves canonical history unchanged.
Provenance-only watermark advancement atomically rewrites the canonical file but
does not create a snapshot because no shares value is superseded.
An adjacent cross-process lock scoped to each output path serializes the entire
existing-file read, validation, merge, snapshot, and atomic replacement
transaction. Lock acquisition fails clearly after 30 seconds. Custom outputs
use independent locks.

No price-style disappearance rule is applied to shares. Shares observations are
intentionally sparse, and the inspected yfinance 1.5.2 implementation does not
establish exact fundamentals-timeseries response inclusivity. A missing sparse
date therefore is not sufficient evidence that a previously stored shares
observation vanished inside confirmed coverage.

Shares outstanding are the provider's count of ETF shares represented by this
source. They are not trading volume, assets under management, NAV, fund flow, or
creation/redemption activity. Day 3 calculates no creation/redemption flow
proxy. A later methodology may estimate such a proxy from changes in shares,
but must define the formula, units, timing, sparse-date alignment, and
limitations separately.

## Planned live factors

### 1. Flow

The flow factor is intended to measure unusual ETF creation and redemption
activity. When it is inferred from changes in shares outstanding, it will be
called a **creation/redemption flow proxy**, not an exact or reported fund flow.
The following is the approved future acceptance specification. It is not
implemented and the project currently has no production-eligible Flow data
source.

#### Interval construction and eligibility

For consecutive canonical nonmissing shares observations
`(d_{i-1}, S_{i-1})` and `(d_i, S_i)`, with no intervening dated null:

- `delta_shares_i = S_i - S_{i-1}`, measured in ETF shares.
- `interval_share_change_i = (S_i - S_{i-1}) / S_{i-1}`, a dimensionless
  signed interval percentage.
- `n_i` is the number of canonical price dates in `(d_{i-1}, d_i]`.
- `raw_flow_rate_i = log(S_i / S_{i-1}) / n_i`, a signed log-share change per
  canonical price observation.

An interval is eligible only when `1 <= n_i <= 5`. An unchanged present shares
observation is a valid zero interval: `delta_shares_i`,
`interval_share_change_i`, and `raw_flow_rate_i` are zero. It is neither missing
nor a neutral-filled replacement.

A dated null breaks the consecutive-observation chain. The methodology does not
interpolate or forward-fill shares, expand shares observations to the daily
price calendar, or bridge from a nonmissing observation before the null to one
after it.

#### Signal timing and dollar diagnostic

Assign an eligible interval observation to the first canonical price date
strictly after `d_i`. This conservative timing rule prevents same-date use of
the ending shares observation. It does not make current-vintage history
point-in-time backtestable because the source does not establish when each
historical shares value first became available.

The optional dollar diagnostic is `delta_shares_i * close`, using the last valid
source `close` on or before `d_i` when it is no more than four calendar days
old. This is quoted-currency notional, not reported fund flow or NAV flow, and
it is not the normalized Flow input. The diagnostic uses source `close`, not
`adjusted_close`.

#### Per-ETF normalization

Normalize separately for each ETF. For a current eligible raw-flow-rate
observation `R_i`, its reference population contains only eligible prior
raw-flow-rate observations within the trailing 756 canonical price dates as of
the signal date. The current observation is excluded, and at least 252 eligible
prior observations are required.

With reference-population size `N`, use the midrank empirical percentile:

```text
P_i = (count(R_j < R_i) + 0.5 * count(R_j = R_i)) / N
Flow_i = 100 * P_i
```

Higher Flow means stronger creation pressure and therefore higher overheating
risk. Redemptions may be displayed separately as a diagnostic; Flow is not
converted into a two-sided extremeness score.

An invalid or ineligible interval, or an insufficient or invalid normalization
reference, produces `NaN`. The methodology does not substitute zero or
neutrality, use stale observations, or redistribute component weights. No
winsorization is applied.

#### Temporary suspected-corporate-action quarantine

For every shares interval between consecutive present observations, define
`share_ratio = S_i / S_{i-1}`. The common split-factor set is
`{1.25, 1.5, 2, 3, 4, 5, 10}` plus the reciprocal of every factor in that set.
A common-factor match occurs for a factor when:

```text
abs(log(share_ratio / factor)) <= log(1.05)
```

The interval is independently large when `share_ratio >= 1.5` or
`share_ratio <= 2/3`. An interval is a suspected split or reverse-split
candidate when it has either a common-factor match or an independently large
share ratio.

Price corroboration uses the last valid source `close` on or before each shares
endpoint. Each close must be no more than four calendar days older than its
corresponding shares date. When both endpoint closes qualify, define:

```text
price_ratio = end_close / start_close
notional_ratio = share_ratio * price_ratio
```

Notional continuity occurs when
`abs(log(notional_ratio)) <= log(1.10)`. Price continuity may label a
price-confirmed split candidate, but missing or inconsistent price evidence
must not clear a share-ratio candidate because the provider's historical
`close` adjustment semantics are not authoritative for this purpose.

Quarantine exactly the single shares interval crossing the suspected action.
Exclude that interval from Flow, normalization reference populations,
eligible-observation counts, and earliest-signal feasibility calculations.
Preserve its raw shares values, matched factor, tolerance results, endpoint
closes and their ages, and quarantine reason code for review. Do not infer or
apply an adjustment, widen the quarantine to adjacent intervals, or winsorize
the interval automatically.

#### Historical scope and current status

Use an explicit shares-history start of `2018-01-01`. The 548-day omitted-start
behavior in the pinned yfinance implementation is a provider query default, not
a methodology lookback, and is insufficient for the approved 756-price-date
normalization window and 252-prior-observation minimum.

Historical results remain exploratory current-vintage history, not a
point-in-time backtest. Flow is deferred from both historical and current scores
until a source passes this specification. Its absence must remain missing rather
than being represented by zero, a neutral component, stale Yahoo data, stitched
issuer-page observations, or automatically redistributed component weights.

### 2. Momentum

The momentum factor is intended to describe the strength or persistence of
recent price performance. Total-return calculations will use adjusted prices
when appropriate. Any use of source OHLC instead will require an explicit reason
and must account for the provider semantics described above. The lookback
horizon, return definition, minimum-history requirement, and date alignment have
not yet been selected.

### 3. Concentration

The concentration factor is intended to describe how strongly an ETF's current
portfolio is concentrated in a small number of holdings. Its eventual definition
must identify the holdings date, source, coverage, cash treatment, and handling
of incomplete weights.

Concentration will initially be a current-only factor. Current ETF holdings must
not be applied retroactively to historical periods because doing so would
introduce look-ahead bias. Historical concentration may be incorporated later
only if reliable point-in-time holdings snapshots become available and their
availability dates can be audited.

### 4. Volatility

The volatility factor is intended to measure the magnitude or change in ETF
price variability. The return frequency, estimator, lookback window, annualizing
convention, minimum observations, and missing-data rules remain to be defined.

## Composite score status

No historical or current composite has been approved. Flow is deferred, and the
project will not redefine the planned composite as Momentum plus Volatility or
otherwise finalize a reduced-factor substitute. Composite inputs, weights,
thresholds, interpretation, and naming remain undecided.

Current holdings may eventually support a current concentration measurement if
their date and source are disclosed, but this does not define a current score.
Likewise, the availability of Momentum and Volatility alone does not justify a
historical score. A result lacking direct positioning or flow evidence must not
be called a Crowding Score merely because Momentum and Volatility are present.

Any future composite will require transparent definitions, hand-calculated unit
tests, sensitivity analysis, point-in-time controls, and explicit missing-input
behavior before it is presented as a result.

## Point-in-time and data-integrity principles

- A value at time `t` may use only information observable at or before `t`.
- Data provenance and availability timing must be recorded well enough to audit
  look-ahead risk.
- Legitimately unavailable observations will remain missing and be flagged.
- Values will not be silently forward-filled.
- Current holdings can support a current concentration score but not a
  historical concentration series.
- Estimated creation/redemption activity will always be labeled as a proxy.

## Anticipated limitations

### Survivorship bias

The initial 24-ETF universe is curated from funds available today. Historical
analysis based on that universe may omit funds that closed, merged, or otherwise
left the investable set.

### Incomplete historical shares-outstanding data

Shares-outstanding histories may begin late, contain gaps, or reflect revisions.
The 2026-08-21 audit found that the observed Yahoo/yfinance histories could not
support the approved Flow specification across the configured universe. This is
a dated provider-feasibility result, not proof that Yahoo internally capped or
truncated its responses. See
[`flow-data-source-feasibility.md`](flow-data-source-feasibility.md).

### Flow proxy limitations

Changes in shares outstanding can approximate creation and redemption activity,
but they are not the same as independently reported net fund flows. Timing,
valuation, and data-quality differences may affect the estimate.

### Current versus historical holdings

Current holdings describe the current portfolio only. Applying them to earlier
dates would misstate the information set and introduce look-ahead bias. Historical
concentration therefore depends on obtaining reliable point-in-time snapshots.

### Third-party market-data availability

Coverage, field definitions, adjustment methods, revisions, rate limits, and
service continuity are controlled partly by yfinance and Yahoo Finance and may
change. The current ingestion layer records retrieval time but cannot reconstruct
an exchange-authoritative historical publication time from the provider output.

### Missing observations

Prices, shares outstanding, and holdings may be missing or stale on particular
dates. The price and shares pipelines preserve source missingness and do not
fill absent observations. Shares history may be sparse and is not expanded to
the price calendar. Future factor methodology must define minimum coverage,
alignment, and exclusion rules without fabricating observations.

Most of these remain anticipated research constraints. The shares-source
feasibility limitation was empirically observed on 2026-08-21, but no Flow
factor, score, backtest, or broader empirical conclusion was produced.
