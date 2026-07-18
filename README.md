<p align="center">
  <img src="https://raw.githubusercontent.com/0Mattias/bettermemory/main/docs/assets/banner.svg" alt="bettermemory: memory that is checked before it is believed" width="100%">
</p>

<p align="center">
  <a href="https://pypi.org/project/bettermemory/"><img src="https://img.shields.io/pypi/v/bettermemory.svg" alt="PyPI"></a>
  <a href="https://github.com/0Mattias/bettermemory/actions/workflows/ci.yml"><img src="https://github.com/0Mattias/bettermemory/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.11%E2%80%933.14-blue.svg" alt="Python 3.11-3.14"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-yellow.svg" alt="MIT"></a>
</p>

Claude forgets you. Every session starts from zero: the stack you
explained yesterday, the deploy quirk you debugged last week, the "we
use uv, not pip" you have now said eleven times.

bettermemory gives Claude Code — or any MCP client — memory that
survives the session and distrusts itself on your behalf. Every stored
fact is a claim that has to keep earning belief: checked against the
filesystem and git before the model relies on it, tracked when it
shapes an answer, curated when the evidence says it rotted. You
operate none of it. The agent does.

## Install

Paste this into your agent:

> Install bettermemory as my memory server: read
> https://github.com/0Mattias/bettermemory and follow the
> "For agents" section of the README.

That's the whole install. Prefer to do it yourself? Two commands:

```sh
uv tool install bettermemory
bettermemory init --client claude-code   # or claude-desktop / cursor / continue / cline
```

Restart the client and you're done. Want proof before wiring anything
up? `uvx bettermemory try` runs a 60-second offline demo in a
throwaway store: it writes a memory citing a file, deletes the file,
and shows the next search flag the memory as stale.

## What that feels like

Three weeks ago, mid-session:

```text
> for the record: staging deploys go through Fly now, the Render
  setup is dead

⏺ Stored: "Staging deploys via fly deploy --config fly.staging.toml;
  the old Render service is decommissioned." (projects:acme)
```

Today, brand-new session, no context given:

```text
> staging is 404ing after every deploy. ideas?

⏺ You have a stored note that staging deploys through Fly and Render
  is gone — last verified 3 weeks and 41 commits ago, so checking it
  before relying on it… fly.staging.toml still exists, but its
  internal_port changed on Monday. That's the 404: the app listens
  on 8080, the config still says 3000.

> fix it

⏺ Fixed. Re-verified the memory too, so the next session inherits it
  fresh instead of re-checking.
```

(Illustrative transcript.) Two things happened that a raw session
cannot do: it remembered across a three-week gap, and it distrusted
itself exactly enough to catch that the world had moved since.

## Why this one

- **It knows when it might be wrong.** Every hit carries a staleness
  verdict built from calendar age, the file paths it cites, and the
  commits landed since it was last confirmed.
- **It stays out of your context.** Nothing is auto-injected;
  retrieval is a deliberate tool call, announced when it shapes an
  answer.
- **It refuses to hoard.** Transient state, secret-shaped tokens, and
  near-duplicates bounce at write time; claims about *you* stage for
  your confirmation.
- **It's your data, on your disk.** One markdown file per memory —
  greppable, hand-editable, git-syncable. No database, no cloud, no
  account. MIT.
- **It proves it's helping.** A built-in eval reports how often memory
  actually shaped a reply. Numbers, not vibes —
  [published](docs/eval-results.md).

## For agents

You were likely handed this link with "set this up." Steps are
idempotent and safe to re-run:

1. `uv tool install bettermemory` — or `pipx install bettermemory`,
   or `pip install bettermemory` into a venv. Python 3.11–3.14.
2. `bettermemory init --client claude-code` — likewise
   `claude-desktop`, `cursor`, `continue`, `cline`. For any other MCP
   host, plain `bettermemory init` prints the canonical `mcpServers`
   JSON snippet plus known config locations.
3. `bettermemory doctor` — exit 0 means correctly wired; every failed
   check prints a one-line fix.
4. Have the user restart the client so the server loads, then confirm
   by asking the model *"what memory tools do you have?"*

Your operating contract — tool signatures, retrieval discipline,
write gates — is [docs/api.md](docs/api.md); the server's
`instructions` block delivers the core policy automatically. For the
long-form policy in your system prompt: `bettermemory init
--with-addendum`. Migrating from Claude Code's built-in auto-memory:
`bettermemory ingest` imports those files once.

## Everything else

You shouldn't need it, but it's all written down: mechanics, storage
format, the full tool surface, CLI, configuration, and limitations in
[docs/internals.md](docs/internals.md); per-client setup in
[docs/clients.md](docs/clients.md) and
[docs/installation.md](docs/installation.md); eval methodology in
[docs/eval.md](docs/eval.md); postmortems in
[docs/incidents/](docs/incidents/); release history in
[CHANGELOG.md](CHANGELOG.md); dev setup and the compatibility
contract in [CONTRIBUTING.md](CONTRIBUTING.md).

MIT licensed — see [LICENSE](LICENSE).
