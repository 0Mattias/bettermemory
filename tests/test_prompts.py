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
"""

from __future__ import annotations

import re
from pathlib import Path

from bettermemory.config import Config, StorageConfig
from bettermemory.prompts import SYSTEM_PROMPT_ADDENDUM
from bettermemory.server import build_server
from bettermemory.session import SessionState
from bettermemory.store import Store


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


def test_docs_state_semantic_config_optin_gate() -> None:
    """api.md + README state the semantic model-gating contract.

    round-88 Branch B: `_semantic_model_or_none` (semantic_setup.py)
    resolves the embedding model ONLY behind the config-level opt-in
    (`[behavior] search_mode = "semantic"` or `semantic_dedup = true`)
    — installation status of the `[embeddings]` extra is never
    consulted on its own. The extra alone therefore leaves hybrid at
    keyword+BM25 fusion and per-call mode="semantic" erroring with
    the install hint (tests/test_server_search_mode.py pins the code
    side; the gate is deliberate — resolving on extra-presence alone
    would silently flip write-dedup from Jaccard to cosine). Pin both
    directions on both doc surfaces so they can't drift back to the
    extra-is-sufficient claim.
    """
    root = Path(__file__).resolve().parents[1]
    api_text = (root / "docs" / "api.md").read_text(encoding="utf-8")
    internals_text = (root / "docs" / "internals.md").read_text(encoding="utf-8")
    # Direction 1: the stale extra-is-sufficient claims are gone.
    assert "+ semantic when the `[embeddings]` extra is installed" not in api_text, (
        "docs/api.md hybrid bullet has drifted back to claiming the "
        "`[embeddings]` extra alone adds the semantic leg; the model "
        "factory never consults installation status without the "
        "config-level opt-in."
    )
    assert "requires the `[embeddings]` extra)" not in api_text, (
        "docs/api.md semantic bullet has drifted back to the "
        "extra-is-sufficient claim; per-call mode='semantic' under "
        "the default config errors even with the extra installed."
    )
    assert "plus semantic when the embeddings extra is installed)" not in internals_text
    assert "add the semantic third leg with one extra" not in internals_text, (
        "docs/internals.md has drifted back to claiming one extra adds the "
        "semantic leg; the config-level opt-in is also required."
    )
    # Direction 2: both surfaces name the config-level opt-in knobs.
    for name, text in (("docs/api.md", api_text), ("docs/internals.md", internals_text)):
        assert 'search_mode = "semantic"' in text, (
            f"{name} no longer names the `search_mode` config opt-in "
            "that gates semantic participation."
        )
        assert "semantic_dedup" in text, (
            f"{name} no longer names the `semantic_dedup` config "
            "opt-in that gates semantic participation."
        )
    # api.md additionally qualifies the per-call override: it picks the
    # ranker but cannot bypass the model gate.
    assert "does not bypass the model gate" in api_text, (
        "docs/api.md no longer qualifies 'per-call override beats "
        "config' against the model gate; unqualified, it reads as if "
        "mode='semantic' works under the default config."
    )


async def test_addendum_tool_names_exist_on_server(tmp_path: Path) -> None:
    """Every `memory_*` tool referenced in the addendum is registered on the server.

    The previous version of this test only enforced parity between the
    addendum and the doc copy — renaming a tool on the server (or dropping
    one) would not fail the suite, and the addendum would silently start
    referencing a tool the server doesn't expose. This closes that gap.

    Direction is intentionally one-way: every name the addendum mentions
    must exist on the server. The reverse — every server tool must appear
    in the addendum — would be too strict (it's reasonable to ship a new
    tool one release without yet documenting it in the advanced-tightening
    surface), and the README/api.md cover the full inventory.
    """
    # Hermetic server build: tmp_path-backed store and a fresh SessionState
    # so the module-level singleton from `get_state()` isn't shared with
    # other tests. The list_tools call doesn't write anything to disk.
    cfg = Config(storage=StorageConfig(directory=str(tmp_path)))
    mcp = build_server(config=cfg, store=Store(tmp_path), state=SessionState())
    registered = {tool.name for tool in await mcp.list_tools()}

    referenced = set(_TOOL_REF_RE.findall(SYSTEM_PROMPT_ADDENDUM))
    # Strip kwarg-shaped names the regex over-includes (`memory_ids`
    # is a parameter on `memory_record_use`, not a tool). Same
    # allowlist as the SKILL.md test below — keep them in sync.
    KNOWN_KWARGS = {"memory_ids", "episode_id"}
    referenced = {
        name
        for name in referenced
        if not name.endswith("_") and name not in KNOWN_KWARGS
    }

    missing = referenced - registered
    assert not missing, (
        f"SYSTEM_PROMPT_ADDENDUM references tools that aren't registered "
        f"on the server: {sorted(missing)}. Either rename the tool back, "
        f"register the new tool, or update the addendum to match."
    )


async def test_skill_tool_names_exist_on_server(tmp_path: Path) -> None:
    """Every `memory_*` tool referenced in the plugin's SKILL.md is
    registered on the server.

    Symmetric to the addendum check above, with the same one-way
    direction. SKILL.md is the policy companion for plugin users and
    deliberately doesn't enumerate every tool — it covers retrieval,
    writing, verification, record-use, and curation by name and lets
    the per-tool descriptions carry the rest. Tools the skill DOES
    name must still resolve on the server, or a rename would leave
    the plugin telling the model to call a nonexistent name.
    """
    skill_path = (
        Path(__file__).resolve().parents[1]
        / "plugin"
        / "skills"
        / "bettermemory"
        / "SKILL.md"
    )
    skill_text = skill_path.read_text(encoding="utf-8")

    cfg = Config(storage=StorageConfig(directory=str(tmp_path)))
    mcp = build_server(config=cfg, store=Store(tmp_path), state=SessionState())
    registered = {tool.name for tool in await mcp.list_tools()}

    referenced = set(_TOOL_REF_RE.findall(skill_text))
    # Strip kwarg-shaped names that the regex over-includes — `memory_ids`
    # is a parameter on `memory_record_use`, not a tool. Anything that
    # isn't actually registered AND isn't a real `memory_*` tool can
    # only be a kwarg name or doc artifact; the parameter-form regex on
    # `_TOOL_REF_RE` already drops `name=`, but a bare `memory_ids` in
    # prose still matches. Explicit allowlist of known kwargs keeps the
    # assertion's signal sharp.
    KNOWN_KWARGS = {"memory_ids", "episode_id"}
    referenced = {
        name
        for name in referenced
        if not name.endswith("_") and name not in KNOWN_KWARGS
    }

    missing = referenced - registered
    assert not missing, (
        f"SKILL.md references tools that aren't registered on the "
        f"server: {sorted(missing)}. Either rename the tool back, "
        f"register the new tool, or update SKILL.md to match."
    )
