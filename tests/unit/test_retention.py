from datetime import UTC, datetime, timedelta

import pytest

from codex_observatory.retention import RetentionPlanV1, RetentionPolicyV1
from codex_observatory.sqlite import connect, migrate


def test_policy_uses_one_utc_evaluation_time_and_strict_cutoffs() -> None:
    evaluation = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    policy = RetentionPolicyV1(evaluation_time=evaluation)

    assert policy.enabled is False
    assert policy.hot_days == 30
    assert policy.raw_days == 7
    assert policy.hot_cutoff == evaluation - timedelta(days=30)
    assert not policy.eligible(policy.hot_cutoff, policy.hot_cutoff)
    assert policy.eligible(policy.hot_cutoff - timedelta(microseconds=1), policy.hot_cutoff)


def test_policy_rejects_ambiguous_or_negative_inputs() -> None:
    naive = datetime(2026, 9, 21, tzinfo=UTC).replace(tzinfo=None)
    with pytest.raises(ValueError, match="timezone-aware"):
        RetentionPolicyV1(evaluation_time=naive)
    with pytest.raises(ValueError, match="non-negative"):
        RetentionPolicyV1(evaluation_time=datetime.now(UTC), hot_days=-1)


def test_plan_is_contract_only_and_defaults_to_v1() -> None:
    policy = RetentionPolicyV1(evaluation_time=datetime.now(UTC))
    plan = RetentionPlanV1(policy=policy, selections=(), deletion_order=())
    assert plan.schema_version == 1


def test_documented_table_inventory_matches_all_five_migrations(tmp_path) -> None:
    connection = connect(tmp_path / "inventory.sqlite3")
    migrate(connection)
    actual = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    documented = {
        "schema_migrations", "raw_events", "events", "collector_health",
        "app_server_state", "app_server_messages", "threads", "turns",
        "thread_items", "app_server_token_usage", "hook_events",
        "hook_source_state", "correlation_edges", "repositories", "worktrees",
        "git_snapshots", "git_snapshot_paths", "git_snapshot_correlations",
        "git_health", "archive_batches", "archive_files", "archive_watermarks",
        "archive_health", "analytics_health",
    }
    assert actual == documented
    assert [row[0] for row in connection.execute(
        "SELECT version FROM schema_migrations ORDER BY version"
    )] == [1, 2, 3, 4, 5]
    connection.close()
