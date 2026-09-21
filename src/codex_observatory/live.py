from __future__ import annotations

import asyncio
import sqlite3
from contextlib import suppress
from pathlib import Path
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

from .query import project_event
from .sqlite import connect

SCHEMA = "codex.observatory.live.v1"
REPLAY_LIMIT = 500
HEARTBEAT_SECONDS = 15.0
POLL_SECONDS = 0.05


def _bounds(connection: sqlite3.Connection) -> tuple[int | None, int | None]:
    row = connection.execute(
        "SELECT min(event_seq), max(event_seq) FROM events"
    ).fetchone()
    return row[0], row[1]


def _rows(
    connection: sqlite3.Connection, after: int, through: int | None = None
) -> list[sqlite3.Row]:
    if through is None:
        return connection.execute(
            "SELECT * FROM events WHERE event_seq>? ORDER BY event_seq LIMIT ?",
            (after, REPLAY_LIMIT + 1),
        ).fetchall()
    return connection.execute(
        "SELECT * FROM events WHERE event_seq>? AND event_seq<=? ORDER BY event_seq LIMIT ?",
        (after, through, REPLAY_LIMIT + 1),
    ).fetchall()


def event_message(row: sqlite3.Row) -> dict[str, Any]:
    event = project_event(row)
    return {
        "type": "event",
        "schema": SCHEMA,
        "sequence": event.sequence,
        "event": event.model_dump(mode="json"),
    }


class LiveBroker:
    """One-process notification fanout; SQLite remains the sequence authority."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.subscribers: set[asyncio.Queue[int]] = set()
        self.task: asyncio.Task[None] | None = None
        self.last_seen = 0

    async def start(self) -> None:
        with connect(self.db_path) as connection:
            _, latest = _bounds(connection)
        self.last_seen = latest or 0
        self.task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
            self.task = None

    def subscribe(self) -> asyncio.Queue[int]:
        queue: asyncio.Queue[int] = asyncio.Queue()
        self.subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[int]) -> None:
        self.subscribers.discard(queue)

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(POLL_SECONDS)
            with connect(self.db_path) as connection:
                sequences = [
                    row[0]
                    for row in connection.execute(
                        "SELECT event_seq FROM events WHERE event_seq>? ORDER BY event_seq",
                        (self.last_seen,),
                    )
                ]
            for sequence in sequences:
                self.last_seen = max(self.last_seen, sequence)
                for queue in tuple(self.subscribers):
                    queue.put_nowait(sequence)


async def serve_live(websocket: WebSocket, db_path: Path, broker: LiveBroker) -> None:
    await websocket.accept()
    raw_cursor = websocket.query_params.get("last_sequence")
    if raw_cursor is not None:
        try:
            cursor = int(raw_cursor)
            if cursor < 0 or str(cursor) != raw_cursor:
                raise ValueError
        except ValueError:
            await websocket.send_json(
                {"type": "error", "schema": SCHEMA, "error": "invalid_last_sequence"}
            )
            await websocket.close(code=1008)
            return
    else:
        cursor = -1

    with connect(db_path) as connection:
        oldest, latest = _bounds(connection)
        # An omitted cursor means live-from-now. Explicit cursors request durable replay.
        if cursor == -1:
            cursor = latest or 0
        elif oldest is not None and cursor < oldest - 1:
            await websocket.send_json(
                {
                    "type": "reset_required",
                    "schema": SCHEMA,
                    "oldest_available_sequence": oldest,
                    "latest_sequence": latest,
                }
            )
            await websocket.close(code=1000)
            return
        boundary = latest or cursor
        replay = _rows(connection, cursor, boundary)
    if len(replay) > REPLAY_LIMIT:
        await websocket.send_json(
            {
                "type": "reset_required",
                "schema": SCHEMA,
                "oldest_available_sequence": oldest,
                "latest_sequence": latest,
            }
        )
        await websocket.close(code=1000)
        return
    for row in replay:
        await websocket.send_json(event_message(row))
        cursor = row["event_seq"]

    queue = broker.subscribe()
    try:
        # Register first, then reconcile against SQLite. Queue duplicates are discarded by sequence.
        with connect(db_path) as connection:
            catchup = _rows(connection, cursor)
        if len(catchup) > REPLAY_LIMIT:
            with connect(db_path) as connection:
                catchup_oldest, catchup_latest = _bounds(connection)
            await websocket.send_json(
                {
                    "type": "reset_required",
                    "schema": SCHEMA,
                    "oldest_available_sequence": catchup_oldest,
                    "latest_sequence": catchup_latest,
                }
            )
            return
        for row in catchup:
            if row["event_seq"] > cursor:
                await websocket.send_json(event_message(row))
                cursor = row["event_seq"]
        while True:
            queue_task = asyncio.create_task(queue.get())
            receive_task = asyncio.create_task(websocket.receive())
            done, pending = await asyncio.wait(
                {queue_task, receive_task},
                timeout=HEARTBEAT_SECONDS,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            for task in pending:
                with suppress(asyncio.CancelledError):
                    await task
            if not done:
                await websocket.send_json(
                    {"type": "heartbeat", "schema": SCHEMA, "sequence": cursor}
                )
                continue
            if receive_task in done:
                incoming = receive_task.result()
                if incoming["type"] == "websocket.disconnect":
                    return
                await websocket.close(
                    code=1008, reason="inbound messages are not supported"
                )
                return
            with connect(db_path) as connection:
                rows = _rows(connection, cursor)
            if len(rows) > REPLAY_LIMIT:
                with connect(db_path) as connection:
                    current_oldest, current_latest = _bounds(connection)
                await websocket.send_json(
                    {
                        "type": "reset_required",
                        "schema": SCHEMA,
                        "oldest_available_sequence": current_oldest,
                        "latest_sequence": current_latest,
                    }
                )
                return
            for row in rows:
                if row["event_seq"] > cursor:
                    await websocket.send_json(event_message(row))
                    cursor = row["event_seq"]
    except WebSocketDisconnect, RuntimeError:
        pass
    finally:
        broker.unsubscribe(queue)
