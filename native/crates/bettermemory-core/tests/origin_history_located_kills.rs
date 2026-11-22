//! Cases a plausible wrong `origin_history::located` fails: `cuts_in`
//! over a file system in memory, the discovery half over trees on disk.

mod common;

use bettermemory_core::fsdata::{Flavour, MemFs, Node};
use bettermemory_core::gitfile_rule::bounded::Hung;
use bettermemory_core::gitfile_rule::discovery::Rule;
use bettermemory_core::gitfile_rule::follow::NoRecords;
use bettermemory_core::gitfile_rule::format::GitFacts;
use bettermemory_core::gitfile_rule::gitdir::{Ctx, Failure};
use bettermemory_core::origin_history::{cuts_in, Cut, CUTS_MAX};

const V0: &[u8] = b"[core]\n\trepositoryformatversion = 0\n";
const V256: &[u8] =
    b"[core]\n\trepositoryformatversion = 1\n[extensions]\n\tobjectFormat = sha256\n";
const A: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
const B: &str = "0123456789abcdef0123456789abcdef01234567";

fn read(config: Option<&[u8]>, shallow: Option<Node>, path: &[u8]) -> Result<Option<Cut>, Failure> {
    let mut fs = MemFs::new(Flavour::Posix);
    fs.insert(b"/c", Node::Dir);
    if let Some(config) = config {
        fs.insert(b"/c/config", Node::File(config.to_vec()));
    }
    if let Some(node) = shallow {
        fs.insert(b"/c/shallow", node);
    }
    let ctx = Ctx {
        fs: &fs,
        flavour: Flavour::Posix,
        cwd: b"/",
    };
    let git = GitFacts {
        release: Some((2, 54)),
        reads_compat: Some(true),
    };
    let rule = Rule {
        ctx: &ctx,
        answers: &Hung::default(),
        records: &NoRecords,
        env: None,
        git: &git,
    };
    cuts_in(&rule, path)
}

fn cuts(config: &[u8], data: &str) -> Option<Vec<String>> {
    read(
        Some(config),
        Some(Node::File(data.as_bytes().to_vec())),
        b"/c/shallow",
    )
    .unwrap()
    .map(|set| {
        set.iter()
            .map(|c| c.iter().map(|&u| char::from_u32(u).unwrap()).collect())
            .collect()
    })
}

#[test]
fn uppercase_hex_is_read_lowercased() {
    assert_eq!(
        cuts(V0, &format!("{}\n", B.to_uppercase())),
        Some(vec![B.to_owned()])
    );
}

#[test]
fn a_suffix_is_ignored_even_more_hex() {
    assert_eq!(cuts(V0, &format!("{A}{B}\n")), Some(vec![A.to_owned()]));
    assert_eq!(cuts(V0, &format!("{A}\r\n")), Some(vec![A.to_owned()]));
}

#[test]
fn one_final_empty_record_is_dropped_not_two() {
    assert_eq!(cuts(V0, &format!("{A}\n")), Some(vec![A.to_owned()]));
    assert_eq!(cuts(V0, &format!("{A}\n\n")), None);
    assert_eq!(cuts(V0, "\n"), None);
    assert_eq!(cuts(V0, A), Some(vec![A.to_owned()]));
}

#[test]
fn an_empty_file_is_read_before_the_format() {
    assert_eq!(
        cuts(b"[core]\n\trepositoryformatversion = 2\n", ""),
        Some(vec![])
    );
    assert_eq!(
        cuts(
            b"[core]\n\trepositoryformatversion = 2\n",
            &format!("{A}\n")
        ),
        None
    );
}

#[test]
fn the_width_comes_from_the_config_not_the_record() {
    let c = "c".repeat(64);
    assert_eq!(cuts(V256, &format!("{c}\n")), Some(vec![c.clone()]));
    assert_eq!(cuts(V256, &format!("{A}\n")), None);
    assert_eq!(cuts(V0, &format!("{c}\n")), Some(vec!["c".repeat(40)]));
}

#[test]
fn a_missing_config_reads_sha1() {
    let got = read(
        None,
        Some(Node::File(format!("{A}\n").into_bytes())),
        b"/c/shallow",
    )
    .unwrap();
    assert_eq!(got.map(|s| s.len()), Some(1));
}

#[test]
fn the_bound_is_cuts_max_bytes() {
    let at = format!("{}\n", "f".repeat(CUTS_MAX - 1));
    assert_eq!(cuts(V0, &at), Some(vec!["f".repeat(40)]));
    let over = format!("{}\n", "f".repeat(CUTS_MAX));
    assert_eq!(cuts(V0, &over), None);
}

