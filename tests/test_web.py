"""Tests for the local web UI (T4.3 of the v1.6 plan).

Uses FastAPI's TestClient — same in-process HTTP testing pattern the
fastapi docs recommend. Skips when the [ui] extra isn't installed
(fastapi / httpx missing) so the suite stays portable.
"""

from __future__ import annotations

import contextlib
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from bettermemory.config import Config, StorageConfig
from bettermemory.store import Store

from ._mcp import call_tool as _mcp_call
from .conftest import shielded_child_env

# Reuse the commit-drift fixture helpers rather than re-implementing a
# second git harness: the parity test below has to build the exact repo
# shape `test_server_commit_drift.py` builds for the MCP surface, since
# the point is that both surfaces read the same signal.
from .test_server_commit_drift import (
    _GIT_AVAILABLE,
    _REMOTE,
    _commit_at,
    _commit_touching,
    _init_repo,
)


# Skip the whole module when the ui extra isn't available.
fastapi = pytest.importorskip("fastapi")
testclient_mod = pytest.importorskip("fastapi.testclient")
TestClient = testclient_mod.TestClient


@pytest.fixture
def memory_dir(tmp_path: Path) -> Path:
    return tmp_path / "memories"


@pytest.fixture
def store(memory_dir: Path) -> Store:
    return Store(memory_dir)


@pytest.fixture
def client(memory_dir: Path, store: Store) -> Any:
    from bettermemory.web import build_app

    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))
    app = build_app(cfg, store)
    return TestClient(app)


@pytest.fixture
def csrf_token(client: Any) -> str:
    """audit H4 — extract the per-process CSRF token from the test
    app. Read it from app.state (the regression tests below also
    cover scraping it from the rendered HTML meta tag)."""
    return client.app.state.csrf_token


# ---------------------------------------------------------------------------
# Smoke
# ---------------------------------------------------------------------------


def test_index_returns_200_with_empty_store(client: Any) -> None:
    """A fresh store with no memories should render the overview
    without crashing — empty buckets shouldn't surface as zero-row
    crashes."""
    r = client.get("/")
    assert r.status_code == 200
    assert "Overview" in r.text


def test_memories_list_renders(client: Any, store: Store) -> None:
    """Memory list view shows each memory with scope tags and a link
    to the detail page."""
    store.write(content="python list comprehension", scopes=["tools"])
    store.write(content="kubernetes notes", scopes=["infrastructure"])

    r = client.get("/memories")
    assert r.status_code == 200
    assert "python list comprehension" in r.text
    assert "kubernetes notes" in r.text
    assert "tools" in r.text
    assert "infrastructure" in r.text


def test_memories_list_search_filter(client: Any, store: Store) -> None:
    """The `q` query param filters by case-insensitive substring on
    the summary. Useful for browsing large stores."""
    store.write(content="python list comprehension", scopes=["tools"])
    store.write(content="kubernetes notes", scopes=["infrastructure"])

    r = client.get("/memories", params={"q": "python"})
    assert r.status_code == 200
    assert "python list comprehension" in r.text
    assert "kubernetes notes" not in r.text


def test_memories_list_scope_filter(client: Any, store: Store) -> None:
    """The `scope` query param is a strict scope filter — like the
    `scopes=` parameter on memory_search."""
    store.write(content="python list comprehension", scopes=["tools"])
    store.write(content="kubernetes notes", scopes=["infrastructure"])

    r = client.get("/memories", params={"scope": "tools"})
    assert r.status_code == 200
    assert "python list comprehension" in r.text
    assert "kubernetes notes" not in r.text


def test_memories_list_rejects_malformed_scope(client: Any) -> None:
    """A scope that doesn't match the MCP scope regex must 400 — same
    contract MCP handlers enforce. Without this the UI would silently
    return an empty list (set-intersection of "real scope" with
    "garbage" is empty), which masks user typos as "no results"."""
    r = client.get("/memories", params={"scope": "../etc/passwd"})
    assert r.status_code == 400


def test_memory_detail_renders(client: Any, store: Store) -> None:
    """Detail view shows the body, scopes, timestamps, and a verify
    form."""
    m = store.write(content="durable fact body content here", scopes=["tools"])
    r = client.get(f"/memories/{m.id}")
    assert r.status_code == 200
    assert "durable fact body content here" in r.text
    assert "tools" in r.text
    assert "Mark verified now" in r.text


def test_memory_detail_flags_stale_verification(client: Any, store: Store) -> None:
    """A memory verified longer ago than `verification_stale_days`
    must render a `stale (verified Nd ago)` warn cue on the detail
    page — the curation surface must not collapse verified-but-stale
    into a bare "verified", which is the exact memory the staleness
    model exists to flag. A freshly verified memory must NOT show the
    cue, so the threshold actually gates the warning rather than it
    firing on every verified row.

    The default `verification_stale_days` is 30; we stamp
    `last_verified_at` ~400 days back (well over threshold) by writing
    then updating with a backdated copy — the same pattern
    `test_links_render_on_detail` uses to seed a field the public write
    path doesn't expose directly."""
    from datetime import datetime, timedelta, timezone

    m = store.write(content="durable claim verified long ago", scopes=["tools"])
    stale_when = datetime.now(timezone.utc) - timedelta(days=400)
    store.update(m.model_copy(update={"last_verified_at": stale_when}))

    r = client.get(f"/memories/{m.id}")
    assert r.status_code == 200
    # The warn cue appears with the integer day age (>= the threshold).
    assert "stale (verified" in r.text
    assert "d ago)" in r.text
    # And it carries the warn class, mirroring the chip warn vocabulary
    # (the 2026-07 overhaul renamed .tag to .chip; the contract is the
    # warn-classed cue, not the class token's spelling).
    assert 'class="chip warn">stale' in r.text

    # A freshly verified memory must NOT trip the cue.
    fresh = store.write(content="durable claim verified just now", scopes=["tools"])
    store.mark_verified(fresh.id)
    r2 = client.get(f"/memories/{fresh.id}")
    assert r2.status_code == 200
    assert "stale (verified" not in r2.text


def test_search_uses_the_ranked_engine_not_a_substring_filter(
    client: Any, store: Store
) -> None:
    """The 2026-07 overhaul's core fix. "alpha beta" has no contiguous
    substring match in the body below — the pre-overhaul filter (a bare
    `needle in summary.lower()`) returned zero rows for exactly this
    query shape, live-reproduced against the operator store. The ranked
    engine tokenizes, so both terms hit and the memory surfaces."""
    m = store.write(
        content=(
            "alpha subsystem design notes\n\n"
            "The beta rollout flag lives in the deploy config."
        ),
        scopes=["tools"],
    )
    r = client.get("/memories", params={"q": "alpha beta"})
    assert r.status_code == 200
    assert m.id in r.text, "ranked search failed to surface a two-token match"
    assert "ranked hit" in r.text


def test_search_hits_carry_the_staleness_verdict(client: Any, store: Store) -> None:
    """Verdict parity: a never-verified hit must wear the same
    spot-check-required verdict the MCP surface reports for it —
    computed by the same compute_verification_status /
    compute_staleness_verdict pair, never web-side arithmetic."""
    store.write(content="gamma pipeline runbook", scopes=["tools"])
    r = client.get("/memories", params={"q": "gamma runbook"})
    assert r.status_code == 200
    assert "spot-check required" in r.text


def test_detail_flags_missing_cited_paths(
    client: Any, store: Store, tmp_path: Path
) -> None:
    """The detail page runs the real path-drift check: cite a file,
    delete it, and the page must show the missing bucket plus a
    non-fresh verdict — the `bettermemory try` demo, on the web.

    Which axis carries the verdict here is worth naming, because it is
    not the drift: this memory was never verified, so the CALENDAR leg
    alone earns `spot-check required`. The absence is prose-provenance
    and no longer raises a tier on its own — see
    `test_detail_page_verdict_matches_memory_show_on_a_prose_miss` for
    the calendar-fresh version of this same body, where the bucket
    renders and the verdict stays `fresh`.
    """
    target = tmp_path / "cited-then-deleted.md"
    target.write_text("ephemeral", encoding="utf-8")
    m = store.write(content=f"The runbook lives at {target}", scopes=["tools"])
    target.unlink()
    r = client.get(f"/memories/{m.id}")
    assert r.status_code == 200
    assert "Missing paths" in r.text
    assert "spot-check" in r.text


def test_eval_page_renders_the_three_rates(client: Any) -> None:
    """The effectiveness telemetry reaches the UI — the same three
    rates `eval --report` publishes, rendered read-only. On a fresh
    store the denominators are zero and the page must say n/a rather
    than inventing a number."""
    r = client.get("/eval")
    assert r.status_code == 200
    for label in ("memory_helped_rate", "endorsement_rate", "silent_miss_rate"):
        assert label in r.text
    assert "n/a" in r.text


def test_episodes_page_renders_takeaways(client: Any, store: Store) -> None:
    from bettermemory.episodes import EpisodeStore

    estore = EpisodeStore(store.root)
    estore.write(
        session_id="sess_webui_test",
        body="round 1: drained the queue",
        takeaway="round 1 takeaway marker",
    )
    r = client.get("/episodes")
    assert r.status_code == 200
    assert "round 1 takeaway marker" in r.text
    assert "sess_webui_test" in r.text


def test_episodes_page_empty_state(client: Any) -> None:
    r = client.get("/episodes")
    assert r.status_code == 200
    assert "No episodes" in r.text


def test_curation_page_previews_duplicates_without_mutating(
    client: Any, store: Store
) -> None:
    """Two same-body memories clear the auto-dedup Jaccard threshold,
    so the preview must show the pair — and the page is a dry run by
    construction (`apply` is a literal False at the only call site),
    so both memories must still be active afterwards."""
    body = "identical duplicated fact about the deploy pipeline and its flags"
    a = store.write(content=body, scopes=["tools"])
    b = store.write(content=body, scopes=["tools"])
    r = client.get("/curation")
    assert r.status_code == 200
    assert "Near-duplicates" in r.text
    assert "Preview only" in r.text
    assert store.load_one(a.id) is not None
    assert store.load_one(b.id) is not None


def test_new_pages_render_in_read_only_mode(ro_client: Any) -> None:
    """The --tunnel posture extends to every new surface: all three are
    GETs and must render read-only, wearing the badge, with no CSRF
    plumbing emitted."""
    for path in ("/eval", "/episodes", "/curation"):
        r = ro_client.get(path)
        assert r.status_code == 200, path
        assert "read-only" in r.text, path
        assert "csrf-token" not in r.text, path


def test_nav_carries_the_new_pages(client: Any) -> None:
    r = client.get("/")
    assert r.status_code == 200
    for href in ('href="/curation"', 'href="/eval"', 'href="/episodes"'):
        assert href in r.text


def test_memory_detail_404_when_missing(client: Any) -> None:
    """A request for a non-existent (well-formed) memory id returns
    404 — not a 500 or a silent empty render."""
    # 26-char ULID-shaped id that doesn't exist.
    r = client.get("/memories/01J0000000000000000000000A")
    assert r.status_code == 404


_LOOPBACK_ORIGIN = {"Origin": "http://127.0.0.1:8765"}


def _verify_headers(csrf_token: str) -> dict[str, str]:
    """Headers for a same-origin /verify POST that passes both the
    same-origin check and the per-process CSRF token check."""
    return {**_LOOPBACK_ORIGIN, "X-CSRF-Token": csrf_token}


# audit follow-up — pin all three loopback host forms accepted by
# `_same_origin` (web.py: `{"localhost", "127.0.0.1", "::1"}`). Prior
# to this parametrise only `127.0.0.1` was exercised end-to-end through
# the verify endpoint; narrowing the accept-set to a single form (or
# breaking the IPv6 bracketed-host parse) would 403 users typing
# `http://localhost:<port>` or `http://[::1]:<port>` without CI noticing.
# The bracketed-IPv6 form is RFC 3986; `urllib.parse.urlparse` strips
# the brackets and returns the bare `::1` host, matching the accept-set.
_LOOPBACK_ORIGIN_FORMS = [
    pytest.param("http://localhost:8765", id="localhost"),
    pytest.param("http://127.0.0.1:8765", id="127.0.0.1"),
    pytest.param("http://[::1]:8765", id="ipv6-bracketed"),
]


