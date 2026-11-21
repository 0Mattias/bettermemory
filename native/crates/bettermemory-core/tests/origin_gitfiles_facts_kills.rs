//! Cases a plausible wrong port of `origin_gitfiles::facts` fails, and the
//! native states of `_machine_config_dies` the reference never has.

use bettermemory_core::fsdata::{Flavour, MemFs, Node, PyErr};
use bettermemory_core::gitconfig::remotes::NotPortedWhy;
use bettermemory_core::gitconfig::tree::ConfigValues;
use bettermemory_core::gitconfig::Name;
use bettermemory_core::gitfile_rule::format::GitFacts;
use bettermemory_core::origin_gitfiles::facts::{
    config_entries, directories_held, facts_of, global_attributes_path, hold_directory,
    machine_config_dies, machine_value_dies, merge_word_dies, no_config, rev_parse_dies, Held,
    HoldFault, NotPorted,
};

const NONE: GitFacts = GitFacts {
    release: None,
    reads_compat: None,
};

fn git(major: u32, minor: u32) -> GitFacts {
    GitFacts {
        release: Some((major, minor)),
        reads_compat: None,
    }
}

fn read(section: &str, key: &str, value: Option<&[u8]>) -> ConfigValues {
    ConfigValues {
        values: vec![(
            Name {
                section: section.into(),
                subsection: None,
                key: key.into(),
            },
            vec![value.map(<[u8]>::to_vec)],
        )],
        complete: true,
        ..ConfigValues::default()
    }
}

fn clean() -> ConfigValues {
    ConfigValues {
        complete: true,
        ..ConfigValues::default()
    }
}

#[test]
fn a_bare_line_is_a_plain_false_only_as_a_whole_line() {
    let bare = |t: &[u8]| facts_of(t, &NONE).bare_moves;
    assert!(!bare(b"[core]\n\tbare = false\n"));
    assert!(!bare(b"[core]\n\tbare = false\r\n"));
    assert!(bare(b"[core]\n\tbare = false\r\r\n"));
    assert!(bare(b"[core]\n\tbare = falsey\n"));
    assert!(bare(b"[core]\n\tbare = 00\n"));
    assert!(bare(b"[core]\n\tbare = false # c\n"));
    assert!(!bare(b"[core]\n\tBaRe\t=\tOFF \t\n"));
    assert!(bare(b"x bare = false\n"));
    // A vertical tab is no blank of the pattern.
    assert!(bare(b"\x0bbare = no\n"));
    // A line without "bare" never moves, whatever it says.
    assert!(!bare(b"[core]\n\tworktree = x\n"));
}

#[test]
fn the_raw_text_is_read_even_where_git_refuses_it() {
    let f = facts_of(b"[core\n\tattributesFile\0bare\n", &NONE);
    assert_eq!(f.names, None);
    assert!(f.attributes_elsewhere && f.bare_moves && !f.git_dies && !f.includes);
    assert!(facts_of(b"; [ATTR\n", &NONE).attributes_elsewhere);
    assert_eq!(config_entries(b"[core]\n\tx = \"a\0\"\n"), None);
    assert_eq!(facts_of(b"", &NONE), no_config());
}

#[test]
fn includes_are_named_by_the_section_prefix() {
    assert!(facts_of(b"[include]\n\tx = 1\n", &NONE).includes);
    assert!(!facts_of(b"[include]\n\tx = 1\n", &NONE).git_dies);
    assert!(facts_of(b"[include]\n\tpath = x\n", &NONE).git_dies);
    assert!(!facts_of(b"[includes]\n\tpath = x\n", &NONE).includes);
}

