//! Hostile arguments for `write`, `update` and `put_memory`'s home and
//! standing answer: every argument position given a value nested 9,998 and
//! 100,000 deep, on a 512 KiB thread (the debug build's frames are the
//! largest), enormous bodies and wrong types. Each is a typed refusal with
//! nothing written, or (where the reference accepts it: `list()` of a dict
//! is its keys) written whole; never a panic or an overflow; the store
//! verifies ok after.

mod common;

use std::path::Path;

use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::records::Memory;
use bettermemory_core::store::create;
use bettermemory_core::store::home::{Answer, Home};
use bettermemory_core::store::open::{open_for_writing, WritableStore};
use bettermemory_core::store::writers::{Put, Update, WriteArgs};
use common::created::{fixed, fixture};
use common::opened;
use common::record_calls::{fixed_answer, fixed_home, Minting};
use common::stores::scratch;
use common::written::{self, instant, ulid};

fn fresh(root: &Path) -> WritableStore {
    let c = root.join("c");
    create::create(&c, &c.join("keys"), &mut fixed(&fixture())).expect("create");
    open_for_writing(
        &c.join("memory.sqlite"),
        &c.join("keys"),
        &opened::engine_provider(),
    )
    .expect("open")
}

fn provider() -> Minting<create::Fixed> {
    Minting {
        inner: written::clock(&[instant("2001-02-03T11:00:00.000001Z")]),
        next: 0,
    }
}

fn log_len(store: &WritableStore) -> i64 {
    store
        .connection()
        .query_row("SELECT count(*) FROM log", [], |r| r.get(0))
        .expect("count")
}

/// A list nested `depth` deep around an int, built without recursion.
fn deep(depth: usize, dict: bool) -> PyValue {
    let mut v = PyValue::int(1);
    for _ in 0..depth {
        v = if dict {
            PyValue::Dict(vec![(PyValue::str("k"), v)])
        } else {
            PyValue::List(vec![v])
        };
    }
    v
}

fn record(n: u64) -> Memory {
    let p = written::memory_payload(&ulid(n, "01K"), "a record", &["global"], &[]);
    Memory::from_payload(p.get("memory").expect("memory")).expect("valid")
}

#[derive(Clone, Copy)]
enum Slot {
    Content,
    Scopes,
    Confidence,
    Source,
    Origin,
    Category,
    Claims,
    Links,
    Actor,
    HomeProject,
    HomeCheckout,
    HomeRule,
    Probability,
    AnswerSource,
    AnsweredBy,
}

const SLOTS: [Slot; 15] = [
    Slot::Content,
    Slot::Scopes,
    Slot::Confidence,
    Slot::Source,
    Slot::Origin,
    Slot::Category,
    Slot::Claims,
    Slot::Links,
    Slot::Actor,
    Slot::HomeProject,
    Slot::HomeCheckout,
    Slot::HomeRule,
    Slot::Probability,
    Slot::AnswerSource,
    Slot::AnsweredBy,
];

/// One `write` with `bad` in `slot` and plain values elsewhere: its
/// refusal's class, or `written`.
fn write_with(store: &mut WritableStore, slot: Slot, bad: &PyValue) -> String {
    let content = PyValue::str("a body");
    let scopes = written::payload("[\"global\"]");
    let mut home = fixed_home();
    let mut answer: Answer = fixed_answer();
    let pick = |s: Slot| -> Option<&PyValue> {
        std::mem::discriminant(&s)
            .eq(&std::mem::discriminant(&slot))
            .then_some(bad)
    };
    match slot {
        Slot::HomeProject => home.project = bad.clone(),
        Slot::HomeCheckout => home.checkout = bad.clone(),
        Slot::HomeRule => home.rule = bad.clone(),
        Slot::Probability => answer.probability = bad.clone(),
        Slot::AnswerSource => answer.source = bad.clone(),
        Slot::AnsweredBy => answer.answered_by = bad.clone(),
        _ => {}
    }
    let args = WriteArgs {
        content: pick(Slot::Content).unwrap_or(&content),
        scopes: pick(Slot::Scopes).unwrap_or(&scopes),
        confidence: pick(Slot::Confidence),
        source: pick(Slot::Source),
        origin: pick(Slot::Origin),
        category: pick(Slot::Category),
        claims: pick(Slot::Claims),
        links: pick(Slot::Links),
        actor: pick(Slot::Actor),
        session: Some("robust"),
        home: Some(&home),
        standing: Some(&answer),
    };
    let before = log_len(store);
    match store.write(&mut provider(), args) {
        // `list()` of a dict is its keys: a dict holding a deep value can
        // be a valid list of scopes or claims, as in the reference.
        Ok(done) => {
            assert!(log_len(store) > before);
            assert!(!done.committed.rows.is_empty());
            "written".to_owned()
        }
        Err(e) => {
            assert_eq!(log_len(store), before, "{}", e.class());
            e.class()
        }
    }
}

