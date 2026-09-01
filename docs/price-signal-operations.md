# Price-Signal Operations Runbook

## Scope and safety boundary

This runbook governs a manual, on-demand local refresh of the configured 24-ETF
price batch followed by standalone Momentum and Volatility evaluation and local
bundle publication. It is an operating procedure, not another empirical audit,
and it does not authorize methodology, schema, calculation, dependency,
configuration, or provider-adapter changes.

The authorized path is limited to prices, canonical price persistence, the
existing standalone evaluation, and its six-file local bundle. It does not
authorize shares, holdings, Flow, Concentration, a composite, score, export,
public display, or any provider action beyond the explicitly authorized price
batch.

## Preconditions and authorization

- Operate manually and on demand. There is no scheduler.
- Obtain explicit authorization for exactly one complete 24-ETF batch. One
  authorization does not permit a retry or second batch.
- Record the intended timezone-aware evaluation instant, expected Git branch and
  commit, expected local `main...origin/main` divergence, and the opening
  protected-artifact inventory before execution. Do not fetch or contact a
  remote to establish the local Git baseline.
- Run from the repository root in the configured Python 3.12 environment. Stop
  if the Git baseline, configured-universe count, canonical input, or protected
  inventory is unexpected.
- Ensure that no other price writer is running. The adjacent canonical lock file
  is persistent coordination metadata; its presence is expected and it must not
  be deleted.
- Treat the eventual run ID as unknown. It is generated during publication and
  cannot be promised exactly before acquisition.

## Read-only preflight

Confirm the authorized local Git baseline without fetching:

```powershell
git branch --show-current
git rev-parse HEAD
git rev-list --left-right --count main...origin/main
git status --short --branch
git status --porcelain=v1 --untracked-files=all
git diff --name-only
git diff --cached --name-only
git diff
git diff --cached
```

Inventory the configured universe and protected paths. The universe command must
report 24 ordered definitions.

```powershell
.venv\Scripts\python.exe -c "from etf_crowding.config import load_etf_universe; u=load_etf_universe(); print(len(u)); print(','.join(x.ticker for x in u))"

$BundleRoot = 'data\processed\signal_evaluations'
$PriceSnapshotRoot = 'data\snapshots\prices'
$SharesSnapshotRoot = 'data\snapshots\shares'

Get-ChildItem -LiteralPath $BundleRoot -Force -Directory |
    Sort-Object Name | Select-Object Name, LastWriteTimeUtc
Get-ChildItem -LiteralPath $PriceSnapshotRoot -Force -File |
    Sort-Object Name | Select-Object Name, Length, LastWriteTimeUtc
Get-ChildItem -LiteralPath $SharesSnapshotRoot -Force -File |
    Sort-Object Name | Select-Object Name, Length, LastWriteTimeUtc
```

Capture opening SHA-256, byte size, and UTC mtime for the canonical prices,
persistent lock, every price and shares snapshot, and every file below the
bundle root. Keep the following variables in the same PowerShell session through
post-run verification.

```powershell
function Get-ProtectedArtifactState {
    $paths = @(
        Get-Item -LiteralPath 'data\processed\etf_prices_daily.parquet'
        Get-Item -LiteralPath 'data\processed\.etf_prices_daily.parquet.lock'
        Get-ChildItem -LiteralPath $PriceSnapshotRoot -Force -File
        Get-ChildItem -LiteralPath $BundleRoot -Force -File -Recurse
        Get-ChildItem -LiteralPath $SharesSnapshotRoot -Force -File
    )
    foreach ($path in ($paths | Sort-Object FullName)) {
        [pscustomobject]@{
            Path = $path.FullName
            SHA256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $path.FullName).Hash.ToLowerInvariant()
            Size = $path.Length
            MtimeUtc = $path.LastWriteTimeUtc.ToString('o')
        }
    }
}

$OpeningProtected = @(Get-ProtectedArtifactState)
$OpeningProtected | Format-Table -AutoSize
```

