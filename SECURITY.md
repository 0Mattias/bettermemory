# Security policy

## Supported versions

bettermemory follows semver from 1.0 onward. The latest minor of the current major gets security fixes; older majors and earlier minors do not. Concretely:

| Version | Status |
|---------|--------|
| 9.x latest | Supported |
| 9.x earlier minor | Upgrade to latest 9.x |
| 8.x | Unsupported; upgrade to 9.x and run `bettermemory migrate v8` once per store directory (the 8.x files are read, never written) |
| 7.x and earlier | Unsupported; upgrade to 9.x. No on-disk change between 1.x and 8.x (`models.SCHEMA_VERSION` stayed at 1), so the 8.x migration reads these directories too |
| 0.x | Unsupported (pre-1.0) |

Upgrading across 4.0, 5.0 or 6.0 costs no data migration but does lose surface: 4.0 removed the embedding lane (the `"semantic"` search mode, both embedding extras, `[behavior] semantic_provider` and `semantic_dedup`), 5.0 removed the web UI (`bettermemory ui`, the `[ui]` extra), and 6.0 removed the embedding lane again after its 5.5.0 opt-in reentry (the `[embeddings]` and `[embeddings-fast]` extras). 7.0 removed nothing and changed one default: `episode_handoff` rows carry `body` only when `include_bodies=True` is passed. 8.0 made a `user-inference` write commit on the first call. 9.0 replaced the markdown directory with one SQLite file, reduced twenty-seven tools (eighteen registered by default) to nine and removed the sync lane; a server pointed at an un-migrated 8.x directory refuses to start and names the migration. See the release notes for each.

## Reporting a vulnerability

