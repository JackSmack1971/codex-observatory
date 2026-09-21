#!/usr/bin/env python3
"""Disposable loopback gate for durable WebSocket replay and recovery."""

from __future__ import annotations

import asyncio
import json
import socket
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import uvicorn
import websockets

from codex_observatory.app import create_app
from codex_observatory.sqlite import connect


def seed(db: Path, name: str) -> int:
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
            "gate",
            "gate",
            "sha256:gate",
            "gate",
            "gate",
            name,
            "ok",
            "{}",
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


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def start_server(db: Path, port: int) -> tuple[uvicorn.Server, threading.Thread]:
    server = uvicorn.Server(
        uvicorn.Config(create_app(db), host="127.0.0.1", port=port, log_level="error")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.01)
    if not server.started:
        raise RuntimeError("loopback server did not start")
    return server, thread


def stop_server(server: uvicorn.Server, thread: threading.Thread) -> None:
    server.should_exit = True
    thread.join(timeout=10)
    if thread.is_alive():
        raise RuntimeError("loopback server did not stop")


async def receive_event(websocket: Any) -> dict[str, Any]:
    message = json.loads(await asyncio.wait_for(websocket.recv(), timeout=5))
    if message["type"] != "event":
        raise AssertionError(message)
    return message


async def run() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="codex-observatory-live-") as directory:
        db = Path(directory) / "gate.db"
        port = free_port()
        server, thread = start_server(db, port)
        uri = f"ws://127.0.0.1:{port}/api/v1/live"
        received: list[dict[str, Any]] = []
        async with websockets.connect(uri) as websocket:
            sequence_a = seed(db, "A")
            received.append(await receive_event(websocket))
        sequence_b, sequence_c = seed(db, "B"), seed(db, "C")
        async with websockets.connect(f"{uri}?last_sequence={sequence_a}") as websocket:
            received.extend(
                [await receive_event(websocket), await receive_event(websocket)]
            )
            sequence_d = seed(db, "D")
            received.append(await receive_event(websocket))
        stop_server(server, thread)

        server, thread = start_server(db, port)
        async with websockets.connect(f"{uri}?last_sequence={sequence_d}") as websocket:
            sequence_e = seed(db, "E")
            received.append(await receive_event(websocket))
        connection = connect(db)
        connection.execute("DELETE FROM events WHERE event_seq<?", (sequence_e,))
        connection.commit()
        connection.close()
        async with websockets.connect(f"{uri}?last_sequence=0") as websocket:
            reset = json.loads(await asyncio.wait_for(websocket.recv(), timeout=5))
        async with httpx.AsyncClient() as client:
            rest = (
                await client.get(f"http://127.0.0.1:{port}/api/v1/events", timeout=5)
            ).json()
        stop_server(server, thread)

        expected = [sequence_a, sequence_b, sequence_c, sequence_d, sequence_e]
        actual = [message["sequence"] for message in received]
        assert actual == expected
        assert reset == {
            "type": "reset_required",
            "schema": "codex.observatory.live.v1",
            "oldest_available_sequence": sequence_e,
            "latest_sequence": sequence_e,
        }
        assert rest["items"][0]["sequence"] == sequence_e
        return {
            "sequences": {
                name: value for name, value in zip("ABCDE", expected, strict=True)
            },
            "message_types": [message["type"] for message in received],
            "reset": reset,
            "rest_recovery_sequence": rest["items"][0]["sequence"],
        }


if __name__ == "__main__":
    print(json.dumps(asyncio.run(run()), sort_keys=True))
