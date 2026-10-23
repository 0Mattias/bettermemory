"""OC-P1..P6: a capture answers its relations once, at the end.

A capture write that carries review={"unsafe", "secret"} commits in one
call with its unanswered related ids as `relations_pending`, and one
closing call per batch, memory_admin(action="relations"), answers them.
Graded on scratch copies of tests/fixtures/v8/store (migrated with
`latency.import_copy`) and on constructed statements only; never the live
store, never anything under ~/.cache/bettermemory-v9.

  OC-P1  scripted captures of 1, 3 and 5 writes (the first bodies of the
         CAPS-P2 good set) take N+1 tool calls with pre-answered scores,
         against 2N on the two-call review (the same capture writes sent
         without scores, which this tree serves unchanged). A capture none
         of whose writes has a related statement needs no closing call, so
         it takes N: the bound is at most N+1, and each row records whether
         it was exactly N+1.
  OC-P2  the 30 CAPS-P2 good bodies, plus constructed cue cases, through
         both paths with the same scripted relation answers (update,
         conflict, restates, unrelated, and unrelated on a cue-linked
         target): the end state (active bodies, supersedes links, pairs
         filed with their detector, update_suggested) is equal, except
         that the restating write is a tombstoned member on the deferred
         path where the two-call path never wrote it.
  OC-P3  10 scripted writes with secret or unsafe at or above tau write
         nothing on the deferred path.
  OC-P4  a batch settled without the closing call keeps its commits, files
         nothing from relations, and its capture_close names every pending
         id (count = the sum of the related-set sizes the writes returned).
  OC-P5  `capture undo --apply` after a closing call that tombstoned,
         linked and filed restores the before mirror (the CAPS-P3 check:
         every file but .mirror.json and the batch's tombstones identical)
         and `log verify` is ok.
  OC-P6  tools/list bytes and the instructions sha256 at HEAD against the
         baseline revision (its src from `git archive`), through the shim
         and the in-process server: tools/list grows by at most 400 bytes
         and the instructions are byte-identical.

Every store is keyed by body text for comparison, since the two paths mint
different ids. Only counts, statuses, shas and constructed or fixture
bodies leave the run; every scratch directory is removed at the end.

    python bench/capture/one_call.py [--baseline-rev 28eae04c]
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common  # noqa: E402

FIXTURE = _common.ROOT / "tests" / "fixtures" / "v8" / "store"
SCOPES = ["projects:acme"]
SCORES = {"unsafe": 0.0, "secret": 0.0, "standing": 0.0}
SIZES = (1, 3, 5)
SURFACE_BOUND = 400

# Constructed statements seeded into both copies before a batch, and the
# writes that touch them. The cue code links PORT_NEW to PORT_OLD (a
# change cue and two ports of one shape); the review then disputes it.
PORT_OLD = "The staging API listens on port 8443 behind the gateway."
PORT_NEW = "The staging API moved to port 9443 behind the gateway."
BACKUP_OLD = "Nightly database backups are handled by Restic."
BACKUP_NEW = "Backups moved to Borg for the archive volumes."
DEPLOY_OLD = "Staging deploys run from the deploy-staging branch."
DEPLOY_NEW = "Staging deploys run from the main branch behind a manual gate."
RESTATE = "The acme databases are backed up every night with Restic."
# A record too long to be claim-sized: an update answer on it can only be
# suggested, never linked.
LONG_OLD = " ".join(
    f"The acme-search reindex job step {n} runs on worker-3 against the "
    "search cluster and reports to the reindex dashboard."
    for n in range(6)
)
LONG_NEW = "The acme-search reindex job runs on worker-5 against the search cluster."

# OC-P2's scripted answer for the first related id of good body i; every
# other related id is answered unrelated. The restating write is the last
# of the batch, so no later write can see it on either path.
CYCLE = ("update", "conflict", "unrelated")

# OC-P3: (unsafe, secret), each with a score at or above the shipped tau.
REFUSALS = (
    (0.0, 0.3),
    (0.0, 0.5),
    (0.0, 0.9),
    (0.1, 1.0),
    (0.29, 0.31),
    (0.3, 0.0),
    (0.5, 0.0),
    (0.9, 0.1),
    (1.0, 0.0),
    (0.31, 0.29),
)


class Counted(_common.World):
    """A World that counts its tool calls."""

    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.calls = 0

    async def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        return await super().call(name, arguments)

    def seed(self, content: str) -> str:
        return self.store.write(content=content, scopes=SCOPES).id

    def body(self, memory_id: str) -> str:
        try:
            return self.store.load_one(memory_id).body.strip()
        except Exception:  # noqa: BLE001 - a tombstoned record
            return self.store.load_tombstone(memory_id).body.strip()

    def end_state(self) -> dict[str, Any]:
        """The store's end state keyed by body."""
        from bettermemory.conflicts import ConflictQueue

        active = self.store.load_all()
        links = sorted(
            [self.body(m.id), self.body(link.target_id), _note(link.note)]
            for m in active
            for link in m.links
        )
        conflicts = sorted(
            [self.body(c.a_id), self.body(c.b_id), c.detector or ""]
            for c in ConflictQueue(self.store).load()
        )
        return {
            "active": sorted(m.body.strip() for m in active),
            "links": links,
            "conflicts": conflicts,
        }


