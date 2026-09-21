from dataclasses import dataclass
from pathlib import Path

from platformdirs import user_cache_dir, user_config_dir, user_data_dir, user_log_dir


@dataclass(frozen=True, slots=True)
class RuntimePaths:
    config_dir: Path
    data_dir: Path
    cache_dir: Path
    log_dir: Path

    @property
    def sqlite_path(self) -> Path:
        return self.data_dir / "observatory.db"

    @property
    def spool_root(self) -> Path:
        return self.data_dir / "spool"

    @property
    def parquet_root(self) -> Path:
        return self.data_dir / "parquet"

    @property
    def manifests_root(self) -> Path:
        return self.data_dir / "manifests"


def resolve_paths() -> RuntimePaths:
    return RuntimePaths(
        config_dir=Path(user_config_dir("codex-observatory")).resolve(),
        data_dir=Path(user_data_dir("codex-observatory")).resolve(),
        cache_dir=Path(user_cache_dir("codex-observatory")).resolve(),
        log_dir=Path(user_log_dir("codex-observatory")).resolve(),
    )
