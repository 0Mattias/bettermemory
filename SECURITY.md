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
- **High** (denial of service against a user's local store, integrity break on a stored record): fix in the next minor release, with an advisory if there is no acceptable workaround.
- **Medium or low**: addressed in the normal release cadence.

## Threat model

bettermemory is a single-user, local-first tool. The store is one SQLite file, `memory.sqlite`, whose tables are the fold of a hash-chained log: every record row was produced by a signed log row and points at it. Everything the store believes about a memory is in that file: its body, its scopes, and the trust fields (`last_verified_at`, `source`, `confidence`, `claims`, the `verified_*` attestations). The key that signs the log is 32 random bytes under the user's config directory, never in the store. The threat model is therefore organised around two questions: who can write into the store file, and who holds the key.

### The attackers

**The user.** Trying not to lock themselves out: data integrity, no corruption, no data loss under normal operation and concurrent access. Every mutation is one SQLite transaction that appends the log row and applies it to its table together, and tombstones make removals restorable.

**A process running as the same user with write access to the store file but not the key.** OS filesystem permissions are the access control: the store directory is created `0o700`, the store file `0o600`, and bettermemory does not encrypt at rest or authenticate callers. What the store believes about a row such a writer inserts:

- **Provenance at read.** Every memories, tombstones and episodes row carries the MAC of the log row that produced it, and every search, show and handoff verifies that pointer: the log row exists, it is of the producing kind, its payload names the record, and its MAC verifies under the store's key. A row inserted around the tool has no such row and reads `unaccounted`; a log row appended without the key fails its MAC and the row pointing at it reads `unaccounted` the same way. The label rides every read surface and the recall hook's pointer, and the health report lists the unaccounted records. A planted row is detected on the next search; it is not silently trusted. The integrity benchmark measures exactly this: a memory planted around the write API, and the same memory with a forged log row, on every arm (`docs/eval-results.md`).
- **The chain audit.** `bettermemory log verify` walks every row, checks every MAC and every link to the row before, compares the head checkpoint kept beside the key, and refolds the log to compare every table against it. An edited row, a deleted row, a deleted tail and a table row no log row accounts for are each reported; a segment signed by a key this machine does not hold reads `unverifiable`. The command never writes.
- **Detect-only.** None of this stops a write. A writer with the file can still edit a body in place; the edit is reported by the audit and, when the edit touches a row's pointer, at the next read. Treat the store file's integrity as equal to the host account's integrity.
- **Write-time supersession is a lever.** A claim-sized statement admitted through the write API that carries a change cue and a value against a stored claim earns a `supersedes` link over it, so a false statement can mark the true one superseded: on the integrity benchmark one of ten admitted false facts does. The link note names the cue and both values, every detector-set link is in the log under `supersedes_detected`, and `memory_update(links=[])` on the new record clears it. The lever is bounded by admission, and `[behavior] write_supersession = false` removes it.

**A process that holds the key.** The key is under the user's config directory with owner-only permissions, so a process running as the user can read it. Such a writer can sign anything, and the store cannot tell its rows from the tool's own. There is no defence against the user's own account; tamper evidence is against a writer who reaches the file but not the config directory (a synced or copied store file, a backup restored elsewhere, a process confined to the store directory), and against accidents.

**A client that lies about who it is.** Every write records who made it (`actor`) and which channel named the workspace (`origin.source`); see `docs/api.md` under Identity. Every declared field (an `x-bettermemory-*` header, a `BETTERMEMORY_*` variable in the server's environment, the `initialize` handshake's `clientInfo`) is client input and is stored as such, with its channel beside it. Only `actor.principal` is attested: it is read solely through the SDK's `authenticated_principal`, the identity of a verified OAuth token on an HTTP transport, and it is `null` everywhere else. A declared value cannot reach that field by any path, and `tests/test_identity.py` pins it. What the label buys is what provenance buys: a memory that says it came from one client is a claim the reader can weigh, not a fact the store vouches for. Identity is evidence, never a permission: `memory_show(id)` stays unrestricted, and the per-client session isolation it keys (disabled scopes, use tokens) separates clients that declare differently; it is not a boundary against a client that declares another client's name.

**A remote writer.** 9.0 ships no cross-host lane: no sync, no pull, no admission. A store file copied from another machine reads here without its key, every row `trust_unavailable`, and `log verify` reports it `unverifiable`. An 8.x directory is read only by `bettermemory migrate v8`, which imports its records as `imported` rows under this host's key: the migration is the one path a foreign record takes into the store, and its rows are this host's own record of that directory, not a claim about who wrote the files.

**A malicious memory body crafted to exploit a parser bug.** The 8.x migration and the mirror parse and render YAML frontmatter; the server itself does not. The vendored parser (`src/bettermemory/_frontmatter.py`) uses `yaml.SafeLoader` exclusively, caps the YAML region at 64 KB and the file at 1 MiB before parsing, and refuses alias expansion on dump, so a hostile file exhausts a bounded budget and fails cleanly.

**Instructions inside a memory.** A memory body is data the model reads, and a body can contain text shaped like instructions. bettermemory delivers bodies through tool calls the model makes (`memory_show`, `memory_search`, and the episode handoff with `include_bodies=True`); the recall hook injects a pointer and a snippet, never a body, and the handoff never delivers the body of an episode whose pointer does not verify. Every delivery carries the verification block and the provenance label beside the text so the model has the signals to weigh it. What no label can see is an injection-driven legitimate write: a memory the model was talked into writing through the gates reads `local`, truthfully. Cause provenance, what was in context when the model wrote, is the open question behind the label and is not solved today.

### Out of scope

These would not be treated as security issues:

- "I edited the store file and lost data." The file is SQLite; edit it with the tool, or with `sqlite3` knowing the audit will report the edit.
- "Anyone with read access to my home directory can read my memories." Yes, by design. Memories are plaintext on disk. Use OS-level disk encryption (FileVault, LUKS, BitLocker).
- "The MCP server does not authenticate clients." Yes, by design. MCP stdio servers are spawned per-client by the MCP host; trust derives from the spawn relationship. The server records what a client declares about itself and keeps the attested OAuth principal in a separate field; it authenticates nothing on its own.
- "A process running as me signed a row." The key is the user's; a process with the user's account has it. Deciding what is true is the model's and the user's job, with the signals the read surfaces carry.

## Hardening notes

- **One writer at a time.** The store is one SQLite connection per process in WAL mode with a busy timeout; a second process waits rather than interleaving, and every mutation is one transaction. There are no lock files.
- **Git shelling.** `origin.py` calls `subprocess.run` without `shell=True` and with an explicit argv list; output is parsed defensively (the git remote URL is run through `urlparse`) and decoded as UTF-8 explicitly.
- **Credential refusal at write.** The write path refuses a body carrying a secret-shaped token (API keys, PEM blocks, JWTs, `password=` assignments) unless the caller acknowledges it as a documented example. The event records only the detector kinds, never the value.
- **Query redaction in the log** (`events.py`). Every `query` / `probe_query` field is stored as `{"hash", "preview", "len"}` rather than verbatim text, and `redact_query` strips known secret token shapes (Anthropic `sk-ant-…`, OpenAI `sk-…`, GitHub `ghp_…` and `github_pat_…`, AWS `AKIA…`) to opaque markers before the 32-char preview is taken. There is no verbatim mode. The migration redacts every 8.x event the same way on import.
- **The key file.** Created `0o600` under a `0o700` directory, read once at open, never logged. A store whose key is lost is readable and unverifiable; there is no recovery path that re-signs history, by design.

### Controls that no longer apply at HEAD

Recorded because a reader scoping the attack surface should know these are gone rather than unguarded.

- **Sync admission and quarantine** (6.6.0 through 8.x). `bettermemory sync` replicated the store over a git remote, and every pull was judged by a size cap, the parser, an id-alias check and the credential gate, with refusals quarantined. 9.0 removed the sync lane whole, with the admission chain; the `quarantine` table stays in the schema for the lane's return.
- **Content evidence over files** (7.3.0 through 8.x). Every store write recorded the SHA-256 of the file it wrote beside the index row, and `bettermemory doctor` named files whose bytes changed with no store write behind the change. The signed log and the per-row pointer replace it: the evidence is now on every row and checked on every read, not on a rebuild.
- **Web UI CSRF gate** (2.0 through 4.x, `bettermemory ui`). 5.0.0 removed the web module; bettermemory serves no HTTP surface of its own.
- **`np.load(allow_pickle=False)` on the semantic-dedup cache** (through 3.x). 4.0.0 removed the module and numpy itself.

## Disclosure timeline

After a fix lands and a release is cut, an advisory is published on the GitHub repository. Researchers who reported the issue are credited in the advisory unless they prefer otherwise.
