//! Short circuits, typed failures and both bounded caches of origin.py.
mod common;
use bettermemory_core::{
    fsdata::{Numbered, OsError, PyErr},
    origin_match::{MatchError, OriginMatcher, WorktreeReads},
    pyvalue::PyStr,
    records::Origin,
    unicode::cps,
};
use std::sync::Arc;

#[derive(Debug, PartialEq, Eq)]
enum Failure {
    Source(usize),
    Match(MatchError),
}
impl From<MatchError> for Failure {
    fn from(e: MatchError) -> Self {
        Self::Match(e)
    }
}

#[derive(Default)]
struct Reads {
    bytes: Option<Vec<u8>>,
    resolved: Option<PyStr>,
    stat: Option<Numbered>,
    reject_resolve: bool,
    reject_log: bool,
    calls: Vec<(&'static str, PyStr)>,
}
impl WorktreeReads for Reads {
    type Error = Failure;
    fn gitfile(&mut self, worktree: &[u32]) -> Result<Option<Vec<u8>>, Failure> {
        self.calls.push(("gitfile", worktree.to_vec()));
        Ok(self.bytes.clone())
    }
    fn resolve(&mut self, prefix: &[u32]) -> Result<Option<PyStr>, Failure> {
        self.calls.push(("resolve", prefix.to_vec()));
        if self.reject_resolve {
            Err(Failure::Source(7))
        } else {
            Ok(self.resolved.clone())
        }
    }
    fn stat(&mut self, worktree: &[u32]) -> Result<(), Numbered> {
        self.calls.push(("stat", worktree.to_vec()));
        self.stat.map_or(Ok(()), Err)
    }
    fn indeterminate(&mut self, worktree: &[u32], error: Numbered) -> Result<(), Failure> {
        self.calls.push(("debug", worktree.to_vec()));
        assert_eq!(self.stat, Some(error));
        if self.reject_log {
            Err(Failure::Source(11))
        } else {
            Ok(())
        }
    }
}

#[test]
fn origins_choose_repository_before_touching_worktrees() {
    let matcher = OriginMatcher::new();
    let (repo, mirror, root, caller) = (
        cps("opaque remote"),
        cps("caller remote"),
        cps("base"),
        cps("linked"),
    );
    let origin = Origin {
        repo: Some(repo.clone()),
        worktree_root: Some(root.clone()),
        ..Origin::default()
    };
    let mut io = Reads {
        bytes: Some(b"gitdir: raw/.git/worktrees/topic".to_vec()),
        resolved: Some(root.clone()),
        ..Reads::default()
    };
    assert!(!matcher
        .should_include_for_caller(Some(&origin), Some(&mirror), Some(&caller), None, &mut io)
        .unwrap());
    assert!(io.calls.is_empty());
    matcher.register_caller_alternates(mirror.clone(), vec![repo]);
    assert!(matcher
        .should_include_for_caller(Some(&origin), Some(&mirror), Some(&caller), None, &mut io)
        .unwrap());
    assert_eq!(
        io.calls,
        vec![("gitfile", caller.clone()), ("resolve", cps("raw"))]
    );
    io.calls.clear();
    assert!(matcher
        .worktrees_match(Some(&root), Some(&caller), &mut io)
        .unwrap());
    assert!(matcher
        .worktrees_match(Some(&root), Some(&root), &mut io)
        .unwrap());
    assert!(matcher
        .worktrees_match(None, Some(&caller), &mut io)
        .unwrap());
    assert!(matcher
        .should_include_for_caller(None, None, None, None, &mut io)
        .unwrap());
    assert!(io.calls.is_empty());
}

#[test]
fn stat_answers_keep_os_error_code_and_debug_exception() {
    let matcher = OriginMatcher::new();
    let memory = cps("older");
    let caller = cps("current");
    let os = |class, errno, winerror| OsError {
        class,
        errno,
        winerror,
    };
    for (error, gone) in [
        (OsError::value(), true),
        (os(PyErr::FileNotFound, Some("ENOENT"), None), true),
        (os(PyErr::Os, Some("ELOOP"), None), true),
        (os(PyErr::Os, Some("ENAMETOOLONG"), None), true),
        (os(PyErr::NotADirectory, None, None), true),
        (os(PyErr::Os, None, Some(3)), true),
        (os(PyErr::FileNotFound, Some("ENOENT"), Some(53)), false),
        (os(PyErr::Permission, Some("EACCES"), None), false),
        (os(PyErr::Timeout, None, None), false),
    ] {
        let mut reads = Reads {
            stat: Some(error.into()),
            ..Reads::default()
        };
        assert_eq!(
            matcher
                .worktrees_match(Some(&memory), Some(&caller), &mut reads)
                .unwrap(),
            gone
        );
        assert_eq!(
            reads
                .calls
                .iter()
                .filter(|(kind, _)| *kind == "debug")
                .count(),
            usize::from(!gone)
        );
        if !gone {
            reads.reject_log = true;
            assert_eq!(
                matcher.worktrees_match(Some(&memory), Some(&caller), &mut reads),
                Err(Failure::Source(11))
            );
        }
    }
}

#[test]
fn negative_lru_and_resolver_error_keep_separate_results() {
    let matcher = OriginMatcher::new();
    let mut io = Reads::default();
    let key = cps("old");
    assert_eq!(matcher.primary_root_of(&key, &mut io).unwrap(), None);
    for i in 0..63 {
        matcher
            .primary_root_of(&cps(&i.to_string()), &mut io)
            .unwrap();
    }
    io.bytes = Some(b"gitdir: back\\.git\\worktrees\\one/.git/worktrees/second".to_vec());
    let before = io.calls.len();
    assert!(matcher.primary_root_of(&key, &mut io).unwrap().is_none());
    assert_eq!(io.calls.len(), before);
    matcher.primary_root_of(&cps("new"), &mut io).unwrap();
    assert!(matcher.primary_root_of(&key, &mut io).unwrap().is_none());
    let raw = cps(r"back\.git\worktrees\one");
    assert_eq!(
        matcher
            .primary_root_of(&cps("0"), &mut io)
            .unwrap()
            .as_deref(),
        Some(raw.as_slice())
    );
    io.reject_resolve = true;
    assert_eq!(
        matcher.primary_root_of(&cps("raises"), &mut io),
        Err(Failure::Source(7))
    );
    io.reject_resolve = false;
    io.resolved = Some(vec![0xdcff]);
    let hot = matcher
        .primary_root_of(&cps("raises"), &mut io)
        .unwrap()
        .unwrap();
    assert_eq!(&*hot, &[0xdcff]);
    assert!(Arc::ptr_eq(
        &hot,
        &matcher
            .primary_root_of(&cps("raises"), &mut io)
            .unwrap()
            .unwrap()
    ));
}

#[cfg(unix)]
#[test]
fn real_logger_exception_preserves_identity() {
    use bettermemory_core::{fsutil::fsdecode, origin_match::NativeWorktrees};
    use std::sync::Arc;
    use std::{fs, os::unix::fs::PermissionsExt};
    // Use the POSIX permission check only when this user cannot search the directory.
    let temp = common::stores::scratch("origin-match-debug");
    let scratch = &temp.0;
    let denied = scratch.join("denied");
    fs::create_dir(&denied).unwrap();
    fs::set_permissions(&denied, fs::Permissions::from_mode(0o000)).unwrap();
    let inaccessible = denied.join("memory");
    let expected = fsdecode(inaccessible.as_os_str());
    let observed = fs::metadata(&inaccessible);
    let token = Arc::new(17);
    #[derive(Debug)]
    struct Callback(Arc<i32>);
    impl From<MatchError> for Callback {
        fn from(_: MatchError) -> Self {
            panic!("unexpected marker error")
        }
    }
    let mut host = NativeWorktrees {
        debug: |path: &[u32], e: Numbered| {
            assert_eq!(path, expected);
            assert_eq!(e.error.class, PyErr::Permission);
            Err(Callback(Arc::clone(&token)))
        },
    };
    let got = OriginMatcher::new().worktrees_match(
        Some(&expected),
        Some(&fsdecode(scratch.as_os_str())),
        &mut host,
    );
    fs::set_permissions(&denied, fs::Permissions::from_mode(0o700)).unwrap();
    if observed.unwrap_err().kind() == std::io::ErrorKind::PermissionDenied {
        assert!(Arc::ptr_eq(&got.unwrap_err().0, &token));
    }
}
