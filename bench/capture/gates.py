"""CAPS-P2: the capture rules on a tagged write, against three sets.

  known_bad   10 constructed statements, each breaking exactly one
              server-enforced capture rule, two per rule name in
              `durability.find_capture_rule_hits` (plain_subject, no_todo,
              drifting_count, test_total, absolute_date). Bound: all 10
              refused, each with its own rule and no other.
  good        30 constructed durable, specific project facts that break no
              rule. Bound: all 30 pass.
  integrity   the integrity benchmark's 94 legitimate statements (its first,
              second and third statements per topic plus the hard negatives,
              `bench/integrity/corpus.json`; `score.ingestion_plan`'s rows).
              Bound: at most 2 refused. Read once; only counts, rule names,
              ids and the file's sha256 leave this script, never a body.

The gate is `find_capture_rule_hits`, which `CaptureRulesGate` calls on a
capture write's body. The two constructed sets also go through the real
write path: a tagged memory_write into an open batch on a scratch store
(one batch per known-bad statement, since a refusal ends a capture), and the
refusal's status and rule names are checked against the gate's. The
transient-marker list (`find_transient_markers`, the next body gate on any
write) is counted beside each set for information; it is not graded.

    python bench/capture/gates.py [--constructed-only]
"""

from __future__ import annotations

import asyncio
import hashlib
import shutil
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common  # noqa: E402

CORPUS = _common.ROOT / "bench" / "integrity" / "corpus.json"
INTEGRITY_N = 94
INTEGRITY_MAX_REFUSED = 2

#: (rule name, statement): each breaks that rule and no other.
KNOWN_BAD: tuple[tuple[str, str], ...] = (
    (
        "plain_subject",
        "The user's laptop runs macOS 15 with the acme repository under ~/src/acme.",
    ),
    (
        "plain_subject",
        "The assistant suggested pinning httpx to 0.27.2 because 0.28 dropped "
        "the proxies argument.",
    ),
    ("no_todo", "TODO: the migration script for the billing tables is unfinished."),
    (
        "no_todo",
        "Fix the flaky retry case in tests/test_sync.py before the 2.4 release.",
    ),
    ("drifting_count", "The acme-sync repository on GitHub has 1,240 stars."),
    ("drifting_count", "The ruff-lsp package sees 50k downloads a month on PyPI."),
    ("test_total", "The pytest suite for acme-sync reports 412 passed and 3 skipped."),
    ("test_total", "CI for the parser package ends with 1,038 tests passing."),
    (
        "absolute_date",
        "The staging cluster was recently moved to the eu-west-2 region.",
    ),
    ("absolute_date", "The billing service moved off Heroku three weeks ago."),
)

