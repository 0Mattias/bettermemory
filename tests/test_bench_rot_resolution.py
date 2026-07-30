"""Tests for the resolution probe — the module that decides whether this
project may claim a real-world staleness-accuracy number.

`bench/rot/resolution.py` exists to establish a NEGATIVE: that the second
factor of `real_world_J <= J_resolved x resolution_rate` cannot be
measured yet, because the claim reader is a corpus-format reader rather
than a claim extractor. A negative result is only worth anything while
three properties hold, and all three are the kind that rot silently:

- the control passes, so a zero on real bodies is attributable to body
  shape rather than to a parser that matches nothing at all;
- the zero on real bodies is STRUCTURAL — the templates are anchored
  full-string sentences, so loosening an anchor would start manufacturing
  a real-world accuracy number out of generated-sentence machinery;
- the published status stays UNDEFINED rather than collapsing to a
  measured `0.0`. That is the same None-vs-zero discipline the verdict fix
  was about: "could not ask" and "asked, got zero" are different facts,
  and only one of them may be multiplied into a published claim.

So the tests below are written adversarially in the same register as
`tests/test_bench_claims.py`: the interesting cases are the ones that must
NOT parse, and each pin comes with its own demonstration that it can fail
(a stubbed parser drives the control to zero; a single generated body
drives the real-body count off zero).

Everything is hermetic. `bench/rot/results/resolution.json` records the
absolute store path of the machine that produced it; that path is read as
data and never touched, and no test here needs a live memory store.

`bench/` is not a package and is not on the import path, so the module is
loaded by file location the same way a bench run would execute it.
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_PROBE = _ROOT / "bench" / "rot" / "resolution.py"
_ARTIFACT = _ROOT / "bench" / "rot" / "results" / "resolution.json"

# The load-bearing keys. Named here rather than compared as a whole key set
# so that adding a counter to the probe does not fail a test whose subject
# is the honest-status contract.
_STATUS_KEYS = frozenset(
    {
        "parsed",
        "parse_rate",
        "control_parsed",
        "control_total",
        "resolution_rate",
        "resolution_rate_status",
    }
)


def _load() -> ModuleType:
    # Registered under its own name: the probe itself puts run.py in
    # sys.modules under "bench_rot_run", and the sibling suites register
    # run.py under keys of their own. Distinct keys keep the module objects
    # from stepping on each other.
    spec = importlib.util.spec_from_file_location("bench_rot_resolution", _PROBE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["bench_rot_resolution"] = module
    spec.loader.exec_module(module)
    return module


resolution = _load()
rot = resolution.rot

_CITED = "src/bettermemory/store.py"

# Bodies shaped like real memories rather than like generated claims. The
# last six are adversarial: each one CONTAINS a generated template, wrapped
# in the prose a human would write around it. They are the cases a loosened
# anchor would start "parsing", and the reason the zero below is a
# structural property instead of a lucky corpus.
_PROSE_BODIES = (
    "The store lives at `~/.claude-memory`; `memory_write` stages a "
    "`user-inference` write as pending until the user confirms it.",
    "Retrieval is opt-in — call `memory_search` only when the user "
    "references shared context.",
    "Run `uv run pytest -q` before every commit; both legs.",
    '`search_mode = "hybrid"` is the shipped default and the semantic leg '
    "stays off unless an extra is installed.",
    "The user prefers code-driven tutorials over prose walkthroughs.",
    "Two things landed today.\n\nThe module `src/bettermemory/verify.py` is "
    "part of this package.\n\nThat is the file to read first.",
    f"We confirmed the module `{_CITED}` is part of this package.",
    f"The module `{_CITED}` is part of this package. Nothing else changed.",
    f"Note that `write` is defined at the top level of `{_CITED}`.",
    f"`write` is defined at the top level of `{_CITED}`. It always was.",
    f"Reminder: `LIMIT` in `{_CITED}` is set to `30`.",
    f"`LIMIT` in `{_CITED}` is set to `30`. That is the current value.",
)


def _seed(store: Path, bodies: tuple[str, ...]) -> None:
    for index, body in enumerate(bodies, 1):
        (store / f"2026-01-0{index % 9 + 1}-seed-{index:02d}.md").write_text(
            f"---\nid: SEED{index:02d}\nscopes:\n  - projects:bettermemory\n"
            f"---\n\n{body}\n",
            encoding="utf-8",
        )


def _emit_json(
    store: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> tuple[str, dict]:
    monkeypatch.setattr(
        sys,
        "argv",
        ["resolution.py", "--store", str(store), "--repo-root", str(_ROOT), "--json"],
    )
    assert resolution.main() == 0
    raw = capsys.readouterr().out
    return raw, json.loads(raw)


# ---------------------------------------------------------------------------
# 1. The synthetic control — without it the real-body zero is uninterpretable
# ---------------------------------------------------------------------------


def test_the_control_parses_the_generators_own_three_sentence_forms() -> None:
    """The control is only a control while it tracks what the generator
    actually emits.

    Its samples are hard-coded strings, so they can drift away from the
    renderer they are supposed to stand in for — and a drifted control that
    still returns 3/3 would keep vouching for a parser that no longer reads
    the benchmark's own output. So the templates are re-derived from the
    renderer here, in both citation styles, and the control's own source is
    checked to still exercise all three arms.
    """
    for root in (None, _ROOT):
        for claim, expected in (
            (rot.Claim("path", _CITED, _CITED, ""), "path"),
            (rot.Claim("symbol", _CITED, "write", ""), "symbol"),
            (rot.Claim("literal", _CITED, "LIMIT", "30"), "literal"),
        ):
            cite = rot.parse_claim_citation(claim.body(root), _ROOT)
            assert cite is not None, f"{expected} body not recoverable, root={root}"
            assert (cite.kind, cite.rel_path) == (expected, _CITED)
            assert (cite.name, cite.value) == (claim.name, claim.value)

    assert resolution.synthetic_control() == (3, 3)

    source = inspect.getsource(resolution.synthetic_control)
    for phrase in (
        "is part of this package",
        "is defined at the top level of",
        "is set to",
    ):
        assert phrase in source, (
            f"the control stopped exercising the {phrase!r} arm; a 3/3 that "
            "covers two templates vouches for less than it appears to"
        )


def test_the_control_reports_zero_when_the_parser_stops_working() -> None:
    """The failure demonstration for the control itself.

    A hard-coded `return 3, 3` would satisfy every other test in this file.
    Stub the parser out and the control has to collapse — otherwise the 3/3
    in the committed artifact is decoration rather than evidence.
    """
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(rot, "parse_claim_citation", lambda *a, **k: None)
        assert resolution.synthetic_control() == (0, 3)


# ---------------------------------------------------------------------------
# 2. Zero on real bodies, by construction
# ---------------------------------------------------------------------------


def test_no_real_shaped_memory_body_parses(tmp_path: Path) -> None:
    """The 0-of-N result, reproduced hermetically.

    Half this corpus embeds a generated template inside ordinary prose, so
    the zero is not "our synthetic bodies happened to look nothing like a
    claim" — it is the two-sided anchoring refusing sentences it can see.
    A regression that loosens an anchor starts reporting a real-world
    resolution rate computed by a corpus-format reader, which is precisely
    the false claim the probe was written to prevent.
    """
    _seed(tmp_path, _PROSE_BODIES)
    r = resolution.run(tmp_path, _ROOT)

    assert r.total == len(_PROSE_BODIES)
    assert r.parsed == 0
    assert r.unparsed == r.total
    assert r.by_kind == {}
    assert r.parse_rate == 0.0
    # The corpus has plenty to chew on, so the zero cannot be blamed on a
    # corpus with no literals in it.
    assert r.checkable >= 8
    assert r.bare >= 1
    assert r.checkable + r.bare == r.total
    assert r.checkable_but_unparsed == r.checkable
    assert r.checkable_rate > 0.5


def test_the_zero_is_a_property_of_body_shape_not_a_dead_counter(
    tmp_path: Path,
) -> None:
    """The failure demonstration for the count above.

    Same corpus, plus one body the generator would emit. If the parsed
    counter cannot move, the zero next door measures nothing at all — it
    would be the "pipeline structurally produces no output, mistaken for a
    subject performing badly" error that the probe's whole argument rests
    on avoiding.
    """
    generated = rot.Claim("path", _CITED, _CITED, "").body(None)
    _seed(tmp_path, (*_PROSE_BODIES, generated))
    r = resolution.run(tmp_path, _ROOT)

    assert r.total == len(_PROSE_BODIES) + 1
    assert r.parsed == 1
    assert r.by_kind == {"path": 1}
    assert r.unparsed == len(_PROSE_BODIES)


@pytest.mark.parametrize(
    "body",
    [
        f"We confirmed the module `{_CITED}` is part of this package.",
        f"The module `{_CITED}` is part of this package. Nothing else changed.",
        f"Note that `write` is defined at the top level of `{_CITED}`.",
        f"`write` is defined at the top level of `{_CITED}`. It always was.",
        f"Reminder: `LIMIT` in `{_CITED}` is set to `30`.",
        f"`LIMIT` in `{_CITED}` is set to `30`. That is the current value.",
    ],
)
def test_a_template_wrapped_in_prose_is_refused(body: str) -> None:
    """Both anchors, checked by behaviour rather than by reading the regex.

    A human writing down the same fact adds a lead-in or a following
    sentence. Each body here carries a verbatim template plus exactly that,
    and each must be refused — the templates are full-string matches, not
    searches.
    """
    assert rot.parse_claim_citation(body, _ROOT) is None


def test_prose_never_yields_a_clean_literal_citation() -> None:
    """The one arm whose anchoring is not airtight, pinned honestly.

    The literal template compiles with DOTALL so a value may span lines,
    which lets its value group swallow a trailing backticked span when a
    body both opens with the template and closes with a backticked token.
    The property that actually matters is weaker than "always refused" and
    survives a future tightening: whatever comes back out of prose must not
    look like a clean citation, so nothing downstream can bank it as a
    resolved claim.
    """
    body = f"`LIMIT` in `{_CITED}` is set to `30`. See also `notes`."
    cite = rot.parse_claim_citation(body, _ROOT)
    assert cite is None or "`" in cite.value, (
        "the literal arm extracted a clean value out of prose; a real body "
        "would now count as a resolved citation"
    )


# ---------------------------------------------------------------------------
# 3. resolution_rate is UNDEFINED, not zero
# ---------------------------------------------------------------------------


def test_the_json_reports_resolution_rate_as_null_not_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """The distinction the whole module exists to protect.

    `parse_rate` is a measured zero and is reported as `0.0`.
    `resolution_rate` is a quantity nobody has been able to ask for, and it
    is reported as null. Flipping the second one to a float would licence
    the multiplication and publish a false accuracy claim about this
    project's own product, so the raw serialization is checked as text —
    `0.0` and `null` are indistinguishable to a truthiness test.
    """
    _seed(tmp_path, _PROSE_BODIES)
    raw, payload = _emit_json(tmp_path, monkeypatch, capsys)

    assert '"resolution_rate": null' in raw
    assert payload["resolution_rate"] is None
    assert not isinstance(payload["resolution_rate"], (int, float))
    assert "UNDEFINED" in payload["resolution_rate_status"]

    assert payload["parsed"] == 0
    assert payload["parse_rate"] == 0.0
    assert payload["control_parsed"] == payload["control_total"] == 3
    assert _STATUS_KEYS <= set(payload)


def test_an_empty_store_does_not_downgrade_undefined_to_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """Zero denominators are where "could not ask" turns into "measured
    zero" by accident. With nothing to read at all, the status must still
    be undefined rather than a number a scorecard could multiply."""
    raw, payload = _emit_json(tmp_path, monkeypatch, capsys)

    assert payload["total"] == 0
    assert '"resolution_rate": null' in raw
    assert payload["resolution_rate"] is None
    assert "UNDEFINED" in payload["resolution_rate_status"]


def test_the_committed_artifact_records_undefined_not_zero() -> None:
    """The published artifact has to carry the same semantics as the code.

    Read as data only: the recorded store path belongs to the machine that
    produced the file, and this test deliberately never stats it — a guard
    that needs the author's home directory is a guard that only runs on one
    laptop.
    """
    raw = _ARTIFACT.read_text(encoding="utf-8")
    data = json.loads(raw)

    assert '"resolution_rate": null' in raw
    assert data["resolution_rate"] is None
    assert "UNDEFINED" in data["resolution_rate_status"]
    assert _STATUS_KEYS <= set(data)

    # The two numbers that make the undefined verdict readable: the parser
    # works, and it read nothing.
    assert data["control_parsed"] == data["control_total"] == 3
    assert data["parsed"] == 0
    assert data["parse_rate"] == 0.0
    assert data["by_kind"] == {}
    assert data["checkable_but_unparsed"] == data["checkable"] > 0
    assert data["unparsed"] == data["total"] > 0


# ---------------------------------------------------------------------------
# 4. Privacy — the result file is meant to be publishable verbatim
# ---------------------------------------------------------------------------


def test_the_resolution_report_holds_no_memory_content(tmp_path: Path) -> None:
    """Counters only, pinned structurally rather than by trusting the
    renderer. The probe reads every body in a personal store, so a leak
    here would be published the next time a result file is committed."""
    (tmp_path / "2026-01-01-secret-01.md").write_text(
        "---\nid: S\nscopes:\n  - private-scope\n---\n\nSENSITIVE `src/a.py`.\n",
        encoding="utf-8",
    )
    r = resolution.run(tmp_path, _ROOT)
    rendered = repr(r) + resolution._format_text(r, tmp_path, (3, 3))

    assert "SENSITIVE" not in rendered
    assert "private-scope" not in rendered
    assert "secret" not in rendered
