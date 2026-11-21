//! The core of bettermemory 9's native build: the parts of the Python
//! reference that need no database, reproduced exactly.
//!
//! - [`config`]: the settings: the config file's loader, the managed
//!   file's pins, and `config check|show|path --json`;
//! - [`identity`]: a request's caller as the reference resolves it, its
//!   leaves first (the declared-value check, the actor rule, `resolve`);
//! - [`session`]: `session.py` whole: the per-session state (scopes, the
//!   turn counter, use-tokens), the per-client registry and its LRU;
//! - [`json`]: `json.dumps` as the reference's call sites spell values
//!   (the log payload's canonical text among them) and `json.loads`;
//! - [`in_force`]: what is in force at delivery (`in_force.py` whole):
//!   the supersession head, the newest extender, the held slot;
//! - [`float_repr`]: Python's `float.__repr__`;
//! - [`datetime`]: the datetime spellings and readings;
//! - [`origin_history`]: how `origin` reads a repository's history, its
//!   first unit (the leaves and the memo of author dates);
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
//!   test of one code point against it;
//! - [`models_text`]: the text helpers of `models.py`: a memory's slug
//!   and file name, its summary line, the truncation heuristic and the
//!   search snippets;
//! - [`lexicon`]: the search tokenizer and its fingerprint;
//! - [`verify_candidates`]: the path candidates verification reads, its
//!   first unit (one candidate validated);
//! - [`log`]: the row MAC, the key ring and `verify_chain`, and the key
//!   ring's files (`log::ring`: the current key, the origin anchor, the
//!   binding);
//! - [`machine`]: this machine's id, its first unit (the identity
//!   record, the id minted from given bytes, the clone rule);
//! - [`search`]: `search.search` over records in memory, bit for bit;
//! - [`search_dedup`]: write-time dedup, the Jaccard scorer and its
//!   token cache;
//! - [`records`]: the reference's record models (memory, tombstone,
//!   episode, origin, actor, link) validated from a payload or a row and
//!   written back, with pydantic's errors;
//! - [`history`]: one record's history read off the log, its first unit
//!   (the rows that name a record, with the linking records' versions and
//!   the rows that say the store's history was replaced);
//! - [`store`]: a store file read without writing to it: the version
//!   contract, the tables, the key ring from a keys directory, the chain
//!   verified on disk, the FTS5 prefilter, the additive schema steps
//!   with their leaves (`store::schema`), and the restore lock as a read
//!   and a writing open meet it (`store::restore_lock`: `reading`,
//!   `admit`, `restore_state`);
//! - [`membership_records`]: the vocabulary and records of the membership
//!   model, units 1 to 3 (the legacy ask rule and the helpers that
//!   compare, join and rebase recorded paths in either spelling; the
//!   manifest's JSON and match, the reader traits and the checkout,
//!   view, project and proposal records);
//! - [`membership_rows`]: the membership table rows read into records and
//!   written back, the object-key and stamp codecs, `_jsonable`,
//!   `_signature`, `_unwritten` and `_Tally`;
//! - [`toml`]: `tomllib.loads`, its values and its refusals;
//! - [`events`]: the telemetry recorder's rows, its first unit (the
//!   event kinds, their row classes and the fields a state row keeps);
//! - [`control_tokens`]: the chat-template delimiters a stored text may
//!   not carry, found, counted, refused and written by their bare names;
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
//! - [`location_git`]: a repository's head, ref tips, content manifest
//!   and bounded history walks, the readers of `location_git.py` that need
//!   neither discovery nor the object reader;
//! - [`location_reads`]: the bounded reads of recorded paths as a timed
//!   runtime on worker threads (the whole module);
//! - [`githead`]: HEAD and refs read from a git directory's files, and
//!   discovery (unit githead-1: the walk git's `setup.c` makes, `stamp`);
//! - [`location_probe`]: the hook client's namespace probe (the cwd's
//!   state, its view spelling, the host's name);
//! - [`location_witness`]: the witness exchange's history half: the
//!   question raised (`NeedWitnesses`), the readers over the client's
//!   answers, git's reading of tied witnesses and the keeping of one
//!   exchange's reads between rounds (unit location_witness-1);
//! - [`witness_questions`]: the question half of `location_witness.py`:
//!   the answered token and liveness readers, the client's re-mint answer
//!   and its refusal, and the client's reading of a view question;
//! - [`gitobjects`]: git objects read from a repository's files, its
//!   first three units (the byte parsers; the zlib streams and the delta
//!   chain over a file handle; the file reads, the object format and the
//!   alternates walk);
//! - [`fsdata`]: the file system as the config readers ask it, as data;
//! - [`fsutil`]: `_fsutil.py`, its first unit (`unlink_retrying` with
//!   its Windows retry, `bounded_stream_read`);
//! - [`verify_status`] and [`attribution`]: a memory's verification
//!   staleness, and phrase-match attribution of memories to a reply;
//! - [`printable`] and [`membership_lines`]: text made safe to show, and
//!   the lines shown about which project a folder belongs to;
//! - [`origin_remote`]: a remote URL read into the triple `origin`
//!   compares, with `urllib.parse.urlparse` as CPython 3.13.13 has it;
//! - [`credentials`]: the write gate's credential detectors, its first
//!   two units (the vendor-prefixed shapes and the value gate);
//! - [`origin_gitfiles`]: the repository files `origin` reads in-process,
//!   its first three units (the pure decoders; the leaves that read the
//!   machine: a file's prefix, a path relative to the root, the root as
//!   the kernel spells it; and what git dies on in a config, what a config
//!   text says, and the directories a check holds);
//! - [`checkout_token`]: the checkout token, its first three units (the
//!   text, the reads over the file-system trait, and the write);
//! - [`claims`]: what a memory claims, its first unit (the claim record,
//!   the path spellings and the patterns a patch is read by);
//! - [`pysyntax`]: Python source as CPython 3.13.13 reads it, its first
//!   unit: the eval-mode tokenizer `ast.literal_eval` meets, with string
//!   and number literals read;
//! - [`gitfile_rule`]: the rule a `.git` entry is followed by, whole on
//!   POSIX: from a `gitdir:` file's bytes to the judge of an entry and the
//!   walk git's discovery makes.

#![forbid(unsafe_code)]

pub mod attribution;
pub mod capture;
pub mod checkout_token;
pub mod claims;
pub mod config;
pub mod control_tokens;
pub mod credentials;
pub mod datetime;
pub mod durability;
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
pub mod history;
pub mod identity;
pub mod in_force;
pub mod json;
pub mod lexicon;
pub mod location_git;
pub mod location_probe;
pub mod location_reads;
pub mod location_stamps;
pub mod location_view;
pub mod location_witness;
pub mod log;
pub mod machine;
pub mod membership_lines;
pub mod membership_records;
pub mod membership_rows;
pub mod models_text;
pub mod origin_gitfiles;
pub mod origin_history;
pub mod origin_remote;
pub mod printable;
pub mod pyre;
pub mod pysyntax;
pub mod pyvalue;
pub mod records;
pub mod search;
pub mod search_dedup;
pub mod session;
pub mod store;
pub mod toml;
pub mod unicode;
pub mod verify_candidates;
pub mod verify_status;
pub mod witness_questions;
