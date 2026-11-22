//! Cases a plausible wrong reading of `build_binding_index` and
//! `claim_level_drift` gets wrong; each answer is the reference's.

use bettermemory_core::claim_drift::{build_binding_index, claim_level_drift, Index, LiteralLines};
use bettermemory_core::claims::{Claim, IntTooLong, Kind};
use bettermemory_core::pyvalue::PyStr;

fn c(s: &str) -> Vec<u32> {
    s.chars().map(u32::from).collect()
}

/// `string_fragment` of a line that is one quoted literal, `_rhs_repr` of
/// the text after `=`, and no anchors; a value in quotes is its `str`.
struct Plain;

fn unquote(t: &[u32]) -> Option<PyStr> {
    let q = u32::from(b'\'');
    (t.len() >= 2 && t[0] == q && t[t.len() - 1] == q).then(|| t[1..t.len() - 1].to_vec())
}

impl LiteralLines for Plain {
    fn string_fragment(&self, content: &[u32]) -> Option<PyStr> {
        unquote(content)
    }
    fn rhs_repr(&self, content: &[u32]) -> Option<PyStr> {
        let at = content.iter().position(|&x| x == u32::from(b'='))?;
        Some(
            content[at + 1..]
                .iter()
                .copied()
                .filter(|&x| x != 0x20)
                .collect(),
        )
    }
    fn anchors_from_value(&self, _: &[u32]) -> Vec<PyStr> {
        Vec::new()
    }
    fn claimed_str(&self, value: &[u32]) -> Option<PyStr> {
        unquote(value)
    }
}

const A: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";

fn index(lines: &[&str]) -> Index {
    build_binding_index(&c(&lines.join("\n")), &Plain).unwrap()
}

fn claim(kind: Kind, path: &str, name: &str, value: &str) -> Claim {
    Claim {
        kind,
        rel_path: c(path),
        name: c(name),
        value: c(value),
    }
}

#[test]
fn a_no_newline_marker_is_not_a_hunk_line() {
    let mark = format!("\u{1}{A}");
    let ix = index(&[
        &mark,
        "--- a/f",
        "+++ b/f",
        "@@ -1 +1 @@",
        "-X = 1",
        "\\ No newline",
        "+X = 2",
    ]);
    assert_eq!(ix.parse_mismatches, 0);
    assert_eq!(ix.hunks, 1);
}

#[test]
fn content_reading_as_a_header_is_consumed_by_count() {
    let mark = format!("\u{1}{A}");
    let ix = index(&[
        &mark,
        "--- a/f",
        "+++ b/f",
        "@@ -0,0 +2 @@",
        "+++ b/other",
        "+@@ -1 +1 @@",
    ]);
    assert!(!ix.files.contains(&c("other")));
    assert_eq!((ix.hunks, ix.parse_mismatches), (1, 0));
}

#[test]
fn a_short_hunk_backs_up_and_is_one_mismatch() {
    let mark = format!("\u{1}{A}");
    let ix = index(&[
        &mark,
        "--- a/f",
        "+++ b/f",
        "@@ -1,3 +1 @@",
        "-Y = 1",
        " ctx",
        "+Y = 2",
    ]);
    assert_eq!(ix.parse_mismatches, 1);
    // The `+` line after the context line is no hunk line.
    let e = &ix.bindings[&(c("f"), "assign", c("Y"))];
    assert_eq!((e.adds, e.removes), (0, 1));
}

#[test]
fn a_bad_mark_is_a_mismatch_and_keeps_the_last_sha() {
    let mark = format!("\u{1}{A}");
    let upper = format!("\u{1}{}", A.to_uppercase());
    let ix = index(&[
        &mark,
        &upper,
        "--- a/f",
        "+++ b/f",
        "@@ -1 +0,0 @@",
        "-def g(x):",
    ]);
    assert_eq!(ix.parse_mismatches, 1);
    assert_eq!(ix.commits, 1);
    let e = &ix.bindings[&(c("f"), "def", c("g"))];
    assert!(e.commits.contains(&c(A)));
}

