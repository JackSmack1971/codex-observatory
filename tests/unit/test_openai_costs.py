from __future__ import annotations

import json
import subprocess
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from codex_observatory.api_models import AdminCost
from codex_observatory.app import create_app
from codex_observatory.config import OpenAIAdminConfig
from codex_observatory.openai_costs import health, sync
from codex_observatory.sqlite import connect, migrate


def result(value: object = 1.25, **extra: object) -> dict[str, object]:
    return {
        "object": "organization.costs.result",
        "amount": {"value": value, "currency": "usd"},
        "quantity": 1000.0,
        "quantity_unit": "tokens",
        "project_id": "proj",
        "line_item": "input_tokens",
        "api_key_id": None,
        **extra,
    }


class Pages:
    def __init__(self, pages: list[dict[str, object]]):
        self.pages = pages
        self.calls: list[dict[str, object]] = []

    def costs(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(kwargs)
        return self.pages[len(self.calls) - 1]


@pytest.fixture()
def db(tmp_path: Path):
    connection = connect(tmp_path / "observatory.db")
    migrate(connection)
    yield connection
    connection.close()


def config(**kwargs: object) -> OpenAIAdminConfig:
    return OpenAIAdminConfig(costs_enabled=True, **kwargs)


def page(
    *results: dict[str, object], more: bool = False, next_page: str | None = None
) -> dict[str, object]:
    return {
        "object": "page",
        "data": [
            {
                "object": "bucket",
                "start_time": 100,
                "end_time": 86400,
                "results": list(results),
            }
        ],
        "has_more": more,
        "next_page": next_page,
    }


def test_disabled_and_missing_credential_states(db) -> None:
    assert (
        health(db, enabled=False, credential_present=False)["status"]
        == "ADMIN_COSTS_DISABLED"
    )
    assert (
        health(db, enabled=True, credential_present=False)["status"]
        == "ADMIN_COSTS_CREDENTIAL_MISSING"
    )


def test_one_cost_nullable_exact_amount_and_idempotency(db) -> None:
    client = Pages(
        [page(result(project_id=None, line_item=None, api_key_id=None, value=0.1))]
    )
    sync(db, config(), client, now=datetime.fromtimestamp(86400, UTC))
    row = db.execute("SELECT * FROM openai_costs").fetchone()
    assert row["amount_value"] == "0.1"
    assert (
        row["project_id"] is None
        and row["line_item"] is None
        and row["api_key_id"] is None
    )
    sync(
        db,
        config(),
        Pages(
            [page(result(project_id=None, line_item=None, api_key_id=None, value=0.1))]
        ),
        now=datetime.fromtimestamp(86400, UTC),
    )
    assert db.execute("SELECT count(*) FROM openai_costs").fetchone()[0] == 1


def test_pagination_revision_and_repeated_cursor(db) -> None:
    client = Pages(
        [page(result(), more=True, next_page="p2"), page(result(value=2.5), more=False)]
    )
    sync(db, config(), client, now=datetime.fromtimestamp(86400, UTC))
    assert [call.get("page") for call in client.calls] == [None, "p2"]
    assert db.execute("SELECT count(*) FROM openai_costs").fetchone()[0] == 2
    changed = Pages([page(result(value=9.0))])
    sync(db, config(), changed, now=datetime.fromtimestamp(86400, UTC))
    assert db.execute("SELECT count(*) FROM openai_costs").fetchone()[0] == 3
    assert (
        db.execute(
            "SELECT amount_value FROM openai_costs WHERE is_current=1"
        ).fetchone()[0]
        == "9.0"
    )
    with pytest.raises(RuntimeError, match="repeated"):
        sync(
            db,
            config(),
            Pages(
                [
                    page(result(), more=True, next_page="same"),
                    page(result(), more=True, next_page="same"),
                ]
            ),
            now=datetime.fromtimestamp(86400, UTC),
        )


@pytest.mark.parametrize(
    "bad",
    [
        {"data": [], "has_more": True, "next_page": None},
        {"object": "page", "data": [{}], "has_more": False, "next_page": None},
        {"object": "page", "data": [], "has_more": False, "next_page": None},
    ],
)
def test_malformed_and_empty_pages(db, bad: dict[str, object]) -> None:
    if bad["data"] == [] and bad["has_more"] is False:
        assert (
            sync(db, config(), Pages([bad]), now=datetime.fromtimestamp(86400, UTC))[
                "status"
            ]
            == "ADMIN_COSTS_HEALTHY"
        )
    else:
        with pytest.raises(RuntimeError):
            sync(db, config(), Pages([bad]), now=datetime.fromtimestamp(86400, UTC))


@pytest.mark.parametrize("status", [401, 403, 400])
def test_auth_and_ordinary_4xx_do_not_retry(db, status: int) -> None:
    class Failure:
        calls = 0

        def costs(self, **kwargs: object):
            self.calls += 1
            error = RuntimeError(f"Authorization: Bearer secret-{status}")
            error.status_code = status  # type: ignore[attr-defined]
            raise error

    client = Failure()
    with pytest.raises(RuntimeError):
        sync(
            db,
            config(),
            client,
            now=datetime.fromtimestamp(86400, UTC),
            sleep=lambda _: None,
        )
    assert client.calls == 1


@pytest.mark.parametrize("status", [None, 408, 409, 429, 500, 503])
def test_transient_retries(db, status: int | None) -> None:
    class Retry:
        calls = 0

        def costs(self, **kwargs: object):
            self.calls += 1
            if self.calls == 1:
                error = RuntimeError("temporary")
                if status is not None:
                    error.status_code = status  # type: ignore[attr-defined]
                raise error
            return page()

    client = Retry()
    sync(
        db,
        config(),
        client,
        now=datetime.fromtimestamp(86400, UTC),
        sleep=lambda _: None,
    )
    assert client.calls == 2


def test_checkpoint_long_outage_zero_overlap_and_restart(db) -> None:
    cfg = config(initial_lookback_hours=24, overlap_hours=0)
    first = Pages([page()])
    sync(db, cfg, first, now=datetime.fromtimestamp(100000, UTC))
    assert first.calls[0]["start_time"] == 100000 - 86400
    second = Pages([page()])
    sync(db, cfg, second, now=datetime.fromtimestamp(100000 + 10 * 86400, UTC))
    assert second.calls[0]["start_time"] == 100000

    class Broken(Pages):
        def costs(self, **kwargs: object):
            if not self.calls:
                self.calls.append(kwargs)
                return page(result(), more=True, next_page="resume")
            raise OSError("stopped")

    with pytest.raises(RuntimeError):
        sync(db, cfg, Broken([]), now=datetime.fromtimestamp(200000, UTC))
    assert (
        db.execute("SELECT page_cursor FROM openai_cost_sync_state").fetchone()[0]
        == "resume"
    )
    resumed = Pages([page(more=False)])
    sync(db, cfg, resumed, now=datetime.fromtimestamp(999999, UTC))
    assert (
        resumed.calls[0]["page"] == "resume" and resumed.calls[0]["end_time"] == 200000
    )


def test_concurrent_busy_and_api_allow_list(tmp_path: Path) -> None:
    path = tmp_path / "busy.db"
    first_db = connect(path)
    second_db = connect(path)
    migrate(first_db)
    entered, release = threading.Event(), threading.Event()

    class Blocking:
        def costs(self, **kwargs: object):
            entered.set()
            release.wait(5)
            return page()

    holder: list[dict[str, object]] = []
    worker = threading.Thread(
        target=lambda: holder.append(
            sync(first_db, config(), Blocking(), now=datetime.fromtimestamp(5000, UTC))
        )
    )
    worker.start()
    assert entered.wait(5)
    assert (
        sync(second_db, config(), Pages([]), now=datetime.fromtimestamp(9000, UTC))[
            "status"
        ]
        == "ADMIN_COSTS_BUSY"
    )
    release.set()
    worker.join(5)
    assert holder[0]["status"] == "ADMIN_COSTS_HEALTHY"
    first_db.close()
    second_db.close()
    with TestClient(create_app(path)) as client:
        response = client.get("/api/v1/admin/costs")
        assert response.status_code == 200
        assert client.get("/api/v1/admin/costs/health").status_code == 200
        assert client.post("/api/v1/admin/costs").status_code == 405
        if response.json()["items"]:
            AdminCost.model_validate(response.json()["items"][0])
            assert "result_sha256" not in response.json()["items"][0]


def test_concurrent_cost_sync_is_busy_across_processes(tmp_path: Path) -> None:
    from codex_observatory.openai_costs import (
        _acquire_database_lock,
        _release_database_lock,
    )

    path = tmp_path / "cross-process.db"
    connection = connect(path)
    migrate(connection)
    owner = "parent-test-owner"
    assert _acquire_database_lock(connection, owner)
    code = """
import json, sys
from pathlib import Path
from codex_observatory.config import OpenAIAdminConfig
from codex_observatory.openai_costs import sync
from codex_observatory.sqlite import connect, migrate
class Empty:
    def costs(self, **kwargs):
        return {"object": "page", "data": [], "has_more": False, "next_page": None}
db = connect(Path(sys.argv[1])); migrate(db)
print(json.dumps(sync(db, OpenAIAdminConfig(costs_enabled=True), Empty())))
db.close()
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["status"] == "ADMIN_COSTS_BUSY"
    _release_database_lock(connection, owner)
    connection.close()


def test_fresh_and_upgrade_migrations(tmp_path: Path) -> None:
    fresh = connect(tmp_path / "fresh.db")
    migrate(fresh)
    assert (
        fresh.execute(
            "SELECT version FROM schema_migrations WHERE version=13"
        ).fetchone()[0]
            == 13
    )
    fresh.close()
    upgraded = connect(tmp_path / "upgrade.db")
    upgraded.execute(
        "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL, checksum TEXT NOT NULL UNIQUE)"
    )
    for version, sql in __import__(
        "codex_observatory.sqlite", fromlist=["MIGRATIONS"]
    ).MIGRATIONS:
        if version < 11:
            upgraded.execute(
                "INSERT INTO schema_migrations VALUES(?,?,?)",
                (
                    version,
                    "now",
                    __import__("hashlib").sha256(sql.encode()).hexdigest(),
                ),
            )
    upgraded.commit()
    migrate(upgraded)
    assert upgraded.execute("SELECT count(*) FROM openai_costs").fetchone()[0] == 0
    upgraded.close()
