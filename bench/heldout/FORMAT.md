# Held-out instrument — file format and authoring container

This document specifies the **files** a held-out retrieval instrument
is made of: their shape, their fields, and the rules a set of them has
to satisfy to be loadable and scorable. It is written for an author who
has never read this repository's retrieval code and does not need to.

**It deliberately contains no authoring guidance.** There is nothing
here about what to write, what register to write it in, what makes a
question hard or easy, or what the system under test is good or bad at.
That omission is the point, not an oversight — see *Why this document is
this thin*. Content direction comes from your separate brief; this file
is the container it has to fit in.

Every value shown below is a semantically neutral placeholder
(`persona_1`, `fact_1`). None of them is an example of good content.

---

## 1. What an instrument is

Three JSON files in one directory:

```
bench/heldout/data/
  personas.json     the material a retriever searches over
  questions.json    the questions, and which material answers each
  manifest.json     provenance and self-description
```

A **persona** is one person's history, split into **sessions**. A
session is an ordered list of **turns**. Each persona is stored and
searched independently: a question about `persona_1` is only ever
scored against `persona_1`'s sessions.

A **question** names one persona and lists the session ids that contain
its answer. Those ids are the gold labels.

---

## 2. `personas.json`

A JSON array. One object per persona.

```json
[
  {
    "persona_id": "persona_1",
    "sessions": [
      {
        "session_id": "persona_1_s1",
        "date": "2026-01-05",
        "turns": [
          {"role": "user", "text": "fact_1"},
          {"role": "assistant", "text": "fact_2"}
        ]
      },
      {
        "session_id": "persona_1_s2",
        "date": "2026-01-19",
        "turns": [
          {"role": "user", "text": "fact_3"}
        ]
      }
    ]
  }
]
```

| field | type | rules |
| --- | --- | --- |
| `persona_id` | string | Unique across the file. `^[a-z0-9_]+$`. |
| `sessions` | array | At least 2. Order is the order they happened in. |
| `session_id` | string | **Globally unique across the whole file**, not just within its persona. `^[a-z0-9_]+$`. |
| `date` | string | `YYYY-MM-DD`. Non-decreasing within a persona. |
| `turns` | array | At least 1. |
| `role` | string | Exactly `"user"` or `"assistant"`. |
| `text` | string | Non-empty after stripping. No newlines — one turn is one utterance. |

**The session id must never appear in any `text`.** It is a label, and a
label that appears in searchable content leaks the answer into the thing
being measured. The loader rejects a file where it does.

---

## 3. `questions.json`

A JSON array. One object per question.

```json
[
  {
    "question_id": "q_1",
    "persona_id": "persona_1",
    "question": "fact_1",
    "answer_session_ids": ["persona_1_s1"]
  }
]
```

| field | type | rules |
| --- | --- | --- |
| `question_id` | string | Unique across the file. `^[a-z0-9_]+$`. |
| `persona_id` | string | Must exist in `personas.json`. |
| `question` | string | Non-empty after stripping. |
| `answer_session_ids` | array | 1 or more session ids, all belonging to `persona_id`, all distinct. |

A question with more than one answer session is scored as needing all of
them; see *Metrics*.

---

## 4. `manifest.json`

```json
{
  "instrument": "heldout",
  "version": "1",
  "authored": "YYYY-MM-DD",
  "author": "free text",
  "license": "SPDX identifier",
  "license_notes": "free text",
  "description": "free text",
  "sealed": true
}
```

`license` must be an SPDX identifier permitting redistribution, because
these files are committed to an MIT-licensed repository. If any content
is adapted from an outside source, say so in `license_notes` with a
link. If it is original work authored for this purpose, say that.

`sealed` is the flag described in *The seal*.

---

## 5. Size and shape envelope

| quantity | target |
| --- | --- |
| personas | 12–20 |
| sessions per persona | 8–20 |
| turns per session | 4–20 |
| questions total | 100–150 |
| questions per persona | roughly even |
| questions with >1 answer session | 15–35% of the total |

**Distractors are structural, not authored.** A question is scored
against every session of its persona, so all the sessions that are not
its answer are its distractors. That means a persona needs enough
sessions for a wrong answer to be possible: a persona with 3 sessions
makes every question nearly free. The 8–20 range above is what makes the
score mean something.

**Size cap.** Each file must stay under **500 kB** — the repository's
pre-commit hook rejects larger additions. The envelope above fits
comfortably; if you approach the cap, prefer more personas over longer
sessions.

---

## 6. Determinism

The instrument is a committed artifact, so two people loading it must
get identical results forever.

- Arrays are in a fixed order and that order is part of the file.
- Ids are stable. Once a `session_id` or `question_id` is published it
  never changes meaning.
- No timestamps, RNG, or environment-dependent values in content.
- Files are UTF-8, `\n` line endings, and end with a newline.
- Formatted with 2-space JSON indentation and keys as written above, so
  a diff shows content changes rather than reformatting.

---

## 7. Metrics (what the harness computes)

For each question, the retriever is given the persona's material and the
question text, and returns a ranked list. That list is collapsed to
**distinct sessions**, first occurrence winning.

```
recall@k = |retrieved answer sessions ∩ answer sessions| / |answer sessions|
```

computed per question over the top *k* distinct sessions, then
**macro-averaged** over questions. Reported at k = 1, 5, 10.

A consequence worth knowing while authoring: for a question with more
answer sessions than *k*, recall@k is bounded below 1 by construction.
The harness reports those ceilings alongside the figures.

The harness also writes a per-question sidecar recording, for each
question, the rank at which each answer session appeared (or `null`),
so a later reader can recompute the aggregate without re-running.

---

## 8. The seal

**The instrument's content is authored independently of the system it
measures, and the separation is enforced by git history rather than by
good intentions.**

The protocol:

1. The data files land in their **own commit**, authored by someone who
   is not implementing the mechanism under test.
2. From that commit until the preregistered run, **the implementer must
   not read `questions.json` or the `answer_session_ids`.** Reading the
   format document, the loader, and the harness is fine — reading the
   content is not.
3. The preregistration commit carries a **no-read attestation** stating
   that (2) held.
4. The enforcement record is the ordering of three shas, checkable by
   anyone afterwards:

   ```
   data commit  <  preregistration commit  <  run commit
   ```

   A preregistration that predates the data cannot have been fitted to
   it. A run that postdates both is scored against criteria fixed before
   the data was visible.

`manifest.json`'s `sealed: true` is the author's assertion that the
content was produced without reference to the system under test.

If the seal is broken — accidentally read, or fitted to — the honest
move is to say so and retire the instrument. An instrument believed
clean but quietly compromised is worse than no instrument, because its
numbers carry authority they have not earned.

---

## 9. Why this document is this thin

A held-out instrument is only worth building if its content is authored
without knowledge of what the system under test finds difficult. If this
document told you which phenomena to include, the resulting questions
would be shaped by that knowledge, and the instrument would be measuring
its own author's model of the system rather than the system.

So the specification stops at structure. **If you find yourself wanting
to know "what kind of questions are you looking for?", that question
belongs to your brief and not to this file, and the person who wrote
this file is the wrong person to ask.**

---

## 10. Checking your work

```sh
.venv/bin/python bench/heldout/run.py --validate
```

Validates structure, uniqueness, cross-references, the session-id leak
rule, and the envelope in §5 — and reports counts. It reads content only
to validate it and prints no question text.

Scoring is a separate command and is not run until the instrument is
sealed and the preregistration is committed.
