"""The conversational lane's temporal reading and the per-hit labels of `search.py`.

The temporal-scaffold class and its df-floor (`_conv_scaffold_terms`,
`_scaffold_floor_stats`), the query's temporal reading
(`_temporal_reading`, `_TemporalReading` and the date and phrase
patterns it matches) and a memory's anchor day (`_memory_anchor_day`).
With them, the per-hit leg and relevance labels: `_matched_leg`,
`_relevance_label` and `_relevance_label_v2`.

The lane's section in `search.py` describes both repairs and keeps what
tests patch: the rerank and the pricing gate, with the constants they
read (`_CONV_WINDOW_BOOST` and kin). `search.py` re-exports the names
tests and other modules import from it.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import NamedTuple

from .models import Memory
from .search_bm25 import CorpusStats
from .search_lexicon import _stem_token


# Closed class of temporal-scaffold SURFACE forms, stemmed through the
# live stemmer at import so membership tests run in ranker token space.
# Generic English temporal syntax plus small numerals — no topical
# vocabulary is admitted (declaration §3 caps the class at 40 stems).
_CONV_SCAFFOLD_SURFACE = (
    "day",
    "days",
    "week",
    "weeks",
    "weekend",
    "month",
    "months",
    "year",
    "years",
    "ago",
    "yesterday",
    "today",
    "last",
    "latest",
    "first",
    "earliest",
    "past",
    "recent",
    "recently",
    "current",
    "currently",
    "many",
    "much",
    "long",
    "total",
    "time",
    "times",
    "since",
    "between",
    "order",
    "passed",
    "once",
    "twice",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
    "twelve",
)
_CONV_SCAFFOLD_STEMS = frozenset(_stem_token(w) for w in _CONV_SCAFFOLD_SURFACE)

# The time-unit subset of the scaffold surface: the only words that can
# license a bare numeral as temporal scaffold. "3 months ago" reads 3 as
# a duration because `months` follows it; "schema 8" and "port 80" have
# no unit to modify, so their numerals stay content terms. Kept as a
# subset of the surface above rather than its own vocabulary — a unit
# that is not already scaffold would reprice a term the class never
# declared.
_CONV_TIME_UNIT_SURFACE = (
    "day",
    "days",
    "week",
    "weeks",
    "weekend",
    "month",
    "months",
    "year",
    "years",
)
_CONV_TIME_UNIT_STEMS = frozenset(_stem_token(w) for w in _CONV_TIME_UNIT_SURFACE)

# df floor for scaffold terms, as a fraction of the ranked collection.
# A floored term still matches and still scores. Tuning read 3 raised
# this from the filler floor's 0.5 to the declared cap: at 1.0 a
# scaffold term prices as a word every document carries — near-zero
# Okapi IDF — so the BM25 legs rank temporal-shaped queries on their
# content terms alone while the keyword leg still counts everything.
_CONV_SCAFFOLD_FLOOR_RATIO = 1.0


_MONTH_NAMES = (
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
)
_MONTH_NO = {name: i + 1 for i, name in enumerate(_MONTH_NAMES)}

# Query-side temporal shapes. Matched against the RAW lowercased query,
# not the token stream — these are phrase shapes, and stemming would
# destroy them.
_CONV_DATE_RE = re.compile(r"\b(\d{4})[/-](\d{2})[/-](\d{2})\b")
# 'may' is the one month name that doubles as a high-frequency function
# word (the modal auxiliary — "you may need sudo"), and the lane is
# default-on over raw user messages (the silent-miss probe feeds them
# verbatim), so a bare-'may' month reading would hand every modal
# sentence a May window and a ×_CONV_WINDOW_BOOST rerank inside the
# near-tie band. The may-branch therefore demands disambiguation the
# genuine month reading carries anyway: an explicit trailing year
# ("may 2024") or a preceding temporal preposition/determiner ("in
# may", "since may", "mid-may"). Every other month name stays bare —
# none of them is a function word, and demanding context there would
# drop real windows. 'and' stays OUT of the context set even though it
# appears in range phrases ("between march and may"): "and may" is the
# modal's own high-frequency bigram ("crashed and may need a restart"),
# and a missed month leg only narrows a window while a phantom May
# window reranks every modal sentence — the asymmetry the lane's
# boost-only design already commits to. The guard is zero-width
# (lookbehinds, each fixed-width as `re` requires, plus one
# lookahead), so the two capture groups keep their (month, year)
# numbering.
_CONV_MAY_CONTEXT_WORDS = (
    "in",
    "last",
    "during",
    "by",
    "until",
    "since",
    "for",
    "next",
    "this",
    "early",
    "late",
    "mid",
)
_CONV_MAY_GUARD = (
    "(?:"
    + "|".join(rf"(?<=\b{w}[\s-])" for w in _CONV_MAY_CONTEXT_WORDS)
    + r"|(?=may\s+\d{4}))"
)
# The residual the context guard cannot see: "this" is a legitimate
# month determiner ("this May we shipped"), so "this may <verb>" — the
# modal's most common written shape — passed the lookbehind and read as
# a May window. Disambiguate on the token AFTER "may": a closed list of
# the words that follow the modal and never a month name (auxiliaries,
# negation, common adverbs, and the verbs that dominate "may <verb>" in
# engineering prose). A closed list rather than a part-of-speech guess
# keeps the lane deterministic; unlisted verbs remain the priced
# residual, bounded to the near-tie window boost the month reading can
# engage. A following year or day number is never on the list, so
# "this may 2026" keeps its window.
_CONV_MAY_MODAL_FOLLOWERS = (
    "be",
    "have",
    "has",
    "not",
    "also",
    "still",
    "never",
    "just",
    "only",
    "even",
    "already",
    "well",
    "actually",
    "need",
    "needs",
    "want",
    "require",
    "requires",
    "cause",
    "take",
    "help",
    "seem",
    "seems",
    "work",
    "look",
    "get",
    "become",
    "make",
    "break",
    "fail",
    "mean",
    "differ",
    "vary",
    "change",
    "depend",
    "use",
    "include",
    "apply",
    "affect",
    "result",
    "lead",
    "contain",
    "involve",
    "run",
    "return",
    "throw",
    "raise",
    "crash",
    "hang",
    "conflict",
    "explain",
    "indicate",
    "suggest",
    "show",
    "appear",
    "exist",
    "occur",
    "happen",
    "come",
    "go",
    "do",
    "prove",
    "turn",
    "end",
    "start",
    "stop",
    "block",
    "prevent",
    "allow",
    "let",
    "keep",
    "hold",
    "lose",
    "miss",
    "skip",
    "ignore",
    "override",
    "expire",
    "drop",
    "leak",
    "matter",
    "sound",
    "feel",
)
_CONV_MAY_MODAL_LOOKAHEAD = r"(?!\s+(?:" + "|".join(_CONV_MAY_MODAL_FOLLOWERS) + r")\b)"
_CONV_MONTH_RE = re.compile(
    r"\b((?:"
    + "|".join(n for n in _MONTH_NAMES if n != "may")
    + r")|"
    + _CONV_MAY_GUARD
    + r"may"
    + _CONV_MAY_MODAL_LOOKAHEAD
    + r")\b(?:\s+(\d{4}))?"
)
_CONV_LAST_PERIOD_RE = re.compile(r"\blast\s+(week|month|year)\b")
_CONV_THIS_PERIOD_RE = re.compile(r"\bthis\s+(week|month|year)\b")
_CONV_YESTERDAY_RE = re.compile(r"\byesterday\b")
# "N days/weeks/months/years ago" as a STATEMENT of when — a window one
# unit wide centred on the rollback. The elapsed-ASK shape ("how many
# days ago did I…") is deliberately excluded: there the count is the
# question, not a constraint, and `_CONV_ELAPSED_RE` owns that reading.
_CONV_AGO_RE = re.compile(
    r"\b(?<!how many )(?<!how much )"
    r"(a|an|one|two|three|four|five|six|seven|eight|nine|ten|\d{1,2})\s+"
    r"(day|week|month|year)s?\s+ago\b"
)
_CONV_AGO_NUMBERS = {
    "a": 1,
    "an": 1,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
}
_CONV_AGO_UNIT_DAYS = {"day": 1, "week": 7, "month": 30, "year": 365}
_CONV_ELAPSED_RE = re.compile(
    r"\bhow\s+(?:many|much)\s+(?:days?|weeks?|months?|years?|time)\b"
    r"|\bhow\s+long\b"
)
_CONV_ORDER_RE = re.compile(
    r"\b(?:the\s+)?order\s+(?:of|from)\b|\bearliest\s+to\s+latest\b"
    r"|\bfirst\s+to\s+last\b|\bchronolog"
    r"|\bwhich\s+.{0,80}?(?:first|last)\b"
)
_CONV_WHEN_RE = re.compile(r"^when\b|\bwhen\s+did\b|\bwhat\s+(?:day|date|month)\b")
# 'last' used adverbially about the user's own action selects the LATEST
# narration ("since I last visited", "the last time I…"); 'last' as a
# period word ("last month") is a window, and 'last' inside an order ask
# ("first to last") is neither — the window and order regexes above
# consume those readings first.
_CONV_LATEST_RE = re.compile(
    r"\b(?:i|we)\s+last\b|\blast\s+time\b|\bmost\s+recent(?:ly)?\b"
)

# Memory-side anchors: the leading bracketed date line conversational
# ingest writes, else the first ISO-ish date early in the body.
_CONV_ANCHOR_HEAD_RE = re.compile(r"^\[(\d{4})[/-](\d{2})[/-](\d{2})")
_CONV_ANCHOR_SCAN_CHARS = 200


class _TemporalReading(NamedTuple):
    """A query's parsed temporal shape — `None`-free sentinel via fields.

    `window` is a closed [start, end] day range when the query names one
    (a month, a date, "last month"); `selector` is `"earliest"` /
    `"latest"` when the query's shape picks a narration by date order.
    Both empty means the query is not temporal and the lane must not
    touch anything.
    """

    window: tuple[date, date] | None
    selector: str | None

    @property
    def is_temporal(self) -> bool:
        return self.window is not None or self.selector is not None


def _month_window(month: int, year: int) -> tuple[date, date]:
    start = date(year, month, 1)
    end = (
        date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    ) - timedelta(days=1)
    return start, end


def _temporal_reading(query: str, now: datetime) -> _TemporalReading:
    """Parse the query's temporal shape against the caller's clock.

    Windows collected from every explicit form are merged to their
    envelope (min start, max end), which is what makes "between March
    and May" one range instead of two competing ones. Relative periods
    resolve calendar-correct against `now` — "last month" is the
    previous calendar month, not a 30-day rollback. Selector precedence
    per the declaration: an explicit window wins over a selector; the
    latest-selector's adverbial-'last' reading wins over the earliest
    default when both shapes appear.
    """
    lower = query.lower()
    today = now.date()
    windows: list[tuple[date, date]] = []

    for m in _CONV_DATE_RE.finditer(lower):
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            try:
                windows.append((date(y, mo, d), date(y, mo, d)))
            except ValueError:
                pass
    for m in _CONV_MONTH_RE.finditer(lower):
        month = _MONTH_NO[m.group(1)]
        if m.group(2):
            year = int(m.group(2))
        else:
            # The most recent occurrence of that month not after now.
            year = today.year if month <= today.month else today.year - 1
        try:
            windows.append(_month_window(month, year))
        except ValueError:
            # An explicit year outside date's range — "december 9999"
            # (the never-expires sentinel, so it shows up in ordinary
            # cert/expiry talk; month 12 touches year+1) or "january
            # 0000" — is not a window, same as the sibling date branch.
            pass
    for m in _CONV_LAST_PERIOD_RE.finditer(lower):
        unit = m.group(1)
        if unit == "week":
            start_this = today - timedelta(days=today.weekday())
            windows.append(
                (start_this - timedelta(days=7), start_this - timedelta(days=1))
            )
        elif unit == "month":
            prev_last = date(today.year, today.month, 1) - timedelta(days=1)
            windows.append(_month_window(prev_last.month, prev_last.year))
        else:
            windows.append((date(today.year - 1, 1, 1), date(today.year - 1, 12, 31)))
    for m in _CONV_THIS_PERIOD_RE.finditer(lower):
        unit = m.group(1)
        if unit == "week":
            start_this = today - timedelta(days=today.weekday())
            windows.append((start_this, start_this + timedelta(days=6)))
        elif unit == "month":
            windows.append(_month_window(today.month, today.year))
        else:
            windows.append((date(today.year, 1, 1), date(today.year, 12, 31)))
    if _CONV_YESTERDAY_RE.search(lower):
        yday = today - timedelta(days=1)
        windows.append((yday, yday))
    for m in _CONV_AGO_RE.finditer(lower):
        count = _CONV_AGO_NUMBERS.get(m.group(1)) or int(m.group(1))
        unit_days = _CONV_AGO_UNIT_DAYS[m.group(2)]
        target = today - timedelta(days=count * unit_days)
        half = max(1, unit_days // 2)
        windows.append((target - timedelta(days=half), target + timedelta(days=half)))

    window: tuple[date, date] | None = None
    if windows:
        window = (min(w[0] for w in windows), max(w[1] for w in windows))

    selector: str | None = None
    if _CONV_ORDER_RE.search(lower):
        selector = "earliest"
    elif _CONV_ELAPSED_RE.search(lower) or _CONV_WHEN_RE.search(lower):
        selector = "latest" if _CONV_LATEST_RE.search(lower) else "earliest"
    elif _CONV_LATEST_RE.search(lower):
        selector = "latest"

    return _TemporalReading(window=window, selector=selector)


def _conv_scaffold_terms(tokens: list[str]) -> list[str]:
    """The query tokens the scaffold floor reprices: the closed class
    plus bare small numerals that actually read as a duration.

    A one- or two-digit token counts as scaffold only when a time unit
    IMMEDIATELY FOLLOWS it — the `NUM + UNIT` shape of "3 months ago" or
    "the last 2 weeks". Bare numerals carry the discriminating signal in
    this store's everyday vocabulary ("schema 8", "port 80", "issue 12"),
    and flooring their df to the whole collection collapses their IDF to
    roughly zero, which drops the one memory that names the number off
    the result page. Adjacency alone is too weak a test: in "what about
    84 last week" the numeral neighbours a scaffold stem without
    modifying it, so the following token must be the unit itself.

    Four-digit years are a window constraint, never scaffold, and dotted
    version literals never match `isdigit`.

    `tokens` is read IN ORDER, so callers must pass the query's token
    sequence rather than a deduplicated set — dedup silently moves the
    neighbour that licenses a numeral.
    """
    out: list[str] = []
    for i, t in enumerate(tokens):
        if t in _CONV_SCAFFOLD_STEMS:
            out.append(t)
        elif t.isdigit() and len(t) <= 2:
            following = tokens[i + 1] if i + 1 < len(tokens) else None
            if following in _CONV_TIME_UNIT_STEMS:
                out.append(t)
    return out


def _scaffold_floor_stats(
    base: CorpusStats | None, terms: list[str], pool_n: int
) -> CorpusStats | None:
    """CorpusStats with the scaffold df-floor applied for `terms`.

    The filler floor's exact mechanics (`_filler_floor_stats`, which
    see) pointed at the temporal-scaffold class instead: floor entries
    override per term through `compute_idf`, `max(real df, floor)`
    keeps the honest direction, and a query with no scaffold terms
    returns `base` untouched, allocation-free.
    """
    present = _conv_scaffold_terms(terms)
    if not present:
        return base
    size = base.size if base is not None else pool_n
    if size <= 0:
        return base
    floor = max(1, int(size * _CONV_SCAFFOLD_FLOOR_RATIO))
    body_df = dict(base.body_df) if base is not None else {}
    scope_df = dict(base.scope_df) if base is not None else {}
    for t in present:
        body_df[t] = max(body_df.get(t, 0), floor)
        scope_df[t] = max(scope_df.get(t, 0), floor)
    return CorpusStats(size=size, body_df=body_df, scope_df=scope_df)


def _memory_anchor_day(memory: Memory) -> date:
    """The day a memory's content is anchored to.

    In order: the leading bracketed date line conversational ingest
    writes, else the first ISO-ish date within the body's first
    `_CONV_ANCHOR_SCAN_CHARS` characters, else the day the memory was
    created — the product fallback, where `created` IS the best-known
    event time.
    """
    m = _CONV_ANCHOR_HEAD_RE.match(memory.body)
    if m is None:
        m = _CONV_DATE_RE.search(memory.body[:_CONV_ANCHOR_SCAN_CHARS])
    if m is not None:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            try:
                return date(y, mo, d)
            except ValueError:
                pass
    created = memory.created
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return created.date()


# `matched_leg` vocabulary: WHICH RANKER surfaced a hit. Reported per hit
# alongside `relevance` so the caller can read the label in the light of
# how the hit was found — evidence about the retrieval, not a second
# verdict about the memory.
#
# Since 4.0.0 every ranker is lexical (keyword, BM25, their fusion), so
# the base label's live value is `lexical`; the field survives so
# callers keyed on it keep parsing, and so the vocabulary has somewhere
# to grow if a future CODE ranker earns a leg of its own. The rescue-
# expansion leg (still deterministic lexical code, but matching
# SYNTHESIZED vocabulary rather than the caller's words) is that
# growth: a hit surfaced ONLY by it reports `expansion`, which is what
# keeps `relevance` readable — coverage of the caller's own tokens is
# legitimately "low" for a hit found through a synonym, and the leg
# says so instead of leaving the label looking broken.
LEG_LEXICAL = "lexical"
LEG_EXPANSION = "expansion"


def _matched_leg(*, lexical: bool, expansion: bool = False) -> str:
    """Leg label for one hit; `""` when no ranker ran at all.

    `lexical` wins when both are set — the base legs matched the
    caller's own words, which is the stronger statement; `expansion`
    is reported only for hits NO base leg scored. The empty case is
    browse mode (`allow_empty_query` with no query tokens): candidates
    are filtered and date-sorted, never ranked, so there is no leg to
    report and the field is omitted rather than guessed. The leg
    reports what RAN, not what was requested.
    """
    if lexical:
        return LEG_LEXICAL
    if expansion:
        return LEG_EXPANSION
    return ""


def _relevance_label(matched_unique: int, query_unique: int) -> str:
    """Map coverage (fraction of distinct query terms that hit) to a label.

    Calibrated for short queries: matching 1/1 or 2/2 is "high"; matching 1/3
    is "low". The thresholds are deliberately generous on the high side
    because a 1-word query with a strong match shouldn't be downgraded.

    NEGATIVE RESULT, 2026-07-30 — the recut that was NOT shipped. The
    plan for this label was to stop scoring pure-paraphrase hits by lexical
    coverage and label them from calibrated cosine bands instead, gated
    on the telemetry_v2 shadow replay: no user-message-length bucket at a
    0% high-rate, and a max/min bucket spread under 3x. Replaying the
    dogfood store's `turn_audited` log (317 turns, 274 carrying hits;
    the harness reproduces both logged labels exactly, so it is measuring
    the shipped rules and not a paraphrase of them) returns:

        user message chars     0-40   40-80   80-150   150+
        turns                    65      79       63      67
        v1 "high" rate          43%     25%       5%      4%

    and — the finding that closed the item — **0 of those 274 top hits
    carried `matched_unique == 0`**. The store runs the default install,
    so no semantic leg ever ran (that leg was removed outright in 4.0.0), so the population a cosine-band rule
    changes has ZERO representation in the instrument the bar is measured
    on. A semantic-only rule is provably byte-identical on that corpus:
    it cannot clear the 3x bar, and it cannot fail it either. Shipping
    bands anyway would have been calibrating a verdict on taste, which is
    the one thing this label's history says not to do (see
    `_relevance_label_v2`). So v1 stands, `matched_leg` ships instead of
    a new number, and `search` events now carry the leg — that is the
    instrument whose absence made the question unanswerable.

    Worth recording for whoever re-opens this: on the same 274 turns the
    v2 rule now profiles 46/62/86/100, i.e. no zero bucket and a 2.17x
    spread, so it PASSES the stated bar while remaining the
    length-credulous rule this project measured and rejected. The bar is
    a necessary condition, not a sufficient one — exactly what
    `_relevance_label_v2` says about the screen it comes from. Do not use
    it alone as a gate.
    """
    if query_unique <= 0:
        return "low"
    coverage = matched_unique / query_unique
    if coverage >= 0.75:
        return "high"
    if coverage >= 0.40:
        return "medium"
    return "low"


# Absolute matched-token floor for the v2 relevance label's "high" arm.
# Four distinct content tokens landing in one memory is strong evidence
# regardless of query length; the value deliberately mirrors
# `attribution._MIN_CONTAINMENT_TOKENS` — both gates answer "how many
# distinct content-token overlaps constitute a deliberate connection
# rather than coincidence" (cross-pinned in tests).
_V2_HIGH_MATCHED_FLOOR = 4


def _relevance_label_v2(matched_unique: int, query_unique: int) -> str:
    """Candidate successor to `_relevance_label` — SHADOW-ONLY.

    Same coverage mapping, plus an absolute matched-count floor on the
    "high" arm: a long natural-language query that matches >=
    `_V2_HIGH_MATCHED_FLOOR` distinct content tokens labels "high" even
    when the coverage FRACTION dips below 0.75 — the denominator grows
    with query length, the evidence doesn't shrink. This is the
    structural candidate fix for audit.py's documented blind spot
    ("multi-token natural language with stopwords often lands at
    2/3 = medium and does not fire").

    Shadow contract: the v2 label is logged on `search` /
    `turn_audited` / `search_miss` events for calibration and NEVER
    surfaced in an MCP response — live behavior (expand_top, the miss
    threshold rule) stays on v1 until the logged v1/v2 disagreement
    data justifies a flip. v1-high implies v2-high (the fraction arm is
    unchanged), so v2 is a strict widening.

    THAT DATA NOW EXISTS, AND IT SAYS DO NOT FLIP. Measured 2026-07-25
    over 195 `turn_audited` events carrying both labels, from a
    185-memory dogfood store. Bucketing the SAME turns by the length of
    the user message that produced them:

        user message chars     0-40   40-80   80-150   150+
        v1 "high" rate          45%     32%       0%     3%
        v2 "high" rate          47%     63%      83%   100%

    Neither label is measuring relevance; both are measuring LENGTH, in
    opposite directions. v1's coverage fraction has a denominator that
    grows with the query, so a long message can no longer clear 0.75 and
    the rate collapses to zero — the documented blind spot this function
    was written to close. But the floor that closes it replaces a
    length-BLIND rule with a length-CREDULOUS one: four distinct content
    tokens landing somewhere in a 185-document store is not evidence
    about a specific memory, it is a near-certainty for any message long
    enough, which is why the v2 arm reaches 100% and stays there.

    Flipping the miss rule to v2 would have taken this store from 11
    flagged misses to 105 of 195 turns. Reading the 95 newly-flagged
    previews is the fastest way to see it: they are dominated by bare
    continuations ("sorry continue, 5 hour limit cap" x6, "sorry about
    that, i restarted the server" x8) and ordinary work turns ("fix the
    readme" x4, "improve the slogans", "dude this svg sucks") — turns
    with no memory to miss.

    A rule's flag rate not tracking message length is a necessary
    condition, not a sufficient one, and it is checkable without ground
    truth, which is why it is the screen used here. Same 195 turns,
    max-minus-min flag rate across those four buckets: v1 45%, v2 53%
    (worse than what it replaces, and the only candidate that climbs
    monotonically to 100%), `matched>=4` alone 92%, and the CONJUNCTIVE
    form `coverage>=0.75 AND matched>=4` 29% at a 11.3% flag rate. So
    the matched-token count is worth keeping and the `or` is the defect:
    it lets the floor overrule the fraction instead of corroborating it.
    That conjunction is the candidate a v5 should be calibrated from —
    against labelled turns, since this screen can only rank candidates,
    never confirm one. Caveat that travels with every number here: one
    store, one user, n=195.
    """
    if query_unique <= 0:
        return "low"
    coverage = matched_unique / query_unique
    if coverage >= 0.75 or matched_unique >= _V2_HIGH_MATCHED_FLOOR:
        return "high"
    if coverage >= 0.40:
        return "medium"
    return "low"
