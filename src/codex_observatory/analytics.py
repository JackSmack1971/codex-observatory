"""Typed, fixed-query DuckDB projection over published archive files."""

from __future__ import annotations

import json
from datetime import UTC, datetime
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

    def _event_population(
        self, query_name: str, start: str | None = None, end: str | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Return canonical event rows from cold and hot storage, with hot winning."""

        columns = "event_id,event_time,source_class,category,name,status,attributes_json"
        where, params = _time_filter("event_time", start, end)
        archived = self._run(
            query_name, "events",
            f"SELECT {columns} FROM read_parquet({{paths}}, union_by_name=true, "
            "hive_partitioning=true) WHERE 1=1 " + where,
            params,
        )
        hot_where, hot_params = _sqlite_time_filter("event_time", start, end)
        hot = self.connection.execute(
            f"SELECT {columns} FROM events WHERE 1=1 " + hot_where,
            hot_params,
        ).fetchall()
        population = {row["event_id"]: dict(row) for row in archived}
        population.update({row["event_id"]: dict(row) for row in hot})
        return population

    def event_identities(self, start: str | None = None, end: str | None = None) -> list[str]:
        """Return the stable, ordered union of hot and archived events.

        SQLite wins on overlap.  This makes an archive-before-prune and the
        corresponding archive-after-prune query logically identical without
        counting the overlap twice.
        """

        population = self._event_population("event_identities", start, end)
        return [
            identity for identity, row in sorted(
                population.items(),
                key=lambda item: (_timestamp_sort_key(item[1]["event_time"]), item[0]),
                reverse=True,
            )
        ]

    def events_by(self, column: str, start: str | None = None, end: str | None = None) -> list[dict[str, Any]]:
        if column not in {"source_class", "category", "repo_id"}:
            raise ValueError("unsupported analytics grouping")
        value_sql = (
            "CAST(json_extract(attributes_json, '$.repo_id') AS VARCHAR)"
            if column == "repo_id" else column
        )
        counts = self._grouped_event_counts(
            f"events_by_{column}", value_sql, start, end,
            normalize=_normalize_repo_id if column == "repo_id" else None,
        )
        return [
            {column: value, "count": count}
            for value, count in sorted(
                counts.items(), key=lambda item: (item[0] is None, str(item[0])),
            )
        ]

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
            row["snapshot_observation_id"]: row["captured_at"] for row in archived
        }
        identities.update({row["snapshot_observation_id"]: row["captured_at"] for row in hot})
        return [
            identity for identity, _ in sorted(
                identities.items(),
                key=lambda item: (_timestamp_sort_key(item[1]), item[0]),
                reverse=True,
            )
        ]

    def tool_calls(self, start: str | None = None, end: str | None = None) -> list[dict[str, Any]]:
        counts = self._grouped_event_counts(
            "tool_calls", "status", start, end,
            predicate="(contains(lower(category), 'tool') OR contains(lower(name), 'tool'))",
        )
        return [
            {"status": status, "count": count}
            for status, count in sorted(
                counts.items(), key=lambda item: (item[0] is None, str(item[0])),
            )
        ]

    def _grouped_event_counts(
        self,
        query_name: str,
        value_sql: str,
        start: str | None,
        end: str | None,
        *,
        predicate: str = "TRUE",
        normalize: Any | None = None,
    ) -> dict[Any, int]:
        """Aggregate canonical events without transferring cold event rows to Python."""

        columns = "event_id,event_seq,event_time,source_class,category,name,status,attributes_json"
        hot_where, hot_params = _sqlite_time_filter("event_time", start, end)
        hot = self.connection.execute(
            f"SELECT {columns.replace(',event_seq', '')} FROM events WHERE 1=1 " + hot_where,
            hot_params,
        ).fetchall()
        counts: dict[Any, int] = {}

        def add(value: Any, count: int = 1) -> None:
            key = normalize(value) if normalize else value
            counts[key] = counts.get(key, 0) + count

        for row in hot:
            if predicate != "TRUE" and not _is_tool_event(row["category"], row["name"]):
                continue
            add(_repo_id_json(row["attributes_json"]) if normalize else row[value_sql], 1)

        try:
            paths = published_paths(self.connection, self.archive_root, "events")
            if paths:
                db = duckdb.connect(":memory:")
                try:
                    db.execute("CREATE TABLE hot_event_ids(event_id VARCHAR PRIMARY KEY)")
                    if hot:
                        db.executemany(
                            "INSERT INTO hot_event_ids VALUES (?)",
                            [(row["event_id"],) for row in hot],
                        )
                    quoted = ",".join("'" + str(path).replace("'", "''") + "'" for path in paths)
                    where, params = _time_filter("event_time", start, end)
                    rows = db.execute(
                        f"""WITH cold AS (
                            SELECT {columns} FROM read_parquet([{quoted}], union_by_name=true,
                                hive_partitioning=true) WHERE 1=1 {where}
                            QUALIFY row_number() OVER (PARTITION BY event_id ORDER BY event_seq DESC)=1
                        )
                        SELECT {value_sql} AS grouping_value, count(*) AS count
                        FROM cold ANTI JOIN hot_event_ids USING (event_id)
                        WHERE {predicate} GROUP BY grouping_value""",
                        params,
                    ).fetchall()
                    for value, count in rows:
                        add(value, int(count))
                finally:
                    db.close()
            self._health(True, query_name, None)
        except Exception as exc:
            self._health(False, query_name, str(exc))
            raise
        return counts


def _time_filter(column: str, start: str | None, end: str | None) -> tuple[str, list[str]]:
    clauses: list[str] = []
    params: list[str] = []
    if start:
        clauses.append(f"AND {column} >= ?")
        params.append(_canonical_timestamp(start))
    if end:
        clauses.append(f"AND {column} < ?")
        params.append(_canonical_timestamp(end))
    return " ".join(clauses), params


def _canonical_timestamp(value: str) -> str:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return value
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _timestamp_sort_key(value: str | datetime) -> datetime:
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _sqlite_time_filter(
    column: str, start: str | None, end: str | None,
) -> tuple[str, list[str]]:
    where, params = _time_filter(column, start, end)
    return where, params


def _repo_id_json(attributes_json: Any) -> str | None:
    try:
        attributes = json.loads(attributes_json)
        return json.dumps(
            attributes.get("repo_id"), separators=(",", ":"), ensure_ascii=False,
        )
    except (AttributeError, TypeError, ValueError):
        return None


def _normalize_repo_id(value_json: Any) -> str | None:
    """Return a hashable, deterministic representation of a JSON repo id."""

    try:
        value = json.loads(value_json)
    except (TypeError, ValueError):
        return None
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _is_tool_event(category: str, name: str) -> bool:
    return "tool" in category.lower() or "tool" in name.lower()
