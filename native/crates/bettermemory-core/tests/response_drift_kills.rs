//! Cases a plausible wrong drift pass fails: a key part dropped, the
//! signature check skipped, a failed store that does not break the pass,
//! a CALL_BUDGET or a mixed failure kept, an empty `anchor_paths`
//! ignored, a naive stamp converted instead of replaced. The last module
//! runs the POSIX adapter over a real repository that moves between a
//! pass's lookups and its store.
mod common;
mod response_drift_script;

mod verify_count_script;
mod verify_weak_script;

use bettermemory_core::attestation_paths::Record;
use bettermemory_core::caches;
use bettermemory_core::datetime::DateTime;
use bettermemory_core::git_read_budget::Scope;
use bettermemory_core::origin_history::{ReachableWalk, Unwalked, Walked, WalkedValue};
use bettermemory_core::response_drift::{
    clear_drift_memo, drift_memo, DriftKey, DriftPass, HitResolver, LookupError, Memo, Row,
    CLEAR_KEY,
};
use bettermemory_core::verify::count::CountReads;
use bettermemory_core::verify::literal_lines::LiteralReader;
use bettermemory_core::verify::weak::{Place, WeakError};
use response_drift_script::{attributes, cps, replay, shared, text, walk_files, Admit, Reads};
use serde_json::{json, Value};
use std::cell::RefCell;
use std::collections::hash_map::DefaultHasher;
use std::hash::{Hash, Hasher};
use std::sync::Arc;
use verify_count_script::Counted;
use verify_weak_script::literal;

fn state(over: Value) -> Value {
    let mut st = json!({
        "files": "H", "git": "H", "moved": false, "head": "H", "dates": [2, 3],
        "sig": "s1", "attrs": 1, "chains": {}, "walk": 1, "held": true,
        "deny": [], "now": 100.0, "raise": "", "error": "RuntimeError"
    });
    for (k, v) in over.as_object().unwrap() {
        st[k] = v.clone();
    }
    st
}

fn run(over: Value, steps: Value) -> (Vec<Value>, Vec<Value>) {
    let st = state(over);
    let case = json!({"cap": 5000, "cwd": "/r/s", "at_home": false, "state": st, "steps": steps});
    let out = replay(&case);
    (
        out[0].as_array().unwrap().clone(),
        out[1]
            .as_array()
            .unwrap()
            .iter()
            .map(|r| r[0][1].clone())
            .collect(),
    )
}

fn resolves(calls: &[Value]) -> usize {
    calls.iter().filter(|c| c[0] == "resolve").count()
}

fn since(text: &str) -> DateTime {
    DateTime::from_isoformat(&cps(text)).unwrap()
}

fn base() -> DriftKey {
    DriftKey {
        root: cps("/r"),
        head: cps("H"),
        cwd: cps("/r/s"),
        anchors: vec![cps("src/a.py")],
        claims: vec![],
        since: since("2024-01-02T03:04:05+00:00"),
        verified_head: None,
        attributes: None,
        walk_files: walk_files(1),
    }
}

fn hash(k: &DriftKey) -> u64 {
    let mut h = DefaultHasher::new();
    k.hash(&mut h);
    h.finish()
}

