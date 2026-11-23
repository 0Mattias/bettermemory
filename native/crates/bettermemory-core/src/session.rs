//! `session.py` whole: the per-session state (disabled scopes, the turn
//! counter, pending and expired use-tokens), the per-client registry with
//! its LRU cap, and the process default registry.
//!
//! The clock (`time.time()`) and the random source (`secrets.token_hex(8)`)
//! are [`Effects`], passed to every call that reads them, in the order the
//! reference reads them. The reference's `RLock` is the [`Mutex`] of
//! [`SessionState`]: [`SessionState::lock`] gives a guard whose methods
//! are the reference's methods, so a caller needing several calls under
//! one lock takes the guard once.

use std::collections::{BTreeSet, HashMap};
use std::hash::Hash;
use std::sync::{Arc, Mutex, MutexGuard, OnceLock};
use std::time::{SystemTime, UNIX_EPOCH};

use crate::pyvalue::PyStr;

/// `_DEFAULT_CLIENT_KEY`.
pub const DEFAULT_CLIENT_KEY: &str = "__default__";
/// `_PENDING_USE_TOKEN_TTL_SECONDS` (30 minutes).
pub const PENDING_USE_TOKEN_TTL_SECONDS: i64 = 30 * 60;
/// `DEFAULT_USE_TOKEN_TTL_TURNS`.
pub const DEFAULT_USE_TOKEN_TTL_TURNS: i64 = 2;
/// `AUTO_COMMIT_MIN_AGE_SECONDS`.
pub const AUTO_COMMIT_MIN_AGE_SECONDS: f64 = 600.0;
/// `SessionRegistry.DEFAULT_MAX_CLIENTS`.
pub const DEFAULT_MAX_CLIENTS: i64 = 256;

/// The effects the module reads.
pub trait Effects {
    /// `time.time()`.
    fn time(&mut self) -> f64;
    /// `secrets.token_hex(8)`: 16 lowercase hex digits.
    fn token_hex8(&mut self) -> String;
}

/// The machine's clock and random source.
#[derive(Debug, Default, Clone, Copy)]
pub struct System;

impl Effects for System {
    /// As CPython's `time.time()`: the nanoseconds since the epoch, as a
    /// double divided by 1e9 (whole seconds exactly).
    fn time(&mut self) -> f64 {
        let ns: i128 = match SystemTime::now().duration_since(UNIX_EPOCH) {
            Ok(d) => i128::try_from(d.as_nanos()).unwrap_or(i128::MAX),
            Err(e) => -i128::try_from(e.duration().as_nanos()).unwrap_or(i128::MAX),
        };
        if ns % 1_000_000_000 == 0 {
            (ns / 1_000_000_000) as f64
        } else {
            ns as f64 / 1e9
        }
    }

    /// `os.urandom(8)` as hex. The reference raises where the random
    /// source fails; this panics with the cause.
    fn token_hex8(&mut self) -> String {
        let mut b = [0u8; 8];
        if let Err(e) = getrandom::fill(&mut b) {
            panic!("the random source: {e}");
        }
        b.iter().map(|x| format!("{x:02x}")).collect()
    }
}

/// `_new_session_id()`.
pub fn new_session_id(fx: &mut dyn Effects) -> String {
    format!("sess_{}", fx.token_hex8())
}

/// A Python `dict`'s order: an assignment to a present key keeps its
/// place, a new key goes last, a removal frees the place. Every slot
/// before `head` is free, so `pop_front` (`popitem(last=False)`) takes
/// amortized constant time, as the reference's does.
#[derive(Debug, Clone)]
pub struct OrderedMap<K, V> {
    slots: Vec<Option<(K, V)>>,
    index: HashMap<K, usize>,
    head: usize,
}

impl<K, V> Default for OrderedMap<K, V> {
    fn default() -> Self {
        OrderedMap {
            slots: Vec::new(),
            index: HashMap::new(),
            head: 0,
        }
    }
}

impl<K: Eq + Hash + Clone, V> OrderedMap<K, V> {
    pub fn len(&self) -> usize {
        self.index.len()
    }

    pub fn is_empty(&self) -> bool {
        self.index.is_empty()
    }

    pub fn get(&self, key: &K) -> Option<&V> {
        let i = *self.index.get(key)?;
        self.slots[i].as_ref().map(|(_, v)| v)
    }

