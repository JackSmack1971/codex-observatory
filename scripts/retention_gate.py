#!/usr/bin/env python3
"""Disposable end-to-end verification gate for the Phase 7 retention lifecycle."""

from __future__ import annotations

import json
import sqlite3
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from codex_observatory.analytics import AnalyticsService
from codex_observatory.archive import export_dataset, verify_archive
from codex_observatory.config import RetentionConfig
from codex_observatory.retention import retention_health, run_retention
from codex_observatory.sqlite import connect, migrate

EVALUATION_TIME = datetime(2026, 9, 21, tzinfo=UTC)
OLD = "2026-01-01T00:00:00Z"
HOT = "2026-09-20T00:00:00Z"


def _event(connection: sqlite3.Connection, identity: str, event_time: str) -> None:
    connection.execute(
        "INSERT INTO events(event_id,event_time,observed_at,source_class,fact_type,stability,"
        "source_event,source_instance,raw_event_sha256,adapter_version,category,name,attributes_json) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            identity,
            event_time,
            event_time,
            "HOOK",
            "OBSERVED",
            "OBSERVED",
            "retention-gate",
            "retention-gate",
            f"sha256:{identity}",
            "v1",
            "gate",
            identity,
            "{}",
        ),
    )


def run_gate() -> dict[str, object]:
    with tempfile.TemporaryDirectory(
        prefix="codex-observatory-retention-"
    ) as directory:
        root = Path(directory)
        database = root / "observatory.sqlite"
        archive_root = root / "archive"
        connection = connect(database)
        migrate(connection)
        _event(connection, "archived-one", OLD)
        _event(connection, "archived-two", OLD)
        connection.commit()
        exported = export_dataset(connection, archive_root)
        assert exported.status == "PUBLISHED" and exported.rows == 2
        assert verify_archive(connection, archive_root)["status"] == "healthy"

        # These rows deliberately are not in the published batch.
        _event(connection, "uncovered-old", OLD)
        _event(connection, "hot", HOT)
        connection.commit()
        first = run_retention(
            connection,
            RetentionConfig(),
            archive_root=archive_root,
            evaluation_time=EVALUATION_TIME,
        )
        assert first.status == "COMPLETED" and first.deleted_count == 2
        assert {
            row[0] for row in connection.execute("SELECT event_id FROM events")
        } == {
            "uncovered-old",
            "hot",
        }
        assert AnalyticsService(connection, archive_root).event_count() == 2
        connection.close()

        # A new process would observe the same durable audit and live state.
        connection = connect(database)
        migrate(connection)
        durable = connection.execute(
            "SELECT status,deleted_count FROM retention_runs WHERE run_id=?",
            (first.run_id,),
        ).fetchone()
        assert tuple(durable) == ("COMPLETED", 2)
        assert retention_health(connection, enabled=True)["rows_deleted_total"] == 2
        second = run_retention(
            connection,
            RetentionConfig(),
            archive_root=archive_root,
            evaluation_time=EVALUATION_TIME,
        )
        assert second.status == "COMPLETED" and second.deleted_count == 0
        assert verify_archive(connection, archive_root)["status"] == "healthy"
        connection.close()
        return {
            "decision": "PHASE_7_VERIFIED",
            "first_deleted": first.deleted_count,
            "rerun_deleted": second.deleted_count,
            "historical_events": 2,
            "preserved_live_events": ["hot", "uncovered-old"],
        }


if __name__ == "__main__":
    print(json.dumps(run_gate(), sort_keys=True))
