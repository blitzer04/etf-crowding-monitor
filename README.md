# U.S. ETF Crowding & Overheating Risk Monitor

An early-stage quantitative-finance research project for monitoring potential
crowding and overheating across a curated set of U.S.-listed equity ETFs. The
planned public application will present transparent risk indicators while the
reusable methodology remains in a tested Python package.

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

## Planned methodology

The monitor is planned around four interpretable factors:

1. **Flow:** a creation/redemption flow proxy when derived from changes in
   shares outstanding, clearly distinguished from reported fund flows.
2. **Momentum:** price-trend measures based on appropriately adjusted prices.
3. **Concentration:** a current holdings-based measure of how strongly an ETF is
   exposed to a small number of constituents.
4. **Volatility:** measures of the magnitude and change in price variability.

The planned historical score will combine flow, momentum, and volatility.
Concentration will be added only to the current score until reliable
point-in-time holdings snapshots are available. Current holdings will not be
applied retroactively because that would introduce look-ahead bias. Definitions,
alignment rules, and limitations are documented in
[`docs/methodology.md`](docs/methodology.md).

No factor calculations, weights, thresholds, scores, or empirical conclusions
have been implemented yet.

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

The future Streamlit application will call functions from `src/etf_crowding/`;
financial logic will not be duplicated in the presentation layer.

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

## Planned technology stack

- Python 3.12
- pandas, NumPy, SciPy, and statsmodels for data and statistical work
- yfinance as an anticipated third-party market-data interface
- PyYAML and PyArrow for configuration and data storage
- Plotly and Streamlit for the planned public application
- pytest, Ruff, and mypy for quality checks

## Current development status

Day 1 establishes the repository layout, packaging and tool configuration, ETF
universe configuration, configuration validation, methodology documentation,
and offline unit tests. Market-data downloads, factor calculations, crowding
scores, backtests, notebooks with analysis, and Streamlit pages are not yet
implemented.

## Data limitations

Anticipated limitations include survivorship bias from the present-day curated
universe, incomplete shares-outstanding history, the difference between a flow
proxy and reported flows, lack of historical point-in-time holdings, third-party
data availability, revisions, and missing observations. Missing data will not be
silently invented or forward-filled. Availability timing and provenance will be
documented as data pipelines are added.

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

## Planned testing commands

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
