//! Cases a plausible wrong `symbols` checker fails: each names the
//! mistake it catches. The reference's answer is stated from
//! `symbols.py`'s text (checker at 289-451).

use std::cell::RefCell;
use std::collections::{BTreeMap, BTreeSet};

use bettermemory_core::fsdata::{Flavour, MemFs, Node};
use bettermemory_core::pyvalue::PyStr;
use bettermemory_core::symbols::{
    check_symbol_citations, tier, Anchor, Binding, Declined, Machine, ModuleNames, NameSets,
    SymbolDriftReport, MAX_MODULE_BYTES,
};

fn t(s: &str) -> PyStr {
    s.chars().map(u32::from).collect()
}

fn set(names: &[&str]) -> BTreeSet<PyStr> {
    names.iter().map(|n| t(n)).collect()
}

/// Names by the exact text: `top` holds `TOP`, `all` holds `TOP` and
/// `inner`, for any text in `known`.
struct ByText {
    known: BTreeMap<PyStr, NameSets>,
    calls: RefCell<Vec<PyStr>>,
}

impl ModuleNames for ByText {
    fn names(&self, source: &[u32]) -> Option<NameSets> {
        self.calls.borrow_mut().push(source.to_vec());
        self.known.get(source).cloned()
    }
}

fn by_text(texts: &[&str]) -> ByText {
    ByText {
        known: texts
            .iter()
            .map(|s| (t(s), (set(&["TOP"]), set(&["TOP", "inner"]))))
            .collect(),
        calls: RefCell::new(Vec::new()),
    }
}

fn fs_with(files: &[(&str, &[u8])]) -> MemFs {
    let mut fs = MemFs::new(Flavour::Posix);
    fs.insert(b"/S", Node::Dir);
    for (path, data) in files {
        fs.insert(path.as_bytes(), Node::File(data.to_vec()));
    }
    fs
}

fn check(
    fs: &MemFs,
    citations: &[(&str, &str)],
    root: Option<&str>,
    anchor: Option<Anchor>,
    names: &ByText,
) -> SymbolDriftReport {
    let cites: Vec<(PyStr, PyStr)> = citations.iter().map(|(s, m)| (t(s), t(m))).collect();
    let machine = Machine {
        fs,
        flavour: Flavour::Posix,
        cwd: b"/S",
    };
    let root = root.map(t);
    check_symbol_citations(&cites, root.as_deref(), anchor.as_ref(), &machine, names).unwrap()
}

fn bindings(list: &[bettermemory_core::symbols::SymbolCitation]) -> Vec<&'static str> {
    list.iter().map(|c| c.binding.as_str()).collect()
}

#[test]
fn kills_a_tier_that_reads_every_segment_or_only_one_end() {
    let (top, all) = (set(&["Store"]), set(&["Store", "mark"]));
    assert_eq!(tier(&t("Store.mark"), &top, &all), Binding::TopLevel);
    assert_eq!(tier(&t("pkg.Store"), &top, &all), Binding::TopLevel);
    assert_eq!(tier(&t("a.Store.b"), &top, &all), Binding::Absent);
    assert_eq!(tier(&t("x.mark"), &top, &all), Binding::Nested);
    assert_eq!(tier(&t(""), &set(&[""]), &all), Binding::TopLevel);
}

#[test]
fn kills_a_size_bound_off_by_one() {
    let at = vec![b'#'; MAX_MODULE_BYTES as usize];
    let mut past = at.clone();
    past.push(b'#');
    let fs = fs_with(&[("/S/p/at.py", &at), ("/S/p/past.py", &past)]);
    let names = by_text(&[&"#".repeat(MAX_MODULE_BYTES as usize)]);
    let r = check(
        &fs,
        &[("TOP", "p/at.py"), ("TOP", "p/past.py")],
        Some("/S"),
        None,
        &names,
    );
    assert_eq!(bindings(&r.checked), ["top_level"]);
    assert_eq!(bindings(&r.unresolved), ["unresolved"]);
}

