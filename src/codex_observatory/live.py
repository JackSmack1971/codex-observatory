from __future__ import annotations

import asyncio
import json
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
    row = connection.execute("SELECT min(event_seq), max(event_seq) FROM events").fetchone()
    return row[0], row[1]


def _filters_sql(filters: dict[str, Any]) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    session_id = filters.get("session_id")
    if session_id is not None:
        clauses.append("session_id=?")
        params.append(session_id)
    categories = filters.get("categories")
    if categories:
        clauses.append("(" + " OR ".join("(category=? OR category LIKE ?)" for _ in categories) + ")")
        for category in categories:
            params.extend((category, category + "_%"))
    return (" AND " + " AND ".join(clauses)) if clauses else "", params


def _rows(
    connection: sqlite3.Connection,
    after: int,
    through: int | None = None,
    filters: dict[str, Any] | None = None,
) -> list[sqlite3.Row]:
    where = "event_seq>?"
    params: list[Any] = [after]
    if through is not None:
        where += " AND event_seq<=?"
        params.append(through)
    filter_sql, filter_params = _filters_sql(filters or {})
    params.extend(filter_params)
    params.append(REPLAY_LIMIT + 1)
    return connection.execute(
        f"SELECT * FROM events WHERE {where}{filter_sql} ORDER BY event_seq LIMIT ?",
        params,
    ).fetchall()


def _interval_is_complete(connection: sqlite3.Connection, after: int, through: int) -> bool:
    """Prove that AUTOINCREMENT sequences in (after, through] are retained."""
    if through <= after:
        return True
    count = connection.execute(
        "SELECT count(*) FROM events WHERE event_seq>? AND event_seq<=?",
        (after, through),
    ).fetchone()[0]
    return count == through - after


def event_message(row: sqlite3.Row) -> dict[str, Any]:
    event = project_event(row)
    return {
        "type": "event",
        "schema": SCHEMA,
        "sequence": event.sequence,
        "payload": event.model_dump(mode="json"),
    }


def _error(code: str, message: str) -> dict[str, str]:
    return {"type": "error", "schema": SCHEMA, "error": code, "message": message}


def _validate_filters(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict) or set(value) - {"session_id", "categories"}:
        raise ValueError("filters must contain only session_id and categories")
    session_id = value.get("session_id")
    if session_id is not None and not isinstance(session_id, str):
        raise ValueError("session_id must be a string or null")
    categories = value.get("categories")
    if categories is not None and (
        not isinstance(categories, list)
        or not all(isinstance(category, str) and category for category in categories)
        or len(set(categories)) != len(categories)
    ):
        raise ValueError("categories must be a list of unique non-empty strings or null")
    return {"session_id": session_id, "categories": categories}


def _resume(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("resume_from_sequence must be a non-negative integer or null")
    return value


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


async def _send_reset(websocket: WebSocket, oldest: int | None, latest: int | None) -> None:
    await websocket.send_json(
        {
            "type": "reset_required",
            "schema": SCHEMA,
            "oldest_available_sequence": oldest,
            "latest_sequence": latest,
        }
    )


async def serve_live(websocket: WebSocket, db_path: Path, broker: LiveBroker) -> None:
    await websocket.accept()
    legacy = False
    cursor: int | None = None
    query_cursor = websocket.query_params.get("last_sequence")
    if query_cursor is not None:
        # Compatibility for the pre-v1 client. New clients must use subscribe.
        try:
            cursor = int(query_cursor)
            if cursor < 0 or str(cursor) != query_cursor:
                raise ValueError
        except ValueError:
            await websocket.send_json(_error("invalid_last_sequence", "last_sequence must be a non-negative integer"))
            await websocket.close(code=1008)
            return
        filters: dict[str, Any] = {}
        legacy = True
    else:
        try:
            incoming = await websocket.receive_json()
        except (WebSocketDisconnect, ValueError, json.JSONDecodeError):
            await websocket.send_json(_error("invalid_subscribe", "expected a JSON subscribe message"))
            await websocket.close(code=1008)
            return
        if not isinstance(incoming, dict) or incoming.get("type") != "subscribe":
            await websocket.send_json(_error("unsupported_message_type", "the first message must be subscribe"))
            await websocket.close(code=1008)
            return
        try:
            cursor = _resume(incoming.get("resume_from_sequence"))
            filters = _validate_filters(incoming.get("filters"))
        except ValueError as exc:
            await websocket.send_json(_error("invalid_subscribe", str(exc)))
            await websocket.close(code=1008)
            return

    queue = broker.subscribe()
    try:
        with connect(db_path) as connection:
            oldest, latest = _bounds(connection)
            if cursor is None:
                cursor = latest or 0
                replay: list[sqlite3.Row] = []
            else:
                if oldest is not None and cursor < oldest - 1:
                    await _send_reset(websocket, oldest, latest)
                    return
                boundary = latest or cursor
                if not _interval_is_complete(connection, cursor, boundary):
                    await _send_reset(websocket, oldest, latest)
                    return
                replay = _rows(connection, cursor, boundary, filters)
        if len(replay) > REPLAY_LIMIT:
            await _send_reset(websocket, oldest, latest)
            return
        for row in replay:
            await websocket.send_json(event_message(row))
        # The durable boundary, not the last filtered row, is the resume
        # cursor. Filtered-out sequences are intentionally acknowledged.
        if latest is not None:
            cursor = latest

        # Registration happened before the durable read. Reconcile everything
        # committed during replay, then acknowledge this durable baseline.
        with connect(db_path) as connection:
            catchup_oldest, catchup_latest = _bounds(connection)
            current_latest = catchup_latest or cursor
            if not _interval_is_complete(connection, cursor, current_latest):
                await _send_reset(websocket, catchup_oldest, catchup_latest)
                return
            catchup = _rows(connection, cursor, current_latest, filters)
        if len(catchup) > REPLAY_LIMIT:
            await _send_reset(websocket, catchup_oldest, catchup_latest)
            return
        for row in catchup:
            await websocket.send_json(event_message(row))
        cursor = current_latest
        if not legacy:
            await websocket.send_json({"type": "subscribed", "schema": SCHEMA, "sequence": cursor})

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
                await websocket.send_json({"type": "heartbeat", "schema": SCHEMA, "sequence": cursor})
                continue
            if receive_task in done:
                incoming = receive_task.result()
                if incoming["type"] == "websocket.disconnect":
                    return
                await websocket.close(code=1008, reason="only one subscribe message is supported")
                return
            with connect(db_path) as connection:
                live_oldest, live_latest = _bounds(connection)
                boundary = live_latest or cursor
                if not _interval_is_complete(connection, cursor, boundary):
                    await _send_reset(websocket, live_oldest, live_latest)
                    return
                rows = _rows(connection, cursor, boundary, filters)
            if len(rows) > REPLAY_LIMIT:
                await _send_reset(websocket, live_oldest, live_latest)
                return
            for row in rows:
                await websocket.send_json(event_message(row))
            cursor = boundary
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        broker.unsubscribe(queue)
