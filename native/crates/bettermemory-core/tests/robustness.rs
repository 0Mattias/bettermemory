//! Input from rows, payloads, head files and origin files is data an
//! attacker may have written: no public entry point may panic on it (the
//! test profile keeps debug assertions, so an overflow panics here too).
//! Also: `next_row` past i64::MAX, key bytes kept out of every `Debug`,
//! the fingerprint guard, and the nesting limit at [`json::MAX_DEPTH`].
//! Seqs at the ends of i64 against the reference's reports are cases of
//! the golden `log_keyring.jsonl` (`golden_log_keyring.rs`).

use bettermemory_core::datetime::{self, DateTime};
use bettermemory_core::json::{self, DumpOptions, MAX_DEPTH};
use bettermemory_core::log::{self, Key, KeyRing, LogRow};
use bettermemory_core::pyvalue::{self, PyError, PyValue};
use bettermemory_core::{float_repr, lexicon, unicode};

#[test]
fn next_row_past_i64_max_is_overflow_error() {
    let key = [7u8; 32];
    let payload = PyValue::Dict(vec![]);
    let first = log::next_row(None, &key, "note", &payload, None, "t").expect("row 1");
    assert_eq!(first.seq, 1);
    let last = LogRow {
        seq: i64::MAX - 1,
        ..first
    };
    let at_max = log::next_row(Some(&last), &key, "note", &payload, None, "t").expect("max");
    assert_eq!(at_max.seq, i64::MAX);
    assert_eq!(
        log::next_row(Some(&at_max), &key, "note", &payload, None, "t"),
        Err(PyError::OverflowError)
    );
}

#[test]
fn no_debug_text_spells_key_bytes() {
    let bytes: Vec<u8> = (0..32u8)
        .map(|i| i.wrapping_mul(37).wrapping_add(11))
        .collect();
    let mut ring = KeyRing {
        current: Some(Key::new(bytes.clone())),
        ..KeyRing::default()
    };
    ring.retire(Key::new(bytes.iter().map(|b| b ^ 0x5A).collect()));
    let texts = [
        format!("{ring:?}"),
        format!("{ring:#?}"),
        format!("{:?}", Key::new(bytes.clone())),
        format!("{:?}", ring.held_keys()),
        format!("{:?}", ring.key_for_fingerprint(&log::fingerprint(&bytes))),
    ];
    for key in [bytes.clone(), bytes.iter().map(|b| b ^ 0x5A).collect()] {
        let lower: String = key.iter().map(|b| format!("{b:02x}")).collect();
        let decimal = format!("{key:?}");
        let decimal_tail = decimal
            .trim_start_matches('[')
            .trim_end_matches(']')
            .to_owned();
        let spellings = [
            lower.clone(),
            lower.to_uppercase(),
            decimal_tail,
            key.iter()
                .map(|b| format!("{b:#x}"))
                .collect::<Vec<_>>()
                .join(", "),
            key[..8]
                .iter()
                .map(|b| b.to_string())
                .collect::<Vec<_>>()
                .join(",\n"),
            String::from_utf8_lossy(&key).into_owned(),
        ];
        for text in &texts {
            assert!(text.contains("bytes"), "{text}");
            for spelling in &spellings {
                assert!(
                    !text.contains(spelling.as_str()),
                    "{text} spells {spelling}"
                );
            }
            // No run of four byte values from the key in any spelling.
            for w in key.windows(4) {
                let run = w
                    .iter()
                    .map(|b| b.to_string())
                    .collect::<Vec<_>>()
                    .join(", ");
                assert!(!text.contains(&run), "{text} holds {run}");
            }
        }
    }
}

#[test]
fn a_name_that_is_no_fingerprint_matches_no_key() {
    let key = vec![3u8; 32];
    let fp = log::fingerprint(&key);
    let mut ring = KeyRing::default();
    ring.retire(Key::new(key.clone()));
    assert!(log::is_fingerprint(&fp));
    assert!(ring.key_for_fingerprint(&fp).is_some());
    for name in [
        "",
        &fp[..16],
        &fp[..63],
        &format!("{fp}0"),
        &fp.to_uppercase(),
        &format!("{}*", &fp[..63]),
        &format!("{}../", &fp[..61]),
    ] {
        assert!(!log::is_fingerprint(name), "{name}");
        assert!(ring.key_for_fingerprint(name).is_none(), "{name}");
    }
}

