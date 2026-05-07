"""Tests for the vendored frontmatter parser.

Originally `python-frontmatter`, vendored as `bettermemory._frontmatter` so we
can drop the dependency (it calls deprecated `codecs.open()` on 3.14) and pin
pure-Python YAML directly. Tests here document the contract that
`store.py` relies on; the cross-checks against the upstream library were
done at vendor time.
"""

from __future__ import annotations

import pytest

from bettermemory._frontmatter import Post, dumps, load, loads


# ---------------------------------------------------------------------------
# loads
# ---------------------------------------------------------------------------


def test_loads_round_trip() -> None:
    text = "---\nid: x\nscopes:\n- tools\n---\n\nbody\n"
    p = loads(text)
    assert p.metadata == {"id": "x", "scopes": ["tools"]}
    assert p.content == "body\n"


def test_loads_empty_string() -> None:
    p = loads("")
    assert p.metadata == {} and p.content == ""


def test_loads_no_frontmatter_treats_all_as_body() -> None:
    p = loads("just a plain markdown body\n")
    assert p.metadata == {}
    assert p.content == "just a plain markdown body\n"


def test_loads_unclosed_frontmatter_treats_as_body() -> None:
    p = loads("---\nid: x\nbody without closing delim")
    assert p.metadata == {}
    assert p.content == "---\nid: x\nbody without closing delim"


def test_loads_empty_metadata() -> None:
    p = loads("---\n---\n\nbody\n")
    assert p.metadata == {}
    assert p.content == "body\n"


def test_loads_triple_dash_inside_quoted_string_is_not_a_close() -> None:
    text = "---\nid: x\nreason: 'has --- inside'\n---\n\nbody --- with delim\n"
    p = loads(text)
    assert p.metadata == {"id": "x", "reason": "has --- inside"}
    assert p.content == "body --- with delim\n"


def test_loads_rejects_non_mapping_metadata() -> None:
    with pytest.raises(ValueError):
        loads("---\n[1, 2, 3]\n---\n\nbody")


def test_loads_dash_dash_dash_not_followed_by_newline_is_not_frontmatter() -> None:
    # Some markdown legitimately starts with `---something`. Don't eat that.
    p = loads("---something\nbody\n")
    assert p.metadata == {}
    assert "---something" in p.content


def test_loads_handles_crlf_line_endings() -> None:
    text = "---\r\nid: x\r\n---\r\n\r\nbody line\r\n"
    p = loads(text)
    assert p.metadata == {"id": "x"}
    assert "body line" in p.content


# ---------------------------------------------------------------------------
# dumps
# ---------------------------------------------------------------------------


def test_dumps_basic_shape() -> None:
    out = dumps(Post(content="body\n", metadata={"id": "x"}))
    assert out == "---\nid: x\n---\n\nbody"


def test_dumps_strips_only_trailing_whitespace_from_body() -> None:
    out = dumps(Post(content="  leading spaces preserved\n\n", metadata={"k": 1}))
    assert out.endswith("\n\n  leading spaces preserved")


def test_dumps_uses_block_style_for_lists() -> None:
    out = dumps(Post(content="b", metadata={"scopes": ["a", "b", "c"]}))
    assert "scopes:\n- a\n- b\n- c" in out


def test_dumps_handles_unicode() -> None:
    out = dumps(Post(content="hi", metadata={"note": "αβγ ✓ こんにちは"}))
    assert "αβγ ✓ こんにちは" in out


def test_dumps_emits_literal_values_not_yaml_aliases() -> None:
    """When two metadata fields share the same object — e.g. `created` and
    `updated` on a fresh write, both pointing at the same `utcnow()` value —
    pyyaml's default behavior is to emit `&id001` / `*id001` anchor/alias
    syntax. We override that: literal repetition is more human-readable for
    a directory the user is expected to grep and read by eye.
    """
    from datetime import datetime, timezone

    ts = datetime(2026, 5, 7, 20, 56, 8, 199145, tzinfo=timezone.utc)
    out = dumps(Post(content="body", metadata={"created": ts, "updated": ts}))

    assert "&id" not in out
    assert "*id" not in out
    # Both timestamps should appear literally.
    assert out.count("2026-05-07") == 2

    # And the result must still round-trip through `loads`.
    parsed = loads(out)
    assert parsed.metadata["created"] == ts
    assert parsed.metadata["updated"] == ts


# ---------------------------------------------------------------------------
# load (file-based)
# ---------------------------------------------------------------------------


def test_load_reads_file(tmp_path) -> None:  # type: ignore[no-untyped-def]
    p = tmp_path / "memo.md"
    p.write_text("---\nid: y\n---\n\nfile body\n", encoding="utf-8")
    post = load(p)
    assert post.metadata == {"id": "y"}
    assert post.content == "file body\n"


def test_load_round_trip_through_disk(tmp_path) -> None:  # type: ignore[no-untyped-def]
    p = tmp_path / "memo.md"
    original = Post(content="body line\n", metadata={"id": "z", "tag": "ok"})
    p.write_text(dumps(original), encoding="utf-8")
    parsed = load(p)
    assert parsed.metadata == original.metadata
    # `dumps` rstrips the body; `loads` returns it as-is.
    assert parsed.content == original.content.rstrip()


# ---------------------------------------------------------------------------
# Compatibility with files written by the previous python-frontmatter impl
# ---------------------------------------------------------------------------


def test_loads_existing_example_file() -> None:
    """The example memories shipped with the repo were authored by hand or
    by python-frontmatter — make sure the vendored parser reads them."""
    from pathlib import Path

    example = Path(__file__).resolve().parents[1] / "examples" / "memories" / "2025-03-14-tutorial-style.md"
    p = load(example)
    assert "id" in p.metadata
    assert isinstance(p.metadata.get("scopes"), list)
    assert p.content.strip()
