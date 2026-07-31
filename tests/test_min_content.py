"""The opt-in minimum-content floor (`[behavior] min_content_tokens`).

A default-ON floor was designed and then killed on the evidence: `content="x"`
is a legitimate fixture across dozens of handler-path call sites, and the only
in-repo floor precedent (the proposals extractor's 30 chars / 6 tokens,
`src/bettermemory/proposals.py`) sits an order of magnitude above what this
corpus treats as a valid body. So the floor ships as hardening for callers who
want it — unattended imports, bulk writers — and OFF for everyone else.

That makes "off is byte-identical to the pre-floor server" the load-bearing
property, not the floor itself. Two ways it could quietly stop being true:
the dataclass default flips, or `load_config` grows a default of its own the
way `full_tool_surface` deliberately does (dataclass True, loader False).
`tests/test_config.py`'s round-trip test enumerates fields by hand, so a NEW
field's asymmetry is exactly what it cannot see — pinned here instead.

The knob then shipped INERT, and every test in this file passed while it was:
the floor was wired into `_validate_write_payload` but never threaded from
`deps.config.behavior` at either call site, so `Config(behavior=BehaviorConfig(
min_content_tokens=6))` + `memory_write(content="x")` returned
`{"status": "committed"}` and the store grew by one. The tests all handed the
value to the validator BY HAND — they proved the validator, and the validator
was never the part that was broken. Same shape as the `ingest --force`
regression the postmortem in `docs/incidents/` covers, and the same lesson:
a test that asserts an intermediate artifact does not test the behaviour a
flag promises. Two model- and user-facing surfaces were asserting the false
claim meanwhile — `docs/api.md` and `config.py`'s `DEFAULT_CONFIG`, which
ships that promise verbatim into every user's config.toml.

So the sections below are the load-bearing half: they turn the knob on through
a real `Config`, drive the actual MCP tools through `build_server`, and assert
what a caller observes. The validator unit tests above stay — they are good
unit tests. They were just the ONLY tests.
"""

from __future__ import annotations
from ._mcp import call_tool as _mcp_call

import ast
from pathlib import Path
from typing import Any

import pytest

from bettermemory.config import (
    DEFAULT_CONFIG,
    BehaviorConfig,
    Config,
    StorageConfig,
    load_config,
)
from bettermemory.handlers._shared import (
    _validate_content_floor,
    _validate_write_payload,
)
from bettermemory.proposals import Proposal, ProposalQueue
from bettermemory.server import build_server
from bettermemory.session import SessionState
from bettermemory.store import Store

# A body that clears a 6-token floor and every content gate the write path
# runs (not transient, not first-person, not a credential). Reused so the
# floor is the only variable between the admit and refuse cases.
_LONG_BODY = "the deploy pipeline builds an image before it promotes it"
_SHORT_BODY = "zsh"


def _validate(content: str, **overrides: Any) -> dict[str, Any]:
    """Run the shared write validator with the kwargs `memory_write` passes."""
    kwargs: dict[str, Any] = {
        "scopes": ["tools"],
        "confidence": "medium",
        "source": "explicit-statement",
        "allowed_scopes": [],
    }
    kwargs.update(overrides)
    return _validate_write_payload(content=content, **kwargs)


# ---------------------------------------------------------------------------
# Default OFF — the property the whole design rests on.
# ---------------------------------------------------------------------------


def test_one_token_body_still_commits_when_the_floor_is_unset() -> None:
    """The handler-path fixtures writing `content="x"` must keep working.

    Omitting the argument entirely is the path every caller that hasn't been
    updated takes; it has to mean "no floor", not "some floor".
    """
    assert _validate("x")["content"] == "x"


def test_explicit_zero_is_the_same_as_unset() -> None:
    """0 is the documented disable value, matching `max_content_bytes`'s
    `<= 0` convention rather than meaning "at least zero tokens"."""
    assert _validate("x", min_content_tokens=0)["content"] == "x"


