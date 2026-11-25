//! The memo changes no answer (unit pyre-9, decision condition 7): every
//! text the engine's committed goldens pin (`pyre_exec.jsonl`,
//! `pyre_api.jsonl`, `pyre_inventory.jsonl` and their rows in
//! `pyre_unicode.jsonl` and `pyre_python.jsonl`, under those files'
//! gates) runs through `match`, `fullmatch`, `search`, `finditer` and
//! `findall`, and the replacing rows through `subn`, `subn` with a
//! function and `split`, under every way of the memo: off, on from the
//! first step with and without the intervals, as shipped, and switched on
//! after each budget from 0 to 40 steps. Any answer that differs from the
//! answer with the memo off fails, whatever the golden says; the goldens'
//! own tests hold the shipped way to the reference.

mod common;
mod pyre_api_support;

use bettermemory_core::pyre::memo::{Setting, Way};
use common::parity::Ledger;
use pyre_api_support::{build, codes, line, replaced, Built};
use serde_json::Value;

const METHODS: [&str; 5] = ["match", "fullmatch", "search", "finditer", "findall"];
const MULT: u64 = 6364136223846793005;
const INC: u64 = 1442695040888963407;

/// Every way but off, which is the answer the others are held to.
fn ways() -> Vec<Setting> {
    let mut out = vec![
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

struct Lcg(u64);

impl Lcg {
    fn draw(&mut self) -> usize {
        self.0 = self.0.wrapping_mul(MULT).wrapping_add(INC);
        (self.0 >> 33) as usize
    }
}

fn all_texts(alphabet: &[u32], long: usize) -> Vec<Vec<u32>> {
    let k = alphabet.len();
    let mut out = Vec::new();
    for n in 0..=long {
        for index in 0..k.pow(n as u32) {
            let mut t = vec![0u32; n];
            let mut rest = index;
            for slot in t.iter_mut().rev() {
                *slot = alphabet[rest % k];
                rest /= k;
            }
            out.push(t);
        }
    }
    out
}

fn spelled(v: &Value) -> Vec<u32> {
    match v.as_str() {
        Some(s) => s.chars().map(u32::from).collect(),
        None => codes(v),
    }
}

/// `gen_pyre_inventory.texts` with the row's witnesses.
fn inventory_texts(row: &Value) -> Vec<Vec<u32>> {
    let alphabet = codes(&row["alphabet"]);
    let fragments: Vec<Vec<u32>> = row["fragments"]
        .as_array()
        .expect("fragments")
        .iter()
        .map(spelled)
        .collect();
    let mut out = all_texts(&alphabet, row["long"].as_u64().expect("long") as usize);
    let k = alphabet.len();
    let mut rng = Lcg(row["seed"].as_u64().expect("seed"));
    for _ in 0..300 {
        let mut t = Vec::new();
        for _ in 0..1 + rng.draw() % 5 {
            if !fragments.is_empty() && !rng.draw().is_multiple_of(3) {
                let mut piece = fragments[rng.draw() % fragments.len()].clone();
                if rng.draw().is_multiple_of(4) {
                    let at = rng.draw() % piece.len();
                    piece.remove(at);
                }
                t.extend(piece);
            } else {
                t.push(alphabet[rng.draw() % k]);
            }
        }
        out.push(t);
    }
    out.extend(
        row["witnesses"]
            .as_array()
            .expect("witnesses")
            .iter()
            .map(spelled),
    );
    out
}

/// A row's calls: each text with its windows, and whether the replacing
/// methods run on it.
type Windows = Vec<(isize, isize)>;

struct Calls {
    texts: Vec<(Vec<u32>, Windows)>,
    replace: bool,
}

fn int(row: &Value, key: &str) -> Option<isize> {
    row.get(key).and_then(Value::as_i64).map(|v| v as isize)
}

fn calls(row: &Value) -> Option<Calls> {
    let part = row["part"].as_str().expect("part");
    let full = |t: Vec<u32>| {
        let n = t.len() as isize;
        (t, vec![(0, n), (1, n)])
    };
    let swept = |alphabet: &[u32], long: usize| {
        // Every window of the short texts would make this file run for an
        // hour in a debug build; the whole text, a later `pos` and an
        // earlier `endpos` reach every way of the memo's acceptance.
        let every = all_texts(alphabet, long);
        // The first 300 texts (the shortest, all of them) and an even
        // sample of the rest, about 300 more.
        let stride = every.len().div_ceil(300).max(1);
        every
            .into_iter()
            .enumerate()
            .filter(|(i, _)| *i < 300 || i % stride == 0)
            .map(|(_, t)| t)
            .map(|t| {
                let n = t.len() as isize;
                (t, vec![(0, n), (1, n), (0, n - 1)])
            })
            .collect()
    };
    let texts = match part {
        "exec.sweep" | "api.sweep" => swept(
            &codes(&row["alphabet"]),
            row["long"].as_u64().expect("long") as usize,
        ),
        "api.fuzz" => {
            if row.get("refused").is_some() {
                return None;
            }
            all_texts(&[97, 98], 4)
                .into_iter()
                .map(|t| {
                    let n = t.len() as isize;
                    (t, vec![(0, n), (1, n), (0, n - 1)])
                })
                .collect()
        }
        "exec.kill" | "api.kill" | "api.replace" => {
            let t = codes(&row["text"]);
            let n = t.len() as isize;
            let (p, e) = (
                int(row, "pos").unwrap_or(0),
                int(row, "endpos").unwrap_or(n),
            );
            vec![(t, vec![(p, e), (0, n)])]
        }
        "inventory.pattern" => inventory_texts(row).into_iter().map(full).collect(),
        _ => return None,
    };
    let replace = matches!(part, "api.replace" | "api.fuzz") || row.get("replace").is_some();
    Some(Calls { texts, replace })
}

/// Every answer of `b` over `calls`, one line per text.
fn answers(b: &Built, calls: &Calls) -> Vec<String> {
    calls
        .texts
        .iter()
        .map(|(t, wins)| {
            let mut out = String::new();
            for &(p, e) in wins {
                for m in METHODS {
                    out.push_str(&line(b, t, p, e, m, true));
                    out.push(' ');
                }
            }
            if calls.replace {
                for m in ["subn", "subn_fn", "split"] {
                    for c in [0, 1] {
                        let got = replaced(b, t, m, &[60, 62], c);
                        out.push_str(&format!("{got:?} "));
                    }
                }
            }
            out
        })
        .collect()
}

/// Checks one row; answers the comparisons made.
fn check(row: &Value) -> u64 {
    let Some(calls) = calls(row) else { return 0 };
    let Ok(plain) = build(row) else { return 0 };
    let bytes = plain.bytes;
    let want = answers(
        &Built {
            re: plain.re.clone().with_memo(Setting::OFF),
            bytes,
        },
        &calls,
    );
    let mut compared = 0;
    for way in ways() {
        let b = Built {
            re: plain.re.clone().with_memo(way),
            bytes,
        };
        let got = answers(&b, &calls);
        for (i, (g, w)) in got.iter().zip(&want).enumerate() {
            assert_eq!(
                g, w,
                "{way:?} differs from off: {} text {:?}",
                row["pattern"], calls.texts[i].0
            );
        }
        compared += got.len() as u64;
    }
    compared
}

fn file(name: &str, test: &str, floor: u64) {
    let rows = common::jsonl(name);
    let rows: Vec<&Value> = rows.iter().filter(|r| calls(r).is_some()).collect();
    let mut ledger = Ledger::new(test, name);
    let total = std::sync::atomic::AtomicU64::new(0);
    let chunks: Vec<&[&Value]> = rows.chunks(rows.len().div_ceil(4).max(1)).collect();
    let mut failures = Vec::new();
    std::thread::scope(|s| {
        let handles: Vec<_> = chunks
            .iter()
            .map(|chunk| {
                let total = &total;
                s.spawn(move || {
                    for row in chunk.iter() {
                        total.fetch_add(check(row), std::sync::atomic::Ordering::Relaxed);
                    }
                })
            })
            .collect();
        for h in handles {
            if let Err(e) = h.join() {
                failures.push(e);
            }
        }
    });
    if let Some(e) = failures.pop() {
        std::panic::resume_unwind(e);
    }
    for row in &rows {
        ledger.case(format!("{} {}", row["part"], row["pattern"]), || {});
    }
    let total = total.into_inner();
    eprintln!("{name}: {total} texts compared under {} ways", ways().len());
    assert!(total >= floor, "{total} comparisons, below {floor}");
    ledger.finish(rows.len());
}

#[test]
fn three_ways_on_pyre_exec() {
    file("pyre_exec.jsonl", "three_ways_on_pyre_exec", 10_000);
}

#[test]
fn three_ways_on_pyre_api() {
    file("pyre_api.jsonl", "three_ways_on_pyre_api", 10_000);
}

#[test]
fn three_ways_on_pyre_inventory() {
    file(
        "pyre_inventory.jsonl",
        "three_ways_on_pyre_inventory",
        1_000_000,
    );
}

#[test]
fn three_ways_on_pyre_unicode() {
    file("pyre_unicode.jsonl", "three_ways_on_pyre_unicode", 10_000);
}

#[test]
fn three_ways_on_pyre_python() {
    file("pyre_python.jsonl", "three_ways_on_pyre_python", 10_000);
}
