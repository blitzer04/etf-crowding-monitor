# Flow Data-Source Feasibility

## Status

The creation/redemption flow proxy is deferred from both historical and current
scores. The shares-outstanding ingestion pipeline and raw-data contract remain
implemented, but an implemented pipeline does not establish that its current
provider is empirically suitable for Flow scoring.

This note records a dated provider-feasibility result. It does not change the
future Flow acceptance specification in [`methodology.md`](methodology.md), and
it is not a provider root-cause analysis.

## Yahoo/yfinance audit result

On 2026-08-21, a read-only empirical audit requested shares-outstanding history
for all 24 configured ETFs with an explicit start of `2018-01-01` and used
matching canonical price observations to evaluate the planned event-time
eligibility rules. The audit found:

- Zero of 24 ETFs could reach 252 eligible prior observations.
- Accepted shares histories ended in March 2021.
- Seven tickers returned empty shares histories.
- QQQ and XOP failed canonical validation because their responses contained
  conflicting values for the same date.
- No ETF produced a normalized Flow series. The differing successful, empty,
  and canonical-validation-failure outcomes prevented complete, comparable
  coverage of the configured universe.

These findings establish only that the observed Yahoo/yfinance responses were
not suitable for the approved Flow methodology on the audit date. They do not
establish Yahoo's undocumented internal cause. In particular, the project does
not claim as fact that the responses were capped or truncated.

## Approved consequence

Flow must remain absent from both historical and current scores until a source
passes the unchanged acceptance specification. Its absence must not be replaced
with zero, a neutral component, stale Yahoo observations, stitched issuer-page
observations, or automatically redistributed component weights.

No reduced-factor composite has been approved. Composite inputs, weights,
thresholds, interpretation, and naming remain undecided. A result containing
Momentum and Volatility but no direct positioning or flow evidence must not be
called a Crowding Score merely because those two components are available.

## Leading future validation candidate

DTCC ETF Portfolio Data Service is the leading candidate for a separately
authorized empirical validation. It is not an approved, licensed, available, or
implemented project source. Production eligibility requires all of the
following to be demonstrated:

- Populated ETF shares-outstanding coverage for all 24 configured ETFs.
- Documented shares history from at least `2018-01-01` through the current
  period.
- Deterministic semantics for unchanged observations, blanks, explicit zeros,
  duplicates, and corrections.
- Sufficient eligible observations under the unchanged event-time methodology.
- Authoritative corporate-action handling suitable for replacing the temporary
  conservative quarantine heuristic.
- Receipt and availability timing suitable for prospective point-in-time use.
- Written licensing permission to display derived metrics on the public
  dashboard.

Until those requirements are satisfied, the project has no production-eligible
Flow data source.
