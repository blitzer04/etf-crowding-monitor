# Methodology

## Scope and status

This document describes the implemented price and shares-outstanding data
foundations, the implemented standalone Momentum and Volatility calculations,
and the intended research design for the U.S. ETF Crowding & Overheating Risk
Monitor. No Flow or Concentration component is implemented, and no composite
score, threshold set, or point-in-time backtest exists at this stage. Momentum
and Volatility are standalone diagnostic components, and neither is a Crowding
Score. Two controlled current-vintage price-signal evaluations have been
completed and audited. The Day 9 run targeted 2026-08-24 and had complete
target-session standalone Momentum and Volatility. The Day 11 run targeted
2026-08-28; all 24 canonical target rows exist, but `close` and
`adjusted_close` are missing, so target-session Momentum and Volatility remain
unavailable and one XNYS session stale. Their bounded empirical records are
documented in
[`price-signal-empirical-audit-2026-08-25.md`](price-signal-empirical-audit-2026-08-25.md)
and
[`price-signal-second-vintage-audit-2026-08-31.md`](price-signal-second-vintage-audit-2026-08-31.md).

Flow is deferred from both historical and current scores because the current
shares source did not pass the approved event-time acceptance specification.
This is a source-feasibility decision, not a change to that future specification
or to the implemented shares-ingestion contract. The dated empirical evidence
is recorded in [`flow-data-source-feasibility.md`](flow-data-source-feasibility.md).

Concentration is deferred pending a production-eligible holdings and
economic-entity-mapping architecture. Day 7 established the future methodology
and reviewed official-source feasibility; it did not implement holdings
ingestion, a Concentration calculation, normalization, or application behavior.
The evidence and unresolved source requirements are recorded in
[`concentration-data-source-feasibility.md`](concentration-data-source-feasibility.md).

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

## Factor methodology and status

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

Momentum is implemented as a standalone, per-ETF price-trend diagnostic. It
measures positive trailing total-return pressure; it does not measure investor
positioning, ownership concentration, or creation/redemption activity. Momentum
alone is not a Crowding Score and does not define a composite.

#### Reference calendar and alignment

Version-pinned `exchange-calendars==4.13.2` supplies the common `XNYS`
regular-session calendar for the configured U.S. ETF universe. XNYS session
labels are timezone-naive local market dates and align directly with canonical
price dates. Regular holidays and full special closures are not sessions. An
early-close day remains one session and receives no duration adjustment.

Calendar reindexing is validation and alignment only. It must not create,
modify, fill, or persist a price value. Any canonical price date inside the
Momentum calculation scope that is not an XNYS session is a validation error;
it is never silently dropped or remapped.

`exchange-calendars` is a versioned methodology dependency because corrected or
newly recorded closures can change session positions. Before approving any
future version, compare the complete old and candidate XNYS session-label sets
from `2018-01-01` through the evaluation end and review every added or removed
session. Do not allow an automatic dependency upgrade to change the calendar.

#### Raw return and endpoint eligibility

For an XNYS signal session `d_t`, let `d_{t-252}` be the session exactly 252
XNYS positions earlier. With finite, positive canonical adjusted closes `A_t`
and `A_{t-252}`, define the dimensionless raw Momentum observation:

```text
R_t = log(A_t / A_{t-252})
```

The optional display return, expressed in percentage points, is:

```text
simple_return_pct_t = 100 * (A_t / A_{t-252} - 1)
```

The implementation evaluates these unchanged formulas without first forcing an
exact integer endpoint difference through a rounded endpoint ratio. It uses a
cancellation-safe relative-change calculation where representable and a
log-domain fallback for extreme valid endpoints. If a mathematically valid
positive display return exceeds finite Float64 range, the primary raw log return
remains eligible and usable, `simple_return_pct` remains missing, and
`simple_return_status` is `exceeds_float64_range`. Ordinary representable display
returns use status `available`; endpoint-ineligible rows use
`endpoint_ineligible`.

There is one absolute 252-session horizon and no recent-session skip. The
normalized input is the log return, not the display percentage. Only canonical
`adjusted_close` is used. The calculation never substitutes `close`, shifts an
endpoint, carries an earlier value, interpolates, backfills, or reinterprets the
horizon as the 252nd nonmissing price.

