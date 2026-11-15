"""Fixtures 5 and 6: the MCP tool surface and the tools' responses.

- ``tool_surface.json`` (`surface`, ungated): what ``tools/list`` serves
  (each tool's name, description, input and output schema, in served
  order, as the wire spells them) and the server's ``initialize``
  instructions, under the default configuration.
- ``tool_responses_plain.jsonl`` (`plain`, ungated): calls whose answers
  rank nothing and tokenize nothing a Unicode or SQLite version could
  change: the write gates' refusals over an empty store, the error paths,
  the episode journal, the admin summaries of an empty store.
- ``tool_responses.jsonl`` (`responses`, gated): the calls that write,
  relate, rank and search, over a store they fill as they go.

Each record is a request and the structured response the call returned,
or the error text of a call the server refused as a tool error. Calls run
in file order; later ones see what earlier ones wrote.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from collections.abc import Callable, Coroutine, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from bettermemory import location
from bettermemory import review as breview
from bettermemory.config import Config, StorageConfig
from bettermemory.server import build_server
from bettermemory.session import SessionState
from bettermemory.store import Store

from ._common import json_doc, jsonl
from ._sandbox import PLACEHOLDERS, Scrubber, World, world

#: How every server here is built, for the headers.
SETUP = (
    "Config(storage=StorageConfig(directory=<scratch>/mem)), every other "
    "setting at the package default; the store is Store.create(<scratch>/mem/"
    "store) with its key under <scratch>/keys; the server is build_server("
    "config, store, state=SessionState()). Calls are made in process "
    "(server.call_tool), so no identity header is sent: the caller is the "
    "stdio shape, its session the server's default. The process works in a "
    "git checkout <scratch>/alpha: branch main, one commit of README.md "
    "('alpha\\n'), author and committer Fixture <fixture@example.invalid> at "
    "2026-01-01T00:00:00Z, remote origin https://example.invalid/fixture/"
    "alpha.git, core.autocrlf false."
)


def _thresholds() -> dict[str, Any]:
    return {
        "TAU_UNSAFE": breview.TAU_UNSAFE,
        "TAU_SECRET": breview.TAU_SECRET,
        "note": (
            "the review thresholds in force (review.TAU_UNSAFE, TAU_SECRET); "
            "while either is unset (None) memory_write issues no review page"
        ),
    }


@contextmanager
def _server(w: World) -> Iterator[tuple[Any, Store, Config]]:
    store = Store.create(w.memory_dir / "store", keys_dir=w.keys_dir)
    try:
        cfg = Config(storage=StorageConfig(directory=str(w.memory_dir)))
        yield build_server(config=cfg, store=store, state=SessionState()), store, cfg
    finally:
        store.close()


def surface() -> dict[str, bytes]:
    with world() as w, _server(w) as (server, _store, _cfg):
        tools = asyncio.run(server.list_tools())
        doc = {
            "fixture": "tool_surface",
            "read": (
                "tools: the tools/list result in served order, each as the "
                "wire spells it (name, description, inputSchema, outputSchema); "
                "instructions: the initialize result's instructions string; "
                "server_name: serverInfo.name. Default configuration."
            ),
            "server_name": server.name,
            "instructions": server.instructions,
            "tools": [
                t.model_dump(by_alias=True, exclude_none=True, mode="json")
                for t in tools
            ],
        }
    return {"tool_surface.json": json_doc(doc)}


#: The answers a write's review asks for, all "no".
_NO = {"relation": {}, "unsafe": 0.0, "secret": 0.0, "standing": 0.0}
#: A ULID no record has.
_UNKNOWN = "01ARZ3NDEKTSV4RRFFQ69G5FAV"


class _Run:
    def __init__(self, server: Any) -> None:
        self.server = server
        self.cases: list[dict[str, Any]] = []
        #: The response of each case before the placeholders, by case name.
        self.raw: dict[str, Any] = {}

    async def call(
        self, case: str, tool: str, args: dict[str, Any], server: Any = None
    ) -> Any:
        target = server if server is not None else self.server
        record: dict[str, Any] = {"case": case, "tool": tool, "request": args}
        try:
            result = await target.call_tool(tool, args)
        except ToolError as exc:  # a refusal the server reports as a tool error
            record["error"] = str(exc)
            self.cases.append(record)
            return None
        structured = getattr(result, "structured_content", None)
        if structured is None:
            structured = json.loads(result.content[0].text)
        record["is_error"] = bool(getattr(result, "is_error", False))
        record["response"] = structured
        self.raw[case] = structured
        self.cases.append(record)
        return structured

    async def answered(
        self, case: str, args: dict[str, Any], answers: dict[str, Any], relate: str
    ) -> Any:
        """A memory_write sent with `answers`; when the server returns a
        review page (related records to relate), the page is recorded as
        ``<case>.review_page`` and the call is re-sent with its review id
        and `relate` for every related id."""
        first = await self.call(case, "memory_write", {**args, "review": answers})
        if first.get("status") != "review":
            return first
        self.cases[-1]["case"] = f"{case}.review_page"
        page = first["review"]
        relation = {row["id"]: relate for row in page["related"]}
        review = {**answers, "review_id": page["review_id"], "relation": relation}
        return await self.call(case, "memory_write", {**args, "review": review})


def _records(
    w: World, run: _Run, scrub: Scrubber, fix: Callable[[dict[str, Any]], None]
) -> list[dict[str, Any]]:
    out = []
    for record in run.cases:
        clean = scrub(record)
        fix(clean)
        if "response" in clean:
            clean["response_text"] = json.dumps(
                clean["response"], ensure_ascii=True, separators=(",", ":")
            )
        out.append(clean)
    return out


_READ = (
    "request: the tool's arguments; response: the structured result (keys "
    "sorted here); response_text: the same object with the key order the "
    "server produced; is_error: the result's isError; error: the text of a "
    "call the server refused as a tool error (no response). Values the run "
    "mints are typed placeholders (placeholders), in requests and responses "
    "alike."
)


def _run(
    script: Callable[[_Run, Any, World, Store], Coroutine[Any, Any, None]],
) -> tuple[World, _Run, Scrubber]:
    with world() as w, _server(w) as (server, store, cfg):
        other = build_server(config=cfg, store=store, state=SessionState())
        run = _Run(server)
        asyncio.run(script(run, other, w, store))
        return w, run, Scrubber(w.root, now=w.now)


# ---------------------------------------------------------------------------
# Ungated: nothing ranked, nothing tokenized
# ---------------------------------------------------------------------------


async def _plain_script(run: _Run, other: Any, w: World, store: Store) -> None:
    call = run.call
    await call(
        "write.refused_transient",
        "memory_write",
        {
            "content": "The deploy is broken right now, retry later.",
            "scopes": ["tools"],
        },
    )
    await call(
        "write.refused_user_claim",
        "memory_write",
        {"content": "The user prefers tabs over spaces.", "scopes": ["tools"]},
    )
    await call(
        "write.refused_credential",
        "memory_write",
        {
            "content": (
                "The fixture deploy uses the AWS documentation example key "
                "AKIAIOSFODNN7EXAMPLE."
            ),
            "scopes": ["tools"],
        },
    )
    await call("show.unknown_id", "memory_show", {"id": _UNKNOWN})
    await call(
        "update.unknown_id", "memory_update", {"id": _UNKNOWN, "confidence": "high"}
    )
    await call("remove.unknown_id", "memory_remove", {"id": _UNKNOWN, "reason": "gone"})
    await call(
        "record_use.unknown_id",
        "memory_record_use",
        {"memory_ids": [_UNKNOWN], "outcome": "ignored"},
    )
    await call(
        "record_use.bad_outcome",
        "memory_record_use",
        {"memory_ids": [_UNKNOWN], "outcome": "maybe"},
    )
    await call(
        "record_use.not_a_ulid",
        "memory_record_use",
        {"memory_ids": ["x"], "outcome": "ignored"},
    )
    await call("episode.bad_action", "episode", {"action": "erase"})
    await call("episode.handoff_no_prior", "episode", {"action": "handoff"})
    await call(
        "episode.write",
        "episode",
        {
            "action": "write",
            "body": "Tried the port; it fell over at the fold.",
            "takeaway": "the fold needs a column",
        },
    )
    await call("episode.handoff_same_session", "episode", {"action": "handoff"})
    await call(
        "episode.handoff_next_session", "episode", {"action": "handoff"}, server=other
    )
    await call(
        "episode.handoff_bodies",
        "episode",
        {"action": "handoff", "include_bodies": True, "max_episodes": 1},
        server=other,
    )
    await call("admin.bad_action", "memory_admin", {"action": "erase"})
    await call("admin.tombstones_empty", "memory_admin", {"action": "tombstones"})
    await call("admin.conflicts_empty", "memory_admin", {"action": "conflicts"})
    await call(
        "admin.restore_unknown", "memory_admin", {"action": "restore", "id": _UNKNOWN}
    )
    await call(
        "admin.disable_scope",
        "memory_admin",
        {"action": "disable_scope", "scope": "projects:zephyrcore"},
    )
    await call(
        "admin.enable_scope",
        "memory_admin",
        {"action": "enable_scope", "scope": "projects:zephyrcore"},
    )
    await call(
        "admin.acknowledge_miss_unknown",
        "memory_admin",
        {"action": "acknowledge_miss", "id": _UNKNOWN, "reason": "not a miss"},
    )
    await call(
        "admin.answer_question_none",
        "memory_admin",
        {"action": "answer_question", "answer": "yes"},
    )


def plain() -> dict[str, bytes]:
    _w, run, scrub = _run(_plain_script)
    header = {
        "fixture": "tool_responses_plain",
        "read": (
            "One server over a new, empty store; a second server on the same "
            "store (the records marked server: other) is the next session. "
            + _READ
            + " Nothing here ranks or tokenizes, so the file does not depend "
            "on the Unicode or SQLite version."
        ),
        "setup": SETUP,
        "review_thresholds": _thresholds(),
        "placeholders": PLACEHOLDERS,
    }
    records = _records(_w, run, scrub, lambda r: None)
    for record in records:
        if record["case"].endswith(("_next_session", "handoff_bodies")):
            record["server"] = "other"
    return {"tool_responses_plain.jsonl": jsonl([header, *records])}


# ---------------------------------------------------------------------------
# Gated: writes that relate, searches that rank
# ---------------------------------------------------------------------------

_FACT = "The project zephyrcore uses ruff for linting and formatting, in pyproject."
_BUILD = "Builds run with uv; the lockfile is uv.lock at the repository root."
_BUILD2 = "Builds run with uv 0.5 now; the lockfile is uv.lock at the root."
_Z = ["projects:zephyrcore"]

#: The pending conflict the script seeds through the store's own API.
CONFLICT = {
    "id": "conflict-0001",
    "summary_a": "ruff lints zephyrcore",
    "summary_b": "uv 0.5 builds zephyrcore",
    "similarity": 0.75,
    "method": "jaccard",
    "detector": "fixture",
    "status": "open",
}


async def _script(run: _Run, other: Any, w: World, store: Store) -> None:
    call = run.call
    page = await call(
        "write.review_page", "memory_write", {"content": _FACT, "scopes": _Z}
    )
    fact = await call(
        "write.committed_after_review",
        "memory_write",
        {
            "content": _FACT,
            "scopes": _Z,
            "review": {**_NO, "review_id": page["review"]["review_id"]},
        },
    )
    build = await call(
        "write.committed_answers_on_first_call",
        "memory_write",
        {"content": _BUILD, "scopes": _Z, "review": _NO},
    )
    await call(
        "write.refused_scope_mismatch",
        "memory_write",
        {
            "content": "The linter zephyrcore relies on is configured in pyproject.",
            "scopes": ["tools"],
            "review": _NO,
        },
    )
    await call(
        "write.refused_duplicate",
        "memory_write",
        {"content": _FACT, "scopes": _Z, "review": _NO},
    )
    standing = await run.answered(
        "write.standing_declared",
        {"content": "Always sign commits in zephyrcore.", "scopes": _Z},
        {**_NO, "standing": 0.95},
        "unrelated",
    )
    await run.answered(
        "write.standing_conflicting",
        {"content": "Never sign commits in zephyrcore; signing is off.", "scopes": _Z},
        {**_NO, "standing": 0.95},
        "conflict",
    )
    newer = await run.answered(
        "write.committed_supersedes",
        {"content": _BUILD2, "scopes": _Z, "supersedes": [build["id"]]},
        _NO,
        "update",
    )
    claimed = await run.answered(
        "write.committed_claims",
        {
            "content": "The README of the alpha checkout names the project alpha.",
            "scopes": _Z,
            "claims": ["README.md"],
        },
        _NO,
        "unrelated",
    )
    await call(
        "search.fresh", "memory_search", {"query": "ruff linting", "auto_scope": False}
    )
    await call(
        "search.expand_top",
        "memory_search",
        {"query": "ruff linting", "auto_scope": False, "expand_top": True},
    )
    await call(
        "search.scopes_and_max_results",
        "memory_search",
        {"query": "zephyrcore", "scopes": _Z, "max_results": 2, "auto_scope": False},
    )
    await call(
        "search.exclude_scopes",
        "memory_search",
        {"query": "zephyrcore", "exclude_scopes": _Z, "auto_scope": False},
    )
    await call(
        "update.review_owed_standing",
        "memory_update",
        {"id": fact["id"], "content": _FACT + " Line length is 100."},
    )
    await call(
        "update.committed", "memory_update", {"id": fact["id"], "confidence": "high"}
    )
    await call(
        "update.bad_confidence",
        "memory_update",
        {"id": fact["id"], "confidence": "certain"},
    )
    await call(
        "update.committed_links",
        "memory_update",
        {
            "id": fact["id"],
            "links": [{"type": "contradicts", "target_id": newer["id"]}],
        },
    )
    await call(
        "search.contradicts",
        "memory_search",
        {"query": "ruff linting", "auto_scope": False},
    )
    await call(
        "search.superseded_by",
        "memory_search",
        {"query": "uv lockfile builds", "auto_scope": False},
    )
    await call("search.auto_scoped", "memory_search", {"query": "sign commits"})
    await call("search.empty", "memory_search", {"query": "zzzz nothing matches"})
    await call("show.compact", "memory_show", {"id": standing["id"]})
    await call("show.links", "memory_show", {"id": fact["id"]})
    await call("show.reverse_links", "memory_show", {"id": build["id"]})
    await call(
        "verify.paths",
        "memory_verify",
        {"id": fact["id"], "note": "read the file", "verified_paths": ["README.md"]},
    )
    await call(
        "verify.absent_path_present",
        "memory_verify",
        {"id": fact["id"], "note": "checked", "verified_absent_paths": ["README.md"]},
    )
    await call(
        "verify.missing_path",
        "memory_verify",
        {"id": fact["id"], "note": "checked", "verified_paths": ["missing.txt"]},
    )
    await call("show.verified", "memory_show", {"id": fact["id"]})
    await call(
        "verify.claimed",
        "memory_verify",
        {"id": claimed["id"], "note": "read it", "verified_paths": ["README.md"]},
    )
    await call("show.claimed", "memory_show", {"id": claimed["id"]})
    await call(
        "record_use.contradicted",
        "memory_record_use",
        {
            "memory_ids": [newer["id"]],
            "outcome": "contradicted",
            "note": "wrong version",
        },
    )
    await call(
        "search.not_fresh_reason",
        "memory_search",
        {"query": "uv lockfile builds", "auto_scope": False},
    )
    await call(
        "remove.committed", "memory_remove", {"id": build["id"], "reason": "obsolete"}
    )
    await call(
        "remove.already_removed",
        "memory_remove",
        {"id": build["id"], "reason": "again"},
    )
    await call(
        "update.removed", "memory_update", {"id": build["id"], "confidence": "low"}
    )
    await call(
        "record_use.removed",
        "memory_record_use",
        {"memory_ids": [build["id"]], "outcome": "ignored"},
    )
    await call(
        "remove.standing", "memory_remove", {"id": standing["id"], "reason": "paused"}
    )
    await call(
        "remove.standing_said",
        "memory_remove",
        {"id": standing["id"], "reason": "paused", "said": "Drop the signing rule."},
    )
    await call(
        "admin.restore", "memory_admin", {"action": "restore", "id": standing["id"]}
    )
    await call(
        "search.held", "memory_search", {"query": "sign commits", "auto_scope": False}
    )
    await call("admin.tombstones_summary", "memory_admin", {"action": "tombstones"})
    await call(
        "admin.tombstones_rows", "memory_admin", {"action": "tombstones", "scopes": _Z}
    )
    await call("admin.health_summary", "memory_admin", {"action": "health"})
    await call(
        "admin.health_section",
        "memory_admin",
        {"action": "health", "section": "contradicted,scope_distribution"},
    )
    await call(
        "admin.health_section_with_path",
        "memory_admin",
        {"action": "health", "section": "commit_drift_debt"},
    )
    await call(
        "admin.health_unknown_section",
        "memory_admin",
        {"action": "health", "section": "counts"},
    )
    store.put_conflict(
        {
            **CONFLICT,
            "a_id": fact["id"],
            "b_id": newer["id"],
            "created": fact["created"],
        }
    )
    await call("admin.conflicts_pending", "memory_admin", {"action": "conflicts"})
    await call("show.conflict_member", "memory_show", {"id": fact["id"]})
    await call(
        "admin.conflicts_resolve",
        "memory_admin",
        {
            "action": "conflicts",
            "id": "conflict-0001",
            "verdict": "compatible",
            "note": "both hold",
        },
    )
    await call(
        "admin.conflicts_scan", "memory_admin", {"action": "conflicts", "scan": True}
    )
    await call(
        "admin.rename_scope",
        "memory_admin",
        {"action": "rename_scope", "old_scope": "tools", "new_scope": "tooling"},
    )
    await call(
        "admin.join_project",
        "memory_admin",
        {"action": "join_project", "scope": "projects:zephyrcore"},
    )
    await call(
        "admin.unjoin_project",
        "memory_admin",
        {"action": "unjoin_project", "scope": "projects:zephyrcore"},
    )
    # A drifted claim: one commit after the verified head that deletes the
    # README.md the record claims present. It is dated BEFORE the
    # verification, so only the reachability rule (commits in
    # verified_head..HEAD) counts it; an author-date count would read 0.
    # A verification is then refused twice (the stored attestation, then
    # the claim that no longer holds) and the record reads drifted still.
    _git(w.checkout, _DRIFT_STAMP, "rm", "README.md")
    await call("show.claimed_drifted", "memory_show", {"id": claimed["id"]})
    await call(
        "verify.claimed_drifted_refused",
        "memory_verify",
        {
            "id": claimed["id"],
            "note": "README.md is gone",
            "verified_absent_paths": ["README.md"],
        },
    )
    await call(
        "verify.claimed_drifted_claim_broken",
        "memory_verify",
        {
            "id": claimed["id"],
            "note": "README.md is gone",
            "verified_paths": [],
            "verified_absent_paths": ["README.md"],
        },
    )
    await call("show.claimed_still_drifted", "memory_show", {"id": claimed["id"]})
    # The author-date rule: the history is rewritten (an orphan branch),
    # so the verified head is no ancestor of HEAD any more. Two commits
    # touch README.md, one dated before the verification and one after;
    # the author-date count reads 1, where counting every commit reachable
    # from HEAD would read 2.
    _git(w.checkout, None, "checkout", "-q", "--orphan", "rewritten")
    _git(w.checkout, _DRIFT_STAMP, "write", "README.md", "alpha\n")
    _git(w.checkout, _LATE_STAMP, "write", "README.md", "alpha, rewritten\n")
    await call("show.author_date_drift", "memory_show", {"id": fact["id"]})
    # A recorded miss, landed as the Stop hook's audit lands it.
    store.record_event(
        "search_miss", **{**MISS, "top_hits": [{**MISS_HIT, "id": fact["id"]}]}
    )
    await call(
        "admin.acknowledge_miss",
        "memory_admin",
        {"action": "acknowledge_miss", "id": MISS["event_id"], "reason": "probe noise"},
    )
    # A pending question: a clone of alpha under another remote asks.
    fork = w.root / "fork"
    _clone(w.checkout, fork, "https://example.invalid/me/fork.git")
    os.chdir(fork)
    location.forget()
    try:
        answered = await call(
            "admin.answer_question",
            "memory_admin",
            {"action": "answer_question", "answer": "separate"},
        )
    finally:
        os.chdir(w.checkout)
        location.forget()
    theirs = answered["project"]
    await call(
        "admin.join_project.normal",
        "memory_admin",
        {"action": "join_project", "id": theirs, "said": "join the fork into alpha"},
    )
    await call(
        "admin.unjoin_project.normal",
        "memory_admin",
        {"action": "unjoin_project", "id": theirs},
    )


#: The search_miss event the script lands before admin.acknowledge_miss
#: (the fields `audit.search_miss_fields` builds; top_hits[0].id is the
#: write.committed_after_review record).
MISS: dict[str, Any] = {
    "session": "sess_hook",
    "event_id": "01ARZ3NDEKTSV4RRFFQ69G5FB0",
    "session_id": "sess_hook",
    "threshold_rule": "top_score>=0.5",
    "lookback_seconds": 120,
    "recent_retrieval_count": 0,
    "probe_query": "ruff linting",
    "triggered_from": "stop_hook",
}
MISS_HIT = {"score": 0.9, "relevance": "high"}


#: The drift commit's author and committer date: before any run's
#: verification, so only the reachability rule counts it; fixed, so its
#: SHA is.
_DRIFT_STAMP = "2001-01-01T00:00:00Z"
#: The rewritten history's second commit: after any run's verification.
_LATE_STAMP = "2099-01-01T00:00:00Z"


def _git(repo: Path, stamp: str | None, verb: str, *args: str) -> None:
    """In `repo`: ``rm NAME`` commits the deletion of NAME, ``write NAME
    TEXT`` commits NAME holding TEXT, both at `stamp` (author and
    committer); anything else is a plain git command."""
    env = dict(os.environ)
    if stamp is not None:
        env.update(GIT_AUTHOR_DATE=stamp, GIT_COMMITTER_DATE=stamp)
    if verb == "rm":
        steps = [["rm", "-q", args[0]], ["commit", "-q", "-m", f"delete {args[0]}"]]
    elif verb == "write":
        (repo / args[0]).write_bytes(args[1].encode("ascii"))
        steps = [["add", args[0]], ["commit", "-q", "-m", f"write {args[0]}"]]
    else:
        steps = [[verb, *args]]
    for step in steps:
        subprocess.run(
            ["git", *step], cwd=repo, check=True, capture_output=True, env=env
        )


def _clone(src: Path, dst: Path, url: str) -> None:
    for args in (
        ["clone", "-q", src.resolve().as_uri(), str(dst)],
        ["-C", str(dst), "remote", "set-url", "origin", url],
    ):
        subprocess.run(["git", *args], check=True, capture_output=True)


#: Health sections whose rendered text holds the checkout's absolute path.
_PATH_SIZED = ("commit_drift_debt",)


def responses() -> dict[str, bytes]:
    w, run, scrub = _run(_script)
    raw_sections = run.raw.get("admin.health_section_with_path") or {}

    def fix(record: dict[str, Any]) -> None:
        # A section_chars count over text that holds the scratch path is
        # restated over the text as written here (the path as <scratch>).
        chars = record.get("response", {}).get("section_chars", {})
        for name in _PATH_SIZED:
            if name in chars and name in raw_sections:
                raw = json.dumps(raw_sections[name], separators=(",", ":"))
                clean = json.dumps(scrub(raw_sections[name]), separators=(",", ":"))
                chars[name] = chars[name] - len(raw) + len(clean)

    records = _records(w, run, scrub, fix)
    header = {
        "fixture": "tool_responses",
        "read": (
            "One server over a new, empty store. " + _READ + " Before "
            "admin.conflicts_pending it seeds one pending conflict through "
            "Store.put_conflict (conflict, a_id: the write.committed_after_"
            "review id, b_id: the write.committed_supersedes id, created: the "
            "first one's created). section_chars of commit_drift_debt is "
            "counted over the section's compact JSON with the scratch path "
            "written as <scratch> (path_sized_sections): the product's count "
            "is this plus, per occurrence, the JSON-escaped scratch path's "
            "length less 9. Before show.claimed_drifted it commits, in "
            "<scratch>/alpha, the deletion of README.md (git rm; author and "
            "committer Fixture at drift_commit_date, before any run's "
            "verification: only the reachability rule, commits in "
            "verified_head..HEAD, counts it). Before show.author_date_drift "
            "it starts an orphan branch rewritten in <scratch>/alpha (git "
            "checkout --orphan, so the verified head is no ancestor of HEAD) "
            "and commits README.md = 'alpha\\n' at drift_commit_date, then "
            "'alpha, rewritten\\n' at late_commit_date: the author-date rule "
            "counts only the second. The script appends 9 records after "
            "admin.unjoin_project. Before admin.acknowledge_miss it lands one "
            "search_miss event through Store.record_event (miss, with "
            "top_hits [{id: the write.committed_after_review id, score: 0.9, "
            "relevance: high}]). Before admin.answer_question it clones "
            "<scratch>/alpha to <scratch>/fork (git clone of its file URI, "
            "origin then set to https://example.invalid/me/fork.git), works "
            "there (location.forget()) so the caller's checkout has the open "
            "question, and works in <scratch>/alpha again from admin."
            "join_project.normal on."
        ),
        "setup": SETUP,
        "review_thresholds": _thresholds(),
        "conflict": CONFLICT,
        "miss": MISS,
        "drift_commit_date": _DRIFT_STAMP,
        "late_commit_date": _LATE_STAMP,
        "path_sized_sections": list(_PATH_SIZED),
        "placeholders": PLACEHOLDERS,
    }
    return {"tool_responses.jsonl": jsonl([header, *records])}
