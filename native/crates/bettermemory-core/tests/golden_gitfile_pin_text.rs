//! Every case of `gitfile_pin_text.jsonl` (class i, every platform) and
//! `gitfile_pin_text_python.jsonl` (class ii, Python): what
//! `gitdir_pin._read` makes of a record file's bytes with `os.path` taken
//! as `posixpath` (`px`) and as `ntpath` (`nt`), against
//! `gitfile_rule::follow::pin_text_under` of each flavour. Both are read on
//! every platform: the files are the same wherever they are made.

mod common;

use bettermemory_core::fsdata::Flavour;
use bettermemory_core::gitfile_rule::follow::{pin_text, pin_text_under, PinText};
use common::jsonl;
use common::parity::Ledger;
use serde_json::{json, Value};

const FILE: &str = "gitfile_pin_text.jsonl";
/// The `nt` answers 3.11 and 3.12 give otherwise (a path rooted without a
/// drive).
const GATED: &str = "gitfile_pin_text_python.jsonl";

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

fn answer(data: &[u8], flavour: Flavour) -> Value {
    let got = pin_text_under(data, flavour);
    if flavour == Flavour::Posix {
        assert_eq!(pin_text(data), got, "pin_text is the POSIX reading");
    }
    match got {
        PinText::Pin(pin) => json!([passed(&pin.git_dir), passed(&pin.tree)]),
        PinText::NotAPin => Value::Null,
        PinText::Raised(name) => json!({"e": name}),
    }
}

/// Every case of `file`; each must hold the flavours `must`.
fn every_case(test: &str, file: &str, must: &[&str]) {
    let lines = jsonl(file);
    let cases = &lines[1..];
    assert_eq!(lines[0]["cases"].as_u64(), Some(cases.len() as u64));
    let ledger = Ledger::new(test, file);
    let mut failures = Vec::new();
    let mut compared = 0;
    for (i, case) in cases.iter().enumerate() {
        for key in must {
            assert!(case.get(*key).is_some(), "{file} #{i} has no {key}");
        }
        let data = match case.get("b").and_then(Value::as_str) {
            Some(hex) => common::unhex(hex),
            None => long_text(case),
        };
        for (key, flavour) in [("px", Flavour::Posix), ("nt", Flavour::Windows)] {
            let Some(want) = case.get(key) else {
                continue;
            };
            compared += 1;
            let got = answer(&data, flavour);
            if &got != want {
                failures.push(format!("#{i} {key}: native {got} reference {want}"));
            }
        }
    }
    ledger.settle(compared, &failures);
}

#[test]
fn pin_text_every_case() {
    every_case("golden_gitfile_pin_text", FILE, &["px"]);
}

#[test]
fn pin_text_gated_every_case() {
    every_case("golden_gitfile_pin_text_gated", GATED, &["nt"]);
}
