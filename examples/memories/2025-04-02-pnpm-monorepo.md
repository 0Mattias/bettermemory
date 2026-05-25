---
schema_version: 1
id: 01JQVTHX80N5KQ4M8B0V3X7T9A
created: 2025-04-02T18:00:00+00:00
updated: 2025-04-02T18:00:00+00:00
scopes: [tools, infrastructure]
confidence: high
source: explicit-statement
last_verified_at: 2025-04-02T18:00:00+00:00
origin:
  cwd: /Users/example/code/atlas
  repo: https://github.com/example/atlas.git
  branch: main
  worktree_root: /Users/example/code/atlas
verified_paths:
  - pnpm-workspace.yaml
  - package.json
---
This repo uses pnpm with a workspace, not npm or yarn. Run
`pnpm install` at the root; per-package lockfiles are forbidden
by `.npmrc` (only the root `pnpm-lock.yaml` is committed). When
adding a dep to a workspace package, use
`pnpm --filter <pkg> add <dep>` so the root lockfile stays the
single source of truth.
