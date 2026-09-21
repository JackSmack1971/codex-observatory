import json
from pathlib import Path

from codex_observatory.cli import main


def test_doctor_is_structured(capsys) -> None:
    result = main(["doctor"])
    payload = json.loads(capsys.readouterr().out)
    assert result in (1, 2)
    assert payload["status"] == "degraded"
    assert isinstance(payload["checks"], list)


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
