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
import json
import random
import subprocess
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


def test_a_constant_classifier_scores_zero_on_the_primary_metric() -> None:
    """Youden's J is the primary metric precisely because it cannot be
    gamed by flagging everything. Both constant classifiers must score
    exactly 0.0 — if that ever stops being true, the metric has stopped
    doing the one job it was chosen for."""
    assert rot.youden_j(tp=10, fn=0, fp=90, tn=0) == 0.0  # always_flag
    assert rot.youden_j(tp=0, fn=10, fp=0, tn=90) == 0.0  # never_flag
    # A detector with genuine discrimination must beat both.
    assert rot.youden_j(tp=9, fn=1, fp=10, tn=80) > 0.5


def test_perfect_recall_at_a_high_flag_rate_is_not_significant() -> None:
    """The error this benchmark shipped in its first version: 0% miss rate
    was reported as an achievement when a flag-everything detector earns
    the same score by construction. Fisher against a rate-matched random
    detector is what distinguishes them, so it is pinned here."""
    caught_all_by_flagging_all = rot.fisher_one_sided(tp=26, fn=0, fp=628, tn=21)
    assert caught_all_by_flagging_all is not None
    assert caught_all_by_flagging_all > 0.05, (
        "catching everything by flagging everything must NOT read as significant"
    )
    genuinely_discriminating = rot.fisher_one_sided(tp=20, fn=6, fp=10, tn=639)
    assert genuinely_discriminating is not None
    assert genuinely_discriminating < 0.001


def test_readme_never_reports_a_miss_rate_without_its_counterweights() -> None:
    """A prose ratchet, in the style this project already uses on its docs.

    The first version of the rot README led with "the verdict never
    misses" and a 0% unflagged-stale rate, which is what `always_flag`
    scores. Any future edit that reintroduces a bare miss-rate claim
    without J, a significance test and alerts-per-catch nearby is
    reproducing the exact framing error the benchmark was built to expose.
    """
    readme = (_ROOT / "bench" / "rot" / "README.md").read_text(encoding="utf-8")
    lowered = readme.lower()
    if "unflagged-stale" in lowered or "unflagged_stale" in lowered:
        for required in ("youden", "fisher", "alerts/catch", "always_flag"):
            assert required.lower() in lowered, (
                f"rot README reports a miss rate without {required!r} — a "
                "flag-everything detector scores a perfect miss rate, so the "
                "number is meaningless without its counterweights"
            )
    assert "never misses" not in lowered


def test_the_shipped_default_is_not_a_constant_function() -> None:
    """The regression guard for the 3.30.0 verdict fix.

    Before it, `verification.status in {"never", "stale"}` pre-empted
    both drift inputs, so the `shipped_default` arm — anchored 400 days
    back, past the freshness window — flagged 100% of claims in every
    class and both windows: J = 0.000, arithmetically identical to
    `always_flag`. The drift legs that carry all the discrimination
    were unreachable in the configuration most users run.

    The invariant that says the pre-emption is gone is arm CONVERGENCE:
    `shipped_default` must now score exactly what
    `drift_only_relative_cite` scores, because the only difference
    between them is a calendar anchor that no longer erases the
    measurement. Pinned on the committed results rather than by
    re-running the corpus, so the guard costs nothing in CI — the JSON
    is the published artifact, and if a future edit reintroduces the
    pre-emption the two arms separate again and this fails.
    """
    results = _ROOT / "bench" / "rot" / "results"
    published = sorted(results.glob("bettermemory-*d-*.json"))
    assert published, "no committed rot results to guard"
    for path in published:
        report = json.loads(path.read_text(encoding="utf-8"))
        shipped = report["modes"]["shipped_default"]
        drift_only = report["modes"]["drift_only_relative_cite"]
        assert shipped == drift_only, (
            f"{path.name}: the shipped default diverged from the "
            "calendar-disabled arm — the calendar leg is pre-empting the "
            "drift legs again, which is what made the verdict a constant "
            "function"
        )
        assert shipped["ALL"]["flag_rate"] < 1.0, (
            f"{path.name}: the shipped default flags every claim — that is "
            "`always_flag` with extra steps, not a detector"
        )