This capture is identity evidence, not authorization to alter, prune, or delete
any artifact.

## Target-session resolution

Choose and record one timezone-aware instant. Use a placeholder until the actual
controlled instant is fixed; do not copy a prior audit date as a default.

```powershell
$EvaluationInstant = '<ISO-8601 timezone-aware evaluation instant>'
$env:ETF_CROWDING_EVALUATION_INSTANT = $EvaluationInstant
.venv\Scripts\python.exe -c "import os; import exchange_calendars as xcals; import pandas as pd; from etf_crowding.analysis import resolve_evaluation_target; t=resolve_evaluation_target(os.environ['ETF_CROWDING_EVALUATION_INSTANT']); cal=xcals.get_calendar('XNYS', start=t.target_session-pd.Timedelta(days=1), end=t.target_session+pd.Timedelta(days=1)); c=cal.session_close(t.target_session); print(f'captured_at={t.captured_at.isoformat()}'); print(f'target_session={t.target_session.date().isoformat()}'); print(f'target_close={c.isoformat()}'); print(f'operational_gate={(c + pd.Timedelta(hours=2)).isoformat()}'); print(f'request=[{t.request_start.isoformat()}, {t.request_end.isoformat()})')"
```

The methodological target rule has no grace period: the target is the latest
XNYS session whose scheduled close is not later than the captured instant. This
rule determines the target only.

## Operational post-close buffer

For same-day execution, wait until at least two hours after the resolved target
close before starting the one authorized batch. The two-hour gate is an
operational safeguard, not a methodological grace period; it does not change the
resolved target or permit the target to move backward. It also does not prove or
guarantee that the provider has published complete target-session data.

Stop before provider contact if the recorded execution time is earlier than the
printed operational gate. Waiting longer still does not establish provider
availability.

## Exactly-once refresh execution

After the preflight and operational gate pass, invoke the refresh command once:

```powershell
.venv\Scripts\python.exe scripts\evaluate_price_signals.py --refresh --evaluation-instant $EvaluationInstant
$RefreshExitCode = $LASTEXITCODE
```

Do not place this command in a loop, wrapper retry, scheduler, or automatic
follow-up. The project performs zero orchestration-level retries. One authorized
batch means 24 configured ETF acquisition operations, but yfinance-managed
authentication, cookie, crumb, retry, or ancillary transport can make the raw
HTTP transmission count different from 24.

Canonical persistence occurs before evaluation and bundle publication. A run ID
is created only during publication. Never pre-create or reserve a run directory.

## Outcome branches

1. **All acquisition outcomes unusable:** the workflow exits with no canonical
   dataset write and no bundle publication. Record the failure and stop; do not
   rerun automatically.
2. **Valid full batch:** canonical persistence completes, then evaluation and a
   new non-overwriting six-file bundle may be published. Continue with all
   post-run checks.
3. **Valid partial batch:** at least one ticker supplied usable observations, so
   the validated partial batch may persist. The evaluation must still contain
   all 24 coverage records, with success, empty, or failed acquisition outcomes
   and missing/stale signal diagnostics retained explicitly.
4. **Failure after canonical persistence:** canonical data and any revision
   snapshot may already be durable even if evaluation or publication fails.
   There is no automatic rollback. Freeze further writes, inventory the exact
   state, and follow the failure procedure.

Any same-target follow-up is a new controlled source vintage requiring separate
authorization. It is not a retry of the completed or failed authorization.

## Post-run verification

First repeat the Git baseline commands from the preflight. Confirm the branch,
HEAD, local divergence, exact tracked changes, staged state, and untracked state.

Capture closing artifact identity and compare every opening path on SHA-256,
size, and UTC mtime. Expected new paths, such as a valid new bundle or a revision
snapshot, must be reviewed separately; they do not make changes to opening paths
acceptable without explanation.

```powershell
$ClosingProtected = @(Get-ProtectedArtifactState)
$ClosingProtected | Format-Table -AutoSize

Compare-Object $OpeningProtected $ClosingProtected -Property Path,SHA256,Size,MtimeUtc
```

