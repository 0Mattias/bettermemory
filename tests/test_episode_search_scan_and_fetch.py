"""The two-step episode read: scan takeaways, then fetch one body by id.

`episode_search` had exactly one shape — every matching row carried its
full `body`. On the live store the mean episode body is ~3 KB, so the
default 20-row page billed ~84 KB of journal prose to answer "what did I
conclude lately?", a question the takeaways alone answer. There was also
no by-id read at all: `episode_promote` was the only surface that took an
episode id, and it is write-side (it distills and DELETES).

G1 adds the two halves of the cheap pattern:

* `include_bodies` (default True — compat) — False OMITS the `body` key
  from every row. Omitted, not emptied: a row carrying `"body": ""` would
  save eleven characters instead of three thousand, and a test written as
  `row["body"] == ""` would pass while the payload stayed the same size.
  `test_include_bodies_false_omits_the_key_not_its_value` pins the
  omission.
* `ids` — the by-id read the scan step needs to be useful. An explicit
  selector, so it joins `swarm_id` / `parent_session_id` in the
  worktree-filter carve-out (pinned next door in
  `tests/test_episode_search_isolation.py`); AND-composed with every
  other filter; unknown ids are absent rather than an error.

THE WIRE IS THE POINT. FastMCP builds each tool's JSON schema from the
`_handlers.py` facade signature, not from the handler module, and a
parameter present on the handler alone is silently DROPPED by the
pydantic argument model — `call_tool("episode_search",
{"include_bodies": False})` then succeeds and returns the full bodies
forever. That defect has shipped twice in this repo. So the proof here is
two-part and neither half is redundant: the schema assertion localises a
regression to the facade, and the behavioural assertion goes through
`server.call_tool` (never the handler function) so a facade that drops
the parameter fails rather than passes.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from bettermemory.config import Config, StorageConfig
from bettermemory.episodes import EpisodeStore
from bettermemory.server import build_server
from bettermemory.session import SessionState
from bettermemory.store import Store


async def _call(server: Any, name: str, **kwargs: Any) -> Any:
    """Invoke a registered tool and return the parsed payload — the same
    helper `tests/test_episode_search_isolation.py` uses, so these tests
    exercise the served surface rather than the handler function."""
    content, structured = await server.call_tool(name, kwargs)
    if structured is not None:
        return structured
    if content and hasattr(content[0], "text"):
        return json.loads(content[0].text)
    return None


def _unwrap(res: Any) -> Any:
    return res.get("result", res) if isinstance(res, dict) and "result" in res else res


def _server(memory_dir: Path) -> Any:
    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))
    return build_server(config=cfg, store=Store(memory_dir), state=SessionState())


# ---------------------------------------------------------------------------
# The wire — the facade signature IS the served schema
# ---------------------------------------------------------------------------


async def test_the_new_parameters_are_on_the_served_input_schema(
    memory_dir: Path,
) -> None:
    """Localises a regression to the `_handlers.py` facade.

    The handler module can grow a parameter with no effect on the wire at
    all: the served `inputSchema` is built from
    `inspect.signature(ToolHandlers.episode_search)`. If this fails while
    the behavioural test below also fails, the facade is where to look."""
    tools = {t.name: t for t in await _server(memory_dir).list_tools()}
    props = tools["episode_search"].inputSchema["properties"]

    assert {"include_bodies", "ids"} <= set(props), (
        f"episode_search serves {sorted(props)} — the new parameters reached "
        f"the handler but not the `_handlers.py` facade, so they are absent "
        f"from the client manifest and silently dropped at call time."
    )
    # Compat is a wire-level promise, not just a Python default: a client
    # that omits the flag must keep getting bodies.
    assert props["include_bodies"].get("default") is True
    assert props["ids"].get("default") is None


async def test_the_new_parameters_are_documented_in_the_desc(
    memory_dir: Path,
) -> None:
    """A parameter the model cannot read about is one it never sends. The
    DESC is the only place it learns the scan-then-fetch pattern exists —
    `docs/api.md` is not resident."""
    tools = {t.name: t for t in await _server(memory_dir).list_tools()}
    desc = tools["episode_search"].description or ""
    assert "include_bodies" in desc
    assert "`ids`" in desc


# ---------------------------------------------------------------------------
# include_bodies — omission, not emptying
# ---------------------------------------------------------------------------


async def test_include_bodies_false_omits_the_key_not_its_value(
    memory_dir: Path,
) -> None:
    """Driven through `call_tool`, so a facade that drops the parameter
    fails here instead of passing: the handler-only variant returns a
    perfectly successful response carrying every body.

    The assertion is `"body" not in row`. `row["body"] == ""` would be
    satisfied by an empty-string emit that saves ~11 chars of the ~2,500
    this parameter exists to save."""
    server = _server(memory_dir)
    await _call(server, "episode_write", body="B" * 3000, takeaway="the conclusion")

    rows = _unwrap(await _call(server, "episode_search", include_bodies=False))
    assert len(rows) == 1
    (row,) = rows
    assert "body" not in row, (
        f"include_bodies=False still emitted a `body` key ({row.get('body')!r}); "
        f"the row must OMIT it — an empty string costs the same wire framing "
        f"and keeps every caller's parser reading a field that is now a lie."
    )
    # Every other documented key survives: this is a projection, not a
    # different return shape.
    assert set(row) == {"id", "session_id", "created", "takeaway", "scopes", "swarm_id"}
    assert row["takeaway"] == "the conclusion"


async def test_include_bodies_defaults_to_true(memory_dir: Path) -> None:
    """The compat half. `tests/test_episode_patterns.py` reads
    `row["body"]` off an `episode_search` result without passing the flag,
    and so does every caller written before this parameter existed."""
    server = _server(memory_dir)
    await _call(server, "episode_write", body="full journal prose", takeaway="t")

    (row,) = _unwrap(await _call(server, "episode_search"))
    assert row["body"] == "full journal prose"


# ---------------------------------------------------------------------------
# ids — the by-id read
# ---------------------------------------------------------------------------


async def test_ids_selects_exactly_those_episodes(memory_dir: Path) -> None:
    server = _server(memory_dir)
    written = [
        await _call(server, "episode_write", body=f"body {n}", takeaway=f"t{n}")
        for n in range(3)
    ]

    rows = _unwrap(
        await _call(server, "episode_search", ids=[written[0]["id"], written[2]["id"]])
    )
    assert [r["takeaway"] for r in rows] == ["t0", "t2"]


async def test_an_unknown_id_is_absent_not_an_error(memory_dir: Path) -> None:
    """`episode_promote` raises on an unresolvable id because it is a
    single-target write. `episode_search` is a list-returning filter, and
    raising on one bad id in a batch of twenty is hostile — the same
    reasoning `list_by_swarm` records for an unknown swarm id ("an empty
    result for an unknown id is the correct (and only) failure mode")."""
    server = _server(memory_dir)
    real = await _call(server, "episode_write", body="body", takeaway="kept")

    rows = _unwrap(
        await _call(server, "episode_search", ids=[real["id"], "01ABSENTABSENTABSENT"])
    )
    assert [r["takeaway"] for r in rows] == ["kept"]

    # Every id unknown is an empty list, still not a raise.
    assert _unwrap(await _call(server, "episode_search", ids=["01NOPENOPENOPE"])) == []


async def test_an_empty_ids_list_means_unset(memory_dir: Path) -> None:
    """Mirrors `scopes=[]`, which the handler also reads as "no filter"
    (`set(scopes) if scopes else None`). A client library that serializes
    an unset list as `[]` must not silently receive nothing."""
    server = _server(memory_dir)
    await _call(server, "episode_write", body="body", takeaway="t")

    assert len(_unwrap(await _call(server, "episode_search", ids=[]))) == 1


async def test_ids_composes_with_the_other_filters(memory_dir: Path) -> None:
    """One rule, no exceptions: `ids` ANDs with every other filter. The
    only carve-out is the worktree one, pinned in
    `tests/test_episode_search_isolation.py`."""
    server = _server(memory_dir)
    a = await _call(server, "episode_write", body="a", takeaway="ta", scopes=["alpha"])
    b = await _call(server, "episode_write", body="b", takeaway="tb", scopes=["beta"])

    both = [a["id"], b["id"]]
    assert [
        r["takeaway"]
        for r in _unwrap(
            await _call(server, "episode_search", ids=both, scopes=["beta"])
        )
    ] == ["tb"]
    # A named id whose scope is filtered out is dropped, not rescued.
    assert (
        _unwrap(await _call(server, "episode_search", ids=[a["id"]], scopes=["beta"]))
        == []
    )

    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    assert (
        _unwrap(await _call(server, "episode_search", ids=both, since=tomorrow)) == []
    )


async def test_ids_still_does_not_surface_floor_episodes(memory_dir: Path) -> None:
    """Session-tag floors carry an empty takeaway and a placeholder body;
    the DESC and docs/api.md both say they are filtered off this surface.
    Naming one explicitly does not reopen it — the alternative is a
    caller-visible row that reads as a takeaway and is not one."""
    server = _server(memory_dir)
    floor = EpisodeStore(memory_dir).write_floor(session_id="sess_floor001")

    assert floor.is_floor
    assert _unwrap(await _call(server, "episode_search", ids=[floor.id])) == []


# ---------------------------------------------------------------------------
# The acceptance criterion, measured
# ---------------------------------------------------------------------------


async def test_a_ten_episode_scan_then_fetch_costs_a_fraction_of_the_page(
    memory_dir: Path,
) -> None:
    """The AC, as a measurement rather than a claim.

    THE FIXTURE IS SIZED FROM THE REAL STORE, because a flattering one
    would make this test prove nothing. Measured 2026-07-31 over the 138
    non-floor episodes in `~/.claude-memory/episodes`: mean body 3,055
    chars, mean takeaway **614** chars. That second number is the one
    worth carrying — the upgrade plan's AC ("~1-2 KB instead of ~30 KB")
    was written against a 64-char takeaway, off by an order of magnitude,
    and the saving is correspondingly smaller than advertised: takeaways
    on this store are paragraphs, not sentences (the cap is
    `max_takeaway_bytes`, 4 KB, and writers use it).

    So the honest claim, and what is asserted below, is a ~4-5x cut
    rather than the ~20x the AC implies. Measured on the live store
    itself: a 10-row page goes 50.2 KB -> 4.8 KB (90.5%), a 20-row page
    84.1 KB -> 8.9 KB (89.4%). The fixture here reproduces the shape, not
    those exact figures. Sizes are compact JSON, the convention
    `tests/test_resident_footprint.py` measures schemas with.

    Also the round-trip: the scan hands back ids, and one of them fetches
    its body back verbatim. A scan you cannot follow up on is not cheaper,
    it is just lossy."""
    server = _server(memory_dir)
    ids = []
    for n in range(10):
        written = await _call(
            server,
            "episode_write",
            # 3,055 and 614 chars respectively, to the nearest whole unit.
            body=f"episode {n}: " + "journal prose. " * 203,
            takeaway=f"takeaway {n}: " + "the conclusion that survived. " * 20,
        )
        ids.append(written["id"])

    def _size(rows: Any) -> int:
        return len(json.dumps(rows, sort_keys=True, separators=(",", ":")))

    full = _unwrap(await _call(server, "episode_search"))
    scan = _unwrap(await _call(server, "episode_search", include_bodies=False))
    assert len(full) == len(scan) == 10

    full_bytes, scan_bytes = _size(full), _size(scan)
    print(
        f"\n10-episode page: full={full_bytes} B, takeaway-only={scan_bytes} B "
        f"({100 * (full_bytes - scan_bytes) / full_bytes:.1f}% smaller)"
    )

    assert full_bytes > 30_000, (
        f"the fixture is no longer representative of the live store — a "
        f"10-episode page measured {full_bytes} B against ~50 KB there."
    )
    # Two bounds, because either alone is satisfiable by the wrong change.
    # The ratio alone would be met by growing the fixture's bodies; the
    # absolute bound alone would be met by shrinking them.
    assert scan_bytes * 4 < full_bytes, (
        f"the takeaway-only page ({scan_bytes} B) is not a quarter of the "
        f"full page ({full_bytes} B). Bodies are the only thing that can "
        f"carry that weight, so this failing means they are still emitted."
    )
    assert scan_bytes < 9_000, (
        f"the takeaway-only page measured {scan_bytes} B. Ten rows of "
        f"id + session_id + created + takeaway is ~800 B each at live-store "
        f"takeaway lengths, so materially more than that is a body, or a new "
        f"field nobody costed."
    )

    # Round-trip: one id from the scan fetches exactly one full body back,
    # and it is the body that was written.
    scanned_id = scan[3]["id"]
    (fetched,) = _unwrap(await _call(server, "episode_search", ids=[scanned_id]))
    assert fetched["id"] == scanned_id == ids[3]
    assert fetched["body"] == full[3]["body"]
    # And the follow-up read is a fraction of the page it came from.
    assert _size([fetched]) < full_bytes // 5