def test_a_silent_commit_leg_is_not_reported_as_a_measured_zero() -> None:
    """`verdict_for` must hand the verdict `None`, not `0`, when
    `compute_commit_drift` declined to emit.

    Since 3.30.0 the two inputs mean opposite things to the rollup: a
    measured zero stands the calendar leg down on a stale memory, while
    `None` ("the leg could not ask") deliberately does not. The bench
    row still records an int for schema stability, so the conflation is
    invisible unless pinned — and it would manufacture a false green
    inside the very instrument that measures the guard against false
    greens.
    """
    source = (_ROOT / "bench" / "rot" / "run.py").read_text(encoding="utf-8")
    body = source.split("def verdict_for(", 1)[1].split("\ndef ", 1)[0]
    assert "commit_drift_count=count if drift_status is not None else None" in body, (
        "verdict_for no longer distinguishes a silent commit leg from a "
        "measured zero when computing the verdict"
    )


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


# ---------------------------------------------------------------------------
# Continuous commit counts, and the AUROC they make computable
# ---------------------------------------------------------------------------


def _git_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    for key, value in (("user.email", "t@t"), ("user.name", "t")):
        subprocess.run(["git", "-C", str(root), "config", key, value], check=True)


def _commit(root: Path, rel: str, source: str, message: str) -> None:
    _tree(root, rel, source)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(root), "commit", "-q", "-m", message],
        check=True,
    )


def test_commit_counts_are_counts_not_booleans(tmp_path: Path) -> None:
    """The defect item (c) exists to fix.

    The first version wrote `{p: 1 for p in changed}` — one bit meaning
    "touched at some point". Every score was then 0 or 1, which makes the
    ROC curve degenerate and the question "does churn MAGNITUDE carry
    information the >0 threshold discards?" unaskable. A file hammered
    five times must score above one touched once, or the continuous
    metric is measuring nothing the boolean did not already say.
    """
    _git_repo(tmp_path)
    _commit(tmp_path, "src/a.py", "X = 1\n", "base")
    _commit(tmp_path, "src/quiet.py", "Y = 1\n", "add quiet")
    t0 = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    for n in range(5):
        _commit(tmp_path, "src/a.py", f"X = {n + 2}\n", f"churn {n}")
    _commit(tmp_path, "src/quiet.py", "Y = 2\n", "one touch")

    counts = rot.commit_counts_touching(tmp_path, t0, "HEAD", "src")
    assert counts["src/a.py"] == 5, "a hammered file must carry its real count"
    assert counts["src/quiet.py"] == 1
    assert set(counts.values()) != {1}, (
        "counts collapsed back to booleans — AUROC becomes degenerate and "
        "the continuous metric silently re-reports the flag rate"
    )


def test_a_boolean_score_makes_auroc_degenerate() -> None:
    """Why the count had to become continuous, stated as arithmetic.

    Under the old model the only scores were 0 and 1, so almost every
    (false, still-true) pair was a TIE and contributed exactly 0.5. AUROC
    was therefore pinned near the flag rate rather than measuring
    discrimination. Real counts break the ties.
    """
    boolean_pos, boolean_neg = [1.0, 1.0, 1.0], [1.0, 1.0, 0.0]
    counted_pos, counted_neg = [9.0, 7.0, 5.0], [2.0, 1.0, 0.0]
    degenerate = rot.auroc(boolean_pos, boolean_neg)
    informative = rot.auroc(counted_pos, counted_neg)
    assert degenerate is not None and informative is not None
    assert degenerate < informative
    assert informative == 1.0, "perfectly ordered counts must score a perfect AUROC"


