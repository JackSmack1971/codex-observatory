from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from codex_observatory.app_server import ingest_notification, upsert_thread
from codex_observatory.hook import import_spool, run_hook, write_spool
from codex_observatory.sqlite import MIGRATIONS, connect, migrate


def db(tmp_path: Path) -> sqlite3.Connection:
    connection = connect(tmp_path / "observatory.db")
    migrate(connection)
    return connection


def event(name: str, **extra: object) -> dict[str, object]:
    return {"hook_event_name": name, "session_id": "sess_1", "cwd": "C:/fixture", "model": "test-model", **extra}


def ingest(connection: sqlite3.Connection, spool: Path, payload: dict[str, object]) -> None:
    write_spool(payload, spool)
    import_spool(connection, spool)


@pytest.mark.parametrize(
    ("hook_name", "canonical_name"),
    [("SessionStart", "session_start"), ("SessionEnd", "session_end"), ("Stop", "turn_stop"), ("Interrupt", "turn_interrupt")],
)
def test_lifecycle_normalization(tmp_path: Path, hook_name: str, canonical_name: str) -> None:
    connection = db(tmp_path)
    ingest(connection, tmp_path / "spool", event(hook_name, turn_id="turn_1"))
    row = connection.execute("SELECT category,name FROM events").fetchone()
    assert tuple(row) == ("session_lifecycle", canonical_name) if hook_name.startswith("Session") else tuple(row) == ("turn_lifecycle", canonical_name)


@pytest.mark.parametrize(
    "hook_name",
    ["PreCompact", "PostCompact", "SubagentStart", "SubagentStop", "PermissionRequest", "PostToolUse"],
)
def test_required_hook_events_are_persisted(tmp_path: Path, hook_name: str) -> None:
    connection = db(tmp_path)
    payload = event(hook_name, turn_id="turn_1", trigger="manual", agent_id="agent_1", agent_type="worker",
                    tool_name="Bash", tool_use_id="call_1", tool_input={"command": "echo secret"},
                    tool_response={"success": True, "output": "secret"})
    ingest(connection, tmp_path / "spool", payload)
    row = connection.execute("SELECT hook_event_name FROM hook_events").fetchone()
    assert row[0] == hook_name
    assert connection.execute("SELECT count(*) FROM events").fetchone()[0] == 1


def test_post_tool_use_nonzero_is_observed_as_failure(tmp_path: Path) -> None:
    connection = db(tmp_path)
    ingest(connection, tmp_path / "spool", event("PostToolUse", turn_id="turn_1", tool_name="Bash", tool_use_id="call_1", tool_response={"exit_code": 7}))
    row = connection.execute("SELECT status,attributes_json FROM events").fetchone()
    assert row[0] == "error"
    assert json.loads(row[1])["tool_result_status"] == "error"


def test_privacy_redacts_prompt_messages_and_tool_payloads_before_persistence(tmp_path: Path) -> None:
    connection = db(tmp_path)
    ingest(connection, tmp_path / "spool", event("UserPromptSubmit", prompt="TOP SECRET"))
    ingest(connection, tmp_path / "spool", event("Stop", last_assistant_message="TOP SECRET ASSISTANT"))
    ingest(connection, tmp_path / "spool", event("PostToolUse", tool_name="Bash", tool_use_id="call_1", tool_input={"command": "TOP SECRET"}, tool_response={"output": "TOP SECRET"}))
    rows = [row[0] for row in connection.execute("SELECT attributes_json FROM events")]
    assert all("TOP SECRET" not in value for value in rows)
    assert connection.execute("SELECT privacy_redaction_total FROM hook_source_state").fetchone()[0] == 4
    assert connection.execute("SELECT payload FROM raw_events WHERE source='hook'").fetchone()[0] is None


def test_permission_request_is_neutral_and_preserves_description_metadata(tmp_path: Path) -> None:
    connection = db(tmp_path)
    ingest(connection, tmp_path / "spool", event("PermissionRequest", tool_name="Bash", tool_input={"command": "rm -rf", "description": "approval description"}))
    row = connection.execute("SELECT name,attributes_json FROM events").fetchone()
    assert row[0] == "approval_request_observed"
    attrs = json.loads(row[1])
    assert attrs["decision"] == "none"
    assert attrs["tool_input"]["description"] == "approval description"
    assert "decision" not in (tmp_path / "spool").read_text(encoding="utf-8") if (tmp_path / "spool").is_file() else True


def test_unknown_hook_is_preserved_and_counted(tmp_path: Path) -> None:
    connection = db(tmp_path)
    ingest(connection, tmp_path / "spool", event("FutureHook", future_field="kept"))
    assert connection.execute("SELECT source_event,name FROM events").fetchone()[1] == "unknown"
    assert connection.execute("SELECT unknown_event_total FROM hook_source_state").fetchone()[0] == 1


