//! Hostile inputs to the record readers: enormous strings, deep JSON in
//! the JSON columns and in payload fields, NaN and infinities, integers
//! past i64 and u64, bytes that are not UTF-8, and every column holding
//! every kind of value. Each read ends in a record or an error, never a
//! panic, and nothing is walked recursively (the deep cases run on a
//! thread with a small stack).

use bettermemory_core::json;
use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::records::rows::{self, Row};
use bettermemory_core::records::{
    Actor, Episode, Memory, MemoryLink, Origin, RecordError, Tombstone,
};
use bettermemory_core::store::SqlValue;
use bettermemory_core::unicode;

const ULID: &str = "01ARZ3NDEKTSV4RRFFQ69G5FAV";

fn deep_list(n: usize) -> PyValue {
    let mut v = PyValue::List(Vec::new());
    for _ in 0..n {
        v = PyValue::List(vec![v]);
    }
    v
}

fn deep_dict(n: usize) -> PyValue {
    let mut v = PyValue::None;
    for _ in 0..n {
        v = PyValue::Dict(vec![(PyValue::str("cwd"), v)]);
    }
    v
}

fn base() -> Vec<(&'static str, PyValue)> {
    vec![
        ("id", PyValue::str(ULID)),
        ("created", PyValue::str("2026-01-02T03:04:05Z")),
        ("updated", PyValue::str("2026-01-02T03:04:05Z")),
        ("scopes", PyValue::List(vec![PyValue::str("global")])),
        ("confidence", PyValue::str("high")),
        ("source", PyValue::str("inferred")),
        ("body", PyValue::str("b")),
        ("session_id", PyValue::str("s")),
        ("removed", PyValue::str("2026-01-02T03:04:05Z")),
        ("removed_reason", PyValue::str("r")),
        ("type", PyValue::str("extends")),
        ("target_id", PyValue::str(ULID)),
    ]
}

/// The base with `field` set to `value`, moved in (a deep value is never
/// cloned: `Clone` would recurse).
fn with(field: &str, value: PyValue) -> PyValue {
    let mut items: Vec<(PyValue, PyValue)> = base()
        .into_iter()
        .filter(|(k, _)| *k != field)
        .map(|(k, v)| (PyValue::str(k), v))
        .collect();
    items.push((PyValue::str(field), value));
    PyValue::Dict(items)
}

fn every_model(data: &PyValue) {
    let _ = Memory::from_payload(data).map(|m| (m.payload_text(), m.row()));
    let _ = Tombstone::from_payload(data).map(|m| (m.payload_text(), m.row()));
    let _ = Episode::from_payload(data).map(|m| (m.payload_text(), m.row()));
    let _ = Origin::from_value(data);
    let _ = Actor::from_value(data);
    let _ = MemoryLink::from_value(data);
}

const FIELDS: [&str; 30] = [
    "id",
    "created",
    "updated",
    "scopes",
    "confidence",
    "source",
    "body",
    "origin",
    "actor",
    "last_verified_at",
    "category",
    "verified_paths",
    "verified_commits",
    "verified_versions",
    "verified_absent_paths",
    "verified_path_targets",
    "verified_absent_path_targets",
    "claims",
    "verified_head",
    "links",
    "corroborations",
    "last_corroborated",
    "removed",
    "removed_reason",
    "removed_session",
    "session_id",
    "takeaway",
    "is_floor",
    "swarm_id",
    "type",
];

const HOSTILE: usize = 22;

fn hostile_value(i: usize) -> PyValue {
    let huge = || "9".repeat(5000);
    match i {
        0 => PyValue::Float(f64::NAN),
        1 => PyValue::Float(f64::INFINITY),
        2 => PyValue::Float(f64::NEG_INFINITY),
        3 => PyValue::Float(f64::MAX),
        4 => PyValue::Float(f64::MIN_POSITIVE),
        5 => PyValue::Int(huge()),
        6 => PyValue::Int(format!("-{}", huge())),
        7 => PyValue::Int("18446744073709551616".to_owned()),
        8 => PyValue::Int("9223372036854775808".to_owned()),
        9 => PyValue::Int("-9223372036854775809".to_owned()),
        10 => PyValue::str(&huge()),
        11 => PyValue::str(&format!("{}.0", huge())),
        12 => PyValue::str(&"x".repeat(1 << 20)),
        13 => PyValue::Str(vec![0xD800; 1000]),
        14 => PyValue::str(&"2026-01-02T03:04:05.".repeat(1000)),
        15 => PyValue::List(vec![PyValue::str("global"); 20_000]),
        16 => PyValue::List(vec![PyValue::Float(f64::NAN); 1000]),
        17 => PyValue::Dict(
            (0..10_000)
                .map(|i| (PyValue::str(&format!("k{i}")), PyValue::None))
                .collect(),
        ),
        18 => deep_list(50_000),
        19 => deep_dict(50_000),
        20 => PyValue::List(vec![deep_dict(50_000)]),
        21 => PyValue::Dict(vec![(PyValue::str("a"), deep_list(50_000))]),
        _ => unreachable!(),
    }
}

