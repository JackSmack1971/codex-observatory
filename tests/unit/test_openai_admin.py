from __future__ import annotations

import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from codex_observatory.api_models import AdminCompletionUsage
from codex_observatory.app import create_app
from codex_observatory.config import OpenAIAdminConfig
from codex_observatory.openai_admin import health, sanitize_admin_error, sync
from codex_observatory.sqlite import connect, migrate


def result(model: str = "gpt-test", **extra: object) -> dict[str, object]:
    return {"object": "organization.usage.completions.result", "input_tokens": 10, "output_tokens": 5,
            "num_model_requests": 2, "model": model, **extra}


class Pages:
    def __init__(self, pages: list[dict[str, object]]):
        self.pages = pages
        self.calls: list[dict[str, object]] = []

    def completions(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(kwargs)
        return self.pages[len(self.calls) - 1]


@pytest.fixture()
def db(tmp_path: Path):
    connection = connect(tmp_path / "observatory.db")
    migrate(connection)
    yield connection
    connection.close()


def test_disabled_and_missing_credential_states(db) -> None:
    assert health(db, enabled=False, credential_present=False)["status"] == "ADMIN_DISABLED"
    assert health(db, enabled=True, credential_present=False)["status"] == "ADMIN_CREDENTIAL_MISSING"


def test_multi_page_nullable_dimensions_and_idempotency(db) -> None:
    client = Pages([
        {"data": [{"start_time": 100, "end_time": 3700, "results": [result(project_id=None, user_id=None, api_key_id=None, batch=None, service_tier=None)]}], "has_more": True, "next_page": "page-2"},
        {"data": [{"start_time": 3700, "end_time": 7300, "results": [result("gpt-other", project_id="proj", user_id="user", api_key_id="key", batch=True, service_tier="default")]}], "has_more": False, "next_page": None},
    ])
    config = OpenAIAdminConfig(enabled=True)
    sync(db, config, client, now=datetime.fromtimestamp(7300, UTC))
    assert [call.get("page") for call in client.calls] == [None, "page-2"]
    assert db.execute("SELECT count(*) FROM openai_usage_completions").fetchone()[0] == 2
    sync(db, config, Pages(client.pages), now=datetime.fromtimestamp(7300, UTC))
    assert db.execute("SELECT count(*) FROM openai_usage_completions").fetchone()[0] == 2


def test_changed_upstream_result_creates_revision(db) -> None:
    config = OpenAIAdminConfig(enabled=True)
    first = Pages([{"data": [{"start_time": 100, "end_time": 3700, "results": [result()]}], "has_more": False, "next_page": None}])
    sync(db, config, first, now=datetime.fromtimestamp(3700, UTC))
    changed = Pages([{"data": [{"start_time": 100, "end_time": 3700, "results": [result(input_tokens=99)]}], "has_more": False, "next_page": None}])
    sync(db, config, changed, now=datetime.fromtimestamp(3700, UTC))
    assert db.execute("SELECT count(*) FROM openai_usage_completions").fetchone()[0] == 2
    assert db.execute("SELECT input_tokens FROM openai_usage_completions WHERE is_current=1").fetchone()[0] == 99


def test_retry_and_malformed_cursor_protection(db) -> None:
    class Retry:
        def __init__(self): self.calls = 0
        def completions(self, **kwargs: object):
            self.calls += 1
            if self.calls == 1:
                error = RuntimeError("temporary")
                error.status_code = 500  # type: ignore[attr-defined]
                raise error
            return {"data": [], "has_more": False, "next_page": None}
    retry = Retry()
    sync(db, OpenAIAdminConfig(enabled=True), retry, now=datetime.fromtimestamp(1000, UTC), sleep=lambda _: None)
    assert retry.calls == 2

    bad = Pages([{"data": [], "has_more": True, "next_page": None}])
    with pytest.raises(ValueError, match="next_page"):
        sync(db, OpenAIAdminConfig(enabled=True), bad, now=datetime.fromtimestamp(1000, UTC))


@pytest.mark.parametrize("status", [401, 403, 400])
def test_non_transient_transport_failures_do_not_retry(db, status: int) -> None:
    class Failure:
        calls = 0

        def completions(self, **kwargs: object):
            self.calls += 1
            error = RuntimeError(f"failure {status}")
            error.status_code = status  # type: ignore[attr-defined]
            raise error

    client = Failure()
    with pytest.raises(RuntimeError):
        sync(db, OpenAIAdminConfig(enabled=True), client, now=datetime.fromtimestamp(1000, UTC), sleep=lambda _: None)
    assert client.calls == 1


@pytest.mark.parametrize("status", [None, 408, 409, 429, 500, 503])
def test_transient_transport_failures_retry(db, status: int | None) -> None:
    class Retry:
        calls = 0

        def completions(self, **kwargs: object):
            self.calls += 1
            if self.calls == 1:
                error = RuntimeError("transient")
                if status is not None:
                    error.status_code = status  # type: ignore[attr-defined]
                raise error
            return {"data": [], "has_more": False, "next_page": None}

    client = Retry()
    sync(db, OpenAIAdminConfig(enabled=True), client, now=datetime.fromtimestamp(1000, UTC), sleep=lambda _: None)
    assert client.calls == 2


def test_statusless_transport_failure_is_sanitized_when_exhausted(db) -> None:
    class Failure:
        calls = 0

        def completions(self, **kwargs: object):
            self.calls += 1
            raise RuntimeError("connection failed")

    client = Failure()
    with pytest.raises(RuntimeError, match="connection failed"):
        sync(db, OpenAIAdminConfig(enabled=True), client, now=datetime.fromtimestamp(1000, UTC), sleep=lambda _: None)
    assert client.calls == 3


def test_statusless_transport_failure_then_success(db) -> None:
    class Retry:
        calls = 0

        def completions(self, **kwargs: object):
            self.calls += 1
            if self.calls == 1:
                raise OSError("connection failed")
            return {"data": [], "has_more": False, "next_page": None}

    client = Retry()
    sync(db, OpenAIAdminConfig(enabled=True), client, now=datetime.fromtimestamp(1000, UTC), sleep=lambda _: None)
    assert client.calls == 2


def test_restart_resumes_page_chain_and_preserves_window_boundaries(db) -> None:
    class Broken(Pages):
        def completions(self, **kwargs: object):
            if not self.calls:
                self.calls.append(kwargs)
                return {"data": [{"start_time": 100, "end_time": 3700, "results": [result()]}], "has_more": True, "next_page": "page-2"}
            raise RuntimeError("transport stopped")
    with pytest.raises(RuntimeError):
        sync(db, OpenAIAdminConfig(enabled=True), Broken([]), now=datetime.fromtimestamp(7300, UTC))
    assert db.execute("SELECT page_cursor FROM openai_admin_sync_state").fetchone()[0] == "page-2"
    resumed = Pages([{"data": [{"start_time": 3700, "end_time": 7300, "results": [result("gpt-final")]}], "has_more": False, "next_page": None}])
    sync(db, OpenAIAdminConfig(enabled=True), resumed, now=datetime.fromtimestamp(7300, UTC))
    assert resumed.calls[0]["page"] == "page-2"
    assert resumed.calls[0]["end_time"] == 7300


def test_auth_error_state_and_secret_are_not_durable(db, monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "admin-secret-for-test"
    monkeypatch.setenv("OPENAI_ADMIN_KEY", secret)
    class Unauthorized:
        def completions(self, **kwargs: object):
            error = RuntimeError(f"bad authorization {secret}")
            error.status_code = 401  # type: ignore[attr-defined]
            raise error
    with pytest.raises(RuntimeError):
        sync(db, OpenAIAdminConfig(enabled=True), Unauthorized(), now=datetime.fromtimestamp(1000, UTC))
    state = db.execute("SELECT status,last_error FROM openai_admin_sync_state").fetchone()
    assert state[0] == "ADMIN_FAILED"
    assert secret not in (state[1] or "")
    assert db.execute("SELECT count(*) FROM openai_usage_completions").fetchone()[0] == 0
    assert "thread_id" not in AdminCompletionUsage.model_fields


def test_secret_sanitization_covers_headers_and_public_exception(db, monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "admin-secret-header-test"
    monkeypatch.setenv("OPENAI_ADMIN_KEY", secret)
    message = f'bad authorization {secret} Authorization: Bearer {secret}'
    assert secret not in sanitize_admin_error(message)

    class Unauthorized:
        def completions(self, **kwargs: object):
            error = RuntimeError(message)
            error.status_code = 401  # type: ignore[attr-defined]
            raise error

    with pytest.raises(RuntimeError) as raised:
        sync(db, OpenAIAdminConfig(enabled=True), Unauthorized(), now=datetime.fromtimestamp(1000, UTC))
    assert secret not in str(raised.value)
    state = db.execute("SELECT last_error FROM openai_admin_sync_state").fetchone()
    assert secret not in (state[0] or "")
    assert secret not in str(health(db, enabled=True, credential_present=True))


def test_typed_read_api_is_bounded_and_get_only(tmp_path: Path) -> None:
    db = tmp_path / "api.db"
    connection = connect(db)
    migrate(connection)
    connection.close()
    with TestClient(create_app(db)) as client:
        assert client.get("/api/v1/admin/usage/health").status_code == 200
        assert client.get("/api/v1/admin/usage/completions?limit=101").status_code == 422
        assert client.post("/api/v1/admin/usage/completions").status_code == 405


def test_populated_typed_read_api_excludes_internal_columns(tmp_path: Path) -> None:
    db = tmp_path / "populated-api.db"
    connection = connect(db)
    migrate(connection)
    sync(connection, OpenAIAdminConfig(enabled=True), Pages([
        {"data": [{"start_time": 100, "end_time": 3700, "results": [result()]}], "has_more": False, "next_page": None},
    ]), now=datetime.fromtimestamp(3700, UTC))
    connection.close()
    with TestClient(create_app(db)) as client:
        response = client.get("/api/v1/admin/usage/completions")
        health_response = client.get("/api/v1/admin/usage/health")
    assert response.status_code == 200
    assert health_response.status_code == 200
    item = response.json()["items"][0]
    AdminCompletionUsage.model_validate(item)
    assert "is_current" not in item
    assert "result_sha256" not in item
    assert set(item) == set(AdminCompletionUsage.model_fields)


def test_checkpoint_continuity_uses_completed_end_after_long_outage(tmp_path: Path) -> None:
    db = connect(tmp_path / "checkpoint.db")
    migrate(db)
    config = OpenAIAdminConfig(enabled=True, initial_lookback_hours=24, overlap_hours=1)
    first = Pages([{"data": [], "has_more": False, "next_page": None}])
    sync(db, config, first, now=datetime.fromtimestamp(100000, UTC))
    assert first.calls[0]["start_time"] == 100000 - 24 * 3600
    normal = Pages([{"data": [], "has_more": False, "next_page": None}])
    sync(db, config, normal, now=datetime.fromtimestamp(101000, UTC))
    assert normal.calls[0]["start_time"] == 100000 - 3600
    outage = Pages([{"data": [], "has_more": False, "next_page": None}])
    sync(db, config, outage, now=datetime.fromtimestamp(100000 + 48 * 3600, UTC))
    assert outage.calls[0]["start_time"] == 101000 - 3600
    db.close()


def test_checkpoint_zero_overlap_and_restart(tmp_path: Path) -> None:
    path = tmp_path / "checkpoint-restart.db"
    db = connect(path)
    migrate(db)
    config = OpenAIAdminConfig(enabled=True, initial_lookback_hours=24, overlap_hours=0)
    sync(db, config, Pages([{"data": [], "has_more": False, "next_page": None}]), now=datetime.fromtimestamp(2000, UTC))
    db.close()
    reopened = connect(path)
    restarted = Pages([{"data": [], "has_more": False, "next_page": None}])
    sync(reopened, config, restarted, now=datetime.fromtimestamp(10 * 86400, UTC))
    assert restarted.calls[0]["start_time"] == 2000
    reopened.close()


def test_concurrent_sync_fails_fast_without_changing_active_window(tmp_path: Path) -> None:
    path = tmp_path / "concurrent.db"
    first_db = connect(path)
    second_db = connect(path)
    migrate(first_db)
    entered = threading.Event()
    release = threading.Event()

    class Blocking:
        def completions(self, **kwargs: object):
            entered.set()
            release.wait(5)
            return {"data": [], "has_more": False, "next_page": None}

    result_holder: list[dict[str, object]] = []
    worker = threading.Thread(target=lambda: result_holder.append(sync(
        first_db, OpenAIAdminConfig(enabled=True), Blocking(), now=datetime.fromtimestamp(5000, UTC), sleep=lambda _: None)))
    worker.start()
    assert entered.wait(5)
    busy = sync(second_db, OpenAIAdminConfig(enabled=True), Pages([]), now=datetime.fromtimestamp(9000, UTC))
    assert busy["status"] == "ADMIN_BUSY"
    assert tuple(second_db.execute("SELECT requested_start,requested_end FROM openai_admin_sync_state").fetchone()) == (5000 - 24 * 3600, 5000)
    release.set()
    worker.join(5)
    assert result_holder[0]["status"] == "ADMIN_HEALTHY"
    first_db.close()
    second_db.close()