#[test]
fn kills_a_read_without_universal_newlines_or_with_lossless_decoding() {
    let fs = fs_with(&[("/S/p/a.py", b"x\r\ny\rz\xff\n")]);
    let names = by_text(&["x\ny\nz\u{fffd}\n"]);
    let r = check(&fs, &[("TOP", "p/a.py")], Some("/S"), None, &names);
    assert_eq!(bindings(&r.checked), ["top_level"]);
}

#[test]
fn kills_a_checker_that_parses_a_module_once_per_citation() {
    let fs = fs_with(&[("/S/p/a.py", b"m")]);
    let names = by_text(&["m"]);
    let r = check(
        &fs,
        &[("TOP", "p/a.py"), ("inner", "p/a.py"), ("TOP", "p/a.py")],
        Some("/S"),
        None,
        &names,
    );
    assert_eq!(bindings(&r.checked), ["top_level", "nested", "top_level"]);
    assert_eq!(names.calls.borrow().len(), 1);
}

#[test]
fn kills_an_anchor_that_falls_back_to_the_worktree_root() {
    let fs = fs_with(&[("/S/p/a.py", b"m")]);
    let names = by_text(&["m"]);
    let none = Anchor {
        root: None,
        convicting: true,
    };
    let r = check(&fs, &[("TOP", "p/a.py")], Some("/S"), Some(none), &names);
    assert!(!r.truthy());
    let standin = Anchor {
        root: Some(t("/S")),
        convicting: false,
    };
    let r = check(
        &fs,
        &[("gone", "p/a.py"), ("inner", "p/a.py"), ("x", "a.py")],
        Some("/nowhere"),
        Some(standin),
        &names,
    );
    assert_eq!(bindings(&r.checked), ["nested"]);
    assert_eq!(bindings(&r.unconfirmed), ["unconfirmed"]);
    assert_eq!(bindings(&r.unresolved), ["unresolved"]);
    assert!(r.missing().is_empty());
    let keys: Vec<&str> = r.to_dict().iter().map(|(k, _)| *k).collect();
    assert_eq!(keys, ["checked", "missing", "unresolved", "unconfirmed"]);
}

#[test]
fn kills_containment_as_a_string_prefix() {
    let mut fs = fs_with(&[("/S/rx/a.py", b"m"), ("/S/r/in.py", b"m")]);
    fs.insert(b"/S/r/pkg", Node::Link(b"/S/rx".to_vec()));
    fs.insert(b"/S/r/sub", Node::Link(b"/S/r".to_vec()));
    let names = by_text(&["m"]);
    let r = check(
        &fs,
        &[("TOP", "pkg/a.py"), ("TOP", "sub/in.py")],
        Some("/S/r"),
        None,
        &names,
    );
    assert_eq!(bindings(&r.unresolved), ["unresolved"]);
    assert_eq!(bindings(&r.checked), ["top_level"]);
}

#[test]
fn kills_a_root_of_slash_read_as_no_prefix() {
    let fs = fs_with(&[("/S/a.py", b"m")]);
    let names = by_text(&["m"]);
    let r = check(&fs, &[("TOP", "S/a.py")], Some("/"), None, &names);
    assert_eq!(bindings(&r.checked), ["top_level"]);
}

#[test]
fn kills_module_spellings_read_as_paths() {
    let fs = fs_with(&[("/S/p/a.py", b"m"), ("/S/a.py", b"m")]);
    let names = by_text(&["m"]);
    for module in [
        "a.py",
        "p/",
        "/p/a.py",
        "p/a.py/",
        "~/a.py",
        "p\\a.py",
        "\\p/a.py",
        "x:/a.py",
        "p:/a.py",
        "p//a.py",
        "p/./a.py",
        "p/../p/a.py",
        "p",
    ] {
        let r = check(&fs, &[("TOP", module)], Some("/S"), None, &names);
        assert_eq!(bindings(&r.unresolved), ["unresolved"], "{module}");
    }
    let r = check(&fs, &[("TOP", "/p/a.py/")], Some("/S"), None, &names);
    assert_eq!(bindings(&r.unresolved), ["unresolved"]);
}

