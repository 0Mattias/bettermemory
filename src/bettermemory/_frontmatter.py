"""Minimal YAML-frontmatter parser, vendored to drop python-frontmatter.

`python-frontmatter` (1.1.0, the current release as of writing) calls
`codecs.open()` in `frontmatter.load()`, which Python 3.14 emits a
`DeprecationWarning` for. The library hasn't shipped a fix, and we only ever
used four entry points (`load`, `loads`, `dumps`, `Post`) — reimplementing
them is shorter than the docstring explaining why we vendor.

Bonus: we avoid the upstream foot-gun where `frontmatter.load(path,
Loader=yaml.SafeLoader)` silently swallows the `Loader` kwarg into
"default metadata" instead of using it. Here we always use pure-Python
`yaml.SafeLoader` / `yaml.SafeDumper` (see store.py for why C-extension
yaml is avoided).

On-disk format (matches what python-frontmatter produced, so existing
memory files keep loading):

    ---
    key: value
    list:
    - item
    ---

    body text
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ._fsutil import bounded_read


_DELIM = "---"

# YAML SafeLoader doesn't protect against alias-expansion DoS — the
# classic "billion laughs" pattern where deeply-nested aliases expand
# to a memory-pinning blob during parse. Memory frontmatter is dozens
# to a few hundred bytes per record in normal use; capping the YAML
# region at 64 KB neutralises the DoS without rejecting any real
# memory file. The store widens its trust boundary once `sync pull`
# is in use (a remote can push files into the memory directory), so
# the cap is no longer purely a defence against local mistakes.
_MAX_YAML_BYTES = 64 * 1024

# File-level cap on `_frontmatter.load`. The previous `read_text()`
# loaded the entire file before the 64 KB YAML cap could fire, so a
# hostile `sync pull` from a remote pushing a multi-GB `.md` exhausted
# memory before the parse path even ran. 1 MiB is ~250× the largest
# legitimate body the project has produced (verified_paths + body
# dense memories cap out under a couple of KB) and 16× the YAML cap,
# so any in-the-wild memory fits comfortably while pathological
# inputs hit a clean ValueError instead of an OOM.
_MAX_FILE_BYTES = 1024 * 1024

# Headroom reserved below the read cap for content-admitting writes, so any
# record we ACCEPT as active can still be tombstoned or renamed and stay
# readable. `Store.tombstone` re-dumps a loaded record with `removed` /
# `removed_reason` / `removed_session` appended, and `rename_scope` may swap
# in a longer scope string; those maintenance re-dumps must be allowed up to
# the full `_MAX_FILE_BYTES` (a tombstone over the read cap would be silently
# skipped on restore). Enforcing the reduced `_MAX_WRITE_BYTES` at admission
# guarantees that bounded growth always fits: without it, a record written
# right up to the read cap became un-removable AND un-renameable — the
# tombstone re-dump crossed the cap and the write-side guard rejected it,
# leaving a fully-visible record that could not be removed. 4 KiB comfortably
# covers the fixed tombstone metadata (`removed` timestamp + `removed_session`
# id) plus the removal reason, which `store._cap_removed_reason` bounds on its
# SERIALIZED (YAML-escaped) size — not raw length — so the reason's real
# contribution provably fits regardless of content (a raw-length bound let a
# control-char reason escape-inflate ~4x, past this headroom).
_MAINTENANCE_HEADROOM_BYTES = 4 * 1024
_MAX_WRITE_BYTES = _MAX_FILE_BYTES - _MAINTENANCE_HEADROOM_BYTES

# Pre-flight bounds for the `dumps` alias-expansion guard. `_NoAliasDumper`
# (below) refuses to emit YAML anchors/aliases, so every shared reference in
# a metadata structure expands into a full literal copy on dump. A crafted
# ~300-byte frontmatter with nested aliases (the classic "billion laughs"
# shape, e.g. from a hostile `sync pull` that wrote `.md` into the memory
# dir, or a hand-edit) therefore expands to hundreds of MB — and the
# `_MAX_YAML_BYTES` cap in `dumps` is checked only AFTER `yaml.dump` has
# already materialized that whole string, so it's a post-hoc backstop, not a
# guard. These bounds let `dumps` walk the structure FIRST and reject a
# pathological one cheaply, before any expansion happens. The node budget is
# tied to the byte cap: a 64 KB YAML region holds at most ~8 K nodes even at
# maximal scalar density, so 64 K expanded nodes is a comfortable ceiling
# above anything that fits the byte cap (the largest legitimate frontmatter
# the project produces is ~270 nodes) while a real alias bomb — millions of
# expanded nodes — trips the budget in microseconds. The depth cap stops a
# deep-nesting bomb (and unbounded recursion); real frontmatter nests 3
# levels at most (dict → list of link dicts), so 64 is wildly generous.
_MAX_DUMP_NODES = 64 * 1024
_MAX_DUMP_DEPTH = 64

# Companion BYTE budget for the same walk, and the load-bearing half of the
# guard. Counting nodes alone bounds an alias bomb built from MANY SMALL
# scalars and completely misses one built from A FEW LARGE ones: a 50 KB
# file whose frontmatter is one 50,000-character scalar plus four levels of
# ten-way aliases expands to 12,359 nodes — comfortably under the 64 K node
# budget — and then spends 218 seconds and 1.1 GB of RSS inside `yaml.dump`
# producing a 555 MB string, which the post-hoc `_MAX_YAML_BYTES` check
# finally rejects. Measured, not estimated. The record is also left
# permanently un-removable, because `tombstone` has to re-serialise it and
# pays the expansion twice on every attempt.
#
# The multiple over `_MAX_YAML_BYTES` is deliberate slack: this walk counts
# raw scalar lengths, while the emitted YAML adds quoting, escaping,
# indentation and key overhead, so a structure that is legitimately just
# under the byte cap can measure somewhat over it here. 4x is far below any
# bomb (which overshoots by four or more orders of magnitude) and far above
# the densest real record (~1,200 nodes, a few KB of scalars).
_MAX_DUMP_SCALAR_BYTES = 4 * _MAX_YAML_BYTES


@dataclass
class Post:
    """A parsed frontmatter document. Mirror of `frontmatter.Post`."""

    content: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


def loads(text: str) -> Post:
    """Parse a frontmatter string into a Post.

    If `text` doesn't begin with a `---` line, the whole thing is body and
    metadata is empty — same as python-frontmatter.
    """
    # Split into lines but remember whether the input had a trailing newline,
    # so the body round-trips. Strip CR so CRLF line endings work — a file
    # checked out with `core.autocrlf`, written by a Windows editor, or
    # arriving over `sync pull` from a Windows host must parse, and its body
    # must read the same on every platform.
    #
    # This normalisation is HALF of a pair, and it was the only half for a
    # long time: `dumps` wrote the body verbatim, so a CRLF body went to disk
    # with its CRs while every reader returned it without them — the file and
    # its readers disagreed, and the first lifecycle re-dump made the readers'
    # version permanent. The fix is the matching normalisation in `dumps`, NOT
    # dropping this one: removing it makes a Windows-authored memory read back
    # with CRs in its body on every platform, which then flow into the FTS
    # text, snippets and the next re-dump.
    raw_lines = text.split("\n")
    lines = [ln.rstrip("\r") for ln in raw_lines]

    # Opening delimiter must be `---` on its own line, line 0.
    if not lines or lines[0] != _DELIM:
        return Post(content=text, metadata={})

    # Find the closing `---` on its own line, somewhere after line 0.
    close_idx: int | None = None
    for i in range(1, len(lines)):
        if lines[i] == _DELIM:
            close_idx = i
            break
    if close_idx is None:
        # No closing delimiter — treat the whole text as body.
        return Post(content=text, metadata={})

    yaml_text = "\n".join(lines[1:close_idx])
    if len(yaml_text.encode("utf-8")) > _MAX_YAML_BYTES:
        # Pre-flight size check. Reject before yaml.load gets a chance
        # to start expanding aliases. Real memories don't approach this
        # ceiling — the largest legitimate frontmatter the project
        # produces (a memory with dense `verified_paths` /
        # `verified_commits` and several `links`) is a couple of KB.
        raise ValueError(
            f"frontmatter YAML exceeds {_MAX_YAML_BYTES}-byte cap "
            f"({len(yaml_text)} chars); refusing to parse"
        )
    body_lines = lines[close_idx + 1 :]
    # Drop a single separator blank line, mirroring python-frontmatter's
    # `---\n\n<body>` shape.
    if body_lines and body_lines[0] == "":
        body_lines = body_lines[1:]
    body = "\n".join(body_lines)

    # Translate the yaml package's exception hierarchy into ValueError
    # at the parser boundary. `yaml.YAMLError` inherits from `Exception`,
    # NOT from `ValueError`, so downstream callers in `store.py` that
    # catch `(ValueError, KeyError, OSError)` to skip malformed files
    # would otherwise crash on a single corrupt file — torn writes from
    # `sync pull`, hand-edit typos, partial-write recovery. The
    # frontmatter module owns the YAML boundary, so the translation
    # belongs here, not threaded through every callsite. `from exc`
    # preserves the original `yaml.YAMLError` on `__cause__` for
    # debugging.
    try:
        metadata = yaml.load(yaml_text, Loader=yaml.SafeLoader) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"malformed YAML: {exc}") from exc
    except RecursionError as exc:
        # A deeply-nested YAML document — the classic nesting bomb, e.g.
        # `id: [[[[ ... 600 levels ... ]]]]` or block-style `- - - -` —
        # drives the pure-Python `yaml.SafeLoader` past Python's recursion
        # limit. `RecursionError` subclasses `RuntimeError`, NOT
        # `yaml.YAMLError`, so the catch above misses it and it would
        # escape `loads`. The store's malformed-file skip path only
        # catches `(ValueError, KeyError, OSError)`, so an uncaught
        # RecursionError propagates out of every read surface
        # (`load_all` / `load_one` / `load_tombstones` / `rename_scope` /
        # `_find_path_for_id`) — one crafted ~1 KB file (a hand-edit, or a
        # hostile `sync pull` writing into the memory dir) would DoS reads
        # of the WHOLE store. That is the exact fail-open this module's
        # "one corrupt file shouldn't blind the rest of the store"
        # contract exists to prevent. The bomb is also under the 64 KB
        # `_MAX_YAML_BYTES` cap (~1 KB of brackets already overflows the
        # stack), so that guard doesn't fire. Translate to ValueError so
        # the skip path engages — the read-side mirror of the write-side
        # `_guard_dump_expansion` depth guard (`_MAX_DUMP_DEPTH`).
        raise ValueError(f"malformed YAML: nesting too deep ({exc})") from exc
    if not isinstance(metadata, dict):
        # Frontmatter must be a mapping; anything else is malformed.
        raise ValueError("frontmatter metadata must be a YAML mapping")

    return Post(content=body, metadata=metadata)


def load(path: Path | str) -> Post:
    """Read and parse a file. Replaces `frontmatter.load(path, ...)`.

    Stat-rejects files larger than :data:`_MAX_FILE_BYTES` before any
    allocation — the previous unbounded ``read_text()`` was a sync-pull
    DoS vector (a hostile remote pushing a multi-GB ``.md`` would OOM
    the loader before the 64 KB YAML cap ever fired).

    Decodes UTF-8 strictly: invalid bytes raise ``ValueError``. Not
    ``errors="replace"`` — substituting U+FFFD would let a corrupt
    memory file load into the retrieval surface with `doctor`
    reporting it clean, and the next mutator would rewrite the file,
    laundering the corruption permanently. Raising keeps the
    pre-2.6.4 contract (``read_text(encoding="utf-8")`` raised here
    too) so the store's malformed-file skip path fires.

    Re-raises `ValueError` from :func:`loads` with the file path
    prefixed — `loads` doesn't know which path it came from, but
    diagnostic output (the store's `doctor`, error logs) is dramatically
    more useful when the path is named.
    """
    raw = bounded_read(Path(path), _MAX_FILE_BYTES)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{path}: not valid UTF-8: {exc}") from exc
    try:
        return loads(text)
    except ValueError as exc:
        raise ValueError(f"{path}: {exc}") from exc


def _guard_dump_expansion(metadata: dict[str, Any]) -> None:
    """Reject metadata whose alias-free expansion would be pathological.

    `_NoAliasDumper` expands every shared reference into a literal copy, so a
    nested-alias bomb materializes to hundreds of MB inside `yaml.dump` before
    the `_MAX_YAML_BYTES` cap can fire. This walk counts nodes *with*
    expansion — shared children are re-counted on every path that reaches
    them, exactly as the dumper re-emits them — and aborts the moment the
    running count crosses :data:`_MAX_DUMP_NODES`. The early abort is what
    makes it bounded: a structure that would expand to millions of nodes is
    rejected after visiting only ~64 K, in microseconds, before `yaml.dump`
    allocates anything. A by-identity ``visited`` set would defeat the point —
    it would count each shared node once and so *miss* the bomb, which is
    precisely the structure built from shared references.

    It also accumulates SCALAR BYTES against :data:`_MAX_DUMP_SCALAR_BYTES`,
    and that half is what catches the bomb the node budget cannot see. A
    node count treats a 50,000-character string and the letter ``a`` as the
    same cost, so a bomb assembled from a few huge scalars instead of many
    small ones passes the node budget outright — measured: 12,359 expanded
    nodes against a 64 K budget, 218 seconds and 1.1 GB of RSS inside
    ``yaml.dump``, and a 555 MB string for the post-hoc byte cap to reject.
    Summing scalar lengths on the same expanding walk aborts that in
    microseconds, before ``yaml.dump`` allocates anything.

    Also bounds nesting depth (:data:`_MAX_DUMP_DEPTH`) to stop a deep-nesting
    bomb and the unbounded Python recursion it would otherwise drive.

    Raises ``ValueError`` on a pathological structure; returns ``None`` for
    anything a real memory could hold (largest legitimate frontmatter is
    ~270 nodes, ~3 levels deep — orders of magnitude under all three bounds).
    """
    count = 0
    scalar_bytes = 0

    def walk(obj: Any, depth: int) -> None:
        nonlocal count, scalar_bytes
        if isinstance(obj, (str, bytes)):
            # Counted with expansion, exactly like `count`: a scalar shared
            # by N alias paths is re-emitted N times, so it must be charged
            # N times. `len` rather than an encode, because the point is a
            # cheap bound — a character count is within a small factor of
            # the emitted byte count and the 4x slack absorbs the rest.
            scalar_bytes += len(obj)
            if scalar_bytes > _MAX_DUMP_SCALAR_BYTES:
                raise ValueError(
                    f"frontmatter metadata expands past "
                    f"{_MAX_DUMP_SCALAR_BYTES} scalar bytes; refusing to "
                    "serialize — shared references expand to literal copies "
                    "on dump, so this would materialize a multi-MB blob "
                    "(alias bomb?)"
                )
        if depth > _MAX_DUMP_DEPTH:
            raise ValueError(
                f"frontmatter metadata nests deeper than {_MAX_DUMP_DEPTH} "
                "levels; refusing to serialize (alias/nesting bomb?)"
            )
        count += 1
        if count > _MAX_DUMP_NODES:
            raise ValueError(
                f"frontmatter metadata expands past {_MAX_DUMP_NODES} nodes; "
                "refusing to serialize — shared references expand to literal "
                "copies on dump, so this would materialize a multi-MB blob "
                "(alias bomb?)"
            )
        if isinstance(obj, dict):
            for key, value in obj.items():
                # A YAML mapping key is itself an emitted node; count it so a
                # mapping packed with shared-reference values can't slip the
                # budget on the key side.
                count += 1
                if count > _MAX_DUMP_NODES:
                    raise ValueError(
                        f"frontmatter metadata expands past {_MAX_DUMP_NODES} "
                        "nodes; refusing to serialize — shared references "
                        "expand to literal copies on dump, so this would "
                        "materialize a multi-MB blob (alias bomb?)"
                    )
                walk(key, depth + 1)
                walk(value, depth + 1)
        elif isinstance(obj, (list, tuple)):
            for item in obj:
                walk(item, depth + 1)

    walk(metadata, 0)


class _NoAliasDumper(yaml.SafeDumper):
    """SafeDumper variant that never emits YAML anchors/aliases.

    Without this, pyyaml emits `&idN` / `*idN` syntax whenever two metadata
    fields reference the same object — e.g. `created` / `updated` on a fresh
    write, where both come from the same `utcnow()` call. The output is
    valid YAML and round-trips, but humans grep memory files and the alias
    form is harder to read than two literal timestamps.
    """

    def ignore_aliases(self, data: Any) -> bool:
        return True


def _represent_str(dumper: yaml.SafeDumper, data: str) -> yaml.ScalarNode:
    """Force double-quoted style for strings pyyaml would emit lossily.

    U+0085 (NEL) is a line break in YAML 1.1. With `allow_unicode=True`
    pyyaml writes it raw inside a single-quoted scalar, and the loader
    then folds it back to a space — so `dumps` -> `loads` silently
    rewrote the value. An episode takeaway "ran the migration\\x85then
    reverted it" read back as "...migration then reverted...", a tombstone
    `removed_reason` lost its break the same way, and two adjacent NELs
    collapsed to a single newline. Every frontmatter string is affected:
    `links[].note`, `origin.cwd`, `verified_paths` entries.

    Double-quoted style emits it as the `\\N` escape, which round-trips
    exactly. Scoped to strings that actually contain the character so the
    on-disk format — which people grep — is otherwise unchanged.
    """
    if "\x85" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style='"')
    return dumper.represent_str(data)


_NoAliasDumper.add_representer(str, _represent_str)


def normalise_body(body: str) -> str:
    """The presentation normalisation `dumps` applies to a body on the
    way to disk: the same per-line `rstrip("\\r")` `loads` performs on the
    way back, then a trailing `rstrip`.

    Public because it is not only `dumps` that needs it. Anything holding
    an in-memory body and comparing it against what a reader returns —
    `doctor`'s index content leg is the first — is comparing across this
    normalisation, and a private copy of the rule is a defect with a
    countdown (`docs/incidents/2026-07-26-staleness-verdict-constant-function.md`,
    "mirrored implementations guarded by comments"). One definition, two
    callers, and the round-trip property `tests/test_frontmatter.py` pins
    covers both.

    See the comment at the `dumps` call site for why this is a per-line
    strip rather than a `replace("\\r\\n", "\\n")`.
    """
    return "\n".join(ln.rstrip("\r") for ln in body.split("\n")).rstrip()


def dumps(
    post: Post,
    *,
    max_file_bytes: int = _MAX_WRITE_BYTES,
    max_yaml_bytes: int = _MAX_YAML_BYTES,
) -> str:
    """Serialise a Post to a frontmatter string.

    Matches python-frontmatter's output: `---\\n<yaml>\\n---\\n\\n<body>`,
    with `body` right-stripped of trailing whitespace (leading whitespace
    preserved). Existing files written by the previous library round-trip
    byte-for-byte (modulo the alias-suppression policy above, which only
    affects metadata dicts where two fields share the same object).

    `max_file_bytes` bounds the total serialized size. It defaults to
    `_MAX_WRITE_BYTES` (the read cap minus maintenance headroom) so any
    accepted record can later be tombstoned/renamed and stay readable;
    the lifecycle re-dump paths (`store.tombstone` / `rename_scope`) pass
    `_MAX_FILE_BYTES` to use the full read cap for that bounded growth.

    `max_yaml_bytes` is the analogous bound on just the frontmatter-YAML
    region. The signature default is the flat `_MAX_YAML_BYTES` — but that says
    only what an argument-less caller gets, NOT that a first write is
    unaffected, which is what this docstring inferred until
    `store._yaml_admission_cap` (645d909) made the inference false. A reduced
    ceiling now reaches here from TWO sources, keyed on what the write DOES
    rather than on which function performs it:

      * `store._yaml_admission_cap()` — `Store._write_path`'s own default, so
        every write that ADMITS content (`Store.write`, `Store.update`) arrives
        with a reduced ceiling, a first write included. `memory_update` can grow
        the frontmatter without touching the body — 64 `links`, each with a long
        `note`, are all individually legal and together land the frontmatter
        just under the flat cap — and without the reservation that minted
        records `memory_remove` could then never tombstone.
      * `store._lifecycle_redump_yaml_cap(current_yaml)` — the metadata-only
        re-dumps of an already-admitted record (`mark_verified`,
        `record_corroboration`, `rename_scope`'s active branch, `migrate`'s
        origin backfill/repair) reserve the same `_REMOVAL_META_BUDGET_BYTES`,
        but keyed on the record's CURRENT frontmatter, so a record already past
        the ceiling freezes there rather than being refused outright.

    A caller that passes nothing takes the flat cap, which is what the re-dumps
    that must not become refusable want — `store.tombstone` (it adaptively trims
    its own removal metadata to fit rather than lean on a ceiling),
    `store.restore`, `rename_scope`'s tombstone branch — and what `episodes`
    wants, being written once and never tombstoned, so it reserves nothing for a
    removal that never comes. Neither reduced ceiling can RELAX the flat cap: both
    are `<= _MAX_YAML_BYTES`, and the second check below fires only for a
    ceiling that comes in strictly under it.
    """
    # Pre-flight: reject an alias/nesting bomb BEFORE `yaml.dump` materializes
    # its (alias-free) expansion. The `_MAX_YAML_BYTES` check below is a
    # post-hoc backstop — it only fires after the whole expanded string is
    # already in memory — so on its own it neither bounds peak memory nor the
    # CPU spent expanding. This bounded walk is the actual guard. (Untrusted
    # metadata reaches here via every re-dump of loaded raw frontmatter:
    # store.tombstone / restore / rename_scope, migrate.migrate_origin_in_…)
    _guard_dump_expansion(post.metadata)
    yaml_text = yaml.dump(
        post.metadata,
        Dumper=_NoAliasDumper,
        default_flow_style=False,
        allow_unicode=True,
    ).strip()
    # Write-side mirror of the `loads` read cap. Without this, a write whose
    # serialized frontmatter exceeds `_MAX_YAML_BYTES` (e.g. many/long
    # `links` notes or `verified_paths`/`verified_commits` entries) succeeds
    # on disk but then fails to PARSE on every subsequent read — the store's
    # malformed-file skip silently drops the record from search/list/show/
    # health while the write reported committed. Catching it here, at the one
    # chokepoint every persist routes through, turns that silent permanent
    # data loss into a clean ValueError (surfaced as a structured tool error)
    # for ALL frontmatter fields, present and future — not field-by-field.
    yaml_bytes = len(yaml_text.encode("utf-8"))
    if yaml_bytes > _MAX_YAML_BYTES:
        raise ValueError(
            f"frontmatter YAML exceeds {_MAX_YAML_BYTES}-byte cap "
            f"({yaml_bytes} bytes); refusing to write — a file this large "
            "would be rejected on read, silently dropping the record"
        )
    # Additional, tighter YAML ceiling, opted into per call. This fires ONLY
    # when a caller passes a reduced `max_yaml_bytes` (strictly under the flat
    # cap checked above), so it never weakens the flat enforcement, and a caller
    # that passes nothing skips it entirely. It gives the frontmatter-YAML axis
    # the removal-budget reservation the file axis already has, on BOTH of that
    # axis's arms: admission (`store._yaml_admission_cap`, mirroring
    # `_MAX_WRITE_BYTES`) and lifecycle re-dump
    # (`store._lifecycle_redump_yaml_cap`, mirroring `store._lifecycle_redump_cap`).
    # Either way, a record cannot be grown to within the removal-metadata budget
    # of `_MAX_YAML_BYTES` and stranded un-removable — its tombstone re-dump
    # could not fit the `removed:` metadata under the flat YAML cap even after
    # the adaptive trim. The message below says "lifecycle" because that arm
    # shipped first (dde864f); the admission arm (645d909) raises it too, so a
    # `memory_update` that over-fills `links` sees it on a content-admitting
    # write rather than on a re-dump.
    if max_yaml_bytes < _MAX_YAML_BYTES and yaml_bytes > max_yaml_bytes:
        raise ValueError(
            f"frontmatter YAML exceeds {max_yaml_bytes}-byte lifecycle cap "
            f"({yaml_bytes} bytes); refusing to grow — this re-dump would leave "
            "no room for the record's own removal metadata, making it "
            "un-removable. Shrink the frontmatter (verified_paths / scopes)."
        )
    # Normalise CRLF to LF, the missing half of the pair `loads` has always
    # had. `loads` strips the CR off every line so a Windows-authored or
    # `core.autocrlf`-checked-out file reads the same everywhere; writing the
    # body verbatim meant a CRLF body reached disk with its CRs while every
    # reader returned it without them. The file and its readers disagreed —
    # `alpha\r\nbeta` on disk, `alpha\nbeta` from `memory_show` — until the
    # first tombstone / restore / rename / update re-dump silently made the
    # readers' version permanent. Normalising here makes the two agree at
    # every step instead of eventually.
    #
    # Applied as the SAME per-line `rstrip("\r")` `loads` performs, not as a
    # `replace("\r\n", "\n")`: the output has to be a fixed point of the read
    # normalisation, or the disagreement just moves one carriage return
    # deeper. `alpha\r\r\nbeta` under a plain replace lands `alpha\r\nbeta` on
    # disk, which `loads` then reads back as `alpha\nbeta` — the identical
    # bug. Mirroring the operation exactly means disk equals reader for every
    # input, with no shape left over.
    #
    # A lone `\r` not followed by a newline survives both sides untouched: it
    # is content, not a line terminator this format knows about. Like the
    # `rstrip` below, this normalises PRESENTATION, not content.
    body = normalise_body(post.content)
    final = f"{_DELIM}\n{yaml_text}\n{_DELIM}\n\n{body}"
    # Total-file cap: the `_MAX_YAML_BYTES` check above bounds only the
    # frontmatter region, but `load` rejects the WHOLE file (frontmatter +
    # body) against `_MAX_FILE_BYTES` via `bounded_read`'s stat check. A
    # legal-sized frontmatter (dense `verified_paths` etc.) plus a body near
    # the handler-boundary `max_content_bytes` cap (config-tunable, default
    # 1 MB) can push the serialized total over that read cap — whereupon the
    # store's malformed-file skip silently drops the record from every read
    # surface (load_all / load_one / search / health) while the write reported
    # committed. The byte count here matches exactly what `bounded_read` will
    # measure on read: store.py / episodes.py both persist
    # `dumps(post).encode("utf-8")`, i.e. this same string. Catching it at the
    # one chokepoint every persist routes through turns that silent permanent
    # data loss into a clean ValueError for body+frontmatter combined and ALL
    # fields, present and future — the write-side mirror of the file cap.
    # `max_file_bytes` defaults to `_MAX_WRITE_BYTES` (read cap minus
    # maintenance headroom) for content-admitting writes so the record stays
    # tombstoneable; the lifecycle re-dump paths pass the full `_MAX_FILE_BYTES`
    # to allow that bounded growth up to (but never past) the read cap.
    final_bytes = len(final.encode("utf-8"))
    if final_bytes > max_file_bytes:
        raise ValueError(
            f"serialized frontmatter file exceeds {max_file_bytes}-byte cap "
            f"({final_bytes} bytes); refusing to write — a file this large "
            "would be rejected on read, silently dropping the record"
        )
    return final


__all__ = ["Post", "load", "loads", "dumps"]
