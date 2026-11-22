//! Cases a plausible wrong `_first_file_root`, `unquote` or
//! `workspace_declaration` fails. Each expected value is the reference's
//! (CPython 3.13.13), read off `identity_roots.jsonl` or the reference.

use bettermemory_core::config::{self, Flavour, Fs, OsFailure, Raised};
use bettermemory_core::identity::{self as id, Caller, Published, Workspace};
use bettermemory_core::pyvalue::PyStr;
use bettermemory_core::unicode::cps;

fn root(uri: &str) -> Result<Option<PyStr>, Raised> {
    id::first_file_root(&[Some(cps(uri))])
}

fn some(text: &str) -> Result<Option<PyStr>, Raised> {
    Ok(Some(cps(text)))
}

#[test]
fn unquote_decodes_each_ascii_run_on_its_own() {
    // `%C3` and `%A9` sit in two ASCII runs split by U+00E9: each is an
    // invalid sequence alone, so both escape.
    assert_eq!(
        id::unquote(&cps("%C3\u{e9}%A9")),
        vec![0xDCC3, 0xE9, 0xDCA9]
    );
    assert_eq!(id::unquote(&cps("%C3%A9")), vec![0xE9]);
}

#[test]
fn unquote_escapes_the_maximal_subpart_and_resumes() {
    assert_eq!(id::unquote(&cps("%E2%82A")), vec![0xDCE2, 0xDC82, 0x41]);
    assert_eq!(id::unquote(&cps("%ED%A0%80")), vec![0xDCED, 0xDCA0, 0xDC80]);
    assert_eq!(id::unquote(&cps("%F4%90%80%80")).len(), 4);
    assert_eq!(id::unquote(&cps("%C0%AF")), vec![0xDCC0, 0xDCAF]);
}

#[test]
fn unquote_keeps_a_malformed_percent_and_passes_surrogates() {
    assert_eq!(id::unquote(&cps("%%41")), cps("%A"));
    assert_eq!(id::unquote(&cps("%4g%")), cps("%4g%"));
    assert_eq!(id::unquote(&cps("%7e%7E")), cps("~~"));
    assert_eq!(id::unquote(&[0xDC80, 0x25, 0x34, 0x31]), vec![0xDC80, 0x41]);
}

#[test]
fn unquote_returns_text_without_a_percent_as_it_is() {
    let text = [0x61, 0xDC80, 0xE9, 0x10FFFF, 0x7F];
    assert_eq!(id::unquote(&text), text.to_vec());
    assert_eq!(id::unquote(&[]), Vec::<u32>::new());
}

#[test]
fn the_drive_rule_takes_any_code_point_before_the_colon() {
    assert_eq!(root("file:///C:/work"), some("C:/work"));
    assert_eq!(root("file:///\u{e9}:/x"), some("\u{e9}:/x"));
    assert_eq!(root("file:///%43:/x"), some("C:/x"));
    assert_eq!(root("file:///C"), some("/C"));
    assert_eq!(root("file:///CC:/x"), some("/CC:/x"));
    assert_eq!(root("file:////:x"), some("/:x"));
}

#[test]
fn the_unc_rule_is_case_sensitive_and_follows_the_drive_rule() {
    assert_eq!(root("file://localhost/x"), some("/x"));
    assert_eq!(root("file://LOCALHOST/x"), some("//LOCALHOST/x"));
    assert_eq!(root("file://localhost:80/x"), some("//localhost:80/x"));
    // The drive's slash goes first, so the host runs into the drive.
    assert_eq!(root("file://server/C:/x"), some("//serverC:/x"));
    assert_eq!(root("file://server"), some("//server"));
}

#[test]
fn the_scheme_is_folded_and_file_keeps_its_semicolons() {
    assert_eq!(root("FILE:///up"), some("/up"));
    assert_eq!(root("fi\tle:///tab"), some("/tab"));
    assert_eq!(root("file:///a;b/c;d"), some("/a;b/c;d"));
    assert_eq!(root("file:///a?q#f"), some("/a"));
    assert_eq!(root("C:/work"), Ok(None));
    assert_eq!(root("file+x:///x"), Ok(None));
}

#[test]
fn a_refused_path_moves_on_and_a_raise_stops() {
    let roots = |r: &[&str]| -> Vec<Option<PyStr>> { r.iter().map(|u| Some(cps(u))).collect() };
    assert_eq!(
        id::first_file_root(&roots(&["file:///%00", "file:///b"])),
        some("/b")
    );
    assert_eq!(
        id::first_file_root(&[None, Some(cps("file:///b"))]),
        some("/b")
    );
    let err = id::first_file_root(&roots(&["file://[x/", "file:///a"])).unwrap_err();
    assert_eq!(
        (err.class, err.message.as_str()),
        ("ValueError", "Invalid IPv6 URL")
    );
    assert_eq!(
        id::first_file_root(&roots(&["file:///a", "file://[x/"])),
        some("/a")
    );
    // A URI of any scheme is parsed first, so it raises too.
    assert!(root("http://[x").is_err());
}

