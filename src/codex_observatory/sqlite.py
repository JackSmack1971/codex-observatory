from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from .models import CanonicalEvent, RawEnvelope

MIGRATIONS: tuple[tuple[int, str], ...] = (
    (
        1,
        """
        CREATE TABLE raw_events (
            ingest_id TEXT PRIMARY KEY,
            received_at TEXT NOT NULL,
            source TEXT NOT NULL,
            source_instance TEXT NOT NULL,
            source_event_type TEXT NOT NULL,
            source_version TEXT,
            content_type TEXT NOT NULL,
            content_encoding TEXT,
            payload_encoding TEXT NOT NULL,
            payload_sha256 TEXT NOT NULL,
            payload_size INTEGER NOT NULL,
            payload_ref TEXT,
            payload BLOB,
            parse_status TEXT NOT NULL,
            error_code TEXT,
            error_message TEXT,
            retention_class TEXT NOT NULL,
            duplicate_of TEXT
        );
        CREATE INDEX idx_raw_digest ON raw_events(source_event_type, payload_sha256);
        CREATE TABLE events (
            event_seq INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL UNIQUE,
            event_time TEXT NOT NULL,
            event_time_unix_nano INTEGER,
            observed_at TEXT NOT NULL,
            source_class TEXT NOT NULL,
            fact_type TEXT NOT NULL,
            stability TEXT NOT NULL,
            source_event TEXT NOT NULL,
            source_instance TEXT NOT NULL,
            source_version TEXT,
            raw_event_sha256 TEXT NOT NULL,
            adapter_version TEXT NOT NULL,
            session_id TEXT, thread_id TEXT, turn_id TEXT, item_id TEXT,
            call_id TEXT, trace_id TEXT, span_id TEXT, operation_id TEXT,
            category TEXT NOT NULL, name TEXT NOT NULL, status TEXT,
            attributes_json TEXT NOT NULL
        );
        CREATE INDEX idx_events_time ON events(event_time, event_seq);
        CREATE INDEX idx_events_name ON events(category, name, event_time);
        CREATE TABLE collector_health (
            collector TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            received_total INTEGER NOT NULL DEFAULT 0,
            normalized_total INTEGER NOT NULL DEFAULT 0,
            rejected_total INTEGER NOT NULL DEFAULT 0,
            unknown_event_total INTEGER NOT NULL DEFAULT 0,
            persistence_error_total INTEGER NOT NULL DEFAULT 0,
            last_success TEXT,
            last_error TEXT,
            updated_at TEXT NOT NULL
        );
        """,
    ),
    (
        2,
        """
        CREATE TABLE app_server_state (
            source_instance TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            server_version TEXT,
            capabilities_json TEXT NOT NULL DEFAULT '{}',
            reconnect_total INTEGER NOT NULL DEFAULT 0,
            messages_received_total INTEGER NOT NULL DEFAULT 0,
            responses_received_total INTEGER NOT NULL DEFAULT 0,
            notifications_received_total INTEGER NOT NULL DEFAULT 0,
            malformed_message_total INTEGER NOT NULL DEFAULT 0,
            unknown_notification_total INTEGER NOT NULL DEFAULT 0,
            protocol_error_total INTEGER NOT NULL DEFAULT 0,
            reconciliation_runs_total INTEGER NOT NULL DEFAULT 0,
            last_connected TEXT,
            last_message TEXT,
            last_error TEXT,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE app_server_messages (
            message_id TEXT PRIMARY KEY,
            source_instance TEXT NOT NULL,
            received_at TEXT NOT NULL,
            kind TEXT NOT NULL,
            method TEXT,
            request_id TEXT,
            payload_json TEXT NOT NULL,
            known INTEGER NOT NULL DEFAULT 1,
            FOREIGN KEY(source_instance) REFERENCES app_server_state(source_instance)
        );
        CREATE INDEX idx_app_server_messages_method ON app_server_messages(method, received_at);
        CREATE TABLE threads (
            thread_id TEXT PRIMARY KEY,
            name TEXT,
            cwd TEXT,
            model_provider TEXT,
            model TEXT,
            created_at TEXT,
            updated_at TEXT,
            archived INTEGER,
            runtime_status TEXT,
            loaded INTEGER NOT NULL DEFAULT 0,
            forked_from_id TEXT,
            source_instance TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            FOREIGN KEY(source_instance) REFERENCES app_server_state(source_instance)
        );
        CREATE TABLE turns (
            turn_id TEXT PRIMARY KEY,
            thread_id TEXT NOT NULL,
            status TEXT NOT NULL,
            started_at TEXT,
            completed_at TEXT,
            error_json TEXT,
            duration_ms INTEGER,
            source_instance TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            FOREIGN KEY(thread_id) REFERENCES threads(thread_id),
            FOREIGN KEY(source_instance) REFERENCES app_server_state(source_instance)
        );
        CREATE INDEX idx_turns_thread ON turns(thread_id);
        CREATE TABLE thread_items (
            item_id TEXT PRIMARY KEY,
            thread_id TEXT NOT NULL,
            turn_id TEXT NOT NULL,
            item_type TEXT NOT NULL,
            lifecycle_status TEXT NOT NULL,
            final INTEGER NOT NULL DEFAULT 0,
            content_json TEXT NOT NULL,
            source_instance TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            FOREIGN KEY(thread_id) REFERENCES threads(thread_id),
            FOREIGN KEY(turn_id) REFERENCES turns(turn_id),
            FOREIGN KEY(source_instance) REFERENCES app_server_state(source_instance)
        );
        CREATE INDEX idx_thread_items_turn ON thread_items(turn_id);
        CREATE TABLE app_server_token_usage (
            thread_id TEXT NOT NULL,
            turn_id TEXT NOT NULL,
            usage_json TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            source_instance TEXT NOT NULL,
            PRIMARY KEY(thread_id, turn_id),
            FOREIGN KEY(source_instance) REFERENCES app_server_state(source_instance)
        );
        """,
    ),
    (
        3,
        """
        CREATE TABLE hook_events (
            ingest_id TEXT PRIMARY KEY,
            event_id TEXT NOT NULL UNIQUE,
            hook_event_name TEXT NOT NULL,
            session_id TEXT,
            turn_id TEXT,
            tool_use_id TEXT,
            agent_id TEXT,
            agent_type TEXT,
            redaction_count INTEGER NOT NULL DEFAULT 0,
            normalized INTEGER NOT NULL DEFAULT 1,
            received_at TEXT NOT NULL,
            FOREIGN KEY(ingest_id) REFERENCES raw_events(ingest_id),
            FOREIGN KEY(event_id) REFERENCES events(event_id)
        );
        CREATE INDEX idx_hook_events_session ON hook_events(session_id, received_at);
        CREATE INDEX idx_hook_events_turn ON hook_events(turn_id, received_at);
        CREATE INDEX idx_hook_events_tool ON hook_events(tool_use_id, received_at);
        CREATE INDEX idx_hook_events_agent ON hook_events(agent_id, received_at);
        CREATE TABLE hook_source_state (
            source_instance TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            received_total INTEGER NOT NULL DEFAULT 0,
            normalized_total INTEGER NOT NULL DEFAULT 0,
            unknown_event_total INTEGER NOT NULL DEFAULT 0,
            privacy_redaction_total INTEGER NOT NULL DEFAULT 0,
            duplicate_total INTEGER NOT NULL DEFAULT 0,
            correlation_total INTEGER NOT NULL DEFAULT 0,
            correlation_unresolved_total INTEGER NOT NULL DEFAULT 0,
            ingest_failure_total INTEGER NOT NULL DEFAULT 0,
            delivery_failure_total INTEGER NOT NULL DEFAULT 0,
            last_event TEXT,
            last_error TEXT,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE correlation_edges (
            edge_id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL,
            related_event_id TEXT NOT NULL,
            correlation_method TEXT NOT NULL,
            correlation_confidence TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(event_id, related_event_id, correlation_method),
            FOREIGN KEY(event_id) REFERENCES events(event_id),
            FOREIGN KEY(related_event_id) REFERENCES events(event_id)
        );
        CREATE INDEX idx_correlation_event ON correlation_edges(event_id);
        CREATE INDEX idx_correlation_related ON correlation_edges(related_event_id);
        """,
    ),
    (
        4,
        """
        CREATE TABLE repositories (
            repo_id TEXT PRIMARY KEY,
            root TEXT NOT NULL,
            remote_identity TEXT,
            bare INTEGER NOT NULL DEFAULT 0,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL
        );
        CREATE TABLE worktrees (
            worktree_id TEXT PRIMARY KEY,
            repo_id TEXT NOT NULL,
            path TEXT NOT NULL,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            FOREIGN KEY(repo_id) REFERENCES repositories(repo_id)
        );
        CREATE INDEX idx_worktrees_repo ON worktrees(repo_id);
        CREATE TABLE git_snapshots (
            snapshot_observation_id TEXT PRIMARY KEY,
            snapshot_content_digest TEXT NOT NULL,
            repo_id TEXT NOT NULL,
            worktree_id TEXT NOT NULL,
            head_sha TEXT,
            head_ref TEXT,
            detached INTEGER NOT NULL,
            head_state TEXT NOT NULL,
            upstream_ref TEXT,
            clean INTEGER NOT NULL,
            staged_count INTEGER NOT NULL,
            unstaged_count INTEGER NOT NULL,
            untracked_count INTEGER NOT NULL,
            conflicted_count INTEGER NOT NULL,
            changed_file_count INTEGER NOT NULL,
            insertions INTEGER NOT NULL,
            deletions INTEGER NOT NULL,
            binary_count INTEGER NOT NULL,
            captured_at TEXT NOT NULL,
            adapter_version TEXT NOT NULL,
            evidence_digest TEXT NOT NULL,
            FOREIGN KEY(repo_id) REFERENCES repositories(repo_id),
            FOREIGN KEY(worktree_id) REFERENCES worktrees(worktree_id)
        );
        CREATE INDEX idx_git_snapshots_content ON git_snapshots(snapshot_content_digest);
        CREATE INDEX idx_git_snapshots_worktree ON git_snapshots(worktree_id,captured_at);
        CREATE TABLE git_snapshot_paths (
            snapshot_observation_id TEXT NOT NULL,
            path TEXT NOT NULL,
            status TEXT NOT NULL,
            area TEXT NOT NULL,
            old_path TEXT,
            insertions INTEGER,
            deletions INTEGER,
            binary INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(snapshot_observation_id,path),
            FOREIGN KEY(snapshot_observation_id) REFERENCES git_snapshots(snapshot_observation_id)
        );
        CREATE TABLE git_snapshot_correlations (
            correlation_id INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_observation_id TEXT NOT NULL,
            session_id TEXT,
            thread_id TEXT,
            turn_id TEXT,
            correlation_method TEXT NOT NULL,
            correlation_confidence TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(snapshot_observation_id) REFERENCES git_snapshots(snapshot_observation_id)
        );
        CREATE TABLE git_health (
            collector TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            repositories_discovered_total INTEGER NOT NULL DEFAULT 0,
            snapshots_total INTEGER NOT NULL DEFAULT 0,
            capture_failures_total INTEGER NOT NULL DEFAULT 0,
            parse_failures_total INTEGER NOT NULL DEFAULT 0,
            correlations_total INTEGER NOT NULL DEFAULT 0,
            unresolved_correlations_total INTEGER NOT NULL DEFAULT 0,
            last_capture TEXT,
            last_error TEXT,
            updated_at TEXT NOT NULL
        );
        """,
    ),
    (
        5,
        """
        CREATE TABLE archive_batches (
            batch_id TEXT PRIMARY KEY,
            dataset TEXT NOT NULL,
            schema_version TEXT NOT NULL,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            selection_start TEXT,
            selection_end TEXT,
            source_start INTEGER,
            source_end INTEGER,
            row_count INTEGER NOT NULL DEFAULT 0,
            file_count INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL,
            manifest_path TEXT UNIQUE,
            manifest_digest TEXT,
            last_error TEXT
        );
        CREATE INDEX idx_archive_batches_dataset_status ON archive_batches(dataset,status);
        CREATE TABLE archive_files (
            file_id TEXT PRIMARY KEY,
            batch_id TEXT NOT NULL,
            dataset TEXT NOT NULL,
            path TEXT NOT NULL UNIQUE,
            row_count INTEGER NOT NULL,
            size_bytes INTEGER NOT NULL,
            sha256 TEXT NOT NULL,
            min_event_time TEXT,
            max_event_time TEXT,
            schema_version TEXT NOT NULL,
            FOREIGN KEY(batch_id) REFERENCES archive_batches(batch_id)
        );
        CREATE TABLE archive_watermarks (
            dataset TEXT PRIMARY KEY,
            source_table TEXT NOT NULL,
            last_watermark INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE archive_health (
            collector TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            batches_started_total INTEGER NOT NULL DEFAULT 0,
            batches_published_total INTEGER NOT NULL DEFAULT 0,
            batches_failed_total INTEGER NOT NULL DEFAULT 0,
            rows_archived_total INTEGER NOT NULL DEFAULT 0,
            files_published_total INTEGER NOT NULL DEFAULT 0,
            verification_failures_total INTEGER NOT NULL DEFAULT 0,
            small_files_total INTEGER NOT NULL DEFAULT 0,
            last_export TEXT,
            last_success TEXT,
            last_error TEXT,
            pending_rows INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE analytics_health (
            collector TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            queries_total INTEGER NOT NULL DEFAULT 0,
            query_failures_total INTEGER NOT NULL DEFAULT 0,
            last_query TEXT,
            last_error TEXT,
            updated_at TEXT NOT NULL
        );
        """,
    ),
    (
        6,
        """
        CREATE TABLE retention_runs (
            run_id TEXT PRIMARY KEY,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            evaluation_time TEXT NOT NULL,
            policy_digest TEXT NOT NULL,
            cutoff_configuration TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('PLANNED','VERIFIED','EXECUTING','COMPLETED','BLOCKED','FAILED')),
            candidate_count INTEGER NOT NULL CHECK(candidate_count >= 0),
            covered_count INTEGER NOT NULL CHECK(covered_count >= 0),
            uncovered_count INTEGER NOT NULL CHECK(uncovered_count >= 0),
            planned_delete_count INTEGER NOT NULL CHECK(planned_delete_count >= 0),
            deleted_count INTEGER NOT NULL CHECK(deleted_count >= 0),
            failure_reason TEXT
        );
        CREATE INDEX idx_retention_runs_status_started ON retention_runs(status,started_at);
        CREATE TABLE retention_run_tables (
            run_id TEXT NOT NULL,
            table_name TEXT NOT NULL,
            candidate_rows INTEGER NOT NULL CHECK(candidate_rows >= 0),
            covered_rows INTEGER NOT NULL CHECK(covered_rows >= 0),
            uncovered_rows INTEGER NOT NULL CHECK(uncovered_rows >= 0),
            planned_deletes INTEGER NOT NULL CHECK(planned_deletes >= 0),
            actual_deletes INTEGER NOT NULL CHECK(actual_deletes >= 0),
            PRIMARY KEY(run_id,table_name),
            FOREIGN KEY(run_id) REFERENCES retention_runs(run_id)
        );
        """,
    ),
    (
        7,
        """
        CREATE TABLE retention_run_candidates (
            run_id TEXT NOT NULL,
            table_name TEXT NOT NULL,
            row_identity TEXT NOT NULL,
            PRIMARY KEY(run_id,table_name,row_identity),
            FOREIGN KEY(run_id,table_name) REFERENCES retention_run_tables(run_id,table_name)
        );
        """,
    ),
)


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=5.0, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=5000")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    return connection


