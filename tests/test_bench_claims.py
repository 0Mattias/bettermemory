"""Adversarial tests for `bench/claims.py`'s claim classifier.

The census this backs is meant to be a *publishable* number bounding what
the verification layer can speak to. A classifier that silently over- or
under-counts turns that number into decoration, and the failure would be
invisible — the output is aggregates, so nothing downstream can notice a
misclassification the way a wrong path would surface as a broken link.

So the classifier gets its own suite, and it is written adversarially:
the interesting cases are the ones that SHOULD NOT count. A detector that
only proves it catches an obvious path has proved nothing about the
number it produces.

`bench/` is not a package and is not on the import path, so the module is
loaded by file location the same way a bench run would execute it.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

_BENCH = Path(__file__).resolve().parents[1] / "bench" / "claims.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("bench_claims", _BENCH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["bench_claims"] = module
    spec.loader.exec_module(module)
    return module


claims = _load()


# ---------------------------------------------------------------------------
# What must be counted
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("cites `src/bettermemory/store.py` here", "path"),
        ("a bare dir `bench/` counts as a path", "path"),
        ("`pyproject.toml` by extension alone", "path"),
        ("`~/.claude-memory` is a path", "path"),
        ("run `git log --oneline`", "command"),
        ("run `uv pip install -e .`", "command"),
        ("`patterns.clusterable_episodes` is dotted", "symbol"),
        ("`load_all()` is called", "symbol"),
        ("`_private_helper` leads with an underscore", "symbol"),
        ("`SCHEMA_VERSION` is a constant", "symbol"),
        ("`verified_paths` is bare snake_case", "symbol"),
        ('`search_mode = "hybrid"` is config', "config"),
        ("`[behavior]` is a config section", "config"),
    ],
)
def test_counts_the_literal_shapes_it_claims_to(body: str, expected: str) -> None:
    assert expected in claims.classify_body(body)


def test_snake_case_arm_is_the_one_that_regressed() -> None:
    """The arm added after a real-store run: a memory citing `verified_paths`
    and `bench/` scored zero checkable classes under the original rule, which
    required a dot, parens, a leading underscore, or ALL-CAPS. Bare
    snake_case is the most common literal in this corpus."""
    found = claims.classify_body("`verified_paths` and `bench/` and `silent_miss_rate`")
    assert "symbol" in found
    assert "path" in found


# ---------------------------------------------------------------------------
# What must NOT be counted — the half that keeps the number honest
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        "prose mentioning either/or without backticks",
        "an unbackticked path src/bettermemory/store.py in prose",
        "unbackticked run git log --oneline",
        "`the`",  # backticked English, too short and not identifier-shaped
        "`a plain english sentence in backticks`",
        "no backticks at all, just a stated preference",
        "",
    ],
)
def test_does_not_count_prose(body: str) -> None:
    assert claims.classify_body(body) & set(claims.CHECKABLE_CLASSES) == set()


def test_version_and_sha_are_tracked_but_not_checkable() -> None:
    """They date a claim rather than assert one about the world. Folding
    them into the headline would inflate the number the census exists to
    bound, so they are counted separately and excluded."""
    found = claims.classify_body("shipped in `3.29.0` at `be6e012`")
    assert found & {"version", "sha"}
    assert found & set(claims.CHECKABLE_CLASSES) == set()


def test_command_wins_over_symbol_for_the_same_span() -> None:
    """`git log` would also satisfy a looser identifier rule; specificity
    order decides, and the census would double-count if it did not."""
    assert claims.classify_body("`git log`") == {"command"}


# ---------------------------------------------------------------------------
# Frontmatter/body split — the circularity guard
# ---------------------------------------------------------------------------


def test_attested_path_in_frontmatter_is_not_a_body_citation(tmp_path: Path) -> None:
    """If frontmatter counted as a citation, every attested memory would
    anchor by construction and the anchoring number would be a tautology."""
    memory = tmp_path / "2026-01-01-x-01.md"
    memory.write_text(
        "---\nid: X\nverified_paths:\n  - src/a.py\n---\n\nNo literal here.\n",
        encoding="utf-8",
    )
    census = claims.run_census(tmp_path)
    assert census.attested == 1
    assert census.anchor_none == 1
    assert census.anchor_full == 0
    assert census.attested_entries_cited == 0


def test_basename_only_citation_is_its_own_bucket(tmp_path: Path) -> None:
    memory = tmp_path / "2026-01-01-y-02.md"
    memory.write_text(
        "---\nid: Y\nverified_paths:\n  - src/deep/nested/a.py\n---\n\n"
        "Body says `a.py` only.\n",
        encoding="utf-8",
    )
    census = claims.run_census(tmp_path)
    assert (census.anchor_basename, census.anchor_full, census.anchor_none) == (1, 0, 0)


def test_unattested_memory_is_not_counted_as_attested(tmp_path: Path) -> None:
    (tmp_path / "2026-01-01-z-03.md").write_text(
        "---\nid: Z\nverified_paths: []\n---\n\nBody cites `src/a.py`.\n",
        encoding="utf-8",
    )
    census = claims.run_census(tmp_path)
    assert census.total == 1
    assert census.attested == 0
    assert census.checkable == 1


def test_census_holds_no_memory_content(tmp_path: Path) -> None:
    """The census output is meant to be publishable verbatim. That is only
    true while the counters cannot carry a body, a filename or a scope —
    so pin it structurally rather than trusting the renderer."""
    (tmp_path / "2026-01-01-secret-04.md").write_text(
        "---\nid: S\nscopes:\n  - private-scope\n---\n\nSENSITIVE `src/a.py`.\n",
        encoding="utf-8",
    )
    census = claims.run_census(tmp_path)
    rendered = repr(census) + claims._format_text(census, tmp_path)
    assert "SENSITIVE" not in rendered
    assert "private-scope" not in rendered
    assert "secret" not in rendered
