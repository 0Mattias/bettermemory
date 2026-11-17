//! The legacy remote reads stay bounded and typed: 10,000 names, a 1 MiB
//! remote file, a FIFO where a remote file should be (nothing waits), a
//! `remotes` folder nested 1,000 deep, all on a 512 KiB thread; names
//! sorted as the reference sorts text; the Windows flavour declining a
//! stamp it cannot hold.

mod common;

use bettermemory_core::fsdata::{Flavour, MemFs, Node};
use bettermemory_core::gitconfig::legacy::{
    file_stamp, legacy_names, legacy_remote, Against, Files, Halt, Legacy, NotPorted, LEGACY_LIMIT,
};

fn on_small_stack(f: impl FnOnce() + Send + 'static) {
    std::thread::Builder::new()
        .stack_size(512 << 10)
        .spawn(f)
        .expect("a thread")
        .join()
        .expect("no panic");
}

fn against() -> Against {
    Against {
        resets: Some(true),
        default: Some(b"master".to_vec()),
    }
}

#[test]
fn ten_thousand_names_a_long_file_and_a_deep_folder_stay_bounded() {
    on_small_stack(|| {
        let mut fs = MemFs::new(Flavour::Posix);
        fs.insert(b"/c", Node::Dir);
        for i in 0..10_000 {
            fs.insert(
                format!("/c/remotes/r{i}").as_bytes(),
                Node::File(Vec::new()),
            );
        }
        let mut files = Files::new();
        let got = legacy_names(&fs, Flavour::Posix, b"/c", &mut files);
        assert_eq!(
            got,
            Err(Halt::Declines(
                b"the legacy remote folders name more than 256 remotes".to_vec()
            ))
        );
        assert_eq!(files.len(), 1, "stops after the first folder");

        // A remote file of 1 MiB: refused at the bound.
        let mut fs = MemFs::new(Flavour::Posix);
        let mut text = b"URL: u\n".to_vec();
        text.resize(1 << 20, b'a');
        fs.insert(b"/c/remotes/origin", Node::File(text));
        let mut files = Files::new();
        let got = legacy_remote(
            &fs,
            Flavour::Posix,
            b"/c",
            b"origin",
            &mut files,
            &against(),
        );
        assert_eq!(
            got,
            Err(Halt::Declines(
                b"/c/remotes/origin is larger than 64 KiB".to_vec()
            ))
        );
        // At the bound exactly: read.
        let mut text = b"URL: u\n".to_vec();
        text.resize(LEGACY_LIMIT, b'\n');
        fs.insert(b"/c/remotes/origin", Node::File(text));
        let got = legacy_remote(
            &fs,
            Flavour::Posix,
            b"/c",
            b"origin",
            &mut files,
            &against(),
        );
        assert_eq!(got, Ok(Legacy::Urls(vec![b"u".to_vec()])));

        // A remotes folder nested 1,000 deep: one name, a folder, read as
        // nothing.
        let mut fs = MemFs::new(Flavour::Posix);
        let mut deep = b"/c/remotes".to_vec();
        for _ in 0..1000 {
            deep.extend_from_slice(b"/a");
        }
        fs.insert(&deep, Node::Dir);
        let mut files = Files::new();
        let names = legacy_names(&fs, Flavour::Posix, b"/c", &mut files).expect("listed");
        assert_eq!(names, vec![b"a".to_vec()]);
        let got = legacy_remote(&fs, Flavour::Posix, b"/c", b"a", &mut files, &against());
        assert_eq!(got, Ok(Legacy::Urls(Vec::new())));
    });
}

#[test]
fn names_sort_as_the_reference_sorts_text() {
    // Python sorts `os.listdir`'s text by code point: a byte that is not
    // UTF-8 is a surrogate (U+DCFF), below U+10000, though 0xFF is above
    // 0xF0 as a byte.
    let mut fs = MemFs::new(Flavour::Posix);
    for name in [&b"\xf0\x90\x80\x80"[..], b"\xff", b"a", b"\xc3\xa9"] {
        fs.insert(
            &[&b"/c/remotes/"[..], name].concat(),
            Node::File(Vec::new()),
        );
    }
    fs.insert(b"/c/branches/\xff", Node::File(Vec::new()));
    let mut files = Files::new();
    let names = legacy_names(&fs, Flavour::Posix, b"/c", &mut files).expect("listed");
    assert_eq!(
        names,
        vec![
            b"a".to_vec(),
            b"\xc3\xa9".to_vec(),
            b"\xff".to_vec(),
            b"\xf0\x90\x80\x80".to_vec()
        ]
    );
}

#[test]
fn the_windows_flavour_declines_a_stamp_it_cannot_hold() {
    let mut fs = MemFs::new(Flavour::Windows);
    fs.insert(b"/c/remotes/origin", Node::File(b"URL: u\n".to_vec()));
    assert_eq!(file_stamp(&fs, b"/c/remotes/origin"), Err(NotPorted));
    assert_eq!(file_stamp(&fs, b"/c/missing"), Ok(None));
    let mut files = Files::new();
    assert_eq!(
        legacy_names(&fs, Flavour::Windows, b"/c", &mut files),
        Err(Halt::NotPorted)
    );
}

#[cfg(unix)]
#[test]
fn a_fifo_where_a_remote_file_should_be_is_refused_without_waiting() {
    use bettermemory_core::fsdata::StdFs;
    use std::sync::atomic::{AtomicUsize, Ordering};
    static NEXT: AtomicUsize = AtomicUsize::new(0);
    let n = NEXT.fetch_add(1, Ordering::Relaxed);
    let dir = std::env::temp_dir().join(format!("bm-gclegacy-fifo-{}-{n}", std::process::id()));
    std::fs::create_dir(&dir).unwrap();
    let dir = std::fs::canonicalize(&dir).unwrap();
    std::fs::create_dir(dir.join("remotes")).unwrap();
    let made = std::process::Command::new("mkfifo")
        .arg(dir.join("remotes/origin"))
        .status()
        .is_ok_and(|s| s.success());
    assert!(made, "mkfifo");
    let root = dir.to_str().unwrap().as_bytes().to_vec();
    let (tx, rx) = std::sync::mpsc::channel();
    let reader = std::thread::Builder::new()
        .stack_size(512 << 10)
        .spawn(move || {
            let mut files = Files::new();
            let got = legacy_remote(
                &StdFs,
                Flavour::Posix,
                &root,
                b"origin",
                &mut files,
                &against(),
            );
            tx.send(got).unwrap();
        })
        .unwrap();
    // No writer ever opens the FIFO: a read that waited would never end,
    // and this bound only turns that hang into a failure.
    let got = rx
        .recv_timeout(std::time::Duration::from_secs(120))
        .expect("the read returns without waiting on the FIFO");
    reader.join().unwrap();
    let _ = std::fs::remove_dir_all(&dir);
    match got {
        Err(Halt::Declines(why)) => assert!(why.ends_with(b"remotes/origin is not a regular file")),
        other => panic!("{other:?}"),
    }
}
