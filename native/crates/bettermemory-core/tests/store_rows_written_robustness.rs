//! Hostile arguments to the row writers (`store::home`, `store::tables`):
//! every type a caller can pass where the reference names a type, an
//! enormous body, ids that are not ids, and a `homes` or `standing` table
//! altered under the writer. Each call either writes as a whole or returns
//! a typed error with nothing written (the log's length and every table's
//! digest as before); none panics; a further write succeeds afterwards,
//! and the store verifies ok before its tables are altered.

mod common;

use std::path::Path;

use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::store::create;
use bettermemory_core::store::home::{self, Answer, Home};
use bettermemory_core::store::open::{open_for_writing, WritableStore};
use bettermemory_core::store::tables::{EpisodeArgs, Quarantine, Verification};
use bettermemory_core::store::write::WriteError;
use common::created::{fixed, fixture};
use common::opened;
use common::rows::Counting;
use common::stores::scratch;
use common::written::{self, instant, ulid};

const STAMP: &str = "2001-02-03T06:00:00.000001Z";

fn open(c: &Path) -> WritableStore {
    open_for_writing(
        &c.join("memory.sqlite"),
        &c.join("keys"),
        &opened::engine_provider(),
    )
    .expect("open")
}

/// Every table's rows as one text, with the log's length.
fn snapshot(store: &WritableStore) -> String {
    let conn = store.connection();
    let tables: Vec<String> = conn
        .prepare("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE '%fts%' ORDER BY name")
        .expect("tables")
        .query_map([], |r| r.get(0))
        .expect("query")
        .collect::<rusqlite::Result<_>>()
        .expect("names");
    let mut out = String::new();
    for t in tables {
        let mut st = conn
            .prepare(&format!("SELECT * FROM \"{t}\""))
            .expect("select");
        let n = st.column_count();
        let mut rows = st.query([]).expect("rows");
        out.push_str(&t);
        while let Some(r) = rows.next().expect("row") {
            for i in 0..n {
                out.push_str(&format!("{:?}|", r.get_ref(i).expect("value")));
            }
            out.push('\n');
        }
    }
    out
}

fn s(t: &str) -> PyValue {
    PyValue::str(t)
}

fn hostile_values() -> Vec<PyValue> {
    vec![
        PyValue::None,
        PyValue::Bool(true),
        PyValue::Bool(false),
        PyValue::int(0),
        PyValue::int(-7),
        PyValue::int_from_decimal(&"9".repeat(500)).expect("big"),
        PyValue::Float(f64::NAN),
        PyValue::Float(f64::INFINITY),
        PyValue::Float(0.5),
        s(""),
        s("x"),
        PyValue::Str(vec![0xD800, 0x41]),
        PyValue::Str(vec![0]),
        PyValue::DateTime(instant(STAMP)),
        PyValue::Path(bettermemory_core::unicode::cps("a/b")),
        PyValue::Tuple(vec![s("a")]),
        PyValue::List(vec![PyValue::List(vec![])]),
        PyValue::Dict(vec![(s("k"), PyValue::None)]),
    ]
}

/// The call either wrote (the snapshot changed) or failed with nothing
/// written.
fn whole_or_nothing<T: std::fmt::Debug>(
    store: &mut WritableStore,
    label: &str,
    f: impl FnOnce(&mut WritableStore) -> Result<T, WriteError>,
) -> bool {
    let before = snapshot(store);
    match f(store) {
        Ok(_) => true,
        Err(e) => {
            assert!(!e.class().is_empty());
            assert_eq!(before, snapshot(store), "{label}: refused ({e}) but wrote");
            false
        }
    }
}

