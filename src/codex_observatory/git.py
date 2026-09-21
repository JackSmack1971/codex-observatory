from __future__ import annotations

import hashlib
import json
import shlex
import subprocess
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .sqlite import utc_now

ADAPTER_VERSION = "git.v1"
ALLOWED_COMMANDS = frozenset({"config", "diff", "rev-parse", "status"})


class GitError(RuntimeError):
    pass


class NotARepository(GitError):
    pass


@dataclass(frozen=True, slots=True)
class GitPath:
    path: str
    status: str
    area: str
    old_path: str | None = None
    insertions: int | None = None
    deletions: int | None = None
    binary: bool = False


@dataclass(frozen=True, slots=True)
class RepositoryIdentity:
    repo_id: str
    root: str
    git_dir: str
    common_dir: str
    remote_identity: str | None
    bare: bool


@dataclass(frozen=True, slots=True)
class GitSnapshot:
    snapshot_observation_id: str
    snapshot_content_digest: str
    repo_id: str
    worktree_id: str
    root: str
    worktree_path: str
    remote_identity: str | None
    head_sha: str | None
    head_ref: str | None
    detached: bool
    head_state: str
    upstream_ref: str | None
    clean: bool
    staged_count: int
    unstaged_count: int
    untracked_count: int
    conflicted_count: int
    changed_file_count: int
    insertions: int
    deletions: int
    binary_count: int
    captured_at: str
    evidence_digest: str
    paths: tuple[GitPath, ...]


Runner = Callable[[tuple[str, ...], Path], str]


