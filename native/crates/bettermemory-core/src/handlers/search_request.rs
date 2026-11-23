//! Request arguments and per-request record hydration from `handlers/search.py`.

pub mod width;

use crate::pyvalue::PyStr;
use crate::records::Memory;
use crate::search::Record;
use crate::session::OrderedMap;
use crate::store::record_reads::{get_many_python_ids, ReadError};
use rusqlite::Connection;
use std::borrow::Cow;
use std::sync::Arc;

/// The ID used to replace a ranking candidate with its full signed record.
pub trait RecordId<E> {
    fn record_id(&self) -> Result<Cow<'_, [u32]>, E>;
}

impl<E> RecordId<E> for Memory {
    fn record_id(&self) -> Result<Cow<'_, [u32]>, E> {
        Ok(Cow::Borrowed(&self.fields.id))
    }
}

impl<E> RecordId<E> for Record {
    fn record_id(&self) -> Result<Cow<'_, [u32]>, E> {
        Ok(Cow::Owned(crate::unicode::cps(&self.id)))
    }
}

impl<E, T: RecordId<E>> RecordId<E> for &T {
    fn record_id(&self) -> Result<Cow<'_, [u32]>, E> {
        (**self).record_id()
    }
}

/// A loader may yield records before failing. The ordinary Store collects
/// its entire SQL result first, so a failed query yields no records. Propagate
/// each emitter error before advancing the source to the next record.
pub trait LoadMany<F> {
    type Error;
    fn load_many(
        &mut self,
        ids: &[PyStr],
        emit: &mut dyn FnMut(Arc<F>) -> Result<(), Self::Error>,
    ) -> Result<(), Self::Error>;
}

impl LoadMany<Memory> for &Connection {
    type Error = ReadError;

    fn load_many(
        &mut self,
        ids: &[PyStr],
        emit: &mut dyn FnMut(Arc<Memory>) -> Result<(), ReadError>,
    ) -> Result<(), ReadError> {
        for memory in get_many_python_ids(self, ids)? {
            emit(Arc::new(memory))?;
        }
        Ok(())
    }
}

/// A missing row keeps the original candidate. A present row keeps its full
/// signed fields even when the caller supplied only a ranking `Record`.
#[derive(Debug)]
pub enum Hydrated<'a, L, F> {
    Original(&'a L),
    Full(Arc<F>),
}

impl<T> Hydrated<'_, T, T> {
    pub fn record(&self) -> &T {
        match self {
            Self::Original(light) => light,
            Self::Full(full) => full,
        }
    }
}

/// One search's Python `Hydrator`. Reassigning an ID preserves its
/// dictionary position and replaces the full record for all later lookups.
pub struct Hydrator<F, S> {
    store: S,
    pub full: OrderedMap<PyStr, Arc<F>>,
}

impl<S: LoadMany<F>, F: RecordId<S::Error>> Hydrator<F, S> {
    pub fn new(store: S) -> Self {
        Self {
            store,
            full: OrderedMap::default(),
        }
    }

    pub fn hydrate<'a, L: RecordId<S::Error>>(
        &mut self,
        memories: &'a [L],
    ) -> Result<Vec<Hydrated<'a, L, F>>, S::Error> {
        let mut missing = Vec::new();
        for memory in memories {
            if self
                .full
                .get_borrowed(memory.record_id()?.as_ref())
                .is_none()
            {
                missing.push(memory.record_id()?.into_owned());
            }
        }
        if !missing.is_empty() {
            let full = &mut self.full;
            self.store.load_many(&missing, &mut |memory| {
                let id = memory.record_id()?.into_owned();
                full.insert(id, memory);
                Ok(())
            })?;
        }
        self.replaced(memories)
    }

    pub fn replaced<'a, L: RecordId<S::Error>>(
        &self,
        memories: &'a [L],
    ) -> Result<Vec<Hydrated<'a, L, F>>, S::Error> {
        memories
            .iter()
            .map(|memory| {
                Ok(match self.full.get_borrowed(memory.record_id()?.as_ref()) {
                    Some(full) => Hydrated::Full(Arc::clone(full)),
                    None => Hydrated::Original(memory),
                })
            })
            .collect()
    }
}

/// The four values carried from top-hit expansion into the search event.
/// The two Python integer fields use canonical decimal text.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct TopExpansion {
    pub expanded_id: Option<PyStr>,
    pub drift_missing: String,
    pub commit_drift_status: Option<PyStr>,
    pub commits_since_verify: Option<String>,
}