@pytest.mark.parametrize("origin", _LOOPBACK_ORIGIN_FORMS)
def test_verify_marks_memory_and_redirects(
    client: Any, store: Store, csrf_token: str, origin: str
) -> None:
    """POST /memories/{id}/verify bumps last_verified_at and 303s
    back to the detail page (PRG pattern — refreshes don't repeat
    the verify).

    Parametrised across all three loopback Origin forms accepted by
    `_same_origin` so a regression narrowing the accept-set or breaking
    IPv6 bracket parsing surfaces immediately. Each case must drive the
    full state mutation (303 + `last_verified_at` actually bumped) so a
    half-fix that 303s without doing the work also fails."""
    m = store.write(content="some claim", scopes=["tools"])
    assert m.last_verified_at is None

    r = client.post(
        f"/memories/{m.id}/verify",
        data={"note": "spot-checked"},
        headers={"Origin": origin, "X-CSRF-Token": csrf_token},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == f"/memories/{m.id}"

    reloaded = store.load_one(m.id)
    assert reloaded.last_verified_at is not None


def test_verify_404_when_missing(client: Any, csrf_token: str) -> None:
    """Posting a verify for a non-existent id returns 404 with a
    clean error — not a 500."""
    r = client.post(
        "/memories/01J0000000000000000000000A/verify",
        data={"note": ""},
        headers=_verify_headers(csrf_token),
    )
    assert r.status_code == 404


def test_verify_rejects_cross_origin(
    client: Any, store: Store, csrf_token: str
) -> None:
    """A POST carrying an Origin / Referer that doesn't point at
    loopback is rejected as a cross-site forgery. Defends against a
    malicious page submitting a form to the localhost UI from another
    open tab. (Token alone would also reject this case after the
    token check, but this exercises the same-origin gate
    independently — useful for the LAN-exposure scenario where a
    forged Origin alone shouldn't be enough to bypass.)"""
    m = store.write(content="some claim", scopes=["tools"])
    r = client.post(
        f"/memories/{m.id}/verify",
        data={"note": ""},
        headers={"Origin": "https://evil.example.com", "X-CSRF-Token": csrf_token},
        follow_redirects=False,
    )
    assert r.status_code == 403
    reloaded = store.load_one(m.id)
    assert reloaded.last_verified_at is None


def test_verify_accepts_loopback_origin(
    client: Any, store: Store, csrf_token: str
) -> None:
    """A POST with an Origin pointing at this UI's own loopback host
    AND a valid CSRF token passes both gates. Mirrors the normal in-UI
    form submission."""
    m = store.write(content="some claim", scopes=["tools"])
    r = client.post(
        f"/memories/{m.id}/verify",
        data={"note": ""},
        headers=_verify_headers(csrf_token),
        follow_redirects=False,
    )
    assert r.status_code == 303


def test_verify_rejects_oversized_note(
    client: Any, store: Store, csrf_token: str
) -> None:
    """A note longer than 500 chars returns 400 without bumping
    last_verified_at — same cap discipline as `claim_excerpts`."""
    m = store.write(content="some claim", scopes=["tools"])
    r = client.post(
        f"/memories/{m.id}/verify",
        data={"note": "x" * 501},
        headers=_verify_headers(csrf_token),
        follow_redirects=False,
    )
    assert r.status_code == 400
    reloaded = store.load_one(m.id)
    assert reloaded.last_verified_at is None


def test_verify_rejects_headerless_post(client: Any, store: Store) -> None:
    """Regression for the M3 audit finding: a POST with neither
    Origin nor Referer must be rejected. The prior behaviour accepted
    header-less POSTs on the rationale that some browser configs
    strip Referer — but modern browsers reliably send Origin on
    POSTs, so a header-less request is a non-browser tool. In the
    LAN-exposed configuration, accepting it would be an
    unauthenticated state-mutation primitive for any host that can
    reach the socket."""
    m = store.write(content="some claim", scopes=["tools"])
    r = client.post(
        f"/memories/{m.id}/verify",
        data={"note": ""},
        follow_redirects=False,
    )
    assert r.status_code == 403
    reloaded = store.load_one(m.id)
    assert reloaded.last_verified_at is None


# ---------------------------------------------------------------------------
# H4 regression — per-process CSRF token
# ---------------------------------------------------------------------------


def test_verify_rejects_post_without_csrf_token(client: Any, store: Store) -> None:
    """audit H4 — a POST that passes the same-origin check but
    carries no X-CSRF-Token (and no `csrf_token` form field) must be
    rejected. The prior same-origin-only gate accepted a forged
    `Origin: http://localhost:8765` from any non-browser client on
    the LAN under --host 0.0.0.0; the token check closes that
    window."""
    m = store.write(content="some claim", scopes=["tools"])
    r = client.post(
        f"/memories/{m.id}/verify",
        data={"note": ""},
        headers=_LOOPBACK_ORIGIN,  # no X-CSRF-Token
        follow_redirects=False,
    )
    assert r.status_code == 403
    reloaded = store.load_one(m.id)
    assert reloaded.last_verified_at is None


def test_verify_rejects_post_with_wrong_csrf_token(client: Any, store: Store) -> None:
    """audit H4 — a POST with a token that doesn't match the
    per-process value must be rejected. Guards against an attacker
    who guessed the token shape (`secrets.token_urlsafe(32)` output)
    but didn't read it from the page."""
    m = store.write(content="some claim", scopes=["tools"])
    r = client.post(
        f"/memories/{m.id}/verify",
        data={"note": ""},
        headers={**_LOOPBACK_ORIGIN, "X-CSRF-Token": "this-is-not-the-token"},
        follow_redirects=False,
    )
    assert r.status_code == 403
    reloaded = store.load_one(m.id)
    assert reloaded.last_verified_at is None


def test_verify_accepts_token_scraped_from_rendered_page(
    client: Any, store: Store
) -> None:
    """audit H4 — the token rendered into the <meta name="csrf-token">
    tag on any page is the one the server accepts. Exercises the
    end-to-end contract: a real browser would parse the meta tag,
    set X-CSRF-Token, and the server would compare_digest it against
    the per-process value."""
    import re

    m = store.write(content="some claim", scopes=["tools"])
    page = client.get(f"/memories/{m.id}")
    assert page.status_code == 200
    match = re.search(r'<meta name="csrf-token" content="([^"]+)"', page.text)
    assert match is not None, "expected a csrf-token meta tag on every page"
    token = match.group(1)
    assert token, "csrf-token must be a non-empty string"

    r = client.post(
        f"/memories/{m.id}/verify",
        data={"note": ""},
        headers={**_LOOPBACK_ORIGIN, "X-CSRF-Token": token},
        follow_redirects=False,
    )
    assert r.status_code == 303


def test_verify_accepts_csrf_via_form_field(
    client: Any, store: Store, csrf_token: str
) -> None:
    """audit H4 — the token can also be supplied via a `csrf_token`
    form field for plain <form method=post> submissions that aren't
    able to set custom request headers without JavaScript. The inline
    JS in _layout adds a hidden input; this test exercises the
    server-side acceptance of that hidden-input path directly."""
    m = store.write(content="some claim", scopes=["tools"])
    r = client.post(
        f"/memories/{m.id}/verify",
        data={"note": "", "csrf_token": csrf_token},
        headers=_LOOPBACK_ORIGIN,
        follow_redirects=False,
    )
    assert r.status_code == 303


def test_csrf_token_is_stable_within_process(client: Any) -> None:
    """audit H4 — the token is generated once at app-build time and
    stays constant for the process lifetime. Rotating per-request
    would break submits across tabs and buy no defence against the
    threat model (a local attacker who can read one page can read
    any page)."""
    import re

    page1 = client.get("/")
    page2 = client.get("/memories")
    t1 = re.search(r'<meta name="csrf-token" content="([^"]+)"', page1.text)
    t2 = re.search(r'<meta name="csrf-token" content="([^"]+)"', page2.text)
    assert t1 is not None and t2 is not None
    assert t1.group(1) == t2.group(1)


def test_csrf_token_differs_across_apps(memory_dir: Path, store: Store) -> None:
    """audit H4 — two independently built apps get distinct random
    tokens. Guards against accidentally hoisting the token to module
    scope (which would persist across server restarts and undermine
    the "regenerate on restart" property)."""
    from bettermemory.web import build_app

    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))
    app1 = build_app(cfg, store)
    app2 = build_app(cfg, store)
    assert app1.state.csrf_token != app2.state.csrf_token


def test_non_loopback_bind_logs_warning(caplog: Any) -> None:
    """audit H4 — binding to a non-loopback host emits a clear
    WARNING about the unencrypted-transport caveat. Exercise both
    branches via the extracted ``_warn_if_non_loopback_bind`` helper:
    loopback hosts return False and emit nothing, non-loopback hosts
    return True and emit a single WARNING record."""
    import logging

    from bettermemory.web import _is_loopback_bind, _warn_if_non_loopback_bind

    assert _is_loopback_bind("127.0.0.1") is True
    assert _is_loopback_bind("localhost") is True
    assert _is_loopback_bind("::1") is True
    # 0.0.0.0 is the "bind to all interfaces" wildcard; treat it as
    # non-loopback because it exposes the socket to every NIC.
    assert _is_loopback_bind("0.0.0.0") is False

    # Loopback path: no warning, return False (didn't fire).
    with caplog.at_level(logging.WARNING, logger="bettermemory"):
        assert _warn_if_non_loopback_bind("127.0.0.1") is False
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING], (
        "loopback bind must not log a warning"
    )

    caplog.clear()
    # Non-loopback path: exactly one warning, return True (fired).
    with caplog.at_level(logging.WARNING, logger="bettermemory"):
        assert _warn_if_non_loopback_bind("0.0.0.0") is True
    warn_records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warn_records) == 1, (
        f"expected exactly one WARNING for non-loopback bind, got {warn_records!r}"
    )
    assert "non-loopback" in warn_records[0].getMessage().lower()
    assert "csrf" in warn_records[0].getMessage().lower()


def test_health_renders(client: Any, store: Store) -> None:
    """/health surfaces the memory_health buckets. With one memory in
    the store, the active count should show as 1."""
    store.write(content="a memory", scopes=["tools"])
    r = client.get("/health")
    assert r.status_code == 200
    assert "Health" in r.text
    assert "active memories" in r.text.lower()


def test_tombstones_renders(client: Any, store: Store) -> None:
    """/tombstones lists removed memories with their reasons."""
    m = store.write(content="will be removed", scopes=["tools"])
    store.tombstone(m.id, reason="test removal")

    r = client.get("/tombstones")
    assert r.status_code == 200
    assert "will be removed" in r.text
    assert "test removal" in r.text


def test_html_escapes_user_content(client: Any, store: Store) -> None:
    """Memory bodies that contain HTML special characters must be
    escaped on render — no XSS via memory_write."""
    store.write(
        content="<script>alert('xss')</script> with brackets",
        scopes=["tools"],
    )
    r = client.get("/memories")
    assert r.status_code == 200
    # The raw <script> tag must not appear unescaped.
    assert "<script>alert" not in r.text
    # The escaped form must appear.
    assert "&lt;script&gt;" in r.text


def test_links_render_on_detail(client: Any, store: Store) -> None:
    """Memories with `links` show them on the detail view with the
    type label and a link to the target."""
    from bettermemory.models import LinkType, MemoryLink

    a = store.write(content="target memory", scopes=["tools"])
    b = store.write(content="source memory", scopes=["tools"])
    b_with_links = b.model_copy(
        update={
            "links": [
                MemoryLink(type=LinkType.SUPERSEDES, target_id=a.id, note="newer")
            ]
        }
    )
    store.update(b_with_links)

    r = client.get(f"/memories/{b.id}")
    assert r.status_code == 200
    assert "supersedes" in r.text
    assert a.id in r.text
    assert "newer" in r.text


def test_navigation_links_present(client: Any) -> None:
    """Every page should carry the same header nav so users can move
    between sections. Sanity check on the layout chrome."""
    r = client.get("/")
    assert r.status_code == 200
    for path in ("/", "/memories", "/health", "/tombstones"):
        assert f'href="{path}"' in r.text


# ---------------------------------------------------------------------------
# Verdict parity with the MCP surface
#
# The web's entire trust claim is "it computes nothing itself". These
# tests are the enforcement: each one fails if a surface starts deriving
# its own answer, or if a config knob / response decorator lands on the
# MCP side only.
# ---------------------------------------------------------------------------


