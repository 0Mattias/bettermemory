"""Run the comparative harness and render its report.

`run_comparative` drives every adapter over a workload, collecting a
`RunResult` per system (with `ran=False` for the ones that can't execute
here). `render_text` / `render_json` turn that into a publishable report:
a capability matrix first (the structural finding), then bettermemory's
measured lanes, then an honest accounting of what wasn't run and why.

Runnable as a module for a manual pass:

    python -m tests.eval.comparative          # text
    python -m tests.eval.comparative --json    # machine-readable
    python -m tests.eval.comparative --k 3
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Sequence

from bettermemory.eval import RateCI

from .adapters import RunResult, SystemAdapter, SystemUnavailable, default_adapters
from .workload import Workload, default_workload


@dataclass
class ComparativeReport:
    """All systems' outcomes for one workload, plus render helpers."""

    workload_name: str
    k: int
    generated_at: datetime
    results: list[RunResult] = field(default_factory=list)

    @property
    def ran(self) -> list[RunResult]:
        return [r for r in self.results if r.ran]

    @property
    def unavailable(self) -> list[RunResult]:
        return [r for r in self.results if not r.ran]

    def to_dict(self) -> dict[str, Any]:
        return {
            "workload": self.workload_name,
            "k": self.k,
            "generated_at": self.generated_at.isoformat(),
            "results": [r.to_dict() for r in self.results],
        }


def run_comparative(
    adapters: Sequence[SystemAdapter],
    workload: Workload,
    *,
    k: int = 5,
    now: datetime | None = None,
) -> ComparativeReport:
    """Run each adapter; record a RunResult (ran or unavailable) for each."""
    results: list[RunResult] = []
    for adapter in adapters:
        caps = adapter.capabilities()
        try:
            results.append(adapter.run(workload, k=k))
        except SystemUnavailable as exc:
            results.append(
                RunResult(
                    name=adapter.name,
                    capabilities=caps,
                    ran=False,
                    k=k,
                    unavailable_reason=exc.reason,
                )
            )
    return ComparativeReport(
        workload_name=workload.name,
        k=k,
        generated_at=now or datetime.now(timezone.utc),
        results=results,
    )


def _yn(value: bool) -> str:
    return "yes" if value else "no"


def _fmt_rate(rate: RateCI) -> str:
    """Render a RateCI as ``num/den = rate (95% CI lo–hi)`` or an N/A note."""
    if rate.rate is None:
        return f"n/a (denominator {rate.denominator})"
    ci = (
        f" (95% CI {rate.lower:.2f}–{rate.upper:.2f})"
        if rate.lower is not None and rate.upper is not None
        else ""
    )
    return f"{rate.numerator}/{rate.denominator} = {rate.rate:.2f}{ci}"


def render_text(report: ComparativeReport) -> str:
    lines: list[str] = []
    lines.append(
        f"bettermemory comparative eval — workload: {report.workload_name}  (k={report.k})"
    )
    lines.append("=" * 70)
    lines.append("")
    lines.append("Capability matrix — can the system compute the published trio?")
    lines.append(
        f"  {'system':<16}{'retrieval':>10}{'endorse':>9}{'audit':>7}{'trio':>7}"
    )
    for r in report.results:
        c = r.capabilities
        lines.append(
            f"  {r.name:<16}{_yn(c.logs_retrieval):>10}{_yn(c.logs_endorsement):>9}"
            f"{_yn(c.has_audit_hook):>7}{('YES' if c.can_compute_trio else 'no'):>7}"
        )
    lines.append("")
    lines.append(
        "  Only a system with all three signals can compute memory_helped_rate,"
    )
    lines.append(
        "  endorsement_rate, and silent_miss_rate. (structural finding — holds"
    )
    lines.append("  whether or not a competitor executes here.)")

    for r in report.ran:
        lines.append("")
        lines.append(f"Measured — {r.name} (ran locally):")
        if r.recall_at_k is not None:
            lines.append(
                f"  recall@{r.k:<19}{r.recalled}/{r.gold_total} = {r.recall_at_k:.2f}"
            )
        if r.eval_report is not None:
            ev = r.eval_report
            lines.append(f"  silent_miss_rate     {_fmt_rate(ev.silent_miss_rate)}")
            lines.append(f"  memory_helped_rate   {_fmt_rate(ev.memory_helped_rate)}")
            lines.append(f"  endorsement_rate     {_fmt_rate(ev.endorsement_rate)}")
            lines.append(
                "  (helped/endorsement are n/a offline by design: they need a live"
            )
            lines.append(
                "   agent emitting record_use events; fabricating them would just"
            )
            lines.append("   relabel recall.)")

    if report.unavailable:
        lines.append("")
        lines.append("Not run in this environment:")
        for r in report.unavailable:
            lines.append(f"  {r.name:<14} — {r.unavailable_reason}")

    return "\n".join(lines)


def render_json(report: ComparativeReport) -> str:
    return json.dumps(report.to_dict(), indent=2)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tests.eval.comparative",
        description="Run bettermemory's comparative evaluation harness.",
    )
    parser.add_argument(
        "--json", action="store_true", help="emit machine-readable JSON"
    )
    parser.add_argument(
        "--k", type=int, default=5, help="retrieval cutoff for recall@k (default 5)"
    )
    args = parser.parse_args(argv)

    report = run_comparative(default_adapters(), default_workload(), k=args.k)
    print(render_json(report) if args.json else render_text(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
