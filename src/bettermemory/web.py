"""Local web UI for bettermemory.

A small FastAPI app that surfaces the trust machinery — the staleness
verdict, ranked search, memory_health rollups, eval telemetry, the
episode journal, and a curation preview — plus a memory browser,
detail view, and one-click verify. The CLI tool surface is the
canonical entrypoint for everyday writes / searches; the web UI's
killer use case is the *curation* pass — looking at the rot rollups
and candidate lists side-by-side beats reading them out via tool
calls.

Scope:

- Local-only by default (binds to 127.0.0.1).
- No editing UI: writes happen in-conversation via `memory_write`,
  not from the browser. The UI is read-mostly with one mutation —
  `memory_verify`, since "I just spot-checked this claim" is a
  natural human action. Every other surface, including the eval,
  episodes, and curation-preview pages, is strictly read-only.
- Verdict parity with the MCP surface, by construction: ranked search
  runs the same `search.search` ranker on the same config inputs
  (`handlers.search.resolve_ranking_inputs`), then serialises each hit
  through the same response pipeline `memory_search` runs —
  `ResponseBuilder.hit_to_dict`, then `attach_commit_drift_counts` and
  `attach_recent_negative_outcomes`. A rendered hit's verdict is
  therefore the string `memory_search` returns for that hit in its
  non-expanded form, commit-drift upgrade included; the web computes
  no verdict arithmetic of its own. The detail page folds in commit
  drift the way `memory_show` does — which is also where the
  body-level refinement `expand_top` applies to the MCP top hit
  lives, since no list surface loads bodies.
  The one surface that stops short is the no-query BROWSE list
  (`_render_memory_list`): `store.list_summaries` carries no body, so
  neither path drift nor commit anchors are derivable without loading
  every memory. Those rows deliberately wear a *verification* chip
  (`compute_verification_status`) and never a staleness verdict, so
  they cannot contradict the verdict-bearing surfaces.
- No JS framework: server-side rendered HTML, minimal inline CSS,
  no template engine. Each route returns a complete HTML response
  built from the helper functions below. Cheap to maintain, no
  install-time template discovery story.

Gated behind the optional ``[ui]`` extra. The CLI's `bettermemory ui`
subcommand surfaces a clean install hint when fastapi / uvicorn
isn't available. Module-level imports stay fastapi-free so importing
this module never raises for users without the extra; the framework
loads lazily inside `build_app` / `serve`.
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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from ._response import ResponseBuilder
from .config import Config
from .consolidate import consolidate
from .episodes import EpisodeStore
from .eval import compute_eval
from .events import iter_all_events
from .handlers.search import resolve_ranking_inputs
from .health import report_for_directory
from .models import validate_scope
from .origin import capture as capture_origin
from .search import SearchMode, search as run_search
from .store import MemoryNotFoundError, Store, TombstonedError
from .verify import (
    compute_commit_drift,
    compute_staleness_verdict,
    compute_verification_status,
    detect_path_drift,
)

if TYPE_CHECKING:
    from types import FrameType

    from fastapi import FastAPI


log = logging.getLogger("bettermemory.web")

# Ranked-search result cap for the web list. Wider than memory_search's
# default 5 because a human scans a page where a model pays per-token —
# but still bounded, because rendering the whole store under a broad
# query would bury the ranking this page exists to show off.
_SEARCH_MAX_RESULTS = 30


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------


_BASE_STYLE = """
:root {
    color-scheme: light dark;
    --fg: #1c1c1a;
    --muted: #6d6b64;
    --bg: #faf9f6;
    --card: #ffffff;
    --border: #e3e1d9;
    --border-soft: #eeece5;
    --accent: #1d4ed8;
    --ok: #0e7a4f;   --ok-bg: #e4f3eb;
    --warn: #935800; --warn-bg: #fbf0d9;
    --bad: #b3261e;  --bad-bg: #fbe5e3;
    --code-bg: #f2f1ec;
}
@media (prefers-color-scheme: dark) {
    :root {
        --fg: #e7e5e0;
        --muted: #96948c;
        --bg: #131311;
        --card: #1c1c19;
        --border: #33322c;
        --border-soft: #26251f;
        --accent: #8ab4ff;
        --ok: #53c08a;   --ok-bg: #14291d;
        --warn: #dfa63f; --warn-bg: #2b2110;
        --bad: #ef7168;  --bad-bg: #331413;
        --code-bg: #22221e;
    }
}
* { box-sizing: border-box; }
body {
    font-family: -apple-system, system-ui, BlinkMacSystemFont, sans-serif;
    background: var(--bg);
    color: var(--fg);
    max-width: 1080px;
    margin: 0 auto;
    padding: 1rem 1.25rem 3rem;
    line-height: 1.55;
    font-size: 15px;
}
header {
    display: flex;
    align-items: baseline;
    gap: 1rem;
    flex-wrap: wrap;
    border-bottom: 1px solid var(--border);
    padding-bottom: 0.6rem;
    margin-bottom: 1.25rem;
}
nav { display: flex; gap: 0.9rem; flex-wrap: wrap; }
nav a { color: var(--muted); text-decoration: none; font-weight: 500; padding-bottom: 2px; }
nav a:hover { color: var(--fg); }
nav a.active { color: var(--fg); border-bottom: 2px solid var(--accent); }
.storepath { margin-left: auto; color: var(--muted); font-size: 0.82rem; }
.storepath code { background: none; color: var(--muted); }
h1 { font-size: 1.4rem; margin: 0 0 0.75rem; letter-spacing: -0.01em; }
h2 { font-size: 1.05rem; margin: 1.75rem 0 0.5rem; }
a { color: var(--accent); }
code, .mono {
    font-family: ui-monospace, "SF Mono", Menlo, monospace;
    font-size: 0.86em;
    background: var(--code-bg);
    padding: 0.05rem 0.3rem;
    border-radius: 4px;
}
.card {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 0.8rem 1rem;
    margin-bottom: 0.6rem;
}
.card h3 { margin: 0 0 0.25rem; font-size: 0.98rem; }
.card h3 a { color: var(--fg); text-decoration: none; }
.card h3 a:hover { text-decoration: underline; }
.muted { color: var(--muted); font-size: 0.86rem; }
.chip {
    display: inline-block;
    border: 1px solid var(--border);
    color: var(--muted);
    padding: 0 0.45rem;
    border-radius: 999px;
    font-size: 0.78rem;
    line-height: 1.5;
    margin-right: 0.3rem;
    white-space: nowrap;
}
.chip.ok   { background: var(--ok-bg);   color: var(--ok);   border-color: transparent; }
.chip.warn { background: var(--warn-bg); color: var(--warn); border-color: transparent; }
.chip.bad  { background: var(--bad-bg);  color: var(--bad);  border-color: transparent; }
form { display: inline; }
.searchbar { display: flex; gap: 0.5rem; flex-wrap: wrap; margin-bottom: 0.9rem; }
.searchbar input[type="text"] { flex: 1 1 16rem; }
input[type="text"], textarea {
    padding: 0.45rem 0.6rem;
    border: 1px solid var(--border);
    border-radius: 6px;
    font-family: inherit;
    font-size: 0.95rem;
    background: var(--card);
    color: var(--fg);
}
button {
    background: var(--accent);
    color: var(--bg);
    border: none;
    padding: 0.42rem 0.9rem;
    border-radius: 6px;
    cursor: pointer;
    font-size: 0.9rem;
    font-weight: 600;
}
button:hover { filter: brightness(1.08); }
pre {
    background: var(--code-bg);
    padding: 0.75rem;
    border-radius: 6px;
    overflow-x: auto;
    font-size: 0.84rem;
    line-height: 1.5;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
}
ul.bare { list-style: none; padding: 0; margin: 0.25rem 0; }
ul.bare li { padding: 0.28rem 0; border-bottom: 1px solid var(--border-soft); }
ul.bare li:last-child { border-bottom: none; }
table { border-collapse: collapse; width: 100%; font-size: 0.88rem; margin: 0.4rem 0 1rem; }
th, td { text-align: left; padding: 0.35rem 0.6rem 0.35rem 0; border-bottom: 1px solid var(--border-soft); }
th { color: var(--muted); font-weight: 500; font-size: 0.8rem; }
td.num, th.num { text-align: right; padding-right: 1rem; font-variant-numeric: tabular-nums; }
.bucket-summary { display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 0.6rem; margin-bottom: 1rem; }
.bucket-summary .item {
    background: var(--card);
    border: 1px solid var(--border);
    padding: 0.55rem 0.75rem;
    border-radius: 8px;
}
.bucket-summary .label { font-size: 0.76rem; color: var(--muted); display: block; }
.bucket-summary .value { font-size: 1.35rem; font-weight: 650; font-variant-numeric: tabular-nums; }
.bucket-summary .value.bad { color: var(--bad); }
.bucket-summary .value.warn { color: var(--warn); }
.bucket-summary .value.ok { color: var(--ok); }
.hit-meta { display: flex; align-items: center; flex-wrap: wrap; gap: 0.15rem 0; margin-top: 0.3rem; }
details { margin: 0.4rem 0; }
details summary { cursor: pointer; color: var(--muted); font-size: 0.86rem; }
.snippet { margin: 0.35rem 0 0; font-size: 0.9rem; color: var(--fg); }
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
    active: str = "",
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
    ro_badge = '<span class="chip warn">read-only</span> ' if read_only else ""
    nav_items = (
        ("/", "Overview"),
        ("/memories", "Memories"),
        ("/health", "Health"),
        ("/curation", "Curation"),
        ("/eval", "Eval"),
        ("/episodes", "Episodes"),
        ("/tombstones", "Tombstones"),
    )
    nav_links: list[str] = []
    for href, label in nav_items:
        cls = ' class="active"' if href == active else ""
        nav_links.append(f'<a href="{href}"{cls}>{label}</a>')
    nav = "".join(nav_links)
    return (
        "<!doctype html>"
        "<html><head>"
        f"<title>{html.escape(title)} · bettermemory</title>"
        '<meta name="viewport" content="width=device-width, initial-scale=1"/>'
        f"{csrf_meta}"
        f"<style>{_BASE_STYLE}</style>"
        "</head><body>"
        "<header>"
        f"<nav>{nav}</nav>"
        f'<span class="storepath">{ro_badge}'
        f"<code>{html.escape(str(store_root))}</code></span>"
        "</header>"
        f"<h1>{html.escape(title)}</h1>"
        f"{body}"
        f"{csrf_script}"
        "</body></html>"
    )


