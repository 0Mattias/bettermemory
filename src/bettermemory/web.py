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

import html
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .config import Config
from .health import report_for_directory
from .models import validate_scope
from .origin import capture as capture_origin
from .store import MemoryNotFoundError, Store, TombstonedError

if TYPE_CHECKING:
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


def _layout(title: str, body: str, store_root: Path) -> str:
    """Render a full HTML page with the standard chrome.

    Title is HTML-escaped for safety; body is trusted (the route
    builds it from escaped pieces internally). Header carries the
    nav and a small "served from" indicator so a user juggling
    multiple stores can tell at a glance which one they're in.
    """
    return (
        "<!doctype html>"
        "<html><head>"
        f"<title>{html.escape(title)} · bettermemory</title>"
        f"<style>{_BASE_STYLE}</style>"
        "</head><body>"
        "<header>"
        '<a href="/">Overview</a>'
        '<a href="/memories">Memories</a>'
        '<a href="/health">Health</a>'
        '<a href="/tombstones">Tombstones</a>'
        f'<span class="muted" style="float:right">'
        f"<strong>{html.escape(str(store_root))}</strong></span>"
        "</header>"
        f"<h1>{html.escape(title)}</h1>"
        f"{body}"
        "</body></html>"
    )


def _render_overview(report: Any) -> str:
    """Dashboard summary built from a HealthReport."""
    n = report.total_active_memories
    debt = report.verification_debt
    never_verified = len(debt.never_verified) if debt else 0
    stale_verifications = len(debt.stale) if debt else 0
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
        f'<input type="text" name="q" placeholder="Search bodies + scopes…" '
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


def _render_memory_detail(memory: Any) -> str:
    """Full body + metadata + verify form."""
    scope_tags = " ".join(
        f'<span class="tag">{html.escape(sc)}</span>' for sc in memory.scopes
    )
    verified_str = (
        html.escape(memory.last_verified_at.isoformat())
        if memory.last_verified_at is not None
        else "never"
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

    return (
        f'<div class="card">'
        f"<div>{scope_tags}</div>"
        f'<div class="muted">id={html.escape(memory.id)} · created '
        f"{html.escape(memory.created.isoformat())} · updated "
        f"{html.escape(memory.updated.isoformat())} · verified {verified_str}"
        f"</div>"
        f"<h2>Body</h2>"
        f"<pre>{body_html}</pre>"
        f"{links_section}"
        f"{verified_paths_section}"
        f"<h2>Verify</h2>"
        f'<form method="post" action="/memories/{html.escape(memory.id)}/verify">'
        f'<input type="text" name="note" placeholder="Optional note (what you checked)"/>'
        f'<button type="submit">Mark verified now</button>'
        f"</form>"
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


def build_app(config: Config, store: Store | None = None) -> "FastAPI":
    """Build a FastAPI app wired to the given store. The factory
    pattern lets tests inject a hermetic store; production code uses
    the default config-resolved one.

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

    # Cap the verify note at 500 chars — same discipline as
    # `claim_excerpts` on `memory_record_use`. The UI's note field is a
    # short "what did I check" prompt, not a free-form blob; bounding
    # it here keeps a paste-bomb from inflating the event log.
    _NOTE_MAX_CHARS = 500

    def _layout_resp(title: str, body: str) -> HTMLResponse:
        return HTMLResponse(_layout(title, body, store.root))

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
            _render_memory_detail(memory),
        )

    @app.post("/memories/{memory_id}/verify")
    def memory_verify(
        memory_id: str,
        note: str = Form(default=""),
        origin: str | None = Header(default=None),
        referer: str | None = Header(default=None),
    ) -> RedirectResponse:
        # CSRF defence: even though the UI binds to 127.0.0.1 by default,
        # any open browser tab on the user's machine can submit a form
        # against localhost — a malicious page could forge a bump to
        # `last_verified_at` and corrupt the trust signal. Require the
        # request's Origin (preferred) or Referer to point at this same
        # UI so cross-site forms are rejected. Both headers are sent by
        # mainstream browsers on form POSTs.
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


def serve(
    config: Config,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> None:
    """Run the web UI via uvicorn. Blocking — the caller (CLI) cedes
    control until SIGINT.

    Local-only by default (127.0.0.1). To expose to other hosts on
    a trusted network, pass a different host like '0.0.0.0' from the
    CLI; the server prints a warning when binding non-loopback so
    operators don't accidentally expose curation surfaces.
    """
    try:
        import uvicorn
    except ImportError as exc:
        raise ImportError(
            "uvicorn is required for the web UI. Install with "
            "`pip install bettermemory[ui]`."
        ) from exc

    app = build_app(config)
    if host != "127.0.0.1" and host != "localhost":
        log.warning(
            "binding to non-loopback host %s — the web UI is read-mostly "
            "but exposes curation surfaces to anyone on the network",
            host,
        )
    log.info("bettermemory ui starting on http://%s:%d", host, port)
    uvicorn.run(app, host=host, port=port, log_level="warning")


__all__ = ["build_app", "serve"]
