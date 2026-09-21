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
from codex_observatory.retention import plan_retention, retention_health, run_retention
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


def _snapshot(connection: sqlite3.Connection, identity: str, captured_at: str) -> None:
    connection.execute(
        "INSERT OR IGNORE INTO repositories VALUES('repo','/repo',NULL,0,?,?)",
        (OLD, HOT),
    )
    connection.execute(
        "INSERT OR IGNORE INTO worktrees VALUES('tree','repo','/repo',?,?)",
        (OLD, HOT),
    )
    connection.execute(
        "INSERT INTO git_snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (identity, f"digest-{identity}", "repo", "tree", None, None, 0, "unborn", None,
         1, 0, 0, 0, 0, 0, 0, 0, 0, captured_at, "v1", f"evidence-{identity}"),
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
        _snapshot(connection, "git-cold", OLD)
        connection.execute(
            "INSERT INTO app_server_state(source_instance,status,updated_at) "
            "VALUES('gate','connected',?)", (HOT,),
        )
        connection.execute(
            "INSERT INTO app_server_token_usage VALUES('thread','turn','{\"total\":{\"totalTokens\":7}}',?,'gate')",
            (OLD,),
        )
        connection.commit()
        exported = export_dataset(connection, archive_root, "events")
        assert exported.status == "PUBLISHED" and exported.rows == 2
        export_dataset(connection, archive_root, "git_snapshots")
        export_dataset(connection, archive_root, "token_usage")
        assert verify_archive(connection, archive_root)["status"] == "healthy"

        # These rows deliberately are not in the published batch.
        _event(connection, "uncovered-old", OLD)
        _event(connection, "hot", HOT)
        _snapshot(connection, "git-hot", HOT)
        connection.commit()
        service = AnalyticsService(connection, archive_root)
        before_ids = service.event_identities()
        before_git = service.git_snapshot_identities()
        before_tokens = service.token_total()
        first_plan = plan_retention(
            connection,
            RetentionConfig(),
            archive_root=archive_root,
            evaluation_time=EVALUATION_TIME,
        )
        assert (first_plan.eligible_count, first_plan.covered_count) == (4, 3)
        assert first_plan.uncovered_count == 1
        assert connection.execute(
            "SELECT count(*) FROM events WHERE event_id='uncovered-old'"
        ).fetchone()[0] == 1

        # Publish the remaining event, then create a fresh proof before delete.
        remaining = export_dataset(connection, archive_root, "events")
        assert remaining.status == "PUBLISHED" and remaining.rows == 2
        second_plan = plan_retention(
            connection, RetentionConfig(), archive_root=archive_root,
            evaluation_time=EVALUATION_TIME,
        )
        assert (second_plan.eligible_count, second_plan.covered_count) == (4, 4)
        executed = run_retention(
            connection, RetentionConfig(), archive_root=archive_root,
            evaluation_time=EVALUATION_TIME,
        )
        assert executed.status == "COMPLETED" and executed.deleted_count == 4
        assert {
            row[0] for row in connection.execute("SELECT event_id FROM events")
        } == {"hot"}
        assert connection.execute(
            "SELECT snapshot_observation_id FROM git_snapshots"
        ).fetchone()[0] == "git-hot"
        after_ids = service.event_identities()
        assert before_ids == after_ids
        assert len(after_ids) == len(set(after_ids)) == 4
        assert before_git == service.git_snapshot_identities()
        assert before_tokens == service.token_total() == 7
        connection.close()

        # A new process would observe the same durable audit and live state.
        connection = connect(database)
        migrate(connection)
        durable = connection.execute(
            "SELECT status,deleted_count FROM retention_runs WHERE run_id=?",
            (executed.run_id,),
        ).fetchone()
        assert tuple(durable) == ("COMPLETED", 4)
        health = retention_health(connection, enabled=True)
        assert health["rows_deleted_total"] == 4
        rerun = run_retention(
            connection,
            RetentionConfig(),
            archive_root=archive_root,
            evaluation_time=EVALUATION_TIME,
        )
        assert rerun.status == "COMPLETED" and rerun.deleted_count == 0
        assert verify_archive(connection, archive_root)["status"] == "healthy"
        assert AnalyticsService(connection, archive_root).event_identities() == before_ids
        connection.close()
        return {
            "decision": "PHASE_7_VERIFIED",
            "candidates": first_plan.eligible_count,
            "covered": first_plan.covered_count,
            "uncovered": first_plan.uncovered_count,
            "planned": executed.planned_deletion_count,
            "deleted": executed.deleted_count,
            "retained": 1,
            "duplicates": len(after_ids) - len(set(after_ids)),
            "before_query_count": len(before_ids),
            "after_query_count": len(after_ids),
            "restart_result": durable["status"],
            "rerun_result": rerun.status,
            "rerun_deleted": rerun.deleted_count,
            "token_total": before_tokens,
            "git_snapshots": len(before_git),
        }


if __name__ == "__main__":
    print(json.dumps(run_gate(), sort_keys=True))
