//! `control_tokens` over hostile input: a megabyte of each opener the
//! pattern starts on, runs at and past the name bound, whitespace runs
//! before a turn marker, lone surrogates and every code point, and a value
//! nested 100,000 deep. No panic, in debug or release, and each reading in
//! linear time: within a small multiple of the reference's time on the
//! same input (measured on an Apple M-series laptop, Python 3.13.13, in
//! milliseconds: find and defang), and doubling the input at most about
//! doubles the time.

use std::time::{Duration, Instant};

use bettermemory_core::control_tokens as ct;
use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::unicode::cps;

const M: usize = 1 << 20;

fn repeat(piece: &str, times: usize) -> Vec<u32> {
    cps(&piece.repeat(times))
}

/// (name, text, the reference's find and defang milliseconds).
fn hostile() -> Vec<(&'static str, Vec<u32>, u64, u64)> {
    let mut pipe = cps("<|");
    pipe.extend(std::iter::repeat_n(u32::from(b'a'), M));
    let mut ws = vec![0x20; M];
    ws.extend(cps("Human:"));
    vec![
        (
            "prose",
            repeat(
                "the quick brown fox <|im_start|> jumps\n\nHuman: x ",
                M / 48,
            ),
            39,
            225,
        ),
        ("lt", vec![u32::from(b'<'); M], 253, 198),
        ("pipe", pipe, 64, 53),
        (
            "nl",
            repeat(&format!("\n{}", " ".repeat(1000)), M / 1001),
            103,
            82,
        ),
        ("ws", ws, 2, 89),
        ("brk", repeat("[A", M / 2), 128, 108),
        ("tokens", repeat("<|end|>", M / 7), 302, 1380),
    ]
}

fn timed(f: impl FnOnce()) -> Duration {
    let t = Instant::now();
    f();
    t.elapsed()
}

/// The fastest of three runs: a loaded machine slows some runs, never
/// the work itself.
fn best(mut f: impl FnMut()) -> Duration {
    (0..3).map(|_| timed(&mut f)).min().unwrap_or_default()
}

/// The allowed multiple of the reference's time. A debug build reads
/// each character's class through unoptimised table lookups, so it is held
/// to a loose bound only.
fn multiple() -> u32 {
    if cfg!(debug_assertions) {
        600
    } else {
        3
    }
}

#[test]
fn a_megabyte_of_each_opener_is_read_in_the_references_time() {
    for (name, text, find_ms, defang_ms) in hostile() {
        let a = best(|| drop(ct::find_control_tokens(&text)));
        let b = best(|| drop(ct::defang_control_tokens(&text)));
        let limit = |ms: u64| Duration::from_millis(ms.max(20)) * multiple();
        assert!(a <= limit(find_ms), "{name}: find took {a:?}");
        assert!(b <= limit(defang_ms), "{name}: defang took {b:?}");
        ct::control_token_offsets(&text);
        ct::folded_quote(&text);
    }
}

#[test]
fn twice_the_text_costs_about_twice_the_time() {
    let one = repeat("<|<<[\n \n<｜", 1 << 16);
    let two = repeat("<|<<[\n \n<｜", 1 << 17);
    let best = |t: &[u32]| {
        (0..3)
            .map(|_| timed(|| drop(ct::defang_control_tokens(t))))
            .min()
            .unwrap()
    };
    let (a, b) = (best(&one), best(&two));
    let slack = if cfg!(debug_assertions) { 8 } else { 4 };
    assert!(
        b <= a * slack + Duration::from_millis(5),
        "{a:?} then {b:?}"
    );
}

#[test]
fn names_at_and_past_the_bound_and_odd_code_points_never_panic() {
    for n in [0usize, 1, 31, 32, 33, 1000] {
        for (open, fill, close) in [
            ("<|", "a", "|>"),
            ("<\u{ff5c}", "x", "\u{ff5c}>"),
            ("<<", "A", ">>"),
            ("<</", "A", ">>"),
            ("<", "a", ">"),
            ("[", "A", "]"),
            ("[/", "A", "]"),
        ] {
            let t = cps(&format!("{open}{}{close}", fill.repeat(n)));
            let hits = ct::find_control_tokens(&t);
            assert!(hits.is_empty(), "{n} {open}");
            ct::defang_control_tokens(&t);
        }
    }
    let mut every: Vec<u32> = (0..0x110000).collect();
    every.extend(cps("<|end|>\n\nHuman:"));
    for chunk in every.chunks(4099) {
        let d = ct::defang_control_tokens(chunk);
        assert!(ct::find_control_tokens(&d).is_empty());
        ct::folded_quote(chunk);
        let _ = ct::refuse_control_tokens(&cps("f"), Some(chunk), &[], &[]);
    }
    for edge in [
        vec![],
        vec![0xD800],
        vec![0x0A],
        vec![u32::from(b'<')],
        vec![0x10FFFF],
    ] {
        ct::find_control_tokens(&edge);
        ct::defang_control_tokens(&edge);
        ct::folded_quote(&edge);
    }
}

#[test]
fn a_value_nested_deep_is_named_without_the_stack() {
    let mut deep = PyValue::str("<|end|>");
    for i in 0..100_000 {
        deep = if i % 2 == 0 {
            PyValue::List(vec![deep])
        } else {
            PyValue::Tuple(vec![deep])
        };
    }
    let t = best(|| {
        let named = ct::named_tokens(&deep);
        let mut at = &named;
        loop {
            match at {
                PyValue::List(v) | PyValue::Tuple(v) => at = &v[0],
                PyValue::Str(s) => {
                    assert_eq!(*s, cps("end"));
                    break;
                }
                other => panic!("{other:?}"),
            }
        }
    });
    // The reference: 730 ms.
    assert!(t <= Duration::from_millis(730) * multiple(), "{t:?}");
    let mut dict = PyValue::str("x");
    for _ in 0..100_000 {
        dict = PyValue::Dict(vec![(PyValue::str("k<|end|>"), dict)]);
    }
    ct::named_tokens(&dict);
    let wide = PyValue::Dict(
        (0..50_000)
            .map(|i| (PyValue::str(&format!("{i}<|end|>")), PyValue::int(i)))
            .collect(),
    );
    match ct::named_tokens(&wide) {
        PyValue::Dict(ref items) => assert_eq!(items.len(), 50_000),
        other => panic!("{other:?}"),
    }
}

#[test]
fn a_dict_with_many_number_keys_is_rebuilt_in_linear_time() {
    // The reference: 7 ms for int keys, 14 ms for float keys.
    for (keys, ms) in [
        ((0..20_000).map(PyValue::int).collect::<Vec<_>>(), 7u64),
        (
            (0..20_000)
                .map(|i| PyValue::Float(f64::from(i) + 0.5))
                .collect(),
            14,
        ),
    ] {
        let mut pairs: Vec<(PyValue, PyValue)> = keys.into_iter().map(|k| (k.clone(), k)).collect();
        pairs[0].1 = PyValue::str("<|end|>");
        let value = PyValue::Dict(pairs);
        let t = best(|| match ct::named_tokens(&value) {
            PyValue::Dict(ref items) => assert_eq!(items.len(), 20_000),
            other => panic!("{other:?}"),
        });
        assert!(t <= Duration::from_millis(ms.max(20)) * multiple(), "{t:?}");
    }
}
