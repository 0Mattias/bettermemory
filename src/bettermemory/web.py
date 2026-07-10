"""Local web UI for bettermemory (T4.3 of the v1.6 plan).

A small FastAPI app that surfaces the curation surfaces (memory_health
rollups, dead-weight, contradictions, never-verified) plus a memory
browser, detail view, and one-click verify. The CLI tool surface is
the canonical entrypoint for everyday writes / searches; the web UI's
killer use case is the *curation* pass — looking at a list of
dead-weight memories side-by-side beats reading them out via tool
calls.

Scope:

- Local-only by default (binds to 127.0.0.1).
- No editing UI: writes happen in-conversation via `memory_write`,
  not from the browser. The UI is read-mostly with one mutation —
  `memory_verify`, since "I just spot-checked this claim" is a
  natural human action.
- No JS framework: server-side rendered HTML, minimal inline CSS,
  no template engine. Each route returns a complete HTML response
  built from the helper functions below. Cheap to maintain, no
  install-time template discovery story.

Gated behind the optional ``[ui]`` extra. The CLI's `bettermemory ui`
subcommand surfaces a clean install hint when fastapi / uvicorn
isn't available.
"""

from __future__ import annotations

import contextlib
import html
import logging
import secrets
import shutil
import signal
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .config import Config
from .health import report_for_directory
from .models import validate_scope
from .origin import capture as capture_origin
from .store import MemoryNotFoundError, Store, TombstonedError
from .verify import compute_verification_status

if TYPE_CHECKING:
    from types import FrameType

    from fastapi import FastAPI


log = logging.getLogger("bettermemory.web")


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------


_BASE_STYLE = """
:root {
    --fg: #1a1a1a;
    --muted: #666;
    --bg: #fafafa;
    --card: #fff;
    --border: #e0e0e0;
    --accent: #2563eb;
    --warn: #d97706;
    --bad: #dc2626;
    --ok: #059669;
}
* { box-sizing: border-box; }
body {
    font-family: -apple-system, system-ui, BlinkMacSystemFont, sans-serif;
    background: var(--bg);
    color: var(--fg);
    max-width: 1000px;
    margin: 0 auto;
    padding: 1rem;
    line-height: 1.5;
}
header { border-bottom: 1px solid var(--border); padding-bottom: 0.5rem; margin-bottom: 1rem; }
header a { color: var(--accent); text-decoration: none; margin-right: 1rem; font-weight: 500; }
header a:hover { text-decoration: underline; }
header strong { font-weight: 600; color: var(--fg); }
h1 { font-size: 1.5rem; margin-top: 0; }
h2 { font-size: 1.2rem; margin-top: 1.5rem; }
.card {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 1rem;
    margin-bottom: 0.75rem;
}
.muted { color: var(--muted); font-size: 0.9rem; }
.tag {
    display: inline-block;
    background: #eef2ff;
    color: var(--accent);
    padding: 0.1rem 0.5rem;
    border-radius: 4px;
    font-size: 0.85rem;
    margin-right: 0.25rem;
}
.tag.warn { background: #fef3c7; color: var(--warn); }
.tag.bad  { background: #fee2e2; color: var(--bad);  }
.tag.ok   { background: #d1fae5; color: var(--ok);   }
form { display: inline; }
input[type="text"], textarea {
    width: 100%;
    padding: 0.5rem;
    border: 1px solid var(--border);
    border-radius: 4px;
    font-family: inherit;
    font-size: 1rem;
}
button {
    background: var(--accent);
    color: #fff;
    border: none;
    padding: 0.4rem 0.8rem;
    border-radius: 4px;
    cursor: pointer;
    font-size: 0.9rem;
}
button:hover { background: #1d4ed8; }
button.secondary {
    background: #fff;
    color: var(--fg);
    border: 1px solid var(--border);
}
pre {
    background: #f5f5f5;
    padding: 0.75rem;
    border-radius: 4px;
    overflow-x: auto;
    font-size: 0.85rem;
}
ul.bare { list-style: none; padding: 0; }
ul.bare li { padding: 0.25rem 0; }
.bucket-summary { display: flex; gap: 1rem; flex-wrap: wrap; margin-bottom: 1rem; }
.bucket-summary .item {
    background: var(--card);
    border: 1px solid var(--border);
    padding: 0.5rem 0.75rem;
    border-radius: 4px;
    min-width: 100px;
}
.bucket-summary .label { font-size: 0.8rem; color: var(--muted); display: block; }
.bucket-summary .value { font-size: 1.4rem; font-weight: 600; }
.bucket-summary .value.bad { color: var(--bad); }
.bucket-summary .value.warn { color: var(--warn); }
"""


# Inline JS that copies the per-process CSRF token from the meta tag
# onto every mutating <form>'s submission (and any fetch() the page
# might add later). Plain forms can't send custom request headers, so
# we intercept submit, append the token as a hidden field, and the
# server checks the header form (X-CSRF-Token) OR the form-field form
# (csrf_token) — same value, two transports. Keeping the helper tiny
# and self-contained beats pulling in any framework.
_CSRF_JS = """
(function () {
  var meta = document.querySelector('meta[name="csrf-token"]');
  if (!meta) return;
  var token = meta.getAttribute('content');
  // Augment every form on the page so plain <form method=post> works
  // without touching each call site.
  document.querySelectorAll('form').forEach(function (form) {
    if ((form.method || '').toLowerCase() !== 'post') return;
    var existing = form.querySelector('input[name="csrf_token"]');
    if (existing) { existing.value = token; return; }
    var input = document.createElement('input');
    input.type = 'hidden';
    input.name = 'csrf_token';
    input.value = token;
    form.appendChild(input);
  });
  // Patch fetch so any future inline JS that wants to POST inherits
  // the header automatically.
  var origFetch = window.fetch;
  window.fetch = function (input, init) {
    init = init || {};
    var method = (init.method || (typeof input === 'object' && input.method) || 'GET').toUpperCase();
    if (method !== 'GET' && method !== 'HEAD' && method !== 'OPTIONS') {
      init.headers = new Headers(init.headers || {});
      if (!init.headers.has('X-CSRF-Token')) {
        init.headers.set('X-CSRF-Token', token);
      }
    }
    return origFetch.call(this, input, init);
  };
})();
"""