#[test]
fn each_table_reads_its_own_rule() {
    assert!(rev_parse_dies(b"core.filemode", Some(b"maybe"), &NONE));
    assert!(!rev_parse_dies(b"core.filemode", None, &NONE));
    assert!(rev_parse_dies(b"advice.anything", Some(b"x"), &NONE));
    assert!(!rev_parse_dies(b"advice", Some(b"x"), &NONE));
    assert!(rev_parse_dies(b"user.name", None, &NONE));
    assert!(!rev_parse_dies(b"user.name", Some(b""), &NONE));
    // The size: a sign anywhere in the whole value, past a NUL too.
    assert!(rev_parse_dies(
        b"core.bigfilethreshold",
        Some(b"1\0-"),
        &NONE
    ));
    assert!(!rev_parse_dies(
        b"core.bigfilethreshold",
        Some(b"1g"),
        &NONE
    ));
    assert!(rev_parse_dies(b"core.bigfilethreshold", Some(b"2g"), &NONE));
    assert!(!rev_parse_dies(b"core.autocrlf", Some(b"INPUT\0x"), &NONE));
    assert!(!rev_parse_dies(b"core.autocrlf", None, &NONE));
    assert!(rev_parse_dies(b"core.logallrefupdates", None, &NONE));
    assert!(rev_parse_dies(b"push.default", Some(b"Simple"), &NONE));
    assert!(!rev_parse_dies(b"push.default", Some(b"simple\0x"), &NONE));
    assert!(!rev_parse_dies(
        b"core.repositoryformatversion",
        Some(b"-7"),
        &NONE
    ));
    assert!(rev_parse_dies(
        b"core.repositoryformatversion",
        Some(b"2"),
        &NONE
    ));
    assert!(!rev_parse_dies(
        b"fetch.negotiationalgorithm",
        Some(b"NOOP"),
        &NONE
    ));
    assert!(rev_parse_dies(
        b"commitgraph.generationversion",
        None,
        &NONE
    ));
    assert!(rev_parse_dies(b"includeif.x.path", Some(b"y"), &NONE));
    assert!(!rev_parse_dies(b"includeif.x.paths", Some(b"y"), &NONE));
    assert!(!rev_parse_dies(b"core.bare", Some(b"maybe"), &NONE));
    assert!(machine_value_dies(b"core.bare", Some(b"maybe"), &NONE));
    assert!(rev_parse_dies(b"color.advicex", None, &NONE));
    assert!(!rev_parse_dies(b"color.ui", None, &NONE));
    assert!(rev_parse_dies(b"pack.compression", Some(b"1"), &NONE));
}

#[test]
fn the_merge_words_turn_on_the_release() {
    assert!(!merge_word_dies(Some(b"always"), &NONE));
    assert!(merge_word_dies(Some(b"inherit"), &NONE));
    assert!(merge_word_dies(Some(b"inherit"), &git(2, 34)));
    assert!(!merge_word_dies(Some(b"inherit"), &git(2, 35)));
    assert!(merge_word_dies(Some(b"simple"), &git(2, 36)));
    assert!(!merge_word_dies(Some(b"simple"), &git(2, 37)));
    // Another case of "always" reads only before 2.35.
    assert!(!merge_word_dies(Some(b"ALWAYS"), &git(2, 34)));
    assert!(merge_word_dies(Some(b"ALWAYS"), &git(2, 35)));
    assert!(merge_word_dies(Some(b"ALWAYS"), &NONE));
    // Another case of "inherit" is no word: a boolean or not.
    assert!(merge_word_dies(Some(b"Inherit"), &git(3, 0)));
    assert!(!merge_word_dies(None, &NONE));
    assert!(!merge_word_dies(Some(b"yes"), &NONE));
}

