//! The location and whole-history reads of one `_response._DriftPass`.
//! The request owns both the pass and the Git scope used by its readers.

use crate::git_read_budget::{unread, Raise, Scope};
use crate::origin_dates::memo::Timestamps;
use crate::origin_history::{HistoryUnread, Key};
use crate::pyvalue::PyStr;

/// The result of a read outside a deadline-catching decorator.
pub type Read<T, E> = Result<T, Raise<E>>;

/// The origin readers and the runner's counter sink for the request.
/// Paths are Python code points, with the spelling of the caller's Path.
/// Every effect receives the budget and root active at its call site.
pub trait HistoryReads {
    type Error;

    /// `origin.toplevel_and_head_from_files(cwd)` when the pass opens.
    fn files_pair(&mut self, scope: &mut Scope, cwd: &[u32]) -> Read<Option<Key>, Self::Error>;
    /// `origin.repo_toplevel_and_head(cwd)` on the first fallback read.
    fn git_pair(&mut self, scope: &mut Scope, cwd: &[u32]) -> Read<Option<Key>, Self::Error>;
    /// `githead::discovery::discovery_moved` with the current environment.
    fn discovery_moved(&mut self, scope: &mut Scope) -> Read<bool, Self::Error>;
    /// `origin_gitfiles::walk::located_head` at the discovered root.
    fn located_head(&mut self, scope: &mut Scope, root: &[u32])
        -> Read<Option<PyStr>, Self::Error>;
    /// `origin_dates::memo::commit_author_timestamps` with the original
    /// cwd and the pass's explicit pair. A returned unread is cacheable.
    fn dates(
        &mut self,
        scope: &mut Scope,
        cwd: &[u32],
        located: Option<&Key>,
    ) -> Read<Timestamps, Self::Error>;
    /// `origin_history::note_unread` applied to the original runner.
    fn note_unread(&mut self, scope: &mut Scope, why: HistoryUnread) -> Read<(), Self::Error>;
}

/// Location, key eligibility and lazy dates shared by all rows in a pass.
pub struct HistoryPass {
    cwd: PyStr,
    at_home: bool,
    settled: Option<Key>,
    located: Option<Key>,
    located_read: bool,
    keyed: Option<Key>,
    keyed_read: bool,
    history_head: Option<PyStr>,
    history_read: bool,
    timestamps: Timestamps,
}

impl HistoryPass {
    /// Open the pass. The files are read now, even for a pass with no rows.
    pub fn open<R: HistoryReads + ?Sized>(
        reads: &mut R,
        scope: &mut Scope,
        cwd: PyStr,
        at_home: bool,
    ) -> Read<Self, R::Error> {
        let settled = reads.files_pair(scope, &cwd)?;
        Ok(Self {
            cwd,
            at_home,
            keyed_read: settled.is_some(),
            keyed: settled.clone(),
            settled,
            located: None,
            located_read: false,
            history_head: None,
            history_read: false,
            timestamps: Timestamps::Absent,
        })
    }

    /// Whether files supplied the pass's root and head (`_owned`).
    pub fn owned(&self) -> bool {
        self.settled.is_some()
    }

    /// The caller's directory, `str(self.cwd)`.
    pub fn cwd(&self) -> &[u32] {
        &self.cwd
    }

    /// Whether the pass is at a record's home root (`at_home`).
    pub fn at_home(&self) -> bool {
        self.at_home
    }

    /// The pair the rows key on as last decided (`_keyed`), with no read
    /// and no check of the history's head.
    pub fn keyed(&self) -> Option<&Key> {
        self.keyed.as_ref()
    }

    /// Whether an attempted history read named this head (`_history_at`).
    pub fn history_at(&self, head: &[u32]) -> bool {
        !self.history_read || self.history_head.as_deref() == Some(head)
    }

    /// Files' pair, else the first completed Git lookup, including no pair.
    pub fn located<R: HistoryReads + ?Sized>(
        &mut self,
        reads: &mut R,
        scope: &mut Scope,
    ) -> Read<Option<&Key>, R::Error> {
        if !self.located_read {
            self.located = match &self.settled {
                Some(pair) => Some(pair.clone()),
                None => reads.git_pair(scope, &self.cwd)?,
            };
            self.located_read = true;
        }
        Ok(self.located.as_ref())
    }

    /// The pair eligible to key a row. A failed probe consumes this check.
    pub fn keyed_pair<R: HistoryReads + ?Sized>(
        &mut self,
        reads: &mut R,
        scope: &mut Scope,
        resolve: bool,
    ) -> Read<Option<&Key>, R::Error> {
        if !self.keyed_read && (resolve || self.located_read) {
            self.keyed_read = true;
            if let Some(pair) = self.located(reads, scope)? {
                if !reads.discovery_moved(scope)?
                    && reads.located_head(scope, &pair.0)?.as_ref() == Some(&pair.1)
                {
                    self.keyed = Some(pair.clone());
                }
            }
        }
        Ok(self.keyed.as_ref().filter(|pair| self.history_at(&pair.1)))
    }