def _layout(
    title: str,
    body: str,
    store_root: Path,
    csrf_token: str,
    *,
    read_only: bool = False,
) -> str:
    """Render a full HTML page with the standard chrome.

    Title is HTML-escaped for safety; body is trusted (the route
    builds it from escaped pieces internally). Header carries the
    nav and a small "served from" indicator so a user juggling
    multiple stores can tell at a glance which one they're in.

    Every page carries a <meta name="csrf-token"> tag plus the
    `_CSRF_JS` helper that injects the token onto every same-origin
    mutating form / fetch(). The server side checks the token on
    every mutating endpoint — see `_check_csrf` in `build_app`. The
    token is per-process (regenerated on every server restart); we
    don't bother with rotating-per-request tokens because the local
    UI's session lifetime is "user has the tab open" and rotating
    would break submits across tabs without buying real defence.

    In `read_only` mode (the --tunnel posture) the CSRF meta tag and
    helper script are omitted entirely — there are no mutations to
    protect, and a page served through a tunnel should not hand out
    a token that names a mutation surface at all. A header badge
    tells the viewer why the verify buttons are gone.
    """
    csrf_meta = (
        ""
        if read_only
        else f'<meta name="csrf-token" content="{html.escape(csrf_token)}"/>'
    )
    csrf_script = "" if read_only else f"<script>{_CSRF_JS}</script>"
    ro_badge = '<span class="tag warn">read-only</span> ' if read_only else ""
    return (
        "<!doctype html>"
        "<html><head>"
        f"<title>{html.escape(title)} · bettermemory</title>"
        f"{csrf_meta}"
        f"<style>{_BASE_STYLE}</style>"
        "</head><body>"
        "<header>"
        '<a href="/">Overview</a>'
        '<a href="/memories">Memories</a>'
        '<a href="/health">Health</a>'
        '<a href="/tombstones">Tombstones</a>'
        f'<span class="muted" style="float:right">{ro_badge}'
        f"<strong>{html.escape(str(store_root))}</strong></span>"
        "</header>"
        f"<h1>{html.escape(title)}</h1>"
        f"{body}"
        f"{csrf_script}"
        "</body></html>"
    )


def _render_overview(report: Any) -> str:
    """Dashboard summary built from a HealthReport."""
    n = report.total_active_memories
    debt = report.verification_debt
    # Read the uncapped totals, not len() of the capped row lists.
    # compute_health slices never_verified / stale at _VERIFICATION_DEBT_CAP
    # (20) to bound the JSON; the dashboard headline must reflect the real
    # backlog, so on a store with >20 of either the count would otherwise
    # freeze at 20 and the warn cue would saturate.
    never_verified = debt.never_verified_total if debt else 0
    stale_verifications = debt.stale_total if debt else 0
    parts: list[str] = []
    parts.append('<div class="bucket-summary">')
    for label, value, cls in (
        ("active memories", n, ""),
        ("never verified", never_verified, "warn" if never_verified else ""),
        (
            "stale verifications",
            stale_verifications,
            "warn" if stale_verifications else "",
        ),
        ("dead weight", len(report.dead_weight), "bad" if report.dead_weight else ""),
        (
            "cold memories",
            len(report.cold_memories),
            "warn" if report.cold_memories else "",
        ),
        (
            "unresolved contradictions",
            len(report.contradicted),
            "bad" if report.contradicted else "",
        ),
    ):
        parts.append(
            f'<div class="item"><span class="label">{html.escape(label)}</span>'
            f'<span class="value {cls}">{int(value)}</span></div>'
        )
    parts.append("</div>")

    if report.heavily_used:
        parts.append("<h2>Most-applied memories</h2>")
        parts.append('<ul class="bare">')
        for stats in report.heavily_used[:10]:
            parts.append(
                f'<li><a href="/memories/{html.escape(stats.id)}">'
                f"{html.escape(stats.summary or stats.id)}</a> "
                f'<span class="muted">applied {stats.applied_count}×</span></li>'
            )
        parts.append("</ul>")
    else:
        parts.append(
            '<p class="muted">No memories have crossed the heavily-used floor yet '
            "(record_use(applied) events accumulate over time).</p>"
        )

    return "".join(parts)


def _render_memory_list(
    summaries: list[Any], *, query: str = "", scope_filter: str = ""
) -> str:
    """List view with a top search/filter bar."""
    parts: list[str] = []
    parts.append('<form method="get" action="/memories">')
    parts.append(
        f'<input type="text" name="q" placeholder="Search summaries…" '
        f'value="{html.escape(query)}" style="margin-bottom:0.5rem"/>'
    )
    parts.append(
        f'<input type="text" name="scope" placeholder="Filter by scope (optional)…" '
        f'value="{html.escape(scope_filter)}" style="margin-bottom:0.5rem"/>'
    )
    parts.append('<button type="submit">Search</button>')
    parts.append("</form>")
    parts.append(f'<p class="muted">{len(summaries)} memories</p>')

    if not summaries:
        parts.append('<p class="muted">No matching memories.</p>')
        return "".join(parts)

    for s in summaries:
        scope_tags = " ".join(
            f'<span class="tag">{html.escape(sc)}</span>' for sc in s.scopes
        )
        cat = (
            f'<span class="tag warn">{html.escape(s.category.value)}</span>'
            if getattr(s, "category", None) is not None
            else ""
        )
        verified_at = getattr(s, "last_verified_at", None)
        verify_tag = ""
        if verified_at is None:
            verify_tag = '<span class="tag bad">never verified</span>'
        parts.append(
            f'<div class="card">'
            f'<a href="/memories/{html.escape(s.id)}"><strong>'
            f"{html.escape(s.summary or s.id)}</strong></a><br/>"
            f"{scope_tags}{cat}{verify_tag}"
            f'<div class="muted">id={html.escape(s.id)} · created '
            f"{html.escape(s.created.isoformat())}</div>"
            f"</div>"
        )
    return "".join(parts)