#[test]
fn hostile_payloads_end_in_a_record_or_an_error() {
    std::thread::Builder::new()
        .stack_size(256 * 1024)
        .spawn(|| {
            for i in 0..HOSTILE {
                every_model(&hostile_value(i));
                for field in FIELDS {
                    every_model(&with(field, hostile_value(i)));
                }
            }
        })
        .expect("thread")
        .join()
        .expect("no panic");
}

#[test]
fn a_huge_body_round_trips() {
    let body = "\u{e9}".repeat(8 << 20);
    let m = Memory::from_payload(&with("body", PyValue::str(&body))).expect("valid");
    assert_eq!(
        unicode::to_string(&m.fields.body).as_deref(),
        Some(body.as_str())
    );
    assert!(m.payload_text().expect("payload").contains(&body));
}

fn row_values() -> Vec<SqlValue> {
    let deep = "[".repeat(200_000) + &"]".repeat(200_000);
    vec![
        SqlValue::Null,
        SqlValue::Integer(i64::MIN),
        SqlValue::Integer(i64::MAX),
        SqlValue::Real(f64::NAN),
        SqlValue::Real(f64::INFINITY),
        SqlValue::Real(-0.0),
        SqlValue::Text(String::new()),
        SqlValue::Text("x".repeat(1 << 20)),
        SqlValue::Text(deep.clone()),
        SqlValue::Text(format!("{{\"memory\": {deep}}}")),
        SqlValue::Text("[".repeat(9998) + &"]".repeat(9998)),
        SqlValue::Text("9".repeat(5000)),
        SqlValue::Text("NaN".to_owned()),
        SqlValue::Text("[Infinity, -Infinity, NaN]".to_owned()),
        SqlValue::Text(
            "{\"memory\": {\"id\": null, \"verified_path_targets\": {\"a\": [[[[1]]]]}}}"
                .to_owned(),
        ),
        SqlValue::Blob(vec![0xFF; 1 << 16]),
        SqlValue::Blob(vec![0xED, 0xA0, 0x80]),
        SqlValue::Blob(vec![0, 0, 0xFE, 0xFF, 0, 0x11, 0, 0]),
        SqlValue::Blob(vec![0xFF, 0xFE, 0x00, 0xD8]),
        SqlValue::Blob(deep.clone().into_bytes()),
        SqlValue::Blob(Vec::new()),
    ]
}

const COLUMNS: [&str; 28] = [
    "id",
    "created",
    "updated",
    "scopes_json",
    "confidence",
    "source",
    "body",
    "origin_json",
    "actor_json",
    "last_verified_at",
    "category",
    "verified_paths_json",
    "verified_commits_json",
    "verified_versions_json",
    "verified_absent_paths_json",
    "claims_json",
    "verified_head",
    "links_json",
    "corroborations",
    "last_corroborated",
    "removed",
    "removed_reason",
    "removed_session",
    "session_id",
    "takeaway",
    "is_floor",
    "swarm_id",
    "attestation_payload",
];

fn base_row() -> Row {
    let text = |t: &str| SqlValue::Text(t.to_owned());
    Row {
        columns: vec![
            ("id".into(), text(ULID)),
            ("created".into(), text("2026-01-02T03:04:05+00:00")),
            ("updated".into(), text("2026-01-02T03:04:05+00:00")),
            ("scopes_json".into(), text("[\"global\"]")),
            ("confidence".into(), text("high")),
            ("source".into(), text("inferred")),
            ("body".into(), text("b")),
            ("origin_json".into(), SqlValue::Null),
            ("actor_json".into(), SqlValue::Null),
            ("last_verified_at".into(), SqlValue::Null),
            ("category".into(), SqlValue::Null),
            ("verified_paths_json".into(), text("[]")),
            ("verified_commits_json".into(), text("[]")),
            ("verified_versions_json".into(), text("[]")),
            ("verified_absent_paths_json".into(), text("[]")),
            ("claims_json".into(), text("[]")),
            ("verified_head".into(), SqlValue::Null),
            ("links_json".into(), text("[]")),
            ("corroborations".into(), SqlValue::Integer(0)),
            ("last_corroborated".into(), SqlValue::Null),
            ("removed".into(), text("2026-01-02T03:04:05+00:00")),
            ("removed_reason".into(), text("r")),
            ("removed_session".into(), SqlValue::Null),
            ("session_id".into(), text("s")),
            ("takeaway".into(), SqlValue::Null),
            ("is_floor".into(), SqlValue::Integer(0)),
            ("swarm_id".into(), SqlValue::Null),
            ("attestation_payload".into(), SqlValue::Null),
        ],
    }
}

