//! Hostile input for `origin_dates::memo`: a long history over the budget
//! and every instant bound, with no panic.

mod common;

use bettermemory_core::datetime::DateTime;
use bettermemory_core::origin_dates::memo::instant;
use bettermemory_core::origin_history::{TimestampsMemo, MEMO_SLOTS};
use common::origin_dates_memo::run_step;
use serde_json::{json, Value};

#[test]
fn history_at_and_over_the_budget() {
    let a = "a".repeat(40);
    for (n, want_kept) in [(MEMO_SLOTS, true), (MEMO_SLOTS + 1, false)] {
        let mut memo: TimestampsMemo<Value> = TimestampsMemo::new();
        let st = json!({"cwd": "W", "now": 0.0, "g": [{"rep": "1\n", "n": n}],
            "l": {"files": ["/r1", a], "guard": "g", "hold": "h", "cuts": [[]], "held": [true]}});
        let (r, s) = run_step(&mut memo, &st);
        assert_eq!(memo.entries().len(), usize::from(want_kept));
        if want_kept {
            assert_eq!(r.as_array().unwrap().len(), n);
        } else {
            assert_eq!(r, json!("over_budget"));
            assert_eq!(s.0.borrow().failed, 1);
        }
    }
}

#[test]
fn instant_bounds() {
    for us in [
        -62_135_596_800_000_000i64,
        -62_135_596_799_999_999,
        -1,
        0,
        1,
        253_402_300_799_999_999,
    ] {
        let stamp = DateTime::from_unix_micros_utc(us).unwrap();
        let t = instant(&stamp).unwrap();
        assert!(t.is_finite());
    }
}
