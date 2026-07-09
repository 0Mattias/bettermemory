"""Tests for the local web UI (T4.3 of the v1.6 plan).

Uses FastAPI's TestClient — same in-process HTTP testing pattern the
fastapi docs recommend. Skips when the [ui] extra isn't installed
(fastapi / httpx missing) so the suite stays portable.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from bettermemory.config import Config, StorageConfig
from bettermemory.store import Store


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
    # And it carries the warn class, mirroring the tag warn vocabulary.
    assert 'class="tag warn">stale' in r.text

    # A freshly verified memory must NOT trip the cue.
    fresh = store.write(content="durable claim verified just now", scopes=["tools"])
    store.mark_verified(fresh.id)
    r2 = client.get(f"/memories/{fresh.id}")
    assert r2.status_code == 200
    assert "stale (verified" not in r2.text


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
    from bettermemory import web

    fake_cli = tmp_path / "Tailscale"
    fake_cli.write_bytes(b"")
    monkeypatch.setattr(web.shutil, "which", lambda _name: None)
    monkeypatch.setattr(web, "_MACOS_TAILSCALE_APP_CLI", str(fake_cli))
    monkeypatch.setattr(web.sys, "platform", "darwin")
    assert web._find_tailscale() == str(fake_cli)


def test_find_tailscale_prefers_path_over_bundle(monkeypatch: Any) -> None:
    from bettermemory import web

    monkeypatch.setattr(web.shutil, "which", lambda _name: "/usr/local/bin/tailscale")
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


def test_start_tunnel_warns_only_for_public_providers(
    monkeypatch: Any, caplog: Any
) -> None:
    """funnel/cloudflare create PUBLIC unauthenticated URLs to a
    personal memory store — the warning is load-bearing. The
    tailnet-only provider must NOT cry wolf."""
    import logging as _logging

    from bettermemory import web

    spawned: list[list[str]] = []

    class _FakeProc:
        def __init__(self, argv: list[str]) -> None:
            spawned.append(argv)

    monkeypatch.setattr(web.subprocess, "Popen", _FakeProc)

    with caplog.at_level(_logging.WARNING, logger="bettermemory.web"):
        web._start_tunnel("tailnet", "/bin/ts", 8765)
    assert not any("PUBLIC" in r.message for r in caplog.records)

    for provider, binary in (("funnel", "/bin/ts"), ("cloudflare", "/bin/cf")):
        caplog.clear()
        with caplog.at_level(_logging.WARNING, logger="bettermemory.web"):
            web._start_tunnel(provider, binary, 8765)
        assert any("PUBLIC" in r.message for r in caplog.records)
    assert len(spawned) == 3


def test_serve_tunnel_rejects_non_loopback_host(monkeypatch: Any) -> None:
    """--tunnel + a non-loopback bind is a contradiction: the tunnel
    is the front door. Must fail fast, before any process spawns or
    port binds."""
    from bettermemory import web

    pytest.importorskip("uvicorn")
    spawned: list[list[str]] = []
    monkeypatch.setattr(web.subprocess, "Popen", lambda argv: spawned.append(argv))
    cfg = Config(storage=StorageConfig(directory="/tmp/nonexistent-ro"))
    with pytest.raises(web.TunnelError, match="loopback"):
        web.serve(cfg, host="0.0.0.0", port=8765, tunnel="auto")
    assert spawned == []


def test_serve_tunnel_spawns_provider_and_terminates(
    monkeypatch: Any, memory_dir: Path
) -> None:
    """End-to-end wiring of serve(tunnel=...): resolves the provider,
    spawns the right argv, builds the READ-ONLY app, and terminates
    the tunnel child when uvicorn returns."""
    import uvicorn

    from bettermemory import web

    events: list[str] = []
    spawned: list[list[str]] = []

    class _FakeProc:
        def __init__(self, argv: list[str]) -> None:
            spawned.append(argv)

        def terminate(self) -> None:
            events.append("terminate")

        def wait(self, timeout: float | None = None) -> int:
            events.append("wait")
            return 0

        def kill(self) -> None:  # pragma: no cover - only on hang
            events.append("kill")

    served_apps: list[Any] = []

    def _fake_run(app: Any, **kwargs: Any) -> None:
        served_apps.append(app)
        events.append("uvicorn")

    monkeypatch.setattr(web, "_find_tailscale", lambda: "/bin/ts")
    monkeypatch.setattr(web.subprocess, "Popen", _FakeProc)
    monkeypatch.setattr(uvicorn, "run", _fake_run)

    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))
    web.serve(cfg, host="127.0.0.1", port=8123, tunnel="auto")

    assert spawned == [["/bin/ts", "serve", "8123"]]
    assert served_apps and served_apps[0].state.read_only is True
    assert events == ["uvicorn", "terminate", "wait"]


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