def _app_with(memory_dir: Path, store: Store, behavior: Any) -> Any:
    from bettermemory.web import build_app

    cfg = Config(storage=StorageConfig(directory=str(memory_dir)), behavior=behavior)
    return TestClient(build_app(cfg, store))


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not on PATH")
def test_search_hits_fold_in_commit_drift_like_the_mcp_surface(
    memory_dir: Path, store: Store, tmp_path: Path, monkeypatch: Any
) -> None:
    """The 🔴 regression this section exists for.

    A memory verified moments ago is calendar-fresh and cites a path
    that still exists, so verification + path drift alone say "fresh" —
    which is exactly the initial verdict `hit_to_dict` sets and the old
    `_render_hits` recomputed. `memory_search` then runs
    `attach_commit_drift_counts`, which counts the commits that touched
    the cited anchor since the verify and RAISES the verdict. The web
    skipped that step, so this page rendered a green "fresh" chip while
    its own detail page (which folds commit drift in) said spot-check —
    the false-negative direction, steering a curator away from the
    spot-check.
    """
    from datetime import datetime, timezone

    from bettermemory.origin import Origin

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    notes = repo / "notes.md"
    notes.write_text("anchor\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "notes.md"], cwd=repo, check=True, capture_output=True
    )
    _commit_at(repo, "add notes", when=datetime(2025, 1, 1, tzinfo=timezone.utc))

    origin = Origin(cwd=str(repo), repo=_REMOTE, branch="main")
    m = store.write(
        content=f"epsilon widget rules live in {notes}",
        scopes=["tools"],
        origin=origin,
    )
    store.mark_verified(m.id)
    # A commit that TOUCHES the cited anchor, after the verify.
    _commit_touching(repo, "post", when=datetime(2099, 1, 1, tzinfo=timezone.utc))

    monkeypatch.setattr("bettermemory.web.capture_origin", lambda cwd=None: origin)
    client = _app_with(memory_dir, store, Config().behavior)

    r = client.get("/memories", params={"q": "epsilon widget"})
    assert r.status_code == 200
    assert "commit drift: 1 since verify" in r.text
    assert "spot-check recommended" in r.text
    # And specifically NOT the green chip the pre-fix render produced.
    assert '<span class="chip ok">fresh</span>' not in r.text
    # The detail page for the same memory agrees — that agreement is the
    # property the bug broke.
    detail = client.get(f"/memories/{m.id}")
    assert "commit drift: 1 since verify" in detail.text
    assert "spot-check recommended" in detail.text


async def test_detail_page_verdict_matches_memory_show_on_a_prose_miss(
    memory_dir: Path, store: Store, tmp_path: Path
) -> None:
    """The detail page's verdict, checked against the MCP answer for the
    same memory rather than against a hard-coded string.

    `_render_memory_detail` claims it "cannot disagree with what the
    model sees for the same memory" — and it did, on the one input the
    two sites did not share: it folded the FULL `path_drift.missing`
    count into `compute_staleness_verdict` while `handlers/show.py`
    folded in `claim_anchored_missing`. The trigger is ordinary, not
    exotic: any calendar-fresh memory whose body mentions a path this
    machine does not have and nobody attested — a remote host's config,
    an `/etc/...` example — read `fresh` to the model and `spot-check
    recommended` on the page, sending a curator to re-verify a memory the
    ~0-of-15 sweep says is almost certainly fine.

    Both halves of the provenance contract are pinned here, because the
    cheap way to make the verdicts agree would be to stop rendering the
    evidence: the path stays on the page, it just no longer speaks as a
    verdict.
    """
    import html as html_mod

    from bettermemory.server import build_server
    from bettermemory.session import SessionState
    from bettermemory.web import _verdict_chip

    cited = tmp_path / "prose-only.toml"
    cited.write_text("x\n", encoding="utf-8")
    m = store.write(
        content=f"The bastion reads its collector config from `{cited}`.",
        scopes=["tools"],
    )
    # Verified just now (calendar-fresh) with NOTHING attested, then the
    # cited file goes away — so the absence is prose-provenance only.
    store.mark_verified(m.id)
    cited.unlink()

    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))
    server = build_server(config=cfg, store=store, state=SessionState())
    shown = await _mcp_call(server, "memory_show", {"id": m.id})

    # Premise, stated rather than assumed: the model is shown the absence
    # and still told `fresh`.
    assert shown["path_drift"]["missing"] == [str(cited)]
    assert shown["path_drift"]["claim_anchored_missing"] == []
    assert shown["staleness_verdict"] == "fresh"

    client = _app_with(memory_dir, store, cfg.behavior)
    detail = client.get(f"/memories/{m.id}")
    assert detail.status_code == 200
    assert _verdict_chip(shown["staleness_verdict"]) in detail.text
    # The pre-fix render, named explicitly so a regression cannot pass by
    # rendering both chips.
    assert "spot-check recommended" not in detail.text
    # And the evidence is still on the page.
    assert "Missing paths" in detail.text
    assert html_mod.escape(str(cited)) in detail.text


def test_search_threads_every_ranking_input_the_handler_threads(
    memory_dir: Path, store: Store, monkeypatch: Any
) -> None:
    """Ranking parity, pinned against future one-sided knobs.

    `handlers.search.resolve_ranking_inputs` is the single source of the
    config-driven ranker inputs, and every field it carries except the
    shared event read is a `search.search` keyword. Asserting against
    `RankingInputs._fields` means a knob added to the helper (and wired
    into the MCP handler) fails HERE until the web threads it too —
    which is the failure mode that let the web run the same ranker with
    endorsement/demotion/corroboration all silently dropped.
    """
    from bettermemory.config import BehaviorConfig
    from bettermemory.handlers.search import RankingInputs
    from bettermemory.search import search as real_search

    captured: dict[str, Any] = {}

    def spy(*args: Any, **kwargs: Any) -> Any:
        captured.update(kwargs)
        return real_search(*args, **kwargs)

    monkeypatch.setattr("bettermemory.web.run_search", spy)

    store.write(content="delta pipeline runbook", scopes=["tools"])
    client = _app_with(
        memory_dir,
        store,
        BehaviorConfig(
            endorsement_boost=True,
            outcome_demotion=True,
            corroboration_boost=True,
            recency_boost_half_life_days=7.0,
        ),
    )
    r = client.get("/memories", params={"q": "delta runbook"})
    assert r.status_code == 200

    expected = set(RankingInputs._fields) - {"events"}
    assert expected <= set(captured), (
        f"web search dropped ranking inputs: {sorted(expected - set(captured))}"
    )
    assert captured["corroboration_boost"] is True
    assert captured["half_life_days"] == 7.0
    # Both tallies RAN (empty dicts), rather than being left at the
    # ranker-neutral None the dropped-input shape produced.
    assert captured["applied_by_id"] == {}
    assert captured["negative_by_id"] == {}


def test_every_ranker_input_is_classified_shared_or_divergent() -> None:
    """The ledger behind the page's ranking claim, enforced.

    `RankingInputs` covers only the `[behavior]` knobs, so the test above
    can see a NEW knob landing there — and is blind to a new `search.search`
    keyword that never travels through the helper. `semantic_model` is the
    proof: it is a ranker input the MCP surface resolves and this page does
    not, and no parity test could see it. The two frozensets must partition
    the ranker's keyword-only signature, so the next such input has to be
    threaded or filed with a reason before it can ship.
    """
    import inspect

    from bettermemory.search import search as real_search
    from bettermemory.web import _RANKER_INPUTS_DIVERGENT, _RANKER_INPUTS_SHARED

    kwargs = {
        name
        for name, param in inspect.signature(real_search).parameters.items()
        if param.kind is inspect.Parameter.KEYWORD_ONLY
    }
    assert not (_RANKER_INPUTS_SHARED & _RANKER_INPUTS_DIVERGENT)
    assert _RANKER_INPUTS_SHARED | _RANKER_INPUTS_DIVERGENT == kwargs, (
        "unclassified search.search inputs: "
        f"{sorted(kwargs - _RANKER_INPUTS_SHARED - _RANKER_INPUTS_DIVERGENT)}"
    )
    # The two ledgers agree: every `[behavior]` knob the handler threads is
    # on the SHARED side, so a knob cannot be classified divergent here
    # while the test above proves the route passes it.
    from bettermemory.handlers.search import RankingInputs

    assert set(RankingInputs._fields) - {"events"} <= _RANKER_INPUTS_SHARED
    # And the one divergence the page has to disclose out loud.
    assert "semantic_model" in _RANKER_INPUTS_DIVERGENT


def test_search_stays_lexical_and_says_so_under_a_semantic_config(
    memory_dir: Path, store: Store, monkeypatch: Any
) -> None:
    """The divergence `_RANKER_INPUTS_DIVERGENT` files, pinned end to end.

    With `search_mode = "semantic"` and a resolvable model, `memory_search`
    ranks by the embedding scorer; this route resolves no model, downgrades
    the mode to `hybrid`, and fuses keyword + BM25 — so the two surfaces can
    return different rows for the same query (reproduced: a paraphrase-only
    memory that memory_search ranks FIRST never appears here at all). The
    contract is not that they agree; it is that the page does not pass the
    lexical order off as the model's.

    The note is per-config because the divergence is: the same lexical order
    stands next to a THIRD fused leg under `hybrid` + `semantic_dedup`, and
    next to a ranking that fuses neither of its two legs under
    `search_mode = "semantic"`. Which text belongs to which config is
    pinned against the handler in
    `test_lexical_only_note_fires_exactly_when_a_semantic_leg_ranks`.
    """
    import html

    from bettermemory.config import BehaviorConfig
    from bettermemory.search import search as real_search
    from bettermemory.semantic_setup import _semantic_model_or_none
    from bettermemory.web import (
        _LEXICAL_ONLY_FUSED_NOTE,
        _LEXICAL_ONLY_SEMANTIC_NOTE,
    )

    captured: dict[str, Any] = {}

    def spy(*args: Any, **kwargs: Any) -> Any:
        captured.update(kwargs)
        return real_search(*args, **kwargs)

    monkeypatch.setattr("bettermemory.web.run_search", spy)
    # Make a model resolvable in this process, so the premise is real
    # rather than assumed: the factory the MCP handler calls returns one
    # for both configs below, and this route still must not reach for it.
    monkeypatch.setattr("bettermemory.semantic.get_model", lambda *a, **k: object())

    store.write(content="theta rollout runbook", scopes=["tools"])
    for behavior, expected_note, other_note in (
        (
            BehaviorConfig(search_mode="semantic"),
            _LEXICAL_ONLY_SEMANTIC_NOTE,
            _LEXICAL_ONLY_FUSED_NOTE,
        ),
        (
            BehaviorConfig(search_mode="hybrid", semantic_dedup=True),
            _LEXICAL_ONLY_FUSED_NOTE,
            _LEXICAL_ONLY_SEMANTIC_NOTE,
        ),
    ):
        cfg = Config(
            storage=StorageConfig(directory=str(memory_dir)), behavior=behavior
        )
        assert _semantic_model_or_none(cfg) is not None, "premise: MCP has a model"
        captured.clear()
        client = _app_with(memory_dir, store, behavior)
        r = client.get("/memories", params={"q": "theta runbook"})
        assert r.status_code == 200
        assert captured.get("semantic_model") is None
        assert captured["mode"] == "hybrid"
        # The caveat is on the page, escaped like every other rendered
        # string, so a reader knows this order is not memory_search's — and
        # it is the caveat for THIS config, not the other one's divergence.
        assert html.escape(expected_note) in r.text
        assert html.escape(other_note) not in r.text

    # And it stays OFF when both surfaces genuinely fuse the same two legs —
    # a permanent caveat would be noise. That is now the NO-EXTRA case rather
    # than "the default config": with an extra importable the default config
    # does diverge, because `hybrid` resolves a model and this page still
    # does not. Pinning importability False is what makes the assertion about
    # the caveat's gate instead of about the machine running the suite.
    monkeypatch.setattr(
        "bettermemory.semantic_setup._embeddings_extra_importable", lambda: False
    )
    client = _app_with(memory_dir, store, BehaviorConfig())
    r = client.get("/memories", params={"q": "theta runbook"})
    for note in (_LEXICAL_ONLY_FUSED_NOTE, _LEXICAL_ONLY_SEMANTIC_NOTE):
        assert html.escape(note) not in r.text
    assert "keyword + BM25 fusion only" not in r.text