#[test]
fn every_key_part_tells_keys_apart() {
    let b = base();
    let chain = vec![Some([1, 0, 0, 0, 0])];
    let variants: Vec<(&str, DriftKey)> = vec![
        (
            "root",
            DriftKey {
                root: cps("/q"),
                ..base()
            },
        ),
        (
            "head",
            DriftKey {
                head: cps("G"),
                ..base()
            },
        ),
        (
            "cwd",
            DriftKey {
                cwd: cps("/r/t"),
                ..base()
            },
        ),
        (
            "anchors",
            DriftKey {
                anchors: vec![cps("src/b.py")],
                ..base()
            },
        ),
        (
            "claims",
            DriftKey {
                claims: vec![cps("  ")],
                ..base()
            },
        ),
        (
            "since",
            DriftKey {
                since: since("2024-01-02T03:04:06+00:00"),
                ..base()
            },
        ),
        (
            "naive since",
            DriftKey {
                since: since("2024-01-02T03:04:05"),
                ..base()
            },
        ),
        (
            "verified head",
            DriftKey {
                verified_head: Some(cps("")),
                ..base()
            },
        ),
        (
            "attributes",
            DriftKey {
                attributes: Some((attributes(1), chain.clone())),
                ..base()
            },
        ),
        (
            "walk files",
            DriftKey {
                walk_files: walk_files(2),
                ..base()
            },
        ),
    ];
    for (part, v) in &variants {
        assert_ne!(&b, v, "{part}");
    }
    let with = |a: i64, c: i128| DriftKey {
        attributes: Some((attributes(a), vec![Some([c, 0, 0, 0, 0])])),
        ..base()
    };
    assert_ne!(with(1, 1), with(2, 1), "the repository's attribute files");
    assert_ne!(with(1, 1), with(1, 2), "the governed files' chain");
    // The same instant under another offset is the same key, as aware
    // datetimes compare and hash.
    let shifted = DriftKey {
        since: since("2024-01-02T04:04:05+01:00"),
        ..base()
    };
    assert_eq!(b, shifted);
    assert_eq!(hash(&b), hash(&shifted));
}

#[test]
fn a_row_differing_in_one_part_resolves_again() {
    let lk = |opts: Value| json!(["lookup", opts]);
    let rows = [
        ("anchors", json!({"anchors": ["src/b.py"]})),
        ("raw claims", json!({"claims": ["  "]})),
        ("since", json!({"since": "2024-01-02T03:04:06+00:00"})),
        ("verified head", json!({"vh": ""})),
        ("raw claims, governed", json!({"claims": ["src/a.py"]})),
    ];
    for (part, opts) in rows {
        let (calls, _) = run(
            json!({}),
            json!([["open"], lk(json!({})), lk(opts.clone()), lk(opts)]),
        );
        assert_eq!(resolves(&calls), 2, "{part}");
    }
    // The parts a pass reads: the head (files), the directory, the walk
    // files and the attribute files' tokens, each in a second pass.
    for (part, change) in [
        ("head", json!(["set", "files", "G"])),
        ("walk files", json!(["set", "walk", 2])),
        ("cwd", json!(["set", "none", 0])),
    ] {
        let reopen = if part == "cwd" {
            json!(["open", "/r/t"])
        } else {
            json!(["open"])
        };
        let (calls, _) = run(
            json!({}),
            json!([
                ["open"],
                lk(json!({})),
                ["store"],
                change,
                reopen,
                lk(json!({}))
            ]),
        );
        assert_eq!(resolves(&calls), 2, "{part}");
    }
    let governed = json!({"claims": ["src/a.py"]});
    for (part, name) in [("repository attributes", "attrs"), ("chain", "chains")] {
        let value = if name == "attrs" {
            json!(2)
        } else {
            json!({"src/a.py": 2})
        };
        let (calls, _) = run(
            json!({}),
            json!([
                ["open"],
                lk(governed.clone()),
                ["store"],
                ["set", name, value],
                ["open"],
                lk(governed.clone())
            ]),
        );
        assert_eq!(resolves(&calls), 2, "{part}");
    }
}

#[test]
fn a_moved_signature_keeps_nothing() {
    let steps = |sig: &str| {
        json!([
            ["open"],
            ["lookup", {}],
            ["set", "sig", sig],
            ["store"],
            ["open"],
            ["lookup", {}]
        ])
    };
    let (calls, answers) = run(json!({}), steps("s2"));
    assert_eq!(answers[3], json!(false));
    assert_eq!(resolves(&calls), 2);
    let (calls, answers) = run(json!({}), steps("s1"));
    assert_eq!(answers[3], json!(true));
    assert_eq!(resolves(&calls), 1);
}

#[test]
fn a_failed_store_breaks_the_pass() {
    let (calls, answers) = run(
        json!({}),
        json!([
            ["open"], ["lookup", {"vh": "A"}], ["set", "held", false], ["store"],
            ["set", "held", true], ["store"], ["lookup", {"vh": "B"}], ["store"],
            ["open"], ["lookup", {"vh": "B"}]
        ]),
    );
    assert_eq!(answers[3], json!(false));
    assert_eq!(answers[5], json!(false), "nothing held, a broken pass");
    assert_eq!(answers[7], json!(false), "every later store fails");
    assert_eq!(resolves(&calls), 3, "B was never merged");
}

