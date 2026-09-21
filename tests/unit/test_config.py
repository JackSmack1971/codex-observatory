from pathlib import Path

import pytest
from pydantic import ValidationError

from codex_observatory.config import ObservatoryConfig, load_config
from codex_observatory.paths import resolve_paths


def test_default_contract_is_loopback_and_minimal() -> None:
    config = load_config(Path("config/observatory.example.toml"))
    assert config.server.host == "127.0.0.1"
    assert config.privacy.mode == "minimal"
    assert config.privacy.store_prompts is False


def test_non_loopback_host_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ObservatoryConfig.model_validate({"server": {"host": "0.0.0.0"}})


def test_forensic_controls_are_required() -> None:
    with pytest.raises(ValidationError):
        ObservatoryConfig.model_validate({"privacy": {"store_prompts": True}})


def test_runtime_paths_are_absolute() -> None:
    paths = resolve_paths()
    assert all(path.is_absolute() for path in (paths.config_dir, paths.data_dir, paths.cache_dir, paths.log_dir))
