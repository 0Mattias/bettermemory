//! Unit pyre-1's rows of the three `pyre` fixtures (`gen_pyre`): the case
//! folding `_sre` uses and the test of one literal.
//!
//! Each file's header says how many rows each unit of the engine wrote; a
//! test here checks every row of unit `pyre-1` and fails when the number
//! checked is zero or is not the header's, and when the header's counts do
//! not add up to the file's rows.
//!
//! - `pyre.jsonl` (not gated): ASCII literals over ASCII text.
//! - `pyre_unicode.jsonl` (Unicode): every code point through the lower
//!   case, the upper case, the cased test and the three categories, by
//!   digest per block; the extra cases in full; the exact set every cased
//!   literal matches; the kills past ASCII.
//! - `pyre_python.jsonl` (Python): the interpreter the engine was read from,
//!   one row per fact.
//!
//! The sets of the two literal sweeps are taken by calling `Lit::matches`.
//! A release build asks it of every code point for every pattern (about six
//! billion calls, on every core). A debug build asks it of the candidates
//! only (every cased code point, every code point below U+3000, every 61st
//! code point and the pattern itself), unless `BM_PYRE_FULL` is set. That
//! leaves out what `Lit::matches` answers for the other code points, so
//! `tools/parity.py` runs this test once more in a release build (step 1b),
//! and every gated run asks every code point.

mod common;

use bettermemory_core::pyre;
use bettermemory_core::pyre::fold::{
    extra_cases, sre_iscased, sre_lower, sre_upper, Lit, IGNORECASE, UNICODE,
};
use bettermemory_core::unicode;
use common::parity::{self, Ledger};
use common::{assert_count, golden_path, jsonl};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

const UNIT: &str = "pyre-1";
const N: u32 = 0x110000;
const BLOCK: u32 = 0x4000;

/// The flags `_compile` reads for a `str` pattern under `flags` as
/// `re.compile` takes them: the row's `sre_flags`, which must carry
/// UNICODE beside the flags given.
fn sre_flags(row: &Value) -> u32 {
    let flags = row["flags"].as_u64().expect("flags") as u32;
    assert!(flags == 0 || flags == IGNORECASE, "flags {flags}");
    let sre_flags = row["sre_flags"].as_u64().expect("sre_flags") as u32;
    assert_eq!(sre_flags, flags | UNICODE, "a str pattern carries UNICODE");
    sre_flags
}

/// A literal under IGNORECASE on a `str` pattern.
fn folded(p: u32) -> Lit {
    Lit::new(p, IGNORECASE | UNICODE).expect("the Unicode fold")
}

/// The header of `name` and the rows unit `pyre-1` wrote into it.
fn unit_rows(name: &str) -> (Value, Vec<Value>) {
    let mut lines = jsonl(name);
    let header = lines.remove(0);
    assert_eq!(header["part"], "header", "{name}");
    assert_eq!(header["fixture"], name.trim_end_matches(".jsonl"), "{name}");
    let counts = header["rows"].as_object().expect("rows");
    let total: u64 = counts.values().map(|n| n.as_u64().expect("a count")).sum();
    assert_eq!(
        total as usize,
        lines.len(),
        "{name}: the header's counts are not the file's rows"
    );
    let mine: Vec<Value> = lines.into_iter().filter(|r| r["unit"] == UNIT).collect();
    assert_eq!(
        counts[UNIT].as_u64().expect("a count") as usize,
        mine.len(),
        "{name}: rows of {UNIT}"
    );
    (header, mine)
}

fn text(value: &Value) -> Vec<u32> {
    match value {
        Value::String(s) => unicode::cps(s),
        other => other
            .as_array()
            .expect("a text")
            .iter()
            .map(|c| c.as_u64().expect("a code point") as u32)
            .collect(),
    }
}

fn numbers(value: &Value) -> Vec<u32> {
    value
        .as_array()
        .expect("a list")
        .iter()
        .map(|c| c.as_u64().expect("a number") as u32)
        .collect()
}

fn hex(digest: &[u8]) -> String {
    digest.iter().map(|b| format!("{b:02x}")).collect()
}

fn digest16(data: &[u8]) -> String {
    hex(&Sha256::digest(data)[..8])
}

