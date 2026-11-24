//! `history_record.jsonl` against `history`'s fourth unit: each store
//! rebuilt from the store `store_create.json` pins, its five tables' rows
//! and each opening's key ring put back as the reference left them, then
//! `record_history` asked on a read-only open, verifying as the
//! reference's open did and under its `strict_provenance`. The whole
//! `to_dict(diff=True)` (as `json.dumps` writes it: rows in order, every
//! field, every key in its order), `exit_code` and `known` equal the
//! reference's, or the same exception class is raised. The sweep's
//! answers are compared by count and SHA-256. POSIX only, as the fixture
//! is.

#![cfg(unix)]

mod common;

use bettermemory_core::history_record::{record_history, HistoryError, RecordHistory};
use bettermemory_core::json::{self, DumpOptions};
use bettermemory_core::log::fingerprint;
use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::store::provenance::{Provenance, ProvenanceError};
use bettermemory_core::store::{StoreError, StoreFile};
use bettermemory_core::unicode;
use common::parity::Ledger;
use common::provenance::{build, ring, scratch};
use common::{hex, jsonl, text_field};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

const COMPACT: DumpOptions = DumpOptions {
    ensure_ascii: true,
    item_separator: ",",
    key_separator: ":",
    ..DumpOptions::DEFAULT
};

fn dumps(value: &PyValue) -> String {
    let text = json::dumps(value, COMPACT).expect("dumps");
    unicode::to_string(&text).expect("ascii")
}

fn class(e: &HistoryError) -> String {
    match e {
        HistoryError::Store(StoreError::Py(e)) => e.name().to_owned(),
        HistoryError::NoKeyFingerprint => "KeyError".to_owned(),
        HistoryError::Provenance(ProvenanceError::Raised { class, .. }) => (*class).to_owned(),
        other => format!("{other:?}"),
    }
}

/// One opening of a store: its ring laid out, the store opened, the
/// reader made as the reference's open verified.
fn open_and<T>(
    tables: &Value,
    opening: &Value,
    store_id: &str,
    each: impl FnOnce(&StoreFile, &bettermemory_core::log::KeyRing, &Provenance<'_>) -> T,
) -> Result<T, String> {
    let dir = scratch("history-record");
    let (file, keys) = build(
        &json!({"tables": tables, "keys": opening["keys"]}),
        store_id,
        &dir.0,
    );
    let keyring = ring(&keys, store_id);
    let verifies = !opening["verifies"].is_null();
    if verifies {
        let current = keyring.current.as_ref().ok_or("no current key")?;
        if opening["verifies"] != fingerprint(current.bytes()).as_str() {
            return Err("the reference verifies under another key".to_owned());
        }
    }
    let strict = opening["strict"].as_bool().expect("strict");
    let store = StoreFile::open(&file).map_err(|e| format!("open: {e:?}"))?;
    let reader = Provenance::new(&store, &keyring, verifies, strict).map_err(|e| e.to_string())?;
    Ok(each(&store, &keyring, &reader))
}

fn check(ask: &Value, got: &Result<RecordHistory, HistoryError>) -> Result<(), String> {
    match (got, ask.get("raised")) {
        (Ok(history), None) => {
            let text = dumps(&history.to_dict(true));
            if text != ask["r"].as_str().expect("r") {
                return Err(format!("got {text}"));
            }
            if json!(history.exit_code()) != ask["exit"] || json!(history.known()) != ask["known"] {
                return Err(format!(
                    "exit {} known {}",
                    history.exit_code(),
                    history.known()
                ));
            }
            Ok(())
        }
        (Err(e), Some(want)) if class(e) == want.as_str().expect("class") => Ok(()),
        (Err(e), _) => Err(format!("raised {e:?}")),
        (Ok(_), Some(want)) => Err(format!("answered where the reference raised {want}")),
    }
}

#[test]
fn every_history_is_the_reference_s() {
    let lines = jsonl("history_record.jsonl");
    let store_id = lines[0]["store_id"].as_str().expect("store_id");
    let mut failures = Vec::new();
    let mut asked = 0;
    let mut want = 0;
    for line in &lines[1..] {
        let Some(openings) = line.get("openings") else {
            continue;
        };
        let name = line["store"].as_str().expect("store");
        for (n, opening) in openings.as_array().expect("openings").iter().enumerate() {
            let asks = opening["asks"].as_array().expect("asks");
            want += asks.len();
            let run = open_and(
                &line["tables"],
                opening,
                store_id,
                |store, keyring, reader| {
                    let mut out = Vec::new();
                    for ask in asks {
                        let id: PyStr = text_field(ask, "id");
                        let got = record_history(store, keyring, reader, &id);
                        out.push(check(ask, &got).map_err(|e| format!("{name} #{n} {ask}: {e}")));
                    }
                    out
                },
            );
            match run {
                Ok(results) => {
                    for r in results {
                        asked += 1;
                        if let Err(e) = r {
                            failures.push(e);
                        }
                    }
                }
                Err(e) => failures.push(format!("{name} #{n}: {e}")),
            }
        }
    }
    eprintln!("history_record.jsonl: {asked} histories checked");
    assert!(asked > 0, "no history checked");
    assert_eq!(asked, want, "histories checked against the file's");
    Ledger::new("every_history_is_the_reference_s", "history_record.jsonl")
        .settle(asked, &failures);
}

#[test]
fn the_sweep_reads_as_the_reference_s() {
    let lines = jsonl("history_record.jsonl");
    let store_id = lines[0]["store_id"].as_str().expect("store_id");
    let line = lines
        .iter()
        .find(|l| l.get("sweep").is_some())
        .expect("a sweep");
    let sweep = &line["sweep"];
    let ids: Vec<&str> = sweep["ids"]
        .as_array()
        .expect("ids")
        .iter()
        .map(|i| i.as_str().expect("id"))
        .collect();
    let (count, digest) = open_and(
        &line["tables"],
        sweep,
        store_id,
        |store, keyring, reader| {
            let mut hash = Sha256::new();
            for id in &ids {
                let history =
                    record_history(store, keyring, reader, &unicode::cps(id)).expect("history");
                let answer = PyValue::List(vec![
                    PyValue::str(id),
                    history.to_dict(true),
                    PyValue::int(i128::from(history.exit_code())),
                    PyValue::Bool(history.known()),
                ]);
                hash.update(dumps(&answer).as_bytes());
                hash.update(b"\n");
            }
            (ids.len(), hex(&hash.finalize()))
        },
    )
    .expect("sweep");
    assert!(count > 0);
    assert_eq!(json!(count), sweep["count"]);
    let ledger = Ledger::new("the_sweep_reads_as_the_reference_s", "history_record.jsonl");
    let failures = if sweep["sha256"] == digest.as_str() {
        Vec::new()
    } else {
        vec![format!("sweep digest {digest}")]
    };
    ledger.settle(count, &failures);
}