def test_negative_floor_disables_rather_than_rejecting_everything() -> None:
    """A hand-edited `-1` must disable, not compare `token_count < -1` in a
    way a later refactor could invert. Same guard shape as the byte cap."""
    _validate_content_floor("x", -1)


def test_dataclass_default_is_off() -> None:
    assert Config().behavior.min_content_tokens == 0


def test_shipped_config_round_trips_to_the_dataclass_default(tmp_path: Path) -> None:
    """DEFAULT_CONFIG ships verbatim to every first-run user, and the loader
    is where deployment policy diverges from the dataclass (`full_tool_surface`
    does exactly that on purpose). This field is NOT one of those: a user who
    never edits config.toml must get the same 0 a programmatic `Config()` gets.
    """
    config_path = tmp_path / "config.toml"
    config_path.write_text(DEFAULT_CONFIG, encoding="utf-8")

    loaded = load_config(config_path)

    assert loaded.behavior.min_content_tokens == 0
    assert loaded.behavior.min_content_tokens == Config().behavior.min_content_tokens


# ---------------------------------------------------------------------------
# The floor itself, once a deployment turns it on.
# ---------------------------------------------------------------------------


def test_floor_rejects_a_body_under_the_configured_count() -> None:
    with pytest.raises(ValueError, match="min_content_tokens"):
        _validate("zsh", min_content_tokens=6)


def test_rejection_names_the_setting_that_lifts_it() -> None:
    """A refused write has to tell the caller which knob to turn — the same
    contract `_validate_content_size`'s message holds up for the byte cap."""
    with pytest.raises(ValueError) as excinfo:
        _validate_content_floor("zsh", 6)
    message = str(excinfo.value)
    assert "1 tokens < 6 tokens" in message
    assert "[behavior] min_content_tokens" in message


def test_a_body_exactly_at_the_floor_is_admitted() -> None:
    """`<` not `<=`: the configured number is the minimum acceptable count,
    not the first rejected one."""
    body = "the deploy runs through GitHub Actions"
    assert len(body.split()) == 6
    assert _validate(body, min_content_tokens=6)["content"] == body


def test_one_token_below_the_floor_is_refused() -> None:
    body = "deploy runs through GitHub Actions"
    assert len(body.split()) == 5
    with pytest.raises(ValueError, match="min_content_tokens"):
        _validate(body, min_content_tokens=6)


def test_tokens_are_counted_across_arbitrary_whitespace() -> None:
    """Bodies arrive with newlines and indentation. A `split(" ")` spelling
    would count the empty strings between runs of whitespace and let a
    four-token body clear a six-token floor."""
    body = "  ships\tzsh\n\n   not   bash  "
    assert len(body.split()) == 4
    with pytest.raises(ValueError, match="4 tokens < 6 tokens"):
        _validate_content_floor(body, 6)


def test_floor_does_not_disturb_the_rest_of_the_payload() -> None:
    """An admitted write returns the same normalised dict it always did —
    the floor is a guard, not a transform."""
    body = "the deploy runs through GitHub Actions"
    assert _validate(body, min_content_tokens=3) == _validate(body)


def test_empty_content_still_reports_the_original_error() -> None:
    """The pre-existing non-empty check runs first, so an empty body under an
    enabled floor keeps its own message rather than being re-badged as a
    floor violation."""
    with pytest.raises(ValueError, match="content must be a non-empty string"):
        _validate("   ", min_content_tokens=6)


# ---------------------------------------------------------------------------
# Loader coercion — same family as the other [behavior] int knobs.
# ---------------------------------------------------------------------------


def test_load_config_reads_and_coerces_the_floor(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[behavior]\nmin_content_tokens = 6\n",
        encoding="utf-8",
    )
    cfg = load_config(config_path)
    assert cfg.behavior.min_content_tokens == 6
    assert isinstance(cfg.behavior.min_content_tokens, int)