If either exact endpoint row or endpoint `adjusted_close` is missing, `R_t`, the
display return, and normalized Momentum are `NaN`. A missing interior canonical
row or interior adjusted price does not shift the endpoints and does not by
itself invalidate the endpoint holding-period return because it is not an input
to the formula. The implementation preserves the counts and exact XNYS dates of
interior missing rows and present rows with missing `adjusted_close` as separate
diagnostics.

#### Signal timing

The observation is labeled as of the close of `d_t`. Its first prospective-use
date is the next XNYS session. This prevents same-session prospective use but
does not make the historical series point-in-time backtestable: canonical
prices and their adjusted-close history represent the currently retrieved
source vintage rather than a reconstruction of what the provider had published
on each historical date.

#### Per-ETF normalization

Normalize each ETF independently. The trailing 756-XNYS-session window as of
`d_t` is inclusive of `d_t`, but the current raw observation is excluded from
its reference population. Eligible prior raw observations may therefore be
dated only from `d_{t-755}` through `d_{t-1}`. Let their count be `N`; require
`N >= 252`.

For current raw observation `R_t`, use the exact midrank empirical percentile:

```text
P_t = (count(R_j < R_t) + 0.5 * count(R_j = R_t)) / N
Momentum_t = 100 * P_t
```

The output range is 0 to 100. A current value tied with a zero-variance
reference population receives 50. Higher Momentum means stronger positive
price pressure and therefore a stronger one-sided overheating indication.
Negative returns are not converted to absolute values or a two-sided extremeness
measure.

An invalid current return or fewer than 252 eligible prior observations
produces `NaN`. The calculation applies no winsorization or clipping and does
not substitute zero, neutrality, a stale observation, or redistributed
component weights.

With complete adjusted-close endpoints from the beginning of price history,
the first raw observation is on the 253rd XNYS session, the first normalized
observation is on the 505th session, and its first prospective-use date is the
506th session. Missing endpoints can delay eligibility.

#### Derived output and persistence

`calculate_momentum` returns an in-memory DataFrame sorted by ticker and signal
date. Its narrow audit contract contains:

- ticker, signal date, exact endpoint dates, and exact endpoint adjusted closes;
- raw log return, optional simple-return percentage, and deterministic
  display-return status;
- Momentum percentile and normalization reference count;
- first prospective-use XNYS session;
- endpoint eligibility and a reason code;
- counts and exact dates for interior missing rows and missing adjusted prices.

The calculation does not mutate or persist canonical prices and does not
persist its derived result. Historical values remain exploratory
current-vintage diagnostics, not point-in-time backtest results.

### 3. Concentration

Concentration is deferred pending a production-eligible holdings and
economic-entity-mapping architecture. The following is the approved future raw
methodology and its data-integrity boundary. It is not implemented.

#### Financial purpose and interpretation

Concentration will be current-only. Current holdings must never be applied
retroactively because that would misstate the historical information set and
introduce look-ahead bias. Historical Concentration would require point-in-time
holdings snapshots, auditable publication and retrieval timing, and mappings
valid for each historical observation.

The preferred future raw metric is direct-long-equity economic-entity HHI. For
each eligible economic entity `e`, define:

```text
p_e = (
    aggregated eligible direct-long equity weight for entity e
    / total eligible mapped direct-long equity weight
)

raw_hhi = sum(p_e ** 2)
hhi_points = 10,000 * raw_hhi
effective_holdings = 1 / raw_hhi
top_10_weight_pct = 100 * sum of the ten largest p_e values
```

`raw_hhi` is dimensionless. `hhi_points` is measured in HHI points,
`effective_holdings` is an effective count, and `top_10_weight_pct` is measured
in percentage points. With `n >= 1` eligible mapped entities, `raw_hhi` ranges
from `1 / n` to `1`, HHI points from `10,000 / n` to `10,000`, and effective
holdings from `1` to `n`. Top-10 weight is no greater than 100% and equals 100%
when ten or fewer entities are included.

