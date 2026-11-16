//! Hostile config texts: the reader never panics, ends in time linear in
//! its input, and answers as the reference does where `gitconfig.jsonl`
//! pins the shape (only NULs, only 0xFF bytes, an odd run of quotes and
//! an even one, a value of escapes, a long subsection over many keys).

use std::time::{Duration, Instant};

use bettermemory_core::gitconfig::{git_bool, git_entries, git_int, Refused};

fn timed<T>(f: impl FnOnce() -> T) -> (T, Duration) {
    let start = Instant::now();
    let out = f();
    (out, start.elapsed())
}

fn with(head: &[u8], piece: &[u8], n: usize, tail: &[u8]) -> Vec<u8> {
    let mut out = Vec::with_capacity(head.len() + piece.len() * n + tail.len());
    out.extend_from_slice(head);
    for _ in 0..n {
        out.extend_from_slice(piece);
    }
    out.extend_from_slice(tail);
    out
}

#[test]
fn a_50_mb_file_is_read_whole() {
    let data = with(b"", b"[s]\nk = v\n", 5 << 20, b"");
    let entries = git_entries(&data).expect("read");
    assert_eq!(entries.len(), 5 << 20);
    assert_eq!(entries[0].name(), b"s.k");
}

#[test]
fn a_10_mb_line_is_one_value() {
    let data = with(b"[s]\nk = ", b"x", 10 << 20, b"");
    let entries = git_entries(&data).expect("read");
    assert_eq!(entries.len(), 1);
    assert_eq!(entries[0].value.as_ref().map(Vec::len), Some(10 << 20));
    let quoted = with(b"[s]\nk = \"", b"x", 10 << 20, b"");
    assert_eq!(git_entries(&quoted), Err(Refused));
}

#[test]
fn deep_quoting_ends() {
    let odd = with(b"[s]\nk = ", b"\"", (1 << 22) + 1, b"\n");
    assert_eq!(git_entries(&odd), Err(Refused));
    let even = with(b"[s]\nk = ", b"\"", 1 << 22, b"\n");
    let entries = git_entries(&even).expect("read");
    assert_eq!(entries[0].value.as_deref(), Some(&b""[..]));
}

#[test]
fn only_escapes_and_only_backslashes() {
    let tabs = with(b"[s]\nk = ", b"\\t", 1 << 21, b"\n");
    let entries = git_entries(&tabs).expect("read");
    assert_eq!(entries[0].value.as_ref().map(Vec::len), Some(1 << 21));
    assert_eq!(git_entries(&with(b"", b"\\", 1 << 22, b"")), Err(Refused));
    let joined = with(b"[s]\nk = a", b"\\\n", 1 << 20, b"b\n");
    let entries = git_entries(&joined).expect("read");
    assert_eq!(entries[0].value.as_deref(), Some(&b"ab"[..]));
}

#[test]
fn nuls_and_bytes_that_are_not_utf8() {
    assert_eq!(git_entries(&vec![0u8; 10 << 20]), Err(Refused));
    assert_eq!(git_entries(&vec![0xffu8; 10 << 20]), Err(Refused));
    let value = with(b"[s]\nk = ", b"\xff\xfe\x00", 1 << 20, b"\n");
    assert_eq!(git_entries(&value).expect("read").len(), 1);
    let sub = with(b"[s \"", b"\xff", 1 << 20, b"\"]\nk\n");
    let entries = git_entries(&sub).expect("read");
    assert_eq!(
        entries[0].spelled().subsection.map(|s| s.len()),
        Some(1 << 20)
    );
    assert_eq!(git_int(Some(&vec![b'9'; 1 << 20])), None);
    assert_eq!(git_bool(Some(&vec![0xff; 1 << 20])), None);
}

#[test]
fn a_long_subsection_over_many_keys_is_kept_once() {
    let mut data = with(b"[s \"", b"y", 4 << 20, b"\"]\n");
    data.extend(with(b"", b"k\n", 1 << 20, b""));
    let (entries, took) = timed(|| git_entries(&data).expect("read"));
    assert_eq!(entries.len(), 1 << 20);
    assert!(took < Duration::from_secs(20), "{took:?}");
}

/// Ten times the input takes at most 30 times as long (plus 50 ms), for
/// each shape whose cost might grow faster: many headers, many keys, a
/// long value of quotes and escapes, a long comment, a long subsection.
/// Linear growth is 10 times and quadratic 100 times, so the margin is
/// three times linear and a third of quadratic; each time is the best of
/// five runs, and the inputs (100k and 1M pieces) are large enough that
/// the small one takes milliseconds even in an optimized build.
#[test]
fn texts_read_in_linear_time() {
    let shapes: [(&[u8], &[u8], &[u8]); 6] = [
        (b"", b"[a \"b\"]\n", b""),
        (b"[s]\n", b"k\n", b""),
        (b"[s]\nk = ", b"\"a\\t\" ", b"\n"),
        (b"[s]\nk = v #", b"x", b"\n"),
        (b"[s \"", b"\\y", b"\"]\nk\n"),
        (b"", b" \t\r\n;c\n", b"k\n"),
    ];
    for (head, piece, tail) in shapes {
        let small = with(head, piece, 100_000, tail);
        let large = with(head, piece, 1_000_000, tail);
        let best = |data: &[u8]| {
            (0..5)
                .map(|_| timed(|| git_entries(data).map(|e| e.len())).1)
                .min()
                .expect("timed")
        };
        let (s, l) = (best(&small), best(&large));
        let bound = s * 30 + Duration::from_millis(50);
        assert!(l <= bound, "{piece:?}: 1M {l:?}, 100k {s:?}");
    }
}
