//! Hostile git directories: files that claim far more than the bounds,
//! symbolic cycles and chains past the depth, NULs, bytes that are not
//! UTF-8, a file system that fails every call, and the Windows flavour's
//! answers to links and ENOTDIR. No panic, and the work stays within the
//! bounds: the file system counts what it serves.

use std::cell::{Cell, RefCell};
use std::time::Instant;

use bettermemory_core::githead::{
    self, fs::Node, Flavour, FsError, GitDir, HeadCommit, MemFs, RefFs, Stop, MAX_READS,
    PACKED_LIMIT, REF_LIMIT,
};

const H1: &[u8] = b"1111111111111111111111111111111111111111";

fn gd() -> GitDir {
    GitDir {
        gitdir: b"g".to_vec(),
        commondir: b"g".to_vec(),
    }
}

fn mem(files: &[(&str, &[u8])]) -> MemFs {
    let mut fs = MemFs::new(Flavour::Posix);
    for (path, data) in files {
        fs.insert(path.as_bytes(), Node::File(data.to_vec()));
    }
    fs
}

/// Counts the calls and the bytes served by another file system.
struct Counting<F> {
    inner: F,
    reads: Cell<usize>,
    served: Cell<usize>,
    largest: Cell<usize>,
    paths: RefCell<Vec<Vec<u8>>>,
}

impl<F: RefFs> Counting<F> {
    fn new(inner: F) -> Self {
        Counting {
            inner,
            reads: Cell::new(0),
            served: Cell::new(0),
            largest: Cell::new(0),
            paths: RefCell::new(Vec::new()),
        }
    }
}

impl<F: RefFs> RefFs for Counting<F> {
    fn read(&self, path: &[u8], limit: usize, follow: bool) -> Result<Vec<u8>, FsError> {
        self.reads.set(self.reads.get() + 1);
        self.paths.borrow_mut().push(path.to_vec());
        let out = self.inner.read(path, limit, follow)?;
        assert!(out.len() <= limit + 1, "a read past its limit");
        self.served.set(self.served.get() + out.len());
        self.largest.set(self.largest.get().max(out.len()));
        Ok(out)
    }

    fn lstat_is_dir(&self, path: &[u8]) -> Result<bool, FsError> {
        self.inner.lstat_is_dir(path)
    }
}

/// Every file claims 1 GB: a read serves `limit + 1` bytes of `fill`,
/// built only to the limit asked for.
struct Claims {
    fill: u8,
}

impl RefFs for Claims {
    fn read(&self, _path: &[u8], limit: usize, _follow: bool) -> Result<Vec<u8>, FsError> {
        Ok(vec![self.fill; (1usize << 30).min(limit + 1)])
    }

    fn lstat_is_dir(&self, _path: &[u8]) -> Result<bool, FsError> {
        Ok(false)
    }
}

/// Fails every call with one error.
struct Failing(FsError);

impl RefFs for Failing {
    fn read(&self, _path: &[u8], _limit: usize, _follow: bool) -> Result<Vec<u8>, FsError> {
        Err(self.0)
    }

    fn lstat_is_dir(&self, _path: &[u8]) -> Result<bool, FsError> {
        Err(self.0)
    }
}

fn all_answers(fs: &dyn RefFs) -> (Option<String>, Option<String>, Option<bool>, HeadCommit) {
    let gd = gd();
    let _ = githead::head_bytes(fs, &gd);
    let _ = githead::resolve(fs, &gd);
    let _ = githead::packed_names(fs, &gd, b"refs/heads/main");
    (
        githead::head_sha(fs, &gd),
        githead::head_ref(fs, &gd),
        githead::head_unborn(fs, &gd),
        githead::head_commit(fs, &gd),
    )
}

