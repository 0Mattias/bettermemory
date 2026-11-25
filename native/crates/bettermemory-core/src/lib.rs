//! The core of bettermemory 9's native build: the parts of the Python
//! reference that need no database, reproduced exactly.
//!
//! - [`config`]: the settings: the config file's loader, the managed
//!   file's pins, the first-run write, and `config check|show|path --json`;
//! - [`difflib`]: `difflib.unified_diff` as `history._body_diff` calls it
//!   (the matcher with autojunk, the grouped opcodes) and `str.splitlines`;
//! - [`identity`]: a request's caller as the reference resolves it, its
//!   leaves first (the declared-value check, the actor rule, `resolve`),
//!   the first usable `file://` root and `workspace_declaration`;
//! - [`session`]: `session.py` whole: the per-session state (scopes, the
//!   turn counter, use-tokens), the per-client registry and its LRU;
//! - [`use_tokens`]: the per-turn use-token bookkeeping of
//!   `handlers/_shared.py` (the turn bump and its auto-commit, the expiry
//!   drain and its event, the settled-`use` scan, the hits' tokens);
//! - [`json`]: `json.dumps` as the reference's call sites spell values
//!   (the log payload's canonical text among them) and `json.loads`;
//! - [`native_text`]: exact native strings at UTF-8 boundaries (the
//!   surrogate test, `utf8_text`, `encoded_json`, `origin_projection`);
//! - [`caches`], [`daemon_client`] and [`install_hints`]: the registry of
//!   module-level caches, the hook client's budget constants and
//!   `hook_budget_seconds`, and the install-command spellings;
//! - [`in_force`]: what is in force at delivery (`in_force.py` whole):
//!   the supersession head, the newest extender, the held slot;
//! - [`admission`]: `admission.py`'s vocabulary beside `store::caller`'s
//!   `Admission`: `DriftScope`, `CountedRecords`, `EveryRecord`,
//!   `admitted_nothing`;
//! - [`health_summary`]: `health_summary.py` whole: the default health
//!   answer and the answer naming sections;
//! - [`health_model`]: the report model of `health.py`, its first unit:
//!   the dead-weight, contradiction and hook-telemetry predicates, the
//!   applied tiers, and the per-memory, per-marker, per-scope and
//!   per-actor report types with their `to_dict`;
//! - [`health_roots`]: `health_roots.py`'s estate progress cursor;
//! - [`float_repr`]: Python's `float.__repr__`;
//! - [`datetime`]: the datetime spellings and readings;
//! - [`argparse`]: the flat-parser subset of `argparse` the hook mains
//!   parse through, its first two units (optionals and positionals of one
//!   argument, the error exits and the usage line; subparsers and the
//!   PARSER `nargs`);
//! - [`transcript`]: a transcript read from its end, whole: the reader,
//!   the Stop turn, interrupted turns, the owner's words and the
//!   compaction marker (`ReverseLines`, `_row`, `_prompt_id`,
//!   `stop_turn`, `interrupted_turns`, `owner_words`, `compact_marker`),
//!   with the file
//!   and `owner_prompt.py` as traits;
//! - [`owner_prompt`]: `owner_prompt.py` but `probe_text`, which callers
//!   supply until the regular-expression engine reads its patterns;
//! - [`policy`]: `policy.py` whole: the `[policy]` refusal of a write
//!   tool's override flags and its audit row;
//! - [`origin_history`]: how `origin` reads a repository's history: the
//!   leaves and the memo of author dates, and (`origin_history::located`)
//!   the repository's key, the history guard and the shallow cuts
//!   (`cuts_in`);
//! - [`origin_model`]: `Origin` as a caller captures it, the stored part
//!   with the private capture state beside it;
//! - [`origin_dates`]: the author dates a drift count is read off, its
//!   git-asking leaves (the log reads, why `git log HEAD` was refused,
//!   the extension of a remembered history) and (`origin_dates::memo`)
//!   the memoised read of a head's whole history;
//! - [`unicode`]: normalization, case mapping and the character classes,
//!   XID_Start and XID_Continue (`unicode::xid`, `str.isidentifier`),
//!   over tables generated from the reference Python's Unicode 15.1.0;
//! - [`pyre`]: Python's regular expressions as CPython 3.13.13's `re` runs
//!   them, its first units: the case folding and the test of one literal;
//!   the parser's tree and its dump, the parser's state and its tokenizer,
//!   the escapes, sets and flat sequences it reads, and `re.escape`; the
//!   repeats the parser reads and the width of a tree; `re._parser.parse`
//!   with alternatives, groups, lookarounds and inline flags, and the
//!   refusal of what the matcher has no rule for; a class
//!   compiled as `_optimize_charset` compiles it, with its opcode and the
//!   test of one code point against it; a parsed
//!   tree lowered to the engine's arena, its repeats classed as
//!   `re._compiler` classes them; a lowered program run as `_sre` answers
//!   `match`, `fullmatch` and `search`, lookarounds included;
//!   `re.Pattern` built from a pattern's text and flags, with `match`,
//!   `fullmatch`, `search`, `finditer` and `findall` and `re.Match`;
//!   `sub`, `subn` and `split` with a count, a literal or a function
//!   replacement; every pattern of the reference proven against `re`
//!   (tests only); the call-owned memo that keeps the hostile texts of
//!   the reference's patterns linear in steps, and the steps of a call;
//! - [`models_text`]: the text helpers of `models.py`: a memory's slug
//!   and file name, its summary line, the truncation heuristic and the
//!   search snippets;
//! - [`standing`]: `standing.py`: its constants,
//!   the standing test, the probability check, the answer a put writes,
//!   and the lines and questions the surfaces carry; and its store
//!   reads (the body sha memo, the backlog, the standing, guarded and
//!   held ids, the refusal rows) over the `StandingStore` and
//!   `Admission` traits;
//! - [`review`]: `review.py`'s sealed tables (the related_for constants,
//!   the thresholds and their overrides, the labels and statements,
//!   `QUESTIONS` and the seal shas, the action names), the related-set
//!   records, the text one related entry shows, the roster line, and the
//!   answers with their readers, teaching errors and two checks, and the
//!   pure write decision, its replacement IDs and its event fields;
//! - [`mirror_format`]: the v8 file names and frontmatter the mirror
//!   writes (`mirror_format.py` whole);
//! - [`lexicon`]: the search tokenizer and its fingerprint;
//! - [`verify_candidates`]: the path candidates verification reads, its
//!   first three units (one candidate validated; `_BACKTICK_RE`,
//!   `_BARE_RE` and `_DOMAIN_ROUTE_RE` as hand matchers; the candidate
//!   scan of a body);
//! - [`path_drift_steps`]: `_check_body_candidates`, the body-candidate
//!   pass of path drift, its effects as closures;
//! - [`attestation_paths`]: explicit path attestations compiled beside
//!   the legacy reference grammar (`attestation_paths.py` whole);
//! - [`project_file`]: the project file, its first two units (the
//!   vocabulary, `mint_id`, `render` and the id and name checks; `parse`
//!   and its bounded read);
//! - [`project_walk`]: the project file's upward walk (`Walk`,
//!   `above_start`, `find`, `_walk`, `_look`) and `write` (its third unit);
//! - [`log`]: the row MAC, the key ring and `verify_chain`, and the key
//!   ring's files (`log::ring`: the current key, the origin anchor, the
//!   binding); the segment map with the caller's key (`log::segments`)
//!   and the rows that say a history was replaced (`log::replaced`);
//! - [`machine`]: this machine's id, its first three units (the identity
//!   record, the id minted from given bytes, the clone rule; the hardware
//!   id, the hostname, the id file's object and the two files read;
//!   `open_machine` and `_write_record`, with the machine's own reads);
//! - [`search`]: `search.search` over records in memory, bit for bit,
//!   the query-biased snippet and its memos, and the token cache;
//! - [`search_dedup`]: write-time dedup, the Jaccard scorer and its
//!   token cache;
//! - [`records`]: the reference's record models (memory, tombstone,
//!   episode, origin, actor, link) validated from a payload or a row and
//!   written back, with pydantic's errors, and the result models
//!   (`records::summaries`: the search hit and the two listings' rows);
//! - [`history`]: one record's history read off the log, its first two
//!   units (the rows that name a record, with the linking records'
//!   versions and the rows that say the store's history was replaced; a
//!   row's MAC checked under its chain segment's key, and `_set`);
//! - [`history_rows`]: `history.py`'s third unit, what one log row of a
//!   record's history says (`HistoryRow`, `_changed`, `_event_detail`,
//!   `_record_row`, `_link_change_rows`);
//! - [`history_record`]: `history.py`'s fourth unit, one record's whole
//!   history (`RecordHistory`, `record_history`, `_read_history`,
//!   `_state`, `_table_kind`, `replaced_detail`);
//! - [`history_render`]: `history.py`'s fifth unit, a record's history
//!   as `memory history` prints it (`render_text` with `_actor_text`,
//!   `_value_text` and `_VERIFIED_TEXT`, and `render_json`);
//! - [`store`]: a store file read without writing to it: the version
//!   contract, the tables, the key ring from a keys directory, the chain
//!   verified on disk, the FTS5 prefilter, the additive schema steps
//!   with their leaves (`store::schema`), the digest's questions
//!   (`store::digest_query`), the restore lock as a read
//!   and a writing open meet it and a restore holds it
//!   (`store::restore_lock`: `reading`, `admit`, `restore_state`,
//!   `acquire`, `held_here`, `probe`, `unlock`, `state_finding`), and
//!   `store_rows`' shared leaves (the
//!   membership specs, the row sort, the schema's columns, the
//!   verification row, a log row as a telemetry event and the kind sets);
//!   the membership tables' store methods (`store::membership`: the puts,
//!   deletes and control rows on the write transaction, the batched
//!   reads over legacy and native rows, the id counters, `committed_seq`,
//!   `in_batch`, `wrote_record` and the store id);
//!   `store.py`'s connection leaves and its admitted reads (`store::reads`);
//!   the finish of a committed key rotation (`store::rotation`); the
//!   keys a store opened without writing keeps
//!   (`store::key_read::open_reader`: `_open_connected`'s dispatch,
//!   `_open_reader`'s read key, `Store.can_verify`); the
//!   episode journal's reads (`store::journal`); the upgrade's proof
//!   that no other connection holds the store (`store::upgrade`); the
//!   document frequency memo with the store's side as a trait
//!   (`store::docfreq`) and that side over a store connection with
//!   `document_frequencies`, `ranking_statistics` and an entry carried
//!   over a moved head by the log's record rows
//!   (`store::docfreq_store`); the event and capture readers and the window
//!   floor (`store::event_reads`); the plain reads of the index and the
//!   records over a connection (`store::index_reads`,
//!   `store::record_reads`); the event writes with their retry and
//!   deadline, the v8 import, the migration and capture rows
//!   (`store::record_event`); the owner's writing open's upkeep with its
//!   key present, a foreign tail closed and the origin anchor settled
//!   (`store::open`, `open/upkeep.rs`); and the returned marks' backfill on the
//!   writable store (`store::returned`: `missing_links`,
//!   `returned_rows`, `backfill`, `ensure_backfilled`);
//! - [`store_refusal`]: the stores a hook refuses before it starts a
//!   daemon (`_store_refusal.py` whole: `bundle_refusal`, `restore_state`,
//!   `restore_refusal`, `store_refusal`);
//! - [`membership_records`]: the vocabulary and records of the membership
//!   model, units 1 to 4 (the legacy ask rule and the helpers that
//!   compare, join and rebase recorded paths in either spelling; the
//!   manifest's JSON and match, the reader traits and the checkout,
//!   view, project and proposal records; the portable identity and the
//!   remaining frozen records);
//! - [`membership_shared`]: one git directory several machines see, its
//!   first unit (the stamp and tick comparisons, the re-mint read from
//!   another machine's view over carriers given, and the predicates over
//!   a checkout's question rows and log rows), and its heal half in
//!   `membership::shared_heal` (`_carriers`, `_restamped`, `_healed`,
//!   `_chosen`, `retired_into`, `_folded` over the `MembershipLog` trait)
//!   and its asking half in `membership::shared_ask` (`_ask_which`,
//!   `_retire_into`, `_asked_at`, `_asked_here`, `_unseen`, `_asked_plan`);
//! - [`membership_rows`]: the membership table rows read into records and
//!   written back, the object-key and stamp codecs, `_jsonable`,
//!   `_signature`, `_unwritten` and `_Tally`;
//! - [`membership`]: the membership model over its store trait, units 1
//!   to 3 (its state and plumbing, the projects with merges followed, the
//!   members index, the witness, file-binding and checkout reads; the
//!   witness, remote, file-binding, boundary and checkout writes; the
//!   views and their liveness, continuity, the checkout of an object
//!   store, the history readings), unit 2b (the machine rows and the
//!   proposals) and unit 4a (the sighting of a known checkout,
//!   `_touch_view`, and the token re-mints: `_checkout_name`,
//!   `_give_token`, `token_written`, `_foreign`), and the read side of
//!   `membership_queries.py` in `membership::queries` (homes, the prompt
//!   recall's admission, a question as it is shown), and unit mlegacy in
//!   `membership::legacy` (the legacy questions settled, the open
//!   questions shown, a directory against a checkout's root, the record
//!   counts by project), and rung 2 of the resolution ladder in
//!   `membership::rung` (`_token_now`, `_token_read`, `_by_object`,
//!   `_rung_two` of `membership_resolve.py`);
//! - [`declare`]: `declare.py`'s `declaring_model`, the model a
//!   declaration opens, with its token writer (`write_token`);
//! - [`location_continuity`]: `location.continuity`, witness O where birth
//!   time cannot decide;
//! - [`toml`]: `tomllib.loads`, its values and its refusals;
//! - [`tool_handlers`]: the refusal classes and the dependency bundle of
//!   `_handlers.py`, its first unit (the nine forwarders are later);
//! - [`events`]: the telemetry recorder's rows, its first unit (the
//!   event kinds, their row classes and the fields a state row keeps)
//!   and the id-list readers (`event_id_items`, `event_id_list`);
//! - [`event_redact`]: `events.py`'s second unit: the query redaction
//!   (`redact_query`, the five secret patterns, the detector spans passed
//!   in) and the named event fields (`named_event_fields`,
//!   `_redact_event_fields`);
//! - [`time_utils`]: `isoformat_utc_optional` and `ensure_utc`;
//! - [`event_hook`]: `events.py`'s suppression switch, a hook's deadline
//!   and busy timeout (`HookScope`), `StoreBusy`'s text and the SQLite
//!   busy test (`primary_code`, `is_busy`);
//! - [`event_recorder`]: `events.py`'s fourth unit: `Recorder`
//!   (`session_for_request`, `worktree_for_request`, `records`, `record`)
//!   over a store taken as the trait `EventSink`;
//! - [`audit`]: the silent-miss audit (the constants, `MissHit`,
//!   `MissReport`, the `turn_audited`, `search_miss`, `recall_held` and
//!   `prompt_recall` field builders, the re-audit dedup, the miss probe
//!   over a ranker trait and its retrieval shield);
//! - [`recall_settle`]: the Stop's settlement of shown pointers
//!   (`settle_shown`, `settle_unused`), `previous_audit_at` and
//!   `review_unanswered`, over traits for the store, recorder and state,
//!   and in `interrupted` `_probe_message` and `settle_interrupted`, the
//!   probe, the pool and the transcript read taken as closures;
//! - [`recall_prompt`]: the prompt recall's turn, window, settlement,
//!   probe and the `recall_held` and `prompt_recall` rows;
//! - [`control_tokens`]: the chat-template delimiters a stored text may
//!   not carry, found, counted, refused and written by their bare names;
//! - [`recall`]: the prompt recall: the table of the user's words
//!   (`WordsSeen`), the recall block's renderer, and the hit choice and
//!   delivery (`recall::choose`);
//! - [`durability`]: the write gate's transient markers, its first unit
//!   (the marker's canonical form, quoted spans, the snippet and sentence
//!   helpers);
//! - [`gitconfig`]: the text of a git config file as the reference reads it;
//! - [`git_read_budget`] and [`git_calls`]: the shared deadline of a
//!   call's history reads (`git_read_budget.py` whole), and `_git.py`'s
//!   call tallies, `printed_path`, `subcommand` and the nonzero-exit line;
//! - [`git_run`]: `_git.run_git` whole over a process the caller hands in,
//!   and `log_nonzero_exit`, the logged lines returned as values;
//! - [`location_stamps`]: a git directory's init stamps and whether its
//!   file system keeps FILETIME's ticks (the whole module);
//! - [`location_view`]: `location.py`'s view continuity (`continuity`,
//!   `view_state` and their helpers), with `object_key`, `_at_dotgit`,
//!   the project file's parse and the history's `holds` as arguments;
//! - [`location_cache`]: the capture signature and the capture cache
//!   (`location.py`'s `signature`, `_capture_cached`, `capture`);
//! - [`location_answer`]: `location.py`'s client answer to a daemon's
//!   question and its liveness reads (`path_liveness`,
//!   `path_liveness_many`, `answer`, `Exchange`, `_history_of`), the
//!   history, the re-mint and the view read as arguments;
//! - [`location_git`]: a repository's head, ref tips, content manifest
//!   and bounded history walks, the readers of `location_git.py` that need
//!   neither discovery nor the object reader;
//! - [`location`]: the caller's location (`location.py`), units 1a and
//!   1b: the capture's filesystem leaves (birth time, the object key,
//!   eviction and fetch, the HEAD stamp, the local remote key, the
//!   kept-apart rule, the working-tree record, the resolved git paths)
//!   and the uncached capture (the project-file walk and its cache above
//!   a root, the tracked-file read, the token fields);
//! - [`location_reads`]: the bounded reads of recorded paths as a timed
//!   runtime on worker threads (the whole module);
//! - [`location_discovery`]: the repository git finds from a directory
//!   (`GitDies`, `DiscoveryDies`, `_from_env`, `_discover`, `_at_dotgit`);
//! - [`repository_history`]: `location_git.RepositoryHistory` and its
//!   git fallback `_git`: a repository's objects through an object
//!   reader made on first use, `git cat-file` where it is incomplete, and
//!   `git merge-base --independent`;
//! - [`githead`]: HEAD and refs read from a git directory's files, and
//!   discovery (unit githead-1: the walk git's `setup.c` makes, `stamp`;
//!   unit githead-2: `entry_above` and the change signature);
//! - [`location_probe`]: the hook client's namespace probe (the cwd's
//!   state, its view spelling, the host's name), on Windows over
//!   `fsdata::realpath`'s `ntpath.realpath` and `abspath` (unit ntreal);
//! - [`hook_stop`]: the Stop audit's front half (`run_audit` through
//!   `_is_repeat_audit`, unit hook-2a) as a sequence over
//!   [`hook_stop_side::StopSide`], the calls into modules not yet ported,
//!   with the settlement steps behind `hook_stop::StopSettle`;
//! - [`hook_settle`]: the Stop audit's settlement half (unit hook-2b):
//!   `_settle_stop_turn`, `_record_stop_rows`, `_count_stop_reviews` and
//!   `_settle_stop_interrupted`, as `hook_settle::Settlement`;
//! - [`location_witness`]: the witness exchange's history half: the
//!   question raised (`NeedWitnesses`), the readers over the client's
//!   answers, git's reading of tied witnesses and the keeping of one
//!   exchange's reads between rounds (unit location_witness-1);
//! - [`witness_questions`]: the question half of `location_witness.py`:
//!   the answered token and liveness readers, the client's re-mint answer
//!   and its refusal, and the client's reading of a view question;
//! - [`location_caller`]: a caller's resolution, its first unit (the
//!   per-process state around a resolution: the machine opened once,
//!   the per-location locks, where each session stands, the file
//!   errors' lines and the namespace test);
//! - [`location_resolve`]: a caller's resolution, unit loccaller (the
//!   capture, the model, the ladder under the location's lock, the
//!   token re-minted where asked, the session's stand kept by a hook);
//! - [`location_hook`]: a caller's resolution, unit lochook (a hook's
//!   caller resolved from what it posted: captured here, from the
//!   client's token, or by asking it an exchange);
//! - [`gitobjects`]: git objects read from a repository's files, the
//!   whole module in four units (the byte parsers; the zlib streams and
//!   the delta chain over a file handle; the file reads, the object format
//!   and the alternates walk; the reader's object questions);
//! - [`fsdata`]: the file system as the config readers ask it, as data;
//! - [`fsutil`]: `_fsutil.py`, its first two units (`unlink_retrying` with
//!   its Windows retry, `bounded_stream_read`, `flock_excl` and its
//!   Windows branch `_flock_windows`);
//! - [`verify_anchoring`]: whether cited paths are anchored, its first
//!   two units and unit 2a (the sparse-checkout switch, git's varint, the
//!   index's skip-worktree reader, and the probe that reads them from disk
//!   with its memo; the drift report and the claim anchors; the attestation
//!   write gate; the attestation and citation read passes; the drift
//!   readers `detect_path_drift` and `holds_commit`);
//! - [`verify_status`] and [`attribution`]: a memory's verification
//!   staleness, and phrase-match attribution of memories to a reply;
//! - [`response_rows`]: `ResponseBuilder`'s search and list serializers
//!   (`hit_to_dict`, `summary_to_dict`, `tombstone_summary_to_dict`);
//! - [`response_write`]: `ResponseBuilder`'s write and review shapes
//!   (`committed`, `review`, `roster`, the similar and transient rows, the
//!   origin and actor dicts);
//! - [`response_history`]: the location, key eligibility and whole-history reads of a drift pass;
//! - [`response_links`], [`response_memo`] and [`verdict_signals`]: the
//!   response layer's leaves (`candidate_link_rows`, the per-hit drift
//!   memo as a generic bounded LRU, and the verdict ladder);
//! - [`verify_basis`] and [`verify`]: the bases a commit-drift count is
//!   measured on and the count since a verify (`verify_basis.py` whole,
//!   `origin`'s two reads as a trait), and `verify.py`'s status leaves
//!   (`CommitDriftStatus`, `commit_drift_status`,
//!   `staleness_verdict_from_report` and the claim gates), and the
//!   claim-level narrowing (`_weak_tier_evaluation`, `_claim_legs`, both
//!   resolvers and `resolve_commit_drift`, git and the spelling as traits),
//!   and the claim anchors and `compute_commit_drift` with its count;
//! - [`hook`]: the hooks of `hook.py`, their first unit (the stdin
//!   payload reader, the pending-retrieval scan and the Stop's
//!   attribution settlement);
//! - [`response_render`]: the wire shapes of a search hit and of
//!   `memory_show` (`render_hit`, `render_show` and their helpers);
//! - [`response_reason`] and [`response_trust`]: the reason a row asks for
//!   a check and the verdict with its objection, and the trust appliers
//!   that rewrite a row's verdict from the store's trust rows;
//! - [`verdict`]: one staleness verdict per memory (`verdict.py`'s
//!   `resolve_verdicts`, `memoised_verdicts`, `memory_verdict` and
//!   `_verdict`), the commit legs a trait and the path drift read a
//!   closure;
//! - [`handlers`]: the MCP tool handlers, their first unit (the pure
//!   gates of `handlers/verify.py`);
//! - [`printable`] and [`membership_lines`]: text made safe to show, and
//!   the lines shown about which project a folder belongs to;
//! - [`membership_migrate`]: the migration to the membership model, its
//!   first unit (the constants, the report types with `to_dict` and
//!   `lines`, and the pure text helpers);
//! - [`entry`] and [`cli`]: the process entry's hook-word routing, and
//!   the CLI's shared leaves (`cli::common`: the refusals, the
//!   key-unreadable line, the migration line, the ISO cutoff parser) and
//!   the CLI root (`cli::root`: the root parser and `main`'s dispatch over
//!   command modules taken as trait objects);
//! - [`origin_remote`]: a remote URL read into the triple `origin`
//!   compares, with `urllib.parse.urlparse` as CPython 3.13.13 has it;
//! - [`credentials`]: the write gate's credential detectors, whole: the
//!   vendor-prefixed shapes, the value gate, the generic keyword rule and
//!   the URI password on the engine, and the spans, snippets and markers;
//! - [`builder`]: the server builder's two pure schema scrubs
//!   (`_strip_titles`, `_fold_nullable`);
//! - [`origin_gitfiles`]: the repository files `origin` reads in-process,
//!   its first three units (the pure decoders; the leaves that read the
//!   machine: a file's prefix, a path relative to the root, the root as
//!   the kernel spells it; and what git dies on in a config, what a config
//!   text says, and the directories a check holds), unit 4 (the config
//!   memo and the root read from the files), unit 6 (the attribute
//!   file signatures and the index's `.gitmodules` entry) and unit 7
//!   (the origin cache's signature, the files beside the history, the
//!   located head and the check at the end of a search), so the whole
//!   module;
//! - [`checkout_token`]: the checkout token, whole: the text, the reads
//!   over the file-system trait, the write, and the re-mint with its
//!   claim;
//! - [`claims`]: what a memory claims, its first two units (the claim
//!   record, the path spellings and the patterns a patch is read by; the
//!   claim parser and the literal readers over [`pysyntax`]);
//! - [`claim_drift`]: the drift index of `claims.py` (`_Headers`,
//!   `build_binding_index`, `claim_level_drift`), pure over a patch
//!   stream, with the literal helpers as a trait;
//! - [`symbols`]: `symbols.py`'s checker, its first unit: the bounds, the
//!   citation records and `check_symbol_citations` over parsed citations,
//!   with a module's names a trait;
//! - [`pysyntax`]: Python source as CPython 3.13.13 reads it, its first
//!   four units: the eval-mode tokenizer `ast.literal_eval` meets, with
//!   string and number literals read, the literal reader with
//!   `claims._canonical_repr`, and the module-mode tokenizer `ast.parse`
//!   meets (logical lines, INDENT and DEDENT) with its f-string modes;
//! - [`gitfile_rule`]: the rule a `.git` entry is followed by, whole on
//!   POSIX: from a `gitdir:` file's bytes to the judge of an entry and the
//!   walk git's discovery makes;
//! - [`handler_checks`]: the write-payload validators of
//!   `handlers/_shared.py`, its first unit;
//! - [`standing_guard`]: the guard on a change to a standing memory
//!   (`handlers/_guard.py` whole).

