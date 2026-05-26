---
schema_version: 1
id: 01JNX8ZKW0HC5VPB8Z2QXFM7TR
created: 2025-02-10T11:30:00+00:00
updated: 2025-02-10T11:30:00+00:00
scopes: [projects:atlas, infrastructure]
confidence: high
source: explicit-statement
origin:
  cwd: /Users/example/code/atlas
  repo: https://github.com/example/atlas.git
  branch: main
  worktree_root: /Users/example/code/atlas
---
Atlas CI runs on Jenkins. The `Jenkinsfile` at the repo root drives
the backend test + container build; the iOS build is a separate
multi-branch pipeline that polls the Mac mini agent pool. New
pipeline stages must declare an `agent { label '...' }` clause —
node-less stages get scheduled on the controller and starve
everything else.