#[test]
fn files_claiming_a_gigabyte_are_read_to_their_bound() {
    for fill in [b' ', b'\0', b'1', b'\n', 0xff] {
        let fs = Counting::new(Claims { fill });
        let answers = all_answers(&fs);
        assert_eq!(answers, (None, None, None, HeadCommit::Decline), "{fill}");
        // Each read stops at its bound (`Counting` asserts it): HEAD at
        // 64 KiB + 1, refused there; `packed_names` reads packed-refs to
        // 64 MiB + 1, whatever the file claims.
        assert!(fs.largest.get() <= PACKED_LIMIT + 1);
        let heads = fs
            .paths
            .borrow()
            .iter()
            .filter(|p| p.ends_with(b"HEAD"))
            .count();
        assert!(fs.served.get() <= heads * (REF_LIMIT + 1) + 2 * (PACKED_LIMIT + 1));
    }
}

#[test]
fn a_packed_file_of_fifty_megabytes_is_read_once_per_question() {
    let mut data = Vec::with_capacity(50 << 20);
    data.extend_from_slice(b"# pack-refs with: peeled fully-peeled sorted \n");
    let mut i = 0u64;
    while data.len() < (50 << 20) {
        data.extend_from_slice(format!("{} refs/tags/t{i:010}\n", "2".repeat(40)).as_bytes());
        i += 1;
    }
    data.extend_from_slice(H1);
    data.extend_from_slice(b" refs/tags/z\n");
    let fs = Counting::new(mem(&[
        ("g/HEAD", b"ref: refs/heads/main\n"),
        ("g/packed-refs", &data),
    ]));
    let gd = gd();
    assert_eq!(
        githead::resolve(&fs, &gd),
        Ok((Some(b"refs/heads/main".to_vec()), None))
    );
    assert_eq!(fs.reads.get(), 3, "HEAD, the loose ref, packed-refs");
    assert!(fs.served.get() <= data.len() + 64);
    assert_eq!(githead::head_unborn(&fs, &gd), Some(true));
    // HEAD at a tag in the file: the hash, to be peeled.
    let fs = mem(&[("g/HEAD", b"ref: refs/tags/z\n"), ("g/packed-refs", &data)]);
    assert_eq!(
        githead::head_commit(&fs, &gd),
        HeadCommit::Peel(String::from_utf8(H1.to_vec()).unwrap())
    );
    // One byte past the bound declines without reading further.
    let mut over = vec![b'x'; PACKED_LIMIT - 1];
    over.extend_from_slice(b"\n\n");
    let fs = Counting::new(mem(&[
        ("g/HEAD", b"ref: refs/heads/main\n"),
        ("g/packed-refs", &over),
    ]));
    assert_eq!(githead::resolve(&fs, &gd), Err(Stop::Unsettled));
    assert_eq!(fs.largest.get(), PACKED_LIMIT + 1);
}

#[test]
fn chains_stop_at_the_depth_and_cycles_end() {
    let gd = gd();
    // A cycle of two and a self-loop: at most MAX_READS reads.
    for files in [
        vec![
            ("g/HEAD", &b"ref: refs/heads/a\n"[..]),
            ("g/refs/heads/a", b"ref: refs/heads/b\n"),
            ("g/refs/heads/b", b"ref: refs/heads/a\n"),
        ],
        vec![
            ("g/HEAD", &b"ref: refs/heads/a\n"[..]),
            ("g/refs/heads/a", b"ref: refs/heads/a\n"),
        ],
    ] {
        let fs = Counting::new(mem(&files));
        assert_eq!(githead::resolve(&fs, &gd), Err(Stop::Unsettled));
        assert_eq!(fs.reads.get(), MAX_READS);
        assert_eq!(githead::head_unborn(&fs, &gd), None);
    }
    // A chain of 1000 refs is read to the depth only.
    let names: Vec<String> = (0..1000).map(|i| format!("refs/heads/s{i}")).collect();
    let mut owned: Vec<(String, Vec<u8>)> =
        vec![("g/HEAD".into(), format!("ref: {}\n", names[0]).into_bytes())];
    for pair in names.windows(2) {
        owned.push((
            format!("g/{}", pair[0]),
            format!("ref: {}\n", pair[1]).into_bytes(),
        ));
    }
    let files: Vec<(&str, &[u8])> = owned
        .iter()
        .map(|(p, d)| (p.as_str(), d.as_slice()))
        .collect();
    let fs = Counting::new(mem(&files));
    assert_eq!(githead::resolve(&fs, &gd), Err(Stop::Unsettled));
    assert_eq!(fs.reads.get(), MAX_READS);
}