def _render_memory_detail(
    memory: Any, *, stale_after_days: int, read_only: bool = False
) -> str:
    """Full body + metadata + verify form (form omitted in read-only mode).

    `stale_after_days` is the verification freshness window (the
    `behavior.verification_stale_days` config knob). When the memory
    has been verified but the verification is older than the window,
    a `stale (verified Nd ago)` warn tag is appended next to the raw
    timestamp — the curation surface must not collapse verified-but-stale
    into a bare "verified", since that's the exact memory the staleness
    model exists to flag. The comparison routes through the same
    `compute_verification_status` helper `_response` / `health` use, so
    the web verdict can't drift from the rest of the system and the
    naive/aware-datetime normalisation is handled in one place.
    """
    scope_tags = " ".join(
        f'<span class="tag">{html.escape(sc)}</span>' for sc in memory.scopes
    )
    verified_str = (
        html.escape(memory.last_verified_at.isoformat())
        if memory.last_verified_at is not None
        else "never"
    )
    stale_tag = ""
    if memory.last_verified_at is not None:
        status = compute_verification_status(
            memory.last_verified_at,
            now=datetime.now(timezone.utc),
            stale_after_days=stale_after_days,
        )
        if status.status == "stale":
            stale_tag = (
                f' <span class="tag warn">stale '
                f"(verified {int(status.age_days or 0)}d ago)</span>"
            )
    body_html = html.escape(memory.body)

    links_section = ""
    if memory.links:
        items = "".join(
            f"<li><strong>{html.escape(link.type.value)}</strong> → "
            f'<a href="/memories/{html.escape(link.target_id)}">'
            f"{html.escape(link.target_id)}</a>"
            + (f": {html.escape(link.note)}" if link.note else "")
            + "</li>"
            for link in memory.links
        )
        links_section = f"<h2>Links</h2><ul>{items}</ul>"

    verified_paths_section = ""
    if memory.verified_paths:
        items = "".join(
            f"<li><code>{html.escape(p)}</code></li>" for p in memory.verified_paths
        )
        verified_paths_section = f"<h2>Verified paths</h2><ul>{items}</ul>"
    if memory.verified_absent_paths:
        items = "".join(
            f"<li><code>{html.escape(p)}</code></li>"
            for p in memory.verified_absent_paths
        )
        verified_paths_section += f"<h2>Expected-absent paths</h2><ul>{items}</ul>"

    verify_section = (
        ""
        if read_only
        else (
            f"<h2>Verify</h2>"
            f'<form method="post" action="/memories/{html.escape(memory.id)}/verify">'
            f'<input type="text" name="note" placeholder="Optional note (what you checked)"/>'
            f'<button type="submit">Mark verified now</button>'
            f"</form>"
        )
    )
    return (
        f'<div class="card">'
        f"<div>{scope_tags}</div>"
        f'<div class="muted">id={html.escape(memory.id)} · created '
        f"{html.escape(memory.created.isoformat())} · updated "
        f"{html.escape(memory.updated.isoformat())} · verified {verified_str}"
        f"{stale_tag}"
        f"</div>"
        f"<h2>Body</h2>"
        f"<pre>{body_html}</pre>"
        f"{links_section}"
        f"{verified_paths_section}"
        f"{verify_section}"
        f"</div>"
    )


def _render_health(report: Any) -> str:
    """Full memory_health rollup, every bucket rendered."""
    parts: list[str] = []
    parts.append(_render_overview(report))

    if report.dead_weight:
        parts.append("<h2>Dead weight</h2>")
        parts.append(
            '<p class="muted">Retrieved within the window but never applied. '
            "Either the body is misleading, the scopes are wrong, or the "
            "content is duplicate-noise. Consider memory_update or "
            "memory_remove.</p>"
        )
        parts.append('<ul class="bare">')
        for stats in report.dead_weight[:20]:
            parts.append(
                f'<li><a href="/memories/{html.escape(stats.id)}">'
                f"{html.escape(stats.summary or stats.id)}</a> "
                f'<span class="muted">retrieved {stats.retrieval_count}× '
                f"never applied</span></li>"
            )
        parts.append("</ul>")

    if report.cold_memories:
        parts.append("<h2>Cold memories</h2>")
        parts.append(
            '<p class="muted">Never retrieved in the window. Is the trigger '
            "for this memory still real?</p>"
        )
        parts.append('<ul class="bare">')
        for stats in report.cold_memories[:20]:
            parts.append(
                f'<li><a href="/memories/{html.escape(stats.id)}">'
                f"{html.escape(stats.summary or stats.id)}</a></li>"
            )
        parts.append("</ul>")

    if report.contradicted:
        parts.append("<h2>Unresolved contradictions</h2>")
        parts.append('<ul class="bare">')
        for stats in report.contradicted[:20]:
            parts.append(
                f'<li><a href="/memories/{html.escape(stats.id)}">'
                f"{html.escape(stats.summary or stats.id)}</a> "
                f'<span class="tag bad">contradicted</span></li>'
            )
        parts.append("</ul>")

    if report.rare_scopes:
        parts.append("<h2>Rare scopes (possible typos)</h2>")
        items = "".join(
            f"<li><code>{html.escape(s)}</code></li>" for s in report.rare_scopes
        )
        parts.append(f"<ul>{items}</ul>")

    return "".join(parts)


def _render_tombstones(tombstones: list[Any]) -> str:
    if not tombstones:
        return '<p class="muted">No tombstones.</p>'
    parts: list[str] = [f'<p class="muted">{len(tombstones)} tombstoned memories</p>']
    for t in tombstones:
        parts.append(
            f'<div class="card">'
            f"<strong>{html.escape(t.summary or t.id)}</strong><br/>"
            f'<span class="muted">id={html.escape(t.id)} · removed '
            f"{html.escape(t.removed.isoformat())}</span>"
            f"<p>Reason: {html.escape(t.removed_reason or '<none>')}</p>"
            f"</div>"
        )
    return "".join(parts)


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def _same_origin(origin: str | None, referer: str | None) -> bool:
    """Decide whether a state-changing POST originated from the UI itself.

    Returns True when an Origin or Referer header points at a loopback
    host (`localhost`, `127.0.0.1`, `[::1]`) on any port. Same-machine
    coverage is the entire trust model — the UI binds loopback by
    default, and a user who deliberately exposes it to a LAN is
    accepting the implied trust of every browser on that LAN.

    Header-less POSTs are REJECTED. Browsers reliably send Origin on
    POSTs (the HTML spec requires it for non-safe method requests
    initiated from a document); a request with neither Origin nor
    Referer is a non-browser tool (`curl -X POST ...`) hitting the
    endpoint directly. In the LAN-exposed configuration that would
    otherwise be an unauthenticated state-mutation primitive for any
    other host that can reach the socket. CLI users who genuinely
    need to script against the UI should set `-H "Origin:
    http://127.0.0.1:<port>"` — the standard CSRF-safe pattern.
    """
    from urllib.parse import urlparse

    candidates = [h for h in (origin, referer) if h]
    if not candidates:
        return False
    for header in candidates:
        try:
            host = (urlparse(header).hostname or "").lower()
        except ValueError:
            return False
        if host not in {"localhost", "127.0.0.1", "::1"}:
            return False
    return True


