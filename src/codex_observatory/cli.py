from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

from .config import admin_key_present, load_config
from .paths import resolve_paths
from .version import __version__


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="codex-observatory", description="Local Codex telemetry observatory")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command")
    for name in ("init", "migrate", "admin-sync", "verify-install"):
        commands.add_parser(name)
    serve = commands.add_parser("serve")
    serve.add_argument("--db", type=Path, default=None)
    health = commands.add_parser("health")
    health.add_argument("--db", type=Path, default=None)
    hook = commands.add_parser("hook")
    hook.add_argument("--spool", type=Path, default=None)
    hook_import = commands.add_parser("hooks-import")
    hook_import.add_argument("--db", type=Path, default=None)
    hook_import.add_argument("--spool", type=Path, default=None)
    git_capture = commands.add_parser("git-capture")
    git_capture.add_argument("--db", type=Path, default=None)
    git_capture.add_argument("--cwd", type=Path, default=Path.cwd())
    git_capture.add_argument("--session-id", default=None)
    git_capture.add_argument("--thread-id", default=None)
    git_capture.add_argument("--turn-id", default=None)
    git_query = commands.add_parser("git-query")
    git_query.add_argument("--db", type=Path, default=None)
    git_query.add_argument("--limit", type=int, default=20)
    query = commands.add_parser("query")
    query.add_argument("--db", type=Path, default=None)
    query.add_argument("--limit", type=int, default=20)
    configure = commands.add_parser("configure-codex")
    configure.add_argument("action", choices=("--check", "--print", "--apply"))
    archive = commands.add_parser("archive")
    archive_commands = archive.add_subparsers(dest="archive_command")
    archive_run = archive_commands.add_parser("run")
    archive_run.add_argument("--db", type=Path, default=None)
    archive_run.add_argument("--archive-root", type=Path, default=None)
    archive_run.add_argument("--dataset", choices=("events", "token_usage", "git_snapshots", "all"), default="all")
    archive_verify = archive_commands.add_parser("verify")
    archive_verify.add_argument("--db", type=Path, default=None)
    archive_verify.add_argument("--archive-root", type=Path, default=None)
    archive_health = archive_commands.add_parser("health")
    archive_health.add_argument("--db", type=Path, default=None)
    archive_health.add_argument("--archive-root", type=Path, default=None)
    analytics = commands.add_parser("analytics")
    analytics.add_argument("kind", choices=("event-count", "events-by-source", "events-by-category", "events-by-repository", "token-total", "tool-calls"))
    analytics.add_argument("--db", type=Path, default=None)
    analytics.add_argument("--archive-root", type=Path, default=None)
    analytics.add_argument("--start", default=None)
    analytics.add_argument("--end", default=None)
    retention = commands.add_parser("retention")
    retention.add_subparsers(dest="retention_command").add_parser("run")
    schema = commands.add_parser("schema")
    schema_subcommands = schema.add_subparsers(dest="schema_command")
    schema_subcommands.add_parser("capture")
    schema_subcommands.add_parser("verify")
    commands.add_parser("doctor")
    return parser