fn every_slot_deep() {
    let dir = scratch("records-b-robust-deep");
    let mut store = fresh(&dir.0);
    store
        .put_memory(&mut provider(), &record(1), Put::default())
        .expect("put");
    let mut seen = Vec::new();
    for depth in [9_998, 100_000] {
        for dict in [false, true] {
            let bad = deep(depth, dict);
            for slot in SLOTS {
                seen.push(write_with(&mut store, slot, &bad));
            }
            // put_memory's provenance and update's standing answer.
            let before = log_len(&store);
            let put = Put {
                provenance: Some(&bad),
                ..Put::default()
            };
            seen.push(
                store
                    .put_memory(&mut provider(), &record(2), put)
                    .expect_err("provenance")
                    .class(),
            );
            let answer = Answer {
                probability: PyValue::Float(0.5),
                source: bad.clone(),
                answered_by: PyValue::str("a"),
            };
            let update = Update {
                force: true,
                standing: Some(&answer),
                ..Update::default()
            };
            seen.push(
                store
                    .update(&mut provider(), &record(1), update)
                    .expect_err("deep answer")
                    .class(),
            );
            assert_eq!(log_len(&store), before);
        }
    }
    let written = seen.iter().filter(|c| *c == "written").count();
    eprintln!("{} calls, {written} written", seen.len());
    seen.sort();
    seen.dedup();
    eprintln!("outcomes seen: {seen:?}");
    drop(store);
    let (status, _) = written::native_verify(&dir.0.join("c"), &common::stores::store_id());
    assert_eq!(status, "ok");
}

#[test]
fn every_argument_nested_deep_is_refused_on_a_small_stack() {
    std::thread::Builder::new()
        .stack_size(512 * 1024)
        .spawn(every_slot_deep)
        .expect("thread")
        .join()
        .expect("no overflow, no panic");
}

#[test]
fn enormous_bodies_and_wrong_types() {
    let dir = scratch("records-b-robust-types");
    let mut store = fresh(&dir.0);
    let scopes = written::payload("[\"global\"]");
    // A body of 4 MiB of spaces and text is written whole, stripped.
    let big = PyValue::str(&format!("{}x{}", " ".repeat(1 << 21), "y ".repeat(1 << 20)));
    let done = store
        .write(
            &mut provider(),
            WriteArgs {
                content: &big,
                scopes: &scopes,
                confidence: None,
                source: None,
                origin: None,
                category: None,
                claims: None,
                links: None,
                actor: None,
                session: None,
                home: None,
                standing: None,
            },
        )
        .expect("a big body");
    assert_eq!(done.value.fields.body.first(), Some(&u32::from('x')));
    let before = log_len(&store);
    let wrong = [
        PyValue::None,
        PyValue::Bool(true),
        PyValue::Float(f64::NAN),
        PyValue::int(i128::MAX),
        PyValue::str("\u{0}"),
        PyValue::Tuple(vec![]),
        PyValue::Dict(vec![]),
    ];
    let mut classes = Vec::new();
    for v in &wrong {
        for slot in [
            Slot::Content,
            Slot::Scopes,
            Slot::Links,
            Slot::Actor,
            Slot::Probability,
        ] {
            let content = PyValue::str("c");
            let r = store.write(
                &mut provider(),
                WriteArgs {
                    content: if matches!(slot, Slot::Content) {
                        v
                    } else {
                        &content
                    },
                    scopes: if matches!(slot, Slot::Scopes) {
                        v
                    } else {
                        &scopes
                    },
                    confidence: None,
                    source: None,
                    origin: None,
                    category: None,
                    claims: None,
                    links: matches!(slot, Slot::Links).then_some(v),
                    actor: matches!(slot, Slot::Actor).then_some(v),
                    session: None,
                    home: None,
                    standing: None,
                },
            );
            match r {
                Ok(_) => classes.push("written".to_owned()),
                Err(e) => {
                    classes.push(e.class());
                }
            }
        }
    }
    eprintln!("wrong types: {classes:?}");
    // An update of a record that is not there, of one whose stored row was
    // altered, with a home that is not a str.
    let e = store
        .update(&mut provider(), &record(77), Update::default())
        .expect_err("missing");
    assert_eq!(e.class(), "MemoryNotFoundError");
    store
        .connection()
        .execute(
            "UPDATE memories SET created = 'not a time' WHERE id = ?1",
            [&written::ulid(1, "01K")],
        )
        .expect("alter");
    let e = store
        .update(
            &mut provider(),
            &record(1),
            Update {
                force: true,
                ..Update::default()
            },
        )
        .expect_err("an altered row");
    eprintln!("an altered row: {}", e.class());
    let home = Home {
        project: PyValue::int(5),
        checkout: PyValue::None,
        rule: PyValue::str("r"),
    };
    let e = store
        .put_memory(
            &mut provider(),
            &record(3),
            Put {
                home: Some(&home),
                ..Put::default()
            },
        )
        .expect_err("a project that is not a str");
    assert_eq!(e.class(), "ValueError");
    let written_ok = classes.iter().filter(|c| *c == "written").count() as i64;
    assert_eq!(log_len(&store), before + written_ok);
}