# The web surface never invents verdict language: labels and severity
# classes map one-to-one onto the strings `compute_staleness_verdict`
# returns, and anything unexpected renders as itself with the warn
# treatment rather than being silently dropped.
_VERDICT_META = {
    "fresh": ("fresh", "ok"),
    "spot_check_recommended": ("spot-check recommended", "warn"),
    "spot_check_required": ("spot-check required", "bad"),
}


def _verdict_chip(verdict: str) -> str:
    label, cls = _VERDICT_META.get(verdict, (verdict, "warn"))
    return f'<span class="chip {cls}">{html.escape(label)}</span>'


def _scope_chips(scopes: Any) -> str:
    return "".join(f'<span class="chip">{html.escape(str(sc))}</span>' for sc in scopes)


def _date(dt: Any) -> str:
    """Short date for list rows; full ISO stays on the detail page.

    Accepts a `datetime` or an already-serialised ISO-8601 string — the
    ranked-hit rows render from `ResponseBuilder.hit_to_dict` output,
    which stringifies timestamps — trimming the string form to its date
    prefix so both inputs render the same way.
    """
    try:
        formatted = str(dt.strftime("%Y-%m-%d"))
    except AttributeError:
        formatted = str(dt)[:10]
    return html.escape(formatted)


def _commit_drift_chip(count: int | None) -> str:
    """Commit-drift badge, shared by the ranked-hit rows and the detail
    page so the two spell the same signal identically.

    `None` means the signal isn't applicable for this memory (caller
    outside its origin repo, never verified, or no claim anchors) and
    renders nothing — mirroring the MCP surface's omit-the-key contract
    rather than inventing a third "unknown" badge.
    """
    if count is None:
        return ""
    if count:
        return f'<span class="chip warn">commit drift: {int(count)} since verify</span>'
    return '<span class="chip ok">commit drift: clean</span>'


