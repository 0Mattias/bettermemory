//! `_parse_remote` and `urlparse` on hostile input: megabyte remotes of
//! each shape the reference reads in one pass (the answer's lengths and
//! the reference's time taken from CPython 3.13.13 on an Apple M-series
//! laptop), nested brackets, the address grammar's bounds, and random
//! texts over the delimiters, surrogates included, none of which panics.

use std::time::{Duration, Instant};

use bettermemory_core::origin_remote::{hostname, parse_remote, urlparse};

const N: usize = 1_000_000;

fn cps(s: &str) -> Vec<u32> {
    s.chars().map(u32::from).collect()
}

/// The answer's lengths, and that it came within a bound: the
/// reference's slowest shape here took 743 ms, and a release build
/// takes at most 60 ms on any of them.
fn lengths(url: &str) -> Option<[usize; 3]> {
    let url = cps(url);
    let start = Instant::now();
    let got = parse_remote(&url).map(|(h, o, n)| [h.len(), o.len(), n.len()]);
    // A debug build's NFKC of a megabyte takes some seconds.
    let bound = Duration::from_secs(if cfg!(debug_assertions) { 30 } else { 1 });
    assert!(start.elapsed() < bound, "{:?}", start.elapsed());
    got
}

#[test]
fn megabyte_remotes() {
    // (shape, the reference's answer lengths, its milliseconds)
    let a2 = "a/".repeat(N);
    assert_eq!(
        lengths(&format!("git@hh:{a2}n.git")),
        Some([2, 1, 1_999_999])
    ); // 91
    assert_eq!(lengths(&format!("hh:{}\nb", "a".repeat(N))), None); // 108
    assert_eq!(lengths(&format!("{}:o/n", "h".repeat(N))), Some([N, 1, 1])); // 65
    assert_eq!(lengths(&format!("https://h/{a2}")), Some([1, 1, 1_999_997])); // 10
    let e = "\u{e9}".repeat(N);
    assert_eq!(lengths(&format!("https://{e}/o/n")), Some([N, 1, 1])); // 89
                                                                       // U+0130 decomposes under NFKC and composes back: the quick check
                                                                       // leaves it, and the hostname lowers it to two code points.
    let dotted = "\u{130}".repeat(N);
    assert_eq!(
        lengths(&format!("https://{dotted}/o/n")),
        Some([2 * N, 1, 1])
    ); // 26
    let ws = "\u{3000}".repeat(N);
    assert_eq!(
        lengths(&format!("{ws}hh:o/n{}", " ".repeat(N))),
        Some([2, 1, 1])
    ); // 12
    assert_eq!(lengths(&format!("hh:{}", "/".repeat(N))), None); // 0
    assert_eq!(
        lengths(&format!("hh:{}", ".git".repeat(N))),
        Some([2, 0, 3_999_992])
    ); // 743
}

#[test]
fn nested_brackets_and_address_bounds() {
    let deep = format!(
        "https://{}::1{}/o/n",
        "[".repeat(10_000),
        "]".repeat(10_000)
    );
    assert_eq!(lengths(&deep), None);
    let at = |inner: &str| lengths(&format!("https://[{inner}]/o/n")).map(|l| l[0]);
    // The longest valid address text is 45 code points. A first hextet
    // of five digits fails the hextet rule.
    let full = "0000:0000:0000:0000:0000:ffff:255.255.255.255";
    assert_eq!(full.len(), 45);
    assert_eq!(at(full), Some(45));
    assert_eq!(at(&format!("0{full}")), None);
    assert_eq!(at(&format!("{full}%z")), Some(47));
    assert_eq!(at(&format!("{}::", "0".repeat(43))), None);
    assert_eq!(at(&format!("{}0000::1.2.3.4", "0000:".repeat(5))), None);
    let scoped = format!("::1%{}", "z".repeat(N));
    assert_eq!(at(&scoped), Some(N + 4));
    for octet in ["255", "256", "999", "00", "0"] {
        let want = (octet == "255" || octet == "0").then_some(8 + octet.len());
        assert_eq!(at(&format!("::1.2.3.{octet}")), want, "{octet}");
    }
    assert_eq!(at("1:2:3:4:5:6:7:8"), Some(15));
    assert_eq!(at("1:2:3:4:5:6:7:8:9"), None);
    assert_eq!(at("::ffff"), Some(6));
    assert_eq!(at("::fffff"), None);
}

#[test]
fn random_texts_never_panic() {
    let pieces: Vec<Vec<u32>> = [
        "a",
        "B",
        ":",
        "/",
        "@",
        ".",
        "[",
        "]",
        "%",
        "?",
        "#",
        ";",
        "v",
        "1",
        "f",
        " ",
        "\t",
        "\n",
        "\r",
        "\u{0}",
        "\u{1c}",
        "-",
        "_",
        "+",
        ".git",
        "::",
        "//",
        "\u{130}",
        "\u{2100}",
        "\u{ff0f}",
        "\u{661}",
        "\u{3000}",
        "\u{3a3}",
        "https://",
        "ssh://",
        "dev.azure.com",
        ".visualstudio.com",
        "bitbucket",
        "scm",
        "a",
        "v3",
        "_git",
        "DefaultCollection",
    ]
    .iter()
    .map(|s| cps(s))
    .chain([vec![0xD800], vec![0xDFFF], vec![0x10FFFF]])
    .collect();
    let mut seed: u64 = 0x9E37_79B9_7F4A_7C15;
    let mut next = |n: usize| {
        seed ^= seed << 13;
        seed ^= seed >> 7;
        seed ^= seed << 17;
        (seed % n as u64) as usize
    };
    for _ in 0..50_000 {
        let len = next(24);
        let url: Vec<u32> = (0..len)
            .flat_map(|_| pieces[next(pieces.len())].clone())
            .collect();
        let _ = parse_remote(&url);
        if let Ok(p) = urlparse(&url) {
            let _ = hostname(&p.netloc);
        }
    }
}
