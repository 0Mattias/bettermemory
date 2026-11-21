//! Hostile classes: many members, ranges over every code point, bounds at
//! the ends of `u32`, every code point asked. No panic in debug or
//! release; the work grows with the members as the reference's does.

mod timing;

use bettermemory_core::pyre::charset::{compile_class, optimize_charset, Fold};
use bettermemory_core::pyre::tree::{Category, Member};

const U: u32 = 32;
const I: u32 = 2;

fn literals(n: u32) -> Vec<Member> {
    (0..n)
        .map(|i| Member::Literal((i * 7919) % 0x110000))
        .collect()
}

#[test]
fn many_members_grow_linearly() {
    for fold in [Fold::None, Fold::Unicode] {
        let (small, large) = (literals(20_000), literals(200_000));
        timing::grows_within(
            "optimize_charset over literals",
            timing::TENFOLD,
            &mut || timing::once(|| optimize_charset(&small, fold)),
            &mut || timing::once(|| optimize_charset(&large, fold)),
        );
    }
}

#[test]
fn bounds_of_u32_do_not_panic() {
    let hostile = [
        Member::Literal(u32::MAX),
        Member::Range(0, u32::MAX),
        Member::Range(u32::MAX - 1, u32::MAX),
        Member::Range(0xFFFF, 0x10000),
        Member::Literal(0x10FFFF),
    ];
    for fold in [Fold::None, Fold::Ascii, Fold::Unicode] {
        for m in &hostile {
            let _ = optimize_charset(&[*m], fold);
            let _ = optimize_charset(&[Member::Negate, *m, Member::Literal(0x61)], fold);
        }
    }
    let set = compile_class(&[Member::Range(0, 0x10FFFF)], U).expect("compiled");
    for c in [0, 0xFFFF, 0x10000, 0x10FFFF, 0x110000, u32::MAX] {
        let _ = set.contains(c);
    }
    let set = compile_class(&[Member::Range(0x41, 0x5A), Member::Literal(0x100)], I | U)
        .expect("compiled");
    for c in [0x110000, u32::MAX] {
        assert!(!set.contains(c));
    }
}

#[test]
fn every_code_point_through_a_folded_bitmap() {
    let members = [
        Member::Range(0, 0xFFFF),
        Member::Category(Category::CATEGORY_NOT_WORD),
        Member::Negate,
    ];
    let set = compile_class(&members, I | U).expect("compiled");
    let held = (0..0x110000).filter(|&c| set.contains(c)).count();
    assert!(held > 0);
}