#[test]
fn call_budget_and_mixed_failures_are_not_kept() {
    let lk = |res: Value| json!(["lookup", {"res": res}]);
    for (what, res, kept) in [
        ("call budget", json!({"v": "call_budget"}), json!([[], []])),
        (
            "a deadline",
            json!({"raise": "ReadDeadline"}),
            json!([[], []]),
        ),
        ("a failure", json!({"fail": 1}), json!([[], []])),
        (
            "a mixed failure",
            json!({"fail": 1, "tout": 1, "v": "timeout"}),
            json!([[], []]),
        ),
    ] {
        let (_, answers) = run(json!({}), json!([["open"], lk(res), ["obs"]]));
        assert_eq!(json!([answers[2][0], answers[2][1]]), kept, "{what}");
    }
    let (_, answers) = run(
        json!({}),
        json!([["open"], lk(json!({"tout": 2, "v": "timeout"})), ["obs"]]),
    );
    assert_eq!(answers[1], json!([true, "timeout"]));
    assert_eq!(answers[2][0], json!([]));
    assert_eq!(
        answers[2][1].as_array().unwrap().len(),
        1,
        "every failure a timeout"
    );
}

#[test]
fn an_empty_anchor_paths_replaces_the_anchors() {
    let (calls, answers) = run(json!({}), json!([["open"], ["lookup", {"anchors": []}]]));
    assert_eq!(answers[1], json!([true, null]));
    assert_eq!(resolves(&calls), 0);
    let (calls, _) = run(json!({}), json!([["open"], ["lookup", {"anchors": null}]]));
    assert_eq!(resolves(&calls), 1);
}

#[test]
fn a_naive_stamp_is_read_as_utc_not_converted() {
    let (calls, answers) = run(
        json!({}),
        json!([["open"], ["lookup", {"since": "2024-01-02T23:30:00"}], ["obs"]]),
    );
    let resolve = calls.iter().find(|c| c[0] == "resolve").unwrap();
    assert_eq!(resolve[6], json!("2024-01-02T23:30:00+00:00"));
    assert_eq!(answers[2][0][0][0][5], json!("2024-01-02T23:30:00+00:00"));
}

#[test]
fn the_process_memo_is_registered_for_clearing() {
    let memo = drift_memo();
    assert!(caches::keys().contains(&CLEAR_KEY));
    memo.remember(&[(base(), None)], &[(base(), None)], 1.0);
    assert_eq!(memo.memo_keys().len(), 1);
    caches::clear_all();
    assert!(memo.memo_keys().is_empty());
    assert!(memo.timed_out_keys().is_empty());
    memo.remember(&[(base(), None)], &[], 1.0);
    clear_drift_memo();
    assert!(memo.memo_keys().is_empty());
}

