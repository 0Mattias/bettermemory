# Security policy

## Supported versions

bettermemory follows semver from 1.0 onward. The latest 1.x line gets
security fixes; older minor releases do not. Concretely:

| Version | Status |
|---------|--------|
| 1.x latest | Supported |
| 1.x earlier minor | Upgrade to latest 1.x |
| 0.x | Unsupported (pre-1.0; no users expected) |

## Reporting a vulnerability

**Please do not file a public GitHub issue.** Email the maintainer
directly at the address listed on the [GitHub profile](https://github.com/0Mattias),
or open a [private security advisory](https://github.com/0Mattias/bettermemory/security/advisories/new)
on this repository.

Include in the report:

- A description of the issue and its impact (what does an attacker
  gain?).
- A reproduction — minimal repo, command, or input that triggers the
  behavior.
- The bettermemory version (`bettermemory --version`) and the OS /
  Python version where you observed it.
- Optional: a proposed fix.

You'll get an acknowledgement within seven days. The expected timeline
for a fix depends on severity:

- **Critical** (RCE, arbitrary file write outside the storage dir,
  exfiltration of memory contents) — hotfix release within two weeks
  of confirmation, with an advisory.
- **High** (denial of service against a user's local store, integrity
  break on memory frontmatter) — fix in the next minor release;
  advisory if there's no acceptable workaround.
- **Medium / low** — addressed in the normal release cadence.

## Threat model

bettermemory is a single-user, local-first tool. The threat model is
correspondingly narrow:

- The attacker is **the user themselves** trying not to lock themselves
  out (data integrity, no-corruption, no-data-loss invariants under
  normal operation and concurrent access).
- The attacker is **a process running as the same user** that gains
  write access to the memory directory. Outside the threat model —
  bettermemory does not encrypt data on disk and does not authenticate
  callers; the OS-level filesystem permissions ARE the access control.
  If you need stronger isolation, use OS-level disk encryption (FileVault,
  LUKS, BitLocker) and the standard per-user file permissions.
- The attacker is a **malicious memory body** crafted to exploit a
  parser bug (YAML deserialization, frontmatter handling). bettermemory
  uses `yaml.SafeLoader` exclusively — no `yaml.load` anywhere, no
  pickle, no `eval`. The vendored frontmatter parser
  (`src/bettermemory/_frontmatter.py`) was added in part to remove the
  upstream `python-frontmatter` dependency and pin parser behavior.

**Out of scope** (would not be treated as security issues):

- "I edited a memory file and lost data" — the on-disk format is
  hand-editable by design; it is also `git`-versionable for the same
  reason. Use `git` for change tracking if it matters.
- "Anyone with read access to my home directory can read my memories"
  — yes, by design. Memories are plaintext on disk. Use OS-level disk
  encryption.
- "The MCP server doesn't authenticate clients" — yes, by design. MCP
  stdio servers are spawned per-client by the MCP host; trust derives
  from the spawn relationship.

## Hardening notes

- The fcntl-based per-file locking in `store.py` and the parallel lock
  on the event log in `events.py` are stress-tested under multi-process
  contention by `tests/test_concurrency.py`. On Windows, fcntl is
  unavailable and the locks fall back to no-ops; the recommendation on
  Windows is single-process use.
- The semantic-dedup cache (`semantic.py`) uses `np.load` with
  `allow_pickle=False`. A maliciously-crafted cache file cannot trigger
  arbitrary code execution.
- The git-shelling in `origin.py` calls `subprocess.run` without
  `shell=True` and with an explicit argv list — the output is parsed
  defensively (the git remote URL is run through `urlparse`).

## Disclosure timeline

After a fix lands and a release is cut, an advisory is published on
the GitHub repository. Researchers who reported the issue are credited
in the advisory unless they prefer otherwise.
