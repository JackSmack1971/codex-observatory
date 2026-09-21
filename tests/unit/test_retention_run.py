from __future__ import annotations

import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

import codex_observatory.retention as retention_module
from codex_observatory.archive import export_dataset
from codex_observatory.config import RetentionConfig
from codex_observatory.retention import run_retention
from codex_observatory.sqlite import connect, migrate

NOW = datetime(2026, 9, 21, tzinfo=UTC)
OLD = "2026-01-01T00:00:00Z"


def _event(connection: sqlite3.Connection, identity: str) -> None:
    connection.execute(
        "INSERT INTO events(event_id,event_time,observed_at,source_class,fact_type,stability,"
        "source_event,source_instance,raw_event_sha256,adapter_version,category,name,attributes_json) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (identity, OLD, OLD, "HOOK", "OBSERVED", "OBSERVED", "test", "test",
         f"sha256:{identity}", "v1", "test", "test", "{}"),
    )


def _archived_fixture(tmp_path: Path) -> tuple[sqlite3.Connection, Path]:
    connection = connect(tmp_path / "copy.sqlite")
    migrate(connection)
    _event(connection, "covered")
    connection.commit()
    root = tmp_path / "archive"
    export_dataset(connection, root)
    return connection, root


def test_mixed_run_deletes_only_covered_and_audits_counts(tmp_path: Path) -> None:
    connection, root = _archived_fixture(tmp_path)
    _event(connection, "uncovered")
    connection.commit()

    result = run_retention(connection, RetentionConfig(), archive_root=root, evaluation_time=NOW)

    assert result.status == "COMPLETED"
    assert result.deleted_count == result.deleted_by_table["events"] == 1
    assert [row[0] for row in connection.execute("SELECT event_id FROM events")] == ["uncovered"]
    assert tuple(connection.execute(
        "SELECT status,candidate_count,covered_count,uncovered_count,planned_delete_count,deleted_count "
        "FROM retention_runs WHERE run_id=?", (result.run_id,),
    ).fetchone()) == ("COMPLETED", 2, 1, 1, 1, 1)
    assert connection.execute(
        "SELECT actual_deletes FROM retention_run_tables WHERE run_id=? AND table_name='events'",
        (result.run_id,),
    ).fetchone()[0] == 1

    rerun = run_retention(connection, RetentionConfig(), archive_root=root, evaluation_time=NOW)
    assert (rerun.status, rerun.deleted_count) == ("COMPLETED", 0)


def test_execution_failure_rolls_back_all_deletes(tmp_path: Path) -> None:
    connection, root = _archived_fixture(tmp_path)
    connection.execute(
        "CREATE TRIGGER reject_prune BEFORE DELETE ON events "
        "BEGIN SELECT RAISE(ABORT, 'injected delete failure'); END"
    )
    connection.commit()

    result = run_retention(connection, RetentionConfig(), archive_root=root, evaluation_time=NOW)

    assert result.status == "FAILED"
    assert result.deleted_count == 0
    assert connection.execute("SELECT count(*) FROM events").fetchone()[0] == 1
    assert "injected delete failure" in (result.failure_reason or "")


def test_candidate_drift_requires_replanning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    connection, root = _archived_fixture(tmp_path)
    original = retention_module.plan_retention

    def plan_then_change(*args, **kwargs):
        plan = original(*args, **kwargs)
        _event(connection, "late-candidate")
        connection.commit()
        return plan

    monkeypatch.setattr(retention_module, "plan_retention", plan_then_change)
    result = run_retention(connection, RetentionConfig(), archive_root=root, evaluation_time=NOW)

    assert result.status == "BLOCKED"
    assert "candidate set changed" in (result.failure_reason or "")
    assert {row[0] for row in connection.execute("SELECT event_id FROM events")} == {
        "covered", "late-candidate",
    }


def test_foreign_keys_raw_and_token_projection_are_never_cascaded(tmp_path: Path) -> None:
    connection, root = _archived_fixture(tmp_path)
    connection.execute(
        "INSERT INTO raw_events(ingest_id,received_at,source,source_instance,source_event_type,"
        "content_type,payload_encoding,payload_sha256,payload_size,parse_status,retention_class) "
        "VALUES('raw',?,'hook','test','test','application/json','json','sha256:raw',0,'accepted','metadata')",
        (OLD,),
    )
    connection.execute(
        "INSERT INTO app_server_state(source_instance,status,updated_at) VALUES('app','connected',?)", (OLD,),
    )
    connection.execute(
        "INSERT INTO app_server_token_usage VALUES('thread','turn','{}',?,'app')", (OLD,),
    )
    connection.commit()

    result = run_retention(connection, RetentionConfig(), archive_root=root, evaluation_time=NOW)

    assert result.status == "COMPLETED"
    assert connection.execute("SELECT count(*) FROM raw_events").fetchone()[0] == 1
    assert connection.execute("SELECT count(*) FROM app_server_token_usage").fetchone()[0] == 1
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_busy_writer_fails_within_configured_bound(tmp_path: Path) -> None:
    path = tmp_path / "busy.sqlite"
    locker = connect(path)
    migrate(locker)
    runner = connect(path)
    runner.execute("PRAGMA busy_timeout=50")
    locker.execute("BEGIN IMMEDIATE")
    started = time.monotonic()
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        run_retention(runner, RetentionConfig(), archive_root=tmp_path / "archive", evaluation_time=NOW)
    assert time.monotonic() - started < 1
    locker.rollback()


def test_concurrent_reader_keeps_snapshot_during_prune(tmp_path: Path) -> None:
    connection, root = _archived_fixture(tmp_path)
    reader = connect(tmp_path / "copy.sqlite")
    reader.execute("BEGIN")
    assert reader.execute("SELECT count(*) FROM events").fetchone()[0] == 1
    result = run_retention(connection, RetentionConfig(), archive_root=root, evaluation_time=NOW)
    assert result.status == "COMPLETED"
    assert reader.execute("SELECT count(*) FROM events").fetchone()[0] == 1
    reader.rollback()
    assert reader.execute("SELECT count(*) FROM events").fetchone()[0] == 0


def test_covered_git_snapshot_without_children_is_deleted(tmp_path: Path) -> None:
    connection = connect(tmp_path / "git-copy.sqlite")
    migrate(connection)
    connection.execute("INSERT INTO repositories VALUES('repo','/repo',NULL,0,?,?)", (OLD, OLD))
    connection.execute("INSERT INTO worktrees VALUES('tree','repo','/repo',?,?)", (OLD, OLD))
    connection.execute(
        "INSERT INTO git_snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("snapshot", "digest", "repo", "tree", None, None, 0, "unborn", None, 1,
         0, 0, 0, 0, 0, 0, 0, 0, OLD, "v1", "evidence"),
    )
    connection.commit()
    root = tmp_path / "archive"
    export_dataset(connection, root, "git_snapshots")

    result = run_retention(connection, RetentionConfig(), archive_root=root, evaluation_time=NOW)

    assert result.status == "COMPLETED"
    assert result.deleted_by_table["git_snapshots"] == 1
    assert connection.execute("SELECT count(*) FROM git_snapshots").fetchone()[0] == 0
    assert connection.execute("SELECT count(*) FROM repositories").fetchone()[0] == 1
