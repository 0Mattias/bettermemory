# W2 body census — does the second surface of the pair corpus carry the prize?

The W2 pair-class census read the labeled duplicate-question TITLES
empty where the prize is: zero of the committed six cross-form pairs
supported across the full labeled-pair file, with the presence check
showing the vocabulary present but never substituting
(`bench/w/W2_PAIR_CENSUS_RECORD.md`, artifact
`bench/w/results/w2-pair-census-2026-08-20.json`). Its park named exactly two
candidate data axes and licensed neither; the first was: bodies, not
titles — the pinned per-site archives carry full `Posts.xml` bodies, a
duplicate pair's bodies are long-form paraphrases of one need, and the
thin-marginals mechanism that emptied the title read is a claim about
title LENGTH, not about the corpus. The owner's continuation sentence
of 2026-08-20, under the standing delegation, commissions that
first-named candidate; this document is the declaration the record
said a new axis must carry, argued from the record rather than from
appetite. The W2 ENTRY DECISION remains downstream: this census only
decides whether a W2 training declaration may be written over the
body surface.

The sha ordering is the enforcement record, as in every unit of this
lane: this declaration first, then the reader with its CI leg, then
the run and the record. A miss is published, never renegotiated.

## 1. The input — in hand, pinned, no fetch

The eighteen per-site Stack Exchange archives
`bench/w/W3P2_DECLARATION.md` §3 admitted, by the same register names,
with `bench/w/corpora.json` the authoritative pins — sha256 re-verified
over the exact bytes before any member is read, a mismatch stopping
the unit. The edge rule is W3-P2 §3.2 verbatim, through the committed
code path (`w3p2_pairs.duplicate_edges`): `PostLinks.xml` streamed,
`LinkTypeId="3"` rows yielding directed duplicate edges in document
order, exact repeats dropped. What changes is the read surface only:
where W3-P2 resolved each edge to the two question TITLES, this census
resolves each edge to the two question BODIES — the `Body` attribute
of the `PostTypeId="1"` rows on either side of the edge.

The Stack Overflow PostLinks archive is NOT pinned and is NOT fetched
by this census, whatever the outcome — the twitch row below licenses a
follow-up declaration, not a download.

The resolved pair bodies are written beside the corpus as a derived
intermediate (`bench/w/corpus/derived/w2-bodies-<date>.tsv`, one
`site<TAB>left-prose<TAB>right-prose<TAB>left-full<TAB>right-full`
line per kept pair) — NOT committed (CC BY-SA per-post attribution),
its sha256 recorded in every artifact that reads it, exactly the
W3-P2 §3.6 arrangement.

## 2. The surface rule — the one rule this declaration owns

Everything else in this census is imported from committed modules; the
body-cleaning rule is new and is therefore fixed here, before any byte
is read. A `Body` attribute value is XML-escaped HTML. The PRIMARY
surface — the asker's own prose — is produced mechanically:

1. Unescape the XML attribute (entity decoding) to obtain the HTML.
2. Remove `<blockquote>…</blockquote>`, `<pre>…</pre>` and
   `<code>…</code>` spans, case-insensitive, non-greedy, applied
   repeatedly to a fixpoint so nested spans fall too. Quoted material
   is not the asker's phrasing — and the platform's own legacy
   closure notice sits in a blockquote that QUOTES THE MATE'S TITLE
   into the closed question's body, so leaving quotes in place would
   let the mate's vocabulary leak across the very edge under test, in
   both directions at once. Code is not paraphrase surface.
3. Strip remaining tags to spaces, unescape entities once more,
   collapse whitespace runs to single spaces.

The WITH-MARKUP-TEXT variant — step 2 skipped, tags stripped only, so
code and quoted text keep their words — is computed in the same pass
and reported per probe pair, informationally. It prices the one honest
objection to the prose rule: a `--flag` in a command line is a real
occurrence of the concept, and a census that silently deleted it
would manufacture absence. Neither variant gates the other; the
ladder reads on the prose surface, the variant is published beside it.

The pair-keep rule is the family's, verbatim: both bodies resolved
(both posts present in the dump as questions, each carrying a `Body`),
both prose surfaces non-empty, distinct after lowercasing, each
yielding ≥ 2 content tokens under the W3-P tokenizer
(`w3p_pairs.content_tokens`, unchanged). Unresolved edges and
rule-dropped edges are counted per site and published.

## 3. The reads — imported probes, two token spaces

As in the title census (`bench/w/W2_PAIR_CENSUS_DECLARATION.md` §2),
the probe rows run in ENGINE STEM space — the space every table and
probe in this program matches in — and the continuity row runs in
W3-P2's surface space, so the censuses read side by side without
renegotiation. The counting rule is `w3p_pairs.need_supported`,
exclusive substitution either direction, unchanged: a's stem in one
body's stemmed tokens, b's stem in the other's, neither stem in the
opposite body.

