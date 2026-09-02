"""Discover and verify immutable local signal-evaluation bundles."""

from __future__ import annotations

import hashlib
import json
import re
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Literal, cast

import exchange_calendars as xcals  # type: ignore[import-untyped]
import numpy as np
import pandas as pd
import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.parquet as pq  # type: ignore[import-untyped]

from etf_crowding.analysis.signal_evaluation import (
    COVERAGE_COLUMNS,
    DEPENDENCE_COLUMNS,
    EvaluationTarget,
    _assert_frame_exact,
    _coverage_table,
    _dependence_table,
    _evaluation_sessions,
    _input_price_table,
    _momentum_table,
    _schema_manifest,
    _universe_hash,
    _validate_canonical_evaluation_input,
    _validate_coverage,
    _validate_dependence,
    _validate_present_finite_columns,
    _validate_table_semantics,
    _volatility_table,
    calculate_dependence_diagnostics,
    resolve_evaluation_target,
)
from etf_crowding.config import ETFDefinition, load_etf_universe
from etf_crowding.data.validation import (
    CANONICAL_PRICE_COLUMNS,
    PRICE_VALUE_COLUMNS,
)
from etf_crowding.signals import (
    MOMENTUM_OUTPUT_COLUMNS,
    VOLATILITY_OUTPUT_COLUMNS,
)
from etf_crowding.signals.momentum import (
    MOMENTUM_LAG_SESSIONS,
    MOMENTUM_MINIMUM_PRIOR_OBSERVATIONS,
    MOMENTUM_NORMALIZATION_WINDOW_SESSIONS,
)
from etf_crowding.signals.volatility import (
    VolatilityDataValidationError,
    _validate_volatility_output,
)

DEFAULT_SIGNAL_BUNDLE_DIRNAME = "signal_evaluations"

type ArtifactName = Literal[
    "input_prices",
    "coverage",
    "momentum",
    "volatility",
    "dependence",
]

_RUN_ID_PATTERN = re.compile(r"^[0-9]{8}T[0-9]{12}Z$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_GIT_HASH_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_ARTIFACT_FILENAMES: Mapping[ArtifactName, str] = MappingProxyType(
    {
        "input_prices": "input_prices.parquet",
        "coverage": "coverage.parquet",
        "momentum": "momentum.parquet",
        "volatility": "volatility.parquet",
        "dependence": "dependence.parquet",
    }
)
_EXPECTED_FILENAMES = frozenset({*_ARTIFACT_FILENAMES.values(), "manifest.json"})
_MANIFEST_KEYS = frozenset(
    {
        "artifacts",
        "captured_utc_reference_instant",
        "command_arguments",
        "created_at",
        "git_head",
        "input_row_count",
        "input_sha256",
        "mode",
        "package_versions",
        "request_end_exclusive",
        "request_start",
        "run_id",
        "target_xnys_session",
        "ticker_metadata",
        "universe_config_sha256",
        "worktree_dirty",
    }
)
_PACKAGE_VERSION_KEYS = frozenset(
    {
        "etf-crowding-monitor",
        "exchange-calendars",
        "numpy",
        "pandas",
        "pyarrow",
        "python",
        "scipy",
        "yfinance",
    }
)
_TICKER_METADATA_KEYS = frozenset(
    {
        "acquisition_status",
        "error",
        "first_returned_date",
        "input_first_retrieved_at",
        "input_last_retrieved_at",
        "input_rows",
        "last_returned_date",
        "query_end_exclusive",
        "query_start",
        "retrieved_at",
        "rows_received",
        "ticker",
    }
)
_MOMENTUM_FLOAT_COLUMNS = (
    "raw_momentum",
    "simple_return_pct",
    "momentum_percentile",
)
_MOMENTUM_COUNT_COLUMNS = (
    "normalization_reference_count",
    "interior_missing_row_count",
    "interior_missing_adjusted_close_count",
)
_MOMENTUM_DATE_COLUMNS = (
    "signal_date",
    "endpoint_start_date",
    "endpoint_end_date",
    "first_prospective_session",
)
_VOLATILITY_FLOAT_COLUMNS = (
    "raw_annualized_volatility",
    "annualized_volatility_pct",
    "volatility_percentile",
)
_VOLATILITY_COUNT_COLUMNS = (
    "normalization_reference_count",
    "missing_row_count",
    "missing_adjusted_close_count",
)
_VOLATILITY_DATE_COLUMNS = (
    "signal_date",
    "window_start_date",
    "window_end_date",
    "first_prospective_session",
)
_COVERAGE_NUMERIC_COLUMNS = (
    "acquisition_rows_received",
    "expected_xnys_observation_count",
    "present_xnys_observation_count",
    "missing_canonical_count",
    "missing_adjusted_close_count",
    "price_staleness_sessions",
    "momentum_target_raw",
    "momentum_target_simple_return_pct",
    "momentum_target_percentile",
    "momentum_target_reference_count",
    "momentum_raw_staleness_sessions",
    "momentum_normalized_staleness_sessions",
    "volatility_target_raw",
    "volatility_target_annualized_pct",
    "volatility_target_percentile",
    "volatility_target_reference_count",
    "volatility_raw_staleness_sessions",
    "volatility_normalized_staleness_sessions",
)


class SignalBundleError(ValueError):
    """Base error for read-only bundle discovery and verification failures."""


class SignalBundleDiscoveryError(SignalBundleError):
    """Indicate that the local bundle root cannot be inspected safely."""


class SignalBundleSelectionError(SignalBundleError):
    """Indicate that a requested run is not a selectable direct child."""


class SignalBundleIntegrityError(SignalBundleError):
    """Indicate missing, changed, unreadable, or hash-invalid bundle bytes."""


class SignalBundleValidationError(SignalBundleError):
    """Indicate that parsed bundle data contradict the supported contract."""


@dataclass(frozen=True, slots=True)
class SignalBundleInventory:
    """Describe selectable and explicitly excluded local bundle directories.

    Attributes:
        run_ids: Candidate run IDs in reverse chronological filename order.
        temporary_names: In-progress publisher directories, never selectable.
        quarantined_names: Publisher-quarantined directories, never selectable.
        rejected_names: Other direct children that are not safe run candidates.
    """

    run_ids: tuple[str, ...]
    temporary_names: tuple[str, ...]
    quarantined_names: tuple[str, ...]
    rejected_names: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class VerifiedSignalEvaluationBundle:
    """Hold one fully verified local bundle without mutable financial frames.

    PyArrow tables are immutable. ``manifest`` and the artifact mappings are
    recursively read-only. ``to_pandas`` returns a new caller-owned frame.

    Attributes:
        run_id: Selected UTC run identifier.
        bundle_path: Verified direct-child bundle directory.
        content_sha256: Combined digest of the exact six snapshotted files.
        manifest_sha256: Digest of the snapshotted manifest bytes.
        manifest: Deeply immutable parsed manifest.
        artifact_sha256: Immutable computed artifact hashes by logical name.
        tables: Immutable verified Arrow tables by logical artifact name.
        universe: Current packaged ETF definitions in configured order.
        target: Validated captured instant, target, and request bounds.
    """

    run_id: str
    bundle_path: Path
    content_sha256: str
    manifest_sha256: str
    manifest: Mapping[str, object]
    artifact_sha256: Mapping[str, str]
    tables: Mapping[str, pa.Table]
    universe: tuple[ETFDefinition, ...]
    target: EvaluationTarget

    def to_pandas(self, artifact: ArtifactName) -> pd.DataFrame:
        """Return a new pandas representation of one verified artifact.

        Args:
            artifact: Logical artifact name from the bundle contract.

        Returns:
            A newly materialized contract-typed DataFrame.

        Raises:
            KeyError: If ``artifact`` is not part of the verified bundle.
        """

        table = self.tables[artifact]
        return _artifact_frame(artifact, table)


@dataclass(frozen=True, slots=True)
class _BundleSnapshot:
    path: Path
    files: Mapping[str, bytes]
    sha256_by_filename: Mapping[str, str]
    content_sha256: str