HHI points, effective holdings, and top-10 weight are diagnostics. Higher HHI
and top-10 weight and lower effective holdings indicate greater disclosed
direct-long portfolio concentration. These measures do not establish investor
crowding, fund flows, positioning, liquidity stress, valuation excess,
overheating, crash risk, or future returns.

#### Position scope and reconciliation

Only eligible positive direct-long equity weights enter the future HHI
numerator. Cash, currencies, receivables, payables, liabilities, collateral,
futures, swaps, options, other derivatives, short positions, pooled funds, and
unknown instruments remain outside the numerator but must remain visible in
reconciliation diagnostics. Day 7 performs no fund-of-funds look-through.

Negative positions must not be squared, converted to positive weights, or
netted into direct-long weights. Derivative market value, notional, or
delta-adjusted exposure must not replace direct-long equity weight. Pooled funds
must not be treated as their underlying securities without a separately
approved look-through methodology.

Exact duplicate rows may be aggregated only after a stable security identifier
and identical economic meaning are established. Multiple share classes,
depositary receipts, and local listings may be aggregated to one economic entity
only through deterministic mapping. Ticker or company-name matching is not an
acceptable substitute.

Renormalization over eligible mapped direct-long equity weights is permitted
only after all of the following have been validated:

- the source identifies the correct legal fund or series and represents complete
  holdings for its stated scope;
- holdings as-of date, publication timing, and retrieval timing are auditable;
- required fields, identifiers, units, signs, and weight basis are established;
- every row is classified and duplicates are resolved deterministically;
- every included direct-long equity row has a valid positive weight and the
  mapping required by the selected metric;
- raw totals and excluded-position buckets pass a source-specific reconciliation
  contract; and
- the observation satisfies a future approved freshness policy.

No universal `98-102%` total-weight gate, `95%` direct-equity gate, `99%`
mapping gate, or `1%` unknown-position gate is approved. The official-source
study found no empirical support for those proposed values. Source totals may
not have comparable relationships to NAV, so any future gate must be supported
by authorized samples and source-specific semantics.

Missing, stale, partial, truncated, ambiguous, internally inconsistent, or
unmappable data produce `NaN`. Do not fill, carry forward, clip, winsorize,
substitute neutrality, automatically redistribute missing weights, or
substitute another component. An older observation may be displayed only as a
separately labeled stale diagnostic; it must not replace a current result.

#### Identifiers and economic-entity mapping

Economic-entity aggregation requires deterministic, versioned mappings from
security to issuing entity and from issuer to the selected parent entity. The
mapping source, relationship type, version or retrieval vintage, and applicable
date must be auditable. Open official sources have not demonstrated complete
ultimate-parent coverage for the configured holdings. Licensed mapping access,
point-in-time coverage, archival rights, and permission to display derived
results remain unresolved.

Security-level HHI remains a material initial alternative but is not the
approved economic-entity metric. If later approved, it must be named
`security-level concentration`; it must not be presented as economic-entity
concentration because separate share classes or listings can split one economic
exposure across multiple securities.

#### Freshness, universe, and normalization status

No universal two-XNYS-session freshness rule is approved. Official-source
research found that this rule is incompatible with the monthly complete-holdings
publication cadence for VTI and VGT. A 22-ETF daily cohort must not silently
replace the configured 24-ETF universe.

A lagged common-month-end cohort and issuer-specific raw observations remain
material future alternatives, not approved production methodology. Their date
semantics, staleness interpretation, source eligibility, and display treatment
would require separate approval.

No cross-sectional Concentration percentile is defined. Current-observation
inclusion, normalization population, eligibility denominator, minimum eligible
ETF count, and realized percentile range therefore remain undecided. Do not
reuse Momentum or Volatility normalization for Concentration.

#### Relationship to other components

Any future Concentration diagnostics would remain separate from implemented
Momentum and Volatility. They would not change either component's input,
calendar, normalization, timing, or interpretation. Flow remains deferred.
Do not define a composite, component weights, thresholds, risk classes,
missing-component substitution, or a Crowding Score.

### 4. Volatility

