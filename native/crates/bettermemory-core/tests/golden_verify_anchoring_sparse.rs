//! Every case of `verify_anchoring_sparse.jsonl` (not gated) against
//! `verify_anchoring::sparse`. The spelling is the generator's
//! (`tests/contract/gen_verify_anchoring_sparse.py`).

mod common;

use bettermemory_core::verify_anchoring::sparse::{
    config_switch, index_hash_len, index_varint, parse_skip_index, SkipIndex, CE_EXTENDED,
    CE_EXTENDED_FLAGS, CE_SKIP_WORKTREE,
};
use common::parity::Ledger;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

const FILE: &str = "verify_anchoring_sparse.jsonl";

fn unhex(s: &str) -> Vec<u8> {
    (0..s.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&s[i..i + 2], 16).expect("hex"))
        .collect()
}

fn hex(b: &[u8]) -> String {
    b.iter().map(|x| format!("{x:02x}")).collect()
}

fn bytes(v: &Value) -> Vec<u8> {
    let mut out = Vec::new();
    for piece in v.as_array().expect("pieces") {
        match piece {
            Value::String(s) => out.extend(unhex(s)),
            Value::Array(run) => {
                let b = unhex(run[0].as_str().expect("a byte"))[0];
                let n = run[1].as_u64().expect("a count") as usize;
                out.extend(std::iter::repeat_n(b, n));
            }
            other => panic!("piece {other}"),
        }
    }
    out
}

fn cps(v: &Value) -> Vec<u32> {
    v.as_array()
        .expect("code points")
        .iter()
        .map(|c| c.as_u64().expect("a code point") as u32)
        .collect()
}

fn names(v: &Value) -> Vec<Vec<u8>> {
    v.as_array()
        .expect("names")
        .iter()
        .map(|n| unhex(n.as_str().expect("hex")))
        .collect()
}

fn spelled(got: &Result<SkipIndex, impl std::fmt::Debug>) -> Value {
    match got {
        Ok(i) => json!({
            "s": i.skipped.iter().map(|n| hex(n)).collect::<Vec<_>>(),
            "k": i.kept.iter().map(|n| hex(n)).collect::<Vec<_>>(),
        }),
        Err(_) => json!("declined"),
    }
}

fn sweep_line(data: &[u8], h: usize) -> String {
    match parse_skip_index(data, h) {
        Err(_) => "D".into(),
        Ok(i) => {
            let join = |n: &[Vec<u8>]| n.iter().map(|x| hex(x)).collect::<Vec<_>>().join(",");
            format!("s{}|k{}", join(&i.skipped), join(&i.kept))
        }
    }
}

fn answer(case: &Value) -> Value {
    match case["f"].as_str().expect("a function") {
        "c" => match config_switch(&cps(&case["t"]), &cps(&case["s"]), &cps(&case["k"])) {
            Ok(o) => json!(o),
            Err(_) => json!("UnicodeEncodeError"),
        },
        "v" => {
            let pos = case["p"].as_u64().expect("a pos") as usize;
            match index_varint(&bytes(&case["d"]), pos) {
                Ok((v, p)) => json!([v, p]),
                Err(_) => json!("declined"),
            }
        }
        "p" => {
            let h = case["h"].as_u64().expect("a length") as usize;
            spelled(&parse_skip_index(&bytes(&case["d"]), h))
        }
        "s" => {
            let index = SkipIndex {
                skipped: names(&case["skipped"]),
                kept: names(&case["kept"]),
            };
            json!(index.skips(&names(&case["parts"])))
        }
        "sweep" => {
            let data = bytes(&case["d"]);
            let h = case["h"].as_u64().expect("a length") as usize;
            let mut lines = Vec::new();
            for n in 0..=data.len() {
                lines.push(sweep_line(&data[..n], h));
            }
            for i in 0..data.len() {
                for b in [0x00u8, 0xFF] {
                    let mut d = data.clone();
                    d[i] = b;
                    lines.push(sweep_line(&d, h));
                }
            }
            let mut hasher = Sha256::new();
            for line in &lines {
                hasher.update(line.as_bytes());
                hasher.update(b"\n");
            }
            let digest = hex(&hasher.finalize());
            json!({"n": lines.len(), "sha256": digest})
        }
        other => panic!("unknown function {other}"),
    }
}

#[test]
fn golden_verify_anchoring_sparse() {
    let lines = common::jsonl(FILE);
    let header = &lines[0];
    assert_eq!(header["k"], "header");
    assert_eq!(
        header["flags"],
        json!([CE_EXTENDED, CE_SKIP_WORKTREE, CE_EXTENDED_FLAGS])
    );
    let table = header["hash_len"].as_object().expect("a table");
    assert_eq!(table.len(), 2);
    for (name, len) in table {
        assert_eq!(
            index_hash_len(name.as_bytes()).map(|n| n as u64),
            len.as_u64()
        );
    }
    assert_eq!(index_hash_len(b"sha512"), None);
    let count = header["count"].as_u64().expect("a count") as usize;
    let mut ledger = Ledger::new("golden_verify_anchoring_sparse", FILE);
    for (i, case) in lines[1..].iter().enumerate() {
        let got = answer(case);
        let want = if case["f"] == "sweep" {
            json!({"n": case["n"], "sha256": case["sha256"]})
        } else {
            case["o"].clone()
        };
        let label = format!("line {}", i + 2);
        assert!(
            !ledger.differs_fatally(&label, got == want),
            "{label} {case}: got {got}"
        );
    }
    assert_eq!(lines.len() - 1, count);
    ledger.finish(count);
}
