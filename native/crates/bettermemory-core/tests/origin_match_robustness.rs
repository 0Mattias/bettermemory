//! Actual process-local registration, concurrent cold reads and physical linked checkouts.
mod common;
use bettermemory_core::{
    fsdata::Numbered,
    origin_match::{process_matcher, MatchError, OriginMatcher, WorktreeReads},
    pyvalue::PyStr,
    unicode::cps,
};
use std::sync::{mpsc, Arc};

#[test]
fn process_registry_is_shared_and_the_fifo_survives_concurrent_registration() {
    let owner = process_matcher();
    owner.register_caller_alternates(cps("current"), vec![cps("historical")]);
    assert!(process_matcher().repos_match(Some(&cps("historical")), Some(&cps("current")), None));
    assert!(!OriginMatcher::new().repos_match(
        Some(&cps("historical")),
        Some(&cps("current")),
        None
    ));
    let matcher = OriginMatcher::new();
    std::thread::scope(|scope| {
        for t in 0..8 {
            let m = &matcher;
            scope.spawn(move || {
                for n in 0..128 {
                    m.register_caller_alternates(
                        cps(&format!("raw-{t}-{n}")),
                        vec![cps("known mirror")],
                    );
                }
            });
        }
    });
    let mut remaining = 0;
    for t in 0..8 {
        for n in 0..128 {
            remaining += usize::from(matcher.repos_match(
                Some(&cps("known mirror")),
                Some(&cps(&format!("raw-{t}-{n}"))),
                None,
            ));
        }
    }
    assert_eq!(remaining, 32);
}

struct Overlap {
    value: PyStr,
    start: Option<mpsc::SyncSender<()>>,
    wait: Option<mpsc::Receiver<()>>,
}
impl WorktreeReads for Overlap {
    type Error = MatchError;
    fn gitfile(&mut self, _: &[u32]) -> Result<Option<Vec<u8>>, MatchError> {
        if let Some(start) = self.start.take() {
            start.send(()).unwrap();
        }
        if let Some(wait) = self.wait.take() {
            wait.recv_timeout(std::time::Duration::from_secs(5))
                .unwrap();
        }
        Ok(Some(b"gitdir: prefix/.git/worktrees/x".to_vec()))
    }
    fn resolve(&mut self, _: &[u32]) -> Result<Option<PyStr>, MatchError> {
        Ok(Some(self.value.clone()))
    }
    fn stat(&mut self, _: &[u32]) -> Result<(), Numbered> {
        unreachable!()
    }
    fn indeterminate(&mut self, _: &[u32], _: Numbered) -> Result<(), MatchError> {
        unreachable!()
    }
}

#[test]
fn overlapping_primary_misses_keep_the_first_completion_in_cache() {
    let m = OriginMatcher::new();
    let key = cps("shared spelling");
    let (began, lookup) = mpsc::sync_channel(0);
    let (finished, release) = mpsc::sync_channel(0);
    std::thread::scope(|scope| {
        let m = &m;
        let key = &key;
        let slow = scope.spawn(move || {
            m.primary_root_of(
                key,
                &mut Overlap {
                    value: cps("slow"),
                    start: Some(began),
                    wait: Some(release),
                },
            )
            .unwrap()
            .unwrap()
        });
        lookup.recv().unwrap();
        let mut other = Overlap {
            value: cps("fast"),
            start: None,
            wait: None,
        };
        let fast = m.primary_root_of(key, &mut other).unwrap().unwrap();
        finished.send(()).unwrap();
        assert_eq!(&*slow.join().unwrap(), cps("slow"));
        assert_eq!(&*fast, cps("fast"));
        let cached = m.primary_root_of(key, &mut other).unwrap().unwrap();
        assert!(Arc::ptr_eq(&fast, &cached));
    });
}

#[cfg(unix)]
#[test]
fn a_real_linked_checkout_and_missing_worktree_use_the_native_entry() {
    use bettermemory_core::{fsutil::fsdecode, origin_match::NativeWorktrees};
    use std::{fs, process::Command};
    let tmp = common::stores::scratch("origin-match-linked");
    let root = tmp.0.join("primary");
    let linked = tmp.0.join("linked");
    let git = |args: &[&str]| {
        let out = Command::new("git")
            .env_clear()
            .env("PATH", std::env::var_os("PATH").unwrap())
            .env("HOME", &tmp.0)
            .env("GIT_CONFIG_NOSYSTEM", "1")
            .current_dir(&tmp.0)
            .args(args)
            .output()
            .unwrap();
        assert!(
            out.status.success(),
            "{}",
            String::from_utf8_lossy(&out.stderr)
        );
    };
    git(&["init", "-q", "primary"]);
    git(&[
        "-C",
        "primary",
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "--allow-empty",
        "-qm",
        "fixture",
    ]);
    git(&[
        "-C",
        "primary",
        "worktree",
        "add",
        "-q",
        "--detach",
        "../linked",
    ]);
    let matcher = OriginMatcher::new();
    let mut reads = NativeWorktrees::default();
    let primary = fsdecode(root.as_os_str());
    let caller = fsdecode(linked.as_os_str());
    assert!(matcher
        .worktrees_match(Some(&primary), Some(&caller), &mut reads)
        .unwrap());
    assert!(!matcher
        .worktrees_match(Some(&caller), Some(&primary), &mut reads)
        .unwrap());
    let missing = fsdecode(tmp.0.join("former checkout").as_os_str());
    assert!(matcher
        .worktrees_match(Some(&missing), Some(&caller), &mut reads)
        .unwrap());
    let frozen = matcher
        .primary_root_of(&caller, &mut reads)
        .unwrap()
        .unwrap();
    fs::write(linked.join(".git"), b"gitdir: changed\n").unwrap();
    assert!(Arc::ptr_eq(
        &frozen,
        &matcher
            .primary_root_of(&caller, &mut reads)
            .unwrap()
            .unwrap()
    ));
    assert!(OriginMatcher::new()
        .primary_root_of(&caller, &mut reads)
        .unwrap()
        .is_none());
}

#[cfg(unix)]
#[test]
fn reads_every_chunk_and_propagates_the_parsed_nul_at_resolve_time() {
    use bettermemory_core::{fsutil::fsdecode, origin_match::NativeWorktrees};
    use std::fs;
    let tmp = common::stores::scratch("origin-match-large");
    let key = fsdecode(tmp.0.as_os_str());
    let mut marker = vec![b'#'; 3 * 65_536];
    marker.extend_from_slice(b"\r\ngitdir: somewhere\0bad/.git/worktrees/x\n");
    fs::write(tmp.0.join(".git"), marker).unwrap();
    let m = OriginMatcher::new();
    assert_eq!(
        m.primary_root_of(&key, &mut NativeWorktrees::default()),
        Err(MatchError::Value("lstat: embedded null character in path"))
    );
    fs::write(tmp.0.join(".git"), b"now a normal marker without a link").unwrap();
    assert!(m
        .primary_root_of(&key, &mut NativeWorktrees::default())
        .unwrap()
        .is_none());
}