def _negative_outcome_chips(entries: Any) -> str:
    """`recent_negative_outcomes` badges — the same rejections
    `memory_search` annotates a hit with, so a curator sees what the
    model sees ("this was ignored twice; stop re-surfacing it")."""
    if not entries:
        return ""
    parts: list[str] = []
    for entry in entries:
        label = str(entry.get("outcome") or "?")
        count = int(entry.get("count_in_window") or 0)
        parts.append(f'<span class="chip bad">{html.escape(label)} ×{count}</span>')
    return "".join(parts)


def _verification_chip(
    last_verified_at: datetime | None, *, stale_after_days: int, now: datetime
) -> str:
    """fresh / stale (verified Nd ago) / never-verified chip for a row.

    Routes through `compute_verification_status` — the same helper the
    MCP response layer uses — so a row chip can never disagree with the
    detail page or a search hit for the same memory.
    """
    if last_verified_at is None:
        return '<span class="chip bad">never verified</span>'
    status = compute_verification_status(
        last_verified_at, now=now, stale_after_days=stale_after_days
    )
    if status.status == "stale":
        return (
            f'<span class="chip warn">stale '
            f"(verified {int(status.age_days or 0)}d ago)</span>"
        )
    return '<span class="chip ok">verified</span>'


def _render_overview(report: Any, *, tombstone_count: int | None = None) -> str:
    """Dashboard summary built from a HealthReport.

    Verdict-first: the grid leads with the verification split and the
    telemetry rollups (silent misses, cold endorsements, commit drift)
    rather than celebrating zeros on the legacy rot axes — those axes
    still render, but a healthy store's overview should say what IS
    moving, not enumerate what isn't.
    """
    n = report.total_active_memories
    debt = report.verification_debt
    # Read the uncapped totals, not len() of the capped row lists.
    # compute_health slices never_verified / stale at _VERIFICATION_DEBT_CAP
    # (20) to bound the JSON; the dashboard headline must reflect the real
    # backlog, so on a store with >20 of either the count would otherwise
    # freeze at 20 and the warn cue would saturate.
    never_verified = debt.never_verified_total if debt else 0
    stale_verifications = debt.stale_total if debt else 0
    fresh = debt.fresh_count if debt else 0
    misses = report.silent_misses.miss_total if report.silent_misses else 0
    cold_endorse = (
        report.cold_endorsement_memories.total
        if report.cold_endorsement_memories
        else 0
    )
    cards: list[tuple[str, int, str]] = [
        ("active memories", n, ""),
        ("verified fresh", fresh, "ok" if fresh else ""),
        (
            "stale verifications",
            stale_verifications,
            "warn" if stale_verifications else "",
        ),
        ("never verified", never_verified, "warn" if never_verified else ""),
        ("dead weight", len(report.dead_weight), "bad" if report.dead_weight else ""),
        (
            "cold memories",
            len(report.cold_memories),
            "warn" if report.cold_memories else "",
        ),
        (
            "contradictions",
            len(report.contradicted),
            "bad" if report.contradicted else "",
        ),
        ("silent misses", misses, "warn" if misses else ""),
        ("cold endorsements", cold_endorse, "warn" if cold_endorse else ""),
    ]
    if report.commit_drift_debt is not None:
        cards.append(
            (
                "commit-drifted",
                report.commit_drift_debt.total_drifted,
                "warn" if report.commit_drift_debt.total_drifted else "",
            )
        )
    if tombstone_count is not None:
        cards.append(("tombstones", tombstone_count, ""))

    parts: list[str] = []
    parts.append('<div class="bucket-summary">')
    for label, value, cls in cards:
        parts.append(
            f'<div class="item"><span class="label">{html.escape(label)}</span>'
            f'<span class="value {cls}">{int(value)}</span></div>'
        )
    parts.append("</div>")
    parts.append(
        f'<p class="muted">{int(report.total_events)} events · '
        f"{int(report.distinct_sessions)} sessions · "
        f"window {int(report.window_days)}d</p>"
    )

    if report.heavily_used:
        parts.append("<h2>Most-applied memories</h2>")
        parts.append('<ul class="bare">')
        for stats in report.heavily_used[:10]:
            explicit = (
                f" · {stats.explicit_applied_count} explicit"
                if getattr(stats, "explicit_applied_count", 0)
                else ""
            )
            parts.append(
                f'<li><a href="/memories/{html.escape(stats.id)}">'
                f"{html.escape(stats.summary or stats.id)}</a> "
                f"{_scope_chips(stats.scopes)}"
                f'<span class="muted">applied {stats.applied_count}×'
                f"{explicit}</span></li>"
            )
        parts.append("</ul>")
    else:
        parts.append(
            '<p class="muted">No memories have crossed the heavily-used floor yet '
            "(record_use(applied) events accumulate over time).</p>"
        )

    return "".join(parts)


def _render_search_bar(query: str, scope_filter: str) -> str:
    return (
        '<form method="get" action="/memories" class="searchbar">'
        f'<input type="text" name="q" placeholder="Search — ranked by the same '
        f'engine memory_search uses…" value="{html.escape(query)}"/>'
        f'<input type="text" name="scope" placeholder="Scope filter (optional)…" '
        f'value="{html.escape(scope_filter)}"/>'
        '<button type="submit">Search</button>'
        "</form>"
    )