GOOD: tuple[str, ...] = (
    "The acme-sync daemon listens on 127.0.0.1 port 8420 and never binds a "
    "public interface.",
    "acme-web builds with Vite 5.4 and serves the dist/ directory behind nginx.",
    "The billing service stores amounts as integer cents in a BIGINT column, "
    "never as floats.",
    "Database migrations in acme-api run through Alembic, with each revision "
    "under migrations/versions/.",
    "The project pins Python 3.12 in .python-version and in pyproject's "
    "requires-python.",
    "Releases of acme-cli are tagged vX.Y.Z on main and published to PyPI by "
    "the release workflow.",
    "The parser rejects input files larger than 64 MiB with error code E_TOO_LARGE.",
    "Staging lives in eu-west-2 and production in eu-west-1, in separate AWS accounts.",
    "Secrets for acme-api come from AWS Secrets Manager at boot, never from "
    ".env files in the image.",
    "The mobile app calls the API under the /v2 prefix; /v1 was retired with "
    "release 3.0.",
    "Code style in acme-web is enforced by Biome with a line width of 100.",
    "The search index is rebuilt at 02:00 UTC every night by the reindex cron "
    "job on worker-3.",
    "The acme repositories squash-merge feature branches and keep merge commits "
    "for release branches.",
    "The Go services build with Go 1.23 and vendor their dependencies under vendor/.",
    "Feature flags in acme-web are read from LaunchDarkly with a 30-second client "
    "cache.",
    "The Postgres primary runs version 16 with logical replication to the "
    "analytics replica.",
    "acme-api returns errors as RFC 9457 problem+json bodies with a stable type "
    "URI per error.",
    "The CSV importer strips a leading UTF-8 byte order mark before parsing a file.",
    "Integration tests in acme-api need Docker because they start Postgres "
    "with testcontainers.",
    "Design tokens live in packages/tokens and compile to CSS variables through "
    "Style Dictionary.",
    "Auth sessions expire after 12 hours of inactivity and refresh tokens after "
    "30 days.",
    "The monorepo uses pnpm workspaces, and the pre-commit hook rejects npm and "
    "yarn lockfiles.",
    "Webhook deliveries are retried with exponential backoff up to 8 attempts "
    "over about 4 hours.",
    "The image pipeline converts uploads to AVIF and WebP and keeps the original "
    "in the cold bucket.",
    "Every service ships its logs to Grafana Loki with a 14-day retention.",
    "The rate limiter allows 600 requests per minute per API key and answers 429 "
    "beyond that.",
    "The events table stores timestamps in UTC as timestamptz and the UI converts "
    "them to the viewer's zone.",
    "acme-cli reads ~/.config/acme/config.toml, and the ACME_CONFIG variable "
    "overrides that path.",
    "The Rust crate acme-core uses edition 2021 and forbids unsafe code at the "
    "crate root.",
    "Payments go through Stripe test mode on staging and live mode only in production.",
)


def _gate(body: str) -> list[str]:
    from bettermemory.durability import find_capture_rule_hits

    return [hit.name for hit in find_capture_rule_hits(body)]


def _markers(body: str) -> list[str]:
    from bettermemory.durability import find_transient_markers

    return sorted({m.marker for m in find_transient_markers(body)})


def integrity_statements() -> tuple[list[tuple[str, str]], dict[str, Any]]:
    """The 94 statements as (id, body), and the read's record. The file is
    opened once."""
    import json

    raw = CORPUS.read_bytes()
    read = {
        "path": str(CORPUS.relative_to(_common.ROOT)),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "read_at": _common.stamp(),
        "reads": 1,
    }
    corpus = json.loads(raw)
    rows = [
        (stmt["id"], stmt["text"])
        for topic in corpus["topics"]
        for stmt in topic["statements"]
    ]
    rows += [(hn["id"], hn["text"]) for hn in corpus["hard_negatives"]]
    if len(rows) != INTEGRITY_N:
        raise SystemExit(f"expected {INTEGRITY_N} statements, found {len(rows)}")
    return rows, read


async def _write_path(root: Path) -> dict[str, Any]:
    """The constructed sets through tagged memory_write calls."""
    world = _common.World(root)
    bad: list[dict[str, Any]] = []
    for rule, body in KNOWN_BAD:
        batch = world.open()
        reply = await world.capture_write(batch, body, ["projects:acme"])
        world.close(batch, "failed")
        bad.append(
            {
                "rule": rule,
                "status": reply.get("status"),
                "capture_rules": [r["name"] for r in reply.get("rules") or []],
            }
        )
    batch = world.open()
    good: list[dict[str, Any]] = []
    for body in GOOD:
        reply = await world.capture_write(batch, body, ["projects:acme"])
        good.append({"status": reply.get("status"), "rule": reply.get("rule")})
    world.close(batch)
    from bettermemory import capture

    members = len(capture.members(world.store, batch.batch))
    return {"known_bad": bad, "good": good, "good_batch_members": members}


