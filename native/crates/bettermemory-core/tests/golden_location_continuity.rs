//! `location_continuity.jsonl` (not gated) against
//! `location_continuity::continuity`: the rows written out, each with the
//! readers it asked, then the digest of the generator's exhaustive sweep
//! (`_domain`: the product of ten small axes, the last fastest),
//! regenerated here by the same rule.

mod common;

use std::cell::RefCell;

use bettermemory_core::location_continuity::{continuity, Readers};
use bettermemory_core::pyvalue::PyValue;
use common::membership_rows::{field, lines, list, name};
use common::parity::Ledger;
use sha2::{Digest, Sha256};

type Tok = Option<&'static str>;
type St = &'static [(&'static str, i32)];

const TOKENS: [Tok; 4] = [None, Some(""), Some("A"), Some("B")];
const HEADS: [Tok; 3] = [None, Some("h1"), Some("h2")];
const S0: St = &[];
const S1: St = &[("s", 1)];
const S12: St = &[("s", 1), ("t", 2)];
const S2: St = &[("t", 2)];
const READER: [Option<bool>; 3] = [None, Some(false), Some(true)];

struct Case {
    token: Tok,
    last: Tok,
    stamps: St,
    here: Tok,
    head: Tok,
    stamps_here: St,
    holds: bool,
    confirms: Option<bool>,
    superseded: bool,
    reminted: Option<bool>,
}

/// The cases in the generator's order.
fn domain() -> Vec<Case> {
    let mut out = Vec::new();
    for token in TOKENS {
        for last in &HEADS[..2] {
            for stamps in [S0, S1, S12] {
                for here in TOKENS {
                    for head in HEADS {
                        for stamps_here in [S0, S1, S2] {
                            for holds in [false, true] {
                                for confirms in READER {
                                    for superseded in [false, true] {
                                        for reminted in READER {
                                            out.push(Case {
                                                token,
                                                last: *last,
                                                stamps,
                                                here,
                                                head,
                                                stamps_here,
                                                holds,
                                                confirms,
                                                superseded,
                                                reminted,
                                            });
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    }
    out
}

fn u(t: Tok) -> Option<Vec<u32>> {
    t.map(|t| t.chars().map(u32::from).collect())
}

/// (continues, witness, readers asked in order).
fn one(c: &Case) -> (bool, &'static str, Vec<&'static str>) {
    let asked = RefCell::new(Vec::new());
    let mut holds = |_: &[u32]| -> Result<bool, ()> {
        asked.borrow_mut().push("holds");
        Ok(c.holds)
    };
    let mut confirms = || -> Result<bool, ()> {
        asked.borrow_mut().push("confirms");
        Ok(c.confirms == Some(true))
    };
    let mut reminted = || -> Result<bool, ()> {
        asked.borrow_mut().push("reminted");
        Ok(c.reminted == Some(true))
    };
    let (tok, last, here, head) = (u(c.token), u(c.last), u(c.here), u(c.head));
    let (ok, how) = continuity(
        tok.as_deref(),
        last.as_deref(),
        c.stamps,
        here.as_deref(),
        head.as_deref(),
        c.stamps_here,
        c.superseded,
        Readers {
            holds: &mut holds,
            confirms: c
                .confirms
                .map(|_| &mut confirms as &mut dyn FnMut() -> Result<bool, ()>),
            reminted: c
                .reminted
                .map(|_| &mut reminted as &mut dyn FnMut() -> Result<bool, ()>),
        },
    )
    .expect("no reader fails");
    (ok, how, asked.into_inner())
}

fn tok_of(v: &PyValue) -> Tok {
    match v {
        PyValue::None => None,
        v => Some(Box::leak(name(v).into_boxed_str())),
    }
}

#[test]
fn location_continuity_every_row_and_the_sweep() {
    let file = "location_continuity.jsonl";
    let all = lines(file);
    let mut ledger = Ledger::new("location_continuity_every_row_and_the_sweep", file);
    let cases = domain();
    let mut total = 0;
    for l in &all {
        if let Some(count) = l.get("count") {
            let mut h = Sha256::new();
            for c in &cases {
                let (ok, how, asked) = one(c);
                h.update(format!("{}|{how}|{}\n", u8::from(ok), asked.join(",")).as_bytes());
            }
            let got = common::hex(&h.finalize());
            total += 1;
            let ok = common::membership_rows::int(count) as usize == cases.len()
                && got == name(field(l, "sha256"));
            assert!(!ledger.differs_fatally("sweep", ok), "sweep: {got}");
            continue;
        }
        let a = list(field(l, "a"));
        let o = list(field(l, "o"));
        // Find the case the row names among the sweep's.
        let st = |v: &PyValue| list(v).len();
        let c = cases
            .iter()
            .find(|c| {
                c.token == tok_of(&a[0])
                    && c.last == tok_of(&a[1])
                    && c.stamps.len() == st(&a[2])
                    && c.here == tok_of(&a[3])
                    && c.head == tok_of(&a[4])
                    && c.stamps_here == stamps_here_of(&a[5])
                    && PyValue::Bool(c.holds) == a[6]
                    && c.confirms.map_or(PyValue::None, PyValue::Bool) == a[7]
                    && PyValue::Bool(c.superseded) == a[8]
                    && c.reminted.map_or(PyValue::None, PyValue::Bool) == a[9]
            })
            .expect("the row is a case of the sweep");
        let (ok, how, asked) = one(c);
        let want_asked: Vec<String> = list(&o[2]).iter().map(name).collect();
        let same = PyValue::Bool(ok) == o[0] && name(&o[1]) == how && want_asked == asked;
        total += 1;
        assert!(
            !ledger.differs_fatally(format!("row {a:?}"), same),
            "row {a:?}: {ok} {how} {asked:?}"
        );
    }
    assert_eq!(total, all.len());
    assert!(total > 1);
    ledger.finish(total);
}

fn stamps_here_of(v: &PyValue) -> St {
    let items = list(v);
    match items.len() {
        0 => S0,
        _ if name(&list(&items[0])[0]) == "s" => S1,
        _ => S2,
    }
}
