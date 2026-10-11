"""Samples of reviewed writes for the owner to grade (DESIGN item 14).

`bettermemory review samples` draws answered review chains from a window
and shows, for each, the related set, the answers and the action. Where
the write's body comes from depends on how the chain ended:

- committed: the body from the store;
- refused as a secret (a review-secret refusal, or any credential_warning):
  the kinds only, never the body, from any source;
- any other refusal: the body from the call in the host's transcripts that
  carried one of the chain's review_ids. The CLI can find transcripts of one
  host only, under ``~/.claude/projects``, so a sample from any other host
  carries the related set and the answers without the body.

The draw is stratified: every refusal, link and filing up to half the
sample, then pre-answered writes up to `PRE_ANSWERED_MIN` where that many
exist, then random draws up to the requested size. `bettermemory review
grade` writes the owner's grade as a `review_grade` event row, which
`bettermemory eval` counts under `review.grades`.
"""

from __future__ import annotations

import json
import random
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from . import review as _review
from .credentials import find_credential_markers
from .review_counters import ReviewChain, build_chains, chains_in_window
from .time_utils import isoformat_utc, parse_event_ts

if TYPE_CHECKING:
    from .models import Memory

#: The client value the hook path binds for the one host whose transcripts
#: the CLI can find (`identity.bind_transcript`).
TRANSCRIPT_HOST_CLIENT = "claude-code"

#: The default sample size (P12 grades 40).
DEFAULT_SAMPLE_SIZE = 40
#: How many pre-answered writes the draw holds where that many exist.
PRE_ANSWERED_MIN = 10

STRATUM_FLAGGED = "refusal_link_filing"
STRATUM_PRE_ANSWERED = "pre_answered"
STRATUM_RANDOM = "random"

BODY_STORE = "store"
BODY_TRANSCRIPT = "transcript"
BODY_KINDS_ONLY = "kinds_only"
BODY_OTHER_HOST = "other_host"
BODY_UNAVAILABLE = "unavailable"


def default_transcripts_dir() -> Path:
    """Where the transcript host keeps its session transcripts."""
    return Path.home() / ".claude" / "projects"


def _is_secret_refusal(chain: ReviewChain) -> bool:
    decided = chain.decided
    assert decided is not None
    return (
        decided.action == _review.ACTION_SECRET
        or decided.status == "credential_warning"
    )


def _is_flagged(chain: ReviewChain) -> bool:
    """A refusal, a review link or a review filing."""
    decided = chain.decided
    assert decided is not None
    return (not decided.committed) or bool(decided.links) or bool(decided.filings)


def answered_chains(
    events: Iterable[Mapping[str, Any]],
    *,
    start: datetime | None,
    end: datetime | None,
    now: datetime,
) -> list[ReviewChain]:
    """The reviewed writes of the window: answered chains, oldest first."""
    return [
        chain
        for chain in chains_in_window(events, start=start, end=end, now=now)
        if chain.state == "answered" and chain.decided is not None
    ]


def draw(
    chains: list[ReviewChain], *, n: int, seed: int
) -> list[tuple[ReviewChain, str]]:
    """The stratified draw, as (chain, stratum) in stratum then time order."""
    rng = random.Random(seed)
    n = max(0, n)
    flagged = [c for c in chains if _is_flagged(c)]
    half = n // 2
    if len(flagged) > half:
        flagged = rng.sample(flagged, half)
    chosen: list[tuple[ReviewChain, str]] = [
        (c, STRATUM_FLAGGED) for c in sorted(flagged, key=lambda c: c.issued_at)
    ]
    taken = {c.key for c, _ in chosen}

    have_pre = sum(1 for c, _ in chosen if c.pre_answered)
    pool = [c for c in chains if c.key not in taken and c.pre_answered]
    room = n - len(chosen)
    need = min(max(0, PRE_ANSWERED_MIN - have_pre), room, len(pool))
    picked = rng.sample(pool, need) if need else []
    chosen += [
        (c, STRATUM_PRE_ANSWERED) for c in sorted(picked, key=lambda c: c.issued_at)
    ]
    taken |= {c.key for c in picked}

    rest = [c for c in chains if c.key not in taken]
    room = n - len(chosen)
    extra = rng.sample(rest, min(room, len(rest))) if room > 0 else []
    chosen += [(c, STRATUM_RANDOM) for c in sorted(extra, key=lambda c: c.issued_at)]
    return chosen


# ---------------------------------------------------------------------------
# Transcripts
# ---------------------------------------------------------------------------


def _write_inputs(row: Mapping[str, Any]) -> Iterable[Mapping[str, Any]]:
    """The inputs of the memory_write tool calls in one transcript row."""
    if row.get("type") != "assistant":
        return
    message = row.get("message")
    if not isinstance(message, Mapping):
        return
    content = message.get("content")
    if not isinstance(content, list):
        return
    for block in content:
        if not isinstance(block, Mapping) or block.get("type") != "tool_use":
            continue
        name = block.get("name")
        if not isinstance(name, str) or not name.endswith("memory_write"):
            continue
        tool_input = block.get("input")
        if isinstance(tool_input, Mapping):
            yield tool_input