def _note(note: str | None) -> str:
    from bettermemory.review import REVIEW_LINK_NOTE

    if note == REVIEW_LINK_NOTE:
        return "review"
    return "cue" if (note or "").startswith("set at write time") else "other"


def _fresh(scratch: Path, name: str) -> Counted:
    from latency import import_copy  # type: ignore[import-not-found]

    store_dir = scratch / name
    import_copy(FIXTURE, store_dir)
    return Counted(store_dir)


def _good() -> tuple[str, ...]:
    from gates import GOOD  # type: ignore[import-not-found]

    return tuple(GOOD)


async def _two_call(
    world: Counted, batch: Any, body: str, labels: Any
) -> dict[str, Any]:
    """The two-call review: a capture write, then its answers re-sent."""
    args = {"content": body, "scopes": SCOPES, "source": f"capture:{batch.token}"}
    reply = await world.call("memory_write", args)
    for _ in range(2):
        if reply.get("status") != "review":
            break
        shown = [e["id"] for e in reply["review"]["related"]]
        reply = await world.call(
            "memory_write",
            {
                **args,
                "review": {
                    "review_id": reply["review"]["review_id"],
                    "relation": labels(shown),
                    **SCORES,
                },
            },
        )
    return reply


async def _scored(world: Counted, batch: Any, body: str, **scores: Any) -> dict:
    return await world.call(
        "memory_write",
        {
            "content": body,
            "scopes": SCOPES,
            "source": f"capture:{batch.token}",
            "review": {**SCORES, **scores},
        },
    )


async def _close_relations(world: Counted, answers: dict[str, Any]) -> dict:
    return await world.call(
        "memory_admin", {"action": "relations", "relations": answers}
    )


def _pending(reply: dict[str, Any]) -> list[str]:
    return [e["id"] for e in reply.get("relations_pending") or []]


# ---------------------------------------------------------------------------
# OC-P1
# ---------------------------------------------------------------------------


async def oc_p1(scratch: Path) -> dict[str, Any]:
    good = _good()
    rows = []
    for n in SIZES:
        bodies = good[:n]
        base = _fresh(scratch, f"p1-base-{n}")
        batch = base.open()
        base_status = [
            (
                await _two_call(
                    base, batch, b, lambda ids: dict.fromkeys(ids, "unrelated")
                )
            )["status"]
            for b in bodies
        ]
        base.close(batch)
        deferred = _fresh(scratch, f"p1-deferred-{n}")
        batch = deferred.open()
        replies = [await _scored(deferred, batch, b) for b in bodies]
        answers = {
            r["id"]: dict.fromkeys(_pending(r), "unrelated")
            for r in replies
            if _pending(r)
        }
        closing = await _close_relations(deferred, answers) if answers else {}
        deferred.close(batch)
        rows.append(
            {
                "n": n,
                "base_calls": base.calls,
                "base_committed": base_status.count("committed"),
                "deferred_calls": deferred.calls,
                "deferred_committed": [r["status"] for r in replies].count("committed"),
                "pending_ids": sum(len(_pending(r)) for r in replies),
                "closing_left": len(closing.get("relations_pending") or {}),
                # A capture whose writes all have empty related sets needs
                # no closing call, so N+1 is the bound and N its floor.
                "exactly_n_plus_1": deferred.calls == n + 1,
                "hit": base.calls == 2 * n and deferred.calls <= n + 1,
            }
        )
        base.store.close()
        deferred.store.close()
    return {
        "bound": "deferred at most N+1 calls (N+1 when any relation is "
        "pending), two-call 2N, all committed",
        "rows": rows,
        "verdict": "HIT"
        if all(
            r["hit"] and r["base_committed"] == r["n"] == r["deferred_committed"]
            for r in rows
        )
        else "MISSED",
    }


