from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, Header, Request, Response

from .models import RawEnvelope
from .normalization import normalize
from .otlp import OtlpError, decode, error_response, response
from .sqlite import connect, migrate, persist, update_health


def create_app(db_path: Path | str) -> FastAPI:
    db = Path(db_path)
    migration_connection = connect(db)
    try:
        migrate(migration_connection)
    finally:
        migration_connection.close()
    app = FastAPI(title="Codex Observatory")

    @contextmanager
    def request_connection() -> Iterator[sqlite3.Connection]:
        connection = connect(db)
        try:
            yield connection
        finally:
            connection.close()

    @app.post("/v1/{signal}")
    async def ingest(signal: str, request: Request, response_: Response, content_encoding: str | None = Header(default=None)) -> Response:
        if signal not in {"logs", "metrics", "traces"}:
            return Response(status_code=404)
        content_type = request.headers.get("content-type", "")
        body = await request.body()
        decoded = None
        with request_connection() as connection:
            try:
                decoded = decode(signal, body, content_type, content_encoding)
                try:
                    events, unknown = normalize(signal, decoded.message, source_version=None, digest=decoded.envelope.payload_sha256.removeprefix("sha256:"), now=decoded.envelope.received_at)
                except Exception as exc:  # noqa: BLE001 - valid transport must retain normalization diagnostics.
                    failed = replace(decoded.envelope, parse_status="normalization_failed", error_code="normalization_failed", error_message=str(exc))
                    try:
                        persist(connection, failed, [])
                    except (OSError, sqlite3.Error, RuntimeError):
                        pass
                    try:
                        update_health(connection, received=1, normalized=0, rejected=1, unknown=0, persistence_error=0, error=str(exc))
                    except (OSError, sqlite3.Error, RuntimeError):
                        pass
                    return Response(json.dumps({"error": "normalization_failure", "message": str(exc)}), status_code=503, media_type="application/json")
                source_version = next((event.source_version for event in events if event.source_version), None)
                envelope = replace(decoded.envelope, source_version=source_version)
                count = persist(connection, envelope, events)
                update_health(connection, received=1, normalized=count, rejected=0, unknown=unknown, persistence_error=0, error=None)
                payload, media = response(signal, envelope.content_type)
                return Response(payload, status_code=200, media_type=media)
            except OtlpError as exc:
                try:
                    media = content_type.split(";", 1)[0].strip().lower()
                    rejected = RawEnvelope(uuid.uuid4().hex, datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                                           "native_otel", "local-default", signal, None, media or "unknown", content_encoding,
                                           "json" if media == "application/json" else "protobuf", f"sha256:{hashlib.sha256(body).hexdigest()}",
                                           len(body), None, "rejected", exc.code, str(exc), "metadata")
                    persist(connection, rejected, [])
                    update_health(connection, received=1, normalized=0, rejected=1, unknown=0, persistence_error=0, error=str(exc))
                except (OSError, sqlite3.Error, RuntimeError):
                    pass
                media = content_type.split(";", 1)[0] if content_type.split(";", 1)[0] in {"application/json", "application/x-protobuf"} else "application/x-protobuf"
                payload, _ = error_response(str(exc), media)
                return Response(payload, status_code=exc.status_code, media_type=media)
            except Exception as exc:  # noqa: BLE001 - ingestion must convert failures to evidence.
                try:
                    update_health(connection, received=1, normalized=0, rejected=1, unknown=0, persistence_error=1, error=str(exc))
                except (OSError, sqlite3.Error, RuntimeError):
                    pass
                media = content_type.split(";", 1)[0].strip().lower()
                if media not in {"application/json", "application/x-protobuf"}:
                    media = "application/x-protobuf"
                payload, _ = error_response(str(exc), media)
                return Response(payload, status_code=503, media_type=media)

    @app.get("/health")
    def health() -> dict[str, object]:
        with request_connection() as connection:
            row = connection.execute("SELECT * FROM collector_health WHERE collector='otlp'").fetchone()
            result = dict(row) if row else {"collector": "otlp", "status": "healthy", "received_total": 0, "normalized_total": 0, "rejected_total": 0, "unknown_event_total": 0, "persistence_error_total": 0, "last_success": None, "last_error": None}
            app_server = connection.execute("SELECT * FROM app_server_state WHERE source_instance='app-server'").fetchone()
            result["app_server"] = dict(app_server) if app_server else {"status": "disconnected", "reconnect_total": 0}
            hooks = connection.execute("SELECT * FROM hook_source_state WHERE source_instance='local-default'").fetchone()
            result["hooks"] = dict(hooks) if hooks else {"status": "disabled"}
            return result

    @app.get("/events")
    def events(limit: int = 100) -> list[dict[str, object]]:
        limit = max(1, min(limit, 1000))
        with request_connection() as connection:
            return [dict(row) for row in connection.execute("SELECT * FROM events ORDER BY event_seq DESC LIMIT ?", (limit,)).fetchall()]

    @app.get("/raw-events")
    def raw_events(limit: int = 100) -> list[dict[str, object]]:
        limit = max(1, min(limit, 1000))
        with request_connection() as connection:
            return [dict(row) for row in connection.execute("SELECT ingest_id,received_at,source,source_instance,source_event_type,source_version,content_type,content_encoding,payload_encoding,payload_sha256,payload_size,payload_ref,parse_status,error_code,error_message,retention_class,duplicate_of FROM raw_events ORDER BY received_at DESC LIMIT ?", (limit,)).fetchall()]

    return app
