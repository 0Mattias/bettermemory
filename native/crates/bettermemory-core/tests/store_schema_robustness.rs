//! Hostile input to `store::schema`'s leaves: orphan values nested past
//! the JSON reader's budget, a hundred thousand names, v8 names a megabyte
//! long and full of dashes and newlines, `absent_text` of a long tuple. No
//! panic, and each grows linearly with its input.

mod common;
mod timing;

use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::store::schema::{self, Absent};
use bettermemory_core::unicode;

#[test]
fn deep_and_wide_orphan_values() {
    for depth in [999, 1_000, 1_001, 100_000] {
        let v = unicode::cps(&("[".repeat(depth) + &"]".repeat(depth)));
        assert!(schema::orphan_names(Some(&v), false).is_empty());
    }
    let unclosed = unicode::cps(&"[".repeat(200_000));
    assert!(schema::orphan_names(Some(&unclosed), true).is_empty());
    let wide = |n: usize| {
        let mut s = String::from("[");
        for i in 0..n {
            if i > 0 {
                s.push(',');
            }
            s.push_str(&format!("\"n{i}\", \"a/b\", 1"));
        }
        s.push(']');
        unicode::cps(&s)
    };
    let v = wide(100_000);
    assert_eq!(schema::orphan_names(Some(&v), false).len(), 100_000);
    let (small, large) = (wide(5_000), wide(50_000));
    timing::grows_within(
        "orphan_names: 5,000 and 50,000 names",
        timing::TENFOLD,
        &mut || timing::once(|| assert_eq!(schema::orphan_names(Some(&small), true).len(), 5_000)),
        &mut || timing::once(|| assert_eq!(schema::orphan_names(Some(&large), true).len(), 50_000)),
    );
}

#[test]
fn long_v8_names() {
    let ulid = "01HZY3K6V0A7Q8R9S0T1V2W3X4";
    let name = |n: usize, fill: &str| format!("{}-{ulid}.md", fill.repeat(n));
    assert!(schema::is_v8_name(name(1_000_000, "-").as_bytes()));
    assert!(!schema::is_v8_name(name(1_000_000, "\n").as_bytes()));
    assert!(!schema::is_v8_name("-".repeat(1_000_000).as_bytes()));
    let (small, large) = (name(100_000, "a-"), name(1_000_000, "a-"));
    timing::grows_within(
        "is_v8_name: 200 KB and 2 MB",
        timing::TENFOLD,
        &mut || timing::once(|| assert!(schema::is_v8_name(small.as_bytes()))),
        &mut || timing::once(|| assert!(schema::is_v8_name(large.as_bytes()))),
    );
}

#[test]
fn absent_text_of_many_arguments() {
    let args: Vec<PyValue> = (0..100_000).map(PyValue::int).collect();
    let got = schema::absent_text(Absent::NotFound, &args).unwrap();
    assert!(got.len() > 600_000);
    assert!(
        schema::absent_text(Absent::Message, &[PyValue::str(&"x".repeat(1 << 20))])
            .unwrap()
            .len()
            == 1 << 20
    );
}
