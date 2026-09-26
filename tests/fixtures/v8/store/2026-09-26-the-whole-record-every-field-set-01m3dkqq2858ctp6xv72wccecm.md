---
actor:
  client: claude-code
  client_version: 2.1.281
  model: claude-fable-5-1
  sources:
    client: client-info
    model: header
category: user-inference
claims:
- src/a.py::main
- '!tools/gone.py'
confidence: high
corroborations: 2
created: 2026-09-26 01:02:08.456000+00:00
id: 01M3DKQQ2858CTP6XV72WCCECM
last_corroborated: 2026-09-27 01:02:08.456000+00:00
last_verified_at: 2026-09-26 02:02:08.456000+00:00
links:
- note: newer
  target_id: 01M3DKQK58CTHCC16KV6S5W7AE
  type: supersedes
- target_id: 01M3DKQN3RYA4GXXP2K49WAHWZ
  type: extends
origin:
  branch: main
  cwd: /w/foo
  repo: https://example.com/foo.git
  source: roots
  worktree_root: /w/foo
schema_version: 1
scopes:
- projects:foo
- tools
source: inferred
updated: 2026-09-26 01:03:08.456000+00:00
verified_absent_paths:
- tools/gone.py
verified_commits:
- abc1234
verified_head: bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
verified_paths:
- src/a.py
- src/b.py
verified_versions:
- 8.0.0
---

the whole record, every field set