# (search_mode, semantic_dedup, does a semantic leg RANK in memory_search).
# The third column is not asserted from reading the handler — the test below
# drives it and fails if the handler's answer moves.
_SEMANTIC_LEG_MATRIX = [
    # Every row below runs with an embeddings extra pinned IMPORTABLE, which
    # is what the patched `get_model` already implied. That is a PREMISE of
    # this matrix, not an omission: the gate deliberately does not probe
    # importability, so the config where none is importable is not a row here
    # — the note still fires there, and what it must then SAY is pinned by
    # `test_fused_caveat_holds_when_no_embeddings_extra_can_load`.
    #
    # `hybrid` + dedup off used to be the "nothing asks for the model" row.
    # It is now the headline case instead: installing the extra is by itself
    # enough to grow the default mode its third leg, because the measured
    # recall gain was large and requiring an unrelated write-time flag to
    # unlock a search improvement was a foot-gun. With NO extra importable
    # this same row still resolves nothing — pinned separately in
    # `test_semantic.py::test_factory_hybrid_stays_silent_without_an_extra`,
    # since this matrix holds importability fixed.
    ("hybrid", False, True),
    # Dedup's model also reaches `hybrid`, as it always did.
    ("hybrid", True, True),
    ("", True, True),  # unset search_mode resolves to hybrid on both sides
    # Retrieval asks for the model itself; the dedup flag is irrelevant.
    ("semantic", False, True),
    ("semantic", True, True),
    # The false-positive class: the gate `_semantic_model_configured` is OPEN
    # (dedup wants a model for the write path) and no semantic leg ranks on
    # either surface, because the handler resolves a model only for
    # hybrid/semantic. A caveat here would invent a divergence.
    ("keyword", True, False),
    ("bm25", True, False),
    ("keyword", False, False),
    ("bm25", False, False),
]


async def test_lexical_only_note_fires_exactly_when_a_semantic_leg_ranks(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """`_lexical_only_note`'s gate, pinned against the handler it describes.

    The caveat's predicate used to be `_semantic_model_configured` alone —
    the model-LOAD gate, which `semantic_dedup = true` opens for the write
    path under ANY search mode. Driven across the config matrix, that fired
    the caveat under `search_mode = "keyword"` / `"bm25"` + dedup, where
    `memory_search` passes `semantic_model=None` and both surfaces run the
    same single scorer.

    Every row runs with an extra importable, deliberately — see the matrix
    comment. The gate's other false-positive class, `semantic_dedup = true`
    with nothing importable, is not a gate question at all: the note fires
    there by design and it is its TEXT that has to survive, which
    `test_fused_caveat_holds_when_no_embeddings_extra_can_load` drives.

    Which legs ran is observed, not inferred: `_score_semantic` /
    `_score_keyword` / `_score_bm25` are spied at their `search` call sites,
    so this also proves each note's TEXT — the fused note claims a third leg
    beside these two, the semantic note claims neither of them runs.
    """
    import bettermemory.search as search_mod
    from bettermemory.config import BehaviorConfig, StorageConfig
    from bettermemory.models import Memory
    from bettermemory.server import build_server
    from bettermemory.session import SessionState
    from bettermemory.web import (
        _LEXICAL_ONLY_FUSED_NOTE,
        _LEXICAL_ONLY_SEMANTIC_NOTE,
        _lexical_only_note,
    )

    legs: list[str] = []

    def _spy(name: str) -> Any:
        real = getattr(search_mod, name)

        def wrapper(*a: Any, **k: Any) -> Any:
            legs.append(name)
            return real(*a, **k)

        return wrapper

    for scorer in ("_score_keyword", "_score_bm25"):
        monkeypatch.setattr(search_mod, scorer, _spy(scorer))

    def fake_semantic(candidates: list[Memory], *a: Any, **k: Any) -> Any:
        # Stands in for the real cosine scorer so the matrix runs without
        # an embeddings extra (and without numpy). Shape only — this test
        # asserts WHICH scorers run, never their order.
        legs.append("_score_semantic")
        return [(m, 1.0, ["theta"]) for m in candidates]

    monkeypatch.setattr(search_mod, "_score_semantic", fake_semantic)
    # A resolvable model, so every "the handler resolves one" row is real.
    monkeypatch.setattr("bettermemory.semantic.get_model", lambda *a, **k: object())
    # Hold extra-importability fixed at True. It is now an INPUT to the gate
    # (hybrid resolves on an importable extra), so leaving it to the ambient
    # environment would make this matrix mean different things on the plain
    # CI legs and the embeddings ones.
    monkeypatch.setattr(
        "bettermemory.semantic_setup._embeddings_extra_importable", lambda: True
    )

    for i, (mode_cfg, dedup, semantic_expected) in enumerate(_SEMANTIC_LEG_MATRIX):
        label = f"search_mode={mode_cfg!r}, semantic_dedup={dedup}"
        md = tmp_path / f"store{i}"
        store = Store(md)
        store.write(content="theta rollout runbook alpha", scopes=["tools"])
        cfg = Config(
            storage=StorageConfig(directory=str(md)),
            behavior=BehaviorConfig(search_mode=mode_cfg, semantic_dedup=dedup),
        )
        server = build_server(config=cfg, store=store, state=SessionState())
        legs.clear()
        payload = await _mcp_call(server, "memory_search", {"query": "theta runbook"})
        assert payload is not None
        ran = set(legs)
        assert ("_score_semantic" in ran) is semantic_expected, label

        note = _lexical_only_note(cfg)
        assert bool(note) is semantic_expected, label
        if not semantic_expected:
            continue
        if "_score_keyword" in ran or "_score_bm25" in ran:
            # A leg on TOP of the two this page fuses.
            assert ran == {"_score_keyword", "_score_bm25", "_score_semantic"}, label
            assert note == _LEXICAL_ONLY_FUSED_NOTE, label
        else:
            # Neither of this page's legs contributed to that ranking.
            assert ran == {"_score_semantic"}, label
            assert note == _LEXICAL_ONLY_SEMANTIC_NOTE, label


async def test_fused_caveat_holds_when_no_embeddings_extra_can_load(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """The config the matrix above holds fixed away: `hybrid` +
    `semantic_dedup = true` on a machine where nothing imports.

    `_semantic_model_configured` opens on that flag ALONE, without probing
    any install, so the caveat renders here — and it is supposed to. The
    gate is two conditions on purpose: narrowing it with an importability
    probe is the merge `semantic_setup._semantic_rank_leg_active` refuses
    and `docs/incidents/2026-08-01-broken-optional-extra-killed-retrieval.md`
    records being attempted, because these notes describe what the HANDLER
    does rather than what this process could import.

    The burden that leaves is on the sentence, and the sentence failed it:
    it read "An embeddings extra is installed and search_mode is hybrid",
    which told this reader they had an extra they do not have and promised
    a reordering that cannot happen — measured below, the handler fuses the
    same two legs the page fuses. The contract is not a phrasing; it is
    that ONE string has to be true on both machines, which is what forces
    the install to appear as a condition with both branches spelled out.
    """
    import bettermemory.search as search_mod
    from bettermemory.config import BehaviorConfig, StorageConfig
    from bettermemory.models import Memory
    from bettermemory.server import build_server
    from bettermemory.session import SessionState
    from bettermemory.web import _LEXICAL_ONLY_FUSED_NOTE, _lexical_only_note

    legs: list[str] = []

    def _spy(name: str) -> Any:
        real = getattr(search_mod, name)

        def wrapper(*a: Any, **k: Any) -> Any:
            legs.append(name)
            return real(*a, **k)

        return wrapper

    for scorer in ("_score_keyword", "_score_bm25"):
        monkeypatch.setattr(search_mod, scorer, _spy(scorer))

    def fake_semantic(candidates: list[Memory], *a: Any, **k: Any) -> Any:
        # Same stand-in the matrix uses: a regression that resolves a model
        # here fails the assertion below instead of dying inside a real
        # cosine scorer on a machine with no extra.
        legs.append("_score_semantic")
        return [(m, 1.0, ["theta"]) for m in candidates]

    monkeypatch.setattr(search_mod, "_score_semantic", fake_semantic)

    # One machine, told consistently: nothing imports, nothing is on disk,
    # and the factory therefore hands back no model. Patched at the probe
    # `semantic.extra_importable` rather than at a derived predicate, so
    # `_embeddings_extra_importable` and `resolve_provider` both run for
    # real — and so the row means the same thing on a CI leg that has an
    # extra installed as on one that does not.
    monkeypatch.setattr("bettermemory.semantic.extra_importable", lambda _module: False)
    monkeypatch.setattr("bettermemory.semantic._torch_extra_installed", lambda: False)
    monkeypatch.setattr(
        "bettermemory.semantic._fastembed_extra_installed", lambda: False
    )
    monkeypatch.setattr("bettermemory.semantic.get_model", lambda *a, **k: None)

    md = tmp_path / "store"
    store = Store(md)
    store.write(content="theta rollout runbook alpha", scopes=["tools"])
    cfg = Config(
        storage=StorageConfig(directory=str(md)),
        behavior=BehaviorConfig(search_mode="hybrid", semantic_dedup=True),
    )
    server = build_server(config=cfg, store=store, state=SessionState())
    payload = await _mcp_call(server, "memory_search", {"query": "theta runbook"})
    assert payload is not None
    # Measured, not assumed: `memory_search` ran exactly this page's two legs.
    assert set(legs) == {"_score_keyword", "_score_bm25"}, legs

    note = _lexical_only_note(cfg)
    assert note == _LEXICAL_ONLY_FUSED_NOTE, "the gate fires here by design"
    assert "An embeddings extra is installed" not in note, (
        "the caveat asserts an install the gate never checked — this reader "
        "has none. Name the extra as a condition instead."
    )
    for branch in ("with one installed", "with none"):
        assert branch in note, (
            f"the caveat no longer spells out the {branch!r} branch. Reword "
            "freely, but the same string renders with and without an extra, "
            "so it has to be true in both cases."
        )


def test_lexical_only_note_stays_silent_and_inert_on_an_unknown_search_mode(
    memory_dir: Path, store: Store
) -> None:
    """An unparseable `search_mode` gets no caveat, and cannot reach the page.

    `memory_search` raises on an unknown mode before it ranks anything (the
    `mode` entry in `_RANKER_INPUTS_DIVERGENT`), so it produces no ranking to
    diverge FROM — a caveat comparing this page's order to a search that
    errors would describe a divergence that never happens. The route
    still renders (it coerces the unknown value to `hybrid`), and because no
    config value is interpolated into the caveat, a markup-shaped
    `search_mode` has no path onto the page through it. Differently-cased
    `"Semantic"` is the same case: `_semantic_model_configured` normalises,
    the handler does not, so the handler raises.
    """
    import html

    from bettermemory.config import BehaviorConfig
    from bettermemory.web import _lexical_only_note

    store.write(content="theta rollout runbook", scopes=["tools"])
    hostile = '<script>alert("mode")</script>'
    for mode_cfg in (hostile, "Semantic", "emantic"):
        behavior = BehaviorConfig(search_mode=mode_cfg, semantic_dedup=True)
        cfg = Config(
            storage=StorageConfig(directory=str(memory_dir)), behavior=behavior
        )
        assert _lexical_only_note(cfg) == "", mode_cfg
        client = _app_with(memory_dir, store, behavior)
        r = client.get("/memories", params={"q": "theta runbook"})
        assert r.status_code == 200
        assert "keyword + BM25 fusion only" not in r.text
        # The page carries its own inline CSRF script, so look for the
        # payload rather than for `<script>`: the hostile value reaches the
        # response neither raw nor escaped, because it is never rendered.
        assert "alert(" not in r.text
        assert hostile not in r.text
        assert html.escape(hostile) not in r.text


def test_search_hits_carry_recent_negative_outcomes(
    memory_dir: Path, store: Store
) -> None:
    """`attach_recent_negative_outcomes` is part of the hit pipeline the
    model sees: a memory the model explicitly ignored is annotated so it
    stops re-surfacing it. A curator scanning the same ranked list gets
    the same annotation instead of a row that looks clean."""
    from bettermemory.events import Recorder

    m = store.write(content="zeta deployment checklist", scopes=["tools"])
    rec = Recorder(root=store.root, session_id="test-negative")
    rec.record("use", ids=[m.id], outcome="ignored", auto=False)

    client = _app_with(memory_dir, store, Config().behavior)
    r = client.get("/memories", params={"q": "zeta checklist"})
    assert r.status_code == 200
    assert "ignored ×1" in r.text


def test_health_routes_thread_the_cold_endorsement_ratio_threshold(
    memory_dir: Path, store: Store, monkeypatch: Any
) -> None:
    """Both health-bearing routes must pass the same knob the
    `memory_health` handler passes. Dropping it silently reverted the
    cold-endorsement bucket to its strict `explicit == 0` semantics on
    the web only — the page a curator uses to decide what to prune."""
    from bettermemory.config import BehaviorConfig
    from bettermemory.health import report_for_directory as real_report

    captured: list[dict[str, Any]] = []

    def spy(root: Any, **kwargs: Any) -> Any:
        captured.append(kwargs)
        return real_report(root, **kwargs)

    monkeypatch.setattr("bettermemory.web.report_for_directory", spy)

    client = _app_with(
        memory_dir, store, BehaviorConfig(cold_endorsement_ratio_threshold=0.25)
    )
    assert client.get("/").status_code == 200
    assert client.get("/health").status_code == 200
    assert len(captured) == 2, "both routes must build a health report"
    for kwargs in captured:
        assert kwargs["cold_endorsement_ratio_threshold"] == 0.25


def test_curation_renders_polarity_skipped_pairs() -> None:
    """The consolidate engine's polarity/numeric guard produces pairs
    that are conflicts, not duplicates. The page dropped them entirely,
    so a store whose ONLY finding was a skipped pair rendered "nothing
    to curate" — the empty state actively contradicting the report."""
    from bettermemory.consolidate import ConsolidateReport, PolaritySkippedPair
    from bettermemory.web import _render_curation

    report = ConsolidateReport(
        polarity_skipped=[
            PolaritySkippedPair(
                memory_id_a="01AAAAAAAAAAAAAAAAAAAAAAAA",
                summary_a="always use uv for dependency management",
                memory_id_b="01BBBBBBBBBBBBBBBBBBBBBBBB",
                summary_b="never use uv for dependency management",
                similarity=0.93,
                method="jaccard",
                detector="polarity",
            )
        ]
    )
    out = _render_curation(report)
    assert "Polarity-skipped" in out
    assert "/memories/01AAAAAAAAAAAAAAAAAAAAAAAA" in out
    assert "/memories/01BBBBBBBBBBBBBBBBBBBBBBBB" in out
    assert "93% similar" in out
    assert "memory_conflicts" in out
    # It counts as content: the empty state must not fire alongside it.
    assert "Nothing to curate" not in out
    # And the empty state names the bucket, so "nothing to curate" is a
    # claim about polarity-skipped pairs too.
    assert "polarity-skipped pairs" in _render_curation(ConsolidateReport())


def test_eval_rate_cell_flags_a_clamped_rate() -> None:
    """`RateCI` clamps a torn/windowed rate to 1.0 and flags it; the CLI
    prints the caveat. The web table dropped the flag, publishing a bare
    1.00 next to a numerator larger than its denominator."""
    from bettermemory.eval import RateCI
    from bettermemory.web import _fmt_rate

    torn = RateCI.from_counts(7, 3)
    assert torn.torn_read is True
    cell = _fmt_rate(torn)
    assert "clamped to 1.0" in cell
    assert "7/3" in cell
    assert "clamped" not in _fmt_rate(RateCI.from_counts(1, 3))


def _empty_health_report(**overrides: Any) -> Any:
    from datetime import datetime, timezone

    from bettermemory.health import HealthReport

    return HealthReport(
        generated_at=datetime.now(timezone.utc),
        window_days=30,
        total_active_memories=0,
        total_events=0,
        distinct_sessions=0,
        **overrides,
    )


def test_health_page_accounts_for_every_report_bucket() -> None:
    """The /health docstring used to claim "every bucket rendered" while
    three never reached the page. The claim is now a declaration split
    into rendered / deliberately-disclaimed, and this test is what keeps
    it true: a bucket added to `HealthReport.to_dict()` fails here until
    someone renders it or files it with a reason."""
    from bettermemory.web import (
        _HEALTH_DISCLAIMED_BUCKETS,
        _HEALTH_RENDERED_BUCKETS,
    )

    keys = set(_empty_health_report().to_dict())
    assert not (_HEALTH_RENDERED_BUCKETS & _HEALTH_DISCLAIMED_BUCKETS)
    assert _HEALTH_RENDERED_BUCKETS | _HEALTH_DISCLAIMED_BUCKETS == keys, (
        "unclassified HealthReport buckets: "
        f"{sorted(keys - _HEALTH_RENDERED_BUCKETS - _HEALTH_DISCLAIMED_BUCKETS)}"
    )


def test_health_renders_orphan_use_events_when_non_zero() -> None:
    """`orphan_use_events` is the fabrication smoke test — record_use
    against ids that resolve to nothing. It now renders as a warn row
    when it fires, and stays silent (not a permanent "0") when clean."""
    from bettermemory.web import _render_health

    fired = _render_health(_empty_health_report(orphan_use_events=3))
    assert "3 orphan use event(s)" in fired
    assert "fabricated ULIDs" in fired
    assert "orphan use event" not in _render_health(_empty_health_report())


# ---------------------------------------------------------------------------
# Read-only mode (the --tunnel posture)
# ---------------------------------------------------------------------------


@pytest.fixture
def ro_client(memory_dir: Path, store: Store) -> Any:
    """App built with read_only=True — what every tunnel serves."""
    from bettermemory.web import build_app

    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))
    app = build_app(cfg, store, read_only=True)
    return TestClient(app)