fn nested_list(depth: usize) -> PyValue {
    let mut v = PyValue::List(vec![]);
    for _ in 1..depth {
        v = PyValue::List(vec![v]);
    }
    v
}

fn nested_dict(depth: usize) -> PyValue {
    let mut v = PyValue::Dict(vec![]);
    for _ in 1..depth {
        v = PyValue::Dict(vec![(PyValue::str("k"), v)]);
    }
    v
}

#[test]
fn nesting_up_to_max_depth_is_read_and_written_and_past_it_is_recursion_error() {
    for depth in [MAX_DEPTH, MAX_DEPTH + 1, 100_000] {
        let ok = depth <= MAX_DEPTH;
        let lists = format!("{}{}", "[".repeat(depth), "]".repeat(depth));
        let dicts = format!("{}1{}", "{\"k\":".repeat(depth), "}".repeat(depth));
        for text in [&lists, &dicts] {
            let got = json::loads(&unicode::cps(text));
            assert_eq!(got.is_ok(), ok, "loads at {depth}");
            if !ok {
                assert_eq!(got.err(), Some(PyError::RecursionError));
            }
        }
        for value in [nested_list(depth), nested_dict(depth)] {
            for options in [
                DumpOptions::DEFAULT,
                DumpOptions::SORTED_ASCII_COMPACT,
                DumpOptions {
                    indent: Some(2),
                    ..DumpOptions::DEFAULT
                },
            ] {
                let got = json::dumps(&value, options);
                assert_eq!(got.is_ok(), ok, "dumps at {depth}");
            }
            assert_eq!(pyvalue::repr(&value).is_ok(), ok, "repr at {depth}");
        }
        // The outer payload dict is one level of its own.
        let payload = PyValue::Dict(vec![(PyValue::str("p"), nested_list(depth - 1))]);
        assert_eq!(
            json::canonical_payload(&payload).is_ok(),
            ok,
            "payload at {depth}"
        );
        // An origin file nested this deep: verify_chain raises as the
        // reference does, or reads it.
        let ring = KeyRing {
            origin_file: Some(dicts.clone().into_bytes()),
            head_file: Some(lists.clone().into_bytes()),
            ..KeyRing::default()
        };
        let got = log::verify_chain(&[], &ring, &"0".repeat(64));
        assert_eq!(got.is_ok(), ok, "verify_chain at {depth}");
    }
}

/// A small deterministic generator: no crate, no clock.
struct Rng(u64);

impl Rng {
    fn next(&mut self) -> u64 {
        self.0 ^= self.0 << 13;
        self.0 ^= self.0 >> 7;
        self.0 ^= self.0 << 17;
        self.0
    }
    fn below(&mut self, n: u64) -> u64 {
        self.next() % n
    }
}

const PIECES: &[&str] = &[
    "{",
    "}",
    "[",
    "]",
    ",",
    ":",
    "\"",
    "\\",
    "\\u",
    "\\ud800",
    "\\udc00",
    "1",
    "-",
    "0",
    "e",
    "E+",
    ".",
    "1e999999999",
    "-0",
    "NaN",
    "Infinity",
    "-Infinity",
    "true",
    "null",
    " ",
    "\u{0}",
    "99999999999999999999999999999999",
    "\"seq\"",
    "\"mac\"",
    "\"old_fingerprint\"",
    "\"new_fingerprint\"",
    "\"ts\"",
    "T",
    "Z",
    "+",
    "24:00",
    "9999-12-31",
    "0000",
    "é",
    "Σ",
    "\u{10FFFF}",
    "_",
    "1_0",
    "٣",
    "c++",
    "'s",
    "-",
];

fn random_cps(rng: &mut Rng) -> Vec<u32> {
    let mut out = Vec::new();
    for _ in 0..rng.below(24) {
        match rng.below(4) {
            0 => out.push(rng.below(0x11_0000) as u32),
            1 => out.push(0xD800 + rng.below(0x800) as u32),
            _ => out.extend(unicode::cps(
                PIECES[rng.below(PIECES.len() as u64) as usize],
            )),
        }
    }
    out
}

