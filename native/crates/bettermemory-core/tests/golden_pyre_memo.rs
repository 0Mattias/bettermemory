//! Every row of `pyre_memo.jsonl` (`gen_pyre_memo`): each hostile family
//! of the memo's decision, its text regenerated here by the row's recipe
//! at the sizes `re` finishes quickly, answered by the engine built from
//! the reference's own pattern text and flags, and compared with `re`'s
//! answer by count and SHA-256 under every way of the memo (off, on with
//! and without the intervals, as shipped, and switched on after 0 to 40
//! steps).

mod common;

use bettermemory_core::pyre::memo::{Setting, Way};
use bettermemory_core::pyre::regex::Regex;
use common::parity::Ledger;
use serde_json::Value;
use sha2::{Digest, Sha256};

pub fn spelled(v: &Value) -> Vec<u32> {
    match v.as_str() {
        Some(s) => s.chars().map(u32::from).collect(),
        None => v
            .as_array()
            .expect("code points")
            .iter()
            .map(|c| c.as_u64().expect("a code point") as u32)
            .collect(),
    }
}

/// `gen_pyre_memo.recipe`.
pub fn recipe(row: &Value, n: usize) -> Vec<u32> {
    let r = &row["recipe"];
    let unit = spelled(&r["unit"]);
    let div = r["div"].as_u64().expect("div") as usize;
    let mut t = spelled(&r["head"]);
    for _ in 0..n / div {
        t.extend(&unit);
    }
    t.extend(spelled(&r["tail"]));
    t
}

pub fn regex(row: &Value) -> Regex {
    let flags = row["flags"].as_u64().expect("flags") as u32;
    Regex::new(&spelled(&row["pattern"]), flags)
        .unwrap_or_else(|e| panic!("the engine refuses {}: {e:?}", row["pattern"]))
}

fn string(t: &[u32]) -> String {
    t.iter().filter_map(|&c| char::from_u32(c)).collect()
}

/// `gen_pyre_memo.answer`.
pub fn answer(re: &Regex, method: &str, t: &[u32]) -> (u64, String) {
    let (count, body) = match method {
        "sub" => {
            let (out, n) = re.subn(&[45], t, 0).expect("subn");
            (n as u64, format!("{}|{n}", string(&out)))
        }
        "finditer" => {
            let ms = re.finditer(t, 0, isize::MAX).expect("finditer");
            let spans: Vec<String> = ms
                .iter()
                .map(|m| format!("{},{}", m.regs[0].0, m.regs[0].1))
                .collect();
            (ms.len() as u64, spans.join(";"))
        }
        m => {
            let found = match m {
                "match" => re.r#match(t, 0, isize::MAX),
                "fullmatch" => re.fullmatch(t, 0, isize::MAX),
                "search" => re.search(t, 0, isize::MAX),
                other => panic!("a method: {other}"),
            }
            .expect("a call");
            match found {
                None => (0, "-".into()),
                Some(m) => (1, format!("{},{}", m.regs[0].0, m.regs[0].1)),
            }
        }
    };
    (count, common::hex(&Sha256::digest(body.as_bytes())))
}

fn ways() -> Vec<Setting> {
    let mut out = vec![
        Setting::OFF,
        Setting {
            way: Way::On,
            intervals: true,
        },
        Setting {
            way: Way::On,
            intervals: false,
        },
        Setting::SHIPPED,
    ];
    out.extend((0..=40).map(|b| Setting {
        way: Way::After(b),
        intervals: true,
    }));
    out
}

fn check(row: &Value) -> u64 {
    let plain = regex(row);
    let method = row["method"].as_str().expect("method");
    let mut checked = 0;
    for (size, want) in row["answers"].as_object().expect("answers") {
        let t = recipe(row, size.parse().expect("a size"));
        let want = (
            want["count"].as_u64().expect("count"),
            want["sha256"].as_str().expect("sha256").to_string(),
        );
        for way in ways() {
            let re = plain.clone().with_memo(way);
            assert_eq!(
                answer(&re, method, &t),
                want,
                "{} {} at {size} under {way:?}",
                row["pattern"],
                row["note"]
            );
            checked += 1;
        }
    }
    checked
}

#[test]
fn pyre_memo_every_row() {
    let mut file = common::jsonl("pyre_memo.jsonl");
    let header = file.remove(0);
    assert_eq!(header["part"], "header");
    let mut ledger = Ledger::new("pyre_memo_every_row", "pyre_memo.jsonl");
    let mut calls = 0;
    for row in &file {
        let part = row["part"].as_str().expect("part");
        assert!(part == "memo.family" || part == "memo.extra", "{part}");
        ledger.case(format!("{} {}", row["pattern"], row["note"]), || {
            calls += check(row);
        });
    }
    assert!(calls > 0, "no call checked");
    common::assert_count(
        "pyre_memo.jsonl header",
        file.len(),
        header["rows"].as_u64().expect("rows") as usize,
    );
    ledger.finish(file.len());
}
