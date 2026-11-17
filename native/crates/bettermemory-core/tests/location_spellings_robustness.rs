//! A checkout's remote URLs and keys stay bounded and typed on a 512 KiB
//! thread: 10,000 remotes against 10,000 `insteadOf` rules (each URL
//! checked against every prefix, as the reference does: 10^8 prefix
//! tests, no more), a 1 MiB URL and base, and hostile bytes in names and
//! URLs.

use bettermemory_core::fsdata::{Flavour, MemFs, Node};
use bettermemory_core::gitconfig::paths::Machine;
use bettermemory_core::gitconfig::remotes::{Env, Inputs, Sources};
use bettermemory_core::gitconfig::spellings::{best_rule, read_remotes, rewrites, url_text};
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

fn run(config: Vec<u8>) -> bettermemory_core::gitconfig::spellings::Remotes {
    run_with(config, None)
}

fn run_with(
    config: Vec<u8>,
    global: Option<Vec<u8>>,
) -> bettermemory_core::gitconfig::spellings::Remotes {
    let none = |_: &[u8]| None;
    let machine = Machine {
        flavour: Flavour::Posix,
        home: Some(b"/h".to_vec()),
        profile: None,
        user_home: &none,
    };
    let mut fs = MemFs::new(Flavour::Posix);
    fs.insert(b"/r/.git/config", Node::File(config));
    fs.insert(
        b"/r/.git/HEAD",
        Node::File(b"ref: refs/heads/main\n".to_vec()),
    );
    if let Some(global) = global {
        fs.insert(b"/h/.gitconfig", Node::File(global));
    }
    let refs = githead::MemFs::new(githead::Flavour::Posix);
    let inputs = Inputs {
        machine: &machine,
        env: Env {
            git_config_nosystem: Some(b"1".to_vec()),
            ..Env::default()
        },
        sources: Sources {
            hasconfig: Some(true),
            legacy: Some(true),
            resets: Some(true),
            version: Some((2, 54, 0)),
            ..Sources::default()
        },
        git: GitFacts {
            release: Some((2, 54)),
            reads_compat: None,
        },
        cwd: b"/".to_vec(),
        asked_branch: None,
    };
    rewrites(&fs, &refs, b"/r/.git", b"/r/.git", &inputs).expect("ported")
}

fn hosted(url: &[u32]) -> Option<Vec<u32>> {
    Some(url.to_vec())
}

#[test]
fn ten_thousand_remotes_against_ten_thousand_rules() {
    on_small_stack(|| {
        let mut text = String::new();
        for i in 0..10_000 {
            text.push_str(&format!(
                "[remote \"r{i}\"]\n\turl = https://example.invalid/{i}/x\n\
                 [url \"https://m.invalid/{i}/\"]\n\tinsteadOf = https://example.invalid/{i}/\n"
            ));
        }
        let found = run(text.into_bytes());
        assert_eq!(found.declined, None);
        assert_eq!(found.spellings.len(), 10_000);
        let last = &found.spellings[9_999];
        assert_eq!(
            last.1.as_deref(),
            Some(&url_text(b"https://m.invalid/9999/x")[..])
        );
        let keys = read_remotes(found, &hosted);
        assert_eq!(keys.keys.len(), 20_000);
    });
}

#[test]
fn a_mebibyte_url_and_base() {
    on_small_stack(|| {
        let big = "a".repeat(1 << 20);
        // Each in its own file: the two would pass the 2 MiB a file is read to.
        let config = format!("[remote \"o\"]\n\turl = https://example.invalid/{big}\n");
        let global =
            format!("[url \"https://m.invalid/{big}\"]\n\tinsteadOf = https://example.invalid/\n");
        let found = run_with(config.into_bytes(), Some(global.into_bytes()));
        assert_eq!(found.declined, None);
        let (_, rewritten, by_machine) = &found.spellings[0];
        assert!(by_machine);
        assert_eq!(
            rewritten.as_ref().expect("rewritten").len(),
            "https://m.invalid/".len() + (2 << 20)
        );
    });
}

#[test]
fn hostile_bytes_in_names_and_urls() {
    on_small_stack(|| {
        let mut config = b"[remote \"\xff\xfe\"]\n\turl = https://example.invalid/\xed\xa0\x80\xf4\x90\x80\x80\xc0\xaf\n".to_vec();
        config.extend_from_slice(b"[url \"\xff/\"]\n\tinsteadOf = https://example.invalid/\xed\n");
        let found = run(config);
        assert_eq!(found.declined, None);
        let (url, rewritten, by_machine) = &found.spellings[0];
        assert!(url.iter().filter(|&&c| c == 0xFFFD).count() >= 5);
        assert!(rewritten.is_some());
        assert!(!by_machine);
        // The longest prefix, of equal ones the first met.
        let rules = vec![
            (b"a".to_vec(), vec![(b"x".to_vec(), false)]),
            (b"b".to_vec(), vec![(b"xy".to_vec(), true)]),
            (b"c".to_vec(), vec![(b"xy".to_vec(), false)]),
        ];
        assert_eq!(
            best_rule(b"xyz", &rules),
            Some((b"b".to_vec(), b"xy".to_vec(), true))
        );
        assert_eq!(best_rule(b"q", &rules), None);
    });
}