/// `HitResolver`, the resolution a search uses: the walk is handed the
/// pass's directory and located root (not `count.at`), the row's
/// `verified_head` as the anchor and the located head, and the pass's own
/// walked map.
#[test]
fn hit_resolver_hands_the_walk_the_pass_place_and_heads() {
    let cell = shared(&state(json!({})));
    let mut reads = Reads(cell.clone());
    let memo = Memo::new();
    let mut scope = Scope {
        root: Some((cps("outside"), true)),
        ..Scope::default()
    };
    let mut pass = DriftPass::open(
        &mut reads,
        &mut scope,
        cps("/r/s"),
        false,
        Admit(cell.clone()),
        &memo,
    )
    .unwrap();
    let git = Counted::new(&json!({}));
    let lit = LiteralReader::default();
    let elsewhere = cps("/elsewhere");
    let count = CountReads {
        git: &git,
        at: Place {
            cwd: &elsewhere,
            toplevel: Some(&elsewhere),
        },
        literal_pathspecs: &literal,
        lit: &lit,
    };
    let mut unwalked = Unwalked::new();
    let seen = RefCell::new(Vec::new());
    let mut walk = |walked: &mut Walked,
                    at: Place,
                    anchor: &[u32],
                    head: &[u32],
                    _: &mut Unwalked|
     -> Result<Option<Arc<ReachableWalk>>, WeakError> {
        seen.borrow_mut().push(json!([
            text(at.cwd),
            at.toplevel.map(text),
            text(anchor),
            text(head),
            walked.len()
        ]));
        walked.insert(
            (cps("/r"), anchor.to_vec(), head.to_vec()),
            WalkedValue::Failed(true),
        );
        Err(WeakError::IntTooLong)
    };
    let mut resolver = HitResolver {
        count,
        unwalked: &mut unwalked,
        walk: &mut walk,
    };
    let record = Record {
        verified_paths: vec![cps("src/a.py")],
        ..Record::default()
    };
    let stamp = DateTime::from_isoformat(&cps(response_drift_script::SINCE)).unwrap();
    for (i, vh) in ["V", "W"].into_iter().enumerate() {
        let vh = cps(vh);
        let row = Row {
            id: "m1",
            origin_repo: true,
            body: &[],
            paths: &record,
            claims: &[],
            verified_head: Some(&vh),
        };
        let got = pass.lookup(
            &mut reads,
            &mut scope,
            &row,
            Some(&stamp),
            true,
            None,
            &mut resolver,
        );
        assert!(
            matches!(got, Err(LookupError::Drift(WeakError::IntTooLong))),
            "{got:?}"
        );
        assert_eq!(
            pass.walked().len(),
            i + 1,
            "the pass's map is the one walked"
        );
    }
    assert_eq!(
        seen.into_inner(),
        vec![
            json!(["/r/s", "/r", "V", "H", 0]),
            json!(["/r/s", "/r", "W", "H", 1])
        ]
    );
}

/// The home and the user table the anchors read: `~` and `~u` spellings
/// compare by their expansion (`_normalize_for_compare`), so each dedups
/// against the absolute path it expands to. The reference answers
/// `('~/proj/src/x.py',)` under HOME=/h and `('~u/src/y.py',)` with
/// `pwd.getpwnam('u').pw_dir == '/hu'` (and raises RuntimeError for an
/// unknown user).
#[test]
fn the_anchors_expand_against_the_home_and_the_user_table() {
    let resolved_anchors = |calls: &[Value]| {
        calls
            .iter()
            .find(|c| c[0] == "resolve")
            .map(|c| c[4].clone())
    };
    let (calls, _) = run(
        json!({"home": "/h"}),
        json!([["open"], ["lookup", {"paths": ["~/proj/src/x.py", "/h/proj/src/x.py"]}]]),
    );
    assert_eq!(resolved_anchors(&calls), Some(json!(["~/proj/src/x.py"])));
    let absent = json!({"paths": [], "absent": ["~u/src/y.py", "/hu/src/y.py"]});
    let (calls, _) = run(
        json!({"users": {"u": "/hu"}}),
        json!([["open"], ["lookup", absent.clone()]]),
    );
    assert_eq!(resolved_anchors(&calls), Some(json!(["~u/src/y.py"])));
    let (calls, answers) = run(json!({}), json!([["open"], ["lookup", absent]]));
    assert_eq!(answers[1], json!("Anchors"), "an unknown user raises");
    assert_eq!(resolved_anchors(&calls), None);
}

/// The tallies are read around the resolution alone: a git call that
/// failed while the row was keyed (git's pair, read before the tallies)
/// does not keep the row out, one that failed inside the resolution does.
#[test]
fn the_tallies_count_only_the_resolution() {
    let (_, answers) = run(
        json!({"files": null, "fails": {"git": 1}}),
        json!([["open"], ["lookup", {}], ["obs"]]),
    );
    assert_eq!(answers[2][0].as_array().unwrap().len(), 1, "kept");
    let (_, answers) = run(
        json!({"fails": {"dates": 1}}),
        json!([["open"], ["lookup", {"res": {"ts": true}}], ["obs"]]),
    );
    assert_eq!(answers[2][0], json!([]), "a failure in the resolution");
    assert_eq!(answers[2][1], json!([]));
}

