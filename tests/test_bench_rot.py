"""Adversarial tests for the rot benchmark's extractor and oracle.

The oracle IS the benchmark. If it mislabels, every downstream number is
noise — and the failure is silent, because the output is rates. A
benchmark that grades the project's headline mechanism has to be held to
a higher standard than the mechanism, so the interesting cases here are
the ones where a careless checker would get the label backwards:

- a pure reformat must NOT read as drift
- a moved-but-re-exported symbol MUST read as drift at its old site
- a changed literal MUST read as drift, including a same-type change
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_RUNNER = _ROOT / "bench" / "rot" / "run.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("bench_rot_run", _RUNNER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["bench_rot_run"] = module
    spec.loader.exec_module(module)
    return module


rot = _load()


def _tree(root: Path, rel: str, source: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


def test_extracts_the_three_claim_classes(tmp_path: Path) -> None:
    _tree(
        tmp_path,
        "src/pkg/mod.py",
        "TIMEOUT = 30\n\n\ndef handler():\n    pass\n\n\nclass Thing:\n    pass\n",
    )
    claims = rot.extract_claims(tmp_path, "src")
    kinds = {c.kind for c in claims}
    assert kinds == {"path", "symbol", "literal"}
    names = {(c.kind, c.name) for c in claims}
    assert ("symbol", "handler") in names
    assert ("symbol", "Thing") in names
    assert ("literal", "TIMEOUT") in names


def test_does_not_extract_non_literal_or_lowercase_assignments(tmp_path: Path) -> None:
    """A computed value is not a claim about the world — its 'value' would
    change with the environment, which would manufacture false drift."""
    _tree(
        tmp_path,
        "src/pkg/mod.py",
        "import os\n\nCOMPUTED = os.getpid()\nlowercase = 3\nDERIVED = [1, 2]\n",
    )
    literals = [c for c in rot.extract_claims(tmp_path, "src") if c.kind == "literal"]
    assert literals == []


def test_nested_definitions_are_not_top_level_claims(tmp_path: Path) -> None:
    """A method is not a top-level symbol. Claiming it at module level
    would be false at t0, and a benchmark whose claims start false grades
    nothing."""
    _tree(
        tmp_path,
        "src/pkg/mod.py",
        "class Outer:\n    def inner(self):\n        pass\n",
    )
    symbols = {
        c.name for c in rot.extract_claims(tmp_path, "src") if c.kind == "symbol"
    }
    assert symbols == {"Outer"}


def test_unparseable_file_still_yields_its_path_claim(tmp_path: Path) -> None:
    _tree(tmp_path, "src/pkg/broken.py", "def (:\n")
    claims = rot.extract_claims(tmp_path, "src")
    assert [c.kind for c in claims] == ["path"]


# ---------------------------------------------------------------------------
# Oracle — the half that decides whether any number means anything
# ---------------------------------------------------------------------------


def test_pure_reformat_is_not_drift(tmp_path: Path) -> None:
    """The single most important negative. If whitespace or comment churn
    read as drift, the base rate would inflate and precision would look
    far better than it is."""
    _tree(tmp_path, "src/pkg/mod.py", "TIMEOUT = 30\n\n\ndef handler():\n    pass\n")
    claims = rot.extract_claims(tmp_path, "src")

    t1 = tmp_path / "t1"
    _tree(
        t1,
        "src/pkg/mod.py",
        "# a new explanatory comment\n"
        "TIMEOUT  =  30\n\n\n"
        "def handler():\n"
        '    """Now documented."""\n'
        "    pass\n",
    )
    for claim in claims:
        assert rot.label_claim(claim, t1) == "still_true", claim


def test_deleted_file_falsifies_every_claim_about_it(tmp_path: Path) -> None:
    _tree(tmp_path, "src/pkg/mod.py", "X = 1\n\n\ndef gone():\n    pass\n")
    claims = rot.extract_claims(tmp_path, "src")
    empty = tmp_path / "t1"
    empty.mkdir()
    for claim in claims:
        assert rot.label_claim(claim, empty) == "false", claim


def test_renamed_symbol_is_drift_even_though_the_file_remains(tmp_path: Path) -> None:
    """This is the class `path_drift` structurally cannot see, so the
    oracle must catch it or the benchmark cannot show the blind spot."""
    _tree(tmp_path, "src/pkg/mod.py", "def old_name():\n    pass\n")
    claim = next(c for c in rot.extract_claims(tmp_path, "src") if c.kind == "symbol")
    t1 = tmp_path / "t1"
    _tree(t1, "src/pkg/mod.py", "def new_name():\n    pass\n")
    assert rot.label_claim(claim, t1) == "false"


def test_symbol_moved_away_but_reexported_still_reads_as_drift(tmp_path: Path) -> None:
    """The interesting case. An import makes the name reachable, but the
    claim was that it is DEFINED here, and that is no longer so. Labelling
    it true would let a real relocation pass unnoticed."""
    _tree(tmp_path, "src/pkg/mod.py", "def helper():\n    pass\n")
    claim = next(c for c in rot.extract_claims(tmp_path, "src") if c.kind == "symbol")
    t1 = tmp_path / "t1"
    _tree(t1, "src/pkg/mod.py", "from pkg.other import helper\n")
    assert rot.label_claim(claim, t1) == "false"


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("TIMEOUT = 30", "TIMEOUT = 60"),
        ('MODE = "hybrid"', 'MODE = "semantic"'),
        ("ENABLED = True", "ENABLED = False"),
        ("TIMEOUT = 30", "TIMEOUT = 30.0"),
    ],
)
def test_changed_literal_is_drift(tmp_path: Path, before: str, after: str) -> None:
    _tree(tmp_path, "src/pkg/mod.py", before + "\n")
    claim = next(c for c in rot.extract_claims(tmp_path, "src") if c.kind == "literal")
    t1 = tmp_path / "t1"
    _tree(t1, "src/pkg/mod.py", after + "\n")
    assert rot.label_claim(claim, t1) == "false"


def test_unchanged_literal_is_not_drift(tmp_path: Path) -> None:
    _tree(tmp_path, "src/pkg/mod.py", "TIMEOUT = 30\n")
    claim = next(c for c in rot.extract_claims(tmp_path, "src") if c.kind == "literal")
    t1 = tmp_path / "t1"
    _tree(t1, "src/pkg/mod.py", "OTHER = 1\nTIMEOUT = 30\n")
    assert rot.label_claim(claim, t1) == "still_true"


# ---------------------------------------------------------------------------
# Claim bodies must be visible to the thing being graded
# ---------------------------------------------------------------------------


def test_relative_citations_get_no_path_checking_at_all(tmp_path: Path) -> None:
    """A product property, pinned here because the benchmark cannot show it.

    `detect_path_drift` excludes relative paths BY DESIGN — verify.py's
    module docstring says so: without an anchor, checking them would mean
    checking the cwd at retrieval time. The consequence is easy to miss
    and worth stating plainly: a memory that cites `src/pkg/mod.py`, which
    is how a developer naturally writes it, receives **no** path-drift
    protection whatsoever. Only the commit-drift and calendar legs can
    ever fire for it.

    The rot benchmark runs both citation styles as separate arms, but on a
    repository with no deletions in the window they are indistinguishable,
    so the difference is pinned directly here instead.
    """
    from bettermemory.verify import detect_path_drift

    _tree(tmp_path, "src/pkg/mod.py", "TIMEOUT = 30\n\n\ndef handler():\n    pass\n")
    for claim in rot.extract_claims(tmp_path, "src"):
        assert detect_path_drift(claim.body()).checked == (), (
            "relative citations became checkable; the benchmark's "
            "relative-vs-absolute arms and this project's path-drift "
            "coverage claims both need revisiting"
        )


def test_absolute_citations_are_checked_and_catch_a_deletion(tmp_path: Path) -> None:
    """The other half of the same property: the identical claim, cited
    absolutely, IS checked — and a removed file is reported missing. This
    is what the benchmark's absolute arm exercises on a repo whose window
    actually contains deletions."""
    from bettermemory.verify import detect_path_drift

    _tree(tmp_path, "src/pkg/mod.py", "def handler():\n    pass\n")
    claim = next(c for c in rot.extract_claims(tmp_path, "src") if c.kind == "path")

    present = detect_path_drift(claim.body(tmp_path))
    assert len(present.checked) == 1
    assert present.missing == ()

    (tmp_path / "src/pkg/mod.py").unlink()
    gone = detect_path_drift(claim.body(tmp_path))
    assert len(gone.missing) == 1