#[test]
fn nuls_and_bytes_that_are_not_utf8() {
    let gd = gd();
    for head in [
        &b"\0\0\0\0"[..],
        &[0u8; REF_LIMIT],
        b"\xff\xfe\xfd",
        b"ref: \0refs/heads/main\n",
        b"ref: refs/heads/\0\n",
    ] {
        let fs = mem(&[("g/HEAD", head), ("g/refs/heads/main", H1)]);
        assert_eq!(githead::resolve(&fs, &gd), Err(Stop::Unsettled));
        assert_eq!(githead::head_unborn(&fs, &gd), None);
    }
    // A name that is not UTF-8 resolves, but head_ref has no text for it.
    let fs = mem(&[("g/HEAD", b"ref: refs/heads/\xff\n")]);
    assert_eq!(
        githead::resolve(&fs, &gd),
        Ok((Some(b"refs/heads/\xff".to_vec()), None))
    );
    assert_eq!(githead::head_ref(&fs, &gd), None);
    assert_eq!(githead::head_unborn(&fs, &gd), Some(true));
    let fs = mem(&[
        ("g/HEAD", b"ref: refs/heads/main\n"),
        ("g/packed-refs", &[0u8; 4096]),
    ]);
    assert_eq!(githead::resolve(&fs, &gd), Err(Stop::Unsettled));
    assert!(!githead::packed_names(&fs, &gd, b"refs/heads/main"));
}

#[test]
fn a_file_system_that_fails_every_call() {
    for error in [
        FsError::Missing,
        FsError::NotADirectory,
        FsError::IsADirectory,
        FsError::Other,
    ] {
        let answers = all_answers(&Failing(error));
        assert_eq!(
            answers,
            (None, None, None, HeadCommit::Decline),
            "{error:?}"
        );
    }
}

#[test]
fn the_windows_flavour_follows_links_and_reads_enotdir_as_missing() {
    let gd = gd();
    let mut posix = MemFs::new(Flavour::Posix);
    posix.insert(b"g/real", Node::File(b"ref: refs/heads/main\n".to_vec()));
    posix.insert(b"g/HEAD", Node::Link(b"real".to_vec()));
    posix.insert(b"g/refs/heads/main", Node::File(H1.to_vec()));
    let mut windows = posix.clone();
    windows.flavour = Flavour::Windows;
    assert_eq!(githead::head_bytes(&posix, &gd), None);
    assert_eq!(
        githead::head_bytes(&windows, &gd),
        Some(b"ref: refs/heads/main\n".to_vec())
    );
    assert_eq!(
        githead::head_sha(&windows, &gd).as_deref(),
        Some(std::str::from_utf8(H1).unwrap())
    );
    // `refs/heads` a file: ENOTDIR on POSIX raises through `_lookup`; on
    // Windows it is "not found" and packed-refs answers.
    let mut posix = mem(&[
        ("g/HEAD", b"ref: refs/heads/main\n"),
        ("g/refs/heads", b"x\n"),
        ("g/packed-refs", &[H1, b" refs/heads/main\n"].concat()),
    ]);
    assert_eq!(githead::resolve(&posix, &gd), Err(Stop::Os));
    assert_eq!(githead::head_unborn(&posix, &gd), None);
    posix.flavour = Flavour::Windows;
    assert_eq!(
        githead::resolve(&posix, &gd),
        Ok((
            Some(b"refs/heads/main".to_vec()),
            Some(String::from_utf8(H1.to_vec()).unwrap())
        ))
    );
}

/// Best of five runs of `work`, in seconds.
fn best_of_five(work: &dyn Fn()) -> f64 {
    (0..5)
        .map(|_| {
            let start = Instant::now();
            work();
            start.elapsed().as_secs_f64()
        })
        .fold(f64::INFINITY, f64::min)
}

