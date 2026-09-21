from __future__ import annotations

import base64
import binascii
import json
import sqlite3
from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, TypeVar

from fastapi import HTTPException

from .api_models import (
    Agent,
    Approval,
    ArchiveHealth,
    ComponentHealth,
    Event,
    Evidence,
    GitSnapshot,
    Health,
    Metric,
    Overview,
    Page,
    Repository,
    Session,
    Skill,
    Thread,
    Tool,
    Turn,
)
from .sqlite import connect

T = TypeVar("T")


def _cursor(value: str | None) -> int:
    if not value:
        return 0
    try:
        decoded = json.loads(base64.urlsafe_b64decode(value.encode()).decode())
        if not isinstance(decoded, int) or decoded < 0:
            raise ValueError
        return decoded
    except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError, binascii.Error) as exc:
        raise HTTPException(400, "invalid cursor") from exc


def _page[T](items: list[T], cursor: str | None, limit: int) -> Page[T]:
    offset = _cursor(cursor)
    next_offset = offset + limit
    return Page(items=items[offset:next_offset], next_cursor=base64.urlsafe_b64encode(json.dumps(next_offset).encode()).decode() if next_offset < len(items) else None, has_more=next_offset < len(items))


def _evidence(row: Any, source: str | None = None, fact: str = "observed") -> Evidence:
    def get(key: str) -> Any:
        try: return row[key]
        except (KeyError, IndexError, TypeError): return None
    return Evidence(source_class=source or get("source_class") or "UNKNOWN", fact_type=fact, stability=get("stability"), source_event=get("source_event"), correlation_method=get("correlation_method"), correlation_confidence=get("correlation_confidence"))


def _int(value: Any) -> int: return int(value or 0)