# ---------------------------------------------------------------------------
# OC-P2
# ---------------------------------------------------------------------------


def _p2_bodies() -> list[tuple[str, str]]:
    """(body, scripted label for its first related id). The cue cases
    first, the good set next, the restating write last."""
    rows: list[tuple[str, str]] = [
        (PORT_NEW, "unrelated"),  # disputes the cue link: review-disagrees
        (BACKUP_NEW, "update"),
        (DEPLOY_NEW, "conflict"),
        (LONG_NEW, "update"),  # a long target: update_suggested
    ]
    rows += [(b, CYCLE[i % len(CYCLE)]) for i, b in enumerate(_good())]
    rows.append((RESTATE, "restates"))
    return rows


def _labeller(world: Counted, first: str) -> Any:
    def labels(ids: list[str]) -> dict[str, str]:
        return {i: first if n == 0 else "unrelated" for n, i in enumerate(ids)}

    return labels


async def oc_p2(scratch: Path) -> dict[str, Any]:
    plan = _p2_bodies()
    seeds = (PORT_OLD, BACKUP_OLD, DEPLOY_OLD, LONG_OLD)

    two = _fresh(scratch, "p2-two-call")
    for s in seeds:
        two.seed(s)
    batch = two.open()
    two_related: list[list[str]] = []
    two_suggested: dict[str, list[str]] = {}
    two_status = []
    for body, first in plan:
        args = {"content": body, "scopes": SCOPES, "source": f"capture:{batch.token}"}
        shown_reply = await two.call("memory_write", args)
        if shown_reply.get("status") != "review":
            raise SystemExit(f"OC-P2 {body!r}: gate status {shown_reply.get('status')}")
        shown = [e["id"] for e in shown_reply["review"]["related"]]
        two_related.append([two.body(i) for i in shown])
        reply = await two.call(
            "memory_write",
            {
                **args,
                "review": {
                    "review_id": shown_reply["review"]["review_id"],
                    "relation": _labeller(two, first)(shown),
                    **SCORES,
                },
            },
        )
        two_status.append(reply.get("status"))
        if reply.get("update_suggested"):
            two_suggested[body] = sorted(two.body(i) for i in reply["update_suggested"])
    two.close(batch)

    deferred = _fresh(scratch, "p2-deferred")
    for s in seeds:
        deferred.seed(s)
    batch = deferred.open()
    replies = []
    deferred_related: list[list[str]] = []
    for body, _first in plan:
        reply = await _scored(deferred, batch, body)
        replies.append(reply)
        deferred_related.append([deferred.body(i) for i in _pending(reply)])
    answers = {
        r["id"]: _labeller(deferred, first)(_pending(r))
        for r, (_body, first) in zip(replies, plan, strict=True)
        if _pending(r)
    }
    closing = await _close_relations(deferred, answers)
    deferred.close(batch)
    deferred_suggested = {
        deferred.body(member): sorted(
            deferred.body(i) for i in result["update_suggested"]
        )
        for member, result in closing["applied"].items()
        if result.get("update_suggested")
    }
    tombstoned = sorted(
        deferred.body(member)
        for member, result in closing["applied"].items()
        if result.get("tombstoned")
    )

    a, b = two.end_state(), deferred.end_state()
    same = {
        "active": a["active"] == b["active"],
        "links": a["links"] == b["links"],
        "conflicts": a["conflicts"] == b["conflicts"],
        "update_suggested": two_suggested == deferred_suggested,
        "related_sets": two_related == deferred_related,
    }
    labels_used = sorted({first for _b, first in plan})
    detectors = sorted({c[2] for c in b["conflicts"]})
    restated_not_written = [
        body
        for (body, first), s in zip(plan, two_status, strict=True)
        if s == "duplicate"
    ]
    out = {
        "writes": len(plan),
        "labels_scripted": labels_used,
        "two_call_status": {s: two_status.count(s) for s in sorted(set(two_status))},
        "deferred_status": {
            s: [r["status"] for r in replies].count(s)
            for s in sorted({r["status"] for r in replies})
        },
        "closing_left_pending": len(closing.get("relations_pending") or {}),
        "end_state": {
            "active": len(b["active"]),
            "links": {
                k: sum(1 for x in b["links"] if x[2] == k) for k in ("cue", "review")
            },
            "conflicts_by_detector": {
                d: sum(1 for c in b["conflicts"] if c[2] == d) for d in detectors
            },
            "update_suggested": sum(len(v) for v in deferred_suggested.values()),
        },
        "equal": same,
        "restating_write": {
            "two_call_not_written": restated_not_written,
            "deferred_tombstoned_member": tombstoned,
            "match": restated_not_written == tombstoned == [RESTATE],
        },
    }
    covered = {
        "update_link": out["end_state"]["links"]["review"] > 0,
        "update_suggested": out["end_state"]["update_suggested"] > 0,
        "conflict": out["end_state"]["conflicts_by_detector"].get("review", 0) > 0,
        "cue_disagree": out["end_state"]["conflicts_by_detector"].get(
            "review-disagrees", 0
        )
        > 0,
        "restates": bool(tombstoned),
    }
    out["covered"] = covered
    out["bound"] = (
        "end state equal on both paths, the restating write a tombstoned "
        "member on the deferred path only"
    )
    out["verdict"] = (
        "HIT"
        if all(same[k] for k in ("active", "links", "conflicts", "update_suggested"))
        and out["restating_write"]["match"]
        else "MISSED"
    )
    two.store.close()
    deferred.store.close()
    return out


