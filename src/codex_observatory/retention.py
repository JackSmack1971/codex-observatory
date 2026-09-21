"""Phase 7 retention contracts.

This module deliberately contains no retention executor or deletion capability.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Final


class RetentionTableClass(StrEnum):
    """The single retention classification assigned to an application table."""

    PRUNABLE_HISTORY = "PRUNABLE_HISTORY"
    CURRENT_PROJECTION = "CURRENT_PROJECTION"
    CONTROL_STATE = "CONTROL_STATE"
    ARCHIVE_CONTROL = "ARCHIVE_CONTROL"
    AUDIT = "AUDIT"


_TABLE_CLASSIFICATIONS = {
    # Migration bookkeeping is application audit state. sqlite_sequence is an
    # SQLite-internal table and is intentionally not part of this registry.
    "schema_migrations": RetentionTableClass.AUDIT,
    "raw_events": RetentionTableClass.PRUNABLE_HISTORY,
    "events": RetentionTableClass.PRUNABLE_HISTORY,
    "collector_health": RetentionTableClass.CONTROL_STATE,
    "app_server_state": RetentionTableClass.CONTROL_STATE,
    "app_server_messages": RetentionTableClass.PRUNABLE_HISTORY,
    "threads": RetentionTableClass.CURRENT_PROJECTION,
    "turns": RetentionTableClass.CURRENT_PROJECTION,
    "thread_items": RetentionTableClass.CURRENT_PROJECTION,
    "app_server_token_usage": RetentionTableClass.CURRENT_PROJECTION,
    "hook_events": RetentionTableClass.PRUNABLE_HISTORY,
    "hook_source_state": RetentionTableClass.CONTROL_STATE,
    "correlation_edges": RetentionTableClass.PRUNABLE_HISTORY,
    "repositories": RetentionTableClass.CURRENT_PROJECTION,
    "worktrees": RetentionTableClass.CURRENT_PROJECTION,
    "git_snapshots": RetentionTableClass.PRUNABLE_HISTORY,
    "git_snapshot_paths": RetentionTableClass.PRUNABLE_HISTORY,
    "git_snapshot_correlations": RetentionTableClass.PRUNABLE_HISTORY,
    "git_health": RetentionTableClass.CONTROL_STATE,
    "archive_batches": RetentionTableClass.ARCHIVE_CONTROL,
    "archive_files": RetentionTableClass.ARCHIVE_CONTROL,
    "archive_watermarks": RetentionTableClass.ARCHIVE_CONTROL,
    "archive_health": RetentionTableClass.ARCHIVE_CONTROL,
    "analytics_health": RetentionTableClass.CONTROL_STATE,
    "retention_runs": RetentionTableClass.AUDIT,
    "retention_run_tables": RetentionTableClass.AUDIT,
}

# The future planner must consume this immutable, authoritative registry.
TABLE_CLASSIFICATIONS: Final[Mapping[str, RetentionTableClass]] = MappingProxyType(
    _TABLE_CLASSIFICATIONS
)

PRUNABLE_HISTORY_TABLES: Final[frozenset[str]] = frozenset(
    table
    for table, classification in TABLE_CLASSIFICATIONS.items()
    if classification is RetentionTableClass.PRUNABLE_HISTORY
)

# Contractual referential order only; this is not executable deletion logic.
PRUNABLE_HISTORY_ORDER: Final[tuple[str, ...]] = (
    "git_snapshot_correlations",
    "git_snapshot_paths",
    "git_snapshots",
    "correlation_edges",
    "hook_events",
    "app_server_messages",
    "raw_events",
    "events",
)

# Timestamp sources for future planner validation. No SQL is executed here.
PRUNABLE_TIMESTAMP_COLUMNS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "app_server_messages": "received_at",
        "correlation_edges": "created_at",
        "events": "event_time",
        "git_snapshot_correlations": "created_at",
        "git_snapshot_paths": "parent git_snapshots.captured_at",
        "git_snapshots": "captured_at",
        "hook_events": "received_at",
        "raw_events": "received_at",
    }
)


def _require_aware(value: datetime, name: str) -> None:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


@dataclass(frozen=True, slots=True)
class RetentionPolicyV1:
    """Low-level age policy for timestamps already validated by a planner."""

    cutoff: datetime

    def __post_init__(self) -> None:
        _require_aware(self.cutoff, "cutoff")

    def eligible(self, timestamp: datetime) -> bool:
        """Return whether a validated, aware timestamp predates the cutoff."""

        _require_aware(timestamp, "timestamp")
        return timestamp < self.cutoff


@dataclass(frozen=True, slots=True)
class RetentionDiagnosticV1:
    """A planner-visible reason that a row could not be age-classified."""

    table: str
    timestamp_column: str
    row_identity: str
    reason: str


@dataclass(frozen=True, slots=True)
class RetentionPlanV1:
    """Immutable planning result; execution is outside Retention Contract v1."""

    diagnostics: tuple[RetentionDiagnosticV1, ...] = ()


def render_table_classification_markdown() -> str:
    """Render the documentation mirror from the production registry."""

    lines = ["| Table | Classification |", "| --- | --- |"]
    lines.extend(
        f"| `{table}` | `{classification.value}` |"
        for table, classification in TABLE_CLASSIFICATIONS.items()
    )
    return "\n".join(lines)
