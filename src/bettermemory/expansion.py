"""Committed vocabulary tables for query-time rescue expansion.

The retrieval campaign's Phase-1 lane (see docs/ROADMAP and
bench/retrieval/README.md): the measured gap between a casual query and
a stored fact is VOCABULARY — the query says "toggles", the memory says
"feature flags" — and on the gold set that gap is worth 25 recall@1
points (60% semantic-arm vs 35% lexical, both corpora). These tables
attack it in deterministic code, under the WaC rules the project ships
by ("the code is the model"):

- every entry is readable source, reviewable in a diff — no derived
  binaries, no downloads, no network at any point;
- each table states what class of miss it closes and why its entries
  are safe; growing one is an ordinary reviewed edit;
- everything here is QUERY-side. The persisted index stream
  (`fts_index_text`) never changes, so no `tokenizer_fingerprint` /
  schema-version ceremony attaches to a table edit.

Consumers: `search.search()` builds stemmed lookup structures once via
`build_tables` (the stemmer lives in `search.py`; taking it as a
callable keeps this module import-cycle-free and lets tests drive the
raw tables with an identity stem). Nothing else may import the raw
tables for ranking — one build site is what keeps query-side and
test-side views identical.

Measurement provenance for every constant here: the probe grid recorded
in bench/retrieval/README.md (2026-08-09) — recall@1/@5 as-asked
35%/60% -> 50%/90% with requery byte-stable at 80%/100% on the
technical-prose gold set. The lane ships OPT-IN (`[behavior]
rescue_expansion`, default off): its preregistered held-out check on
LongMemEval's conversational stores killed default-on — inflection
variants of common chat verbs are promiscuous matchers there, the
inverse of a technical corpus where expansion vocabulary is rare and
discriminating. The kill, the ablation that isolated it to the
expansion leg, and the experiment that could earn the default back
(df-gating the EMITTED terms) live in bench/longmemeval/ and the
bench/retrieval README's 5.1 section.
"""

from __future__ import annotations

from typing import Callable, NamedTuple

# Discourse filler that appears in real retrieval questions ("i vaguely
# remember we do something more than just running them") but is almost
# never the SUBJECT of a stored memory. The failure mode this list
# closes is an IDF artifact: memory bodies are technical prose, so
# conversational filler is RARE in the corpus, and BM25 prices rare
# terms high — a distractor matching "supposed" + "remember" outranked
# the right memory matching "paged" + "wake". These words are NOT
# stopwords: they still match and still count toward coverage; the
# search layer only floors their document frequency so corpus rarity
# cannot inflate them (strip-based variants were measured and rejected
# — they delete the only hooks some queries have; the cap kept those
# hits alive, bench 2026-08-09).
#
# Curation rule, applied entry by entry: a word stays OUT of this list
# if a plausible technical memory could be ABOUT it. That is why
# 'again'/'still' (recurrence — "crashed again" is a real signal),
# 'someone' (a person), 'get'/'set'/'run'/'check'/'back'/'end' (live
# tech vocabulary) are absent, and why every listed word is either
# discourse-verb ("remember", "guess"), hedge ("probably", "kinda"),
# vague noun ("thing", "stuff"), or emphasis ("really", "exactly").
# 'whole' is the deliberate borderline: it has a scope reading ("the
# whole test suite") but its query use is overwhelmingly the vague-noun
# idiom ("how that whole thing is wired"), and because this list CAPS
# rather than strips, the scope reading still matches at deflated
# weight — the measured grid kept it in.
QUERY_FILLER_WORDS: tuple[str, ...] = (
    "actually",
    "anyway",
    "anyways",
    "apparently",
    "basically",
    "certainly",
    "definitely",
    "ever",
    "exactly",
    "forever",
    "forget",
    "forgot",
    "forgotten",
    "guess",
    "guessing",
    "honestly",
    "just",
    "kinda",
    "knew",
    "know",
    "knows",
    "literally",
    "maybe",
    "perhaps",
    "probably",
    "properly",
    "really",
    "remember",
    "remembered",
    "remembers",
    "seriously",
    "somehow",
    "sorta",
    "stuff",
    "supposed",
    "supposedly",
    "sure",
    "thing",
    "things",
    "think",
    "thinking",
    "thought",
    "vaguely",
    "want",
    "wanted",
    "whatever",
    "whole",
    "wonder",
    "wondered",
    "wondering",
)

