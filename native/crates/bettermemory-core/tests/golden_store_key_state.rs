//! Check data, diagnostics and authenticated store reads against store_keys.py.
mod common;

use bettermemory_core::fsutil::fsdecode;
use bettermemory_core::json::loads;
use bettermemory_core::log::ring::KeyFiles;
use bettermemory_core::log::{Head, Key, LogRow, OriginCheck};
use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::store::create::{create, System};
use bettermemory_core::store::key_state::{
    head_status, warn_bound_elsewhere, warn_unclaimed, HeadStatus, KeyReadError, KeyState,
    KeyStatus, Level, RingFit, StateError, Warning,
};
use bettermemory_core::store::keys::load_key_ring;
use bettermemory_core::store::open::open_for_writing;
use bettermemory_core::store::record_event::{record_event, EventCall, EventTransaction};
use bettermemory_core::store::StoreFile;
use bettermemory_core::unicode::{cps, to_string};
use common::parity::Ledger;
use common::stores::scratch;
use serde_json::{json, Value};
use std::path::{Path, PathBuf};

fn ring(path: &Path) -> KeyFiles {
    KeyFiles::new(path, &PyValue::str(&"a".repeat(32))).unwrap()
}

fn opt(text: Option<PyStr>) -> PyValue {
    text.map_or(PyValue::None, PyValue::Str)
}

fn fit(origin: OriginCheck, status: &str, head: bool, end: bool) -> RingFit {
    let head_status = match status {
        "absent" => HeadStatus::Absent,
        "ok" => HeadStatus::Ok,
        "stale" => HeadStatus::Stale,
        "tail_deleted" => HeadStatus::Past,
        "mismatch" => HeadStatus::Mismatch,
        x => panic!("{x}"),
    };
    RingFit {
        origin,
        head_status,
        head: head.then(|| Head {
            seq: PyValue::int(5),
            mac: cps(&"4".repeat(64)),
        }),
        log_end: end.then(|| LogRow {
            seq: 3,
            ts: "now".into(),
            session: None,
            kind: "search".into(),
            payload: "{}".into(),
            mac: "2".repeat(64),
            prev_mac: "3".repeat(64),
        }),
    }
}
fn static_variant(s: &str) -> &'static str {
    [
        "absent",
        "ok",
        "unverifiable",
        "mismatch",
        "signature",
        "mac",
        "missing",
    ]
    .into_iter()
    .find(|&v| s == v)
    .expect("recognized fixture value")
}
fn fit_projection(doc: &Value) -> PyValue {
    let input = doc["origin"].as_array().unwrap();
    let status = static_variant(input[0].as_str().unwrap());
    let origin = OriginCheck {
        status,
        seq: input[1].as_i64(),
        reason: input[2].as_str().map(static_variant),
        signer: (status == "ok").then(|| "d".repeat(64)),
    };
    let f = fit(
        origin,
        doc["head_status"].as_str().unwrap(),
        doc["head"].as_bool().unwrap(),
        doc["end"].as_bool().unwrap(),
    );
    PyValue::List(vec![
        f.origin.to_dict(),
        PyValue::Bool(f.origin.holds()),
        PyValue::Bool(f.origin.broken()),
        PyValue::Bool(f.carries_origin()),
        PyValue::Bool(f.reaches_head()),
        PyValue::Bool(f.fits()),
        opt(f.reason().unwrap()),
        f.to_dict().unwrap(),
    ])
}
fn state_projection(doc: &Value) -> PyValue {
    let state = match doc["kind"].as_str().unwrap() {
        "present" => KeyStatus::Present,
        "missing" => KeyStatus::Missing,
        "unreadable" => KeyStatus::Unreadable,
        "foreign" => KeyStatus::Foreign,
        "bound_elsewhere" => KeyStatus::BoundElsewhere,
        "unclaimed" => KeyStatus::Unclaimed,
        x => panic!("{x}"),
    };
    let variant = doc["variant"].as_u64().unwrap();
    let expected = if variant == 2 {
        let mut v = cps(&("1".repeat(14) + "🦩"));
        v.push(0xdcff);
        v.extend(cps("extra"));
        v
    } else {
        cps(&"2".repeat(64))
    };
    let mut s = KeyState::new(
        state,
        ring(Path::new(".")),
        PathBuf::from("store"),
        expected,
    );
    if variant > 0 {
        s.key = Some(Key::new((0..32).collect()));
    }
    if state == KeyStatus::BoundElsewhere || variant == 2 {
        s.bound_to = Some("other".into());
    }
    if variant == 1 {
        s.unread = Some(Vec::new());
    }
    if variant == 2 {
        let mut reason = cps("reason-");
        reason.push(0xdcff);
        s.error = Some(KeyReadError {
            path: s.key_path(),
            errno: None,
            reason,
        });
        let mut unread = cps("detail-");
        unread.push(0xdcff);
        s.unread = Some(unread);
        s.unfit = Some(fit(
            OriginCheck {
                status: "mismatch",
                seq: Some(1),
                reason: Some("missing"),
                signer: None,
            },
            "tail_deleted",
            true,
            true,
        ));
    }
    PyValue::List(vec![
        PyValue::Str(fsdecode(s.key_path().as_os_str())),
        opt(s.withheld().as_ref().map(|p| fsdecode(p.as_os_str()))),
        opt(s.held().as_deref().map(cps)),
        PyValue::Str(s.summary().unwrap()),
        opt(s.remedy().unwrap()),
        PyValue::Str(s.refusal(Path::new("display-🦩")).unwrap()),
        s.details().unwrap(),
    ])
}
#[test]
fn state_and_history_match_reference() {
    let name = "store_key_state.jsonl";
    let rows = common::jsonl(name);
    let mut ledger = Ledger::new("state_and_history_match_reference", name);
    for (n, doc) in rows.iter().enumerate() {
        ledger.case(n, || {
            let actual = if doc["op"] == "fit" {
                fit_projection(doc)
            } else {
                state_projection(doc)
            };
            assert_eq!(
                actual,
                loads(&cps(doc["expected"].as_str().unwrap())).unwrap(),
                "row {n}"
            );
        });
    }
    ledger.finish(rows.len());
}