    /// A borrowed lookup for an index whose key shares ownership of its data.
    pub(crate) fn get_borrowed<Q>(&self, key: &Q) -> Option<&V>
    where
        K: std::borrow::Borrow<Q>,
        Q: Eq + Hash + ?Sized,
    {
        let slot = *self.index.get(key)?;
        self.slots[slot].as_ref().map(|(_, value)| value)
    }

    pub(crate) fn get_or_insert_default(&mut self, key: K) -> &mut V
    where
        V: Default,
    {
        let slot = *self.index.entry(key.clone()).or_insert_with(|| {
            let next = self.slots.len();
            self.slots.push(Some((key, V::default())));
            next
        });
        &mut self.slots[slot].as_mut().expect("indexed slot").1
    }

    /// Consuming the map releases the index before its ordered entries are used.
    pub(crate) fn into_entries(self) -> impl Iterator<Item = (K, V)> {
        self.slots.into_iter().flatten()
    }

    pub fn insert(&mut self, key: K, value: V) {
        if let Some(&i) = self.index.get(&key) {
            self.slots[i] = Some((key, value));
            return;
        }
        self.index.insert(key.clone(), self.slots.len());
        self.slots.push(Some((key, value)));
    }

    pub fn remove(&mut self, key: &K) -> Option<V> {
        let i = self.index.remove(key)?;
        let (_, v) = self.slots[i].take()?;
        if self.slots.len() > 32 && self.index.len() * 2 < self.slots.len() {
            self.compact();
        }
        Some(v)
    }

    fn compact(&mut self) {
        self.slots.retain(Option::is_some);
        self.head = 0;
        for (i, slot) in self.slots.iter().enumerate() {
            if let Some((k, _)) = slot {
                self.index.insert(k.clone(), i);
            }
        }
    }

    /// `move_to_end(key)`; false when the key is absent.
    pub fn move_to_end(&mut self, key: &K) -> bool {
        match self.remove(key) {
            Some(v) => {
                self.insert(key.clone(), v);
                true
            }
            None => false,
        }
    }

    /// `popitem(last=False)`.
    pub fn pop_front(&mut self) -> Option<(K, V)> {
        while matches!(self.slots.get(self.head), Some(None)) {
            self.head += 1;
        }
        let key = self.slots.get(self.head)?.as_ref()?.0.clone();
        let v = self.remove(&key)?;
        Some((key, v))
    }

    pub fn clear(&mut self) {
        self.slots.clear();
        self.index.clear();
        self.head = 0;
    }

    pub fn iter(&self) -> impl Iterator<Item = (&K, &V)> {
        self.slots[self.head..]
            .iter()
            .flatten()
            .map(|(k, v)| (k, v))
    }

    pub fn keys(&self) -> impl Iterator<Item = &K> {
        self.iter().map(|(k, _)| k)
    }

    /// Every value in order, the map left empty.
    pub fn drain_values(&mut self) -> Vec<V> {
        let out = self.slots.drain(..).flatten().map(|(_, v)| v).collect();
        self.index.clear();
        self.head = 0;
        out
    }
}

/// `PendingUseToken`.
#[derive(Debug, Clone, PartialEq)]
pub struct PendingUseToken {
    pub token: String,
    pub memory_id: PyStr,
    pub issued_at: f64,
    pub issued_at_turn: i64,
}

/// `SessionState`'s fields: what its lock guards.
#[derive(Debug, Clone)]
pub struct Inner {
    pub session_id: String,
    pub client_key: PyStr,
    pub disabled_scopes: BTreeSet<PyStr>,
    pub pending_use_tokens: OrderedMap<PyStr, PendingUseToken>,
    pub turn_counter: i64,
    pub expired_use_tokens: OrderedMap<PyStr, PendingUseToken>,
}

impl Inner {
    /// `disable(scope)`: the scopes after, a copy.
    pub fn disable(&mut self, scope: PyStr) -> BTreeSet<PyStr> {
        self.disabled_scopes.insert(scope);
        self.disabled_scopes.clone()
    }

    /// `enable(scope)`: the scopes after, a copy.
    pub fn enable(&mut self, scope: &PyStr) -> BTreeSet<PyStr> {
        self.disabled_scopes.remove(scope);
        self.disabled_scopes.clone()
    }

