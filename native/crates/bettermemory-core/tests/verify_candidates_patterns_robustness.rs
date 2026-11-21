//! Hostile input for `verify_candidates::patterns`: 200,000 code points of
//! each shape a backtracking matcher would retry (unclosed backticks,
//! spaced words with no slash, dashed and dotted runs, boundaries with no
//! path), lone surrogates and the last code point. No panic, and time
//! linear in the input: ten times the input costs under `TENFOLD` times
//! the time (`timing`). The reference is linear on these shapes too: the
//! comment at `_DOMAIN_ROUTE_RE` measures 2.0x at twice the length.

mod timing;

use bettermemory_core::verify_candidates::patterns::{
    backtick_iter, bare_iter, domain_route_iter, Hit,
};

const N: usize = 200_000;

fn rep(piece: &str, len: usize) -> Vec<u32> {
    piece.chars().map(u32::from).cycle().take(len).collect()
}

type Matcher = fn(&[u32]) -> Vec<Hit>;

fn shapes() -> Vec<(&'static str, Matcher, Vec<&'static str>)> {
    vec![
        ("backtick", backtick_iter, vec!["`", "`a", "`\n", "a"]),
        (
            "bare",
            bare_iter,
            vec![
                "/", " /a", " Bb", "/a Bb", "/a Bb/", " A", "C:", "=", "$HOME", "~",
            ],
        ),
        (
            "domain",
            domain_route_iter,
            vec!["a-", "a.", "-.", ".-", "a..", "..a", "a.b/", "/a.b", "."],
        ),
    ]
}

/// `head` once, then `piece` to `len` code points.
fn text(head: &str, piece: &str, len: usize) -> Vec<u32> {
    let mut t: Vec<u32> = head.chars().map(u32::from).collect();
    t.extend(rep(piece, len));
    t
}

#[test]
fn hostile_shapes_do_not_panic_and_hits_are_ordered() {
    for (name, f, pieces) in shapes() {
        for piece in pieces {
            for head in ["", "/a", "a.b"] {
                let hits = f(&text(head, piece, N));
                for w in hits.windows(2) {
                    assert!(w[0].end <= w[1].start, "{name} {piece:?}");
                }
                for h in &hits {
                    assert!(h.start <= h.group_start && h.group_start < h.group_end);
                    assert!(h.group_end <= h.end && h.end <= N + head.len());
                }
            }
        }
        let odd = [0xD800, 0xDFFF, 0x10FFFF, 0x60, 0x2F, 0x2E, 0x20, 0];
        let t: Vec<u32> = odd.iter().copied().cycle().take(N).collect();
        let _ = f(&t);
        let _ = f(&[]);
    }
}

#[test]
fn hostile_shapes_are_linear() {
    for (name, f, pieces) in shapes() {
        for piece in pieces {
            let (small, large) = (text("/a", piece, N / 10), text("/a", piece, N));
            f(&small);
            timing::grows_within(
                &format!("{name} {piece:?}"),
                timing::TENFOLD,
                &mut || timing::once(|| f(&small)),
                &mut || timing::once(|| f(&large)),
            );
        }
    }
}
