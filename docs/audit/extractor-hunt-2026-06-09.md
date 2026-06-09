# Extractor false-signal hunt — 2026-06-09 (parked backlog)

Provenance: a 4-round multi-agent hunt (224 agents; 10 heuristic surfaces;
every finding adversarially verified with a runnable repro by an independent
agent) over heuristic-extraction false signals, run at HEAD 874b0b0 / v3.8.0.
The hunt never went dry — it hit the round cap still producing fresh
findings, so this is an open queue, not a completed sweep.

Already fixed (v3.9.0): all 20 verify.py path-extraction findings, the two
HIGH findings (credentials sentence-final-period false negative; origin
linked-worktree blackout), and two credential coverage gaps (encrypted-PKCS#8
PEM header, Slack xapp/xoxc/xoxe token families).

This file indexes the REMAINING verified findings. Full detail (description,
example input, verifier notes, suggested fix) lives in the sibling JSON:
`extractor-hunt-2026-06-09.json`. Intended consumer: /audit-loop ticks —
treat each entry as a pre-verified audit finding; re-confirm against the
then-current HEAD before fixing (some may have drifted or been fixed en
route), and weigh each suggested fix against the module's documented
conservative trade-offs before applying.

Remaining: **146** findings — by file: proposals.py (19), consolidate.py (18), durability.py (17), credentials.py (16), groundedness.py (16), audit.py (14), search.py (14), origin.py (13), scope_match.py (10), health.py (4), hook.py (3), models.py (1), _handlers.py (1)

