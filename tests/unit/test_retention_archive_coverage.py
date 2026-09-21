from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pytest

from codex_observatory.archive import export_dataset
from codex_observatory.config import RetentionConfig
from codex_observatory.retention import plan_retention
from codex_observatory.sqlite import connect, migrate

NOW = datetime(2026, 9, 21, tzinfo=UTC)
OLD = "2026-01-01T00:00:00Z"


def _event(connection, identity: str) -> None:
    connection.execute(
        "INSERT INTO events(event_id,event_time,observed_at,source_class,fact_type,stability,"
        "source_event,source_instance,raw_event_sha256,adapter_version,category,name,attributes_json) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (identity, OLD, OLD, "HOOK", "OBSERVED", "OBSERVED", "test", "test", "sha256:x", "v1", "test", "test", "{}"),
    )


def _fixture(tmp_path: Path):
    connection = connect(tmp_path / "db.sqlite")
    migrate(connection)
    _event(connection, "covered")
    connection.commit()
    root = tmp_path / "archive"
    result = export_dataset(connection, root)
    manifest_path = root / result.manifest_path
    return connection, root, result.batch_id, manifest_path


def _rewrite_manifest(connection, batch_id: str, path: Path, manifest: dict) -> None:
    unsigned = dict(manifest)
    unsigned.pop("manifest_sha256", None)
    digest = hashlib.sha256((json.dumps(unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode()).hexdigest()
    manifest["manifest_sha256"] = digest
    path.write_text(json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n")
    connection.execute("UPDATE archive_batches SET manifest_digest=? WHERE batch_id=?", (digest, batch_id))
    connection.commit()


def _plan(connection, root):
    return plan_retention(connection, RetentionConfig(), archive_root=root, evaluation_time=NOW)


def test_valid_archive_is_covered_and_verification_never_deletes(tmp_path: Path) -> None:
    connection, root, _, _ = _fixture(tmp_path)
    before = connection.execute("SELECT count(*) FROM events").fetchone()[0]
    statements: list[str] = []
    connection.set_trace_callback(statements.append)
    plan = _plan(connection, root)
    assert plan.coverage_by_table["events"] == {"covered": "COVERED"}
    assert (plan.eligible_count, plan.covered_count, plan.uncovered_count, plan.planned_deletion_count) == (1, 1, 0, 1)
    assert plan.status == "VERIFIED"
    assert connection.execute("SELECT count(*) FROM events").fetchone()[0] == before
    assert not any(statement.lstrip().upper().startswith("DELETE") for statement in statements)


@pytest.mark.parametrize("damage", [
    "missing_manifest", "invalid_manifest_digest", "missing_parquet", "changed_file_digest",
    "unreadable_file", "missing_row_identity", "wrong_dataset", "wrong_schema", "traversal",
])
def test_invalid_archive_evidence_blocks_candidate(tmp_path: Path, damage: str) -> None:
    connection, root, batch_id, manifest_path = _fixture(tmp_path)
    manifest = json.loads(manifest_path.read_text())
    parquet_path = root / manifest["files"][0]["path"]
    if damage == "missing_manifest":
        manifest_path.unlink()
    elif damage == "invalid_manifest_digest":
        manifest["total_rows"] = 99
        manifest_path.write_text(json.dumps(manifest))
    elif damage == "missing_parquet":
        parquet_path.unlink()
    elif damage == "changed_file_digest":
        parquet_path.write_bytes(parquet_path.read_bytes() + b"changed")
    elif damage == "unreadable_file":
        parquet_path.write_bytes(b"not parquet")
        digest = hashlib.sha256(parquet_path.read_bytes()).hexdigest()
        manifest["files"][0]["sha256"] = digest
        connection.execute("UPDATE archive_files SET sha256=? WHERE batch_id=?", (digest, batch_id))
        _rewrite_manifest(connection, batch_id, manifest_path, manifest)
    elif damage == "missing_row_identity":
        replacement = parquet_path.with_suffix(".replacement")
        db = duckdb.connect(":memory:")
        escaped_in = str(parquet_path).replace("'", "''")
        escaped_out = str(replacement).replace("'", "''")
        db.execute(f"COPY (SELECT * EXCLUDE(event_id) FROM read_parquet('{escaped_in}')) TO '{escaped_out}' (FORMAT parquet)")
        db.close()
        replacement.replace(parquet_path)
        digest = hashlib.sha256(parquet_path.read_bytes()).hexdigest()
        manifest["files"][0]["sha256"] = digest
        connection.execute("UPDATE archive_files SET sha256=? WHERE batch_id=?", (digest, batch_id))
        _rewrite_manifest(connection, batch_id, manifest_path, manifest)
    elif damage == "wrong_dataset":
        manifest["dataset"] = "git_snapshots"
        _rewrite_manifest(connection, batch_id, manifest_path, manifest)
    elif damage == "wrong_schema":
        manifest["canonical_schema_version"] = "wrong.v1"
        _rewrite_manifest(connection, batch_id, manifest_path, manifest)
    else:
        connection.execute("UPDATE archive_batches SET manifest_path='../outside.json' WHERE batch_id=?", (batch_id,))
        connection.commit()

    plan = _plan(connection, root)
    assert plan.coverage_by_table["events"] == {"covered": "BLOCKED"}
    assert plan.covered_count == plan.planned_deletion_count == 0
    assert plan.blocked_count == 1
    assert plan.status == "BLOCKED"


def test_partial_and_unmapped_coverage_never_enters_deletion_set(tmp_path: Path) -> None:
    connection, root, _, _ = _fixture(tmp_path)
    _event(connection, "not-exported")
    connection.commit()
    # raw_events has no safe Phase 5 identity mapping and therefore remains uncovered.
    connection.execute(
        "INSERT INTO raw_events(ingest_id,received_at,source,source_instance,source_event_type,content_type,"
        "payload_encoding,payload_sha256,payload_size,parse_status,retention_class) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        ("raw", OLD, "hook", "test", "test", "application/json", "json", "sha256:x", 0, "accepted", "metadata"),
    )
    connection.commit()
    plan = _plan(connection, root)
    assert plan.coverage_by_table["events"] == {"covered": "COVERED", "not-exported": "UNCOVERED"}
    assert plan.coverage_by_table["raw_events"] == {"raw": "UNCOVERED"}
    assert plan.planned_deletion_count == 1
    assert plan.covered_count == 1
    assert plan.uncovered_count == 2
