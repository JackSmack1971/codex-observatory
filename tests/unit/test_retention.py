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
    render_table_classification_markdown,
)
from codex_observatory.sqlite import MIGRATIONS, connect, migrate


def test_production_registry_matches_migrations_one_through_six(tmp_path: Path) -> None:
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
    ] == [1, 2, 3, 4, 5, 6]


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
