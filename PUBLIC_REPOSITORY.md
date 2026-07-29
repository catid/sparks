# Public repository safety

This repository is public. It intentionally contains reproducible source,
tests, pinned dependency revisions, sanitized examples, and curated aggregate
benchmark reports. It intentionally does not contain:

- API keys, provider tokens, SSH private keys, certificates, or passwords
- active OpenClaw, dashboard, or shell configuration
- model weights or Hugging Face caches
- raw prompts, responses, reasoning traces, shell transcripts, or journals
- runtime logs, PIDs, container state, or generated workspaces

Secrets should be supplied at runtime from a mode-`0600` file outside this
checkout or from a secret manager. OpenClaw examples use environment
`SecretRef` objects; replace those references locally, never with literal
values in a tracked file.

Before every public commit, run:

```bash
scripts/check-public.sh --staged
```

The same checker runs in CI over every tracked file. It is deliberately
conservative. A clean automated scan is necessary but not sufficient: raw
model output and operational telemetry still require semantic review.

The two DSpark `.env` profiles are tracked exceptions because they contain
only reproducible topology and runtime settings, not credentials. Paths and
private RFC1918 addresses describe this specific two-Spark installation and
must be adapted before use elsewhere.

No top-level license has been selected yet. Public visibility alone does not
grant permission to copy or redistribute this repository; add the owner's
chosen license before presenting it as generally reusable.
