"""Privacy-first, observational Codex command-hook adapter."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path
from typing import Any

from .models import CanonicalEvent, RawEnvelope
from .paths import resolve_paths
from .sqlite import persist, utc_now

ADAPTER_VERSION = "codex-hooks-v1"
SOURCE_CLASS = "hook"
SUPPORTED_EVENTS = frozenset({
    "SessionStart", "SessionEnd", "PreToolUse", "PermissionRequest", "PostToolUse",
    "PreCompact", "PostCompact", "UserPromptSubmit", "SubagentStart", "SubagentStop",
    "Stop", "Interrupt",
})
_REDACTED_KEYS = frozenset({"prompt", "last_assistant_message", "tool_input", "tool_response", "agent_transcript_path"})


def _digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _redact(value: object, key: str = "") -> tuple[object, int]:
    if key in _REDACTED_KEYS and value not in (None, ""):
        if key == "tool_input" and isinstance(value, dict):
            return {"description": value.get("description"), "redacted": True, "sha256": _digest(value)}, 1
        return {"redacted": True, "sha256": _digest(value)}, 1
    if isinstance(value, dict):
        output: dict[str, object] = {}
        count = 0
        for child_key, child in value.items():
            safe, child_count = _redact(child, child_key.lower())
            output[child_key] = safe
            count += child_count
        return output, count
    if isinstance(value, list):
        list_output: list[object] = []
        count = 0
        for child in value:
            safe, child_count = _redact(child, key)
            list_output.append(safe)
            count += child_count
        return list_output, count
    return value, 0


def _event_name(event_name: str) -> tuple[str, str]:
    names = {
        "SessionStart": ("session_lifecycle", "session_start"),
        "SessionEnd": ("session_lifecycle", "session_end"),
        "PostToolUse": ("tool_observation", "tool_observed"),
        "PreToolUse": ("tool_observation", "tool_observation_requested"),
        "PermissionRequest": ("approval_request_observed", "approval_request_observed"),
        "PreCompact": ("context_lifecycle", "context_pre_compact"),
        "PostCompact": ("context_lifecycle", "context_post_compact"),
        "UserPromptSubmit": ("turn_lifecycle", "prompt_submitted"),
        "SubagentStart": ("agent_lifecycle", "agent_start"),
        "SubagentStop": ("agent_lifecycle", "agent_stop"),
        "Stop": ("turn_lifecycle", "turn_stop"),
        "Interrupt": ("turn_lifecycle", "turn_interrupt"),
    }
    return names.get(event_name, ("hook", "unknown"))


def normalize(payload: dict[str, Any], *, received_at: str | None = None) -> tuple[RawEnvelope, CanonicalEvent, dict[str, Any], int]:
    if not isinstance(payload, dict):
        raise TypeError("hook input must be a JSON object")
    event_name = payload.get("hook_event_name")
    if not isinstance(event_name, str) or not event_name:
        raise ValueError("hook_event_name is required")
    received = received_at or utc_now()
    safe_payload, redactions = _redact(payload)
    if not isinstance(safe_payload, dict):
        raise TypeError("redacted hook payload must be an object")
    body = json.dumps(safe_payload, sort_keys=True, separators=(",", ":")).encode()
    digest = hashlib.sha256(body).hexdigest()
    ingest_id = uuid.uuid4().hex
    event_id = hashlib.sha256(f"hook:{event_name}:{digest}".encode()).hexdigest()
    category, name = _event_name(event_name)
    session_id = payload.get("session_id") if isinstance(payload.get("session_id"), str) else None
    turn_id = payload.get("turn_id") if isinstance(payload.get("turn_id"), str) else None
    tool_use_id = payload.get("tool_use_id") if isinstance(payload.get("tool_use_id"), str) else None
    agent_id = payload.get("agent_id") if isinstance(payload.get("agent_id"), str) else None
    agent_type = payload.get("agent_type") if isinstance(payload.get("agent_type"), str) else None
    attributes = dict(safe_payload)
    attributes["source_class"] = SOURCE_CLASS
    attributes["received_at"] = received
    attributes["adapter_version"] = ADAPTER_VERSION
    if event_name == "PermissionRequest":
        attributes["decision"] = "none"
    result = payload.get("tool_response")
    status: str | None = None
    if isinstance(result, dict):
        if isinstance(result.get("success"), bool):
            status = "ok" if result["success"] else "error"
        elif isinstance(result.get("exit_code"), int):
            status = "ok" if result["exit_code"] == 0 else "error"
    if status:
        attributes["tool_result_status"] = status
    event = CanonicalEvent(event_id, received, None, received, event_name, "hook", ADAPTER_VERSION,
                           f"sha256:{digest}", ADAPTER_VERSION, category, name, attributes,
                           session_id=session_id, turn_id=turn_id, call_id=tool_use_id,
                           operation_id=agent_id, status=status)
    envelope = RawEnvelope(ingest_id, received, SOURCE_CLASS, "local-default", event_name, ADAPTER_VERSION,
                           "application/json", None, "json", f"sha256:{digest}", len(body),
                           None, "accepted", retention_class="metadata", payload=body)
    return envelope, event, {"hook_event_name": event_name, "session_id": session_id,
                             "turn_id": turn_id, "tool_use_id": tool_use_id,
                             "agent_id": agent_id, "agent_type": agent_type,
                             "redaction_count": redactions, "payload": safe_payload}, redactions


def spool_root(path: Path | None = None) -> Path:
    return path or resolve_paths().spool_root / "hooks"


def write_spool(payload: dict[str, Any], path: Path | None = None) -> Path:
    envelope, event, details, redactions = normalize(payload)
    root = spool_root(path)
    root.mkdir(parents=True, exist_ok=True)
    record = {"envelope": {"ingest_id": envelope.ingest_id, "received_at": envelope.received_at,
                             "source": envelope.source, "source_instance": envelope.source_instance,
                             "source_event_type": envelope.source_event_type, "source_version": envelope.source_version,
                             "payload_sha256": envelope.payload_sha256,
                             "payload_size": envelope.payload_size, "parse_status": envelope.parse_status},
              "event": {"event_id": event.event_id, "event_time": event.event_time, "observed_at": event.observed_at,
                        "source_event": event.source_event, "category": event.category, "name": event.name,
                        "attributes": event.attributes, "session_id": event.session_id, "turn_id": event.turn_id,
                        "call_id": event.call_id, "operation_id": event.operation_id, "status": event.status},
              "details": {**{k: v for k, v in details.items() if k != "payload"}, "payload": details["payload"]},
              "redaction_count": redactions}
    target = root / f"{envelope.ingest_id}.json"
    with target.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    return target


def _write_rejected_spool(raw: str, path: Path | None = None) -> Path:
    body = raw.encode()
    received = utc_now()
    ingest_id = uuid.uuid4().hex
    root = spool_root(path)
    root.mkdir(parents=True, exist_ok=True)
    record = {"envelope": {"ingest_id": ingest_id, "received_at": received, "source": SOURCE_CLASS,
                            "source_instance": "local-default", "source_event_type": "malformed",
                            "source_version": ADAPTER_VERSION, "payload_sha256": "sha256:" + hashlib.sha256(body).hexdigest(),
                            "payload_size": len(body), "parse_status": "rejected"},
              "details": {"hook_event_name": "malformed", "session_id": None, "turn_id": None,
                          "tool_use_id": None, "agent_id": None, "agent_type": None, "payload": {}},
              "redaction_count": 0}
    target = root / f"{ingest_id}.json"
    with target.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    return target


def _event_from_record(data: dict[str, Any]) -> CanonicalEvent:
    value = data["event"]
    return CanonicalEvent(value["event_id"], value["event_time"], None, value["observed_at"], value["source_event"],
                          "hook", None, data["envelope"]["payload_sha256"], ADAPTER_VERSION, value["category"],
                          value["name"], value["attributes"], session_id=value.get("session_id"),
                          turn_id=value.get("turn_id"), call_id=value.get("call_id"), operation_id=value.get("operation_id"),
                          status=value.get("status"))


def _envelope_from_record(data: dict[str, Any]) -> RawEnvelope:
    value = data["envelope"]
    payload = json.dumps(data["details"]["payload"], sort_keys=True, separators=(",", ":")).encode()
    return RawEnvelope(value["ingest_id"], value["received_at"], SOURCE_CLASS, value["source_instance"],
                       value["source_event_type"], value.get("source_version"), "application/json", None, "json", value["payload_sha256"],
                       value["payload_size"], None, value["parse_status"], retention_class="metadata", payload=payload)


def _correlate(connection: Any, event: CanonicalEvent) -> int:
    clauses: list[tuple[str, str, str]] = []
    if event.session_id:
        clauses.append(("session_id", event.session_id, "session_id"))
    if event.turn_id:
        clauses.append(("turn_id", event.turn_id, "turn_id"))
    if event.call_id:
        clauses.append(("call_id", event.call_id, "tool_use_id"))
    if event.operation_id:
        clauses.append(("operation_id", event.operation_id, "agent_id"))
    total = 0
    for column, value, method in clauses:
        if method == "session_id":
            rows = connection.execute("SELECT event_id FROM events WHERE (session_id=? OR thread_id=?) AND event_id<>?", (value, value, event.event_id)).fetchall()
        else:
            rows = connection.execute(f"SELECT event_id FROM events WHERE {column}=? AND event_id<>?", (value, event.event_id)).fetchall()
        for row in rows:
            confidence = "high" if method in {"turn_id", "tool_use_id", "agent_id"} else "medium"
            connection.execute("INSERT OR IGNORE INTO correlation_edges(event_id,related_event_id,correlation_method,correlation_confidence,created_at) VALUES(?,?,?,?,?)",
                               (event.event_id, row[0], method, confidence, utc_now()))
            total += 1
    return total


def _update_state(connection: Any, *, received: int = 0, normalized: int = 0, unknown: int = 0,
                  redactions: int = 0, duplicate: int = 0, correlations: int = 0, unresolved: int = 0,
                  ingest_failure: int = 0, delivery_failure: int = 0, event: str | None = None,
                  error: str | None = None) -> None:
    now = utc_now()
    with connection:
        connection.execute("""INSERT INTO hook_source_state(source_instance,status,received_total,normalized_total,unknown_event_total,privacy_redaction_total,duplicate_total,correlation_total,correlation_unresolved_total,ingest_failure_total,delivery_failure_total,last_event,last_error,updated_at)
            VALUES('local-default',?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(source_instance) DO UPDATE SET
            received_total=received_total+excluded.received_total,normalized_total=normalized_total+excluded.normalized_total,
            unknown_event_total=unknown_event_total+excluded.unknown_event_total,privacy_redaction_total=privacy_redaction_total+excluded.privacy_redaction_total,
            duplicate_total=duplicate_total+excluded.duplicate_total,correlation_total=correlation_total+excluded.correlation_total,
            correlation_unresolved_total=correlation_unresolved_total+excluded.correlation_unresolved_total,
            ingest_failure_total=ingest_failure_total+excluded.ingest_failure_total,delivery_failure_total=delivery_failure_total+excluded.delivery_failure_total,
            status=CASE WHEN excluded.last_error IS NOT NULL THEN 'degraded' WHEN excluded.normalized_total>0 THEN 'healthy' ELSE hook_source_state.status END,
            last_event=COALESCE(excluded.last_event,hook_source_state.last_event),last_error=COALESCE(excluded.last_error,hook_source_state.last_error),updated_at=excluded.updated_at""",
            ("healthy", received, normalized, unknown, redactions, duplicate, correlations, unresolved, ingest_failure, delivery_failure, event, error, now))


def import_spool(connection: Any, path: Path | None = None) -> dict[str, int]:
    root = spool_root(path)
    counts = {"received": 0, "normalized": 0, "unknown": 0, "redactions": 0, "duplicate": 0, "correlation": 0, "unresolved": 0, "failure": 0}
    if not root.exists():
        return counts
    for record_path in sorted(root.glob("*.json")):
        counts["received"] += 1
        try:
            data = json.loads(record_path.read_text(encoding="utf-8"))
            envelope = _envelope_from_record(data)
            if envelope.parse_status != "accepted":
                persist(connection, envelope, [], source_class=SOURCE_CLASS)
                counts["failure"] += 1
                record_path.unlink()
                continue
            event = _event_from_record(data)
            prior = connection.execute("SELECT 1 FROM raw_events WHERE source_event_type=? AND payload_sha256=? AND parse_status IN ('accepted','duplicate')", (envelope.source_event_type, envelope.payload_sha256)).fetchone()
            if prior:
                counts["duplicate"] += 1
            else:
                persist(connection, envelope, [event], source_class=SOURCE_CLASS)
                details = data["details"]
                with connection:
                    connection.execute("INSERT INTO hook_events(ingest_id,event_id,hook_event_name,session_id,turn_id,tool_use_id,agent_id,agent_type,redaction_count,received_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                                       (envelope.ingest_id, event.event_id, details["hook_event_name"], details.get("session_id"), details.get("turn_id"), details.get("tool_use_id"), details.get("agent_id"), details.get("agent_type"), data.get("redaction_count", 0), envelope.received_at))
                correlations = _correlate(connection, event)
                counts["normalized"] += 1
                counts["correlation"] += correlations
                if not correlations and any((event.session_id, event.turn_id, event.call_id, event.operation_id)):
                    counts["unresolved"] += 1
            counts["redactions"] += int(data.get("redaction_count", 0))
            if data["details"]["hook_event_name"] not in SUPPORTED_EVENTS:
                counts["unknown"] += 1
            record_path.unlink()
        except Exception:  # noqa: BLE001 - quarantine malformed records and keep importing later records.
            counts["failure"] += 1
            bad = record_path.with_suffix(".bad")
            try:
                record_path.replace(bad)
            except OSError:
                pass
    _update_state(connection, received=counts["received"], normalized=counts["normalized"], unknown=counts["unknown"], redactions=counts["redactions"], duplicate=counts["duplicate"], correlations=counts["correlation"], unresolved=counts["unresolved"], ingest_failure=counts["failure"], event="import" if counts["received"] else None, error=None if not counts["failure"] else "malformed_or_unpersisted_hook_record")
    return counts


def run_hook(stdin: str, path: Path | None = None) -> int:
    try:
        payload = json.loads(stdin)
        if isinstance(payload, dict):
            write_spool(payload, path)
        else:
            _write_rejected_spool(stdin, path)
    except (json.JSONDecodeError, TypeError, ValueError):
        try:
            _write_rejected_spool(stdin, path)
        except OSError:
            pass
    except Exception:  # noqa: BLE001 - hook failure must never affect Codex behavior.
        # Observational hooks are deliberately neutral, including malformed input or spool failure.
        return 0
    return 0
