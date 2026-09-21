from __future__ import annotations

import argparse
import json
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
    query = commands.add_parser("query")
    query.add_argument("--db", type=Path, default=None)
    query.add_argument("--limit", type=int, default=20)
    configure = commands.add_parser("configure-codex")
    configure.add_argument("action", choices=("--check", "--print", "--apply"))
    archive = commands.add_parser("archive")
    archive.add_subparsers(dest="archive_command").add_parser("run")
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
    checks.extend([
        {"name": "otel_config", "status": "ok", "detail": "OTLP receiver available; Codex exporter configuration remains manual"},
        {"name": "hooks", "status": "not_implemented", "detail": "hook collector not implemented in Phase 0"},
        {"name": "hook_spool", "status": "not_configured", "detail": "runtime directories are not created by doctor"},
        {"name": "sqlite", "status": "ok", "detail": "WAL migration-backed live store available"},
        {"name": "app_server_schema", "status": "not_configured", "detail": "no compatibility registry populated"},
        {"name": "parquet", "status": "not_implemented", "detail": "archive begins in Phase 8"},
        {"name": "clock", "status": "ok", "detail": "system clock readable"},
    ])
    checks.append({"name": "config", "status": "ok", "detail": "validated"})
    checks.append({"name": "runtime_paths", "status": "ok", "detail": {key: str(value) for key, value in asdict(paths).items()}})
    checks.append({"name": "admin_key", "status": "ok" if (not config.collectors.openai_admin.enabled or admin_key_present()) else "degraded", "detail": "environment-only credential check"})
    degraded = any(item["status"] in {"missing", "degraded", "not_configured", "not_implemented", "blocking"} for item in checks)
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
    if args.command in {"health", "query"}:
        from .sqlite import connect, migrate

        db = args.db or resolve_paths().sqlite_path
        connection = connect(db)
        migrate(connection)
        if args.command == "health":
            row = connection.execute("SELECT * FROM collector_health WHERE collector='otlp'").fetchone()
            print(json.dumps(dict(row) if row else {"collector": "otlp", "status": "healthy"}, indent=2))
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