def test_read_only_verify_post_403(ro_client: Any, store: Store) -> None:
    """The single mutating endpoint must refuse in read-only mode —
    with a policy 403 that names --tunnel, BEFORE the CSRF/origin
    machinery runs (no token is ever served in this mode, so a token
    error would be misleading). The store must be untouched."""
    m = store.write(content="tunnel visible claim", scopes=["tools"])
    r = ro_client.post(
        f"/memories/{m.id}/verify",
        data={"note": "attempt"},
        headers={
            "Origin": "http://127.0.0.1:8765",
            "X-CSRF-Token": ro_client.app.state.csrf_token,
        },
        follow_redirects=False,
    )
    assert r.status_code == 403
    assert "read-only" in r.text
    assert store.load_one(m.id).last_verified_at is None


def test_read_only_detail_hides_verify_form(ro_client: Any, store: Store) -> None:
    """No verify form on detail pages in read-only mode — a button
    that always 403s is worse than no button."""
    m = store.write(content="tunnel visible claim", scopes=["tools"])
    r = ro_client.get(f"/memories/{m.id}")
    assert r.status_code == 200
    assert "tunnel visible claim" in r.text
    assert "Mark verified now" not in r.text
    assert "<form" not in r.text.replace('<form method="get"', "")


def test_read_only_pages_omit_csrf_plumbing(ro_client: Any) -> None:
    """Read-only pages must not emit the csrf meta tag or the helper
    script — a tunneled page should not hand out a token that names a
    mutation surface."""
    r = ro_client.get("/")
    assert r.status_code == 200
    assert "csrf-token" not in r.text
    assert "X-CSRF-Token" not in r.text


def test_read_only_header_shows_badge(ro_client: Any) -> None:
    """Viewers need to know WHY the verify buttons are gone."""
    r = ro_client.get("/")
    assert r.status_code == 200
    assert "read-only" in r.text


def test_normal_mode_keeps_verify_form_and_csrf(client: Any, store: Store) -> None:
    """Guard the flip's default: a plain build_app() still renders the
    verify form and the CSRF plumbing (regression pin so read_only
    can't accidentally become the default)."""
    m = store.write(content="local claim", scopes=["tools"])
    detail = client.get(f"/memories/{m.id}")
    assert "Mark verified now" in detail.text
    assert "csrf-token" in detail.text
    assert client.app.state.read_only is False


# ---------------------------------------------------------------------------
# Tunnel provider resolution + orchestration
# ---------------------------------------------------------------------------


def test_resolve_tunnel_auto_prefers_tailnet(monkeypatch: Any) -> None:
    """auto picks the tailnet-only provider when tailscale exists,
    even when cloudflared is ALSO present — private-by-default is the
    posture for a personal memory store."""
    from bettermemory import web

    monkeypatch.setattr(web, "_find_tailscale", lambda: "/opt/bin/tailscale")
    monkeypatch.setattr(web, "_find_cloudflared", lambda: "/opt/bin/cloudflared")
    assert web.resolve_tunnel_provider("auto") == ("tailnet", "/opt/bin/tailscale")


def test_resolve_tunnel_auto_falls_back_to_cloudflare(monkeypatch: Any) -> None:
    from bettermemory import web

    monkeypatch.setattr(web, "_find_tailscale", lambda: None)
    monkeypatch.setattr(web, "_find_cloudflared", lambda: "/opt/bin/cloudflared")
    assert web.resolve_tunnel_provider("auto") == (
        "cloudflare",
        "/opt/bin/cloudflared",
    )


def test_resolve_tunnel_auto_errors_when_no_binary(monkeypatch: Any) -> None:
    """The auto error must name both install options — it's the first
    thing a user without either CLI sees."""
    from bettermemory import web

    monkeypatch.setattr(web, "_find_tailscale", lambda: None)
    monkeypatch.setattr(web, "_find_cloudflared", lambda: None)
    with pytest.raises(web.TunnelError, match="Tailscale.*cloudflared"):
        web.resolve_tunnel_provider("auto")


@pytest.mark.parametrize("requested", ["tailnet", "funnel"])
def test_resolve_tunnel_explicit_tailscale_missing(
    monkeypatch: Any, requested: str
) -> None:
    from bettermemory import web

    monkeypatch.setattr(web, "_find_tailscale", lambda: None)
    with pytest.raises(web.TunnelError, match="tailscale"):
        web.resolve_tunnel_provider(requested)


def test_resolve_tunnel_explicit_cloudflare_missing(monkeypatch: Any) -> None:
    from bettermemory import web

    monkeypatch.setattr(web, "_find_cloudflared", lambda: None)
    with pytest.raises(web.TunnelError, match="cloudflared"):
        web.resolve_tunnel_provider("cloudflare")


def test_resolve_tunnel_unknown_provider() -> None:
    from bettermemory import web

    with pytest.raises(web.TunnelError, match="unknown tunnel provider"):
        web.resolve_tunnel_provider("ngrok")


def test_find_tailscale_darwin_app_bundle_fallback(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """The macOS Tailscale app ships its CLI inside the app bundle
    without touching PATH; `_find_tailscale` must probe that location
    so bare --tunnel works on the most common desktop install."""
    import shutil
    import sys

    from bettermemory import web

    # Patch the stdlib singletons directly (web.py imports the same
    # module objects), not `web.shutil` / `web.sys` — reaching a
    # re-imported stdlib module through another module trips mypy's
    # no_implicit_reexport, and the effect is identical.
    fake_cli = tmp_path / "Tailscale"
    fake_cli.write_bytes(b"")
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    monkeypatch.setattr(web, "_MACOS_TAILSCALE_APP_CLI", str(fake_cli))
    monkeypatch.setattr(sys, "platform", "darwin")
    assert web._find_tailscale() == str(fake_cli)


def test_find_tailscale_prefers_path_over_bundle(monkeypatch: Any) -> None:
    import shutil

    from bettermemory import web

    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/local/bin/tailscale")
    assert web._find_tailscale() == "/usr/local/bin/tailscale"


def test_tunnel_argv_shapes() -> None:
    """The exact foreground invocations for each provider — all three
    print their own URL and tear down on exit, which is what lets the
    orchestration skip output parsing entirely."""
    from bettermemory import web

    assert web._tunnel_argv("tailnet", "/bin/ts", 8765) == ["/bin/ts", "serve", "8765"]
    assert web._tunnel_argv("funnel", "/bin/ts", 8765) == ["/bin/ts", "funnel", "8765"]
    assert web._tunnel_argv("cloudflare", "/bin/cf", 9000) == [
        "/bin/cf",
        "tunnel",
        "--url",
        "http://127.0.0.1:9000",
    ]
    with pytest.raises(web.TunnelError):
        web._tunnel_argv("ngrok", "/bin/ngrok", 8765)


def _stub_tunnel_argv(provider: str, binary: str, port: int) -> list[str]:
    """A REAL provider stand-in that models the worst case: it sleeps
    and ignores stdin, exactly like the real `tailscale serve`
    (verified live — it does NOT exit on stdin EOF). Teardown must
    therefore come from the supervisor shim, never from provider
    cooperation. Everything else in the spawn/teardown path (Popen,
    pipes, signals) runs for real; only the binary differs."""
    return [sys.executable, "-c", "import time; time.sleep(3600)"]


def test_start_tunnel_warns_only_for_public_providers(
    monkeypatch: Any, caplog: Any
) -> None:
    """funnel/cloudflare create PUBLIC unauthenticated URLs to a
    personal memory store — the warning is load-bearing. The
    tailnet-only provider must NOT cry wolf. Spawns real child
    processes (the stdin-reader stub) — no fake Popen."""
    import logging as _logging

    from bettermemory import web

    monkeypatch.setattr(web, "_tunnel_argv", _stub_tunnel_argv)

    with caplog.at_level(_logging.WARNING, logger="bettermemory.web"):
        proc = web._start_tunnel("tailnet", "/bin/ts", 8765)
    web._reap_tunnel(proc)
    assert not any("PUBLIC" in r.message for r in caplog.records)

    for provider, binary in (("funnel", "/bin/ts"), ("cloudflare", "/bin/cf")):
        caplog.clear()
        with caplog.at_level(_logging.WARNING, logger="bettermemory.web"):
            proc = web._start_tunnel(provider, binary, 8765)
        web._reap_tunnel(proc)
        assert any("PUBLIC" in r.message for r in caplog.records)


def test_reap_tunnel_stops_real_child_and_is_idempotent() -> None:
    """_reap_tunnel must stop a live child (stdin EOF first, then
    terminate as backstop) and be safe to call again on the dead
    child and on None."""
    from bettermemory import web

    proc = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.read()"],
        stdin=subprocess.PIPE,
        env=shielded_child_env(),
    )
    assert proc.poll() is None
    web._reap_tunnel(proc)
    assert proc.poll() is not None
    web._reap_tunnel(proc)
    web._reap_tunnel(None)