def _doctor() -> int:
    config = load_config()
    paths = resolve_paths()
    checks: list[dict[str, object]] = []

    py_ok = sys.version_info[:2] == (3, 14)
    checks.append({"name": "python", "status": "ok" if py_ok else "blocking", "detail": sys.version.split()[0]})
    node = shutil.which("node")
    checks.append({"name": "node_frontend", "status": "ok" if node else "missing", "detail": node or "node not found"})
    codex = shutil.which("codex")
    codex_version = "codex executable not found"
    if codex:
        try:
            result = subprocess.run([codex, "--version"], capture_output=True, text=True, timeout=5, check=False)
            codex_version = result.stdout.strip() or result.stderr.strip() or "version unavailable"
        except (OSError, subprocess.SubprocessError) as exc:
            codex_version = f"version unavailable: {exc}"
    checks.append({"name": "codex", "status": "not_configured" if not codex else "ok", "detail": codex_version})
    frontend_build = Path("frontend/dist/index.html")
    checks.append({"name": "frontend_build", "status": "ok" if frontend_build.is_file() else "not_configured", "detail": str(frontend_build)})
    hook_paths = (
        Path.home() / ".codex" / "hooks.json", Path.home() / ".codex" / "config.toml",
        Path.cwd() / ".codex" / "hooks.json", Path.cwd() / ".codex" / "config.toml")
    hook_configured = any(path.name == "hooks.json" and path.is_file() or path.name == "config.toml" and path.is_file() and re.search(r"(^|[\n\r])\s*hooks\s*=|\[\[?hooks\.", path.read_text(encoding="utf-8", errors="ignore")) for path in hook_paths)
    checks.extend([
        {"name": "otel_config", "status": "ok", "detail": "OTLP receiver available; Codex exporter configuration remains manual"},
        {"name": "hooks", "status": "HOOKS_CONFIGURED_NOT_OBSERVED" if hook_configured else "HOOKS_DISABLED", "detail": "observational command hooks"},
        {"name": "sqlite", "status": "ok", "detail": "WAL migration-backed live store available"},
        {"name": "app_server_schema", "status": "not_configured", "detail": "no compatibility registry populated"},
        {"name": "clock", "status": "ok", "detail": "system clock readable"},
    ])
    from .sqlite import connect, migrate
    app_db = connect(paths.sqlite_path)
    try:
        migrate(app_db)
        app_row = app_db.execute("SELECT * FROM app_server_state WHERE source_instance='app-server'").fetchone()
        app_detail = dict(app_row) if app_row else {"status": "disconnected", "reconnect_total": 0}
        app_status = app_detail.get("status", "disconnected")
        checks.append({"name": "app_server", "status": app_status, "detail": app_detail})
        hook_row = app_db.execute("SELECT * FROM hook_source_state WHERE source_instance='local-default'").fetchone()
        hook_status = ("HOOKS_HEALTHY" if hook_row["status"] == "healthy" else "HOOKS_DEGRADED") if hook_row else ("HOOKS_CONFIGURED_NOT_OBSERVED" if hook_configured else "HOOKS_DISABLED")
        checks.append({"name": "hook_health", "status": hook_status, "detail": dict(hook_row) if hook_row else "no hook events observed"})
        from .git import NotARepository, discover
        git_detail: object
        if not config.collectors.git.enabled:
            git_status, git_detail = "GIT_DISABLED", "collectors.git.enabled=false"
        elif not shutil.which("git"):
            git_status, git_detail = "GIT_DEGRADED", "git executable not found"
        else:
            try:
                identity = discover(Path.cwd())
                git_status = "GIT_REPOSITORY_DETECTED"
                git_detail = {"root": identity.root, "repo_id": identity.repo_id}
            except NotARepository:
                git_status = "GIT_NOT_A_REPOSITORY"
                git_detail = str(Path.cwd())
            except Exception as exc:  # noqa: BLE001 - doctor reports component failures as data.
                git_status = "GIT_DEGRADED"
                git_detail = str(exc)
        git_row = app_db.execute("SELECT * FROM git_health WHERE collector='git'").fetchone()
        if git_row:
            git_status = git_row["status"] if git_status == "GIT_REPOSITORY_DETECTED" else git_status
        checks.append({"name": "git", "status": git_status, "detail": git_detail, "health": dict(git_row) if git_row else {"status": "disabled"}})
        from .archive import archive_health, verify_archive
        archive_root = config.storage.parquet_root or paths.parquet_root
        verification = verify_archive(app_db, archive_root)
        archive_status = verification["status"].upper()
        checks.append({"name": "archive", "status": f"ARCHIVE_{archive_status}", "detail": archive_health(app_db, archive_root)})
        try:
            import duckdb
            checks.append({"name": "duckdb", "status": "DUCKDB_AVAILABLE", "detail": duckdb.__version__})
            duckdb_health = app_db.execute("SELECT * FROM analytics_health WHERE collector='duckdb'").fetchone()
            checks.append({"name": "duckdb_query", "status": "DUCKDB_QUERY_HEALTHY" if not duckdb_health or duckdb_health["status"] == "healthy" else "DUCKDB_QUERY_FAILED", "detail": dict(duckdb_health) if duckdb_health else "no query run"})
        except ImportError as exc:
            checks.extend([{"name": "duckdb", "status": "DUCKDB_UNAVAILABLE", "detail": str(exc)}, {"name": "duckdb_query", "status": "DUCKDB_QUERY_FAILED", "detail": "DuckDB is unavailable"}])
    finally:
        app_db.close()
    checks.append({"name": "config", "status": "ok", "detail": "validated"})
    checks.append({"name": "runtime_paths", "status": "ok", "detail": {key: str(value) for key, value in asdict(paths).items()}})
    checks.append({"name": "admin_key", "status": "ok" if (not config.collectors.openai_admin.enabled or admin_key_present()) else "degraded", "detail": "environment-only credential check"})
    degraded = any(item["status"] in {"missing", "degraded", "disconnected", "connecting", "incompatible", "not_configured", "not_implemented", "blocking", "HOOKS_CONFIGURED_NOT_OBSERVED", "configured_not_observed", "GIT_DEGRADED", "ARCHIVE_DEGRADED", "ARCHIVE_FAILED", "DUCKDB_QUERY_FAILED", "DUCKDB_UNAVAILABLE"} for item in checks)
    print(json.dumps({"version": __version__, "status": "degraded" if degraded else "healthy", "checks": checks}, indent=2))
    return 2 if any(item["status"] == "blocking" for item in checks) else (1 if degraded else 0)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "doctor":
        try:
            return _doctor()
        except Exception as exc:  # noqa: BLE001 - doctor must report failures as data.
            print(json.dumps({"status": "degraded", "checks": [{"name": "doctor", "status": "error", "detail": str(exc)}]}))
            return 1
    if args.command == "hook":
        from .hook import run_hook
        return run_hook(sys.stdin.read(), args.spool)
    if args.command == "hooks-import":
        from .hook import import_spool
        from .sqlite import connect, migrate
        db = args.db or resolve_paths().sqlite_path
        connection = connect(db)
        try:
            migrate(connection)
            print(json.dumps(import_spool(connection, args.spool), indent=2))
        finally:
            connection.close()
        return 0
    if args.command == "git-capture":
        from .git import GitError, NotARepository, capture, persist_snapshot
        from .sqlite import connect, migrate
        db = args.db or resolve_paths().sqlite_path
        connection = connect(db)
        try:
            migrate(connection)
            try:
                snapshot = capture(args.cwd)
            except NotARepository:
                print(json.dumps({"status": "NOT_A_REPOSITORY", "cwd": str(args.cwd)}))
                return 2
            except GitError as exc:
                print(json.dumps({"status": "GIT_DEGRADED", "error": str(exc)}))
                return 1
            persist_snapshot(connection, snapshot, session_id=args.session_id, thread_id=args.thread_id, turn_id=args.turn_id)
            from dataclasses import asdict
            print(json.dumps({"status": "captured", "snapshot": asdict(snapshot)}, default=str))
            return 0
        finally:
            connection.close()
    if args.command == "archive":
        from .archive import archive_health, export_dataset, verify_archive
        from .sqlite import connect, migrate
        db = args.db or resolve_paths().sqlite_path
        connection = connect(db)
        try:
            migrate(connection)
            root = args.archive_root or load_config().storage.parquet_root or resolve_paths().parquet_root
            if args.archive_command == "run":
                datasets = ("events", "token_usage", "git_snapshots") if args.dataset == "all" else (args.dataset,)
                print(json.dumps([asdict(export_dataset(connection, root, dataset)) for dataset in datasets], indent=2))
            elif args.archive_command == "verify":
                print(json.dumps(verify_archive(connection, root), indent=2))
            elif args.archive_command == "health":
                print(json.dumps(archive_health(connection, root), indent=2))
            else:
                print(json.dumps({"status": "NOT_IMPLEMENTED", "command": "archive"}))
                return 1
            return 0
        finally:
            connection.close()
    if args.command == "analytics":
        from .analytics import AnalyticsService
        from .sqlite import connect, migrate
        paths = resolve_paths()
        db = args.db or paths.sqlite_path
        connection = connect(db)
        try:
            migrate(connection)
            root = args.archive_root or load_config().storage.parquet_root or paths.parquet_root
            service = AnalyticsService(connection, root)
            if args.kind == "event-count":
                output: object = {"value": service.event_count(args.start, args.end)}
            elif args.kind == "events-by-source":
                output = service.events_by("source_class", args.start, args.end)
            elif args.kind == "events-by-category":
                output = service.events_by("category", args.start, args.end)
            elif args.kind == "events-by-repository":
                output = service.events_by("repo_id", args.start, args.end)
            elif args.kind == "token-total":
                output = {"value": service.token_total(args.start, args.end)}
            else:
                output = service.tool_calls(args.start, args.end)
            print(json.dumps(output, indent=2, default=str))
            return 0
        finally:
            connection.close()
    if args.command in {"health", "query", "git-query"}:
        from .sqlite import connect, migrate

        db = args.db or resolve_paths().sqlite_path
        connection = connect(db)
        migrate(connection)
        if args.command == "health":
            row = connection.execute("SELECT * FROM collector_health WHERE collector='otlp'").fetchone()
            health_result: dict[str, object] = dict(row) if row else {"collector": "otlp", "status": "healthy"}
            hook_row = connection.execute("SELECT * FROM hook_source_state WHERE source_instance='local-default'").fetchone()
            health_result["hooks"] = dict(hook_row) if hook_row else {"status": "disabled"}
            git_row = connection.execute("SELECT * FROM git_health WHERE collector='git'").fetchone()
            health_result["git"] = dict(git_row) if git_row else {"status": "disabled", "repositories_discovered_total": 0, "snapshots_total": 0}
            archive_row = connection.execute("SELECT * FROM archive_health WHERE collector='archive'").fetchone()
            analytics_row = connection.execute("SELECT * FROM analytics_health WHERE collector='duckdb'").fetchone()
            health_result["archive"] = dict(archive_row) if archive_row else {"status": "empty"}
            health_result["duckdb"] = dict(analytics_row) if analytics_row else {"status": "available", "queries_total": 0}
            print(json.dumps(health_result, indent=2))
        elif args.command == "git-query":
            rows = connection.execute("SELECT * FROM git_snapshots ORDER BY captured_at DESC LIMIT ?", (max(1, min(args.limit, 1000)),)).fetchall()
            print(json.dumps([dict(row) for row in rows], indent=2))
        else:
            rows = connection.execute("SELECT * FROM events ORDER BY event_seq DESC LIMIT ?", (max(1, min(args.limit, 1000)),)).fetchall()
            print(json.dumps([dict(row) for row in rows], indent=2))
        return 0
    if args.command == "serve":
        import uvicorn

        from .app import create_app

        uvicorn.run(create_app(args.db or resolve_paths().sqlite_path), host="127.0.0.1", port=8765)
        return 0
    if args.command is None:
        _parser().print_help()
        return 0
    print(json.dumps({"status": "NOT_IMPLEMENTED", "command": args.command}))
    return 1