/// Counts the wall clock's reads; everything else is `inner`'s.
struct Counting {
    inner: Minting<create::Fixed>,
    wall: usize,
}

impl bettermemory_core::store::create::Provider for Counting {
    fn store_id(&mut self) -> std::io::Result<String> {
        self.inner.store_id()
    }
    fn key(&mut self) -> std::io::Result<bettermemory_core::log::Key> {
        self.inner.key()
    }
    fn membership_store_id(&mut self) -> std::io::Result<String> {
        self.inner.membership_store_id()
    }
    fn now(&mut self) -> std::io::Result<bettermemory_core::datetime::DateTime> {
        self.inner.now()
    }
    fn wall_now(&mut self) -> std::io::Result<bettermemory_core::datetime::DateTime> {
        self.wall += 1;
        self.inner.wall_now()
    }
    fn engine_version(&self) -> String {
        self.inner.engine_version()
    }
    fn ulid(&mut self) -> std::io::Result<String> {
        self.inner.ulid()
    }
}

/// `put_memory` runs `_standing_row` before its transaction begins: with
/// another connection holding the write lock (held until each call has
/// returned; the writer waits no time), a bad answer is the answer's
/// ValueError, and a valid one reads the wall clock once and then meets
/// the lock. `write` reads its own stamp first, so twice.
#[test]
fn the_standing_answer_is_checked_before_the_transaction_starts() {
    let dir = scratch("records-b-robust-locked");
    let mut store = fresh(&dir.0);
    store
        .connection()
        .busy_timeout(std::time::Duration::ZERO)
        .expect("no wait");
    let holder = rusqlite::Connection::open(dir.0.join("c").join("memory.sqlite")).expect("open");
    holder
        .execute_batch("BEGIN IMMEDIATE")
        .expect("hold the lock");
    let bad = Answer {
        probability: PyValue::Float(2.0),
        source: PyValue::str("s"),
        answered_by: PyValue::str("a"),
    };
    let good = fixed_answer();
    let scopes = written::payload("[\"global\"]");
    let content = PyValue::str("c");
    for (answer, is_write, class, reads) in [
        (&bad, false, "ValueError", 0),
        (&good, false, "native:sqlite", 1),
        (&bad, true, "ValueError", 1),
        (&good, true, "native:sqlite", 2),
    ] {
        let mut p = Counting {
            inner: provider(),
            wall: 0,
        };
        let e = if is_write {
            store
                .write(
                    &mut p,
                    WriteArgs {
                        content: &content,
                        scopes: &scopes,
                        confidence: None,
                        source: None,
                        origin: None,
                        category: None,
                        claims: None,
                        links: None,
                        actor: None,
                        session: None,
                        home: None,
                        standing: Some(answer),
                    },
                )
                .expect_err("refused")
        } else {
            let put = Put {
                standing: Some(answer),
                ..Put::default()
            };
            store
                .put_memory(&mut p, &record(5), put)
                .expect_err("refused")
        };
        assert_eq!(e.class(), class, "write {is_write}: {e}");
        if class == "ValueError" {
            assert_eq!(
                e.text().map(|t| bettermemory_core::json::utf8_text(&t)),
                Some("a standing answer is a probability from 0 to 1".to_owned())
            );
        }
        assert_eq!(p.wall, reads, "write {is_write}, {class}");
    }
    holder.execute_batch("ROLLBACK").expect("release");
}
