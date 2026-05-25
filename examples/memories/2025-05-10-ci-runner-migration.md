---
schema_version: 1
id: 01JTXAANP0MQ8F2N0YH3WK6PCA
created: 2025-05-10T14:42:00+00:00
updated: 2025-05-10T14:42:00+00:00
scopes: [projects:atlas, infrastructure]
confidence: high
source: explicit-statement
origin:
  cwd: /Users/example/code/atlas-feat-runners
  repo: https://github.com/example/atlas.git
  branch: feat/buildkite-migration
  worktree_root: /Users/example/code/atlas-feat-runners
links:
  - type: supersedes
    target_id: 01JNX8ZKW0HC5VPB8Z2QXFM7TR
    note: Replaces the Jenkins note; we cut over on 2025-05-09.
---
Atlas CI now runs on Buildkite. Pipelines live in
`.buildkite/pipeline.yaml`; agent queues are `linux-amd64` for the
backend and `macos-arm64` for the iOS build. The old Jenkinsfile
has been deleted — retrieval should prefer this memory and demote
the Jenkins one until it is tombstoned.