Volatility is implemented as a standalone diagnostic of whether an ETF's
recent price variability is unusually high relative to that ETF's own history.
It does not establish crowding, positioning, creation activity, expected
returns, crash probability, or an imminent reversal. It is not a Crowding
Score.

#### Reference calendar and alignment

Use the already pinned `exchange-calendars==4.13.2` `XNYS` regular-session
calendar. Let `d_t` be the exact XNYS signal session. An early-close session
counts once and receives no duration adjustment; full closures and holidays are
not sessions.

Any canonical price date inside the Volatility calculation scope that is not an
XNYS session must be rejected rather than silently removed or remapped. Calendar
reindexing is validation and alignment only. It must not create, fill, modify,
or persist prices.

Retain the controlled calendar-upgrade policy established for Momentum. Before
approving any future `exchange-calendars` version, compare the complete old and
candidate XNYS session-label sets from `2018-01-01` through the evaluation end
and review every added or removed session.

#### Daily return and stable arithmetic

Use canonical `adjusted_close` exclusively. For adjacent exact XNYS sessions,
define the dimensionless daily log return:

```text
r_e,t = log(A_e,t / A_e,t-1)
```

The calculation must not substitute `close`, skip a missing session, use the
previous available observation, interpolate, forward-fill, backfill, or
reinterpret the return as being between adjacent nonmissing prices. The
implementation uses numerically stable log-return arithmetic consistent with
the exact-domain protections required by Momentum.

#### Raw 21-session Volatility

Use exactly 21 adjacent daily log returns:

```text
r_e,t-20, ..., r_e,t
```

Those returns require the complete 22-price chain:

```text
A_e,t-21, ..., A_e,t
```

Define:

```text
mean_return_e,t =
    sum(r_e,j) / 21

raw_volatility_e,t =
    sqrt(252) *
    sqrt(
        sum((r_e,j - mean_return_e,t)^2) / (21 - 1)
    )
```

Use sample dispersion with `ddof=1`, so the denominator is 20. The raw result
is an annualized decimal. For display only, use:

```text
annualized_volatility_pct_e,t = 100 * raw_volatility_e,t
```

Use a fixed 252-session annualization convention. Do not adjust annualization
for the actual number of XNYS sessions in an individual calendar year or for
early-close duration. A sequence of identical daily returns has valid raw
Volatility equal to zero. This measures return dispersion and intentionally
differs from Momentum's directional price movement.

#### Eligibility and missingness

A raw observation is eligible only when all 22 exact XNYS price positions from
`d_t-21` through `d_t` have canonical rows with finite, positive
`adjusted_close`. Any missing canonical row or missing `adjusted_close` anywhere
in that required chain makes current raw Volatility `NaN`.

This differs intentionally from Momentum: every price in the Volatility chain
is an endpoint of at least one required adjacent-session return. Interior
missingness therefore invalidates the Volatility window instead of remaining
diagnostic-only.

The derived output retains deterministic diagnostics identifying:

- the missing canonical-row count and exact dates;
- the present-row/missing-`adjusted_close` count and exact dates;
- window eligibility and a deterministic reason code.

Do not permit partial windows, reduced minimum-period calculations, filling,
endpoint movement, or stale substitution.

#### Signal timing and freshness

Label Volatility as of the close of `d_t`. The value is observable only after
that close and must not be used prospectively at the same closing price. Its
first prospective-use date is the next XNYS regular session.

A current result requires an eligible observation on the declared target
session. An older value may be shown only as a separately labeled stale
diagnostic and must not replace current Volatility. `retrieved_at` may be
disclosed but is not an authoritative historical publication timestamp.

#### Per-ETF normalization

Normalize each ETF separately. For eligible current raw Volatility `V_e,t`, use
eligible prior raw Volatility observations whose signal dates are within the
trailing 756 XNYS sessions inclusive of `d_t`. The reference population is
limited exactly to:

```text
d_t-755 through d_t-1
```

Exclude the current raw observation and require at least 252 eligible prior
observations. Not every date in the 756-session window must contain an eligible
observation.

With reference-population size `N`, use the exact midrank empirical percentile:

```text
P_e,t = (
    count(V_e,j < V_e,t)
    + 0.5 * count(V_e,j = V_e,t)
) / N

Volatility_e,t = 100 * P_e,t
```

