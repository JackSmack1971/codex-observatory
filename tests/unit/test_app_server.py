from __future__ import annotations

import hashlib
import io
import json
import sqlite3
from pathlib import Path

from codex_observatory.app_server import (
    AppServerClient,
    AppServerError,
    JsonlTransport,
    ProtocolError,
    apply_item,
    apply_turn,
    ingest_notification,
    reconcile,
    set_app_server_health,
    upsert_thread,
)
from codex_observatory.sqlite import connect, migrate


def db(tmp_path: Path) -> sqlite3.Connection:
    connection = connect(tmp_path / "observatory.db")
    migrate(connection)
    connection.execute("INSERT INTO app_server_state(source_instance,status,updated_at) VALUES('app-server','connecting','now')")
    connection.commit()
    return connection


def test_allowlist_fails_closed() -> None:
    transport = JsonlTransport(["unused"])
    try:
        transport.send_request("turn/start")
    except AppServerError as exc:
        assert "not allowed" in str(exc)
    else:
        raise AssertionError("mutating method escaped allowlist")


class FakeProcess:
    def __init__(self, output: str) -> None:
        self.stdin = io.StringIO()
        self.stdout = io.StringIO(output)

    def poll(self) -> int | None:
        return None


def test_transport_initialize_response_and_notification_framing() -> None:
    process = FakeProcess(json.dumps({"id": 1, "result": {"userAgent": "codex-test"}}) + "\n")
    client = AppServerClient(JsonlTransport(["unused"], process=process))
    assert client.initialize()["userAgent"] == "codex-test"
    assert json.loads(process.stdin.getvalue().splitlines()[0])["method"] == "initialize"
    assert json.loads(process.stdin.getvalue().splitlines()[1])["method"] == "initialized"


def test_transport_notification_malformed_error_and_unexpected_id() -> None:
    notification = FakeProcess('{"method":"warning","params":{"message":"notice"}}\n')
    assert next(JsonlTransport(["unused"], process=notification).messages())["method"] == "warning"
    malformed = FakeProcess("not-json\n")
    try:
        list(JsonlTransport(["unused"], process=malformed).messages())
    except ProtocolError:
        pass
    else:
        raise AssertionError("malformed protocol message was accepted")
    structured = FakeProcess(json.dumps({"id": 1, "error": {"code": -32600, "message": "bad"}}) + "\n")
    try:
        AppServerClient(JsonlTransport(["unused"], process=structured)).initialize()
    except AppServerError as exc:
        assert "bad" in str(exc)
    else:
        raise AssertionError("structured protocol error was not surfaced")
    unexpected = FakeProcess('{"id":99,"result":{}}\n')
    client = AppServerClient(JsonlTransport(["unused"], process=unexpected))
    client.initialized = True
    try:
        client.request("thread/list", {})
    except ProtocolError:
        pass
    else:
        raise AssertionError("unexpected response id was accepted")


def test_transport_eof_is_disconnect() -> None:
    client = AppServerClient(JsonlTransport(["unused"], process=FakeProcess("")))
    try:
        client.initialize()
    except AppServerError as exc:
        assert "disconnected" in str(exc)
    else:
        raise AssertionError("EOF was not reported as disconnect")


def test_thread_turn_item_lifecycle_is_idempotent_and_final_wins(tmp_path: Path) -> None:
    connection = db(tmp_path)
    upsert_thread(connection, {"id": "thr_1", "name": "safe", "createdAt": 1})
    apply_turn(connection, "thr_1", {"id": "turn_1", "status": "completed"})
    apply_turn(connection, "thr_1", {"id": "turn_1", "status": "inProgress"})
    apply_item(connection, "thr_1", "turn_1", {"id": "item_1", "type": "agentMessage", "text": "secret"}, final=True)
    apply_item(connection, "thr_1", "turn_1", {"id": "item_1", "type": "agentMessage", "text": "older"}, final=False)
    assert connection.execute("SELECT status FROM turns WHERE turn_id='turn_1'").fetchone()[0] == "completed"
    item = connection.execute("SELECT final,content_json FROM thread_items WHERE item_id='item_1'").fetchone()
    assert item[0] == 1
    assert "secret" not in item[1]


def test_unknown_warning_token_usage_and_status_are_preserved(tmp_path: Path) -> None:
    connection = db(tmp_path)
    ingest_notification(connection, "future/event", {"threadId": "thr_1", "secret": "no"})
    ingest_notification(connection, "warning", {"message": "notice"})
    ingest_notification(connection, "thread/tokenUsage/updated", {"threadId": "thr_1", "turnId": "turn_1", "tokenUsage": {"total": {"totalTokens": 3}}})
    assert connection.execute("SELECT known FROM app_server_messages WHERE method='future/event'").fetchone()[0] == 0
    assert connection.execute("SELECT unknown_notification_total FROM app_server_state").fetchone()[0] == 1
    assert connection.execute("SELECT count(*) FROM app_server_token_usage").fetchone()[0] == 1