#[test]
fn a_body_line_clears_the_header_touch() {
    let mark = format!("\u{1}{A}");
    let ix = index(&[&mark, "diff --git a/f b/f", "new mode 100755", "--- a/f"]);
    assert!(ix.header_only.is_empty());
    let ix = index(&[&mark, "diff --git a/f b/f", "new mode 100755"]);
    assert!(ix.header_only[&c("f")].contains(&c(A)));
}

#[test]
fn copy_from_is_no_header_and_rename_from_deletes() {
    let mark = format!("\u{1}{A}");
    let ix = index(&[&mark, "diff --git a/p b/q", "copy from p", "rename from r"]);
    assert!(!ix.files.contains(&c("p")));
    assert!(ix.deleted.contains(&c("r")));
}

#[test]
fn a_hunk_before_any_path_counts_nothing() {
    let ix = index(&["@@ -1 +1 @@", "-X = 1", "+X = 2"]);
    assert_eq!(ix.hunks, 0);
    assert!(ix.bindings.is_empty());
}

#[test]
fn the_digit_limit_raises_only_with_a_path() {
    let long = "1".repeat(4301);
    let line = format!("@@ -1,{long} +1 @@");
    let with = format!("--- a/f\n{line}");
    assert_eq!(
        build_binding_index(&c(&with), &Plain).err(),
        Some(IntTooLong)
    );
    assert!(build_binding_index(&c(&line), &Plain).is_ok());
}

#[test]
fn symbol_strict_is_net_removal() {
    let mark = format!("\u{1}{A}");
    let ix = index(&[
        &mark,
        "--- a/f",
        "+++ b/f",
        "@@ -1 +1 @@",
        "-def h(x):",
        "+def h(y):",
    ]);
    let d = claim_level_drift(&claim(Kind::Symbol, "f", "h", ""), &ix, &Plain);
    assert!(!d.strict && d.weak);
    assert_eq!((d.cite_commits, d.cite_edit_lines), (1, 2));
}

#[test]
fn a_value_put_back_is_not_gone_and_fragments_are_substrings() {
    let mark = format!("\u{1}{A}");
    let ix = index(&[
        &mark,
        "--- a/f",
        "+++ b/f",
        "@@ -1,2 +1,2 @@",
        "-K = 3",
        "-'twelve chars here'",
        "+K = 3",
        "+'x'",
    ]);
    let d = claim_level_drift(&claim(Kind::Literal, "f", "K", "3"), &ix, &Plain);
    assert!(!d.value_gone && !d.strict && d.weak);
    let d = claim_level_drift(
        &claim(Kind::Literal, "f", "Q", "'..twelve chars here..'"),
        &ix,
        &Plain,
    );
    assert!(d.strict);
    assert_eq!(d.anchor_commits, 1);
    assert_eq!(d.cite_commits, 1);
}

#[test]
fn a_fragment_is_measured_after_strip() {
    let mark = format!("\u{1}{A}");
    let ix = index(&[&mark, "+++ b/f", "@@ -0,0 +1 @@", "+'     abc     '"]);
    assert!(ix.changed_fragments.is_empty());
    assert_eq!(ix.changed_text.len(), 1);
}

#[test]
fn absent_matches_a_child_but_not_a_prefix_sibling() {
    let mark = format!("\u{1}{A}");
    let ix = index(&[&mark, "diff --git a/dirx b/dirx", "new file mode 100644"]);
    let d = claim_level_drift(&claim(Kind::Absent, "dir", "", ""), &ix, &Plain);
    assert!(!d.weak);
    let ix = index(&[
        &mark,
        "diff --git a/dir/x b/dir/x",
        "deleted file mode 100644",
    ]);
    let d = claim_level_drift(&claim(Kind::Absent, "dir//", "", ""), &ix, &Plain);
    assert!(d.weak && !d.strict);
}
