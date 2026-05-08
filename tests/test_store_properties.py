"""Property-based tests for `Store` invariants.

Example-based tests pin specific scenarios. These tests pin
*invariants* — properties that must hold regardless of input — and
let hypothesis generate the inputs. The goal isn't to find the same
bugs the example-based tests already cover; it's to find the bugs
that show up only under inputs nobody thought to write by hand
(weird Unicode, empty-or-near-empty bodies, scope shapes that pass
the regex but stress the formatter, etc.).

Invariants under test:

1. **Write round-trip identity.** A memory written with body `b`
   and scopes `s` reads back with the same body and scopes (modulo
   the trailing-newline normalization Store applies).

2. **Update preserves identity but bumps `updated`.** After
   `update()`, `id` and `created` are unchanged, `updated` is
   newer (or equal — same-microsecond write is allowed), the new
   body is what we asked for.

3. **Tombstone-then-restore is body-preserving.** Write, tombstone,
   restore. The restored memory has the same body and `created` /
   `updated` / `last_verified_at` it had before tombstoning. The
   tombstone path does not modify the body.

4. **`mark_verified` is idempotent and monotonic.** Calling it
   twice in a row succeeds; the second call's `last_verified_at`
   is `>=` the first's.

5. **`load_all` is order-deterministic.** Two consecutive
   `load_all()` calls return the same IDs in the same order.

6. **Independent writes don't pollute.** Writing memory B doesn't
   mutate memory A on disk. The atomic-rename in `_write_path`
   should guarantee this; we cross-check under random input.

Each test mints a fresh per-example subdir under `tmp_path`. We do
this rather than reuse `tmp_path` directly because hypothesis
re-uses the same fixture instance across examples — the
order-deterministic and pollution invariants would otherwise count
or compare against memories written by earlier examples, which is
not what we're checking.

`max_examples=10` is deliberately low. Each example performs real
disk I/O (write + load + sometimes tombstone + restore); the point
is *breadth of input space*, not exhaustive enumeration.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from hypothesis import HealthCheck, given, settings, strategies as st

from bettermemory.store import Store


def _fresh_root(tmp_path: Path) -> Path:
    """Per-example subdir under the test's tmp_path. Hypothesis re-uses
    the same fixture across examples; this makes each example's disk
    state independent."""
    root = tmp_path / f"ex_{uuid.uuid4().hex[:12]}"
    root.mkdir()
    return root


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------


# Bodies: any non-empty unicode text up to 200 chars. We don't filter
# transient markers — Store.write doesn't run the durability gate
# (that's the server's tool layer). Empty / whitespace-only bodies are
# legal at the store level too, so we don't restrict to printable text.
# Excluded: surrogates (frontmatter UTF-8 round-trip would fail) and
# the carriage-return / line-separator family (frontmatter strips them
# unpredictably and the resulting equality check would be unreliable).
_FORBIDDEN_CODEPOINTS = {
    "\r",
    "\v",
    "\f",
    "\x1c",  # FS
    "\x1d",  # GS
    "\x1e",  # RS
    "\x85",  # NEL
    " ",  # line separator
    " ",  # paragraph separator
}


def _printable_char(c: str) -> bool:
    if c in _FORBIDDEN_CODEPOINTS:
        return False
    if 0xD800 <= ord(c) <= 0xDFFF:  # surrogates
        return False
    return True


bodies = st.text(min_size=1, max_size=200).filter(
    lambda s: s.strip() and all(_printable_char(c) for c in s)
)


# Scopes: must match `^[a-z0-9]+(?:[-:][a-z0-9]+)*$`. Build by
# alternation of segments separated by `-` or `:`.
_segment = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789", min_size=1, max_size=8
)


def _scope_strategy() -> st.SearchStrategy[str]:
    return st.builds(
        lambda first, rest: first + "".join(rest),
        _segment,
        st.lists(
            st.tuples(st.sampled_from(["-", ":"]), _segment),
            min_size=0,
            max_size=3,
        ).map(lambda pairs: ["".join(p) for p in pairs]),
    )


scopes = st.lists(_scope_strategy(), min_size=1, max_size=4, unique=True)


# ---------------------------------------------------------------------------
# Invariant 1: write round-trip
# ---------------------------------------------------------------------------


@settings(
    max_examples=10,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(body=bodies, scope_list=scopes)
def test_write_roundtrip_preserves_body_and_scopes(
    tmp_path: Path, body: str, scope_list: list[str]
) -> None:
    store = Store(_fresh_root(tmp_path))
    memory = store.write(content=body, scopes=scope_list)

    loaded = store.load_one(memory.id)
    # Store strips trailing whitespace and re-adds one trailing newline.
    # Compare against the same normalization on the input.
    expected_body = body.strip() + "\n"
    assert loaded.body == expected_body
    assert loaded.scopes == scope_list
    assert loaded.id == memory.id
    assert loaded.created == memory.created


# ---------------------------------------------------------------------------
# Invariant 2: update preserves identity, bumps `updated`
# ---------------------------------------------------------------------------


@settings(
    max_examples=10,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(initial=bodies, refined=bodies, scope_list=scopes)
def test_update_preserves_id_created_bumps_updated(
    tmp_path: Path, initial: str, refined: str, scope_list: list[str]
) -> None:
    store = Store(_fresh_root(tmp_path))
    memory = store.write(content=initial, scopes=scope_list)
    bumped = memory.model_copy(update={"body": refined.strip() + "\n"})
    updated_memory = store.update(bumped)

    loaded = store.load_one(memory.id)
    assert loaded.id == memory.id
    assert loaded.created == memory.created
    # `updated` should not move backwards. Same-microsecond writes are
    # allowed (the OS clock resolution is what it is).
    assert loaded.updated >= memory.updated
    assert updated_memory.updated == loaded.updated
    assert loaded.body == refined.strip() + "\n"


# ---------------------------------------------------------------------------
# Invariant 3: tombstone-then-restore is body-preserving
# ---------------------------------------------------------------------------


@settings(
    max_examples=10,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(body=bodies, scope_list=scopes)
def test_tombstone_then_restore_preserves_everything(
    tmp_path: Path, body: str, scope_list: list[str]
) -> None:
    store = Store(_fresh_root(tmp_path))
    memory = store.write(content=body, scopes=scope_list)

    pre_body = memory.body
    pre_created = memory.created
    pre_updated = memory.updated
    pre_verified = memory.last_verified_at

    store.tombstone(memory.id, reason="property test")
    restored = store.restore(memory.id)

    # Body and timestamps survive the round-trip.
    assert restored.body == pre_body
    assert restored.created == pre_created
    assert restored.updated == pre_updated
    assert restored.last_verified_at == pre_verified
    # Scopes too.
    assert restored.scopes == scope_list


# ---------------------------------------------------------------------------
# Invariant 4: `mark_verified` is idempotent + monotonic
# ---------------------------------------------------------------------------


@settings(
    max_examples=10,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(body=bodies, scope_list=scopes)
def test_mark_verified_monotonic(
    tmp_path: Path, body: str, scope_list: list[str]
) -> None:
    store = Store(_fresh_root(tmp_path))
    memory = store.write(content=body, scopes=scope_list)
    first = store.mark_verified(memory.id)
    second = store.mark_verified(memory.id)
    assert first.last_verified_at is not None
    assert second.last_verified_at is not None
    # Time can advance or stay equal (same-microsecond clock resolution
    # on fast machines), but never go backwards.
    assert second.last_verified_at >= first.last_verified_at
    # The value persists across reloads — not just a transient field
    # in the returned dataclass.
    reloaded = store.load_one(memory.id)
    assert reloaded.last_verified_at == second.last_verified_at


# ---------------------------------------------------------------------------
# Invariant 5: `load_all` is order-deterministic
# ---------------------------------------------------------------------------


@settings(
    max_examples=10,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    bodies_list=st.lists(bodies, min_size=2, max_size=8, unique=True),
    scope_list=scopes,
)
def test_load_all_is_order_deterministic(
    tmp_path: Path, bodies_list: list[str], scope_list: list[str]
) -> None:
    store = Store(_fresh_root(tmp_path))
    for body in bodies_list:
        store.write(content=body, scopes=scope_list)

    first_ids = [m.id for m in store.load_all()]
    second_ids = [m.id for m in store.load_all()]
    assert first_ids == second_ids
    assert len(first_ids) == len(bodies_list)
    # IDs are unique across our writes.
    assert len(set(first_ids)) == len(first_ids)


# ---------------------------------------------------------------------------
# Invariant 6: write does not pollute foreign memories
# ---------------------------------------------------------------------------


@settings(
    max_examples=10,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    body_a=bodies,
    body_b=bodies,
    scopes_a=scopes,
    scopes_b=scopes,
)
def test_independent_writes_do_not_pollute(
    tmp_path: Path,
    body_a: str,
    body_b: str,
    scopes_a: list[str],
    scopes_b: list[str],
) -> None:
    """Writing memory B doesn't mutate memory A on disk. (The atomic
    rename should guarantee this; the property test cross-checks under
    random input.)"""
    store = Store(_fresh_root(tmp_path))
    a = store.write(content=body_a, scopes=scopes_a)
    pre_a_loaded = store.load_one(a.id)
    store.write(content=body_b, scopes=scopes_b)
    post_a_loaded = store.load_one(a.id)

    assert pre_a_loaded.body == post_a_loaded.body
    assert pre_a_loaded.scopes == post_a_loaded.scopes
    assert pre_a_loaded.created == post_a_loaded.created
    assert pre_a_loaded.updated == post_a_loaded.updated
