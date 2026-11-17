//! `access` and `open_no_follow` on hostile paths, on a 512 KiB thread
//! (debug build): typed answers, no panic, no stack overflow; and a FIFO
//! opened without following, which must not wait for a writer.

use bettermemory_core::fsdata::{Access, Flavour, Fs, Kind, MemFs, Node, PyErr, PATH_MAX};

fn on_small_stack(work: impl FnOnce() + Send + 'static) {
    std::thread::Builder::new()
        .stack_size(512 * 1024)
        .spawn(work)
        .expect("a thread")
        .join()
        .expect("no panic on a 512 KiB stack");
}

fn hostile() -> Vec<Vec<u8>> {
    let mut out: Vec<Vec<u8>> = vec![
        Vec::new(),
        b"/".to_vec(),
        b"//".to_vec(),
        b"\0".to_vec(),
        b"/a\0b".to_vec(),
        b"/loop".to_vec(),
        b"/loop/x".to_vec(),
        b"/chain0".to_vec(),
        b"/f/".to_vec(),
        b"/f/..".to_vec(),
        vec![0xff, b'/', 0xfe],
    ];
    out.push(b"/..".repeat(PATH_MAX / 3 - 1));
    out.push(b"/d".repeat(PATH_MAX / 2 - 1));
    out.push(b"/d".repeat(PATH_MAX));
    out.push(b"/".repeat(PATH_MAX * 64));
    out.push(vec![b'n'; 300]);
    out
}

fn tree(flavour: Flavour) -> MemFs {
    let mut fs = MemFs::new(flavour);
    fs.insert(b"/f", Node::File(b"x".to_vec()));
    fs.insert(&b"/d".repeat(PATH_MAX / 2 - 1), Node::Dir);
    fs.insert(b"/loop", Node::Link(b"/loop".to_vec()));
    for i in 0..100 {
        fs.insert(
            format!("/chain{i}").as_bytes(),
            Node::Link(format!("/chain{}", i + 1).into_bytes()),
        );
    }
    fs.insert(b"/chain100", Node::Link(b"/f".to_vec()));
    fs
}

#[test]
fn hostile_paths_answer_typed_on_a_small_stack() {
    on_small_stack(|| {
        for flavour in [Flavour::Posix, Flavour::Windows] {
            let fs = tree(flavour);
            for path in hostile() {
                for mode in [
                    Access::READ,
                    Access::WRITE,
                    Access::EXECUTE,
                    Access::default(),
                ] {
                    match fs.access(&path, mode) {
                        Ok(_) => {}
                        Err(e) => assert_eq!(e, PyErr::Value, "{path:?}"),
                    }
                }
                if let Ok(mut file) = fs.open_no_follow(&path) {
                    let kind = fs.fstat(&mut file).expect("fstat").kind;
                    assert_ne!(kind, Kind::Link);
                }
            }
            // A NUL is ValueError, never False.
            assert_eq!(fs.access(b"/a\0b", Access::READ), Err(PyErr::Value));
            // A final link is ELOOP on POSIX; Windows follows.
            let chained = fs.open_no_follow(b"/chain100");
            match flavour {
                Flavour::Posix => assert_eq!(chained.err().and_then(|e| e.errno), Some("ELOOP")),
                Flavour::Windows => assert!(chained.is_ok()),
            }
        }
    });
}

/// A FIFO with no writer, opened without following: the open must answer
/// at once (O_NONBLOCK). Should it wait, a helper opens the FIFO for
/// writing, which releases it, and the test fails; passing never waits on
/// the clock (the helper is only a rescue).
#[cfg(unix)]
#[test]
fn a_fifo_opened_without_following_does_not_wait() {
    use bettermemory_core::fsdata::StdFs;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::mpsc;
    static NEXT: AtomicUsize = AtomicUsize::new(0);
    let dir = std::env::temp_dir().join(format!(
        "bm-fsaccess-fifo-{}-{}",
        std::process::id(),
        NEXT.fetch_add(1, Ordering::Relaxed)
    ));
    std::fs::create_dir(&dir).expect("a new scratch directory");
    let fifo = dir.join("p");
    let made = std::process::Command::new("mkfifo")
        .arg(&fifo)
        .status()
        .is_ok_and(|s| s.success());
    assert!(made, "mkfifo");
    let (done, opened) = mpsc::channel::<()>();
    let rescue_path = fifo.clone();
    let rescue = std::thread::spawn(move || {
        if opened
            .recv_timeout(std::time::Duration::from_secs(60))
            .is_ok()
        {
            return false;
        }
        // The open waited: release it with a writer.
        let _ = std::fs::OpenOptions::new().write(true).open(&rescue_path);
        true
    });
    let path = fifo.to_str().unwrap().as_bytes().to_vec();
    let answer = StdFs
        .open_no_follow(&path)
        .map(|mut f| StdFs.fstat(&mut f).map(|s| s.kind));
    let _ = done.send(());
    let rescued = rescue.join().unwrap();
    let _ = std::fs::remove_dir_all(&dir);
    assert!(!rescued, "the open waited for a writer");
    assert_eq!(answer.ok().and_then(Result::ok), Some(Kind::Fifo));
}