# Irregular past participles a suffix rule cannot reach: "why are the
# images WRITTEN with those hash things" must be able to meet a body
# that says "write"/"writes". Only the high-frequency irregulars that
# plausibly describe engineering actions; the regular -ed/-ing family
# is handled by rule in `morph_variants`.
IRREGULAR_PAST: dict[str, tuple[str, ...]] = {
    "brought": ("bring",),
    "broken": ("break",),
    "built": ("build",),
    "caught": ("catch",),
    "chosen": ("choose",),
    "held": ("hold",),
    "kept": ("keep",),
    "made": ("make",),
    "paid": ("pay",),
    "ran": ("run",),
    "sent": ("send",),
    "taken": ("take",),
    "written": ("write",),
}
# 'went'/'gone' -> 'go' and 'done' -> 'do' are deliberately absent: their
# targets are under `_MIN_EXPANSION_LEN` and would be filtered anyway —
# dead entries misread as coverage. See the filter note below for the
# measured incident that makes the length floor non-negotiable.
#
# 'came' -> 'come' is absent for the reason BEHIND that floor rather
# than the floor itself: the stemmer's final-e normalisation carries
# 'come' to 'com', which clears `_MIN_EXPANSION_LEN` by one character
# and is a live body token in every memory that cites a `.com` host
# ('status.example.com' splits to ['status', 'example', 'com'] through
# `_kebab_parts`). That is precisely the promiscuous-short-term class
# the floor exists to block — a rescue leg earns its weight by adding
# DISCRIMINATING vocabulary — and the entry buys little: 'came' is rare
# in retrieval questions and 'come' is rarely what a memory is about.

# Colloquial clippings -> the full form a written memory actually uses.
# A query says "creds"/"repo"/"PR"; bodies say "credentials"/
# "repository"/"pull request". One-directional on purpose: expanding
# the clipping toward the full form is safe (the full form is specific);
# expanding a full form toward its clipping would let "production" drag
# in every "prod" mention. Multi-word values expand to each word.
CLIPPINGS: dict[str, tuple[str, ...]] = {
    "auth": ("authentication",),
    "config": ("configuration",),
    "configs": ("configuration",),
    "cred": ("credential",),
    "creds": ("credential",),
    "db": ("database",),
    "dbs": ("database",),
    "dep": ("dependency",),
    "deps": ("dependency",),
    "dev": ("development",),
    "docs": ("documentation",),
    "env": ("environment",),
    "envs": ("environment",),
    "info": ("information",),
    "k8s": ("kubernetes",),
    "lib": ("library",),
    "libs": ("library",),
    "perf": ("performance",),
    "pr": ("pull", "request"),
    "prs": ("pull", "request"),
    "prod": ("production",),
    "repo": ("repository",),
    "repos": ("repository",),
    "spec": ("specification",),
    "specs": ("specification",),
}

# Small dev-domain synonym groups, bidirectional inside a group. This
# is the direct assault on the measured vocabulary gap: the casual side
# of each group is what people type ("toggles", "hash", "undo"), the
# formal side is what memories say ("feature flag", "digest",
# "rollback"). Kept deliberately small and general — every group must
# read as ordinary engineering vocabulary to a stranger; nothing
# corpus-specific belongs here. Precision is protected downstream:
# these terms only ever feed a down-weighted rescue leg behind a
# confidence gate, never the base ranking.
SYNONYM_GROUPS: tuple[tuple[str, ...], ...] = (
    ("toggle", "toggles", "flag", "flags"),
    ("rip", "remove", "removal"),
    ("hash", "hashes", "digest", "sha256", "checksum"),
    ("cred", "creds", "credential", "credentials", "secret", "secrets"),
    ("library", "libraries", "dependency", "dependencies", "package", "packages"),
    ("bar", "criteria", "approval", "policy"),
    ("undo", "rollback", "revert"),
    ("dump", "dumps", "backup", "backups", "restore"),
    ("staged", "phased", "stage", "phase", "phases"),
    ("old", "legacy"),
    ("cutover", "switchover", "switching", "decommission", "shutdown"),
)


# Minimum length for any emitted expansion term, across all sources.
# Enforced centrally in `expansion_terms` (morph_variants also applies
# it to its own rule output) — see the docstring there for the measured
# 'go' incident this floor exists to prevent.
_MIN_EXPANSION_LEN = 3


class ExpansionTables(NamedTuple):
    """Stemmed lookup structures, built once per process by the search
    layer. All keys and values live in the same post-stem token space
    the rankers match in — that alignment is the whole point of
    building them through the live stemmer instead of shipping stemmed
    literals that could drift when a stemmer rule changes."""

    filler_stems: frozenset[str]
    irregular: dict[str, tuple[str, ...]]
    clippings: dict[str, tuple[str, ...]]
    synonyms: dict[str, frozenset[str]]


