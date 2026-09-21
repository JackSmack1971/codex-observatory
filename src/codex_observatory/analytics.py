"""Typed, fixed-query DuckDB projection over published archive files."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import duckdb

from .archive import published_paths
from .sqlite import utc_now


class AnalyticsService:
    def __init__(self, connection: Any, archive_root: Path) -> None:
        self.connection = connection
        self.archive_root = archive_root

    def _run(self, name: str, dataset: str, sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
        try:
            paths = published_paths(self.connection, self.archive_root, dataset)
            if not paths:
                rows: list[dict[str, Any]] = []
            else:
                db = duckdb.connect(":memory:")
                try:
                    quoted = ",".join("'" + str(path).replace("'", "''") + "'" for path in paths)
                    result = db.execute(sql.format(paths=f"[{quoted}]"), params or []).fetchall()
                    columns = [item[0] for item in db.description]
                    rows = [dict(zip(columns, row)) for row in result]
                finally:
                    db.close()
            self._health(True, name, None)
            return rows
        except Exception as exc:
            self._health(False, name, str(exc))
            raise

    def _health(self, success: bool, name: str, error: str | None) -> None:
        now = utc_now()
        with self.connection:
            self.connection.execute("""INSERT INTO analytics_health(collector,status,queries_total,query_failures_total,last_query,last_error,updated_at)
            VALUES('duckdb',?,?,?,?,?,?) ON CONFLICT(collector) DO UPDATE SET status=excluded.status,
            queries_total=analytics_health.queries_total+1,query_failures_total=analytics_health.query_failures_total+excluded.query_failures_total,
            last_query=excluded.last_query,last_error=excluded.last_error,updated_at=excluded.updated_at""",
                               ("healthy" if success else "failed", 1, 0 if success else 1, name, error, now))

    def event_count(self, start: str | None = None, end: str | None = None) -> int:
        return len(self.event_identities(start, end))

    def event_identities(self, start: str | None = None, end: str | None = None) -> list[str]:
        """Return the stable, ordered union of hot and archived events.

        SQLite wins on overlap.  This makes an archive-before-prune and the
        corresponding archive-after-prune query logically identical without
        counting the overlap twice.
        """

        where, params = _time_filter("event_time", start, end)
        archived = self._run(
            "event_identities", "events",
            "SELECT event_id,event_time FROM read_parquet({paths}, union_by_name=true, "
            "hive_partitioning=true) WHERE 1=1 " + where,
            params,
        )
        hot_where, hot_params = _sqlite_time_filter("event_time", start, end)
        hot = self.connection.execute(
            "SELECT event_id,event_time FROM events WHERE 1=1 " + hot_where,
            hot_params,
        ).fetchall()
        identities = {row["event_id"]: str(row["event_time"]) for row in archived}
        identities.update({row["event_id"]: row["event_time"] for row in hot})
        return [
            identity for identity, _ in sorted(
                identities.items(), key=lambda item: (item[1], item[0]), reverse=True,
            )
        ]

    def events_by(self, column: str, start: str | None = None, end: str | None = None) -> list[dict[str, Any]]:
        if column not in {"source_class", "category", "repo_id"}:
            raise ValueError("unsupported analytics grouping")
        expression = "json_extract_string(attributes_json, '$.repo_id')" if column == "repo_id" else column
        where, params = _time_filter("event_time", start, end)
        return self._run(f"events_by_{column}", "events", f"SELECT {expression} AS {column}, count(*) AS count FROM read_parquet({{paths}}, union_by_name=true, hive_partitioning=true) WHERE 1=1 {where} GROUP BY 1 ORDER BY 1", params)

    def token_total(self, start: str | None = None, end: str | None = None) -> int:
        where, params = _time_filter("observed_at", start, end)
        archived = self._run(
            "token_total", "token_usage",
            "SELECT thread_id,turn_id,total_tokens,observed_at FROM "
            "read_parquet({paths}, union_by_name=true, hive_partitioning=true) "
            f"WHERE 1=1 {where} QUALIFY row_number() OVER (PARTITION BY thread_id,turn_id "
            "ORDER BY observed_at DESC)=1",
            params,
        )
        totals = {(row["thread_id"], row["turn_id"]): int(row["total_tokens"] or 0) for row in archived}
        hot_where, hot_params = _sqlite_time_filter("observed_at", start, end)
        for row in self.connection.execute(
            "SELECT thread_id,turn_id,usage_json FROM app_server_token_usage WHERE 1=1 "
            + hot_where, hot_params,
        ).fetchall():
            try:
                usage = json.loads(row["usage_json"])
                total = usage.get("total", usage)
                totals[(row["thread_id"], row["turn_id"])] = int(
                    total.get("totalTokens", total.get("total_tokens", 0)) or 0
                )
            except (AttributeError, TypeError, ValueError):
                continue
        return sum(totals.values())

    def git_snapshot_identities(
        self, start: str | None = None, end: str | None = None,
    ) -> list[str]:
        """Return the deduplicated hot/cold Git snapshot identity sequence."""

        where, params = _time_filter("captured_at", start, end)
        archived = self._run(
            "git_snapshot_identities", "git_snapshots",
            "SELECT snapshot_observation_id,captured_at FROM read_parquet({paths}, "
            "union_by_name=true, hive_partitioning=true) WHERE 1=1 " + where,
            params,
        )
        hot_where, hot_params = _sqlite_time_filter("captured_at", start, end)
        hot = self.connection.execute(
            "SELECT snapshot_observation_id,captured_at FROM git_snapshots WHERE 1=1 "
            + hot_where, hot_params,
        ).fetchall()
        identities = {
            row["snapshot_observation_id"]: str(row["captured_at"]) for row in archived
        }
        identities.update({row["snapshot_observation_id"]: row["captured_at"] for row in hot})
        return [
            identity for identity, _ in sorted(
                identities.items(), key=lambda item: (item[1], item[0]), reverse=True,
            )
        ]

    def tool_calls(self, start: str | None = None, end: str | None = None) -> list[dict[str, Any]]:
        where, params = _time_filter("event_time", start, end)
        return self._run("tool_calls", "events", f"SELECT status, count(*) AS count FROM read_parquet({{paths}}, union_by_name=true, hive_partitioning=true) WHERE (category LIKE '%tool%' OR name LIKE '%tool%') {where} GROUP BY status ORDER BY status", params)


def _time_filter(column: str, start: str | None, end: str | None) -> tuple[str, list[str]]:
    clauses: list[str] = []
    params: list[str] = []
    if start:
        clauses.append(f"AND {column} >= ?")
        params.append(start)
    if end:
        clauses.append(f"AND {column} < ?")
        params.append(end)
    return " ".join(clauses), params


def _sqlite_time_filter(
    column: str, start: str | None, end: str | None,
) -> tuple[str, list[str]]:
    where, params = _time_filter(column, start, end)
    return where, params
