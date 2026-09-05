# Security policy

## Supported versions

bettermemory follows semver from 1.0 onward. The latest minor of the current major gets security fixes; older majors and earlier minors do not. Concretely:

| Version | Status |
|---------|--------|
| 7.x latest | Supported |
| 7.x earlier minor | Upgrade to latest 7.x |
| 6.x | Unsupported; upgrade to 7.x (no on-disk migration required, SCHEMA_VERSION stayed at 1) |
| 5.x and earlier | Unsupported; upgrade to 7.x (no on-disk migration required, SCHEMA_VERSION stayed at 1) |
| 0.x | Unsupported (pre-1.0) |

Upgrading across 4.0, 5.0 or 6.0 costs no data migration but does lose surface: 4.0 removed the embedding lane (the `"semantic"` search mode, both embedding extras, `[behavior] semantic_provider` and `semantic_dedup`), 5.0 removed the web UI (`bettermemory ui`, the `[ui]` extra), and 6.0 removed the embedding lane again after its 5.5.0 opt-in reentry (the `[embeddings]` and `[embeddings-fast]` extras). 7.0 removed nothing and changed one default: `episode_handoff` rows carry `body` only when `include_bodies=True` is passed. See the release notes for each.

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

bettermemory is a single-user, local-first tool. The store is a directory of plain markdown files, one per memory, plus a derived SQLite index and a few sidecars. Everything the store believes about a memory used to be in that file: its body, its scopes, and the trust fields (`last_verified_at`, `source`, `confidence`, `claims`, the `verified_*` attestations). The threat model is therefore organised around one question: who can write into the store directory, and what does the store believe when they do.

### The attackers

**The user.** Trying not to lock themselves out: data integrity, no corruption, no data loss under normal operation and concurrent access. Every mutator takes a per-file lock, every write is atomic, and tombstones make removals restorable.

**A process running as the same user with write access to the store directory.** This is the root precondition for every other attack below, and OS filesystem permissions are the access control: the store directory is created `0o700`, memory files `0o600`, and bettermemory does not encrypt at rest or authenticate callers. What has changed since 6.5.0 is what the store believes about a file that appeared under such a writer:

