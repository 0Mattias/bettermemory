//! The store connection's prepared-statement cache as the reference's
//! `sqlite3` connection keeps it (128 statements, least recently used
//! out), seen from outside: a full-record lookup whose statement is still
//! prepared keeps the connection's last SQLite error when a record id
//! fails to encode (the database error, with the UnicodeEncodeError as
//! context); one whose statement was evicted is prepared again, which
//! clears that error, and raises the UnicodeEncodeError alone. So the
//! lookup is evicted exactly when 128 other statements were used since it:
//! every reader that runs on the connection counts, by the distinct SQL
//! texts it sends.
//!
//! Each case here warms the lookup, sends `n` distinct `light_many` (or
//! provenance) shapes, runs one reader, fails a read and asks for an id
//! that does not encode. The signed store is `store_files.jsonl`'s
//! ``intact`` base with its digest brought current.
//!
//! `the_reference_evicts_at_the_same_reads` runs the same plan through the
//! reference's public `Store` on the same file (`BM_REFERENCE_PYTHON`) and
//! compares every answer; `each_reader_counts_its_statements` holds the
//! native side to the counts that run gave, without Python.
//!
//! One reader is known to count differently and is held to both counts
//! ([`Owner::native`]): a pool on a connection opened after another
//! connection of the process drew one. The reference keeps a store's
//! admission inputs for the process (`store_index._DOC_FREQS`) and sends
//! three texts; `PoolReader` keeps them in the reader, reads them again
//! and sends seven, so it drops the lookup after 121 light reads where
//! the reference drops it after 125.
//!
//! The last tests are a pool's reads on stores whose rows are not what a
//! store writes: the `store_created` payload as bytes, a payload that
//! cannot be read, and two records that fail in different ways.

mod common;

use std::path::{Path, PathBuf};

use bettermemory_core::datetime::DateTime;
use bettermemory_core::log::KeyRing;
use bettermemory_core::store::fold::FoldError;
use bettermemory_core::store::open::open_for_writing;
use bettermemory_core::store::pool::{Filters, PoolError, PoolReader};
use bettermemory_core::store::provenance::{ConnSource, Provenance};
use bettermemory_core::store::record_reads::{
    get_many, get_many_python_ids, light_many, ReadError,
};
use bettermemory_core::store::{digest, event_reads, reads, StoreError, StoreFile};
use bettermemory_core::unicode::cps;
use common::created::{reference_python, run_reference};
use common::parity::record;
use common::{opened, provenance, stores};
use rusqlite::Connection;
use serde_json::{json, Value};

/// The statements a `sqlite3` connection keeps prepared, and an owned
/// native store with it.
const CAPACITY: usize = 128;

/// The store a plan reads: the signed ``intact`` base, its digest brought
/// to the log's last record mutation (`_ingest`), in a scratch directory.
struct Fixture {
    _dir: stores::Scratch,
    file: PathBuf,
    keys: PathBuf,
    ring: KeyRing,
    memories: Vec<String>,
    episodes: Vec<String>,
}

fn ids(conn: &Connection, table: &str) -> Vec<String> {
    let mut statement = conn
        .prepare(&format!("SELECT id FROM {table} ORDER BY rowid"))
        .expect("ids");
    let rows = statement
        .query_map([], |row| row.get::<_, String>(0))
        .expect("rows");
    rows.map(|id| id.expect("id")).collect()
}

fn fixture() -> Fixture {
    fixture_with(|_| {})
}

/// The fixture with `edit` applied to the store once its digest is
/// current, by a plain connection (no log row is appended).
fn fixture_with(edit: impl FnOnce(&Connection)) -> Fixture {
    let (dir, _) = stores::case_store("intact");
    let file = dir.0.join("memory.sqlite");
    let keys = dir.0.join("keys");
    let conn = Connection::open(&file).expect("open");
    conn.execute_batch("BEGIN IMMEDIATE").expect("begin");
    digest::ingest(&conn, 0).expect("ingest");
    conn.execute_batch("COMMIT").expect("commit");
    let memories = ids(&conn, "memories");
    let episodes = ids(&conn, "episodes");
    assert_eq!((memories.len(), episodes.len()), (2, 2));
    edit(&conn);
    drop(conn);
    let ring = provenance::ring(&keys, &stores::store_id());
    Fixture {
        _dir: dir,
        file,
        keys,
        ring,
        memories,
        episodes,
    }
}

fn absent(n: u64) -> Vec<String> {
    (0..n).map(|i| i.to_string()).collect()
}

fn refs(ids: &[String]) -> Vec<&str> {
    ids.iter().map(String::as_str).collect()
}

fn kinds(names: &[&str]) -> Vec<Vec<u32>> {
    names.iter().map(|k| cps(k)).collect()
}

/// The telemetry kind and the session the event reads name; the store
/// holds no telemetry row.
const TELEMETRY: &str = "memory_searched";
const SESSION: &str = "sess_0000000000000001";

