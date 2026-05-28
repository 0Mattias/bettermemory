"""The fixed synthetic workload the comparative harness runs against.

A workload is a small corpus of memories plus a list of probes. Each probe
is a user message paired with the memory it *should* surface (``gold_id``,
``None`` for a deliberate distractor) and whether the simulated agent had
already retrieved this turn (``agent_searched``). Those two facts are what
``probe_for_miss`` crosses to decide a silent-miss verdict, so they fully
determine the expected audit outcome:

    expects a silent miss  ==  gold_id is not None  and  not agent_searched

The corpus uses deliberately distinctive vocabulary per fact so the ranker
has an unambiguous top-1 for every gold probe and distractor queries match
nothing at high relevance. That keeps the expected metrics deterministic
without the test having to pin ranker internals.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from bettermemory.models import Category, Confidence, Memory, Source, generate_ulid

# Fixed clock for the whole workload so memory timestamps and probe `now`
# are stable across runs (recency weighting in the ranker is then constant).
BASE_NOW = datetime(2026, 5, 28, 12, 0, 0, tzinfo=timezone.utc)


@dataclass(frozen=True)
class WorkloadFact:
    """One memory in the corpus, before it becomes a `Memory` record."""

    key: str  # stable human handle, e.g. "pytest"; not the ULID
    scopes: list[str]
    body: str
    category: Category = Category.FACT


@dataclass(frozen=True)
class WorkloadProbe:
    """One user message the harness audits.

    `gold_key` names the fact this query should retrieve (`None` for a
    distractor). `agent_searched` simulates whether the agent already hit
    the store this turn — when True, `probe_for_miss` returns ``ok`` even
    for a high-relevance hit, because the retrieval contract was honored.
    """

    query: str
    gold_key: str | None
    agent_searched: bool
    note: str = ""

    @property
    def expects_miss(self) -> bool:
        """True when this probe should register as a silent miss.

        Mirrors the v1 audit rule's preconditions that the workload
        controls: a retrievable gold memory exists and the agent did not
        search. (The remaining precondition — top-1 hit scores ``high`` —
        is the ranker's job; the corpus is built so it always holds for a
        gold probe.)
        """
        return self.gold_key is not None and not self.agent_searched


@dataclass
class Workload:
    """A named corpus + probe set, plus the id mapping built at construction."""

    name: str
    facts: list[WorkloadFact]
    probes: list[WorkloadProbe]
    # key -> generated ULID, frozen at construction so `gold_key` lookups
    # resolve to the same id the memories carry.
    _ids: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self._ids:
            self._ids = {f.key: generate_ulid() for f in self.facts}
        self._validate()

    def _validate(self) -> None:
        keys = {f.key for f in self.facts}
        if len(keys) != len(self.facts):
            raise ValueError("workload fact keys must be unique")
        for p in self.probes:
            if p.gold_key is not None and p.gold_key not in keys:
                raise ValueError(f"probe gold_key {p.gold_key!r} has no matching fact")

    def gold_id(self, probe: WorkloadProbe) -> str | None:
        """Resolve a probe's `gold_key` to the ULID its memory carries."""
        if probe.gold_key is None:
            return None
        return self._ids[probe.gold_key]

    def memories(self) -> list[Memory]:
        """Materialize the corpus as `Memory` records on the fixed clock."""
        out: list[Memory] = []
        for i, f in enumerate(self.facts):
            # Stagger created times slightly so the corpus isn't a single
            # timestamp; all well inside the recency window so the ranker
            # treats them comparably.
            created = BASE_NOW - timedelta(days=i + 1)
            out.append(
                Memory(
                    id=self._ids[f.key],
                    created=created,
                    updated=created,
                    scopes=list(f.scopes),
                    confidence=Confidence.HIGH,
                    source=Source.EXPLICIT,
                    body=f.body,
                    category=f.category,
                )
            )
        return out

    @property
    def gold_probes(self) -> list[WorkloadProbe]:
        return [p for p in self.probes if p.gold_key is not None]

    @property
    def expected_miss_probes(self) -> list[WorkloadProbe]:
        return [p for p in self.probes if p.expects_miss]


