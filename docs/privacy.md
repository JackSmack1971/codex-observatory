# Privacy contract v1

The default mode is `minimal`.

- Prompts are not persisted.
- Raw tool arguments and raw tool output are not persisted; the default durable representation is digest-only.
- Raw wire payloads are not persisted.
- A wire payload may exist transiently in a processing spool while normalization completes, then is deleted after a successful commit.
- `forensic` is explicit opt-in and is required for prompt storage, full arguments/output, or durable raw wire bodies.
- No API/admin credential is part of the TOML schema. The optional Admin adapter may detect `OPENAI_ADMIN_KEY` from the environment only; it is never persisted, logged, or returned by the API.

SHA-256 digests cover canonical UTF-8 text or canonical JSON encoding. Redaction precedes canonicalization and hashing. Privacy failures are treated as correctness failures.
