# Planned Methodology

## Scope and status

This document describes the intended research design for the U.S. ETF Crowding
& Overheating Risk Monitor. No empirical factor calculations, scores,
thresholds, backtests, or conclusions exist at this stage. Exact definitions,
units, transformations, date alignment, and missing-data rules will be specified
and tested before any factor is presented as a result.

The framework is intended to monitor unusual combinations of risk indicators.
It is not intended to predict crashes or provide a trading signal.

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
when appropriate. Any use of raw prices will require an explicit reason. The
lookback horizon, return definition, minimum-history requirement, and date
alignment have not yet been selected.

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
service continuity are controlled partly by external providers and may change.

### Missing observations

Prices, shares outstanding, and holdings may be missing or stale on particular
dates. The eventual methodology must define minimum coverage and exclusion rules
without fabricating observations.

These are anticipated research constraints, not empirical findings. Their actual
impact cannot be assessed until data ingestion and analysis are implemented.
