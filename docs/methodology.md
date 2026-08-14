# Methodology

## Scope and status

This document describes the implemented price-data foundation and intended
research design for the U.S. ETF Crowding & Overheating Risk Monitor. Historical
daily price ingestion is implemented. No empirical factor calculations, scores,
thresholds, backtests, or conclusions exist at this stage. Exact definitions,
units, transformations, date alignment, and missing-data rules will be specified
and tested before any factor is presented as a result.

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
Canonical column labels must be unique, and all six market columns must use real
numeric pandas dtypes. Boolean, complex, string, object, datetime, timedelta,
categorical, and other non-real-numeric dtypes are invalid before Parquet
serialization. Values are not inspected or coerced to rescue a malformed dtype.
Individual missing market fields are preserved; observations are never
forward-filled, backfilled, or invented. Every ticker/date row must contain at
least one of `open`, `high`, `low`, `close`, `adjusted_close`, or `volume`. A row
with all six fields missing remains visible through normalization, is invalid,
and cannot enter canonical history. A response with no dated observations
remains a genuine empty response rather than a validation failure.

The canonical `data/processed/etf_prices_daily.parquet` file represents the
latest successfully validated Yahoo/yfinance source vintage available to this
pipeline. Exact duplicate observations within one incoming dataset may be
retained once; conflicting duplicates within that dataset remain invalid.
Incremental persistence compares each validated incoming row with the existing
row for the same ticker/date:

- Identical market fields retain one existing row; a different `retrieved_at`
  alone is not a revision.
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
Identical repeated writes do not create snapshots. The validated latest-vintage
canonical write remains atomic. An adjacent cross-process file lock, scoped to
the canonical output path, covers the entire existing-file read, validation,
merge, snapshot, and replacement transaction. Acquisition fails after a
30-second timeout rather than waiting indefinitely. The persistent lock file is
coordination metadata, not financial data; custom output paths do not share a
global lock.

The batch records successful, empty, and failed tickers separately. A partial
batch may persist valid ticker results only after the CLI reports that it is
partial; existing history for failed, empty, or otherwise unrequested tickers is
not deleted. An entirely empty or failed batch cannot replace an existing
canonical file.

## Planned live factors

### 1. Flow

The flow factor is intended to measure unusual ETF creation and redemption
activity. When it is inferred from changes in shares outstanding, it will be
called a **creation/redemption flow proxy**, not an exact or reported fund flow.
The eventual implementation must document the proxy formula, units, observation
timing, stale values, corporate actions, and missing-data behavior.

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

## Planned score distinction

The framework is expected to expose two related scores because concentration
data have different historical availability from price and shares-outstanding
data.

### Historical Crowding Score

Planned inputs:

```text
Flow + Momentum + Volatility
```

This score will be calculated only from information observable at or before each
historical calculation date. Concentration is excluded initially because the
project does not yet have reliable point-in-time historical holdings.

### Current Crowding Score

Planned inputs:

```text
Flow + Momentum + Concentration + Volatility
```

This score may use current holdings for a current concentration measurement,
provided the holdings date and source are disclosed. It will not be presented as
directly interchangeable with a three-factor historical score unless a later
methodology explicitly establishes comparability.

Factor normalization, weighting, aggregation, and interpretation thresholds are
not yet specified. They will require transparent definitions, hand-calculated
unit tests, sensitivity analysis, and clear communication of missing inputs.

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
This may reduce the usable history or comparability of the planned flow proxy.

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
dates. The price pipeline preserves missing market fields and does not fill
absent trading observations. Future factor methodology must define minimum
coverage and exclusion rules without fabricating observations.

These are anticipated research constraints, not empirical findings. Their actual
impact cannot be assessed until data ingestion and analysis are implemented.
