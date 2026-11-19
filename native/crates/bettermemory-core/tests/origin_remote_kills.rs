//! Remotes a plausible wrong `_parse_remote` answers otherwise: a greedy
//! name, a lowered scp host, `$` before a newline, tab and newline kept
//! in a URL, a bracketed IPv4 host allowed, a zone lowered, the vendor
//! prefix stripped on every host, scheme or owner count, NFKC unchecked,
//! ASCII-only whitespace, a hostname lowered without the final sigma, the
//! params split on every scheme, the port read. Each answer was taken
//! from the reference (CPython 3.13.13).

use bettermemory_core::origin_remote::{parse_remote, urlparse, UrlError};

fn cps(s: &str) -> Vec<u32> {
    s.chars().map(u32::from).collect()
}

fn parsed(url: &str) -> Option<(String, String, String)> {
    let s = |v: Vec<u32>| v.into_iter().map(|c| char::from_u32(c).unwrap()).collect();
    parse_remote(&cps(url)).map(|(h, o, n)| (s(h), s(o), s(n)))
}

fn check(url: &str, want: Option<(&str, &str, &str)>) {
    let want = want.map(|(h, o, n)| (h.to_owned(), o.to_owned(), n.to_owned()));
    assert_eq!(parsed(url), want, "{url:?}");
}

#[test]
fn the_scp_pattern() {
    check("hh:o/n.git.git", Some(("hh", "o", "n")));
    check("hh:o.git/", Some(("hh", "", "o")));
    check("hh:o//n", Some(("hh", "o", "/n")));
    check("hh:/o/n/", Some(("hh", "o", "n")));
    check("git@hh:/n", Some(("hh", "", "n")));
    check("git@hh:/", None);
    check("h:o/n", None);
    check("C:/U/x", None);
    check("host://o/n", None);
    check("Git.Example:o/n", Some(("Git.Example", "o", "n")));
    check("git@github.com:O/N", Some(("github.com", "O", "N")));
    check("hh:o/n\nx", None);
    check("hh:o\n/n", Some(("hh", "o\n", "n")));
    check("h\nx:o/n", Some(("h\nx", "o", "n")));
    check("h x:o/n", Some(("h x", "o", "n")));
    check("ab:x.git/", Some(("ab", "", "x")));
}

#[test]
fn whitespace_is_the_unicode_datas() {
    check("\u{3000}git@hh:o/n\u{2028}", Some(("hh", "o", "n")));
    check("\u{200b}git@hh:o/n", None);
    check("\u{0}git@h:o/n", None);
}

#[test]
fn the_url_form() {
    check("https://gi\tthub.com/o/n", Some(("github.com", "o", "n")));
    check("https://h/o/n\u{0}", Some(("h", "o", "n\u{0}")));
    check("HTTPS://h/o/n", None);
    check("https://h/o/n;p", Some(("h", "o", "n")));
    check("ssh://h/o/n;p", Some(("h", "o", "n;p")));
    check("https://h:99999/o/n", Some(("h", "o", "n")));
    check("https://h/a/b/c", Some(("h", "a", "b/c")));
    check("https://h/o/n?x#y", Some(("h", "o", "n")));
    check("https://h#/o/n", None);
    check("https://h//o/n", Some(("h", "o", "n")));
    check(
        "https://\u{3a3}\u{391}.com/o/n",
        Some(("\u{3c3}\u{3b1}.com", "o", "n")),
    );
    check("https://\u{130}.com/o/n", Some(("i\u{307}.com", "o", "n")));
    check("https://a\u{2100}b/o/n", None);
}

#[test]
fn bracketed_hosts() {
    check("https://[1.2.3.4]/o/n", None);
    check(
        "https://[fe80::1%Eth0]/o/n",
        Some(("fe80::1%Eth0", "o", "n")),
    );
    check("https://[FE80::1]/o/n", Some(("fe80::1", "o", "n")));
    check("https://[::1]x/o/n", None);
    check("https://u[x]@h/o/n", None);
    check("https://[v1.x]/o/n", Some(("v1.x", "o", "n")));
    check("https://[v.x]/o/n", None);
    check("https://[::1.2.3.04]/o/n", None);
    check(
        "https://[1:2:3:4:5:6:7::]/o/n",
        Some(("1:2:3:4:5:6:7::", "o", "n")),
    );
    check("https://[::1:2:3:4:5:6:7:8]/o/n", None);
    let kind = |u: &str| urlparse(&cps(u)).err();
    assert_eq!(kind("https://[1.2.3.4]/"), Some(UrlError::Ipv4));
    assert_eq!(kind("https://[v.x]/"), Some(UrlError::Future));
    assert_eq!(kind("https://[::1/"), Some(UrlError::Ipv6));
    assert_eq!(kind("https://[x]/"), Some(UrlError::Address));
    assert_eq!(kind("https://a\u{2100}b/"), Some(UrlError::Nfkc));
    check("https://[v1.]/o/n", None);
    assert_eq!(kind("https://[v1.]/"), Some(UrlError::Future));
    check("https://[::1%a%b]/o/n", None);
    assert_eq!(kind("https://[::1%a%b]/"), Some(UrlError::Address));
    check("https://a\u{fe13}b/o/n", None);
    check("https://a\u{ff1a}b/o/n", None);
    assert_eq!(kind("https://a\u{fe13}b/"), Some(UrlError::Nfkc));
    assert_eq!(kind("https://a\u{ff1a}b/"), Some(UrlError::Nfkc));
}

#[test]
fn leading_space_is_stripped() {
    let p = urlparse(&cps(" https://h/o")).unwrap();
    assert_eq!(p.scheme, cps("https"));
    assert_eq!(p.netloc, cps("h"));
    assert_eq!(p.path, cps("/o"));
}

#[test]
fn vendor_shapes() {
    check(
        "https://gitlab.com/scm/team/proj",
        Some(("gitlab.com", "scm", "team/proj")),
    );
    check(
        "https://bitbucket.x/scm/proj",
        Some(("bitbucket.x", "scm", "proj")),
    );
    check(
        "https://bitbucket.x/scm/t/p",
        Some(("bitbucket.x", "t", "p")),
    );
    check(
        "ssh://bitbucket.x/scm/t/p",
        Some(("bitbucket.x", "scm", "t/p")),
    );
    check("https://gerrit.x/A/t/p", Some(("gerrit.x", "t", "p")));
    check("ssh.github.com:o/n", Some(("github.com", "o", "n")));
    check("https://SSH.GitHub.com/o/n", Some(("github.com", "o", "n")));
    let azure = Some(("dev.azure.com", "org", "proj/repo"));
    check("git@ssh.dev.azure.com:v3/org/proj/repo", azure);
    check("https://org@dev.azure.com/org/proj/_git/repo", azure);
    check(
        "https://org.visualstudio.com/DefaultCollection/proj/_git/repo",
        azure,
    );
    check("git@vs-ssh.visualstudio.com:v3/org/proj/repo", azure);
}
