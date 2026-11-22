//! Cases a plausible wrong port of the migration's report and helpers
//! fails, each with the reference's answer (`membership_migrate.py`).

use bettermemory_core::membership_migrate::{
    self as mm, MigrationReport, MigrationTotals, RootDecision, RootlessHome,
};
use bettermemory_core::pyvalue::{PyError, PyValue};
use bettermemory_core::unicode::{cps, to_string};

fn s(t: &str) -> Vec<u32> {
    cps(t)
}

fn t(v: &[u32]) -> String {
    to_string(v).expect("no surrogate")
}

#[test]
fn id_order_is_length_first() {
    let mut ids = [s("P10"), s("P9"), s("P1"), s("P100")];
    ids.sort_by_key(|i| mm::id_order(i));
    let got: Vec<String> = ids.iter().map(|i| t(i)).collect();
    assert_eq!(got, ["P1", "P9", "P10", "P100"]);
}

#[test]
fn repository_name_strips_one_suffix_and_a_lowercase_prefix() {
    let r = |k: &str| t(&mm::repository_name(&s(k)));
    assert_eq!(r("github.com/acme/app.git"), "app");
    assert_eq!(r("github.com/acme/app.git.git"), "app.git");
    assert_eq!(r("github.com/acme/app.git/"), "app");
    assert_eq!(r("app.GIT"), "app.GIT");
    assert_eq!(r("local:/a/b"), "b");
    assert_eq!(r("local:local:x"), "local:x");
    assert_eq!(r("LOCAL:/a/b"), "b");
    assert_eq!(r("local:"), "local:");
    assert_eq!(r(".git"), ".git");
    assert_eq!(r("github.com/acme/.git"), "github.com/acme/.git");
    assert_eq!(r("/"), "/");
    assert_eq!(r("a\\b.git"), "b");
}

#[test]
fn basename_follows_the_flavour() {
    let b = |p: &str, nt: bool| t(&mm::basename_on(&s(p), nt));
    assert_eq!(b("C:x", false), "C:x");
    assert_eq!(b("C:x", true), "x");
    assert_eq!(b("//server/share", false), "share");
    assert_eq!(b("//server/share", true), "//server/share");
    assert_eq!(b("//server/share/x", true), "x");
    assert_eq!(b("//?/UnC/s/sh/z", true), "z");
    // Only the seventh character tells `//?/UNC/` from a device path.
    assert_eq!(b("//?/UNX/s", true), "s");
    assert_eq!(b("//?/UNC/s", true), "//?/UNC/s");
    assert_eq!(b("\\\\?\\UNX\\s", true), "s");
    assert_eq!(b("a\\b\\", false), "b");
    assert_eq!(b("/", false), "/");
    assert_eq!(b("", true), "");
    assert_eq!(mm::basename(&s("/a/b")), s("b"));
}

#[test]
fn shas_bounds_and_folding() {
    let vals: Vec<PyValue> = [
        "abc",
        "abcd",
        " ABCD\u{a0}",
        "a".repeat(64).as_str(),
        &"a".repeat(65),
        "abcg",
        "\u{212a}bcd",
    ]
    .iter()
    .map(|v| PyValue::str(v))
    .chain([PyValue::int(0xabcd), PyValue::None])
    .collect();
    let got: Vec<String> = mm::shas(&vals).iter().map(|v| t(v)).collect();
    assert_eq!(got, vec!["a".repeat(64), "abcd".to_string()]);
}

#[test]
fn text_is_unstripped_and_needs_a_str() {
    let d = vec![
        (PyValue::str("k"), PyValue::str(" v ")),
        (PyValue::str("n"), PyValue::int(1)),
        (PyValue::str("w"), PyValue::str("\u{a0}")),
    ];
    assert_eq!(mm::text(Some(&d), &s("k")), Some(s(" v ")));
    assert_eq!(mm::text(Some(&d), &s("n")), None);
    assert_eq!(mm::text(Some(&d), &s("w")), None);
    assert_eq!(mm::text(None, &s("k")), None);
    // A hand-built slice with a key twice: the last pair wins, as a dict's
    // later assignment does.
    let twice = vec![
        (PyValue::str("k"), PyValue::str("x")),
        (PyValue::str("k"), PyValue::str("y")),
    ];
    assert_eq!(mm::text(Some(&twice), &s("k")), Some(s("y")));
}

