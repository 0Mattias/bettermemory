//! The reads of a checkout's remotes stay bounded and typed on a 512 KiB
//! thread: 10,000 remotes and 10,000 `onbranch:` and `hasconfig:`
//! conditions through every pass, 1,000 machine files, a HEAD chain past
//! git's depth; a branch asked once; the Windows flavour declining as not
//! ported; no answer of git given where it is asked.

use bettermemory_core::fsdata::{Flavour, MemFs, Node};
use bettermemory_core::gitconfig::paths::Machine;
use bettermemory_core::gitconfig::remotes::{gather, Env, Inputs, NotPortedWhy, Outcome, Sources};
use bettermemory_core::gitconfig::tree::Branch;
use bettermemory_core::gitfile_rule::format::GitFacts;
use bettermemory_core::githead;

fn on_small_stack(f: impl FnOnce() + Send + 'static) {
    std::thread::Builder::new()
        .stack_size(512 << 10)
        .spawn(f)
        .expect("a thread")
        .join()
        .expect("no panic");
}

fn inputs<'a>(machine: &'a Machine<'a>, sources: Sources, asked: Option<Branch>) -> Inputs<'a> {
    Inputs {
        machine,
        env: Env {
            git_config_nosystem: Some(b"1".to_vec()),
            ..Env::default()
        },
        sources: Sources {
            hasconfig: Some(true),
            ..sources
        },
        git: GitFacts {
            release: Some((2, 54)),
            reads_compat: None,
        },
        cwd: b"/".to_vec(),
        asked_branch: asked,
    }
}

#[test]
fn ten_thousand_remotes_and_conditions_stay_bounded() {
    on_small_stack(|| {
        let none = |_: &[u8]| None;
        let machine = Machine {
            flavour: Flavour::Posix,
            home: Some(b"/h".to_vec()),
            profile: None,
            user_home: &none,
        };
        let mut text = String::new();
        for i in 0..10_000 {
            text.push_str(&format!(
                "[remote \"r{i}\"]\n\turl = https://example.invalid/{i}\n\
                 [includeIf \"onbranch:b{i}\"]\n\tpath = x\n\
                 [includeIf \"hasconfig:remote.*.url:*/no{i}\"]\n\tpath = x\n"
            ));
        }
        let mut fs = MemFs::new(Flavour::Posix);
        fs.insert(b"/r/.git/config", Node::File(text.into_bytes()));
        fs.insert(b"/r/.git/x", Node::File(b"[x]\n\ty = 1\n".to_vec()));
        fs.insert(
            b"/r/.git/HEAD",
            Node::File(b"ref: refs/heads/main\n".to_vec()),
        );
        let mut refs = githead::MemFs::new(githead::Flavour::Posix);
        refs.insert(
            b"r/.git/HEAD",
            githead::fs::Node::File(b"ref: refs/heads/main\n".to_vec()),
        );
        // 1,000 machine files, none there.
        let own = (0..1000)
            .map(|i| format!("/etc/{i}").into_bytes())
            .collect();
        let sources = Sources {
            own,
            ..Sources::default()
        };
        let mut given = inputs(&machine, sources, None);
        given.env.git_config_nosystem = None;
        let got = gather(&fs, &refs, b"/r/.git", b"/r/.git", &given).expect("ported");
        // HEAD stamped once, before the first read's files; the read
        // stops at the 64-file bound and declines.
        let heads = got
            .files
            .iter()
            .filter(|(p, _)| p.as_slice() == b"/r/.git/HEAD")
            .count();
        assert_eq!(heads, 1);
        assert_eq!(
            got.outcome,
            Outcome::Declined(b"a config file is not read whole here".to_vec())
        );
    });
}

#[test]
fn a_head_chain_past_the_depth_asks_git_and_declines_without_an_answer() {
    on_small_stack(|| {
        let none = |_: &[u8]| None;
        let machine = Machine {
            flavour: Flavour::Posix,
            home: None,
            profile: None,
            user_home: &none,
        };
        let mut fs = MemFs::new(Flavour::Posix);
        let mut refs = githead::MemFs::new(githead::Flavour::Posix);
        let config = b"[remote \"o\"]\n\turl = u\n[includeIf \"onbranch:c*\"]\n\tpath = x\n";
        fs.insert(b"/r/.git/config", Node::File(config.to_vec()));
        refs.insert(
            b"r/.git/HEAD",
            githead::fs::Node::File(b"ref: refs/heads/c0\n".to_vec()),
        );
        fs.insert(
            b"/r/.git/HEAD",
            Node::File(b"ref: refs/heads/c0\n".to_vec()),
        );
        for i in 0..1000 {
            let text = format!("ref: refs/heads/c{}\n", i + 1).into_bytes();
            let path = format!("r/.git/refs/heads/c{i}");
            refs.insert(path.as_bytes(), githead::fs::Node::File(text.clone()));
            fs.insert(format!("/{path}").as_bytes(), Node::File(text));
        }
        // With no answer the read declines after stamping what the
        // answered twin below stamps up to packed-refs: HEAD, four refs,
        // the reftable list, packed-refs (the error carries no files).
        let given = inputs(&machine, Sources::default(), None);
        assert_eq!(
            gather(&fs, &refs, b"/r/.git", b"/r/.git", &given),
            Err(NotPortedWhy::AskedBranch)
        );
        let given = inputs(
            &machine,
            Sources::default(),
            Some(Branch::Name(b"c9".to_vec())),
        );
        let got = gather(&fs, &refs, b"/r/.git", b"/r/.git", &given).expect("ported");
        // HEAD and four refs (git's depth of five reads), then the
        // reftable list (one folder) and packed-refs, the include x, the
        // two legacy folders.
        assert_eq!(got.files.len(), 1 + 4 + 1 + 1 + 1 + 2);
        let names: Vec<&[u8]> = got.files.iter().take(7).map(|(p, _)| &p[..]).collect();
        assert_eq!(
            names,
            [
                &b"/r/.git/HEAD"[..],
                b"/r/.git/refs/heads/c0",
                b"/r/.git/refs/heads/c1",
                b"/r/.git/refs/heads/c2",
                b"/r/.git/refs/heads/c3",
                b"/r/.git/reftable/tables.list",
                b"/r/.git/packed-refs",
            ]
        );
        match got.outcome {
            Outcome::Reads { own, .. } => assert_eq!(own[0].values.len(), 2),
            other => panic!("{other:?}"),
        }
    });
}