# ---------------------------------------------------------------------------
# OC-P3 and OC-P4
# ---------------------------------------------------------------------------


async def oc_p3(scratch: Path) -> dict[str, Any]:
    from bettermemory import capture

    world = _fresh(scratch, "p3")
    before = len(world.store.load_all())
    rows = []
    good = _good()
    for i, (unsafe, secret) in enumerate(REFUSALS):
        batch = world.open()
        reply = await _scored(world, batch, good[i], unsafe=unsafe, secret=secret)
        rows.append(
            {
                "unsafe": unsafe,
                "secret": secret,
                "status": reply.get("status"),
                "members": len(capture.members(world.store, batch.batch)),
            }
        )
        world.close(batch, "failed")
    after = len(world.store.load_all())
    world.store.close()
    nothing = after == before and all(r["members"] == 0 for r in rows)
    refused = all(
        r["status"] in ("credential_warning", "instruction_warning") for r in rows
    )
    return {
        "bound": "10 of 10 refused, nothing written",
        "rows": rows,
        "memories_before": before,
        "memories_after": after,
        "verdict": "HIT" if nothing and refused and len(rows) == 10 else "MISSED",
    }


async def oc_p4(scratch: Path) -> dict[str, Any]:
    from bettermemory import capture, capture_review
    from bettermemory.conflicts import ConflictQueue

    world = _fresh(scratch, "p4")
    conflicts_before = len(ConflictQueue(world.store).load())
    batch = world.open()
    replies = [await _scored(world, batch, b) for b in _good()[:10]]
    world.close(batch)
    close = capture.closes(world.store)[batch.batch]
    returned = sum(len(_pending(r)) for r in replies)
    recorded = sum(len(v) for v in close.relations_pending.values())
    ids_match = {r["id"]: tuple(_pending(r)) for r in replies if _pending(r)} == dict(
        close.relations_pending
    )
    queued = ConflictQueue(world.store).load()
    from_relations = sum(
        1 for c in queued if c.detector in ("review", "review-disagrees")
    )
    active = {m.id for m in world.store.load_all()}
    kept = sum(1 for r in replies if r.get("id") in active)
    (row,) = [
        r
        for r in capture_review.review(world.store)["batches"]
        if r["batch"] == batch.batch
    ]
    world.store.close()
    return {
        "bound": "commits kept, nothing filed from relations, close names every pending id",
        "writes": len(replies),
        "committed_kept": kept,
        "pending_returned": returned,
        "pending_on_close": recorded,
        "ids_match": ids_match,
        "capture_review_pending": row["relations_pending"],
        "conflicts_before": conflicts_before,
        "conflicts_after": len(queued),
        "filed_from_relations": from_relations,
        "verdict": "HIT"
        if kept == len(replies)
        and ids_match
        and recorded == returned == row["relations_pending"]
        and from_relations == 0
        else "MISSED",
    }


