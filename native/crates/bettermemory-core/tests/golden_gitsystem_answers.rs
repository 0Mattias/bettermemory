//! Every case of `gitsystem_answers.jsonl`, `gitsystem_answers_unicode.jsonl`
//! and `gitsystem_answers_python.jsonl` against `gitsystem::answers`: what
//! git's answers say, under both path flavours, on any host.

mod common;

use bettermemory_core::fsdata::Flavour;
use bettermemory_core::gitsystem::answers::{
    absolute, older_system, own_files, parse_version, reads_remotes, text, variables, Raised,
};
use bettermemory_core::gitsystem::record::surrogatepass;
use common::gitconfig_file::content;
use common::jsonl;
use common::parity::Ledger;
use serde_json::{json, Value};

fn b_spelled(data: &[u8]) -> String {
    if data
        .iter()
        .all(|&b| (0x20..0x7f).contains(&b) && b != b'\\')
    {
        return format!("={}", std::str::from_utf8(data).unwrap());
    }
    data.iter().map(|b| format!("{b:02x}")).collect()
}

/// The generator's `_t`: `_sp` of the text's `surrogatepass` bytes.
fn t(text: &[u32]) -> Value {
    let data = surrogatepass(text);
    let mut runs: Vec<(Vec<u8>, usize)> = Vec::new();
    let mut plain = Vec::new();
    let mut i = 0;
    while i < data.len() {
        let mut j = i;
        while j < data.len() && data[j] == data[i] {
            j += 1;
        }
        if j - i >= 32 {
            if !plain.is_empty() {
                runs.push((std::mem::take(&mut plain), 1));
            }
            runs.push((vec![data[i]], j - i));
        } else {
            plain.extend_from_slice(&data[i..j]);
        }
        i = j;
    }
    if runs.is_empty() {
        return json!(b_spelled(&data));
    }
    if !plain.is_empty() {
        runs.push((plain, 1));
    }
    json!(runs
        .iter()
        .map(|(p, n)| json!([b_spelled(p), n]))
        .collect::<Vec<_>>())
}

/// `bytes.decode("utf-8", "surrogatepass")`.
fn passed(mut b: &[u8]) -> Vec<u32> {
    let mut out = Vec::new();
    loop {
        match std::str::from_utf8(b) {
            Ok(s) => {
                out.extend(s.chars().map(u32::from));
                return out;
            }
            Err(e) => {
                let (good, rest) = b.split_at(e.valid_up_to());
                out.extend(std::str::from_utf8(good).unwrap().chars().map(u32::from));
                let [0xed, x @ 0xa0..=0xbf, y @ 0x80..=0xbf, ..] = rest else {
                    panic!("not surrogatepass UTF-8");
                };
                out.push(0xd000 | (u32::from(x & 0x3f) << 6) | u32::from(y & 0x3f));
                b = &rest[3..];
            }
        }
    }
}

fn flavour(case: &Value) -> Flavour {
    match case.get("fl").and_then(Value::as_str) {
        Some("nt") => Flavour::Windows,
        _ => Flavour::Posix,
    }
}

fn raised(r: Raised) -> Value {
    json!({"x": r.name()})
}

fn answer(case: &Value) -> Value {
    let fl = flavour(case);
    match case["f"].as_str().unwrap() {
        "version" => match parse_version(&text(&content(&case["i"]))) {
            Ok(v) => json!(v.map(|v| v.map(|p| p.as_str().to_owned()))),
            Err(r) => raised(r),
        },
        "absolute" => json!(absolute(fl, &passed(&content(&case["i"])))),
        "own" => match own_files(fl, &content(&case["i"])) {
            Ok(found) => json!(found.iter().map(|p| t(p)).collect::<Vec<_>>()),
            Err(r) => raised(r),
        },
        "older" => {
            let rc = case["rc"].as_i64().unwrap() as i32;
            match older_system(fl, rc, &content(&case["i"])) {
                Ok(p) => t(&p),
                Err(r) => raised(r),
            }
        }
        "variables" => json!(variables(&text(&content(&case["i"])))
            .items
            .iter()
            .map(|(k, v)| json!([t(k), t(v)]))
            .collect::<Vec<_>>()),
        "probe" => {
            let given = |v: &Value| -> Option<(i32, Vec<u8>)> {
                v.as_array()
                    .map(|a| (a[0].as_i64().unwrap() as i32, content(&a[1])))
            };
            let rm = given(&case["rm"]);
            let px = given(&case["px"]);
            let cp = case["cp"].as_i64().map(|c| c as i32);
            let p = reads_remotes(
                rm.as_ref().map(|(s, o)| (*s, o.as_slice())),
                px.as_ref().map(|(s, o)| (*s, o.as_slice())),
                cp,
                case["h"].as_bool().unwrap(),
            );
            json!([
                p.legacy,
                p.resets,
                p.prefix.as_deref().map(t),
                p.compat_differ,
                p.hasconfig
            ])
        }
        other => panic!("no function {other}"),
    }
}

fn run(test: &str, file: &str) {
    let lines = jsonl(file);
    let cases = &lines[1..];
    assert_eq!(lines[0]["cases"].as_u64(), Some(cases.len() as u64));
    let ledger = Ledger::new(test, file);
    let mut failures = Vec::new();
    for (i, case) in cases.iter().enumerate() {
        let got = answer(case);
        if got != case["r"] {
            failures.push(format!("#{i}: native {got} reference {}", case["r"]));
        }
    }
    ledger.settle(cases.len(), &failures);
}

#[test]
fn gitsystem_answers_every_case() {
    run("golden_gitsystem_answers", "gitsystem_answers.jsonl");
}

#[test]
fn gitsystem_answers_unicode_every_case() {
    run(
        "golden_gitsystem_answers_unicode",
        "gitsystem_answers_unicode.jsonl",
    );
}

#[test]
fn gitsystem_answers_python_every_case() {
    run(
        "golden_gitsystem_answers_python",
        "gitsystem_answers_python.jsonl",
    );
}
