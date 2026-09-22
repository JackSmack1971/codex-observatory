import json
import sqlite3
from pathlib import Path

from codex_observatory.cli import DEGRADED_DOCTOR_STATUSES, main
from codex_observatory.config import ObservatoryConfig


def test_doctor_is_structured(capsys) -> None:
    result = main(["doctor"])
    payload = json.loads(capsys.readouterr().out)
    assert result in (1, 2)
    assert payload["status"] == "degraded"
    assert isinstance(payload["checks"], list)


def test_retention_failures_degrade_doctor() -> None:
    assert {"RETENTION_BLOCKED", "RETENTION_DEGRADED", "RETENTION_FAILED"} <= (
        DEGRADED_DOCTOR_STATUSES
    )
    assert {"RETENTION_DISABLED", "RETENTION_READY"}.isdisjoint(
        DEGRADED_DOCTOR_STATUSES
    )


def test_health_command_is_machine_readable(tmp_path, capsys) -> None:
    assert main(["health", "--db", str(tmp_path / "observatory.db")]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "healthy"


def test_retention_plan_command_is_machine_readable(tmp_path, capsys) -> None:
    assert main(["retention", "plan", "--db", str(tmp_path / "observatory.db")]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "VERIFIED"
    assert payload["archive_coverage_status"] == "COVERED"
    assert {"eligible", "covered", "uncovered", "would_delete"} <= payload.keys()
    assert payload["planned_deletion_count"] == 0
    assert set(payload["candidates_by_table"]) == {
        "git_snapshot_correlations", "git_snapshot_paths", "git_snapshots", "correlation_edges",
        "hook_events", "app_server_messages", "raw_events", "events",
    }


def test_retention_plan_accepts_documented_config(tmp_path, capsys) -> None:
    assert main([
        "retention", "plan",
        "--db", str(tmp_path / "observatory.db"),
        "--config", str(Path("config/retention.example.toml")),
    ]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "VERIFIED"
    assert payload["cutoffs"]


def test_retention_run_command_is_machine_readable(tmp_path, capsys) -> None:
    assert main([
        "retention", "run", "--db", str(tmp_path / "observatory.db"),
        "--archive-root", str(tmp_path / "archive"),
    ]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "COMPLETED"
    assert payload["deleted_count"] == 0
    assert payload["deleted_by_table"]["events"] == 0


def test_retention_run_operational_failure_is_machine_readable(monkeypatch, capsys) -> None:
    import codex_observatory.sqlite as sqlite_module

    def unavailable(_path):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(sqlite_module, "connect", unavailable)
    assert main(["retention", "run"]) == 2
    assert json.loads(capsys.readouterr().out) == {
        "status": "FAILED", "error": "database is locked",
    }


def test_admin_cli_secret_canary_has_no_stdout_or_stderr_leak(tmp_path, monkeypatch, capsys) -> None:
    import codex_observatory.openai_admin as admin

    secret = "cli-admin-secret"
    config = ObservatoryConfig()
    config.storage.sqlite_path = tmp_path / "admin.db"
    config.collectors.openai_admin.enabled = True
    monkeypatch.setenv("OPENAI_ADMIN_KEY", secret)
    monkeypatch.setattr("codex_observatory.cli.load_config", lambda: config)

    class Unauthorized:
        def completions(self, **kwargs: object):
            error = RuntimeError(f"Authorization: Bearer {secret}")
            error.status_code = 401  # type: ignore[attr-defined]
            raise error

    monkeypatch.setattr(admin, "create_client", lambda *, enabled: Unauthorized())
    assert main(["admin-sync"]) == 1
    captured = capsys.readouterr()
    assert secret not in captured.out
    assert secret not in captured.err
