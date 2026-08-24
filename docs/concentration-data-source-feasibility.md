# Concentration Data-Source Feasibility

## Status and evidence boundary

Concentration is deferred pending a production-eligible holdings and
economic-entity-mapping architecture. Day 7 established the future methodology
and performed an official-source feasibility study; it did not implement
holdings ingestion, Concentration calculations, normalization, application
logic, configuration, tests, dependencies, or persistence.

No holdings file was downloaded during the study. No bulk holdings request,
login, licensed-data access, or provider contact occurred. The findings below
are limited to official issuer interfaces and documentation, SEC documentation,
and official identifier or mapping-vendor documentation. They do not establish
the contents of an uninspected downloadable file.

Evidence labels used in this note are:

- **Confirmed:** stated in official documentation or visible in an official
  public interface.
- **Inference:** a conclusion drawn from confirmed facts but not itself an
  issuer guarantee.
- **Unresolved:** requires authorized file samples, licensed access, or legal or
  provider clarification.

## Regulatory and issuer coverage

**Confirmed:** Rule 6c-11 generally requires a covered open-end ETF to disclose
the prior business day's portfolio holdings before regular trading opens on its
primary listing exchange. Required fields include ticker, CUSIP or another
identifier, description, quantity, and portfolio weight. The disclosure scope
includes cash, short positions, and written options, although the SEC identified
inconsistent derivative presentation. Unit investment trusts and ETF share
classes are outside Rule 6c-11's scope.
[SEC Rule 6c-11 compliance guide](https://www.sec.gov/investment/exchange-traded-funds-small-entity-compliance-guide),
[SEC holdings statement](https://www.sec.gov/newsroom/speeches-statements/im-staff-statement-foreign-currency-holdings-011924-0),
[SEC adopting release](https://www.sec.gov/files/rules/final/2019/33-10695.pdf)

**Inference:** matching the configured funds' legal structures with the
regulatory requirement and official issuer interfaces establishes daily
complete-holdings availability for 22 of the 24 ETFs. Twenty are standalone
open-end ETFs subject to the Rule 6c-11 disclosure floor. SPY and DIA are unit
investment trusts outside that rule but have issuer-provided daily holdings.
State Street also provides daily and month-end SPDR holdings files.
[State Street SPY](https://www.ssga.com/us/en/individual/etfs/state-street-spdr-sp-500-etf-trust-spy),
[State Street DIA](https://www.ssga.com/us/en/intermediary/etfs/state-street-spdr-dow-jones-industrial-average-etf-trust-dia),
[State Street fund finder](https://www.ssga.com/us/en/intermediary/fund-finder)

**Confirmed:** VTI and VGT are ETF share classes with complete holdings
published monthly, approximately 15 calendar days after month-end. Vanguard's
disclosure definition excludes cash equivalents, derivatives, and other
positions and permits some positions to be withheld, so its public complete-
holdings contract is not equivalent to a full daily NAV reconciliation.
[VTI holdings](https://advisors.vanguard.com/investments/products/vti/vanguard-total-stock-market-etf?source=autosugg),
[VGT holdings](https://advisors.vanguard.com/investments/products/vgt/vanguard-information-technology-etf),
[Vanguard disclosure policy](https://www.sec.gov/Archives/edgar/data/52848/000119312525320080/f43543d1.htm)

**Inference:** a fresh exact-date 24-of-24 cross-section is therefore not
feasible under the proposed universal two-XNYS-session freshness rule. A daily
22-ETF cohort would change the configured universe and is not an approved
substitute. A prospectively captured common-month-end cohort could be evaluated
only after the lagged Vanguard publication and would no longer satisfy the
proposed freshness rule.

## Cross-issuer comparability

The official interfaces confirm that holdings are available, but they do not
establish one interchangeable data contract:

| Issuer family | Confirmed official disclosure | Unresolved production questions |
| --- | --- | --- |
| State Street/SPDR | Daily all-holdings interfaces plus daily and month-end holdings files; rendered fields include holding name, shares, and weight. [SPY](https://www.ssga.com/us/en/individual/etfs/state-street-spdr-sp-500-etf-trust-spy), [fund finder](https://www.ssga.com/us/en/intermediary/fund-finder) | Downloaded schema, holding identifiers for every fund, weight-to-NAV basis, non-equity taxonomy, derivative exposure fields, correction vintages, and permitted automation or redistribution. |
| iShares | Daily CSV interfaces expose identifiers and fields including ticker, name, asset class, market value, weight, quantity, and notional. Official notes state that investment-book or vendor values can differ from accounting-book/NAV values; options notional is delta-adjusted. [IWM](https://www.ishares.com/us/products/239710/), [iShares disclosure timing](https://www.ishares.com/us/literature/sai/sai-ishares-inc-eo-8-31.pdf) | Uniform treatment of every derivative, public correction history, and permission for systematic archival and derived display. |
| Vanguard | Monthly complete-holdings tables expose percent of fund, market value, shares, CUSIP, and SEDOL. [VTI](https://advisors.vanguard.com/investments/products/vti/vanguard-total-stock-market-etf?source=autosugg), [VGT](https://advisors.vanguard.com/investments/products/vgt/vanguard-information-technology-etf) | Exact export payload, withheld-position behavior, reconciliation after excluded positions, correction vintages, and production reuse rights. |
| Invesco | QQQ provides an all-holdings interface, and QQQ and TAN are standalone open-end ETFs subject to daily Rule 6c-11 disclosure. The interface offers an Excel download and a generic security-identifier field. [QQQ holdings](https://www.invesco.com/qqq-etf/en/about.html), [QQQ structure](https://www.invesco.com/qqq-etf/en/market-outlook/whats-new-about-qqq.html), [TAN](https://www.invesco.com/us/en/financial-products/etfs/invesco-solar-etf.html) | Exact identifier type, downloaded schema, weight/NAV reconciliation, derivative treatment, history, and correction vintages. |
| VanEck | SMH provides daily holdings and an XLS download with ticker, holding name, percent net assets, and market value. Its official interface can include positive cash and negative other/cash rows. [SMH](https://www.vaneck.com/us/en/investments/semiconductor-etf-smh/) | Holding-level identifiers in the file, notional or delta fields, correction history, and automated archival or display permission. |
| ARK | ARKK provides full CSV and PDF holdings with company, ticker, CUSIP, shares, market value, and weight. The document date denotes the next trading day. [ARKK](https://www.ark-funds.com/funds/arkk), [ARK date explanation](https://helpcenter.ark-funds.com/can-you-explain-the-date-listed-on-the-ark-etf-holdings-documents) | Weight/NAV reconciliation, non-equity and derivative taxonomy, correction vintages, and an authorized automated-access mechanism. |
| Global X | LIT provides a full CSV with net-assets percentage, ticker, name, SEDOL, market price, shares, and market value. Its interface defines cash as U.S. dollars and excludes cash, currencies, and other positions from some breakdowns. [LIT](https://www.globalxetfs.com/funds/lit) | Holding-level CUSIP/ISIN coverage, derivative exposure, correction history, and automated archival or derived-display rights. |

**Confirmed:** formats, available identifiers, weight labels, position fields,
publication cadence, documented exclusions, available history, and website
terms differ by issuer. The reviewed materials also do not provide one common
contract for derivative notional or delta-adjusted exposure.

**Unresolved:** no 24-file sample exists from which to measure total reported
weight, direct-equity coverage, cash, derivatives, shorts, pooled funds,
negative rows, unknown rows, or cross-issuer publication latency. Weight-to-NAV
reconciliation, daily correction behavior, and production reuse rights therefore
remain unverified.

The SEC's public N-PORT datasets provide a lagged regulatory cross-check, not a
daily point-in-time holdings archive. The public datasets expose the last month
of each fiscal quarter and are updated quarterly.
[SEC N-PORT datasets](https://www.sec.gov/data-research/sec-markets-data/form-n-port-data-sets),
[SEC reporting modernization guide](https://www.sec.gov/resources-small-businesses/small-business-compliance-guides/investment-company-reporting-modernization-rules)

## Economic-entity mapping

The required future mapping path is:

```text
security identifier
    -> issuing legal entity
    -> selected parent entity
    -> stable, versioned economic-entity identifier
```

**Confirmed:** GLEIF and ANNA publish ISIN-to-LEI relationship files, while
GLEIF Level 2 data records direct and ultimate accounting-consolidating parent
relationships. The mappings contain coverage limitations and parent-reporting
exceptions; they do not establish complete ultimate-parent coverage for all
future holdings.
[GLEIF/ANNA ISIN-to-LEI files](https://www.gleif.org/en/lei-data/lei-mapping/download-isin-to-lei-relationship-files),
[GLEIF Level 2 data](https://www.gleif.org/en/lei-data/access-and-use-lei-data/level-2-data-who-owns-whom)

**Confirmed:** OpenFIGI maps external identifiers to instrument, composite, and
share-class FIGIs but does not itself provide a complete ultimate-parent
hierarchy. CUSIP's issuer prefix likewise does not encode corporate-control
relationships, and storage or redistribution of CUSIP data can require a
license.
[OpenFIGI documentation](https://www.openfigi.com/api/documentation),
[CUSIP identifier structure](https://www.cusip.com/identifiers.html?section=CUSIP),
[CUSIP licensing](https://www.cusip.com/apply/index.html)

**Confirmed:** licensed services can provide parts of the required hierarchy.
CGS LEI Plus maps CUSIP issuers to LEIs where available; LSEG provides
instrument and organization relationships; and S&P advertises links from
instrument identifiers to issuers and ultimate-parent hierarchies.
[CGS LEI Plus](https://www.cusip.com/pdf/CGS_LEI_Plus.pdf),
[LSEG Symbology](https://developers.lseg.com/en/api-catalog/refinitiv-data-platform/symbology-API),
[S&P Cross Reference Services](https://www.spglobal.com/market-intelligence/en/solutions/cross-reference-services)

**Inference:** economic-entity HHI is not production-reproducible from the
reviewed open official sources alone. A licensed mapping master is the leading
architecture, but coverage, point-in-time versioning, storage rights, and
permission to display derived results remain unresolved. Ticker and company-name
heuristics are prohibited.

## Unsupported gates and normalization

No downloaded holdings sample exists to support the proposed universal gates:

- total reported weight between 98% and 102%;
- at least 95% direct-equity coverage;
- at least 99% mapped weight; or
- no more than 1% unknown weight.

Those values are not approved. Issuer-specific accounting and disclosure
semantics must be understood before total-weight or coverage tolerances are
calibrated. Missing, stale, partial, truncated, ambiguous, or unmappable inputs
remain `NaN`; they are not filled, carried forward, clipped, winsorized, or
replaced with neutral values.

No cross-sectional Concentration percentile is approved. Current-observation
inclusion, normalization population, eligibility denominator, minimum eligible
ETF count, and realized percentile range remain undecided. A 22-ETF daily cohort,
a lagged common-month-end cohort, and issuer-specific raw observations are
alternatives for future approval, not production methodology.

## Access, archival, and display rights

**Confirmed:** reviewed issuer terms contain restrictions or limitations relevant
to automated collection, copying, redistribution, or commercial use.
[State Street SPY legal footer](https://www.ssga.com/us/en/individual/etfs/state-street-spdr-sp-500-etf-trust-spy),
[BlackRock terms](https://www.blackrock.com/corporate/compliance/terms-and-conditions),
[Vanguard terms](https://investor.vanguard.com/terms-conditions),
[Invesco terms](https://www.invesco.com/us/en/resources/terms-of-use.html),
[VanEck legal terms](https://www.vaneck.com/us/en/legal/),
[ARK terms](https://www.ark-funds.com/terms)

**Inference:** those terms do not collectively establish a production right to
retrieve holdings systematically, retain permanent source-vintage archives, and
show derived results on a public dashboard.

**Unresolved:** production eligibility requires a source-by-source determination
of automated-access, archival, correction-retention, and public derived-display
rights. The research did not contact providers or reach a legal conclusion.
Absence of an identified restriction is not permission.

## Future validation plan -- not executed

This plan requires separate authorization and access-rights review before any
retrieval begins.

### Initial source sample

Retrieve one complete holdings file and its metadata page for each configured
ETF:

- 13 State Street/SPDR holdings files;
- 4 iShares CSV files;
- 2 Vanguard complete monthly exports;
- 2 Invesco full holdings exports;
- 1 VanEck SMH XLS file;
- 1 ARK ARKK CSV file; and
- 1 Global X LIT CSV file.

The planned initial sample is exactly 24 holdings files plus 24 metadata
captures. A family-level archive may replace individual files only after its
fund coverage, file identity, schema, and permitted use have been validated.

### Cadence and correction study

After automated access is permitted, observe each issuer across a separately
approved sequence of XNYS sessions. Record holdings as-of date, publication and
retrieval timing, content hash, unchanged publications, corrections, and missing
or delayed files. Do not infer a historical publication vintage from a current
file.

### Regulatory cross-check

Compare eligible issuer samples with the corresponding public N-PORT positions
for the latest available reporting period. Treat accounting date, publication
lag, scope, and valuation differences as reconciliation inputs rather than
assuming equality. SEC automated access must follow its fair-access policy.
[SEC fair-access policy](https://www.sec.gov/filergroup/announcements-old/new-rate-control-limits)

### Mapping and licensing validation

Measure deterministic security-to-issuer-to-parent mapping coverage by row and
by eligible direct-equity weight. Test open mappings and any separately licensed
candidate against duplicate securities, multiple share classes, depositary
receipts, local listings, missing identifiers, parent exceptions, and corporate
relationships. Confirm point-in-time/version behavior, storage rights, and
permission to publish derived concentration diagnostics.

### Acceptance tests

The validation must test:

- fund identity, holdings as-of date, publication timing, retrieval timing, and
  correction vintage;
- file format, schema, required fields, identifiers, datatypes, signs, and units;
- number-of-holdings and source-specific weight/NAV reconciliation;
- complete classification of direct equity, cash, currencies, liabilities,
  collateral, futures, swaps, options, shorts, pooled funds, and unknown rows;
- market value, weight, derivative notional, and delta conventions;
- duplicate-row and multiple-share-class treatment;
- deterministic economic-entity mapping coverage and mapping revisions;
- common-date availability, XNYS-session age, stale and delayed behavior;
- partial, truncated, corrected, and republished files; and
- permission for systematic retrieval, permanent archival, and public display
  of derived diagnostics.

Only evidence from that separately authorized validation may be used to propose
numeric gates, a freshness policy, an eligible reference population, or a
cross-sectional normalization.

## Approved consequence

Concentration remains unimplemented and missing. Flow also remains deferred.
Do not substitute another factor, redistribute weights, or define a composite,
thresholds, risk classes, or a Crowding Score.
