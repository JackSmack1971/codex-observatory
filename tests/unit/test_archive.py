from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from codex_observatory.analytics import AnalyticsService
from codex_observatory.archive import export_dataset, verify_archive
from codex_observatory.sqlite import connect, migrate


def _db(tmp_path: Path):
    connection = connect(tmp_path / "observatory.db")
    migrate(connection)
    return connection


def _event(connection, event_id: str, timestamp: str, source: str = "hook", category: str = "tool_observation", status: str | None = "ok") -> None:
    connection.execute("""INSERT INTO events(event_id,event_time,event_time_unix_nano,observed_at,source_class,fact_type,stability,source_event,source_instance,source_version,raw_event_sha256,adapter_version,category,name,status,attributes_json)
    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (event_id, timestamp, None, timestamp, source, "native", "documented", "event", "local", None, "sha256:source", "test", category, category, status, json.dumps({"repo_id": "repo-1", "safe": True})))


def test_export_empty_and_incremental_idempotent(tmp_path: Path) -> None:
    connection = _db(tmp_path)
    root = tmp_path / "archive"
    assert export_dataset(connection, root).status == "NOOP"
    _event(connection, "e1", "2026-09-21T10:00:00Z")
    connection.commit()
    first = export_dataset(connection, root)
    assert first.rows == 1
    assert export_dataset(connection, root).status == "NOOP"
    _event(connection, "e2", "2026-09-21T11:00:00Z", source="otel", category="api")
    connection.commit()
    second = export_dataset(connection, root)
    assert second.rows == 1
    assert connection.execute("SELECT last_watermark FROM archive_watermarks WHERE dataset='events'").fetchone()[0] == 2
    assert connection.execute("SELECT sum(row_count) FROM archive_files WHERE dataset='events'").fetchone()[0] == 2
    connection.close()


def test_manifest_and_sha_verification_detects_modified_file(tmp_path: Path) -> None:
    connection = _db(tmp_path)
    _event(connection, "e1", "2026-09-21T10:00:00Z")
    connection.commit()
    root = tmp_path / "archive"
    result = export_dataset(connection, root)
    manifest = json.loads((root / result.manifest_path).read_text())
    parquet = root / manifest["files"][0]["path"]
    original = parquet.read_bytes()
    parquet.write_bytes(original + b"corruption")
    verification = verify_archive(connection, root)
    assert verification["status"] == "degraded"
    assert verification["failures"]
    parquet.write_bytes(original)
    connection.close()


def test_duckdb_partition_and_typed_queries(tmp_path: Path) -> None:
    connection = _db(tmp_path)
    _event(connection, "e1", "2026-09-30T23:00:00Z", source="hook")
    _event(connection, "e2", "2026-10-01T00:00:00Z", source="otel", category="api", status=None)
    connection.commit()
    root = tmp_path / "archive"
    export_dataset(connection, root)
    service = AnalyticsService(connection, root)
    assert service.event_count("2026-10-01T00:00:00Z", "2026-11-01T00:00:00Z") == 1
    assert {row["source_class"] for row in service.events_by("source_class")} == {"hook", "otel"}
    assert service.events_by("category")[0]["category"] in {"api", "tool_observation"}
    assert service.events_by("repo_id")[0]["repo_id"] == "repo-1"
    with pytest.raises(ValueError):
        service.events_by("attributes_json")
    connection.close()


def test_hot_cold_queries_deduplicate_overlap_and_preserve_order(tmp_path: Path) -> None:
    connection = _db(tmp_path)
    root = tmp_path / "archive"
    _event(connection, "cold", "2026-01-01T00:00:00Z")
    _event(connection, "overlap", "2026-02-01T00:00:00Z")
    connection.commit()
    export_dataset(connection, root, "events")
    _event(connection, "hot", "2026-09-20T00:00:00Z")
    connection.commit()

    service = AnalyticsService(connection, root)
    assert service.event_identities() == ["hot", "overlap", "cold"]
    assert service.event_count("2026-09-01T00:00:00Z") == 1
    assert service.event_count(end="2026-03-01T00:00:00Z") == 2
    connection.execute("DELETE FROM events WHERE event_id='cold'")
    connection.commit()
    assert service.event_identities() == ["hot", "overlap", "cold"]
    connection.close()


def test_archive_has_no_raw_payload_and_manifest_digest(tmp_path: Path) -> None:
    connection = _db(tmp_path)
    _event(connection, "e1", "2026-09-21T10:00:00Z")
    connection.commit()
    root = tmp_path / "archive"
    result = export_dataset(connection, root)
    manifest_path = root / result.manifest_path
    manifest = json.loads(manifest_path.read_text())
    unsigned = dict(manifest)
    unsigned.pop("manifest_sha256")
    assert manifest["manifest_sha256"] == hashlib.sha256((json.dumps(unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode()).hexdigest()
    assert "TOP SECRET" not in manifest_path.read_text()
    connection.close()