def test_auroc_scores_a_tie_as_exactly_half_credit() -> None:
    """Midranks, not an arbitrary win. A detector that cannot separate two
    claims must be given no credit for the pair, in either direction."""
    assert rot.auroc([1.0], [1.0]) == 0.5
    assert rot.auroc([1.0, 1.0], [1.0, 1.0]) == 0.5
    # One greater, one tied, one less -> (1 + 0.5 + 0) / 3
    assert rot.auroc([2.0], [1.0, 2.0, 3.0]) == 0.5


def test_auroc_of_a_constant_classifier_is_exactly_a_coin() -> None:
    """The same property that makes Youden's J the primary metric: a
    detector that emits one value for everything must score 0.5, so
    "flag everything" cannot buy a good AUROC either."""
    assert rot.auroc([1.0] * 6, [1.0] * 479) == 0.5
    assert rot.auroc([0.0] * 6, [0.0] * 479) == 0.5


def test_the_same_auroc_is_less_significant_on_fewer_positives() -> None:
    """The honesty ratchet on the new metric.

    The symbol class has SIX actually-false claims. A point estimate
    around 0.75 there reads like a finding, and the identical estimate on
    sixty positives is a different piece of evidence entirely. The number
    that must move with n is the p, not the AUROC — so this pins that an
    AUROC held FIXED gets less significant as n shrinks, and that at n=6
    it does not clear the project's own p<0.01 bar.

    This is the shape of the claim the benchmark already retracted once:
    a strong-looking figure whose n could not support it.
    """
    negative = [float(i) for i in range(100)]
    small = rot.auroc_permutation_p([75.0] * 6, negative)
    large = rot.auroc_permutation_p([75.0] * 60, negative)
    assert rot.auroc([75.0] * 6, negative) == rot.auroc([75.0] * 60, negative)
    assert small is not None and large is not None
    assert small > large, "significance must track n at a fixed effect size"
    assert small > 0.01, (
        "an AUROC of 0.755 on six positives must not clear the project's "
        "significance bar on the strength of its size alone"
    )
    assert large < 0.01


def test_auroc_permutation_p_is_reproducible_and_never_reports_zero() -> None:
    """A published p that moves between runs is not a published p. The
    seed is fixed, and the +1/+1 estimator forbids a p of exactly 0 —
    20,000 samples can never justify that.

    The floor has to survive PRINTING too: at 4 decimal places the
    smallest attainable value (1/20001) renders as "0.0000", which would
    put the forbidden number on the page anyway.
    """
    pos, neg = [5.0, 6.0, 7.0], [0.0, 1.0, 2.0, 3.0]
    first = rot.auroc_permutation_p(pos, neg)
    assert first == rot.auroc_permutation_p(pos, neg)
    assert first is not None and first > 0.0

    # A separation so total that every permutation is beaten — the case
    # that drives the estimator to its floor.
    floored = rot.auroc_permutation_p([1e6] * 60, [float(i) for i in range(200)])
    assert floored is not None
    assert floored > 0.0, "the +1/+1 floor was rounded away by the formatter"
    assert floored == round(1 / (rot._PERMUTATIONS + 1), 5)


def test_auroc_permutation_p_calls_a_real_effect_significant() -> None:
    """The counterweight to the test above: the gate must not be so
    conservative that nothing can ever pass it."""
    rng = random.Random(4)
    negative = [float(rng.choice([0, 1])) for _ in range(200)]
    positive = [50.0, 60.0, 70.0, 80.0, 90.0, 100.0, 110.0, 120.0]
    p = rot.auroc_permutation_p(positive, negative)
    assert p is not None and p < 0.01


# ---------------------------------------------------------------------------
# Claim-level drift — and the guards that keep it from becoming the oracle
# ---------------------------------------------------------------------------


def _diff(*hunks: str) -> str:
    """A minimal `git log -p -U0` stream, one commit."""
    return "\x01" + "deadbeef\n" + "".join(hunks)