fn full_sweeps() -> bool {
    !cfg!(debug_assertions) || std::env::var_os("BM_PYRE_FULL").is_some()
}

/// The code points a debug build asks a literal of.
fn candidates() -> Vec<u32> {
    (0..N)
        .filter(|&c| c < 0x3000 || c % 61 == 0 || sre_iscased(c))
        .collect()
}

/// For each pattern under IGNORECASE, the code points it matches, ascending:
/// of every code point, or of `among` and the pattern itself.
fn matched_sets(patterns: &[u32], among: Option<&[u32]>) -> Vec<Vec<u32>> {
    let one = |p: u32| -> Vec<u32> {
        let lit = folded(p);
        match among {
            None => (0..N).filter(|&c| lit.matches(c)).collect(),
            Some(list) => {
                let mut out: Vec<u32> = list.iter().copied().filter(|&c| lit.matches(c)).collect();
                if lit.matches(p) {
                    out.push(p);
                }
                out.sort_unstable();
                out.dedup();
                out
            }
        }
    };
    let workers = std::thread::available_parallelism().map_or(1, |n| n.get());
    let per = patterns.len().div_ceil(workers).max(1);
    std::thread::scope(|scope| {
        let handles: Vec<_> = patterns
            .chunks(per)
            .map(|chunk| scope.spawn(move || chunk.iter().map(|&p| one(p)).collect::<Vec<_>>()))
            .collect();
        handles
            .into_iter()
            .flat_map(|h| h.join().expect("a worker"))
            .collect()
    })
}

/// A set as `gen_pyre._set_bytes` writes it.
fn set_bytes(out: &mut Vec<u8>, pattern: u32, members: &[u32]) {
    out.extend_from_slice(&pattern.to_le_bytes());
    out.extend_from_slice(&(members.len() as u32).to_le_bytes());
    for m in members {
        out.extend_from_slice(&m.to_le_bytes());
    }
}

/// What `gen_pyre._fold_sets_row` records of `patterns` and their sets.
fn sets_row(patterns: &[u32], sets: &[Vec<u32>], per: usize) -> Value {
    let mut whole = Sha256::new();
    let mut blocks = Vec::new();
    for (ps, ss) in patterns.chunks(per).zip(sets.chunks(per)) {
        let mut data = Vec::new();
        for (p, members) in ps.iter().zip(ss) {
            set_bytes(&mut data, *p, members);
        }
        whole.update(&data);
        blocks.push(digest16(&data));
    }
    json!({
        "patterns": patterns.len(),
        "members": sets.iter().map(Vec::len).sum::<usize>(),
        "largest": sets.iter().map(Vec::len).max().unwrap_or(0),
        "per": per,
        "blocks": blocks,
        "sha256": hex(&whole.finalize()),
    })
}

fn assert_sets_row(row: &Value, patterns: &[u32], sets: &[Vec<u32>]) {
    let per = row["per"].as_u64().expect("per") as usize;
    let got = sets_row(patterns, sets, per);
    let (want_blocks, got_blocks) = (
        row["blocks"].as_array().expect("blocks"),
        got["blocks"].as_array().unwrap(),
    );
    if let Some(i) = (0..want_blocks.len().max(got_blocks.len()))
        .find(|&i| want_blocks.get(i) != got_blocks.get(i))
    {
        let from = i * per;
        let shown: Vec<String> = patterns
            .iter()
            .zip(sets)
            .skip(from)
            .take(per)
            .map(|(p, s)| format!("{p:X}:{s:X?}"))
            .collect();
        panic!(
            "{}: block {i} differs; the port's sets there: {}",
            row["part"],
            shown.join(" ")
        );
    }
    for key in ["patterns", "members", "largest", "sha256"] {
        assert_eq!(got[key], row[key], "{}: {key}", row["part"]);
    }
}

