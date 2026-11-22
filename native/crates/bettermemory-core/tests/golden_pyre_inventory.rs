//! Every pattern of the reference the engine is to run, built from the
//! reference's own text and flags (`pyre_inventory.jsonl` and the `pyre-7`
//! rows of `pyre_unicode.jsonl`, written by `gen_pyre_inventory`): the
//! build must succeed (a refusal fails the test, decision rule 5), the
//! group count, names and tree must be `re`'s, and the five methods over
//! the row's texts, regenerated here by the generator's rule, must give
//! the same lines by count and digest. A row one of whose sites is used
//! through `sub`, `subn` or `split` (`replace`) also runs those over the
//! same texts (`subn` with `<>`, `subn_fn`, `split`, each with the counts
//! `REPLACE_COUNTS`) by count and digest. Each row runs on a thread of its
//! own and fails when it gives no answer within `ROW_LIMIT`, so an engine
//! that never ends fails the test. An `inventory.builder` row builds
//! each input's pattern by the reference's own string code (with the
//! port's `re.escape`) and runs it over the input with every prefix and
//! suffix of `PREFIXES` and `SUFFIXES`. The `inventory.sites` row is read
//! for its shape: every engine site has a pattern row, and each site's
//! methods are written.
//!
//! `pyre_inventory_differential_file` (ignored) reads the file
//! `native/tools/pyre_differential.py inventory` writes and names the
//! first text whose lines differ.

mod common;
mod pyre_api_support;

use std::collections::BTreeSet;
use std::sync::mpsc::{channel, RecvTimeoutError};
use std::time::Duration;

use bettermemory_core::pyre::atom::escape;
use bettermemory_core::pyre::parse::parse;
use bettermemory_core::pyre::regex::Regex;
use common::parity::Ledger;
use pyre_api_support::{build, line, replaced, unspell, Built};
use serde_json::Value;
use sha2::{Digest, Sha256};

const UNIT: &str = "pyre-7";
const JOINED: usize = 300;
const EXHAUSTIVE: usize = 300;
const MULT: u64 = 6364136223846793005;
const INC: u64 = 1442695040888963407;
const METHODS: [&str; 5] = ["match", "fullmatch", "search", "finditer", "findall"];
/// `gen_pyre_inventory.REPLACE_COUNTS`.
const REPLACE_COUNTS: [isize; 2] = [0, 1];
/// The longest one row may take, far above what any row takes here.
const ROW_LIMIT: Duration = Duration::from_secs(600);

/// Runs `check` on `row` on a thread of its own: a panic is passed on, and
/// no answer within `ROW_LIMIT` fails, so an engine that loops fails the
/// row instead of hanging the test.
fn watched(row: &Value, check: fn(&Value)) {
    let owned = row.clone();
    let (tx, rx) = channel();
    let handle = std::thread::spawn(move || {
        check(&owned);
        let _ = tx.send(());
    });
    match rx.recv_timeout(ROW_LIMIT) {
        Ok(()) => handle.join().expect("the row's thread"),
        Err(RecvTimeoutError::Disconnected) => {
            if let Err(panic) = handle.join() {
                std::panic::resume_unwind(panic);
            }
        }
        Err(RecvTimeoutError::Timeout) => panic!(
            "no answer within {ROW_LIMIT:?}: an engine that does not end on {}",
            row.get("pattern").unwrap_or(&row["family"])
        ),
    }
}

/// `gen_pyre_tree.Lcg`.
struct Lcg(u64);

impl Lcg {
    fn draw(&mut self) -> usize {
        self.0 = self.0.wrapping_mul(MULT).wrapping_add(INC);
        (self.0 >> 33) as usize
    }
}

fn text(v: &Value) -> &str {
    v.as_str().unwrap_or_else(|| panic!("a text: {v}"))
}

fn uint(row: &Value, key: &str) -> u64 {
    row[key].as_u64().unwrap_or_else(|| panic!("{key}: {row}"))
}

/// A text as `gen_pyre_inventory._spelled` writes it: a string, or its
/// code points when it holds a surrogate.
fn spelled(v: &Value) -> Vec<u32> {
    match v.as_str() {
        Some(s) => s.chars().map(u32::from).collect(),
        None => pyre_api_support::codes(v),
    }
}