def _file_hunk(path: str, removed: list[str], added: list[str]) -> str:
    return (
        f"diff --git a/{path} b/{path}\n"
        f"--- a/{path}\n+++ b/{path}\n"
        f"@@ -1,{len(removed)} +1,{len(added)} @@\n"
        + "".join(f"-{line}\n" for line in removed)
        + "".join(f"+{line}\n" for line in added)
    )


def test_the_detector_cannot_see_a_claim() -> None:
    """The firewall, enforced by signature rather than by discipline.

    `build_binding_index` takes the diff text and nothing else, so it
    cannot look a claim up. Everything the detector knows about a claim
    arrives through `parse_claim_citation`, which reads the RENDERED BODY
    — exactly the material a production implementation has. Handing it the
    `Claim` dataclass would give it structured truth the product never
    sees, and would make the value comparison privileged rather than fair.
    """
    import inspect

    params = list(inspect.signature(rot.build_binding_index).parameters)
    assert params == ["diff_text"], (
        "build_binding_index grew an argument; if a claim can reach it, "
        "the detector can no longer be distinguished from the oracle"
    )
    for claim in (
        rot.Claim("path", "src/m.py", "src/m.py", ""),
        rot.Claim("symbol", "src/m.py", "handler", ""),
        rot.Claim("literal", "src/m.py", "TIMEOUT", "30"),
    ):
        cite = rot.parse_claim_citation(claim.body())
        assert cite is not None, f"body not recoverable for {claim.kind}"
        assert cite.kind == claim.kind
        assert cite.rel_path == "src/m.py"
        assert cite.name == claim.name
        assert cite.value == claim.value


def test_a_body_only_edit_is_not_drift() -> None:
    """The negative the whole design rests on.

    `label_claim` matches a definition by `.name` and never reads its
    contents, so a body edit leaves the claim TRUE by construction
    (`test_pure_reformat_is_not_drift` pins that). A detector that counted
    body churn could therefore only manufacture false alarms — which is
    exactly today's failure, restored under a new name.
    """
    index = rot.build_binding_index(
        _diff(
            _file_hunk("src/m.py", ["    return 1"], ["    # explain", "    return 2"])
        )
    )
    cite = rot.parse_claim_citation(
        rot.Claim("symbol", "src/m.py", "handler", "").body()
    )
    assert cite is not None
    assert rot.claim_level_drift(cite, index)["strict"] is False
    assert rot.claim_level_drift(cite, index)["weak"] is False


def test_a_signature_reflow_fires_weak_but_not_strict() -> None:
    """The inverse ratchet, and the strongest guard against the detector
    quietly becoming the oracle.

    Adding a parameter touches the `def` line but leaves the symbol
    defined, so the oracle says still_true. STRICT must stay quiet (net
    removals are zero — the binding was re-added). WEAK must fire, because
    "the binding was touched" is genuinely true. If WEAK ever stops firing
    here, the detector has started reading truth instead of diffs.
    """
    index = rot.build_binding_index(
        _diff(_file_hunk("src/m.py", ["def handler(a):"], ["def handler(a, b=1):"]))
    )
    cite = rot.parse_claim_citation(
        rot.Claim("symbol", "src/m.py", "handler", "").body()
    )
    assert cite is not None
    drift = rot.claim_level_drift(cite, index)
    assert drift["weak"] is True, "a touched binding must reach the weak tier"
    assert drift["strict"] is False, (
        "a re-added binding is not drift — net-of-readds is what separates "
        "a surviving definition from one that went away"
    )


def test_a_removed_definition_is_strict_drift() -> None:
    """The counterweight: recall must not be free."""
    index = rot.build_binding_index(
        _diff(_file_hunk("src/m.py", ["def handler(a):"], ["def renamed(a):"]))
    )
    cite = rot.parse_claim_citation(
        rot.Claim("symbol", "src/m.py", "handler", "").body()
    )
    assert cite is not None
    assert rot.claim_level_drift(cite, index)["strict"] is True