def migrate(connection: sqlite3.Connection) -> None:
    connection.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL, checksum TEXT NOT NULL UNIQUE)")
    connection.execute("BEGIN IMMEDIATE")
    try:
        for version, sql in sorted(MIGRATIONS):
            checksum = hashlib.sha256(sql.encode()).hexdigest()
            row = connection.execute("SELECT checksum FROM schema_migrations WHERE version = ?", (version,)).fetchone()
            if row:
                if row[0] != checksum:
                    raise RuntimeError(f"migration checksum mismatch for version {version}")
                continue
            for statement in (part.strip() for part in sql.split(";")):
                if statement:
                    connection.execute(statement)
            connection.execute("INSERT INTO schema_migrations(version, applied_at, checksum) VALUES (?, ?, ?)", (version, utc_now(), checksum))
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def _health_status(received: int, rejected: int, last_error: str | None) -> str:
    if last_error and received == rejected:
        return "failed"
    if rejected or last_error:
        return "degraded"
    return "healthy"


def persist(
    connection: sqlite3.Connection,
    envelope: RawEnvelope,
    events: Iterable[CanonicalEvent],
    *,
    unknown_count: int = 0,
    normalization_error: str | None = None,
    source_class: str = "native_otel",
) -> int:
    event_list = list(events)
    prior = connection.execute(
        "SELECT ingest_id FROM raw_events WHERE source_event_type = ? AND payload_sha256 = ? AND parse_status IN ('accepted', 'duplicate') ORDER BY rowid LIMIT 1",
        (envelope.source_event_type, envelope.payload_sha256),
    ).fetchone()
    status = "duplicate" if prior else envelope.parse_status
    with connection:
        connection.execute(
            "INSERT INTO raw_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (envelope.ingest_id, envelope.received_at, envelope.source, envelope.source_instance, envelope.source_event_type,
             envelope.source_version, envelope.content_type, envelope.content_encoding, envelope.payload_encoding,
             envelope.payload_sha256, envelope.payload_size, envelope.payload_ref,
             envelope.payload if envelope.retention_class == "forensic" else None, status,
             envelope.error_code, envelope.error_message or normalization_error, envelope.retention_class,
             prior[0] if prior else None),
        )
        if prior:
            return 0
        for event in event_list:
            connection.execute(
                """INSERT INTO events (event_id,event_time,event_time_unix_nano,observed_at,source_class,fact_type,stability,
                source_event,source_instance,source_version,raw_event_sha256,adapter_version,session_id,thread_id,turn_id,
                item_id,call_id,trace_id,span_id,operation_id,category,name,status,attributes_json)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (event.event_id, event.event_time, event.event_time_unix_nano, event.observed_at, source_class, "native",
                 "documented" if event.name != "unknown" else "provider_extension", event.source_event, event.source_instance,
                 event.source_version, event.raw_event_sha256, event.adapter_version, event.session_id, event.thread_id,
                 event.turn_id, event.item_id, event.call_id, event.trace_id, event.span_id, event.operation_id,
                 event.category, event.name, event.status, __import__("json").dumps(event.attributes, sort_keys=True, separators=(",", ":"))),
            )
    return len(event_list)


def update_health(connection: sqlite3.Connection, *, received: int, normalized: int, rejected: int, unknown: int, persistence_error: int, error: str | None) -> None:
    current = connection.execute("SELECT * FROM collector_health WHERE collector='otlp'").fetchone()
    values = {
        "received_total": (current["received_total"] if current else 0) + received,
        "normalized_total": (current["normalized_total"] if current else 0) + normalized,
        "rejected_total": (current["rejected_total"] if current else 0) + rejected,
        "unknown_event_total": (current["unknown_event_total"] if current else 0) + unknown,
        "persistence_error_total": (current["persistence_error_total"] if current else 0) + persistence_error,
        "last_success": utc_now() if normalized else (current["last_success"] if current else None),
        "last_error": error or (current["last_error"] if current else None),
    }
    values["status"] = _health_status(cast(int, values["received_total"]), cast(int, values["rejected_total"]), values["last_error"] if isinstance(values["last_error"], str) else None)
    with connection:
        connection.execute(
            """INSERT INTO collector_health(collector,status,received_total,normalized_total,rejected_total,unknown_event_total,
            persistence_error_total,last_success,last_error,updated_at) VALUES ('otlp',?,?,?,?,?,?,?,?,?)
            ON CONFLICT(collector) DO UPDATE SET status=excluded.status,received_total=excluded.received_total,
            normalized_total=excluded.normalized_total,rejected_total=excluded.rejected_total,unknown_event_total=excluded.unknown_event_total,
            persistence_error_total=excluded.persistence_error_total,last_success=excluded.last_success,last_error=excluded.last_error,
            updated_at=excluded.updated_at""",
            (values["status"], values["received_total"], values["normalized_total"], values["rejected_total"], values["unknown_event_total"],
             values["persistence_error_total"], values["last_success"], values["last_error"], utc_now()),
        )
