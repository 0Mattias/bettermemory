//! Python dicts use identity before equality, including inside tuple keys.
//! A common NaN handle identifies the standard JSON decoder's constant.
//! Runtime callers can retain identities for individual NaN leaves instead.
use std::{
    collections::HashMap,
    hash::{Hash, Hasher},
    sync::Arc,
};

#[derive(Clone, Debug, Default)]
pub struct KeyIdentity(Arc<()>);

impl KeyIdentity {
    pub fn new() -> Self {
        Self::default()
    }
}
impl PartialEq for KeyIdentity {
    fn eq(&self, other: &Self) -> bool {
        Arc::ptr_eq(&self.0, &other.0)
    }
}
impl Eq for KeyIdentity {}
impl Hash for KeyIdentity {
    fn hash<H: Hasher>(&self, state: &mut H) {
        Arc::as_ptr(&self.0).hash(state);
    }
}

#[derive(Clone, Debug, Default)]
pub struct KeyObjects {
    shared: Option<KeyIdentity>,
    keys: HashMap<(usize, usize), KeyIdentity>,
}
impl KeyObjects {
    /// Bind a runtime identity to one occurrence. Within a pair's key, NaN
    /// leaves are numbered from zero, left to right through nested tuples.
    pub fn bind(&mut self, pair: usize, nan: usize, identity: KeyIdentity) {
        self.keys.insert((pair, nan), identity);
    }
    pub(crate) fn json_constant() -> Self {
        Self {
            shared: Some(KeyIdentity::new()),
            ..Self::default()
        }
    }
    pub(crate) fn at(&self, key: usize, nan: usize) -> Option<&KeyIdentity> {
        self.keys.get(&(key, nan)).or(self.shared.as_ref())
    }
    pub(crate) fn is_empty(&self) -> bool {
        self.shared.is_none() && self.keys.is_empty()
    }
    pub(crate) fn retained(&self, first_rows: &[usize]) -> Self {
        if self.keys.is_empty() {
            return self.clone();
        }
        let positions: HashMap<_, _> = first_rows
            .iter()
            .enumerate()
            .map(|(index, &row)| (row, index))
            .collect();
        let keys = self
            .keys
            .iter()
            .filter_map(|(&(row, nan), id)| Some(((*positions.get(&row)?, nan), id.clone())))
            .collect();
        Self {
            keys,
            shared: self.shared.clone(),
        }
    }
    pub(crate) fn same(&self, a: usize, b: usize, nan: usize) -> bool {
        self.at(a, nan).is_some_and(|x| Some(x) == self.at(b, nan))
    }
}
