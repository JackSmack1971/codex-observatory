from __future__ import annotations

import gzip
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from google.protobuf.json_format import MessageToJson
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import (
    ExportLogsServiceRequest,
)
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import (
    ExportMetricsServiceRequest,
)
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)

from codex_observatory.app import create_app
from codex_observatory.config import ObservatoryConfig
from codex_observatory.sqlite import connect, migrate


def _client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(tmp_path / "observatory.db"))


def _log(name: str, prompt: str | None = None) -> bytes:
    request = ExportLogsServiceRequest()
    record = request.resource_logs.add().scope_logs.add().log_records.add()
    event = record.attributes.add()
    event.key = "event.name"
    event.value.string_value = name
    unknown = record.attributes.add()
    unknown.key = "future.codex.field"
    unknown.value.string_value = "kept"
    if prompt:
        value = record.attributes.add()
        value.key = "user.prompt"
        value.value.string_value = prompt
    return request.SerializeToString()


def _metric() -> bytes:
    request = ExportMetricsServiceRequest()
    metric = request.resource_metrics.add().scope_metrics.add().metrics.add()
    metric.name = "codex.tool.call"
    metric.sum.data_points.add().as_int = 1
    return request.SerializeToString()


def _trace() -> bytes:
    request = ExportTraceServiceRequest()
    span = request.resource_spans.add().scope_spans.add().spans.add()
    span.name = "codex.tool"
    span.trace_id = b"1234567890123456"
    span.span_id = b"12345678"
    span.start_time_unix_nano = 1_000_000_000
    span.end_time_unix_nano = 2_000_000_000
    return request.SerializeToString()


@pytest.mark.parametrize(("path", "payload"), [("logs", _log("codex.api_request")), ("metrics", _metric()), ("traces", _trace())])
def test_all_otlp_signals_are_persisted(tmp_path: Path, path: str, payload: bytes) -> None:
    client = _client(tmp_path)
    result = client.post(f"/v1/{path}", content=payload, headers={"content-type": "application/x-protobuf"})
    assert result.status_code == 200
    assert result.headers["content-type"].startswith("application/x-protobuf")
    assert client.get("/health").json()["normalized_total"] == 1
    assert len(client.get("/events").json()) == 1


def test_gzip_and_duplicate_policy(tmp_path: Path) -> None:
    client = _client(tmp_path)
    payload = _log("codex.tool_result")
    headers = {"content-type": "application/x-protobuf", "content-encoding": "gzip"}
    assert client.post("/v1/logs", content=gzip.compress(payload), headers=headers).status_code == 200
    assert client.post("/v1/logs", content=gzip.compress(payload), headers=headers).status_code == 200
    assert len(client.get("/events").json()) == 1
    raw = client.get("/raw-events").json()
    assert len(raw) == 2 and raw[0]["parse_status"] == "duplicate"


def test_invalid_payload_media_type_and_diagnostics(tmp_path: Path) -> None:
    client = _client(tmp_path)
    assert client.post("/v1/logs", content=b"bad", headers={"content-type": "application/x-protobuf"}).status_code == 400
    assert client.post("/v1/logs", content=b"{}", headers={"content-type": "text/plain"}).status_code == 415
    health = client.get("/health").json()
    assert health["rejected_total"] == 2
    assert len(client.get("/raw-events").json()) == 2


def test_malformed_gzip_isolated_as_rejected_raw_evidence(tmp_path: Path) -> None:
    client = _client(tmp_path)
    result = client.post("/v1/logs", content=b"not-gzip", headers={"content-type": "application/x-protobuf", "content-encoding": "gzip"})
    assert result.status_code == 400
    assert client.get("/health").json()["rejected_total"] == 1
    assert client.get("/raw-events").json()[0]["error_code"] == "malformed_compression"


def test_unknown_event_and_attribute_survive_without_prompt(tmp_path: Path) -> None:
    client = _client(tmp_path)
    assert client.post("/v1/logs", content=_log("codex.future_event", "secret prompt"), headers={"content-type": "application/x-protobuf"}).status_code == 200
    event = client.get("/events").json()[0]
    attrs = json.loads(event["attributes_json"])
    assert attrs["future.codex.field"] == "kept"
    assert "prompt" not in event["attributes_json"].lower()
    assert client.get("/health").json()["unknown_event_total"] == 1
    raw = client.get("/raw-events").json()[0]
    assert raw["payload_sha256"].startswith("sha256:")
    assert raw["payload_size"] > 0


