"""Phase 7 retention contracts.

This module deliberately contains no database access or deletion capability.
It only defines the immutable inputs and output shape of a future planner.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("evaluation_time must be timezone-aware")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class RetentionPolicyV1:
    """Deterministic policy input shared by every selection in one run."""

    evaluation_time: datetime
    enabled: bool = False
    hot_days: int = 30
    raw_days: int = 7

    def __post_init__(self) -> None:
        if self.hot_days < 0 or self.raw_days < 0:
            raise ValueError("retention days must be non-negative")
        object.__setattr__(self, "evaluation_time", _utc(self.evaluation_time))

    @property
    def hot_cutoff(self) -> datetime:
        return self.evaluation_time - timedelta(days=self.hot_days)

    @property
    def raw_cutoff(self) -> datetime:
        return self.evaluation_time - timedelta(days=self.raw_days)

    @staticmethod
    def eligible(timestamp: datetime, cutoff: datetime) -> bool:
        """Return true only for timestamps strictly older than the cutoff."""

        return _utc(timestamp) < _utc(cutoff)


@dataclass(frozen=True, slots=True)
class RetentionSelectionV1:
    """A future planner's deterministic selection for one SQLite table."""

    table: str
    retention_class: str
    timestamp_column: str
    cutoff: datetime
    row_identities: tuple[tuple[object, ...], ...]
    required_archive_dataset: str
    covering_archive_batch_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "cutoff", _utc(self.cutoff))


@dataclass(frozen=True, slots=True)
class RetentionPlanV1:
    """In-memory plan shape; creating a plan never authorizes execution."""

    policy: RetentionPolicyV1
    selections: tuple[RetentionSelectionV1, ...]
    deletion_order: tuple[str, ...]
    schema_version: int = 1

