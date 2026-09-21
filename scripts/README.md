# Scripts

## Phase 7 retention lifecycle gate

Run the disposable integration gate from the repository root:

```bash
uv run python scripts/retention_gate.py
```

The gate creates all runtime state under a temporary directory and removes it
on exit. A successful run prints `PHASE_7_VERIFIED`; assertion failures produce
a non-zero exit. It exercises a partial archive, verification of the manifest
and file digest chain, eligible-only pruning, live and uncovered-row
preservation, historical archive queries, restart-visible audit state, and a
zero-delete rerun. It does not run `VACUUM` or checkpoint maintenance.
