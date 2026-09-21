from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from codex_observatory.retention import (
    PRUNABLE_HISTORY_ORDER,
    PRUNABLE_HISTORY_TABLES,
    PRUNABLE_TIMESTAMP_COLUMNS,
    TABLE_CLASSIFICATIONS,
    RetentionDiagnosticV1,
    RetentionPlanV1,
    RetentionPolicyV1,
    RetentionTableClass,
    plan_retention,
    render_table_classification_markdown,
)
from codex_observatory.sqlite import MIGRATIONS, connect, migrate


def test_production_registry_matches_migrations(tmp_path: Path) -> None:
    connection = connect(tmp_path / "inventory.db")
    migrate(connection)
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    all_tables = {row[0] for row in rows}
    sqlite_internal_tables = {name for name in all_tables if name.startswith("sqlite_")}
    assert sqlite_internal_tables == {"sqlite_sequence"}
    actual_application_tables = all_tables - sqlite_internal_tables

    assert set(TABLE_CLASSIFICATIONS) == actual_application_tables
    assert all(
        isinstance(value, RetentionTableClass)
        for value in TABLE_CLASSIFICATIONS.values()
    )
    assert len(TABLE_CLASSIFICATIONS) == len(actual_application_tables)
    connection.close()


def test_migration_six_upgrade_matches_fresh_schema(tmp_path: Path) -> None:
    phase6 = connect(tmp_path / "phase6.db")
    phase6.execute(
        "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL, checksum TEXT NOT NULL UNIQUE)"
    )
    for version, sql in MIGRATIONS[:5]:
        phase6.executescript(sql)
        phase6.execute(
            "INSERT INTO schema_migrations VALUES (?, 'now', ?)",
            (version, hashlib.sha256(sql.encode()).hexdigest()),
        )
    phase6.commit()

    fresh = connect(tmp_path / "fresh.db")
    migrate(phase6)
    migrate(fresh)
    migrate(phase6)

    def schema(connection: sqlite3.Connection) -> list[tuple[str, str, str]]:
        return [
            tuple(row)
            for row in connection.execute(
                "SELECT type,name,sql FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
            )
        ]

    assert schema(phase6) == schema(fresh)
    assert [
        row[0]
        for row in phase6.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        )
    ] == [1, 2, 3, 4, 5, 6, 7]


def test_retention_audit_records_survive_restart(tmp_path: Path) -> None:
    path = tmp_path / "audit.db"
    connection = connect(path)
    migrate(connection)
    with connection:
        connection.execute(
            "INSERT INTO retention_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "run-1",
                "2026-09-21T00:00:00Z",
                None,
                "2026-09-21T00:00:00Z",
                "sha256:policy",
                '{"cutoff":"30d"}',
                "PLANNED",
                5,
                4,
                1,
                4,
                0,
                None,
            ),
        )
        connection.execute(
            "INSERT INTO retention_run_tables VALUES (?,?,?,?,?,?,?)",
            ("run-1", "events", 5, 4, 1, 4, 0),
        )
    connection.close()

    reopened = connect(path)
    migrate(reopened)
    assert tuple(
        reopened.execute(
            "SELECT status,candidate_count,covered_count,uncovered_count,planned_delete_count,deleted_count "
            "FROM retention_runs WHERE run_id='run-1'"
        ).fetchone()
    ) == ("PLANNED", 5, 4, 1, 4, 0)
    assert tuple(
        reopened.execute(
            "SELECT table_name,candidate_rows,covered_rows,uncovered_rows,planned_deletes,actual_deletes "
            "FROM retention_run_tables WHERE run_id='run-1'"
        ).fetchone()
    ) == ("events", 5, 4, 1, 4, 0)
    reopened.close()


def test_retention_audit_constraints_reject_invalid_evidence(tmp_path: Path) -> None:
    connection = connect(tmp_path / "constraints.db")
    migrate(connection)
    insert_run = (
        "INSERT INTO retention_runs VALUES "
        "(?, '2026-09-21T00:00:00Z', NULL, '2026-09-21T00:00:00Z', "
        "'sha256:policy', '{}', ?, ?, 0, 0, 0, 0, NULL)"
    )

    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(insert_run, ("invalid-status", "UNKNOWN", 0))
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(insert_run, ("negative-count", "PLANNED", -1))
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO retention_run_tables VALUES (?,?,?,?,?,?,?)",
            ("missing-run", "events", 0, 0, 0, 0, 0),
        )
    connection.close()


def test_documented_registry_is_generated_from_production_contract() -> None:
    documentation = Path("docs/retention.md").read_text()
    start = "<!-- TABLE_CLASSIFICATIONS:START -->\n"
    end = "\n<!-- TABLE_CLASSIFICATIONS:END -->"
    documented = documentation.split(start, 1)[1].split(end, 1)[0]
    assert documented == render_table_classification_markdown()


def test_token_usage_is_a_current_projection_only() -> None:
    assert (
        TABLE_CLASSIFICATIONS["app_server_token_usage"]
        is RetentionTableClass.CURRENT_PROJECTION
    )
    assert "app_server_token_usage" not in PRUNABLE_HISTORY_TABLES
    assert "app_server_token_usage" not in PRUNABLE_HISTORY_ORDER
    assert "app_server_token_usage" not in PRUNABLE_TIMESTAMP_COLUMNS