#[test]
fn origin_and_json_list_shapes() {
    assert_eq!(mm::origin(&PyValue::str("[1]")), Ok(None));
    assert_eq!(mm::origin(&PyValue::str("{")), Ok(None));
    assert_eq!(mm::origin(&PyValue::int(1)), Ok(None));
    assert_eq!(
        mm::origin(&PyValue::str("{\"a\": 1, \"a\": 2}")),
        Ok(Some(vec![(PyValue::str("a"), PyValue::int(2))]))
    );
    assert_eq!(mm::json_list(&PyValue::str("{}")), Ok(vec![]));
    assert_eq!(
        mm::json_list(&PyValue::Tuple(vec![PyValue::int(1)])),
        Ok(vec![])
    );
    assert_eq!(
        mm::json_list(&PyValue::List(vec![PyValue::int(1)])),
        Ok(vec![PyValue::int(1)])
    );
    let deep = format!("{}{}", "[".repeat(9999), "]".repeat(9999));
    assert_eq!(
        mm::json_list(&PyValue::str(&deep)),
        Err(PyError::RecursionError)
    );
    assert_eq!(
        mm::origin(&PyValue::str(&deep)),
        Err(PyError::RecursionError)
    );
}

#[test]
fn totals_keep_field_order() {
    let dict = MigrationTotals::default().to_dict();
    let PyValue::Dict(d) = &dict else {
        panic!("a dict")
    };
    let keys: Vec<String> = d
        .iter()
        .map(|(k, _)| match k {
            PyValue::Str(k) => t(k),
            _ => panic!("a str key"),
        })
        .collect();
    assert_eq!(keys[0], "projects");
    assert_eq!(keys[3], "memory_homes");
    assert_eq!(keys[8], "log_rows");
    assert_eq!(keys[11], "episodes_rewritten");
}

#[test]
fn lines_sort_held_as_text_and_skip_an_empty_remint() {
    let root = RootDecision {
        path: s("/w/\"x\""),
        state: s(mm::GONE),
        project: s("P1"),
        project_name: s(""),
        checkout: Some(s("")),
        rule: s("migration:gone"),
        recorded: 7,
        held: vec![(s("C9"), 2), (s("C10"), 5)],
        ..Default::default()
    };
    let x = RootlessHome {
        record: s("M1"),
        kind: s("memory"),
        cwd: s("/c"),
        root: s("/r"),
        project: s("P1"),
        checkout: None,
        rule: s("r"),
        carriers: vec![s("P2"), s("P1")],
    };
    let r = MigrationReport {
        dry_run: true,
        kept: s("rolled back"),
        roots: vec![root],
        rootless: vec![x],
        homes_by_rule: vec![(s("b"), 1), (s("a"), 2)],
        ..Default::default()
    };
    let lines: Vec<String> = r.lines().iter().map(|l| t(l)).collect();
    assert_eq!(
        lines[0],
        "bettermemory membership migrate (dry run: rolled back)"
    );
    assert!(lines[2].contains("no checkout"), "{}", lines[2]);
    assert!(
        lines[2].ends_with("homed; held of its 7 recorded commits: C10 5, C9 2"),
        "{}",
        lines[2]
    );
    assert!(
        lines[2].starts_with("  \"/w/\\\"x\\\"\": gone"),
        "{}",
        lines[2]
    );
    assert!(lines[5].ends_with("; carried by P2, P1"), "{}", lines[5]);
    assert_eq!(&lines[lines.len() - 2..], ["  a: 2", "  b: 1"]);
    assert!(!lines.iter().any(|l| l.starts_with("tokens due")));
    assert_eq!(r.homed(), 0);
}