def test_only_column_zero_bindings_count() -> None:
    """The entire difference between this detector and a name-grep.

    An indented `def` is a method or a nested function, which is not what
    a top-level claim asserts. A keyword argument and a dict entry are not
    bindings at all. If any of these produced a token, every method edit
    inside a class would read as drift on the class's own claim.
    """
    assert rot._binding_token("def handler():") == ("def", "handler")
    assert rot._binding_token("async def handler():") == ("def", "handler")
    assert rot._binding_token("class Store:") == ("def", "Store")
    assert rot._binding_token("TIMEOUT = 30") == ("assign", "TIMEOUT")
    for not_a_binding in (
        "    def inner(self):",  # a method
        "\tdef inner(self):",
        "        TIMEOUT = 30",  # a local
        "foo(TIMEOUT=30)",  # a keyword argument
        '    "TIMEOUT": 30,',  # a dict entry
        "if x == 30:",  # a comparison, not an assignment
        "# def handler():",  # a comment
    ):
        assert rot._binding_token(not_a_binding) is None, not_a_binding


def test_string_fragments_survive_implicit_concatenation() -> None:
    """The bug that cost 12 of 20 literal catches before it was found.

    Python writes a long constant as adjacent string literals, so the
    value's LOGICAL lines and the file's PHYSICAL lines are different
    objects — a logical line spans several physical ones, and a value with
    no newline at all still occupies a dozen lines of source. Matching
    whole logical lines against diff lines therefore finds almost nothing.
    Decoding each physical line and testing CONTAINMENT is what works.
    """
    fragment = rot.string_fragment('    "the user references shared context "')
    assert fragment == "the user references shared context "
    # Escapes must be decoded, or every interesting line fails to match.
    assert rot.string_fragment(r'    "a \"quoted\" phrase here"') == (
        'a "quoted" phrase here'
    )
    assert rot.string_fragment('    "trailing piece",') == "trailing piece"
    assert rot.string_fragment('    "closing piece")') == "closing piece"
    # Not self-contained string literals.
    assert rot.string_fragment("    return handler(x)") is None
    assert rot.string_fragment("TIMEOUT = 30") is None

    # End to end: a concatenated constant whose edited physical line is
    # nowhere to be found among the value's logical lines.
    value = repr("Search stored memories. Default: do NOT call.\nSecond line here.")
    index = rot.build_binding_index(
        _diff(
            _file_hunk(
                "src/m.py",
                ['    "Search stored memories. Default: do NOT call.\\n"'],
                ['    "Search stored memories. Call it always.\\n"'],
            )
        )
    )
    cite = rot.parse_claim_citation(
        rot.Claim("literal", "src/m.py", "DESC", value).body()
    )
    assert cite is not None
    assert rot.claim_level_drift(cite, index)["strict"] is True, (
        "a multi-line constant edited through implicit concatenation must "
        "be caught; whole-line anchors alone miss it"
    )


def test_hunk_parsing_survives_content_that_looks_like_a_diff() -> None:
    """File content can contain `diff --git` and `@@` lines — a docstring
    about diffs, or this very test file. At -U0 a hunk's line count is
    exact, so the parser consumes a known number of lines and verifies it
    rather than scanning for the next header and hoping."""
    hostile = (
        "diff --git a/src/m.py b/src/m.py\n"
        "--- a/src/m.py\n+++ b/src/m.py\n"
        "@@ -1,2 +1,2 @@\n"
        "-diff --git a/fake b/fake\n"
        "-@@ -9,9 +9,9 @@\n"
        "+def handler():\n"
        "+TIMEOUT = 5\n"
    )
    index = rot.build_binding_index("\x01deadbeef\n" + hostile)
    assert index["parse_mismatches"] == 0, "the -U0 line-count check failed"
    assert index["hunks"] == 1
    # The decoy header lines were consumed as CONTENT, not as structure.
    assert ("src/m.py", "def", "handler") in index["bindings"]
    assert ("src/m.py", "assign", "TIMEOUT") in index["bindings"]
    assert "fake" not in index["files"]


