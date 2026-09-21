# Event schema v1

This document freezes the Phase-0 vocabulary for later implementations. It does not implement ingestion or storage.

## Boundary objects

`RawEnvelope` is the normalized boundary around an input signal. It carries a UUID, received timestamp, source kind, source version, payload encoding, payload digest, optional transient spool reference, and a retention class. The durable contract must never require raw content in minimal privacy mode.

`CanonicalEvent` is the source-labelled, append-only fact used by later correlation. It includes event identity, event time, ingest time, session/thread/turn identifiers when present, event kind, provenance, privacy class, and a canonical JSON payload containing only permitted fields. Unknown fields remain explicitly unknown; they are not silently reinterpreted.

`CorrelationResult` records candidate links, rule identifier, confidence, exact versus heuristic method, and evidence references. A verified alias is valid only for a tested Codex version and compatibility rule.

## Invariants

- Every event is source-labelled and has stable identity and timestamps.
- Canonicalization uses UTF-8 canonical JSON where a digest is required.
- Minimal mode persists neither prompts nor raw tool arguments/output; those values may exist only in the short-lived processing spool while being normalized.
- Raw wire bodies are durable only with explicit `privacy.mode = "forensic"`.
- Correlation uncertainty is represented, never hidden by a guessed join.
- This repository does not yet define a database schema or collector behavior.
