"""Deterministic, non-destructive retention planning."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final

import duckdb

from .archive import ARCHIVE_SCHEMA, DATASETS
from .config import RetentionConfig


class RetentionTableClass(StrEnum):
    """The single retention classification assigned to an application table."""

    PRUNABLE_HISTORY = "PRUNABLE_HISTORY"
    CURRENT_PROJECTION = "CURRENT_PROJECTION"
    CONTROL_STATE = "CONTROL_STATE"
    ARCHIVE_CONTROL = "ARCHIVE_CONTROL"
    AUDIT = "AUDIT"


_TABLE_CLASSIFICATIONS = {
    "schema_migrations": RetentionTableClass.AUDIT,
    "raw_events": RetentionTableClass.PRUNABLE_HISTORY,
    "events": RetentionTableClass.PRUNABLE_HISTORY,
    "collector_health": RetentionTableClass.CONTROL_STATE,
    "app_server_state": RetentionTableClass.CONTROL_STATE,
    "app_server_messages": RetentionTableClass.PRUNABLE_HISTORY,
    "threads": RetentionTableClass.CURRENT_PROJECTION,
    "turns": RetentionTableClass.CURRENT_PROJECTION,
    "thread_items": RetentionTableClass.CURRENT_PROJECTION,
    "app_server_token_usage": RetentionTableClass.CURRENT_PROJECTION,
    "hook_events": RetentionTableClass.PRUNABLE_HISTORY,
    "hook_source_state": RetentionTableClass.CONTROL_STATE,
    "correlation_edges": RetentionTableClass.PRUNABLE_HISTORY,
    "repositories": RetentionTableClass.CURRENT_PROJECTION,
    "worktrees": RetentionTableClass.CURRENT_PROJECTION,
    "git_snapshots": RetentionTableClass.PRUNABLE_HISTORY,
    "git_snapshot_paths": RetentionTableClass.PRUNABLE_HISTORY,
    "git_snapshot_correlations": RetentionTableClass.PRUNABLE_HISTORY,
    "git_health": RetentionTableClass.CONTROL_STATE,
    "archive_batches": RetentionTableClass.ARCHIVE_CONTROL,
    "archive_files": RetentionTableClass.ARCHIVE_CONTROL,
    "archive_watermarks": RetentionTableClass.ARCHIVE_CONTROL,
    "archive_health": RetentionTableClass.ARCHIVE_CONTROL,
    "analytics_health": RetentionTableClass.CONTROL_STATE,
    "retention_runs": RetentionTableClass.AUDIT,
    "retention_run_tables": RetentionTableClass.AUDIT,
    "retention_run_candidates": RetentionTableClass.AUDIT,
}

TABLE_CLASSIFICATIONS: Final[Mapping[str, RetentionTableClass]] = MappingProxyType(_TABLE_CLASSIFICATIONS)
PRUNABLE_HISTORY_TABLES: Final[frozenset[str]] = frozenset(
    table for table, classification in TABLE_CLASSIFICATIONS.items()
    if classification is RetentionTableClass.PRUNABLE_HISTORY
)
PRUNABLE_HISTORY_ORDER: Final[tuple[str, ...]] = (
    "git_snapshot_correlations", "git_snapshot_paths", "git_snapshots",
    "correlation_edges", "hook_events", "app_server_messages", "raw_events", "events",
)
PRUNABLE_TIMESTAMP_COLUMNS: Final[Mapping[str, str]] = MappingProxyType({
    "app_server_messages": "received_at", "correlation_edges": "created_at",
    "events": "event_time", "git_snapshot_correlations": "created_at",
    "git_snapshot_paths": "parent git_snapshots.captured_at", "git_snapshots": "captured_at",
    "hook_events": "received_at", "raw_events": "received_at",
})

# SQL and identity expressions are fixed application code, never configuration input.
_SELECTIONS: Final[Mapping[str, tuple[str, str, str]]] = MappingProxyType({
    "app_server_messages": ("SELECT message_id AS identity, received_at AS timestamp, NULL AS retention_class FROM app_server_messages ORDER BY message_id", "message_id", "hot"),
    "correlation_edges": ("SELECT CAST(edge_id AS TEXT) AS identity, created_at AS timestamp, NULL AS retention_class FROM correlation_edges ORDER BY edge_id", "edge_id", "hot"),
    "events": ("SELECT event_id AS identity, event_time AS timestamp, NULL AS retention_class FROM events ORDER BY event_id", "event_id", "hot"),
    "git_snapshot_correlations": ("SELECT CAST(correlation_id AS TEXT) AS identity, created_at AS timestamp, NULL AS retention_class FROM git_snapshot_correlations ORDER BY correlation_id", "correlation_id", "hot"),
    "git_snapshot_paths": ("SELECT p.snapshot_observation_id, p.path, s.captured_at AS timestamp, NULL AS retention_class FROM git_snapshot_paths p JOIN git_snapshots s USING(snapshot_observation_id) ORDER BY p.snapshot_observation_id,p.path", "snapshot_observation_id,path", "hot"),
    "git_snapshots": ("SELECT snapshot_observation_id AS identity, captured_at AS timestamp, NULL AS retention_class FROM git_snapshots ORDER BY snapshot_observation_id", "snapshot_observation_id", "hot"),
    "hook_events": ("SELECT ingest_id AS identity, received_at AS timestamp, NULL AS retention_class FROM hook_events ORDER BY ingest_id", "ingest_id", "hot"),
    "raw_events": ("SELECT ingest_id AS identity, received_at AS timestamp, retention_class FROM raw_events ORDER BY ingest_id", "ingest_id", "raw"),
})


def _require_aware(value: datetime, name: str) -> None:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


def _utc_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise TypeError("timestamp is not text")
    parsed = datetime.fromisoformat(value)
    _require_aware(parsed, "timestamp")
    return parsed.astimezone(UTC)


def _row_identity(row: Any, identity_columns: str) -> str:
    """Return a reversible identity for composite keys and the scalar key otherwise."""

    columns = identity_columns.split(",")
    if len(columns) == 1:
        return str(row["identity"])
    return json.dumps(
        {column: row[column] for column in columns},
        sort_keys=True,
        separators=(",", ":"),
    )


@dataclass(frozen=True, slots=True)
class RetentionPolicyV1:
    cutoff: datetime

    def __post_init__(self) -> None:
        _require_aware(self.cutoff, "cutoff")

    def eligible(self, timestamp: datetime) -> bool:
        _require_aware(timestamp, "timestamp")
        return timestamp < self.cutoff


@dataclass(frozen=True, slots=True)
class RetentionDiagnosticV1:
    table: str
    timestamp_column: str
    row_identity: str
    reason: str


class CoverageStatus(StrEnum):
    COVERED = "COVERED"
    UNCOVERED = "UNCOVERED"
    BLOCKED = "BLOCKED"


_ARCHIVE_IDENTITIES: Final[Mapping[str, tuple[str, str]]] = MappingProxyType({
    "events": ("events", "event_id"),
    "git_snapshots": ("git_snapshots", "snapshot_observation_id"),
})


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _manifest_bytes(manifest: Mapping[str, Any]) -> bytes:
    unsigned = dict(manifest)
    unsigned.pop("manifest_sha256", None)
    return (json.dumps(unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode()


def _archive_path(root: Path, relative: Any, suffix: str) -> Path:
    if not isinstance(relative, str) or Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise ValueError("archive path escapes configured root")
    path = (root / relative).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError("archive path escapes configured root") from exc
    if path.suffix != suffix:
        raise ValueError(f"archive path is not a {suffix} file")
    return path


def _verified_archive_identities(connection: Any, root: Path) -> tuple[dict[str, set[str]], set[str], list[str]]:
    """Read stable identities only from fully verified published batches."""

    root = root.expanduser().resolve()
    identities: dict[str, set[str]] = {
        dataset: set() for dataset, _ in _ARCHIVE_IDENTITIES.values()
    }
    blocked: set[str] = set()
    failures: list[str] = []
    for batch in connection.execute(
        "SELECT batch_id,dataset,schema_version,manifest_path,manifest_digest "
        "FROM archive_batches WHERE status='PUBLISHED' ORDER BY batch_id"
    ).fetchall():
        dataset = batch["dataset"]
        if dataset not in identities:
            continue
        try:
            expected_schema = DATASETS[dataset]["schema"]
            if batch["schema_version"] != expected_schema:
                raise ValueError("registry schema is incompatible")
            manifest_path = _archive_path(root, batch["manifest_path"], ".json")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            digest = manifest.get("manifest_sha256")
            if (manifest.get("schema") != ARCHIVE_SCHEMA or manifest.get("batch_id") != batch["batch_id"]
                    or manifest.get("dataset") != dataset
                    or manifest.get("canonical_schema_version") != expected_schema):
                raise ValueError("manifest dataset or schema is incompatible")
            if (not isinstance(digest, str) or digest != batch["manifest_digest"]
                    or hashlib.sha256(_manifest_bytes(manifest)).hexdigest() != digest):
                raise ValueError("manifest digest mismatch")
            files = manifest.get("files")
            if not isinstance(files, list) or not files:
                raise ValueError("manifest contains no files")
            identity_column = next(value[1] for value in _ARCHIVE_IDENTITIES.values() if value[0] == dataset)
            batch_identities: set[str] = set()
            parquet = duckdb.connect(":memory:")
            try:
                for spec in files:
                    if not isinstance(spec, dict):
                        raise TypeError("invalid manifest file entry")
                    registered = connection.execute(
                        "SELECT dataset,path,row_count,sha256,schema_version FROM archive_files "
                        "WHERE batch_id=? AND file_id=?", (batch["batch_id"], spec.get("archive_file_id")),
                    ).fetchone()
                    if registered is None or registered["dataset"] != dataset or registered["schema_version"] != expected_schema:
                        raise ValueError("manifest file is not compatibly registered")
                    if any(registered[key] != spec.get(key) for key in ("path", "row_count", "sha256")):
                        raise ValueError("manifest and file registry disagree")
                    path = _archive_path(root, spec.get("path"), ".parquet")
                    if not path.is_file():
                        raise FileNotFoundError("archive Parquet file is missing")
                    if _sha256_file(path) != spec.get("sha256"):
                        raise ValueError("archive file digest mismatch")
                    escaped = str(path).replace("'", "''")
                    columns = {row[0] for row in parquet.execute(f"DESCRIBE SELECT * FROM read_parquet('{escaped}')").fetchall()}
                    if identity_column not in columns:
                        raise ValueError("archive identity column is missing")
                    rows = parquet.execute(f'SELECT "{identity_column}" FROM read_parquet(\'{escaped}\')').fetchall()
                    if len(rows) != spec.get("row_count"):
                        raise ValueError("archive row count mismatch")
                    batch_identities.update(str(row[0]) for row in rows if row[0] is not None)
            finally:
                parquet.close()
            identities[dataset].update(batch_identities)
        except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError, duckdb.Error) as exc:
            blocked.add(dataset)
            failures.append(f"{batch['batch_id']}: {exc}")
    return identities, blocked, failures


@dataclass(frozen=True, slots=True)
class RetentionPlanV1:
    run_id: str = ""
    evaluation_time: str = ""
    policy_digest: str = ""
    cutoffs: Mapping[str, str] | None = None
    candidates_by_table: Mapping[str, tuple[str, ...]] | None = None
    coverage_by_table: Mapping[str, Mapping[str, str]] | None = None
    eligible_count: int = 0
    ineligible_count: int = 0
    archive_coverage_status: str = "UNCOVERED"
    covered_count: int = 0
    uncovered_count: int = 0
    blocked_count: int = 0
    planned_deletion_count: int = 0
    status: str = "PLANNED"
    diagnostics: tuple[RetentionDiagnosticV1, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id, "evaluation_time": self.evaluation_time,
            "policy_digest": self.policy_digest, "cutoffs": dict(self.cutoffs or {}),
            "candidates_by_table": {key: list(items) for key, items in (self.candidates_by_table or {}).items()},
            "coverage_by_table": {key: dict(items) for key, items in (self.coverage_by_table or {}).items()},
            "eligible_count": self.eligible_count, "ineligible_count": self.ineligible_count,
            "archive_coverage_status": self.archive_coverage_status,
            "covered_count": self.covered_count, "uncovered_count": self.uncovered_count,
            "blocked_count": self.blocked_count,
            "planned_deletion_count": self.planned_deletion_count, "status": self.status,
            "eligible": self.eligible_count, "covered": self.covered_count,
            "uncovered": self.uncovered_count, "would_delete": self.planned_deletion_count,
            "diagnostics": [asdict(item) for item in self.diagnostics],
        }


def plan_retention(connection: Any, config: RetentionConfig, *, archive_root: Path | None = None,
                   evaluation_time: datetime | None = None) -> RetentionPlanV1:
    """Persist and return one dry-run plan. This function contains no DELETE SQL."""

    if connection.in_transaction:
        raise ValueError("plan_retention requires a connection with no active transaction")
    evaluated = evaluation_time or datetime.now(UTC)
    _require_aware(evaluated, "evaluation_time")
    evaluated = evaluated.astimezone(UTC)
    policy = config.model_dump(mode="json")
    policy_json = json.dumps(policy, sort_keys=True, separators=(",", ":"))
    digest = "sha256:" + hashlib.sha256(policy_json.encode()).hexdigest()
    cutoffs = {
        "hot": _utc_text(evaluated - timedelta(days=config.hot_days)),
        "raw_metadata": _utc_text(evaluated - timedelta(days=config.raw_metadata_days)),
        "forensic_raw": _utc_text(evaluated - timedelta(days=config.forensic_raw_days)),
    }
    candidates: dict[str, tuple[str, ...]] = {}
    diagnostics: list[RetentionDiagnosticV1] = []
    ineligible = 0
    run_id = str(uuid.uuid4())
    evaluation_text = _utc_text(evaluated)
    cutoff_json = json.dumps(cutoffs, sort_keys=True, separators=(",", ":"))
    connection.execute("BEGIN IMMEDIATE")
    try:
        for table in PRUNABLE_HISTORY_ORDER:
            sql, identity_columns, kind = _SELECTIONS[table]
            eligible: list[str] = []
            for row in connection.execute(sql).fetchall():
                identity = _row_identity(row, identity_columns)
                try:
                    timestamp = _parse_timestamp(row["timestamp"])
                except (TypeError, ValueError) as exc:
                    diagnostics.append(RetentionDiagnosticV1(table, PRUNABLE_TIMESTAMP_COLUMNS[table], identity, str(exc)))
                    ineligible += 1
                    continue
                cutoff_name = "hot"
                if kind == "raw":
                    cutoff_name = "forensic_raw" if row["retention_class"] == "forensic" else "raw_metadata"
                if config.enabled and timestamp < _parse_timestamp(cutoffs[cutoff_name]):
                    eligible.append(identity)
                else:
                    ineligible += 1
            candidates[table] = tuple(eligible)

        eligible_count = sum(map(len, candidates.values()))
        archive_identities, blocked_datasets, archive_failures = (
            _verified_archive_identities(connection, archive_root) if archive_root is not None
            else ({dataset: set() for dataset, _ in _ARCHIVE_IDENTITIES.values()}, set(), [])
        )
        coverage: dict[str, Mapping[str, str]] = {}
        for table, rows in candidates.items():
            mapping = _ARCHIVE_IDENTITIES.get(table)
            statuses: dict[str, str] = {}
            for identity in rows:
                if mapping is None:
                    status = CoverageStatus.UNCOVERED
                elif identity in archive_identities[mapping[0]]:
                    status = CoverageStatus.COVERED
                elif mapping[0] in blocked_datasets:
                    status = CoverageStatus.BLOCKED
                else:
                    status = CoverageStatus.UNCOVERED
                statuses[identity] = status.value
            coverage[table] = MappingProxyType(statuses)
        covered_count = sum(value == CoverageStatus.COVERED for rows in coverage.values() for value in rows.values())
        blocked_count = sum(value == CoverageStatus.BLOCKED for rows in coverage.values() for value in rows.values())
        uncovered_count = eligible_count - covered_count - blocked_count
        plan_status = "BLOCKED" if blocked_count else "VERIFIED"
        coverage_status = "BLOCKED" if blocked_count else ("COVERED" if covered_count == eligible_count else "UNCOVERED")
        connection.execute(
            "INSERT INTO retention_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, evaluation_text, evaluation_text, evaluation_text, digest, cutoff_json,
             plan_status, eligible_count, covered_count, uncovered_count + blocked_count, covered_count, 0,
             "; ".join(archive_failures) if archive_failures else None),
        )
        for table in PRUNABLE_HISTORY_ORDER:
            rows = candidates[table]
            table_covered = sum(value == CoverageStatus.COVERED for value in coverage[table].values())
            connection.execute("INSERT INTO retention_run_tables VALUES (?,?,?,?,?,?,?)", (run_id, table, len(rows), table_covered, len(rows) - table_covered, table_covered, 0))
            connection.executemany(
                "INSERT INTO retention_run_candidates(run_id,table_name,row_identity,coverage_status) VALUES(?,?,?,?)",
                ((run_id, table, identity, coverage[table][identity]) for identity in rows),
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return RetentionPlanV1(
        run_id=run_id, evaluation_time=evaluation_text, policy_digest=digest,
        cutoffs=MappingProxyType(cutoffs), candidates_by_table=MappingProxyType(candidates),
        coverage_by_table=MappingProxyType(coverage), eligible_count=eligible_count,
        ineligible_count=ineligible, archive_coverage_status=coverage_status,
        covered_count=covered_count, uncovered_count=uncovered_count,
        blocked_count=blocked_count, planned_deletion_count=covered_count,
        status=plan_status, diagnostics=tuple(diagnostics),
    )


def render_table_classification_markdown() -> str:
    lines = ["| Table | Classification |", "| --- | --- |"]
    lines.extend(f"| `{table}` | `{classification.value}` |" for table, classification in TABLE_CLASSIFICATIONS.items())
    return "\n".join(lines)
