import json

from codex_observatory.cli import main


def test_doctor_is_structured(capsys) -> None:
    result = main(["doctor"])
    payload = json.loads(capsys.readouterr().out)
    assert result in (1, 2)
    assert payload["status"] == "degraded"
    assert isinstance(payload["checks"], list)


def test_unimplemented_commands_are_explicit(capsys) -> None:
    assert main(["serve"]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "NOT_IMPLEMENTED"