#[test]
fn malformed_input_to_every_entry_point_never_panics() {
    let mut rng = Rng(0x9E37_79B9_7F4A_7C15);
    let key = [9u8; 32];
    let fp = log::fingerprint(&key);
    let numbers = [
        0.0,
        -0.0,
        f64::NAN,
        f64::INFINITY,
        f64::NEG_INFINITY,
        f64::MAX,
        f64::MIN_POSITIVE,
        1e300,
        -1e300,
        253402300800.0,
        -62135596801.0,
        9.2e18,
        -9.3e18,
    ];
    for &n in &numbers {
        let _ = float_repr::repr(n);
        let _ = DateTime::from_timestamp_utc(n);
    }
    for _ in 0..20_000 {
        let cps = random_cps(&mut rng);
        let text = unicode::to_string(&cps).unwrap_or_default();
        let truncated = &cps[..rng.below(cps.len() as u64 + 1) as usize];

        let _ = lexicon::tokenize(&cps);
        let _ = lexicon::tokenize_str(&text);
        let _ = lexicon::tokenize_unstemmed(&cps);
        let _ = lexicon::fts_index_text(&cps);
        let _ = lexicon::fts_match_query(&cps);
        let _ = lexicon::fold_lowered(&cps);
        let _ = lexicon::stem_token(&text);
        let _ = lexicon::kebab_parts(&text);
        let _ = lexicon::expand_kebab(std::slice::from_ref(&text));
        let _ = lexicon::strip_stopwords(std::slice::from_ref(&text));

        for form in [
            unicode::Form::Nfc,
            unicode::Form::Nfd,
            unicode::Form::Nfkc,
            unicode::Form::Nfkd,
        ] {
            let _ = unicode::normalize(form, &cps);
        }
        let _ = unicode::lower(&cps);
        let _ = unicode::upper(&cps);
        let _ = unicode::casefold(&cps);

        let _ = datetime::parse_event_ts(&cps);
        let _ = datetime::parse_record_dt(&cps);
        let _ = datetime::git_instant(&cps);
        let _ = datetime::git_seconds(&cps);
        let _ = datetime::ulid_time(&cps);
        if let Ok(dt) = DateTime::from_isoformat(&cps) {
            let _ = dt.isoformat();
            let _ = dt.python_repr();
            let _ = dt.pydantic_json();
            let _ = dt.to_utc();
            let _ = dt.compact_utc();
            let _ = dt.strftime(&text);
        }

        let _ = pyvalue::int_from_str(&cps);
        let _ = pyvalue::repr_str(&cps);
        for input in [&cps[..], truncated] {
            if let Ok(value) = json::loads(input) {
                let _ = json::dumps(&value, DumpOptions::DEFAULT);
                let _ = json::dumps(&value, DumpOptions::SORTED_ASCII_COMPACT);
                let _ = json::canonical_payload(&value);
                let _ = pyvalue::repr(&value);
                let _ = pyvalue::py_str(&value);
                let _ = pyvalue::py_int(&value);
                let _ = log::parse_origin(&value);
            }
        }

        let bytes = text.as_bytes().to_vec();
        let mut ring = KeyRing {
            current: Some(Key::new(key.to_vec())),
            head_file: Some(bytes.clone()),
            origin_file: Some(bytes[..rng.below(bytes.len() as u64 + 1) as usize].to_vec()),
            ..KeyRing::default()
        };
        if rng.below(2) == 0 {
            ring.retire(Key::new(bytes.clone()));
        }
        let _ = ring.read_head();
        let _ = ring.read_origin();
        let _ = ring.key_for_fingerprint(&text);
        let row = LogRow {
            seq: match rng.below(5) {
                0 => i64::MAX,
                1 => i64::MIN,
                2 => 0,
                _ => rng.next() as i64,
            },
            ts: text.clone(),
            session: (rng.below(2) == 0).then(|| text.clone()),
            kind: ["rekey", "note", ""][rng.below(3) as usize].to_owned(),
            payload: text.clone(),
            prev_mac: text.clone(),
            mac: text.clone(),
        };
        let _ = log::rekey_fingerprints(&row);
        let _ = log::row_verifies(&key, &row);
        let _ = log::same_mac(&cps, truncated);
        let mut next = row.clone();
        next.seq = row.seq.wrapping_add(rng.below(3) as i64);
        let _ = log::verify_chain(&[row.clone(), next], &ring, &fp);
        let _ = log::verify_chain(&[row], &ring, &text);
        let _ = log::head_file_text(rng.next() as i64, &text, &text);
    }
}
