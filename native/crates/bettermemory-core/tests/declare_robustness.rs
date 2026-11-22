//! Hostile checkouts for the declaring token writer: a git directory, a
//! kind and a machine of a million code points, a lone surrogate, integer
//! texts past every machine integer in the object key. No panic, and the
//! writer's time grows linearly with the text it compares and copies.

mod common;
mod timing;

use bettermemory_core::capture::location::ObjectKey;
use bettermemory_core::declare::{write_token, RemintCall};
use bettermemory_core::membership_records::records::Checkout;
use bettermemory_core::pyvalue::PyStr;
use bettermemory_core::unicode::cps;

fn ck(machine: PyStr, kind: PyStr, git_dir: PyStr, device: &str) -> Checkout {
    Checkout {
        id: cps("k1"),
        project: cps("p1"),
        machine,
        kind,
        object_key: Some(ObjectKey {
            device: cps(device),
            inode: cps("1"),
            birth: None,
        }),
        common_object_key: None,
        git_dir: Some(git_dir),
        token: Some(cps("0123456789ABCDEFGHJKMNPQRS")),
        token_status: cps("own"),
        stamps: Vec::new(),
        last_head: None,
        branch: None,
        remotes: Vec::new(),
        manifest: None,
        legacy: false,
        retired: false,
        registered: cps("t0"),
        views: Vec::new(),
        sessions: Vec::new(),
        replaced_by: None,
        ticks: None,
    }
}

#[test]
fn huge_and_odd_texts() {
    let big: PyStr = vec![0xD800; 1_000_000];
    let w = write_token(big.clone(), true, |c: &RemintCall| {
        Ok((Some(c.git_dir.clone()), cps("written")))
    });
    let k = ck(
        big.clone(),
        vec![0x62; 1_000_000],
        big.clone(),
        &"9".repeat(10_000),
    );
    let (t, s) = w(&k).unwrap().unwrap();
    assert_eq!(t.unwrap().len(), 1_000_000);
    assert_eq!(s, cps("written"));
    let mut other = big.clone();
    other[999_999] = 0xDC00;
    assert_eq!(
        w(&ck(other, cps("bare"), cps("/g"), "1"))
            .unwrap()
            .unwrap()
            .1,
        cps("skipped:other_machine")
    );
}

#[test]
fn linear_in_text() {
    let run = |n: usize| {
        let m: PyStr = vec![0x4D; n];
        let w = write_token(m.clone(), false, |c: &RemintCall| {
            Ok((None, cps(if c.bare { "b" } else { "n" })))
        });
        let k = ck(m, vec![0x62; n], vec![0x2F; n], "1");
        assert_eq!(w(&k).unwrap().unwrap().1, cps("n"));
    };
    timing::grows_within(
        "write_token: 100,000 and 1,000,000 code points",
        timing::TENFOLD,
        &mut || timing::once(|| run(100_000)),
        &mut || timing::once(|| run(1_000_000)),
    );
}
