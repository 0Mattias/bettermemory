//! Hostile input to `symbols`: many citations, long module paths, large
//! name sets, a module at the size bound, a chain of links. No panic, and
//! the work grows with the input.

mod timing;

use std::collections::BTreeSet;

use bettermemory_core::fsdata::{Flavour, MemFs, Node};
use bettermemory_core::pyvalue::PyStr;
use bettermemory_core::symbols::{
    check_symbol_citations, tier, Machine, ModuleNames, NameSets, MAX_MODULE_BYTES,
};

fn t(s: &str) -> PyStr {
    s.chars().map(u32::from).collect()
}

struct Fixed(NameSets);

impl ModuleNames for Fixed {
    fn names(&self, _source: &[u32]) -> Option<NameSets> {
        Some(self.0.clone())
    }
}

fn fixed(n: usize) -> Fixed {
    let names: BTreeSet<PyStr> = (0..n).map(|i| t(&format!("n{i}"))).collect();
    Fixed((names.clone(), names))
}

fn machine(fs: &MemFs) -> Machine<'_, MemFs> {
    Machine {
        fs,
        flavour: Flavour::Posix,
        cwd: b"/S",
    }
}

#[test]
fn many_citations_of_one_module_grow_linearly() {
    let mut fs = MemFs::new(Flavour::Posix);
    fs.insert(b"/S/p/a.py", Node::File(b"x".to_vec()));
    let names = fixed(100);
    let run = |n: usize| {
        let cites: Vec<(PyStr, PyStr)> = (0..n)
            .map(|i| (t(&format!("n{}.x", i % 200)), t("p/a.py")))
            .collect();
        let r =
            check_symbol_citations(&cites, Some(&t("/S")), None, &machine(&fs), &names).unwrap();
        assert_eq!(r.checked.len(), n);
    };
    timing::grows_within(
        "check: 2,000 and 20,000 citations",
        timing::TENFOLD,
        &mut || timing::once(|| run(2_000)),
        &mut || timing::once(|| run(20_000)),
    );
}

#[test]
fn long_and_strange_modules_do_not_panic() {
    let mut fs = MemFs::new(Flavour::Posix);
    fs.insert(b"/S", Node::Dir);
    let deep = vec!["d"; 400].join("/") + "/m.py";
    let cites: Vec<(PyStr, PyStr)> = [
        deep.as_str(),
        "",
        "/",
        "//",
        ":",
        "a:",
        "x/\u{0}",
        "x/\u{ffff}/\u{10ffff}",
    ]
    .iter()
    .map(|m| (t("s"), t(m)))
    .chain([(vec![0xDFFF; 1000], vec![0xDC80, u32::from('/'), 0xD800])])
    .collect();
    let r = check_symbol_citations(&cites, Some(&t("/S")), None, &machine(&fs), &fixed(1)).unwrap();
    assert_eq!(r.unresolved.len(), cites.len());
}

#[test]
fn a_chain_of_links_and_a_loop_do_not_panic() {
    let mut fs = MemFs::new(Flavour::Posix);
    fs.insert(b"/S/p/l0", Node::File(b"x".to_vec()));
    for i in 1..200 {
        fs.insert(
            format!("/S/p/l{i}").as_bytes(),
            Node::Link(format!("l{}", i - 1).into_bytes()),
        );
    }
    fs.insert(b"/S/p/loop", Node::Link(b"loop".to_vec()));
    let cites = vec![(t("n0"), t("p/l199")), (t("n0"), t("p/loop"))];
    let r = check_symbol_citations(&cites, Some(&t("/S")), None, &machine(&fs), &fixed(1)).unwrap();
    assert_eq!(r.checked.len(), 1);
    assert_eq!(r.unresolved.len(), 1);
}

#[test]
fn a_module_at_the_bound_and_large_sets() {
    let mut fs = MemFs::new(Flavour::Posix);
    let mut data = vec![b'\r'; MAX_MODULE_BYTES as usize - 1];
    data.push(0xff);
    fs.insert(b"/S/p/a.py", Node::File(data));
    let cites = vec![(t("n99999"), t("p/a.py"))];
    let r = check_symbol_citations(&cites, Some(&t("/S")), None, &machine(&fs), &fixed(100_000))
        .unwrap();
    assert_eq!(r.checked.len(), 1);
    let big: PyStr = std::iter::repeat_n(u32::from('.'), 1_000_000).collect();
    let empty = BTreeSet::new();
    assert_eq!(tier(&big, &empty, &empty).as_str(), "absent");
}
