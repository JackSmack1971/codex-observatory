from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from codex_observatory.git import (
    GitError,
    NotARepository,
    capture,
    discover,
    normalize_remote,
    persist_snapshot,
)
from codex_observatory.sqlite import connect, migrate


def git(path: Path, *args: str) -> str:
    result = subprocess.run(("git", *args), cwd=path, capture_output=True, text=True, check=True)
    return result.stdout


def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init")
    git(path, "config", "user.email", "test@example.com")
    git(path, "config", "user.name", "Test")
    (path / "tracked.txt").write_text("one\ntwo\n", encoding="utf-8")
    git(path, "add", "tracked.txt")
    git(path, "commit", "-m", "initial")
    return path


def test_clean_nested_identity_and_deterministic_digest(tmp_path: Path) -> None:
    path = repo(tmp_path)
    nested = path / "nested"
    nested.mkdir()
    first = capture(nested)
    second = capture(path)
    assert first.clean and first.head_state == "attached"
    assert first.root == str(path.resolve())
    assert first.worktree_id == second.worktree_id
    assert first.snapshot_content_digest == second.snapshot_content_digest
    assert first.snapshot_observation_id != second.snapshot_observation_id


def test_status_areas_and_diff_statistics(tmp_path: Path) -> None:
    path = repo(tmp_path)
    (path / "tracked.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
    staged = capture(path)
    assert staged.unstaged_count == 1 and staged.paths[0].area == "unstaged"
    git(path, "add", "tracked.txt")
    (path / "tracked.txt").write_text("one\ntwo\nthree\nfour\n", encoding="utf-8")
    (path / "new.txt").write_text("new\n", encoding="utf-8")
    mixed = capture(path)
    assert mixed.staged_count == 1 and mixed.unstaged_count == 1 and mixed.untracked_count == 1
    assert {item.area for item in mixed.paths} == {"staged+unstaged", "untracked"}
    assert mixed.insertions >= 2
    git(path, "add", "new.txt")
    assert capture(path).staged_count == 2


def test_deleted_renamed_binary_and_digest_change(tmp_path: Path) -> None:
    path = repo(tmp_path)
    (path / "binary.bin").write_bytes(b"\x00\x01")
    git(path, "add", "binary.bin")
    git(path, "commit", "-m", "binary")
    git(path, "mv", "tracked.txt", "renamed.txt")
    (path / "binary.bin").write_bytes(b"\x00\x02")
    snapshot = capture(path)
    assert any(item.old_path == "tracked.txt" and item.path == "renamed.txt" for item in snapshot.paths)
    assert any(item.binary for item in snapshot.paths)
    before = snapshot.snapshot_content_digest
    git(path, "rm", "-f", "renamed.txt")
    assert capture(path).snapshot_content_digest != before


def test_upstream_and_conflicted_status(tmp_path: Path) -> None:
    path = repo(tmp_path)
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", str(remote))
    git(path, "remote", "add", "origin", str(remote))
    git(path, "push", "-u", "origin", "master")
    assert capture(path).upstream_ref == "origin/master"
    git(path, "checkout", "-b", "conflict")
    (path / "tracked.txt").write_text("branch\n", encoding="utf-8")
    git(path, "commit", "-am", "branch")
    git(path, "checkout", "master")
    (path / "tracked.txt").write_text("master\n", encoding="utf-8")
    git(path, "commit", "-am", "master")
    result = subprocess.run(("git", "merge", "conflict"), cwd=path, capture_output=True, text=True, check=False)
    assert result.returncode != 0
    snapshot = capture(path)
    assert snapshot.conflicted_count == 1 and snapshot.paths[0].area == "conflicted"


def test_detached_and_unborn(tmp_path: Path) -> None:
    path = repo(tmp_path)
    sha = git(path, "rev-parse", "HEAD").strip()
    git(path, "checkout", "--detach", sha)
    detached = capture(path)
    assert detached.detached and detached.head_state == "detached" and detached.head_ref is None
    unborn = tmp_path / "unborn"
    unborn.mkdir()
    git(unborn, "init")
    snapshot = capture(unborn)
    assert snapshot.head_sha is None and snapshot.head_state == "unborn"
    bare = tmp_path / "bare.git"
    git(tmp_path, "init", "--bare", str(bare))
    assert discover(bare).bare


def test_remote_normalization_and_no_secret_persistence(tmp_path: Path) -> None:
    assert normalize_remote("https://alice:token@example.com/org/repo.git") == "https://example.com/org/repo"
    assert normalize_remote("git@example.com:org/repo.git") == "ssh://example.com/org/repo"
    path = repo(tmp_path)
    git(path, "remote", "add", "origin", "https://alice:token@example.com/org/repo.git")
    assert discover(path).remote_identity == "https://example.com/org/repo"


def test_not_repository_and_read_only_allowlist(tmp_path: Path) -> None:
    with pytest.raises(NotARepository):
        discover(tmp_path)
    with pytest.raises(GitError):
        capture(tmp_path, run=lambda args, cwd: (_ for _ in ()).throw(GitError("blocked")))


def test_migration_persistence_restart_and_correlation(tmp_path: Path) -> None:
    path = repo(tmp_path)
    db_path = tmp_path / "observatory.db"
    connection = connect(db_path)
    migrate(connection)
    snapshot = capture(path)
    persist_snapshot(connection, snapshot, session_id="s", thread_id="t", turn_id="u")
    connection.close()
    reopened = connect(db_path)
    migrate(reopened)
    assert reopened.execute("SELECT count(*) FROM git_snapshots").fetchone()[0] == 1
    assert reopened.execute("SELECT correlation_confidence FROM git_snapshot_correlations").fetchone()[0] == "exact_worktree"
    assert reopened.execute("SELECT snapshots_total FROM git_health WHERE collector='git'").fetchone()[0] == 1
