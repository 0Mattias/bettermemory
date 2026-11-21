//! `membership_shared`'s rules over hostile input: numbers of thousands
//! of digits, many stamps, many rows, a payload nested past the JSON
//! limit. No panic, and time linear in the size: ten times the stamps or
//! rows take within a small multiple of the time.

mod common;
#[path = "common/membership_shared.rs"]
mod shared;
mod timing;

use bettermemory_core::capture::Stamp;
use bettermemory_core::membership_shared as ms;
use shared::*;

fn st(name: &str, ino: &str, m: &str, c: &str) -> Stamp {
    Stamp {
        name: cps(name),
        numbers: [ino.into(), m.into(), c.into(), "1".into()],
    }
}

#[test]
fn numbers_of_thousands_of_digits() {
    let big = format!("7{}", "0".repeat(4000));
    let near = format!("7{}999999999", "0".repeat(3991));
    let neg = format!("-{big}");
    for (a, b) in [(&big, &near), (&big, &neg), (&neg, &neg), (&near, &big)] {
        let x = st("d", "1", a, a);
        let y = st("d", "1", b, b);
        let _ = ms::same_file(&x, &y);
        let _ = ms::ticked(&[x, y]);
    }
    assert!(ms::same_file(
        &st("d", "1", &big, &big),
        &st("d", "1", &near, &big)
    ));
    assert!(!ms::same_file(
        &st("d", "1", &big, &big),
        &st("d", "1", &neg, &big)
    ));
}

fn stamps_of(n: usize) -> Vec<Stamp> {
    (0..n)
        .map(|i| {
            st(
                &format!("f{i}"),
                &i.to_string(),
                &(i as i128 * 1_000_000_007).to_string(),
                "5",
            )
        })
        .collect()
}

#[test]
fn many_stamps_cost_in_proportion() {
    let run = |n: usize| {
        let a = stamps_of(n);
        let k = make_checkout("m1", None, a.clone(), None, None);
        let loc = make_location(a, None);
        move || {
            let _ = ms::moved_under(&k, &loc);
            let _ = ms::carried_in_ticks(&k, &k, &loc);
            let _ = ms::ticked(&k.stamps);
        }
    };
    let (s, l) = (run(2_000), run(20_000));
    timing::grows_within("stamps", 40, &mut || timing::once(&s), &mut || {
        timing::once(&l)
    });
}

#[test]
fn many_rows_and_a_deep_payload() {
    let rows: Vec<_> = (0..50_000)
        .map(|_| {
            log_row(
                "checkout_registered",
                r#"{"checkout": "k1", "rule": "join:T"}"#.into(),
            )
        })
        .collect();
    assert_eq!(ms::joined_silently_by_token(&cps("k1"), &rows), Ok(true));
    let deep = log_row(
        "checkout_registered",
        format!("{}{}", "{\"a\":".repeat(100_000), "1"),
    );
    let _ = ms::joined_silently_by_token(&cps("k1"), &[deep]);
}
