//! `gitsystem.sources`, `reads_compat` and `is_at_least`, with the held
//! answers (`_HELD`).
//!
//! The reference holds the answers of up to eight PATHs in a module dict
//! under a lock, cleared by `_caches`. Here they are [`Held`], a cache the
//! caller owns and hands in (as `StampedReads` is): no static, no lock.
//! Clearing it is [`Held::clear`].
//!
//! Everything the reference reads of the process comes in through
//! [`Caller`]: the environment (PATH and DEVELOPER_DIR), whether it runs on
//! macOS (`_MACOS`), the link `_developer` reads, the clocks, git, the
//! default path and `git_read_budget`'s active budget.

use super::ask::{Asker, Budget, Clock, Declined, Env, Git};
use super::record::{developer, key, Answer, Natural, Place, Refused, Sources, KEPT};
use crate::fsdata::{Flavour, Fs};
use crate::pyvalue::PyStr;

/// `_HELD`: the answer of each PATH this process asked or read, oldest
/// first (a dict's insertion order), at most [`KEPT`].
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Held {
    entries: Vec<(String, Answer)>,
}

impl Held {
    pub fn new() -> Held {
        Held::default()
    }

    /// `_clear()` (`_caches.clear_all`).
    pub fn clear(&mut self) {
        self.entries.clear();
    }

    /// `_HELD.get(key)`.
    pub fn get(&self, key: &str) -> Option<&Answer> {
        self.entries.iter().find(|(k, _)| k == key).map(|(_, a)| a)
    }

    /// The held answers, oldest first.
    pub fn entries(&self) -> &[(String, Answer)] {
        &self.entries
    }

    /// `_HELD.pop(key, None); _HELD[key] = answer`, then the oldest
    /// dropped while more than [`KEPT`] are held.
    fn put(&mut self, key: String, answer: Answer) {
        self.entries.retain(|(k, _)| *k != key);
        self.entries.push((key, answer));
        let over = self.entries.len().saturating_sub(KEPT);
        self.entries.drain(..over);
    }
}

/// Why `sources` gives no answer here.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Fault {
    /// From the record: the reference raises, or the stamp is not ported.
    Refused(Refused),
    /// From the ask: the reference raises, or it is not ported.
    Declined(Declined),
}

impl From<Refused> for Fault {
    fn from(r: Refused) -> Fault {
        Fault::Refused(r)
    }
}

impl From<Declined> for Fault {
    fn from(d: Declined) -> Fault {
        Fault::Declined(d)
    }
}

/// What the caller hands in for one call.
pub struct Caller<'a, 'p, F: Fs, G: Git, C: Clock> {
    pub place: &'a Place<'p, F>,
    pub git: &'a mut G,
    pub clock: &'a mut C,
    /// `os.environ`, in its order.
    pub env: &'a Env,
    /// `os.confstr("CS_PATH")` (or `os.defpath`), for `shutil.which`.
    pub cs_path: Option<&'a [u32]>,
    pub budget: &'a mut Budget,
    /// `_MACOS` (`sys.platform == "darwin"`).
    pub macos: bool,
    /// `XCODE_SELECT_LINK`.
    pub link: &'a [u8],
}

/// `os.environ.get(name)`: POSIX names match exactly; under Windows
/// `os.environ` upper-cases its keys.
fn get<'e>(flavour: Flavour, env: &'e Env, name: &str) -> Option<&'e PyStr> {
    let want: PyStr = name.chars().map(u32::from).collect();
    match flavour {
        Flavour::Posix => env.iter().find(|(k, _)| *k == want).map(|(_, v)| v),
        Flavour::Windows => env
            .iter()
            .rev()
            .find(|(k, _)| crate::unicode::upper(k) == want)
            .map(|(_, v)| v),
    }
}

impl<F: Fs, G: Git, C: Clock> Caller<'_, '_, F, G, C> {
    /// `_key(os.environ.get("PATH"))`.
    pub fn key(&self) -> String {
        let flavour = self.place.flavour;
        let named = get(flavour, self.env, "DEVELOPER_DIR");
        let path = get(flavour, self.env, "PATH");
        let dev = developer(self.place.fs, self.macos, named.map(|v| &v[..]), self.link);
        key(path.map(|v| &v[..]), &dev)
    }

    /// `sources(stamp=stamp)`.
    pub fn sources(&mut self, held: &mut Held, stamp: bool) -> Result<Sources, Fault> {
        let key = self.key();
        let now = self.clock.wall();
        let place = self.place;
        let seen = held.get(&key).cloned();
        if let Some(h) = &seen {
            if place.current(h, now, stamp)? {
                return Ok(h.sources.clone());
            }
        }
        let recorded = place.recorded(&key)?;
        let got = match &recorded {
            Some(r) if place.current(r, now, true)? => r.clone(),
            _ => {
                let asked = Asker {
                    place,
                    git: &mut *self.git,
                    clock: &mut *self.clock,
                    env: self.env,
                    cs_path: self.cs_path,
                    budget: &mut *self.budget,
                }
                .ask(&key, now)?;
                let got = if asked.ok {
                    asked
                } else {
                    place
                        .kept(&key, now, &[seen.as_ref(), recorded.as_ref()])?
                        .unwrap_or(asked)
                };
                // Err only where the reference raises (a record past what
                // `json` handles); a refused write is `Recorded::Refused`.
                place.record(&got)?;
                got
            }
        };
        let out = got.sources.clone();
        held.put(key, got);
        Ok(out)
    }

    /// `reads_compat()`.
    pub fn reads_compat(&mut self, held: &mut Held) -> Result<Option<bool>, Fault> {
        Ok(self.sources(held, true)?.compat_differ)
    }

    /// `is_at_least(release)`.
    pub fn is_at_least(
        &mut self,
        held: &mut Held,
        release: (u64, u64),
    ) -> Result<Option<bool>, Fault> {
        Ok(self
            .sources(held, true)?
            .version
            .map(|v| at_least(&v, release)))
    }
}

/// `found[:2] >= release`, Python's tuple order over ints of any size.
pub fn at_least(found: &[Natural; 3], release: (u64, u64)) -> bool {
    use std::cmp::Ordering;
    let cmp = |n: &Natural, r: u64| {
        let r = r.to_string();
        let a = n.as_str();
        a.len().cmp(&r.len()).then_with(|| a.cmp(r.as_str()))
    };
    match cmp(&found[0], release.0) {
        Ordering::Greater => true,
        Ordering::Less => false,
        Ordering::Equal => cmp(&found[1], release.1) != Ordering::Less,
    }
}