def _render_hits(
    hits: list[dict[str, Any]],
    *,
    query: str,
    scope_filter: str,
) -> str:
    """Ranked search results, verdict-first.

    Takes SERIALISED hits — `ResponseBuilder.hit_to_dict` output with
    `attach_commit_drift_counts` / `attach_recent_negative_outcomes`
    already applied — not raw `MemoryHit`s. That is the parity contract:
    the verdict rendered here is the string `memory_search` returns for
    the same hit in its NON-expanded form, commit-drift upgrade
    included. (`expand_top=True` additionally re-derives the top hit's
    verdict from the loaded body; no list surface loads bodies, so that
    refinement lives on the detail page instead, which does the same
    body-level work per memory.) The previous shape recomputed the
    verdict per row from verification + path drift only — the
    pre-`attach` INITIAL value — so any hit whose commit drift raised
    the verdict rendered a "fresh" chip the detail page contradicted one
    click away.

    Nothing here derives a verdict; it reads `staleness_verdict` off the
    dict and maps it to a chip.
    """
    parts: list[str] = [_render_search_bar(query, scope_filter)]
    parts.append(
        f'<p class="muted">{len(hits)} ranked hit(s) for '
        f"<strong>{html.escape(query)}</strong></p>"
    )
    if not hits:
        parts.append(
            '<p class="muted">No hits. The ranker tokenizes and strips '
            "stopwords, so try distinctive terms rather than exact phrases.</p>"
        )
        return "".join(parts)

    for hit in hits:
        hit_id = str(hit.get("id", ""))
        cat_val = hit.get("category")
        cat_chip = ""
        if cat_val is not None and str(cat_val) != "fact":
            cat_chip = f'<span class="chip warn">{html.escape(str(cat_val))}</span>'
        missing = int(hit.get("path_drift_missing") or 0)
        drift_chip = (
            f'<span class="chip bad">paths missing: {missing}</span>' if missing else ""
        )
        cd_count = hit.get("commit_drift_count")
        cd_chip = _commit_drift_chip(
            int(cd_count) if isinstance(cd_count, int) else None
        )
        neg_chips = _negative_outcome_chips(hit.get("recent_negative_outcomes"))
        matched = ", ".join(list(hit.get("match_terms") or [])[:8])
        title = (str(hit.get("snippet") or "") or hit_id).splitlines()[0][:140]
        parts.append(
            f'<div class="card">'
            f'<h3><a href="/memories/{html.escape(hit_id)}">'
            f"{html.escape(title)}</a></h3>"
            f'<div class="hit-meta">'
            f"{_verdict_chip(str(hit.get('staleness_verdict', '')))}"
            f'<span class="chip">relevance '
            f"{html.escape(str(hit.get('relevance', '')))}</span>"
            f"{cat_chip}{drift_chip}{cd_chip}{neg_chips}"
            f"{_scope_chips(hit.get('scopes') or [])}</div>"
            f'<div class="muted">score {float(hit.get("score") or 0.0):g} '
            f"· matched: {html.escape(matched) or '—'} · "
            f'<span class="mono">{html.escape(hit_id)}</span> · '
            f"updated {_date(hit.get('updated'))}</div>"
            f"</div>"
        )
    return "".join(parts)


def _render_memory_list(
    summaries: list[Any],
    *,
    query: str = "",
    scope_filter: str = "",
    stale_after_days: int,
    now: datetime,
) -> str:
    """Browse view (no query): every memory, verification-chipped."""
    parts: list[str] = [_render_search_bar(query, scope_filter)]
    parts.append(f'<p class="muted">{len(summaries)} memories</p>')

    if not summaries:
        parts.append('<p class="muted">No matching memories.</p>')
        return "".join(parts)

    for s in summaries:
        cat = getattr(s, "category", None)
        cat_chip = ""
        if cat is not None:
            cat_val = str(getattr(cat, "value", cat))
            if cat_val != "fact":
                cat_chip = f'<span class="chip warn">{html.escape(cat_val)}</span>'
        verify_chip = _verification_chip(
            getattr(s, "last_verified_at", None),
            stale_after_days=stale_after_days,
            now=now,
        )
        parts.append(
            f'<div class="card">'
            f'<h3><a href="/memories/{html.escape(s.id)}">'
            f"{html.escape(s.summary or s.id)}</a></h3>"
            f'<div class="hit-meta">{verify_chip}{cat_chip}'
            f"{_scope_chips(s.scopes)}</div>"
            f'<div class="muted"><span class="mono">{html.escape(s.id)}</span> '
            f"· created {_date(s.created)}</div>"
            f"</div>"
        )
    return "".join(parts)


