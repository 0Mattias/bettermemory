"""Drift tests for the model-facing policy surfaces.

Three surfaces carry the policy the model reads:

1. `SYSTEM_PROMPT_ADDENDUM` (in `prompts.py`) — programmatic embedding.
2. The fenced block in `docs/system_prompt.md` — copy-paste for humans.
3. `plugin/skills/bettermemory/SKILL.md` — loaded when the plugin's skill
   activates.

(1) and (2) are byte-equal by design — both are the same advanced-tightening
addendum, exposed two ways. (3) is the policy-as-companion surface for plugin
users; intentionally shorter and policy-focused, NOT a full tool inventory.

Failure modes guarded here:

- **Doc/code drift** between (1) and (2). Verbatim string comparison.

- **Tool-name drift** on (1) and (3): someone renames or removes an MCP
  tool but forgets to update the policy surface. Future clients then get
  told to call a tool the server doesn't expose. One-way parity tests
  against `build_server`'s registered tool names cover this — for each
  surface, every `memory_*` name it mentions must resolve to a real tool
  on the server. The reverse direction is intentionally NOT enforced
  (the skill is policy, not inventory; not every server tool needs to
  appear there).

- **Surface drift** on the same two: the parity tests run against BOTH
  values of `[behavior] full_tool_surface`, because the two defaults
  disagree. `BehaviorConfig` defaults it True; `load_config()` — the only
  path the `bettermemory` entry point takes — coerces an unset key to
  False. The lean surface is therefore what ships, and what a plugin
  install gets (`plugin/.mcp.json` runs `uvx bettermemory` with an empty
  env). On the lean leg a referenced tool must either resolve on the
  server or be declared full-surface in the prose, following the marker
  convention SKILL.md already uses ("Two full-surface tools drain what no
  single conversation can see:").
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from bettermemory.config import BehaviorConfig, Config, StorageConfig
from bettermemory.prompts import SYSTEM_PROMPT_ADDENDUM
from bettermemory.server import build_server
from bettermemory.session import SessionState
from bettermemory.store import Store

# The tracked-file corpus lives in one place on purpose: a second
# `git ls-files` routine would be a second chance for the two to diverge,
# which is the failure `test_doc_claims` already paid for once.
from .test_doc_claims import _git_tracked_files


# Match the first ```...``` fence in the doc — the addendum is the first one.
_DOC_FENCE_RE = re.compile(r"```\n(.*?)```", re.DOTALL)

# Match any identifier of the form `memory_*` or `episode_*` appearing
# in the addendum that is NOT immediately followed by `=` (which would
# mark it as a keyword-argument name like `memory_ids=[...]`, not a
# tool reference). The addendum uses tool names in the explicit
# "Available tools:" list and in call shapes ("call
# memory_write_confirm(...)"); both populations need to map to real
# tools on the server. The episode_* family was added when the loop
# story shipped — keep the regex covering both so a future rename of
# either family catches the same parity check.
_TOOL_REF_RE = re.compile(r"\b((?:memory|episode)_[a-z_]+)\b(?!\s*=)")

# Tool-SHAPED identifiers `_TOOL_REF_RE` over-includes: `memory_ids` and
# `episode_id` are parameters on `memory_record_use` / `episode_promote`,
# and `episode_volume` is a key on `memory_health`'s return shape. None
# of them is a tool. Shared by both parity guards below so the two can't
# drift apart.
KNOWN_NON_TOOL_IDENTIFIERS = {"memory_ids", "episode_id", "episode_volume"}

# Prose marker declaring a tool available only under `[behavior]
# full_tool_surface`. SKILL.md's "Two full-surface tools drain what no
# single conversation can see:" is the original instance; the addendum's
# `Tools:` headline follows the same convention with "Full-surface only:".
_FULL_SURFACE_MARKER_RE = re.compile(r"full[-_ ]surface|full_tool_surface", re.I)

# Split a block into sentences. Coarse, but the policy surfaces don't use
# abbreviations mid-sentence, and a false split can only ever make the
# marker cover LESS text — it cannot smuggle an unmarked name in.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")

_BULLET_RE = re.compile(r"^\s*[-*]\s+")


def _tool_refs(text: str) -> set[str]:
    """Tool names referenced in `text`, minus the known non-tools."""
    return {
        name
        for name in _TOOL_REF_RE.findall(text)
        if not name.endswith("_") and name not in KNOWN_NON_TOOL_IDENTIFIERS
    }


def _full_surface_marked_names(text: str) -> set[str]:
    """Tool names the document declares as `full_tool_surface`-only.

    Two shapes count, both drawn from prose that already exists rather
    than invented here:

    - A sentence carrying the marker declares every tool name in that
      sentence. The addendum's "Full-surface only: memory_health, …"
      clause is this shape.
    - A marker-carrying lead-in that ends in a colon declares the SUBJECT
      of each bullet in the block that follows — the first tool name on
      the bullet line. SKILL.md's "Two full-surface tools drain what no
      single conversation can see:" is this shape, and taking only the
      subject matters: those bullets also mention `memory_verify`,
      `memory_update` and `memory_write` as the follow-up actions, and
      those are lean tools that must not be swept into the declaration.

    Declaration is per-document, not per-mention. Once a doc has told the
    reader a tool needs `full_tool_surface = true`, later prose may use
    the bare name — these files are read start to finish, and demanding
    the caveat at every mention would bloat a paste-in whose only real
    constraint is length. The failure this closes is a doc that names a
    lean-absent tool with no such signal anywhere, which is what both
    surfaces did before: a plugin install runs `uvx bettermemory` with an
    empty env, `load_config()` defaults `full_tool_surface` to false, and
    the shipped prose named nine tools that install never registers.
    """
    marked: set[str] = set()
    carry = False
    for block in re.split(r"\n\s*\n", text):
        has_marker = bool(_FULL_SURFACE_MARKER_RE.search(block))
        if carry:
            for line in block.splitlines():
                if not _BULLET_RE.match(line):
                    continue
                subjects = _tool_refs(line)
                first = _TOOL_REF_RE.search(line)
                if first is not None and first.group(1) in subjects:
                    marked.add(first.group(1))
        if has_marker:
            for sentence in _SENTENCE_SPLIT_RE.split(block):
                if _FULL_SURFACE_MARKER_RE.search(sentence):
                    marked.update(_tool_refs(sentence))
        carry = has_marker and block.rstrip().endswith(":")
    return marked


async def _registered_tool_names(
    tmp_path: Path, *, full_tool_surface: bool
) -> set[str]:
    """Tool names a server registers under the given surface setting.

    Hermetic build: tmp_path-backed store and a fresh SessionState so the
    module-level singleton from `get_state()` isn't shared with other
    tests. The list_tools call doesn't write anything to disk.
    """
    cfg = Config(
        storage=StorageConfig(directory=str(tmp_path)),
        behavior=BehaviorConfig(full_tool_surface=full_tool_surface),
    )
    mcp = build_server(config=cfg, store=Store(tmp_path), state=SessionState())
    return {tool.name for tool in await mcp.list_tools()}


def _assert_surface_parity(
    *,
    surface_name: str,
    text: str,
    registered: set[str],
    full_tool_surface: bool,
) -> None:
    """Shared body of the two parity guards.

    On the full leg every referenced tool must be registered outright. On
    the lean leg a referenced tool may instead be declared full-surface
    in the prose. Both legs assert the check RAN rather than merely that
    it passed: the lean leg pins that the surface actually shrank, that
    the document actually references a lean-absent tool (so the marker
    path is exercised), and that the marker admitted at least one of
    them. Without those, a marker regex that matched nothing — or a lean
    server that quietly registered everything — would sail through green.
    """
    referenced = _tool_refs(text)
    assert referenced, f"{surface_name} references no tools at all; regex broke."

    if full_tool_surface:
        missing = referenced - registered
        assert not missing, (
            f"{surface_name} references tools that aren't registered on the "
            f"server: {sorted(missing)}. Either rename the tool back, "
            f"register the new tool, or update {surface_name} to match."
        )
        return

    assert "memory_health" not in registered, (
        "the lean leg registered memory_health, so `full_tool_surface="
        "False` did not reach the builder and this leg is testing nothing."
    )

    marked = _full_surface_marked_names(text)
    assert not marked & registered, (
        f"{surface_name} marks tools as full-surface that the LEAN server "
        f"registers anyway: {sorted(marked & registered)}. Either the "
        f"marker is spilling past its sentence or the caveat is wrong."
    )

    lean_absent = referenced - registered
    assert lean_absent, (
        f"{surface_name} names no lean-absent tool, so the marker path is "
        "never exercised. If that is genuinely true now, delete this leg "
        "rather than leaving a guard that asserts nothing."
    )
    assert lean_absent & marked, (
        f"{surface_name} names lean-absent tools {sorted(lean_absent)} but "
        "the full-surface marker admitted none of them — the marker "
        "convention is not being detected."
    )

    missing = lean_absent - marked
    assert not missing, (
        f"{surface_name} references tools the LEAN server (what "
        f"`load_config()` builds by default, and what a plugin install "
        f"gets) does not register: {sorted(missing)}. Either register "
        f"them, or mark them full-surface the way SKILL.md's "
        f'"Two full-surface tools…" lead-in does.'
    )


def test_addendum_matches_docs() -> None:
    doc_path = Path(__file__).resolve().parents[1] / "docs" / "system_prompt.md"
    text = doc_path.read_text(encoding="utf-8")
    matches = _DOC_FENCE_RE.findall(text)
    assert matches, f"no fenced code block in {doc_path}"

    canonical = matches[0].strip()
    expected = SYSTEM_PROMPT_ADDENDUM.strip()
    assert canonical == expected, (
        "SYSTEM_PROMPT_ADDENDUM in prompts.py has drifted from "
        "docs/system_prompt.md. Update both in sync."
    )


def test_addendum_tools_headline_enumerates_episode_family() -> None:
    """The single-line "Tools:" headline names every episode_* tool.

    The headline is the model's only place to learn — without calling
    `list_tools` — that the episode_* sibling family exists alongside
    memory_*. Earlier revisions enumerated only `memory_*`, leaving a
    model that paste-loaded the addendum unaware of the loop-iteration
    surface. Pin each name explicitly so a future trim can't silently
    drop one.
    """
    assert "episode_write" in SYSTEM_PROMPT_ADDENDUM
    assert "episode_handoff" in SYSTEM_PROMPT_ADDENDUM
    assert "episode_search" in SYSTEM_PROMPT_ADDENDUM
    assert "episode_promote" in SYSTEM_PROMPT_ADDENDUM


def test_api_md_documents_loop_phase_surface() -> None:
    """`docs/api.md` documents the feature/loops-phase-1 additions.

    Four contract additions landed across feature/loops-phase-1 that
    callers (Claude Code clients + the model itself) read from
    `docs/api.md`. Out-of-sync docs ship as user-visible bugs — the
    model can't use a feature it doesn't know exists. Pin a short
    text-presence check for each addition so a future doc trim trips
    one assertion rather than silently regressing the contract:

    - `since_prior_session` param on `memory_search`
    - `recently_removed_in_worktree` + `curation_pending_new_since_last_session`
      on `memory_scope_overview`
    - inline `curation_hint` on `memory_write`
    - `depends_on_resolved` on search hits
    - `recommendations` on the `memory_health` rollup
    """
    api_md = Path(__file__).resolve().parents[1] / "docs" / "api.md"
    text = api_md.read_text(encoding="utf-8")
    # memory_search since_prior_session — signature + bullet
    assert "since_prior_session" in text, (
        "docs/api.md missing the since_prior_session parameter; "
        "memory_search signature is out of sync with the handler."
    )
    # memory_scope_overview new fields
    assert "recently_removed_in_worktree" in text
    assert "curation_pending_new_since_last_session" in text
    # memory_write inline curation_hint
    assert "curation_hint" in text
    # memory_search hits — depends_on_resolved
    assert "depends_on_resolved" in text
    # memory_health rollup — recommendations
    assert "recommendations" in text


def test_api_md_documents_handler_return_shapes() -> None:
    """`docs/api.md` enumerates the return-shape fields handlers emit.

    Audit-2 (A2-12 / A2-13 / A2-14 / A2-15) flagged that several return
    surfaces were under-specified in api.md: `episode_search` didn't
    name its fields (notably `session_id`), `memory_show` enumerated
    only a subset of its actual 18-field dict, `memory_health` omitted
    `generated_at` + `window_days` from the rollup, and the
    `episode_handoff` auto-resolution branch didn't document the
    caller-worktree + disabled_scopes filters that tick-22 / tick-11
    established. Pin each as a text-presence check so a future doc
    trim trips a single assertion rather than silently regressing the
    contract surface a caller relies on.
    """
    api_md = Path(__file__).resolve().parents[1] / "docs" / "api.md"
    text = api_md.read_text(encoding="utf-8")
    # A2-12: episode_search return shape includes session_id (cross-session
    # surface) and documents the "most-recent N" cap direction (tick-21).
    assert "session_id" in text, (
        "docs/api.md no longer mentions `session_id` in any return "
        "shape; episode_search emits it (cross-session lookup) but "
        "callers can't discover the field if it's not documented."
    )
    assert "most-recent" in text, (
        "docs/api.md no longer documents the most-recent-N cap "
        "direction for episode_search; tick-21 fixed the slice but "
        "callers reading the doc still won't know which end of the "
        "list survives the cap."
    )
    # A2-13: memory_show enumerates the structured attestation fields
    # (verified_paths / verified_commits / verified_versions) the
    # handler returns from show.py:142-144.
    assert "verified_paths" in text, (
        "docs/api.md no longer enumerates `verified_paths` in the "
        "memory_show return; the handler returns it (show.py) but "
        "the documented shape stops short of the attestation block."
    )
    assert "verified_commits" in text
    assert "verified_versions" in text
    # A2-14: memory_health rollup includes generated_at + window_days
    # (health.py HealthReport.to_dict).
    assert "generated_at" in text, (
        "docs/api.md memory_health rollup no longer names "
        "`generated_at`; the report's ISO timestamp is the only way "
        "to pin a stored snapshot's age."
    )
    assert "window_days" in text, (
        "docs/api.md memory_health rollup no longer names "
        "`window_days`; the echoed analysis window is part of the "
        "return contract."
    )
    # A2-15: episode_handoff auto-resolve documents the caller-worktree
    # filter (tick-22) and the disabled_scopes cascade (tick-11). Match
    # case-insensitively so a prose rewording (sentence-leading capital
    # vs in-paragraph lowercase) doesn't false-trip the contract check.
    lowered = text.lower()
    assert "caller-worktree" in lowered or "caller worktree" in lowered, (
        "docs/api.md no longer documents the caller-worktree filter "
        "on episode_handoff auto-resolution; tick-22 established "
        "strict equality but callers reading the doc won't see "
        "cross-worktree isolation as part of the contract."
    )
    assert "disabled_scopes" in text, (
        "docs/api.md no longer mentions the disabled_scopes cascade "
        "on episode_handoff; tick-11 added the filter but the doc "
        "needs to surface it as an implicit filter."
    )


def test_handler_descs_enumerate_loop_phase_fields() -> None:
    """Per-tool DESC strings enumerate the loop-phase-1 additions.

    Sibling pin to `test_api_md_documents_loop_phase_surface`: api.md
    and SYSTEM_PROMPT_ADDENDUM are the human/policy-facing surfaces,
    but the model reads each tool's DESC directly off the MCP
    registration when deciding what to call and how to interpret the
    response. If api.md documents a field the DESC doesn't, the model
    can't discover it from inside a conversation — by the time it
    would look up api.md, it's already past the decision. Pin each
    field's presence in its own DESC so a future trim trips here
    rather than silently regressing feature discoverability:

    - `recently_removed_in_worktree` on `memory_scope_overview`
    - `recommendations` on `memory_health`
    - `depends_on_resolved` on `memory_search` hits
    - `curation_hint` on `memory_write` responses
    """
    from bettermemory.handlers.health import DESC_MEMORY_HEALTH
    from bettermemory.handlers.scope_overview import DESC_MEMORY_SCOPE_OVERVIEW
    from bettermemory.handlers.search import DESC_MEMORY_SEARCH
    from bettermemory.handlers.write import DESC_MEMORY_WRITE

    assert "recently_removed_in_worktree" in DESC_MEMORY_SCOPE_OVERVIEW, (
        "DESC_MEMORY_SCOPE_OVERVIEW no longer names "
        "`recently_removed_in_worktree`; the handler returns it "
        "(scope_overview.py) but the model can't discover it from "
        "the registered tool description. Restore the field or "
        "remove the runtime return."
    )
    assert "recommendations" in DESC_MEMORY_HEALTH, (
        "DESC_MEMORY_HEALTH no longer names `recommendations`; "
        "`HealthReport.to_dict` returns it but clients reading the "
        "registered description won't see the digest exists."
    )
    assert "depends_on_resolved" in DESC_MEMORY_SEARCH, (
        "DESC_MEMORY_SEARCH no longer names `depends_on_resolved`; "
        "the handler attaches it to hits but the model can't branch "
        "on a field whose existence isn't advertised."
    )
    assert "curation_hint" in DESC_MEMORY_WRITE, (
        "DESC_MEMORY_WRITE no longer mentions `curation_hint`; the "
        "passive curation-pressure surface fires on committed writes "
        "(`_maybe_attach_curation_hint`) but the model has no "
        "advertised hook telling it the block may appear."
    )


def test_handler_descs_enumerate_episode_tier_fields() -> None:
    """Per-tool DESC strings for the episode_* family enumerate the
    post-polish fields the api.md contract documents.

    Sibling pin to `test_handler_descs_enumerate_loop_phase_fields`:
    audit-3 (A3-09) flagged that the t14/t23 docs+DESC sweep covered the
    memory_* family but missed the episode_* family. Several post-t14
    fixes (t16 takeaway cap, t21 most-recent-N slice, t22 worktree
    strict equality, t11 disabled_scopes cascade) updated the handler
    code without updating the model-facing DESC string. Pin each
    field's presence in its own DESC so a future trim trips here rather
    than silently regressing episode-tier discoverability:

    - `max_takeaway_bytes` (t16 cap) on DESC_EPISODE_WRITE
    - `pruned_sessions` return field on DESC_EPISODE_WRITE
    - "most-recent" (t21 cap direction) on DESC_EPISODE_SEARCH
    - `worktree` (t22 strict equality) on DESC_EPISODE_HANDOFF
    - `disabled_scopes` (t11 cascade) on DESC_EPISODE_HANDOFF
    - `promoted_from_episode_id` return field on DESC_EPISODE_PROMOTE
    """
    from bettermemory.handlers.episode_handoff import DESC_EPISODE_HANDOFF
    from bettermemory.handlers.episode_promote import DESC_EPISODE_PROMOTE
    from bettermemory.handlers.episode_search import DESC_EPISODE_SEARCH
    from bettermemory.handlers.episode_write import DESC_EPISODE_WRITE

    assert "max_takeaway_bytes" in DESC_EPISODE_WRITE, (
        "DESC_EPISODE_WRITE no longer mentions `max_takeaway_bytes`; "
        "t16 (4d36967) added the cap because over-cap takeaways "
        "silently corrupt the YAML frontmatter — the model needs the "
        "limit advertised so it can size its summary."
    )
    assert "pruned_sessions" in DESC_EPISODE_WRITE, (
        "DESC_EPISODE_WRITE no longer names `pruned_sessions`; the "
        "handler returns it on every write but the model can't "
        "discover the field from the registered description."
    )
    assert "most-recent" in DESC_EPISODE_SEARCH, (
        "DESC_EPISODE_SEARCH no longer documents the most-recent-N "
        "cap direction; t21 fixed the slice so callers reading the "
        "DESC see which end of the list survives the cap."
    )
    lowered_handoff = DESC_EPISODE_HANDOFF.lower()
    assert "worktree" in lowered_handoff, (
        "DESC_EPISODE_HANDOFF no longer mentions the worktree "
        "isolation filter; t22 established strict equality for "
        "auto-resolution but the model can't reason about which "
        "prior session it'll adopt without the contract in the DESC."
    )
    assert "disabled_scopes" in DESC_EPISODE_HANDOFF, (
        "DESC_EPISODE_HANDOFF no longer mentions the disabled_scopes "
        "cascade; t11 added the filter to mirror memory_search / "
        "memory_list, but the DESC needs to surface it as an "
        "implicit filter so the model isn't surprised when a "
        "scope-disabled session's prior takeaways don't appear."
    )
    assert "promoted_from_episode_id" in DESC_EPISODE_PROMOTE, (
        "DESC_EPISODE_PROMOTE no longer names "
        "`promoted_from_episode_id`; the handler annotates this on "
        "every response (committed / pending / rejected) but the "
        "model can't correlate the promotion attempt back to its "
        "source episode without the field advertised."
    )


def test_desc_episode_promote_nudges_close_of_session_minting() -> None:
    """`DESC_EPISODE_PROMOTE` carries the state-channel convention.

    Phase 7 / G2. The store this project runs on is the evidence: its
    single most-used memory is an audit-loop state blob, filed as a
    `fact`. The durability gate's no-state rule did not stop state being
    written down — it only stopped it being written down in the tier
    built for it, so state landed in the fact layer wearing a fact's
    clothes, and then got re-retrieved every iteration because a loop
    re-reads its own position constantly.

    The convention that fixes it is a routing rule plus a moment:
    loop/working state goes to `episode_write` while the run is in
    flight, and session close is when the takeaways that hardened get
    minted into durable memories through this tool. That path is
    strictly better than an in-flight `memory_write` — the claim has
    survived the session that produced it, and the promoter can see the
    whole session's takeaways at once, so one consolidated write lands
    where three in-flight fragments would have arrived as separate
    near-duplicates.

    This lives in exactly ONE lean description. It is deliberately NOT
    mirrored into `DESC_EPISODE_WRITE`: duplicated policy across
    descriptions is the regression
    `test_policy_lives_once_not_triplicated_in_descriptions` exists to
    stop, and the budget cannot afford it twice.

    Hence the pin. This is resident prose with no other survival floor,
    entering a phase whose description pass is an explicit byte-count
    scalpel, and an unpinned paragraph is the cheapest thing such a pass
    can delete. The convention may be re-worded — both asserts are
    substring presence, not an exact string — but it may not be deleted
    by someone who never decided to delete it.

    The rationale and the worked scan-then-promote example live in
    docs/api.md and the plugin skill body, both free of the description
    budget. Only the routing rule and the timing are resident here.
    """
    from bettermemory.handlers.episode_promote import DESC_EPISODE_PROMOTE

    assert "belongs in episodes" in DESC_EPISODE_PROMOTE, (
        "DESC_EPISODE_PROMOTE no longer states the routing half of the "
        "state-channel convention (Phase 7 / G2): loop and working "
        "state belongs in episodes, not in a durable memory. Without "
        "it the model is back to the status quo this codified against "
        "— run-state written into the fact layer, because the fact "
        "layer is the tier it was told about."
    )
    assert "session close" in DESC_EPISODE_PROMOTE, (
        "DESC_EPISODE_PROMOTE no longer names session close as the "
        "moment to promote (Phase 7 / G2). The routing rule on its own "
        "sends state to episodes and leaves it there; the timing cue "
        "is what turns the journal into a source of durable facts "
        "instead of a write-only log that ages out on the TTL."
    )


def test_episode_worktree_descs_match_the_filter_each_handler_calls() -> None:
    """The two episode read surfaces filter by worktree with DIFFERENT
    functions, and each DESC has to describe its own.

    `episode_handoff` calls `_worktrees_equal_strict`; `episode_search`
    calls `origin.worktrees_match`, which is permissive — either side
    `None`, a caller in a LINKED worktree of the recorded checkout, or a
    recorded worktree positively gone from disk all pass through. So
    "the same isolation" is not a thing that can be said about both, and
    `DESC_EPISODE_SEARCH` said it anyway: it advertised "WORKTREE
    ISOLATION ... mirroring the isolation episode_handoff enforces" and
    enumerated two of the four pass-through cases, omitting exactly the
    two a reader cannot guess. The linked-worktree case is the one that
    bites, because agent fan-out runs in linked worktrees — under it the
    primary checkout's episodes stay fully visible, which is the
    opposite of what the word "isolation" promised.

    The handler's own inline comments were correct the whole time. Only
    the model-facing copy was wrong, which is the recurring shape: a
    DESC is the highest-leverage prose in the system and the least
    re-read. So this asserts against the SOURCE rather than against a
    remembered fact — swap either handler's filter and the test names
    the DESC that now lies.
    """
    import importlib
    import inspect

    # `importlib` and not `from ... import episode_search`: the package
    # `__init__` binds each handler's NAME to the coroutine it re-exports,
    # which shadows the submodule of the same name, and the attribute
    # access below would then read a function.
    search_mod = importlib.import_module("bettermemory.handlers.episode_search")
    handoff_mod = importlib.import_module("bettermemory.handlers.episode_handoff")

    search_src = inspect.getsource(search_mod)
    handoff_src = inspect.getsource(handoff_mod)

    assert "worktrees_match" in search_src, (
        "episode_search no longer calls the permissive `worktrees_match`. "
        "If it moved to strict equality, DESC_EPISODE_SEARCH's "
        "'PERMISSIVE, not a boundary' paragraph is now false — rewrite it."
    )
    assert "_worktrees_equal_strict" in handoff_src, (
        "episode_handoff no longer calls `_worktrees_equal_strict`. "
        "DESC_EPISODE_HANDOFF advertises 'strict equality' and "
        "DESC_EPISODE_SEARCH contrasts itself against it — both are now "
        "stale."
    )

    desc = search_mod.DESC_EPISODE_SEARCH
    assert "PERMISSIVE" in desc, (
        "DESC_EPISODE_SEARCH stopped calling its worktree filter "
        "permissive. It runs `worktrees_match`, which lets four distinct "
        "cases through; describing that as isolation tells the model it "
        "has a boundary it does not have."
    )
    for cue, why in (
        (
            "LINKED",
            "the linked-worktree pass-through — agent fan-out runs in "
            "linked worktrees, so this is the case most likely to "
            "surprise a caller that trusted the filter",
        ),
        (
            "gone from disk",
            "the dead-worktree degrade, which reopens episodes from "
            "checkouts that no longer exist",
        ),
    ):
        assert cue in desc, f"DESC_EPISODE_SEARCH no longer documents {cue!r}: {why}."

    assert "strict" in desc, (
        "DESC_EPISODE_SEARCH no longer contrasts itself with "
        "episode_handoff's strict filter. The two surfaces answer "
        "'same worktree?' differently and a caller reading only one "
        "DESC will assume they agree."
    )


def test_audit_turn_desc_enumerates_retrieval_event_kinds() -> None:
    """DESC_MEMORY_AUDIT_TURN names every event kind that shields the
    miss probe.

    audit-3 (A3-01) flagged that `_RETRIEVAL_EVENT_KINDS` includes
    `list` but the DESC + docstring only named `search` / `show`. A
    silent-miss verdict is gated on "no retrieval in the lookback
    window," so a caller reading the DESC needs to see the full set
    of events that count as "the model retrieved" — otherwise the
    contract reads tighter than it actually is.
    """
    from bettermemory.handlers.audit_turn import DESC_MEMORY_AUDIT_TURN

    assert "memory_list" in DESC_MEMORY_AUDIT_TURN, (
        "DESC_MEMORY_AUDIT_TURN no longer names `memory_list` in the "
        "retrieval-event predicate; `_RETRIEVAL_EVENT_KINDS` "
        "(audit.py) includes it but the DESC stops short — a caller "
        "would assume a list call doesn't shield the audit."
    )


def test_api_md_does_not_promise_worktree_isolation() -> None:
    """`auto_scope`'s worktree half is permissive, and api.md described it
    as isolation in three places.

    All three read `origin.worktrees_match` — `memory_search` via
    `should_include_for_caller`, `episode_search` and `episode_patterns`
    directly — and it passes an episode or memory through on four
    distinct cases, of which the docs listed two. The unlisted pair is
    the pair a reader cannot infer: a caller in a LINKED worktree of the
    recording checkout, and a recorded worktree positively gone from
    disk.

    This is worth a guard rather than a one-time correction because
    "isolation" is a claim a user can rely on for separation between
    projects, and the true rule is weaker than the word. `episode_patterns`
    raises the stakes again — its commit path DELETES the episodes its
    filter admits, so every pass-through case is a cross-worktree delete.

    Asserted as an absence plus the two missing cues, so a future doc
    rewrite cannot quietly restore the stronger word.
    """
    api_md = Path(__file__).resolve().parents[1] / "docs" / "api.md"
    text = api_md.read_text(encoding="utf-8")

    assert "Worktree isolation for the" not in text, (
        "docs/api.md is advertising worktree ISOLATION for a filter that "
        "runs the permissive `origin.worktrees_match`. It is scoping, not "
        "a boundary — say so, and keep the four pass-through cases."
    )
    for cue, why in (
        ("linked worktree", "the linked-worktree pass-through (agent fan-out)"),
        ("gone from disk", "the dead-worktree degrade"),
        (
            "permissive",
            "the word that stops a reader treating this as a guarantee",
        ),
    ):
        assert cue in text, f"docs/api.md no longer documents {cue!r} — {why}."


def test_api_md_since_prior_session_strict_after() -> None:
    """api.md memory_search documents the strict-after boundary semantic.

    audit-3 (A3-02) flagged that api.md said "at or after" but the
    code path was strict-`>` post-t20 (ffad750) — the boundary IS the
    prior session's last-event ts so a memory whose `updated` equals
    it belongs to that prior session, not the current-session delta.
    Pin the corrected wording so a future doc rewording can't drift
    back to the inclusive form (which would double-count the
    boundary memory across memory_search and
    `curation_pending_new_since_last_session`).
    """
    api_md = Path(__file__).resolve().parents[1] / "docs" / "api.md"
    text = api_md.read_text(encoding="utf-8")
    assert "strictly after" in text, (
        "docs/api.md memory_search no longer says `strictly after` "
        "for since_prior_session; the boundary is exclusive — strict-`>` "
        "in the handler — and the doc has to match or the two "
        "'what's new' surfaces drift on the boundary-equal memory."
    )


def test_api_md_memory_show_documents_commit_drift_recommendation() -> None:
    """api.md memory_show enumerates the `commit_drift.recommendation` field.

    audit-3 (A3-08) flagged that the api.md commit_drift mention
    stopped at the block-existence note without enumerating the
    fields. `CommitDriftStatus.to_dict` (verify.py:719-724) returns
    `{status, commits_since_verify, recommendation}`; the
    `recommendation` string is the actionable surface the model would
    use, and a caller reading the doc needs to know it's there.
    """
    api_md = Path(__file__).resolve().parents[1] / "docs" / "api.md"
    text = api_md.read_text(encoding="utf-8")
    # Look for `recommendation` near a commit_drift mention so the
    # assertion targets the right block. We can't anchor on a paragraph
    # because the doc evolves; a presence check across the file is
    # sufficient — `recommendation` appears only on commit_drift /
    # staleness rollups, both of which are part of the documented
    # return surface.
    assert "recommendation" in text, (
        "docs/api.md no longer documents the `recommendation` field "
        "on commit_drift; CommitDriftStatus.to_dict returns it but "
        "the doc stops at the block-existence note — callers can't "
        "discover the actionable string is part of the return shape."
    )


def test_api_md_documents_max_takeaway_bytes() -> None:
    """api.md episode_write enumerates the `max_takeaway_bytes` cap.

    audit-3 (A3-07) flagged that t16 (4d36967) added the cap to the
    handler but never landed in api.md. The cap exists because
    takeaways serialize into the YAML frontmatter region (64 KB
    ceiling) — an over-cap takeaway corrupts the file and the episode
    vanishes from every read surface despite returning `committed`.
    Pin the doc mention so a caller writing against the contract
    knows the limit before they discover it via a vanished episode.
    """
    api_md = Path(__file__).resolve().parents[1] / "docs" / "api.md"
    text = api_md.read_text(encoding="utf-8")
    assert "max_takeaway_bytes" in text, (
        "docs/api.md no longer documents the `max_takeaway_bytes` "
        "cap on episode_write; the handler enforces it but a "
        "caller can't discover the limit from the contract doc."
    )


def test_api_md_dead_weight_rule_matches_shared_predicate() -> None:
    """api.md memory_curate states the consolidated dead-weight rule.

    round-88 Branch B flagged that the demotion parenthetical still
    taught the pre-consolidation rule ("created before the window,
    retrieved at least once, never applied") while the shared
    `_is_dead_weight` predicate (health.py) keys the window on the
    freshest maintenance touch (created/updated/last_verified_at),
    grants a 2-day endorsement grace on the earliest retrieval, and
    parks memories with an unresolved contradiction. Every extra gate
    is exclusionary, so the stale form over-promises demotions that
    never happen on `dry_run=False` — a state-mutating contract. Pin
    both directions so the doc can't drift back.
    """
    api_md = Path(__file__).resolve().parents[1] / "docs" / "api.md"
    text = api_md.read_text(encoding="utf-8")
    assert "created before the window" not in text, (
        "docs/api.md has drifted back to the stale pre-consolidation "
        "dead-weight rule; `_is_dead_weight` windows on the freshest "
        "touch (created/updated/verified), not `created` alone."
    )
    assert "earliest retrieval" in text, (
        "docs/api.md memory_curate no longer documents the 2-day "
        "endorsement grace on the earliest retrieval; the shared "
        "predicate exempts freshly-retrieved memories whose "
        "auto-applied endorsement hasn't had time to land."
    )
    assert "no unresolved contradiction" in text, (
        "docs/api.md memory_curate no longer documents contradiction "
        "parking; the shared predicate excludes memories with an "
        "unresolved `use(contradicted)` event from demotion."
    )


def test_docs_state_semantic_is_enabled_by_the_extra_alone() -> None:
    """api.md + internals.md state the semantic model-gating contract.

    REVERSED, deliberately, and this test is the record of it. It used to
    pin the opposite: that `_semantic_model_or_none` resolves ONLY behind
    a config opt-in (`search_mode = "semantic"` or `semantic_dedup =
    true`), so an installed extra alone left `hybrid` at keyword+BM25 and
    made per-call `mode="semantic"` error with the install hint.

    That contract lost on measurement. Fusing the semantic leg took
    recall@1 from 35% to 60% on plainly-worded questions and from 80% to
    90% on re-queried ones
    (`bench/retrieval/results/v2-unpadded-2026-07-26.json` — 180 synthetic
    documents, 20 blind-authored questions per probe, easier than a real
    store, so the deltas carry and the absolute rates do not), and requiring
    an unrelated WRITE-time flag to unlock a SEARCH improvement was a
    foot-gun that had already produced wrong install advice more than once.
    `hybrid` now resolves a model whenever an extra imports.

    The old gate's stated reason — the shared factory would flip
    write-dedup Jaccard->cosine — was real and is answered rather than
    ignored: `handlers.write._resolve_dedup_thresholds` reads
    `semantic_dedup` itself now, pinned by
    `test_tombstone_dedup.py::test_write_dedup_ignores_a_model_resolved_for_search`.

    Both directions stay pinned, just inverted: the docs must not
    re-acquire the opt-in claim, and they must keep saying `semantic_dedup`
    is about writes.
    """
    root = Path(__file__).resolve().parents[1]

    # Whitespace-collapsed: these are prose docs, so a phrase legitimately
    # wraps across a line. Matching raw text would make the guard depend on
    # where a paragraph happened to break rather than on what it says.
    def _flat(path: str) -> str:
        return re.sub(r"\s+", " ", (root / "docs" / path).read_text(encoding="utf-8"))

    api_text = _flat("api.md")
    internals_text = _flat("internals.md")

    # Direction 1 runs over EVERY tracked markdown file, not a hand-list.
    #
    # It used to name `api.md` and `internals.md`. `docs/installation.md` —
    # the canonical install page, the one `doctor`'s own `fix_hint` sends
    # people to — was never in the population, and carried the retired
    # claim for three releases while five other surfaces were pinned
    # against it. A guard whose population is a hand-list only ever covers
    # the files someone remembered; the population has to come from
    # somewhere that grows on its own. `git ls-files` is that somewhere,
    # and it is the same correction 3.34.0 applied to the prose corpora.
    #
    # The forbidden literals widened too, and that half matters just as
    # much: the drift wrote "the extra alone doesn't change ranking —
    # semantic search also needs the config opt-in", which matches neither
    # original literal. Adding the file without adding the wording would
    # have left the guard green over the very text it exists to forbid.
    forbidden = (
        "plus a config opt-in",
        "the extra alone is not enough",
        "also needs the config opt-in",
        "doesn't change ranking",
        "does not change ranking",
    )
    tracked_md = _git_tracked_files("*.md")
    assert tracked_md, "expected a git checkout with tracked markdown"
    for rel in tracked_md:
        # CHANGELOG.md is the project's record of what changed, so it
        # QUOTES retired prose on purpose. Exempting it is not narrowing
        # the population to dodge a failure — a changelog that could not
        # name the wording it retired could not describe the fix.
        if rel == "CHANGELOG.md":
            continue
        text = re.sub(r"\s+", " ", (root / rel).read_text(encoding="utf-8"))
        for literal in forbidden:
            assert literal not in text, (
                f"{rel} states that an embeddings extra alone does not enable "
                f"the semantic leg ({literal!r}); installing one is the whole "
                "opt-in, and `semantic_dedup` is a WRITE-time flag."
            )

    # Direction 2: both surfaces say installation is what enables it, and
    # that `semantic_dedup` is not the lever.
    assert "installing it is the whole opt-in" in api_text, (
        "docs/api.md no longer states that installing the extra is what "
        "enables the semantic leg."
    )
    assert "installing it is the whole opt-in" in internals_text, (
        "docs/internals.md no longer states that installing the extra is "
        "what enables the semantic leg."
    )
    for name, text in (
        ("docs/api.md", api_text),
        ("docs/internals.md", internals_text),
    ):
        assert "semantic_dedup" in text, (
            f"{name} no longer mentions `semantic_dedup`. It still exists and "
            "still changes behaviour — silently dropping it is how a reader "
            "concludes it is the search knob again."
        )
    assert "WRITE-time dedup only" in api_text, (
        "docs/api.md no longer scopes `semantic_dedup` to write-time dedup; "
        "unscoped, it reads as a retrieval gate."
    )


@pytest.mark.parametrize("full_tool_surface", [True, False])
async def test_addendum_tool_names_exist_on_server(
    tmp_path: Path, full_tool_surface: bool
) -> None:
    """Every `memory_*` tool referenced in the addendum resolves on the
    server — under BOTH tool surfaces.

    The earlier version of this test only enforced parity between the
    addendum and the doc copy — renaming a tool on the server (or dropping
    one) would not fail the suite, and the addendum would silently start
    referencing a tool the server doesn't expose. That gap closed; this
    one closes the next.

    The check used to build `Config(storage=…)` with no `BehaviorConfig`,
    which takes the dataclass default `full_tool_surface = True`.
    `load_config()` — the only path the `bettermemory` entry point runs —
    coerces the same key to False when it is unset, so the guard was
    certifying the addendum against a surface production never builds.
    Parametrizing both legs is what makes the LEAN leg, the shipped one,
    actually get checked.

    Direction stays one-way: every name the addendum mentions must exist.
    The reverse — every server tool must appear in the addendum — would be
    too strict (it's reasonable to ship a new tool one release without yet
    documenting it in the advanced-tightening surface), and the
    README/api.md cover the full inventory.
    """
    _assert_surface_parity(
        surface_name="SYSTEM_PROMPT_ADDENDUM",
        text=SYSTEM_PROMPT_ADDENDUM,
        registered=await _registered_tool_names(
            tmp_path, full_tool_surface=full_tool_surface
        ),
        full_tool_surface=full_tool_surface,
    )


@pytest.mark.parametrize("full_tool_surface", [True, False])
async def test_skill_tool_names_exist_on_server(
    tmp_path: Path, full_tool_surface: bool
) -> None:
    """Every `memory_*` tool referenced in the plugin's SKILL.md resolves
    on the server — under BOTH tool surfaces.

    Symmetric to the addendum check above, with the same one-way
    direction. SKILL.md is the policy companion for plugin users and
    deliberately doesn't enumerate every tool — it covers retrieval,
    writing, verification, record-use, and curation by name and lets
    the per-tool descriptions carry the rest. Tools the skill DOES
    name must still resolve, or a rename would leave the plugin telling
    the model to call a nonexistent name.

    The lean leg is the one that matters most here: `plugin/.mcp.json`
    launches `uvx bettermemory` with an empty env, so a plugin install IS
    the lean surface, and this file is the prose that install ships.
    """
    skill_path = (
        Path(__file__).resolve().parents[1]
        / "plugin"
        / "skills"
        / "bettermemory"
        / "SKILL.md"
    )
    _assert_surface_parity(
        surface_name="SKILL.md",
        text=skill_path.read_text(encoding="utf-8"),
        registered=await _registered_tool_names(
            tmp_path, full_tool_surface=full_tool_surface
        ),
        full_tool_surface=full_tool_surface,
    )