Validate the canonical schema, key count, universe count, and date span through
the public validator:

```powershell
.venv\Scripts\python.exe -c "import pandas as pd; from etf_crowding.data.validation import validate_price_data; p=pd.read_parquet('data/processed/etf_prices_daily.parquet'); validate_price_data(p); d=int(p.duplicated(['ticker','date']).sum()); print(f'rows={len(p)} tickers={p.ticker.nunique()} duplicate_keys={d} first={p.date.min()} last={p.date.max()}'); print(p.dtypes.to_string())"
```

Inventory every snapshot and every exact bundle directory. A valid bundle must
be one direct-child run directory containing exactly the five expected Parquet
files and `manifest.json`; temporary, quarantined, rejected, or extra names must
be reported separately and never treated as valid.

```powershell
Get-ChildItem -LiteralPath $PriceSnapshotRoot -Force -File |
    Sort-Object Name | Select-Object FullName, Length, LastWriteTimeUtc
Get-ChildItem -LiteralPath $SharesSnapshotRoot -Force -File |
    Sort-Object Name | Select-Object FullName, Length, LastWriteTimeUtc
Get-ChildItem -LiteralPath $BundleRoot -Force -Directory | Sort-Object Name |
    ForEach-Object {
        [pscustomobject]@{
            Directory = $_.Name
            Files = ((Get-ChildItem -LiteralPath $_.FullName -Force |
                Sort-Object Name | Select-Object -ExpandProperty Name) -join ',')
        }
    }
```

Explicitly list every relevant run ID; never substitute “latest.” Load each one
through the public consumer. This verifies the exact six snapshotted bytes,
manifest, hashes, schemas, counts, configured universe, timing, statuses, and
cross-artifact semantics. It also prints the complete acquisition-status
inventory and the required per-ETF missing/stale report.

```powershell
$RelevantRunIds = @('<UTC run ID 1>', '<UTC run ID 2>')
foreach ($RunId in $RelevantRunIds) {
    $env:ETF_CROWDING_RUN_ID = $RunId
    .venv\Scripts\python.exe -c "import os; from pathlib import Path; from etf_crowding.analysis import load_signal_evaluation_bundle; b=load_signal_evaluation_bundle(Path('data/processed/signal_evaluations'), os.environ['ETF_CROWDING_RUN_ID']); c=b.to_pandas('coverage'); print(f'run={b.run_id} verified_content_sha256={b.content_sha256} rows={len(c)}'); print(c['acquisition_status'].value_counts(dropna=False).to_string()); cols=['ticker','acquisition_status','target_price_row_present','target_adjusted_close_present','price_staleness_sessions','momentum_target_status','momentum_target_normalization_status','momentum_raw_staleness_sessions','momentum_normalized_staleness_sessions','volatility_target_status','volatility_target_normalization_status','volatility_raw_staleness_sessions','volatility_normalized_staleness_sessions']; print(c[cols].to_string(index=False))"
    if ($LASTEXITCODE -ne 0) { throw "Public consumer verification failed for $RunId" }
}
```

Finally run the repository's authorized validation gates, inspect every complete
tracked and untracked diff, and report the exact final Git state. A failed check
does not authorize another provider batch.

## Missing-target treatment

The target never moves backward to chase available data. Missing target rows,
missing target `adjusted_close`, ineligible raw signals, unavailable normalized
signals, and all staleness counts remain explicit in coverage and presentation.
Do not fill, substitute `close`, select an older run silently, relabel stale data
as current, or infer that an acquisition `success` means every target field is
present.

## Partial-batch treatment

A validated partial price batch may be canonically persisted. Successful
tickers are retained with their provenance; empty and failed tickers retain
their prior canonical history when the persistence contract permits it. The
evaluation and any valid published bundle must retain exactly one coverage row
for every configured ETF. Never drop unavailable ETFs, redistribute weights, or
convert missing values to zero.