def test_malformed_input_and_unavailable_collector_are_neutral(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    spool = tmp_path / "spool"
    assert run_hook("not json", spool) == 0
    assert run_hook(json.dumps(event("SessionStart")), tmp_path / "a-file") == 0
    assert capsys.readouterr().out == ""
    connection = db(tmp_path / "db")
    counts = import_spool(connection, spool)
    assert counts["failure"] == 1
    assert connection.execute("SELECT parse_status FROM raw_events").fetchone()[0] == "rejected"


def test_duplicate_restart_and_health_counters(tmp_path: Path) -> None:
    connection = db(tmp_path)
    spool = tmp_path / "spool"
    payload = event("SessionStart")
    write_spool(payload, spool)
    first = import_spool(connection, spool)
    write_spool(payload, spool)
    second = import_spool(connection, spool)
    assert first["normalized"] == 1 and second["duplicate"] == 1
    assert connection.execute("SELECT count(*) FROM events").fetchone()[0] == 1
    state = connection.execute("SELECT received_total,normalized_total,duplicate_total FROM hook_source_state").fetchone()
    assert tuple(state) == (2, 1, 1)


def test_concurrent_hook_delivery_is_safe(tmp_path: Path) -> None:
    spool = tmp_path / "spool"
    payloads = [event("PostToolUse", tool_use_id=f"call_{i}") for i in range(16)]
    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(lambda value: write_spool(value, spool), payloads))
    connection = db(tmp_path)
    counts = import_spool(connection, spool)
    assert counts["normalized"] == 16
    assert connection.execute("SELECT count(*) FROM hook_events").fetchone()[0] == 16


def test_correlation_prefers_native_ids_and_preserves_unresolved(tmp_path: Path) -> None:
    connection = db(tmp_path)
    with connection:
        connection.execute("INSERT INTO app_server_state(source_instance,status,updated_at) VALUES('app-server','healthy','now')")
    upsert_thread(connection, {"id": "sess_1"})
    ingest_notification(connection, "turn/completed", {"threadId": "sess_1", "turnId": "turn_1", "status": "completed"})
    ingest(connection, tmp_path / "spool", event("PostToolUse", turn_id="turn_1", tool_use_id="call_1"))
    ingest(connection, tmp_path / "spool", event("PostToolUse", session_id="sess_unseen", turn_id="unseen", tool_use_id="call_unseen"))
    methods = {row[0] for row in connection.execute("SELECT correlation_method FROM correlation_edges")}
    assert "turn_id" in methods
    assert "session_id" in methods
    assert connection.execute("SELECT correlation_total FROM hook_source_state").fetchone()[0] >= 1
    assert connection.execute("SELECT correlation_unresolved_total FROM hook_source_state").fetchone()[0] == 1


def test_fresh_and_phase2_to_phase3_migrations_match(tmp_path: Path) -> None:
    fresh = db(tmp_path / "fresh")
    phase2 = connect(tmp_path / "phase2" / "db.sqlite")
    for statement in (part.strip() for part in MIGRATIONS[0][1].split(";")):
        if statement:
            phase2.execute(statement)
    for statement in (part.strip() for part in MIGRATIONS[1][1].split(";")):
        if statement:
            phase2.execute(statement)
    phase2.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL, checksum TEXT NOT NULL UNIQUE)")
    import hashlib
    phase2.execute("INSERT INTO schema_migrations VALUES(1,'now',?)", (hashlib.sha256(MIGRATIONS[0][1].encode()).hexdigest(),))
    phase2.execute("INSERT INTO schema_migrations VALUES(2,'now',?)", (hashlib.sha256(MIGRATIONS[1][1].encode()).hexdigest(),))
    phase2.commit()
    migrate(phase2)
    assert [row[0] for row in fresh.execute("SELECT version FROM schema_migrations ORDER BY version")] == [1, 2, 3, 4, 5, 6, 7]
    assert [row[0] for row in phase2.execute("SELECT version FROM schema_migrations ORDER BY version")] == [1, 2, 3, 4, 5, 6, 7]
    assert {row[0] for row in fresh.execute("SELECT name FROM sqlite_master WHERE type='table'")} == {row[0] for row in phase2.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_no_transcript_file_is_read(tmp_path: Path) -> None:
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("should not be read", encoding="utf-8")
    connection = db(tmp_path / "db")
    ingest(connection, tmp_path / "spool", event("SessionStart", transcript_path=str(transcript)))
    assert connection.execute("SELECT count(*) FROM events").fetchone()[0] == 1
