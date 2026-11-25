//! Inputs a plausible wrong reading of `claims.py`'s patterns answers
//! otherwise than the reference: each assertion is the reference's answer
//! (CPython 3.13.13), and the comment names the wrong reading it kills.

use bettermemory_core::claims::{self, Binding, IntTooLong};

fn c(s: &str) -> Vec<u32> {
    s.chars().map(u32::from).collect()
}

fn tok(s: &str) -> Option<(Binding, String)> {
    claims::binding_token(&c(s))
        .map(|(b, n)| (b, n.iter().map(|&x| char::from_u32(x).unwrap()).collect()))
}

fn def(n: &str) -> Option<(Binding, String)> {
    Some((Binding::Def, n.into()))
}

fn assign(n: &str) -> Option<(Binding, String)> {
    Some((Binding::Assign, n.into()))
}

#[test]
fn identifier_end_anchor_allows_one_final_line_feed() {
    // A `\z` anchor (Rust's `$`) refuses this; `re`'s `$` does not.
    assert!(claims::is_identifier(&c("name\n")));
    assert!(!claims::is_identifier(&c("name\n\n")));
    assert!(!claims::is_identifier(&c("\n")));
    // `\w` is Unicode: an ASCII-only class refuses these.
    assert!(claims::is_identifier(&c("a\u{e9}\u{660}\u{4e00}")));
    // The first character is ASCII only.
    assert!(!claims::is_identifier(&c("\u{e9}")));
    assert!(!claims::is_identifier(&c("a\u{b7}")));
}

#[test]
fn def_pattern_bounds() {
    assert_eq!(tok("async\tdef f("), def("f"));
    assert_eq!(tok("asyncdef f("), None);
    assert_eq!(tok("def f :"), def("f"));
    assert_eq!(tok("class A[T]:"), def("A"));
    assert_eq!(tok(" def f("), None);
    // `[ \t]`, not `\s`.
    assert_eq!(tok("def\u{a0}f("), None);
    assert_eq!(tok("def f\n("), None);
    // A `def` line that fails the def pattern can still be an assignment.
    assert_eq!(tok("def = 1"), assign("def"));
}

#[test]
fn assign_pattern_annotation_and_operators() {
    assert_eq!(tok("x: int = 3"), assign("x"));
    // The annotation needs one character: a walrus is no assignment.
    assert_eq!(tok("x:= 3"), None);
    assert_eq!(tok("x: = 3"), assign("x"));
    // The annotation stops at the first `=`, so a comparison never matches.
    assert_eq!(tok("x: int == 3"), None);
    // Backtracking the annotation lets an operator sit before the `=`.
    assert_eq!(tok("x: int //= 1"), assign("x"));
    // A line feed ends the annotation.
    assert_eq!(tok("x: \n= 1"), None);
    assert_eq!(tok("x **= 1"), assign("x"));
    assert_eq!(tok("x //= 1"), assign("x"));
    assert_eq!(tok("x @= 1"), None);
    assert_eq!(tok("x != 1"), None);
    assert_eq!(tok("x <= 1"), None);
    assert_eq!(tok("x == 1"), None);
    assert_eq!(tok("x **== 1"), None);
    assert_eq!(tok("x // = 1"), None);
    assert_eq!(tok("foo(TIMEOUT=30)"), None);
    assert_eq!(tok("x=(y==1)"), assign("x"));
    assert_eq!(tok("x\u{a0}= 1"), None);
}

fn hunk(s: &str) -> Option<String> {
    claims::hunk_lines(&c(s)).expect("in range")
}

#[test]
fn hunk_counts() {
    assert_eq!(hunk("@@ -1 +1 @@").as_deref(), Some("2"));
    // "0" is a true string: `int("0" or 1)` is 0, not 1.
    assert_eq!(hunk("@@ -1,0 +1,0 @@").as_deref(), Some("0"));
    assert_eq!(hunk("@@ -007,010 +1,099 @@").as_deref(), Some("109"));
    // `\d` is Unicode, and `int` reads every decimal digit set.
    assert_eq!(
        hunk("@@ -1,\u{669}\u{660} +1,\u{966} @@").as_deref(),
        Some("90")
    );
    // No anchor at the end: anything may follow the closing `@@`.
    assert_eq!(hunk("@@ -1 +1 @@@").as_deref(), Some("2"));
    assert_eq!(hunk("@@ -1, +1 @@"), None);
    assert_eq!(hunk("@@ -1 +1@@"), None);
    // Past u64.
    assert_eq!(
        hunk("@@ -1,18446744073709551615 +1,18446744073709551615 @@").as_deref(),
        Some("36893488147419103230")
    );
}

