//! Where macOS answers ENAMETOOLONG as a path grows through a link
//! (measured on this host: the path as given at 1,024 bytes or more, and
//! at each link followed its target plus the bytes after it at 1,024 or
//! more), held between `StdFs` and `MemFs` on the same tree at the same
//! paths. macOS only: Linux's limit is 4,096, so these cases cannot sit in
//! a POSIX fixture shared with Linux.

#![cfg(target_os = "macos")]

use bettermemory_core::fsdata::{Flavour, Fs, MemFs, Node, OsError, StdFs};

fn answer<F: Fs>(fs: &F, path: &[u8]) -> (Result<(), OsError>, Result<(), OsError>) {
    (fs.stat(path).map(|_| ()), fs.listdir(path).map(|_| ()))
}

/// A remainder of exactly `n` bytes: `/.` repeated, `/` to make it up.
fn remainder(n: usize) -> Vec<u8> {
    let mut out = Vec::new();
    while out.len() + 2 <= n {
        out.extend_from_slice(b"/.");
    }
    if out.len() < n {
        out.push(b'/');
    }
    out
}

#[test]
fn link_expansion_meets_the_path_limit_as_macos_does() {
    use std::sync::atomic::{AtomicUsize, Ordering};
    static NEXT: AtomicUsize = AtomicUsize::new(0);
    let dir = std::env::temp_dir().join(format!(
        "bm-pathmax-{}-{}",
        std::process::id(),
        NEXT.fetch_add(1, Ordering::Relaxed)
    ));
    std::fs::create_dir(&dir).expect("a new scratch directory");
    let root = std::fs::canonicalize(&dir).expect("resolved");
    let a = "a".repeat(255);
    let deep = root.join(&a).join(&a).join(&a);
    std::fs::create_dir_all(&deep).unwrap();
    std::fs::create_dir(deep.join("sub")).unwrap();
    std::os::unix::fs::symlink(&deep, root.join("L")).unwrap();
    let mid = root.join("b".repeat(200)).join("c".repeat(200));
    std::fs::create_dir_all(&mid).unwrap();
    std::os::unix::fs::symlink(&mid, root.join("M")).unwrap();

    let text = |p: &std::path::Path| p.to_str().unwrap().as_bytes().to_vec();
    let mut mem = MemFs::new(Flavour::Posix);
    mem.insert(&text(&deep.join("sub")), Node::Dir);
    mem.insert(&text(&root.join("L")), Node::Link(text(&deep)));
    mem.insert(&text(&mid), Node::Dir);
    mem.insert(&text(&root.join("M")), Node::Link(text(&mid)));

    let mut queries: Vec<Vec<u8>> = Vec::new();
    let l = text(&root.join("L"));
    // The review's tree: `L/A` and `L/../A/x`, about 300 bytes typed and
    // past 1,024 once `L` is followed; and short ones that are not.
    let long = format!("/{a}");
    let back = format!("/../{a}/x");
    for rest in [
        long.as_bytes(),
        back.as_bytes(),
        b"/sub",
        b"/../x",
        b"/sub/",
        b"",
    ] {
        queries.push([l.as_slice(), rest].concat());
    }
    // Through M: the target and the bytes after it, from 1,020 to 1,027.
    let m = text(&root.join("M"));
    let target = text(&mid).len();
    for total in 1020..1028 {
        queries.push([m.as_slice(), &remainder(total - target)].concat());
    }
    // The path as given, from 1,020 to 1,027.
    let r = text(&root);
    for total in 1020..1028 {
        queries.push([r.as_slice(), &remainder(total - r.len())].concat());
    }
    let mut differ = Vec::new();
    let mut too_long = 0;
    for q in &queries {
        let (real, model) = (answer(&StdFs, q), answer(&mem, q));
        if real.0.as_ref().err().and_then(|e| e.errno) == Some("ENAMETOOLONG") {
            too_long += 1;
        }
        if real != model {
            differ.push(format!("{} bytes: host {real:?}, model {model:?}", q.len()));
        }
    }
    let _ = std::fs::remove_dir_all(&dir);
    assert!(differ.is_empty(), "{differ:#?}");
    // The host did answer ENAMETOOLONG where the cases were built for it.
    assert!(too_long >= 10, "{too_long} host ENAMETOOLONG answers");
}