#[test]
fn the_raised_messages_are_cpythons() {
    let msg = |u: &str| root(u).unwrap_err().message;
    assert_eq!(
        msg("file://a\u{ff0f}b/x"),
        "netloc 'a\u{ff0f}b' contains invalid characters under NFKC normalization"
    );
    assert_eq!(
        msg("file://u@[zz]/p"),
        "'zz' does not appear to be an IPv4 or IPv6 address"
    );
    assert_eq!(
        msg("file://[a@b]/p"),
        "'b]' does not appear to be an IPv4 or IPv6 address"
    );
    assert_eq!(
        msg("file://[1.2.3.4]/p"),
        "An IPv4 address cannot be in brackets"
    );
    assert_eq!(msg("file://[v1.]/p"), "IPvFuture address is invalid");
}

#[test]
fn the_declared_bound_applies_after_unquoting() {
    assert!(root(&format!("file:///{}", "a".repeat(255)))
        .unwrap()
        .is_some());
    assert_eq!(root(&format!("file:///{}", "a".repeat(256))), Ok(None));
    assert!(root(&format!("file:///{}", "%41".repeat(255)))
        .unwrap()
        .is_some());
}

struct Home;

impl Fs for Home {
    fn read(&self, _: &str) -> Result<Vec<u8>, Option<OsFailure>> {
        Err(None)
    }
    fn present(&self, _: &str) -> Result<bool, Raised> {
        Ok(false)
    }
    fn is_symlink(&self, _: &str) -> bool {
        false
    }
    fn is_dir(&self, _: &str) -> bool {
        false
    }
    fn resolve(&self, p: &str) -> Result<String, Raised> {
        Ok(p.to_owned())
    }
    fn expanduser(&self, p: &str) -> Result<String, Raised> {
        match p.strip_prefix('~') {
            Some("") => Ok("/h".to_owned()),
            Some(rest) if rest.starts_with('/') => Ok(format!("/h{rest}")),
            Some(_) => Err(config::no_home()),
            None => Ok(p.to_owned()),
        }
    }
    fn join(&self, p: &str, n: &str) -> String {
        format!("{p}/{n}")
    }
    fn home(&self) -> Option<String> {
        None
    }
    fn cwd(&self) -> Option<String> {
        None
    }
    fn under(&self, _: &str, _: &str) -> bool {
        false
    }
}

type Declared = Result<Option<(PyStr, PyStr)>, Raised>;

fn declare(published: &Published, env: &[(&str, &str)]) -> (Declared, usize, usize) {
    let (mut reads, mut cwds) = (0, 0);
    let got = id::workspace_declaration(
        published,
        &mut |k| {
            reads += 1;
            env.iter().find(|(e, _)| *e == k).map(|(_, v)| cps(v))
        },
        &mut || {
            cwds += 1;
            Some(cps("/cwd"))
        },
        &Home,
        Flavour::Posix,
    );
    (got, reads, cwds)
}

fn published(path: Option<&str>, source: &str) -> Published {
    let mut p = Published::default();
    p.publish(Caller {
        workspace: Workspace {
            path: path.map(cps),
            source: cps(source),
        },
        ..Caller::default()
    });
    p
}

#[test]
fn a_published_caller_wins_and_the_environment_is_not_read() {
    let env = [("BETTERMEMORY_WORKSPACE", "/env")];
    let (got, reads, cwds) = declare(&published(Some("~/w"), "header"), &env);
    assert_eq!(got.unwrap(), Some((cps("/h/w"), cps("header"))));
    assert_eq!((reads, cwds), (0, 0));
    let (got, reads, _) = declare(&published(Some("/p"), "process-cwd"), &env);
    assert_eq!(got.unwrap(), None);
    assert_eq!(reads, 0);
    assert_eq!(declare(&published(None, "header"), &[]).0.unwrap(), None);
}

#[test]
fn without_a_caller_the_environment_declares() {
    let (got, reads, cwds) = declare(&Published::default(), &[("BETTERMEMORY_WORKSPACE", "~")]);
    assert_eq!(got.unwrap(), Some((cps("/h"), cps("env"))));
    assert_eq!((reads, cwds), (5, 0));
    let (got, _, cwds) = declare(&Published::default(), &[]);
    assert_eq!(got.unwrap(), None);
    assert_eq!(cwds, 1);
}

#[test]
fn an_unknown_user_raises_and_the_path_is_spelled_first() {
    let err = declare(&published(Some("~x"), "header"), &[])
        .0
        .unwrap_err();
    assert_eq!(err.class, "RuntimeError");
    let (got, _, _) = declare(&published(Some("./~//w/"), "header"), &[]);
    assert_eq!(got.unwrap(), Some((cps("/h/w"), cps("header"))));
}

#[test]
fn the_ascii_run_split_starts_at_u0080_and_format_characters_stay() {
    // U+0080 is past ASCII: it passes through on its own, and the walk
    // moves on rather than looping on an empty run.
    assert_eq!(root("file:///\u{80}"), some("/\u{80}"));
    assert_eq!(id::unquote(&cps("\u{80}%41")), cps("\u{80}A"));
    // `_clean(path, strip_format=False)` keeps a category Cf code point.
    assert_eq!(root("file:///a\u{200b}b"), some("/a\u{200b}b"));
}