def main() -> None:
    scratch = Path(tempfile.mkdtemp(prefix="caps-gates-"))
    try:
        _common.scratch_env(scratch)
        bad_rows = []
        for index, (rule, body) in enumerate(KNOWN_BAD):
            hits = _gate(body)
            bad_rows.append(
                {
                    "index": index,
                    "intended_rule": rule,
                    "gate_rules": hits,
                    "own_rule_only": hits == [rule],
                    "transient_markers": _markers(body),
                }
            )
        good_rows = [
            {"index": i, "gate_rules": _gate(b), "transient_markers": _markers(b)}
            for i, b in enumerate(GOOD)
        ]
        path = asyncio.run(_write_path(scratch / "store"))
        if "--constructed-only" in sys.argv:
            # A dry run of the two constructed sets: the integrity file is
            # not opened and no result is written.
            for row in bad_rows + good_rows:
                print(row)
            print(path)
            return
        # Read last, once, after everything that could fail has run.
        statements, read = integrity_statements()
        integrity_refused: dict[str, list[str]] = {}
        integrity_markers = 0
        for stmt_id, body in statements:
            hits = _gate(body)
            if hits:
                integrity_refused[stmt_id] = hits
            integrity_markers += bool(_markers(body))
        del statements
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    for row, wrote in zip(bad_rows, path["known_bad"], strict=True):
        row["write_status"] = wrote["status"]
        row["write_rules"] = wrote["capture_rules"]
    for row, wrote in zip(good_rows, path["good"], strict=True):
        row["write_status"] = wrote["status"]
        row["write_rule"] = wrote["rule"]

    bad_ok = sum(
        r["own_rule_only"]
        and r["write_status"] == "capture_rule"
        and r["write_rules"] == [r["intended_rule"]]
        for r in bad_rows
    )
    good_ok = sum(not r["gate_rules"] for r in good_rows)
    by_rule = Counter(rule for hits in integrity_refused.values() for rule in hits)
    refused_n = len(integrity_refused)
    doc = {
        "kind": "capture-gates",
        "prediction": "CAPS-P2",
        "commit": _common.git("rev-parse", "HEAD"),
        "run_at": _common.stamp(),
        "gate": "bettermemory.durability.find_capture_rule_hits (CaptureRulesGate)",
        "sets": {
            "known_bad": {
                "n": len(bad_rows),
                "refused_by_own_rule_only": bad_ok,
                "by_intended_rule": dict(Counter(r["intended_rule"] for r in bad_rows)),
                "write_path_status": dict(Counter(r["write_status"] for r in bad_rows)),
                "with_transient_markers": sum(
                    bool(r["transient_markers"]) for r in bad_rows
                ),
                "rows": bad_rows,
            },
            "good": {
                "n": len(good_rows),
                "passed_gate": good_ok,
                "refused_by_rule": dict(
                    Counter(x for r in good_rows for x in r["gate_rules"])
                ),
                "write_path_status": dict(
                    Counter(r["write_status"] for r in good_rows)
                ),
                "batch_members": path["good_batch_members"],
                "with_transient_markers": sum(
                    bool(r["transient_markers"]) for r in good_rows
                ),
                "rows": good_rows,
            },
            "integrity": {
                "n": INTEGRITY_N,
                "source": read,
                "refused": refused_n,
                "refused_by_rule": dict(by_rule),
                "refused_ids": sorted(integrity_refused),
                "with_transient_markers": integrity_markers,
            },
        },
        "bounds": {
            "known_bad_all_refused_own_rule": {
                "bound": "10 of 10",
                "observed": bad_ok,
                "verdict": "HIT" if bad_ok == len(KNOWN_BAD) else "MISSED",
            },
            "good_all_pass": {
                "bound": "30 of 30",
                "observed": good_ok,
                "verdict": "HIT" if good_ok == len(GOOD) else "MISSED",
            },
            "integrity_at_most_2_refused": {
                "bound": f"<= {INTEGRITY_MAX_REFUSED} of {INTEGRITY_N}",
                "observed": refused_n,
                "verdict": "HIT" if refused_n <= INTEGRITY_MAX_REFUSED else "MISSED",
            },
        },
    }
    out = _common.write_result("gates.json", doc)
    print(f"wrote {out}")
    for name, bound in doc["bounds"].items():
        print(f"{name}: {bound['observed']} ({bound['bound']}) {bound['verdict']}")
    print(f"integrity refused by rule: {dict(by_rule)}")
    print(
        f"integrity read: {read['path']} sha256 {read['sha256']} at {read['read_at']}"
    )


if __name__ == "__main__":
    main()