/// The POSIX adapter over a real repository: a pass stores while the
/// repository holds still, and not after a commit, a rewritten config or
/// a `.gitattributes` beside a governed file.
#[cfg(unix)]
mod unix {
    use crate::common::origin_dates_memo::Step;
    use crate::common::origin_history_located::Scratch;
    use crate::response_drift_script::{cps, text, Admit};
    use bettermemory_core::attestation_paths::Record;
    use bettermemory_core::datetime::DateTime;
    use bettermemory_core::fsdata::{Flavour, StdFs};
    use bettermemory_core::git_read_budget::Scope;
    use bettermemory_core::gitfile_rule::bounded::Hung;
    use bettermemory_core::gitfile_rule::discovery::Rule;
    use bettermemory_core::gitfile_rule::follow::NoRecords;
    use bettermemory_core::gitfile_rule::format::GitFacts;
    use bettermemory_core::gitfile_rule::gitdir::Ctx;
    use bettermemory_core::githead::discovery::{Env, Host};
    use bettermemory_core::githead::signature::First;
    use bettermemory_core::origin_dates::memo::Timestamps;
    use bettermemory_core::origin_dates::{GitResult, OriginGit};
    use bettermemory_core::origin_gitfiles::attrs::IndexMemo;
    use bettermemory_core::origin_gitfiles::facts::Held;
    use bettermemory_core::origin_gitfiles::facts::NotPorted;
    use bettermemory_core::origin_gitfiles::root::ConfigMemo;
    use bettermemory_core::origin_history::Walked;
    use bettermemory_core::origin_history::{Key, TimestampsMemo, ValueError};
    use bettermemory_core::pyvalue::PyStr;
    use bettermemory_core::response_drift::posix::{Beside, DriftOrigin, Uncaught};
    use bettermemory_core::response_drift::{
        DriftPass, DriftReads, HitArgs, LookupError, Memo, Resolve, Row,
    };
    use bettermemory_core::response_history::posix::{OriginReads, UnsupportedPath};
    use bettermemory_core::response_history::Read;
    use bettermemory_core::verify::hit::HitDrift;
    use bettermemory_core::verify::status::ResolvedCommitDrift;
    use bettermemory_core::verify_candidates::scan::UserHome;
    use serde_json::json;
    use std::cell::Cell;
    use std::os::unix::ffi::OsStrExt;
    use std::os::unix::fs::MetadataExt;
    use std::path::Path;
    use std::process::Command;
    use std::rc::Rc;

    fn git(cwd: &Path, args: &[&str]) -> String {
        let out = Command::new("git")
            .env_clear()
            .env("PATH", std::env::var_os("PATH").unwrap_or_default())
            .env("GIT_CONFIG_NOSYSTEM", "1")
            .env("GIT_CONFIG_GLOBAL", "/dev/null")
            .env("GIT_AUTHOR_NAME", "tester")
            .env("GIT_AUTHOR_EMAIL", "test@example.invalid")
            .env("GIT_COMMITTER_NAME", "tester")
            .env("GIT_COMMITTER_EMAIL", "test@example.invalid")
            .current_dir(cwd)
            .args(args)
            .output()
            .unwrap();
        assert!(out.status.success(), "git {args:?}");
        String::from_utf8(out.stdout).unwrap()
    }
    fn commit(dir: &Path, message: &str) {
        git(
            dir,
            &[
                "-c",
                "commit.gpgsign=false",
                "commit",
                "--allow-empty",
                "-qm",
                message,
            ],
        );
    }

    #[derive(Debug)]
    #[allow(dead_code)]
    enum Error {
        Cut,
        Unsupported(UnsupportedPath),
        NotPorted,
        Uncaught(&'static str),
    }
    impl From<ValueError> for Error {
        fn from(_: ValueError) -> Self {
            Error::Cut
        }
    }
    impl From<UnsupportedPath> for Error {
        fn from(e: UnsupportedPath) -> Self {
            Error::Unsupported(e)
        }
    }
    impl From<NotPorted> for Error {
        fn from(_: NotPorted) -> Self {
            Error::NotPorted
        }
    }
    impl From<Uncaught> for Error {
        fn from(e: Uncaught) -> Self {
            Error::Uncaught(e.0)
        }
    }