- **Provenance.** The index carries one label per memory, derived at every rebuild from the event log and the sync repo and never from the file: `local` (written through this host's own code path), `synced` (a `sync pull` brought it down), `untracked` (the event log cannot speak to it) or `unaccounted` (the log covers its creation and nothing wrote or pulled it: the hand-planted shape). The label rides every read surface and the recall hook's pointer, `memory_health` lists the unaccounted records and `bettermemory doctor` warns on them. A planted file is detected; it is not silently trusted. Episodes carry a label of the same kind since 7.0.0, read from the event log at each read: a file dropped into a session directory reads `unaccounted` on `episode_search` and `episode_handoff`.
- **What is not provided: tamper evidence.** There is no hash, MAC or chain over memory files or the event log. A writer with access can change any body, scope or trust field, or append an event line, and nothing at read time notices; `doctor`'s index reconcile is consistency evidence that a reindex clears. Treat the store directory's integrity as equal to the host account's integrity. Detect-only tamper evidence is on the roadmap. The integrity benchmark measures the gap: a file planted around the write API reads `unaccounted` on every surface, and the same file with a forged `write` event line reads `local` (`docs/eval-results.md`).
- **Write-time supersession is a lever.** Since 7.2.0 a claim-sized statement admitted through the write API that carries a change cue and a value against a stored claim earns a `supersedes` link over it, so a false statement can mark the true one superseded: on the integrity benchmark one of ten admitted false facts does (`docs/eval-results.md`). The link note names the cue and both values, every detector-set link is in the event log under `supersedes_detected`, and `memory_update(links=[])` on the new record clears it. The trade is deliberate: without the link the false fact and the true one sit side by side with nothing to tell them apart, which the same benchmark shows on every arm. The lever is bounded by admission — what the write path refuses never reaches it — and `[behavior] write_supersession = false` removes it.

**A remote writer.** `bettermemory sync` replicates the store over a git remote the user controls. Anyone who can push to that remote, and the remote host itself, can put files into every clone's store directory on its next `sync pull`. Since 6.6.0 a pull is an admission, not a copy:

- **Admission.** Every memory file a pull brings down is judged before the event is recorded and before the index is rebuilt: a size cap (a file the store would refuse to read is not read at all), the store's own parser, an id-alias check (a pulled file carrying an id another active file already carries is refused, and the file already here keeps the id, so a push cannot shadow a memory by sorting later in the directory), and the credential gate the write path runs. A refusal is quarantined: the file stays where git put it, tracked and unchanged, so no deletion propagates to other hosts, and this host's store skips it on every walk. It is never indexed, never returned by a search or a listing, never shown, never counted by health, never pointed at by the recall hook. `bettermemory sync quarantine` lists the refusals, `bettermemory doctor` warns on them, every later pull judges them again and releases a file that was fixed upstream, and `--release` runs the same chain by hand. Transient and user-claim phrasing is reported as an advisory flag on admitted files rather than refused, because the writing host's acknowledgement does not travel with the file.
- **Provenance.** A pulled file reads `synced` everywhere the label appears.
- **Remote stamps are not local evidence.** A pulled file carries whatever `last_verified_at` and `verified_*` attestations the other host, or the attacker, wrote. The index records separately when this host last verified the memory through its own `memory_verify`, and a pull clears that record for every file it lands. A `synced` record with a stamp this host never made reads `verification.status: "remote"` and `staleness_verdict: spot_check_required` on every surface until a local verify re-stamps it, and that verify re-checks the attested paths against this machine. The recall pointer says `unverified here`.
- **What is not defended.** A pulled body that is merely false or manipulative, with no secret in it, is admitted; it reads `synced` and unverified, and it is retrievable. Git history is trusted exactly as git trusts it: there is no signature verification and no `fsck`. A remote deletion removes the file here, with `git log` in the store directory as the only record. Tombstones under `.tombstones/` sync without admission, and `memory_restore` re-admits a tombstone's trust fields without re-checking them; both are on the roadmap. A store cloned by hand rather than pulled carries no pull record, so its files read `untracked` and the remote-stamp rule does not apply to them.

**A malicious memory body crafted to exploit a parser bug.** YAML deserialization and frontmatter handling. bettermemory uses `yaml.SafeLoader` exclusively (every `yaml.load` pins `Loader=yaml.SafeLoader`, the dumper subclasses `yaml.SafeDumper`, no unsafe loaders, no pickle, no `eval`). The frontmatter parser caps the YAML region at 64 KB and the file at 1 MiB before parsing, and refuses alias expansion on dump, so a hostile file exhausts a bounded budget and fails cleanly. The vendored parser (`src/bettermemory/_frontmatter.py`) exists in part to pin that behaviour.

**Instructions inside a memory.** A memory body is data the model reads, and a body can contain text shaped like instructions. bettermemory delivers bodies through tool calls the model makes (`memory_show`, `memory_search`, `episode_search`, and `episode_handoff` with `include_bodies=True`) and, when the standing tier is on (off by default), at session start. Since 7.0.0 the two deliveries that reach context without a specific request are gated on provenance: the standing tier delivers a body only for a memory the index labels `local` whose live verdict is fresh, and renders every other admitted ambient memory as a pointer (id, scopes, label); `episode_handoff` delivers takeaways by default, bodies only on request, and never the body of an episode the event log did not see written. The recall hook injects a pointer and a snippet, never a body. Every delivery carries the verification block and the provenance label beside the text so the model has the signals to weigh it. What no label can see is an injection-driven legitimate write: a memory the model was talked into writing through the gates reads `local`, truthfully. Cause provenance, what was in context when the model wrote, is the open question behind the label and is on the roadmap.

### Out of scope

These would not be treated as security issues:

- "I edited a memory file and lost data." The on-disk format is hand-editable by design; it is also `git`-versionable for the same reason. Use `git` for change tracking if it matters.
- "Anyone with read access to my home directory can read my memories." Yes, by design. Memories are plaintext on disk. Use OS-level disk encryption (FileVault, LUKS, BitLocker).
- "The MCP server does not authenticate clients." Yes, by design. MCP stdio servers are spawned per-client by the MCP host; trust derives from the spawn relationship.
- "A false memory was pulled from my own remote and the store served it." The remote is the user's; admission refuses what the store can judge from the bytes and labels the rest. Deciding what is true is the model's and the user's job, with the signals the read surfaces carry.

## Hardening notes

- **Locking.** The per-file locking in `store.py` and the parallel lock on the event log in `events.py` are stress-tested under multi-process contention by `tests/test_concurrency.py`. Both alias a single `flock_excl` helper in `_fsutil.py`. On POSIX it is an `fcntl.flock(LOCK_EX)` on a sidecar lockfile; on Windows (since 3.0.0, commit bc47593) it is a `msvcrt.locking(LK_NBLCK)` byte-range lock with a retry-and-backoff loop, a real cross-process lock with a 30 s timeout overridable via `BETTERMEMORY_FLOCK_TIMEOUT`. Only if `msvcrt` itself cannot load or the lockfile cannot be opened does the helper degrade to an in-process-only yield, with a one-shot `logger.warning` so the regression is visible. The lockfile is created `0o600` so a cross-host `sync push` does not leak it world-readable.
- **Git shelling.** `origin.py` and `sync.py` call `subprocess.run` without `shell=True` and with an explicit argv list; output is parsed defensively (the git remote URL is run through `urlparse`) and decoded as UTF-8 explicitly. `sync pull` passes `--no-tags`, so a hostile or sloppy remote cannot inject refs under `refs/tags/` that shadow branch names. The active-file walk rejects symlinks, so a pushed `something.md` pointing at `/etc/passwd` is never opened as a memory.
- **Sync admission and quarantine** (6.6.0). The chain above runs inside the same store-wide sync lock as the pull, after git's own conflict checks and before the index rebuild. The quarantine sidecar (`.quarantine.json`) is host-local and gitignored, like every other sidecar in `sync._GITIGNORE_LINES`; a structural test discovers every `*_FILENAME` constant and fails when one is missing from that list, which is how the six earlier sidecar leaks were closed for good. `--force` on a release is accepted only for a credential refusal; an oversize, unparseable or id-alias refusal cannot be forced, because the store could not serve the file as it is or would put two active files behind one id.
- **Credential redaction in `bettermemory sync`** (2.0+). Git accepts HTTPS remote URLs with embedded auth (`https://user:token@github.com/...`). `_redact_url` and `_redact_text` in `sync.py` mask the userinfo segment in `init` action strings, `SyncStatus.remote_url` (visible in `bettermemory sync status --json`), and `SyncError` messages built from `git push` and `git pull` failures. SSH URLs (`git@host:path`) are left alone; the `git@` is a username, not a token. The user's git config itself is untouched.
- **Credential refusal at write and at admission.** The write path refuses a body carrying a secret-shaped token (API keys, PEM blocks, JWTs, `password=` assignments) unless the caller acknowledges it as a documented example, and the same detector is the one hard gate of sync admission. The quarantine entry and the event record only the detector kinds, never the value.
- **Query redaction in the event log** (`events.py`). The event log (`.events.jsonl` and its shards) is created `0o600` and lives next to the memories under the same per-user trust boundary. `telemetry.log_queries_verbatim` defaults to `false`, so `query` / `probe_query` fields are stored as `{"hash", "preview", "len"}` rather than verbatim text, and `redact_query` strips known secret token shapes (Anthropic `sk-ant-…`, OpenAI `sk-…`, GitHub `ghp_…` and `github_pat_…`, AWS `AKIA…`) to opaque markers before the 32-char preview is taken. A 32-char preview alone can capture a whole short token, so the strip protects the cases where the log leaves its perimeter: an attached `bettermemory eval` export, a shared transcript, a bug report. Verbatim mode is an explicit opt-in.

### Controls that no longer apply at HEAD

Recorded because a reader scoping the attack surface should know these are gone rather than unguarded, and because the removals shrank the surface rather than leaving it exposed.

- **Web UI CSRF gate** (2.0 through 4.x, `bettermemory ui`). The one state-changing endpoint (`POST /memories/{id}/verify`) was guarded by a per-process `secrets.token_urlsafe(32)` echoed back in a header or form field, plus a loopback same-origin check on `Origin`/`Referer`. 5.0.0 removed the web module, every route, the `ui` subcommand and the `[ui]` extra. bettermemory serves no HTTP surface at all; the only transport is the MCP stdio server. There is no port, no binding, and no browser-reachable endpoint to forge a request against.
- **`np.load(allow_pickle=False)` on the semantic-dedup cache** (through 3.x, `semantic.py`). 4.0.0 removed the module, the persistent `.embeddings.*.npz` cache, and numpy itself; nothing at HEAD reads a cache file of that class. `sync.py` still excludes the glob so an upgraded store's leftovers are not pushed, but no code path opens them.

## Disclosure timeline

After a fix lands and a release is cut, an advisory is published on the GitHub repository. Researchers who reported the issue are credited in the advisory unless they prefer otherwise.