def _run(args: tuple[str, ...], cwd: Path, timeout: int = 5) -> str:
    if not args or args[0] not in ALLOWED_COMMANDS:
        raise GitError(f"Git command is not allowlisted: {args[0] if args else '<empty>'}")
    try:
        result = subprocess.run(("git", *args), cwd=cwd, capture_output=True, text=False, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise GitError(str(exc)) from exc
    stdout = result.stdout.decode("utf-8", errors="surrogateescape")
    stderr = result.stderr.decode("utf-8", errors="replace")
    if result.returncode != 0:
        if args[0] == "rev-parse" and "not a git repository" in stderr.lower():
            raise NotARepository(stderr.strip())
        raise GitError(f"git {shlex.join(args)} failed ({result.returncode}): {stderr.strip()}")
    return stdout


def normalize_remote(value: str) -> str | None:
    value = value.strip()
    if not value:
        return None
    if value.startswith(("/", "file:")):
        return value.removeprefix("file:").rstrip("/")
    if "://" not in value and ":" in value.split("/", 1)[0]:
        user_host, path = value.split(":", 1)
        host = user_host.rsplit("@", 1)[-1].lower()
        return f"ssh://{host}/{path.lstrip('/').removesuffix('.git')}"
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme in {"http", "https", "ssh", "git"}:
        host = (parsed.hostname or "").lower()
        path = parsed.path.rstrip("/").removesuffix(".git")
        return f"{parsed.scheme}://{host}{path}"
    return value.rsplit("@", 1)[-1].rstrip("/").removesuffix(".git")


def _identity(cwd: Path, run: Runner = _run) -> RepositoryIdentity:
    cwd = cwd.expanduser().resolve()
    bare = run(("rev-parse", "--is-bare-repository"), cwd).strip() == "true"
    root = cwd if bare else Path(run(("rev-parse", "--show-toplevel"), cwd).strip()).resolve()
    git_dir = Path(run(("rev-parse", "--absolute-git-dir"), cwd).strip()).resolve()
    common_dir = Path(run(("rev-parse", "--path-format=absolute", "--git-common-dir"), cwd).strip()).resolve()
    try:
        remotes = run(("config", "--get-regexp", r"^remote\..*\.url$"), cwd)
    except GitError:
        remotes = ""
    remote = next((normalize_remote(line.split(" ", 1)[1]) for line in remotes.splitlines() if " " in line), None)
    repo_key = "\0".join((str(common_dir), remote or "", "bare" if bare else "worktree"))
    repo_id = hashlib.sha256(repo_key.encode()).hexdigest()
    return RepositoryIdentity(repo_id, str(root), str(git_dir), str(common_dir), remote, bare)


def discover(cwd: Path | str, run: Runner = _run) -> RepositoryIdentity:
    return _identity(Path(cwd), run)


def _status(raw: str) -> tuple[GitPath, ...]:
    records = raw.split("\0")
    paths: list[GitPath] = []
    i = 0
    while i < len(records):
        record = records[i]
        i += 1
        if not record or record.startswith("##"):
            continue
        if len(record) < 4 or record[2] != " ":
            continue
        xy, first = record[:2], record[3:]
        old_path = None
        path = first
        if xy in {"R ", " R", "RM", "MR", "C ", " C"} and i < len(records):
            path, old_path = first, records[i]
            i += 1
        staged = xy[0] not in {" ", "?"}
        unstaged = xy[1] not in {" ", "?"}
        area = "untracked" if xy == "??" else "staged+unstaged" if staged and unstaged else "staged" if staged else "unstaged"
        if xy[0] == "U" or xy[1] == "U" or xy == "AA" or xy == "DD":
            area = "conflicted"
        paths.append(GitPath(path, xy, area, old_path))
    return tuple(paths)


def _diff(raw: str) -> dict[str, tuple[int, int, bool]]:
    result: dict[str, tuple[int, int, bool]] = {}
    parts = raw.split("\0")
    i = 0
    while i + 1 < len(parts):
        fields = parts[i].split("\t")
        i += 1
        if len(fields) < 3:
            continue
        added, deleted, path = fields[0], fields[1], "\t".join(fields[2:])
        if added == "-" and deleted == "-":
            result[path] = (0, 0, True)
        else:
            try:
                added_number, deleted_number = int(added), int(deleted)
            except ValueError:
                continue
            result[path] = (added_number, deleted_number, False)
    return result


def capture(cwd: Path | str, *, run: Runner = _run, timeout: int = 5) -> GitSnapshot:
    identity = discover(cwd, run)
    worktree_path = identity.root
    root = Path(identity.root)
    status_raw = run(("status", "--porcelain=v1", "-z", "--untracked-files=all", "--renames"), root)
    header = run(("status", "--porcelain=v2", "--branch", "--untracked-files=no"), root)
    values = {line.split(" ", 2)[1]: line.split(" ", 2)[2] for line in header.splitlines() if line.startswith("# ") and len(line.split(" ", 2)) == 3}
    oid = values.get("branch.oid")
    head_sha = None if oid in {None, "(initial)"} else oid
    head = values.get("branch.head")
    detached = head == "(detached)"
    head_ref = None if detached or head in {None, "(unknown)"} else f"refs/heads/{head}"
    upstream = values.get("branch.upstream")
    paths = list(_status(status_raw))
    staged_diff = _diff(run(("diff", "--cached", "--numstat", "-z", "--no-renames"), root))
    unstaged_diff = _diff(run(("diff", "--numstat", "-z", "--no-renames"), root))
    enriched: list[GitPath] = []
    for item in paths:
        staged_counts = staged_diff.get(item.path)
        unstaged_counts = unstaged_diff.get(item.path)
        counts: tuple[int, int, bool] | None
        if item.area == "staged+unstaged" and staged_counts and unstaged_counts:
            counts = (staged_counts[0] + unstaged_counts[0], staged_counts[1] + unstaged_counts[1], staged_counts[2] or unstaged_counts[2])
        else:
            counts = staged_counts if item.area in {"staged", "conflicted"} else unstaged_counts
        enriched.append(GitPath(item.path, item.status, item.area, item.old_path, *(counts or (None, None, False))))
    paths = enriched
    staged = sum(path.area in {"staged", "staged+unstaged", "conflicted"} for path in paths)
    unstaged = sum(path.area in {"unstaged", "staged+unstaged", "conflicted"} for path in paths)
    untracked = sum(path.area == "untracked" for path in paths)
    conflicted = sum(path.area == "conflicted" for path in paths)
    insertions = sum(path.insertions or 0 for path in paths)
    deletions = sum(path.deletions or 0 for path in paths)
    binary = sum(path.binary for path in paths)
    clean = not paths
    evidence = {"status": status_raw, "staged": staged_diff, "unstaged": unstaged_diff, "header": header}
    evidence_digest = hashlib.sha256(json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode("utf-8", "surrogateescape")).hexdigest()
    worktree_key = f"{identity.common_dir}\0{root}"
    worktree_id = hashlib.sha256(worktree_key.encode()).hexdigest()
    content = {
        "repo": identity.repo_id, "worktree": worktree_id, "head": [head_sha, head_ref, detached, values.get("branch.oid")],
        "upstream": upstream, "clean": clean, "paths": [(path.path, path.status, path.area, path.old_path, path.insertions, path.deletions, path.binary) for path in paths],
        "evidence": evidence_digest,
    }
    digest = hashlib.sha256(json.dumps(content, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    captured_at = utc_now()
    observation_id = hashlib.sha256(f"{digest}:{captured_at}".encode()).hexdigest()
    return GitSnapshot(observation_id, digest, identity.repo_id, worktree_id, identity.root, worktree_path, identity.remote_identity, head_sha, head_ref, detached, "detached" if detached else "unborn" if head_sha is None else "attached", upstream, clean, staged, unstaged, untracked, conflicted, len(paths), insertions, deletions, binary, captured_at, evidence_digest, tuple(paths))


def persist_snapshot(connection, snapshot: GitSnapshot, *, session_id: str | None = None, thread_id: str | None = None, turn_id: str | None = None) -> None:
    with connection:
        connection.execute("INSERT OR IGNORE INTO repositories(repo_id,root,remote_identity,bare,first_seen,last_seen) VALUES(?,?,?,?,?,?)", (snapshot.repo_id, snapshot.root, snapshot.remote_identity, 0, snapshot.captured_at, snapshot.captured_at))
        connection.execute("INSERT OR IGNORE INTO worktrees(worktree_id,repo_id,path,first_seen,last_seen) VALUES(?,?,?,?,?)", (snapshot.worktree_id, snapshot.repo_id, snapshot.worktree_path, snapshot.captured_at, snapshot.captured_at))
        connection.execute("UPDATE repositories SET last_seen=? WHERE repo_id=?", (snapshot.captured_at, snapshot.repo_id))
        connection.execute("UPDATE worktrees SET last_seen=? WHERE worktree_id=?", (snapshot.captured_at, snapshot.worktree_id))
        connection.execute("INSERT INTO git_snapshots(snapshot_observation_id,snapshot_content_digest,repo_id,worktree_id,head_sha,head_ref,detached,head_state,upstream_ref,clean,staged_count,unstaged_count,untracked_count,conflicted_count,changed_file_count,insertions,deletions,binary_count,captured_at,adapter_version,evidence_digest) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (snapshot.snapshot_observation_id, snapshot.snapshot_content_digest, snapshot.repo_id, snapshot.worktree_id, snapshot.head_sha, snapshot.head_ref, int(snapshot.detached), snapshot.head_state, snapshot.upstream_ref, int(snapshot.clean), snapshot.staged_count, snapshot.unstaged_count, snapshot.untracked_count, snapshot.conflicted_count, snapshot.changed_file_count, snapshot.insertions, snapshot.deletions, snapshot.binary_count, snapshot.captured_at, ADAPTER_VERSION, snapshot.evidence_digest))
        for path in snapshot.paths:
            connection.execute("INSERT INTO git_snapshot_paths(snapshot_observation_id,path,status,area,old_path,insertions,deletions,binary) VALUES(?,?,?,?,?,?,?,?)", (snapshot.snapshot_observation_id, path.path, path.status, path.area, path.old_path, path.insertions, path.deletions, int(path.binary)))
        if session_id or thread_id or turn_id:
            connection.execute("INSERT INTO git_snapshot_correlations(snapshot_observation_id,session_id,thread_id,turn_id,correlation_method,correlation_confidence,created_at) VALUES(?,?,?,?,?,?,?)", (snapshot.snapshot_observation_id, session_id, thread_id, turn_id, "explicit_workflow", "exact_worktree", snapshot.captured_at))
        connection.execute("INSERT INTO git_health(collector,status,repositories_discovered_total,snapshots_total,capture_failures_total,parse_failures_total,correlations_total,unresolved_correlations_total,last_capture,last_error,updated_at) VALUES('git','healthy',1,1,0,0,?,?,?,NULL,?) ON CONFLICT(collector) DO UPDATE SET status='healthy',repositories_discovered_total=git_health.repositories_discovered_total+1,snapshots_total=git_health.snapshots_total+1,correlations_total=git_health.correlations_total+excluded.correlations_total,last_capture=excluded.last_capture,updated_at=excluded.updated_at", (1 if session_id or thread_id or turn_id else 0, 0, snapshot.captured_at, snapshot.captured_at))