def test_serve_tunnel_rejects_non_loopback_host(monkeypatch: Any) -> None:
    """--tunnel + a non-loopback bind is a contradiction: the tunnel
    is the front door. Must fail fast, before any process spawns or
    port binds."""
    from bettermemory import web

    pytest.importorskip("uvicorn")
    spawned: list[str] = []
    monkeypatch.setattr(web, "_start_tunnel", lambda *a, **k: spawned.append("spawn"))
    cfg = Config(storage=StorageConfig(directory="/tmp/nonexistent-ro"))
    with pytest.raises(web.TunnelError, match="loopback"):
        web.serve(cfg, host="0.0.0.0", port=8765, tunnel="auto")
    assert spawned == []


def test_serve_tunnel_wires_readonly_app_and_reaps_child(
    monkeypatch: Any, memory_dir: Path
) -> None:
    """End-to-end wiring of serve(tunnel=...): resolves the provider,
    spawns a REAL child through the real _start_tunnel (stdin-reader
    stub), builds the READ-ONLY app, and reaps the child when uvicorn
    returns. Only two seams are patched — the argv shape (no real
    tailscale in CI) and the uvicorn server's run (so the call returns);
    process management is real.

    The tunnel branch now constructs the uvicorn server explicitly and
    calls ``Server.run()`` (it overrides ``handle_exit`` to mark the
    shutdown flag at signal delivery), so the seam is ``Server.run`` —
    NOT ``uvicorn.run``, which the tunnel path no longer uses. The served
    app is read back off the server's own config, which real Config
    construction populates from the ``uvicorn.Config(app, ...)`` call —
    so this also pins that construction against a bad kwarg."""
    import uvicorn

    from bettermemory import web

    monkeypatch.setattr(web, "_find_tailscale", lambda: "/bin/ts")
    monkeypatch.setattr(web, "_tunnel_argv", _stub_tunnel_argv)

    spawned: list[Any] = []
    real_start = web._start_tunnel

    def _tracking_start(provider: str, binary: str, port: int) -> Any:
        proc = real_start(provider, binary, port)
        spawned.append(proc)
        return proc

    monkeypatch.setattr(web, "_start_tunnel", _tracking_start)

    served_apps: list[Any] = []

    def _fake_run(self: Any, *args: Any, **kwargs: Any) -> None:
        served_apps.append(self.config.app)
        # A real run() sets this once the socket is bound. serve() reads it to
        # decide whether to exit non-zero on a startup failure, so a fake that
        # left it False would look like a bind failure.
        self.started = True

    monkeypatch.setattr(uvicorn.Server, "run", _fake_run)

    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))
    web.serve(cfg, host="127.0.0.1", port=8123, tunnel="auto")

    assert served_apps and served_apps[0].state.read_only is True
    assert len(spawned) == 1
    child = spawned[0]
    assert child.stdin is not None  # the parent-death watchdog pipe
    assert child.poll() is not None  # reaped when serve() returned


def test_serve_tunnel_exits_nonzero_when_server_never_starts(
    monkeypatch: Any, memory_dir: Path
) -> None:
    """A bind failure in tunnel mode must exit non-zero, and must still reap
    the tunnel child.

    `uvicorn.run()` ends with `sys.exit(STARTUP_FAILURE)` when the server never
    started. The tunnel branch builds the Server by hand to override
    `handle_exit`, which drops that tail — so without an explicit check a
    `--tunnel` bind failure (port already in use) would return normally and the
    CLI would exit 0. A systemd unit with `Restart=on-failure` would not
    restart, and a shell checking `$?` would read success while nothing served.

    Mutation-sound: delete serve()'s `if not server.started: raise SystemExit`
    and this test fails — serve() returns None instead of raising."""
    import uvicorn

    from bettermemory import web

    monkeypatch.setattr(web, "_find_tailscale", lambda: "/bin/ts")
    monkeypatch.setattr(web, "_tunnel_argv", _stub_tunnel_argv)

    spawned: list[Any] = []
    real_start = web._start_tunnel

    def _tracking_start(provider: str, binary: str, port: int) -> Any:
        proc = real_start(provider, binary, port)
        spawned.append(proc)
        return proc

    monkeypatch.setattr(web, "_start_tunnel", _tracking_start)

    # A real run() that fails to bind returns with `started` still False.
    def _never_starts(self: Any, *args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(uvicorn.Server, "run", _never_starts)

    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))
    with pytest.raises(SystemExit) as excinfo:
        web.serve(cfg, host="127.0.0.1", port=8124, tunnel="auto")

    assert excinfo.value.code == 3, (
        "a tunnel-mode startup failure must exit with uvicorn's STARTUP_FAILURE "
        f"code (3), got {excinfo.value.code!r}"
    )
    # The child is reaped on the way out regardless — the `finally` runs before
    # the SystemExit propagates, so a failed bind never leaks a tunnel.
    assert len(spawned) == 1
    assert spawned[0].poll() is not None


# ---------------------------------------------------------------------------
# Provider-death detection — the reverse of parent-death teardown
# ---------------------------------------------------------------------------
#
# The stdin watchdog only fires on PARENT death. If the provider exits
# first (tailscaled down or logged out, Funnel not in the tailnet ACLs,
# cloudflared can't reach the edge, a mid-session logout), the shim used
# to block on its stdin read forever while the parent's poll() stayed
# None, so serve() kept serving read-only on loopback and the dead share
# went unnoticed. The shim now mirrors the provider's exit and serve()'s
# watcher logs it loudly. Real child processes, no fake Popen.


def test_tunnel_shim_exits_when_provider_dies_first(monkeypatch: Any) -> None:
    """The supervisor shim must exit — mirroring the provider's
    returncode — when its provider child dies first, so the parent's
    wait()/poll() goes non-None and serve() can flag the dead share.

    The parent deliberately holds the shim's stdin pipe OPEN, so the
    stdin-EOF watchdog can't fire: the ONLY exit path exercised here is
    the new provider-death watchdog. Pre-fix the shim stayed blocked on
    read() forever and this wait() timed out (rc stays None)."""
    from bettermemory import web

    monkeypatch.setattr(
        web,
        "_tunnel_argv",
        lambda provider, binary, port: [
            sys.executable,
            "-c",
            "import sys; sys.exit(7)",
        ],
    )
    proc = web._start_tunnel("tailnet", "/bin/ts", 8765)
    rc: int | None = None
    try:
        rc = proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        rc = None  # shim outlived its dead provider — the regression
    finally:
        # Close stdin only NOW: doing it earlier would hand the pre-fix
        # shim a second (EOF) exit path and mask the bug. Reap either way.
        if proc.stdin is not None:
            proc.stdin.close()
        web._reap_tunnel(proc)
    assert rc == 7, (
        "shim must exit mirroring the provider's returncode when the "
        f"provider dies first; got {rc!r} (None == the shim outlived its "
        "dead provider — the 3-tunnel-provider-death regression)"
    )


def test_watch_tunnel_provider_flags_dead_share(caplog: Any) -> None:
    """serve()'s provider-death watcher logs a LOUD error naming the
    provider when the shim exits unexpectedly (shutting_down clear) —
    the loopback UI keeps serving, so this log is the only signal the
    shared URL went dead."""
    import logging as _logging
    import threading as _threading

    from bettermemory import web

    # Any already-exited process stands in for the dead shim; the watcher
    # only blocks on proc.wait() and inspects the shutdown flag.
    proc = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.exit(1)"], env=shielded_child_env()
    )
    shutting_down = _threading.Event()  # NOT set -> unexpected death
    with caplog.at_level(_logging.ERROR, logger="bettermemory.web"):
        web._watch_tunnel_provider(proc, "funnel", shutting_down)
    errors = [r for r in caplog.records if r.levelno == _logging.ERROR]
    assert errors, "expected a loud ERROR when the provider dies unexpectedly"
    msg = errors[0].getMessage()
    assert "DEAD" in msg
    assert "funnel" in msg  # names the provider so the user knows which


def test_watch_tunnel_provider_quiet_on_clean_shutdown(caplog: Any) -> None:
    """When serve() is tearing down (shutting_down set before it reaps
    the shim), the watcher must NOT cry wolf: a clean Ctrl-C reaps the
    shim on purpose, which is not a dead share."""
    import logging as _logging
    import threading as _threading

    from bettermemory import web

    proc = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.exit(0)"], env=shielded_child_env()
    )
    shutting_down = _threading.Event()
    shutting_down.set()  # teardown in progress -> expected exit
    with caplog.at_level(_logging.ERROR, logger="bettermemory.web"):
        web._watch_tunnel_provider(proc, "tailnet", shutting_down)
    assert not [r for r in caplog.records if "DEAD" in r.getMessage()], (
        "a clean-shutdown reap must not log a dead-share error"
    )


# ---------------------------------------------------------------------------
# Tunnel child lifecycle — real processes, real signals
# ---------------------------------------------------------------------------
#
# Regression suite for two live-validation findings on the 3.18.0
# tunnel feature. (1) uvicorn's capture_signals() re-raises captured
# signals with the pre-run handlers restored, so a finally-based
# teardown never runs on signal exits — the tunnel child outlived the
# server and kept the share URL alive (502) after `kill <pid>`.
# (2) `tailscale serve` does NOT exit on stdin EOF, so the provider
# needs the _TUNNEL_SUPERVISOR shim to reap it when the server dies
# uncleanly. The old fake-Popen test certified exactly the path
# reality bypasses; these use real child processes and real signals,
# and they track BOTH the shim and the provider under it.


# `signal.SIGHUP` / `signal.SIGKILL` do not exist in the Windows `signal`
# stubs, so mypy's windows-latest leg rejects a bare attribute reference —
# even inside a test that `skipif`s off Windows at runtime, because mypy type-
# checks the whole file regardless of the marker. (The `hasattr` guard inside
# `_LIFECYCLE_DRIVER` below is invisible to mypy: it lives in a string.) Bind
# the POSIX numbers once, guarded; the fallbacks are the POSIX values and are
# never reached on a platform where these tests actually run.
_SIGHUP: int = getattr(signal, "SIGHUP", 1)
_SIGKILL: int = getattr(signal, "SIGKILL", 9)

# `os.killpg` is POSIX-only and absent from the Windows `os` stubs, so a
# bare `os.killpg(...)` trips mypy's windows-latest leg even inside tests
# that skipif off Windows (mypy type-checks the whole file regardless of
# the marker). Bind it once via getattr — typed `Any`, so the win32 leg
# sees no missing-attribute — and only the POSIX-only group-signal tests
# below call it.
_killpg: Any = getattr(os, "killpg", None)