def test_load_config_truncates_a_float_floor(tmp_path: Path) -> None:
    """A TOML float would otherwise survive as a float and compare oddly
    against an integer token count."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[behavior]\nmin_content_tokens = 6.9\n",
        encoding="utf-8",
    )
    cfg = load_config(config_path)
    assert cfg.behavior.min_content_tokens == 6
    assert isinstance(cfg.behavior.min_content_tokens, int)


def test_load_config_locates_a_malformed_floor(tmp_path: Path) -> None:
    """Non-numeric values raise a located error naming the key, rather than
    letting a bare `int(...)` escape `load_config` unlabelled."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[behavior]\nmin_content_tokens = "six"\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match=r"\[behavior\] min_content_tokens"):
        load_config(config_path)


# ---------------------------------------------------------------------------
# End-to-end: the floor as a caller actually experiences it.
#
# Everything above hands the floor to the validator by hand, which is exactly
# how the knob shipped inert — the value was never threaded from config at
# either call site and every test still passed. These drive real MCP tools
# built from a real `Config`, so an unthreaded call site fails here.
# ---------------------------------------------------------------------------


async def _call(server: Any, name: str, **kwargs: Any) -> Any:
    """Invoke a tool and return its structured payload.

    Delegates to `tests/_mcp.py`, which owns the SDK's return shape so
    the mcp 2.x port edits one function rather than forty-four.
    """
    return await _mcp_call(server, name, kwargs)


def _unwrap(res: Any) -> Any:
    return res.get("result", res) if isinstance(res, dict) and "result" in res else res


def _server(memory_dir: Path, **behavior: Any) -> Any:
    """A real server over `memory_dir`, configured the way a deployment would.

    `full_tool_surface` is on because `memory_proposals` — the second half of
    the floor's documented blast radius — is otherwise not registered at all.
    """
    cfg = Config(
        storage=StorageConfig(directory=str(memory_dir)),
        behavior=BehaviorConfig(full_tool_surface=True, **behavior),
    )
    return build_server(config=cfg, store=Store(memory_dir), state=SessionState())


def _queue_proposal(memory_dir: Path, body: str, *, pid: str = "p1") -> None:
    ProposalQueue(Store(memory_dir).root).append(
        [
            Proposal(
                id=pid,
                body=body,
                source_excerpt=body,
                suggested_category="fact",
                created="2026-01-01T12:00:00+00:00",
            )
        ]
    )


def _stored(memory_dir: Path) -> list[Any]:
    return list(Store(memory_dir).load_all())


# ---- memory_write ---------------------------------------------------------


async def test_memory_write_refuses_a_short_body_when_the_floor_is_configured(
    memory_dir: Path,
) -> None:
    """The exact probe that proved the knob inert: floor 6, one-token body.

    Before the fix this returned `{"status": "committed"}` and the store grew
    by one. The store assertion is the half that matters — a refusal that
    still writes is the failure mode a status-only assertion would miss.
    FastMCP wraps the handler's ValueError in a ToolError; both pass through
    `Exception` and the message carries through either way.
    """
    server = _server(memory_dir, min_content_tokens=6)

    with pytest.raises(Exception, match="below min_content_tokens"):
        await _call(server, "memory_write", content="x", scopes=["tools"])

    assert _stored(memory_dir) == []


async def test_memory_write_admits_a_body_that_clears_the_floor(
    memory_dir: Path,
) -> None:
    """The other side of the same knob — an enabled floor must not become a
    blanket refusal, or the test above would pass against a broken server."""
    server = _server(memory_dir, min_content_tokens=6)

    res = _unwrap(
        await _call(server, "memory_write", content=_LONG_BODY, scopes=["tools"])
    )

    assert res["status"] == "committed"
    assert len(_stored(memory_dir)) == 1


async def test_the_refusal_boundary_tracks_the_configured_number(
    memory_dir: Path, tmp_path: Path
) -> None:
    """The floor the tool enforces is the CONFIGURED one, not a constant.

    A call site that threaded a hard-coded number — or one that read a
    different `[behavior]` field — would satisfy both tests above. Only
    moving the number and watching the same body change verdict pins it.
    """
    body = "ships zsh not bash"
    assert len(body.split()) == 4

    strict = _server(memory_dir, min_content_tokens=6)
    with pytest.raises(Exception, match="4 tokens < 6 tokens"):
        await _call(strict, "memory_write", content=body, scopes=["tools"])

    lenient_dir = tmp_path / "lenient"
    lenient_dir.mkdir()
    lenient = _server(lenient_dir, min_content_tokens=4)
    res = _unwrap(await _call(lenient, "memory_write", content=body, scopes=["tools"]))

    assert res["status"] == "committed"