def _path_is_reparse_point(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return True
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    is_junction = getattr(path, "is_junction", lambda: False)()
    return path.is_symlink() or is_junction or bool(attributes & reparse_flag)


def discover_signal_evaluation_runs(output_root: Path) -> SignalBundleInventory:
    """Discover local run candidates without opening any bundle artifact.

    Discovery never implies validity. Callers must explicitly select a run ID
    and pass it to ``load_signal_evaluation_bundle`` before displaying values.

    Args:
        output_root: Expected parent of non-overwriting run directories.

    Returns:
        Immutable candidate, temporary, quarantined, and rejected name groups.

    Raises:
        SignalBundleDiscoveryError: If the root is missing, unsafe, or unreadable.
    """

    root = Path(output_root)
    if not root.exists() or not root.is_dir() or _path_is_reparse_point(root):
        raise SignalBundleDiscoveryError(
            f"Signal bundle root is missing or unsafe: {root}."
        )
    try:
        entries = tuple(root.iterdir())
    except OSError as error:
        raise SignalBundleDiscoveryError(
            f"Signal bundle root cannot be inspected: {root}."
        ) from error

    candidates: list[str] = []
    temporary: list[str] = []
    quarantined: list[str] = []
    rejected: list[str] = []
    for entry in entries:
        name = entry.name
        if name.startswith(".") and ".tmp-" in name:
            temporary.append(name)
        elif ".invalid-" in name:
            quarantined.append(name)
        elif (
            _RUN_ID_PATTERN.fullmatch(name)
            and entry.is_dir()
            and not _path_is_reparse_point(entry)
        ):
            candidates.append(name)
        else:
            rejected.append(name)

    return SignalBundleInventory(
        run_ids=tuple(sorted(candidates, reverse=True)),
        temporary_names=tuple(sorted(temporary)),
        quarantined_names=tuple(sorted(quarantined)),
        rejected_names=tuple(sorted(rejected)),
    )


def _validated_candidate_path(output_root: Path, run_id: str) -> Path:
    if type(run_id) is not str or _RUN_ID_PATTERN.fullmatch(run_id) is None:
        raise SignalBundleSelectionError(
            "Run ID must use the exact YYYYMMDDTHHMMSSffffffZ format."
        )
    root = Path(output_root)
    if not root.exists() or not root.is_dir() or _path_is_reparse_point(root):
        raise SignalBundleSelectionError(
            f"Signal bundle root is missing or unsafe: {root}."
        )
    try:
        resolved_root = root.resolve(strict=True)
    except OSError as error:
        raise SignalBundleSelectionError(
            f"Signal bundle root cannot be resolved: {root}."
        ) from error
    candidate = root / run_id
    if (
        not candidate.exists()
        or not candidate.is_dir()
        or _path_is_reparse_point(candidate)
    ):
        raise SignalBundleSelectionError(
            f"Selected run is missing, unsafe, or not a directory: {run_id}."
        )
    try:
        resolved_candidate = candidate.resolve(strict=True)
    except OSError as error:
        raise SignalBundleSelectionError(
            f"Selected run cannot be resolved safely: {run_id}."
        ) from error
    if resolved_candidate.parent != resolved_root:
        raise SignalBundleSelectionError(
            "Selected run must be a direct child of the bundle root."
        )
    return resolved_candidate


def _snapshot_bundle(output_root: Path, run_id: str) -> _BundleSnapshot:
    candidate = _validated_candidate_path(output_root, run_id)
    try:
        entries = tuple(candidate.iterdir())
    except OSError as error:
        raise SignalBundleIntegrityError(
            f"Selected bundle cannot be enumerated: {run_id}."
        ) from error
    names = {entry.name for entry in entries}
    if names != _EXPECTED_FILENAMES:
        missing = sorted(_EXPECTED_FILENAMES.difference(names))
        extra = sorted(names.difference(_EXPECTED_FILENAMES))
        raise SignalBundleIntegrityError(
            f"Bundle file set is incomplete or modified; missing={missing}, "
            f"extra={extra}."
        )
    if any(not entry.is_file() or _path_is_reparse_point(entry) for entry in entries):
        raise SignalBundleIntegrityError(
            "Every bundle entry must be a non-reparse regular file."
        )

    files: dict[str, bytes] = {}
    sha256_by_filename: dict[str, str] = {}
    try:
        for filename in sorted(_EXPECTED_FILENAMES):
            payload = (candidate / filename).read_bytes()
            files[filename] = payload
            sha256_by_filename[filename] = hashlib.sha256(payload).hexdigest()
    except OSError as error:
        raise SignalBundleIntegrityError(
            f"Bundle bytes could not be snapshotted: {run_id}."
        ) from error

    # Recheck the directory boundary after the snapshot. Verification and all
    # displayed values use only the captured bytes, never a later file reopen.
    try:
        closing_entries = tuple(candidate.iterdir())
    except OSError as error:
        raise SignalBundleIntegrityError(
            f"Bundle changed while it was being snapshotted: {run_id}."
        ) from error
    if {entry.name for entry in closing_entries} != _EXPECTED_FILENAMES or any(
        not entry.is_file() or _path_is_reparse_point(entry)
        for entry in closing_entries
    ):
        raise SignalBundleIntegrityError(
            f"Bundle changed while it was being snapshotted: {run_id}."
        )

    combined = hashlib.sha256()
    for filename in sorted(files):
        encoded_name = filename.encode("utf-8")
        combined.update(len(encoded_name).to_bytes(4, "big"))
        combined.update(encoded_name)
        combined.update(bytes.fromhex(sha256_by_filename[filename]))
    return _BundleSnapshot(
        path=candidate,
        files=MappingProxyType(files),
        sha256_by_filename=MappingProxyType(sha256_by_filename),
        content_sha256=combined.hexdigest(),
    )


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise SignalBundleIntegrityError(
                f"Manifest contains a duplicate object key: {key!r}."
            )
        result[key] = value
    return result


def _parse_manifest(payload: bytes) -> dict[str, object]:
    try:
        loaded = json.loads(
            payload.decode("utf-8"), object_pairs_hook=_reject_duplicate_json_keys
        )
    except SignalBundleIntegrityError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SignalBundleIntegrityError(
            "Manifest is not valid unique-key UTF-8 JSON."
        ) from error
    if not isinstance(loaded, dict):
        raise SignalBundleValidationError("Manifest must be a JSON object.")
    return cast(dict[str, object], loaded)


def _required_mapping(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(type(key) is not str for key in value):
        raise SignalBundleValidationError(f"Manifest field '{name}' is invalid.")
    return cast(dict[str, object], value)


def _required_list(value: object, name: str) -> list[object]:
    if not isinstance(value, list):
        raise SignalBundleValidationError(f"Manifest field '{name}' must be a list.")
    return value


def _required_string(value: object, name: str) -> str:
    if type(value) is not str or not value:
        raise SignalBundleValidationError(
            f"Manifest field '{name}' must be a nonempty string."
        )
    return value


def _required_nonnegative_int(value: object, name: str) -> int:
    if type(value) is not int or value < 0:
        raise SignalBundleValidationError(
            f"Manifest field '{name}' must be a nonnegative integer."
        )
    return value


def _manifest_date(value: object, name: str) -> date:
    text = _required_string(value, name)
    try:
        parsed = date.fromisoformat(text)
    except ValueError as error:
        raise SignalBundleValidationError(
            f"Manifest field '{name}' must be an ISO date."
        ) from error
    if parsed.isoformat() != text:
        raise SignalBundleValidationError(
            f"Manifest field '{name}' must use YYYY-MM-DD."
        )
    return parsed


def _manifest_utc_instant(value: object, name: str) -> pd.Timestamp:
    text = _required_string(value, name)
    if not text.endswith("Z"):
        raise SignalBundleValidationError(
            f"Manifest field '{name}' must use a Z-suffixed UTC instant."
        )
    try:
        instant = pd.Timestamp(text)
    except (TypeError, ValueError) as error:
        raise SignalBundleValidationError(
            f"Manifest field '{name}' is not a valid UTC instant."
        ) from error
    if pd.isna(instant) or instant.tzinfo is None or str(instant.tz) != "UTC":
        raise SignalBundleValidationError(
            f"Manifest field '{name}' must use the UTC timezone."
        )
    return instant


def _validate_schema_manifest(value: object, artifact: str) -> None:
    fields = _required_list(value, f"artifacts.{artifact}.schema")
    names: list[str] = []
    for field in fields:
        mapping = _required_mapping(field, f"artifacts.{artifact}.schema field")
        if set(mapping) != {"name", "nullable", "type"}:
            raise SignalBundleValidationError(
                f"Artifact '{artifact}' has an incompatible schema manifest."
            )
        names.append(_required_string(mapping["name"], "schema.name"))
        _required_string(mapping["type"], "schema.type")
        if type(mapping["nullable"]) is not bool:
            raise SignalBundleValidationError(
                f"Artifact '{artifact}' schema nullability is invalid."
            )
    if len(names) != len(set(names)):
        raise SignalBundleValidationError(
            f"Artifact '{artifact}' schema contains duplicate fields."
        )


def _validate_ticker_metadata(
    manifest: Mapping[str, object],
    universe: Sequence[ETFDefinition],
    *,
    mode: str,
    request_start: date,
    request_end: date,
) -> list[dict[str, object]]:
    metadata = _required_list(manifest["ticker_metadata"], "ticker_metadata")
    if len(metadata) != len(universe):
        raise SignalBundleValidationError(
            "Manifest ticker metadata does not cover the configured universe."
        )
    rows: list[dict[str, object]] = []
    for expected, raw_row in zip(universe, metadata, strict=True):
        row = _required_mapping(raw_row, "ticker_metadata row")
        if set(row) != _TICKER_METADATA_KEYS:
            raise SignalBundleValidationError(
                f"Ticker metadata for {expected.ticker} has incompatible fields."
            )
        ticker = _required_string(row["ticker"], "ticker_metadata.ticker")
        if ticker != expected.ticker:
            raise SignalBundleValidationError(
                "Manifest ticker metadata does not retain configured order."
            )
        status = _required_string(
            row["acquisition_status"], "ticker_metadata.acquisition_status"
        )
        allowed = {"not_requested", "success", "empty", "failed"}
        if status not in allowed or (mode == "offline") != (status == "not_requested"):
            raise SignalBundleValidationError(
                f"Ticker metadata acquisition status is invalid for {ticker}."
            )
        input_rows = _required_nonnegative_int(
            row["input_rows"], "ticker_metadata.input_rows"
        )
        del input_rows
        input_first = row["input_first_retrieved_at"]
        input_last = row["input_last_retrieved_at"]
        if (input_first is None) != (input_last is None):
            raise SignalBundleValidationError(
                f"Input retrieval extrema disagree for {ticker}."
            )
        if input_first is not None:
            first_instant = _manifest_utc_instant(
                input_first, "ticker_metadata.input_first_retrieved_at"
            )
            last_instant = _manifest_utc_instant(
                input_last, "ticker_metadata.input_last_retrieved_at"
            )
            if first_instant > last_instant:
                raise SignalBundleValidationError(
                    f"Input retrieval extrema are reversed for {ticker}."
                )

        if status == "not_requested":
            if any(
                row[key] is not None
                for key in (
                    "error",
                    "rows_received",
                    "retrieved_at",
                    "query_start",
                    "query_end_exclusive",
                    "first_returned_date",
                    "last_returned_date",
                )
            ):
                raise SignalBundleValidationError(
                    f"Offline acquisition metadata is contradictory for {ticker}."
                )
        else:
            query_start = _manifest_date(
                row["query_start"], "ticker_metadata.query_start"
            )
            query_end = _manifest_date(
                row["query_end_exclusive"], "ticker_metadata.query_end_exclusive"
            )
            if query_start != request_start or query_end != request_end:
                raise SignalBundleValidationError(
                    f"Acquisition request bounds disagree for {ticker}."
                )
            rows_received = _required_nonnegative_int(
                row["rows_received"], "ticker_metadata.rows_received"
            )
            first_returned = row["first_returned_date"]
            last_returned = row["last_returned_date"]
            retrieved_at = row["retrieved_at"]
            error = row["error"]
            if status == "success":
                if rows_received == 0 or error is not None:
                    raise SignalBundleValidationError(
                        f"Successful acquisition metadata is invalid for {ticker}."
                    )
                first_date = _manifest_date(
                    first_returned, "ticker_metadata.first_returned_date"
                )
                last_date = _manifest_date(
                    last_returned, "ticker_metadata.last_returned_date"
                )
                _manifest_utc_instant(retrieved_at, "ticker_metadata.retrieved_at")
                if (
                    first_date > last_date
                    or first_date < query_start
                    or last_date >= query_end
                ):
                    raise SignalBundleValidationError(
                        f"Successful acquisition dates are invalid for {ticker}."
                    )
            elif status == "empty":
                if (
                    rows_received != 0
                    or error is not None
                    or any(
                        value is not None for value in (first_returned, last_returned)
                    )
                    or retrieved_at is None
                ):
                    raise SignalBundleValidationError(
                        f"Empty acquisition metadata is invalid for {ticker}."
                    )
                _manifest_utc_instant(retrieved_at, "ticker_metadata.retrieved_at")
            else:
                if (
                    rows_received != 0
                    or type(error) is not str
                    or not error
                    or error.strip() != error
                    or any(
                        value is not None
                        for value in (first_returned, last_returned, retrieved_at)
                    )
                ):
                    raise SignalBundleValidationError(
                        f"Failed acquisition metadata is invalid for {ticker}."
                    )
        rows.append(row)
    return rows


def _validate_manifest_contract(
    manifest: dict[str, object],
    snapshot: _BundleSnapshot,
    universe: tuple[ETFDefinition, ...],
) -> tuple[EvaluationTarget, list[dict[str, object]]]:
    if set(manifest) != _MANIFEST_KEYS:
        missing = sorted(_MANIFEST_KEYS.difference(manifest))
        extra = sorted(set(manifest).difference(_MANIFEST_KEYS))
        raise SignalBundleValidationError(
            f"Manifest contract is incompatible; missing={missing}, extra={extra}."
        )
    run_id = _required_string(manifest["run_id"], "run_id")
    if run_id != snapshot.path.name or _RUN_ID_PATTERN.fullmatch(run_id) is None:
        raise SignalBundleValidationError(
            "Manifest run ID does not match the selected directory."
        )
    created_at = _manifest_utc_instant(manifest["created_at"], "created_at")
    if created_at.strftime("%Y%m%dT%H%M%S%fZ") != run_id:
        raise SignalBundleValidationError(
            "Manifest creation instant does not reproduce the run ID."
        )
    captured_at = _manifest_utc_instant(
        manifest["captured_utc_reference_instant"],
        "captured_utc_reference_instant",
    )
    target_date = _manifest_date(manifest["target_xnys_session"], "target_xnys_session")
    request_start = _manifest_date(manifest["request_start"], "request_start")
    request_end = _manifest_date(
        manifest["request_end_exclusive"], "request_end_exclusive"
    )
    try:
        target = resolve_evaluation_target(captured_at)
    except ValueError as error:
        raise SignalBundleValidationError(
            "Manifest evaluation timing cannot be resolved."
        ) from error
    if (
        target.target_session.date() != target_date
        or target.request_start != request_start
        or target.request_end != request_end
    ):
        raise SignalBundleValidationError(
            "Manifest target and request bounds contradict the captured instant."
        )

    mode = _required_string(manifest["mode"], "mode")
    if mode not in {"offline", "refresh"}:
        raise SignalBundleValidationError("Manifest mode is unsupported.")
    if type(manifest["worktree_dirty"]) is not bool:
        raise SignalBundleValidationError("Manifest worktree_dirty must be boolean.")
    git_head = _required_string(manifest["git_head"], "git_head")
    if _GIT_HASH_PATTERN.fullmatch(git_head) is None:
        raise SignalBundleValidationError("Manifest Git HEAD is invalid.")
    arguments = _required_list(manifest["command_arguments"], "command_arguments")
    if any(type(argument) is not str for argument in arguments):
        raise SignalBundleValidationError(
            "Manifest command arguments must contain only strings."
        )
    versions = _required_mapping(manifest["package_versions"], "package_versions")
    if set(versions) != _PACKAGE_VERSION_KEYS or any(
        type(value) is not str or not value for value in versions.values()
    ):
        raise SignalBundleValidationError(
            "Manifest package versions do not match the supported contract."
        )
    universe_hash = _required_string(
        manifest["universe_config_sha256"], "universe_config_sha256"
    )
    if _SHA256_PATTERN.fullmatch(
        universe_hash
    ) is None or universe_hash != _universe_hash(universe):
        raise SignalBundleValidationError(
            "Bundle universe hash does not match the current configured universe."
        )
    input_sha = _required_string(manifest["input_sha256"], "input_sha256")
    if _SHA256_PATTERN.fullmatch(input_sha) is None:
        raise SignalBundleValidationError("Manifest input SHA-256 is invalid.")
    _required_nonnegative_int(manifest["input_row_count"], "input_row_count")

    artifacts = _required_mapping(manifest["artifacts"], "artifacts")
    if set(artifacts) != set(_ARTIFACT_FILENAMES):
        raise SignalBundleValidationError(
            "Manifest artifact population does not match the supported contract."
        )
    for artifact, filename in _ARTIFACT_FILENAMES.items():
        metadata = _required_mapping(artifacts[artifact], f"artifacts.{artifact}")
        if set(metadata) != {"filename", "row_count", "schema", "sha256"}:
            raise SignalBundleValidationError(
                f"Artifact metadata is incompatible for '{artifact}'."
            )
        if _required_string(metadata["filename"], "artifact filename") != filename:
            raise SignalBundleValidationError(
                f"Artifact filename is incompatible for '{artifact}'."
            )
        _required_nonnegative_int(metadata["row_count"], "artifact row_count")
        digest = _required_string(metadata["sha256"], "artifact sha256")
        if _SHA256_PATTERN.fullmatch(digest) is None:
            raise SignalBundleValidationError(
                f"Artifact SHA-256 is invalid for '{artifact}'."
            )
        if snapshot.sha256_by_filename[filename] != digest:
            raise SignalBundleIntegrityError(
                f"Artifact hash verification failed for {filename}."
            )
        _validate_schema_manifest(metadata["schema"], artifact)
    input_metadata = _required_mapping(artifacts["input_prices"], "input artifact")
    if (
        manifest["input_sha256"] != input_metadata["sha256"]
        or manifest["input_row_count"] != input_metadata["row_count"]
    ):
        raise SignalBundleValidationError(
            "Manifest input aliases disagree with input artifact metadata."
        )
    ticker_metadata = _validate_ticker_metadata(
        manifest,
        universe,
        mode=mode,
        request_start=request_start,
        request_end=request_end,
    )
    return target, ticker_metadata


def _read_artifact_tables(
    manifest: Mapping[str, object], snapshot: _BundleSnapshot
) -> dict[str, pa.Table]:
    artifacts = cast(Mapping[str, Mapping[str, object]], manifest["artifacts"])
    tables: dict[str, pa.Table] = {}
    for artifact, filename in _ARTIFACT_FILENAMES.items():
        try:
            table = pq.read_table(pa.BufferReader(snapshot.files[filename]))
            table.validate(full=True)
        except (pa.ArrowException, OSError, ValueError) as error:
            raise SignalBundleIntegrityError(
                f"Artifact is unreadable or truncated: {filename}."
            ) from error
        metadata = artifacts[artifact]
        if table.num_rows != metadata["row_count"]:
            raise SignalBundleValidationError(
                f"Artifact row count disagrees for {filename}."
            )
        if _schema_manifest(table.schema) != metadata["schema"]:
            raise SignalBundleValidationError(
                f"Artifact schema disagrees for {filename}."
            )
        tables[artifact] = table
    return tables


def _numeric_series(table: pa.Table, column: str) -> pd.Series:
    arrow_type = table.schema.field(column).type
    values = table[column].combine_chunks().to_pylist()
    if pa.types.is_integer(arrow_type):
        return pd.Series(values, dtype=pd.ArrowDtype(arrow_type), name=column)
    if pa.types.is_floating(arrow_type):
        pandas_dtype = "Float32" if arrow_type.bit_width <= 32 else "Float64"
        return pd.Series(pd.array(values, dtype=pandas_dtype), name=column)
    raise SignalBundleValidationError(
        f"Artifact numeric field '{column}' has unsupported type {arrow_type}."
    )


def _timestamp_series(table: pa.Table, column: str) -> pd.Series:
    values = table[column].combine_chunks().to_pandas()
    return pd.Series(values, name=column)


def _date_tuple_values(table: pa.Table, column: str) -> list[tuple[pd.Timestamp, ...]]:
    values = table[column].combine_chunks().to_pylist()
    return [
        tuple(
            pd.Timestamp(cast(str | date | datetime | np.datetime64, item))
            for item in cast(list[object], value)
        )
        for value in values
    ]


def _artifact_frame(artifact: ArtifactName, table: pa.Table) -> pd.DataFrame:
    if artifact == "input_prices":
        data: dict[str, object] = {
            "date": _timestamp_series(table, "date"),
            "ticker": pd.Series(table["ticker"].to_pylist(), dtype="string"),
        }
        for column in PRICE_VALUE_COLUMNS:
            data[column] = _numeric_series(table, column)
        data["retrieved_at"] = _timestamp_series(table, "retrieved_at")
        return pd.DataFrame(data, columns=CANONICAL_PRICE_COLUMNS)

    if artifact == "momentum":
        data = {}
        for column in MOMENTUM_OUTPUT_COLUMNS:
            if column in {"ticker", "simple_return_status", "endpoint_status"}:
                data[column] = pd.Series(table[column].to_pylist(), dtype="string")
            elif column in _MOMENTUM_DATE_COLUMNS:
                data[column] = _timestamp_series(table, column)
            elif column in {"start_adjusted_close", "end_adjusted_close"}:
                data[column] = _numeric_series(table, column)
            elif column in _MOMENTUM_FLOAT_COLUMNS:
                data[column] = pd.array(table[column].to_pylist(), dtype="Float64")
            elif column in _MOMENTUM_COUNT_COLUMNS:
                data[column] = pd.array(table[column].to_pylist(), dtype="Int64")
            elif column == "endpoint_eligible":
                data[column] = pd.array(table[column].to_pylist(), dtype="boolean")
            else:
                data[column] = _date_tuple_values(table, column)
        return pd.DataFrame(data, columns=MOMENTUM_OUTPUT_COLUMNS)

    if artifact == "volatility":
        data = {}
        for column in VOLATILITY_OUTPUT_COLUMNS:
            if column in {"ticker", "window_status"}:
                data[column] = pd.Series(table[column].to_pylist(), dtype="string")
            elif column in _VOLATILITY_DATE_COLUMNS:
                data[column] = _timestamp_series(table, column)
            elif column in _VOLATILITY_FLOAT_COLUMNS:
                data[column] = pd.array(table[column].to_pylist(), dtype="Float64")
            elif column in _VOLATILITY_COUNT_COLUMNS:
                data[column] = pd.array(table[column].to_pylist(), dtype="Int64")
            elif column == "window_eligible":
                data[column] = pd.array(table[column].to_pylist(), dtype="boolean")
            else:
                data[column] = _date_tuple_values(table, column)
        return pd.DataFrame(data, columns=VOLATILITY_OUTPUT_COLUMNS)

    frame = table.to_pandas()
    if artifact == "coverage":
        frame["missing_canonical_dates"] = _date_tuple_values(
            table, "missing_canonical_dates"
        )
        frame["missing_adjusted_close_dates"] = _date_tuple_values(
            table, "missing_adjusted_close_dates"
        )
        return cast(pd.DataFrame, frame.loc[:, list(COVERAGE_COLUMNS)].copy())
    frame["included_tickers"] = [
        tuple(cast(list[str], value))
        for value in table["included_tickers"].combine_chunks().to_pylist()
    ]
    return cast(pd.DataFrame, frame.loc[:, list(DEPENDENCE_COLUMNS)].copy())


def _expected_table(artifact: ArtifactName, frame: pd.DataFrame) -> pa.Table:
    builders = {
        "input_prices": _input_price_table,
        "coverage": _coverage_table,
        "momentum": _momentum_table,
        "volatility": _volatility_table,
        "dependence": _dependence_table,
    }
    return builders[artifact](frame)


def _validate_frame_round_trip(
    artifact: ArtifactName, frame: pd.DataFrame, table: pa.Table
) -> None:
    try:
        expected = _expected_table(artifact, frame)
    except ValueError as error:
        raise SignalBundleValidationError(
            f"Artifact '{artifact}' violates its pandas/Arrow contract."
        ) from error
    if not expected.equals(table, check_metadata=True):
        raise SignalBundleValidationError(
            f"Artifact '{artifact}' cannot be represented losslessly."
        )
    _validate_table_semantics(frame, table, artifact=artifact)


def _calendar_sessions(target: EvaluationTarget) -> pd.DatetimeIndex:
    calendar = xcals.get_calendar(
        "XNYS",
        start=pd.Timestamp(target.request_start) - timedelta(days=6 * 366),
        end=target.target_session + timedelta(days=370),
    )
    return pd.DatetimeIndex(calendar.sessions)


def _validate_signal_key_population(
    frame: pd.DataFrame,
    prices: pd.DataFrame,
    target: EvaluationTarget,
    *,
    component: str,
) -> None:
    input_tickers = sorted(prices["ticker"].astype(str).unique().tolist())
    first_date = pd.Timestamp(prices["date"].min())
    sessions = _evaluation_sessions(target)
    signal_sessions = sessions[sessions >= first_date]
    expected_tickers = np.repeat(input_tickers, len(signal_sessions))
    expected_dates = np.tile(signal_sessions.to_numpy(), len(input_tickers))
    if (
        len(frame) != len(expected_tickers)
        or not np.array_equal(frame["ticker"].astype(str), expected_tickers)
        or not np.array_equal(
            frame["signal_date"].to_numpy(dtype="datetime64[ns]"), expected_dates
        )
    ):
        raise SignalBundleValidationError(
            f"{component} keys do not cover the exact input ticker/session product."
        )


def _validate_momentum_output(
    momentum: pd.DataFrame, calendar_sessions: pd.DatetimeIndex
) -> None:
    if tuple(momentum.columns) != MOMENTUM_OUTPUT_COLUMNS or momentum.empty:
        raise SignalBundleValidationError(
            "Momentum output schema or population is invalid."
        )
    if not momentum.index.equals(pd.RangeIndex(len(momentum))):
        raise SignalBundleValidationError("Momentum output index is not deterministic.")
    expected = momentum.sort_values(
        ["ticker", "signal_date"], kind="mergesort"
    ).reset_index(drop=True)
    if (
        not momentum.equals(expected)
        or momentum.duplicated(["ticker", "signal_date"]).any()
    ):
        raise SignalBundleValidationError("Momentum keys are duplicated or unsorted.")
    _validate_present_finite_columns(
        momentum,
        (
            "start_adjusted_close",
            "end_adjusted_close",
            *_MOMENTUM_FLOAT_COLUMNS,
            *_MOMENTUM_COUNT_COLUMNS,
        ),
        component="Momentum bundle",
    )
    if any(momentum[column].dropna().lt(0).any() for column in _MOMENTUM_COUNT_COLUMNS):
        raise SignalBundleValidationError(
            "Momentum diagnostic counts must be nonnegative."
        )
    if (
        momentum["normalization_reference_count"]
        .gt(MOMENTUM_NORMALIZATION_WINDOW_SESSIONS - 1)
        .any()
    ):
        raise SignalBundleValidationError(
            "Momentum normalization reference count exceeds its window."
        )
    percentiles = momentum["momentum_percentile"].dropna()
    if percentiles.lt(0).any() or percentiles.gt(100).any():
        raise SignalBundleValidationError("Momentum percentiles must lie in [0, 100].")
    if momentum["simple_return_pct"].dropna().le(-100).any():
        raise SignalBundleValidationError(
            "Momentum simple-return percentages must exceed -100."
        )

    positions = {
        session: position for position, session in enumerate(calendar_sessions)
    }
    allowed_issues = {
        "missing_start_row",
        "missing_start_adjusted_close",
        "missing_end_row",
        "missing_end_adjusted_close",
    }
    for row in momentum.itertuples(index=False):
        signal_date = cast(pd.Timestamp, row.signal_date)
        position = positions.get(signal_date)
        if position is None or position < MOMENTUM_LAG_SESSIONS:
            raise SignalBundleValidationError(
                "Momentum signal date is not valid on XNYS."
            )
        endpoint_start = cast(pd.Timestamp, row.endpoint_start_date)
        if (
            row.endpoint_end_date != signal_date
            or endpoint_start != calendar_sessions[position - MOMENTUM_LAG_SESSIONS]
            or row.first_prospective_session != calendar_sessions[position + 1]
        ):
            raise SignalBundleValidationError(
                "Momentum endpoint or prospective-session dates are inconsistent."
            )
        status = str(row.endpoint_status)
        eligible = bool(row.endpoint_eligible)
        issues = set(status.split("|")) if status != "eligible" else set()
        if eligible != (status == "eligible") or not issues.issubset(allowed_issues):
            raise SignalBundleValidationError(
                "Momentum endpoint eligibility and status are inconsistent."
            )
        if {"missing_start_row", "missing_start_adjusted_close"}.issubset(issues) or {
            "missing_end_row",
            "missing_end_adjusted_close",
        }.issubset(issues):
            raise SignalBundleValidationError(
                "Momentum endpoint status is contradictory."
            )
        raw_present = pd.notna(row.raw_momentum)
        percentile_present = pd.notna(row.momentum_percentile)
        simple_present = pd.notna(row.simple_return_pct)
        reference_count = int(cast(int | np.integer, row.normalization_reference_count))
        if eligible:
            if (
                not raw_present
                or pd.isna(row.start_adjusted_close)
                or pd.isna(row.end_adjusted_close)
            ):
                raise SignalBundleValidationError(
                    "Eligible Momentum output is missing endpoint or raw values."
                )
            if row.simple_return_status == "available" and not simple_present:
                raise SignalBundleValidationError(
                    "Available Momentum display value is missing."
                )
            if row.simple_return_status == "exceeds_float64_range" and simple_present:
                raise SignalBundleValidationError(
                    "Out-of-range Momentum display value must remain missing."
                )
            if row.simple_return_status not in {
                "available",
                "exceeds_float64_range",
            }:
                raise SignalBundleValidationError(
                    "Eligible Momentum simple-return status is invalid."
                )
        elif (
            raw_present
            or percentile_present
            or simple_present
            or (row.simple_return_status != "endpoint_ineligible")
        ):
            raise SignalBundleValidationError(
                "Ineligible Momentum output contains a derived value."
            )
        expected_percentile = (
            eligible and reference_count >= MOMENTUM_MINIMUM_PRIOR_OBSERVATIONS
        )
        if percentile_present != expected_percentile:
            raise SignalBundleValidationError(
                "Momentum percentile presence contradicts eligibility/history."
            )
        missing_rows = cast(tuple[pd.Timestamp, ...], row.interior_missing_row_dates)
        missing_adjusted = cast(
            tuple[pd.Timestamp, ...], row.interior_missing_adjusted_close_dates
        )
        if (
            len(missing_rows)
            != int(cast(int | np.integer, row.interior_missing_row_count))
            or len(missing_adjusted)
            != int(
                cast(
                    int | np.integer,
                    row.interior_missing_adjusted_close_count,
                )
            )
            or tuple(sorted(set(missing_rows))) != missing_rows
            or tuple(sorted(set(missing_adjusted))) != missing_adjusted
            or set(missing_rows).intersection(missing_adjusted)
            or any(not endpoint_start < value < signal_date for value in missing_rows)
            or any(
                not endpoint_start < value < signal_date for value in missing_adjusted
            )
        ):
            raise SignalBundleValidationError(
                "Momentum missingness diagnostics are inconsistent."
            )


def _scalar_is_missing(value: object) -> bool:
    return (
        value is None
        or value is pd.NA
        or value is pd.NaT
        or (isinstance(value, (float, np.floating)) and bool(np.isnan(value)))
    )


def _scalar_equal(left: object, right: object) -> bool:
    if _scalar_is_missing(left) and _scalar_is_missing(right):
        return True
    return bool(left == right)


def _expected_optional_float(value: object) -> float | None:
    return None if _scalar_is_missing(value) else float(cast(float, value))


def _expected_staleness(
    latest: pd.Timestamp | None, sessions: pd.DatetimeIndex
) -> int | None:
    if latest is None:
        return None
    position = int(sessions.searchsorted(latest))
    if position >= len(sessions) or sessions[position] != latest:
        raise SignalBundleValidationError("Staleness date is not an XNYS session.")
    return len(sessions) - 1 - position


def _first_last_present(
    frame: pd.DataFrame, value_column: str
) -> tuple[pd.Timestamp | None, pd.Timestamp | None]:
    dates = frame.loc[frame[value_column].notna(), "signal_date"]
    if dates.empty:
        return None, None
    return pd.Timestamp(dates.iloc[0]), pd.Timestamp(dates.iloc[-1])


def _validate_signal_input_diagnostics(
    prices: pd.DataFrame,
    momentum: pd.DataFrame,
    volatility: pd.DataFrame,
    calendar_sessions: pd.DatetimeIndex,
) -> None:
    calendar_positions = {
        session: position for position, session in enumerate(calendar_sessions)
    }
    price_groups = {
        str(ticker): group.set_index("date", drop=False)
        for ticker, group in prices.groupby("ticker", sort=False, observed=True)
    }
    for ticker, ticker_momentum in momentum.groupby(
        "ticker", sort=False, observed=True
    ):
        price_group = price_groups[str(ticker)]
        present_positions = np.array(
            [calendar_positions[pd.Timestamp(value)] for value in price_group.index],
            dtype=np.int64,
        )
        adjusted_missing_positions = np.array(
            [
                calendar_positions[pd.Timestamp(value)]
                for value in price_group.loc[price_group["adjusted_close"].isna()].index
            ],
            dtype=np.int64,
        )
        all_positions = np.arange(len(calendar_sessions), dtype=np.int64)
        missing_positions = np.setdiff1d(
            all_positions, present_positions, assume_unique=False
        )
        for row in ticker_momentum.itertuples(index=False):
            start = cast(pd.Timestamp, row.endpoint_start_date)
            end = cast(pd.Timestamp, row.endpoint_end_date)
            start_present = start in price_group.index
            end_present = end in price_group.index
            start_value = (
                price_group.at[start, "adjusted_close"] if start_present else pd.NA
            )
            end_value = price_group.at[end, "adjusted_close"] if end_present else pd.NA
            issues: list[str] = []
            if not start_present:
                issues.append("missing_start_row")
            elif pd.isna(start_value):
                issues.append("missing_start_adjusted_close")
            if not end_present:
                issues.append("missing_end_row")
            elif pd.isna(end_value):
                issues.append("missing_end_adjusted_close")
            expected_status = "eligible" if not issues else "|".join(issues)
            if (
                str(row.endpoint_status) != expected_status
                or not _scalar_equal(row.start_adjusted_close, start_value)
                or not _scalar_equal(row.end_adjusted_close, end_value)
            ):
                raise SignalBundleValidationError(
                    f"Momentum endpoint diagnostics disagree with input for {ticker}."
                )
            start_position = calendar_positions[start]
            end_position = calendar_positions[end]
            first = int(np.searchsorted(missing_positions, start_position + 1))
            stop = int(np.searchsorted(missing_positions, end_position))
            expected_missing = tuple(
                calendar_sessions.take(missing_positions[first:stop])
            )
            first = int(np.searchsorted(adjusted_missing_positions, start_position + 1))
            stop = int(np.searchsorted(adjusted_missing_positions, end_position))
            expected_adjusted = tuple(
                calendar_sessions.take(adjusted_missing_positions[first:stop])
            )
            if row.interior_missing_row_dates != expected_missing or (
                row.interior_missing_adjusted_close_dates != expected_adjusted
            ):
                raise SignalBundleValidationError(
                    f"Momentum interior diagnostics disagree with input for {ticker}."
                )

    for ticker, ticker_volatility in volatility.groupby(
        "ticker", sort=False, observed=True
    ):
        price_group = price_groups[str(ticker)]
        present_positions = np.array(
            [calendar_positions[pd.Timestamp(value)] for value in price_group.index],
            dtype=np.int64,
        )
        adjusted_missing_positions = np.array(
            [
                calendar_positions[pd.Timestamp(value)]
                for value in price_group.loc[price_group["adjusted_close"].isna()].index
            ],
            dtype=np.int64,
        )
        missing_positions = np.setdiff1d(
            np.arange(len(calendar_sessions), dtype=np.int64),
            present_positions,
            assume_unique=False,
        )
        for row in ticker_volatility.itertuples(index=False):
            start_position = calendar_positions[
                cast(pd.Timestamp, row.window_start_date)
            ]
            end_position = calendar_positions[cast(pd.Timestamp, row.window_end_date)]
            first = int(np.searchsorted(missing_positions, start_position))
            stop = int(np.searchsorted(missing_positions, end_position, side="right"))
            expected_missing = tuple(
                calendar_sessions.take(missing_positions[first:stop])
            )
            first = int(np.searchsorted(adjusted_missing_positions, start_position))
            stop = int(
                np.searchsorted(adjusted_missing_positions, end_position, side="right")
            )
            expected_adjusted = tuple(
                calendar_sessions.take(adjusted_missing_positions[first:stop])
            )
            if row.missing_row_dates != expected_missing or (
                row.missing_adjusted_close_dates != expected_adjusted
            ):
                raise SignalBundleValidationError(
                    f"Volatility window diagnostics disagree with input for {ticker}."
                )


def _validate_coverage_relationships(
    coverage: pd.DataFrame,
    prices: pd.DataFrame,
    momentum: pd.DataFrame,
    volatility: pd.DataFrame,
    universe: tuple[ETFDefinition, ...],
    target: EvaluationTarget,
    ticker_metadata: Sequence[Mapping[str, object]],
) -> None:
    sessions = _evaluation_sessions(target)
    _validate_coverage(coverage, universe, len(sessions))
    _validate_present_finite_columns(
        coverage, _COVERAGE_NUMERIC_COLUMNS, component="Coverage bundle"
    )
    momentum_groups = {
        str(ticker): group.reset_index(drop=True)
        for ticker, group in momentum.groupby("ticker", sort=False, observed=True)
    }
    volatility_groups = {
        str(ticker): group.reset_index(drop=True)
        for ticker, group in volatility.groupby("ticker", sort=False, observed=True)
    }
    for definition, metadata, row in zip(
        universe, ticker_metadata, coverage.itertuples(index=False), strict=True
    ):
        ticker = definition.ticker
        ticker_prices = prices.loc[prices["ticker"].astype(str).eq(ticker)]
        present_dates = pd.DatetimeIndex(ticker_prices["date"])
        missing_dates = tuple(sessions.difference(present_dates))
        missing_adjusted_dates = tuple(
            pd.Timestamp(value)
            for value in ticker_prices.loc[
                ticker_prices["adjusted_close"].isna(), "date"
            ]
        )
        adjusted_dates = ticker_prices.loc[
            ticker_prices["adjusted_close"].notna(), "date"
        ]
        first_date = None if ticker_prices.empty else pd.Timestamp(present_dates.min())
        last_date = None if ticker_prices.empty else pd.Timestamp(present_dates.max())
        first_adjusted = (
            None if adjusted_dates.empty else pd.Timestamp(adjusted_dates.iloc[0])
        )
        last_adjusted = (
            None if adjusted_dates.empty else pd.Timestamp(adjusted_dates.iloc[-1])
        )
        target_rows = ticker_prices.loc[ticker_prices["date"].eq(target.target_session)]
        target_price_present = len(target_rows) == 1
        target_adjusted_present = target_price_present and pd.notna(
            target_rows.iloc[0]["adjusted_close"]
        )
        expected_price_staleness = _expected_staleness(last_adjusted, sessions)
        retrieved_first = (
            None
            if ticker_prices.empty
            else pd.Timestamp(ticker_prices["retrieved_at"].min())
        )
        retrieved_last = (
            None
            if ticker_prices.empty
            else pd.Timestamp(ticker_prices["retrieved_at"].max())
        )
        exact_values = {
            "ticker": ticker,
            "name": definition.name,
            "category": definition.category,
            "acquisition_status": metadata["acquisition_status"],
            "acquisition_error": metadata["error"],
            "acquisition_rows_received": metadata["rows_received"],
            "request_start": pd.Timestamp(target.request_start),
            "request_end_exclusive": pd.Timestamp(target.request_end),
            "target_session": target.target_session,
            "first_canonical_date": first_date,
            "last_canonical_date": last_date,
            "expected_xnys_observation_count": len(sessions),
            "present_xnys_observation_count": len(ticker_prices),
            "missing_canonical_count": len(missing_dates),
            "missing_canonical_dates": missing_dates,
            "missing_adjusted_close_count": len(missing_adjusted_dates),
            "missing_adjusted_close_dates": missing_adjusted_dates,
            "first_adjusted_close_date": first_adjusted,
            "last_adjusted_close_date": last_adjusted,
            "target_price_row_present": target_price_present,
            "target_adjusted_close_present": target_adjusted_present,
            "price_staleness_sessions": expected_price_staleness,
        }
        for field, expected in exact_values.items():
            if not _scalar_equal(getattr(row, field), expected):
                raise SignalBundleValidationError(
                    f"Coverage field '{field}' is inconsistent for {ticker}."
                )
        for field, expected in (
            ("input_first_retrieved_at", retrieved_first),
            ("input_last_retrieved_at", retrieved_last),
        ):
            if not _scalar_equal(getattr(row, field), expected) or not _scalar_equal(
                metadata[field],
                None
                if expected is None
                else expected.isoformat().replace("+00:00", "Z"),
            ):
                raise SignalBundleValidationError(
                    f"Coverage retrieval field '{field}' is inconsistent for {ticker}."
                )
        expected_acquisition_instant = metadata["retrieved_at"]
        actual_acquisition_instant = getattr(row, "acquisition_retrieved_at")
        if expected_acquisition_instant is None:
            if not pd.isna(actual_acquisition_instant):
                raise SignalBundleValidationError(
                    f"Acquisition retrieval time is inconsistent for {ticker}."
                )
        elif (
            pd.Timestamp(cast(str, expected_acquisition_instant))
            != actual_acquisition_instant
        ):
            raise SignalBundleValidationError(
                f"Acquisition retrieval time is inconsistent for {ticker}."
            )
        if cast(int, metadata["input_rows"]) != len(ticker_prices):
            raise SignalBundleValidationError(
                f"Manifest input row count is inconsistent for {ticker}."
            )

        ticker_momentum = momentum_groups.get(ticker)
        ticker_volatility = volatility_groups.get(ticker)
        momentum_target = (
            None
            if ticker_momentum is None
            else ticker_momentum.loc[
                ticker_momentum["signal_date"].eq(target.target_session)
            ].iloc[0]
        )
        volatility_target = (
            None
            if ticker_volatility is None
            else ticker_volatility.loc[
                ticker_volatility["signal_date"].eq(target.target_session)
            ].iloc[0]
        )
        momentum_first_raw, momentum_last_raw = (
            (None, None)
            if ticker_momentum is None
            else _first_last_present(ticker_momentum, "raw_momentum")
        )
        momentum_first_norm, momentum_last_norm = (
            (None, None)
            if ticker_momentum is None
            else _first_last_present(ticker_momentum, "momentum_percentile")
        )
        volatility_first_raw, volatility_last_raw = (
            (None, None)
            if ticker_volatility is None
            else _first_last_present(ticker_volatility, "raw_annualized_volatility")
        )
        volatility_first_norm, volatility_last_norm = (
            (None, None)
            if ticker_volatility is None
            else _first_last_present(ticker_volatility, "volatility_percentile")
        )
        signal_values: dict[str, object] = {
            "momentum_first_raw_date": momentum_first_raw,
            "momentum_last_raw_date": momentum_last_raw,
            "momentum_first_normalized_date": momentum_first_norm,
            "momentum_last_normalized_date": momentum_last_norm,
            "momentum_target_raw_eligible": (
                momentum_target is not None
                and pd.notna(momentum_target["raw_momentum"])
            ),
            "momentum_target_normalized_eligible": (
                momentum_target is not None
                and pd.notna(momentum_target["momentum_percentile"])
            ),
            "momentum_target_raw": (
                None
                if momentum_target is None
                else _expected_optional_float(momentum_target["raw_momentum"])
            ),
            "momentum_target_simple_return_pct": (
                None
                if momentum_target is None
                else _expected_optional_float(momentum_target["simple_return_pct"])
            ),
            "momentum_target_percentile": (
                None
                if momentum_target is None
                else _expected_optional_float(momentum_target["momentum_percentile"])
            ),
            "momentum_target_status": (
                "ticker_unavailable"
                if momentum_target is None
                else str(momentum_target["endpoint_status"])
            ),
            "momentum_target_normalization_status": (
                "ticker_unavailable"
                if momentum_target is None
                else "raw_ineligible"
                if pd.isna(momentum_target["raw_momentum"])
                else "insufficient_reference_history"
                if pd.isna(momentum_target["momentum_percentile"])
                else "eligible"
            ),
            "momentum_target_reference_count": (
                None
                if momentum_target is None
                else int(momentum_target["normalization_reference_count"])
            ),
            "momentum_raw_staleness_sessions": _expected_staleness(
                momentum_last_raw, sessions
            ),
            "momentum_normalized_staleness_sessions": _expected_staleness(
                momentum_last_norm, sessions
            ),
            "volatility_first_raw_date": volatility_first_raw,
            "volatility_last_raw_date": volatility_last_raw,
            "volatility_first_normalized_date": volatility_first_norm,
            "volatility_last_normalized_date": volatility_last_norm,
            "volatility_target_raw_eligible": (
                volatility_target is not None
                and pd.notna(volatility_target["raw_annualized_volatility"])
            ),
            "volatility_target_normalized_eligible": (
                volatility_target is not None
                and pd.notna(volatility_target["volatility_percentile"])
            ),
            "volatility_target_raw": (
                None
                if volatility_target is None
                else _expected_optional_float(
                    volatility_target["raw_annualized_volatility"]
                )
            ),
            "volatility_target_annualized_pct": (
                None
                if volatility_target is None
                else _expected_optional_float(
                    volatility_target["annualized_volatility_pct"]
                )
            ),
            "volatility_target_percentile": (
                None
                if volatility_target is None
                else _expected_optional_float(
                    volatility_target["volatility_percentile"]
                )
            ),
            "volatility_target_status": (
                "ticker_unavailable"
                if volatility_target is None
                else str(volatility_target["window_status"])
            ),
            "volatility_target_normalization_status": (
                "ticker_unavailable"
                if volatility_target is None
                else "raw_ineligible"
                if pd.isna(volatility_target["raw_annualized_volatility"])
                else "insufficient_reference_history"
                if pd.isna(volatility_target["volatility_percentile"])
                else "eligible"
            ),
            "volatility_target_reference_count": (
                None
                if volatility_target is None
                else int(volatility_target["normalization_reference_count"])
            ),
            "volatility_raw_staleness_sessions": _expected_staleness(
                volatility_last_raw, sessions
            ),
            "volatility_normalized_staleness_sessions": _expected_staleness(
                volatility_last_norm, sessions
            ),
        }
        for field, expected in signal_values.items():
            if not _scalar_equal(getattr(row, field), expected):
                raise SignalBundleValidationError(
                    f"Coverage signal field '{field}' is inconsistent for {ticker}."
                )


def _validate_acquisition_provenance(
    prices: pd.DataFrame,
    ticker_metadata: Sequence[Mapping[str, object]],
) -> None:
    """Reconcile successful and empty claims with stored source vintages."""

    for metadata in ticker_metadata:
        status = cast(str, metadata["acquisition_status"])
        if status not in {"success", "empty"}:
            continue

        ticker = cast(str, metadata["ticker"])
        query_start = _manifest_date(
            metadata["query_start"], "ticker_metadata.query_start"
        )
        query_end = _manifest_date(
            metadata["query_end_exclusive"],
            "ticker_metadata.query_end_exclusive",
        )
        retrieved_at = _manifest_utc_instant(
            metadata["retrieved_at"], "ticker_metadata.retrieved_at"
        )
        ticker_rows = prices.loc[prices["ticker"].astype(str).eq(ticker)]
        source_vintage_rows = ticker_rows.loc[
            ticker_rows["retrieved_at"].eq(retrieved_at)
        ]
        inside_window = source_vintage_rows["date"].ge(
            pd.Timestamp(query_start)
        ) & source_vintage_rows["date"].lt(pd.Timestamp(query_end))
        outside_rows = source_vintage_rows.loc[~inside_window]
        if not outside_rows.empty:
            raise SignalBundleValidationError(
                f"{status.capitalize()} acquisition provenance for {ticker} "
                f"claims canonical rows at {retrieved_at.isoformat()} outside "
                f"[{query_start.isoformat()}, {query_end.isoformat()})."
            )

        acquired_rows = source_vintage_rows.loc[inside_window]
        if status == "empty":
            if not acquired_rows.empty:
                raise SignalBundleValidationError(
                    f"Empty acquisition provenance for {ticker} claims zero "
                    f"canonical rows at {retrieved_at.isoformat()} inside "
                    f"[{query_start.isoformat()}, {query_end.isoformat()}), "
                    f"but {len(acquired_rows)} matching rows exist."
                )
            continue

        expected_count = cast(int, metadata["rows_received"])
        if len(acquired_rows) != expected_count:
            raise SignalBundleValidationError(
                f"Successful acquisition provenance for {ticker} claims "
                f"{expected_count} canonical rows at {retrieved_at.isoformat()} "
                f"inside [{query_start.isoformat()}, {query_end.isoformat()}), "
                f"but {len(acquired_rows)} matching rows exist."
            )

        expected_first = _manifest_date(
            metadata["first_returned_date"],
            "ticker_metadata.first_returned_date",
        )
        expected_last = _manifest_date(
            metadata["last_returned_date"],
            "ticker_metadata.last_returned_date",
        )
        actual_first = pd.Timestamp(acquired_rows["date"].min()).date()
        actual_last = pd.Timestamp(acquired_rows["date"].max()).date()
        if actual_first != expected_first:
            raise SignalBundleValidationError(
                f"Successful acquisition first returned date for {ticker} "
                "disagrees with the canonical source-vintage rows."
            )
        if actual_last != expected_last:
            raise SignalBundleValidationError(
                f"Successful acquisition last returned date for {ticker} "
                "disagrees with the canonical source-vintage rows."
            )


def _validate_bundle_semantics(
    tables: Mapping[str, pa.Table],
    universe: tuple[ETFDefinition, ...],
    target: EvaluationTarget,
    ticker_metadata: Sequence[Mapping[str, object]],
) -> dict[str, pd.DataFrame]:
    frames: dict[str, pd.DataFrame] = {}
    for artifact in _ARTIFACT_FILENAMES:
        typed_artifact = cast(ArtifactName, artifact)
        frame = _artifact_frame(typed_artifact, tables[artifact])
        _validate_frame_round_trip(typed_artifact, frame, tables[artifact])
        frames[artifact] = frame

    prices = frames["input_prices"]
    coverage = frames["coverage"]
    momentum = frames["momentum"]
    volatility = frames["volatility"]
    dependence = frames["dependence"]
    try:
        _validate_acquisition_provenance(prices, ticker_metadata)
        _validate_canonical_evaluation_input(prices, universe, target)
        _validate_signal_key_population(momentum, prices, target, component="Momentum")
        _validate_signal_key_population(
            volatility, prices, target, component="Volatility"
        )
        calendar_sessions = _calendar_sessions(target)
        _validate_momentum_output(momentum, calendar_sessions)
        _validate_volatility_output(volatility, calendar_sessions=calendar_sessions)
        _validate_signal_input_diagnostics(
            prices, momentum, volatility, calendar_sessions
        )
        _validate_coverage_relationships(
            coverage,
            prices,
            momentum,
            volatility,
            universe,
            target,
            ticker_metadata,
        )
        sessions = _evaluation_sessions(target)
        _validate_dependence(
            dependence,
            [definition.ticker for definition in universe],
            sessions,
        )
        expected_dependence = calculate_dependence_diagnostics(
            momentum, volatility, universe, sessions
        )
        expected_dependence = _artifact_frame(
            "dependence", _dependence_table(expected_dependence)
        )
        _assert_frame_exact(
            dependence, expected_dependence, artifact="Dependence output"
        )
    except SignalBundleError:
        raise
    except (
        AssertionError,
        KeyError,
        TypeError,
        ValueError,
        VolatilityDataValidationError,
    ) as error:
        raise SignalBundleValidationError(
            "Bundle artifacts violate the supported semantic contract."
        ) from error
    return frames


def _deep_freeze(value: object) -> object:
    if isinstance(value, dict):
        return MappingProxyType(
            {str(key): _deep_freeze(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return tuple(_deep_freeze(item) for item in value)
    return value


def load_signal_evaluation_bundle(
    output_root: Path,
    run_id: str,
    *,
    previous: VerifiedSignalEvaluationBundle | None = None,
) -> VerifiedSignalEvaluationBundle:
    """Load and fully verify one existing local signal-evaluation bundle.

    The loader snapshots exact bytes, hashes before parsing, validates the same
    bytes, and never calls signal calculators, evaluation orchestration,
    providers, persistence, repair, or quarantine operations. A prior verified
    object is reused only after a new six-file snapshot has the same combined
    digest and exact selected path.

    Args:
        output_root: Parent directory containing non-overwriting run children.
        run_id: Explicitly selected direct-child UTC run identifier.
        previous: Optional earlier verified object eligible for digest reuse.

    Returns:
        An immutable verified bundle backed by immutable Arrow tables.

    Raises:
        SignalBundleSelectionError: If the selected path is unsafe or missing.
        SignalBundleIntegrityError: If files are missing, changed, or unreadable.
        SignalBundleValidationError: If the existing format or semantics fail.
    """

    snapshot = _snapshot_bundle(output_root, run_id)
    if (
        previous is not None
        and previous.run_id == run_id
        and previous.bundle_path == snapshot.path
        and previous.content_sha256 == snapshot.content_sha256
    ):
        return previous

    manifest = _parse_manifest(snapshot.files["manifest.json"])
    universe = load_etf_universe()
    try:
        target, ticker_metadata = _validate_manifest_contract(
            manifest, snapshot, universe
        )
        tables = _read_artifact_tables(manifest, snapshot)
        _validate_bundle_semantics(tables, universe, target, ticker_metadata)
    except SignalBundleError:
        raise
    except (KeyError, TypeError, ValueError) as error:
        raise SignalBundleValidationError(
            "Bundle verification encountered incompatible structured data."
        ) from error

    artifact_hashes: dict[str, str] = {
        artifact: snapshot.sha256_by_filename[filename]
        for artifact, filename in _ARTIFACT_FILENAMES.items()
    }
    frozen_manifest = cast(Mapping[str, object], _deep_freeze(manifest))
    return VerifiedSignalEvaluationBundle(
        run_id=run_id,
        bundle_path=snapshot.path,
        content_sha256=snapshot.content_sha256,
        manifest_sha256=snapshot.sha256_by_filename["manifest.json"],
        manifest=frozen_manifest,
        artifact_sha256=MappingProxyType(artifact_hashes),
        tables=MappingProxyType(dict(tables)),
        universe=universe,
        target=target,
    )
