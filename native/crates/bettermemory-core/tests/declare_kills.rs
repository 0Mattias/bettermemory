//! Cases a plausible wrong `declaring_model` fails: a case-folded or
//! trimmed bare test, a writer that ignores the machine, an empty git
//! directory taken for none, a re-mint without `only_siblings` or without
//! the checkout's lineage, a machine row not recorded, a raise of the
//! record or of the re-mint swallowed.

mod common;

use std::cell::RefCell;
use std::collections::BTreeMap;
use std::rc::Rc;

use bettermemory_core::capture::location::ObjectKey;
use bettermemory_core::checkout_token::Lineage;
use bettermemory_core::declare::{declaring_model, write_token, RemintCall};
use bettermemory_core::machine::{MachineFacts, MachineIdentity};
use bettermemory_core::membership::{Error, LivenessSeam};
use bettermemory_core::membership_lines::Raised;
use bettermemory_core::membership_records::records::{Checkout, View};
use bettermemory_core::pyvalue::PyStr;
use bettermemory_core::unicode::cps;
use common::membership_core::Fake;

const TOKEN: &str = "0123456789ABCDEFGHJKMNPQRS";

fn ck(machine: &str, kind: &str, git_dir: Option<&str>) -> Checkout {
    Checkout {
        id: cps("k1"),
        project: cps("p1"),
        machine: cps(machine),
        kind: cps(kind),
        object_key: Some(ObjectKey {
            device: cps("10"),
            inode: cps("20"),
            birth: None,
        }),
        common_object_key: None,
        git_dir: git_dir.map(cps),
        token: Some(cps(TOKEN)),
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

type Calls = Rc<RefCell<Vec<RemintCall>>>;

fn writer(policy: bool) -> (bettermemory_core::membership::TokenWriter, Calls) {
    let calls: Calls = Rc::default();
    let seen = calls.clone();
    let w = write_token(cps("M"), policy, move |c: &RemintCall| {
        seen.borrow_mut().push(c.clone());
        Ok((Some(cps("t")), cps("written")))
    });
    (w, calls)
}

fn skipped() -> Result<Option<(Option<PyStr>, PyStr)>, Raised> {
    Ok(Some((None, cps("skipped:other_machine"))))
}

#[test]
fn bare_is_exact() {
    let (w, calls) = writer(true);
    for (kind, bare) in [
        ("bare", true),
        ("Bare", false),
        ("BARE", false),
        ("bare ", false),
        ("", false),
    ] {
        w(&ck("M", kind, Some("/g"))).unwrap();
        assert_eq!(calls.borrow().last().unwrap().bare, bare, "{kind:?}");
    }
}

#[test]
fn other_machine_and_no_git_dir_skip() {
    let (w, calls) = writer(true);
    assert_eq!(w(&ck("N", "primary", Some("/g"))), skipped());
    assert_eq!(w(&ck("m", "primary", Some("/g"))), skipped());
    assert_eq!(w(&ck("M", "primary", None)), skipped());
    assert!(calls.borrow().is_empty());
    assert_eq!(
        w(&ck("M", "primary", Some(""))),
        Ok(Some((Some(cps("t")), cps("written"))))
    );
    assert_eq!(calls.borrow()[0].git_dir, cps(""));
}

#[test]
fn remint_arguments() {
    for policy in [true, false] {
        let (w, calls) = writer(policy);
        w(&ck("M", "primary", Some("/g"))).unwrap();
        let c = calls.borrow()[0].clone();
        assert_eq!(c.policy, policy);
        assert!(c.only_siblings);
        assert_eq!(
            c.replacing,
            Some(Lineage {
                token: cps(TOKEN),
                device: cps("10"),
                inode: cps("20")
            })
        );
    }
    let (w, calls) = writer(true);
    let mut k = ck("M", "primary", Some("/g"));
    k.object_key = None;
    w(&k).unwrap();
    assert_eq!(calls.borrow()[0].replacing, None);
}

fn identity(id: &str) -> MachineIdentity {
    MachineIdentity {
        id: cps(id),
        facts: MachineFacts {
            hardware: None,
            hostname: None,
            id_file: None,
        },
        predecessor: None,
        minted: false,
        cloned: false,
        facts_updated: false,
        recorded: None,
    }
}

#[test]
fn open_records_and_passes_a_raise() {
    let n = RefCell::new(0);
    let m = declaring_model(
        Fake::new(BTreeMap::new(), None, false),
        &identity("M"),
        true,
        Some(cps("s")),
        LivenessSeam::Given(Box::new(|_: &Checkout, _: &View| cps("live"))),
        Box::new(|| unreachable!()),
        |_: &RemintCall| Ok((None, cps("x"))),
        |_, _| {
            *n.borrow_mut() += 1;
            Ok(())
        },
    )
    .ok()
    .unwrap();
    assert_eq!(*n.borrow(), 1);
    assert_eq!(m.session(), Some(&cps("s")));
    assert_eq!(
        m.token_writer().unwrap()(&ck("N", "bare", Some("/g"))),
        skipped()
    );
    let raised = declaring_model(
        Fake::new(BTreeMap::new(), None, false),
        &identity("M"),
        true,
        None,
        LivenessSeam::Given(Box::new(|_: &Checkout, _: &View| cps("live"))),
        Box::new(|| unreachable!()),
        |_: &RemintCall| Ok((None, cps("x"))),
        |_, _| {
            Err(Error::Raised(Raised {
                class: "OSError",
                message: "no".into(),
            }))
        },
    );
    assert!(matches!(raised, Err(Error::Raised(r)) if r.class == "OSError"));
}

#[test]
fn a_raise_of_the_remint_passes_out() {
    let w = write_token(cps("M"), true, |_: &RemintCall| {
        Err(Raised {
            class: "OSError",
            message: "[Errno 5] Input/output error".into(),
        })
    });
    let r = w(&ck("M", "primary", Some("/g")));
    assert!(matches!(&r, Err(r) if r.class == "OSError" && r.message.starts_with("[Errno 5]")));
    assert_eq!(w(&ck("N", "primary", Some("/g"))), skipped());
}
