"""Every mention of the retired planning document is accounted for.

`docs/ROADMAP.md` stopped carrying planned work in `0a322ba`, under the
ruling that plans live in the maintainer's memory store rather than in
the repository. The file survives as a seventeen-line pointer because
released CHANGELOG entries cite the path, and rewriting shipped release
notes to tidy a link is worse than the link.

What that left behind is what this module ratchets. A tree-wide sweep
found fifty-three mentions: thirty-six of them claimed the file CARRIES
content it no longer has -- two printed to a user by `bettermemory
eval` -- or promised that an open item is "on the roadmap", a plan this
project deliberately does not publish. All thirty-six were rewritten.
The seventeen that remain are allowlisted below, each with its reason.

`tests/test_doc_claims.py` cannot see either shape. That walk extracts
path claims only from BACKTICKED tokens, and it asks only whether the
path exists. `docs/ROADMAP.md` does exist, so every one of these passed
it. A file that exists but no longer carries what a citation says it
carries is a different defect, and this module is the surface that
catches it.

The rule: every surviving mention is allowlisted with the reason it is
legitimate. Adding one means adding a line here and defending it.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

from bettermemory.cli import _build_parser
from bettermemory.eval import UsageReplayReport, render_usage_replay_text

_REPO_ROOT = Path(__file__).resolve().parent.parent

# Matched case-insensitively so the markdown link form -- `[roadmap](ROADMAP.md)`
# -- is caught beside the bare path form, and so is the English idiom.
_MENTION = re.compile(r"roadmap", re.IGNORECASE)

_WALKED_DIRS = ("src", "docs", "tests")
_WALKED_SUFFIXES = frozenset({".py", ".md"})

# Whole files that carry the word by their nature, for reasons that would not
# change if every citation in the tree were rewritten today.
_EXEMPT_FILES: dict[str, str] = {
    "CHANGELOG.md": (
        "Released notes are immutable, and their citations are the whole "
        "reason the pointer file still exists."
    ),
    "docs/ROADMAP.md": (
        "The pointer file describes itself: it says this project does not "
        "publish a roadmap."
    ),
    "tests/test_roadmap_citations.py": (
        "This module, which names the retired document on nearly every line."
    ),
}

# Whole trees, same standard.
_EXEMPT_DIRS: dict[str, str] = {
    "docs/audit": (
        "Recorded audit artifacts: immutable evidence of what a sweep found "
        "on the day it ran, in the same class as a commit message."
    ),
}

# (repo-relative path, a distinctive substring of the line) -> why it stays.
#
# Keyed on CONTENT, never on a line number. A line-pinned allowlist breaks the
# first time an unrelated edit moves the line, which is the failure mode
# `tests/test_doc_claims.py` already pays for elsewhere.
_FIXTURE = (
    "Fixture data: builds a throwaway ROADMAP.md under `tmp_path` to exercise "
    "the citation checker. Not a claim about this tree."
)
_CORPUS = (
    "Corpus sample. The extractor under test is fed realistic prose, and this "
    "string is input to it, not a citation this repo makes."
)
_PROSE_CLASS = (
    "Names a CLASS of prose the proposal extractor rejects -- deferral and "
    "planning remarks with no remember-intent -- not the retired document."
)

_ALLOWED: dict[tuple[str, str], str] = {
    ("src/bettermemory/verify.py", "# Repo-relative citation:"): (
        "A citation-FORMAT example in a regex comment. The path exists, so "
        "the example is still true."
    ),
    ("src/bettermemory/verify.py", "dot-then-space passes"): (
        "The same example, showing how trailing punctuation is stripped."
    ),
    ("src/bettermemory/proposals.py", "and roadmap prose with no"): _PROSE_CLASS,
    ("docs/swarm-convergence-plan.md", "for what replaced that habit"): (
        "Legitimate: the pointer file genuinely explains what replaced the "
        "habit of tracking unbuilt phases in the repository."
    ),
    ("docs/eval/widening-labeling-2026-07-22.md", "prune the roadmap"): (
        "A dated eval record. Immutable evidence of what was decided on the "
        "day it ran, in the same class as a commit message."
    ),
    ("docs/incidents/TEMPLATE.md", "notes are the roadmap for"): (
        "Metaphor -- the incident notes ARE the plan for the verification "
        "surface. No document is being cited."
    ),
    ("tests/test_audit_sweep_2026_07_31.py", "See the plan:"): _CORPUS,
    ("tests/test_durability.py", "Two ROADMAP entries changed"): _CORPUS,
    ("tests/test_proposals.py", "def test_extract_rejects_for_the_future"): (
        _PROSE_CLASS
    ),
    ("tests/test_proposals.py", "deferral/roadmap remarks"): _PROSE_CLASS,
    ("tests/test_doc_claims.py", "now a pointer"): (
        "Accurate: records that the retired document LEFT `_PLAN_DOCS` when "
        "the repo stopped carrying planned work."
    ),
    ("tests/test_verify.py", 'present = tmp_path / "docs"'): _FIXTURE,
    ("tests/test_verify.py", 'present.write_text("# roadmap'): _FIXTURE,
    ("tests/test_verify.py", "the flip bars are declared in"): _FIXTURE,
    ("tests/test_verify.py", "report.verified] =="): _FIXTURE,
    ("tests/test_verify.py", "report.checked] =="): _FIXTURE,
    ("tests/test_verify.py", "used to be the second citer"): (
        "Accurate history: states that the retired document is no longer a "
        "citer, which is exactly what this ratchet enforces."
    ),
}


def _walked_files() -> list[Path]:
    """Every file this ratchet reads, repo-relative order stable."""
    found: list[Path] = []
    for name in _WALKED_DIRS:
        root = _REPO_ROOT / name
        if not root.is_dir():
            continue
        found.extend(p for p in root.rglob("*") if p.suffix in _WALKED_SUFFIXES)
    found.extend(p for p in _REPO_ROOT.glob("*.md"))
    return sorted(found)


def _is_exempt(rel: str) -> bool:
    if rel in _EXEMPT_FILES:
        return True
    return any(rel == d or rel.startswith(f"{d}/") for d in _EXEMPT_DIRS)


def _mentions() -> list[tuple[str, int, str]]:
    """Every `(path, lineno, text)` naming the retired document."""
    out: list[tuple[str, int, str]] = []
    for path in _walked_files():
        rel = path.relative_to(_REPO_ROOT).as_posix()
        if _is_exempt(rel):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):  # pragma: no cover - unreadable
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if _MENTION.search(line):
                out.append((rel, lineno, line.strip()))
    return out


def test_every_roadmap_mention_is_allowlisted() -> None:
    """No citation claims the pointer file carries content, unless defended.

    The failure lists every unaccounted mention with its text, because the
    fix is never mechanical: a citation whose content moved into the memory
    store cannot be repointed at another path, and must either state the
    fact inline or say plainly that the reasoning is maintainer-held.
    """
    unaccounted = [
        (rel, lineno, text)
        for rel, lineno, text in _mentions()
        if not any(rel == a_rel and a_sub in text for (a_rel, a_sub) in _ALLOWED)
    ]
    assert not unaccounted, (
        f"{len(unaccounted)} mention(s) of the retired planning document are "
        "not allowlisted. Each either claims the file carries content it no "
        "longer has, or promises a roadmap this project does not publish. "
        "Fix the prose, or add an `_ALLOWED` entry with the reason it is "
        "legitimate:\n"
        + "\n".join(f"  {rel}:{lineno}: {text}" for rel, lineno, text in unaccounted)
    )


def test_allowlist_carries_no_stale_entries() -> None:
    """An allowlist entry that matches nothing is a claim about a line that
    has moved or gone -- the same rot the entries exist to prevent."""
    seen = _mentions()
    stale = [
        (a_rel, a_sub)
        for (a_rel, a_sub) in _ALLOWED
        if not any(rel == a_rel and a_sub in text for rel, _, text in seen)
    ]
    assert not stale, (
        "Allowlist entries match no line in the tree. The prose moved or was "
        "fixed; drop the entry:\n"
        + "\n".join(f"  {rel}: {sub!r}" for rel, sub in stale)
    )


# ---------------------------------------------------------------------------
# The two surfaces a USER actually reads
# ---------------------------------------------------------------------------
#
# The ratchet above reads source lines. These two enter by the paths the
# defect lived on instead: one renders the report `bettermemory eval
# --usage-replay` prints, the other formats the help argparse shows. A
# citation that reaches a user is the sharp end of this class -- following it
# landed on a file reading "This project does not publish a roadmap."


def _empty_replay_report() -> UsageReplayReport:
    """A zero-signal report: enough to render the trailing guidance lines."""
    return UsageReplayReport(
        generated_at=datetime(2026, 1, 1, tzinfo=UTC),
        window_seconds=None,
        events_in_window=0,
        replayable_turns=0,
        turn_audited_turns=0,
        prompt_recall_turns=0,
        repeat_audits_skipped=0,
        recall_companion_audits_skipped=0,
        turns_without_capture=0,
        first_capture_ts=None,
        endorsed_distinct_in_window=0,
        negative_distinct_in_window=0,
        corroborated_memories=0,
        corroborated_twice_memories=0,
    )


def test_usage_replay_render_cites_no_retired_document() -> None:
    """`bettermemory eval --usage-replay` names no document for the bars.

    It used to close with "Read these numbers against the declared flip bars
    in docs/ROADMAP.md (the usage-signal flags entry)" -- naming a section of
    a file that carries neither. The thresholds are maintainer-held, so the
    honest render says that rather than pointing anywhere.
    """
    rendered = render_usage_replay_text(_empty_replay_report())
    assert not _MENTION.search(rendered), (
        "The rendered usage-replay report cites the retired planning "
        f"document:\n{rendered}"
    )


def test_eval_help_cites_no_retired_document() -> None:
    """`bettermemory eval --help` names no document for the bars either."""
    _, subparsers = _build_parser()
    help_text = subparsers["eval"].format_help()
    assert not _MENTION.search(help_text), (
        "`bettermemory eval --help` cites the retired planning document."
    )