def default_workload() -> Workload:
    """The standard comparative workload: 10 facts, 10 probes.

    Probe design (expected verdicts in parentheses):
      - 5 gold + not-searched  -> silent miss
      - 2 gold + searched      -> ok (retrieval contract honored)
      - 3 distractor           -> ok / no_signal (nothing relevant stored)

    So a correct run yields recall@5 == 1.0 over the 7 gold probes and a
    silent_miss_rate numerator of 5 over a denominator of 10.
    """
    facts = [
        WorkloadFact(
            key="pytest",
            scopes=["projects:demo"],
            body=(
                "User prefers pytest over unittest for Python testing; "
                "never introduce the unittest TestCase style."
            ),
        ),
        WorkloadFact(
            key="branch",
            scopes=["projects:demo"],
            body=(
                "The default git branch for this repository is trunk, "
                "not main or master."
            ),
        ),
        WorkloadFact(
            key="migrations",
            scopes=["projects:demo"],
            body=(
                "Database schema migrations live in the alembic versions "
                "directory and run automatically on deploy."
            ),
        ),
        WorkloadFact(
            key="formatter",
            scopes=["tools"],
            body=(
                "Code formatting is enforced by ruff format; black is "
                "forbidden in this codebase."
            ),
        ),
        WorkloadFact(
            key="os",
            scopes=["personal-context"],
            body=(
                "User develops on macOS Sequoia while continuous "
                "integration executes on Linux runners."
            ),
        ),
        WorkloadFact(
            key="deploy",
            scopes=["infrastructure"],
            body=(
                "Production deploys happen through the Argo rollouts "
                "pipeline every Tuesday afternoon."
            ),
        ),
        WorkloadFact(
            key="secrets",
            scopes=["infrastructure"],
            body=(
                "API secrets are stored in the Vault kv engine and are "
                "never committed to the repository."
            ),
        ),
        WorkloadFact(
            key="review",
            scopes=["user-inference"],
            body="User wants pull requests kept under four hundred lines for reviewability.",
            category=Category.USER_INFERENCE,
        ),
        WorkloadFact(
            key="lang",
            scopes=["projects:demo"],
            body=(
                "Backend services are written in Rust; the legacy billing "
                "module remains in Python."
            ),
        ),
        WorkloadFact(
            key="adr",
            scopes=["projects:demo"],
            body=(
                "Architecture decision records are kept in the docs adr "
                "folder as numbered markdown files."
            ),
        ),
    ]

    probes = [
        # gold + not searched -> silent miss
        WorkloadProbe(
            "pytest unittest python testing",
            "pytest",
            agent_searched=False,
            note="testing-style preference, agent never searched",
        ),
        WorkloadProbe(
            "default git branch trunk repository",
            "branch",
            agent_searched=False,
            note="default branch, agent never searched",
        ),
        WorkloadProbe(
            "alembic migrations versions directory deploy",
            "migrations",
            agent_searched=False,
            note="migration location, agent never searched",
        ),
        WorkloadProbe(
            "vault secrets kv engine repository",
            "secrets",
            agent_searched=False,
            note="secret storage, agent never searched",
        ),
        WorkloadProbe(
            "argo rollouts deploys pipeline tuesday",
            "deploy",
            agent_searched=False,
            note="deploy cadence, agent never searched",
        ),
        # gold + searched -> ok (agent honored the retrieval contract)
        WorkloadProbe(
            "ruff format black formatting codebase",
            "formatter",
            agent_searched=True,
            note="formatter rule, agent already searched this turn",
        ),
        WorkloadProbe(
            "macos sequoia linux continuous integration runners",
            "os",
            agent_searched=True,
            note="dev/CI platforms, agent already searched this turn",
        ),
        # distractors -> nothing relevant stored
        WorkloadProbe(
            "kubernetes helm chart ingress controller",
            None,
            agent_searched=False,
            note="distractor: no k8s memory exists",
        ),
        WorkloadProbe(
            "redis caching eviction policy latency",
            None,
            agent_searched=False,
            note="distractor: no caching memory exists",
        ),
        WorkloadProbe(
            "graphql subscription websocket resolver",
            None,
            agent_searched=False,
            note="distractor: no graphql memory exists",
        ),
    ]

    return Workload(name="default-coding-agent", facts=facts, probes=probes)