    /// No date is read: the scripted resolution reads none.
    struct NoGit;
    impl OriginGit for NoGit {
        fn git(&mut self, _: &[u8], _: &[PyStr], _: f64, _: bool, _: bool) -> Option<PyStr> {
            panic!("no git process")
        }
        fn git_result(&mut self, _: &[u8], _: &[PyStr], _: f64) -> Option<GitResult> {
            panic!("no git process")
        }
        fn counts(&self) -> (i64, i64) {
            (0, 0)
        }
        fn note_failed(&mut self) {}
    }

    type Move = Box<dyn Fn()>;

    struct One;
    impl Resolve<Error> for One {
        fn resolve(
            &mut self,
            _: HitArgs<'_>,
            _: &mut Walked,
            _: &mut dyn FnMut() -> Result<Timestamps, Error>,
        ) -> Result<HitDrift, LookupError<Error>> {
            Ok(Some(Ok(ResolvedCommitDrift::new(1))))
        }
    }

    type Reads<'r> = dyn DriftReads<Error = Error> + 'r;

    /// How the adapter is set up over a repository.
    struct Knobs {
        /// The files settle the pair; else git's pair keys the rows.
        files_settle: bool,
        /// Added to the directory's owner as the effective user.
        euid_shift: u32,
        /// GIT_INDEX_FILE is set, read at each call.
        index_file_set: Rc<Cell<bool>>,
    }

    impl Default for Knobs {
        fn default() -> Self {
            Knobs {
                files_settle: true,
                euid_shift: 0,
                index_file_set: Rc::new(Cell::new(false)),
            }
        }
    }

    /// A repository with one commit and a `src` directory.
    fn repository(tag: &str) -> Scratch {
        let dir = Scratch::new(tag);
        std::fs::create_dir(dir.0.join("src")).unwrap();
        git(&dir.0, &["init", "-q", "--initial-branch=main"]);
        commit(&dir.0, "first");
        dir
    }