#[test]
fn hostile_arguments_are_typed_errors_with_nothing_written() {
    let dir = scratch("rows-robust");
    let c = dir.0.join("c");
    create::create(&c, &c.join("keys"), &mut fixed(&fixture())).expect("create");
    let mut store = open(&c);
    let mut p = Counting::at(STAMP, 0);
    let one = ulid(1, "01J");
    store
        .mutate(
            "memory_put",
            &written::memory_payload(&one, "a record", &["global"], &[]),
            Some("r"),
            &mut p,
        )
        .expect("memory");
    let mut wrote = 0;
    let mut refused = 0;
    let mut tally = |ok: bool| if ok { wrote += 1 } else { refused += 1 };
    let values = hostile_values();
    for (i, v) in values.iter().enumerate() {
        let id = format!("01J{:023}", 100 + i);
        for h in [
            Home {
                project: v.clone(),
                checkout: PyValue::None,
                rule: s("r"),
            },
            Home {
                project: s("p"),
                checkout: v.clone(),
                rule: s("r"),
            },
            Home {
                project: s("p"),
                checkout: PyValue::None,
                rule: v.clone(),
            },
        ] {
            tally(whole_or_nothing(&mut store, "home", |st| {
                let mut tx = st.begin(&mut p)?;
                let row = home::home_row(&mut tx, &id, "memory", Some(&h))?;
                if let Some(row) = row {
                    home::put_home(&mut tx, &row, None)?;
                }
                tx.commit()
            }));
        }
        for a in [
            Answer {
                probability: v.clone(),
                source: s("u"),
                answered_by: s("b"),
            },
            Answer {
                probability: PyValue::Float(0.5),
                source: v.clone(),
                answered_by: s("b"),
            },
            Answer {
                probability: PyValue::Float(0.5),
                source: s("u"),
                answered_by: v.clone(),
            },
        ] {
            tally(whole_or_nothing(&mut store, "standing", |st| {
                st.put_standing(&one, &a, None, &mut p)
            }));
        }
        let mut args = EpisodeArgs {
            session_id: s("s"),
            body: s("body"),
            scopes: PyValue::None,
            takeaway: PyValue::None,
            swarm_id: PyValue::None,
            origin: PyValue::None,
        };
        for field in 0..6 {
            let mut a = args.clone();
            match field {
                0 => a.session_id = v.clone(),
                1 => a.body = v.clone(),
                2 => a.scopes = v.clone(),
                3 => a.takeaway = v.clone(),
                4 => a.swarm_id = v.clone(),
                _ => a.origin = v.clone(),
            }
            tally(whole_or_nothing(&mut store, "write_episode", |st| {
                st.write_episode(&a, None, None, &mut p)
            }));
        }
        args.body = s("floor");
        tally(whole_or_nothing(&mut store, "floor", |st| {
            st.write_floor(v, v, None, None, &mut p)
        }));
        tally(whole_or_nothing(&mut store, "conflict", |st| {
            st.put_conflict(v, None, &mut p)
        }));
        let record = PyValue::Dict(vec![
            (s("id"), v.clone()),
            (s("a_id"), s("a")),
            (s("b_id"), v.clone()),
        ]);
        tally(whole_or_nothing(&mut store, "conflict fields", |st| {
            st.put_conflict(&record, None, &mut p)
        }));
        tally(whole_or_nothing(&mut store, "import", |st| {
            st.put_import(v, v, v, v, None, &mut p)
        }));
        let q = Quarantine {
            name: v.clone(),
            reason: v.clone(),
            at: v.clone(),
            detail: v.clone(),
            remote: v.clone(),
            size: v.clone(),
            sha256: v.clone(),
        };
        tally(whole_or_nothing(&mut store, "quarantine", |st| {
            st.put_quarantine(&q, None, &mut p)
        }));
        let ver = Verification {
            memory_id: v.clone(),
            verified_at: instant(STAMP),
            machine_id: v.clone(),
            verified_head: v.clone(),
            verified_paths: vec![v.clone()],
            verified_commits: vec![v.clone()],
            verified_versions: Vec::new(),
            verified_absent_paths: Vec::new(),
            verified_path_targets: Some(vec![(v.clone(), v.clone())]),
            verified_absent_path_targets: None,
        };
        tally(whole_or_nothing(&mut store, "verification", |st| {
            st.put_verification(&ver, None, &mut p)
        }));
    }
    // Ids that are not ids.
    for bad in ["", "\0", "x'); DROP TABLE homes; --", &"z".repeat(100_000)] {
        tally(whole_or_nothing(&mut store, "delete episode", |st| {
            st.delete_episode(bad, None, &mut p)
        }));
        tally(whole_or_nothing(&mut store, "delete conflict", |st| {
            st.delete_conflict(bad, None, &mut p)
        }));
        tally(whole_or_nothing(&mut store, "delete import", |st| {
            st.delete_import(bad, None, &mut p)
        }));
        tally(whole_or_nothing(&mut store, "delete quarantine", |st| {
            st.delete_quarantine(bad, None, &mut p)
        }));
        tally(whole_or_nothing(&mut store, "delete verification", |st| {
            st.delete_verification(bad, Some(bad), None, &mut p)
        }));
        tally(whole_or_nothing(&mut store, "standing", |st| {
            st.put_standing(
                bad,
                &Answer {
                    probability: PyValue::int(1),
                    source: s("u"),
                    answered_by: s("b"),
                },
                None,
                &mut p,
            )
        }));
    }
    // An enormous body: 8 MiB of text.
    let big = EpisodeArgs {
        session_id: s("s"),
        body: PyValue::Str(vec![u32::from('w'); 8 << 20]),
        scopes: PyValue::None,
        takeaway: PyValue::None,
        swarm_id: PyValue::None,
        origin: PyValue::None,
    };
    tally(whole_or_nothing(&mut store, "big", |st| {
        st.write_episode(&big, None, None, &mut p)
    }));
    // The store verifies before its tables are altered.
    store.close().expect("close");
    let (status, _) = written::native_verify(&c, &common::stores::store_id());
    assert_eq!(status, "ok");
    let mut store = open(&c);
    // The tables altered under the writer (the native open refuses such a
    // store afterwards: it is no longer one this build wrote).
    let h = Home {
        project: s("p"),
        checkout: PyValue::None,
        rule: s("r"),
    };
    let fine = Answer {
        probability: PyValue::Float(0.5),
        source: s("u"),
        answered_by: s("b"),
    };
    for alter in [
        "ALTER TABLE homes RENAME COLUMN rule TO rule_was",
        "ALTER TABLE homes RENAME COLUMN rule_was TO rule; ALTER TABLE homes ADD COLUMN extra TEXT NOT NULL DEFAULT 'x'",
        "DROP TABLE homes; CREATE VIEW homes AS SELECT 1 AS record_id",
        "DROP VIEW homes; CREATE TABLE homes (record_id INTEGER)",
        "ALTER TABLE standing DROP COLUMN source",
        "DROP TABLE standing",
    ] {
        store.connection().execute_batch(alter).expect("alter");
        let id = ulid(77, "01J");
        tally(whole_or_nothing(&mut store, alter, |st| {
            let mut tx = st.begin(&mut p)?;
            if let Some(row) = home::home_row(&mut tx, &id, "memory", Some(&h))? {
                home::put_home(&mut tx, &row, None)?;
            }
            tx.commit()
        }));
        tally(whole_or_nothing(&mut store, alter, |st| {
            st.write_episode(
                &EpisodeArgs {
                    session_id: s("s"),
                    body: s("altered"),
                    scopes: PyValue::None,
                    takeaway: PyValue::None,
                    swarm_id: PyValue::None,
                    origin: PyValue::None,
                },
                None,
                Some(&h),
                &mut p,
            )
        }));
        tally(whole_or_nothing(&mut store, alter, |st| st.put_standing(&one, &fine, None, &mut p)));
    }
    eprintln!("{wrote} calls wrote, {refused} refused with nothing written");
    assert!(refused > 0 && wrote > 0);
    // A further write succeeds and the store verifies.
    store
        .delete_quarantine("never", None, &mut p)
        .expect_err("no such row");
    let q = Quarantine {
        name: s("after"),
        reason: s("r"),
        at: s("t"),
        detail: PyValue::None,
        remote: PyValue::None,
        size: PyValue::None,
        sha256: PyValue::None,
    };
    store
        .put_quarantine(&q, None, &mut p)
        .expect("a further write");
    store.close().expect("close");
}