def test_labels_come_from_t1_not_from_the_working_tree(tmp_path: Path) -> None:
    """A pinned window must be GRADED at its pinned end.

    `--t1` originally moved only the reported sha and the diff range,
    while `label_claim` kept reading the repository's live working tree.
    Nothing errored: a run pinned to an old t1 would be silently scored
    against whatever the developer happened to have checked out, and the
    published sha would assert a window the numbers did not come from.

    Here t1 is pinned to the commit where `handler` still exists, while
    the working tree has moved on and renamed it. The claim must read
    still_true — if it reads false, the oracle is grading HEAD again.
    """
    _git_repo(tmp_path)
    _commit(tmp_path, "src/m.py", "def handler():\n    pass\n", "base")
    t0 = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    _commit(tmp_path, "src/m.py", "def handler():\n    return 1\n", "edit body")
    t1 = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    # The working tree moves PAST t1 and renames the symbol away.
    _commit(tmp_path, "src/m.py", "def renamed():\n    return 1\n", "rename")

    rows, meta = rot.collect_rows(tmp_path, "src", t0, t1, "")
    symbol_rows = [r for r in rows if r["kind"] == "symbol"]
    assert symbol_rows, "no symbol claims extracted"
    assert {r["truth"] for r in symbol_rows} == {"still_true"}, (
        "the symbol was renamed AFTER t1, so a run pinned to t1 must not "
        "see it as drift — labels are coming from the working tree"
    )
    assert meta["t1"] == t1


def test_claims_already_false_at_t0_are_dropped_and_counted(tmp_path: Path) -> None:
    """An extraction artifact is not drift.

    A module that rebinds a constant yields one claim per binding, but
    `label_claim` returns on the FIRST matching assignment — so the
    second claim reads `false` against its own t0 tree. Counting it as a
    positive credits the window with rot that predates it and inflates
    the base rate every precision figure is measured against.

    The count is published rather than silently filtered: a filter whose
    size is unreported cannot be distinguished from one tuned to taste.
    """
    _git_repo(tmp_path)
    _commit(
        tmp_path, "src/m.py", "X = 1\nX = 2\n\n\ndef handler():\n    pass\n", "base"
    )
    t0 = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    _commit(
        tmp_path, "src/m.py", "X = 1\nX = 2\n\n\ndef handler():\n    return 1\n", "edit"
    )
    t1 = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    # Both bindings are extracted, and the second is false against its own tree.
    raw = rot.extract_claims(tmp_path, "src")
    assert sum(1 for c in raw if c.kind == "literal") == 2

    rows, meta = rot.collect_rows(tmp_path, "src", t0, t1, "")
    assert meta["claims_false_at_t0"] == 1, "the never-true claim was not dropped"
    literal_rows = [r for r in rows if r["kind"] == "literal"]
    assert {r["truth"] for r in literal_rows} == {"still_true"}, (
        "a claim that was false before the window opened is being counted "
        "as drift the window caused"
    )


def test_a_wrong_subdir_fails_loudly_instead_of_scoring_nothing(
    tmp_path: Path,
) -> None:
    """The most dangerous failure mode in a multi-repo run.

    `rglob` on a missing directory returns [] without raising,
    `_detector_stats` on an empty slice returns all-None, and the report
    prints a complete, well-formed table with n = 0 and "n/a" everywhere.
    A repository that silently contributed nothing would be
    indistinguishable from one that contributed cleanly — and across
    fifteen unfamiliar layouts, guessing the source directory wrong is
    not a hypothetical.
    """
    _git_repo(tmp_path)
    _commit(tmp_path, "src/a.py", "X = 1\n", "base")
    t0 = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    _commit(tmp_path, "src/a.py", "X = 2\n", "drift")
    t1 = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    with pytest.raises(ValueError, match="no claims extracted"):
        rot.collect_rows(tmp_path, "lib", t0, t1, "")

    # The correct subdir still works, so the guard is not just refusing.
    rows, _ = rot.collect_rows(tmp_path, "src", t0, t1, "")
    assert rows


