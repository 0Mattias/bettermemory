# Security policy

## Supported versions

bettermemory follows semver from 1.0 onward. The latest minor of the current major gets security fixes; older majors and earlier minors do not. Concretely:

| Version | Status |
|---------|--------|
| 2.x latest | Supported |
| 2.x earlier minor | Upgrade to latest 2.x |
| 1.x | Unsupported; upgrade to 2.x (no on-disk migration required — SCHEMA_VERSION stayed at 1) |
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

- The fcntl-based per-file locking in `store.py` and the parallel lock on the event log in `events.py` are stress-tested under multi-process contention by `tests/test_concurrency.py`. On Windows, fcntl is unavailable and the locks fall back to no-ops; the recommendation on Windows is single-process use.
- The semantic-dedup cache (`semantic.py`) uses `np.load` with `allow_pickle=False`. A maliciously-crafted cache file cannot trigger arbitrary code execution.
- The git-shelling in `origin.py` and `sync.py` calls `subprocess.run` without `shell=True` and with an explicit argv list. The output is parsed defensively (the git remote URL is run through `urlparse`).
- **Web UI CSRF gate** (2.0+, `bettermemory ui`). The one state-changing endpoint (`POST /memories/{id}/verify`) requires the request's `Origin` (preferred) or `Referer` header to point at a loopback host (`localhost`, `127.0.0.1`, `::1`) — the gate rejects cross-site form submissions that mainstream browsers auto-attach headers to. **Header-less POSTs are rejected.** Browsers reliably send `Origin` on POSTs initiated from a document (the HTML spec requires it for non-safe method requests), so a request with neither `Origin` nor `Referer` is a non-browser tool hitting the endpoint directly; in a LAN-exposed configuration that would otherwise be an unauthenticated state-mutation primitive. CLI users who genuinely need to script against the UI should set `-H "Origin: http://127.0.0.1:<port>"` — the standard CSRF-safe pattern. The gate is intentionally port-agnostic — any loopback origin passes, so a co-resident local web service on `localhost:3000` could in principle POST to the UI on `localhost:8765`. This is the design trade-off: same-machine trust is the entire trust model for a tool that binds loopback by default. The `note` form field is capped at 500 chars to bound event-log inflation. The UI exposes no editing surface beyond verify; writes happen in-conversation via `memory_write`. Binding non-loopback via `--host` logs a warning since the UI exposes curation surfaces.
- **Credential redaction in `bettermemory sync`** (2.0+). Git accepts HTTPS remote URLs with embedded auth (`https://user:token@github.com/...`). `_redact_url` and `_redact_text` in `sync.py` mask the userinfo segment in `init` action strings, `SyncStatus.remote_url` (visible in `bettermemory sync status --json`), and `SyncError` messages built from `git push` failures. SSH URLs (`git@host:path`) are left alone — the `git@` is a username, not a token. The user's git config itself is untouched; redaction is purely on the wrapper's output surface.

## Disclosure timeline

After a fix lands and a release is cut, an advisory is published on the GitHub repository. Researchers who reported the issue are credited in the advisory unless they prefer otherwise.
