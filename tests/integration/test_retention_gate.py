from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import duckdb

from codex_observatory.analytics import AnalyticsService
from codex_observatory.archive import export_dataset, published_paths, verify_archive
from codex_observatory.config import RetentionConfig
from codex_observatory.retention import plan_retention, retention_health, run_retention
from codex_observatory.sqlite import connect, migrate

NOW = datetime(2026, 9, 21, tzinfo=UTC)
OLD = "2026-01-01T00:00:00Z"
HOT = "2026-09-20T00:00:00Z"


def _event(connection, event_id: str, timestamp: str) -> None:
    connection.execute(
        "INSERT INTO events(event_id,event_time,observed_at,source_class,fact_type,stability,"
        "source_event,source_instance,raw_event_sha256,adapter_version,category,name,attributes_json) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (event_id, timestamp, timestamp, "HOOK", "OBSERVED", "OBSERVED", "gate", "gate",
         f"sha256:{event_id}", "v1", "tool", event_id, "{}"),
    )


def _snapshot(connection, identity: str, timestamp: str) -> None:
    connection.execute(
        "INSERT INTO git_snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (identity, f"digest-{identity}", "repo", "tree", None, None, 0, "unborn", None,
         1, 0, 0, 0, 0, 0, 0, 0, 0, timestamp, "v1", f"evidence-{identity}"),
    )


def test_complete_disposable_retention_lifecycle(tmp_path: Path) -> None:
    db_path = tmp_path / "disposable.sqlite"
    archive_root = tmp_path / "archive"
    connection = connect(db_path)
    migrate(connection)
    connection.execute("INSERT INTO repositories VALUES('repo','/repo',NULL,0,?,?)", (OLD, HOT))
    connection.execute("INSERT INTO worktrees VALUES('tree','repo','/repo',?,?)", (OLD, HOT))
    _event(connection, "expired-archived-first", OLD)
    connection.commit()
    assert export_dataset(connection, archive_root, "events").rows == 1

    _event(connection, "expired-archived-later", OLD)
    _event(connection, "hot-event", HOT)
    _snapshot(connection, "expired-snapshot", OLD)
    _snapshot(connection, "hot-snapshot", HOT)
    connection.execute(
        "INSERT INTO raw_events(ingest_id,received_at,source,source_instance,source_event_type,"
        "content_type,payload_encoding,payload_sha256,payload_size,parse_status,retention_class) "
        "VALUES('expired-raw',?,'hook','gate','gate','application/json','json',"
        "'sha256:raw',0,'accepted','metadata')", (OLD,),
    )
    connection.execute(
        "INSERT INTO app_server_state(source_instance,status,updated_at) VALUES('gate','connected',?)",
        (HOT,),
    )
    connection.execute(
        "INSERT INTO app_server_token_usage VALUES('thread','turn',?,?,'gate')",
        (json.dumps({"total": {"totalTokens": 7}}), OLD),
    )
    connection.commit()

    tracked = ("raw_events", "events", "app_server_token_usage", "git_snapshots")
    before = {table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in tracked}
    first_plan = plan_retention(connection, RetentionConfig(), archive_root=archive_root, evaluation_time=NOW)
    assert first_plan.coverage_by_table["events"] == {
        "expired-archived-first": "COVERED", "expired-archived-later": "UNCOVERED",
    }
    assert first_plan.coverage_by_table["git_snapshots"] == {"expired-snapshot": "UNCOVERED"}
    assert first_plan.coverage_by_table["raw_events"] == {"expired-raw": "UNCOVERED"}
    assert first_plan.planned_deletion_count == 1
    assert {table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in tracked} == before

    assert export_dataset(connection, archive_root, "events").rows == 2
    assert export_dataset(connection, archive_root, "git_snapshots").rows == 2
    assert export_dataset(connection, archive_root, "token_usage").rows == 1
    verification = verify_archive(connection, archive_root)
    assert verification["status"] == "healthy"
    second_plan = plan_retention(connection, RetentionConfig(), archive_root=archive_root, evaluation_time=NOW)
    assert second_plan.covered_count == 3
    assert second_plan.uncovered_count == 1
    assert second_plan.planned_deletion_count == 3

    analytics = AnalyticsService(connection, archive_root)
    historical_before = {
        "events": analytics.event_count(),
        "tokens": analytics.token_total(),
    }
    event_files = published_paths(connection, archive_root, "events")
    quoted = ",".join("'" + str(path).replace("'", "''") + "'" for path in event_files)
    archive_query = duckdb.connect(":memory:")
    duplicate_count = archive_query.execute(
        f"SELECT count(*) FROM (SELECT event_id FROM read_parquet([{quoted}]) "
        "GROUP BY event_id HAVING count(*) > 1)"
    ).fetchone()[0]
    archive_query.close()
    assert duplicate_count == 0

    result = run_retention(connection, RetentionConfig(), archive_root=archive_root, evaluation_time=NOW)
    assert result.status == "COMPLETED"
    assert result.deleted_by_table["events"] == 2
    assert result.deleted_by_table["git_snapshots"] == 1
    assert connection.execute("SELECT event_id FROM events").fetchall()[0][0] == "hot-event"
    assert connection.execute("SELECT snapshot_observation_id FROM git_snapshots").fetchall()[0][0] == "hot-snapshot"
    assert connection.execute("SELECT count(*) FROM raw_events").fetchone()[0] == 1
    assert connection.execute("SELECT count(*) FROM app_server_token_usage").fetchone()[0] == 1
    assert historical_before == {"events": 3, "tokens": 7}
    assert {"events": analytics.event_count(), "tokens": analytics.token_total()} == historical_before
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    run_id = result.run_id
    connection.close()

    restarted = connect(db_path)
    migrate(restarted)
    assert tuple(restarted.execute(
        "SELECT status,deleted_count FROM retention_runs WHERE run_id=?", (run_id,),
    ).fetchone()) == ("COMPLETED", 3)
    health = retention_health(restarted, RetentionConfig())
    assert health["status"] == "RETENTION_DEGRADED"
    assert health["counters"] == {
        "runs": 3, "dry_runs": 2, "completed": 1, "blocked": 0, "failed": 0,
        "eligible": 12, "verified": 7, "deleted": 3, "uncovered": 5,
    }
    rerun = run_retention(restarted, RetentionConfig(), archive_root=archive_root, evaluation_time=NOW)
    assert (rerun.status, rerun.deleted_count) == ("COMPLETED", 0)
    assert {table: restarted.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in tracked} == {
        "raw_events": 1, "events": 1, "app_server_token_usage": 1, "git_snapshots": 1,
    }
    restarted.close()