    /// Runs `body` over the POSIX adapter for the repository `dir`.
    fn with_reads(dir: &Path, knobs: &Knobs, body: &mut dyn FnMut(&mut Reads<'_>)) {
        let home = Scratch::new("drift-keys-home");
        let ctx = Ctx {
            fs: &StdFs,
            flavour: Flavour::Posix,
            cwd: dir.as_os_str().as_bytes(),
        };
        let rule = Rule {
            ctx: &ctx,
            answers: &Hung::default(),
            records: &NoRecords,
            env: None,
            git: &GitFacts {
                release: Some((2, 54)),
                reads_compat: Some(true),
            },
        };
        let host = Host {
            euid: std::fs::metadata(dir).unwrap().uid() + knobs.euid_shift,
            owners: &StdFs,
        };
        let state = Step::new(&json!({"g":[],"l":{"cuts":[[],[],[]],"held":[true,true]}}));
        let mut memo = TimestampsMemo::new();
        let make_env = Env::default;
        let root = dir.to_path_buf();
        let rev_parse = move || {
            let out = git(&root, &["rev-parse", "--show-toplevel", "HEAD"]);
            let mut lines = out.lines();
            Some((cps(lines.next().unwrap()), cps(lines.next().unwrap())))
        };
        let settle = knobs.files_settle;
        let by_files = rev_parse.clone();
        let mut files = move |_: &mut Scope, _: &[u32]| -> Read<Option<Key>, Error> {
            Ok(if settle { by_files() } else { None })
        };
        let mut pair = move |_: &mut Scope, _: &[u32]| -> Read<Option<Key>, Error> {
            assert!(!settle, "the files settle the pair");
            Ok(rev_parse())
        };
        let mut no_git = NoGit;
        let home_bytes = home.0.as_os_str().as_bytes().to_vec();
        let index_file_set = knobs.index_file_set.clone();
        let beside = move || Beside {
            index_file_set: index_file_set.get(),
            xdg_config_home: None,
            home: Some(home_bytes.clone()),
        };
        let mut users = |_: &[u32]| UserHome::Unknown;
        let mut config = ConfigMemo::new();
        let mut index = IndexMemo::new();
        let mut reads = DriftOrigin {
            origin: OriginReads::<_, _, _, _, _, Error> {
                files_pair: &mut files,
                git_pair: &mut pair,
                env: &make_env,
                rule: &rule,
                reader: bettermemory_core::origin_dates::memo::Reader {
                    git: &mut no_git,
                    files: &mut state.clone(),
                    leaves: &mut state.clone(),
                    hold: &mut state.clone(),
                    memo: &mut memo,
                    clock: &|| 0.0,
                },
            },
            host: &host,
            config: &mut config,
            index: &mut index,
            beside: &beside,
            calls: &|| (0, 0),
            clock: &|| 5.0,
            home: None,
            users: &mut users,
        };
        body(&mut reads);
    }

    /// One pass over `dir`: a lookup of one row (governed by `src/a.py`
    /// where `governed`), `change`, then the store. Answers whether the row
    /// was kept for the store and what the store answered.
    fn pass(reads: &mut Reads<'_>, dir: &Path, governed: bool, change: &dyn Fn()) -> (usize, bool) {
        let cell = crate::response_drift_script::shared(&json!({"deny": []}));
        let drift = Memo::new();
        let mut scope = Scope::default();
        let record = Record {
            verified_paths: vec![cps("src/a.py")],
            ..Record::default()
        };
        let claims = if governed {
            vec![cps("src/a.py")]
        } else {
            vec![]
        };
        let stamp = DateTime::from_isoformat(&cps("2024-01-02T03:04:05")).unwrap();
        let row = Row {
            id: "m1",
            origin_repo: true,
            body: &[],
            paths: &record,
            claims: &claims,
            verified_head: None,
        };
        let cwd = cps(dir.to_str().unwrap());
        let mut pass =
            DriftPass::open(&mut *reads, &mut scope, cwd, false, Admit(cell), &drift).unwrap();
        let found = pass
            .lookup(
                &mut *reads,
                &mut scope,
                &row,
                Some(&stamp),
                true,
                None,
                &mut One,
            )
            .unwrap();
        assert!(found.0);
        let unstored = pass.unstored();
        if unstored > 0 {
            let [kept, _] = pass.kept();
            assert_eq!(text(&kept[0].0.root), dir.to_str().unwrap());
            assert_eq!(kept[0].0.attributes.is_some(), governed);
        }
        change();
        let stored = pass.store(&mut *reads, &mut scope).unwrap();
        assert_eq!(drift.memo_keys().len(), usize::from(stored && unstored > 0));
        (unstored, stored)
    }

    #[test]
    fn stores_only_while_the_repository_holds_still() {
        let dir = repository("drift-keys");
        let mut moves: Vec<(&str, Move)> = vec![("still", Box::new(|| {}))];
        let d = dir.0.clone();
        moves.push(("a commit", Box::new(move || commit(&d, "next"))));
        let d = dir.0.clone();
        moves.push((
            "a rewritten config",
            Box::new(move || {
                git(&d, &["config", "drift.test", "yes"]);
            }),
        ));
        let d = dir.0.clone();
        moves.push((
            "a .gitattributes on the governed path",
            Box::new(move || std::fs::write(d.join("src/.gitattributes"), "*.py diff\n").unwrap()),
        ));
        with_reads(&dir.0, &Knobs::default(), &mut |reads| {
            for (what, change) in &moves {
                let (unstored, stored) = pass(reads, &dir.0, true, change);
                assert_eq!(unstored, 1, "{what}: the row is keyed");
                assert_eq!(stored, *what == "still", "{what}");
            }
        });
    }

    /// A `.gitattributes` rewritten in place (no directory entry moves, so
    /// no held directory's stamp does): the governed files' chain alone
    /// fails the store.
    #[test]
    fn an_attributes_file_rewritten_in_place_fails_the_store_on_the_chain() {
        let dir = repository("drift-keys-chain");
        let attributes = dir.0.join("src/.gitattributes");
        std::fs::write(&attributes, "*.py diff\n").unwrap();
        with_reads(&dir.0, &Knobs::default(), &mut |reads| {
            assert_eq!(pass(reads, &dir.0, true, &|| {}), (1, true), "still");
            let rewrite = || std::fs::write(&attributes, "*.py -diff -text\n").unwrap();
            assert_eq!(pass(reads, &dir.0, true, &rewrite), (1, false));
        });
    }

    /// GIT_INDEX_FILE set leaves the walk files unread, so a row is not
    /// keyed while it is set, and a store after it was set fails.
    #[test]
    fn git_index_file_unkeys_the_rows_and_fails_the_store() {
        let dir = repository("drift-keys-index");
        let knobs = Knobs::default();
        let set = knobs.index_file_set.clone();
        with_reads(&dir.0, &knobs, &mut |reads| {
            set.set(true);
            assert_eq!(pass(reads, &dir.0, false, &|| {}).0, 0, "set at the lookup");
            set.set(false);
            assert_eq!(pass(reads, &dir.0, false, &|| {}), (1, true), "unset");
            assert_eq!(
                pass(reads, &dir.0, false, &|| set.set(true)),
                (1, false),
                "set before the store"
            );
            set.set(false);
        });
    }

    /// A pass keyed by git's pair (the files settle none) reads the
    /// signature as `locate` reads, without the ownership check: under an
    /// effective user that owns nothing there, discovery is unsettled
    /// (`_check_owner` raises `_Unsettled`, an undetermined signature) and
    /// `locate` finds the git directory.
    #[test]
    fn a_pass_keyed_by_gits_pair_reads_the_signature_through_locate() {
        let dir = repository("drift-keys-owner");
        let git_dir = dir.0.join(".git");
        let knobs = Knobs {
            files_settle: false,
            euid_shift: 1,
            ..Knobs::default()
        };
        with_reads(&dir.0, &knobs, &mut |reads| {
            let mut scope = Scope::default();
            let cwd = cps(dir.0.to_str().unwrap());
            let located = reads.signature(&mut scope, &cwd, false).unwrap();
            assert_eq!(
                located.first,
                First::GitDir(git_dir.as_os_str().as_bytes().to_vec())
            );
            let owned = reads.signature(&mut scope, &cwd, true).unwrap();
            assert!(matches!(owned.first, First::Undetermined(_)), "{owned:?}");
        });
        let knobs = Knobs {
            files_settle: false,
            ..Knobs::default()
        };
        with_reads(&dir.0, &knobs, &mut |reads| {
            assert_eq!(pass(reads, &dir.0, false, &|| {}), (1, true), "git's pair");
        });
    }

    /// Paths the file system cannot encode: a governed path answers a None
    /// chain before any directory is held (os.lstat's ValueError), and a
    /// directory an undetermined signature (the reference catches the
    /// ValueError: `(<undetermined>, None, None, None, None, None)` and
    /// the environment).
    #[test]
    fn unencodable_paths_answer_none_and_an_undetermined_signature() {
        let dir = repository("drift-keys-unencodable");
        with_reads(&dir.0, &Knobs::default(), &mut |reads| {
            let mut scope = Scope::default();
            let root = cps(dir.0.to_str().unwrap());
            let mut held = Held::new();
            let chain = reads
                .attribute_chain(&mut scope, &root, &[vec![0x61, 0xD800]], &mut held)
                .unwrap();
            assert_eq!(chain, None);
            assert!(held.is_empty());
            let mut cwd = root.clone();
            cwd.extend([0x2F, 0xD800]);
            let sig = reads.signature(&mut scope, &cwd, false).unwrap();
            assert!(matches!(sig.first, First::Undetermined(_)), "{sig:?}");
            assert_eq!(
                (
                    &sig.head,
                    &sig.config,
                    &sig.head_file,
                    &sig.refs,
                    &sig.packed
                ),
                (&None, &None, &None, &None, &None)
            );
            let again = reads.signature(&mut scope, &cwd, false).unwrap();
            assert_ne!(sig, again, "an undetermined signature equals none");
        });
    }
}