    /// Read the whole history under the pass's budget root. A missing
    /// history is retried. An unread result is counted on each later hit.
    pub fn timestamps<R: HistoryReads + ?Sized>(
        &mut self,
        reads: &mut R,
        scope: &mut Scope,
    ) -> Result<Timestamps, R::Error> {
        unread(scope, Some((self.cwd.clone(), self.at_home)), |scope| {
            match self.timestamps {
                Timestamps::Absent => {
                    self.located(reads, scope)?;
                    self.history_read = true;
                    self.history_head = self.located.as_ref().map(|pair| pair.1.clone());
                    self.timestamps = reads.dates(scope, &self.cwd, self.located.as_ref())?;
                }
                Timestamps::Unread(why) => reads.note_unread(scope, why)?,
                Timestamps::Dates(_) => {}
            }
            Ok(self.timestamps.clone())
        })
    }

    /// A previous read settles this question, then a live location. Only
    /// a pass without either needs the whole-history reader.
    pub fn history_applies<R: HistoryReads + ?Sized>(
        &mut self,
        reads: &mut R,
        scope: &mut Scope,
    ) -> Read<bool, R::Error> {
        if self.history_read {
            return Ok(!matches!(self.timestamps, Timestamps::Absent));
        }
        if self.located(reads, scope)?.is_some() {
            return Ok(true);
        }
        let dates = self.timestamps(reads, scope).map_err(Raise::Other)?;
        Ok(!matches!(dates, Timestamps::Absent))
    }
}

/// Bind the byte-path origin readers on a POSIX host. The origin pair
/// readers remain supplied by the caller. Other hosts can implement
/// [`HistoryReads`] directly using their native path representation.
#[cfg(unix)]
pub mod posix {
    use super::*;
    use crate::fsdata::Fs;
    use crate::gitfile_rule::bounded::Answers;
    use crate::gitfile_rule::discovery::Rule;
    use crate::gitfile_rule::follow::{fsencode, Records};
    use crate::githead::discovery::{discovery_moved, Env};
    use crate::origin_dates::memo::{commit_author_timestamps, Reader, ShallowHold};
    use crate::origin_gitfiles::walk::located_head;
    use crate::origin_history::{note_unread, RunnerNote, ValueError};

    /// The byte-path producers support filesystem-encodable POSIX text.
    /// A caller can supply its own reader for any other path representation.
    #[derive(Clone, Debug, PartialEq, Eq)]
    pub struct UnsupportedPath(pub PyStr);

    /// An origin pair read that keeps the caller's original error.
    pub type PairRead<'a, E> = dyn FnMut(&mut Scope, &[u32]) -> Read<Option<Key>, E> + 'a;

    /// Git environment is read when checked, not frozen when the pass opens.
    pub struct OriginReads<
        'a,
        F: Fs,
        A: Answers + ?Sized,
        R: Records + ?Sized,
        G,
        H: ShallowHold + ?Sized,
        E,
    > {
        pub files_pair: &'a mut PairRead<'a, E>,
        pub git_pair: &'a mut PairRead<'a, E>,
        pub env: &'a dyn Fn() -> Env,
        pub rule: &'a Rule<'a, F, A, R>,
        pub reader: Reader<'a, G, H>,
    }

    impl<F, A, R, G, H, E> HistoryReads for OriginReads<'_, F, A, R, G, H, E>
    where
        F: Fs,
        A: Answers + ?Sized,
        R: Records + ?Sized,
        G: PartialEq + Clone,
        H: ShallowHold + ?Sized,
        E: From<ValueError> + From<UnsupportedPath>,
    {
        type Error = E;
        fn files_pair(&mut self, scope: &mut Scope, cwd: &[u32]) -> Read<Option<Key>, E> {
            (self.files_pair)(scope, cwd)
        }
        fn git_pair(&mut self, scope: &mut Scope, cwd: &[u32]) -> Read<Option<Key>, E> {
            (self.git_pair)(scope, cwd)
        }
        fn discovery_moved(&mut self, _: &mut Scope) -> Read<bool, E> {
            Ok(discovery_moved(&(self.env)()))
        }
        fn located_head(&mut self, _: &mut Scope, root: &[u32]) -> Read<Option<PyStr>, E> {
            // githead._locate catches the ValueError from path encoding.
            let Some(root) = fsencode(root) else {
                return Ok(None);
            };
            Ok(located_head(self.rule, &(self.env)(), &root)
                .map(|s| s.chars().map(u32::from).collect()))
        }
        fn dates(
            &mut self,
            scope: &mut Scope,
            cwd: &[u32],
            located: Option<&Key>,
        ) -> Read<Timestamps, E> {
            let path =
                fsencode(cwd).ok_or_else(|| Raise::Other(UnsupportedPath(cwd.to_vec()).into()))?;
            commit_author_timestamps(scope, &mut self.reader, Some(&path), located.cloned())
                .map_err(|e| Raise::Other(e.into()))
        }
        fn note_unread(&mut self, _: &mut Scope, why: HistoryUnread) -> Read<(), E> {
            match note_unread(why) {
                RunnerNote::TimedOut => self.reader.git.note_timed_out(),
                RunnerNote::Failed => self.reader.git.note_failed(),
            }
            Ok(())
        }
    }
}