fn list(row: &Value, key: &str) -> Vec<Vec<u32>> {
    row[key]
        .as_array()
        .unwrap_or_else(|| panic!("{key}: {row}"))
        .iter()
        .map(spelled)
        .collect()
}

/// `gen_pyre_inventory._long`.
fn long(k: usize) -> usize {
    let mut n = 0;
    while (0..n + 2).map(|i| k.pow(i as u32)).sum::<usize>() <= EXHAUSTIVE {
        n += 1;
    }
    n
}

/// `gen_pyre_inventory.texts`.
pub fn texts(alphabet: &[u32], fragments: &[Vec<u32>], seed: u64, joined: usize) -> Vec<Vec<u32>> {
    let k = alphabet.len();
    let mut out = Vec::new();
    for n in 0..=long(k) {
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
    let mut rng = Lcg(seed);
    for _ in 0..joined {
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
    out
}

/// The texts of a row: the drawn ones, then its witnesses.
fn row_texts(row: &Value) -> Vec<Vec<u32>> {
    let alphabet = pyre_api_support::codes(&row["alphabet"]);
    assert_eq!(long(alphabet.len()) as u64, uint(row, "long"));
    let mut out = texts(
        &alphabet,
        &list(row, "fragments"),
        uint(row, "seed"),
        JOINED,
    );
    out.extend(list(row, "witnesses"));
    out
}

/// `gen_pyre_inventory._lines` for one text: the windows `(0, n)` and
/// `(1, n)`.
pub fn lines(b: &Built, t: &[u32]) -> [String; 2] {
    let n = t.len() as isize;
    [0, 1].map(|pos| METHODS.map(|m| line(b, t, pos, n, m, false)).join(" "))
}

fn built(row: &Value) -> Built {
    let b = build(row).unwrap_or_else(|e| {
        panic!(
            "the engine refuses a pattern of the reference ({e:?}): {} sites {}",
            row["pattern"], row["sites"]
        )
    });
    assert_eq!(u64::from(b.re.flags()), uint(row, "sre_flags"));
    assert_eq!(b.re.groups() as u64, uint(row, "groups"));
    let names: Vec<Value> =
        b.re.names()
            .iter()
            .map(|(i, n)| {
                serde_json::json!([
                    i,
                    n.iter()
                        .filter_map(|&c| char::from_u32(c))
                        .collect::<String>()
                ])
            })
            .collect();
    assert_eq!(Value::Array(names), row["names"]);
    b
}

fn check_tree(row: &Value) {
    let pattern = unspell(&row["pattern"]);
    let istext = !row["bytes"].as_bool().expect("bytes");
    let flags = row.get("flags").and_then(Value::as_u64).unwrap_or(0) as u32;
    let (tree, _) = parse(&pattern, istext, flags).unwrap_or_else(|e| panic!("{e:?}"));
    let digest = common::hex(&Sha256::digest(tree.dump().as_bytes()));
    assert_eq!(&digest[..16], text(&row["tree"]), "{}", row["pattern"]);
}

fn check_pattern(row: &Value) {
    let b = built(row);
    check_tree(row);
    let mut digest = Sha256::new();
    let mut count = 0u64;
    for t in row_texts(row) {
        for l in lines(&b, &t) {
            digest.update(l.as_bytes());
            digest.update(b"\n");
            count += 1;
        }
    }
    assert!(count > 0, "a row of no text");
    assert_eq!(count, uint(row, "count"), "{}", row["pattern"]);
    assert_eq!(
        &common::hex(&digest.finalize())[..16],
        text(&row["sha16"]),
        "{} (replay: native/tools/pyre_differential.py inventory)",
        row["pattern"]
    );
    if row.get("replace").is_some() {
        check_replace(&b, row);
    }
}

/// `gen_pyre_inventory._replace_lines` over the row's texts.
fn check_replace(b: &Built, row: &Value) {
    for m in row["replace"].as_array().expect("replace") {
        assert!(
            ["sub", "subn", "split"].contains(&text(m)),
            "a replacing method: {m}"
        );
    }
    let mut digest = Sha256::new();
    let mut count = 0u64;
    for t in row_texts(row) {
        let answers: Vec<String> = ["subn", "subn_fn", "split"]
            .iter()
            .flat_map(|m| {
                REPLACE_COUNTS
                    .map(|c| replaced(b, &t, m, &[60, 62], c).unwrap_or_else(|e| panic!("{e}")))
            })
            .collect();
        digest.update(answers.join(" ").as_bytes());
        digest.update(b"\n");
        count += 1;
    }
    assert_eq!(count, uint(row, "replace_count"), "{}", row["pattern"]);
    assert_eq!(
        &common::hex(&digest.finalize())[..16],
        text(&row["replace_sha16"]),
        "{} under sub, subn and split",
        row["pattern"]
    );
}

const PREFIXES: [&str; 7] = ["", "x", "-", " ", ".", "/", "abc "];
const SUFFIXES: [&str; 9] = ["", "x", "-", " ", ".", ".x", "\n", "\r\n", "\nz"];

fn cps(s: &str) -> Vec<u32> {
    s.chars().map(u32::from).collect()
}

/// `gen_pyre_inventory.build_pattern`: the pattern text the reference's own
/// string code builds from `value`, with the port's `re.escape`.
fn build_pattern(family: &str, value: &str) -> Vec<u32> {
    let mut out = Vec::new();
    match family {
        "project_name" => {
            out.extend(cps("(?<![A-Za-z0-9_.-])"));
            for (i, seg) in value.split('-').enumerate() {
                if i > 0 {
                    out.extend(cps("[-_.]"));
                }
                out.extend(escape(&cps(seg)));
            }
            out.extend(cps(r"(?![A-Za-z0-9_-]|\.(?=\S))"));
        }
        "declared_root" => out.extend(escape(&cps(value))),
        "column" => {
            out.extend(cps(r"\b"));
            out.extend(cps(value));
            out.extend(cps(r"\b"));
        }
        "packed_name" => {
            out.extend(cps(r"^[^\n]* "));
            out.extend(escape(&cps(value)));
            out.extend(cps(r"\r?$"));
        }
        other => panic!("a family: {other}"),
    }
    out
}

fn check_builder(row: &Value) {
    let family = text(&row["family"]);
    let flags = uint(row, "flags") as u32;
    let bytes = row["bytes"].as_bool().expect("bytes");
    let mut digest = Sha256::new();
    let mut count = 0u64;
    for value in row["inputs"].as_array().expect("inputs").iter().map(text) {
        let pattern = build_pattern(family, value);
        let re = if bytes {
            let b: Vec<u8> = pattern.iter().map(|&c| c as u8).collect();
            Regex::new_bytes(&b, flags)
        } else {
            Regex::new(&pattern, flags)
        }
        .unwrap_or_else(|e| panic!("the engine refuses {family} of {value:?}: {e:?}"));
        let b = Built { re, bytes };
        for p in PREFIXES {
            for s in SUFFIXES {
                let t = cps(&format!("{p}{value}{s}"));
                let l = METHODS
                    .map(|m| line(&b, &t, 0, t.len() as isize, m, false))
                    .join(" ");
                digest.update(l.as_bytes());
                digest.update(b"\n");
                count += 1;
            }
        }
    }
    assert_eq!(count, uint(row, "count"), "{family}");
    assert_eq!(
        &common::hex(&digest.finalize())[..16],
        text(&row["sha16"]),
        "{family}"
    );
}

fn check_sites(row: &Value, file: &[Value]) {
    let sites = row["sites"].as_array().expect("sites");
    assert!(sites.len() > 100, "{} sites", sites.len());
    let mut held: BTreeSet<String> = BTreeSet::new();
    for r in file
        .iter()
        .chain(common::jsonl("pyre_unicode.jsonl").iter())
    {
        if r["part"] == "inventory.pattern" {
            for s in r["sites"].as_array().expect("sites") {
                held.insert(text(s).to_string());
            }
        }
    }
    let mut last = String::new();
    for site in sites {
        let id = text(&site[0]);
        assert!(id > last.as_str(), "sites in order: {id}");
        last = id.to_string();
        match text(&site[1]) {
            "engine" => assert!(
                held.contains(id),
                "an engine site with no pattern row: {id}"
            ),
            "hand" | "builder" => {}
            other => panic!("a kind: {other}"),
        }
        assert_eq!(text(&site[2]).len(), 8);
        for m in text(&site[3]).split_whitespace() {
            let name = m.split('/').next().unwrap_or_default();
            assert!(
                METHODS.contains(&name) || ["sub", "subn", "split"].contains(&name),
                "a method of {id}: {m}"
            );
        }
    }
}

#[test]
fn pyre_inventory_every_row() {
    let mut file = common::jsonl("pyre_inventory.jsonl");
    let header = file.remove(0);
    assert_eq!(header["part"], "header");
    let mut ledger = Ledger::new("pyre_inventory_every_row", "pyre_inventory.jsonl");
    for row in &file {
        let label = format!(
            "{} {}",
            text(&row["part"]),
            row.get("pattern").unwrap_or(&Value::Null)
        );
        match text(&row["part"]) {
            "inventory.sites" => ledger.case(label, || check_sites(row, &file)),
            "inventory.pattern" => ledger.case(label, || watched(row, check_pattern)),
            "inventory.builder" => ledger.case(label, || watched(row, check_builder)),
            other => panic!("a part this test does not know: {other}"),
        }
    }
    common::assert_count(
        "pyre_inventory.jsonl header",
        file.len(),
        uint(&header, "rows") as usize,
    );
    ledger.finish(file.len());
}

/// The patterns that fold case or read past ASCII.
#[test]
fn pyre_inventory_unicode_every_row() {
    let file = common::jsonl("pyre_unicode.jsonl");
    let rows: Vec<&Value> = file
        .iter()
        .filter(|r| r["part"] == "inventory.pattern" || r["part"] == "inventory.builder")
        .collect();
    assert!(!rows.is_empty(), "no inventory row in pyre_unicode.jsonl");
    assert_eq!(
        file[0]["rows"][UNIT]
            .as_u64()
            .expect("the header counts pyre-7") as usize,
        rows.len()
    );
    let mut ledger = Ledger::new("pyre_inventory_unicode_every_row", "pyre_unicode.jsonl");
    for row in &rows {
        let label = format!(
            "{} {}",
            text(&row["part"]),
            row.get("pattern").unwrap_or(&row["family"])
        );
        if row["part"] == "inventory.builder" {
            ledger.case(label, || watched(row, check_builder));
        } else {
            ledger.case(label, || watched(row, check_pattern));
        }
    }
    ledger.finish(rows.len());
}

/// The replay: `BM_PYRE_INVENTORY_DIFF=FILE`, a file of
/// `pyre_differential.py inventory`, one JSON line per text (`pattern`,
/// `flags`, `bytes`, `text` as code points, `lines`), compared line by line;
/// the first text that differs is named.
#[test]
#[ignore = "needs a file written by native/tools/pyre_differential.py inventory"]
fn pyre_inventory_differential_file() {
    let path = std::env::var("BM_PYRE_INVENTORY_DIFF").expect("BM_PYRE_INVENTORY_DIFF");
    let data = std::fs::read_to_string(path).expect("the file");
    let mut current: Option<(Value, Built)> = None;
    let mut count = 0usize;
    for raw in data.lines() {
        let row: Value = serde_json::from_str(raw).expect("a JSON line");
        let same = current.as_ref().is_some_and(|(head, _)| {
            head["pattern"] == row["pattern"] && head["flags"] == row["flags"]
        });
        if !same {
            let b = build(&row).unwrap_or_else(|e| panic!("{e:?}: {}", row["pattern"]));
            current = Some((row.clone(), b));
        }
        let (_, b) = current.as_ref().expect("built");
        let t = pyre_api_support::codes(&row["text"]);
        let want: Vec<&str> = row["lines"]
            .as_array()
            .expect("lines")
            .iter()
            .map(text)
            .collect();
        let got = lines(b, &t);
        assert_eq!(
            got.as_slice(),
            want.as_slice(),
            "pattern {} flags {} text {:?}",
            row["pattern"],
            row["flags"],
            t
        );
        count += 1;
    }
    assert!(count > 0, "an empty file");
    eprintln!("{count} texts agree");
}