#[test]
fn hunk_digit_limit_counts_leading_zeros() {
    let line = |n: usize, d: &str| c(&format!("@@ -1,{} +1 @@", d.repeat(n)));
    assert!(claims::hunk_lines(&line(4300, "0")).is_ok());
    assert_eq!(claims::hunk_lines(&line(4301, "0")), Err(IntTooLong));
    // The start line is never read as an int.
    assert!(claims::hunk_lines(&c(&format!("@@ -{} +1 @@", "9".repeat(5000)))).is_ok());
}

#[test]
fn mark_hash_is_exact() {
    assert!(claims::is_mark_hash(&c(&"a".repeat(40))));
    assert!(!claims::is_mark_hash(&c(&"A".repeat(40))));
    assert!(!claims::is_mark_hash(&c(&format!("{}\n", "a".repeat(40)))));
    assert!(!claims::is_mark_hash(&c(&"a".repeat(104))));
}

#[test]
fn header_path_reads_halves_by_length() {
    let h = |s: &str| {
        claims::header_path(&c(s))
            .iter()
            .map(|&x| char::from_u32(x).unwrap())
            .collect::<String>()
    };
    assert_eq!(h("a/a b/a b/a b/a"), "a b/a");
    assert_eq!(h("a/x b/y"), "");
    assert_eq!(h("a/ b/"), "");
    assert_eq!(h("a/x  b/x"), "");
}

#[test]
fn patch_header_strips_unicode_space() {
    let (mut f, mut d) = (claims::PathSet::new(), claims::PathSet::new());
    let r = claims::patch_header_path(&c("+++ b/x \u{3000}"), &[], &mut f, &mut d);
    assert_eq!(r, Some(c("x")));
    // U+200B is no space to `str.strip`.
    let r = claims::patch_header_path(&c("+++ b/x\u{200b}"), &[], &mut f, &mut d);
    assert_eq!(r, Some(c("x\u{200b}")));
    // `/dev/null` with nothing open deletes nothing.
    let r = claims::patch_header_path(&c("+++ /dev/null"), &[], &mut f, &mut d);
    assert_eq!(r, Some(vec![]));
    assert!(d.is_empty());
    // `--- ` not followed by `a/` keeps the open path and records nothing.
    let r = claims::patch_header_path(&c("--- b/x"), &c("p"), &mut f, &mut d);
    assert_eq!(r, Some(c("p")));
}

#[test]
fn spellings_cap_and_order() {
    // 3 forms each: 3^3 = 27 combinations, 3^4 = 81 is past the cap.
    let path = c("\u{212b}/\u{212b}/\u{212b}");
    assert!(!claims::spellings_capped(&path));
    assert_eq!(claims::stored_spellings(&path).len(), 27);
    let path = c("\u{212b}/\u{212b}/\u{212b}/\u{212b}");
    assert!(claims::spellings_capped(&path));
    assert_eq!(claims::stored_spellings(&path).len(), 3);
    // A lone surrogate is not ASCII and has one form.
    assert_eq!(claims::stored_spellings(&[0xD800]), vec![vec![0xD800]]);
    // The written spelling comes first.
    assert_eq!(claims::stored_spellings(&c("e\u{301}"))[0], c("e\u{301}"));
}

// Unit 2: the claim parser and the literal readers.

fn refused(raw: &str) -> String {
    match claims::parse_claim(&c(raw)) {
        Err(claims::ClaimError::Refused(m)) => {
            m.iter().map(|&x| char::from_u32(x).unwrap()).collect()
        }
        other => panic!("{raw:?} gave {other:?}"),
    }
}

