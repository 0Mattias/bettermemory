//! Cases a plausible wrong port of `history`'s third unit answers
//! differently from the reference, each pinned against the reference's
//! answer (read off CPython 3.13.13).

use std::collections::HashMap;

use bettermemory_core::history_rows as hr;
use bettermemory_core::json;
use bettermemory_core::log::LogRow;
use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::unicode::cps;

fn load(text: &str) -> PyValue {
    json::loads(&cps(text)).expect("json")
}

fn row(seq: i64, kind: &str) -> LogRow {
    LogRow {
        seq,
        ts: "t".into(),
        session: None,
        kind: kind.into(),
        payload: String::new(),
        prev_mac: String::new(),
        mac: String::new(),
    }
}

fn changed(a: &str, b: &str) -> Vec<String> {
    hr::changed(&load(a), &load(b))
        .iter()
        .map(|n| n.iter().map(|&c| char::from_u32(c).unwrap()).collect())
        .collect()
}

#[test]
fn numbers_compare_across_types() {
    assert!(changed(r#"{"a":1,"b":0,"c":-0.0}"#, r#"{"a":true,"b":false,"c":0}"#).is_empty());
    assert_eq!(changed(r#"{"a":0}"#, r#"{"a":""}"#), ["a"]);
    // 2**53 + 1 is not the float it rounds to.
    assert_eq!(
        changed(r#"{"a":9007199254740993}"#, r#"{"a":9007199254740992.0}"#),
        ["a"]
    );
    assert_eq!(
        changed(r#"{"a":[1]}"#, r#"{"a":[1.0]}"#),
        Vec::<String>::new()
    );
}

#[test]
fn nan_differs_at_the_top_only() {
    assert_eq!(changed(r#"{"a":NaN}"#, r#"{"a":NaN}"#), ["a"]);
    assert!(changed(
        r#"{"a":[NaN],"b":{"k":NaN}}"#,
        r#"{"a":[NaN],"b":{"k":NaN}}"#
    )
    .is_empty());
}

#[test]
fn dicts_by_key_lists_by_order() {
    assert!(changed(r#"{"a":{"x":1,"y":2}}"#, r#"{"a":{"y":2,"x":1}}"#).is_empty());
    assert_eq!(changed(r#"{"a":[1,2]}"#, r#"{"a":[2,1]}"#), ["a"]);
    assert_eq!(changed(r#"{"a":1,"b":1}"#, r#"{"b":2,"c":3}"#), ["b"]);
    assert!(changed(r#"{"id":1,"updated":1}"#, r#"{"id":2,"updated":2}"#).is_empty());
    assert_eq!(
        changed(r#"{"z":1,"Z":1,"é":1}"#, r#"{"z":2,"Z":2,"é":2}"#),
        ["Z", "z", "é"]
    );
}

#[test]
fn event_detail_drops_only_four_empties() {
    let d = hr::event_detail(&load(
        r#"{"status":0,"reason":false,"fields":[[]],"door":"","attribution":[],"triggered_from":{}}"#,
    ));
    let keys: Vec<PyValue> = d.into_iter().map(|(k, _)| k).collect();
    assert_eq!(
        keys,
        [
            PyValue::str("status"),
            PyValue::str("reason"),
            PyValue::str("fields")
        ]
    );
    let d = hr::event_detail(&load(
        r#"{"forced":1,"b_acknowledged":0,"a_acknowledged":[0]}"#,
    ));
    assert_eq!(
        d,
        [(
            PyValue::str("acknowledged"),
            PyValue::List(vec![PyValue::str("a")])
        )]
    );
}

fn put(body: &str, extra: &str) -> PyValue {
    load(&format!(
        r#"{{"memory":{{"id":"R","body":"{body}"{extra}}}}}"#
    ))
}

#[test]
fn record_row_actions() {
    let mut walk = hr::RecordWalk::default();
    let first = hr::record_row(
        &mut walk,
        &row(1, "memory_put"),
        &put("", r#","corroborations":1"#),
        None,
        None,
        None,
    );
    assert_eq!(first.action, "created");
    // The empty body still differs from no body: an empty diff, not None.
    assert_eq!(first.diff, Some(vec![]));
    let v = load("{}");
    let corr = put("", r#","corroborations":2"#);
    let r = hr::record_row(
        &mut walk,
        &row(2, "memory_put"),
        &corr,
        None,
        None,
        Some(true),
    );
    assert_eq!((r.action.as_str(), r.diff), ("corroborated", None));
    let r = hr::record_row(
        &mut walk,
        &row(3, "memory_put"),
        &put("", r#","corroborations":3"#),
        None,
        Some(("verification_put", &v)),
        None,
    );
    assert_eq!(r.action, "verified");
    // No field changed is not a corroboration.
    let r = hr::record_row(
        &mut walk,
        &row(4, "memory_put"),
        &put("", r#","corroborations":3"#),
        None,
        None,
        None,
    );
    assert_eq!(r.action, "updated");
    let dropped = load(r#"{"dropped":false}"#);
    let r = hr::record_row(
        &mut walk,
        &row(5, "returned_delete"),
        &dropped,
        None,
        None,
        None,
    );
    assert_eq!(r.action, "unmarked");
    let home = load(r#"{"home":{"project":"p"}}"#);
    assert_eq!(
        hr::record_row(&mut walk, &row(6, "home_put"), &home, None, None, None).action,
        "homed"
    );
    assert_eq!(
        hr::record_row(&mut walk, &row(7, "home_put"), &home, None, None, None).action,
        "moved"
    );
    assert_eq!(walk.last_record_kind.as_deref(), Some("memory_put"));
}

#[test]
fn delete_rows_keep_a_known_record_kind() {
    // The reference's `walk.record_kind or "episode"` keeps a kind already set.
    let empty = load("{}");
    let mut walk = hr::RecordWalk::default();
    hr::record_row(
        &mut walk,
        &row(1, "memory_put"),
        &put("b", ""),
        None,
        None,
        None,
    );
    hr::record_row(
        &mut walk,
        &row(2, "episode_delete"),
        &empty,
        None,
        None,
        None,
    );
    assert_eq!(walk.record_kind, Some("memory"));
    let episode = load(r#"{"episode":{"id":"R","body":"b"}}"#);
    for kind in ["memory_delete", "tombstone_delete"] {
        let mut walk = hr::RecordWalk::default();
        hr::record_row(
            &mut walk,
            &row(1, "episode_put"),
            &episode,
            None,
            None,
            None,
        );
        assert_eq!(walk.record_kind, Some("episode"));
        hr::record_row(&mut walk, &row(2, kind), &empty, None, None, None);
        assert_eq!(walk.record_kind, Some("episode"), "{kind}");
    }
    // With no kind known, each delete names its own.
    for (kind, want) in [
        ("memory_delete", "memory"),
        ("tombstone_delete", "memory"),
        ("episode_delete", "episode"),
    ] {
        let mut walk = hr::RecordWalk::default();
        hr::record_row(&mut walk, &row(1, kind), &empty, None, None, None);
        assert_eq!(walk.record_kind, Some(want), "{kind}");
    }
}

#[test]
fn imported_only_when_first_and_not_relinked() {
    let p = load(r#"{"memory":{"id":"R"},"provenance":"imported","relink":0}"#);
    let mut walk = hr::RecordWalk::default();
    let r = hr::record_row(&mut walk, &row(1, "memory_put"), &p, None, None, None);
    assert_eq!(r.action, "relinked");
    let p = load(r#"{"memory":{"id":"R"},"provenance":"imported"}"#);
    let mut walk = hr::RecordWalk::default();
    let r = hr::record_row(&mut walk, &row(1, "memory_put"), &p, None, None, None);
    assert_eq!(r.action, "imported");
    assert_eq!(
        r.detail,
        [(PyValue::str("provenance"), PyValue::str("imported"))]
    );
}

#[test]
fn links_added_then_dropped() {
    let mut by = HashMap::new();
    let p = load(
        r#"{"memory":{"links":[{"target_id":"R","type":"supersedes"},{"target_id":"R","type":"a"}]}}"#,
    );
    let rows = hr::link_change_rows(
        &row(1, "memory_put"),
        &p,
        &cps("S"),
        &cps("R"),
        &mut by,
        None,
    )
    .unwrap();
    let acts: Vec<&str> = rows.iter().map(|r| r.action.as_str()).collect();
    assert_eq!(acts, ["linked_from", "superseded_by"]);
    let p = load(r#"{"links":[]}"#);
    let rows = hr::link_change_rows(
        &row(2, "tombstone_put"),
        &p,
        &cps("S"),
        &cps("R"),
        &mut by,
        None,
    )
    .unwrap();
    let acts: Vec<&str> = rows.iter().map(|r| r.action.as_str()).collect();
    assert_eq!(acts, ["unlinked", "unlinked"]);
}
