# Security policy

## Supported versions

bettermemory follows semver from 1.0 onward. The latest minor of the current major gets security fixes; older majors and earlier minors do not. Concretely:

| Version | Status |
|---------|--------|
| 3.x latest | Supported |
| 3.x earlier minor | Upgrade to latest 3.x |
| 2.x | Unsupported; upgrade to 3.x (no on-disk migration required — SCHEMA_VERSION stayed at 1) |
| 1.x | Unsupported; upgrade to 3.x (no on-disk migration required — SCHEMA_VERSION stayed at 1) |
| 0.x | Unsupported (pre-1.0) |

## Reporting a vulnerability

**Please do not file a public GitHub issue.** Email the maintainer directly at the address listed on the [GitHub profile](https://github.com/0Mattias), or open a [private security advisory](https://github.com/0Mattias/bettermemory/security/advisories/new) on this repository.

Include in the report:

- A description of the issue and its impact (what does an attacker gain?).
- A reproduction: minimal repo, command, or input that triggers the behavior.
- The bettermemory version (`bettermemory --version`) and the OS or Python version where you observed it.
- Optional: a proposed fix.

You will get an acknowledgement within seven days. The expected timeline for a fix depends on severity:

- **Critical** (RCE, arbitrary file write outside the storage dir, exfiltration of memory contents): hotfix release within two weeks of confirmation, with an advisory.
- **High** (denial of service against a user's local store, integrity break on memory frontmatter): fix in the next minor release, with an advisory if there is no acceptable workaround.
- **Medium or low**: addressed in the normal release cadence.

## Threat model

bettermemory is a single-user, local-first tool. The threat model is correspondingly narrow:

- The attacker is **the user themselves** trying not to lock themselves out (data integrity, no-corruption, no-data-loss invariants under normal operation and concurrent access).
- The attacker is **a process running as the same user** that gains write access to the memory directory. Outside the threat model: bettermemory does not encrypt data on disk and does not authenticate callers; the OS-level filesystem permissions ARE the access control. If you need stronger isolation, use OS-level disk encryption (FileVault, LUKS, BitLocker) and the standard per-user file permissions.
- The attacker is a **malicious memory body** crafted to exploit a parser bug (YAML deserialization, frontmatter handling). bettermemory uses `yaml.SafeLoader` exclusively — every `yaml.load` call in the codebase pins `Loader=yaml.SafeLoader` and the dumper subclasses `yaml.SafeDumper`; no unsafe loaders, no pickle, no `eval`. The frontmatter parser caps incoming YAML at 64 KB before parsing to bound resource use on malformed input. The vendored frontmatter parser (`src/bettermemory/_frontmatter.py`) was added in part to remove the upstream `python-frontmatter` dependency and pin parser behavior.

**Out of scope** (would not be treated as security issues):

- "I edited a memory file and lost data". The on-disk format is hand-editable by design; it is also `git`-versionable for the same reason. Use `git` for change tracking if it matters.
- "Anyone with read access to my home directory can read my memories". Yes, by design. Memories are plaintext on disk. Use OS-level disk encryption.
- "The MCP server does not authenticate clients". Yes, by design. MCP stdio servers are spawned per-client by the MCP host; trust derives from the spawn relationship.

## Hardening notes

- The per-file locking in `store.py` and the parallel lock on the event log in `events.py` are stress-tested under multi-process contention by `tests/test_concurrency.py`. Both alias a single `flock_excl` helper in `_fsutil.py`. On POSIX it is an `fcntl.flock(LOCK_EX)` on a sidecar lockfile; on Windows (audit H3, 3.0.0+ — commit bc47593; through v2.7.3 that branch was a bare `yield`) it is a `msvcrt.locking(LK_NBLCK)` byte-range lock with a retry-and-backoff loop — a real cross-process lock, not a no-op (a 30s timeout is overridable via `BETTERMEMORY_FLOCK_TIMEOUT`). Only if `msvcrt` itself cannot load (extremely unusual — it ships with CPython) or the lockfile cannot be opened does the helper degrade to an in-process-only yield, and it emits a one-shot `logger.warning` so the regression is visible. The lockfile is created `0o600` so a cross-host `sync push` does not leak it world-readable.
- The semantic-dedup cache (`semantic.py`) uses `np.load` with `allow_pickle=False`. A maliciously-crafted cache file cannot trigger arbitrary code execution.
- The git-shelling in `origin.py` and `sync.py` calls `subprocess.run` without `shell=True` and with an explicit argv list. The output is parsed defensively (the git remote URL is run through `urlparse`).
- **Web UI CSRF gate** (2.0+, `bettermemory ui`). The one state-changing endpoint (`POST /memories/{id}/verify`) is guarded by **two** checks, both of which must pass. (1) **A per-process random CSRF token** (audit H4, the load-bearing defense): the server mints a fresh `secrets.token_urlsafe(32)` at startup and embeds it in every rendered page (a `<meta name="csrf-token">` tag / a hidden `csrf_token` form field); the POST must echo it back in the `X-CSRF-Token` header or the `csrf_token` field, and the handler checks this *first*. A cross-site page or a co-resident local web service cannot read the per-process token out of the UI's HTML, so it cannot forge a valid POST — even from a loopback origin. (2) **A loopback same-origin check** on `Origin` (preferred) or `Referer`, kept as belt-and-suspenders. A request missing *either* control is rejected with `403` (header-less and token-less POSTs both fail). CLI users who genuinely need to script against the endpoint must first GET a page, scrape the token from its `<meta name="csrf-token">`, and send it via `-H "X-CSRF-Token: <token>"` **together with** a loopback `-H "Origin: http://127.0.0.1:<port>"` — the token alone, or the origin alone, is insufficient. The `note` form field is capped at 500 chars to bound event-log inflation. The UI exposes no editing surface beyond verify; writes happen in-conversation via `memory_write`. Binding non-loopback via `--host` logs a warning since the UI exposes curation surfaces.
- **Credential redaction in `bettermemory sync`** (2.0+). Git accepts HTTPS remote URLs with embedded auth (`https://user:token@github.com/...`). `_redact_url` and `_redact_text` in `sync.py` mask the userinfo segment in `init` action strings, `SyncStatus.remote_url` (visible in `bettermemory sync status --json`), and `SyncError` messages built from `git push` failures. SSH URLs (`git@host:path`) are left alone — the `git@` is a username, not a token. The user's git config itself is untouched; redaction is purely on the wrapper's output surface.
- **Query redaction in the event log** (`events.py`). The event log (`.events.jsonl`) is created `0o600` and lives next to the memories under the same per-user trust boundary — the primary defense for query contents is filesystem permissions, and `telemetry.log_queries_verbatim` defaults to `false` so `query` / `probe_query` fields are stored as `{"hash", "preview", "len"}` rather than verbatim text. `redact_query` then applies a defense-in-depth pass: before the 32-char preview is taken, known secret token shapes (Anthropic `sk-ant-…`, OpenAI `sk-…`, GitHub `ghp_…` and `github_pat_…`, AWS `AKIA…`) are stripped to opaque markers (`[REDACTED:anthropic-key]` etc.). The 32-char preview alone can capture whole short tokens (a GitHub PAT or an AWS access key fits comfortably inside 32 chars), so pattern-strip protects the cases where the log leaves its `0o600` perimeter — an attached `bettermemory eval` export, a shared transcript, a bug report. Verbatim mode (`telemetry.log_queries_verbatim = true`) bypasses the redaction entirely and is intentional opt-in for users who explicitly want raw queries on disk.

## Disclosure timeline

After a fix lands and a release is cut, an advisory is published on the GitHub repository. Researchers who reported the issue are credited in the advisory unless they prefer otherwise.
