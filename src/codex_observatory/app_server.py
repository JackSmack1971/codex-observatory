"""Read-only Codex App Server adapter.

The adapter deliberately owns only the documented initialization and read methods.
It is not a general JSON-RPC client and has no mutation escape hatch.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import threading
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

from .models import CanonicalEvent, RawEnvelope
from .sqlite import persist

ADAPTER_VERSION = "app-server-v1"
SOURCE_CLASS = "app_server"
READ_ONLY_METHODS = frozenset({"initialize", "thread/list", "thread/read", "thread/loaded/list"})
KNOWN_NOTIFICATIONS = frozenset({
    "thread/started", "thread/status/changed", "thread/archived", "thread/unarchived", "thread/closed",
    "turn/started", "turn/completed", "item/started", "item/completed", "item/agentMessage/delta",
    "item/commandExecution/outputDelta", "thread/tokenUsage/updated", "warning", "configWarning",
    "model/rerouted", "model/safetyBuffering/updated",
})
_SENSITIVE = {"text", "prompt", "arguments", "output", "reasoning", "input"}


class AppServerError(RuntimeError):
    pass


class ProtocolError(AppServerError):
    pass


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _sanitize(value: Any, *, key: str = "") -> Any:
    if isinstance(value, dict):
        return {k: _sanitize(v, key=k.lower()) for k, v in value.items() if k.lower() not in _SENSITIVE}
    if isinstance(value, list):
        return [_sanitize(v, key=key) for v in value]
    if key in _SENSITIVE and value not in (None, ""):
        return {"redacted": True, "sha256": hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()}
    return value


class JsonlTransport:
    """Minimal stdio JSONL transport with explicit request/notification separation."""

    def __init__(self, command: list[str], *, process: subprocess.Popen[str] | None = None) -> None:
        self.command = command
        self.process = process
        self._next_id = 1
        self._lock = threading.Lock()

    def start(self) -> None:
        if self.process is None:
            self.process = subprocess.Popen(self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                            stderr=subprocess.PIPE, text=True, encoding="utf-8", bufsize=1)

    def close(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)

    def send_request(self, method: str, params: dict[str, Any] | None = None) -> int:
        if method not in READ_ONLY_METHODS:
            raise AppServerError(f"App Server method is not allowed: {method}")
        if not self.process or not self.process.stdin:
            raise AppServerError("transport is not started")
        with self._lock:
            request_id = self._next_id
            self._next_id += 1
            payload: dict[str, Any] = {"method": method, "id": request_id}
            if params is not None:
                payload["params"] = params
            self.process.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
            self.process.stdin.flush()
            return request_id

    def send_notification(self, method: str, params: dict[str, Any] | None = None) -> None:
        if method != "initialized":
            raise AppServerError(f"notification is not allowed: {method}")
        if not self.process or not self.process.stdin:
            raise AppServerError("transport is not started")
        payload: dict[str, Any] = {"method": method}
        if params is not None:
            payload["params"] = params
        self.process.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
        self.process.stdin.flush()

    def messages(self) -> Iterator[dict[str, Any]]:
        if not self.process or not self.process.stdout:
            raise AppServerError("transport is not started")
        for line in self.process.stdout:
            try:
                message = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ProtocolError(f"malformed App Server JSONL: {line[:200]!r}") from exc
            if not isinstance(message, dict):
                raise ProtocolError("App Server message must be an object")
            yield message


def _event(method: str, payload: dict[str, Any], received_at: str) -> CanonicalEvent:
    raw_params = payload.get("params")
    params: dict[str, Any] = raw_params if isinstance(raw_params, dict) else payload
    thread_id = params.get("threadId") or (params.get("thread") or {}).get("id")
    turn_id = params.get("turnId")
    raw_item = params.get("item")
    item: dict[str, Any] = raw_item if isinstance(raw_item, dict) else {}
    item_id = params.get("itemId") or item.get("id")
    native_id = ":".join(str(v) for v in (method, thread_id, turn_id, item_id) if v is not None)
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return CanonicalEvent(event_id=hashlib.sha256(native_id.encode() + digest.encode()).hexdigest(),
                          event_time=received_at, event_time_unix_nano=None, observed_at=received_at,
                          source_event=method, source_instance="app-server", source_version=None,
                          raw_event_sha256=f"sha256:{digest}", adapter_version=ADAPTER_VERSION,
                          category="app_server", name=method, attributes=_sanitize(payload),
                          thread_id=thread_id, turn_id=turn_id, item_id=item_id,
                          status=params.get("status", {}).get("type") if isinstance(params.get("status"), dict) else params.get("status"))


def envelope(method: str, payload: dict[str, Any], received_at: str | None = None) -> RawEnvelope:
    received_at = received_at or _now()
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return RawEnvelope(uuid.uuid4().hex, received_at, SOURCE_CLASS, "app-server", method, None,
                       "application/json", None, "json", f"sha256:{hashlib.sha256(body).hexdigest()}",
                       len(body), None, "accepted", retention_class="metadata")


def persist_message(connection: Any, method: str, payload: dict[str, Any], *, kind: str = "notification",
                    request_id: str | None = None, known: bool = True, source_instance: str = "app-server") -> None:
    received = _now()
    connection.execute("INSERT OR IGNORE INTO app_server_state(source_instance,status,updated_at) VALUES(?,?,?)",
                       (source_instance, "healthy", received))
    body = json.dumps(_sanitize(payload), sort_keys=True, separators=(",", ":"))
    message_id = hashlib.sha256(f"{kind}:{method}:{request_id}:{body}".encode()).hexdigest()
    with connection:
        connection.execute("INSERT OR IGNORE INTO app_server_messages(message_id,source_instance,received_at,kind,method,request_id,payload_json,known) VALUES(?,?,?,?,?,?,?,?)",
                           (message_id, source_instance, received, kind, method, request_id, body, int(known)))
        persist(connection, envelope(method, payload, received), [_event(method, payload, received)], source_class=SOURCE_CLASS)
        connection.execute("""UPDATE app_server_state SET messages_received_total=messages_received_total+1,
        notifications_received_total=notifications_received_total+?,unknown_notification_total=unknown_notification_total+?,
        last_message=?,updated_at=? WHERE source_instance=?""",
                           (int(kind == "notification"), int(kind == "notification" and not known), received, received, source_instance))


def set_app_server_health(connection: Any, status: str, *, error: str | None = None,
                          source_instance: str = "app-server", reconnect: bool = False) -> None:
    if status not in {"disabled", "disconnected", "connecting", "healthy", "degraded", "incompatible"}:
        raise ValueError(f"invalid App Server health status: {status}")
    now = _now()
    with connection:
        connection.execute("""INSERT INTO app_server_state(source_instance,status,reconnect_total,last_connected,last_error,updated_at)
        VALUES(?,?,?,?,?,?) ON CONFLICT(source_instance) DO UPDATE SET status=excluded.status,reconnect_total=app_server_state.reconnect_total+excluded.reconnect_total,last_connected=COALESCE(excluded.last_connected,app_server_state.last_connected),last_error=excluded.last_error,updated_at=excluded.updated_at""",
                           (source_instance, status, int(reconnect), now if status == "healthy" else None, error, now))


def _thread_row(thread: dict[str, Any], loaded: bool, source_instance: str, observed: str) -> tuple[Any, ...]:
    status = thread.get("status")
    return (thread.get("id"), thread.get("name"), thread.get("cwd"), thread.get("modelProvider"), thread.get("model"),
            _timestamp(thread.get("createdAt")), _timestamp(thread.get("updatedAt")), int(bool(thread.get("archived", False))),
            status.get("type") if isinstance(status, dict) else status, int(loaded), thread.get("forkedFromId"), source_instance, observed)


def _timestamp(value: Any) -> str | None:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, UTC).isoformat().replace("+00:00", "Z")
    return value if isinstance(value, str) else None


def upsert_thread(connection: Any, thread: dict[str, Any], *, loaded: bool = False, source_instance: str = "app-server") -> None:
    if not thread.get("id"):
        return
    row = _thread_row(thread, loaded, source_instance, _now())
    with connection:
        connection.execute("""INSERT INTO threads(thread_id,name,cwd,model_provider,model,created_at,updated_at,archived,runtime_status,loaded,forked_from_id,source_instance,observed_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(thread_id) DO UPDATE SET name=COALESCE(excluded.name,threads.name),cwd=COALESCE(excluded.cwd,threads.cwd),model_provider=COALESCE(excluded.model_provider,threads.model_provider),model=COALESCE(excluded.model,threads.model),created_at=COALESCE(excluded.created_at,threads.created_at),updated_at=COALESCE(excluded.updated_at,threads.updated_at),archived=COALESCE(excluded.archived,threads.archived),runtime_status=COALESCE(excluded.runtime_status,threads.runtime_status),loaded=excluded.loaded,forked_from_id=COALESCE(excluded.forked_from_id,threads.forked_from_id),observed_at=excluded.observed_at""", row)


def apply_turn(connection: Any, thread_id: str, turn: dict[str, Any], *, source_instance: str = "app-server") -> None:
    turn_id = turn.get("id") or turn.get("turnId")
    if not turn_id:
        return
    status = turn.get("status") if isinstance(turn.get("status"), str) else (turn.get("status") or {}).get("type", "unknown")
    existing = connection.execute("SELECT status FROM turns WHERE turn_id=?", (turn_id,)).fetchone()
    terminal = {"completed", "interrupted", "failed"}
    if existing and existing[0] in terminal and status not in terminal:
        return
    now = _now()
    error = turn.get("error") if isinstance(turn.get("error"), dict) else None
    with connection:
        connection.execute("""INSERT INTO turns(turn_id,thread_id,status,started_at,completed_at,error_json,duration_ms,source_instance,observed_at) VALUES(?,?,?,?,?,?,?,?,?)
        ON CONFLICT(turn_id) DO UPDATE SET status=excluded.status,completed_at=COALESCE(excluded.completed_at,turns.completed_at),error_json=COALESCE(excluded.error_json,turns.error_json),duration_ms=COALESCE(excluded.duration_ms,turns.duration_ms),observed_at=excluded.observed_at""",
                           (turn_id, thread_id, status, _timestamp(turn.get("startedAt")), _timestamp(turn.get("completedAt")) if status in terminal else None, json.dumps(_sanitize(error), sort_keys=True) if error else None, turn.get("durationMs"), source_instance, now))


def apply_item(connection: Any, thread_id: str, turn_id: str, item: dict[str, Any], *, final: bool, source_instance: str = "app-server") -> None:
    item_id = item.get("id")
    if not item_id:
        return
    current = connection.execute("SELECT final FROM thread_items WHERE item_id=?", (item_id,)).fetchone()
    if current and current[0] and not final:
        return
    kind = item.get("type", "unknown")
    content = _sanitize(item)
    with connection:
        connection.execute("""INSERT INTO thread_items(item_id,thread_id,turn_id,item_type,lifecycle_status,final,content_json,source_instance,observed_at) VALUES(?,?,?,?,?,?,?,?,?)
        ON CONFLICT(item_id) DO UPDATE SET lifecycle_status=excluded.lifecycle_status,final=MAX(thread_items.final,excluded.final),content_json=CASE WHEN excluded.final=1 OR thread_items.final=0 THEN excluded.content_json ELSE thread_items.content_json END,observed_at=excluded.observed_at""",
                           (item_id, thread_id, turn_id, kind, "completed" if final else "started", int(final), json.dumps(content, sort_keys=True), source_instance, _now()))


def ingest_notification(connection: Any, method: str, params: dict[str, Any], *, source_instance: str = "app-server") -> None:
    """Decode one notification into the durable message log and projections."""
    known = method in KNOWN_NOTIFICATIONS
    persist_message(connection, method, {"method": method, "params": params}, known=known, source_instance=source_instance)
    thread_id = params.get("threadId")
    if method in {"thread/started", "thread/status/changed"}:
        raw_thread = params.get("thread")
        thread: dict[str, Any] = raw_thread if isinstance(raw_thread, dict) else {"id": thread_id, "status": params.get("status")}
        upsert_thread(connection, thread, loaded=True, source_instance=source_instance)
    elif method == "thread/closed":
        with connection:
            connection.execute("UPDATE threads SET loaded=0,runtime_status='notLoaded',observed_at=? WHERE thread_id=?", (_now(), thread_id))
    elif method in {"turn/started", "turn/completed"}:
        raw_turn = params.get("turn")
        turn: dict[str, Any] = raw_turn if isinstance(raw_turn, dict) else params
        apply_turn(connection, thread_id or "", turn, source_instance=source_instance)
    elif method in {"item/started", "item/completed"}:
        raw_item = params.get("item")
        item: dict[str, Any] = raw_item if isinstance(raw_item, dict) else {}
        apply_item(connection, thread_id or "", params.get("turnId", ""), item, final=method == "item/completed", source_instance=source_instance)
    elif method == "thread/tokenUsage/updated":
        with connection:
            connection.execute("INSERT OR REPLACE INTO app_server_token_usage(thread_id,turn_id,usage_json,observed_at,source_instance) VALUES(?,?,?,?,?)", (thread_id, params.get("turnId", ""), json.dumps(_sanitize(params.get("tokenUsage", {})), sort_keys=True), _now(), source_instance))


def reconcile(connection: Any, client: Any, *, source_instance: str = "app-server") -> None:
    """Run stable discovery only; no resume or experimental history endpoints."""
    with connection:
        connection.execute("INSERT OR IGNORE INTO app_server_state(source_instance,status,updated_at) VALUES(?,?,?)", (source_instance, "connecting", _now()))
    initialization = client.initialize()
    server_info = initialization.get("serverInfo", {}) if isinstance(initialization, dict) else {}
    server_version = (server_info.get("version") if isinstance(server_info, dict) else None) or (initialization.get("userAgent") if isinstance(initialization, dict) else None)
    with connection:
        connection.execute("UPDATE app_server_state SET server_version=?,capabilities_json=?,updated_at=? WHERE source_instance=?",
                           (server_version,
                            json.dumps(initialization.get("capabilities", {}) if isinstance(initialization, dict) else {}, sort_keys=True),
                            _now(), source_instance))
    cursor: str | None = None
    while True:
        result = client.request("thread/list", {"cursor": cursor} if cursor else {})
        for thread in result.get("data", []):
            upsert_thread(connection, thread, source_instance=source_instance)
            full = client.request("thread/read", {"threadId": thread["id"], "includeTurns": True})
            stored = full.get("thread", {})
            upsert_thread(connection, stored, loaded=thread.get("id") in set(), source_instance=source_instance)
            for turn in stored.get("turns", []):
                apply_turn(connection, thread["id"], turn, source_instance=source_instance)
                for item in turn.get("items", []):
                    apply_item(connection, thread["id"], turn.get("id", ""), item, final=True, source_instance=source_instance)
        cursor = result.get("nextCursor")
        if not cursor:
            break
    loaded = client.request("thread/loaded/list", {})
    loaded_ids = set(loaded.get("data", []))
    for thread_id in loaded_ids:
        connection.execute("UPDATE threads SET loaded=1 WHERE thread_id=?", (thread_id,))
    with connection:
        connection.execute("INSERT INTO app_server_state(source_instance,status,reconciliation_runs_total,last_connected,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(source_instance) DO UPDATE SET status='healthy',reconciliation_runs_total=app_server_state.reconciliation_runs_total+1,last_connected=excluded.last_connected,updated_at=excluded.updated_at", (source_instance, "healthy", 1, _now(), _now()))


def reconnect(connection: Any, command: list[str], *, source_instance: str = "app-server") -> None:
    """Restart the child and reconcile again without issuing any control method."""
    set_app_server_health(connection, "connecting", source_instance=source_instance, reconnect=True)
    transport = JsonlTransport(command)
    try:
        reconcile(connection, AppServerClient(transport), source_instance=source_instance)
    except Exception as exc:
        set_app_server_health(connection, "disconnected", error=str(exc), source_instance=source_instance)
        raise
    finally:
        transport.close()


class AppServerClient:
    def __init__(self, transport: JsonlTransport, *, notification_handler: Any | None = None) -> None:
        self.transport = transport
        self.initialized = False
        self.notification_handler = notification_handler

    def _notification(self, message: dict[str, Any]) -> None:
        if self.notification_handler and isinstance(message.get("method"), str):
            params = message.get("params") if isinstance(message.get("params"), dict) else {}
            self.notification_handler(message["method"], params)

    def initialize(self) -> dict[str, Any]:
        self.transport.start()
        request_id = self.transport.send_request("initialize", {"clientInfo": {"name": "codex-observatory", "title": "Codex Observatory", "version": ADAPTER_VERSION}})
        for message in self.transport.messages():
            if "id" not in message:
                self._notification(message)
                continue
            if message.get("id") == request_id:
                if "error" in message:
                    raise AppServerError(json.dumps(message["error"], sort_keys=True))
                self.initialized = True
                self.transport.send_notification("initialized")
                return message.get("result", {})
        raise AppServerError("App Server disconnected during initialize")

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if not self.initialized and method != "initialize":
            raise AppServerError("App Server is not initialized")
        request_id = self.transport.send_request(method, params)
        for message in self.transport.messages():
            if "id" not in message:
                self._notification(message)
                continue
            if message["id"] != request_id:
                raise ProtocolError(f"unexpected App Server response id {message['id']!r}")
            if "error" in message:
                raise AppServerError(json.dumps(message["error"], sort_keys=True))
            result = message.get("result", {})
            return result if isinstance(result, dict) else {}
        raise AppServerError("App Server disconnected while awaiting response")
