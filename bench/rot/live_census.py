"""The T1 live-store census — the shipped verdict graded on declared claims.

Implements the T1 live-store declaration, committed one sha
before this file. The declaration's thresholds are encoded here verbatim
and graded mechanically into the artifact's `predictions` block; the sha
ordering (declaration, implementation, run) is the enforcement record.

READ-ONLY by contract: memory files and event shards are opened for
reading only, no locks are taken, no events are written, nothing is
mutated. Every verdict-shaped quantity is computed by the SHIPPED
machinery — `claims.load_claims` / `claims.check_claim`, and the same
`detect_path_drift` / `compute_verification_status` /
`compute_commit_drift` / `compute_staleness_verdict` composition the
`memory_show` handler runs — never a reimplementation
(the rot-bench notes' standing rule).

One deliberate framing choice, named in the declaration: the verdict for
census B is computed AS A READ FROM THE MEMORY'S OWN WORKTREE would
deliver it (`caller_origin` built from the memory's recorded origin),
because that is the verdict the operator sees where the memory matters.

The claim-carrying cohort transition (census C) is reconstructed from
the event log exactly as: the earliest of (first `verify` event with
non-empty `claims`, first `update` event whose `fields` include
`claims`); a memory whose file carries claims but whose log holds
neither marker is treated as carrying since `created` — write events
record neither id nor claims, so claims-at-write is unrecoverable from
the log and the fallback is named rather than silent.

The committed artifact is AGGREGATES ONLY: counts, rates, buckets,
grades, and a reproducibility pin. No memory ids, no bodies, no claim
strings, no scope names beyond the coarse families
(`this-repo` / `other-repo` / `no-repo`), no paths outside this
repository's own.

    .venv/bin/python bench/rot/live_census.py --out bench/rot/results/live-store-2026-08-14.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent.parent
_SRC = _REPO / "src"
for _p in (str(_SRC), str(_REPO)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from bettermemory import _frontmatter  # noqa: E402
from bettermemory.claims import check_claim, load_claims  # noqa: E402
from bettermemory.config import load_config  # noqa: E402
from bettermemory.eval import compute_eval  # noqa: E402
from bettermemory.events import iter_all_events  # noqa: E402
from bettermemory.origin import Origin  # noqa: E402
from bettermemory.store import TOMBSTONE_DIR  # noqa: E402
from bettermemory.verify import (  # noqa: E402
    compute_commit_drift,
    compute_staleness_verdict,
    compute_verification_status,
    detect_path_drift,
)

from bench.claims import classify_body  # noqa: E402

# ─── Declaration constants, verbatim from T1_LIVE_STORE_DECLARATION.md ───

JOIN_HORIZON = timedelta(days=7)
NOTE_CAP = 500
NOTE_SQUEEZE_FLOOR = 450  # within 50 chars of the cap
# NOTE_CAP stays 500 by design after the shipped cap moved to 800 in
# 5.7.0 (bench/rot/T3_NOTE_CAP_DECISION.md): it is T1's declared
# constant, and grading T-P5 against it is what keeps re-runs
# comparable. A future note-pressure census under the 800 contract
# declares fresh constants in its own declaration.
P3_COHORT_FLOOR = 10  # resolved escalated deliveries per cohort
P4_CLASSIFIABLE_FLOOR = 8
CALIBRATION_TURN_FLOOR = 300
CALIBRATION_MISS_FLOOR = 10
P2_BAR = 0.01
P3_BAR = 2.0
P4_BAR = 0.25
P5_BAR = 0.10
CRITERION_CLAIMS_FLOOR = 200
CRITERION_DELIVERIES_FLOOR = 20


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(raw: Any) -> datetime | None:
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    if not isinstance(raw, str):
        return None
    try:
        ts = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _this_repo_url(repo_root: Path) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_root), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except OSError:
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def _scope_family(origin: dict[str, Any] | None, this_repo: str) -> str:
    repo = (origin or {}).get("repo") or ""
    if not repo:
        return "no-repo"
    return "this-repo" if repo == this_repo else "other-repo"


def _load_memories(store: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(store.glob("*.md")):
        post = _frontmatter.load(path)
        meta = dict(post.metadata)
        rows.append(
            {
                "meta": meta,
                "body": post.content,
                "id": str(meta.get("id") or path.stem),
                "origin": meta.get("origin")
                if isinstance(meta.get("origin"), dict)
                else None,
                "claims_raw": list(meta.get("claims") or []),
                "verified_paths": list(meta.get("verified_paths") or []),
                "absent_paths": list(meta.get("verified_absent_paths") or []),
                "created": _parse_ts(meta.get("created")),
                "last_verified_at": _parse_ts(meta.get("last_verified_at")),
            }
        )
    return rows


def _store_manifest_sha(store: Path) -> str:
    acc = hashlib.sha256()
    for path in sorted(store.glob("*.md")):
        acc.update(path.name.encode())
        acc.update(hashlib.sha256(path.read_bytes()).hexdigest().encode())
    return acc.hexdigest()


def _shard_stats(store: Path) -> list[dict[str, Any]]:
    stats = []
    for shard in sorted(store.glob(".events.*.jsonl")):
        data = shard.read_bytes()
        stats.append(
            {
                "shard": shard.name,
                "bytes": len(data),
                "lines": data.count(b"\n"),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
    return stats


def _tombstoned_ids(store: Path) -> set[str]:
    ids: set[str] = set()
    tdir = store / TOMBSTONE_DIR
    if not tdir.is_dir():
        return ids
    for path in sorted(tdir.glob("*.md")):
        try:
            post = _frontmatter.load(path)
        except Exception:
            continue
        mid = post.metadata.get("id")
        if isinstance(mid, str):
            ids.add(mid)
    return ids


# ─── Census A: population ───


def census_population(rows: list[dict[str, Any]], this_repo: str) -> dict[str, Any]:
    kinds: Counter[str] = Counter()
    parse_failures = 0
    claim_carrying = 0
    claims_total = 0
    checkable_undeclared = 0
    family: Counter[str] = Counter()
    for row in rows:
        family[_scope_family(row["origin"], this_repo)] += 1
        raw = row["claims_raw"]
        if raw:
            claim_carrying += 1
            try:
                parsed = load_claims(raw)
            except ValueError:
                parse_failures += 1
                row["claims"] = []
                continue
            row["claims"] = parsed
            claims_total += len(parsed)
            for claim in parsed:
                kinds[claim.kind] += 1
        else:
            row["claims"] = []
            if classify_body(row["body"]):
                checkable_undeclared += 1
    return {
        "active_memories": len(rows),
        "claim_carrying_memories": claim_carrying,
        "claims_total": claims_total,
        "claims_by_kind": dict(sorted(kinds.items())),
        "claim_parse_failures": parse_failures,
        "checkable_but_undeclared_memories": checkable_undeclared,
        "memories_by_family": dict(sorted(family.items())),
    }


# ─── Census B: claim truth against the delivered verdict, now ───


def _verdict_from_own_worktree(
    row: dict[str, Any], stale_days: int, now: datetime
) -> str:
    origin = row["origin"] or {}
    worktree = origin.get("worktree_root")
    caller = Origin(
        cwd=origin.get("cwd"),
        repo=origin.get("repo"),
        branch=origin.get("branch"),
        worktree_root=worktree,
    )
    drift = detect_path_drift(
        row["body"],
        verified_paths=row["verified_paths"],
        absent_paths=row["absent_paths"],
        worktree_root=worktree,
    )
    verification = compute_verification_status(
        row["last_verified_at"], now=now, stale_after_days=stale_days
    )
    commit_drift = compute_commit_drift(
        row["last_verified_at"],
        origin.get("repo"),
        caller_origin=caller,
        verified_paths=row["verified_paths"],
        body=row["body"],
        claims=row["claims_raw"],
    )
    return compute_staleness_verdict(
        verification=verification,
        path_drift_missing=len(drift.claim_anchored_missing),
        commit_drift_count=(
            commit_drift.commits_since_verify if commit_drift is not None else None
        ),
    )


def census_claim_truth(
    rows: list[dict[str, Any]], this_repo: str, stale_days: int, now: datetime
) -> dict[str, Any]:
    classifiable = 0
    unclassifiable = 0
    false_claims = 0
    false_while_fresh = 0
    false_by_kind: Counter[str] = Counter()
    false_while_fresh_by_family: Counter[str] = Counter()
    memories_with_false_claims = 0
    for row in rows:
        if not row["claims"]:
            continue
        worktree = (row["origin"] or {}).get("worktree_root")
        if not worktree or not Path(worktree).is_dir():
            unclassifiable += len(row["claims"])
            continue
        verdict = _verdict_from_own_worktree(row, stale_days, now)
        any_false = False
        for claim in row["claims"]:
            classifiable += 1
            reason = check_claim(claim, Path(worktree))
            if reason is None:
                continue
            any_false = True
            false_claims += 1
            false_by_kind[claim.kind] += 1
            if verdict == "fresh":
                false_while_fresh += 1
                false_while_fresh_by_family[
                    _scope_family(row["origin"], this_repo)
                ] += 1
        if any_false:
            memories_with_false_claims += 1
    rate = (false_while_fresh / classifiable) if classifiable else None
    return {
        "claims_classifiable": classifiable,
        "claims_unclassifiable_dead_worktree": unclassifiable,
        "claims_false": false_claims,
        "claims_false_by_kind": dict(sorted(false_by_kind.items())),
        "memories_with_false_claims": memories_with_false_claims,
        "false_while_fresh": false_while_fresh,
        "false_while_fresh_rate": rate,
        "false_while_fresh_by_family": dict(
            sorted(false_while_fresh_by_family.items())
        ),
    }


# ─── Census C: the outcome timeline ───


def _claim_transition_moments(
    events: list[dict[str, Any]], rows: list[dict[str, Any]]
) -> dict[str, datetime]:
    """Memory id -> the instant it became claim-carrying, per the declared rule."""
    moments: dict[str, datetime] = {}
    for event in events:
        kind = event.get("kind")
        mid = event.get("id")
        ts = _parse_ts(event.get("ts"))
        if not isinstance(mid, str) or ts is None:
            continue
        if kind == "verify" and event.get("claims"):
            moments.setdefault(mid, ts)
        elif kind == "update" and "claims" in (event.get("fields") or []):
            moments.setdefault(mid, ts)
    for row in rows:
        if (
            row["claims_raw"]
            and row["id"] not in moments
            and row["created"] is not None
        ):
            moments[row["id"]] = row["created"]
    return moments


def census_timeline(
    events: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    this_repo: str,
) -> dict[str, Any]:
    moments = _claim_transition_moments(events, rows)
    family_by_id = {row["id"]: _scope_family(row["origin"], this_repo) for row in rows}
    mutations: dict[str, list[tuple[datetime, str]]] = {}
    for event in events:
        kind = event.get("kind")
        if kind not in ("update", "verify"):
            continue
        mid = event.get("id")
        ts = _parse_ts(event.get("ts"))
        if isinstance(mid, str) and ts is not None:
            mutations.setdefault(mid, []).append((ts, kind))
    for seq in mutations.values():
        seq.sort(key=lambda pair: pair[0])

    cohorts: dict[str, dict[str, Any]] = {
        name: {
            "escalated_deliveries": 0,
            "repairs": 0,
            "holds": 0,
            "unresolved": 0,
            "by_family": Counter(),
        }
        for name in ("claim_carrying", "claim_less")
    }
    for event in events:
        if event.get("kind") != "show":
            continue
        if event.get("staleness_verdict") != "spot_check_required":
            continue
        mid = event.get("id")
        ts = _parse_ts(event.get("ts"))
        if not isinstance(mid, str) or ts is None:
            continue
        carrying = mid in moments and moments[mid] <= ts
        cohort = cohorts["claim_carrying" if carrying else "claim_less"]
        cohort["escalated_deliveries"] += 1
        cohort["by_family"][family_by_id.get(mid, "no-repo")] += 1
        outcome = "unresolved"
        for mut_ts, mut_kind in mutations.get(mid, []):
            if mut_ts <= ts:
                continue
            if mut_ts - ts > JOIN_HORIZON:
                break
            if mut_kind == "update":
                outcome = "repairs"
                break
            outcome = "holds"
        cohort[outcome if outcome != "unresolved" else "unresolved"] += 1

    out: dict[str, Any] = {"join_horizon_days": JOIN_HORIZON.days}
    for name, cohort in cohorts.items():
        resolved = cohort["repairs"] + cohort["holds"]
        out[name] = {
            "escalated_deliveries": cohort["escalated_deliveries"],
            "repairs": cohort["repairs"],
            "holds": cohort["holds"],
            "unresolved": cohort["unresolved"],
            "resolved": resolved,
            "repair_follow_rate": (cohort["repairs"] / resolved) if resolved else None,
            "by_family": dict(sorted(cohort["by_family"].items())),
        }
    return out


# ─── Census D: the absent-attestation cohort ───


def census_absent(rows: list[dict[str, Any]]) -> dict[str, Any]:
    memories_with_absent = 0
    attestations = 0
    historical = 0
    locality = 0
    reappeared = 0
    unclassifiable = 0
    for row in rows:
        paths = row["absent_paths"]
        if not paths:
            continue
        memories_with_absent += 1
        worktree_raw = (row["origin"] or {}).get("worktree_root")
        worktree: Path | None = Path(worktree_raw) if worktree_raw else None
        if worktree is not None and not worktree.is_dir():
            worktree = None
        for raw in paths:
            attestations += 1
            candidate = Path(raw).expanduser()
            if not candidate.is_absolute():
                if worktree is None:
                    unclassifiable += 1
                    continue
                candidate = worktree / raw
            if candidate.exists():
                reappeared += 1
                continue
            if worktree is None or not candidate.is_relative_to(worktree):
                unclassifiable += 1
                continue
            rel = candidate.relative_to(worktree).as_posix()
            try:
                out = subprocess.run(
                    ["git", "-C", str(worktree), "log", "--oneline", "-1", "--", rel],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
            except OSError:
                unclassifiable += 1
                continue
            if out.returncode != 0:
                unclassifiable += 1
            elif out.stdout.strip():
                historical += 1
            else:
                locality += 1
    classifiable = historical + locality
    return {
        "memories_with_absent_attestations": memories_with_absent,
        "attestations_total": attestations,
        "classifiable": classifiable,
        "historical": historical,
        "locality": locality,
        "reappeared": reappeared,
        "unclassifiable": unclassifiable,
        "historical_share": (historical / classifiable) if classifiable else None,
    }


# ─── Census E: note-cap pressure ───


def census_notes(events: list[dict[str, Any]]) -> dict[str, Any]:
    lengths: list[int] = []
    for event in events:
        if event.get("kind") not in ("verify", "use"):
            continue
        note = event.get("note")
        if isinstance(note, str) and note:
            lengths.append(len(note))
    buckets = Counter()
    for n in lengths:
        buckets[f"{(n // 100) * 100:03d}-{(n // 100) * 100 + 99:03d}"] += 1
    squeezed = sum(1 for n in lengths if n >= NOTE_SQUEEZE_FLOOR)
    return {
        "notes_total": len(lengths),
        "cap": NOTE_CAP,
        "length_histogram_by_100": dict(sorted(buckets.items())),
        "squeezed_ge_450": squeezed,
        "squeezed_share": (squeezed / len(lengths)) if lengths else None,
    }


# ─── Census F: calibration accumulation ───


def census_calibration(
    events: list[dict[str, Any]], tombstoned: set[str], now: datetime
) -> dict[str, Any]:
    report = compute_eval([], events, now=now, since=None, tombstoned_ids=tombstoned)
    unlocked = (
        report.turns_audited >= CALIBRATION_TURN_FLOOR
        and report.silent_misses >= CALIBRATION_MISS_FLOOR
    )
    return {
        "post_cutoff_turns_audited": report.turns_audited,
        "post_cutoff_unacked_misses": report.silent_misses,
        "turn_floor": CALIBRATION_TURN_FLOOR,
        "miss_floor": CALIBRATION_MISS_FLOOR,
        "successor_rule_unlocked": unlocked,
    }


# ─── Predictions ───


def grade_predictions(
    a: dict[str, Any],
    b: dict[str, Any],
    c: dict[str, Any],
    d: dict[str, Any],
    e: dict[str, Any],
) -> dict[str, Any]:
    preds: dict[str, Any] = {}
    preds["T-P1"] = {
        "statement": "zero stored-claim parse failures",
        "observed": a["claim_parse_failures"],
        "grade": "hit" if a["claim_parse_failures"] == 0 else "MISSED",
    }
    rate = b["false_while_fresh_rate"]
    preds["T-P2"] = {
        "statement": f"false-while-fresh <= {P2_BAR} of classifiable claims",
        "observed": rate,
        "grade": (
            "ungraded_no_classifiable_claims"
            if rate is None
            else ("hit" if rate <= P2_BAR else "MISSED")
        ),
    }
    cc, cl = c["claim_carrying"], c["claim_less"]
    floors_met = cc["resolved"] >= P3_COHORT_FLOOR and cl["resolved"] >= P3_COHORT_FLOOR
    if not floors_met:
        grade = "underpowered"
        multiplier = None
    else:
        cc_rate, cl_rate = cc["repair_follow_rate"], cl["repair_follow_rate"]
        if cl_rate == 0:
            multiplier = None
            grade = "hit" if cc_rate > 0 else "MISSED"
        else:
            multiplier = cc_rate / cl_rate
            grade = "hit" if multiplier >= P3_BAR else "MISSED"
    preds["T-P3"] = {
        "statement": (
            f"claim-carrying repair-follow rate >= {P3_BAR}x claim-less, "
            f"floors {P3_COHORT_FLOOR} resolved per cohort"
        ),
        "observed_multiplier": multiplier,
        "floors_met": floors_met,
        "grade": grade,
    }
    share = d["historical_share"]
    if d["classifiable"] < P4_CLASSIFIABLE_FLOOR:
        grade4 = "underpowered"
    else:
        grade4 = "hit" if share is not None and share >= P4_BAR else "MISSED"
    preds["T-P4"] = {
        "statement": (
            f"historical share >= {P4_BAR} of classifiable absent attestations, "
            f"floor {P4_CLASSIFIABLE_FLOOR}"
        ),
        "observed": share,
        "grade": grade4,
    }
    sq = e["squeezed_share"]
    preds["T-P5"] = {
        "statement": f"notes with length >= {NOTE_SQUEEZE_FLOOR} are >= {P5_BAR} of notes",
        "observed": sq,
        "grade": (
            "ungraded_no_notes" if sq is None else ("hit" if sq >= P5_BAR else "MISSED")
        ),
    }
    return preds


def criterion_progress(b: dict[str, Any], c: dict[str, Any]) -> dict[str, Any]:
    claims_ok = b["claims_classifiable"] >= CRITERION_CLAIMS_FLOOR
    deliveries_ok = c["claim_carrying"]["resolved"] >= CRITERION_DELIVERIES_FLOOR
    status = "open_floors_unmet"
    bars: dict[str, Any] = {}
    if claims_ok and deliveries_ok:
        rate = b["false_while_fresh_rate"]
        cc_rate = c["claim_carrying"]["repair_follow_rate"]
        cl_rate = c["claim_less"]["repair_follow_rate"]
        bar1 = rate is not None and rate <= P2_BAR
        if cl_rate in (None, 0):
            bar2 = cc_rate is not None and cc_rate > 0
        else:
            bar2 = cc_rate is not None and (cc_rate / cl_rate) >= P3_BAR
        bars = {"silent_rot_bar": bar1, "multiplier_bar": bar2}
        status = "met" if (bar1 and bar2) else "MISSED"
    return {
        "claims_floor": CRITERION_CLAIMS_FLOOR,
        "claims_classifiable": b["claims_classifiable"],
        "deliveries_floor": CRITERION_DELIVERIES_FLOOR,
        "resolved_claim_carrying_deliveries": c["claim_carrying"]["resolved"],
        "status": status,
        **bars,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="The T1 live-store census — the shipped verdict graded on declared claims."
    )
    parser.add_argument("--store", default=str(Path.home() / ".claude-memory"))
    parser.add_argument("--out", default=None)
    args = parser.parse_args()
    store = Path(args.store).expanduser()
    if not store.is_dir():
        print(f"store not found: {store}", file=sys.stderr)
        return 2

    now = _utcnow()
    this_repo = _this_repo_url(_REPO)
    config = load_config()
    stale_days = config.behavior.verification_stale_days

    rows = _load_memories(store)
    events = list(iter_all_events(store))
    tombstoned = _tombstoned_ids(store)

    a = census_population(rows, this_repo)
    b = census_claim_truth(rows, this_repo, stale_days, now)
    c = census_timeline(events, rows, this_repo)
    d = census_absent(rows)
    e = census_notes(events)
    f = census_calibration(events, tombstoned, now)
    predictions = grade_predictions(a, b, c, d, e)
    criterion = criterion_progress(b, c)

    head = subprocess.run(
        ["git", "-C", str(_REPO), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout.strip()
    artifact = {
        "instrument": "T1 live-store census",
        "declaration": "bench/rot/T1_LIVE_STORE_DECLARATION.md",
        "provenance": {
            "run_ts": now.isoformat(),
            "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "repo_head": head,
            "verification_stale_days": stale_days,
            "store_manifest_sha256": _store_manifest_sha(store),
            "event_shards": _shard_stats(store),
            "tombstone_count": len(tombstoned),
        },
        "a_population": a,
        "b_claim_truth": b,
        "c_timeline": c,
        "d_absent_cohort": d,
        "e_notes": e,
        "f_calibration": f,
        "predictions": predictions,
        "lane_t_criterion_v1": criterion,
    }
    rendered = json.dumps(artifact, indent=2, sort_keys=False)
    if args.out:
        Path(args.out).write_text(rendered + "\n")
        print(f"wrote {args.out}")
    grades = ", ".join(f"{k}:{v['grade']}" for k, v in predictions.items())
    print(f"predictions — {grades}")
    print(f"criterion v1 — {criterion['status']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
