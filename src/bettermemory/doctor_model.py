"""The model of `bettermemory doctor`: the statuses and the exit code
each maps to, what a check found (`Finding`), its verdict (`Check`), what
an earlier check leaves for a later one (`_Context`), and the helpers a
check builds its verdict with (`_not_checked`, `_from_findings`).

It lives apart from `doctor.py` so that a check moved out of that module
builds its verdict without importing it, and to keep `doctor.py` under
the package's 2,000-line bound (ENG-96). It imports no module of the
package when it runs; `doctor` imports it and re-exports every name here
a caller reads, so `doctor.Check` and the rest keep their path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from .config import Config
    from .store_index import IndexReport

OK = "ok"
WARN = "warn"
FAIL = "fail"
_RANK = {OK: 0, WARN: 1, FAIL: 2}

#: The exit status for each overall status: 0 when every check is ok,
#: 1 on any failure, 2 when only warnings remain.
EXIT_CODES = {OK: 0, FAIL: 1, WARN: 2}


@dataclass(frozen=True)
class Finding:
    """One thing a check found. A check with several (the client wiring
    reads many files) reports the worst as its own status and summary and
    lists every finding under ``details["findings"]``."""

    status: str
    summary: str
    remedy: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, "summary": self.summary, "remedy": self.remedy}


@dataclass(frozen=True)
class Check:
    """One check's verdict, in the `--json` shape."""

    id: str
    status: str
    summary: str
    remedy: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status,
            "summary": self.summary,
            "remedy": self.remedy,
            "details": self.details,
        }


@dataclass
class _Context:
    """What an earlier check established that a later one reads: the
    working directory the store rule looks in, the loaded config, the
    store directory it resolved, and what the index checks found on the
    store check's open. None where the earlier check could not establish
    it."""

    cwd: Path | None
    config: Config | None = None
    config_failed: bool = False
    directory: Path | None = None
    index: IndexReport | None = None


def _not_checked(check_id: str, why: str, remedy: str) -> Check:
    return Check(check_id, WARN, f"not checked: {why}", remedy)


def _worst(statuses: Sequence[str]) -> str:
    return max(statuses, key=lambda s: _RANK[s], default=OK)


def _from_findings(
    check_id: str,
    findings: Sequence[Finding],
    *,
    ok_summary: str,
    details: dict[str, Any],
) -> Check:
    """A check from its findings: ok when none is worse than ok, else the
    first of the worst, with a count of the others in the summary."""
    details = dict(details)
    if findings:
        details["findings"] = [f.to_dict() for f in findings]
    raised = [f for f in findings if f.status != OK]
    if not raised:
        return Check(check_id, OK, ok_summary, None, details)
    status = _worst([f.status for f in raised])
    first = next(f for f in raised if f.status == status)
    summary = first.summary
    if len(raised) > 1:
        summary += f" (and {len(raised) - 1} more)"
    return Check(check_id, status, summary, first.remedy, details)