# ---------------------------------------------------------------------------
# OC-P5
# ---------------------------------------------------------------------------


def _files(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def _mirror(dest: Path, store_file: Path) -> None:
    proc = _common.cli("export", "--mirror", str(dest), "--store", str(store_file))
    if proc.returncode != 0:
        raise SystemExit(f"export --mirror failed: {proc.stderr[-400:]}")


async def _p5_capture(store_dir: Path) -> tuple[str, list[str], dict[str, Any]]:
    world = Counted(store_dir)
    batch = world.open()
    plan = [(BACKUP_NEW, "update"), (DEPLOY_NEW, "conflict"), (RESTATE, "restates")]
    replies = [await _scored(world, batch, body) for body, _ in plan]
    answers = {
        r["id"]: _labeller(world, first)(_pending(r))
        for r, (_b, first) in zip(replies, plan, strict=True)
        if _pending(r)
    }
    closing = await _close_relations(world, answers)
    world.close(batch)
    world.store.close()
    applied = closing["applied"].values()
    did = {
        "tombstoned": sum(1 for a in applied if a.get("tombstoned")),
        "linked": sum(len(a.get("supersedes") or []) for a in applied),
        "filed": sum(len(a.get("conflicts_filed") or []) for a in applied),
    }
    return batch.batch, [r["id"] for r in replies], did


def oc_p5(scratch: Path) -> dict[str, Any]:
    from latency import import_copy  # type: ignore[import-not-found]

    from bettermemory.mirror import MIRROR_MARKER
    from bettermemory.store import Store
    from bettermemory.v8 import TOMBSTONE_DIR

    store_dir = scratch / "p5"
    import_copy(FIXTURE, store_dir)
    store = Store(store_dir)
    for seed in (BACKUP_OLD, DEPLOY_OLD):
        store.write(content=seed, scopes=SCOPES)
    conflicts_before = sorted(str(c["id"]) for c in store.list_conflicts())
    store.close()
    os.environ["BETTERMEMORY_DIR"] = str(store_dir)
    store_file = store_dir / "memory.sqlite"
    before_dir, after_dir = scratch / "p5-before", scratch / "p5-after"
    _mirror(before_dir, store_file)
    batch_id, ids, did = asyncio.run(_p5_capture(store_dir))
    undo = _common.cli("capture", "undo", batch_id, "--apply", "--json")
    undo_doc = json.loads(undo.stdout) if undo.returncode == 0 else {}
    _mirror(after_dir, store_file)
    old, new = _files(before_dir), _files(after_dir)
    old.pop(MIRROR_MARKER, None)
    new.pop(MIRROR_MARKER, None)
    tombs = {
        k
        for k in new
        if k.startswith(TOMBSTONE_DIR + "/")
        and any(i in k for i in ids)
        and k not in old
    }
    rest = {k: v for k, v in new.items() if k not in tombs}
    differing = sum(old[k] != rest[k] for k in old.keys() & rest.keys())
    added = len(rest.keys() - old.keys())
    removed = len(old.keys() - rest.keys())
    verify = _common.cli("log", "verify", "--json")
    verify_doc = json.loads(verify.stdout) if verify.stdout.strip() else {}
    store = Store(store_dir)
    conflicts_after = sorted(str(c["id"]) for c in store.list_conflicts())
    store.close()
    identical = differing == 0 and added == 0 and removed == 0
    verify_ok = verify.returncode == 0 and verify_doc.get("status") == "ok"
    exercised = all(did[k] > 0 for k in ("tombstoned", "linked", "filed"))
    return {
        "bound": "mirror identical but marker and batch tombstones, log verify ok",
        "closing_call_did": did,
        "undo": {
            "exit": undo.returncode,
            "committed": undo_doc.get("committed"),
            "undone": len(undo_doc.get("to_undo") or []),
            "gone": len(undo_doc.get("gone") or []),
            "own_conflicts": len(undo_doc.get("own_conflicts") or []),
            "refusal": undo_doc.get("refusal_reason"),
        },
        "mirror_files_before": len(old),
        "tombstone_files_added": len(tombs),
        "files_differing": differing,
        "files_added": added,
        "files_removed": removed,
        "conflicts_before": len(conflicts_before),
        "conflicts_after": len(conflicts_after),
        "conflicts_restored": conflicts_after == conflicts_before,
        "log_verify": {"exit": verify.returncode, "status": verify_doc.get("status")},
        "verdict": "HIT"
        if identical and verify_ok and exercised and conflicts_after == conflicts_before
        else "MISSED",
    }


# ---------------------------------------------------------------------------
# OC-P6
# ---------------------------------------------------------------------------


def _archive(rev: str, dest: Path) -> Path:
    """`rev`'s src tree under `dest`, from `git archive` (read-only on the
    repository)."""
    blob = subprocess.run(
        ["git", "-C", str(_common.ROOT), "archive", rev, "src"],
        capture_output=True,
        check=True,
    ).stdout
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(blob)) as tar:
        tar.extractall(dest, filter="data")
    return dest


