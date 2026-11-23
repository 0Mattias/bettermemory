//! Error slots and the order in which discovery and enrichment run.
mod common;

use bettermemory_core::fsdata::{Flavour, MemFs, Node};
use bettermemory_core::gitconfig::paths::Machine;
use bettermemory_core::gitconfig::remotes::{self, Inputs};
use bettermemory_core::gitconfig::stamped::StampedReads;
use bettermemory_core::gitfile_rule::bounded::Hung;
use bettermemory_core::gitfile_rule::discovery::Rule;
use bettermemory_core::gitfile_rule::follow::NoRecords;
use bettermemory_core::gitfile_rule::format::GitFacts;
use bettermemory_core::gitfile_rule::gitdir::Ctx;
use bettermemory_core::githead::discovery::{Env, Host};
use bettermemory_core::githead::signature::First;
use bettermemory_core::origin_cache::{capture_signature, CaptureSignature, Config, Slot};
use common::githead_discovery::Owned;

fn signature(fs: &MemFs, root: &str, flavour: Flavour, darwin: bool) -> CaptureSignature {
    let ctx = Ctx {
        fs,
        flavour,
        cwd: root.as_bytes(),
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
    let owners = Owned {
        fs,
        foreign: Default::default(),
        owner: 1000,
    };
    let host = Host {
        euid: 1000,
        owners: &owners,
    };
    let machine = Machine {
        flavour,
        home: None,
        profile: None,
        user_home: &|_| None,
    };
    let inputs = Inputs {
        machine: &machine,
        env: remotes::Env {
            git_config_nosystem: Some(b"1".to_vec()),
            ..Default::default()
        },
        sources: Default::default(),
        git,
        cwd: root.as_bytes().into(),
        asked_branch: None,
    };
    capture_signature(
        &rule,
        &host,
        &Env::default(),
        root.as_bytes(),
        Config {
            reads: &mut StampedReads::default(),
            inputs: &inputs,
            reuse: false,
            now: 0.,
            darwin,
        },
    )
    .unwrap()
}

fn repository(root: &str) -> MemFs {
    let mut fs = MemFs::new(Flavour::Posix);
    fs.path_max = 4096;
    fs.insert(root.as_bytes(), Node::Dir);
    for path in [".git", ".git/objects", ".git/refs", ".git/refs/heads"] {
        fs.insert(format!("{root}/{path}").as_bytes(), Node::Dir);
    }
    for (path, body) in [
        ("HEAD", "ref: refs/heads/main\n"),
        (
            "config",
            "[core]\n repositoryformatversion = 0\n bare = false\n",
        ),
        (
            "refs/heads/main",
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n",
        ),
    ] {
        fs.insert(
            format!("{root}/.git/{path}").as_bytes(),
            Node::File(body.as_bytes().into()),
        );
    }
    fs
}

#[test]
fn an_unreadable_kernel_spelling_only_replaces_the_spelling() {
    let root = format!("/{}", vec!["longcomponent"; 90].join("/"));
    let fs = repository(&root);
    let first = signature(&fs, &root, Flavour::Posix, true);
    let again = signature(&fs, &root, Flavour::Posix, true);
    let Some(Slot::Value(extra)) = &first.extra else {
        panic!(
            "expected an extended repository signature: {:?}",
            first.base.first
        )
    };
    assert!(matches!(extra.spelled_root, Slot::Unknown(_)));
    assert!(!extra.refs.is_empty());
    assert_eq!(first.base, again.base);
    assert_ne!(first, again);
    assert!(
        matches!(signature(&fs,&root,Flavour::Posix,false).extra,Some(Slot::Value(e)) if matches!(e.spelled_root,Slot::Value(_)))
    );
}

#[test]
fn an_unsettled_windows_discovery_has_no_extension() {
    let fs = MemFs::new(Flavour::Windows);
    let sig = signature(&fs, r"C:\outside", Flavour::Windows, false);
    assert!(sig.extra.is_none());
    assert!(matches!(sig.base.first, First::Undetermined(_)));
}