def transcript_bodies(root: Path, decided_at: Mapping[str, datetime]) -> dict[str, str]:
    """The `content` of the memory_write call that decided each review, from
    the transcripts under `root`, keyed by review_id.

    `decided_at` maps each wanted review_id to its chain's decided time. Of
    the calls carrying a review_id, the deciding one is the latest at or
    before that time: by `timestamp` when every such call carries one, else
    by the order the calls were read. A later call reusing the id never
    decided it. When the deciding call's body is one the credential check
    refuses, no body is returned for that review_id, never an earlier draft
    (DESIGN 14). Reads `*.jsonl` files only and skips any line that is not a
    JSON object."""
    calls: dict[str, list[tuple[datetime | None, str]]] = {}
    if not decided_at or not root.is_dir():
        return {}
    needles = [rid.encode("ascii") for rid in decided_at]
    for path in sorted(root.rglob("*.jsonl")):
        try:
            handle = path.open("rb")
        except OSError:
            continue
        with handle:
            for raw in handle:
                if not any(needle in raw for needle in needles):
                    continue
                try:
                    row = json.loads(raw.decode("utf-8", errors="replace"))
                except ValueError:
                    continue
                if not isinstance(row, Mapping):
                    continue
                stamped = parse_event_ts(row.get("timestamp"))
                for tool_input in _write_inputs(row):
                    answer = tool_input.get("review")
                    content = tool_input.get("content")
                    if not isinstance(answer, Mapping) or not isinstance(content, str):
                        continue
                    review_id = answer.get("review_id")
                    if not isinstance(review_id, str) or review_id not in decided_at:
                        continue
                    if stamped is not None and stamped > decided_at[review_id]:
                        continue
                    calls.setdefault(review_id, []).append((stamped, content))
    found: dict[str, str] = {}
    for review_id, seen in calls.items():
        stamps = [stamped for stamped, _ in seen]
        if all(stamped is not None for stamped in stamps):
            # a stable sort keeps read order among equal stamps
            seen = sorted(seen, key=lambda call: cast(datetime, call[0]))
        content = seen[-1][1]
        if not find_credential_markers(content):
            found[review_id] = content
    return found


# ---------------------------------------------------------------------------
# Samples
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Sample:
    """One reviewed write as the samples command shows it."""

    review_id: str
    stratum: str
    ts: str
    client: str
    pre_answered: bool
    zero_attestation: bool
    closed_by_call: bool
    status: str
    action: str
    related: list[dict[str, Any]]
    unsafe: float | None
    secret: float | None
    links: list[str]
    update_suggested: list[str]
    filings: list[dict[str, str]]
    memory_id: str | None
    kinds: list[str]
    body: str | None
    body_source: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "review_id": self.review_id,
            "stratum": self.stratum,
            "ts": self.ts,
            "client": self.client,
            "pre_answered": self.pre_answered,
            "zero_attestation": self.zero_attestation,
            "closed_by_call": self.closed_by_call,
            "status": self.status,
            "action": self.action,
            "related": self.related,
            "unsafe": self.unsafe,
            "secret": self.secret,
            "links": self.links,
            "update_suggested": self.update_suggested,
            "filings": self.filings,
            "memory_id": self.memory_id,
            "kinds": self.kinds,
            "body": self.body,
            "body_source": self.body_source,
        }


def _related_rows(
    chain: ReviewChain, active: Mapping[str, Memory]
) -> list[dict[str, Any]]:
    decided = chain.decided
    assert decided is not None
    ids = chain.related
    ids += [i for i in decided.relation if i not in ids]
    rows: list[dict[str, Any]] = []
    for memory_id in ids:
        memory = active.get(memory_id)
        rows.append(
            {
                "id": memory_id,
                "answer": decided.relation.get(memory_id),
                "text": (
                    _review.related_text("", memory.body)
                    if memory is not None
                    else None
                ),
            }
        )
    return rows