async def test_memory_write_default_config_still_commits_a_one_token_body(
    memory_dir: Path,
) -> None:
    """Off is byte-identical to the pre-floor server — through the TOOL.

    The validator-level version of this claim is above; it cannot see a call
    site that threads the wrong field or a loader default that diverges.
    """
    server = _server(memory_dir)

    res = _unwrap(await _call(server, "memory_write", content="x", scopes=["tools"]))

    assert res["status"] == "committed"
    assert len(_stored(memory_dir)) == 1


# ---- proposal acceptance --------------------------------------------------


async def test_proposal_accept_refuses_a_short_body_when_the_floor_is_configured(
    memory_dir: Path,
) -> None:
    """The second surface `docs/api.md` and `DEFAULT_CONFIG` promise the floor
    binds. It routes through the same shared validator but a SEPARATE call
    site, so it could be threaded at one and not the other."""
    server = _server(memory_dir, min_content_tokens=6)
    _queue_proposal(memory_dir, _SHORT_BODY)

    with pytest.raises(Exception, match="below min_content_tokens"):
        await _call(
            server,
            "memory_proposals",
            action="accept",
            proposal_id="p1",
            scopes=["tools"],
        )

    assert _stored(memory_dir) == []
    # A payload rejection leaves the entry queued — the reviewer can edit it
    # up to the floor and re-accept rather than losing the capture.
    listed = await _call(server, "memory_proposals", action="list")
    assert [p["id"] for p in listed["proposals"]] == ["p1"]


async def test_proposal_accept_admits_a_short_body_under_the_default(
    memory_dir: Path,
) -> None:
    """Default off, through the real accept tool: the queue's existing
    behaviour is untouched for everyone who never sets the knob."""
    server = _server(memory_dir)
    _queue_proposal(memory_dir, _SHORT_BODY)

    res = _unwrap(
        await _call(
            server,
            "memory_proposals",
            action="accept",
            proposal_id="p1",
            scopes=["tools"],
        )
    )

    assert res["status"] == "accepted"
    assert len(_stored(memory_dir)) == 1


# ---- memory_update: the documented exemption ------------------------------


async def test_memory_update_can_take_a_body_below_the_floor(
    memory_dir: Path,
) -> None:
    """`memory_update` is DELIBERATELY exempt, and this pins that choice.

    It validates a replacement body through `_validate_content_size` directly
    and never routes through `_validate_write_payload`, so the floor does not
    reach it. That is the intended semantics, not an oversight: the floor
    prices the cost of ADMITTING a new durable record, and a correction that
    shortens an existing body creates no new record — it makes one already
    paid for more accurate. Refusing it would mean an enabled floor could
    strand a memory at a wrong-but-wordy body, which is worse than the
    fragment the floor exists to prevent.

    Both directions are pinned: if a later change binds update to the floor,
    this fails and the author has to update `docs/api.md`, the `DEFAULT_CONFIG`
    comment, and `_validate_content_floor`'s docstring — all three currently
    state the exemption — rather than silently contradicting them.
    """
    server = _server(memory_dir, min_content_tokens=6)
    written = _unwrap(
        await _call(server, "memory_write", content=_LONG_BODY, scopes=["tools"])
    )

    updated = _unwrap(
        await _call(server, "memory_update", id=written["id"], content=_SHORT_BODY)
    )

    # `memory_update` returns the shared `responses.committed` envelope, so
    # the success status it reports is "committed", same as a write.
    assert updated["status"] == "committed"
    # Read the body back, so this asserts what actually landed on disk rather
    # than a status string a no-op update would also return.
    shown = _unwrap(await _call(server, "memory_show", id=written["id"]))
    # Stored bodies round-trip through frontmatter with a trailing newline.
    assert shown["body"].strip() == _SHORT_BODY
    assert [m.body.strip() for m in _stored(memory_dir)] == [_SHORT_BODY]