**Please do not file a public GitHub issue.** Report it privately through [GitHub private vulnerability reporting](https://github.com/0Mattias/bettermemory/security/advisories/new) on this repository, or by email to hello@anaphase.ai.

Include in the report:

- A description of the issue and its impact (what does an attacker gain?).
- A reproduction: minimal repo, command, or input that triggers the behavior.
- The bettermemory version (`bettermemory --version`) and the OS or Python version where you observed it.
- Optional: a proposed fix.

You will get an acknowledgement within seven days. The expected timeline for a fix depends on severity:

- **Critical** (RCE, arbitrary file write outside the storage dir, exfiltration of memory contents, access to the daemon's API without its token): hotfix release within two weeks of confirmation, with an advisory.
- **High** (denial of service against a user's local store, integrity break on a stored record): fix in the next minor release, with an advisory if there is no acceptable workaround.
- **Medium or low**: addressed in the normal release cadence.

## Threat model

bettermemory is a single-user, local-first tool. The store is one SQLite file, `memory.sqlite`, whose tables are the fold of a hash-chained log: every record row was produced by a signed log row and points at it. "Signed" means an HMAC-SHA256 under a per-store key, 32 random bytes kept under the user's config directory (`keys/<store_id>.key`) and never in the store; the key is symmetric, so a process that can read it can sign. Everything the store believes about a memory is in that file: its body, its scopes, and the trust fields (`last_verified_at`, `source`, `confidence`, `claims`, the `verified_*` attestations). The threat model is therefore organised around two questions: who can write into the store file, and who holds the key.

### The attackers

**The user.** Trying not to lock themselves out: data integrity, no corruption, no data loss under normal operation and concurrent access. Every mutation is one SQLite transaction that appends the log row and applies it to its table together, and tombstones make removals restorable.

**A process running as the same user with write access to the store file but not the key.** OS filesystem permissions are the access control: the store directory is created `0o700`, the store file `0o600`. bettermemory does not encrypt at rest, and the daemon's bearer token is its only caller check. What the store believes about a row such a writer inserts:

- **Provenance at read.** Every memories, tombstones and episodes row carries the MAC of the log row that produced it, and every search, show and handoff verifies that pointer: the log row exists, it is of the producing kind, its payload names the record, and its MAC verifies under the store's key. A row inserted around the tool has no such row and reads `unaccounted`; a log row appended without the key fails its MAC and the row pointing at it reads `unaccounted` the same way. The label rides every read surface and the recall hook's pointer, and the health report lists the unaccounted records. A planted row is detected on the next search; it is not silently trusted. The integrity benchmark inserts memories around the write API with a forged log row: 10 of 10 forged memories are caught, against 0 of 10 in 8.0 (the frozen result artifacts under `bench/integrity/results/`).
- **The chain audit.** `bettermemory log verify` walks every row, checks every MAC and every link to the row before, compares the head checkpoint kept beside the key, and refolds the log to compare every table against it. An edited row, a deleted row, a deleted tail and a table row no log row accounts for are each reported; a segment signed by a key this machine does not hold reads `unverifiable`. The command never writes.
- **Detect-only.** None of this stops a write. A writer with the file can still edit a body in place; the edit is reported by the audit and, when the edit touches a row's pointer, at the next read. Treat the store file's integrity as equal to the host account's integrity.
- **Write-time supersession is a lever.** A claim-sized statement admitted through the write API that carries a change cue and a value against a stored claim earns a `supersedes` link over it, so a false statement can mark the true one superseded, and the integrity benchmark measures how often. The link note names the cue and both values, every detector-set link is in the log under `supersedes_detected`, and `memory_update(links=[])` on the new record clears it. The lever is bounded by admission, and `[behavior] write_supersession = false` removes it.

**A process that holds the key.** The key file is owner-only, and every process running as the user is its owner; the store cannot tell the rows such a process signs from the tool's own. There is no defence against the user's own account; tamper evidence is against a writer who reaches the file but not the config directory (a synced or copied store file, a backup restored elsewhere, a process confined to the store directory), and against accidents.

**A local process that reaches the daemon.** One daemon per store listens on 127.0.0.1 only (`[daemon] port`, default 7397) and serves the nine tools at `/mcp`, the hook bodies at `/api/v1/hook/<event>`, `/health` and `/api/v1/admin/shutdown`. Every request except `GET /health` must carry the bearer token from the daemon's state file (`daemon-<store hash>.json` under the user's state directory or `BETTERMEMORY_STATE_DIR`; directory `0o700`, file `0o600`), and the MCP SDK's DNS-rebinding protection restricts Host and Origin to localhost. `/health` answers without the token with the daemon's version, pid, store path and the paths of any saved orphan stores. The token lives behind the same account boundary as the store file and the key. The stdio `bettermemory` process an MCP client spawns forwards to the daemon with that token, and serves the store in-process when no daemon can be reached or started.

**A client that lies about who it is.** Every write records who made it (`actor`) and which channel named the workspace (`origin.source`); see `docs/api.md` under Identity. Every declared field (an `x-bettermemory-*` header, a `BETTERMEMORY_*` variable in the server's environment, the `initialize` handshake's `clientInfo`) is client input and is stored as such, with its channel beside it. Only `actor.principal` is attested: it is read solely through the SDK's `authenticated_principal`, the identity of a verified OAuth token on an HTTP transport, and it is `null` everywhere else. A declared value cannot reach that field by any path, and `tests/test_identity.py` pins it. What the label buys is what provenance buys: a memory that says it came from one client is a claim the reader can weigh, not a fact the store vouches for. Identity is evidence, never a permission: `memory_show(id)` stays unrestricted, and the per-client session isolation it keys (disabled scopes, use tokens) separates clients that declare differently; it is not a boundary against a client that declares another client's name.

**A remote writer.** 9.0 ships no cross-host lane: no sync, no pull, no admission. A store file copied from another machine arrives without its key. The server writes a new key for it and logs a warning; rows signed under the old key keep their stored labels, because their pointers cannot be checked here, and `log verify` reports the store `unverifiable`. An 8.x directory is read only by `bettermemory migrate v8`, which imports its records as `imported` rows under this host's key: the migration is the one path a foreign record takes into the store, and its rows are this host's own record of that directory, not a claim about who wrote the files.

**A malicious memory body crafted to exploit a parser bug.** The 8.x migration and the mirror parse and render YAML frontmatter; the server itself does not. The vendored parser (`src/bettermemory/_frontmatter.py`) uses `yaml.SafeLoader` exclusively, caps the YAML region at 64 KB and the file at 1 MiB before parsing, and refuses alias expansion on dump, so a hostile file exhausts a bounded budget and fails cleanly.

**Instructions inside a memory.** A memory body is data the model reads, and a body can contain text shaped like instructions. bettermemory delivers bodies through tool calls the model makes (`memory_show`, `memory_search`, and the episode handoff with `include_bodies=True`); the session brief and the prompt recall inject pointers (an id, the body's first 80 characters as a summary and, for the recall, a matched snippet of at most 100), never a whole body, under a header that says they are pointers, not instructions, and the handoff never delivers the body of an episode whose pointer does not verify. Every delivery carries the verification block and the provenance label beside the text so the model has the signals to weigh it. What no label can see is an injection-driven legitimate write: a memory the model was talked into writing through the gates reads `local`, truthfully. Cause provenance, what was in context when the model wrote, is the open question behind the label and is not solved today.

**An agent that changes the user's rules.** A standing memory, a ruling or rule the user gave (`ambient`, or a standing answer at or above 0.30; see `docs/api.md`), is replaced, edited, removed, demoted or taken out of force through the tools only with `said`, the user's words for the change, in any project. The daemon checks the words as it checks a join's: the quote is in a prompt the prompt hook saw in the caller's checkout; where no hook saw the checkout the words are taken as given and the reply and the log row say so. Nothing about the words' meaning is read. Every such change is one signed `standing_guard` row with the door, the memories and the quote, so a wrong one is found and reverted. What the guard holds out is an agent that forgets or overreaches through the tools, including one talked into it by text it read. An agent that runs as the user with a shell holds the store's key and is the user as far as the store can tell: it can reach the store through the `bettermemory` CLI or the file itself, and it holds the daemon's token, so it can post a prompt of its own to the hook endpoint and the daemon keeps that as the user's words. No tool check holds it out, and a daemon under a separate OS account would not either: that account would keep the file, the key and the log out of the agent's reach, but the agent's account would still hold the token the hook and the shim need, and with it the words channel. The line is the harness's to draw, between the hooks and MCP servers it runs with the user's access and the shell it gives the agent. Claude Code's sandbox draws it when it is on and locked: inside it the agent's shell reaches neither the daemon's port, the store, the key nor the token, the hooks and the shim run outside it, and the only words the daemon holds are the ones the prompt hook posted. `docs/installation.md` names the settings.

**Session capture.** With `[capture] stop_checkpoint = true` (off by default, Claude Code only), the Stop hook periodically hands the chat model an instruction to save what the conversation established, and the model writes memories from its own context. That is a write path a conversation's content can steer, so it is bounded on every side: only a session a person is attending is asked, at most `daily_cap` times per store per day; the writes pass every gate above with no `force` and no acknowledge flag honoured, plus the enforced capture rules (no to-dos, no drifting counts, absolute dates, a plain subject); each write carries the review's `unsafe` and `secret` scores and the unanswered relations are settled in the batch's closing call; and every capture is a batch on the signed log that `bettermemory capture review` lists and `capture undo` takes back, after which the same body is not captured again. A captured memory reads `local`, like any write the model made.

### Out of scope

These would not be treated as security issues:

- "I edited the store file and lost data." The file is SQLite; edit it with the tool, or with `sqlite3` knowing the audit will report the edit.
- "Anyone with read access to my home directory can read my memories." Yes, by design. Memories are plaintext on disk. Use OS-level disk encryption (FileVault, LUKS, BitLocker).
- "The daemon serves any local process that presents its token." Yes, by design; see the daemon and the client that lies about who it is, above.
- "A process running as me signed a row." Yes, by design; see the process that holds the key, above. Deciding what is true is the model's and the user's job, with the signals the read surfaces carry.
- "A process running as me posted a prompt to the daemon's hook endpoint, and the guard took it as my words." Yes, by design; see the agent that changes the user's rules, above. The hook channel is the account's, and the harness's sandbox is what keeps the agent's shell off it.

## Hardening notes

- **One writer at a time.** The store opens in WAL mode with a 5-second busy timeout. The daemon holds one connection on its event loop and one per hook worker thread, every mutation is one transaction, and a second writer waits rather than interleaving. There are no lock files.
- **Git shelling.** `origin.py` calls `subprocess.run` without `shell=True` and with an explicit argv list; output is parsed defensively (the git remote URL is run through `urlparse`) and decoded as UTF-8 explicitly.
- **Credential refusal at write.** The write path refuses a body carrying a secret-shaped token (API keys, PEM blocks, JWTs, `password=` assignments) unless the caller acknowledges it as a documented example. The event records only the detector kinds, never the value.
- **Query redaction in the log** (`events.py`). Every `query` / `probe_query` field is stored as `{"hash", "preview", "len"}` rather than verbatim text, and `redact_query` strips known secret token shapes (Anthropic `sk-ant-…`, OpenAI `sk-…`, GitHub `ghp_…` and `github_pat_…`, AWS `AKIA…`) to opaque markers before the 32-char preview is taken. There is no verbatim mode. The migration redacts every 8.x event the same way on import.
- **The key file.** Created `0o600` under a `0o700` directory, read once at open, never logged. A store whose key is lost is readable and unverifiable; there is no recovery path that re-signs history, by design.

### Controls that no longer apply at HEAD

Recorded because a reader scoping the attack surface should know these are gone rather than unguarded.

- **Sync admission and quarantine** (6.6.0 through 8.x). `bettermemory sync` replicated the store over a git remote, and every pull was judged by a size cap, the parser, an id-alias check and the credential gate, with refusals quarantined. 9.0 removed the sync lane whole, with the admission chain; the `quarantine` table stays in the schema for the lane's return.
- **Content evidence over files** (7.3.0 through 8.x). Every store write recorded the SHA-256 of the file it wrote beside the index row, and `bettermemory doctor` named files whose bytes changed with no store write behind the change. The signed log and the per-row pointer replace it: the evidence is now on every row and checked on every read, not on a rebuild.
- **Web UI CSRF gate** (2.0 through 4.x, `bettermemory ui`). 5.0.0 removed the web module. The 9.0 daemon's HTTP surface is covered in the threat model above.
- **`np.load(allow_pickle=False)` on the semantic-dedup cache** (through 3.x). 4.0.0 removed the module and numpy itself.

## Disclosure timeline

After a fix lands and a release is cut, an advisory is published on the GitHub repository. Researchers who reported the issue are credited in the advisory unless they prefer otherwise.