def _render_memory_detail(
    memory: Any, *, stale_after_days: int, read_only: bool = False
) -> str:
    """Full body + the complete staleness verdict + verify form.

    The verdict block mirrors the `memory_show` handler exactly:
    `detect_path_drift` over the body with the verified/absent
    attestations, `compute_verification_status` for the calendar axis,
    `compute_commit_drift` (claim-anchored, only meaningful when this
    server runs inside the memory's origin repo), and
    `compute_staleness_verdict` folding all three. Same functions, same
    inputs — the page cannot disagree with what the model sees for the
    same memory. The `stale (verified Nd ago)` phrasing is a rendering
    contract pinned by tests; keep it.
    """
    now = datetime.now(timezone.utc)
    drift = detect_path_drift(
        memory.body,
        verified_paths=memory.verified_paths,
        absent_paths=memory.verified_absent_paths,
    )
    verification = compute_verification_status(
        memory.last_verified_at, now=now, stale_after_days=stale_after_days
    )
    commit_drift = compute_commit_drift(
        memory.last_verified_at,
        memory.origin.repo if memory.origin else None,
        caller_origin=capture_origin(),
        verified_paths=memory.verified_paths,
        body=memory.body,
    )
    verdict = compute_staleness_verdict(
        verification=verification,
        path_drift_missing=len(drift.missing),
        commit_drift_count=(
            commit_drift.commits_since_verify if commit_drift is not None else None
        ),
    )

    verified_str = (
        html.escape(memory.last_verified_at.isoformat())
        if memory.last_verified_at is not None
        else "never"
    )
    stale_tag = ""
    if verification.status == "stale":
        stale_tag = (
            f' <span class="chip warn">stale '
            f"(verified {int(verification.age_days or 0)}d ago)</span>"
        )
    cd_chip = _commit_drift_chip(
        commit_drift.commits_since_verify if commit_drift is not None else None
    )

    def _path_list(title: str, paths: Any, note: str = "") -> str:
        if not paths:
            return ""
        items = "".join(f"<li><code>{html.escape(p)}</code></li>" for p in paths)
        note_html = f'<p class="muted">{html.escape(note)}</p>' if note else ""
        return f"<h2>{html.escape(title)}</h2>{note_html}<ul>{items}</ul>"

    drift_sections = (
        _path_list("Missing paths", drift.missing)
        + _path_list("Verified paths (attested)", drift.verified)
        + _path_list("Expected-absent paths", drift.expected_absent)
        + _path_list(
            "Route-suppressed candidates",
            drift.dropped_as_route,
            note=(
                "Path-shaped strings the drift check dropped as probable "
                "application routes rather than filesystem paths — "
                "suppressed, not verified."
            ),
        )
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

    verify_section = (
        ""
        if read_only
        else (
            f"<h2>Verify</h2>"
            f'<p class="muted">Spot-checked a claim against reality? Record it. '
            f"Attested paths/commits need the CLI or memory_verify.</p>"
            f'<form method="post" action="/memories/{html.escape(memory.id)}/verify">'
            f'<input type="text" name="note" placeholder="Optional note (what you checked)"/>'
            f'<button type="submit">Mark verified now</button>'
            f"</form>"
        )
    )
    return (
        f'<div class="card">'
        f'<div class="hit-meta">{_verdict_chip(verdict)}{cd_chip}'
        f"{_scope_chips(memory.scopes)}</div>"
        f'<div class="muted"><span class="mono">{html.escape(memory.id)}</span> · '
        f"created {html.escape(memory.created.isoformat())} · updated "
        f"{html.escape(memory.updated.isoformat())} · verified {verified_str}"
        f"{stale_tag} · paths checked: {len(drift.checked)}"
        f"</div>"
        f"<h2>Body</h2>"
        f"<pre>{body_html}</pre>"
        f"{drift_sections}"
        f"{links_section}"
        f"{verify_section}"
        f"</div>"
    )


# Which `HealthReport.to_dict()` keys the /health page actually puts on
# screen (counting the `_render_overview` block it embeds), and which it
# deliberately leaves off. The two sets partition the report's key set;
# `test_web.py` pins that, so a bucket added to `HealthReport` cannot
# land unrendered and unnoticed — it fails the test until someone either
# renders it or files it under the disclaimed set with a reason.
_HEALTH_RENDERED_BUCKETS = frozenset(
    {
        "window_days",
        "total_active_memories",
        "total_events",
        "distinct_sessions",
        "dead_weight",
        "cold_memories",
        "heavily_used",
        "contradicted",
        "scope_health",
        "rare_scopes",
        "orphan_use_events",
        "verification_debt",
        "commit_drift_debt",
        "silent_misses",
        "recent_silent_misses",
        "cold_endorsement_memories",
        "recommendations",
    }
)

# Deliberately not rendered — each with the reason, because "we show
# everything" was the claim this page could not keep.
_HEALTH_DISCLAIMED_BUCKETS = frozenset(
    {
        # The page is generated per request; the header already says
        # which store, and "now" is the only possible value.
        "generated_at",
        # Transient-marker fire/override rates: a write-path diagnostic
        # (is the durability heuristic calibrated?), not a curation
        # action a human takes from this page. `bettermemory health
        # --json` carries it.
        "marker_stats",
        # Subsumed by the richer `scope_health` table below, which
        # carries the same per-scope active count plus the rot columns.
        "scope_distribution",
    }
)


def _render_health(report: Any) -> str:
    """The memory_health rollup, rendered for a curation pass.

    Not every bucket: `_HEALTH_RENDERED_BUCKETS` is what reaches the
    page and `_HEALTH_DISCLAIMED_BUCKETS` is what doesn't (with the
    reason on each). The two partition `HealthReport.to_dict()`'s keys
    and a test enforces that, so a future bucket can't quietly go
    missing here. For the full machine shape use `memory_health` or
    `bettermemory health --json`.
    """
    parts: list[str] = []
    parts.append(_render_overview(report))

    orphans = int(getattr(report, "orphan_use_events", 0) or 0)
    if orphans:
        # Warn row only when it fired — a zero here is the healthy
        # default and a permanent "0 orphans" line would be noise. The
        # bucket is the fabrication smoke test: record_use against ids
        # that resolve to neither an active nor a tombstoned memory.
        parts.append(
            f'<div class="card"><div class="hit-meta">'
            f'<span class="chip bad">{orphans} orphan use event(s)</span></div>'
            f'<p class="muted">record_use calls naming ids that exist neither '
            f"as active memories nor as tombstones. A few can be a rotated "
            f"log or a hard-deleted file; a growing count usually means "
            f"fabricated ULIDs.</p></div>"
        )

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
                f'<span class="chip bad">contradicted</span></li>'
            )
        parts.append("</ul>")

    cold_endorse = report.cold_endorsement_memories
    if cold_endorse and cold_endorse.rows:
        parts.append("<h2>Cold endorsements</h2>")
        parts.append(
            f'<p class="muted">Crossed the retrieval floor '
            f"({cold_endorse.min_retrievals}+ hits) without a single "
            f"explicit apply — over-surfaced by the ranker, or worth a "
            f"deliberate endorsement.</p>"
        )
        parts.append('<ul class="bare">')
        for stats in cold_endorse.rows[:20]:
            parts.append(
                f'<li><a href="/memories/{html.escape(stats.id)}">'
                f"{html.escape(stats.summary or stats.id)}</a> "
                f'<span class="muted">retrieved {stats.retrieval_count}× · '
                f"0 explicit</span></li>"
            )
        parts.append("</ul>")

    cd = report.commit_drift_debt
    if cd is not None and cd.rows:
        parts.append("<h2>Commit drift</h2>")
        parts.append(
            f'<p class="muted">Verified before commits landed on '
            f"<code>{html.escape(cd.current_repo or '?')}</code> — the claims' "
            f"ground truth moved. {cd.total_drifted} total.</p>"
        )
        parts.append('<ul class="bare">')
        for row in cd.rows[:20]:
            parts.append(
                f'<li><a href="/memories/{html.escape(row.id)}">'
                f"{html.escape(row.summary or row.id)}</a> "
                f'<span class="chip warn">{row.commits_since_verify} commits '
                f"since verify</span></li>"
            )
        parts.append("</ul>")

    if report.recent_silent_misses:
        parts.append("<h2>Recent silent misses</h2>")
        parts.append(
            '<p class="muted">Turns where retrieval should probably have '
            "fired but didn't. Triage: real miss (lesson) or false positive "
            "(acknowledge via memory_acknowledge_miss).</p>"
        )
        parts.append('<ul class="bare">')
        for miss in report.recent_silent_misses[:10]:
            top = (
                f' → <a href="/memories/{html.escape(miss.top_hit_id)}" '
                f'class="mono">{html.escape(miss.top_hit_id[:10])}…</a>'
                if miss.top_hit_id
                else ""
            )
            parts.append(
                f"<li>“{html.escape(miss.query_preview or '')}”"
                f'{top} <span class="muted">{html.escape(str(miss.ts)[:10])}'
                f"</span></li>"
            )
        parts.append("</ul>")

    if report.scope_health:
        parts.append("<h2>Scope health</h2>")
        parts.append(
            "<table><tr><th>scope</th><th class='num'>active</th>"
            "<th class='num'>dead</th><th class='num'>cold</th>"
            "<th class='num'>contradicted</th><th class='num'>applied</th></tr>"
        )
        for sh in report.scope_health:
            parts.append(
                f"<tr><td><code>{html.escape(sh.scope)}</code></td>"
                f'<td class="num">{sh.active}</td>'
                f'<td class="num">{sh.dead}</td>'
                f'<td class="num">{sh.cold}</td>'
                f'<td class="num">{sh.contradicted}</td>'
                f'<td class="num">{sh.applied_total}</td></tr>'
            )
        parts.append("</table>")

    if report.rare_scopes:
        parts.append("<h2>Rare scopes (possible typos)</h2>")
        items = "".join(
            f"<li><code>{html.escape(s)}</code></li>" for s in report.rare_scopes
        )
        parts.append(f"<ul>{items}</ul>")

    if report.recommendations:
        parts.append("<h2>Recommendations</h2>")
        for rec in report.recommendations:
            parts.append(
                f'<div class="card"><strong>{html.escape(rec.kind)}</strong>'
                f"<p>{html.escape(rec.summary)}</p>"
                f'<p class="muted">{html.escape(rec.action)}</p></div>'
            )

    return "".join(parts)


