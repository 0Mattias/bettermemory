//! `transcript.py`: a Claude Code transcript read from its end, for the
//! hooks. Unit transcript-1a: [`ReverseLines`] (the reader), `_row`,
//! `_prompt_id` and [`stop_turn`] with [`StopTurn`]. Unit transcript-1b:
//! [`interrupted_turns`] with [`InterruptedTurn`], [`owner_words`] and
//! [`compact_marker`] with [`CompactMarker`]. The file is reached
//! through [`Opener`] and the definition of the owner's words through
//! [`OwnerPrompt`], so neither is an ambient call.

pub mod reverse;
pub mod stop;
pub mod turns;

pub use reverse::{FsOpener, ReverseLines};
pub use stop::{prompt_id, row, stop_turn, StopTurn};
pub use turns::{compact_marker, interrupted_turns, owner_words, CompactMarker, InterruptedTurn};

use crate::pyvalue::{PyStr, PyValue};

/// `CHUNK_BYTES`: the bytes one read takes.
pub const CHUNK_BYTES: usize = 1_048_576;

/// `WALK_BYTES`: the most bytes a walk covers, counted back from where the
/// file ended when it was opened.
pub const WALK_BYTES: i64 = 64 * 1_048_576;

/// `owner_prompt.INTERRUPT_OPENING`.
pub const INTERRUPT_OPENING: &str = "[Request interrupted";

/// An OSError of the reader: which call raised does not matter, since
/// `ReverseLines` ends its iteration on any.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct OsFailure;

/// The file a [`ReverseLines`] reads: `Path.open("rb")` and the calls it
/// makes on the handle.
pub trait Opener {
    type File;
    /// `path.open("rb")`: a folder is an OSError (`IsADirectoryError`).
    fn open(&self, path: &[u8]) -> Result<Self::File, OsFailure>;
    /// `os.fstat(handle.fileno()).st_size`.
    fn size(&self, file: &mut Self::File) -> Result<u64, OsFailure>;
    /// `handle.seek(offset)`.
    fn seek(&self, file: &mut Self::File, offset: u64) -> Result<(), OsFailure>;
    /// `handle.read(n)` of a buffered binary file: `n` bytes, fewer only at
    /// the end.
    fn read(&self, file: &mut Self::File, n: usize) -> Result<Vec<u8>, OsFailure>;
    /// `path.is_file()`: a regular file, links followed; False on any
    /// error. Required, with no default, so an opener cannot answer
    /// False by omission and make `owner_words` read no file.
    fn is_file(&self, path: &[u8]) -> bool;
}

/// The calls `transcript.py` makes into `owner_prompt.py`. The module that
/// ports `owner_prompt.py` implements it; a test answers as the reference
/// did.
pub trait OwnerPrompt {
    /// `is_submitted(row)`.
    fn is_submitted(&self, row: &PyValue) -> bool;
    /// `owner_prompt(row, cut=cut)`; the reference's default is
    /// `cut=True`.
    fn owner_prompt(&self, row: &PyValue, cut: bool) -> Option<PyStr>;
    /// `submitted_text(row)`.
    fn submitted_text(&self, row: &PyValue) -> Option<PyStr>;
    /// `text_blocks(content)`; an absent content is `PyValue::None`, as
    /// `dict.get` gives.
    fn text_blocks(&self, content: &PyValue) -> Option<PyStr>;
}
