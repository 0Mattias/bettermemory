---
schema_version: 1
id: 01JRWBM3Y0VJS4HJ1CPKCVP9KW
created: 2025-04-15T09:14:00+00:00
updated: 2025-04-15T09:14:00+00:00
scopes: [projects:atlas, infrastructure]
confidence: high
source: user-correction
origin:
  cwd: /Users/example/code/atlas
  repo: https://github.com/example/atlas.git
  branch: main
  worktree_root: /Users/example/code/atlas
links:
  - type: extends
    target_id: 01JQVTHX80N5KQ4M8B0V3X7T9A
    note: pnpm convention applies; this is the atlas-specific layout.
---
Atlas is a FastAPI + SQLAlchemy + Postgres backend with a pnpm
TypeScript frontend in the same monorepo. We do NOT use Alembic for
migrations — they are hand-rolled SQL files in `db/migrations/`,
applied by a small bash runner the deploy pipeline invokes before
boot. New migrations must be append-only and idempotent.