/// One step of a plan on an open store. Each answer goes to `out`, in the
/// spelling the reference's side of the plan prints.
fn step(
    conn: &Connection,
    reader: &Provenance<'_>,
    pools: Option<(&StoreFile, &PoolReader<'_>)>,
    fixture: &Fixture,
    op: &Value,
    out: &mut Vec<Value>,
) {
    let count = || op[1].as_u64().expect("a count");
    match op[0].as_str().expect("an op") {
        "lookup" => assert!(get_many(conn, &["absent".to_owned()])
            .expect("lookup")
            .is_empty()),
        "lights" => {
            // `count` texts, of `first` rows and of each number above it.
            let first = op.get(2).map_or(1, |f| f.as_i64().expect("a first size"));
            for k in first..first + count() as i64 {
                let rows: Vec<i64> = (5000..5000 + k).collect();
                assert!(light_many(conn, &rows).expect("light").is_empty());
            }
        }
        "provenance" => {
            for k in 1..=count() {
                let ids = absent(k);
                assert!(reader
                    .provenance_for(&refs(&ids))
                    .expect("labels")
                    .is_empty());
            }
        }
        "episodes" => {
            for k in 1..=count() {
                let ids = absent(k);
                let found = reader.episode_provenance(&refs(&ids)).expect("labels");
                assert!(found.is_empty());
            }
        }
        "memory" => {
            let id = fixture.memories[count() as usize].as_str();
            out.push(json!(reader.provenance_for(&[id]).expect("labels")));
        }
        "episode" => {
            let id = fixture.episodes[count() as usize].as_str();
            out.push(json!(reader.episode_provenance(&[id]).expect("labels")));
        }
        "trust" => {
            let id = fixture.memories[count() as usize].as_str();
            let rows = reader.trust_rows(&[id]).expect("trust").expect("a key");
            out.push(Value::Array(
                rows.iter()
                    .map(|(id, row)| json!([id, row.label, row.verified_at]))
                    .collect(),
            ));
        }
        "unaccounted" => out.push(json!(reader.unaccounted_ids().expect("ids"))),
        "counts" => out.push(json!(reader.provenance_counts().expect("counts"))),
        "links" => {
            let id = fixture.memories[count() as usize].as_str();
            let (outbound, inbound) = reader.links_for(id).expect("links");
            out.push(json!([outbound.len(), inbound.len()]));
        }
        "events" => {
            let read = event_reads::iter_events(conn, 999);
            assert!(read.error.is_none());
            out.push(json!(read.items.len()));
        }
        "backward" => {
            let read = event_reads::iter_events_backward(conn);
            assert!(read.error.is_none());
            out.push(json!(read.items.len()));
        }
        "capture" => {
            let read = event_reads::capture_rows(conn, &kinds(&["capture_open"]), 0);
            assert!(read.error.is_none());
            out.push(json!(read.items.len()));
        }
        "session" => {
            let read =
                event_reads::session_events_of_kinds(conn, &cps(SESSION), &kinds(&[TELEMETRY]));
            assert!(read.error.is_none());
            out.push(json!(read.items.len()));
        }
        "since" => {
            let since = DateTime {
                year: 2000,
                month: 1,
                day: 1,
                hour: 0,
                minute: 0,
                second: 0,
                microsecond: 0,
                offset_us: Some(0),
            };
            let read = event_reads::events_since(conn, &since, &|_| None);
            assert!(read.error.is_none());
            out.push(json!(read.items.len()));
        }
        "scope" => {
            let (total, _) = reads::scope_counts(conn, &mut |_, _| true).expect("scopes");
            out.push(json!(total));
        }
        "current" => {
            let (_, pools) = pools.expect("a pool reader");
            out.push(json!(pools.digest_current(digest::fingerprint())));
        }
        "pool" => {
            let (_, pools) = pools.expect("a pool reader");
            let query = op[1].as_str().expect("a query");
            let size = op[2].as_u64().expect("a size") as usize;
            let drawn = pools.resolve_search_pool(
                query,
                &Filters::default(),
                size,
                Some(digest::fingerprint()),
            );
            out.push(match drawn {
                Ok(pool) => json!(pool.ids()),
                Err(error) => json!({"raised": pool_error(&error)}),
            });
        }
        "refold" => {
            let (store, _) = pools.expect("a store");
            let tables = |tables: &[bettermemory_core::store::fold::TableFold]| {
                Value::Array(
                    tables
                        .iter()
                        .map(|t| json!([t.table, t.unaccounted.len(), t.missing.len(), t.rows]))
                        .collect(),
                )
            };
            out.push(match store.refold() {
                Ok(report) => json!([
                    report.status,
                    tables(&report.tables),
                    tables(&report.membership),
                    report.problems.len(),
                ]),
                Err(FoldError::Raised(class)) => json!({"raised": class}),
                Err(other) => json!({"raised": other.to_string()}),
            });
        }
        "folds" => {
            let (store, _) = pools.expect("a store");
            out.push(json!(store.refold().expect("the fold").status));
        }
        "fail" => {
            conn.prepare("SELECT missing FROM statement_cache_missing_table")
                .expect_err("no such table");
        }
        "bad" => {
            let error = get_many_python_ids(conn, &[vec![0xd800]]).expect_err("a lone surrogate");
            out.push(match &error {
                ReadError::Bind { context, .. } => json!([error.class(), context.class()]),
                other => json!([other.class(), null]),
            });
        }
        other => panic!("an unknown op {other}"),
    }
}

/// The class of the exception the reference raises where a pool failed
/// this way.
fn pool_error(error: &PoolError) -> String {
    match error {
        PoolError::Store(StoreError::Py(raised)) => raised.name().to_owned(),
        // `sqlite3` cannot decode the column.
        PoolError::Store(StoreError::NotUtf8 { .. }) => "OperationalError".to_owned(),
        PoolError::Record(refused) => refused.class().to_owned(),
        other => format!("{other:?}"),
    }
}

/// A case's answers: each of its opens is a fresh connection, in turn.
fn native(fixture: &Fixture, case: &Value) -> Value {
    let mut out = Vec::new();
    for ops in case["opens"].as_array().expect("opens") {
        let store = StoreFile::open(&fixture.file).expect("open");
        let reader = Provenance::new(&store, &fixture.ring, true, false).expect("reader");
        let pools = PoolReader::new(&store);
        for op in ops.as_array().expect("ops") {
            step(
                store.connection(),
                &reader,
                Some((&store, &pools)),
                fixture,
                op,
                &mut out,
            );
        }
    }
    Value::Array(out)
}

/// What a kept lookup raises, and an evicted one.
fn kept() -> Value {
    json!(["OperationalError", "UnicodeEncodeError"])
}

fn evicted() -> Value {
    json!(["UnicodeEncodeError", null])
}

/// A reader under test: the steps before the lookup is warmed, the
/// reader's own steps after the `n` shapes, and how many distinct
/// statements those send that the shapes did not (what the reference
/// sends, by its trace).
struct Owner {
    name: &'static str,
    before: Value,
    shapes: &'static str,
    /// The rows of the first light shape; each later one reads one more.
    first: usize,
    after: Value,
    statements: usize,
    /// A connection opened, used and closed before the measured one, in
    /// the same process.
    earlier: Option<Value>,
    /// The count this port sends where it is known not to be the
    /// reference's: the difference is held, on both sides.
    native: Option<usize>,
}

fn owner(name: &'static str, before: Value, after: Value, statements: usize) -> Owner {
    Owner {
        name,
        before,
        shapes: "lights",
        first: 1,
        after,
        statements,
        earlier: None,
        native: None,
    }
}

/// The segment map and both pointers' verdicts read before the lookup is
/// warmed, as a connection that has answered a provenance request holds
/// them.
fn resolved() -> Value {
    json!([["memory", 0], ["episode", 0]])
}

const NO_HIT: &str = "qxjhkmvzknotfound";

fn owners() -> Vec<Owner> {
    let none = || json!([]);
    vec![
        owner("no reader", none(), none(), 0),
        Owner {
            shapes: "provenance",
            ..owner("provenance_for shapes", none(), none(), 0)
        },
        Owner {
            shapes: "episodes",
            ..owner("episode_provenance shapes", none(), none(), 0)
        },
        // One `IN (?)` text of its own each.
        owner("provenance_for", none(), json!([["provenance", 1]]), 1),
        owner("episode_provenance", none(), json!([["episodes", 1]]), 1),
        // A pointer not yet checked on this connection: the `IN (?)`
        // text and the log row's read by MAC.
        owner("memory pointer", resolved(), json!([["memory", 1]]), 2),
        owner("episode pointer", resolved(), json!([["episode", 1]]), 2),
        // A verdict already held: the `IN (?)` text and the stamps'.
        owner("trust_rows", resolved(), json!([["trust", 0]]), 2),
        // Every row's pointer: the scan and the log row's read by MAC.
        owner("unaccounted_ids", resolved(), json!([["unaccounted"]]), 2),
        owner("provenance_counts", resolved(), json!([["counts"]]), 3),
        owner("links", none(), json!([["links", 0]]), 2),
        owner("iter_events", none(), json!([["events"]]), 1),
        owner("iter_events_backward", none(), json!([["backward"]]), 1),
        owner("capture_rows", none(), json!([["capture"]]), 1),
        owner("session_events_of_kinds", none(), json!([["session"]]), 1),
        owner("scope_counts", none(), json!([["scope"]]), 1),
        // The window's floor and the rows from it.
        owner("events_since", none(), json!([["since"]]), 2),
        // A pool whose admission inputs its reader does not hold yet:
        // the memory head, the digest's state, the log row at its mark,
        // the last record mutation and the digest's rows, then the
        // ranking and the stored tokenizer. Nothing matches, so no record
        // is read.
        owner("search pool", none(), draw(NO_HIT), 7),
        // Inputs held from a pool drawn before the lookup was warmed: the
        // memory head, the ranking and the stored tokenizer.
        owner("search pool again", draw(NO_HIT), draw(NO_HIT), 3),
        // One record, read by the `IN (?)` text the first shape sent.
        owner(
            "search pool of one record",
            draw("postgres"),
            draw("postgres"),
            3,
        ),
        // The newest two, read by the second shape's text.
        owner("newest pool", draw(""), draw(""), 3),
        // The same with no shape of one row: the two records are one read
        // by the two-row text, not two reads by a one-row text, which
        // would be a text of its own here.
        Owner {
            first: 2,
            ..owner("newest pool, no one-row shape", draw(""), draw(""), 3)
        },
        // Inputs another connection of the process read. The reference
        // finds them in its process-wide memo and sends three texts; this
        // port reads them again, seven (the module's notes).
        Owner {
            earlier: Some(draw(NO_HIT)),
            native: Some(7),
            ..owner(
                "search pool after another connection's",
                none(),
                draw(NO_HIT),
                3,
            )
        },
    ]
}

/// A pool of two drawn for `query`.
fn draw(query: &str) -> Value {
    json!([["pool", query, 2]])
}

/// A case: its plan, and the last answer (what the lookup raised) on each
/// side.
struct Case {
    plan: Value,
    reference: Value,
    native: Value,
}

/// The cases of one owner: `n` shapes for each `n` around the reference's
/// boundary.
fn cases_of(owner: &Owner) -> Vec<Case> {
    let boundary = CAPACITY - owner.statements;
    let native = CAPACITY - owner.native.unwrap_or(owner.statements);
    let last = |gone: bool| if gone { evicted() } else { kept() };
    (boundary - 3..=boundary + 1)
        .map(|n| {
            let mut ops: Vec<Value> = owner.before.as_array().expect("ops").clone();
            ops.push(json!(["lookup"]));
            ops.push(json!([owner.shapes, n, owner.first]));
            ops.extend(owner.after.as_array().expect("ops").iter().cloned());
            ops.push(json!(["fail"]));
            ops.push(json!(["bad"]));
            let mut opens = Vec::new();
            opens.extend(owner.earlier.clone());
            opens.push(Value::Array(ops));
            Case {
                plan: json!({"name": format!("{} n={n}", owner.name), "opens": opens}),
                reference: last(n >= boundary),
                native: last(n >= native),
            }
        })
        .collect()
}

/// The cases on which this port is known to answer otherwise than the
/// reference: a pool on a connection opened after another one drew a pool,
/// after 122, 123 and 124 light reads.
const KNOWN: [&str; 3] = [
    "search pool after another connection's n=122",
    "search pool after another connection's n=123",
    "search pool after another connection's n=124",
];

#[test]
fn each_reader_counts_its_statements() {
    let fixture = fixture();
    let mut checked = 0;
    let mut differing = Vec::new();
    for owner in owners() {
        for case in cases_of(&owner) {
            let got = native(&fixture, &case.plan);
            let got = got.as_array().expect("answers");
            assert_eq!(
                got.last(),
                Some(&case.native),
                "{}: {got:?}",
                case.plan["name"]
            );
            if case.native != case.reference {
                differing.push(case.plan["name"].as_str().expect("name").to_owned());
            }
            checked += 1;
        }
    }
    assert_eq!(checked, owners().len() * 5);
    assert_eq!(differing, KNOWN);
}

/// The reference's side of a plan: every case on a fresh `Store.open`
/// (read only) per open, its process-wide memos emptied before each case.
/// An open that raises answers for its steps: the reference reads the
/// tokenizer the log names when it opens a store.
const ASK: &str = r#"
import json, sqlite3, sys
from datetime import datetime, timezone
from pathlib import Path

from bettermemory import _caches
from bettermemory.handlers.search import resolve_search_pool
from bettermemory.store import Store

file, keys, plan = Path(sys.argv[1]), Path(sys.argv[2]), json.loads(Path(sys.argv[3]).read_text())
MEMORIES, EPISODES = plan["memories"], plan["episodes"]


def absent(n):
    return [str(i) for i in range(n)]


def step(store, op, out):
    name = op[0]
    if name == "lookup":
        assert store.get_many(["absent"]) == []
    elif name == "lights":
        first = op[2] if len(op) > 2 else 1
        for k in range(first, first + op[1]):
            assert store.light_many(range(5000, 5000 + k)) == []
    elif name == "provenance":
        for k in range(1, op[1] + 1):
            assert store.provenance_for(absent(k)) == {}
    elif name == "episodes":
        for k in range(1, op[1] + 1):
            assert store.episode_provenance(absent(k)) == {}
    elif name == "memory":
        out.append([list(i) for i in store.provenance_for([MEMORIES[op[1]]]).items()])
    elif name == "episode":
        out.append([list(i) for i in store.episode_provenance([EPISODES[op[1]]]).items()])
    elif name == "trust":
        rows = store.trust_rows([MEMORIES[op[1]]])
        out.append([[k, row[0], row[1]] for k, row in rows.items()])
    elif name == "unaccounted":
        out.append(store.unaccounted_ids())
    elif name == "counts":
        out.append([list(i) for i in store.provenance_counts().items()])
    elif name == "links":
        outbound, inbound = store.links_for_many([MEMORIES[op[1]]])[MEMORIES[op[1]]]
        out.append([len(outbound), len(inbound)])
    elif name == "events":
        out.append(len(list(store.iter_events(999))))
    elif name == "backward":
        out.append(len(list(store.iter_events_backward())))
    elif name == "capture":
        out.append(len(list(store.capture_rows(["capture_open"]))))
    elif name == "session":
        out.append(len(list(store.session_events_of_kinds("sess_0000000000000001", ["memory_searched"]))))
    elif name == "since":
        out.append(len(list(store.events_since(datetime(2000, 1, 1, tzinfo=timezone.utc)))))
    elif name == "scope":
        out.append(store.scope_counts(admit=lambda scopes, home: True)[0])
    elif name == "current":
        out.append(store.digest_mark(build=False) is not None)
    elif name == "pool":
        try:
            out.append([m.id for m in resolve_search_pool(store, op[1], size=op[2]).memories])
        except Exception as exc:
            out.append({"raised": type(exc).__name__})
    elif name == "refold":
        def tables(found):
            return [[k, len(v["unaccounted"]), len(v["missing"]), v["rows"]] for k, v in found.items()]
        try:
            report = store.refold()
            out.append([report["status"], tables(report["tables"]), tables(report["membership"]), len(report["problems"])])
        except Exception as exc:
            out.append({"raised": type(exc).__name__})
    elif name == "folds":
        out.append(store.refold()["status"])
    elif name == "fail":
        try:
            store._conn.execute("SELECT missing FROM statement_cache_missing_table")
        except sqlite3.OperationalError:
            pass
        else:
            raise AssertionError("the read did not fail")
    elif name == "bad":
        try:
            store.get_many(["\ud800"])
        except (sqlite3.Error, UnicodeEncodeError) as exc:
            context = exc.__context__
            out.append([type(exc).__name__, None if context is None else type(context).__name__])
        else:
            raise AssertionError("the id encoded")
    else:
        raise ValueError(name)


answers = []
for case in plan["cases"]:
    _caches.clear_all()
    out = []
    for ops in case["opens"]:
        try:
            store = Store.open(file, keys_dir=keys, read_only=True)
        except Exception as exc:
            out.append({"raised": type(exc).__name__})
            continue
        try:
            for op in ops:
                step(store, op, out)
        finally:
            store.close()
    answers.append(out)
print(json.dumps(answers))
"#;

fn reference(python: &Path, fixture: &Fixture, cases: &[Value]) -> Vec<Value> {
    let home = fixture.file.parent().expect("dir").join("home");
    std::fs::create_dir_all(&home).expect("home");
    let plan = home.join("plan.json");
    let doc = json!({
        "memories": fixture.memories,
        "episodes": fixture.episodes,
        "cases": cases,
    });
    std::fs::write(&plan, serde_json::to_string(&doc).expect("plan")).expect("plan file");
    let out = run_reference(python, &home, ASK, &[&fixture.file, &fixture.keys, &plan]);
    assert!(
        out.status.success(),
        "the reference failed: {}",
        String::from_utf8_lossy(&out.stderr)
    );
    serde_json::from_slice(&out.stdout).expect("JSON")
}

/// The reference, or None with `test` recorded as not run.
fn python(test: &str) -> Option<PathBuf> {
    match reference_python() {
        Ok(python) => Some(python),
        Err(why) => {
            eprintln!("NOT RUN {test}: {why}");
            record(test, "reference Python", "i", 0, &[test.to_owned()], &[why]);
            None
        }
    }
}

/// Holds each case to its last answer on each side and, where the two are
/// the same, to the same answers throughout. The names of the cases whose
/// last answers differ, which the caller holds to what is known.
fn compare(python: &Path, fixture: &Fixture, cases: &[Case]) -> Vec<String> {
    let plans: Vec<Value> = cases.iter().map(|case| case.plan.clone()).collect();
    let want = reference(python, fixture, &plans);
    assert_eq!(want.len(), cases.len());
    let mut known = Vec::new();
    let mut differing = Vec::new();
    for (case, want) in cases.iter().zip(&want) {
        let name = case.plan["name"].as_str().expect("name");
        let got = native(fixture, &case.plan);
        let (want, got) = (
            want.as_array().expect("answers"),
            got.as_array().expect("answers"),
        );
        // The count this file names for the reader is the reference's.
        assert_eq!(
            want.last(),
            Some(&case.reference),
            "{name}: the reference answers {want:?}"
        );
        assert_eq!(
            got.last(),
            Some(&case.native),
            "{name}: native answers {got:?}"
        );
        if case.reference != case.native {
            known.push(name.to_owned());
            // Everything before the lookup's answer is still the same.
            assert_eq!(want[..want.len() - 1], got[..got.len() - 1], "{name}");
        } else if got != want {
            differing.push(format!("{name}: reference {want:?} native {got:?}"));
        }
    }
    assert!(
        differing.is_empty(),
        "{} of {} cases differ:\n{}",
        differing.len(),
        cases.len(),
        differing.join("\n")
    );
    known
}

#[test]
fn the_reference_evicts_at_the_same_reads() {
    let Some(python) = python("the_reference_evicts_at_the_same_reads") else {
        return;
    };
    let fixture = fixture();
    let mut cases: Vec<Case> = owners().iter().flat_map(cases_of).collect();
    // The digest this file brought forward is current to the reference.
    cases.push(Case {
        plan: json!({"name": "the digest", "opens": [[["current"]]]}),
        reference: json!(true),
        native: json!(true),
    });
    assert_eq!(compare(&python, &fixture, &cases), KNOWN);
}

#[test]
fn a_borrowed_connection_keeps_the_capacity_its_caller_chose() {
    let fixture = fixture();
    for (capacity, shapes, gone) in [(0, 0, true), (4, 2, false), (4, 3, true)] {
        let conn = Connection::open(&fixture.file).expect("open");
        conn.set_prepared_statement_cache_capacity(capacity);
        let source = ConnSource(&conn);
        let reader = Provenance::new(&source, &fixture.ring, true, false).expect("reader");
        let mut out = Vec::new();
        for op in [
            json!(["lookup"]),
            json!(["provenance", shapes]),
            json!(["scope"]),
            json!(["fail"]),
            json!(["bad"]),
        ] {
            step(&conn, &reader, None, &fixture, &op, &mut out);
        }
        let last = if gone { evicted() } else { kept() };
        assert_eq!(
            out.last(),
            Some(&last),
            "capacity {capacity}, {shapes} shapes"
        );
    }
}

#[test]
fn a_store_opened_for_writing_counts_as_one_opened_to_read() {
    let fixture = fixture();
    // 126 parameter counts and the scopes' text are 127 statements; one
    // more count drops the lookup.
    for (shapes, gone) in [(126, false), (127, true)] {
        let store = open_for_writing(&fixture.file, &fixture.keys, &opened::engine_provider())
            .expect("open");
        let conn = store.connection();
        let source = ConnSource(conn);
        let reader = Provenance::new(&source, &fixture.ring, true, false).expect("reader");
        let mut out = Vec::new();
        for op in [
            json!(["lookup"]),
            json!(["provenance", shapes]),
            json!(["scope"]),
            json!(["fail"]),
            json!(["bad"]),
        ] {
            step(conn, &reader, None, &fixture, &op, &mut out);
        }
        let last = if gone { evicted() } else { kept() };
        assert_eq!(out.last(), Some(&last), "{shapes} parameter counts");
    }
}

/// What the cached readers answer on `conn`, together.
fn answers(conn: &Connection, reader: &Provenance<'_>, fixture: &Fixture) -> Vec<Value> {
    let mut out = Vec::new();
    for op in [
        json!(["memory", 0]),
        json!(["episode", 0]),
        json!(["trust", 1]),
        json!(["unaccounted"]),
        json!(["counts"]),
        json!(["scope"]),
        json!(["events"]),
        json!(["since"]),
    ] {
        step(conn, reader, None, fixture, &op, &mut out);
    }
    let rows = light_many(conn, &[2, 3]).expect("light");
    out.push(json!(rows.len()));
    out.push(json!(format!("{rows:?}")));
    out
}

#[test]
fn a_read_under_an_open_cursor_answers_and_leaves_the_cursor_its_rows() {
    let fixture = fixture();
    let conn = Connection::open(&fixture.file).expect("open");
    let source = ConnSource(&conn);
    let reader = Provenance::new(&source, &fixture.ring, true, false).expect("reader");
    let ids = refs(&fixture.memories);
    let want = reader.provenance_for(&ids).expect("labels");
    assert_eq!(want.len(), 2);
    // The reader's own text, taken from the connection's cache and left on
    // its first row: the reference's `execute` finds its cached statement
    // in use and prepares another.
    let mut held = conn
        .prepare_cached("SELECT id, provenance, log_mac FROM memories WHERE id IN (?,?)")
        .expect("the cached statement");
    let mut rows = held
        .query(rusqlite::params_from_iter(ids.iter()))
        .expect("query");
    let first: String = rows
        .next()
        .expect("step")
        .expect("a row")
        .get(0)
        .expect("id");
    assert_eq!(reader.provenance_for(&ids).expect("labels"), want);
    assert_eq!(reader.provenance_for(&ids).expect("labels"), want);
    let second: String = rows
        .next()
        .expect("step")
        .expect("a row")
        .get(0)
        .expect("id");
    assert_ne!(first, second);
    assert!(rows.next().expect("step").is_none());
}

#[test]
fn a_cached_read_follows_its_connection_through_a_transaction() {
    let fixture = fixture();
    let conn = Connection::open(&fixture.file).expect("open");
    let source = ConnSource(&conn);
    let reader = Provenance::new(&source, &fixture.ring, true, false).expect("reader");
    let id = fixture.memories[0].as_str();
    let before = answers(&conn, &reader, &fixture);
    assert_eq!(before[0], json!([[id, "local"]]));
    conn.execute_batch("BEGIN IMMEDIATE").expect("begin");
    conn.execute(
        "UPDATE memories SET provenance = 'imported' WHERE id = ?",
        [id],
    )
    .expect("update");
    let inside = answers(&conn, &reader, &fixture);
    assert_eq!(inside[0], json!([[id, "imported"]]));
    assert_ne!(inside[4], before[4], "the counts by label");
    assert!(!conn.is_autocommit());
    conn.execute_batch("ROLLBACK").expect("rollback");
    assert_eq!(answers(&conn, &reader, &fixture), before);
}

#[test]
fn a_cached_read_answers_the_same_after_the_schema_changes() {
    let fixture = fixture();
    let conn = Connection::open(&fixture.file).expect("open");
    let source = ConnSource(&conn);
    let reader = Provenance::new(&source, &fixture.ring, true, false).expect("reader");
    let before = answers(&conn, &reader, &fixture);
    // Every statement the readers left prepared is compiled again when it
    // is next stepped.
    conn.execute_batch(
        "BEGIN IMMEDIATE;
         ALTER TABLE memories ADD COLUMN future_field TEXT;
         ALTER TABLE episodes ADD COLUMN future_field TEXT;
         ALTER TABLE verifications ADD COLUMN future_field TEXT;
         ALTER TABLE log ADD COLUMN future_field TEXT;",
    )
    .expect("alter");
    assert_eq!(answers(&conn, &reader, &fixture), before);
    conn.execute_batch("ROLLBACK").expect("rollback");
    assert_eq!(answers(&conn, &reader, &fixture), before);
}

/// The `store_created` row's payload, which names the store's tokenizer,
/// set to `payload`.
fn set_created_payload(conn: &Connection, payload: &dyn rusqlite::ToSql) {
    let changed = conn
        .execute(
            "UPDATE log SET payload = ? WHERE kind = 'store_created'",
            [payload],
        )
        .expect("update");
    assert_eq!(changed, 1);
}

/// An edit of a store, by name, and the class of what reading its stored
/// tokenizer then raises.
type Unreadable = (&'static str, fn(&Connection), &'static str);

/// Stores whose stored tokenizer cannot be read: a payload nested past any
/// parser's depth, and one whose text is not UTF-8.
fn unreadable_stamps() -> [Unreadable; 2] {
    [
        (
            "a payload nested 100,000 deep",
            |conn| {
                let deep = format!("{}{}", "[".repeat(100_000), "]".repeat(100_000));
                set_created_payload(conn, &deep);
            },
            "RecursionError",
        ),
        (
            "a payload that is not UTF-8",
            |conn| {
                conn.execute_batch(
                    "UPDATE log SET payload = CAST(x'ff' AS TEXT) WHERE kind = 'store_created'",
                )
                .expect("update");
            },
            "OperationalError",
        ),
    ]
}

/// A record the model refuses: its confidence is none of the three.
const BAD_CONFIDENCE: &str = "UPDATE memories SET confidence = 'bogus' WHERE rowid = 2";

/// Every pool on a store whose stored tokenizer cannot be read: one that
/// reads no record, and one whose only record the model refuses.
fn unreadable_cases(name: &str, class: &str) -> Vec<Case> {
    let raised = json!({"raised": class});
    [NO_HIT, "postgres", ""]
        .iter()
        .map(|query| Case {
            plan: json!({
                "name": format!("{name}: a pool for {query:?}"),
                "opens": [[["pool", query, 2]]],
            }),
            reference: raised.clone(),
            native: raised.clone(),
        })
        .collect()
}

fn unreadable_fixture(edit: fn(&Connection)) -> Fixture {
    fixture_with(|conn| {
        edit(conn);
        conn.execute_batch(BAD_CONFIDENCE).expect("update");
    })
}

#[test]
fn a_pool_raises_what_reading_the_stored_tokenizer_raises_before_its_records() {
    for (name, edit, class) in unreadable_stamps() {
        let fixture = unreadable_fixture(edit);
        for case in unreadable_cases(name, class) {
            // The stamp's error, for a pool with no record too, and not
            // the refused record's ValidationError, which comes after it.
            let got = native(&fixture, &case.plan);
            assert_eq!(got, json!([case.native]), "{}", case.plan["name"]);
        }
    }
}

#[test]
fn the_reference_raises_the_same_for_a_stored_tokenizer_it_cannot_read() {
    // The reference reads the stamp when it opens the store, so its open
    // raises what this port's pool does (`StoreFile::open` does not read
    // it): the same class, one step earlier.
    let Some(python) =
        python("the_reference_raises_the_same_for_a_stored_tokenizer_it_cannot_read")
    else {
        return;
    };
    for (name, edit, class) in unreadable_stamps() {
        let fixture = unreadable_fixture(edit);
        let known = compare(&python, &fixture, &unreadable_cases(name, class));
        assert!(known.is_empty(), "{name}: {known:?}");
    }
}

/// The `store_created` row's payload as the store wrote it.
fn created_payload(conn: &Connection) -> String {
    conn.query_row(
        "SELECT payload FROM log WHERE kind = 'store_created'",
        [],
        |row| row.get(0),
    )
    .expect("the payload")
}

/// A pool drawn again after `n` light reads, around the count at which
/// `statements` texts drop the lookup; then a pool with a record, the
/// newest pool and the fold, answered in full.
fn stamp_cases(name: &str, statements: usize) -> Vec<Case> {
    let mut cases = cases_of(&owner(
        "search pool again",
        draw(NO_HIT),
        draw(NO_HIT),
        statements,
    ));
    for case in &mut cases {
        case.plan["name"] = json!(format!(
            "{name}: {}",
            case.plan["name"].as_str().expect("name")
        ));
    }
    cases.push(Case {
        plan: json!({
            "name": format!("{name}: pools and the fold"),
            "opens": [[["pool", "postgres", 2], ["pool", "", 2], ["refold"], ["folds"]]],
        }),
        reference: json!("ok"),
        native: json!("ok"),
    });
    cases
}

/// A spelling of the payload in bytes, by name, and the texts a pool drawn
/// again then sends.
type InBytes = (&'static str, fn(&str) -> Vec<u8>, usize);

/// The `store_created` payload as bytes, as `json.loads` reads bytes
/// (`_load_json`, `store_rows.py:98-105`): `(name, the bytes of a payload,
/// the texts a pool drawn again sends)`. A payload that names the
/// tokenizer is one read of the log; one that does not decode names none,
/// and meta's copy is read as well.
fn blob_payloads() -> [InBytes; 7] {
    fn utf16(text: &str, big: bool) -> Vec<u8> {
        text.encode_utf16()
            .flat_map(|unit| {
                if big {
                    unit.to_be_bytes()
                } else {
                    unit.to_le_bytes()
                }
            })
            .collect()
    }
    [
        ("UTF-8 bytes", |text| text.as_bytes().to_vec(), 3),
        (
            "UTF-8 bytes after a BOM",
            |text| [b"\xef\xbb\xbf", text.as_bytes()].concat(),
            3,
        ),
        (
            "UTF-16 bytes after a BOM",
            |text| [&b"\xff\xfe"[..], &utf16(text, false)].concat(),
            3,
        ),
        (
            "UTF-16 bytes, big end first, no BOM",
            |text| utf16(text, true),
            3,
        ),
        (
            "UTF-32 bytes, little end first, no BOM",
            |text| {
                text.chars()
                    .flat_map(|c| (c as u32).to_le_bytes())
                    .collect()
            },
            3,
        ),
        ("bytes that are not UTF-8", |_| vec![0xff], 4),
        ("bytes that are not JSON", |_| b"{".to_vec(), 4),
    ]
}

fn blob_fixture(bytes: fn(&str) -> Vec<u8>) -> Fixture {
    fixture_with(|conn| {
        let payload = bytes(&created_payload(conn));
        set_created_payload(conn, &payload);
    })
}

/// The last answer of each case, and for the answered case the whole of
/// it: a pool of the record, the newest pool, the fold's status.
fn stamp_answers(fixture: &Fixture, cases: &[Case]) {
    for case in cases {
        let got = native(fixture, &case.plan);
        let got = got.as_array().expect("answers");
        let name = case.plan["name"].as_str().expect("name");
        assert_eq!(got.last(), Some(&case.native), "{name}: {got:?}");
        if name.ends_with("pools and the fold") {
            // The record the query matches, then the two by newest first.
            let (first, second) = (&fixture.memories[0], &fixture.memories[1]);
            assert_eq!(got[0], json!([first]), "{name}");
            assert_eq!(got[1], json!([second, first]), "{name}");
            assert_eq!(got[2][0], json!("ok"), "{name}: {}", got[2]);
        }
    }
}

#[test]
fn a_pool_and_the_fold_read_a_stored_tokenizer_in_bytes() {
    for (name, bytes, statements) in blob_payloads() {
        let fixture = blob_fixture(bytes);
        stamp_answers(&fixture, &stamp_cases(name, statements));
    }
}

#[test]
fn the_reference_reads_a_stored_tokenizer_in_bytes_the_same() {
    let Some(python) = python("the_reference_reads_a_stored_tokenizer_in_bytes_the_same") else {
        return;
    };
    for (name, bytes, statements) in blob_payloads() {
        let fixture = blob_fixture(bytes);
        let known = compare(&python, &fixture, &stamp_cases(name, statements));
        assert!(known.is_empty(), "{name}: {known:?}");
    }
}

/// Two records that fail differently, in row order: `(name, the edits,
/// what the newest pool raises)`. A cursor's rows are decoded and
/// converted one at a time, so the first row's failure is the one raised.
fn failing_rows() -> [(&'static str, &'static str, &'static str); 2] {
    [
        (
            "a refused record before one that is not UTF-8",
            "UPDATE memories SET confidence = 'bogus' WHERE rowid = 2;
             UPDATE memories SET body = CAST(x'ff41' AS TEXT) WHERE rowid = 3;",
            "ValidationError",
        ),
        (
            "a record that is not UTF-8 before a refused one",
            "UPDATE memories SET body = CAST(x'ff41' AS TEXT) WHERE rowid = 2;
             UPDATE memories SET confidence = 'bogus' WHERE rowid = 3;",
            "OperationalError",
        ),
    ]
}

fn failing_rows_cases(name: &str, class: &str) -> Vec<Case> {
    let raised = json!({"raised": class});
    vec![Case {
        plan: json!({"name": format!("{name}: the newest pool"), "opens": [[["pool", "", 2]]]}),
        reference: raised.clone(),
        native: raised,
    }]
}

#[test]
fn a_pool_raises_for_the_first_row_that_fails() {
    for (name, edits, class) in failing_rows() {
        let fixture = fixture_with(|conn| conn.execute_batch(edits).expect("edits"));
        for case in failing_rows_cases(name, class) {
            let got = native(&fixture, &case.plan);
            assert_eq!(got, json!([case.native]), "{}", case.plan["name"]);
        }
    }
}

#[test]
fn the_reference_raises_for_the_first_row_that_fails_too() {
    let Some(python) = python("the_reference_raises_for_the_first_row_that_fails_too") else {
        return;
    };
    for (name, edits, class) in failing_rows() {
        let fixture = fixture_with(|conn| conn.execute_batch(edits).expect("edits"));
        let known = compare(&python, &fixture, &failing_rows_cases(name, class));
        assert!(known.is_empty(), "{name}: {known:?}");
    }
}
