import json

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