# ---------------------------------------------------------------------------
# Structural: no call site may omit the floor again.
# ---------------------------------------------------------------------------


_SRC_ROOT = Path(__file__).resolve().parents[1] / "src" / "bettermemory"


def _validate_write_payload_call_sites(
    src_root: Path = _SRC_ROOT,
) -> list[tuple[str, int, set[str], bool]]:
    """Every `_validate_write_payload(...)` call under `src_root`, with kwargs.

    `src_root` is a parameter so the scan can be pointed at a synthetic tree
    and proved to actually flag an unthreaded call — a source scanner nobody
    tests is one that quietly matches nothing.
    """
    sites: list[tuple[str, int, set[str], bool]] = []
    for path in sorted(src_root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                name = func.attr
            else:
                continue
            if name != "_validate_write_payload":
                continue
            named = {kw.arg for kw in node.keywords if kw.arg is not None}
            splat = any(kw.arg is None for kw in node.keywords)
            sites.append((str(path.relative_to(src_root)), node.lineno, named, splat))
    return sites


def test_every_write_validator_call_site_threads_the_floor() -> None:
    """The floor's default is 0, so FORGETTING to thread it is silent.

    That is precisely how it shipped inert: the validator grew the parameter,
    both call sites kept passing `max_content_bytes` and `max_scopes_per_write`
    and simply omitted the new one, and nothing failed. A third call site added
    later would inherit the same silence, so the requirement is checked at the
    source level rather than one behaviour test per surface.

    Opting a future call site OUT is still expressible — pass
    `min_content_tokens=0` explicitly. The point is that the decision has to be
    written down at the call site instead of defaulted into by omission.
    """
    sites = _validate_write_payload_call_sites()

    # Guard the scan itself: a rename that makes this find nothing would
    # otherwise turn the test into a permanent pass.
    assert len(sites) >= 2, f"expected the known call sites, found {sites}"

    missing = [
        f"{path}:{lineno}"
        for path, lineno, named, splat in sites
        if "min_content_tokens" not in named and not splat
    ]
    assert missing == [], (
        "these `_validate_write_payload` call sites do not thread "
        f"`[behavior] min_content_tokens`, so the floor is inert there: {missing}"
    )


def test_the_call_site_scan_flags_an_unthreaded_call(tmp_path: Path) -> None:
    """The detector above, proved against the shape that actually shipped.

    Without this, a scanner that silently matched nothing — a rename, a
    walk that skips a subpackage — would read as a permanent green. The
    synthetic module reproduces the defect verbatim: every other bound is
    threaded from config and only the new one is omitted.
    """
    (tmp_path / "handler.py").write_text(
        "def handle(deps, content, scopes):\n"
        "    return _validate_write_payload(\n"
        "        content=content,\n"
        "        scopes=scopes,\n"
        "        max_content_bytes=deps.config.behavior.max_content_bytes,\n"
        "        max_scopes_per_write=deps.config.behavior.max_scopes_per_write,\n"
        "    )\n",
        encoding="utf-8",
    )

    sites = _validate_write_payload_call_sites(tmp_path)

    assert len(sites) == 1
    _path, _lineno, named, splat = sites[0]
    assert "min_content_tokens" not in named
    assert splat is False
    # …and the same scan accepts the threaded spelling, so the check is
    # discriminating rather than just always-unhappy.
    (tmp_path / "handler.py").write_text(
        "def handle(deps, content, scopes):\n"
        "    return _validate_write_payload(\n"
        "        content=content,\n"
        "        scopes=scopes,\n"
        "        min_content_tokens=deps.config.behavior.min_content_tokens,\n"
        "    )\n",
        encoding="utf-8",
    )
    assert "min_content_tokens" in _validate_write_payload_call_sites(tmp_path)[0][2]
