//! ContextVar.reset restores its own previous binding on a return, an
//! ordinary error, a deadline and a nonlocal unwind. The provider's
//! mutations of the shared budget and unbound context variables survive.
use bettermemory_core::git_read_budget::{
    call_scope, read, reading, Progresses, Raise, ReadDeadline, Scope, Shared, Store,
};
use std::panic::{catch_unwind, panic_any, AssertUnwindSafe};
use std::rc::Rc;
use std::sync::Arc;

fn root(n: u32) -> (Vec<u32>, bool) {
    (vec![n], true)
}

#[test]
fn decorated_read_resets_only_installed_bindings() {
    for had_active in [false, true] {
        for installs_root in [false, true] {
            for outcome in 0..4 {
                let original: Shared = Shared::default();
                let changed: Shared = Shared::default();
                let active = had_active.then(|| original.clone());
                let mut scope = Scope {
                    active: active.clone(),
                    owner: Some(original.clone()),
                    root: Some(root(1)),
                };
                let payload = Arc::new(vec![0xdcffu32]);
                let result = catch_unwind(AssertUnwindSafe(|| {
                    read(
                        &mut scope,
                        installs_root.then(|| root(2)),
                        |s| {
                            assert!(Rc::ptr_eq(s.active.as_ref().unwrap(), &original));
                            assert_eq!(s.root, Some(root(if installs_root { 2 } else { 1 })));
                            s.active.as_ref().unwrap().borrow_mut().deadline = Some(37.0);
                            s.active = Some(changed.clone());
                            s.owner = Some(changed.clone());
                            s.root = Some(root(3));
                            match outcome {
                                0 => Ok(5),
                                1 => Err(Raise::Other(7)),
                                2 => Err(Raise::Deadline(ReadDeadline)),
                                _ => panic_any(payload.clone()),
                            }
                        },
                        || 11,
                    )
                }));
                match outcome {
                    0 => assert_eq!(result.unwrap(), Ok(5)),
                    1 => assert_eq!(result.unwrap(), Err(7)),
                    2 => assert_eq!(result.unwrap(), Ok(11)),
                    _ => assert!(Arc::ptr_eq(
                        &payload,
                        &result.unwrap_err().downcast::<Arc<Vec<u32>>>().unwrap()
                    )),
                }
                assert_eq!(scope.root, Some(root(if installs_root { 1 } else { 3 })));
                assert!(Rc::ptr_eq(scope.owner.as_ref().unwrap(), &changed));
                assert_eq!(scope.active.is_some(), had_active);
                if let Some(active) = scope.active {
                    assert!(Rc::ptr_eq(&active, &original));
                }
                assert_eq!(original.borrow().deadline, Some(37.0));
            }
        }
    }
}

#[test]
fn nested_root_read_restores_the_callers_active_budget_on_unwind() {
    let original = Shared::default();
    let mut scope = Scope {
        active: Some(original.clone()),
        root: Some(root(1)),
        ..Scope::default()
    };
    let payload = Arc::new(vec![31u32]);
    let panic = catch_unwind(AssertUnwindSafe(|| {
        reading(&mut scope, &[2], false, |scope, budget| {
            assert!(Rc::ptr_eq(budget, &original));
            assert_eq!(scope.root, Some((vec![2], false)));
            let _: Result<(), ()> = read(
                scope,
                Some(root(3)),
                |scope| {
                    assert_eq!(scope.root, Some(root(3)));
                    budget.borrow_mut().deferred = Some(root(4));
                    panic_any(payload.clone());
                },
                || (),
            );
        })
    }))
    .unwrap_err();
    assert!(Arc::ptr_eq(
        &payload,
        &panic.downcast::<Arc<Vec<u32>>>().unwrap()
    ));
    assert_eq!(scope.root, Some(root(1)));
    assert!(Rc::ptr_eq(scope.active.as_ref().unwrap(), &original));
    assert_eq!(original.borrow().deferred, Some(root(4)));
}

#[test]
fn owner_and_progress_are_restored_across_real_call_boundaries() {
    for inherited in [false, true] {
        for raises in [false, true] {
            let original = Shared::default();
            let replacement = Shared::default();
            let mut scope = Scope {
                owner: inherited.then(|| original.clone()),
                ..Scope::default()
            };
            let table = Progresses::default();
            let store = Store::Keyed(17);
            let payload = Arc::new(vec![0xd800u32]);
            let result = catch_unwind(AssertUnwindSafe(|| {
                call_scope(&mut scope, &table, &store, "outer", |s, outer| {
                    outer.borrow_mut().deferred = Some(root(3));
                    call_scope(s, &table, &store, "inner", |s, inner| {
                        assert!(Rc::ptr_eq(outer, inner));
                        s.root = Some(root(4));
                        s.active = Some(replacement.clone());
                        s.owner = Some(replacement.clone());
                        if raises {
                            panic_any(payload.clone());
                        }
                        payload.clone()
                    })
                })
            }));
            if raises {
                assert!(Arc::ptr_eq(
                    &payload,
                    &result.unwrap_err().downcast::<Arc<Vec<u32>>>().unwrap()
                ));
            } else {
                assert!(Arc::ptr_eq(&payload, &result.unwrap()));
            }
            assert_eq!(scope.owner.is_some(), inherited);
            if let Some(owner) = scope.owner {
                assert!(Rc::ptr_eq(&owner, &replacement));
            }
            assert!(Rc::ptr_eq(scope.active.as_ref().unwrap(), &replacement));
            assert_eq!(scope.root, Some(root(4)));
            assert_eq!(
                table.peek(&store, "outer"),
                (!inherited).then(|| Some(root(3)))
            );
            assert!(table.peek(&store, "inner").is_none());
        }
    }
}

#[test]
fn fallback_runs_while_the_fresh_budget_is_still_active() {
    for raises in [false, true] {
        let mut scope = Scope {
            root: Some(root(1)),
            ..Scope::default()
        };
        let observed = std::cell::RefCell::new(std::rc::Weak::new());
        let payload = Arc::new(vec![0xdcffu32]);
        let result = catch_unwind(AssertUnwindSafe(|| {
            read(
                &mut scope,
                Some(root(2)),
                |scope| -> Result<usize, Raise<()>> {
                    observed.replace(Rc::downgrade(scope.active.as_ref().unwrap()));
                    Err(Raise::Deadline(ReadDeadline))
                },
                || {
                    assert!(observed.borrow().upgrade().is_some());
                    if raises {
                        panic_any(payload.clone());
                    }
                    19
                },
            )
        }));
        if raises {
            assert!(Arc::ptr_eq(
                &payload,
                &result.unwrap_err().downcast::<Arc<Vec<u32>>>().unwrap()
            ));
        } else {
            assert_eq!(result.unwrap(), Ok(19));
        }
        assert!(scope.active.is_none());
        assert_eq!(scope.root, Some(root(1)));
        assert!(observed.borrow().upgrade().is_none());
    }
}