_LIFECYCLE_DRIVER = """
import logging
import os
import signal
import sys

from bettermemory import web
from bettermemory.config import Config, StorageConfig

# Mirror cli/ui.py: route bettermemory's INFO+ logging to stderr so the
# provider-death "DEAD" error (and everything else) lands where the
# operator would see it. The false-alarm / true-positive tests capture
# this stderr and grep it; the other lifecycle tests DEVNULL stderr, so
# this is invisible to them.
logging.basicConfig(level=logging.INFO, stream=sys.stderr)

# Pin the driver's SIGHUP disposition BEFORE serve() spawns the shim or
# installs its handlers, so the lifecycle tests can model each posture
# deterministically regardless of what the test runner itself inherited:
#   "ignore"  -> SIG_IGN: the nohup posture; the tree must SURVIVE a hangup.
#   "guard"   -> a non-fatal, non-ignored no-op handler: a test scaffold
#                that NEUTRALISES the stdin-EOF watchdog backstop. If serve()
#                fails to install its own SIGHUP teardown handler the no-op
#                absorbs the hangup, the driver survives, its stdin pipe to
#                the shim stays open, and the shim/provider are never reaped
#                — so a reaping assertion can only pass when serve()'s
#                handler ran. A correct serve() REPLACES this no-op (it is
#                not SIG_IGN, so the nohup guard does not skip it).
#   "default" -> SIG_DFL: the real default disposition (terminate).
_sighup = os.environ.get("BM_TEST_SIGHUP")
if _sighup and hasattr(signal, "SIGHUP"):
    if _sighup == "ignore":
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
    elif _sighup == "guard":
        signal.signal(signal.SIGHUP, lambda *_a: None)
    else:
        signal.signal(signal.SIGHUP, signal.SIG_DFL)

web.resolve_tunnel_provider = lambda requested: ("tailnet", sys.executable)
web._tunnel_argv = lambda provider, binary, port: [
    sys.executable,
    "-c",
    "import time; time.sleep(3600)",
]

_orig_start = web._start_tunnel


def _traced(provider, binary, port):
    proc = _orig_start(provider, binary, port)
    print(f"TUNNEL_PID={proc.pid}", flush=True)
    return proc


web._start_tunnel = _traced

# A deliberately slow route so the busy-shutdown test can hold a request
# IN FLIGHT across the shutdown signal and stretch uvicorn's graceful drain
# well past the provider-death grace window. It is inert for every other
# lifecycle test — they only ever GET "/". Wrapping build_app (a module
# global serve() looks up at call time) is how the route reaches the served
# app without a public knob. The handler announces its start on stderr
# (which the driver redirects to the test's log file) so the test can
# deliver the signal only once the request is genuinely being served — a
# deterministic in-flight gate, not a timing guess.
_orig_build_app = web.build_app


def _build_app_with_slow(*a, **k):
    import time as _t

    app = _orig_build_app(*a, **k)

    @app.get("/slow")
    def _slow():
        print("SLOW_REQUEST_STARTED", file=sys.stderr, flush=True)
        _t.sleep(3.0)
        return {"slept": True}

    return app


web.build_app = _build_app_with_slow

cfg = Config(storage=StorageConfig(directory=sys.argv[1]))
web.serve(cfg, host="127.0.0.1", port=int(sys.argv[2]), tunnel="tailnet")
"""


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - exists but not ours
        return True
    return True


def _wait_pid_gone(pid: int, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return
        time.sleep(0.1)
    pytest.fail(f"tunnel child {pid} still alive {timeout}s after server death")


def _provider_pid_under(shim_pid: int) -> int:
    """The provider runs as the supervisor shim's only child; find it
    with `pgrep -P` (macOS and Linux; the suite is skipped on
    Windows)."""
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        out = subprocess.run(
            ["pgrep", "-P", str(shim_pid)],
            capture_output=True,
            text=True,
            check=False,
        )
        pids = [int(p) for p in out.stdout.split()]
        if len(pids) == 1:
            return pids[0]
        time.sleep(0.1)
    pytest.fail(f"shim {shim_pid} never had exactly one provider child")


def _spawn_lifecycle_driver(
    memory_dir: Path,
    *,
    sighup: str | None = None,
    new_session: bool = False,
    stderr_path: Path | None = None,
    port: int | None = None,
) -> tuple["subprocess.Popen[bytes]", int, int]:
    """Start serve(tunnel=...) in a real child process and wait until
    uvicorn answers HTTP. The teardown signal handlers are installed
    before uvicorn.run, so an answering server implies armed
    teardown. Returns (driver, shim pid, provider pid).

    ``port`` lets a caller pin the bind port it needs to know up front
    (the busy-shutdown test fires a request at ``/slow`` on it); the
    default picks a free one internally. Passing a pre-chosen free port
    has the same TOCTOU characteristics as generating it here.

    ``sighup`` pins the driver's SIGHUP disposition before serve() runs:
    ``"ignore"`` models the nohup posture (SIG_IGN, must survive a
    hangup), ``"guard"`` installs a non-fatal no-op handler that
    neutralises the stdin-EOF watchdog so serve()'s own SIGHUP handler is
    the only thing that can reap the tunnel, and ``"default"`` forces
    SIG_DFL. ``None`` leaves it inherited — the SIGTERM/SIGKILL tests
    never touch SIGHUP.

    ``new_session`` runs the driver in its own session/process group
    (``setsid``) so a test can ``os.killpg(driver.pid, ...)`` the whole
    tunnel tree — driver + shim + provider — the way a terminal Ctrl-C or
    ``systemctl stop`` delivers a signal, without touching the test
    runner. ``stderr_path`` redirects the driver's stderr (where the
    bettermemory logger writes) to a file the test can read afterwards;
    the default DEVNULL keeps the other lifecycle tests quiet."""
    import urllib.request

    env = shielded_child_env()
    if sighup is not None:
        env["BM_TEST_SIGHUP"] = sighup

    port = port if port is not None else _free_port()
    stderr_target: Any = subprocess.DEVNULL
    stderr_fh = None
    if stderr_path is not None:
        stderr_fh = open(stderr_path, "wb")
        stderr_target = stderr_fh
    driver = subprocess.Popen(
        [sys.executable, "-c", _LIFECYCLE_DRIVER, str(memory_dir), str(port)],
        stdout=subprocess.PIPE,
        stderr=stderr_target,
        start_new_session=new_session,
        env=env,
    )
    if stderr_fh is not None:
        stderr_fh.close()  # the child holds its own dup; drop the parent's
    assert driver.stdout is not None
    line = driver.stdout.readline().decode()
    if not line.startswith("TUNNEL_PID="):
        driver.kill()
        pytest.fail(f"driver did not announce the tunnel child: {line!r}")
    shim_pid = int(line.strip().split("=", 1)[1])
    provider_pid = _provider_pid_under(shim_pid)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1).close()
            return driver, shim_pid, provider_pid
        except OSError:
            time.sleep(0.15)
    driver.kill()
    pytest.fail("driver server never answered HTTP")


def _cleanup_driver(driver: "subprocess.Popen[bytes]") -> None:
    if driver.poll() is None:  # pragma: no cover - only on test failure
        driver.kill()
        driver.wait(timeout=5)
    if driver.stdout is not None:
        driver.stdout.close()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signal semantics")
def test_tunnel_child_reaped_on_sigterm(memory_dir: Path) -> None:
    """`kill <server pid>` must take the whole tunnel down with it —
    shim AND provider — and the server must still die BY the signal
    (exit -SIGTERM), preserving die-by-signal etiquette for
    supervisors."""
    pytest.importorskip("uvicorn")
    driver, shim_pid, provider_pid = _spawn_lifecycle_driver(memory_dir)
    try:
        assert _pid_alive(shim_pid)
        assert _pid_alive(provider_pid)
        driver.send_signal(signal.SIGTERM)
        assert driver.wait(timeout=10) == -signal.SIGTERM
        _wait_pid_gone(shim_pid, timeout=5.0)
        _wait_pid_gone(provider_pid, timeout=5.0)
    finally:
        _cleanup_driver(driver)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signal semantics")
def test_tunnel_child_reaped_on_sigkill(memory_dir: Path) -> None:
    """SIGKILL leaves the server no room for userspace teardown — the
    shim's stdin watchdog is the only thing standing between the
    provider and a leak. It must reap the provider and exit."""
    pytest.importorskip("uvicorn")
    driver, shim_pid, provider_pid = _spawn_lifecycle_driver(memory_dir)
    try:
        assert _pid_alive(shim_pid)
        assert _pid_alive(provider_pid)
        driver.send_signal(9)  # SIGKILL — absent from signal stubs on Windows
        assert driver.wait(timeout=10) == -9
        _wait_pid_gone(shim_pid, timeout=5.0)
        _wait_pid_gone(provider_pid, timeout=5.0)
    finally:
        _cleanup_driver(driver)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signal semantics")
def test_tunnel_survives_sighup_when_inherited_sig_ign(memory_dir: Path) -> None:
    """nohup semantics: a server started with SIGHUP already ignored —
    `nohup bettermemory ui --tunnel tailnet &`, then closing the
    terminal — must SURVIVE the hangup. Server, shim, AND provider all
    stay up and the share stays alive.

    The classic POSIX rule is to skip installing a handler for a signal
    whose inherited disposition is SIG_IGN. That must hold on BOTH the
    serve() teardown-handler loop and the supervisor shim (which is
    spawned before serve()'s handlers, so under nohup it inherits the
    SIG_IGN too). Pre-fix the unconditional installs clobbered SIG_IGN
    and re-raised SIGHUP under SIG_DFL, so the detached server died on
    the hangup and the shim reaped the provider — the share vanished.

    SIGHUP is delivered to every process in the tunnel tree
    individually (as a terminal hangup reaches the whole process
    group), so a regression in EITHER install site is caught: a live
    serve() handler kills the driver, a live shim handler kills the
    shim + provider."""
    pytest.importorskip("uvicorn")
    driver, shim_pid, provider_pid = _spawn_lifecycle_driver(
        memory_dir, sighup="ignore"
    )
    try:
        assert _pid_alive(shim_pid)
        assert _pid_alive(provider_pid)
        # Hang up the whole tree. Every process inherited SIG_IGN, so a
        # convention-respecting build ignores it and nothing tears down.
        for pid in (driver.pid, shim_pid, provider_pid):
            os.kill(pid, _SIGHUP)
        # Give a would-be handler ample time to reap + re-raise before
        # asserting survival: pre-fix the driver is already dead here.
        time.sleep(2.0)
        assert driver.poll() is None, (
            "server died on SIGHUP despite an inherited SIG_IGN — nohup "
            "detachment is broken (serve() clobbered the ignored signal)"
        )
        assert _pid_alive(shim_pid), (
            "supervisor shim died on SIGHUP despite an inherited SIG_IGN — "
            "the shim clobbered the ignored signal and tore the share down"
        )
        assert _pid_alive(provider_pid), (
            "tunnel provider was reaped after SIGHUP despite the tree "
            "inheriting SIG_IGN — the share died under a detached server"
        )
    finally:
        # SIGHUP was ignored, so reap with SIGKILL (uncatchable). Kill
        # the hour-long sleepers directly too so a slow stdin watchdog
        # can't leak them, then let _cleanup_driver stop the driver.
        for pid in (provider_pid, shim_pid):
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, _SIGKILL)
        _cleanup_driver(driver)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signal semantics")
