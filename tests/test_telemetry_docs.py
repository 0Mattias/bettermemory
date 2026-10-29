"""The docs say what `[telemetry] enabled = false` turns off (ENG-108).

operations.md's "Telemetry and product state" names every kind of the
state class and every refusal that is an audit row, so a kind added to
`events.ROW_KINDS` without a line there fails here; the enterprise
checklist's step 2, the shipped config's prose and the plugin's hook
note no longer say telemetry off blanks the brief and the recall.
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
    assert "store_busy (retryable)" in section
    # The measurement paragraph names measurement kinds and no state kind.
    start = section.index("**Measurement**")
    measurement = section[start : section.index("So telemetry off", start)]
    assert _named(measurement) & events.MEASUREMENT_KINDS
    assert _named(measurement) & state == set()


def test_the_checklist_no_longer_says_telemetry_off_blanks_the_recall() -> None:
    checklist = _section(
        OPERATIONS.read_text(encoding="utf-8"), "## Enterprise deployment checklist"
    )
    step2 = checklist[checklist.index("2. ") : checklist.index("3. ")]
    flat = " ".join(step2.split())
    assert "deliver nothing" not in flat
    assert "measurement rows only" in flat
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
