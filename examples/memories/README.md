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
- **`2025-02-10-atlas-jenkins-ci.md`** + **`2025-05-10-ci-runner-migration.md`**
  — a superseded/superseding pair. The May file carries
  `links: [{type: supersedes, target_id: <feb file's id>}]`. The
  edge is advisory in `memory_search` (ranking doesn't auto-demote
  the older id), but `memory_show` surfaces the link and the
  `consolidate --apply` pass uses it as a demotion candidate. Drop
  both into a throwaway `BETTERMEMORY_DIR` to see how the link
  renders end-to-end.

## How the model uses these

`memory_search` returns each hit alongside metadata derived from the
file: `staleness_verdict ∈ {fresh, spot_check_recommended,
spot_check_required}` is computed per call from calendar age,
file-path drift, and git commit drift. Path drift is *checked*
against every path-shaped token the body cites, but only the
CLAIM-ANCHORED misses move the verdict: a path named in
`verified_paths` that has since disappeared, or a relative citation
resolved against the memory's own recorded `origin.worktree_root`.
Those arrive as `path_drift.claim_anchored_missing`, a subset of
`path_drift.missing`; the rest of `missing` (tokens scraped out of
prose — a remote host's path, a documentation example) still ships
as evidence but no longer raises a tier. So `verified_paths` is not
an exclusion list — attesting a path is what makes its next
disappearance escalate. The list that *excludes* a path from the
drift signal is `verified_absent_paths`, the mirror attestation for
a path that is intentionally absent here; those land in
`path_drift.expected_absent` instead. The auto-scope filter
uses the `origin.repo` / `origin.worktree_root` fields to
default-filter to the caller's current project. None of these
fields are required — legacy memories without `last_verified_at`
surface as `spot_check_required` (the verification status is
`never`); a `memory_verify(id, verified_paths=[…])` call drops the
verdict to `fresh` — provided the paths you attest are actually
there, since an attested path that is already gone is precisely the
anchored miss that holds the verdict up.
