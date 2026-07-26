"""Claim census for bettermemory — how much of a REAL store can the
verification layer actually speak to, and are its attestations anchored?

Two questions, both of which bound the product's central pitch and
neither of which anything else in the tree measures:

1. **Checkability.** A staleness verdict can only ever fire on a claim
   something can re-evaluate against the world — a path, a symbol, a
   command, a config key. A memory recording a preference or a decision
   is not wrong-able by any filesystem-or-git check. So what fraction of
   a store is in scope at all? The complement is the honest ceiling on
   every claim the verification lane makes, and it belongs in the
   abstract of any comparative artifact rather than its footnotes.

2. **Attestation anchoring.** `memory_verify(id)` accepts empty
   attestation lists and still resets the verdict to fresh, and
   `doctor attestation_anchors` only fires when `verified_paths` is
   non-empty. So of the memories that ARE attested, how many have a body
   that even names the file it is attested against? An attestation whose
   file the body never references cannot be evidence *for* that body.

   An uncited attestation is not automatically wrong — a memory about
   the release ritual can legitimately be verified against
   `pyproject.toml` without naming it. The defensible reading is
   narrower: those entries are unfalsifiable by any mechanical check, so
   the freshness they support rests on the caller's good faith. Report
   the number; do not call it fraud.

Both censuses are MECHANICAL — no model in the loop, no judgement call,
no LLM grading. That is deliberate: this is the precursor to a rot
benchmark whose whole credibility rests on nobody being able to say the
corpus or the labels were authored by the party the result favours. A
measurement of our own honesty cannot itself require trusting us.

Usage:

    venv/bin/python bench/claims.py                  # configured store
    venv/bin/python bench/claims.py --store ~/other  # explicit path
    venv/bin/python bench/claims.py --json           # machine-readable

Unlike `bench/storage.py` and `bench/swarm.py`, which build synthetic
corpora in a tmp directory, this one reads the real store on purpose —
the number is only interesting on real memories. It is therefore
strictly READ-ONLY, and it emits *aggregates only*: no body, no
frontmatter value, no filename, no scope name ever reaches the output.
`_Census` counts; it does not collect. Keep it that way — the output of
this script is meant to be publishable verbatim, and that property is
only true while nothing here can print a memory.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Add `src/` to sys.path so this script is runnable without an install.
_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


from bettermemory.config import load_config  # noqa: E402

# ---------------------------------------------------------------------------
# Claim detection
# ---------------------------------------------------------------------------

# Only inspect backticked spans. Prose that happens to contain a slash
# ("either/or") is not a citation, and counting it would be exactly the
# charitable scoring this measurement exists to avoid. Backticks are the
# convention every memory in the corpus already follows for a literal.
_BACKTICK = re.compile(r"`([^`\n]{2,200})`")

_PATH = re.compile(
    r"""(?x)
    ^(?: ~/ | \./ | / | (?:[\w.\-]+/)+ )[\w.\-/*]*$   # incl. a bare `bench/`
  | ^[\w.\-]+\.(?:py|md|json|toml|yml|yaml|sh|lock|cfg|ini|txt|js|ts)$
"""
)
# The snake_case arm was added after the first run on a real store, when a
# memory citing `verified_paths` and `bench/` scored zero. Bare snake_case
# identifiers are the single most common literal in this corpus and the
# original rule — dotted, called, _private, or ALL_CAPS — could not see
# them. Requiring an embedded underscore is what keeps the arm honest:
# English prose does not get backticked as `foo_bar`.
#
# Widening a detector in the direction that raises your own headline
# number is exactly the move a hostile reader should distrust, so the
# delta is recorded rather than quietly absorbed: on the 194-memory
# reference store it moved checkable from 118 (60.8%) to 124 (63.9%),
# and `symbol` from 61 to 83. Anyone re-running this can drop the arm
# and reproduce the lower figure.
_SYMBOL = re.compile(
    r"""(?x)
    ^[A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)+$    # module.attr, a.b.c
  | ^[A-Za-z_][\w]*\(\)$                      # func()
  | ^_[A-Za-z_][\w]*$                         # _private
  | ^[A-Z][A-Z0-9_]{3,}$                      # CONSTANT_NAME
  | ^[a-z][a-z0-9]*(?:_[a-z0-9]+)+$           # snake_case_identifier
"""
)
_COMMAND = re.compile(
    r"^(?:git|uv|uvx|pip|pytest|python|npm|npx|docker|gh|ruff|mypy|make|"
    r"bettermemory|curl|ssh|systemctl|tailscale|wrangler|awk|sed|grep)\b"
)
_CONFIG = re.compile(r"^\[[\w.\-]+\]$|^[\w.\-]+\s*=\s*\S")
_VERSION = re.compile(r"^v?\d+\.\d+(?:\.\d+)?$")
_SHA = re.compile(r"^[0-9a-f]{7,40}$")

# Checkable against the world. Version and SHA tokens are tracked but
# excluded: they DATE a claim rather than assert one, so folding them
# into the headline would inflate the number this script exists to bound.
CHECKABLE_CLASSES = ("path", "symbol", "command", "config")
DATING_CLASSES = ("version", "sha")

_VERIFIED_PATHS_BLOCK = re.compile(r"^verified_paths:\s*\n((?:\s*-\s*.+\n)+)", re.M)
_YAML_LIST_ITEM = re.compile(r"^\s*-\s*(.+?)\s*$", re.M)


def classify_body(body: str) -> set[str]:
    """Return the set of claim classes this body cites.

    First match wins per span, in specificity order: a `git log` span is a
    command, not a symbol, even though it would also satisfy a looser
    identifier rule.
    """
    found: set[str] = set()
    for span in _BACKTICK.findall(body):
        token = span.strip()
        if _COMMAND.match(token):
            found.add("command")
        elif _PATH.match(token):
            found.add("path")
        elif _CONFIG.match(token):
            found.add("config")
        elif _SYMBOL.match(token):
            found.add("symbol")
        elif _VERSION.match(token):
            found.add("version")
        elif _SHA.match(token):
            found.add("sha")
    return found


def split_frontmatter(text: str) -> tuple[str, str]:
    """Split a memory file into (frontmatter, body).

    Only the BODY makes claims; frontmatter is metadata the store wrote.
    Counting an attested path in the frontmatter as a body citation would
    make the anchoring number circular by construction.
    """
    parts = text.split("---\n", 2)
    if len(parts) == 3:
        return parts[1], parts[2]
    return "", text


def attested_paths(frontmatter: str) -> list[str]:
    """Extract the `verified_paths` entries, or [] when unattested."""
    block = _VERIFIED_PATHS_BLOCK.search(frontmatter)
    if not block:
        return []
    items = [p.strip().strip("\"'") for p in _YAML_LIST_ITEM.findall(block.group(1))]
    return [p for p in items if p and p != "[]"]


# ---------------------------------------------------------------------------
# Census
# ---------------------------------------------------------------------------


@dataclass
class _Census:
    """Counters only. This type deliberately holds no memory content — see
    the module docstring. Adding a field that stores a body, a filename or
    a scope would silently break the publishable-verbatim property."""

    total: int = 0
    class_counts: dict[str, int] = field(default_factory=dict)
    classes_per_memory: dict[int, int] = field(default_factory=dict)
    checkable: int = 0
    dating_only: int = 0
    bare: int = 0

    attested: int = 0
    anchor_full: int = 0
    anchor_basename: int = 0
    anchor_none: int = 0
    attested_entries: int = 0
    attested_entries_cited: int = 0


def run_census(root: Path) -> _Census:
    c = _Census()
    for path in sorted(root.glob("*.md")):
        if path.name.startswith("."):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        frontmatter, body = split_frontmatter(text)
        c.total += 1

        found = classify_body(body)
        for cls in found:
            c.class_counts[cls] = c.class_counts.get(cls, 0) + 1
        n_checkable = len(found & set(CHECKABLE_CLASSES))
        c.classes_per_memory[n_checkable] = c.classes_per_memory.get(n_checkable, 0) + 1
        if n_checkable:
            c.checkable += 1
        elif found & set(DATING_CLASSES):
            c.dating_only += 1
        else:
            c.bare += 1

        paths = attested_paths(frontmatter)
        if not paths:
            continue
        c.attested += 1
        c.attested_entries += len(paths)
        full = sum(1 for p in paths if p in body)
        base = sum(1 for p in paths if p not in body and Path(p).name in body)
        c.attested_entries_cited += full
        if full:
            c.anchor_full += 1
        elif base:
            c.anchor_basename += 1
        else:
            c.anchor_none += 1
    return c


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _pct(n: int, d: int) -> str:
    return f"{n} ({100.0 * n / d:.1f}%)" if d else f"{n} (n/a)"


def _format_text(c: _Census, root: Path) -> str:
    out = [
        f"store: {root}",
        f"active memories scanned: {c.total}",
        "",
        "CHECKABILITY — memories carrying >=1 mechanically checkable claim",
        f"  checkable             {_pct(c.checkable, c.total)}",
        f"  date/SHA token only   {_pct(c.dating_only, c.total)}",
        f"  no checkable claim    {_pct(c.bare, c.total)}",
        "",
        "  by class (a memory can carry several):",
    ]
    for cls in CHECKABLE_CLASSES + DATING_CLASSES:
        out.append(f"    {cls:<8} {_pct(c.class_counts.get(cls, 0), c.total)}")
    out += ["", "  distinct checkable classes per memory:"]
    for k in sorted(c.classes_per_memory):
        out.append(f"    {k} class(es)  {_pct(c.classes_per_memory[k], c.total)}")

    out += [
        "",
        "ATTESTATION ANCHORING — does the body name the file it is attested to?",
        f"  memories with a non-empty verified_paths  {_pct(c.attested, c.total)}",
    ]
    if c.attested:
        out += [
            f"    body cites the full attested path  {_pct(c.anchor_full, c.attested)}",
            f"    body cites only the basename       "
            f"{_pct(c.anchor_basename, c.attested)}",
            f"    body never mentions it at all      {_pct(c.anchor_none, c.attested)}",
            "",
            f"  attested path entries                {c.attested_entries}",
            f"    entries the body actually cites    "
            f"{_pct(c.attested_entries_cited, c.attested_entries)}",
        ]
    return "\n".join(out) + "\n"


def _as_dict(c: _Census, root: Path) -> dict[str, Any]:
    return {
        "store": str(root),
        "total": c.total,
        "checkability": {
            "checkable": c.checkable,
            "dating_only": c.dating_only,
            "bare": c.bare,
            "by_class": {
                cls: c.class_counts.get(cls, 0)
                for cls in CHECKABLE_CLASSES + DATING_CLASSES
            },
            "classes_per_memory": dict(sorted(c.classes_per_memory.items())),
        },
        "attestation": {
            "attested": c.attested,
            "anchor_full": c.anchor_full,
            "anchor_basename": c.anchor_basename,
            "anchor_none": c.anchor_none,
            "entries": c.attested_entries,
            "entries_cited": c.attested_entries_cited,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Census a real store for mechanically checkable claims and "
            "attestation anchoring. Read-only; emits aggregates only."
        ),
    )
    parser.add_argument(
        "--store",
        type=str,
        default=None,
        help="Store directory. Defaults to the configured/resolved store.",
    )
    parser.add_argument(
        "--json", action="store_true", help="Emit JSON instead of a text report."
    )
    args = parser.parse_args()

    if args.store:
        root = Path(args.store).expanduser().resolve()
    else:
        root = load_config().resolved_directory()

    if not root.is_dir():
        print(f"no such store directory: {root}", file=sys.stderr)
        return 1

    census = run_census(root)
    if census.total == 0:
        print(f"no memories found in {root}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(_as_dict(census, root), indent=2))
    else:
        print(_format_text(census, root))
    return 0


if __name__ == "__main__":
    sys.exit(main())