- **THE COMMITTED SIX** — `w1b_geometry_probe.CROSS_FORM`, byte for
  byte. The prize class; the ladder reads on this set alone, on the
  full file, with the consumer-tech register subset (superuser +
  apple + android) reported beside every cell.
- **THE EXPANDED FAMILY** — `w1c_geometry_census.expanded_cross_form`,
  imported, reported in full, gating nothing.
- **THE PREFERENCE CONTINUITY ROW** — W3-P2's frozen `NEEDS`,
  recomputed on the body surface in surface space. Informational; the
  preference routes stay closed (W3-C) and this census does not
  reopen them.
- **THE PRESENCE ROWS** — for every probe stem, the count of kept
  pair-bodies whose prose surface contains it. The title census
  learned that a zero this absolute must carry its presence check to
  publication; this census builds the check into the artifact rather
  than running it after the fact. The marginals let the record
  separate vocabulary-absent from present-but-never-substituting
  without a second read.
- **SAME-SIDE CO-OCCURRENCE** — per probe pair, the count of kept
  pairs with both stems in ONE body's prose surface. Informational.
  W1c's design fact says a W2 objective must reward co-occurring
  pairs that never substitute; this row is where that raw material
  would show, and the owner decision after this census will want it
  on the same page.

## 4. The criterion, stated before any count exists

The thresholds are the family's constants, unchanged from the title
census — the surface changed, not the arithmetic:

```
SUPPORTED   a probe pair with >= 5 exclusive-substitution pairs
            (prose surface, full file)
LICENSE     >= 4 of the committed six SUPPORTED
            -> writing the W2 training declaration over the body
               surface is licensed
TWITCH      2 or 3 of the committed six SUPPORTED
            -> the named follow-up is the Stack Overflow PostLinks
               fetch, a body-census re-read on the enlarged corpus,
               and nothing else
PARK        0 or 1 of the committed six SUPPORTED
            -> the labeled-duplicate corpus in hand is measured shut
               on BOTH its surfaces — titles and bodies; W2 entry on
               this corpus closes whole, and what remains is the
               unpinned programming register (a new declaration's
               argued burden) or the standing interim sentence
```

Derivation, restated: a contrastive objective can only learn a bridge
that appears in its positives; a prize pair with zero exclusive
substitutions is invisible to any loss over this corpus; five is the
evidentiary minimum every census in this family has used; four of six
is the smallest majority that leaves the objective a class to learn
rather than a pair to memorize.

## 5. Constraint ledger

- Read-only: the census reads the pinned archives, the committed probe
  modules, and the engine's public stemmer. No engine code changes;
  nothing is trained; nothing under `bench/longmemeval/`, `bench/msc/`
  or `bench/heldout/` is touched.
- No fetch, whatever the outcome.
- Budget, hard: sha re-verification plus the full extraction pass is
  ONE pass over the eighteen archives, at most 30 wall-clock minutes
  aggregate (the title extraction's full pass recorded 139.9 seconds —
  `bench/w/results/w3p2-census-2026-08-17.json`; bodies add attribute
  parsing on edge-member rows only). The census stage from
  the derived file is at most 5 minutes. Overrun is a published park.
- Deterministic artifact: sorted iteration, lexicographic ties, no
  wall-clock content beyond the provenance date. The census stage run
  twice over the derived file produces an identical counts block; the
  CI leg (`tests/test_w2_body_determinism.py`) proves the extraction
  and census code paths — edge dedup, body resolution, the cleaning
  fixpoint, the pair-keep rule, the missing-member rule, and the
  probe counting — byte-stable on a committed synthetic fixture, no
  corpus bytes.
- No changelog entry.

## Declared confounds

1. **Length asymmetry, the honest reversal of thin marginals.** Long
   bodies give the mate's stem many chances to appear on the wrong
   side and destroy exclusivity — the exact opposite bias of the
   title read, where thin marginals left the joint event no room. The
   presence rows and same-side row are in the artifact so the record
   can price which mechanism produced whatever number appears, in
   either direction.
2. **The cleaning rule is mechanical regex, not an HTML parser.**
   Malformed markup misclassifies spans; the fixpoint rule is the
   declared behavior, published as-is.
3. **The probe sets are table-derived.** As in W1c and the title
   census: they measure the relations the curated table carries,
   which three instruments have made the named prize — not every
   relation that matters.
4. **Stemming can manufacture or destroy matches.** The engine
   stemmer is used because every downstream consumer matches in its
   space; a pair the stemmer conflates is reported as conflated
   rather than silently counted.
5. **The prose rule can delete genuine occurrences.** Vocabulary
   living only in code spans or quoted error text vanishes from the
   primary surface; the with-markup-text variant is the price tag on
   that deletion, published beside every probe row.

## What is not claimed

- Not a W2 verdict, a training result, or any recall number.
- Not a reopening of the preference-class routes; the continuity row
  is informational.
- Not a statement about Stack Overflow's registers — that corpus is
  unpinned and unread.
- Not a repricing of the title census: its park stands on its own
  surface; this census prices the other one.