#[test]
fn absence_is_empty_but_a_file_on_the_way_is_none() {
    assert_eq!(read(Some(V0), None, b"/c/shallow"), Ok(Some(Cut::new())));
    assert_eq!(
        read(
            Some(V0),
            Some(Node::File(b"x".to_vec())),
            b"/c/shallow/shallow"
        ),
        Ok(None)
    );
    assert_eq!(read(Some(V0), Some(Node::Dir), b"/c/shallow"), Ok(None));
}

#[test]
fn a_path_is_read_as_path_spells_it() {
    let node = || Some(Node::File(format!("{A}\n").into_bytes()));
    assert_eq!(
        read(Some(V0), node(), b"/c/shallow/")
            .unwrap()
            .map(|s| s.len()),
        Some(1)
    );
    assert_eq!(
        read(Some(V0), node(), b"/c//./shallow")
            .unwrap()
            .map(|s| s.len()),
        Some(1)
    );
    assert_eq!(
        read(Some(V0), node(), b"c/shallow")
            .unwrap()
            .map(|s| s.len()),
        Some(1)
    );
}

#[test]
fn a_nul_raises() {
    assert_eq!(read(Some(V0), None, b"/c/sha\0llow"), Err(Failure::Value));
}

/// `cuts_in(path)` over `nodes`, under `flavour` with working directory
/// `cwd`.
fn read_in(
    flavour: Flavour,
    cwd: &[u8],
    nodes: &[(&[u8], Node)],
    path: &[u8],
) -> Result<Option<Cut>, Failure> {
    let mut fs = MemFs::new(flavour);
    for (at, node) in nodes {
        fs.insert(at, node.clone());
    }
    let ctx = Ctx {
        fs: &fs,
        flavour,
        cwd,
    };
    let git = GitFacts {
        release: Some((2, 54)),
        reads_compat: Some(true),
    };
    let rule = Rule {
        ctx: &ctx,
        answers: &Hung::default(),
        records: &NoRecords,
        env: None,
        git: &git,
    };
    cuts_in(&rule, path)
}

#[test]
fn a_bare_relative_path_reads_the_config_of_the_working_directory() {
    // `Path("shallow").parent` is `.`: the config is the one in the
    // working directory, here sha256.
    let c = "c".repeat(64);
    let got = read_in(
        Flavour::Posix,
        b"/c",
        &[
            (b"/c", Node::Dir),
            (b"/c/config", Node::File(V256.to_vec())),
            (b"/c/shallow", Node::File(format!("{c}\n").into_bytes())),
        ],
        b"shallow",
    );
    assert_eq!(got.unwrap().map(|s| s.len()), Some(1));
}

#[test]
fn the_windows_flavour_declines_and_raises_only_for_a_nul() {
    // The reference on Windows answers through ntpath's `Path` and
    // `open`, which the port does not read: every path declines with
    // NotPorted (ENG-124), never with None, which is a wrong answer where
    // the reference reads the file; only a path holding a NUL raises.
    let c = "c".repeat(64);
    let nodes: Vec<(&[u8], Node)> = vec![
        (b"C:\\c", Node::Dir),
        (b"C:\\c\\config", Node::File(V0.to_vec())),
        (b"C:\\c\\shallow", Node::File(format!("{A}\n").into_bytes())),
        (b"C:\\r\\.git\\config", Node::File(V256.to_vec())),
        (
            b"C:\\r\\.git\\shallow",
            Node::File(format!("{c}\n").into_bytes()),
        ),
    ];
    for path in [
        &b"shallow"[..],
        b"nothere",
        b"",
        b"C:\\c\\shallow",
        b"C:\\r\\.git\\shallow",
    ] {
        assert_eq!(
            read_in(Flavour::Windows, b"C:\\c", &nodes, path),
            Err(Failure::NotPorted),
            "{path:?}"
        );
    }
    assert_eq!(
        read_in(Flavour::Windows, b"C:\\c", &nodes, b"sha\0llow"),
        Err(Failure::Value)
    );
}

#[cfg(unix)]
mod disk {
    use crate::common::origin_history_located::{answer, write_tree, Scratch, State};
    use serde_json::{json, Value};

    use super::*;

    fn tree() -> Value {
        json!({
            "f": {
                "r/.git/HEAD": "ref: refs/heads/main\n",
                "r/.git/objects/.keep": "",
                "r/.git/config": "[core]\n\trepositoryformatversion = 0\n",
                "r/.git/refs/heads/main": format!("{A}\n"),
                "r/sub/f": "",
            }
        })
    }

    fn run(queries: &[&str]) -> Vec<Value> {
        let base = Scratch::new("kills");
        let dir = base.0.join("t");
        std::fs::create_dir(&dir).unwrap();
        write_tree(&tree(), &dir);
        let root = dir.to_str().unwrap().to_owned();
        let mut state = State::new();
        queries
            .iter()
            .map(|q| answer(q, &root, &mut state))
            .collect()
    }