    /// `advance_turn()`: the counter's new value, after the wall-clock
    /// eviction. Panics past `i64::MAX`, where the reference's integer
    /// keeps growing.
    pub fn advance_turn(&mut self, fx: &mut dyn Effects) -> i64 {
        self.turn_counter = self
            .turn_counter
            .checked_add(1)
            .expect("the turn counter past i64::MAX");
        self.evict_expired_use_tokens(fx);
        self.turn_counter
    }

    /// `issue_use_tokens(memory_ids)`: `{memory_id: token}` in the dict's
    /// order (a repeated id keeps its first place and its last token).
    pub fn issue_use_tokens(
        &mut self,
        memory_ids: &[PyStr],
        fx: &mut dyn Effects,
    ) -> OrderedMap<PyStr, String> {
        let mut out = OrderedMap::default();
        let now = fx.time();
        for mid in memory_ids {
            let token = format!("use_{}", fx.token_hex8());
            self.pending_use_tokens.insert(
                mid.clone(),
                PendingUseToken {
                    token: token.clone(),
                    memory_id: mid.clone(),
                    issued_at: now,
                    issued_at_turn: self.turn_counter,
                },
            );
            out.insert(mid.clone(), token);
        }
        out
    }

    /// `consume_old_tokens(ttl_turns=, min_age_seconds=, override_ids=)`.
    /// The turn cutoff is computed wide, as the reference's integers.
    pub fn consume_old_tokens(
        &mut self,
        ttl_turns: i64,
        min_age_seconds: Option<f64>,
        override_ids: Option<&BTreeSet<PyStr>>,
        fx: &mut dyn Effects,
    ) -> Vec<PyStr> {
        let min_age = min_age_seconds.unwrap_or(AUTO_COMMIT_MIN_AGE_SECONDS);
        let cutoff_turn = i128::from(self.turn_counter) - i128::from(ttl_turns);
        let age_cutoff = fx.time() - min_age;
        let ready: Vec<PyStr> = self
            .pending_use_tokens
            .iter()
            .filter(|(mid, _)| !override_ids.is_some_and(|o| o.contains(*mid)))
            .filter(|(_, tok)| {
                i128::from(tok.issued_at_turn) <= cutoff_turn && tok.issued_at <= age_cutoff
            })
            .map(|(mid, _)| mid.clone())
            .collect();
        for mid in &ready {
            self.pending_use_tokens.remove(mid);
        }
        ready
    }

    /// `purge_use_token(memory_id)`.
    pub fn purge_use_token(&mut self, memory_id: &PyStr) -> bool {
        self.pending_use_tokens.remove(memory_id).is_some()
    }

    /// `_evict_expired_use_tokens()`.
    fn evict_expired_use_tokens(&mut self, fx: &mut dyn Effects) {
        let cutoff = fx.time() - PENDING_USE_TOKEN_TTL_SECONDS as f64;
        let stale: Vec<PyStr> = self
            .pending_use_tokens
            .iter()
            .filter(|(_, tok)| tok.issued_at < cutoff)
            .map(|(mid, _)| mid.clone())
            .collect();
        for mid in stale {
            if let Some(tok) = self.pending_use_tokens.remove(&mid) {
                self.expired_use_tokens.insert(mid, tok);
            }
        }
    }

    /// `pop_expired_use_tokens()`.
    pub fn pop_expired_use_tokens(&mut self) -> Vec<PendingUseToken> {
        self.expired_use_tokens.drain_values()
    }

    /// `reset()`: the session id and the turn counter stay.
    pub fn reset(&mut self) {
        self.disabled_scopes.clear();
        self.pending_use_tokens.clear();
        self.expired_use_tokens.clear();
    }
}

/// `SessionState`.
#[derive(Debug)]
pub struct SessionState {
    inner: Mutex<Inner>,
}

impl SessionState {
    /// `SessionState()`: a fresh session id, the default client key.
    pub fn new(fx: &mut dyn Effects) -> SessionState {
        SessionState::with_client_key(DEFAULT_CLIENT_KEY.chars().map(u32::from).collect(), fx)
    }

    /// `SessionState(client_key=key)`.
    pub fn with_client_key(client_key: PyStr, fx: &mut dyn Effects) -> SessionState {
        SessionState::from_inner(Inner {
            session_id: new_session_id(fx),
            client_key,
            disabled_scopes: BTreeSet::new(),
            pending_use_tokens: OrderedMap::default(),
            turn_counter: 0,
            expired_use_tokens: OrderedMap::default(),
        })
    }