fn checkpoints(area: &Path) -> Vec<Head> {
    let file = area.join("memory.sqlite");
    let keys = area.join("keys");
    let mut provider = System {
        engine_version: "fixture".to_owned(),
    };
    let created = create(&file, &keys, &mut provider).unwrap();
    let mut store = open_for_writing(&file, &keys, &provider).unwrap();
    let read = || {
        load_key_ring(&keys, &created.store_id)
            .unwrap()
            .read_head()
            .unwrap()
            .unwrap()
    };
    let mut heads = vec![read()];
    for n in 1..3 {
        let call = EventCall {
            kind: &cps("search"),
            session: Some(&cps("session")),
            best_effort: false,
            attempts: 1,
            deadline: None,
        };
        let fields = [(PyValue::str("query"), PyValue::str(&format!("sample {n}")))];
        let row = record_event(
            Ok(EventTransaction::Store(&mut store, &mut provider)),
            &call,
            &fields,
            None,
            &mut |_| Vec::new(),
            &mut Vec::new(),
        )
        .unwrap()
        .unwrap();
        let head = read();
        assert_eq!(head.mac, cps(&row.mac));
        heads.push(head);
    }
    store.close().unwrap();
    heads
}
#[test]
fn ordinary_signed_checkpoints_match_reference() {
    let dir = scratch("key-state-signed");
    let first = dir.0.join("first");
    let own = checkpoints(&first);
    let other = checkpoints(&dir.0.join("second"));
    let store = StoreFile::open(&first.join("memory.sqlite")).unwrap();
    let name = "store_key_state_signed.jsonl";
    let rows = common::jsonl(name);
    let mut ledger = Ledger::new("ordinary_signed_checkpoints_match_reference", name);
    for doc in &rows {
        let case = doc["case"].as_str().unwrap();
        ledger.case(case, || {
            let h = match case {
                "absent" => None,
                "current" => Some(own[2].clone()),
                "stale-first" => Some(own[0].clone()),
                "stale-middle" => Some(own[1].clone()),
                "other-current" => Some(other[2].clone()),
                "other-middle" => Some(other[1].clone()),
                "before-first" | "far-ahead" | "lower-bind" => {
                    let seq = match case {
                        "before-first" => 0,
                        "far-ahead" => 1_i128 << 104,
                        _ => i128::from(i64::MIN),
                    };
                    Some(Head {
                        seq: PyValue::int(seq),
                        mac: own[0].mac.clone(),
                    })
                }
                x => panic!("{x}"),
            };
            let actual = match head_status(h.as_ref(), store.connection()) {
                Ok(result) => json!(["ok", result.name()]),
                Err(StateError::Raised { class, message }) => {
                    json!([class, to_string(&message).unwrap()])
                }
                Err(e) => panic!("{e:?}"),
            };
            assert_eq!(actual, doc["expected"], "{case}");
        });
    }
    ledger.finish(rows.len());
}
#[test]
fn warnings_match_reference() {
    let dir = scratch("key-state-warnings");
    let root = &dir.0;
    let target = root.join("location.sqlite");
    let other = root.join("other.sqlite");
    std::fs::write(&target, []).unwrap();
    std::fs::write(&other, []).unwrap();
    let files = ring(&root.join("keys"));
    let mut presented = Vec::new();
    let norm = |v: String| {
        v.replace(root.to_str().unwrap(), "ROOT")
            .replace(std::path::MAIN_SEPARATOR, "/")
    };
    let mut emit = |w: Warning<'_>| {
        let level = match w.level {
            Level::Warning => "WARNING",
            Level::Debug => "DEBUG",
        };
        presented.push(json!({"level":level,"logger":Warning::LOGGER,"format":Warning::FORMAT,
            "arguments":[norm(w.path.display().to_string()),norm(to_string(&w.summary).unwrap()),norm(to_string(&w.remedy).unwrap())],
            "message":norm(to_string(&w.message()).unwrap())}));
        Ok::<(), ()>(())
    };
    let id = cps(files.store_id());
    warn_bound_elsewhere(&files, &target, &id, &other, None, &mut emit).unwrap();
    warn_bound_elsewhere(
        &files,
        &target,
        &id,
        &other,
        Some(&cps("unreadable this time")),
        &mut emit,
    )
    .unwrap();
    let state = KeyState::new(KeyStatus::Unclaimed, files, target, cps(&"f".repeat(64)));
    warn_unclaimed(&state, &mut emit).unwrap();
    warn_unclaimed(&state, &mut emit).unwrap();
    let name = "store_key_state_warnings.jsonl";
    let rows = common::jsonl(name);
    let mut ledger = Ledger::new("warnings_match_reference", name);
    assert_eq!(rows.len(), presented.len());
    for (n, (actual, expected)) in presented.iter().zip(&rows).enumerate() {
        ledger.case(n, || assert_eq!(actual, expected));
    }
    ledger.finish(rows.len());
}