    #[test]
    fn gitdir_at_does_not_walk() {
        let got = run(&["F:<root>/r/sub|", "F:<root>/r/sub|W", "G:<root>/r/sub"]);
        assert_eq!(got[0], Value::Null);
        assert_eq!(got[1], json!("=<scratch>/r/.git/shallow"));
        assert_eq!(got[2], Value::Null);
    }

    #[test]
    fn head_moved_keeps_the_guard_and_moves_the_key() {
        let w = format!("w:r/.git/refs/heads/main|{B}\n");
        let got = run(&["G:<root>/r", "K:<root>/r", &w, "G:<root>/r", "K:<root>/r"]);
        assert_eq!(got[0], got[3]);
        assert_ne!(got[1], got[4]);
    }

    #[test]
    fn an_empty_variable_is_not_an_unset_one() {
        let got = run(&[
            "G:<root>/r",
            "e:GIT_GRAFT_FILE|",
            "G:<root>/r",
            "e:GIT_GRAFT_FILE",
            "G:<root>/r",
        ]);
        assert_eq!(got[0], json!({"eq": 0}));
        assert_eq!(got[2], json!({"eq": 1}));
        assert_eq!(got[4], json!({"eq": 0}));
    }

    #[test]
    fn a_variable_that_moves_discovery_drops_the_key() {
        for name in [
            "GIT_DIR",
            "GIT_WORK_TREE",
            "GIT_COMMON_DIR",
            "GIT_DISCOVERY_ACROSS_FILESYSTEM",
            "GIT_TEST_ASSUME_DIFFERENT_OWNER",
        ] {
            let set = format!("e:{name}|x");
            let got = run(&["K:<root>/r", &set, "K:<root>/r"]);
            assert_ne!(got[0], Value::Null, "{name}");
            assert_eq!(got[2], Value::Null, "{name}");
        }
    }

    #[test]
    fn a_shallow_file_written_moves_the_guard() {
        let w = format!("w:r/.git/shallow|{A}\n");
        let got = run(&["G:<root>/r", &w, "G:<root>/r", "C:<root>/r|"]);
        assert_eq!(got[2], json!({"eq": 1}));
        assert_eq!(got[3], json!([A]));
    }

    #[test]
    fn a_guard_follows_a_link_at_shallow() {
        // `_stamp(..., follow=True)`: a link at `shallow` is stamped as
        // its target, so rewriting the target moves the guard.
        let base = Scratch::new("kills-link");
        let dir = base.0.join("t");
        std::fs::create_dir(&dir).unwrap();
        write_tree(&tree(), &dir);
        std::fs::write(dir.join("target"), "").unwrap();
        std::os::unix::fs::symlink(dir.join("target"), dir.join("r/.git/shallow")).unwrap();
        let root = dir.to_str().unwrap().to_owned();
        let mut state = State::new();
        let w = format!("w:target|{A}\n");
        let got: Vec<Value> = ["G:<root>/r", &w, "G:<root>/r"]
            .iter()
            .map(|q| answer(q, &root, &mut state))
            .collect();
        assert_eq!(got[0], json!({"eq": 0}));
        assert_eq!(got[2], json!({"eq": 1}));
    }

    #[test]
    fn gitdir_at_is_asked_before_locate() {
        // `gitdir_at(root) or locate(root)`: where `gitdir_at` answers,
        // its owner check runs and `locate` (which checks no owner) is
        // not what answered.
        use bettermemory_core::githead::discovery::{Env, Host, Owners};
        use bettermemory_core::origin_history::history_guard;
        use std::cell::Cell;
        struct Counting(Cell<usize>, u32);
        impl Owners for Counting {
            fn owner_uid(
                &self,
                _: &[u8],
                _: bool,
            ) -> Result<u32, bettermemory_core::fsdata::OsError> {
                self.0.set(self.0.get() + 1);
                Ok(self.1)
            }
        }
        let base = Scratch::new("kills-order");
        let dir = base.0.join("t");
        std::fs::create_dir(&dir).unwrap();
        write_tree(&tree(), &dir);
        let root = dir.join("r");
        let ctx = Ctx {
            fs: &bettermemory_core::fsdata::StdFs,
            flavour: Flavour::Posix,
            cwd: dir.to_str().unwrap().as_bytes(),
        };
        let git = GitFacts {
            release: Some((2, 54)),
            reads_compat: Some(true),
        };
        let rule = Rule {
            ctx: &ctx,
            answers: &Hung::default(),
            records: &NoRecords,
            env: None,
            git: &git,
        };
        let owners = Counting(Cell::new(0), 7);
        let host = Host {
            euid: 7,
            owners: &owners,
        };
        let history = Default::default();
        let got = history_guard(
            &rule,
            &host,
            &Env::default(),
            &history,
            root.to_str().unwrap().as_bytes(),
        );
        assert!(got.is_some());
        assert!(owners.0.get() > 0);
    }
}