#[test]
fn hostile_rows_end_in_a_record_or_an_error() {
    assert!(rows::memory(&base_row()).is_ok());
    assert!(rows::tombstone(&base_row()).is_ok());
    assert!(rows::episode(&base_row()).is_ok());
    std::thread::Builder::new()
        .stack_size(256 * 1024)
        .spawn(|| {
            let values = row_values();
            for column in COLUMNS {
                for value in &values {
                    let mut row = base_row();
                    for (k, v) in &mut row.columns {
                        if k == column {
                            *v = value.clone();
                        }
                    }
                    for read in [
                        |r: &Row| {
                            rows::memory(r).map(|m| (m.payload_text().is_ok(), m.row().is_ok()))
                        },
                        |r: &Row| {
                            rows::tombstone(r).map(|m| (m.payload_text().is_ok(), m.row().is_ok()))
                        },
                        |r: &Row| {
                            rows::episode(r).map(|m| (m.payload_text().is_ok(), m.row().is_ok()))
                        },
                    ] {
                        match read(&row) {
                            Ok(_)
                            | Err(RecordError::Raised(_))
                            | Err(RecordError::Validation { .. }) => {}
                        }
                    }
                }
            }
        })
        .expect("thread")
        .join()
        .expect("no panic");
}

#[test]
fn deep_json_columns_are_recursion_errors_without_recursing() {
    let mut row = base_row();
    for (k, v) in &mut row.columns {
        if k == "scopes_json" {
            *v = SqlValue::Text("[".repeat(10_000) + &"]".repeat(10_000));
        }
    }
    assert_eq!(
        rows::memory(&row),
        Err(RecordError::Raised("RecursionError"))
    );
    let parsed = json::loads(&unicode::cps(&("[".repeat(9998) + &"]".repeat(9998))));
    assert!(parsed.is_ok());
}

fn sources_of(n: usize) -> PyValue {
    PyValue::Dict(
        (0..n)
            .map(|i| (PyValue::str(&format!("k{i}")), PyValue::str("v")))
            .collect(),
    )
}

fn time_actor(n: usize) -> std::time::Duration {
    let data = PyValue::Dict(vec![(PyValue::str("sources"), sources_of(n))]);
    let start = std::time::Instant::now();
    let actor = Actor::from_value(&data).expect("valid");
    assert_eq!(actor.sources.len(), n);
    start.elapsed()
}

/// A dict field validates in linear time: 100,000 keys take at most a
/// small multiple of ten times what 10,000 take.
#[test]
fn dict_fields_validate_in_linear_time() {
    let small = (0..3).map(|_| time_actor(10_000)).min().expect("timed");
    let large = (0..3).map(|_| time_actor(100_000)).min().expect("timed");
    let bound = small * 10 * 4 + std::time::Duration::from_millis(20);
    assert!(large <= bound, "100k keys {large:?}, 10k keys {small:?}");
}

/// The record keeps keys as given (last wins in place for an exact
/// repeat); the dump rebuilds them, so keys that meet only after the lossy
/// rebuild of a surrogate collapse there, as the reference's dump does.
#[test]
fn dict_keys_rebuild_in_the_dump_only() {
    let fffd3: Vec<u32> = vec![0x61, 0xFFFD, 0xFFFD, 0xFFFD];
    let data = PyValue::Dict(vec![(
        PyValue::str("sources"),
        PyValue::Dict(vec![
            (PyValue::Str(vec![0x61, 0xDFFF]), PyValue::str("first")),
            (PyValue::str("b"), PyValue::str("b")),
            (PyValue::Str(fffd3.clone()), PyValue::str("last")),
        ]),
    )]);
    let actor = Actor::from_value(&data).expect("valid");
    assert_eq!(actor.sources.len(), 3);
    assert_eq!(
        actor.dump().get("sources"),
        Some(&PyValue::Dict(vec![
            (PyValue::Str(fffd3), PyValue::str("last")),
            (PyValue::str("b"), PyValue::str("b")),
        ]))
    );
}