class QueryService:
    def __init__(self, db_path: Path | str, archive_root: Path | None = None) -> None:
        self.db_path = Path(db_path)

    @contextmanager
    def _db(self):
        connection = connect(self.db_path)
        try:
            yield connection
        finally:
            connection.close()

    @staticmethod
    def _time(start: str | None, end: str | None, column: str) -> tuple[str, list[str]]:
        clauses: list[str] = []
        params: list[str] = []
        for op, value in ((">=", start), ("<", end)):
            if value:
                try: datetime.fromisoformat(value)
                except ValueError as exc: raise HTTPException(422, f"invalid timestamp: {value}") from exc
                clauses.append(f" AND {column} {op} ?"); params.append(value)
        return "".join(clauses), params

    def _count(self, table: str, where: str = "", params: tuple[Any, ...] = ()) -> int:
        with self._db() as db: return int(db.execute(f"SELECT count(*) FROM {table}{where}", params).fetchone()[0])

    def _session(self, db: sqlite3.Connection, row: sqlite3.Row) -> Session:
        tid = row["thread_id"]; tokens = 0
        for item in db.execute("SELECT usage_json FROM app_server_token_usage WHERE thread_id=?", (tid,)).fetchall():
            try:
                body = json.loads(item[0]); total = body.get("total", body); tokens += _int(total.get("totalTokens", total.get("total_tokens")))
            except (TypeError, json.JSONDecodeError): pass
        tools = db.execute("SELECT count(*) FROM events WHERE thread_id=? AND (category LIKE '%tool%' OR name LIKE '%tool%')", (tid,)).fetchone()[0]
        return Session(session_id=None, thread_id=tid, name=row["name"], cwd=row["cwd"], model=row["model"], started_at=row["created_at"], updated_at=row["updated_at"], status=row["runtime_status"], archived=bool(row["archived"]) if row["archived"] is not None else None, turns=_int(db.execute("SELECT count(*) FROM turns WHERE thread_id=?", (tid,)).fetchone()[0]), tokens=tokens, tool_calls=tools, evidence=_evidence({"source_class": "APP_SERVER", "source_event": "thread"}))

    def sessions(self, limit: int, cursor: str | None, start: str | None, end: str | None) -> Page[Session]:
        time, params = self._time(start, end, "COALESCE(updated_at, observed_at)")
        with self._db() as db:
            rows = db.execute(f"SELECT * FROM threads WHERE 1=1{time} ORDER BY COALESCE(updated_at, observed_at) DESC, thread_id DESC", params).fetchall(); items = [self._session(db, r) for r in rows]
        return _page(items, cursor, limit)

    def session(self, thread_id: str) -> Session | None:
        with self._db() as db:
            row = db.execute("SELECT * FROM threads WHERE thread_id=?", (thread_id,)).fetchone(); return self._session(db, row) if row else None

    def threads(self, limit: int, cursor: str | None, repo_id: str | None) -> Page[Thread]:
        page = self.sessions(limit, cursor, None, None); return Page[Thread](items=[Thread.model_validate(x.model_dump()) for x in page.items], next_cursor=page.next_cursor, has_more=page.has_more)

    def turn(self, turn_id: str) -> Turn | None:
        with self._db() as db:
            r = db.execute("SELECT * FROM turns WHERE turn_id=?", (turn_id,)).fetchone()
        return Turn(turn_id=r["turn_id"], thread_id=r["thread_id"], status=r["status"], started_at=r["started_at"], completed_at=r["completed_at"], duration_ms=r["duration_ms"], evidence=_evidence({"source_class": "APP_SERVER", "source_event": "turn"})) if r else None

    def events(self, limit: int, cursor: str | None, start: str | None, end: str | None, repo_id: str | None, source_class: str | None, thread_id: str | None = None) -> Page[Event]:
        time, params = self._time(start, end, "event_time"); extra = time + (" AND source_class=?" if source_class else "") + (" AND thread_id=?" if thread_id else "") + (" AND json_extract(attributes_json, '$.repo_id')=?" if repo_id else ""); params += [source_class] if source_class else []; params += [thread_id] if thread_id else []; params += [repo_id] if repo_id else []
        with self._db() as db: rows = db.execute(f"SELECT * FROM events WHERE 1=1{extra} ORDER BY event_time DESC, event_seq DESC", params).fetchall()
        return _page([Event(event_id=r["event_id"], sequence=r["event_seq"], event_time=r["event_time"], category=r["category"], name=r["name"], status=r["status"], session_id=r["session_id"], thread_id=r["thread_id"], turn_id=r["turn_id"], evidence=_evidence(r)) for r in rows], cursor, limit)

    def agents(self, limit: int, cursor: str | None) -> Page[Agent]:
        with self._db() as db: rows = db.execute("SELECT * FROM events WHERE category='agent_lifecycle' ORDER BY event_time DESC, event_seq DESC").fetchall()
        seen: dict[str, Agent] = {}
        for r in rows:
            try: attrs = json.loads(r["attributes_json"])
            except json.JSONDecodeError: attrs = {}
            aid = r["operation_id"] or attrs.get("agent_id")
            if aid and aid not in seen: seen[aid] = Agent(agent_id=aid, parent_agent_id=attrs.get("parent_agent_id"), agent_type=attrs.get("agent_type"), state="stopped" if r["name"] == "agent_stop" else "running", thread_id=r["thread_id"], started_at=r["event_time"], stopped_at=r["event_time"] if r["name"] == "agent_stop" else None, tool_count=0, evidence=_evidence(r))
        return _page(list(seen.values()), cursor, limit)

    def tools(self, limit: int, cursor: str | None) -> Page[Tool]:
        with self._db() as db: rows = db.execute("SELECT category,name,status,source_class FROM events WHERE category LIKE '%tool%' OR name LIKE '%tool%' ORDER BY event_time DESC, event_seq DESC").fetchall()
        grouped: dict[str, Tool] = {}
        for r in rows:
            name = r["name"] or r["category"]; old = grouped.get(name); breakdown = dict(old.source_breakdown) if old else {}; breakdown[r["source_class"]] = breakdown.get(r["source_class"], 0) + 1
            grouped[name] = Tool(tool=name, calls=(old.calls if old else 0) + 1, successes=(old.successes if old else 0) + int(r["status"] in {"ok", "success", "completed"}), failures=(old.failures if old else 0) + int(r["status"] in {"error", "failed"}), unknown_results=(old.unknown_results if old else 0) + int(r["status"] is None), source_breakdown=breakdown, evidence=_evidence(r))
        return _page(list(grouped.values()), cursor, limit)

    def _special(self, pattern: str, factory: Callable[[sqlite3.Row], T], limit: int, cursor: str | None) -> Page[T]:
        with self._db() as db: rows = db.execute("SELECT * FROM events WHERE category LIKE ? OR name LIKE ? ORDER BY event_time DESC, event_seq DESC", (f"%{pattern}%", f"%{pattern}%")).fetchall()
        return _page([factory(r) for r in rows], cursor, limit)

    def approvals(self, limit: int, cursor: str | None) -> Page[Approval]: return self._special("approval", lambda r: Approval(approval_id=r["event_id"], decision=None, decision_source=None, tool=r["name"], session_id=r["session_id"], thread_id=r["thread_id"], observed_at=r["event_time"], evidence=_evidence(r)), limit, cursor)
    def skills(self, limit: int, cursor: str | None) -> Page[Skill]: return self._special("skill", lambda r: Skill(skill=r["name"], status=r["status"], thread_id=r["thread_id"], observed_at=r["event_time"], evidence=_evidence(r)), limit, cursor)

    def repositories(self, limit: int, cursor: str | None) -> Page[Repository]:
        with self._db() as db:
            rows = db.execute("SELECT * FROM repositories ORDER BY last_seen DESC, repo_id DESC").fetchall(); items = [Repository(repo_id=r["repo_id"], root=r["root"], remote_identity=r["remote_identity"], worktrees=[x[0] for x in db.execute("SELECT path FROM worktrees WHERE repo_id=? ORDER BY path", (r["repo_id"],)).fetchall()], evidence=_evidence({"source_class": "GIT", "source_event": "repository"})) for r in rows]
        return _page(items, cursor, limit)

    def snapshots(self, limit: int, cursor: str | None, repo_id: str | None) -> Page[GitSnapshot]:
        where = " WHERE repo_id=?" if repo_id else ""; params = (repo_id,) if repo_id else ()
        with self._db() as db: rows = db.execute(f"SELECT * FROM git_snapshots{where} ORDER BY captured_at DESC, snapshot_observation_id DESC", params).fetchall()
        return _page([GitSnapshot(snapshot_id=r["snapshot_observation_id"], repo_id=r["repo_id"], worktree_id=r["worktree_id"], head_sha=r["head_sha"], head_ref=r["head_ref"], head_state=r["head_state"], detached=bool(r["detached"]), clean=bool(r["clean"]), staged=r["staged_count"], unstaged=r["unstaged_count"], untracked=r["untracked_count"], conflicted=r["conflicted_count"], insertions=r["insertions"], deletions=r["deletions"], captured_at=r["captured_at"], evidence=_evidence({"source_class": "GIT", "source_event": "snapshot"})) for r in rows], cursor, limit)

    def health(self) -> Health:
        def status(table: str, fallback: str) -> str:
            with self._db() as db:
                r = db.execute(f"SELECT status FROM {table} ORDER BY updated_at DESC LIMIT 1").fetchone(); return r[0] if r else fallback
        components = [ComponentHealth(component="OTLP", status=status("collector_health", "available")), ComponentHealth(component="App Server", status=status("app_server_state", "disconnected")), ComponentHealth(component="Hooks", status=status("hook_source_state", "blocked_external_runtime"), reason="Phase 3: IMPLEMENTED / EXTERNAL_RUNTIME_BLOCKED"), ComponentHealth(component="Git", status=status("git_health", "unavailable")), ComponentHealth(component="Archive", status=status("archive_health", "empty")), ComponentHealth(component="DuckDB", status=status("analytics_health", "available")), ComponentHealth(component="API", status="healthy")]
        aggregate = "degraded" if any(x.status in {"failed", "degraded", "blocked_external_runtime", "disconnected", "unavailable"} for x in components) else "healthy"; return Health(status=aggregate, components=components)

    def archive_health(self) -> ArchiveHealth:
        with self._db() as db:
            r = db.execute("SELECT * FROM archive_health WHERE collector='archive'").fetchone(); a = db.execute("SELECT status FROM analytics_health WHERE collector='duckdb'").fetchone()
        return ArchiveHealth(status=r["status"] if r else "empty", batches=_int(r["batches_published_total"] if r else 0), files=_int(r["files_published_total"] if r else 0), rows=_int(r["rows_archived_total"] if r else 0), verification_failures=_int(r["verification_failures_total"] if r else 0), small_files=_int(r["small_files_total"] if r else 0), duckdb_status=a[0] if a else "available")

    def overview(self, start: str | None, end: str | None) -> Overview:
        time, params = self._time(start, end, "event_time")
        def count(table: str, extra: str = "") -> int:
            with self._db() as db:
                return int(db.execute(f"SELECT count(*) FROM {table} WHERE 1=1{time if table == 'events' else ''}{extra}", params if table == "events" else ()).fetchone()[0])
        events = count("events"); tools = count("events", " AND (category LIKE '%tool%' OR name LIKE '%tool%')"); failures = count("events", " AND (category LIKE '%tool%' OR name LIKE '%tool%') AND status IN ('error','failed')"); evidence = Evidence(source_class="DERIVED", fact_type="aggregate")
        with self._db() as db:
            token_rows = db.execute("SELECT usage_json FROM app_server_token_usage").fetchall()
        token_total = 0
        for item in token_rows:
            try:
                body = json.loads(item[0]); total = body.get("total", body); token_total += _int(total.get("totalTokens", total.get("total_tokens")))
            except (TypeError, json.JSONDecodeError): pass
        token_metric = Metric(value=token_total, coverage="available", evidence=evidence) if token_rows else Metric(value=None, coverage="unavailable")
        return Overview(sessions=Metric(value=count("threads"), coverage="available", evidence=evidence), threads=Metric(value=count("threads"), coverage="available", evidence=evidence), turns=Metric(value=count("turns"), coverage="available", evidence=evidence), events=Metric(value=events, coverage="available", evidence=evidence), tool_calls=Metric(value=tools, coverage="available", evidence=evidence), tool_failures=Metric(value=failures, coverage="available", evidence=evidence), approvals=Metric(value=count("events", " AND (category LIKE '%approval%' OR name LIKE '%approval%')"), coverage="available", evidence=evidence), agents=Metric(value=count("events", " AND category='agent_lifecycle'"), coverage="available", evidence=evidence), tokens=token_metric, repositories=Metric(value=count("repositories"), coverage="available", evidence=evidence), health=self.health(), archive=self.archive_health())