#[test]
fn packed_refs_scale_linearly() {
    // Shapes that make a naive search slow: near matches of a long name,
    // dots and slashes the bad-name scan stops at, records of one name.
    let name = [b"refs/heads/".as_slice(), &[b'a'; 4096]].concat();
    let shapes: Vec<Box<dyn Fn(usize) -> Vec<u8>>> = vec![
        Box::new(move |n| [b" ".as_slice(), &[b'a'; 4095]].concat().repeat(n / 4096)),
        Box::new(|n| b"/./.".repeat(n / 4)),
        Box::new(|n| [H1, b" refs/heads/a\n".as_slice()].concat().repeat(n / 54)),
    ];
    let gd = gd();
    for (i, shape) in shapes.iter().enumerate() {
        let time = |n: usize| {
            let mut data = shape(n);
            data.push(b'\n');
            let fs = mem(&[("g/HEAD", b"ref: refs/heads/a\n"), ("g/packed-refs", &data)]);
            best_of_five(&|| {
                let _ = githead::resolve(&fs, &gd);
                let _ = githead::packed_names(&fs, &gd, &name);
            })
        };
        let small = time(200_000);
        let large = time(2_000_000);
        assert!(
            large <= small * 30.0 + 0.05,
            "shape {i}: {small:.4}s for 200 KB, {large:.4}s for 2 MB"
        );
    }
}

#[test]
fn the_windows_flavour_declines_a_link_to_a_directory_and_bytes_not_utf8() {
    let gd = gd();
    let mut fs = mem(&[
        ("g/HEAD", b"ref: refs/heads/main\n"),
        ("g/packed-refs", &[H1, b" refs/heads/main\n"].concat()),
    ]);
    fs.insert(b"g/d/x", Node::File(b"x".to_vec()));
    fs.insert(b"g/refs/heads/main", Node::Link(b"../../d".to_vec()));
    // POSIX: O_NOFOLLOW refuses the link (ELOOP), an OSError.
    assert_eq!(githead::resolve(&fs, &gd), Err(Stop::Os));
    // Windows: the open follows it to a directory, PermissionError rises.
    fs.flavour = Flavour::Windows;
    assert_eq!(
        fs.read(b"g/refs/heads/main", 10, false),
        Err(FsError::Other)
    );
    assert_eq!(
        fs.read(b"g/refs/heads/main", 10, true),
        Err(FsError::IsADirectory)
    );
    assert_eq!(githead::resolve(&fs, &gd), Err(Stop::Os));
    assert_eq!(githead::head_unborn(&fs, &gd), None);
    // A name that is not UTF-8: ValueError on Windows, a decline; POSIX
    // reads it as missing and goes on to packed-refs.
    let mut fs = mem(&[
        ("g/HEAD", b"ref: refs/heads/\xff\n"),
        ("g/packed-refs", &[H1, b" refs/heads/\xff\n"].concat()),
    ]);
    assert!(githead::head_sha(&fs, &gd).is_some());
    fs.flavour = Flavour::Windows;
    assert_eq!(githead::resolve(&fs, &gd), Err(Stop::Os));
    assert_eq!(githead::head_sha(&fs, &gd), None);
}

#[test]
fn a_large_packed_file_is_read_once_and_held_once() {
    // 64 MiB of one record repeated: every line ends with the name asked
    // for, so a search that held every match would hold a million.
    let line = [H1, b" refs/heads/main\n".as_slice()].concat();
    let data = line.repeat(PACKED_LIMIT / line.len());
    let fs = Counting::new(mem(&[
        ("g/HEAD", b"ref: refs/heads/main\n"),
        ("g/packed-refs", &data),
    ]));
    let gd = gd();
    // A duplicate record declines at the second match.
    assert_eq!(githead::resolve(&fs, &gd), Err(Stop::Unsettled));
    let packed = fs
        .paths
        .borrow()
        .iter()
        .filter(|p| p.ends_with(b"packed-refs"))
        .count();
    assert_eq!(packed, 1, "packed-refs read once");
    assert!(fs.largest.get() <= PACKED_LIMIT + 1);
    assert!(githead::packed_names(&fs, &gd, b"refs/heads/main"));
}
