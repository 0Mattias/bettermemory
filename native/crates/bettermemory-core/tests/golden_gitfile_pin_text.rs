//! Every case of `gitfile_pin_text.jsonl` (class i, every platform): what
//! `gitdir_pin._read` makes of a record file's bytes, against
//! `gitfile_rule::follow::pin_text`.

mod common;

use bettermemory_core::gitfile_rule::follow::{pin_text, PinText};
use common::jsonl;
use common::parity::Ledger;
use serde_json::{json, Value};

const FILE: &str = "gitfile_pin_text.jsonl";

/// A `str`'s code points encoded UTF-8 with `surrogatepass`, as hex.
fn passed(text: &[u32]) -> String {
    let mut out = Vec::new();
    for &cp in text {
        if (0xD800..=0xDFFF).contains(&cp) {
            out.extend_from_slice(&[
                0xE0 | (cp >> 12) as u8,
                0x80 | ((cp >> 6) & 0x3F) as u8,
                0x80 | (cp & 0x3F) as u8,
            ]);
        } else {
            let mut buf = [0; 4];
            out.extend_from_slice(char::from_u32(cp).unwrap().encode_utf8(&mut buf).as_bytes());
        }
    }
    common::hex(&out)
}

/// `gen_follow.long_text`.
fn long_text(case: &Value) -> Vec<u8> {
    let ok = b"{\"format\": 1, \"git_dir\": \"/g\", \"tree\": \"/t\"}\n";
    if let Some(pad) = case.get("pad").and_then(Value::as_u64) {
        let mut out = ok[..ok.len() - 1].to_vec();
        out.resize(out.len() + usize::try_from(pad).unwrap(), b' ');
        out.push(b'\n');
        return out;
    }
    let digits = usize::try_from(case["digits"].as_u64().unwrap()).unwrap();
    let mut out = b"{\"format\": 1, \"git_dir\": \"/g\", \"tree\": \"/t\", \"n\": ".to_vec();
    out.resize(out.len() + digits, b'9');
    out.push(b'}');
    out
}

#[test]
fn pin_text_every_case() {
    let lines = jsonl(FILE);
    let cases = &lines[1..];
    assert_eq!(lines[0]["cases"].as_u64(), Some(cases.len() as u64));
    let ledger = Ledger::new("golden_gitfile_pin_text", FILE);
    let mut failures = Vec::new();
    for (i, case) in cases.iter().enumerate() {
        let data = match case.get("b").and_then(Value::as_str) {
            Some(hex) => common::unhex(hex),
            None => long_text(case),
        };
        let got = match pin_text(&data) {
            PinText::Pin(pin) => json!([passed(&pin.git_dir), passed(&pin.tree)]),
            PinText::NotAPin => Value::Null,
            PinText::Raised(name) => json!({"e": name}),
        };
        if got != case["v"] {
            failures.push(format!("#{i}: native {got} reference {}", case["v"]));
        }
    }
    ledger.settle(cases.len(), &failures);
}