/// One `kill` row: a literal, or a negated one, under one flag set.
fn check_kill(row: &Value) {
    let flags = sre_flags(row);
    let literal = row["literal"].as_u64().expect("literal") as u32;
    let negated = row["negated"].as_bool().expect("negated");
    let pattern = text(&row["pattern"]);
    // The pattern is the literal, escaped or not, or `[^` literal `]`.
    let written = if negated {
        pattern[pattern.len() - 2]
    } else {
        *pattern.last().expect("a pattern")
    };
    assert_eq!(written, literal, "pattern {pattern:X?}");
    let lit = Lit::new(literal, flags).expect("no flag set of a row is refused");
    let texts = row["texts"].as_array().expect("texts");
    let wanted = row["fullmatch"].as_array().expect("fullmatch");
    assert_eq!(texts.len(), wanted.len());
    for (t, want) in texts.iter().zip(wanted) {
        let t = text(t);
        // One literal takes exactly one character.
        let got = t.len() == 1 && lit.matches(t[0]) != negated;
        assert_eq!(
            got,
            want.as_bool().expect("a bool"),
            "{} fullmatch of {t:X?} by pattern {pattern:X?} flags {flags}",
            row["kills"]
        );
    }
    let matching: Vec<u32> = (0..N).filter(|&c| lit.matches(c)).collect();
    match (row.get("set"), row.get("unmatched")) {
        (Some(set), None) => {
            assert!(!negated);
            assert_eq!(matching, numbers(set), "set of pattern {pattern:X?}");
        }
        (None, Some(unmatched)) => {
            assert!(negated);
            assert_eq!(
                matching,
                numbers(unmatched),
                "unmatched of pattern {pattern:X?}"
            );
        }
        (None, None) => {}
        _ => panic!("a kill row has a set or the unmatched, not both"),
    }
}

#[test]
fn pyre_every_case() {
    let name = "pyre.jsonl";
    let (header, rows) = unit_rows(name);
    assert_eq!(
        header["reproduces"],
        json!({"python": pyre::PYTHON, "unicode": pyre::UNICODE, "sre_magic": pyre::SRE_MAGIC})
    );
    let cap = header["cap_bytes"].as_u64().expect("cap_bytes");
    assert_eq!(cap, 150_000);
    let size = std::fs::metadata(golden_path(name))
        .expect("the file")
        .len();
    assert!(size <= cap, "{name} is {size} bytes, over its cap of {cap}");
    let mut checked = 0;
    for row in &rows {
        match row["part"].as_str().expect("part") {
            "ascii" => {
                let flags = sre_flags(row);
                let sets = row["sets"].as_array().expect("sets");
                assert_eq!(sets.len(), 0x80);
                for (p, want) in sets.iter().enumerate() {
                    let lit = Lit::new(p as u32, flags).expect("the Unicode fold");
                    let got: Vec<u32> = (0..0x80).filter(|&c| lit.matches(c)).collect();
                    assert_eq!(got, numbers(want), "ASCII literal {p:#x} flags {flags}");
                }
            }
            "kill" => {
                assert!(row.get("set").is_none() && row.get("unmatched").is_none());
                check_kill(row);
            }
            other => panic!("{name}: unit {UNIT} has no part {other}"),
        }
        checked += 1;
    }
    assert_count(name, checked, rows.len());
}

/// One value per code point of `what`: the port's side of a sweep.
fn value_of(what: &str, c: u32) -> u32 {
    match what {
        "lower" => sre_lower(c),
        "upper" => sre_upper(c),
        "iscased" => u32::from(sre_iscased(c)),
        "word" => u32::from(unicode::is_word(c)),
        "digit" => u32::from(unicode::is_re_digit(c)),
        "space" => u32::from(unicode::is_space(c)),
        other => panic!("no sweep of {other}"),
    }
}

fn check_sweep(row: &Value) {
    let what = row["what"].as_str().expect("what");
    let wide = match row["bytes"].as_u64().expect("bytes") {
        4 => true,
        1 => false,
        other => panic!("{other} bytes per code point"),
    };
    assert_eq!(wide, matches!(what, "lower" | "upper"), "{what}");
    let mut wanted = Vec::new();
    for item in row["blocks"].as_array().expect("blocks") {
        match item {
            Value::String(digest) => wanted.push(digest.clone()),
            run => {
                let n = run[1].as_u64().expect("a run length");
                assert!(n >= 2);
                for _ in 0..n {
                    wanted.push(run[0].as_str().expect("a digest").to_owned());
                }
            }
        }
    }
    assert_eq!(wanted.len(), (N / BLOCK) as usize, "{what}: blocks");
    let mut whole = Sha256::new();
    let mut count = 0u64;
    for (i, want) in wanted.iter().enumerate() {
        let lo = i as u32 * BLOCK;
        let mut data = Vec::with_capacity(BLOCK as usize * 4);
        for c in lo..lo + BLOCK {
            let v = value_of(what, c);
            if wide {
                data.extend_from_slice(&v.to_le_bytes());
                count += u64::from(v != c);
            } else {
                data.push(v as u8);
                count += u64::from(v);
            }
        }
        whole.update(&data);
        assert_eq!(
            &digest16(&data),
            want,
            "{what}: block {lo:#x}..{:#x}",
            lo + BLOCK
        );
    }
    assert_eq!(
        count,
        row["count"].as_u64().expect("count"),
        "{what}: count"
    );
    assert_eq!(hex(&whole.finalize()), row["sha256"], "{what}: sha256");
}

