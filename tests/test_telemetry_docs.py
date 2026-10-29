"""The docs say what `[telemetry] enabled = false` keeps (ENG-108).

operations.md's "Telemetry and product state" names every kind of the
state class and every refusal that is an audit row, and its table lists
each state kind's kept fields exactly as `events.STATE_FIELDS` keeps
them, so a kind or a field changed without the table fails here. The
enterprise checklist's step 2, the shipped config's prose, SECURITY.md
and the plugin's hook note say a state row keeps only what its readers
read, and none says a query keeps its preview or that no text lands
while the rows kept it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from bettermemory import events
from bettermemory.config import DEFAULT_CONFIG
from bettermemory.policy import REFUSAL_STATUS

ROOT = Path(__file__).resolve().parent.parent
OPERATIONS = ROOT / "docs" / "operations.md"
SECURITY = ROOT / "SECURITY.md"
HOOKS = ROOT / "plugin" / "hooks" / "hooks.json"

_SECTION = "## Telemetry and product state"


def _section(text: str, heading: str) -> str:
    start = text.index(heading)
    end = text.find("\n## ", start + len(heading))
    return text[start : end if end != -1 else len(text)]


def _named(section: str) -> set[str]:
    return set(re.findall(r"`([a-z_]+)`", section))


def test_operations_names_every_state_kind_and_every_audit_refusal() -> None:
    section = _section(OPERATIONS.read_text(encoding="utf-8"), _SECTION)
    named = _named(section)
    state = events.ROW_KINDS[events.RowClass.STATE]
    assert state - named == set(), "state kinds operations.md does not name"
    refusals = set(events.OVERRIDABLE_REFUSALS)
    assert refusals - named == set(), "audit refusals operations.md does not name"
    assert REFUSAL_STATUS in section
    assert "settled_count" in named
    flat = " ".join(section.split())
    assert "store_busy (retryable)" in flat
    # A write kept before its row failed is not retried (the cold review's L2).
    assert "store_busy (not retryable)" in flat
    # The measurement paragraph names measurement kinds and no state kind.
    start = section.index("**Measurement**")
    measurement = section[start : section.index("So telemetry off", start)]
    assert _named(measurement) == events.MEASUREMENT_KINDS
    assert _named(measurement) & state == set()


def test_the_table_lists_what_each_state_kind_keeps() -> None:
    section = _section(OPERATIONS.read_text(encoding="utf-8"), _SECTION)
    rows = [line for line in section.splitlines() if line.startswith("| `")]
    listed: dict[str, set[str]] = {}
    for line in rows:
        kinds_cell, kept_cell, _read_by = line.strip("|").split("|")
        for kind in _named(kinds_cell):
            assert kind not in listed, kind
            listed[kind] = _named(kept_cell)
    assert set(listed) == events.STATE_KINDS
    for kind, kept in events.STATE_FIELDS.items():
        expected = set(kept)
        for keys in kept.values():
            expected |= set(keys or ())
        assert listed[kind] == expected, kind
    # The stamps every row keeps.
    named = _named(section)
    assert events.STATE_STAMP_FIELDS <= named


def test_no_doc_says_a_kept_query_keeps_its_preview() -> None:
    """The cold review's H2: the docs said only a query's hash and length
    were kept while the rows kept snippets, excerpts and quoted words."""
    section = " ".join(
        _section(OPERATIONS.read_text(encoding="utf-8"), _SECTION).split()
    )
    assert "drops the 32-character preview" not in section
    start = DEFAULT_CONFIG.index("\n[telemetry]\n")
    prose = DEFAULT_CONFIG[start : DEFAULT_CONFIG.index("\n[security]\n", start)]
    telemetry = " ".join(prose.replace("#", " ").split())
    assert "without the query preview" not in telemetry
    assert "only the ids, statuses, counts and stamps their readers read" in telemetry
    security = " ".join(SECURITY.read_text(encoding="utf-8").split())
    bullet = security[security.index("**Audit rows with telemetry off**") :]
    bullet = bullet[: bullet.index(" - **")]
    assert "only the fields its readers read" in bullet
    assert "lists what each kind keeps" in bullet


def test_the_checklist_no_longer_says_telemetry_off_blanks_the_recall() -> None:
    checklist = _section(
        OPERATIONS.read_text(encoding="utf-8"), "## Enterprise deployment checklist"
    )
    step2 = checklist[checklist.index("2. ") : checklist.index("3. ")]
    flat = " ".join(step2.split())
    assert "deliver nothing" not in flat
    assert "only the fields its readers read" in flat
    assert "#telemetry-and-product-state" in flat


def test_the_shipped_config_and_the_hook_note_agree() -> None:
    flat = " ".join(DEFAULT_CONFIG.split())
    assert "shows nothing" not in flat
    assert "needs telemetry.enabled" not in flat
    telemetry = flat[flat.index("[telemetry]") : flat.index("[security]")]
    assert "kept either way" in telemetry
    note = json.loads(HOOKS.read_text(encoding="utf-8"))[
        "$schema-comment-prompt-recall"
    ]
    assert "refuses to inject" not in note
    assert "recorded with telemetry off too" in note
    assert "HOOK_HARNESS_TIMEOUT_SECONDS" in note