def _fmt_rate(rate: Any) -> str:
    """One RateCI as `0.07 [0.06, 0.09] · 91/1282` (or n/a on zero-denominator).

    A `torn_read` rate carries the clamp warning `eval.render_text`
    prints, mirrored to this cell: `RateCI.from_counts` clamps the
    displayed value to 1.0 when the numerator exceeds the denominator,
    and a bare "1.00" with a numerator larger than its denominator next
    to it reads as a corrupt table rather than the measurement artifact
    it usually is.
    """
    if rate is None or rate.rate is None:
        n = getattr(rate, "numerator", 0) if rate is not None else 0
        d = getattr(rate, "denominator", 0) if rate is not None else 0
        return f"n/a · {n}/{d}"
    ci = (
        f" [{rate.lower:.2f}, {rate.upper:.2f}]"
        if rate.lower is not None and rate.upper is not None
        else ""
    )
    torn = (
        " · clamped to 1.0 (numerator > denominator — usually a windowing "
        "artifact, where a use event is in-window while its retrieval aged "
        "out; less often a log read mid-rotation)"
        if getattr(rate, "torn_read", False)
        else ""
    )
    return f"{rate.rate:.2f}{ci} · {rate.numerator}/{rate.denominator}{torn}"


def _render_eval(report: Any, *, window_days: int) -> str:
    """The effectiveness telemetry, same numbers `eval --report` publishes.

    Read-only render of `compute_eval` over the live event log. The
    three rates are floors by design — the numerator counts only
    explicit, claim-excerpt-backed endorsements — and the page says so
    rather than dressing them up.
    """
    parts: list[str] = []
    parts.append(
        '<p class="muted">Floors, not estimates: the numerators count only '
        "explicit, attested signals. Method: docs/eval.md · publishable "
        "document: <code>bettermemory eval --report</code>.</p>"
    )
    parts.append("<table><tr><th>rate</th><th>value · 95% CI · n/d</th></tr>")
    for label, rate in (
        ("memory_helped_rate", report.memory_helped_rate),
        ("endorsement_rate", report.endorsement_rate),
        ("silent_miss_rate", report.silent_miss_rate),
    ):
        parts.append(
            f"<tr><td><code>{html.escape(label)}</code></td>"
            f"<td>{html.escape(_fmt_rate(rate))}</td></tr>"
        )
    parts.append("</table>")
    parts.append(
        f'<p class="muted">Window: last {window_days}d · '
        f"{int(report.events_in_window)} events · "
        f"{int(report.turns_audited)} turns audited · "
        f"{int(report.turns_no_signal)} no-signal audits excluded</p>"
    )

    if report.by_model:
        cols = sorted({k for v in report.by_model.values() for k in v})
        parts.append("<h2>By model</h2>")
        parts.append(
            "<table><tr><th>model</th>"
            + "".join(f"<th class='num'>{html.escape(c)}</th>" for c in cols)
            + "</tr>"
        )
        for model in sorted(report.by_model):
            row = report.by_model[model]
            parts.append(
                f"<tr><td><code>{html.escape(model)}</code></td>"
                + "".join(f'<td class="num">{row.get(c, 0)}</td>' for c in cols)
                + "</tr>"
            )
        parts.append("</table>")

    parts.append(
        '<p class="muted">Recent silent misses are triaged on the '
        '<a href="/health">Health</a> page.</p>'
    )
    return "".join(parts)