The output range is `[0, 100]`. Exact ties receive their midrank. If every
reference value equals the current value, Volatility is 50. If a zero-variance
reference population lies entirely below the current value, Volatility is 100;
if it lies entirely above the current value, Volatility is 0. Raw zero
Volatility is valid and is not replaced with missing or neutrality. Do not use
a z-score or standard-deviation normalization.

#### Risk direction and outlier policy

Volatility is one-sided. A higher percentile means unusually high current
return dispersion and higher instability or stress risk. Positive and negative
daily deviations contribute symmetrically to raw volatility. Low volatility is
not automatically transformed into high risk under a complacency theory.

Apply no winsorization or clipping. Preserve extreme valid results and their
inputs. Suspected provider anomalies require a separately approved data-quality
policy. Do not copy Flow's corporate-action quarantine. Use provider-supplied
`adjusted_close` and retain its current-vintage and provider-revision
limitations.

#### Relationship to Momentum and excluded variants

Momentum measures 252-session net price direction. Volatility measures the
dispersion of 21 adjacent daily returns around their sample mean. A constant
positive-return sequence can have high Momentum and zero Volatility; an
oscillating sequence can have low net Momentum and high Volatility.

Do not volatility-adjust Momentum, construct a Sharpe-like metric, or combine
the two. Evaluate their empirical correlation before any future composite
decision.

Do not add a 21/252 volatility ratio, volatility-change factor, multi-horizon
blend, or benchmark-relative Volatility in this phase. The normalized
21-session level already measures whether recent volatility is unusually
elevated relative to the ETF's own history. Additional horizons would require
separately approved lookbacks, zero-denominator behavior, normalization,
interpretation, and weighting.

#### Missing-component and composite behavior

Invalid current data or insufficient normalization history produces `NaN`. Do
not substitute zero, a neutral percentile, stale Volatility, raw `close`,
another component, or automatically redistributed weights.

Flow remains deferred. Do not define a Momentum-plus-Volatility composite,
weights, thresholds, risk classes, or a Crowding Score. Use the name
`Volatility percentile` or `21-session annualized Volatility`.

#### Derived output and persistence

`calculate_volatility` returns an in-memory DataFrame sorted by ticker and
signal date. Its narrow audit contract contains:

- ticker, signal date, exact 22-price window dates, and first prospective-use
  XNYS session;
- raw annualized decimal Volatility, display percentage, percentile, and
  normalization reference count;
- window eligibility and a deterministic reason code;
- counts and exact dates for missing canonical rows and present rows with
  missing `adjusted_close`.

The implementation validates the output schema, nullable dtypes, unique and
sorted keys, finite values, percentile range, diagnostic count/date agreement,
and eligibility/status/value relationships before returning a nonempty result.
It fails rather than repairing a contradictory derived result. The calculation
does not mutate or persist canonical prices and does not persist its derived
output.

#### Historical status and earliest eligibility

Historical Volatility calculated from the current canonical adjusted-price
vintage is exploratory current-vintage history, not a point-in-time backtest.
Historical adjusted prices may be revised, and current `retrieved_at` values
cannot reconstruct when each historical price vintage first became observable.

Under complete adjusted-close availability, the theoretical first raw
Volatility observation is on the 22nd XNYS reference session. The theoretical
first normalized observation with 252 eligible prior observations is on the
274th session, and its first prospective-use date is the 275th session.

Conditional on complete history beginning on the first XNYS session of 2018,
the corresponding theoretical calendar dates are:

- first raw Volatility: `2018-02-01`;
- first normalized Volatility: `2019-02-04`;
- first prospective-use date: `2019-02-05`.

These dates were theoretical before a live dataset existed. The tracked
repository still contains no canonical price data, but the local Git-ignored
2026-08-25 dataset independently confirmed these eligibility dates for every
configured ETF. See
[`price-signal-empirical-audit-2026-08-25.md`](price-signal-empirical-audit-2026-08-25.md).

## Standalone Momentum and Volatility evaluation workflow