def test_memoized_and_unmemoized_runs_agree_exactly(tmp_path: Path) -> None:
    """The only guarantee the 16x speedup is allowed to make.

    98.5% of this harness was one call: the shipped `compute_commit_drift`
    runs a full-history `git log` per row, so cost scales with how much
    HISTORY a repository has — the exact axis a multi-repo corpus of
    established projects maximises. Caching those pure git reads takes the
    published 60-day window from ~114s to ~7s.

    A speedup that moved a number would be a defect, not an optimisation,
    so the two paths are compared row for row. Note what is NOT cached:
    `compute_staleness_verdict`, `compute_commit_drift`,
    `compute_verification_status`, `detect_path_drift` and
    `resolve_commit_drift_count` all still run per row, which is what
    keeps "the function under test is the shipped one" true.
    """
    _git_repo(tmp_path)
    _commit(
        tmp_path, "src/a.py", "TIMEOUT = 30\n\n\ndef handler():\n    pass\n", "base"
    )
    t0 = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    _commit(
        tmp_path, "src/a.py", "TIMEOUT = 60\n\n\ndef renamed():\n    pass\n", "drift"
    )
    _commit(tmp_path, "src/b.py", "OTHER = 1\n", "add another")
    t1 = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    def _run(memoize: bool) -> list[dict[str, object]]:
        if memoize:
            return rot.collect_rows(tmp_path, "src", t0, t1, "")[0]
        # Neutralise the context manager to get the uncached path.
        import contextlib

        original = rot.memoized_git_reads
        setattr(rot, "memoized_git_reads", lambda: contextlib.nullcontext({}))
        try:
            return rot.collect_rows(tmp_path, "src", t0, t1, "")[0]
        finally:
            setattr(rot, "memoized_git_reads", original)

    assert _run(memoize=True) == _run(memoize=False), (
        "caching the whole-history git reads changed a graded value"
    )


def test_memoization_is_removed_again_afterwards() -> None:
    """The cache is installed on `bettermemory.verify`'s own globals, so it
    must come off again — a wrapper left in place would silently serve
    stale git state to anything else running in the same process."""
    from bettermemory import verify

    names = (
        "commit_author_timestamps",
        "commit_author_timestamps_touching_pathspecs",
        "repo_toplevel",
        "resolve_repo_pathspecs",
    )
    before = {n: getattr(verify, n) for n in names}
    with rot.memoized_git_reads():
        assert all(getattr(verify, n) is not before[n] for n in names), (
            "memoization did not install"
        )
    assert all(getattr(verify, n) is before[n] for n in names), (
        "memoization leaked past its context manager"
    )


def test_the_ceiling_baseline_is_present_and_perfect() -> None:
    """`oracle_replica` peeks at the label, so it scores J = 1.000 by
    construction. It is printed beside the real detectors because the
    claim-level detector also reaches 1.000 on this corpus — the window's
    diff is very nearly a sufficient statistic for the oracle's own
    question. Without this row in the same table, a reader cannot tell a
    hard-won result from a trivially reachable ceiling.
    """
    assert "oracle_replica" in rot.BASELINES
    rows = [
        {"truth": "false", "commit_drift": 0},
        {"truth": "still_true", "commit_drift": 9},
        {"truth": "still_true", "commit_drift": 0},
    ]
    ceiling = rot._detector_stats(rows, rot.BASELINES["oracle_replica"])
    assert ceiling["youden_j"] == 1.0
    for constant in ("always_flag", "never_flag"):
        assert rot._detector_stats(rows, rot.BASELINES[constant])["youden_j"] == 0.0


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