def oc_p6(scratch: Path, baseline_rev: str) -> dict[str, Any]:
    from surface import probe_tree  # type: ignore[import-not-found]

    base_tree = _archive(baseline_rev, scratch / "baseline-tree")
    head = probe_tree(_common.ROOT)
    base = probe_tree(base_tree)
    rows = {}
    for label in head:
        rows[label] = {
            "tools_list_bytes": {
                "baseline": base[label]["tools_list_bytes"],
                "head": head[label]["tools_list_bytes"],
                "delta": head[label]["tools_list_bytes"]
                - base[label]["tools_list_bytes"],
            },
            "instructions_bytes": head[label]["instructions_bytes"],
            "instructions_sha256": {
                "baseline": base[label]["instructions_sha256"],
                "head": head[label]["instructions_sha256"],
                "identical": head[label]["instructions_sha256"]
                == base[label]["instructions_sha256"]
                and head[label]["instructions_bytes"]
                == base[label]["instructions_bytes"],
            },
            "tool_count": head[label]["tool_count"],
        }
    ok = all(
        0 <= r["tools_list_bytes"]["delta"] <= SURFACE_BOUND
        and r["instructions_sha256"]["identical"]
        for r in rows.values()
    )
    return {
        "bound": f"tools/list delta <= {SURFACE_BOUND} bytes, instructions identical",
        "baseline_rev": baseline_rev,
        "baseline_commit": _common.git("rev-parse", baseline_rev),
        "method": "bench/capture/surface.py probe_tree (toolcost probe_tools, "
        "sandbox HOME, raw JSON-RPC); baseline src from git archive",
        "rows": rows,
        "verdict": "HIT" if ok else "MISSED",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline-rev", default="28eae04c")
    args = parser.parse_args()
    scratch = Path(tempfile.mkdtemp(prefix="caps-one-call-"))
    results: dict[str, Any] = {}
    try:
        _common.scratch_env(scratch)
        results["OC-P1"] = asyncio.run(oc_p1(scratch))
        results["OC-P2"] = asyncio.run(oc_p2(scratch))
        results["OC-P3"] = asyncio.run(oc_p3(scratch))
        results["OC-P4"] = asyncio.run(oc_p4(scratch))
        results["OC-P5"] = oc_p5(scratch)
        results["OC-P6"] = oc_p6(scratch, args.baseline_rev)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    from bettermemory import review

    doc = {
        "kind": "capture-one-call",
        "predictions": sorted(results),
        "commit": _common.git("rev-parse", "HEAD"),
        "run_at": _common.stamp(),
        "fixture": str(FIXTURE.relative_to(_common.ROOT)),
        "tau": {"unsafe": review.TAU_UNSAFE, "secret": review.TAU_SECRET},
        "good_set_sha256": hashlib.sha256(
            "\n".join(_good()).encode("utf-8")
        ).hexdigest(),
        "results": results,
        "scratch_removed": not scratch.exists(),
    }
    out = _common.write_result("one_call.json", doc)
    print(f"wrote {out}")
    for name, row in results.items():
        print(f"{name}: {row['verdict']}")


if __name__ == "__main__":
    main()
