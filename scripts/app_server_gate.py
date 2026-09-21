"""Safe, read-only local Codex App Server integration gate for Phase 2."""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from codex_observatory.app_server import (
    AppServerClient,
    JsonlTransport,
    reconcile,
    set_app_server_health,
)
from codex_observatory.sqlite import connect, migrate


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--codex", default="codex")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="codex-observatory-app-server-") as directory:
        db_path = Path(directory) / "gate.db"
        connection = connect(db_path)
        migrate(connection)
        transport = JsonlTransport([args.codex, "app-server", "--listen", "stdio://"])
        client = AppServerClient(transport)
        try:
            reconcile(connection, client)
            set_app_server_health(connection, "healthy")
            threads = connection.execute("SELECT count(*) FROM threads").fetchone()[0]
            loaded = connection.execute("SELECT count(*) FROM threads WHERE loaded=1").fetchone()[0]
            print(json.dumps({
                "codex": args.codex,
                "initialize": True,
                "server": dict(connection.execute("SELECT server_version,capabilities_json FROM app_server_state").fetchone()),
                "stored_threads": threads,
                "loaded_threads": loaded,
                "turn_lifecycle_observed": bool(connection.execute("SELECT count(*) FROM turns").fetchone()[0]),
                "item_lifecycle_observed": bool(connection.execute("SELECT count(*) FROM thread_items").fetchone()[0]),
                "mutating_methods_issued": False,
                "status": "healthy",
            }, indent=2))
            return 0
        except Exception as exc:  # noqa: BLE001 - gate emits structured failure evidence.
            set_app_server_health(connection, "degraded", error=str(exc))
            print(json.dumps({"initialize": False, "status": "degraded", "error": str(exc)}, indent=2))
            return 1
        finally:
            transport.close()
            connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
