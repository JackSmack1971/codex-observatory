from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from codex_observatory.admin_queries import completions_summary, costs_summary
from codex_observatory.app import create_app
from codex_observatory.sqlite import connect, migrate

NOW = datetime(2026, 9, 21, 12, tzinfo=UTC)


def _db(tmp_path: Path):
    connection = connect(tmp_path / "observatory.db")
    migrate(connection)
    return connection


def _usage(connection, identity: str, *, model: str | None, project: str | None, start: int, input_tokens: int, output_tokens: int, requests: int, service_tier: str | None = None, batch: int | None = None) -> None:
    values = (identity, 1, "openai_admin_api", "completions", start, start + 3600, "1h", project, None, None, model, batch, service_tier, input_tokens, output_tokens, requests, *([None] * 12), start, start + 3600, NOW.isoformat().replace("+00:00", "Z"), "test", "sha256:" + identity, NOW.isoformat().replace("+00:00", "Z"), NOW.isoformat().replace("+00:00", "Z"), 1)
    connection.execute("INSERT INTO openai_usage_completions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", values)


def _cost(connection, identity: str, *, project: str | None, line_item: str | None, currency: str | None, amount: str | None, start: int) -> None:
    values = (identity, 1, "openai_admin_api", "organization_api_billing", start, start + 86400, "1d", project, line_item, None, amount, currency, None, None, start, start + 86400, NOW.isoformat().replace("+00:00", "Z"), "test", "sha256:" + identity, NOW.isoformat().replace("+00:00", "Z"), NOW.isoformat().replace("+00:00", "Z"), 1)
    connection.execute("INSERT INTO openai_costs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", values)


def test_empty_summaries_are_unavailable_and_bounded(tmp_path: Path) -> None:
    db = _db(tmp_path)
    try:
        usage = completions_summary(db, range_key="24h", enabled=True, credential_present=True, now=NOW)
        costs = costs_summary(db, range_key="24h", enabled=True, credential_present=True, now=NOW)
        assert usage["input_tokens"] is None and usage["result_count"] == 0
        assert costs["totals"] == [] and costs["result_count"] == 0
        assert usage["provenance"] == {"source": "openai_admin_api", "scope": "organization", "attribution": "unavailable"}
    finally:
        db.close()


def test_usage_aggregates_dimensions_and_preserves_nulls(tmp_path: Path) -> None:
    db = _db(tmp_path)
    try:
        _usage(db, "u1", model="gpt-a", project="p1", start=int(NOW.timestamp()) - 3600, input_tokens=10, output_tokens=5, requests=2, service_tier="default", batch=0)
        _usage(db, "u2", model=None, project=None, start=int(NOW.timestamp()) - 7200, input_tokens=7, output_tokens=3, requests=1, service_tier=None, batch=None)
        db.commit()
        result = completions_summary(db, range_key="24h", enabled=True, credential_present=True, now=NOW)
        assert (result["input_tokens"], result["output_tokens"], result["model_requests"]) == (17, 8, 3)
        assert any(group["dimension"] == "model" and group["value"] is None for group in result["groups"])
        assert {group["dimension"] for group in result["groups"]} == {"model", "project", "service_tier", "batch"}
    finally:
        db.close()


def test_costs_are_exact_separate_by_currency_and_grouped(tmp_path: Path) -> None:
    db = _db(tmp_path)
    try:
        start = int(NOW.timestamp()) - 3600
        _cost(db, "c1", project="p1", line_item="input", currency="usd", amount="0.10", start=start)
        _cost(db, "c2", project="p1", line_item="output", currency="usd", amount="0.20", start=start)
        _cost(db, "c3", project="p2", line_item="input", currency="eur", amount="1.005", start=start)
        db.commit()
        result = costs_summary(db, range_key="24h", enabled=True, credential_present=True, now=NOW)
        assert {(row["currency"], row["amount"]) for row in result["totals"]} == {("eur", "1.005"), ("usd", "0.30")}
        assert any(row["dimension"] == "project" and row["value"] == "p1" and row["amount"] == "0.30" for row in result["groups"])
        assert any(row["dimension"] == "line_item" and row["value"] == "output" for row in result["groups"])
    finally:
        db.close()


def test_time_range_and_public_api_are_fixed_read_only_surfaces(tmp_path: Path) -> None:
    db = _db(tmp_path)
    db.close()
    client = TestClient(create_app(tmp_path / "observatory.db"))
    response = client.get("/api/v1/admin/summary?range=7d")
    assert response.status_code == 200
    assert response.json()["interval"]["key"] == "7d"
    assert response.json()["provenance"]["scope"] == "organization"
    assert response.json()["provenance"]["attribution"] == "unavailable"
    assert client.get("/api/v1/admin/summary?range=2d").status_code == 422
    assert client.post("/api/v1/admin/summary").status_code == 405
    assert "is_current" not in response.text and "result_sha256" not in response.text