def _render_episodes(sessions: list[tuple[str, list[Any]]]) -> str:
    """Session journals, newest first — the run-state tier memories
    deliberately exclude. Read-only; episodes self-prune on TTL."""
    if not sessions:
        return (
            '<p class="muted">No episodes. Sessions journal here via '
            "episode_write; entries expire on a 30-day TTL.</p>"
        )
    parts: list[str] = [
        f'<p class="muted">{len(sessions)} session(s) with episodes '
        "(newest first, capped at 15)</p>"
    ]
    for session_id, episodes in sessions:
        newest = episodes[0].created if episodes else None
        parts.append(
            f'<div class="card"><h3><span class="mono">'
            f"{html.escape(session_id)}</span></h3>"
            f'<div class="muted">{len(episodes)} episode(s)'
            f"{' · newest ' + _date(newest) if newest else ''}</div>"
        )
        for ep in episodes[:5]:
            takeaway = ep.takeaway or (ep.body or "").splitlines()[0][:160]
            parts.append(
                f"<details><summary>{_date(ep.created)} — "
                f"{html.escape(takeaway[:180])}</summary>"
                f"<pre>{html.escape(ep.body or '')}</pre></details>"
            )
        if len(episodes) > 5:
            parts.append(
                f'<p class="muted">… and {len(episodes) - 5} more in this session</p>'
            )
        parts.append("</div>")
    return "".join(parts)


def _render_curation(report: Any) -> str:
    """The consolidate engine's dry-run preview — what a curation pass
    WOULD do. Strictly read-only: applying happens in-conversation
    (memory_curate) or via `bettermemory consolidate`, never from the
    browser."""
    parts: list[str] = [
        '<p class="muted">Preview only — nothing on this page mutates the '
        "store. Apply via <code>memory_curate</code> in conversation or "
        "<code>bettermemory consolidate --yes</code>.</p>"
    ]
    any_content = False

    if report.dedup_candidates:
        any_content = True
        parts.append("<h2>Near-duplicates</h2>")
        for c in report.dedup_candidates:
            parts.append(
                f'<div class="card">'
                f'<div class="hit-meta"><span class="chip warn">'
                f"{c.similarity:.0%} similar</span>"
                f'<span class="chip">{html.escape(c.method)}</span></div>'
                f'<p>keep <a href="/memories/{html.escape(c.keeper_id)}">'
                f"{html.escape(c.keeper_summary or c.keeper_id)}</a><br/>"
                f'tombstone <a href="/memories/{html.escape(c.duplicate_id)}">'
                f"{html.escape(c.duplicate_summary or c.duplicate_id)}</a></p>"
                f"</div>"
            )

    if report.polarity_skipped:
        # The pairs the dedup guard kept OUT of `dedup_candidates`:
        # similar enough to merge, but the bodies disagree (opposite
        # negation polarity, or diverging numbers). Conflict-shaped, not
        # duplicate-shaped — so it gets its own section rather than
        # riding along under Near-duplicates, and points at the
        # arbitration tool instead of a merge.
        any_content = True
        parts.append("<h2>Polarity-skipped pairs</h2>")
        parts.append(
            '<p class="muted">Above the dedup threshold but the two bodies '
            "disagree — kept out of the merge list on purpose (the apply "
            "path never touches these). Arbitrate via "
            "<code>memory_conflicts</code>; wave through the benign ones "
            "(an incidental negator, an added-detail number).</p>"
        )
        for ps in report.polarity_skipped:
            parts.append(
                f'<div class="card">'
                f'<div class="hit-meta"><span class="chip warn">'
                f"{ps.similarity:.0%} similar</span>"
                f'<span class="chip">{html.escape(ps.method)}</span>'
                f'<span class="chip bad">{html.escape(ps.detector)}</span></div>'
                f'<p><a href="/memories/{html.escape(ps.memory_id_a)}">'
                f"{html.escape(ps.summary_a or ps.memory_id_a)}</a><br/>"
                f'<a href="/memories/{html.escape(ps.memory_id_b)}">'
                f"{html.escape(ps.summary_b or ps.memory_id_b)}</a></p>"
                f"</div>"
            )

    if report.demotion_candidates:
        any_content = True
        parts.append("<h2>Demotion candidates</h2>")
        parts.append('<ul class="bare">')
        for d in report.demotion_candidates:
            parts.append(
                f'<li><span class="chip warn">{html.escape(d.kind)}</span> '
                f'<a href="/memories/{html.escape(d.memory_id)}" class="mono">'
                f"{html.escape(d.memory_id)}</a> "
                f'<span class="muted">{html.escape(d.reason)}</span></li>'
            )
        parts.append("</ul>")

    if report.cold_scope_suggestions:
        any_content = True
        parts.append("<h2>Cold scopes (suggest-only)</h2>")
        items = "".join(
            f"<li><code>{html.escape(str(s))}</code></li>"
            for s in report.cold_scope_suggestions
        )
        parts.append(f"<ul>{items}</ul>")

    if report.scope_typo_pairs:
        any_content = True
        parts.append("<h2>Scope typo pairs (suggest-only)</h2>")
        items = "".join(
            f"<li><code>{html.escape(str(p))}</code></li>"
            for p in report.scope_typo_pairs
        )
        parts.append(f"<ul>{items}</ul>")

    if not any_content:
        parts.append(
            '<p class="muted">Nothing to curate — no near-duplicates, '
            "polarity-skipped pairs, demotion candidates, cold scopes, or "
            "typo pairs in the window.</p>"
        )
    return "".join(parts)