fn check_extra_cases(row: &Value) {
    assert_eq!(row["source"], "re._casefix._EXTRA_CASES");
    let table = row["table"].as_array().expect("table");
    let mut keys = Vec::new();
    for entry in table {
        let key = entry[0].as_u64().expect("a key") as u32;
        // The partners in the interpreter's order.
        assert_eq!(
            extra_cases(key),
            numbers(&entry[1]),
            "extra cases of {key:#x}"
        );
        keys.push(key);
    }
    assert!(keys.windows(2).all(|w| w[0] < w[1]), "keys ascending");
    let with_some: Vec<u32> = (0..N).filter(|&c| !extra_cases(c).is_empty()).collect();
    assert_eq!(with_some, keys, "the code points that have extra cases");
}

/// The counts `gen_pyre._fold_facts` takes, from the port.
fn facts() -> Value {
    let count = |f: &dyn Fn(u32) -> bool| (0..N).filter(|&c| f(c)).count();
    let mut lower_of_another = vec![false; N as usize];
    for c in 0..N {
        if sre_lower(c) != c {
            lower_of_another[sre_lower(c) as usize] = true;
        }
    }
    let keys: Vec<u32> = (0..N).filter(|&c| !extra_cases(c).is_empty()).collect();
    let pairs = || {
        keys.iter()
            .flat_map(|&k| extra_cases(k).iter().map(move |&c| (k, c)))
    };
    let members = || {
        keys.iter()
            .flat_map(|&k| std::iter::once(k).chain(extra_cases(k).iter().copied()))
    };
    json!({
        "cased": count(&sre_iscased),
        "cased_past_bmp": count(&|c| c >= 0x10000 && sre_iscased(c)),
        "cased_surrogates": count(&|c| (0xD800..0xE000).contains(&c) && sre_iscased(c)),
        "cased_not_lower_or_upper_differs":
            count(&|c| sre_iscased(c) != (sre_lower(c) != c || sre_upper(c) != c)),
        "lower_not_first_of_str_lower": count(&|c| unicode::lower(&[c]).first() != Some(&sre_lower(c))),
        "cased_lowering_to_uncased": count(&|c| sre_iscased(c) && !sre_iscased(sre_lower(c))),
        "lower_is_upper_not_itself": count(&|c| sre_lower(c) == sre_upper(c) && sre_lower(c) != c),
        "uncased_lower_of_another": count(&|c| lower_of_another[c as usize] && !sre_iscased(c)),
        "extra_keys": keys.len(),
        "extra_partners": pairs().count(),
        "extra_largest": members().max(),
        "extra_not_symmetric": pairs().filter(|&(k, c)| !extra_cases(c).contains(&k)).count(),
        "extra_not_cased": members().filter(|&c| !sre_iscased(c)).count(),
        "extra_not_fixed_by_lower": members().filter(|&c| sre_lower(c) != c).count(),
    })
}

/// The uncased patterns of the `uncased_literals` row, by its rule.
fn uncased_patterns(below: u32, seeded: usize, seed: u64) -> Vec<u32> {
    let mut out: Vec<u32> = (0..below).filter(|&p| !sre_iscased(p)).collect();
    let mut seen: std::collections::HashSet<u32> = out.iter().copied().collect();
    let (mut x, mut draw, mut kept) = (seed, 0u64, 0);
    while kept < seeded {
        x = x
            .wrapping_mul(6364136223846793005)
            .wrapping_add(1442695040888963407);
        let p = ((x >> 33) % if draw % 2 == 0 { 0x10000 } else { u64::from(N) }) as u32;
        draw += 1;
        if !sre_iscased(p) && seen.insert(p) {
            out.push(p);
            kept += 1;
        }
    }
    out
}