#[test]
fn the_windows_flavour_declines_as_not_ported() {
    let none = |_: &[u8]| None;
    let machine = Machine {
        flavour: Flavour::Windows,
        home: None,
        profile: None,
        user_home: &none,
    };
    let mut fs = MemFs::new(Flavour::Windows);
    fs.insert(
        b"/r/.git/config",
        Node::File(b"[remote \"o\"]\n\turl = u\n[include]\n\tpath = x\n".to_vec()),
    );
    fs.insert(b"/r/.git/x", Node::File(Vec::new()));
    let refs = githead::MemFs::new(githead::Flavour::Windows);
    let given = inputs(&machine, Sources::default(), None);
    assert_eq!(
        gather(&fs, &refs, b"/r/.git", b"/r/.git", &given),
        Err(NotPortedWhy::Stamp)
    );
}

/// The relative names the decoy test gives, each a config file.
#[cfg(unix)]
const RELATIVE: [&str; 4] = ["rx/git/config", "rel/sys", "rel/glob", "rh/.gitconfig"];

/// Run in a child whose working directory holds decoys of every relative
/// name (a refused line, a remote): `gather` over `StdFs` with `cwd` given
/// as the case's own root, where none of those files is. A decoy opened
/// would stop the read or add a remote; one stamped would be `Some`.
#[cfg(unix)]
#[test]
fn a_relative_machine_path_is_never_read_from_the_process_directory() {
    use bettermemory_core::fsdata::StdFs;
    const CHILD: &str = "BM_REMOTES_DECOY_ROOT";
    let name = "a_relative_machine_path_is_never_read_from_the_process_directory";
    if let Some(root) = std::env::var_os(CHILD) {
        let root = root.into_string().expect("a UTF-8 root").into_bytes();
        let none = |_: &[u8]| None;
        let machine = Machine {
            flavour: Flavour::Posix,
            home: Some(b"rh".to_vec()),
            profile: None,
            user_home: &none,
        };
        let common = [&root[..], b"/repo/.git"].concat();
        for global in [None, Some(b"rel/glob".to_vec())] {
            let mut given = inputs(&machine, Sources::default(), None);
            given.cwd = root.clone();
            given.env = Env {
                git_config_nosystem: None,
                git_config_system: Some(b"rel/sys".to_vec()),
                git_config_global: global,
                xdg_config_home: Some(b"rx".to_vec()),
                git_test_default_initial_branch_name: None,
            };
            let got = gather(&StdFs, &githead::StdFs, &common, &common, &given).expect("ported");
            for (path, stamp) in &got.files {
                if !path.starts_with(&root) {
                    assert_eq!(*stamp, None, "{}", String::from_utf8_lossy(path));
                }
            }
            match got.outcome {
                Outcome::Reads { machine, .. } => {
                    assert!(machine.iter().all(|r| r.values.is_empty()));
                }
                other => panic!("{other:?}"),
            }
        }
        return;
    }
    use std::sync::atomic::{AtomicUsize, Ordering};
    static NEXT: AtomicUsize = AtomicUsize::new(0);
    let n = NEXT.fetch_add(1, Ordering::Relaxed);
    let base = std::env::temp_dir().join(format!("bm-decoy-{}-{n}", std::process::id()));
    std::fs::create_dir(&base).expect("a new scratch directory");
    let base = std::fs::canonicalize(&base).expect("resolved");
    let (root, decoys) = (base.join("root"), base.join("decoys"));
    std::fs::create_dir_all(root.join("repo/.git")).unwrap();
    std::fs::write(
        root.join("repo/.git/config"),
        b"[remote \"o\"]\n\turl = https://example.invalid/a\n",
    )
    .unwrap();
    std::fs::write(root.join("repo/.git/HEAD"), b"ref: refs/heads/main\n").unwrap();
    for rel in RELATIVE {
        let path = decoys.join(rel);
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(&path, b"[remote \"d\"]\n\turl = decoy\n[bad\n").unwrap();
    }
    std::fs::create_dir_all(decoys.join("rh/.config/git")).unwrap();
    std::fs::write(decoys.join("rh/.config/git/config"), b"[bad\n").unwrap();
    let status = std::process::Command::new(std::env::current_exe().unwrap())
        .args(["--exact", name, "--test-threads=1"])
        .current_dir(&decoys)
        .env(CHILD, &root)
        .status()
        .expect("the child runs");
    let _ = std::fs::remove_dir_all(&base);
    assert!(status.success());
}