## Snapshot and bundle retention

Valid non-overwriting bundles and canonical revision snapshots are retained
indefinitely for the present research horizon. The persistent adjacent lock file
is retained as coordination metadata and must not be removed. There is no
automatic overwrite, pruning, cleanup, or deletion of those artifacts or of
successfully quarantined final-path output. This retention rule does not apply to
pre-publication sibling temporary directories, which the publisher removes on a
best-effort basis after an exception.

## Failure and recovery procedure

1. Stop after the first failed command; do not rerun the refresh.
2. Immediately record the exit code, error text, exact Git state, canonical
   identity, lock identity, snapshot inventory, bundle-directory inventory, and
   all opening to closing identity differences; inspect their integrity before
   any recovery decision.
3. Determine whether failure occurred before canonical persistence, after
   canonical persistence, before the final bundle-directory rename, after that
   rename, or during consumer verification. Canonical persistence and any
   required revision snapshot are not automatically rolled back when later
   evaluation or publication fails. Do not infer rollback from the absence of a
   valid bundle.
4. If publication fails before the final directory rename, the publisher removes
   the sibling `.RUNID.tmp-*` directory automatically on a best-effort basis. Do
   not expect, require, or rely on preserving that temporary directory.
5. If validation fails after the final directory rename, the publisher attempts
   to rename the final-looking directory to a unique `RUNID.invalid-<UUID>`
   quarantine directory. A successfully quarantined directory remains retained,
   unselectable, and unavailable to the dashboard. Do not rename it into a
   selectable run, repair it in place, or delete it.
6. If quarantine itself fails, a suspicious final-looking directory may remain.
   Treat it as invalid: the public consumer must still fail closed, and operators
   must inspect it manually. Never select or display it merely because its name
   resembles a run ID.
7. Request separate authorization for any recovery write. Restoring a canonical
   snapshot is a separately authorized manual recovery action, never an
   automatic rollback.
8. Treat any same-target follow-up acquisition as a new controlled vintage with
   its own authorization, preflight, operational gate, opening identities, and
   post-run verification.

## Dashboard use

The local dashboard is a read-only consumer, not part of refresh execution.
Follow [`../app/README.md`](../app/README.md), explicitly select one verified run,
and confirm the displayed run ID and fixed target date. The viewer must never
fall back to newest or older data. Missing and stale results remain visible, and
Momentum and Volatility remain separate. Dashboard use grants no export, public
display, redistribution, provider action, or data-repair authority.

## Prohibited actions

- No scheduler, automatic retry, automatic second batch, or implicit same-target
  follow-up.
- No shares, holdings, Flow, Concentration, composite, score, threshold, risk
  class, export, public display, or additional provider operation.
- No manual edits to canonical data, snapshots, manifests, bundle artifacts, or
  acquisition statuses.
- No fallback to an older target or unverified run, and no representation of
  missing or stale data as current.
- No automatic rollback or snapshot restoration under the refresh authorization;
  no operator-initiated overwrite, pruning, cleanup, or deletion of valid
  bundles, revision snapshots, the persistent lock, or quarantined final-path
  output; and no quarantine promotion, staging, commit, or push.

## Current limitations

- Neither the no-grace target methodology nor the two-hour operating buffer
  proves provider availability or target-session completeness.
- Provider-managed raw HTTP transmissions are not strictly bounded to 24.
- Run IDs are publication-time values and cannot be promised exactly in advance.
- Raw provider response bytes, complete returned-date lists, and an authenticated
  acquisition identifier are not retained in the bundle.
- The manifest is unsigned and not externally self-authenticating; a residual
  local time-of-check/time-of-use risk remains after consumer verification.
- Historical values use the captured current provider vintage, not point-in-time
  archived vintages, and the present-day universe retains survivorship bias.
- Redistribution and public-display rights remain unresolved. Flow and
  Concentration remain deferred, and no composite, weights, thresholds, risk
  classes, missing-component reweighting, or Crowding Score exists.
