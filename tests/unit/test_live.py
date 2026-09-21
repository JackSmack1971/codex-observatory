from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from codex_observatory.app import create_app
from codex_observatory.live import REPLAY_LIMIT, LiveBroker
from codex_observatory.sqlite import connect, migrate


def insert_event(db: Path, name: str) -> int:
    connection = connect(db)
    connection.execute(
        """INSERT INTO events(event_id,event_time,observed_at,source_class,fact_type,stability,
        source_event,source_instance,raw_event_sha256,adapter_version,category,name,status,attributes_json)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            name,
            "2026-09-21T00:00:00Z",
            "2026-09-21T00:00:00Z",
            "NATIVE",
            "observed",
            "stable",
            "test",
            "test",
            "sha256:x",
            "test",
            "test",
            name,
            "ok",
            json.dumps({"prompt": "secret", "raw_tool_output": "secret"}),
        ),
    )
    sequence = int(
        connection.execute(
            "SELECT event_seq FROM events WHERE event_id=?", (name,)
        ).fetchone()[0]
    )
    connection.commit()
    connection.close()
    return sequence


def test_empty_database_no_cursor_and_heartbeat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("codex_observatory.live.HEARTBEAT_SECONDS", 0.01)
    with (
        TestClient(create_app(tmp_path / "empty.db")) as client,
        client.websocket_connect("/api/v1/live") as socket,
    ):
        assert socket.receive_json() == {
            "type": "heartbeat",
            "schema": "codex.observatory.live.v1",
            "sequence": 0,
        }


def test_replay_is_ordered_bounded_and_privacy_safe(tmp_path: Path) -> None:
    db = tmp_path / "events.db"
    migrate(connect(db))
    sequences = [insert_event(db, f"event-{number}") for number in range(3)]
    with (
        TestClient(create_app(db)) as client,
        client.websocket_connect(
            f"/api/v1/live?last_sequence={sequences[0]}"
        ) as socket,
    ):
        messages = [socket.receive_json(), socket.receive_json()]
    assert [message["sequence"] for message in messages] == sequences[1:]
    assert all(message["type"] == "event" for message in messages)
    assert "secret" not in json.dumps(messages)

    for number in range(3, REPLAY_LIMIT + 2):
        insert_event(db, f"event-{number}")
    with (
        TestClient(create_app(db)) as client,
        client.websocket_connect("/api/v1/live?last_sequence=0") as socket,
    ):
        reset = socket.receive_json()
    assert reset["type"] == "reset_required"
    assert reset["oldest_available_sequence"] == 1
    assert reset["latest_sequence"] == REPLAY_LIMIT + 2


def test_live_fanout_two_clients_disconnect_and_reconnect(tmp_path: Path) -> None:
    db = tmp_path / "fanout.db"
    with TestClient(create_app(db)) as client:
        with (
            client.websocket_connect("/api/v1/live") as first,
            client.websocket_connect("/api/v1/live") as second,
        ):
            sequence_a = insert_event(db, "a")
            assert first.receive_json()["sequence"] == sequence_a
            assert second.receive_json()["sequence"] == sequence_a
        sequence_b = insert_event(db, "b")
        sequence_c = insert_event(db, "c")
        with client.websocket_connect(
            f"/api/v1/live?last_sequence={sequence_a}"
        ) as reconnected:
            assert [
                reconnected.receive_json()["sequence"],
                reconnected.receive_json()["sequence"],
            ] == [sequence_b, sequence_c]
            sequence_d = insert_event(db, "d")
            assert reconnected.receive_json()["sequence"] == sequence_d
            reconnected.send_json({"command": "delete everything"})
    with connect(db) as connection:
        assert connection.execute("SELECT count(*) FROM events").fetchone()[0] == 4


def test_restart_recovers_from_sqlite_and_retention_boundary(tmp_path: Path) -> None:
    db = tmp_path / "restart.db"
    with TestClient(create_app(db)):
        sequence_a = insert_event(db, "a")
    sequence_b = insert_event(db, "b")
    with (
        TestClient(create_app(db)) as restarted,
        restarted.websocket_connect(
            f"/api/v1/live?last_sequence={sequence_a}"
        ) as socket,
    ):
        assert socket.receive_json()["sequence"] == sequence_b
    connection = connect(db)
    connection.execute("DELETE FROM events WHERE event_seq=?", (sequence_a,))
    connection.commit()
    connection.close()
    with (
        TestClient(create_app(db)) as client,
        client.websocket_connect("/api/v1/live?last_sequence=0") as socket,
    ):
        assert socket.receive_json() == {
            "type": "reset_required",
            "schema": "codex.observatory.live.v1",
            "oldest_available_sequence": sequence_b,
            "latest_sequence": sequence_b,
        }


def test_replay_to_live_reconciliation_has_no_duplicate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "race.db"
    migrate(connect(db))
    sequence_a = insert_event(db, "a")
    original = LiveBroker.subscribe
    inserted: list[int] = []

    def subscribe_during_transition(broker: LiveBroker):
        queue = original(broker)
        inserted.append(insert_event(db, "race"))
        return queue

    monkeypatch.setattr(LiveBroker, "subscribe", subscribe_during_transition)
    with (
        TestClient(create_app(db)) as client,
        client.websocket_connect(f"/api/v1/live?last_sequence={sequence_a}") as socket,
    ):
        message = socket.receive_json()
        assert message["sequence"] == inserted[0]
        sequence_after = insert_event(db, "after")
        assert socket.receive_json()["sequence"] == sequence_after


@pytest.mark.parametrize("cursor", ["bad", "-1", "01", "1.0"])
def test_malformed_cursor_is_rejected(tmp_path: Path, cursor: str) -> None:
    with (
        TestClient(create_app(tmp_path / "cursor.db")) as client,
        client.websocket_connect(f"/api/v1/live?last_sequence={cursor}") as socket,
    ):
        assert socket.receive_json()["error"] == "invalid_last_sequence"