| # | Sev | File | Dir | Title |
|---|-----|------|-----|-------|
| 0 | MED | durability.py | FP | Lowercase UUIDs and other permanent hex identifiers trip the commit-SHA marker |
| 1 | MED | durability.py | FN | "Today, I ..." / "Today, we ..." (comma after fronted 'Today') defeats the today-i/today-we markers |
| 2 | MED | durability.py | FN | "as of today" / "as of <date>" / "as of this writing" slip through while "as of now" fires |
| 3 | MED | durability.py | FP | "the new" fires on proper nouns: 'the New York office', 'The New Yorker' |
| 4 | MED | durability.py | FP | "the latest" fires on durable version-policy preferences ('prefers tracking the latest LTS') |
| 5 | MED | credentials.py | FN | Generic keyword rule misses env-var-prefixed and compound keyword forms (POSTGRES_PASSWORD=, SECRET_KEY=, secret_key=, aws_secret_access_key=) |
| 6 | MED | credentials.py | FP | Generic rule false-fires on hyphenated technical descriptors after 'is' ("password is sha256-hashed", "secret is base64-encoded") |
| 7 | MED | credentials.py | FP | Prefixed detectors fire on masked/placeholder token shapes (xoxb-…your-bot-token (kebab placeholder), AKIA + 16 X's, ghp_/github_pat_ + x-runs) — no low-diversity guard |
| 8 | MED | credentials.py | FN | Path guard ('/' in value) and 200-char cap silently reject standard-base64 secrets in keyword assignments |
| 9 | MED | groundedness.py | FN | Hallucinated 3-token claims about 'the user' auto-pass via speaker-label freebie; 2-token claims skipped entirely |
| 10 | MED | groundedness.py | FN | Single-newline markdown bullet lists are one 'sentence' -- a hallucinated bullet is diluted by grounded siblings and passes |
| 11 | MED | groundedness.py | FP | 'i.e.'/'e.g.' treated as a sentence boundary isolates the restatement clause, flagging it while the whole sentence would pass |
| 12 | MED | health.py | FP | rare_scopes flags legitimately distinct sibling/successor project scopes as typos |
| 13 | LOW | scope_match.py | FN | Project-root pass misses tilde-form paths — the most common prose form for home paths |
| 14 | MED | health.py | FN | rare_scopes count==1 gate: a typo'd scope written twice is never flagged |
| 15 | LOW | health.py | FN | Namespace-omitted scope (bare 'bettermemory' vs 'projects:bettermemory') never flagged — prefix dominates the distance |
| 16 | LOW | scope_match.py | FN | Project root followed by sentence-final period is skipped; find() first-occurrence masking compounds it |
| 17 | MED | origin.py | FN | worktree_root strict equality hides ALL memories for a repo after the checkout moves, is re-cloned, or the store syncs to another machine |
| 18 | MED | origin.py | FN | Azure DevOps SSH and HTTPS remotes for the same repo never match (and old visualstudio.com vs dev.azure.com forms never match) |
| 19 | MED | origin.py | FN | Bitbucket Server/Data Center: HTTPS '/scm/' path prefix makes SSH and HTTPS remotes of the same repo never match |
| 20 | LOW | origin.py | FN | SSH-over-443 fallback hosts (ssh.github.com, altssh.gitlab.com) never match the canonical host for the same repo |
| 21 | LOW | audit.py | FP | MIN_PROBE_CONTENT_TOKENS gate counts duplicate tokens, so repeated-word continuations ('yes yes', 'push it push it') fire false misses |
| 22 | MED | audit.py | FP | Contraction fragments ('s' from "what's", 't' from "can't") count as content tokens, pushing bare continuations past the gate and inflating coverage to 'high' |
| 23 | MED | audit.py | FN | Project-suppression gate scans all 3 retained top hits, not the threshold-deciding top-1 — a low-relevance project hit at rank 2-3 swallows a real miss on a global memory |
| 24 | MED | audit.py | FP | 60s wall-clock lookback misses a search fired earlier in the same long turn — tool-heavy turns (>60s) get false search_miss despite having searched |
| 25 | MED | consolidate.py | FP | Scope-blind Jaccard dedup tombstones legitimate per-project memories with boilerplate bodies; loser's scopes are not merged into the keeper |
| 26 | MED | consolidate.py | FP | Negation is invisible to Jaccard dedup: 'Do not use X' and 'Use X' have identical token sets (similarity 1.0) because 'no'/'not'/'do' are stopwords |
| 27 | MED | consolidate.py | FP | Demotion pass retags user-inference memories to ambient, contradicting its documented fact/None-only criteria and the project's confirmation-protected tier design |
| 28 | MED | consolidate.py | FN | Elaborated-duplicate false negative: a memory fully containing another's claim scores far below the Jaccard threshold, so the most common duplicate shape is never flagged |
| 29 | MED | consolidate.py | FP | Demotion window keys only on created: a memory rewritten via memory_update and attested via memory_verify yesterday is still demoted to ambient unattended |
| 30 | MED | proposals.py | FP | Bare 'my '/'our ' in _PREFERENCE_RE captures imperative task requests and pasted third-party text as user-inference facts |
| 31 | MED | proposals.py | FP | 'I want you to' / 'I need you to' task requests fire the i-want/i-need preference branch, violating the extractor's own contract (c) |
| 32 | MED | proposals.py | FP | Markdown bullet prefixes defeat the ^-anchored question/command reject — bulleted task lists are queued as facts |
| 33 | LOW | proposals.py | FN | Missing word boundaries in _QUESTION_OR_COMMAND_RE: 'However,'/'Whenever' match bare 'how'/'when' and silently drop canonical first-person preferences |
| 34 | LOW | proposals.py | FN | Transient markers 'the latest'/'the new' silently drop durable preferences that mention them, with no override path in the extractor |
| 35 | MED | search.py | FN | Possessive/contraction fragments ('s', 't') survive stopword stripping and flip the relevance bucket |
| 36 | MED | search.py | FN | Hyphenated/snake_case query token never matches the space-separated or other-separator spelling of the same identifier — total miss |
| 37 | MED | models.py | FP | Snippet truncation ignores newlines as word boundaries — markdown-list bodies get hard-cut mid-path/mid-URL, producing a plausible-but-wrong path in the snippet |
| 38 | MED | search.py | FN | Diacritic folding mismatch: FTS5 index matches accent-insensitively but every Python ranker requires exact codepoints — indexed candidates silently dropped |
| 39 | MED | durability.py | FN | Future-scheduling transients ('next week', 'next month', 'tonight') slip through while 'tomorrow' and 'this week' fire |
| 40 | LOW | durability.py | FP | 'i just' / 'we just' fire on habitual 'just = simply/only' preference statements, rejecting durable first-person preferences |
| 41 | MED | durability.py | FN | Plural subjects escape the now-uses family: 'now use' / 'now rely' are not markers while 'now uses' / 'now relies' are |
| 42 | MED | durability.py | FN | Branch-state family misses the noun-phrase word order: 'has/are unpushed commits' slips while 'is unpushed' fires |
| 43 | MED | credentials.py | FN | Quoted or markdown-formatted keys defeat the generic rule — pasted JSON config ('"password": "..."') never fires |
| 44 | MED | credentials.py | FP | Function-call code expressions fire the generic rule — 'api_key = secrets.token_hex(32)' blocks a legitimate write |
| 45 | MED | credentials.py | FN | The 'bearer' keyword branch can never match its canonical shape — 'Authorization: Bearer <opaque token>' always passes |
| 46 | MED | credentials.py | FN | Connection URIs with embedded userinfo passwords never fire any detector — the classic DATABASE_URL paste commits cleanly |
| 47 | MED | groundedness.py | FP | ISO-normalized dates zero-overlap the spoken form and kebab-expansion quadruples the damage — faithful date facts flagged ungrounded |
| 48 | MED | groundedness.py | FN | Apostrophe-split contraction fragments ('t', 'doesn', 's') are universal freebie anchors — fully hallucinated contraction-bearing claims pass |
| 49 | MED | groundedness.py | FN | Polarity-inverted extractions score perfect groundedness: negators are all stopwords, so 'does not use Docker' is fully 'grounded' by a transcript saying the opposite |
| 50 | LOW | groundedness.py | FN | CJK bodies bypass the gate entirely: each unspaced clause is one giant 'token', so typical sentences fall under MIN_CONTENT_TOKENS and are never evaluated |
| 51 | MED | scope_match.py | FP | Pass-1 \b word boundary fires through '-', '.', '/' — sibling slug names and the exact sibling-path shapes pass-2's trailing guard suppresses are false-flagged via project_name |
| 52 | MED | scope_match.py | FP | collect_project_roots has no depth/sanity check on origin.cwd — a dotfiles-style project worked from $HOME makes its 'root' the home directory, so every home-relative path in any write false-flags |
| 53 | MED | health.py | FP | rare_scopes distance threshold (2) is not scaled to scope length — unrelated short topic scopes (vim/git, go/git, ml/sql, api/aws, just/rust) flag each other as typos |
| 54 | MED | scope_match.py | FP | collect_project_roots attributes the writing session's cwd to every projects:* tag on the memory — facts about project B recorded while working in project A's checkout poison B's root, and then correctly-tagged writes about A are rejected with 'add projects:B' |
| 55 | MED | scope_match.py | FP | Common-word repo names (docs, notes, scripts, blog) pass the 3-char gate and false-fire the project_name pass on everyday prose that merely uses the word |
| 56 | MED | origin.py | FP | Remote not named 'origin' collapses the whole auto-scope boundary: repo AND worktree_root captured as None, so writes go global and the caller matches everything |
| 57 | MED | origin.py | FN | Single-segment (ownerless) repo paths never normalize: gitolite/Gerrit/cgit-root remotes of the same repo fail to match across .git-suffix and protocol spellings |
| 58 | LOW | origin.py | FN | url.insteadOf aliases are captured unexpanded — capture() reads raw `git config --get remote.origin.url` instead of `git remote get-url`, so aliased remotes never match canonical forms |
| 59 | LOW | origin.py | FN | git+ssh:// and ssh+git:// schemes (valid git SSH transports) are unparseable, so such remotes never match any other spelling of the same repo |
| 60 | MED | audit.py | FN | Probe cannot thread a semantic model: search_mode='semantic' crashes every audit (silently swallowed by the Stop hook), and hybrid-with-embeddings probes a different ranker than the model used |
| 61 | MED | audit.py | FP | Project-suppression gate is structurally dead for local-only (remoteless) repos — every in-project continuation fires a false search_miss |
| 62 | MED | audit.py | FP | Two-word acknowledgments built from non-stopword filler ('all done', 'looks good', 'sounds good') clear the MIN_PROBE_CONTENT_TOKENS gate and score 2/2 = 'high' against ordinary memory bodies — false search_miss on bare continuations |
| 63 | MED | audit.py | FN | v1 threshold reads only the rank-1 hit: a 3/3 'high'-relevance answer at rank 2 is swallowed when a fresher, term-frequency-heavy partial match wins the score ranking |
| 64 | MED | audit.py | FN | Lookback window has no anchor at turn start: an unrelated search from the PREVIOUS turn within 60s shields a fresh miss in the next turn |
| 65 | MED | consolidate.py | FP | Symmetric kebab expansion multiplies shared compound identifiers, pushing distinct per-environment facts over the Jaccard dedup threshold |
| 66 | MED | consolidate.py | FP | Keeper selection trusts `updated` as refinement authority, but metadata-only retags (including consolidate's own demotion) bump it — the ambient husk beats the verified fact and the fact is tombstoned |
| 67 | MED | consolidate.py | FP | Demotion's 'retrieved' criterion is relevance-blind: one rank-5 'low' ride-along hit makes an old fact demotion-eligible, even though the search event itself records the relevance the pass ignores |
| 68 | MED | consolidate.py | FP | Cold-scope pass with telemetry disabled: empty event log makes 'no applied events' vacuously true, flagging every stable scope older than 180 days for archiving |
| 69 | MED | proposals.py | FP | Retrieval questions ('Do you remember if/that ...?') are captured as fact proposals — explicit-marker override defeats the question reject, and 'remember i' prefix-matches 'remember if' |
| 70 | MED | proposals.py | FP | 'Make sure to/you ...' and 'Note that ...' — ubiquitous per-turn instructions and code commentary fire the explicit marker and are queued as durable 'fact' proposals |
| 71 | MED | proposals.py | FN | Length floor silently drops short explicit capture requests: 'Remember that I use zsh.' is never proposed |
| 72 | MED | proposals.py | FN | 'Please remember: X' is actively rejected by the ^please command filter, and 'Remember this: X' is missing from the marker list — canonical capture phrasings missed |
| 73 | LOW | proposals.py | FP | suggested_category misassignment: 'we use X' / 'our CI ...' project-infrastructure facts are tagged user-inference, forcing plain facts into the confirmation-pending tier |
| 74 | MED | search.py | FP | Dotted version numbers fragment into bare digit tokens — wrong memory gets relevance 'high' and ties/outranks the right one |
| 75 | MED | search.py | FN | NFD combining marks break tokenization: 'Tjörn' becomes ['tjo','rn'] — NFC queries miss NFD bodies entirely, and the dedup gate misses exact-text duplicates |
| 76 | MED | search.py | FP | Symbol-bearing identifiers (C++, C#, .NET) collapse to a bare letter — list-enumeration memory outranks the real C++ memory and match_terms claims 'c' matched |
| 77 | MED | search.py | FN | Stopword homograph proper nouns ('Will', 'My', 'US', 'IT') are stripped from queries: bare-name search returns zero hits; multi-word queries report 'high' relevance with the name silently gone |
| 78 | MED | search.py | FN | Suspended hyphenation ('pre- and post-deploy') leaves a trailing-hyphen query token that can never match — coverage deflated, relevance bucket drops on exactly-on-topic memories |
| 79 | MED | durability.py | FN | In-progress/mid-work vocabulary — the most canonical transient state — slips the gate entirely |
| 80 | MED | durability.py | FN | Bare 'today' outside the three hardcoded bigrams slips: 'merged today', 'earlier today', 'said today that' |
| 81 | MED | durability.py | FN | Branch-state family misses the rest of git's state vocabulary: 'uncommitted changes', 'untracked files', 'stashed' all slip |
| 82 | MED | durability.py | FP | 'at the moment' fires on 'at the moment of/when <event>' — durable event-trigger descriptions of system behavior |
| 83 | MED | credentials.py | FN | Spaced two-word keywords ('api key is', 'access token is') never match — the canonical prose paste misses |
| 84 | LOW | credentials.py | FP | Placeholder allowlist is exact-equality only — changeme12345 / dummy_password_123 / test_api_key_12345 block legitimate sample-config docs |
| 85 | MED | credentials.py | FN | Dotted-ref guard swallows genuinely dotted vendor secrets — SendGrid SG.x.y and Vault hvs. tokens rejected as 'attribute references' |
| 86 | MED | credentials.py | FN | 'is set to' / 'was changed to' phrasing defeats the separator — high-entropy password in plain sight never fires |
| 87 | MED | groundedness.py | FP | camelCase identifiers are opaque to kebab expansion — identifier-spelled facts vs prose transcripts zero-overlap and flag |
| 88 | MED | groundedness.py | FP | Dotted abbreviations (Ph.D., M.Sc., U.S.) split the sentence mid-claim and shatter into single-letter junk tokens that never match the undotted spelling |
| 89 | MED | groundedness.py | FN | Terminal punctuation inside closing quotes or markdown bold ('."' / '.**') defeats the sentence splitter — a hallucinated following sentence merges and passes |
| 90 | MED | groundedness.py | FN | Kebab expansion of transcript file/branch names manufactures grounding anchors — preferences invented from identifiers pass at high ratio |
| 91 | MED | groundedness.py | FN | Stopword defense is English-only: non-English bodies pass on function words alone — Swedish hallucination grounds on {vill, att, på} while its English translation flags |
| 92 | MED | scope_match.py | FP | Nested project roots: correctly-tagged child-project writes are always gated to add the parent scope |
| 93 | MED | scope_match.py | FN | Scope grammar forces hyphens, so snake_case / dotted / spaced project names never trigger the project-name pass |
| 94 | LOW | scope_match.py | FN | origin.capture stores the symlink-RESOLVED cwd, but pass-2 does a literal find — paths spelled through the symlink (e.g. /tmp/... on macOS) never match the inferred root |
| 95 | MED | origin.py | FN | Userless scp-form remotes (host:path/repo.git) bypass all normalization — even .git-suffix variants of the identical remote never match |
| 96 | LOW | origin.py | FN | Home-relative vs absolute server-path spellings of one plain-SSH repo never match — git's documented-equivalent pair ssh://host/~/path and host:path parse to different (host, owner, name) triples |
| 97 | MED | origin.py | FN | Push-mirror setup (git remote set-url --add origin <mirror>) flips capture() to the LAST configured URL — the day a mirror is added, every previously written memory for the repo goes invisible |
| 98 | MED | audit.py | FP | Proactive-capture turns self-flag: a memory written THIS turn is probed as if it were retrievable, and `write` events don't shield the verdict |
| 99 | MED | hook.py | FP | Concurrent sessions hijack the retrieval-shield anchor: _latest_in_process_session picks whichever process wrote last, flipping the verdict both ways |
| 100 | MED | audit.py | FP | Bare numeric continuation replies ('3.8.0', 'option 2') defeat the no-signal gate via digit fragmentation and score 'high' against unrelated digit-bearing memories |
| 101 | MED | consolidate.py | FP | Default 0.75 Jaccard threshold sits below the natural similarity of parallel same-template facts differing in one load-bearing token — manual --apply tombstones a true per-host fact |
| 102 | MED | consolidate.py | FP | Demotion eligibility is instant upon first retrieval: a 31-day-old fact retrieved minutes ago is dead-weight, although the applied endorsement structurally lags retrieval by >=2 memory-tool turns |
| 103 | MED | consolidate.py | FP | Cold-scope pass keys only on `created`: a scope actively maintained via memory_update/memory_verify is flagged 'consider archiving' although its memory was rewritten 2 days ago |
| 104 | MED | consolidate.py | FP | Scope-typo pass has no rarity gate: two well-populated, legitimately distinct scopes (projects:app 22 vs projects:api 15; tools 30 vs books 8) are flagged as typo pairs with a mass-rename suggestion |
| 105 | MED | consolidate.py | FN | CJK bodies are invisible to Jaccard dedup: unspaced clauses tokenize as single giant tokens, so even a trivially rephrased duplicate Japanese memory scores 0.0 and is never flagged |
| 106 | MED | proposals.py | FN | _PREFERENCE_RE requires subject-verb adjacency: any modal, adverb, or negation between 'I'/'we' and the verb silently drops canonical preference statements |
| 107 | LOW | proposals.py | FN | max_proposals cap is applied at extraction, before queue dedup: a recurring preamble's sentences occupy all 3 slots every turn and permanently mask new durable statements later in the message |
| 108 | LOW | proposals.py | FP | _SENTENCE_SPLIT_RE fragments realistic prose: \n+ splits hard-wrapped sentences so a truncated dangling clause is proposed as the memory body, and 'e.g.'/'i.e.' mid-sentence drops canonical preferences entirely |
| 109 | MED | proposals.py | FP | 'for the future' explicit marker fires on ordinary deferral/roadmap prose ('leave X for the future', 'planned for the future') and its override defeats the let's/please command reject — one-off scoping decisions queued as durable 'fact' proposals |
| 110 | LOW | proposals.py | FP | Dedup is queue-membership only — a dismissed (or accepted) proposal's sentence is re-proposed the very next time it recurs, turning 'a bad proposal costs one dismissal' into one dismissal per recurrence |
| 111 | MED | search.py | FP | Scope-namespace token 'projects' matches every project-scoped memory; BM25's body-derived IDF makes the scope bonus outrank genuine full-coverage body matches |
| 112 | MED | search.py | FN | CJK memory bodies tokenize as one giant clause-length token — every realistic CJK query returns zero hits in all rankers AND the FTS5 index |
| 113 | MED | search.py | FP | Reduplicated phrase queries ('end to end', 'step by step', 'side by side') double-count the repeated token — an off-topic kebab-expansion match outranks the on-topic memory 2:1 |
| 114 | MED | search.py | FN | Retrieval-meta filler words ('remember', 'know', 'anything', 'stored', 'about', 'tell') are not stopwords — natural 'free text' retrieval queries deflate an exactly-on-topic memory to relevance 'low' (= documented 'probable noise') |
| 115 | LOW | durability.py | FN | 'recently' — the most common recent-action adverb — slips the gate entirely while 'i just'/'we just' fire |
| 116 | MED | durability.py | FN | Explicit temporariness vocabulary ('temporarily', 'for the time being', 'interim') slips while its synonym 'for now' fires |
| 117 | LOW | durability.py | FP | Time-word product/title homonyms fire 'tomorrow' and 'this week' on durable infra and preference facts (tomorrow.io API, Tomorrow Night theme, This Week in Rust) |
| 118 | MED | durability.py | FN | git-describe version strings (v3.7.1-5-g874b0b0) defeat the SHA detector — the g-prefix removes the \b, so the canonical machine-generated branch-state string slips |
| 119 | MED | credentials.py | FP | PEM detector fires on key-format documentation prose — the header line alone, with no key material, blocks the write |
| 120 | MED | credentials.py | FN | Digit-less passphrases (diceware/memorable style) never fire the generic rule — has_digit is a hard requirement |
| 121 | MED | credentials.py | FN | Trailing '}' from YAML flow mappings / JS object literals trips the env-template guard — an unquoted password at the end of an inline mapping never fires |
| 122 | MED | credentials.py | FN | Code assignment operators ':=' (Go) and '=>' (Ruby hashrocket) defeat the single-char [:=] separator — pasted config code with a live password never fires |
| 123 | MED | groundedness.py | FP | Numbered-list splitting glues the next item's index onto the previous fragment — bare section headers ('Action items:') are pushed over the MIN gate and flag 0.0 on fully grounded bodies |
| 124 | LOW | groundedness.py | FN | Paragraph-break regex \n\n+ requires strictly adjacent LFs — CRLF bodies and blank lines with trailing whitespace merge paragraphs, diluting a hallucinated paragraph into a pass |
| 125 | MED | groundedness.py | FN | Comma-coordinated embellishment smuggling: a grounded head clause anchors a majority-fabricated sentence — the canonical 'extractor embellishes the fact' failure passes untouched |
| 126 | MED | groundedness.py | FN | Kebab expansion triple-counts a single shared compound in the overlap numerator — one hyphenated identifier from the transcript grounds an otherwise fabricated claim at ratio 0.5 |
| 127 | MED | scope_match.py | FP | Monorepo shared cwd: every path-citing write tagged with one sub-project is gated to add every sibling sub-project (identical roots, symmetric false flag) |
| 128 | MED | origin.py | FN | SSH-config Host-alias remotes (git@github-work:...) never match the canonical host — the standard multi-account GitHub setup splits one repo into two identities |
| 129 | LOW | origin.py | FN | HTTP mount-prefix remotes misparse the prefix as owner: Gerrit's authenticated '/a/' makes two HTTPS clones of the SAME project never match each other, and relative-URL GitLab/Apache '/git/' installs never match their SSH form |
| 130 | MED | hook.py | FN | Stop-hook probe audits synthetic transcript rows (task notifications, skill expansions, command stdout) instead of the user's message — real silent misses structurally suppressed |
| 131 | MED | audit.py | FP | Project-suppression gate requires origin.repo on the MEMORY: project-scoped memories written outside the checkout (Claude Desktop in $HOME, ingest, web UI, legacy) re-open the 95%-noise cohort — false search_miss on every in-project continuation |
| 132 | MED | audit.py | FP | Event-log rotation mid-window archives the turn's own search event — the retrieval shield reads only the active log and a searched-then-continued turn emits a false search_miss |
| 133 | MED | audit.py | FN | Probe cannot thread recency_boost_half_life_days or endorsement_boost — it ranks with hardwired defaults while production search uses the configured values, so the rank-1-only verdict diverges from the model's actual ranker |
| 134 | MED | consolidate.py | FP | Demotion treats contradiction-flagged memories as dead weight, and the retag's `updated` bump silently clears health's unresolved-contradiction signal |
| 135 | MED | consolidate.py | FP | propose_new's provenance stamp is system-manufactured boilerplate that defeats Jaccard dedup: the second distinct fact from the same turn is rejected as a 'near-duplicate' |
| 136 | MED | consolidate.py | FP | find_cold_scopes has no ambient exclusion: all-ambient scopes are flagged 'consider archiving' for lacking the applied signal that is structurally absent for ambient by design |
| 137 | MED | consolidate.py | FN | Cold-scope pass: a single applied event ever grants permanent immunity — finished-but-once-useful project scopes (the most common archivable shape) are never suggested for archiving |
| 138 | MED | proposals.py | FP | _PREFERENCE_RE has no trailing word boundary on the verb alternation — past-tense forms ('I wanted', 'I used', 'We used', 'I liked', 'I needed') fire the present-tense preference branch |
| 139 | MED | proposals.py | FN | we-branch verb list is missing six of the i-branch's eleven verbs, and uncontracted 'I am using' / 'We are using' never match — canonical project-setup facts silently dropped |
| 140 | MED | proposals.py | FP | Conditionals and hypotheticals ('If I use X, we need Y', 'Suppose I always ...') are captured as durable user-inference facts — no conditional guard anywhere in the gate chain |
| 141 | MED | hook.py | FP | hook.py mines harness-injected rows (isMeta skill expansions, command wrappers) as 'the user's own words' — 907 real Stop points on this machine fed non-user text to the extractor, producing proposals from documentation prose |
| 142 | MED | proposals.py | FP | Negative-contraction questions ('Shouldn't I ...', 'Wouldn't it ... if I use ...', 'Couldn't we avoid ...') and '?!'-terminated questions escape both question rejects and are queued as user-inference facts |
| 143 | MED | _handlers.py | FN | FTS top-50 prefilter starves the auto-scope repo/worktree filter: in-repo matches ranked #51+ globally return zero hits on large stores |
| 144 | MED | search.py | FP | Keyword scorer: unbounded body TF beats the 2x-capped coverage multiplier — a 1-of-3-terms memory outranks the full-coverage match and tops hybrid mode too |
| 145 | MED | search.py | FN | No stemming anywhere in the pipeline: plural/singular query inflections ('standups' vs 'standup') are a total miss in every ranker AND the FTS5 index |