def test_serve_installs_narrow_sighup_teardown_handler(memory_dir: Path) -> None:
    """The SIG_IGN guard must be NARROW: serve() must still INSTALL a
    SIGHUP teardown handler whenever SIGHUP is not inherited-ignored (the
    complement of `test_tunnel_survives_sighup_when_inherited_sig_ign`). A
    hangup must reap the whole tunnel — shim AND provider — and re-raise
    so the server still dies BY the signal.

    This test depends on serve()'s HANDLER, not the watchdog backstop.
    Teardown is otherwise guaranteed three ways (the shim's stdin-EOF
    watchdog reaps the provider on any driver death, the shim's own signal
    handler, serve()'s handler), and the prior default-disposition test
    was satisfied by ALL of them — it stayed green even with serve()'s
    SIGHUP install removed wholesale (`if sig == signal.SIGHUP: continue`),
    which made it theater.

    To isolate serve()'s handler, the driver pre-installs a NON-fatal,
    non-ignored SIGHUP disposition (a no-op handler, ``sighup="guard"``)
    BEFORE serve() runs, and the test signals the driver PID only:

      * Correct serve() REPLACES the no-op with its teardown handler
        (the no-op is not SIG_IGN, so the nohup guard does not skip it);
        the hangup reaps the shim + provider and re-raises under SIG_DFL,
        so the driver dies BY SIGHUP and the tunnel is gone.
      * Mutated serve() that skips the SIGHUP install leaves the no-op in
        place. The hangup is absorbed, the driver SURVIVES, its stdin pipe
        to the shim stays open so the watchdog never sees EOF, and the
        shim/provider are never reaped — every assertion below fails.

    The no-op is a test scaffold, not the real default disposition
    (terminate): under a true SIG_DFL the driver dies on SIGHUP no matter
    what serve() does, its pipe closes, and the watchdog reaps the shim —
    the very backstop that made the old assertions watchdog-satisfiable."""
    pytest.importorskip("uvicorn")
    driver, shim_pid, provider_pid = _spawn_lifecycle_driver(memory_dir, sighup="guard")
    try:
        assert _pid_alive(shim_pid)
        assert _pid_alive(provider_pid)
        driver.send_signal(_SIGHUP)
        rc: int | None
        try:
            rc = driver.wait(timeout=10)
        except subprocess.TimeoutExpired:
            rc = None
        assert rc == -_SIGHUP, (
            "serve() did not install a working SIGHUP teardown handler: "
            "the driver's no-op guard absorbed the hangup and the server "
            f"survived (rc={rc!r}). With the watchdog backstop neutralised, "
            "this is the mutation `if sig == signal.SIGHUP: continue`."
        )
        _wait_pid_gone(shim_pid, timeout=5.0)
        _wait_pid_gone(provider_pid, timeout=5.0)
    finally:
        # Under the mutation the whole tree survives; SIGKILL the
        # hour-long sleepers directly so a neutralised watchdog can't leak
        # them, then let _cleanup_driver stop the driver.
        for pid in (provider_pid, shim_pid):
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, _SIGKILL)
        _cleanup_driver(driver)


# ---------------------------------------------------------------------------
# Provider-death false alarm — the b5e5542 regression, driven end-to-end
# ---------------------------------------------------------------------------
#
# The provider-death watcher must stay QUIET on a clean quit and LOUD on a
# genuine mid-session death. The unit tests above pin the flag-already-set
# and flag-never-set orderings; these drive the real serve() under a real
# process-group signal, which is the ordering the unit tests can't model:
# the supervisor shim shares serve()'s process group, so a Ctrl-C /
# `systemctl stop` reaches it directly and reaps it BEFORE serve() finishes
# uvicorn's graceful shutdown and sets `shutting_down`. A bare is_set()
# check races that and cries "DEAD" on every clean exit (b5e5542); the
# bounded grace window in _watch_tunnel_provider closes the race.


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process-group signals")
@pytest.mark.parametrize(
    "sig",
    [
        pytest.param(signal.SIGINT, id="sigint-ctrl-c"),
        pytest.param(signal.SIGTERM, id="sigterm-systemctl-stop"),
    ],
)
def test_clean_group_signal_does_not_log_dead_share(
    memory_dir: Path, tmp_path: Path, sig: int
) -> None:
    """A clean quit must NOT fire the provider-death 'DEAD' alarm.

    Runs serve(tunnel=...) in its own session and delivers the signal to
    the WHOLE process group with ``os.killpg`` — exactly how a terminal
    Ctrl-C (SIGINT) or ``systemctl stop`` (SIGTERM) arrives. That reaches
    the supervisor shim directly, so the watcher's ``proc.wait()`` returns
    while serve() is still inside uvicorn's graceful shutdown and
    ``shutting_down`` is not yet set. Pre-fix (a bare ``is_set()`` check)
    the watcher loses that race and logs the loud 'DEAD' error on this,
    the most common exit path; the grace window keeps it quiet.

    The server must still die BY the signal (die-by-signal etiquette), so
    supervisors observing exit status see the real cause."""
    pytest.importorskip("uvicorn")
    stderr_log = tmp_path / f"driver-stderr-{int(sig)}.log"
    driver, shim_pid, provider_pid = _spawn_lifecycle_driver(
        memory_dir, new_session=True, stderr_path=stderr_log
    )
    try:
        assert _pid_alive(shim_pid)
        assert _pid_alive(provider_pid)
        # Deliver to the whole tunnel tree, not just driver.pid — that is
        # what reaches the shim directly and races serve()'s flag.
        _killpg(driver.pid, sig)
        assert driver.wait(timeout=15) == -sig
        _wait_pid_gone(shim_pid, timeout=5.0)
        _wait_pid_gone(provider_pid, timeout=5.0)
    finally:
        with contextlib.suppress(ProcessLookupError):
            _killpg(driver.pid, _SIGKILL)
        _cleanup_driver(driver)
    log_text = stderr_log.read_bytes().decode(errors="replace")
    assert "DEAD" not in log_text, (
        "a clean group-signalled shutdown logged the spurious dead-share "
        f"error (the b5e5542 false-alarm regression):\n{log_text}"
    )


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process-group signals")
def test_busy_clean_shutdown_does_not_log_dead_share(
    memory_dir: Path, tmp_path: Path
) -> None:
    """A clean quit with a SLOW request IN FLIGHT must NOT fire the
    provider-death 'DEAD' alarm — the BUSY companion to
    test_clean_group_signal_does_not_log_dead_share (which quits idle).

    This is the residual false alarm a pure wall-clock grace window could
    not close. serve() runs in its own session; a request to /slow (the
    handler sleeps 3s, far past the 1s grace window) is put in flight and
    confirmed to be executing, THEN SIGINT is delivered to the whole
    process group with os.killpg — a terminal Ctrl-C. The supervisor shim
    (a group member) is reaped within milliseconds, so the watcher's
    proc.wait() returns while uvicorn is still draining the slow request.

    The fix sets ``shutting_down`` from uvicorn's handle_exit at signal
    DELIVERY, so the watcher finds it already up and stays quiet. The old
    design set it only from the restored teardown handler, which runs
    AFTER the unbounded drain — so the watcher's 1s window lapsed mid-drain
    and it logged the spurious 'DEAD' error on this, the most common exit.

    Also pins that the exit is genuinely clean: the server still dies BY
    the signal (die-by-signal etiquette) and the in-flight request drains
    to a real HTTP 200 — so the suppressed alarm was a false alarm, not a
    real death the watcher legitimately describes."""
    pytest.importorskip("uvicorn")
    import urllib.request

    port = _free_port()
    stderr_log = tmp_path / "driver-stderr-busy.log"
    driver, shim_pid, provider_pid = _spawn_lifecycle_driver(
        memory_dir, new_session=True, stderr_path=stderr_log, port=port
    )

    result: dict[str, Any] = {}

    def _hit_slow() -> None:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/slow", timeout=20
            ) as resp:
                result["status"] = resp.status
                resp.read()
        except Exception as exc:  # recorded for the assertion message
            result["error"] = repr(exc)

    req_thread = threading.Thread(target=_hit_slow, daemon=True)
    try:
        assert _pid_alive(shim_pid)
        assert _pid_alive(provider_pid)
        req_thread.start()
        # Deterministic in-flight gate: wait until the handler has actually
        # started serving (it prints a marker to the driver's stderr) before
        # signalling, so the request is provably in flight and holds the
        # drain open — no fixed-sleep race.
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            started = "SLOW_REQUEST_STARTED" in stderr_log.read_bytes().decode(
                errors="replace"
            )
            if started:
                break
            time.sleep(0.05)
        else:
            pytest.fail("slow request never reached the handler")
        # Deliver to the whole tunnel tree, exactly as a terminal Ctrl-C
        # does; this reaches the shim directly and races serve()'s flag.
        _killpg(driver.pid, signal.SIGINT)
        # The drain waits for the ~2s+ remaining slow request, so allow
        # plenty of time; the server must still die BY the signal.
        assert driver.wait(timeout=20) == -signal.SIGINT
        _wait_pid_gone(shim_pid, timeout=5.0)
        _wait_pid_gone(provider_pid, timeout=5.0)
    finally:
        with contextlib.suppress(ProcessLookupError):
            _killpg(driver.pid, _SIGKILL)
        _cleanup_driver(driver)
    req_thread.join(timeout=5)

    # (c) the in-flight request actually drained to a real 200 — the
    # graceful shutdown served it, so this exit was clean, not a crash.
    assert result.get("status") == 200, (
        f"slow in-flight request did not drain to HTTP 200: {result!r}"
    )
    # (a) and the clean, BUSY exit must not have logged the dead-share
    # alarm — the whole point of setting the flag at signal delivery.
    log_text = stderr_log.read_bytes().decode(errors="replace")
    assert "DEAD" not in log_text, (
        "a clean shutdown with a slow in-flight request logged the spurious "
        "dead-share error — the residual false alarm on a BUSY quit that a "
        f"pure grace window cannot close:\n{log_text}"
    )


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signal semantics")
def test_provider_death_mid_session_logs_dead_share(
    memory_dir: Path, tmp_path: Path
) -> None:
    """True-positive companion to the false-alarm test: a genuine
    mid-session provider death — while the server keeps serving — MUST
    still log the 'DEAD' alarm. This is the whole point of the watcher,
    and it guards the grace-window fix from over-suppressing real deaths.

    Kill ONLY the provider. The driver (server) stays up, so the shim's
    stdin watchdog never sees EOF; the shim's provider-death mirror is the
    sole reason it exits, and serve()'s watcher must then flag the dead
    share after the grace window lapses (the flag is never set here)."""
    pytest.importorskip("uvicorn")
    stderr_log = tmp_path / "driver-stderr-provider-death.log"
    driver, shim_pid, provider_pid = _spawn_lifecycle_driver(
        memory_dir, stderr_path=stderr_log
    )
    try:
        assert _pid_alive(provider_pid)
        os.kill(provider_pid, _SIGKILL)
        # The watcher waits its grace window before logging; poll the
        # driver's stderr with margin over that window.
        deadline = time.monotonic() + 10
        log_text = ""
        while time.monotonic() < deadline:
            log_text = stderr_log.read_bytes().decode(errors="replace")
            if "DEAD" in log_text:
                break
            time.sleep(0.1)
        assert "DEAD" in log_text, (
            "provider died mid-session but the watcher never logged the "
            f"dead-share alarm within the window:\n{log_text}"
        )
        assert "tailnet" in log_text  # names the provider so the user knows
        # The loopback server itself must still be serving — that is why
        # this log is the only signal the share went away.
        assert driver.poll() is None, "server must keep serving after provider death"
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.kill(shim_pid, _SIGKILL)
        _cleanup_driver(driver)


def test_serve_without_tunnel_stays_mutable(monkeypatch: Any, memory_dir: Path) -> None:
    """Plain serve() must keep building the read-write app — the
    tunnel posture must never leak into the default path."""
    import uvicorn

    from bettermemory import web

    served_apps: list[Any] = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: served_apps.append(app))
    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))
    web.serve(cfg, host="127.0.0.1", port=8124)
    assert served_apps and served_apps[0].state.read_only is False


def test_loopback_trusted_hosts_reject_dns_rebinding_host(memory_dir: Path) -> None:
    """The DNS-rebinding guard: a loopback bind is only local-only
    against callers that dial the IP — a browser resolving an
    attacker's domain to 127.0.0.1 sends that domain in Host and gets
    same-origin reads over every route. With `trusted_hosts` armed
    (what `serve` passes for a loopback bind with no tunnel), a
    foreign Host answers 400 before any route runs; the loopback
    spellings — port-suffixed and bracketed-IPv6 included — pass.
    Default construction (tests, tunnel posture, non-loopback binds)
    keeps no guard, so every existing caller is unaffected."""
    from bettermemory.web import build_app

    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))
    store = Store(memory_dir)
    app = build_app(
        cfg, store, trusted_hosts=frozenset({"localhost", "127.0.0.1", "::1"})
    )
    client = TestClient(app)
    assert client.get("/", headers={"host": "localhost:8765"}).status_code == 200
    assert client.get("/", headers={"host": "127.0.0.1"}).status_code == 200
    assert client.get("/", headers={"host": "[::1]:8765"}).status_code == 200
    assert client.get("/", headers={"host": "evil.example:8765"}).status_code == 400

    open_client = TestClient(build_app(cfg, store))
    assert open_client.get("/", headers={"host": "evil.example"}).status_code == 200