const RULE: &str = "x = (x * 6364136223846793005 + 1442695040888963407) mod 2**64, from the \
seed; draw i (from 0) is (x >> 33) mod 0x10000 when i is even and mod 0x110000 when i is odd; \
a draw is kept when it is uncased and new";

#[test]
fn pyre_unicode_every_case() {
    let name = "pyre_unicode.jsonl";
    let (header, rows) = unit_rows(name);
    // The Unicode that made the file; under another than the port's a
    // differing row is set aside, named (class ii).
    assert_eq!(
        header["unidata_version"],
        parity::provenance().unicode.as_str()
    );
    assert_eq!(header["code_points"], N);
    assert_eq!(header["block"], BLOCK);
    let full = full_sweeps();
    let among = (!full).then(candidates);
    let mut ledger = Ledger::new("pyre_unicode_every_case", name);
    let mut parts: std::collections::BTreeMap<String, usize> = Default::default();
    for row in &rows {
        let part = row["part"].as_str().expect("part");
        *parts.entry(part.to_owned()).or_default() += 1;
        match part {
            "sweep" => ledger.case(format!("sweep {}", row["what"]), || check_sweep(row)),
            "extra_cases" => ledger.case("extra_cases", || check_extra_cases(row)),
            "facts" => ledger.case("facts", || {
                let mut want = row.clone();
                let want = want.as_object_mut().expect("an object");
                want.remove("part");
                want.remove("unit");
                assert_eq!(facts(), Value::Object(want.clone()));
            }),
            "cased_literals" => ledger.case("cased_literals", || {
                let patterns: Vec<u32> = (0..N).filter(|&c| sre_iscased(c)).collect();
                let sets = matched_sets(&patterns, among.as_deref());
                assert_sets_row(row, &patterns, &sets);
            }),
            "uncased_literals" => ledger.case("uncased_literals", || {
                assert_eq!(row["rule"], RULE);
                let patterns = uncased_patterns(
                    row["below"].as_u64().expect("below") as u32,
                    row["seeded"].as_u64().expect("seeded") as usize,
                    row["seed"].as_u64().expect("seed"),
                );
                let sets = matched_sets(&patterns, among.as_deref());
                assert_sets_row(row, &patterns, &sets);
            }),
            "kill" => ledger.case(format!("kill {}", row["pattern"]), || check_kill(row)),
            other => panic!("{name}: unit {UNIT} has no part {other}"),
        }
    }
    eprintln!(
        "{name}: {parts:?}; literal sets over {}",
        if full {
            "every code point".to_owned()
        } else {
            format!(
                "{} candidates (a debug build; BM_PYRE_FULL or a release build reads every code point)",
                among.as_ref().map_or(0, Vec::len)
            )
        }
    );
    for part in [
        "sweep",
        "extra_cases",
        "facts",
        "cased_literals",
        "uncased_literals",
        "kill",
    ] {
        assert!(parts.contains_key(part), "{name}: no {part} row");
    }
    ledger.finish(rows.len());
}

