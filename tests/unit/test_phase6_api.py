from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from codex_observatory.app import create_app
from codex_observatory.app_server import apply_turn, upsert_thread
from codex_observatory.sqlite import connect, migrate


def client(tmp_path: Path) -> TestClient:
    db = tmp_path / "observatory.db"
    connection = connect(db)
    migrate(connection)
    connection.execute(
        "INSERT INTO app_server_state(source_instance,status,updated_at) VALUES('app-server','healthy','2026-09-20T00:00:00Z')"
    )
    upsert_thread(
        connection,
        {
            "id": "thread-1",
            "name": "safe",
            "model": "test",
            "createdAt": "2026-09-20T00:00:00Z",
            "updatedAt": "2026-09-20T00:01:00Z",
        },
    )
    apply_turn(connection, "thread-1", {"id": "turn-1", "status": "completed"})
    connection.execute(
        "INSERT INTO events(event_id,event_time,observed_at,source_class,fact_type,stability,source_event,source_instance,raw_event_sha256,adapter_version,thread_id,category,name,status,attributes_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "event-1",
            "2026-09-20T00:00:01Z",
            "2026-09-20T00:00:01Z",
            "NATIVE",
            "observed",
            "stable",
            "test",
            "test",
            "sha256:x",
            "test",
            "thread-1",
            "tool_observation",
            "tool_run",
            "ok",
            json.dumps({"prompt": "must not leave"}),
        ),
    )
    connection.commit()
    connection.close()
    return TestClient(create_app(db))


def test_empty_typed_overview_and_health(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path / "empty.db")) as test:
        overview = test.get("/api/v1/overview").json()
        assert overview["sessions"] == {
            "value": 0,
            "coverage": "available",
            "evidence": {
                "source_class": "DERIVED",
                "fact_type": "aggregate",
                "stability": None,
                "source_event": None,
                "correlation_method": None,
                "correlation_confidence": None,
            },
        }
        health = test.get("/api/v1/health")
        assert health.status_code == 200
        assert any(
            component["status"] == "blocked_external_runtime"
            for component in health.json()["components"]
        )


def test_read_only_typed_routes_pagination_and_privacy(tmp_path: Path) -> None:
    with client(tmp_path) as test:
        sessions = test.get("/api/v1/sessions?limit=1")
        assert (
            sessions.status_code == 200
            and sessions.json()["items"][0]["thread_id"] == "thread-1"
        )
        assert test.get("/api/v1/threads/thread-1").json()["thread_id"] == "thread-1"
        assert test.get("/api/v1/turns/turn-1").status_code == 200
        event = test.get("/api/v1/events").json()["items"][0]
        assert "attributes_json" not in event and "prompt" not in json.dumps(event)
        assert event["evidence"]["source_class"] == "NATIVE"
        assert test.get("/api/v1/events?cursor=not-a-cursor").status_code == 400
        assert test.get("/api/v1/sessions/missing").status_code == 404
        assert test.post("/api/v1/overview").status_code == 405


def test_time_and_source_filters_and_stable_order(tmp_path: Path) -> None:
    with client(tmp_path) as test:
        assert (
            len(
                test.get(
                    "/api/v1/events?source_class=NATIVE&start=2026-09-20T00:00:00Z&end=2026-09-21T00:00:00Z"
                ).json()["items"]
            )
            == 1
        )
        assert test.get("/api/v1/events?source_class=missing").status_code == 422
        assert test.get("/api/v1/events?start=not-a-time").status_code == 422
        assert (
            test.get("/api/v1/events?thread_id=thread-1").json()["items"][0][
                "thread_id"
            ]
            == "thread-1"
        )


def test_event_pagination_and_overlapping_memberships(tmp_path: Path) -> None:
    test = client(tmp_path)
    connection = connect(tmp_path / "observatory.db")
    for number, category, name in (
        (2, "tool_approval_skill", "tool_approval_skill"),
        (3, "tool_observation", "tool_three"),
    ):
        connection.execute(
            "INSERT INTO events(event_id,event_time,observed_at,source_class,fact_type,stability,source_event,source_instance,raw_event_sha256,adapter_version,thread_id,category,name,status,attributes_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                f"event-{number}",
                f"2026-09-20T00:00:0{number}Z",
                f"2026-09-20T00:00:0{number}Z",
                "NATIVE",
                "observed",
                "stable",
                "test",
                "test",
                f"sha256:{number}",
                "test",
                "thread-1",
                category,
                name,
                "ok",
                "{}",
            ),
        )
    connection.execute(
        "INSERT OR IGNORE INTO event_memberships(event_id, kind) SELECT event_id, 'tool' FROM events WHERE category LIKE '%tool%' OR name LIKE '%tool%'"
    )
    connection.execute(
        "INSERT OR IGNORE INTO event_memberships(event_id, kind) SELECT event_id, 'approval' FROM events WHERE category LIKE '%approval%' OR name LIKE '%approval%'"
    )
    connection.execute(
        "INSERT OR IGNORE INTO event_memberships(event_id, kind) SELECT event_id, 'skill' FROM events WHERE category LIKE '%skill%' OR name LIKE '%skill%'"
    )
    connection.commit()
    connection.close()

    first = test.get("/api/v1/events?limit=1").json()
    assert len(first["items"]) == 1 and first["has_more"]
    second = test.get(f"/api/v1/events?limit=1&cursor={first['next_cursor']}").json()
    assert (
        len(second["items"]) == 1
        and second["items"][0]["event_id"] != first["items"][0]["event_id"]
    )
    assert test.get("/api/v1/events?cursor=-1").status_code == 400

    tools = test.get("/api/v1/tools").json()["items"]
    approvals = test.get("/api/v1/approvals").json()["items"]
    skills = test.get("/api/v1/skills").json()["items"]
    assert {item["tool"] for item in tools} == {
        "tool_approval_skill",
        "tool_three",
        "tool_run",
    }
    assert [item["approval_id"] for item in approvals] == ["event-2"]
    assert [item["skill"] for item in skills] == ["tool_approval_skill"]

    connection = connect(tmp_path / "observatory.db")
    assert [
        row[0]
        for row in connection.execute(
            "SELECT kind FROM event_memberships WHERE event_id='event-2' ORDER BY kind"
        )
    ] == ["approval", "skill", "tool"]
    connection.close()
