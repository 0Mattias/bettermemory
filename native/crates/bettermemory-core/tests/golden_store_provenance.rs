//! The provenance readers (`store::provenance`) against
//! `store_provenance.jsonl`: every case's store rebuilt from the
//! store `store_create.json` pins, its six tables' rows and its key ring
//! put back as the reference left them, then every query asked on one
//! native reader, in the case's order, verifying as the reference's open
//! did (`can_verify`, under the ring's current key) and under its
//! `strict_provenance`. Every answer equals the reference's: labels and
//! their order, trust rows, the unaccounted ids, the counts, the links and
//! the episodes' labels. POSIX only, as the fixture is.

#![cfg(unix)]

mod common;

use bettermemory_core::log::fingerprint;
use common::parity::Ledger;
use common::provenance::{answers, build, cases, ring, same, scratch};
use serde_json::Value;

fn run(case: &Value, store_id: &str) -> Result<usize, String> {
    let name = case["case"].as_str().expect("name");
    let dir = scratch("golden");
    let (file, keys) = build(case, store_id, &dir.0);
    let verifies = !case["verifies"].is_null();
    if verifies {
        let current = ring(&keys, store_id).current.ok_or("no current key")?;
        if case["verifies"] != fingerprint(current.bytes()).as_str() {
            return Err(format!("{name}: the reference verifies under another key"));
        }
    }
    let strict = case["strict"].as_bool().expect("strict");
    let queries = case["queries"].as_array().expect("queries");
    let got = answers(&file, &keys, store_id, verifies, strict, queries)
        .map_err(|e| format!("{name}: refused: {e}"))?;
    for (query, got) in queries.iter().zip(&got) {
        let mut want = serde_json::Map::new();
        for k in ["returned", "raised"] {
            if let Some(v) = query.get(k) {
                want.insert(k.to_owned(), v.clone());
            }
        }
        if !same(&Value::Object(want), got) {
            return Err(format!(
                "{name} {} {}: got {got}, the reference {query}",
                query["call"],
                query.get("id").unwrap_or(&Value::Null)
            ));
        }
    }
    Ok(got.len())
}

#[test]
fn every_provenance_answer_is_the_reference_s() {
    let (header, cases) = cases();
    let store_id = header["store_id"].as_str().expect("store_id");
    let mut failures = Vec::new();
    let mut queries = 0;
    for case in &cases {
        match run(case, store_id) {
            Ok(n) => queries += n,
            Err(e) => failures.push(e),
        }
    }
    eprintln!(
        "store_provenance.jsonl: {} cases, {queries} queries checked",
        cases.len()
    );
    let ledger = Ledger::new(
        "every_provenance_answer_is_the_reference_s",
        "store_provenance.jsonl",
    );
    ledger.settle(cases.len(), &failures);
}

/// `json.loads(bytes)`'s decoding (`json.detect_encoding` and
/// `surrogatepass`) and `str(bytes)`, the text a blob payload's MAC is
/// computed over, against the header's samples.
#[test]
fn bytes_decode_and_spell_as_the_reference() {
    use bettermemory_core::store::provenance::decode_json_bytes;
    use bettermemory_core::store::SqlValue;
    let (header, _) = cases();
    let mut failures = Vec::new();
    let decoded = header["json_bytes"].as_array().expect("json_bytes");
    for sample in decoded {
        let bytes = common::unhex(sample[0].as_str().expect("hex"));
        let got = decode_json_bytes(&bytes);
        let want: Option<Vec<u32>> = sample[1].as_array().map(|cps| {
            cps.iter()
                .map(|c| u32::try_from(c.as_u64().expect("cp")).expect("u32"))
                .collect()
        });
        if got != want {
            failures.push(format!("decode {}: got {got:?}, want {want:?}", sample[0]));
        }
    }
    let spelled = header["bytes_repr"].as_array().expect("bytes_repr");
    for sample in spelled {
        let got = SqlValue::Blob(common::unhex(sample[0].as_str().expect("hex"))).py_str();
        // The spelling is recorded as its UTF-8 bytes in hex: its
        // backslashes would read as host paths to the fixture's guard.
        if common::hex(got.as_bytes()) != sample[1].as_str().expect("hex") {
            failures.push(format!("repr {}: got {got}, want {}", sample[0], sample[1]));
        }
    }
    let ledger = Ledger::new(
        "bytes_decode_and_spell_as_the_reference",
        "store_provenance.jsonl",
    );
    ledger.settle(decoded.len() + spelled.len(), &failures);
}