def test_archived_and_unarchived_notifications_update_thread_state(tmp_path: Path) -> None:
    connection = db(tmp_path)
    upsert_thread(connection, {"id": "thr_1"}, loaded=True)
    ingest_notification(connection, "thread/archived", {"threadId": "thr_1"})
    assert tuple(connection.execute("SELECT archived,loaded FROM threads WHERE thread_id='thr_1'").fetchone()) == (1, 0)
    ingest_notification(connection, "thread/unarchived", {"threadId": "thr_1"})
    assert connection.execute("SELECT archived FROM threads WHERE thread_id='thr_1'").fetchone()[0] == 0


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def initialize(self) -> dict[str, object]:
        return {"serverInfo": {"version": "test"}}

    def request(self, method: str, params: dict[str, object]) -> dict[str, object]:
        self.calls.append((method, params))
        if method == "thread/list":
            return {"data": [{"id": "thr_1", "updatedAt": 1}], "nextCursor": None}
        if method == "thread/read":
            return {"thread": {"id": "thr_1", "turns": [{"id": "turn_1", "status": "completed", "items": [{"id": "item_1", "type": "fileChange"}]}]}}
        if method == "thread/loaded/list":
            return {"data": ["thr_1"]}
        raise AssertionError(method)


def test_reconcile_uses_only_stable_reads_and_restart_does_not_duplicate(tmp_path: Path) -> None:
    connection = db(tmp_path)
    client = FakeClient()
    reconcile(connection, client)
    reconcile(connection, client)
    methods = [method for method, _ in client.calls]
    assert "thread/list" in methods and "thread/read" in methods and "thread/loaded/list" in methods
    assert all(method not in {"thread/turns/list", "thread/items/list", "thread/resume"} for method in methods)
    assert connection.execute("SELECT count(*) FROM threads").fetchone()[0] == 1
    assert connection.execute("SELECT count(*) FROM turns").fetchone()[0] == 1
    assert connection.execute("SELECT count(*) FROM thread_items").fetchone()[0] == 1
    assert connection.execute("SELECT loaded FROM threads WHERE thread_id='thr_1'").fetchone()[0] == 1


def test_reconcile_paginates_cursor(tmp_path: Path) -> None:
    connection = db(tmp_path)

    class Paged(FakeClient):
        def request(self, method: str, params: dict[str, object]) -> dict[str, object]:
            self.calls.append((method, params))
            if method == "thread/list":
                if params.get("cursor") is None:
                    return {"data": [{"id": "thr_1"}], "nextCursor": "next"}
                return {"data": [{"id": "thr_2"}], "nextCursor": None}
            if method == "thread/read":
                return {"thread": {"id": params["threadId"], "turns": []}}
            if method == "thread/loaded/list":
                return {"data": []}
            raise AssertionError(method)

    client = Paged()
    reconcile(connection, client)
    assert connection.execute("SELECT count(*) FROM threads").fetchone()[0] == 2
    assert [params.get("cursor") for method, params in client.calls if method == "thread/list"] == [None, "next"]


def test_health_states_are_structured(tmp_path: Path) -> None:
    connection = db(tmp_path)
    set_app_server_health(connection, "disconnected", error="e", reconnect=True)
    row = connection.execute("SELECT status,reconnect_total,last_error FROM app_server_state").fetchone()
    assert tuple(row) == ("disconnected", 1, "e")


def test_fresh_and_phase1_upgrade_have_identical_phase2_schema(tmp_path: Path) -> None:
    fresh = connect(tmp_path / "fresh.db")
    migrate(fresh)
    upgraded = connect(tmp_path / "upgrade.db")
    # Simulate a Phase 1 database without rewriting migration 1.
    from codex_observatory.sqlite import MIGRATIONS
    for statement in (part.strip() for part in MIGRATIONS[0][1].split(";")):
        if statement:
            upgraded.execute(statement)
    upgraded.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL, checksum TEXT NOT NULL UNIQUE)")
    upgraded.execute("INSERT INTO schema_migrations(version,applied_at,checksum) VALUES(1,'now',?)", (hashlib.sha256(MIGRATIONS[0][1].encode()).hexdigest(),))
    upgraded.commit()
    migrate(upgraded)
    fresh_schema = {row[0] for row in fresh.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    upgraded_schema = {row[0] for row in upgraded.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert fresh_schema == upgraded_schema