fn parsed(raw: &str) -> (claims::Kind, String, String, String) {
    let s = |v: &[u32]| {
        v.iter()
            .map(|&x| char::from_u32(x).unwrap())
            .collect::<String>()
    };
    let cl = claims::parse_claim(&c(raw)).expect("a claim");
    (cl.kind, s(&cl.rel_path), s(&cl.name), s(&cl.value))
}

#[test]
fn messages_are_byte_exact() {
    // An ASCII hyphen for the em dash, or a lost backtick, fails these.
    assert_eq!(
        refused("a::"),
        "claim 'a::' ends at '::' \u{2014} name a symbol (`path::name`) or a constant (`path::NAME=value`)"
    );
    assert_eq!(
        refused("!a::b"),
        "claim '!a::b': absence claims are path-only \u{2014} `!path` asserts nothing exists at that path; symbol and literal absence are not claimable kinds"
    );
    // The absence message quotes the whole stripped claim, `!` included;
    // the others quote the text after it.
    assert_eq!(refused("! ::x"), "claim '::x' has no path before '::'");
    assert_eq!(
        refused("a\\b"),
        "claim path 'a\\\\b' must use forward slashes"
    );
}

#[test]
fn length_counts_code_points() {
    // A byte count refuses 1,024 two-byte characters.
    let ok: String = "\u{e9}".repeat(1024);
    assert_eq!(parsed(&ok).0, claims::Kind::Path);
    let long: String = "\u{e9}".repeat(1025);
    assert!(refused(&long).starts_with("claim is 1025 chars \u{2014} cap is 1024."));
    // The cap is read after the strip and before the `!` is dropped.
    assert_eq!(
        parsed(&format!(" {} ", "a".repeat(1024))).0,
        claims::Kind::Path
    );
    assert!(refused(&format!("!{}", "a".repeat(1024))).starts_with("claim is 1025"));
}

#[test]
fn partitions_take_the_first_separator() {
    // A last-separator split (`rpartition`) answers otherwise.
    assert_eq!(
        refused("a::b::c"),
        "claim symbol 'b::c' is not a Python identifier"
    );
    let (_, path, name, _) = parsed("a:b::c");
    assert_eq!((path.as_str(), name.as_str()), ("a:b", "c"));
    assert!(refused("a::f=1=2").starts_with("claim value '1=2' is not a Python literal"));
    assert_eq!(parsed("a::f='a=b'").3, "'a=b'");
    // `a:::b` splits at the first two colons: the name is `:b`.
    assert_eq!(
        refused("a:::b"),
        "claim symbol ':b' is not a Python identifier"
    );
}

#[test]
fn bang_is_followed_by_lstrip_of_every_unicode_space() {
    // An ASCII-only strip keeps U+3000 in the path.
    let (kind, path, _, _) = parsed("!\u{3000}\u{85}x ");
    assert_eq!((kind, path.as_str()), (claims::Kind::Absent, "x"));
    // Only one `!` is dropped.
    assert_eq!(parsed("!!x").1, "!x");
    assert!(refused("! \u{3000}").starts_with("an absence claim needs a path"));
}

#[test]
fn values_are_canonical_and_type_sensitive() {
    assert_eq!(parsed("a::N={3, 1, 2}").3, "{1, 2, 3}");
    assert_eq!(parsed("a::N={'b': 1, 'a': 2}").3, "{'a': 2, 'b': 1}");
    assert_eq!(parsed("a::N=30.0").3, "30.0");
    assert_eq!(parsed("a::N=0x1F").3, "31");
    let raw: Vec<Vec<u32>> = ["a::N={1, 2}", "a::N={2,1}", "a::N=1", "a::N=1.0"]
        .iter()
        .map(|s| c(s))
        .collect();
    // Duplicates collapse on the rendered claim, not on the raw text.
    assert_eq!(claims::parse_claims(&raw).unwrap().len(), 3);
}

