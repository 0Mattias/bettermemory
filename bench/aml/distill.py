"""The distilled memory layer (declaration E1): compact, dated memory units
derived from a conversation, with no model anywhere.

WHY. On LongMemEval-S dev under the served configuration, 52 of the 59
wrong answers had every evidence session in what Search returned
(bench/aml/results/dev-C0.json). The answer model had the evidence and
missed it, because the fact a question needs is usually one sentence the
user dropped in passing ("I just started 'The Nightingale' today") inside a
round the assistant's long reply dominates, and the reader is handed about
88,000 characters of such rounds. A unit is that one sentence, lifted out,
dated, and served first.

WHAT A UNIT IS. A declarative first-person sentence from a USER turn: a
statement the user made about themselves, their preferences, their events,
their numbers. Questions and requests are not units ("Can you recommend
...", "I'm looking for ..."): they say what the user asked, not what is
true of them. Relative time is resolved against the message's own
timestamp and written beside the phrase ("yesterday [= 2023/05/19 (Fri)]"),
so a reader doing date arithmetic works from dates, not from phrases.

Everything here is deterministic string work: the same messages give the
same units.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

_SENTENCE = re.compile(r"(?<=[.!?])[\"')\]]*\s+|\n+")
_FIRST_PERSON = re.compile(
    r"\b(i|i'm|i've|i'd|i'll|im|ive|my|me|mine|we|we're|we've|our|us)\b", re.I
)
_REQUEST_START = re.compile(
    r"^\s*(?:hi|hello|hey|thanks|thank you|ok|okay|so|well|also|and|but|actually|by the way)?[\s,!.]*"
    r"(?:can|could|would|will|should|do|does|did|is|are|was|were|what|what's|how|why|where|"
    r"when|which|who|whom|whose|please|help|tell|give|show|suggest|recommend|explain|list|"
    r"let's|let me know|any|i'm looking for|i am looking for|i'm trying to find|i need|"
    r"i want to know|i'd like to know|i was wondering|i wonder|i'm wondering|i'd love some|"
    r"i'd like some|i need some|i'm interested in learning)\b",
    re.I,
)
_PREFERENCE = re.compile(
    r"\b(?:love|loved|like|liked|enjoy|enjoyed|prefer|preferred|hate|hated|dislike|"
    r"can't stand|cannot stand|favorite|favourite|fan of|into|obsessed|allergic|"
    r"usually|always|never|tend to|rather|avoid|vegan|vegetarian)\b",
    re.I,
)
_SUGGESTION_QUERY = re.compile(
    r"\b(?:recommend|recommendation|recommendations|suggest|suggestion|suggestions|ideas?|"
    r"tips?|advice|any\b.*\bfor me|what should|should i|help me (?:choose|pick|find|decide)|"
    r"what (?:else )?can i|looking for)\b",
    re.I,
)

_NUM_WORDS = {
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
    "eleven": 11,
    "twelve": 12,
    "a couple of": 2,
    "a couple": 2,
    "couple of": 2,
    "a few": 3,
    "few": 3,
}
_WEEKDAYS = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)
_AGO = re.compile(
    r"\b(\d+|a couple of|a couple|couple of|a few|few|an|a|one|two|three|four|five|six|"
    r"seven|eight|nine|ten|eleven|twelve)\s+(day|week|month|year)s?\s+ago\b",
    re.I,
)
_SIMPLE = re.compile(
    r"\b(today|tonight|this morning|this afternoon|this evening|yesterday|last night|"
    r"tomorrow|last week|last weekend|this weekend|last month|last year|next week|"
    r"next weekend|next month)\b",
    re.I,
)
_LAST_WEEKDAY = re.compile(
    r"\b(last|this past|past|next|this coming|coming)\s+("
    + "|".join(_WEEKDAYS)
    + r")\b",
    re.I,
)


def _day(dt: datetime) -> str:
    return dt.strftime("%Y/%m/%d (%a)")


def _num(word: str) -> int:
    w = word.lower()
    return int(w) if w.isdigit() else _NUM_WORDS.get(w, 1)


def resolve_times(text: str, ts_ms: int | None) -> str:
    """Write the absolute date beside every relative time phrase the
    message's own timestamp can anchor. Unanchored text is returned as is."""
    if ts_ms is None:
        return text
    now = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)

    def simple(m: re.Match[str]) -> str:
        p = m.group(1).lower()
        if p in ("today", "tonight", "this morning", "this afternoon", "this evening"):
            val = _day(now)
        elif p in ("yesterday", "last night"):
            val = _day(now - timedelta(days=1))
        elif p == "tomorrow":
            val = _day(now + timedelta(days=1))
        elif p == "last week":
            val = f"the week of {_day(now - timedelta(days=7))}"
        elif p == "next week":
            val = f"the week of {_day(now + timedelta(days=7))}"
        elif p in ("last weekend", "this weekend", "next weekend"):
            back = (now.weekday() - 5) % 7 or 7  # the Saturday before now
            sat = now - timedelta(days=back)
            if p == "next weekend":
                sat = sat + timedelta(days=7)
            elif p == "this weekend" and now.weekday() < 5:
                sat = sat + timedelta(days=7)
            val = f"the weekend of {_day(sat)}"
        elif p == "last month":
            first = now.replace(day=1) - timedelta(days=1)
            val = first.strftime("%B %Y")
        elif p == "next month":
            nxt = (now.replace(day=28) + timedelta(days=4)).replace(day=1)
            val = nxt.strftime("%B %Y")
        elif p == "last year":
            val = str(now.year - 1)
        else:
            return m.group(0)
        return f"{m.group(0)} [= {val}]"

    def ago(m: re.Match[str]) -> str:
        n, unit = _num(m.group(1)), m.group(2).lower()
        if unit == "day":
            val = _day(now - timedelta(days=n))
        elif unit == "week":
            val = f"about {_day(now - timedelta(days=7 * n))}"
        elif unit == "month":
            val = f"about {(now - timedelta(days=30 * n)).strftime('%B %Y')}"
        else:
            val = f"about {now.year - n}"
        return f"{m.group(0)} [= {val}]"

    def weekday(m: re.Match[str]) -> str:
        rel, name = m.group(1).lower(), m.group(2).lower()
        target = _WEEKDAYS.index(name)
        if rel in ("next", "this coming", "coming"):
            delta = (target - now.weekday()) % 7 or 7
            val = _day(now + timedelta(days=delta))
        else:
            delta = (now.weekday() - target) % 7 or 7
            val = _day(now - timedelta(days=delta))
        return f"{m.group(0)} [= {val}]"

    out = _AGO.sub(ago, text)
    out = _LAST_WEEKDAY.sub(weekday, out)
    return _SIMPLE.sub(simple, out)


def sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE.split(text or "") if s and s.strip()]


def is_unit(sentence: str) -> bool:
    s = sentence.strip()
    if len(s) < 12 or len(s) > 600 or s.endswith("?"):
        return False
    if not _FIRST_PERSON.search(s):
        return False
    return not _REQUEST_START.match(s)


def units_of(role: str, content: str, ts_ms: int | None) -> list[tuple[str, str]]:
    """(kind, text) for every unit in one message. Only user turns carry
    units: the assistant's replies are advice, not facts about the user."""
    if role != "user":
        return []
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for s in sentences(content):
        if not is_unit(s):
            continue
        key = s.lower()
        if key in seen:
            continue
        seen.add(key)
        kind = "preference" if _PREFERENCE.search(s) else "fact"
        out.append((kind, resolve_times(s, ts_ms)))
    return out


def wants_suggestions(query: str) -> bool:
    return bool(_SUGGESTION_QUERY.search(query or ""))
