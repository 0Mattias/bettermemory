//! Cases a plausible wrong `origin_dates::memo` fails.

mod common;

use bettermemory_core::datetime::DateTime;
use bettermemory_core::origin_dates::memo::instant;
use bettermemory_core::origin_history::TimestampsMemo;
use common::origin_dates_memo::run_step;
use serde_json::{json, Value};

const A: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
const B: &str = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";

fn step(now: f64, g: Value, l: Value) -> Value {
    json!({"cwd": "W", "now": now, "g": g, "l": l})
}

fn keyed(extra: Value) -> Value {
    let mut l = json!({"files": ["/r1", A], "guard": "g1", "hold": "h1"});
    for (k, v) in extra.as_object().unwrap() {
        l[k] = v.clone();
    }
    l
}

/// A failure read at its 60th second is read again, not served: the
/// window is half open.
#[test]
fn failure_window_is_half_open() {
    let mut memo: TimestampsMemo<Value> = TimestampsMemo::new();
    run_step(
        &mut memo,
        &step(0.0, json!(["x\n"]), keyed(json!({"cuts": [[]]}))),
    );
    let (r, _) = run_step(
        &mut memo,
        &step(59.999, json!([]), keyed(json!({"cuts": [[]]}))),
    );
    assert_eq!(r, json!("error"));
    let (r, _) = run_step(
        &mut memo,
        &step(
            60.0,
            json!(["5\n"]),
            keyed(json!({"cuts": [[]], "held": [true]})),
        ),
    );
    assert_eq!(r, json!(["1970-01-01T00:00:05+00:00"]));
}

/// A served timeout is noted as a timeout, any other cause as a failure.
#[test]
fn served_timeout_notes_a_timeout() {
    let mut memo: TimestampsMemo<Value> = TimestampsMemo::new();
    run_step(
        &mut memo,
        &step(0.0, json!(["T"]), keyed(json!({"cuts": [[]]}))),
    );
    let (_, s) = run_step(
        &mut memo,
        &step(1.0, json!([]), keyed(json!({"cuts": [[]]}))),
    );
    let s = s.0.borrow();
    assert_eq!((s.failed, s.timed), (0, 1));
}

/// A raised deadline answers the call budget and keeps nothing.
#[test]
fn deadline_keeps_nothing() {
    let mut memo: TimestampsMemo<Value> = TimestampsMemo::new();
    let (r, _) = run_step(
        &mut memo,
        &step(0.0, json!(["D"]), keyed(json!({"cuts": [[]]}))),
    );
    assert_eq!(r, json!("call_budget"));
    assert!(memo.entries().is_empty() && memo.failures().is_empty());
}

/// A deadline raised inside the extension answers the call budget at
/// once, before any full read, even from a runner that keeps answering.
#[test]
fn deadline_in_extension_stops_before_the_full_read() {
    let mut memo: TimestampsMemo<Value> = TimestampsMemo::new();
    run_step(
        &mut memo,
        &step(
            0.0,
            json!(["100\n"]),
            keyed(json!({"cuts": [[]], "held": [true]})),
        ),
    );
    let (r, s) = run_step(
        &mut memo,
        &step(
            1.0,
            json!(["D", "50\n"]),
            keyed(json!({"files": ["/r1", B], "cuts": [[]], "loose": true})),
        ),
    );
    assert_eq!(r, json!("call_budget"));
    let s = s.0.borrow();
    assert_eq!(s.answers.len(), 1, "the full read ran: {:?}", s.calls);
}

/// The cut moving during the log keeps nothing and remembers nothing.
#[test]
fn moved_cut_is_not_remembered() {
    let mut memo: TimestampsMemo<Value> = TimestampsMemo::new();
    let (r, s) = run_step(
        &mut memo,
        &step(
            0.0,
            json!(["5\n"]),
            keyed(json!({"cuts": [[]], "held": [false]})),
        ),
    );
    assert_eq!(r, json!("shallow"));
    assert_eq!(s.0.borrow().failed, 1);
    assert!(memo.entries().is_empty() && memo.failures().is_empty());
}

/// A passed `located` is used as given: the files are not asked.
#[test]
fn located_skips_the_files() {
    let mut memo: TimestampsMemo<Value> = TimestampsMemo::new();
    let mut st = step(
        0.0,
        json!(["5\n"]),
        keyed(json!({"cuts": [[]], "held": [true]})),
    );
    st["located"] = json!(["/r9", B]);
    let (_, s) = run_step(&mut memo, &st);
    assert_eq!(s.0.borrow().calls[0], json!(["guard", "/r9"]));
}

/// `_instant` rounds the quotient once: a two-step rounding through the
/// microseconds as a float misses on the large instants.
#[test]
fn instant_rounds_once() {
    let mut differs = 0;
    for us in (0..20_000i64).map(|i| 253_402_300_799_999_999 - i * 7_919) {
        let stamp = DateTime::from_unix_micros_utc(us).unwrap();
        let got = instant(&stamp).unwrap();
        #[allow(clippy::cast_precision_loss)]
        let naive = us as f64 / 1e6;
        if got != naive {
            differs += 1;
        }
    }
    assert!(differs > 0);
    let naive = DateTime::from_isoformat(
        &"2020-01-01T00:00:00"
            .chars()
            .map(u32::from)
            .collect::<Vec<_>>(),
    )
    .unwrap();
    assert_eq!(instant(&naive), None);
}