def build_app(
    config: Config, store: Store | None = None, *, read_only: bool = False
) -> "FastAPI":
    """Build a FastAPI app wired to the given store. The factory
    pattern lets tests inject a hermetic store; production code uses
    the default config-resolved one.

    ``read_only=True`` is the --tunnel posture: the one mutating
    endpoint (verify) answers 403 before doing anything else, the
    verify form disappears from detail pages, and the CSRF plumbing
    is not emitted. The gate lives at the app layer on purpose — a
    tunnel is a transport, not a policy, and the policy must hold
    even if the operator points a different tunnel at the port.

    Raises ImportError when the ``[ui]`` extra isn't installed — the
    CLI catches this and renders a clean install hint.
    """
    try:
        from fastapi import FastAPI, Form, Header, HTTPException
        from fastapi.responses import HTMLResponse, RedirectResponse
    except ImportError as exc:
        raise ImportError(
            "FastAPI is required for the web UI. Install with "
            "`pip install bettermemory[ui]`."
        ) from exc

    store = store or Store(config.resolved_directory())
    app = FastAPI(title="bettermemory")

    # audit H4 — per-process random CSRF token. Generated once at
    # app-build time, served in every page's <meta name="csrf-token">
    # tag, and required on every mutating endpoint. Loopback-name-only
    # same-origin checks (the prior defence) are bypassable when the
    # operator binds --host 0.0.0.0 (DNS rebinding, attacker-controlled
    # Origin header from non-browser clients), so the load-bearing
    # defence is now the token. 32 random bytes -> ~43 url-safe chars,
    # large enough that brute-force during the UI's session lifetime
    # is not a concern.
    csrf_token = secrets.token_urlsafe(32)
    # Stash on the app so tests can read the value without scraping HTML.
    app.state.csrf_token = csrf_token
    app.state.read_only = read_only

    # Cap the verify note at 500 chars — same discipline as
    # `claim_excerpts` on `memory_record_use`. The UI's note field is a
    # short "what did I check" prompt, not a free-form blob; bounding
    # it here keeps a paste-bomb from inflating the event log.
    _NOTE_MAX_CHARS = 500

    def _layout_resp(title: str, body: str) -> HTMLResponse:
        return HTMLResponse(
            _layout(title, body, store.root, csrf_token, read_only=read_only)
        )

    def _check_csrf(header_token: str | None, form_token: str | None) -> None:
        """audit H4 — constant-time check against the per-process
        token. Accepts the token in either the X-CSRF-Token header
        (fetch path) or a `csrf_token` form field (plain <form>
        path, since forms can't set custom request headers). Raises
        403 on miss; the caller doesn't have to do anything else.
        """
        supplied = header_token or form_token or ""
        if not supplied or not secrets.compare_digest(supplied, csrf_token):
            raise HTTPException(
                status_code=403,
                detail="missing or invalid CSRF token",
            )

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        report = report_for_directory(
            store.root,
            window_days=30,
            heavily_used_top_k=10,
            heavily_used_min_applied=config.behavior.heavily_used_min_applied,
            verification_stale_days=config.behavior.verification_stale_days,
            caller_origin=capture_origin(),
        )
        return _layout_resp("Overview", _render_overview(report))

    @app.get("/memories", response_class=HTMLResponse)
    def memories(q: str = "", scope: str = "") -> HTMLResponse:
        # Defence-in-depth: the scope filter feeds straight into the
        # store's set-intersection logic with no SQL or shell exposure,
        # so this isn't an injection vector — but we run the same
        # `validate_scope` MCP handlers use so a malformed scope query
        # param (e.g. `?scope=../`) surfaces a clear 400 instead of
        # silently returning an empty list. Empty string = no filter.
        if scope:
            try:
                scope = validate_scope(scope)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        summaries = store.list_summaries(scopes=[scope] if scope else None)
        if q:
            needle = q.lower()
            summaries = [s for s in summaries if needle in (s.summary or "").lower()]
        return _layout_resp(
            "Memories",
            _render_memory_list(summaries, query=q, scope_filter=scope),
        )

    @app.get("/memories/{memory_id}", response_class=HTMLResponse)
    def memory_detail(memory_id: str) -> HTMLResponse:
        try:
            memory = store.load_one(memory_id)
        except (MemoryNotFoundError, TombstonedError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return _layout_resp(
            memory.body.splitlines()[0][:60] if memory.body else memory_id,
            _render_memory_detail(
                memory,
                stale_after_days=config.behavior.verification_stale_days,
                read_only=read_only,
            ),
        )

    @app.post("/memories/{memory_id}/verify")
    def memory_verify(
        memory_id: str,
        note: str = Form(default=""),
        csrf_token_form: str | None = Form(default=None, alias="csrf_token"),
        origin: str | None = Header(default=None),
        referer: str | None = Header(default=None),
        x_csrf_token: str | None = Header(default=None, alias="X-CSRF-Token"),
    ) -> RedirectResponse:
        # Read-only gate first — before CSRF, origin, or any parsing.
        # The 403 names the posture so a viewer who taps a stale
        # bookmark understands why the mutation vanished.
        if read_only:
            raise HTTPException(
                status_code=403,
                detail=(
                    "this UI is read-only (--tunnel mode); "
                    "verify from the CLI or a local `bettermemory ui`"
                ),
            )
        # audit H4 — primary CSRF defence is the per-process token.
        # Without it the prior `_same_origin` gate was bypassable when
        # the operator passed --host 0.0.0.0: an attacker could forge
        # `Origin: http://localhost:8765` from any non-browser client
        # on the LAN (DNS rebinding from a browser also defeats it).
        # The token is unguessable to anyone who hasn't pulled an HTML
        # page from this same process.
        _check_csrf(x_csrf_token, csrf_token_form)
        # Belt-and-suspenders: keep the loopback-name same-origin gate
        # so plain `curl -X POST` from another host on the LAN still
        # gets rejected even before the token check runs (defence in
        # depth — the token is the load-bearing check, but rejecting
        # the obviously-cross-origin case early surfaces a clearer
        # error). Browsers reliably send Origin on POST.
        if not _same_origin(origin, referer):
            raise HTTPException(
                status_code=403,
                detail="cross-origin form submission rejected",
            )
        if len(note) > _NOTE_MAX_CHARS:
            raise HTTPException(
                status_code=400,
                detail=f"note too long ({len(note)} > {_NOTE_MAX_CHARS} chars)",
            )
        try:
            store.mark_verified(memory_id)
        except (MemoryNotFoundError, TombstonedError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        # Log a "verify" event so the audit trail captures the
        # web-driven verification with the optional note.
        from .events import Recorder

        rec = Recorder(
            root=store.root,
            session_id="web-ui",
            enabled=config.telemetry.enabled,
            log_queries_verbatim=config.telemetry.log_queries_verbatim,
        )
        rec.record("verify", id=memory_id, note=note or None, source="web-ui")
        return RedirectResponse(url=f"/memories/{memory_id}", status_code=303)

    @app.get("/health", response_class=HTMLResponse)
    def health() -> HTMLResponse:
        report = report_for_directory(
            store.root,
            window_days=30,
            heavily_used_top_k=10,
            heavily_used_min_applied=config.behavior.heavily_used_min_applied,
            verification_stale_days=config.behavior.verification_stale_days,
            caller_origin=capture_origin(),
        )
        return _layout_resp("Health", _render_health(report))

    @app.get("/tombstones", response_class=HTMLResponse)
    def tombstones() -> HTMLResponse:
        tombs = store.list_tombstones()
        return _layout_resp("Tombstones", _render_tombstones(tombs))

    return app


# ---------------------------------------------------------------------------
# Tunnel orchestration (`bettermemory ui --tunnel`)
# ---------------------------------------------------------------------------
#
# The tunnel feature is lifecycle-only: spawn the provider CLI pointed
# at the loopback port and let its own stdout/stderr flow to the
# terminal — every provider prints its URL itself, so there is no
# output parsing to drift out of sync with. What bettermemory owns is
# the POLICY: a tunneled UI is always the read-only app (see
# `build_app`), and the bind stays loopback (the tunnel is the only
# way in).


class TunnelError(RuntimeError):
    """Raised when a tunnel cannot be set up; the message is the
    user-facing hint (missing binary, conflicting flags)."""


# The macOS Tailscale app ships its CLI inside the app bundle and does
# not put it on PATH; probing this well-known location makes bare
# `--tunnel` work on the most common desktop install.
_MACOS_TAILSCALE_APP_CLI = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"

# provider -> is the resulting URL reachable by anyone on the internet?
_TUNNEL_PROVIDERS: dict[str, bool] = {
    "tailnet": False,  # tailscale serve — tailnet members only
    "funnel": True,  # tailscale funnel — public internet
    "cloudflare": True,  # cloudflared quick tunnel — public internet
}


def _find_tailscale() -> str | None:
    """Locate the tailscale CLI: PATH first, then the macOS app bundle."""
    found = shutil.which("tailscale")
    if found:
        return found
    if sys.platform == "darwin" and Path(_MACOS_TAILSCALE_APP_CLI).is_file():
        return _MACOS_TAILSCALE_APP_CLI
    return None


def _find_cloudflared() -> str | None:
    return shutil.which("cloudflared")


def resolve_tunnel_provider(requested: str) -> tuple[str, str]:
    """Map a --tunnel value to ``(provider, binary_path)``.

    ``auto`` prefers the tailnet-only provider when tailscale is
    installed — for a personal memory store, "my own devices can
    read it" is the sane default exposure — and falls back to a
    public cloudflared quick tunnel. Explicit providers fail with an
    install hint when their binary is missing.
    """
    if requested == "auto":
        tailscale = _find_tailscale()
        if tailscale:
            return "tailnet", tailscale
        cloudflared = _find_cloudflared()
        if cloudflared:
            return "cloudflare", cloudflared
        raise TunnelError(
            "--tunnel needs a tunnel CLI: install Tailscale "
            "(https://tailscale.com/download) for a tailnet-only URL, "
            "or cloudflared for a public quick tunnel."
        )
    if requested in ("tailnet", "funnel"):
        tailscale = _find_tailscale()
        if not tailscale:
            raise TunnelError(
                f"--tunnel {requested} requires the tailscale CLI "
                "(https://tailscale.com/download)."
            )
        return requested, tailscale
    if requested == "cloudflare":
        cloudflared = _find_cloudflared()
        if not cloudflared:
            raise TunnelError(
                "--tunnel cloudflare requires the cloudflared CLI "
                "(https://developers.cloudflare.com/cloudflare-one/"
                "connections/connect-networks/downloads/)."
            )
        return "cloudflare", cloudflared
    raise TunnelError(f"unknown tunnel provider: {requested!r}")


def _tunnel_argv(provider: str, binary: str, port: int) -> list[str]:
    """Foreground invocation for each provider — all three print their
    URL to the terminal and tear the tunnel down on exit."""
    if provider == "tailnet":
        return [binary, "serve", str(port)]
    if provider == "funnel":
        return [binary, "funnel", str(port)]
    if provider == "cloudflare":
        return [binary, "tunnel", "--url", f"http://127.0.0.1:{port}"]
    raise TunnelError(f"unknown tunnel provider: {provider!r}")


# The tunnel provider runs under this supervisor shim (spawned as
# `python -c _TUNNEL_SUPERVISOR <provider argv...>`). The shim's stdin
# is a pipe the serving process holds open and never writes: when the
# server exits for ANY reason — including SIGKILL, where no userspace
# teardown can run — the kernel closes the pipe, the shim's blocking
# read returns EOF, and the shim reaps the provider before exiting.
# Provider CLIs can't be trusted to watch the pipe themselves:
# `tailscale serve` keeps running after stdin EOF (verified against
# the real CLI, 2026-07-10), and cloudflared ignores stdin too. The
# shim also mirrors SIGTERM/SIGINT/SIGHUP into a reap so the
# _reap_tunnel terminate() path stays prompt.
#
# A second watchdog covers the OPPOSITE direction: the stdin pipe only
# signals PARENT death, so if the provider exits first (never came up —
# tailscaled down or logged out, Funnel not in the tailnet ACLs,
# cloudflared can't reach the edge — or a mid-session logout) the shim
# would otherwise block on the stdin read forever while the parent's
# poll() stays None and the dead share goes unnoticed. A background
# thread waits on the provider and exits the shim with the provider's
# own returncode, so shim death mirrors provider death and serve()'s
# watcher (see _watch_tunnel_provider) can flag the dead URL.
_TUNNEL_SUPERVISOR = """\
import os
import signal
import subprocess
import sys
import threading

child = subprocess.Popen(sys.argv[1:], stdin=subprocess.DEVNULL)


def _reap():
    if child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()


def _on_signal(signum, _frame):
    _reap()
    signal.signal(signum, signal.SIG_DFL)
    signal.raise_signal(signum)


for _sig in [signal.SIGTERM, signal.SIGINT] + (
    [signal.SIGHUP] if hasattr(signal, "SIGHUP") else []
):
    # Respect an inherited SIG_IGN (classic nohup rule). The shim is
    # spawned before serve() installs its own handlers, so under
    # `nohup ... &` it inherits SIG_IGN for SIGHUP. Clobbering that
    # would reap the provider and re-raise SIGHUP under SIG_DFL,
    # tearing the share down beneath a server the operator detached.
    # Parent-death teardown still runs via the stdin watchdog + finally.
    if signal.getsignal(_sig) is signal.SIG_IGN:
        continue
    signal.signal(_sig, _on_signal)


def _mirror_provider_death():
    # Provider exited on its own => the stdin watchdog never fires
    # (parent still alive). Exit with the provider's returncode so the
    # parent observes the shim die exactly as it would on provider death.
    child.wait()
    os._exit(child.returncode)


threading.Thread(target=_mirror_provider_death, daemon=True).start()

try:
    sys.stdin.buffer.read()  # EOF == the server process died (any cause)
finally:
    _reap()
"""


def _start_tunnel(provider: str, binary: str, port: int) -> "subprocess.Popen[bytes]":
    """Spawn the tunnel provider under the _TUNNEL_SUPERVISOR shim
    with inherited stdout/stderr (the provider prints its own URL
    through it). Emits the exposure warning for public providers
    before anything is reachable.

    The returned handle is the shim; its stdin pipe is the
    parent-death watchdog that guarantees the provider cannot outlive
    this process (see _TUNNEL_SUPERVISOR). A leaked provider would
    keep the share URL alive and pointed at whatever binds the port
    next."""
    if _TUNNEL_PROVIDERS.get(provider, True):
        log.warning(
            "--tunnel %s creates a PUBLIC, unauthenticated URL: anyone "
            "who obtains the link can read this memory store (read-only). "
            "Use --tunnel tailnet to restrict access to your own devices.",
            provider,
        )
    argv = _tunnel_argv(provider, binary, port)
    log.info(
        "starting %s tunnel (%s) — the tunnel prints its URL below; "
        "Ctrl-C stops both the tunnel and the UI",
        provider,
        " ".join(argv),
    )
    supervised = [sys.executable, "-c", _TUNNEL_SUPERVISOR, *argv]
    return subprocess.Popen(supervised, stdin=subprocess.PIPE)


def _reap_tunnel(proc: "subprocess.Popen[bytes] | None") -> None:
    """Stop the tunnel child if it is still running. Idempotent.

    Closes stdin first — EOF alone stops providers that watch it
    (tailscale serve) — then terminate()/kill() as the backstop for
    providers that don't (cloudflared)."""
    if proc is None or proc.poll() is not None:
        return
    if proc.stdin is not None:
        with contextlib.suppress(OSError):
            proc.stdin.close()
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


# Backstop grace window for the provider-death watcher
# (`_watch_tunnel_provider`).
#
# When a signal is delivered to serve()'s whole PROCESS GROUP — Ctrl-C at
# a terminal, `systemctl stop` on a cgroup — the supervisor shim, a member
# of that group, receives it directly and reaps + exits within
# milliseconds, so the watcher's `proc.wait()` returns almost immediately.
# ``shutting_down`` must already be set by then or the watcher fires the
# loud "shared URL is DEAD" error on a clean quit (the b5e5542 regression).
#
# The flag IS set in time because serve() overrides the uvicorn server's
# handle_exit hook (see _TunnelServer in serve()) to set it at signal
# DELIVERY — uvicorn calls handle_exit from its own SIGINT/SIGTERM handler
# BEFORE the graceful drain begins. Correctness therefore no longer depends
# on the drain duration at all: it does not matter whether the drain is the
# ~0.1s of an idle quit or several seconds spent draining a slow in-flight
# request over the tunnel — the flag is up the instant the signal arrives.
# (Setting it only from the RESTORED handler _teardown_and_reraise, which
# runs AFTER uvicorn's unbounded graceful shutdown, was the old design and
# lost the race on any BUSY quit — a request slower than this window drained
# while the watcher had already cried wolf.)
#
# This window is now only a backstop for the microsecond race between the
# shim's group-signalled exit and uvicorn's handle_exit running in this
# process — both are driven by the same signal, so the gap is tiny, but the
# interleaving is not guaranteed, so a short wait absorbs it. A genuine
# mid-session provider death signals neither hook, so the flag stays unset,
# the window lapses, and the watcher still fires — at most this long late,
# which is fine for a human-facing "your share died" notice. (SIGHUP is
# handled directly by serve()'s own handler, which likewise sets the flag
# before reaping, so it is already set when the watcher wakes.)
_PROVIDER_DEATH_GRACE_SECONDS = 1.0


# `uvicorn.main.STARTUP_FAILURE`, the exit code `uvicorn.run()` uses when the
# server never bound. Spelled out rather than imported: `uvicorn.main` is
# shadowed by a click Command of the same name, so the constant is not
# reachable as a public attribute.
_UVICORN_STARTUP_FAILURE = 3


def _watch_tunnel_provider(
    proc: "subprocess.Popen[bytes]",
    provider: str,
    shutting_down: threading.Event,
) -> None:
    """Block until the tunnel shim exits, then — unless we're already
    tearing down — log LOUDLY that the shared URL is dead.

    The shim mirrors its provider child's exit (see _TUNNEL_SUPERVISOR),
    so this returning while the server is still up means the provider
    died on its own: it never came up (tailscaled down or logged out,
    Funnel not enabled in the tailnet ACLs, cloudflared could not reach
    the edge) or it dropped mid-session. serve() keeps answering
    read-only on loopback, so nothing else would tell the user the share
    silently no-op'd — this log is the only signal. Runs on a daemon
    thread. A clean exit stays quiet: serve() sets ``shutting_down`` at
    signal DELIVERY (via the uvicorn server's handle_exit override), so a
    group-delivered Ctrl-C / SIGTERM that reaps the shim before serve()
    finishes uvicorn's graceful drain finds the flag already up — even
    when a slow in-flight request stretches that drain well past the
    grace window. The bounded wait (_PROVIDER_DEATH_GRACE_SECONDS) is a
    backstop for the tiny ordering race between the shim's exit and the
    handle_exit hook running. A genuine provider death never sets the
    flag, so the window lapses and the error still fires (at most that
    long late).
    """
    proc.wait()
    # Give a concurrent teardown its grace window to announce itself
    # before crying wolf — a group-delivered signal races serve()'s
    # not-yet-set flag; see _PROVIDER_DEATH_GRACE_SECONDS.
    if shutting_down.wait(timeout=_PROVIDER_DEATH_GRACE_SECONDS):
        return
    log.error(
        "tunnel provider %r exited: the shared URL is now DEAD, but the "
        "local UI is still serving read-only on loopback. Likely cause: "
        "the tunnel CLI could not start (tailscaled not running or logged "
        "out, Funnel not enabled in your tailnet ACLs, or cloudflared "
        "could not reach the edge) or the tunnel dropped mid-session. "
        "Stop the UI and re-run `bettermemory ui --tunnel` after fixing "
        "the provider.",
        provider,
    )


def serve(
    config: Config,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    tunnel: str | None = None,
) -> None:
    """Run the web UI via uvicorn. Blocking — the caller (CLI) cedes
    control until SIGINT.

    Local-only by default (127.0.0.1). To expose to other hosts on
    a trusted network, pass a different host like '0.0.0.0' from the
    CLI; the server prints a warning when binding non-loopback so
    operators don't accidentally expose curation surfaces.

    ``tunnel`` (a --tunnel value: auto/tailnet/funnel/cloudflare)
    spawns the provider CLI against the loopback bind and forces the
    app into read-only mode for the whole process lifetime. Tunnel
    mode requires a loopback host — the tunnel is the front door.

    Tunnel teardown is guaranteed three ways, because a leaked tunnel
    child keeps the share URL alive and pointed at whatever binds the
    port next: the supervisor shim's stdin watchdog from
    _start_tunnel (survives even SIGKILL of this process), the signal
    handlers installed below (SIGTERM/SIGINT re-raised by uvicorn,
    plus SIGHUP), and the ``finally`` for exception exits. One
    exception, the classic nohup rule: a signal inherited as SIG_IGN
    is left ignored rather than clobbered, so a deliberately detached
    server (`nohup ... &`) survives the hangup — the stdin watchdog and
    finally still cover its teardown.

    The reverse failure — the PROVIDER dying while this process lives
    on — is detected by a daemon watcher (_watch_tunnel_provider): the
    shim mirrors its provider's exit, so the watcher notices the shim
    die early and logs LOUDLY that the shared URL is dead, since the
    loopback UI would otherwise keep serving as if nothing happened.
    """
    try:
        import uvicorn
    except ImportError as exc:
        raise ImportError(
            "uvicorn is required for the web UI. Install with "
            "`pip install bettermemory[ui]`."
        ) from exc

    tunnel_proc: subprocess.Popen[bytes] | None = None
    provider: str | None = None
    if tunnel is not None:
        if not _is_loopback_bind(host):
            raise TunnelError(
                "--tunnel requires a loopback --host (the tunnel is the "
                f"front door); got {host!r}. Drop --host or use 127.0.0.1."
            )
        provider, binary = resolve_tunnel_provider(tunnel)
        log.info("read-only mode: mutations are disabled while tunneling")
        tunnel_proc = _start_tunnel(provider, binary, port)

    app = build_app(config, read_only=tunnel is not None)
    _warn_if_non_loopback_bind(host)
    log.info("bettermemory ui starting on http://%s:%d", host, port)
    # `provider` is bound exactly when `tunnel_proc` is — the one branch
    # above sets both. Testing both is what narrows `provider` to `str` for
    # the type-checker at the watchdog spawn below.
    if tunnel_proc is None or provider is None:
        uvicorn.run(app, host=host, port=port, log_level="warning")
        return

    # uvicorn's capture_signals() swallows SIGINT/SIGTERM for the
    # graceful shutdown, restores the handlers that were active before
    # run(), and then RE-RAISES the signal so the process still dies
    # by it. A ``finally`` around run() therefore never sees a signal
    # exit — the re-raise kills the process inside run(). The teardown
    # must BE the restored handler: reap the child, restore the
    # default disposition, re-deliver the signal. SIGHUP is not
    # captured by uvicorn, so our handler fires for it directly.
    #
    # ``shutting_down`` marks an EXPECTED tunnel_proc exit so the
    # provider-death watcher below stays quiet on a clean Ctrl-C / reap
    # instead of crying "dead share". The load-bearing setter is the
    # ``_TunnelServer.handle_exit`` override installed on the uvicorn
    # server below: uvicorn calls that hook synchronously from its own
    # SIGINT/SIGTERM handler, BEFORE the graceful drain, so the flag is
    # up the instant the signal arrives regardless of how long an
    # in-flight request holds the drain open. _teardown_and_reraise and
    # the finally set it too, as belt-and-suspenders for the SIGHUP path
    # (which uvicorn does not capture) and exception exits.
    shutting_down = threading.Event()

    def _teardown_and_reraise(signum: int, _frame: "FrameType | None") -> None:
        shutting_down.set()
        _reap_tunnel(tunnel_proc)
        signal.signal(signum, signal.SIG_DFL)
        signal.raise_signal(signum)

    teardown_signals = [signal.SIGINT, signal.SIGTERM]
    if hasattr(signal, "SIGHUP"):  # absent on Windows
        teardown_signals.append(signal.SIGHUP)
    previous: dict[signal.Signals, Any] = {}
    # signal.signal is main-thread-only (ValueError otherwise); a
    # non-main-thread caller keeps the stdin watchdog + finally.
    with contextlib.suppress(ValueError):
        for sig in teardown_signals:
            # Respect an inherited SIG_IGN (classic nohup rule): a
            # process deliberately started with a signal ignored — e.g.
            # `nohup bettermemory ui --tunnel tailnet &`, then closing
            # the terminal — must keep ignoring it. Installing
            # _teardown_and_reraise would silently un-ignore it and, on
            # SIGHUP, reap the tunnel then re-raise under SIG_DFL,
            # killing a server the operator meant to detach. Teardown
            # for that exit still runs via the shim's stdin watchdog and
            # the finally below.
            if signal.getsignal(sig) is signal.SIG_IGN:
                continue
            previous[sig] = signal.signal(sig, _teardown_and_reraise)

    # Provider-death watchdog: if the shim exits while uvicorn is still
    # up, the provider went away on its own (never came up, or a
    # mid-session logout) and the shared URL is silently dead. Nothing
    # else notices — poll the shim on a daemon thread and log LOUDLY.
    threading.Thread(
        target=_watch_tunnel_provider,
        args=(tunnel_proc, provider, shutting_down),
        daemon=True,
    ).start()

    # Construct the uvicorn server explicitly instead of calling
    # uvicorn.run(): for these arguments (no reload, no workers, no uds)
    # uvicorn.run() reduces to `Server(Config(...)).run()` — verified
    # against uvicorn 0.46's source. Doing it by hand lets us override
    # handle_exit, the public hook uvicorn's OWN SIGINT/SIGTERM handler
    # calls BEFORE the graceful drain. Setting ``shutting_down`` there
    # closes the provider-death false-alarm race on a BUSY quit: a
    # group-delivered Ctrl-C reaps the supervisor shim within
    # milliseconds, so the watcher's proc.wait() returns while uvicorn is
    # still draining a slow in-flight request. Marking the flag at signal
    # delivery — not after the unbounded drain, which is where the
    # restored _teardown_and_reraise runs — means the watcher sees it
    # immediately, no matter how long the drain takes. handle_exit only
    # fires for SIGINT/SIGTERM (uvicorn's HANDLED_SIGNALS); SIGHUP still
    # runs serve()'s own handler, which sets the flag before reaping.
    class _TunnelServer(uvicorn.Server):
        def handle_exit(self, sig: int, frame: "FrameType | None") -> None:
            shutting_down.set()
            super().handle_exit(sig, frame)

    server = _TunnelServer(
        uvicorn.Config(app, host=host, port=port, log_level="warning")
    )
    try:
        server.run()
    except KeyboardInterrupt:  # pragma: no cover - uvicorn.run() swallows it too
        pass
    finally:
        shutting_down.set()
        _reap_tunnel(tunnel_proc)
        for sig, handler in previous.items():
            with contextlib.suppress(ValueError):
                signal.signal(sig, handler)

    # Restore the last tail of `uvicorn.run()` that building the Server by
    # hand drops: it ends with `sys.exit(STARTUP_FAILURE)` when the server
    # never started. Without this a `--tunnel` bind failure (the port is
    # already in use) returns normally and the CLI exits 0 — a systemd unit
    # with `Restart=on-failure` would not restart, and a shell checking `$?`
    # would read success while nothing is being served. Only reachable when
    # run() returns without starting; a signal exit dies inside run().
    if not server.started:
        raise SystemExit(_UVICORN_STARTUP_FAILURE)


def _warn_if_non_loopback_bind(host: str) -> bool:
    """Emit the H4 non-loopback warning when ``host`` isn't loopback.

    Returns ``True`` if the warning was emitted, ``False`` otherwise.
    Extracted from ``serve()`` so tests can exercise the warning path
    without launching uvicorn — the prior shape had the warning inline
    in ``serve()``, so the only way to assert it fired was to mock
    uvicorn and run the full launch path, which `test_web.py` punted
    on (and ended up testing only the predicate, not the warning).

    audit H4 — resolve the bind address against the canonical loopback
    set rather than name-matching "localhost" / "127.0.0.1" only.
    `--host 0.0.0.0`, `--host ::`, or a non-loopback hostname all
    surface as non-loopback here, so the warning fires on every
    exposed deployment instead of the prior name-only check.
    """
    if _is_loopback_bind(host):
        return False
    log.warning(
        "Binding to a non-loopback address; CSRF token protects "
        "mutations but transport is unencrypted. Use loopback or "
        "front with TLS for sensitive deployments."
    )
    return True


def _is_loopback_bind(host: str) -> bool:
    """Return True if the requested bind host resolves to loopback.

    Resolves the host via the stdlib resolver so a custom hostname
    that points at 127.0.0.1 still counts as loopback. Falls back to
    a string compare when DNS lookup fails — better to err on the
    side of warning than to silently skip the warning on a transient
    resolver failure.
    """
    import socket
    from ipaddress import ip_address

    loopback_names = {"localhost", "127.0.0.1", "::1"}
    if host in loopback_names:
        return True
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    for info in infos:
        try:
            addr = ip_address(info[4][0])
        except (ValueError, IndexError):
            continue
        if not addr.is_loopback:
            return False
    return bool(infos)


__all__ = ["TunnelError", "build_app", "resolve_tunnel_provider", "serve"]