/// The refusals that quote a type's name, with the reference's texts
/// (`store_rows_written.jsonl`'s `type_names` case pins the same).
#[test]
fn a_refusal_names_each_type_as_the_reference() {
    let dir = scratch("rows-types");
    let c = dir.0.join("c");
    create::create(&c, &c.join("keys"), &mut fixed(&fixture())).expect("create");
    let mut store = open(&c);
    let mut p = Counting::at(STAMP, 0);
    let typed = [
        (PyValue::Bool(true), "bool"),
        (PyValue::int(7), "int"),
        (PyValue::Float(1.5), "float"),
        (PyValue::List(vec![PyValue::int(1)]), "list"),
        (PyValue::Tuple(vec![s("a")]), "tuple"),
        (PyValue::Dict(vec![(s("k"), PyValue::int(1))]), "dict"),
        (PyValue::DateTime(instant(STAMP)), "datetime.datetime"),
        (
            PyValue::Path(bettermemory_core::unicode::cps("a/b")),
            "PurePosixPath",
        ),
    ];
    let text = |r: Result<_, WriteError>| match r {
        Err(WriteError::Refused { class, text }) => {
            format!("{class}: {}", text.unwrap_or_default())
        }
        other => format!("{other:?}"),
    };
    let args = |body: PyValue, scopes: PyValue, takeaway: PyValue| EpisodeArgs {
        session_id: s("s"),
        body,
        scopes,
        takeaway,
        swarm_id: PyValue::None,
        origin: PyValue::None,
    };
    for (v, name) in &typed {
        let body = store.write_episode(
            &args(v.clone(), PyValue::None, PyValue::None),
            None,
            None,
            &mut p,
        );
        assert_eq!(
            text(body.map(|_| ())),
            format!("AttributeError: '{name}' object has no attribute 'strip'")
        );
        let taken =
            store.write_episode(&args(s("b"), PyValue::None, v.clone()), None, None, &mut p);
        assert_eq!(
            text(taken.map(|_| ())),
            format!("AttributeError: '{name}' object has no attribute 'strip'")
        );
        if *name != "dict" {
            let rec = store.put_conflict(v, None, &mut p);
            assert_eq!(
                text(rec.map(|_| ())),
                format!("AttributeError: '{name}' object has no attribute 'get'")
            );
        }
        if !matches!(v, PyValue::List(_) | PyValue::Tuple(_) | PyValue::Dict(_)) {
            let scoped =
                store.write_episode(&args(s("b"), v.clone(), PyValue::None), None, None, &mut p);
            assert_eq!(
                text(scoped.map(|_| ())),
                format!("TypeError: '{name}' object is not iterable")
            );
        }
    }
    let none = store.put_conflict(&PyValue::None, None, &mut p);
    assert_eq!(
        text(none.map(|_| ())),
        "AttributeError: 'NoneType' object has no attribute 'get'"
    );
    store.close().expect("close");
}

