//! Hostile input for `fsutil`: caps at the integer bounds, a stream far
//! larger than its cap, an endless stream, a large read that grows in
//! proportion, and a remover that fails for ever.

mod timing;

use std::io::Read;

use bettermemory_core::fsutil::{
    bounded_stream_read, unlink_retrying_by, Remover, StreamReadError,
};

struct Endless;

impl Read for Endless {
    fn read(&mut self, buf: &mut [u8]) -> std::io::Result<usize> {
        buf.fill(7);
        Ok(buf.len())
    }
}

#[test]
fn integer_bounds() {
    assert!(matches!(
        bounded_stream_read(&mut Endless, i64::MAX),
        Err(StreamReadError::Overflow)
    ));
    assert!(matches!(
        bounded_stream_read(&mut Endless, i64::MIN),
        Err(StreamReadError::BadLength)
    ));
    // A huge cap over a short stream: nothing is allocated up front. The
    // reference's `BufferedReader.read` allocates the whole cap first and
    // raises MemoryError here (2**60 under CPython 3.13 on macOS); the
    // port's answer is the stated difference of the README.
    let got = bounded_stream_read(&mut std::io::Cursor::new(vec![1u8; 3]), i64::MAX - 1).unwrap();
    assert_eq!(got, [1, 1, 1]);
}

#[test]
fn endless_stream_stops_one_past_the_cap() {
    let err = bounded_stream_read(&mut Endless, 1 << 20).unwrap_err();
    assert_eq!(
        err.message().unwrap(),
        "stream input exceeds cap 1048576 bytes"
    );
}

#[test]
fn read_grows_linearly() {
    let small = vec![3u8; 1 << 18];
    let large = vec![3u8; 10 << 18];
    timing::grows_within(
        "bounded_stream_read",
        30,
        &mut || {
            let mut c = std::io::Cursor::new(&small);
            timing::once(|| bounded_stream_read(&mut c, 1 << 30).unwrap())
        },
        &mut || {
            let mut c = std::io::Cursor::new(&large);
            timing::once(|| bounded_stream_read(&mut c, 1 << 30).unwrap())
        },
    );
}

struct Forever(u32, f64);

impl Remover for Forever {
    type Error = ();
    fn unlink(&mut self) -> Result<(), ()> {
        self.0 += 1;
        Err(())
    }
    fn permission(&self, _: &()) -> bool {
        true
    }
    fn sleep(&mut self, seconds: f64) {
        self.1 += seconds;
    }
}

#[test]
fn failing_remover_is_bounded() {
    let mut r = Forever(0, 0.0);
    let mut still = || true;
    assert_eq!(unlink_retrying_by(&mut r, true, Some(&mut still)), Err(()));
    assert_eq!(r.0, 5);
    assert!((r.1 - 0.15).abs() < 1e-12);
}
