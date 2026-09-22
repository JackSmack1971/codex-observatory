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

from fastapi import FastAPI, Header, HTTPException, Query, Request, Response, WebSocket
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .admin_queries import completions_summary, costs_summary, summary
from .api_models import (
    AdminCompletionsSummary,
    AdminCompletionUsage,
    AdminCost,
    AdminCostsSummary,
    AdminSummary,
    AdminUsageHealth,
    Agent,
    Approval,
    ArchiveHealth,
    Event,
    GitSnapshot,
    Health,
    Overview,
    Page,
    Repository,
    Session,
    Skill,
    Thread,
    Tool,
    Turn,
)
from .config import admin_key_present, load_config
from .live import LiveBroker, serve_live
from .models import RawEnvelope
from .normalization import normalize
from .openai_admin import health as admin_health
from .openai_admin import read_usage
from .openai_costs import health as admin_cost_health
from .openai_costs import read_costs
from .otlp import OtlpError, decode, error_response, response
from .query import QueryService
from .sqlite import connect, migrate, persist, update_health


def create_app(db_path: Path | str, *, archive_root: Path | None = None, frontend_dist: Path | None = None) -> FastAPI:
    db = Path(db_path)
    migration_connection = connect(db)
    try:
        migrate(migration_connection)
    finally:
        migration_connection.close()
    app = FastAPI(title="Codex Observatory")
    query = QueryService(db, archive_root)
    broker = LiveBroker(db)

    @app.on_event("startup")
    async def start_live_broker() -> None:
        await broker.start()

    @app.on_event("shutdown")
    async def stop_live_broker() -> None:
        await broker.stop()

    @app.websocket("/api/v1/live")
    async def api_live(websocket: WebSocket) -> None:
        await serve_live(websocket, db, broker)

    @app.get("/api/v1/overview", response_model=Overview)
    def api_overview(start: str | None = None, end: str | None = None) -> Overview:
        return query.overview(start, end)

    @app.get("/api/v1/sessions", response_model=Page[Session])
    def api_sessions(limit: int = Query(50, ge=1, le=100), cursor: str | None = None, start: str | None = None, end: str | None = None) -> Page[Session]:
        return query.sessions(limit, cursor, start, end)

    @app.get("/api/v1/sessions/{session_id}", response_model=Session)
    def api_session(session_id: str) -> Session:
        result = query.session(session_id)
        if not result:
            raise HTTPException(404, "session not found")
        return result

    @app.get("/api/v1/threads", response_model=Page[Thread])
    def api_threads(limit: int = Query(50, ge=1, le=100), cursor: str | None = None, repo_id: str | None = None) -> Page[Thread]:
        return query.threads(limit, cursor, repo_id)

    @app.get("/api/v1/threads/{thread_id}", response_model=Thread)
    def api_thread(thread_id: str) -> Thread:
        result = query.session(thread_id)
        if not result:
            raise HTTPException(404, "thread not found")
        return Thread.model_validate(result.model_dump())

    @app.get("/api/v1/turns/{turn_id}", response_model=Turn)
    def api_turn(turn_id: str) -> Turn:
        result = query.turn(turn_id)
        if not result:
            raise HTTPException(404, "turn not found")
        return result

    @app.get("/api/v1/agents", response_model=Page[Agent])
    def api_agents(limit: int = Query(50, ge=1, le=100), cursor: str | None = None) -> Page[Agent]: return query.agents(limit, cursor)

    @app.get("/api/v1/tools", response_model=Page[Tool])
    def api_tools(limit: int = Query(50, ge=1, le=100), cursor: str | None = None) -> Page[Tool]: return query.tools(limit, cursor)

    @app.get("/api/v1/approvals", response_model=Page[Approval])
    def api_approvals(limit: int = Query(50, ge=1, le=100), cursor: str | None = None) -> Page[Approval]: return query.approvals(limit, cursor)

    @app.get("/api/v1/skills", response_model=Page[Skill])
    def api_skills(limit: int = Query(50, ge=1, le=100), cursor: str | None = None) -> Page[Skill]: return query.skills(limit, cursor)

    @app.get("/api/v1/git/repositories", response_model=Page[Repository])
    def api_repositories(limit: int = Query(50, ge=1, le=100), cursor: str | None = None) -> Page[Repository]: return query.repositories(limit, cursor)

    @app.get("/api/v1/git/snapshots", response_model=Page[GitSnapshot])
    def api_snapshots(limit: int = Query(50, ge=1, le=100), cursor: str | None = None, repo_id: str | None = None) -> Page[GitSnapshot]: return query.snapshots(limit, cursor, repo_id)

    @app.get("/api/v1/archive/health", response_model=ArchiveHealth)
    def api_archive_health() -> ArchiveHealth: return query.archive_health()

    @app.get("/api/v1/admin/usage/completions", response_model=Page[AdminCompletionUsage])
    def api_admin_usage(limit: int = Query(50, ge=1, le=100), cursor: str | None = None,
                        start_time: int | None = Query(None, ge=0), end_time: int | None = Query(None, ge=0)) -> Page[AdminCompletionUsage]:
        if cursor is not None and (not cursor.isdigit() or int(cursor) < 0):
            raise HTTPException(400, "invalid cursor")
        if start_time is not None and end_time is not None and start_time >= end_time:
            raise HTTPException(422, "start_time must be before end_time")
        with request_connection() as connection:
            page = read_usage(connection, limit=limit, offset=int(cursor or 0), start=start_time, end=end_time)
        return Page[AdminCompletionUsage](items=[AdminCompletionUsage.model_validate(item) for item in page["items"]], next_cursor=page["next_cursor"], has_more=page["has_more"])

    @app.get("/api/v1/admin/usage/health", response_model=AdminUsageHealth)
    def api_admin_health() -> AdminUsageHealth:
        config = load_config()
        with request_connection() as connection:
            return AdminUsageHealth.model_validate(admin_health(connection, enabled=config.collectors.openai_admin.enabled, credential_present=admin_key_present()))

    @app.get("/api/v1/admin/costs", response_model=Page[AdminCost])
    def api_admin_costs(limit: int = Query(50, ge=1, le=100), cursor: str | None = None,
                        start_time: int | None = Query(None, ge=0), end_time: int | None = Query(None, ge=0)) -> Page[AdminCost]:
        if cursor is not None and (not cursor.isdigit() or int(cursor) < 0): raise HTTPException(400, "invalid cursor")
        if start_time is not None and end_time is not None and start_time >= end_time: raise HTTPException(422, "start_time must be before end_time")
        with request_connection() as connection:
            page = read_costs(connection, limit=limit, offset=int(cursor or 0), start=start_time, end=end_time)
        return Page[AdminCost](items=[AdminCost.model_validate(item) for item in page["items"]], next_cursor=page["next_cursor"], has_more=page["has_more"])

    @app.get("/api/v1/admin/costs/health", response_model=AdminUsageHealth)
    def api_admin_cost_health() -> AdminUsageHealth:
        config = load_config()
        with request_connection() as connection:
            return AdminUsageHealth.model_validate(admin_cost_health(connection, enabled=config.collectors.openai_admin.costs_enabled, credential_present=admin_key_present()))

    @app.get("/api/v1/admin/usage/completions/summary", response_model=AdminCompletionsSummary)
    def api_admin_usage_summary(range: str = Query("24h", pattern="^(24h|7d|30d)$")) -> AdminCompletionsSummary:
        config = load_config()
        with request_connection() as connection:
            return AdminCompletionsSummary.model_validate(completions_summary(connection, range_key=range, enabled=config.collectors.openai_admin.enabled, credential_present=admin_key_present()))

    @app.get("/api/v1/admin/costs/summary", response_model=AdminCostsSummary)
    def api_admin_costs_summary(range: str = Query("24h", pattern="^(24h|7d|30d)$")) -> AdminCostsSummary:
        config = load_config()
        with request_connection() as connection:
            return AdminCostsSummary.model_validate(costs_summary(connection, range_key=range, enabled=config.collectors.openai_admin.costs_enabled, credential_present=admin_key_present()))

    @app.get("/api/v1/admin/summary", response_model=AdminSummary)
    def api_admin_summary(range: str = Query("24h", pattern="^(24h|7d|30d)$")) -> AdminSummary:
        config = load_config()
        with request_connection() as connection:
            return AdminSummary.model_validate(summary(connection, range_key=range, enabled=config.collectors.openai_admin.enabled, costs_enabled=config.collectors.openai_admin.costs_enabled, credential_present=admin_key_present()))

    @app.get("/api/v1/health", response_model=Health)
    def api_health() -> Health: return query.health()

    @app.get("/api/v1/events", response_model=Page[Event])
    def api_events(limit: int = Query(100, ge=1, le=200), cursor: str | None = None, start: str | None = None, end: str | None = None, repo_id: str | None = None, source_class: str | None = None, thread_id: str | None = None) -> Page[Event]:
        if source_class and source_class not in {"NATIVE", "ENRICHED", "DERIVED", "UNKNOWN", "native_otel", "app_server", "hook"}:
            raise HTTPException(422, "invalid source_class")
        return query.events(limit, cursor, start, end, repo_id, source_class, thread_id)

    if frontend_dist and (frontend_dist / "index.html").is_file():
        app.mount("/assets", StaticFiles(directory=frontend_dist / "assets"), name="frontend-assets")

        @app.get("/{path:path}", include_in_schema=False)
        def frontend_route(path: str) -> FileResponse:
            if path.startswith("api/"):
                raise HTTPException(404, "not found")
            requested = (frontend_dist / path).resolve()
            root = frontend_dist.resolve()
            if root not in requested.parents and requested != root:
                raise HTTPException(404, "not found")
            return FileResponse(requested if requested.is_file() else frontend_dist / "index.html")

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