Day 8 implements a reusable offline-first workflow around the approved public
Momentum and Volatility APIs. It does not alter, duplicate, or reinterpret either
financial calculation. Momentum and Volatility remain separate price
diagnostics, and their native outputs and missingness diagnostics remain intact.

### Operating mode and target session

Offline mode is the default. It loads the existing canonical price Parquet and
makes no provider request. Absence of the canonical file is an error and must not
produce an empty bundle that resembles a valid evaluation.

Refresh mode requires the explicit `--refresh` command-line option or an
explicit `refresh=True` reusable-API argument. Capture one timezone-aware instant
before any acquisition, normalize it to UTC, and preserve its nanosecond
precision through the in-memory result and manifest. With the pinned
`exchange-calendars==4.13.2` XNYS schedule, define the target as:

```text
d_t = latest XNYS session whose scheduled close <= captured UTC instant
```

Apply no post-close grace period. The provider request begins on `2018-01-01`
inclusive and uses the calendar day immediately after `d_t` as its explicit
exclusive end. The workflow invokes the existing independent per-ticker price
adapter once for every configured ETF and adds no retry loop. Existing canonical
validation, partial-batch behavior, coverage metadata, source-vintage merging,
revision snapshots, locking, and atomic persistence remain unchanged.

Offline evaluation may contain no acquisition-status collection. Whenever
statuses are supplied, the shared canonical price-retrieval validator requires
exact configured-universe coverage, one common request window matching the
evaluation bounds, strict success/empty/failed fields, and exact reconciliation
of successful returned dates, row counts, and UTC retrieval timestamps to the
canonical acquisition rows. The validator does not infer or repair incorrect
metadata. Canonical retrieval timestamps and their per-ticker extrema retain
UTC nanosecond precision through coverage, Parquet, and manifest serialization.

Failure or delay in publishing the target observation does not move `d_t`
backward. A current value exists only when the native component output is
eligible on `d_t`. Older observations may be retained only as separately labeled
diagnostics. Price staleness, raw-signal staleness, and normalized-signal
staleness are each the count of XNYS sessions after the applicable latest date
through `d_t`; missing histories have missing staleness rather than an invented
age.

### Coverage and component diagnostics

The evaluation input is the validated canonical slice for configured ETFs from
`2018-01-01` through `d_t`, inclusive. Calendar alignment creates no price rows.
Coverage output contains exactly one row for every configured ETF in
configuration order, even when that ETF was empty, failed acquisition, or is
entirely absent from the canonical input.

For each ETF, retain:

- request and acquisition metadata where available;
- first and last canonical and nonmissing adjusted-close dates;
- expected and present XNYS counts, exact missing canonical dates, and exact
  present-row/missing-`adjusted_close` dates;
- target price-row and adjusted-close availability;
- first and last eligible raw and normalized dates for each component;
- target raw and normalized eligibility, native target status, normalization
  status, reference count, and financial values where available; and
- separate price, raw-component, and normalized-component XNYS staleness.

An absent or ineligible value remains missing. Do not represent it as zero,
neutrality, an older current value, a substitute component, or an automatically
redistributed value. Exact endpoint and chain reasons remain in the native
Momentum and Volatility artifacts.

### Descriptive dependence diagnostics

Dependence uses only an exact inner join on `(ticker, signal_date)` where both
native normalized percentiles are present. Do not pair different sessions,
carry observations forward, or fill either component.

Report Pearson and Spearman correlations separately:

- for every configured ETF across its exact-date overlaps; and
- for every XNYS session across the ETFs with an exact-date overlap.

An estimate requires at least three pairs and at least two distinct values in
each input. Otherwise the estimate remains missing with deterministic status
`insufficient_pairs` or `constant_input`. Each row records its pair count,
applicable first and last dates, estimator, scope, ETF or session key, and
included tickers. A session is `full_universe` only when all 24 configured ETFs
are included; every other session is `incomplete_universe`.

Do not calculate p-values or a pooled correlation. These estimates are
descriptive co-movement diagnostics only. They do not establish causality,
predictive value, crowding, investor positioning, future returns, or suitability
for a composite.

### Transactional local run bundle

The default output root is `data/processed/signal_evaluations/`. Every run uses a
microsecond-resolution UTC run ID and a new non-overwriting directory containing:

- `input_prices.parquet`: immutable copy of the exact canonical slice used;
- `coverage.parquet`: the configured-universe coverage and staleness contract;
- `momentum.parquet`: native Momentum output;
- `volatility.parquet`: native Volatility output;
- `dependence.parquet`: descriptive exact-date correlations; and
- `manifest.json`: timing, Git, command, version, universe, acquisition, schema,
  row-count, filename, and SHA-256 provenance.

Before pandas-to-Arrow conversion, a holistic non-mutating validator checks the
canonical input and regenerates the native signals, coverage, and dependence
through the approved package calculations. Exact equality is required, including
keys, dtypes, values, null masks, statuses, dates, counts, staleness, and
cross-artifact relationships. Present non-finite values are rejected rather than
silently converted to Arrow nulls. Git HEAD and dirty state are captured before
creating any output directory or artifact.

Parquet artifacts use explicit deterministic Arrow schemas. Diagnostic date
collections use Arrow lists of nanosecond timestamps. Before publication, every
artifact is reloaded and compared losslessly for values, nulls, exact dates,
counts, ordering, statuses, collections, and schema. Hashes are calculated only
after successful reload. The manifest does not contain its own hash and is
reloaded and checked against every artifact.

All files are first written below a sibling temporary directory. Only after all
validation succeeds is that directory renamed to the final previously nonexistent
run path. The manifest and every artifact are then reopened from the final path;
hashes, schemas, counts, semantic equivalence, manifest relationships, and the
holistic evaluation contract are checked again before success is returned. A
failed final-path check is quarantined under a clearly invalid sibling name and
is not returned as a valid run. Modifications after return cannot be prevented,
so every consumer must verify manifest hashes before using a bundle. Bundles are
local generated data and are not automatically published or committed.

### Historical and interpretation limitations

The immutable input artifact preserves the exact adjusted-price vintage used by
that run, but it does not reconstruct the price vintage or publication timing
that existed on each historical signal date. Historical signal paths and their
correlations therefore remain exploratory current-vintage analysis, not a
point-in-time backtest. The present-day configured ETF universe also introduces
survivorship bias.

Day 8.2 used deterministic synthetic data to implement and test the workflow. It
did not execute refresh mode or retrieve market data. Day 9 later completed one
separately authorized live refresh and an independent read-only audit. The
canonical input and bundle remain local Git-ignored artifacts rather than
repository-tracked data; the tracked findings are recorded in
[`price-signal-empirical-audit-2026-08-25.md`](price-signal-empirical-audit-2026-08-25.md).
Flow and Concentration remain deferred. Do not define a composite, component
weights, thresholds, risk classes, missing-component reweighting, or a Crowding
Score.

## Composite score status

No historical or current composite has been approved. Flow and Concentration
are deferred, and the project will not redefine the planned composite as
Momentum plus Volatility or otherwise finalize a reduced-factor substitute.
Composite inputs, weights, thresholds, interpretation, and naming remain
undecided.

Production-eligible current holdings and deterministic economic-entity mappings
may eventually support a current Concentration measurement, but this does not
define a score. Likewise, the availability of Momentum and Volatility alone
does not justify a historical score. A result lacking direct positioning or
flow evidence must not be called a Crowding Score merely because Momentum and
Volatility are present.

Any future composite will require transparent definitions, hand-calculated unit
tests, sensitivity analysis, point-in-time controls, and explicit missing-input
behavior before it is presented as a result.

## Point-in-time and data-integrity principles

- A value at time `t` may use only information observable at or before `t`.
- Data provenance and availability timing must be recorded well enough to audit
  look-ahead risk.
- Legitimately unavailable observations will remain missing and be flagged.
- Values will not be silently forward-filled.
- Production-eligible current holdings may support a current Concentration
  measurement but not a historical series.
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
Concentration therefore depends on reliable point-in-time snapshots with
auditable availability timing. Current Concentration is also deferred because
holdings contracts and deterministic economic-parent mapping are not yet
production-eligible. See
[`concentration-data-source-feasibility.md`](concentration-data-source-feasibility.md).

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
