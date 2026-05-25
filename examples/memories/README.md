# Example memories

These files illustrate the on-disk shape of a bettermemory store and
how each differentiated feature surfaces in the frontmatter. They are
not meant to be loaded into your real store as-is — drop them into a
throwaway `BETTERMEMORY_DIR` if you want to poke at them with
`memory_search`, or just read them.

## Files

- **`2025-03-14-tutorial-style.md`** — the canonical user preference.
  Minimal frontmatter; demonstrates the `learning-style` scope and a
  `source: explicit-statement`. Referenced from the project README and
  installation docs.
- **`2025-04-02-pnpm-monorepo.md`** — an infrastructure / tooling fact
  with a populated `origin` block, `last_verified_at`, and
  `verified_paths`. Shows how the store proves "this fact still
  matches the tree" via file-path drift.
- **`2025-04-15-projects-atlas-stack.md`** — a project-scoped memory
  using plural scopes (`projects:atlas` + `infrastructure`) and a
  `links` entry that `extends` another memory id. Shows how
  `projects:<name>` isolates per-project knowledge.
- **`2025-05-10-ci-runner-migration.md`** — paired with an older
  superseded memory id via `links: [{type: supersedes, ...}]`. Use
  this to see how the `supersedes` edge surfaces at retrieval time
  (the newer memory is preferred; the older is demoted).

## How the model uses these

`memory_search` returns each hit alongside metadata derived from the
file: `staleness_verdict ∈ {fresh, spot_check_recommended,
spot_check_required}` is computed per call from calendar age, drift
against `verified_paths` on disk, and git commits touching those
paths since `last_verified_at`. The auto-scope filter uses the
`origin.repo` / `origin.worktree_root` fields to default-filter to
the caller's current project. None of these fields are required —
legacy memories without them are treated as global, `fresh`, and
unverified.