def build_samples(
    drawn: list[tuple[ReviewChain, str]],
    *,
    active: Mapping[str, Memory],
    transcripts: Path,
) -> list[Sample]:
    """The samples for `drawn`, with each body from the one source its
    outcome allows."""
    wanted: dict[str, datetime] = {}
    for chain, _ in drawn:
        decided = chain.decided
        assert decided is not None
        if (
            not decided.committed
            and not _is_secret_refusal(chain)
            and chain.client == TRANSCRIPT_HOST_CLIENT
        ):
            wanted[decided.review_id] = decided.ts
    bodies = transcript_bodies(transcripts, wanted)

    samples: list[Sample] = []
    for chain, stratum in drawn:
        decided = chain.decided
        assert decided is not None
        body: str | None = None
        kinds: list[str] = []
        if _is_secret_refusal(chain):
            source = BODY_KINDS_ONLY
            kinds = list(decided.credential_kinds) or ["review"]
        elif decided.committed:
            memory = active.get(decided.memory_id or "")
            if memory is not None:
                body, source = memory.body.strip(), BODY_STORE
            else:
                source = BODY_UNAVAILABLE
        elif chain.client != TRANSCRIPT_HOST_CLIENT:
            source = BODY_OTHER_HOST
        else:
            body = bodies.get(decided.review_id)
            source = BODY_TRANSCRIPT if body is not None else BODY_UNAVAILABLE
        samples.append(
            Sample(
                review_id=chain.key,
                stratum=stratum,
                ts=isoformat_utc(decided.ts),
                client=chain.client,
                pre_answered=chain.pre_answered,
                zero_attestation=decided.zero_attestation,
                closed_by_call=chain.closed,
                status=decided.status,
                action=decided.action,
                related=_related_rows(chain, active),
                unsafe=decided.unsafe,
                secret=decided.secret,
                links=list(decided.links),
                update_suggested=list(decided.update_suggested),
                filings=[
                    {"id": target, "detector": detector}
                    for target, detector in decided.filings
                ],
                memory_id=decided.memory_id,
                kinds=kinds,
                body=body,
                body_source=source,
            )
        )
    return samples


def find_answered_chain(
    events: Iterable[Mapping[str, Any]], review_id: str, *, now: datetime
) -> ReviewChain | None:
    """The answered chain any of whose review_ids is `review_id`."""
    for chain in build_chains(events, now=now):
        if chain.absorbed or chain.decided is None:
            continue
        if review_id in chain.review_ids or review_id == chain.decided.review_id:
            return chain
    return None


def render_samples_text(doc: Mapping[str, Any]) -> str:
    """The text form of a samples document."""
    window = doc["window"]
    lines = [
        f"bettermemory review samples: {len(doc['samples'])} of "
        f"{doc['population']} reviewed writes "
        f"({window['since'] or 'the start'} to {window['until'] or 'now'}), "
        f"seed {doc['seed']}",
        "strata "
        + " ".join(f"{name}={count}" for name, count in doc["strata"].items()),
    ]
    for index, sample in enumerate(doc["samples"], start=1):
        lines.append("")
        lines.append(
            f"[{index}] review {sample['review_id']} ({sample['stratum']}) "
            f"{sample['ts']} client {sample['client']}"
        )
        flags = []
        if sample["pre_answered"]:
            flags.append("pre-answered")
        if sample["zero_attestation"]:
            flags.append("zero scores")
        if sample["closed_by_call"]:
            flags.append("closed by a later call")
        lines.append(
            f"  action {sample['action']} (status {sample['status']})"
            + (f", {', '.join(flags)}" if flags else "")
        )
        lines.append(f"  answers unsafe={sample['unsafe']} secret={sample['secret']}")
        if not sample["related"]:
            lines.append("  related: none")
        for row in sample["related"]:
            lines.append(f"  related {row['id']}: {row['answer'] or 'not answered'}")
            if row["text"] is not None:
                lines.append(f"    {row['text']}")
        if sample["links"]:
            lines.append(f"  links {', '.join(sample['links'])}")
        if sample["update_suggested"]:
            lines.append(f"  update_suggested {', '.join(sample['update_suggested'])}")
        for filing in sample["filings"]:
            lines.append(f"  filed {filing['id']} ({filing['detector']})")
        if sample["body_source"] == BODY_KINDS_ONLY:
            lines.append(f"  body withheld, kinds: {', '.join(sample['kinds'])}")
        elif sample["body"] is not None:
            lines.append(f"  body ({sample['body_source']}):")
            lines.extend(f"    {line}" for line in sample["body"].splitlines())
        else:
            lines.append(f"  body not shown ({sample['body_source']})")
    lines.append("")
    lines.append(
        "Grade each with: bettermemory review grade <review_id> "
        "--answers right|wrong --action right|wrong"
    )
    return "\n".join(lines) + "\n"


__all__ = [
    "BODY_KINDS_ONLY",
    "BODY_OTHER_HOST",
    "BODY_STORE",
    "BODY_TRANSCRIPT",
    "BODY_UNAVAILABLE",
    "DEFAULT_SAMPLE_SIZE",
    "PRE_ANSWERED_MIN",
    "STRATUM_FLAGGED",
    "STRATUM_PRE_ANSWERED",
    "STRATUM_RANDOM",
    "Sample",
    "TRANSCRIPT_HOST_CLIENT",
    "answered_chains",
    "build_samples",
    "default_transcripts_dir",
    "draw",
    "find_answered_chain",
    "render_samples_text",
    "transcript_bodies",
]