#[test]
fn count_cap_is_read_before_any_entry() {
    let mut raw = vec![c("bad\\")];
    raw.extend((0..64).map(|i| c(&format!("p{i}"))));
    match claims::parse_claims(&raw) {
        Err(claims::ClaimError::Refused(m)) => {
            assert!(m.starts_with(&c("claims capped at 64 entries (got 65)")))
        }
        other => panic!("{other:?}"),
    }
    // Sixty-four of one claim is within the cap and collapses to one.
    assert_eq!(claims::parse_claims(&vec![c("p"); 64]).unwrap().len(), 1);
}

#[test]
fn containers_are_unaddressable_and_block_every_spelling() {
    let lit = |path: &str, v: &str| claims::Claim {
        kind: claims::Kind::Literal,
        rel_path: c(path),
        name: c("N"),
        value: c(v),
    };
    for v in ["()", "[]", "{}", "set()", "(1,)", "{1}", "not literal", ""] {
        assert_eq!(claims::claim_is_addressable(&lit("a", v)), Ok(false), "{v}");
    }
    for v in ["1", "'[1]'", "b'x'", "None", "(1)", "-1"] {
        assert_eq!(claims::claim_is_addressable(&lit("a", v)), Ok(true), "{v}");
    }
    let path = |p: &str| claims::Claim {
        kind: claims::Kind::Path,
        rel_path: c(p),
        name: c(p),
        value: vec![],
    };
    // A block by the raw path misses the decomposed spelling.
    let cs = [path("caf\u{e9}"), lit("cafe\u{301}", "[1]"), path("cafe")];
    assert_eq!(claims::governed_claim_paths(&cs), Ok(vec![c("cafe")]));
}

#[test]
fn string_fragment_strips_one_paren_each_side() {
    let sf = |s: &str| claims::string_fragment(&c(s)).unwrap().map(|v| v.len());
    assert_eq!(sf("('a'),,"), Some(1));
    // A tuple once the one `)` is gone.
    assert_eq!(sf("'a',)"), None);
    assert_eq!(sf("(('a'))"), None);
    assert_eq!(sf("b'a'"), None);
    // `rstrip(",")` drops every trailing comma.
    assert_eq!(sf("'ab',,,"), Some(2));
}

#[test]
fn anchors_floor_is_twelve_stripped_code_points() {
    let an = |s: &str| claims::anchors_from_value(&c(s)).unwrap().len();
    assert_eq!(an("'abcdefghijkl\\nabcdefghijk'"), 1);
    assert_eq!(an("'  abcdefghijk  \\nx'"), 0);
    assert_eq!(
        an("'\u{e9}\u{e9}\u{e9}\u{e9}\u{e9}\u{e9}\u{e9}\u{e9}\u{e9}\u{e9}\u{e9}\u{e9}\\n'"),
        1
    );
    assert_eq!(an("'aaaaaaaaaaaa\\naaaaaaaaaaaa'"), 1);
    // No line feed: no anchors, however long.
    assert_eq!(an("'abcdefghijklmnopqrstuvwxyz'"), 0);
}

#[test]
fn load_claims_skips_non_str_and_keeps_repeats() {
    use bettermemory_core::pyvalue::PyValue;
    let raw = vec![
        PyValue::Str(c("a")),
        PyValue::None,
        PyValue::Int("1".into()),
        PyValue::Str(c("a::1x")),
        PyValue::Path(c("b")),
        PyValue::Str(c("a")),
    ];
    assert_eq!(claims::load_claims(&raw).unwrap().len(), 2);
}

#[test]
fn a_missing_table_is_a_gap_not_a_refusal() {
    // The reference reads `'\N{BULLET}'` as a str, and the port now reads
    // it with the name table: no gap is left.
    assert!(claims::parse_claim(&c("a::N='\\N{BULLET}'")).is_ok());
    assert_eq!(
        claims::string_fragment(&c("'\\N{BULLET}',")),
        Ok(Some(vec![0x2022]))
    );
    // load_claims keeps it as a literal claim, as the reference does.
    assert_eq!(
        claims::load_claims(&[bettermemory_core::pyvalue::PyValue::Str(c(
            "p::N='\\N{BULLET}'"
        ))])
        .map(|claims| claims.len()),
        Ok(1)
    );
}