#[test]
fn every_machine_read_state() {
    let ok = |reads: &[ConfigValues]| machine_config_dies(Ok(reads), &NONE);
    assert_eq!(ok(&[]), Ok(false));
    assert_eq!(ok(&[clean()]), Ok(false));
    let mut r = clean();
    r.dies = Some(b"x".to_vec());
    assert_eq!(ok(&[r]), Ok(true));
    let mut r = clean();
    r.maybe_dies = Some(b"x".to_vec());
    assert_eq!(ok(&[r]), Ok(true));
    let mut r = clean();
    r.complete = false;
    assert_eq!(ok(&[r]), Ok(true));
    let mut r = clean();
    r.raised = Some(PyErr::Value);
    assert_eq!(ok(&[r]), Ok(true));
    let mut r = read("core", "filemode", Some(b"true"));
    r.asks_remote_urls = true;
    r.forbidden = true;
    assert_eq!(ok(&[r]), Ok(false));
    let mut r = clean();
    r.not_ported = Some(b"gitdir:x".to_vec());
    assert_eq!(ok(&[r]), Err(NotPorted));
    let mut r = clean();
    r.asks_branch = true;
    assert_eq!(ok(&[r.clone()]), Err(NotPorted));
    // Within one read, raised answers before not_ported and asks_branch.
    let mut r = clean();
    r.raised = Some(PyErr::Value);
    r.not_ported = Some(b"gitdir:x".to_vec());
    assert_eq!(ok(&[r]), Ok(true));
    let mut r = clean();
    r.raised = Some(PyErr::Value);
    r.asks_branch = true;
    assert_eq!(ok(&[r]), Ok(true));
    let mut r = clean();
    r.asks_branch = true;
    // A read that dies first answers before a later one that declines.
    assert_eq!(ok(&[read("user", "name", None), r.clone()]), Ok(true));
    assert_eq!(ok(&[r, read("user", "name", None)]), Err(NotPorted));
    assert_eq!(ok(&[read("include", "path", None)]), Ok(false));
    assert_eq!(ok(&[read("core", "bare", Some(b"x"))]), Ok(true));
    let mut r = clean();
    r.undecided = read("core", "filemode", Some(b"x")).values;
    assert_eq!(ok(&[r]), Ok(true));
    let named = |sub: &[u8]| ConfigValues {
        values: vec![(
            Name {
                section: "color".into(),
                subsection: Some(sub.to_vec()),
                key: "hint".into(),
            },
            vec![None],
        )],
        complete: true,
        ..ConfigValues::default()
    };
    assert_eq!(ok(&[named(b"advice")]), Ok(true));
    assert_eq!(ok(&[named(b"x")]), Ok(false));
    assert_eq!(
        machine_config_dies(Err(&NotPortedWhy::Raised), &NONE),
        Ok(true)
    );
    for why in [
        NotPortedWhy::Stamp,
        NotPortedWhy::AskedBranch,
        NotPortedWhy::Condition(b"x".to_vec()),
        NotPortedWhy::RelativeGitDir,
    ] {
        assert_eq!(machine_config_dies(Err(&why), &NONE), Err(NotPorted));
    }
}

#[test]
fn hold_walks_up_only_past_absence() {
    let mut fs = MemFs::new(Flavour::Posix);
    fs.insert(b"/w", Node::Dir);
    fs.insert(b"/w/d", Node::Dir);
    fs.insert(b"/w/f", Node::File(b"x".to_vec()));
    fs.insert(b"/w/ld", Node::Link(b"d".to_vec()));
    let mut held = Held::new();
    hold_directory(&fs, Flavour::Posix, b"/w/ld/a/b", &mut held).unwrap();
    hold_directory(&fs, Flavour::Posix, b"/w/f/x", &mut held).unwrap();
    let keys: Vec<&[u8]> = held.keys().map(Vec::as_slice).collect();
    assert_eq!(keys, [&b"/w/f"[..], b"/w/ld"]);
    assert!(directories_held(&fs, &held));
    assert!(matches!(
        hold_directory(&fs, Flavour::Posix, b"", &mut held),
        Err(HoldFault::Raised(e)) if e.class == PyErr::FileNotFound
    ));
    assert!(matches!(
        hold_directory(&fs, Flavour::Posix, b"/w/a\0b", &mut held),
        Err(HoldFault::Raised(e)) if e.class == PyErr::Value
    ));
    held.insert(b"/w/a\0b".to_vec(), (0, 0));
    assert!(!directories_held(&fs, &held));
}

#[test]
fn hold_declines_without_a_change_time() {
    let mut fs = MemFs::new(Flavour::Windows);
    fs.insert(b"C:\\w", Node::Dir);
    let mut held = Held::new();
    assert_eq!(
        hold_directory(&fs, Flavour::Windows, b"C:\\w", &mut held),
        Err(HoldFault::NotPorted)
    );
    assert!(held.is_empty());
}

#[test]
fn the_global_attributes_path_by_environment() {
    let p =
        |x: Option<&[u8]>, h: Option<&[u8]>| global_attributes_path(Flavour::Posix, x, h, b"/r");
    assert_eq!(p(None, None), None);
    assert_eq!(p(Some(b""), None), None);
    assert_eq!(p(Some(b""), Some(b"")).unwrap(), b"/.config/git/attributes");
    assert_eq!(p(Some(b"x"), Some(b"/h")).unwrap(), b"/r/x/git/attributes");
    assert_eq!(p(None, Some(b"/h")).unwrap(), b"/h/.config/git/attributes");
}