    /// A state with every field given, as the dataclass's keywords.
    pub fn from_inner(inner: Inner) -> SessionState {
        SessionState {
            inner: Mutex::new(inner),
        }
    }

    /// `with state.lock:`. A poisoned lock still gives the state, as an
    /// exception inside the reference's `with` leaves it usable.
    pub fn lock(&self) -> MutexGuard<'_, Inner> {
        self.inner.lock().unwrap_or_else(|e| e.into_inner())
    }

    /// `for_request(ctx)`: this state, whatever the request.
    pub fn for_request(&self) -> &SessionState {
        self
    }
}

/// `SessionSource`: the state for a request whose registry key is `key`
/// (`identity::registry_key` of the bound actor, None when it has none).
pub trait SessionSource {
    fn for_request(&self, key: Option<PyStr>, fx: &mut dyn Effects) -> Arc<SessionState>;
}

impl SessionSource for Arc<SessionState> {
    fn for_request(&self, _key: Option<PyStr>, _fx: &mut dyn Effects) -> Arc<SessionState> {
        Arc::clone(self)
    }
}

/// `SessionRegistry.stats()`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Stats {
    pub size: usize,
    pub evicted: u64,
    pub max_clients: i64,
}

#[derive(Debug, Default)]
struct Lru {
    states: OrderedMap<PyStr, Arc<SessionState>>,
    evicted: u64,
}

/// `SessionRegistry`.
#[derive(Debug)]
pub struct SessionRegistry {
    lru: Mutex<Lru>,
    pub max_clients: i64,
}

impl Default for SessionRegistry {
    fn default() -> Self {
        SessionRegistry::new(DEFAULT_MAX_CLIENTS)
    }
}

impl SessionRegistry {
    /// `SessionRegistry(max_clients)`.
    pub fn new(max_clients: i64) -> SessionRegistry {
        SessionRegistry {
            lru: Mutex::new(Lru::default()),
            max_clients,
        }
    }

    fn lru(&self) -> MutexGuard<'_, Lru> {
        self.lru.lock().unwrap_or_else(|e| e.into_inner())
    }

    /// `for_request(ctx)` with `_key_for_ctx`'s answer given as `key`
    /// (None is `_DEFAULT_CLIENT_KEY`): the lookup, the touch, the insert
    /// and one eviction past the cap, all under one lock.
    pub fn for_request(&self, key: Option<PyStr>, fx: &mut dyn Effects) -> Arc<SessionState> {
        let key = key.unwrap_or_else(|| DEFAULT_CLIENT_KEY.chars().map(u32::from).collect());
        let mut lru = self.lru();
        if let Some(state) = lru.states.get(&key).cloned() {
            lru.states.move_to_end(&key);
            return state;
        }
        let state = Arc::new(SessionState::with_client_key(key.clone(), fx));
        lru.states.insert(key, Arc::clone(&state));
        if i128::try_from(lru.states.len()).unwrap_or(i128::MAX) > i128::from(self.max_clients) {
            lru.states.pop_front();
            lru.evicted += 1;
        }
        state
    }

    /// `known_keys()`.
    pub fn known_keys(&self) -> BTreeSet<PyStr> {
        self.lru().states.keys().cloned().collect()
    }

    /// The keys, least recently used first (the `OrderedDict`'s order).
    pub fn keys_in_order(&self) -> Vec<PyStr> {
        self.lru().states.keys().cloned().collect()
    }

    /// `stats()`.
    pub fn stats(&self) -> Stats {
        let lru = self.lru();
        Stats {
            size: lru.states.len(),
            evicted: lru.evicted,
            max_clients: self.max_clients,
        }
    }
}

impl SessionSource for SessionRegistry {
    fn for_request(&self, key: Option<PyStr>, fx: &mut dyn Effects) -> Arc<SessionState> {
        SessionRegistry::for_request(self, key, fx)
    }
}

/// `get_default_registry()`.
pub fn default_registry() -> &'static SessionRegistry {
    static REGISTRY: OnceLock<SessionRegistry> = OnceLock::new();
    REGISTRY.get_or_init(SessionRegistry::default)
}

/// `get_state()`: the default registry's no-key state.
pub fn get_state() -> Arc<SessionState> {
    default_registry().for_request(None, &mut System)
}