/// `generate_ulid`'s milliseconds: wrapped at 48 bits where the reference
/// wraps, refused where its `int()` raises.
#[test]
fn a_minted_id_wraps_and_refuses_as_the_reference() {
    use bettermemory_core::store::create::ulid_millis;
    assert_eq!(ulid_millis(1.0).expect("1 s"), 1000);
    // 2**48 ms: the year 10889 wraps to 0, and one ms past it to 1.
    assert_eq!(ulid_millis(281_474_976_710.656).expect("wraps"), 0);
    assert_eq!(ulid_millis(281_474_976_710.657).expect("wraps"), 1);
    // Before 1970: -1 ms & (2**48 - 1).
    assert_eq!(ulid_millis(-0.001).expect("negative"), (1 << 48) - 1);
    assert_eq!(ulid_millis(1e300).expect("huge, finite"), 0);
    let inf = ulid_millis(f64::INFINITY).expect_err("inf");
    assert!(inf.to_string().contains("OverflowError"), "{inf}");
    let nan = ulid_millis(f64::NAN).expect_err("nan");
    assert!(nan.to_string().contains("ValueError"), "{nan}");
}

/// One writer call of the deep-argument test.
type DeepCall = Box<dyn FnMut(&mut WritableStore, &mut Counting) -> Result<(), WriteError>>;

/// A list nested `depth` deep, built without recursion.
fn nested(depth: usize) -> PyValue {
    let mut v = PyValue::List(Vec::new());
    for _ in 1..depth {
        v = PyValue::List(vec![v]);
    }
    v
}

