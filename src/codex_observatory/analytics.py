"""Typed, fixed-query DuckDB projection over published archive files."""

from __future__ import annotations

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
        where, params = _time_filter("event_time", start, end)
        return int(self._run("event_count", "events", "SELECT count(*) AS value FROM read_parquet({paths}, union_by_name=true, hive_partitioning=true) WHERE 1=1 " + where, params)[0]["value"] if published_paths(self.connection, self.archive_root, "events") else 0)

    def events_by(self, column: str, start: str | None = None, end: str | None = None) -> list[dict[str, Any]]:
        if column not in {"source_class", "category", "repo_id"}:
            raise ValueError("unsupported analytics grouping")
        expression = "json_extract_string(attributes_json, '$.repo_id')" if column == "repo_id" else column
        where, params = _time_filter("event_time", start, end)
        return self._run(f"events_by_{column}", "events", f"SELECT {expression} AS {column}, count(*) AS count FROM read_parquet({{paths}}, union_by_name=true, hive_partitioning=true) WHERE 1=1 {where} GROUP BY 1 ORDER BY 1", params)

    def token_total(self, start: str | None = None, end: str | None = None) -> int:
        where, params = _time_filter("observed_at", start, end)
        rows = self._run("token_total", "token_usage", f"SELECT coalesce(sum(total_tokens),0) AS value FROM read_parquet({{paths}}, union_by_name=true, hive_partitioning=true) WHERE 1=1 {where}", params)
        return int(rows[0]["value"] if rows else 0)

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