#[test]
fn an_empty_key_is_no_key() {
    let found = run(
        b"[remote \"o\"]\n\turl = https://example.invalid/a\n\turl = https://example.invalid/b\n"
            .to_vec(),
    );
    let empty_for_a = |u: &[u32]| {
        let text: String = u.iter().map(|&c| char::from_u32(c).unwrap()).collect();
        Some(if text.ends_with('a') {
            Vec::new()
        } else {
            u.to_vec()
        })
    };
    let keys = read_remotes(found, &empty_for_a);
    assert_eq!(keys.keys, vec![url_text(b"https://example.invalid/b")]);
}

/// The least of five runs of `f`.
fn best_of(mut f: impl FnMut()) -> std::time::Duration {
    (0..5)
        .map(|_| {
            let started = std::time::Instant::now();
            f();
            started.elapsed()
        })
        .min()
        .expect("a run")
}

/// `read_remotes` over `n` URLs a machine rule aliases: linear, as the
/// reference's set difference.
#[test]
fn aliases_are_linear_in_the_urls_aliased() {
    use bettermemory_core::gitconfig::spellings::Remotes;
    fn found(n: usize) -> Remotes {
        let spellings = (0..n)
            .map(|i| {
                let u = url_text(format!("https://e.invalid/{i}").as_bytes());
                let r = url_text(format!("https://m.invalid/{i}").as_bytes());
                (u, Some(r), true)
            })
            .collect();
        Remotes {
            spellings,
            files: Vec::new(),
            named: Vec::new(),
            declined: None,
        }
    }
    let (small, large) = (found(5_000), found(50_000));
    let s = best_of(|| assert_eq!(read_remotes(small.clone(), &hosted).aliases.len(), 5_000));
    let l = best_of(|| assert_eq!(read_remotes(large.clone(), &hosted).aliases.len(), 50_000));
    assert!(
        l < s * 30 + std::time::Duration::from_millis(50),
        "5,000 aliased {s:?}, 50,000 aliased {l:?}: more than linear"
    );
}

/// `undecided_matters` over undecided `insteadOf` names of one base with
/// many prefixes, against many keyed URLs and bases: the base looked up
/// and its prefixes a set, as the reference's dict and set.
#[test]
fn undecided_rules_against_many_bases_and_urls() {
    use bettermemory_core::gitconfig::spellings::{undecided_matters, Rewrites, Rule};
    use bettermemory_core::gitconfig::tree::ConfigValues;
    use bettermemory_core::gitconfig::Name;
    type World = (ConfigValues, Vec<(Vec<u8>, Option<Rule>)>, Rewrites);
    fn world(n: usize) -> World {
        let undecided = (0..10)
            .map(|k| {
                let name = Name {
                    section: "url".to_owned(),
                    subsection: Some(b"t".to_vec()),
                    key: "insteadof".to_owned(),
                };
                (name, vec![Some(format!("z{k}").into_bytes())])
            })
            .collect();
        let read = ConfigValues {
            undecided,
            ..ConfigValues::default()
        };
        let mut rewrites: Rewrites = (0..n)
            .map(|i| {
                (
                    format!("b{i}").into_bytes(),
                    vec![(b"https://e/".to_vec(), false)],
                )
            })
            .collect();
        rewrites.push((
            b"t".to_vec(),
            (0..n)
                .map(|i| (format!("q{i}").into_bytes(), false))
                .collect(),
        ));
        let best = (0..n)
            .map(|i| {
                let rule = (b"b0".to_vec(), b"https://e/".to_vec(), false);
                (format!("https://e/{i}").into_bytes(), Some(rule))
            })
            .collect();
        (read, best, rewrites)
    }
    let (small, large) = (world(1_000), world(10_000));
    let run = |(read, best, rewrites): &World| {
        best_of(|| assert_eq!(undecided_matters(&[read], best, false, rewrites), None))
    };
    let (s, l) = (run(&small), run(&large));
    assert!(
        l < s * 30 + std::time::Duration::from_millis(50),
        "1,000 {s:?}, 10,000 {l:?}: more than linear"
    );
}
