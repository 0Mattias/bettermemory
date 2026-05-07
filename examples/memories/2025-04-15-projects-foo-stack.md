---
id: 01HXYZPROJECTFOOSTACKEXAMP
created: 2025-04-15T09:14:00+00:00
updated: 2025-04-15T09:14:00+00:00
scopes: [projects:foo]
confidence: high
source: user-correction
---
Project "foo" is a FastAPI + SQLAlchemy + Postgres stack.
We do NOT use Alembic — migrations are hand-rolled SQL
files in db/migrations/, applied by a small bash runner.
