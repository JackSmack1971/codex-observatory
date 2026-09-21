"""Immutable Parquet archive for the SQLite live evidence store.

SQLite is the source of truth.  This module only reads source rows, writes
temporary Parquet files, verifies them, and then publishes immutable files
registered by a manifest.  Normal readers trust manifests, never directory
contents alone.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb

from .sqlite import utc_now

ARCHIVE_SCHEMA = "codex.observatory.archive-manifest.v1"
EVENT_SCHEMA = "codex.observatory.events.v1"
TOKEN_SCHEMA = "codex.observatory.token-usage.v1"
GIT_SCHEMA = "codex.observatory.git-snapshots.v1"
COMPRESSION = "zstd"
SMALL_FILE_BYTES = 64 * 1024

DATASETS = {
    "events": {"table": "events", "watermark": "event_seq", "schema": EVENT_SCHEMA},
    "token_usage": {"table": "app_server_token_usage", "watermark": "rowid", "schema": TOKEN_SCHEMA},
    "git_snapshots": {"table": "git_snapshots", "watermark": "rowid", "schema": GIT_SCHEMA},
}

EVENT_DDL = """CREATE TABLE stage (
    event_id VARCHAR, event_seq BIGINT, event_time TIMESTAMP, event_time_unix_nano BIGINT,
    observed_at TIMESTAMP, source_class VARCHAR, fact_type VARCHAR, stability VARCHAR,
    source_event VARCHAR, source_instance VARCHAR, source_version VARCHAR,
    raw_event_sha256 VARCHAR, adapter_version VARCHAR, session_id VARCHAR, thread_id VARCHAR,
    turn_id VARCHAR, item_id VARCHAR, call_id VARCHAR, trace_id VARCHAR, span_id VARCHAR,
    operation_id VARCHAR, category VARCHAR, name VARCHAR, status VARCHAR, attributes_json VARCHAR
)"""
TOKEN_DDL = """CREATE TABLE stage (
    thread_id VARCHAR, turn_id VARCHAR, observed_at TIMESTAMP, source_instance VARCHAR,
    usage_json VARCHAR, total_tokens BIGINT, input_tokens BIGINT, output_tokens BIGINT
)"""
GIT_DDL = """CREATE TABLE stage (
    snapshot_observation_id VARCHAR, snapshot_content_digest VARCHAR, repo_id VARCHAR,
    worktree_id VARCHAR, head_sha VARCHAR, head_ref VARCHAR, detached BOOLEAN,
    head_state VARCHAR, upstream_ref VARCHAR, clean BOOLEAN, staged_count BIGINT,
    unstaged_count BIGINT, untracked_count BIGINT, conflicted_count BIGINT,
    changed_file_count BIGINT, insertions BIGINT, deletions BIGINT, binary_count BIGINT,
    captured_at TIMESTAMP, adapter_version VARCHAR, evidence_digest VARCHAR
)"""


@dataclass(frozen=True, slots=True)
class ExportResult:
    dataset: str
    batch_id: str | None
    status: str
    rows: int
    files: int
    pending_rows: int
    manifest_path: str | None = None


def _timestamp(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed.astimezone(UTC).replace(tzinfo=None)


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_relative(root: Path, path: Path, suffix: str = ".parquet") -> str:
    resolved_root = root.resolve()
    resolved = path.resolve()
    try:
        relative = resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError("archive path escapes configured root") from exc
    if relative.suffix != suffix or any(part == ".." for part in relative.parts):
        raise ValueError(f"archive path is not a published {suffix} file")
    return relative.as_posix()


def _dataset_ddl(dataset: str) -> str:
    return {"events": EVENT_DDL, "token_usage": TOKEN_DDL, "git_snapshots": GIT_DDL}[dataset]


def _source_rows(connection: Any, dataset: str, watermark: int) -> list[tuple[Any, ...]]:
    if dataset == "events":
        return [tuple(row) for row in connection.execute(
            """SELECT event_id,event_seq,event_time,event_time_unix_nano,observed_at,source_class,
            fact_type,stability,source_event,source_instance,source_version,raw_event_sha256,
            adapter_version,session_id,thread_id,turn_id,item_id,call_id,trace_id,span_id,
            operation_id,category,name,status,attributes_json FROM events
            WHERE event_seq>? ORDER BY event_seq""", (watermark,)).fetchall()]
    if dataset == "token_usage":
        rows = connection.execute(
            "SELECT rowid,thread_id,turn_id,observed_at,source_instance,usage_json FROM app_server_token_usage WHERE rowid>? ORDER BY rowid",
            (watermark,),
        ).fetchall()
        result = []
        for row in rows:
            usage = json.loads(row[5]) if row[5] else {}
            total = usage.get("total", {}) if isinstance(usage, dict) else {}
            result.append(tuple(row[1:]) + (total.get("totalTokens"), total.get("inputTokens"), total.get("outputTokens")))
        return result
    if dataset == "git_snapshots":
        return [tuple(row[1:]) for row in connection.execute(
            """SELECT rowid,snapshot_observation_id,snapshot_content_digest,repo_id,worktree_id,
            head_sha,head_ref,detached,head_state,upstream_ref,clean,staged_count,unstaged_count,
            untracked_count,conflicted_count,changed_file_count,insertions,deletions,binary_count,
            captured_at,adapter_version,evidence_digest FROM git_snapshots WHERE rowid>? ORDER BY rowid""",
            (watermark,),
        ).fetchall()]
    raise ValueError(f"unsupported archive dataset: {dataset}")


def _source_end(connection: Any, dataset: str) -> int:
    table = DATASETS[dataset]["table"]
    return int(connection.execute(f"SELECT COALESCE(MAX(rowid),0) FROM {table}").fetchone()[0])


def _convert_row(dataset: str, row: tuple[Any, ...]) -> tuple[Any, ...]:
    if dataset == "events":
        values = list(row)
        values[2] = _timestamp(values[2])
        values[4] = _timestamp(values[4])
        return tuple(values)
    if dataset == "token_usage":
        values = list(row)
        values[2] = _timestamp(values[2])
        return tuple(values)
    values = list(row)
    values[18] = _timestamp(values[18])
    return tuple(values)


def _write_staged(dataset: str, rows: list[tuple[Any, ...]], staging: Path) -> list[Path]:
    staging.mkdir(parents=True, exist_ok=False)
    conn = duckdb.connect(":memory:")
    try:
        conn.execute(_dataset_ddl(dataset))
        conn.executemany("INSERT INTO stage VALUES (" + ",".join("?" for _ in rows[0]) + ")", [_convert_row(dataset, row) for row in rows])
        time_column = {"events": "event_time", "token_usage": "observed_at", "git_snapshots": "captured_at"}[dataset]
        destination = str(staging / dataset).replace("'", "''")
        conn.execute(
            f"COPY (SELECT *, year({time_column}) AS year, month({time_column}) AS month FROM stage) TO '{destination}' (FORMAT parquet, PARTITION_BY (year, month), COMPRESSION zstd, FILENAME_PATTERN 'batch_{{uuid}}', OVERWRITE_OR_IGNORE)"
        )
    finally:
        conn.close()
    return sorted((staging / dataset).rglob("*.parquet"))


def _verify_file(path: Path, dataset: str, expected_rows: int | None = None) -> tuple[int, str | None, str | None]:
    conn = duckdb.connect(":memory:")
    try:
        escaped = str(path).replace("'", "''")
        time_column = {"events": "event_time", "token_usage": "observed_at", "git_snapshots": "captured_at"}[dataset]
        result = conn.execute(f"SELECT count(*), min({time_column}), max({time_column}) FROM read_parquet('{escaped}')").fetchone()
    finally:
        conn.close()
    if result is None:
        raise ValueError(f"Parquet metadata query returned no result for {path}")
    count = int(result[0])
    if expected_rows is not None and count != expected_rows:
        raise ValueError(f"Parquet row count mismatch for {path}: {count} != {expected_rows}")
    return count, result[1].isoformat() + "Z" if result[1] else None, result[2].isoformat() + "Z" if result[2] else None


def _manifest_bytes(manifest: dict[str, Any]) -> bytes:
    unsigned = dict(manifest)
    unsigned.pop("manifest_sha256", None)
    return (_json(unsigned) + "\n").encode("utf-8")


def _set_archive_health(connection: Any, *, status: str, started: int = 0, published: int = 0, failed: int = 0,
                        rows: int = 0, files: int = 0, verification_failures: int = 0,
                        small_files: int = 0, last_export: str | None = None, last_success: str | None = None,
                        error: str | None = None, pending: int = 0) -> None:
    now = utc_now()
    with connection:
        connection.execute("""INSERT INTO archive_health(collector,status,batches_started_total,batches_published_total,batches_failed_total,rows_archived_total,files_published_total,verification_failures_total,small_files_total,last_export,last_success,last_error,pending_rows,updated_at)
        VALUES('archive',?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(collector) DO UPDATE SET status=excluded.status,
        batches_started_total=archive_health.batches_started_total+excluded.batches_started_total,
        batches_published_total=archive_health.batches_published_total+excluded.batches_published_total,
        batches_failed_total=archive_health.batches_failed_total+excluded.batches_failed_total,
        rows_archived_total=archive_health.rows_archived_total+excluded.rows_archived_total,
        files_published_total=archive_health.files_published_total+excluded.files_published_total,
        verification_failures_total=archive_health.verification_failures_total+excluded.verification_failures_total,
        small_files_total=archive_health.small_files_total+excluded.small_files_total,
        last_export=COALESCE(excluded.last_export,archive_health.last_export),last_success=COALESCE(excluded.last_success,archive_health.last_success),
        last_error=excluded.last_error,pending_rows=excluded.pending_rows,updated_at=excluded.updated_at""",
                           (status, started, published, failed, rows, files, verification_failures, small_files,
                            last_export, last_success, error, pending, now))


def export_dataset(connection: Any, root: Path, dataset: str = "events") -> ExportResult:
    if dataset not in DATASETS:
        raise ValueError(f"unsupported archive dataset: {dataset}")
    root = root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    definition = DATASETS[dataset]
    prior = connection.execute("SELECT last_watermark FROM archive_watermarks WHERE dataset=?", (dataset,)).fetchone()
    watermark = int(prior[0]) if prior else 0
    rows = _source_rows(connection, dataset, watermark)
    source_end = _source_end(connection, dataset)
    pending = max(0, source_end - watermark)
    if not rows:
        _set_archive_health(connection, status="empty" if not connection.execute("SELECT 1 FROM archive_batches WHERE status='PUBLISHED' LIMIT 1").fetchone() else "healthy", pending=0)
        return ExportResult(dataset, None, "NOOP", 0, 0, 0)
    batch_id = str(uuid.uuid4())
    started = utc_now()
    with connection:
        connection.execute("INSERT INTO archive_batches(batch_id,dataset,schema_version,started_at,source_start,source_end,status) VALUES(?,?,?,?,?,?,?)",
                           (batch_id, dataset, definition["schema"], started, watermark + 1, source_end, "PLANNED"))
    staging = root / ".staging" / batch_id
    try:
        with connection:
            connection.execute("UPDATE archive_batches SET status='WRITING' WHERE batch_id=?", (batch_id,))
        _set_archive_health(connection, status="degraded", started=1, last_export=started, pending=pending)
        staged_files = _write_staged(dataset, rows, staging)
        if not staged_files:
            raise ValueError("DuckDB produced no Parquet files")
        file_specs: list[dict[str, Any]] = []
        for staged in staged_files:
            count, minimum, maximum = _verify_file(staged, dataset)
            relative = staged.relative_to(staging / dataset)
            target = root / dataset / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                raise FileExistsError(f"immutable archive file already exists: {target}")
            staged.replace(target)
            file_specs.append({"archive_file_id": str(uuid.uuid4()), "path": _safe_relative(root, target), "row_count": count, "size_bytes": target.stat().st_size,
                               "sha256": _sha256(target), "min_event_time": minimum, "max_event_time": maximum})
        with connection:
            connection.execute("UPDATE archive_batches SET status='VERIFIED' WHERE batch_id=?", (batch_id,))
        first_time = min((f["min_event_time"] for f in file_specs if f["min_event_time"]), default=None)
        last_time = max((f["max_event_time"] for f in file_specs if f["max_event_time"]), default=None)
        manifest: dict[str, Any] = {"schema": ARCHIVE_SCHEMA, "batch_id": batch_id, "dataset": dataset,
                                    "canonical_schema_version": definition["schema"], "created_at": utc_now(),
                                    "selection": {"from": first_time, "to": last_time, "source_start": watermark + 1, "source_end": source_end},
                                    "writer": {"duckdb_version": duckdb.__version__, "compression": COMPRESSION, "row_group_size": "default", "timestamp_semantics": "UTC stored as Parquet TIMESTAMP"},
                                    "files": file_specs, "total_rows": sum(f["row_count"] for f in file_specs)}
        manifest["manifest_sha256"] = hashlib.sha256(_manifest_bytes(manifest)).hexdigest()
        manifest_path = root / "manifests" / dataset / f"{batch_id}.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        temp_manifest = manifest_path.with_suffix(".json.tmp")
        published_manifest = dict(manifest)
        published_manifest["manifest_sha256"] = manifest["manifest_sha256"]
        temp_manifest.write_text(_json(published_manifest) + "\n", encoding="utf-8", newline="\n")
        temp_manifest.replace(manifest_path)
        with connection:
            connection.execute("UPDATE archive_batches SET completed_at=?,selection_start=?,selection_end=?,row_count=?,file_count=?,status='PUBLISHED',manifest_path=?,manifest_digest=? WHERE batch_id=?",
                               (utc_now(), first_time, last_time, manifest["total_rows"], len(file_specs), _safe_relative(root, manifest_path, ".json"), manifest["manifest_sha256"], batch_id))
            connection.execute("INSERT INTO archive_watermarks(dataset,source_table,last_watermark,updated_at) VALUES(?,?,?,?) ON CONFLICT(dataset) DO UPDATE SET last_watermark=excluded.last_watermark,updated_at=excluded.updated_at",
                               (dataset, definition["table"], source_end, utc_now()))
            for spec in file_specs:
                connection.execute("INSERT INTO archive_files(file_id,batch_id,dataset,path,row_count,size_bytes,sha256,min_event_time,max_event_time,schema_version) VALUES(?,?,?,?,?,?,?,?,?,?)",
                                   (spec["archive_file_id"], batch_id, dataset, spec["path"], spec["row_count"], spec["size_bytes"], spec["sha256"], spec["min_event_time"], spec["max_event_time"], definition["schema"]))
        small = sum(1 for f in file_specs if f["size_bytes"] < SMALL_FILE_BYTES)
        _set_archive_health(connection, status="degraded" if small else "healthy", published=1, rows=manifest["total_rows"], files=len(file_specs), small_files=small, last_success=utc_now(), pending=0)
        return ExportResult(dataset, batch_id, "PUBLISHED", manifest["total_rows"], len(file_specs), 0, _safe_relative(root, manifest_path, ".json"))
    except Exception as exc:
        with connection:
            connection.execute("UPDATE archive_batches SET completed_at=?,status='FAILED',last_error=? WHERE batch_id=?", (utc_now(), str(exc), batch_id))
        _set_archive_health(connection, status="failed", failed=1, error=str(exc), pending=pending)
        shutil.rmtree(staging, ignore_errors=True)
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def verify_archive(connection: Any, root: Path) -> dict[str, Any]:
    root = root.expanduser().resolve()
    failures: list[str] = []
    verified_files = 0
    for batch in connection.execute("SELECT batch_id,manifest_path,manifest_digest FROM archive_batches WHERE status='PUBLISHED' ORDER BY batch_id").fetchall():
        try:
            manifest_path = root / batch["manifest_path"]
            _safe_relative(root, manifest_path, ".json")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            digest = manifest.get("manifest_sha256")
            if not isinstance(digest, str) or hashlib.sha256(_manifest_bytes(manifest)).hexdigest() != digest or digest != batch["manifest_digest"]:
                raise ValueError("manifest digest mismatch")
        except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError, duckdb.Error) as exc:  # manifest verification is reported as evidence
            failures.append(f"{batch['batch_id']}: {exc}")
    for row in connection.execute("SELECT * FROM archive_files WHERE batch_id IN (SELECT batch_id FROM archive_batches WHERE status='PUBLISHED') ORDER BY path").fetchall():
        path = root / row["path"]
        try:
            _safe_relative(root, path)
            if not path.is_file() or _sha256(path) != row["sha256"]:
                raise ValueError("file digest mismatch or missing file")
            count, _, _ = _verify_file(path, row["dataset"], row["row_count"])
            if count != row["row_count"]:
                raise ValueError("manifest row count mismatch")
            verified_files += 1
        except (OSError, TypeError, ValueError, KeyError, duckdb.Error) as exc:  # verification is reported as evidence
            failures.append(f"{row['path']}: {exc}")
    status = "healthy" if not failures and verified_files else "degraded" if failures else "empty"
    _set_archive_health(connection, status=status, verification_failures=len(failures), error="; ".join(failures) if failures else None)
    return {"status": status, "files": verified_files, "failures": failures}


def archive_health(connection: Any, root: Path) -> dict[str, Any]:
    row = connection.execute("SELECT * FROM archive_health WHERE collector='archive'").fetchone()
    if row:
        return dict(row)
    published = connection.execute("SELECT count(*) FROM archive_batches WHERE status='PUBLISHED'").fetchone()[0]
    return {"collector": "archive", "status": "healthy" if published else "empty", "batches_published_total": published, "pending_rows": 0}


def published_paths(connection: Any, root: Path, dataset: str) -> list[Path]:
    root = root.expanduser().resolve()
    paths: list[Path] = []
    for row in connection.execute("SELECT path FROM archive_files WHERE dataset=? AND batch_id IN (SELECT batch_id FROM archive_batches WHERE status='PUBLISHED') ORDER BY path", (dataset,)).fetchall():
        path = root / row[0]
        _safe_relative(root, path)
        if not path.is_file():
            raise FileNotFoundError(path)
        paths.append(path)
    return paths
