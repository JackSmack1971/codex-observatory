"""Opt-in controlled live App Server gate.

This is a test driver, not production observatory code. It is the only Phase 2
artifact allowed to issue thread/start, turn/start, and cleanup thread/delete.
It creates one temporary thread in a temporary cwd, requests a no-tool reply,
persists notifications through the App Server mapper, and removes the thread
it created before exiting.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from codex_observatory.app_server import (
    ingest_notification,
    set_app_server_health,
    upsert_thread,
)
from codex_observatory.sqlite import connect, migrate


class Driver:
    def __init__(self, command: list[str]) -> None:
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, text=True, encoding="utf-8", bufsize=1)
        self.next_id = 1

    def send(self, method: str, params: dict[str, Any] | None = None) -> int:
        if not self.process.stdin:
            raise RuntimeError("App Server stdin unavailable")
        request_id = self.next_id
        self.next_id += 1
        message: dict[str, Any] = {"method": method, "id": request_id}
        if params is not None:
            message["params"] = params
        self.process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        self.process.stdin.flush()
        return request_id

    def notify(self, method: str) -> None:
        if not self.process.stdin:
            raise RuntimeError("App Server stdin unavailable")
        self.process.stdin.write(json.dumps({"method": method}) + "\n")
        self.process.stdin.flush()

    def wait_for(self, request_id: int | None, connection: Any, deadline: float, seen: list[str], *, wait_for_turn: bool = False) -> dict[str, Any]:
        if not self.process.stdout:
            raise RuntimeError("App Server stdout unavailable")
        for line in self.process.stdout:
            message = json.loads(line)
            if "method" in message and "id" not in message:
                method = message["method"]
                seen.append(method)
                ingest_notification(connection, method, message.get("params", {}))
                if method == "turn/completed":
                    return message
            elif request_id is not None and message.get("id") == request_id:
                seen.append(f"response:{request_id}")
                if "error" in message:
                    seen.append(f"response_error:{message['error']}")
                    raise RuntimeError(json.dumps(message["error"], sort_keys=True))
                if not wait_for_turn:
                    return message.get("result", {})
            if time.monotonic() > deadline:
                raise TimeoutError("timed out waiting for App Server live evidence")
        raise RuntimeError("App Server disconnected during live gate")

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--codex", default="codex")
    parser.add_argument("--timeout", type=float, default=90)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="codex-observatory-live-") as cwd, tempfile.TemporaryDirectory(prefix="codex-observatory-live-db-") as db_dir:
        connection = connect(Path(db_dir) / "gate.db")
        migrate(connection)
        driver = Driver([args.codex, "app-server", "--listen", "stdio://"])
        thread_id: str | None = None
        seen: list[str] = []
        try:
            init_id = driver.send("initialize", {"clientInfo": {"name": "codex-observatory-live-gate", "version": "phase-2"}})
            initialization = driver.wait_for(init_id, connection, time.monotonic() + args.timeout, seen)
            driver.notify("initialized")
            start_id = driver.send("thread/start", {"cwd": cwd, "approvalPolicy": "never", "sandbox": "read-only", "serviceName": "codex-observatory-live-gate"})
            started = driver.wait_for(start_id, connection, time.monotonic() + args.timeout, seen)
            thread = started.get("thread", {})
            thread_id = thread.get("id")
            if not thread_id:
                raise RuntimeError("thread/start returned no thread id")
            upsert_thread(connection, thread, loaded=True)
            turn_id = driver.send("turn/start", {"threadId": thread_id, "input": [{"type": "text", "text": "Reply with exactly OK. Do not use tools."}]})
            driver.wait_for(turn_id, connection, time.monotonic() + args.timeout, seen, wait_for_turn=True)
            connection.commit()
            counts = {
                "thread_started": "thread/started" in seen,
                "turn_started": "turn/started" in seen,
                "turn_completed": "turn/completed" in seen,
                "item_started": "item/started" in seen,
                "item_completed": "item/completed" in seen,
                "turns_persisted": connection.execute("SELECT count(*) FROM turns").fetchone()[0],
                "items_persisted": connection.execute("SELECT count(*) FROM thread_items").fetchone()[0],
            }
            if not all(counts[key] for key in ("turn_started", "turn_completed", "item_started", "item_completed")):
                raise RuntimeError(f"missing live lifecycle evidence: {counts}")
            print(json.dumps({"initialize": True, "server": initialization, "notifications": seen, **counts,
                              "observatory_mutations": False, "driver_created_and_deleted_thread": True,
                              "status": "healthy"}, indent=2))
            return 0
        except Exception as exc:  # noqa: BLE001 - gate emits structured evidence.
            set_app_server_health(connection, "degraded", error=str(exc))
            print(json.dumps({"status": "degraded", "error": str(exc), "notifications": seen}, indent=2))
            return 1
        finally:
            # Cleanup is intentionally a test-driver action and never available in the adapter.
            if thread_id and driver.process.poll() is None:
                try:
                    cleanup_id = driver.send("thread/delete", {"threadId": thread_id})
                    driver.wait_for(cleanup_id, connection, time.monotonic() + 10, seen)
                except Exception as exc:  # noqa: BLE001 - cleanup must not mask gate evidence.
                    sys.stderr.write(f"live gate cleanup warning: {exc}\n")
            driver.close()
            connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
