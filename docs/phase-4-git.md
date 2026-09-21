# Phase 4 — read-only Git evidence

Phase 4 records mechanically observed Git state. Git is the authority: the
adapter executes an explicit read-only allowlist (`config`, `diff`,
`rev-parse`, and `status`), captures command results, parses stable
machine-readable formats, and persists the resulting snapshot. It cannot add,
commit, checkout, reset, stash, clean, merge, rebase, push, or otherwise write
Git state.

`git-capture --cwd <path>` supports repository roots, nested directories, and
linked worktrees. Repository identity is distinct from worktree identity:
repositories use the canonical common Git directory plus a normalized remote
identity when available; worktrees use that common directory plus the
canonical worktree root. Remote credentials are removed before persistence.

Snapshots preserve attached, detached, and unborn HEAD states; branch and
upstream evidence; staged, unstaged, untracked, renamed, deleted, added, and
conflicted paths; and staged/unstaged `--numstat` evidence. Ignored files do
not make a snapshot dirty. Full diffs are never stored. A content digest is
deterministic for the same observed state, while each observation has its own
observation ID.

Migration 4 adds `repositories`, `worktrees`, `git_snapshots`,
`git_snapshot_paths`, `git_snapshot_correlations`, and `git_health`. Explicit
workflow correlation is recorded as `exact_worktree`; it is an observation
such as “Git state observed during/after this session”, not a claim that the
session caused a change.

Commands:

```text
codex-observatory git-capture --db <db> --cwd <repository-or-subdirectory>
codex-observatory git-query --db <db> --limit 20
```

`doctor` reports Git independently. Outside a repository it reports
`GIT_NOT_A_REPOSITORY`, which is not a collector crash. Phase 3 remains
`IMPLEMENTED / EXTERNAL_RUNTIME_BLOCKED` at
`eddcfcf6f6cc3c0febded52def1db12d720c12d3`; Phase 4 does not depend on hooks.
