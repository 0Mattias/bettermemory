"""Ingest Claude Code's auto-memory directories into the bettermemory store.

Claude Code 2.x ships a filesystem-backed auto-memory feature at
``~/.claude/projects/<sanitized-cwd>/memory/``. It accumulates as the
user works and is owned by the agent surface, not by bettermemory's
audit layer. Today the plugin's SKILL.md banner tells the agent
*"don't fragment memory across ad-hoc files alongside"* — i.e., don't
write into that directory. That's the right policy going forward but
it leaves existing files stranded.

This module fills the gap. ``bettermemory ingest --from PATH`` walks
the source directory, parses each ``.md`` file's frontmatter (the
auto-memory format: ``name``, ``description``, ``metadata.type``),
maps the type to a bettermemory category, dedups against the active
store and the tombstone log, and writes the survivors as ordinary
bettermemory records. The framing is "consume rather than fight":
the user keeps the ergonomic capture Claude Code provides and gains
the verification surface bettermemory layers on top.

Design notes:

- **Dedup leans on the existing `find_similar` Jaccard pass.** A
  re-ingest after a partial run won't duplicate, because byte-for-byte
  matches Jaccard at 1.0 and trip the high-similarity threshold. No
  sidecar state file is needed.

- **Tombstone-aware.** Source files that match a tombstoned memory
  surface as skipped with the reason ``previously_removed`` — the same
  pattern as the MCP `memory_write` handler's dedup gate. Imports of
  memories the user already chose to remove don't quietly resurrect.

- **`user-inference` category writes are kept.** The MCP handler's
  always-pending gate exists because the *model* infers user claims
  in conversation; that's the high-risk surface. An ingest run is the
  user telling bettermemory "these pre-existing user-curated files
  are mine, ingest them" — going through pending-confirm per row
  would be ergonomic theatre. The category lands on the record so
  downstream curation still treats them as user-claim memories.

- **No source-file mutation.** Modifying the source `.md` files
  would race Claude Code's own auto-memory writes. The dedup contract
  makes the source-file marker unnecessary anyway.

The CLI wrapper lives in ``server.py`` (``_cli_ingest``). This module
owns the pure compute (``compute_ingest_plan``), the persistence
(``apply_ingest_plan``), and the rendering (``render_ingest_text``)
so tests can drive each layer in isolation.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from . import _frontmatter as fm
from .models import (
    Category,
    Confidence,
    Memory,
    Source,
    TombstonedMemory,
    generate_ulid,
)
from .origin import Origin, capture
from .search import HIGH_SIMILARITY, find_similar, find_similar_tombstones
from .store import Store


# Map from auto-memory `metadata.type` to bettermemory Category.
# Anything not in the map (or a missing type entirely) falls back to
# FACT, matching the bettermemory write-time default. The mapping is
# deliberately conservative: `feedback` and `project` become facts
# rather than user-inference because the auto-memory convention
# already separates user-claim files (which Claude Code stamps with
# `type: user`) from project/world facts.
_TYPE_TO_CATEGORY: dict[str, Category] = {
    "user": Category.USER_INFERENCE,
    "feedback": Category.FACT,
    "project": Category.FACT,
    "reference": Category.AMBIENT,
}

# Map from auto-memory type to a scope tag that gets appended to the
# ingested record. Lets a downstream curation pass branch on "what
# kind of imported memory is this" without re-reading the body.
_TYPE_TO_EXTRA_SCOPE: dict[str, str] = {
    "user": "user-inferences",
    "feedback": "feedback",
    "project": "project-context",
    "reference": "reference",
}

# Catch typos / divergence early: a key added to one map without the
# other would silently downgrade ingest behaviour (a missing
# extra-scope means the type-derived scope is lost; a missing
# category means the row falls back to FACT). Module-import-time
# assert fails loudly before any user-facing call.
assert set(_TYPE_TO_CATEGORY) == set(_TYPE_TO_EXTRA_SCOPE), (
    "_TYPE_TO_CATEGORY and _TYPE_TO_EXTRA_SCOPE must share the same keys; "
    f"category-only: {set(_TYPE_TO_CATEGORY) - set(_TYPE_TO_EXTRA_SCOPE)}, "
    f"scope-only: {set(_TYPE_TO_EXTRA_SCOPE) - set(_TYPE_TO_CATEGORY)}"
)

# Default scope every ingested memory carries. The provenance tag
# lets a `memory_search` for ``imported-from-claude-code`` surface
# just the imported set — useful when triaging "what came across in
# the migration" later, and as a guardrail for any future un-ingest
# tooling.
DEFAULT_PROVENANCE_SCOPE = "imported-from-claude-code"

# Index files that should NOT be ingested as memories — they're
# navigation artefacts of the auto-memory feature, not stored claims.
# Hardcoded set rather than a glob so a future auto-memory release
# that adds, say, ``INDEX.md`` lands with a deliberate decision
# rather than a silent slip.
_INDEX_FILENAMES: frozenset[str] = frozenset({"MEMORY.md", "INDEX.md", "README.md"})


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


Action = Literal[
    "write",
    "skip_duplicate",
    "skip_tombstone",
    "skip_invalid",
    "skip_empty",
    "skip_symlink",
]

# Single source of truth for the `Action` literals. `IngestPlan.summary`
# pre-seeds zeros for every action so the renderer never has to guard
# missing keys, and adding a new `Action` literal without updating this
# tuple is a one-line diff that fails
# `test_actions_tuple_matches_action_literal` in `tests/test_ingest.py`
# rather than silently producing a missing bucket in the rollup.
_ACTIONS: tuple[Action, ...] = (
    "write",
    "skip_duplicate",
    "skip_tombstone",
    "skip_invalid",
    "skip_empty",
    "skip_symlink",
)


@dataclass
class IngestRow:
    """One source file's classification + planned action.

    ``written_id`` is populated only after ``apply_ingest_plan`` has
    actually committed the write. A dry-run plan carries
    ``action="write"`` with a ``written_id`` of ``None`` — the
    distinction lets the renderer say *"would write"* vs *"wrote"*
    without an extra flag.
    """

    source_path: Path
    title: str
    description: str
    auto_memory_type: str | None
    body: str
    scopes: list[str]
    category: Category
    action: Action
    reason: str
    written_id: str | None = None


@dataclass
class IngestPlan:
    """Output of ``compute_ingest_plan``.

    Carries every source file the walker found, including the ones
    skipped for invalid / empty / dedup reasons, so the renderer can
    summarise the full picture rather than just the successes.
    """

    generated_at: datetime
    source_root: Path
    rows: list[IngestRow] = field(default_factory=list)

    @property
    def summary(self) -> dict[str, int]:
        out: dict[str, int] = {a: 0 for a in _ACTIONS}
        out["total"] = len(self.rows)
        for row in self.rows:
            out[row.action] = out.get(row.action, 0) + 1
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at.isoformat(),
            "source_root": str(self.source_root),
            "summary": self.summary,
            "rows": [
                {
                    "source_path": str(r.source_path),
                    "title": r.title,
                    "description": r.description,
                    "auto_memory_type": r.auto_memory_type,
                    "scopes": list(r.scopes),
                    "category": r.category.value,
                    "action": r.action,
                    "reason": r.reason,
                    "written_id": r.written_id,
                }
                for r in self.rows
            ],
        }


# ---------------------------------------------------------------------------
# Compute
# ---------------------------------------------------------------------------


def _classify_one(
    source_path: Path,
    *,
    existing: list[Memory],
    tombstoned: list[TombstonedMemory],
    extra_scopes: list[str],
    high_threshold: float,
    force: bool = False,
) -> IngestRow:
    """Parse one source file and return the classified IngestRow.

    Pure compute — no writes happen here. Edge cases (parse errors,
    empty bodies, scope conflicts) all surface as a ``skip_*`` action
    with a one-line reason so the renderer can show them.
    """
    blank = IngestRow(
        source_path=source_path,
        title=source_path.stem,
        description="",
        auto_memory_type=None,
        body="",
        scopes=[],
        category=Category.FACT,
        action="skip_invalid",
        reason="placeholder",
    )

    try:
        post = fm.load(source_path)
    except (ValueError, KeyError, OSError) as exc:
        # `_frontmatter.load` raises ValueError on encoding / size /
        # malformed-YAML issues (the YAML backend's `yaml.YAMLError`
        # is translated to ValueError at the parser boundary — see
        # `_frontmatter.loads`) and OSError on file-read failures.
        # Catch the (ValueError, KeyError, OSError) trio so one
        # malformed source file never aborts the rest of the batch;
        # matches the discipline `store.load_all` uses on the active
        # memory directory.
        blank.reason = f"parse error: {exc}"
        return blank

    meta = post.metadata or {}
    # The auto-memory format used the nested
    # `metadata: {type: <kind>}` shape in early Claude Code releases,
    # then flattened to top-level `type: <kind>` later. Both forms
    # show up in real auto-memory directories. **Precedence: nested
    # wins.** If both keys are present (a transitional file mid-
    # migration), the nested value is honoured because the older
    # writer was authoritative when the file was first emitted, and
    # callers can later overwrite via the flat path if needed. Tests:
    # `test_user_type_maps_to_user_inference` covers nested;
    # `test_flat_type_key_is_honored` covers flat;
    # `test_nested_type_wins_when_both_present` pins the precedence.
    raw_type: Any = None
    nested = meta.get("metadata")
    if isinstance(nested, dict):
        raw_type = nested.get("type")
    if raw_type is None:
        raw_type = meta.get("type")
    auto_type: str | None
    if isinstance(raw_type, str):
        auto_type = raw_type.strip().lower() or None
    else:
        auto_type = None

    name = meta.get("name")
    title = name.strip() if isinstance(name, str) and name.strip() else source_path.stem

    description_raw = meta.get("description")
    description = description_raw.strip() if isinstance(description_raw, str) else ""

    # Body composition: the auto-memory format puts the canonical
    # one-line summary in `description` and the full prose in the body.
    # bettermemory's first-line-summary convention expects the summary
    # to live in the body's first line. We synthesise the body as
    # `<description>\n\n<post.content>` so the summary line lands
    # where bettermemory expects it. When `description` is empty,
    # the body's first line stays as the user wrote it.
    content_body = post.content.strip()
    if not content_body and not description:
        return IngestRow(
            source_path=source_path,
            title=title,
            description="",
            auto_memory_type=auto_type,
            body="",
            scopes=[],
            category=Category.FACT,
            action="skip_empty",
            reason="no body content and no description",
        )
    if description and content_body:
        composed = f"{description}\n\n{content_body}"
    elif description:
        composed = description
    else:
        composed = content_body

    category = _TYPE_TO_CATEGORY.get(auto_type or "", Category.FACT)

    scope_list: list[str] = [DEFAULT_PROVENANCE_SCOPE]
    if auto_type and auto_type in _TYPE_TO_EXTRA_SCOPE:
        scope_list.append(_TYPE_TO_EXTRA_SCOPE[auto_type])
    for s in extra_scopes:
        if s not in scope_list:
            scope_list.append(s)

    # Dedup gate: active store wins, then tombstones. Both checks use
    # the same Jaccard threshold as `memory_write` so a re-ingest
    # behaves the same way an interactive write would have. `force`
    # bypasses the active-store check (parity with `memory_write`'s
    # `force=True`) but never bypasses the tombstone check —
    # re-ingesting a deliberately-removed memory stays disallowed.
    if not force:
        active_hits = find_similar(composed, existing, high_threshold=high_threshold)
        high_active = [h for h in active_hits if h.relevance == "high"]
        if high_active:
            return IngestRow(
                source_path=source_path,
                title=title,
                description=description,
                auto_memory_type=auto_type,
                body=composed,
                scopes=scope_list,
                category=category,
                action="skip_duplicate",
                reason=f"matches active memory {high_active[0].id}",
            )

    tombstone_hits = find_similar_tombstones(
        composed, tombstoned, high_threshold=high_threshold
    )
    # Tombstone hits carry a `-removed` suffix on the relevance label
    # (see `find_similar_tombstones` — same convention the
    # MCP write handler uses).
    high_tomb = [h for h in tombstone_hits if h.relevance == "high-removed"]
    if high_tomb:
        return IngestRow(
            source_path=source_path,
            title=title,
            description=description,
            auto_memory_type=auto_type,
            body=composed,
            scopes=scope_list,
            category=category,
            action="skip_tombstone",
            reason=f"matches tombstoned memory {high_tomb[0].id}",
        )

    return IngestRow(
        source_path=source_path,
        title=title,
        description=description,
        auto_memory_type=auto_type,
        body=composed,
        scopes=scope_list,
        category=category,
        action="write",
        reason="ok",
    )


def compute_ingest_plan(
    source_root: Path,
    *,
    existing_memories: list[Memory],
    existing_tombstones: list[TombstonedMemory],
    extra_scopes: list[str] | None = None,
    now: datetime | None = None,
    high_threshold: float = HIGH_SIMILARITY,
    force: bool = False,
) -> IngestPlan:
    """Walk the source root for `.md` files and classify each one.

    Doesn't write — caller follows up with `apply_ingest_plan` if
    they want to commit. `extra_scopes` is appended to every record's
    scope list (after the provenance + type-derived defaults).
    `force=True` bypasses the active-store dedup gate (parity with
    `memory_write`'s `force`); tombstone dedup is always honoured.

    Raises `FileNotFoundError` if the source root doesn't exist —
    distinguished from "exists but empty" so the CLI can tell the
    user "the auto-memory directory doesn't exist yet" vs "exists
    but nothing to ingest."
    """
    now = now or datetime.now(timezone.utc)
    extras = list(extra_scopes or [])

    if not source_root.exists():
        raise FileNotFoundError(
            f"source root {source_root} does not exist. "
            "Pass --from to point at an existing auto-memory directory, "
            "or check whether Claude Code's auto-memory has produced any "
            "files yet."
        )

    if not source_root.is_dir():
        raise NotADirectoryError(f"source root {source_root} is not a directory.")

    rows: list[IngestRow] = []
    # Write rows committed earlier in THIS batch, folded back into the dedup
    # set so a second near-identical source file in the same run is caught as
    # a duplicate. The interactive memory_write path reloads the store between
    # writes; ingest classifies every file against one frozen snapshot, so
    # without this two identical files in one directory would both write.
    # `force` bypasses the active-store dedup in _classify_one anyway, so we
    # skip the bookkeeping there.
    planned: list[Memory] = []
    # Deterministic order for stable rendering + reproducible tests.
    for path in sorted(source_root.glob("*.md")):
        # `Path.is_file()` follows symlinks — so a `.md` symlink pointing
        # at `/etc/passwd` (or any file outside the source dir) would
        # otherwise be ingested verbatim as a memory. Detect and skip
        # symlinks up front rather than trying to parse them. We emit a
        # `skip_symlink` row so the summary surfaces the count alongside
        # the other skip reasons, but never read the target.
        if path.is_symlink():
            rows.append(
                IngestRow(
                    source_path=path,
                    title=path.stem,
                    description="",
                    auto_memory_type=None,
                    body="",
                    scopes=[],
                    category=Category.FACT,
                    action="skip_symlink",
                    reason="symlinks are not ingested",
                )
            )
            continue
        if not path.is_file():
            continue
        if path.name in _INDEX_FILENAMES:
            continue
        row = _classify_one(
            path,
            existing=existing_memories + planned,
            tombstoned=existing_tombstones,
            extra_scopes=extras,
            high_threshold=high_threshold,
            force=force,
        )
        rows.append(row)
        if row.action == "write" and not force:
            # Scopes are irrelevant to the body-Jaccard dedup; use the always-
            # valid provenance scope so a programmatic caller's odd extra_scope
            # can't make this synthetic Memory fail construction.
            planned.append(
                Memory(
                    id=generate_ulid(),
                    created=now,
                    updated=now,
                    scopes=[DEFAULT_PROVENANCE_SCOPE],
                    confidence=Confidence.MEDIUM,
                    source=Source.INFERRED,
                    body=row.body,
                    category=row.category,
                )
            )

    return IngestPlan(generated_at=now, source_root=source_root, rows=rows)


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


def apply_ingest_plan(
    plan: IngestPlan,
    store: Store,
    *,
    recorder: Any | None = None,
    cwd: Path | None = None,
) -> IngestPlan:
    """Execute every ``action="write"`` row in the plan.

    Returns the same plan with `written_id` populated on each
    committed row. Skipped rows pass through unchanged. Errors during
    a single write don't abort the run — the row's action flips to
    `skip_invalid` with the exception text as `reason` so the user
    can see which file failed without losing the rest of the batch.

    Origin capture — only when it's HONEST evidence. When the plan's
    source root IS the auto-memory directory for `cwd` (the process cwd
    when None — same default `discover_default_source_root` uses on the
    CLI path), the source files were *probably* captured while working
    in this cwd, and ``capture(cwd)`` is truthful provenance. Probably,
    not certainly: the layout's sanitization is MANY-TO-ONE (``/``,
    ``.``, ``:`` all fold to ``-``), so sibling paths like ``web-app``,
    ``web.app`` and ``web/app`` share one auto-memory directory, and the
    path equality alone can be satisfied from the wrong project. The
    session ``.jsonl`` records that live alongside the ``memory/`` dir
    carry each writing session's REAL cwd, so we cross-check: any
    observed session cwd that doesn't resolve to the ingest cwd means
    the directory holds (or may hold) a colliding foreign project's
    content, and the stamp is skipped — conservative ``origin=None`` on
    ambiguity. No ``.jsonl`` evidence at all leaves the stamp path
    intact. Any other ``--from`` root (another project's auto-memory, a
    copied directory) keeps ``origin=None`` — the conservative "global"
    default — because nothing proves the content belongs to this
    checkout. Before origin stamping existed, every ingested memory
    landed ``origin=None`` and could never satisfy the audit's
    positive-evidence suppression gate
    (`audit._caller_in_top_hit_project`), producing recurring false
    `search_miss` findings for in-project continuations.

    ``branch`` is always nulled on the stamped origin: the source files
    come from many historical sessions, and stamping them with the
    branch the *ingest* happens to run on would be misinformation —
    the same documented stance `migrate.py` takes for its origin
    backfill.
    """
    origin: Origin | None = None
    default_root = discover_default_source_root(cwd)
    if (
        default_root is not None
        and default_root.resolve() == plan.source_root.resolve()
        and _session_evidence_matches_cwd(plan.source_root, cwd)
    ):
        origin = capture(cwd).model_copy(update={"branch": None})
    for row in plan.rows:
        if row.action != "write":
            continue
        try:
            memory = store.write(
                content=row.body,
                scopes=row.scopes,
                confidence=Confidence.MEDIUM,
                source=Source.EXPLICIT,
                origin=origin,
                category=row.category,
            )
            row.written_id = memory.id
            if recorder is not None:
                recorder.record(
                    "write",
                    id=memory.id,
                    scopes=list(memory.scopes),
                    status="ingested",
                    triggered_from="cli_ingest",
                    source_path=str(row.source_path),
                )
        except (ValueError, OSError) as exc:
            # ValueError covers `Store.write` raising on bad input
            # (empty body, oversize, malformed scope) — per-row recoverable
            # and surfacing as `skip_invalid` lets the rest of the batch
            # continue. OSError covers filesystem hiccups on individual
            # writes (permissions, missing dir under a race). Bare
            # `Exception` would also swallow `MemoryError` and disk-full
            # situations, which should propagate so the operator sees
            # that the run can't continue rather than racking up
            # identical per-row failures.
            row.action = "skip_invalid"
            row.reason = f"write failed: {exc}"
    return plan


# Bounds for the session-record cross-check. Session `.jsonl` files can be
# large (full message transcripts); the cwd field appears on records from
# the first lines of a session, so a bounded prefix read per file is
# enough to collect the evidence without ever streaming whole transcripts.
_SESSION_SCAN_MAX_FILES = 20
_SESSION_SCAN_MAX_BYTES = 262_144
_SESSION_SCAN_MAX_LINES = 200


def _session_evidence_matches_cwd(source_root: Path, cwd: Path | None) -> bool:
    """True when the session records alongside the auto-memory dir don't
    contradict the claim that `cwd` is where the source files were written.

    The auto-memory directory's parent (``~/.claude/projects/<sanitized>/``)
    holds the session ``.jsonl`` transcripts whose records carry the
    session's real ``cwd`` — ground truth the sanitized directory name
    has lost. Decision rule, conservative on ambiguity:

    * every observed session cwd resolves to the ingest cwd → True
      (the stamp is honest evidence);
    * ANY observed session cwd resolves elsewhere → False (a colliding
      foreign project shares this directory — skip the stamp);
    * no ``.jsonl`` evidence at all → True (nothing contradicts the
      sanitized-path match; the pre-existing stamp path stays intact).
    """
    try:
        resolved_cwd = (cwd or Path.cwd()).resolve()
    except OSError:
        return False
    observed = _session_cwds(source_root.parent)
    if not observed:
        return True
    for raw in observed:
        try:
            if Path(raw).resolve() != resolved_cwd:
                return False
        except OSError:
            # Unresolvable claimed cwd — can't prove it's this project.
            return False
    return True


def _session_cwds(project_dir: Path) -> set[str]:
    """Distinct ``cwd`` values from session ``.jsonl`` records in
    `project_dir`, read with hard per-file and file-count bounds.

    Best-effort: unreadable files/lines are skipped individually, and a
    truncated trailing line from the byte-bounded read simply fails
    ``json.loads`` and is ignored. Returns an empty set when there is no
    evidence at all (no ``.jsonl`` files, or none with a ``cwd`` field).
    """
    out: set[str] = set()
    try:
        files = sorted(p for p in project_dir.glob("*.jsonl") if p.is_file())
    except OSError:
        return out
    for path in files[:_SESSION_SCAN_MAX_FILES]:
        try:
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                chunk = fh.read(_SESSION_SCAN_MAX_BYTES)
        except OSError:
            continue
        for line in chunk.splitlines()[:_SESSION_SCAN_MAX_LINES]:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict):
                raw = record.get("cwd")
                if isinstance(raw, str) and raw:
                    out.add(raw)
    return out


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------


def render_ingest_text(plan: IngestPlan, *, dry_run: bool) -> str:
    """Plain-text rendering for the CLI."""
    lines: list[str] = []
    verb = "would write" if dry_run else "wrote"
    header = "bettermemory ingest" + (" --dry-run" if dry_run else "")
    lines.append(header)
    lines.append("─" * 60)
    lines.append(f"Source: {plan.source_root}")
    s = plan.summary
    lines.append(f"Total files       {s['total']:>4d}")
    lines.append(f"  {verb:<11s}      {s['write']:>4d}")
    if s["skip_duplicate"]:
        lines.append(f"  skip duplicate    {s['skip_duplicate']:>4d}")
    if s["skip_tombstone"]:
        lines.append(f"  skip tombstoned   {s['skip_tombstone']:>4d}")
    if s["skip_empty"]:
        lines.append(f"  skip empty        {s['skip_empty']:>4d}")
    if s["skip_invalid"]:
        lines.append(f"  skip invalid      {s['skip_invalid']:>4d}")
    if s["skip_symlink"]:
        lines.append(f"  skip symlink      {s['skip_symlink']:>4d}")
    lines.append("")

    # Per-row detail, grouped by action so the eye lands on the
    # writes first. Within each group sort by source path for stable
    # display.
    by_action: dict[str, list[IngestRow]] = {}
    for row in plan.rows:
        by_action.setdefault(row.action, []).append(row)
    for action in (
        "write",
        "skip_duplicate",
        "skip_tombstone",
        "skip_empty",
        "skip_invalid",
        "skip_symlink",
    ):
        rows = by_action.get(action) or []
        if not rows:
            continue
        for row in sorted(rows, key=lambda r: r.source_path.name):
            scope_str = ",".join(row.scopes) or "—"
            wrote_id = (
                f"  → {row.written_id}"
                if row.written_id
                else ("  (would write)" if action == "write" else "")
            )
            type_str = row.auto_memory_type or "fact"
            lines.append(
                f"  [{action:<14s}] {row.source_path.name:<40s} "
                f"type={type_str:<10s} scopes={scope_str}{wrote_id}"
            )
            if action != "write" and row.reason and row.reason != "ok":
                lines.append(f"      {row.reason}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Path auto-discovery
# ---------------------------------------------------------------------------


def discover_default_source_root(cwd: Path | None = None) -> Path | None:
    """Best-guess auto-memory dir for the current working tree.

    Claude Code's auto-memory layout is
    ``~/.claude/projects/<sanitized-cwd-path>/memory/``, where the
    sanitization replaces BOTH ``/`` and ``.`` with ``-`` after
    stripping a leading ``/`` — so a worktree at
    ``/Users/x/projects/foo/.claude/worktrees/bar`` lives at
    ``-Users-x-projects-foo--claude-worktrees-bar``. Replacing only
    ``/`` silently misses every cwd that contains a dot (worktrees,
    hidden dirs, version-suffixed paths). Returns the path if it
    exists; None otherwise. The CLI treats None as "no auto-memory
    found — pass --from explicitly."

    On Windows, ``cwd.resolve()`` produces backslash-separated paths
    with a drive-letter prefix (``C:\\Users\\...``). ``as_posix()``
    normalises to forward slashes, and the colon is stripped because
    it's illegal in Windows filenames — so ``C:/Users/x`` becomes
    ``-C-Users-x`` rather than the unbuildable ``-C:\\Users\\x``.
    """
    cwd = cwd or Path.cwd()
    resolved = cwd.resolve().as_posix().lstrip("/")
    sanitized = "-" + resolved.replace("/", "-").replace(".", "-").replace(":", "")
    candidate = Path.home() / ".claude" / "projects" / sanitized / "memory"
    if candidate.exists() and candidate.is_dir():
        return candidate
    return None


__all__ = [
    "Action",
    "DEFAULT_PROVENANCE_SCOPE",
    "IngestPlan",
    "IngestRow",
    "apply_ingest_plan",
    "compute_ingest_plan",
    "discover_default_source_root",
    "render_ingest_text",
]