/// Every argument position given a value nested 9,998 deep (the deepest
/// the JSON reader accepts) and 100,000 deep, on a 512 KiB stack: the
/// reference's refusal (established by running it at both depths: the
/// episode fields fail validation, `body` and `takeaway` fail `.strip()`,
/// a probability is out of range, every value that reaches the encoder is
/// RecursionError), nothing written, and the store verifies ok after.
#[test]
fn a_deeply_nested_argument_is_refused_with_nothing_written() {
    std::thread::Builder::new()
        .stack_size(512 * 1024)
        .spawn(deep_arguments)
        .expect("thread")
        .join()
        .expect("no stack overflow");
}

fn deep_arguments() {
    let dir = scratch("rows-deep");
    let c = dir.0.join("c");
    create::create(&c, &c.join("keys"), &mut fixed(&fixture())).expect("create");
    let mut store = open(&c);
    let mut p = Counting::at(STAMP, 0);
    let one = ulid(1, "01J");
    store
        .mutate(
            "memory_put",
            &written::memory_payload(&one, "a record", &["global"], &[]),
            Some("r"),
            &mut p,
        )
        .expect("memory");
    let validation = "ValidationError: 1 validation error for Episode";
    let strip = "AttributeError: 'list' object has no attribute 'strip'";
    let range = "ValueError: a standing answer is a probability from 0 to 1";
    let recursion = "RecursionError";
    let mut checked = 0;
    for depth in [9_998, 100_000] {
        let v = nested(depth);
        let none = PyValue::None;
        let base = EpisodeArgs {
            session_id: s("s"),
            body: s("b"),
            scopes: none.clone(),
            takeaway: none.clone(),
            swarm_id: none.clone(),
            origin: none.clone(),
        };
        let mut calls: Vec<(&str, &str, DeepCall)> = Vec::new();
        for (field, want) in [
            ("session_id", validation),
            ("body", strip),
            ("scopes", validation),
            ("takeaway", strip),
            ("swarm_id", validation),
            ("origin", validation),
        ] {
            let mut a = base.clone();
            match field {
                "session_id" => a.session_id = v.clone(),
                "body" => a.body = v.clone(),
                "scopes" => a.scopes = v.clone(),
                "takeaway" => a.takeaway = v.clone(),
                "swarm_id" => a.swarm_id = v.clone(),
                _ => a.origin = v.clone(),
            }
            calls.push((
                field,
                want,
                Box::new(move |st, p| st.write_episode(&a, None, None, p).map(|_| ())),
            ));
        }
        let (fv, fv2) = (v.clone(), v.clone());
        calls.push((
            "floor session_id",
            validation,
            Box::new(move |st, p| {
                st.write_floor(&fv, &PyValue::None, None, None, p)
                    .map(|_| ())
            }),
        ));
        calls.push((
            "floor origin",
            validation,
            Box::new(move |st, p| st.write_floor(&s("s"), &fv2, None, None, p).map(|_| ())),
        ));
        let h = Home {
            project: s("p"),
            checkout: v.clone(),
            rule: s("r"),
        };
        calls.push((
            "home checkout",
            recursion,
            Box::new(move |st, p| {
                st.write_episode(
                    &EpisodeArgs {
                        session_id: s("s"),
                        body: s("b"),
                        scopes: PyValue::None,
                        takeaway: PyValue::None,
                        swarm_id: PyValue::None,
                        origin: PyValue::None,
                    },
                    None,
                    Some(&h),
                    p,
                )
                .map(|_| ())
            }),
        ));
        for (field, want) in [
            ("probability", range),
            ("source", recursion),
            ("giver", recursion),
        ] {
            let mut a = Answer {
                probability: PyValue::Float(0.5),
                source: s("u"),
                answered_by: s("b"),
            };
            match field {
                "probability" => a.probability = v.clone(),
                "source" => a.source = v.clone(),
                _ => a.answered_by = v.clone(),
            }
            let id = one.clone();
            calls.push((
                field,
                want,
                Box::new(move |st, p| st.put_standing(&id, &a, None, p).map(|_| ())),
            ));
        }
        for field in ["name", "reason", "at", "detail", "remote", "size", "sha256"] {
            let mut q = Quarantine {
                name: s("q"),
                reason: s("r"),
                at: s("t"),
                detail: PyValue::None,
                remote: PyValue::None,
                size: PyValue::None,
                sha256: PyValue::None,
            };
            match field {
                "name" => q.name = v.clone(),
                "reason" => q.reason = v.clone(),
                "at" => q.at = v.clone(),
                "detail" => q.detail = v.clone(),
                "remote" => q.remote = v.clone(),
                "size" => q.size = v.clone(),
                _ => q.sha256 = v.clone(),
            }
            calls.push((
                "quarantine",
                recursion,
                Box::new(move |st, p| st.put_quarantine(&q, None, p).map(|_| ())),
            ));
        }
        for k in 0..4 {
            let mut f = [s("i"), s("h"), s("t"), PyValue::None];
            f[k] = v.clone();
            calls.push((
                "import",
                recursion,
                Box::new(move |st, p| {
                    st.put_import(&f[0], &f[1], &f[2], &f[3], None, p)
                        .map(|_| ())
                }),
            ));
        }
        for record in [
            PyValue::Dict(vec![
                (s("id"), v.clone()),
                (s("a_id"), s("a")),
                (s("b_id"), s("b")),
            ]),
            PyValue::Dict(vec![
                (s("id"), s("c")),
                (s("a_id"), s("a")),
                (s("b_id"), s("b")),
                (s("note"), v.clone()),
            ]),
        ] {
            calls.push((
                "conflict",
                recursion,
                Box::new(move |st, p| st.put_conflict(&record, None, p).map(|_| ())),
            ));
        }
        for k in 0..5 {
            let mut ver = Verification {
                memory_id: s("m"),
                verified_at: instant(STAMP),
                machine_id: PyValue::None,
                verified_head: PyValue::None,
                verified_paths: Vec::new(),
                verified_commits: Vec::new(),
                verified_versions: Vec::new(),
                verified_absent_paths: Vec::new(),
                verified_path_targets: None,
                verified_absent_path_targets: None,
            };
            match k {
                0 => ver.memory_id = v.clone(),
                1 => ver.machine_id = v.clone(),
                2 => ver.verified_head = v.clone(),
                3 => ver.verified_paths = vec![v.clone()],
                _ => ver.verified_path_targets = Some(vec![(s("a"), v.clone())]),
            }
            calls.push((
                "verification",
                recursion,
                Box::new(move |st, p| st.put_verification(&ver, None, p).map(|_| ())),
            ));
        }
        for (label, want, mut call) in calls {
            let before = snapshot(&store);
            let got = match call(&mut store, &mut p) {
                Err(WriteError::Refused { class, text }) => match text {
                    Some(t) if want != recursion => format!("{class}: {t}"),
                    _ => class.to_owned(),
                },
                other => format!("{other:?}"),
            };
            assert_eq!(got, want, "{label} at {depth}");
            assert_eq!(before, snapshot(&store), "{label} at {depth} wrote");
            checked += 1;
        }
    }
    eprintln!("{checked} deep arguments refused with nothing written");
    store.close().expect("close");
    let (status, _) = written::native_verify(&c, &common::stores::store_id());
    assert_eq!(status, "ok");
}

/// A NaN or infinite probability, which the fixture's JSON cannot carry:
/// the reference's text, established by running it.
#[test]
fn a_nan_or_infinite_probability_is_refused_as_the_reference() {
    let dir = scratch("rows-nan");
    let c = dir.0.join("c");
    create::create(&c, &c.join("keys"), &mut fixed(&fixture())).expect("create");
    let mut store = open(&c);
    let mut p = Counting::at(STAMP, 0);
    let one = ulid(1, "01J");
    store
        .mutate(
            "memory_put",
            &written::memory_payload(&one, "r", &["global"], &[]),
            None,
            &mut p,
        )
        .expect("memory");
    for x in [f64::NAN, f64::INFINITY, f64::NEG_INFINITY] {
        let a = Answer {
            probability: PyValue::Float(x),
            source: s("u"),
            answered_by: s("b"),
        };
        match store.put_standing(&one, &a, None, &mut p) {
            Err(WriteError::Refused { class, text }) => {
                assert_eq!(class, "ValueError");
                assert_eq!(
                    text.as_deref(),
                    Some("a standing answer is a probability from 0 to 1")
                );
            }
            other => panic!("{x}: {other:?}"),
        }
    }
    store.close().expect("close");
}