def build_tables(stem: Callable[[str], str]) -> ExpansionTables:
    """Stem every raw table into ranker token space.

    `stem` is `search._stem_token`. Values are stemmed at build time so
    a query-token lookup and the emitted expansion terms are both in
    matchable form; keys are stemmed too because query tokens arrive
    post-stem ("policies" reaches the lookup as "polici")."""
    synonyms: dict[str, set[str]] = {}
    for group in SYNONYM_GROUPS:
        stems = {stem(w) for w in group}
        for s in stems:
            synonyms.setdefault(s, set()).update(stems - {s})
    return ExpansionTables(
        filler_stems=frozenset(stem(w) for w in QUERY_FILLER_WORDS),
        irregular={
            stem(k): tuple(dict.fromkeys(stem(v) for v in vs))
            for k, vs in IRREGULAR_PAST.items()
        },
        clippings={
            stem(k): tuple(dict.fromkeys(stem(v) for v in vs))
            for k, vs in CLIPPINGS.items()
        },
        synonyms={s: frozenset(v) for s, v in synonyms.items()},
    )


def morph_variants(tok: str, stem: Callable[[str], str]) -> set[str]:
    """Inflectional variants of one query token, by rule.

    The shipped stemmer folds PLURALS only (a deliberate precision
    choice — see `search._stem_segment`); the -ing/-ed family it leaves
    alone is exactly what separates "splitting the repos" from a body
    that says "split". Generated per QUERY TOKEN at search time, never
    at index time, so the conservative index-side stemmer contract is
    untouched. Doubling is undone (splitting -> split), the mute-e
    restored (staging -> stage), and each candidate re-inflected the
    other way (switched <-> switching) because BODY tokens keep their
    surface -ed/-ing spelling too. Everything passes back through
    `stem` so the emitted variants live in matchable token space."""
    out: set[str] = set()
    if tok.endswith("ing") and len(tok) > 5:
        base = tok[:-3]
        bases = {base, base + "e"}
        if len(base) >= 3 and base[-1] == base[-2]:
            bases.add(base[:-1])
        for b in bases:
            out.add(b)
            out.add(b + "d" if b.endswith("e") else b + "ed")
    elif tok.endswith("ed") and len(tok) > 4:
        base = tok[:-2]
        bases = {base, base + "e"}
        if len(base) >= 3 and base[-1] == base[-2]:
            bases.add(base[:-1])
        for b in bases:
            out.add(b)
            stem_b = b[:-1] if b.endswith("e") else b
            out.add(stem_b + "ing")
    return {stem(v) for v in out if len(v) >= 3} - {tok}


def expansion_terms(
    query_tokens: list[str],
    tables: ExpansionTables,
    stem: Callable[[str], str],
) -> list[str]:
    """All rescue-expansion terms for a query, sorted for determinism.

    Union of the three sources over every query token: rule-generated
    inflection variants, clipping full-forms, synonym group mates.
    Query tokens themselves are excluded — expansion adds vocabulary
    the query lacks; re-weighting vocabulary it has is the base
    rankers' job (and re-adding it was measured to hurt: the qt+exp
    leg shape lost rank-0 hits the exp-only shape keeps).

    Every emitted term must clear `_MIN_EXPANSION_LEN`, whatever its
    source. This floor is measured, not stylistic: a single 2-char term
    ('go', from went->go) reaching the rescue leg matched broadly
    enough to cost the gold set 5 points at recall@1 AND recall@5 —
    ultra-short terms are promiscuous matchers, and a rescue leg only
    earns its weight adding DISCRIMINATING vocabulary.

    Filler stems are dropped for the same reason, and this is the only
    place the rule source can be caught. The three TABLES are curated
    against the filler list entry by entry, but `morph_variants` is a
    RULE: "wondering" regenerates 'wonder'/'wondered', "thinking"
    regenerates 'think'. The df-floor that deflates filler
    (`search._filler_floor_stats`) is applied to the CALLER's tokens
    only, so a synthesized filler stem reaching the rescue leg prices
    at full corpus-rare IDF — restoring exactly the weight the floor
    removed, on the leg that has no floor. The two mechanisms must stay
    disjoint; this filter is where the rule source is held to it."""
    exp: set[str] = set()
    for tok in query_tokens:
        exp.update(morph_variants(tok, stem))
        exp.update(tables.irregular.get(tok, ()))
        exp.update(tables.clippings.get(tok, ()))
        exp.update(tables.synonyms.get(tok, ()))
    exp -= set(query_tokens)
    exp -= tables.filler_stems
    return sorted(t for t in exp if len(t) >= _MIN_EXPANSION_LEN)