#![forbid(unsafe_code)]

pub mod admission;
pub mod anchor;
pub mod argparse;
pub mod attestation_paths;
pub mod attribution;
pub mod audit;
pub mod builder;
pub mod caches;
pub mod capture;
pub mod checkout_token;
pub mod claim_drift;
pub mod claims;
pub mod cli;
pub mod config;
pub mod control_tokens;
pub mod credentials;
pub mod daemon_client;
pub mod datetime;
pub mod declare;
pub mod difflib;
pub mod durability;
pub mod entry;
pub mod event_hook;
pub mod event_recorder;
pub mod event_redact;
pub mod events;
pub mod float_repr;
pub mod fsdata;
pub mod fsutil;
pub mod git_calls;
pub mod git_read_budget;
pub mod git_run;
pub mod gitconfig;
pub mod gitfile_rule;
pub mod githead;
pub mod gitobjects;
pub mod gitsystem;
pub mod handler_checks;
pub mod handlers;
pub mod health_model;
pub mod health_roots;
pub mod health_summary;
pub mod history;
pub mod history_record;
pub mod history_render;
pub mod history_rows;
pub mod hook;
pub mod hook_settle;
pub mod hook_stop;
pub mod hook_stop_side;
pub mod identity;
pub mod in_force;
pub mod install_hints;
pub mod json;
pub mod lexicon;
pub mod location;
pub mod location_answer;
pub mod location_cache;
pub mod location_caller;
pub mod location_continuity;
pub mod location_discovery;
pub mod location_git;
pub mod location_hook;
pub mod location_probe;
pub mod location_reads;
pub mod location_resolve;
pub mod location_stamps;
pub mod location_view;
pub mod location_witness;
pub mod log;
pub mod machine;
pub mod membership;
pub mod membership_lines;
pub mod membership_migrate;
pub mod membership_records;
pub mod membership_resolve;
pub mod membership_rows;
pub mod membership_shared;
pub mod mirror_format;
pub mod models_text;
pub mod native_text;
pub mod origin_cache;
pub mod origin_dates;
pub mod origin_git;
pub mod origin_gitfiles;
pub mod origin_history;
pub mod origin_match;
pub mod origin_model;
pub mod origin_remote;
pub mod owner_prompt;
pub mod path_drift_steps;
pub mod policy;
pub mod printable;
pub mod project_file;
pub mod project_walk;
pub mod pyre;
pub mod pysyntax;
pub mod pyvalue;
pub mod recall;
pub mod recall_prompt;
pub mod recall_settle;
pub mod records;
pub mod repository_history;
pub mod response_history;
pub mod response_links;
pub mod response_memo;
pub mod response_reason;
pub mod response_render;
pub mod response_rows;
pub mod response_trust;
pub mod response_write;
pub mod review;
pub mod search;
pub mod search_dedup;
pub mod session;
pub mod standing;
pub mod standing_guard;
pub mod store;
pub mod store_refusal;
pub mod symbols;
pub mod time_utils;
pub mod toml;
pub mod tool_handlers;
pub mod transcript;
pub mod unicode;
pub mod use_tokens;
pub mod verdict;
pub mod verdict_signals;
pub mod verify;
pub mod verify_anchoring;
pub mod verify_basis;
pub mod verify_candidates;
pub mod verify_status;
pub mod witness_questions;