def _render_tombstones(tombstones: list[Any]) -> str:
    if not tombstones:
        return '<p class="muted">No tombstones.</p>'
    parts: list[str] = [f'<p class="muted">{len(tombstones)} tombstoned memories</p>']
    for t in tombstones:
        parts.append(
            f'<div class="card">'
            f"<h3>{html.escape(t.summary or t.id)}</h3>"
            f'<div class="muted"><span class="mono">{html.escape(t.id)}</span> '
            f"· removed {html.escape(t.removed.isoformat())}</div>"
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

    def _layout_resp(title: str, body: str, active: str = "") -> HTMLResponse:
        return HTMLResponse(
            _layout(
                title,
                body,
                store.root,
                csrf_token,
                read_only=read_only,
                active=active,
            )
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

    def _health_report() -> Any:
        """The one `report_for_directory` call shape both health-bearing
        routes use, threading every config knob the `memory_health`
        handler threads. Centralised so the overview and the health page
        cannot end up computing different buckets from the same store —
        `cold_endorsement_ratio_threshold` was previously dropped on
        both, silently reverting the bucket to its strict
        `explicit == 0` semantics for the web only.
        """
        return report_for_directory(
            store.root,
            window_days=30,
            heavily_used_top_k=10,
            heavily_used_min_applied=config.behavior.heavily_used_min_applied,
            verification_stale_days=config.behavior.verification_stale_days,
            cold_endorsement_ratio_threshold=(
                config.behavior.cold_endorsement_ratio_threshold
            ),
            caller_origin=capture_origin(),
        )

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        report = _health_report()
        return _layout_resp(
            "Overview",
            _render_overview(report, tombstone_count=len(store.list_tombstones())),
            active="/",
        )

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
        now = datetime.now(timezone.utc)
        stale_days = config.behavior.verification_stale_days
        if q.strip():
            # The whole point of the 2026-07 overhaul: the web search IS
            # the product's search. Same ranker, same tokenizer, same
            # relevance labels as memory_search — a UI that ships a naive
            # substring filter next to a tuned BM25 engine misrepresents
            # the product on its own pitch. The full corpus is loaded
            # (no FTS prefilter), so pool statistics are corpus
            # statistics and no corpus_stats_provider is needed — the
            # same reasoning the MCP handler applies on its load_all
            # branch. `semantic` degrades to `hybrid` here: the web
            # process never loads an embedding model, and hybrid without
            # a model is exactly the ranking that leaves.
            mode_raw = config.behavior.search_mode
            if mode_raw not in ("keyword", "bm25", "hybrid", "semantic"):
                mode_raw = "hybrid"
            if mode_raw == "semantic":
                mode_raw = "hybrid"
            memories_pool = store.load_all()
            # The `[behavior]` ranking inputs, resolved through the same
            # helper `handlers.search.memory_search` calls — flipping
            # `endorsement_boost` / `outcome_demotion` /
            # `corroboration_boost` or retuning the recency half-life
            # now moves both surfaces or neither.
            ranking = resolve_ranking_inputs(store.root, memories_pool, config.behavior)
            hits = run_search(
                memories_pool,
                q,
                scopes=[scope] if scope else None,
                max_results=_SEARCH_MAX_RESULTS,
                mode=cast(SearchMode, mode_raw),
                applied_by_id=ranking.applied_by_id,
                negative_by_id=ranking.negative_by_id,
                corroboration_boost=ranking.corroboration_boost,
                half_life_days=ranking.half_life_days,
            )
            # Serialise through the MCP response pipeline rather than
            # re-deriving anything: `hit_to_dict` sets the initial
            # verdict, `attach_commit_drift_counts` folds in the
            # commit-drift upgrade (the step the web used to skip, so a
            # drifted-but-calendar-fresh hit rendered a "fresh" chip its
            # own detail page contradicted), and
            # `attach_recent_negative_outcomes` annotates the rejections
            # the model is told about.
            builder = ResponseBuilder(stale_after_days=stale_days)
            hit_dicts = [builder.hit_to_dict(h, now=now) for h in hits]
            builder.attach_commit_drift_counts(
                hit_dicts, hits, memories_pool, caller_origin=capture_origin()
            )
            if hit_dicts:
                events = ranking.events
                if events is None:
                    # Neither ranking tally ran, so no event read has
                    # been paid yet — take the annotation's own window,
                    # exactly as the handler does on this branch.
                    from .audit import ATTRIBUTION_LOOKBACK_SECONDS
                    from .events import iter_events_window

                    events = list(
                        iter_events_window(store.root, ATTRIBUTION_LOOKBACK_SECONDS)
                    )
                builder.attach_recent_negative_outcomes(
                    hit_dicts, hits, events, now=now
                )
            return _layout_resp(
                "Memories",
                _render_hits(hit_dicts, query=q, scope_filter=scope),
                active="/memories",
            )
        summaries = store.list_summaries(scopes=[scope] if scope else None)
        return _layout_resp(
            "Memories",
            _render_memory_list(
                summaries,
                query=q,
                scope_filter=scope,
                stale_after_days=stale_days,
                now=now,
            ),
            active="/memories",
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
            active="/memories",
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
        return _layout_resp(
            "Health", _render_health(_health_report()), active="/health"
        )

    @app.get("/curation", response_class=HTMLResponse)
    def curation() -> HTMLResponse:
        # Dry-run ONLY, by construction: `apply` is a literal False at
        # the one call site the web surface has, so no route can be
        # coaxed into mutating. The preview also records no event —
        # mirrors the memory_curate handler's dry-run contract.
        report = consolidate(
            store,
            window_days=30,
            apply=False,
            session_id="web-ui",
        )
        return _layout_resp(
            "Curation preview", _render_curation(report), active="/curation"
        )

    @app.get("/eval", response_class=HTMLResponse)
    def eval_page() -> HTMLResponse:
        window_days = 30
        report = compute_eval(
            store.load_all(),
            iter_all_events(store.root),
            since=timedelta(days=window_days),
            tombstoned_ids={t.id for t in store.list_tombstones()},
        )
        return _layout_resp(
            "Eval",
            _render_eval(report, window_days=window_days),
            active="/eval",
        )

    @app.get("/episodes", response_class=HTMLResponse)
    def episodes() -> HTMLResponse:
        estore = EpisodeStore(store.root)
        sessions: list[tuple[str, list[Any]]] = []
        for sid in estore.iter_session_ids():
            eps = estore.list_by_session(sid)
            if eps:
                eps = sorted(eps, key=lambda e: e.created, reverse=True)
                sessions.append((sid, eps))
        # Newest session first, judged by its newest episode; cap the
        # page at 15 sessions — the journal is a tail, not an archive.
        sessions.sort(key=lambda pair: pair[1][0].created, reverse=True)
        return _layout_resp(
            "Episodes", _render_episodes(sessions[:15]), active="/episodes"
        )

    @app.get("/tombstones", response_class=HTMLResponse)
    def tombstones() -> HTMLResponse:
        tombs = store.list_tombstones()
        return _layout_resp(
            "Tombstones", _render_tombstones(tombs), active="/tombstones"
        )

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