#[test]
fn pyre_python_every_case() {
    let name = "pyre_python.jsonl";
    let (_, rows) = unit_rows(name);
    // The engine's pins are the crate's: the Python the port reproduces and
    // the Unicode its tables carry.
    assert_eq!(pyre::PYTHON, parity::PORT_PYTHON);
    assert_eq!(pyre::UNICODE, unicode::UNIDATA_VERSION);
    // Which files are digested, and in what order, is the generator's
    // (ascending by name), not the interpreter's: held under every Python.
    let files: Vec<&str> = rows
        .iter()
        .filter(|r| r["part"] == "source")
        .map(|r| r["file"].as_str().expect("file"))
        .collect();
    let names: Vec<&str> = pyre::RE_SOURCES.iter().map(|&(file, _)| file).collect();
    assert_eq!(names, files, "RE_SOURCES in the order of the rows");
    assert!(names.windows(2).all(|w| w[0] < w[1]), "{names:?} ascending");
    // One fact a row: under another release each is compared or set aside
    // alone.
    let mut ledger = Ledger::new("pyre_python_every_case", name);
    let mut parts: std::collections::BTreeSet<&str> = Default::default();
    for row in &rows {
        let part = row["part"].as_str().expect("part");
        parts.insert(part);
        let value = &row["value"];
        match part {
            "python" => ledger.case("python", || assert_eq!(*value, pyre::PYTHON)),
            "unicode" => ledger.case("unicode", || assert_eq!(*value, pyre::UNICODE)),
            "sre_magic" => ledger.case("sre_magic", || assert_eq!(*value, pyre::SRE_MAGIC)),
            "sre_codesize" => {
                ledger.case("sre_codesize", || assert_eq!(*value, pyre::SRE_CODESIZE))
            }
            "source" => {
                let file = row["file"].as_str().expect("file");
                let (_, digest) = pyre::RE_SOURCES[names.iter().position(|&n| n == file).unwrap()];
                ledger.case(format!("source {file}"), || {
                    assert_eq!(row["sha256"], digest, "{file}")
                });
            }
            other => panic!("{name}: unit {UNIT} has no part {other}"),
        }
    }
    assert_eq!(
        parts.into_iter().collect::<Vec<_>>(),
        ["python", "source", "sre_codesize", "sre_magic", "unicode"],
        "{name}: the parts"
    );
    ledger.finish(rows.len());
}

/// The whole differential, value by value: the file `BM_PYRE_FOLD_DIFF`
/// names, written by `native/tools/pyre_differential.py fold FILE` under the
/// reference Python. It holds, for every code point, the six values of the
/// sweeps, and for every cased code point as a pattern under IGNORECASE the
/// set `re` matches. The first code point that differs is printed for each.
#[test]
#[ignore]
fn pyre_fold_differential_file() {
    let path = std::env::var("BM_PYRE_FOLD_DIFF").expect("BM_PYRE_FOLD_DIFF");
    let data = std::fs::read(&path).expect("the file");
    let end = data.iter().position(|&b| b == b'\n').expect("a first line");
    let first = String::from_utf8_lossy(&data[..end]).into_owned();
    assert!(first.starts_with("pyre fold 1 "), "{first}");
    let mut at = end + 1;
    let word = |at: &mut usize| {
        let v = u32::from_le_bytes(data[*at..*at + 4].try_into().unwrap());
        *at += 4;
        v
    };
    let mut mismatches = 0u64;
    for what in ["lower", "upper"] {
        let mut shown = false;
        for c in 0..N {
            let want = word(&mut at);
            if value_of(what, c) != want {
                mismatches += 1;
                if !shown {
                    eprintln!(
                        "{what} of U+{c:04X}: the port {:#x}, re {want:#x}",
                        value_of(what, c)
                    );
                    shown = true;
                }
            }
        }
    }
    for what in ["iscased", "word", "digit", "space"] {
        let mut shown = false;
        for c in 0..N {
            let want = u32::from(data[at]);
            at += 1;
            if value_of(what, c) != want {
                mismatches += 1;
                if !shown {
                    eprintln!(
                        "{what} of U+{c:04X}: the port {}, re {want}",
                        value_of(what, c)
                    );
                    shown = true;
                }
            }
        }
    }
    let patterns: Vec<u32> = (0..N).filter(|&c| sre_iscased(c)).collect();
    let sets = matched_sets(&patterns, None);
    let n = word(&mut at) as usize;
    assert_eq!(n, patterns.len(), "the number of cased patterns");
    let (mut pairs, mut shown) = (0u64, false);
    for (p, got) in patterns.iter().zip(&sets) {
        assert_eq!(word(&mut at), *p, "a cased pattern");
        let len = word(&mut at) as usize;
        let want: Vec<u32> = (0..len).map(|_| word(&mut at)).collect();
        pairs += u64::from(N);
        if *got != want {
            mismatches += 1;
            if !shown {
                eprintln!("literal U+{p:04X}: the port {got:X?}, re {want:X?}");
                shown = true;
            }
        }
    }
    assert_eq!(at, data.len(), "the whole file was read");
    eprintln!(
        "{first}: 6 values of {N} code points, {} cased literals over {pairs} pairs, \
         {mismatches} mismatches",
        patterns.len()
    );
    assert_eq!(mismatches, 0);
}