def test_normalization_failure_isolated_as_raw_diagnostic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import codex_observatory.app as app_module

    def fail(*args: object, **kwargs: object) -> tuple[list[object], int]:
        raise ValueError("normalizer test failure")

    monkeypatch.setattr(app_module, "normalize", fail)
    client = _client(tmp_path)
    result = client.post("/v1/logs", content=_log("codex.api_request"), headers={"content-type": "application/x-protobuf"})
    assert result.status_code == 503
    raw = client.get("/raw-events").json()[0]
    assert raw["parse_status"] == "normalization_failed"
    assert "normalizer test failure" in raw["error_message"]
    assert client.get("/health").json()["persistence_error_total"] == 0


def test_persistence_failure_returns_retryable_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import codex_observatory.app as app_module

    def fail(*args: object, **kwargs: object) -> int:
        raise OSError("database unavailable")

    monkeypatch.setattr(app_module, "persist", fail)
    client = _client(tmp_path)
    result = client.post("/v1/logs", content=_log("codex.api_request"), headers={"content-type": "application/x-protobuf"})
    assert result.status_code == 503
    assert client.get("/health").json()["persistence_error_total"] == 1


def test_json_otlp_uses_otlp_mapping(tmp_path: Path) -> None:
    client = _client(tmp_path)
    payload = MessageToJson(ExportLogsServiceRequest.FromString(_log("codex.conversation_starts"))).encode()
    result = client.post("/v1/logs", content=payload, headers={"content-type": "application/json"})
    assert result.status_code == 200
    assert result.headers["content-type"].startswith("application/json")


def test_sqlite_policies_and_restart(tmp_path: Path) -> None:
    path = tmp_path / "observatory.db"
    client = _client(tmp_path)
    assert client.post("/v1/logs", content=_log("codex.api_request"), headers={"content-type": "application/x-protobuf"}).status_code == 200
    first = connect(path)
    migrate(first)
    assert first.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert first.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert first.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    first.close()
    second = connect(path)
    migrate(second)
    assert [row[0] for row in second.execute("SELECT version FROM schema_migrations")] == [1, 2]
    assert second.execute("SELECT count(*) FROM events").fetchone()[0] == 1


def test_concurrent_requests_use_request_owned_connections(tmp_path: Path) -> None:
    client = _client(tmp_path)
    payloads = [_log(f"codex.future_{index}") for index in range(8)]
    with ThreadPoolExecutor(max_workers=4) as executor:
        statuses = list(executor.map(lambda payload: client.post("/v1/logs", content=payload, headers={"content-type": "application/x-protobuf"}).status_code, payloads))
    assert statuses == [200] * len(payloads)
    assert client.get("/health").json()["persistence_error_total"] == 0


def test_default_privacy_configuration() -> None:
    config = ObservatoryConfig()
    assert config.privacy.mode == "minimal"
    assert config.privacy.store_prompts is False
    assert config.privacy.persist_raw_wire_payloads is False


def test_recursive_anyvalue_and_histogram_evidence(tmp_path: Path) -> None:
    from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import (
        ExportLogsServiceRequest,
    )

    request = ExportLogsServiceRequest()
    record = request.resource_logs.add().scope_logs.add().log_records.add()
    nested = record.attributes.add()
    nested.key = "future.map"
    nested.value.kvlist_value.values.add(key="items").value.array_value.values.add(string_value="kept")
    client = _client(tmp_path)
    assert client.post("/v1/logs", content=request.SerializeToString(), headers={"content-type": "application/x-protobuf"}).status_code == 200
    attrs = json.loads(client.get("/events").json()[0]["attributes_json"])
    assert attrs["future.map"] == {"items": ["kept"]}

    metric_request = ExportMetricsServiceRequest()
    metric = metric_request.resource_metrics.add().scope_metrics.add().metrics.add(name="codex.latency")
    point = metric.histogram.data_points.add(count=3, sum=4.5, time_unix_nano=9)
    point.bucket_counts.extend([1, 2])
    point.explicit_bounds.append(10.0)
    assert client.post("/v1/metrics", content=metric_request.SerializeToString(), headers={"content-type": "application/x-protobuf"}).status_code == 200
    metric_attrs = json.loads(client.get("/events").json()[0]["attributes_json"])
    assert "value" not in metric_attrs
    assert metric_attrs["observations"][0]["count"] == 3
