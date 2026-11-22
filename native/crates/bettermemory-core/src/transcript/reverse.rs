//! `ReverseLines` (`transcript.py` 58-130): the complete lines of a file,
//! newest first, as far back as a budget from where the file ended when it
//! was opened.

use std::num::NonZeroUsize;

use super::{Opener, OsFailure};
use crate::fsdata::{Fs, Kind};

/// [`Opener`] over the crate's file system: `open_waiting` (Python's
/// `open` sets no `O_NONBLOCK`), `fstat`, `seek` and `read`. A folder is
/// refused after the open, as `io.FileIO` refuses it (EISDIR); a file
/// system that does not offer `seek` declines as an OSError would end the
/// walk.
pub struct FsOpener<'a, F: Fs + ?Sized>(pub &'a F);

impl<F: Fs + ?Sized> Opener for FsOpener<'_, F> {
    type File = F::File;

    fn open(&self, path: &[u8]) -> Result<F::File, OsFailure> {
        let mut file = self.0.open_waiting(path).map_err(|_| OsFailure)?;
        match self.0.fstat(&mut file) {
            Ok(stat) if stat.kind == Kind::Directory => Err(OsFailure),
            Ok(_) => Ok(file),
            Err(_) => Err(OsFailure),
        }
    }

    fn size(&self, file: &mut F::File) -> Result<u64, OsFailure> {
        self.0.fstat(file).map(|s| s.size).map_err(|_| OsFailure)
    }

    fn seek(&self, file: &mut F::File, offset: u64) -> Result<(), OsFailure> {
        self.0.seek(file, offset).map_err(|_| OsFailure)
    }

    fn read(&self, file: &mut F::File, n: usize) -> Result<Vec<u8>, OsFailure> {
        self.0.read(file, n).map_err(|_| OsFailure)
    }
}

enum State<H> {
    Unopened,
    Walking {
        file: H,
        end: u64,
        floor: u64,
        /// The bytes of the line that runs across `end`; None when a line
        /// starts at `end`.
        carry: Option<Vec<u8>>,
    },
    Done,
}

/// `ReverseLines(path, chunk_bytes=..., budget=..., needle=...)`, iterated
/// once. The file is opened at the first `next`, as the reference's
/// generator opens it. `chunk_bytes` is nonzero: the reference with 0
/// never ends (README, reference findings).
pub struct ReverseLines<'a, O: Opener> {
    opener: &'a O,
    path: Vec<u8>,
    chunk_bytes: usize,
    budget: i64,
    needle: Option<Vec<u8>>,
    /// Whether the iteration, once over, reached the file's first byte.
    pub reached_start: bool,
    state: State<O::File>,
    /// The lines of the chunk being yielded, oldest first.
    queue: Vec<Vec<u8>>,
}

impl<'a, O: Opener> ReverseLines<'a, O> {
    pub fn new(
        opener: &'a O,
        path: &[u8],
        chunk_bytes: NonZeroUsize,
        budget: i64,
        needle: Option<&[u8]>,
    ) -> ReverseLines<'a, O> {
        ReverseLines {
            opener,
            path: path.to_vec(),
            chunk_bytes: chunk_bytes.get(),
            budget,
            needle: needle.map(<[u8]>::to_vec),
            reached_start: false,
            state: State::Unopened,
            queue: Vec::new(),
        }
    }

    fn open(&mut self) -> Result<(), OsFailure> {
        let mut file = self.opener.open(&self.path)?;
        let end = self.opener.size(&mut file)?;
        let floor = (i128::from(end) - i128::from(self.budget)).max(0);
        let floor = u64::try_from(floor).unwrap_or(u64::MAX);
        self.state = State::Walking {
            file,
            end,
            floor,
            carry: Some(Vec::new()),
        };
        Ok(())
    }

    /// One chunk: its lines queued, a carry kept, or the walk ended.
    fn step(&mut self) -> Result<(), OsFailure> {
        let State::Walking {
            file,
            end,
            floor,
            carry,
        } = &mut self.state
        else {
            return Ok(());
        };
        if *end <= *floor {
            self.reached_start = *end == 0;
            self.state = State::Done;
            return Ok(());
        }
        let chunk = u64::try_from(self.chunk_bytes).unwrap_or(u64::MAX);
        let start = (*floor).max(end.saturating_sub(chunk));
        let lead = u64::from(start > 0);
        self.opener.seek(file, start - lead)?;
        let want = usize::try_from(*end - start + lead).map_err(|_| OsFailure)?;
        let mut data = self.opener.read(file, want)?;
        if data.len() != want {
            return Err(OsFailure); // truncated under the read: the walk ends
        }
        let buf = match carry.take() {
            Some(c) => {
                data.extend_from_slice(&c);
                data
            }
            None => {
                data.pop();
                data
            }
        };
        *end = start;
        let lines = if lead == 0 {
            buf
        } else if buf.first() == Some(&b'\n') {
            buf[1..].to_vec()
        } else {
            match buf.iter().position(|&b| b == b'\n') {
                None => {
                    *carry = Some(buf[1..].to_vec());
                    return Ok(());
                }
                Some(nl) => {
                    *carry = Some(buf[1..nl].to_vec());
                    buf[nl + 1..].to_vec()
                }
            }
        };
        if let Some(needle) = &self.needle {
            if !contains(&lines, needle) {
                return Ok(());
            }
        }
        self.queue = lines.split(|&b| b == b'\n').map(<[u8]>::to_vec).collect();
        Ok(())
    }
}

/// `needle in haystack` for bytes.
/// Substring test in linear time (Knuth-Morris-Pratt), as the reference's
/// `in` is linear on bytes; a naive window scan is O(n*m).
fn contains(haystack: &[u8], needle: &[u8]) -> bool {
    if needle.is_empty() {
        return true;
    }
    if needle.len() > haystack.len() {
        return false;
    }
    let mut fail = vec![0usize; needle.len()];
    let mut k = 0;
    for i in 1..needle.len() {
        while k > 0 && needle[i] != needle[k] {
            k = fail[k - 1];
        }
        if needle[i] == needle[k] {
            k += 1;
        }
        fail[i] = k;
    }
    let mut k = 0;
    for &b in haystack {
        while k > 0 && b != needle[k] {
            k = fail[k - 1];
        }
        if b == needle[k] {
            k += 1;
            if k == needle.len() {
                return true;
            }
        }
    }
    false
}

impl<O: Opener> Iterator for ReverseLines<'_, O> {
    type Item = Vec<u8>;

    fn next(&mut self) -> Option<Vec<u8>> {
        loop {
            if let Some(line) = self.queue.pop() {
                return Some(line);
            }
            let outcome = match self.state {
                State::Done => return None,
                State::Unopened => self.open(),
                State::Walking { .. } => self.step(),
            };
            if outcome.is_err() {
                self.state = State::Done;
                return None;
            }
        }
    }
}