def test_retention_plan_represents_immutable_diagnostics() -> None:
    diagnostic = RetentionDiagnosticV1(
        table="events",
        timestamp_column="event_time",
        row_identity="event_id=bad-time",
        reason="timestamp is null",
    )
    plan = RetentionPlanV1(diagnostics=(diagnostic,))
    assert plan.diagnostics == (diagnostic,)
    with pytest.raises((AttributeError, TypeError)):
        plan.diagnostics = ()  # type: ignore[misc]


def test_policy_accepts_only_validated_timezone_aware_datetimes() -> None:
    policy = RetentionPolicyV1(cutoff=datetime(2026, 9, 21, tzinfo=UTC))
    naive_timestamp = datetime(2026, 9, 20, tzinfo=UTC).replace(tzinfo=None)
    naive_cutoff = datetime(2026, 9, 21, tzinfo=UTC).replace(tzinfo=None)
    assert policy.eligible(datetime(2026, 9, 20, tzinfo=UTC))
    assert not policy.eligible(datetime(2026, 9, 22, tzinfo=UTC))
    with pytest.raises(ValueError, match="timezone-aware"):
        policy.eligible(naive_timestamp)
    with pytest.raises(TypeError, match="must be a datetime"):
        policy.eligible("invalid")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="timezone-aware"):
        RetentionPolicyV1(cutoff=naive_cutoff)


def _raw(connection: sqlite3.Connection, identity: str, timestamp: str, retention_class: str = "metadata") -> None:
    connection.execute(
        "INSERT INTO raw_events(ingest_id,received_at,source,source_instance,source_event_type,source_version,content_type,content_encoding,payload_encoding,payload_sha256,payload_size,payload_ref,payload,parse_status,error_code,error_message,retention_class,duplicate_of) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (identity, timestamp, "hook", "test", "test", None, "application/json", None,
         "json", f"sha256:{identity}", 2, None, None, "accepted", None, None, retention_class, None),
    )


def _event(connection: sqlite3.Connection, identity: str, timestamp: str) -> None:
    connection.execute(
        "INSERT INTO events(event_id,event_time,observed_at,source_class,fact_type,stability,source_event,source_instance,raw_event_sha256,adapter_version,category,name,attributes_json) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (identity, timestamp, timestamp, "HOOK", "OBSERVED", "OBSERVED", "test", "test",
         f"sha256:{identity}", "v1", "test", "test", "{}"),
    )


def test_plan_is_deterministic_dry_run_across_tables_and_persists(tmp_path: Path) -> None:
    from codex_observatory.config import RetentionConfig

    path = tmp_path / "plan.db"
    connection = connect(path)
    migrate(connection)
    # raw metadata uses 7 days, forensic raw uses 2, while canonical events use hot_days.
    _raw(connection, "raw-expired", "2026-09-10T00:00:00Z")
    _raw(connection, "raw-hot", "2026-09-19T00:00:00Z")
    _raw(connection, "raw-forensic-expired", "2026-09-18T23:59:59Z", "forensic")
    _event(connection, "event-expired", "2026-08-21T23:59:59Z")
    _event(connection, "event-boundary", "2026-08-22T00:00:00Z")
    _event(connection, "event-hot", "2026-09-20T00:00:00Z")
    connection.commit()
    before = {table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in ("raw_events", "events")}
    statements: list[str] = []
    connection.set_trace_callback(statements.append)

    plan = plan_retention(
        connection,
        RetentionConfig(hot_days=30, raw_metadata_days=7, forensic_raw_days=2),
        evaluation_time=datetime(2026, 9, 21, tzinfo=UTC),
    )

    assert plan.evaluation_time == "2026-09-21T00:00:00Z"
    assert plan.cutoffs == {
        "hot": "2026-08-22T00:00:00Z",
        "raw_metadata": "2026-09-14T00:00:00Z",
        "forensic_raw": "2026-09-19T00:00:00Z",
    }
    assert plan.candidates_by_table["raw_events"] == ("raw-expired", "raw-forensic-expired")
    assert len(plan.candidates_by_table["events"]) == 1
    assert plan.eligible_count == 3
    assert plan.ineligible_count == 3
    assert plan.archive_coverage_status == "NOT_YET_VERIFIED"
    assert plan.planned_deletion_count == 0
    assert not any(statement.lstrip().upper().startswith("DELETE") for statement in statements)
    after = {table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in before}
    assert after == before
    run_id = plan.run_id
    connection.close()

    reopened = connect(path)
    migrate(reopened)
    assert reopened.execute("SELECT status FROM retention_runs WHERE run_id=?", (run_id,)).fetchone()[0] == "PLANNED"
    assert [row[0] for row in reopened.execute(
        "SELECT row_identity FROM retention_run_candidates WHERE run_id=? ORDER BY table_name,row_identity", (run_id,)
    )] == ["1", "raw-expired", "raw-forensic-expired"]


def test_plan_disabled_and_empty_database_have_no_candidates(tmp_path: Path) -> None:
    from codex_observatory.config import RetentionConfig

    connection = connect(tmp_path / "disabled.db")
    migrate(connection)
    _event(connection, "old", "2000-01-01T00:00:00Z")
    disabled = plan_retention(connection, RetentionConfig(enabled=False), evaluation_time=datetime(2026, 9, 21, tzinfo=UTC))
    assert disabled.eligible_count == 0
    assert disabled.ineligible_count == 1

    empty = connect(tmp_path / "empty.db")
    migrate(empty)
    no_rows = plan_retention(empty, RetentionConfig(), evaluation_time=datetime(2026, 9, 21, tzinfo=UTC))
    assert no_rows.eligible_count == no_rows.ineligible_count == 0