#[test]
fn kills_resolving_with_no_citation_or_no_root() {
    let fs = fs_with(&[]);
    let names = by_text(&[]);
    let machine = Machine {
        fs: &fs,
        flavour: Flavour::Windows,
        cwd: b"/S",
    };
    assert_eq!(
        check_symbol_citations(&[], Some(&t("/S")), None, &machine, &names),
        Ok(SymbolDriftReport::default())
    );
    let one = [(t("x"), t("p/a.py"))];
    assert_eq!(
        check_symbol_citations(&one, None, None, &machine, &names),
        Ok(SymbolDriftReport::default())
    );
    assert_eq!(
        check_symbol_citations(&one, Some(&t("/S")), None, &machine, &names),
        Err(Declined::NotPorted)
    );
}

#[test]
fn kills_a_lone_surrogate_or_nul_resolved_as_text() {
    let fs = fs_with(&[("/S/p/a.py", b"m")]);
    let names = by_text(&["m"]);
    let machine = Machine {
        fs: &fs,
        flavour: Flavour::Posix,
        cwd: b"/S",
    };
    let mut module = t("p/a.py");
    module.push(0xD800);
    let r = check_symbol_citations(
        &[(t("TOP"), module)],
        Some(&t("/S")),
        None,
        &machine,
        &names,
    );
    assert_eq!(bindings(&r.unwrap().unresolved), ["unresolved"]);
    let mut root = t("/S");
    root.push(0);
    let r = check_symbol_citations(
        &[(t("TOP"), t("p/a.py"))],
        Some(&root),
        None,
        &machine,
        &names,
    );
    assert!(!r.unwrap().truthy());
}

#[test]
fn kills_module_guards_that_wait_for_the_file_system() {
    let fs = fs_with(&[
        ("/S/pkg/a\\b.py", b"m"),
        ("/S/~pkg/a.py", b"m"),
        ("/S/~/pkg/a.py", b"m"),
        ("/S/pkg/a.py", b"m"),
        ("/S/pkg/?.py", b"m"),
    ]);
    let names = by_text(&["m"]);
    let r = check(&fs, &[("TOP", "pkg/a.py")], Some("/S"), None, &names);
    assert_eq!(bindings(&r.checked), ["top_level"]);
    for module in ["pkg/a\\b.py", "~pkg/a.py", "~/pkg/a.py"] {
        let r = check(&fs, &[("TOP", module)], Some("/S"), None, &names);
        assert_eq!(bindings(&r.unresolved), ["unresolved"], "{module}");
        assert!(r.checked.is_empty(), "{module}");
    }
}

#[test]
fn kills_an_fsencode_that_escapes_too_wide_or_substitutes_a_byte() {
    let fs = fs_with(&[("/S/pkg/a.py", b"m"), ("/S/pkg/?.py", b"m")]);
    let names = by_text(&["m"]);
    let machine = Machine {
        fs: &fs,
        flavour: Flavour::Posix,
        cwd: b"/S",
    };
    for cp in [0xDC61, 0xD800] {
        let mut module = t("pkg/");
        module.push(cp);
        module.extend(t(".py"));
        let r = check_symbol_citations(
            &[(t("TOP"), module)],
            Some(&t("/S")),
            None,
            &machine,
            &names,
        )
        .unwrap();
        assert_eq!(bindings(&r.unresolved), ["unresolved"], "{cp:x}");
        assert!(r.checked.is_empty(), "{cp:x}");
    }
}

#[test]
fn kills_a_truthiness_that_ignores_unconfirmed_citations() {
    let fs = fs_with(&[("/S/p/a.py", b"m")]);
    let names = by_text(&["m"]);
    let standin = Anchor {
        root: Some(t("/S")),
        convicting: false,
    };
    let r = check(
        &fs,
        &[("nothing", "p/a.py")],
        Some("/S"),
        Some(standin),
        &names,
    );
    assert_eq!(bindings(&r.unconfirmed), ["unconfirmed"]);
    assert!(r.checked.is_empty() && r.unresolved.is_empty());
    assert!(r.truthy());
}
