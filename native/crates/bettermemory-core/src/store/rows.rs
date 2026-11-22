//! `store_rows`'s shared leaves: the membership table specs and their row
//! checks (`_MEMBERSHIP_SPECS`, `_membership_values`, `_membership_key`,
//! `_membership_spec`, `_membership_column`), the upsert statement
//! (`_upsert_sql`), the link rows (`_sync_links`), the batch row sort
//! (`_sort_rows`), the schema's columns (`_known_columns`), a
//! verification row read back (`_verification_row`), a log row as a
//! telemetry event (`_event_from_columns`, `Store._event_from_raw`) and
//! the kind sets of `store_schema` and `log` that decide which rows are
//! telemetry.

use std::cmp::Ordering;
use std::sync::OnceLock;

use rusqlite::Connection;

use super::fold::{json_text, FoldError};
use super::{SqlValue, StoreError};
use crate::pyvalue::{repr_str, PyStr, PyValue};
use crate::records::rows::{load_json, Row};
use crate::records::RecordError;
use crate::unicode;

/// One `_MembershipTable`.
pub struct Spec {
    pub table: &'static str,
    pub kind: &'static str,
    pub key: &'static [&'static str],
    pub columns: &'static [&'static str],
    pub optional: &'static [&'static str],
    pub floats: &'static [&'static str],
}

pub const SPECS: [Spec; 14] = [
    Spec {
        table: "native_membership_rows",
        kind: "native_member",
        key: &["member_kind", "key_text"],
        columns: &["member_kind", "key_text", "row_text"],
        optional: &[],
        floats: &[],
    },
    Spec {
        table: "machines",
        kind: "machine",
        key: &["id"],
        columns: &["id", "facts_json", "predecessor", "minted"],
        optional: &[],
        floats: &[],
    },
    Spec {
        table: "projects",
        kind: "project",
        key: &["id"],
        columns: &[
            "id",
            "name",
            "merged_into",
            "provisional",
            "parent",
            "tenant",
            "folder",
            "boundary",
            "minted",
        ],
        optional: &[],
        floats: &[],
    },
    Spec {
        table: "project_witnesses",
        kind: "project_witness",
        key: &["project", "commit_id"],
        columns: &["project", "commit_id", "arrival", "source", "fingerprint"],
        optional: &[],
        floats: &[],
    },
    Spec {
        table: "project_blobs",
        kind: "project_blob",
        key: &["project", "blob"],
        columns: &["project", "blob", "witness"],
        optional: &[],
        floats: &[],
    },
    Spec {
        table: "project_remotes",
        kind: "project_remote",
        key: &["project", "remote_key"],
        columns: &["project", "remote_key"],
        optional: &[],
        floats: &[],
    },
    Spec {
        table: "project_files",
        kind: "project_file",
        key: &["file_id"],
        columns: &["file_id", "project", "evidence"],
        optional: &[],
        floats: &[],
    },
    Spec {
        table: "declared_remotes",
        kind: "declared_remote",
        key: &["remote_key"],
        columns: &["remote_key", "project", "principal"],
        optional: &[],
        floats: &[],
    },
    Spec {
        table: "checkouts",
        kind: "checkout",
        key: &["id"],
        columns: &[
            "id",
            "project",
            "machine",
            "kind",
            "object_device",
            "object_inode",
            "object_birth",
            "common_device",
            "common_inode",
            "common_birth",
            "git_dir",
            "token",
            "token_status",
            "stamps_json",
            "last_head",
            "branch",
            "remotes_json",
            "manifest_json",
            "legacy",
            "retired",
            "registered",
            "unresolved_json",
        ],
        optional: &["unresolved_json"],
        floats: &[],
    },
    Spec {
        table: "checkout_views",
        kind: "checkout_view",
        key: &["checkout", "machine", "path"],
        columns: &[
            "checkout",
            "machine",
            "path",
            "first_seen",
            "last_seen",
            "seen_order",
        ],
        optional: &[],
        floats: &[],
    },
    Spec {
        table: "homes",
        kind: "home",
        key: &["record_id"],
        columns: &[
            "record_id",
            "kind",
            "project",
            "checkout",
            "rule",
            "decided",
        ],
        optional: &[],
        floats: &[],
    },
    Spec {
        table: "proposals",
        kind: "proposal",
        key: &["id"],
        columns: &[
            "id",
            "checkout",
            "kind",
            "candidates_json",
            "evidence_json",
            "reason",
            "line",
            "state",
            "answer",
            "answered_by",
            "signature_json",
            "opened",
        ],
        optional: &[],
        floats: &[],
    },
    Spec {
        table: "standing",
        kind: "standing",
        key: &["record_id"],
        columns: &[
            "record_id",
            "probability",
            "body_sha256",
            "source",
            "answered_by",
            "answered",
        ],
        optional: &[],
        floats: &["probability"],
    },
    Spec {
        table: "returned",
        kind: "returned",
        key: &["record_id"],
        columns: &["record_id", "via", "returned"],
        optional: &[],
        floats: &[],
    },
];

pub(crate) const VERIFICATION_LISTS: [&str; 4] = [
    "verified_paths",
    "verified_commits",
    "verified_versions",
    "verified_absent_paths",
];

pub fn upsert(table: &str, columns: &[&str], key: &str) -> String {
    let updates: Vec<String> = columns
        .iter()
        .filter(|c| **c != key)
        .map(|c| format!("{c} = excluded.{c}"))
        .collect();
    format!(
        "INSERT INTO {table}({}) VALUES ({}) ON CONFLICT({key}) DO UPDATE SET {}",
        columns.join(", "),
        vec!["?"; columns.len()].join(", "),
        updates.join(", ")
    )
}

/// What applying a row raised: an exception `refold` catches, with
/// `str(exc)`, or one it lets out.
#[derive(Clone, Debug, PartialEq)]
pub enum Fail {
    Caught(&'static str, String),
    Raised(&'static str),
}

pub(crate) fn key_error(name: &str) -> Fail {
    Fail::Caught(
        "KeyError",
        unicode::to_string(&repr_str(&unicode::cps(name))).unwrap_or_default(),
    )
}

pub(crate) fn text(s: PyStr) -> String {
    unicode::to_string(&s).unwrap_or_default()
}

pub(crate) fn item<'a>(payload: &'a PyValue, name: &str) -> Result<&'a PyValue, Fail> {
    payload.get(name).ok_or_else(|| key_error(name))
}

pub(crate) fn get<'a>(payload: &'a PyValue, name: &str) -> &'a PyValue {
    static NONE: PyValue = PyValue::None;
    payload.get(name).unwrap_or(&NONE)
}

/// `_membership_values`.
pub fn membership_values(spec: &Spec, row: &PyValue) -> Result<Vec<PyValue>, Fail> {
    let PyValue::Dict(items) = row else {
        return Err(Fail::Caught(
            "TypeError",
            format!("a {} row must be an object", spec.kind),
        ));
    };
    let mut unknown: Vec<PyStr> = Vec::new();
    for (k, _) in items {
        let PyValue::Str(k) = k else {
            return Err(Fail::Raised("TypeError"));
        };
        let known = unicode::to_string(k).is_some_and(|k| spec.columns.contains(&k.as_str()));
        if !known && !unknown.contains(k) {
            unknown.push(k.clone());
        }
    }
    if !unknown.is_empty() {
        unknown.sort();
        let shown: Vec<String> = unknown.iter().map(|k| text(repr_str(k))).collect();
        return Err(Fail::Caught(
            "ValueError",
            format!("unknown {} columns [{}]", spec.kind, shown.join(", ")),
        ));
    }
    let mut values = Vec::with_capacity(spec.columns.len());
    for column in spec.columns {
        let value = if spec.optional.contains(column) {
            get(row, column)
        } else {
            item(row, column)?
        };
        values.push(if column.ends_with("_json") {
            match value {
                PyValue::None => PyValue::None,
                other => json_text(other)?,
            }
        } else {
            match value {
                PyValue::Bool(b) => PyValue::int(i128::from(*b)),
                PyValue::None | PyValue::Str(_) | PyValue::Int(_) => value.clone(),
                PyValue::Float(f) if spec.floats.contains(column) && f.is_finite() => value.clone(),
                _ => {
                    return Err(Fail::Caught(
                        "TypeError",
                        format!("{}.{column} must be text, an integer or null", spec.kind),
                    ))
                }
            }
        });
    }
    membership_key(spec, row)?;
    Ok(values)
}

/// `_membership_key`.
pub(crate) fn membership_key(spec: &Spec, payload: &PyValue) -> Result<Vec<PyValue>, Fail> {
    let mut key = Vec::new();
    for column in spec.key {
        match item(payload, column)? {
            PyValue::Str(s) if !s.is_empty() => key.push(PyValue::Str(s.clone())),
            _ => {
                return Err(Fail::Caught(
                    "ValueError",
                    format!("{}.{column} must be non-empty text", spec.kind),
                ))
            }
        }
    }
    Ok(key)
}

pub(crate) fn columns_of(conn: &Connection, table: &str) -> Result<Vec<String>, FoldError> {
    let mut statement = conn
        .prepare(&format!("PRAGMA table_info({table})"))
        .map_err(StoreError::from)?;
    let names = statement
        .query_map([], |r| r.get::<_, String>(1))
        .map_err(StoreError::from)?
        .collect::<Result<Vec<_>, _>>()
        .map_err(StoreError::from)?;
    Ok(names)
}

/// `_sync_links`: replaces the link rows of `source_id` with `links`
/// (type, target, note), exact duplicates collapsed in first-seen order.
/// `exec` runs one statement with its bound values.
pub fn sync_links<E>(
    source_id: &PyValue,
    links: impl IntoIterator<Item = [PyValue; 3]>,
    mut exec: impl FnMut(&str, &[PyValue]) -> Result<(), E>,
) -> Result<(), E> {
    exec(
        "DELETE FROM memory_links WHERE source_id = ?",
        std::slice::from_ref(source_id),
    )?;
    let mut seen: Vec<[PyValue; 4]> = Vec::new();
    for [kind, target, note] in links {
        let key = [source_id.clone(), kind, target, note];
        if !seen.contains(&key) {
            seen.push(key);
        }
    }
    for key in &seen {
        exec(
            "INSERT OR IGNORE INTO memory_links(source_id, type, target_id, note) \
             VALUES (?, ?, ?, ?)",
            key,
        )?;
    }
    Ok(())
}

/// `_membership_spec`.
pub fn membership_spec(kind: &[u32]) -> Result<&'static Spec, Fail> {
    unicode::to_string(kind)
        .and_then(|k| SPECS.iter().find(|s| s.kind == k))
        .ok_or_else(|| {
            Fail::Caught(
                "ValueError",
                format!("{} is not a membership row kind", text(repr_str(kind))),
            )
        })
}

/// `_MEMBERSHIP_BY_TABLE`.
pub fn membership_spec_by_table(table: &str) -> Option<&'static Spec> {
    SPECS.iter().find(|s| s.table == table)
}

/// `_membership_column`.
pub fn membership_column(spec: &Spec, column: &[u32]) -> Result<(), Fail> {
    if unicode::to_string(column).is_some_and(|c| spec.columns.contains(&c.as_str())) {
        return Ok(());
    }
    Err(Fail::Caught(
        "ValueError",
        format!("{} has no column {}", spec.table, text(repr_str(column))),
    ))
}

/// `_iso_opt`: `dt.isoformat()`, or None for None.
pub fn iso_opt(dt: Option<&crate::datetime::DateTime>) -> Option<String> {
    dt.map(crate::datetime::DateTime::isoformat)
}

/// A sort key: `(value is not None, value or 0)`.
type Key = (bool, SqlValue);

fn type_name(v: &SqlValue) -> &'static str {
    match v {
        SqlValue::Null => "NoneType",
        SqlValue::Integer(_) => "int",
        SqlValue::Real(_) => "float",
        SqlValue::Text(_) => "str",
        SqlValue::Blob(_) => "bytes",
    }
}

/// An int against a float, exactly, as Python compares them.
fn int_float(i: i64, f: f64) -> Option<Ordering> {
    if f.is_nan() {
        return None;
    }
    if f >= 9_223_372_036_854_775_808.0 {
        return Some(Ordering::Less);
    }
    if f < -9_223_372_036_854_775_808.0 {
        return Some(Ordering::Greater);
    }
    let whole = f.trunc();
    // In range: `whole` is an integer that fits i64.
    #[allow(clippy::cast_possible_truncation)]
    let t = whole as i64;
    Some(
        i.cmp(&t)
            .then_with(|| 0.0f64.partial_cmp(&(f - whole)).unwrap_or(Ordering::Equal)),
    )
}

/// Python's comparison of two values of comparable types (None when it
/// is unordered: a NaN), or the TypeError of `<` across types.
fn compare(a: &SqlValue, b: &SqlValue) -> Result<Option<Ordering>, Fail> {
    use SqlValue::{Blob, Integer, Real, Text};
    Ok(match (a, b) {
        (Integer(x), Integer(y)) => Some(x.cmp(y)),
        (Real(x), Real(y)) => x.partial_cmp(y),
        (Integer(x), Real(y)) => int_float(*x, *y),
        (Real(x), Integer(y)) => int_float(*y, *x).map(Ordering::reverse),
        (Text(x), Text(y)) => Some(x.as_bytes().cmp(y.as_bytes())),
        (Blob(x), Blob(y)) => Some(x.cmp(y)),
        _ => {
            return Err(Fail::Caught(
                "TypeError",
                format!(
                    "'<' not supported between instances of '{}' and '{}'",
                    type_name(a),
                    type_name(b)
                ),
            ))
        }
    })
}

/// `x < y` over two keys, as Python compares tuples: the first items
/// that differ (by `==`, which never raises) decide, by `<`.
fn key_lt(x: &Key, y: &Key) -> Result<bool, Fail> {
    if x.0 != y.0 {
        return Ok(!x.0 & y.0);
    }
    // Across types `==` is False and `<` raises.
    Ok(compare(&x.1, &y.1)? == Some(Ordering::Less))
}

/// One keyed row: its sort key and its place in the list before the sort.
type Item = (Key, usize);

fn lt(x: &Item, y: &Item) -> Result<bool, Fail> {
    key_lt(&x.0, &y.0)
}

/// CPython 3.13's `count_run`: the length of the run at the start of
/// `a`, a descending one reversed in place.
fn count_run(a: &mut [Item]) -> Result<usize, Fail> {
    let n = a.len();
    let lt = |a: &[Item], i: usize, j: usize| lt(&a[i], &a[j]);
    let mut k = 1;
    while k < n && !lt(a, k, k - 1)? {
        k += 1;
    }
    if k == n {
        return Ok(n);
    }
    if k > 1 {
        if lt(a, 0, k - 1)? {
            return Ok(k);
        }
        a[..k].reverse();
    }
    k += 1;
    let mut neq = 0;
    while k < n {
        if lt(a, k, k - 1)? {
            if neq > 0 {
                a[k - neq - 1..k].reverse();
                neq = 0;
            }
        } else if lt(a, k - 1, k)? {
            break;
        } else {
            neq += 1;
        }
        k += 1;
    }
    if neq > 0 {
        a[k - neq - 1..k].reverse();
    }
    a[..k].reverse();
    while k < n && !lt(a, k, k - 1)? {
        k += 1;
    }
    Ok(k)
}

/// CPython 3.13's `binarysort`: `a[..ok]` sorted, the rest inserted.
fn binary_sort(a: &mut [Item], ok: usize) -> Result<(), Fail> {
    for ok in ok.max(1)..a.len() {
        let (mut l, mut r) = (0, ok);
        loop {
            let m = (l + r) >> 1;
            if lt(&a[ok], &a[m])? {
                r = m;
            } else {
                l = m + 1;
            }
            if l >= r {
                break;
            }
        }
        a[l..=ok].rotate_right(1);
    }
    Ok(())
}

/// CPython's `MIN_GALLOP`.
const MIN_GALLOP: usize = 7;

/// CPython's `gallop_left`: where `key` goes in the sorted `a`, left of
/// any equal item, searched from `a[hint]` outwards.
fn gallop_left(key: &Item, a: &[Item], hint: usize) -> Result<usize, Fail> {
    let n = a.len();
    let (mut lastofs, mut ofs);
    if lt(&a[hint], key)? {
        let maxofs = n - hint;
        (lastofs, ofs) = (0, 1);
        while ofs < maxofs {
            if lt(&a[hint + ofs], key)? {
                lastofs = ofs;
                ofs = (ofs << 1) + 1;
            } else {
                break;
            }
        }
        ofs = ofs.min(maxofs);
        lastofs += hint;
        ofs += hint;
    } else {
        let maxofs = hint + 1;
        (lastofs, ofs) = (0, 1);
        while ofs < maxofs {
            if lt(&a[hint - ofs], key)? {
                break;
            }
            lastofs = ofs;
            ofs = (ofs << 1) + 1;
        }
        ofs = ofs.min(maxofs);
        // From offsets below `hint` to offsets from the start, one past
        // CPython's (whose `lastofs` may be -1).
        let k = lastofs;
        lastofs = hint + 1 - ofs;
        ofs = hint - k;
        return gallop_finish_left(key, a, lastofs, ofs);
    }
    gallop_finish_left(key, a, lastofs + 1, ofs)
}

/// The binary search that ends `gallop_left`, over `a[lo..hi]` with
/// `a[lo - 1] < key <= a[hi]`.
fn gallop_finish_left(key: &Item, a: &[Item], mut lo: usize, mut hi: usize) -> Result<usize, Fail> {
    while lo < hi {
        let m = lo + ((hi - lo) >> 1);
        if lt(&a[m], key)? {
            lo = m + 1;
        } else {
            hi = m;
        }
    }
    Ok(hi)
}

/// CPython's `gallop_right`: where `key` goes in the sorted `a`, right
/// of any equal item, searched from `a[hint]` outwards.
fn gallop_right(key: &Item, a: &[Item], hint: usize) -> Result<usize, Fail> {
    let n = a.len();
    let (mut lastofs, mut ofs) = (0, 1);
    let lo;
    if lt(key, &a[hint])? {
        let maxofs = hint + 1;
        while ofs < maxofs {
            if lt(key, &a[hint - ofs])? {
                lastofs = ofs;
                ofs = (ofs << 1) + 1;
            } else {
                break;
            }
        }
        ofs = ofs.min(maxofs);
        let k = lastofs;
        // One past CPython's `lastofs`, which may be -1.
        lo = hint + 1 - ofs;
        ofs = hint - k;
    } else {
        let maxofs = n - hint;
        while ofs < maxofs {
            if lt(key, &a[hint + ofs])? {
                break;
            }
            lastofs = ofs;
            ofs = (ofs << 1) + 1;
        }
        ofs = ofs.min(maxofs);
        lo = lastofs + hint + 1;
        ofs += hint;
    }
    let (mut lo, mut hi) = (lo, ofs);
    while lo < hi {
        let m = lo + ((hi - lo) >> 1);
        if lt(key, &a[m])? {
            hi = m;
        } else {
            lo = m + 1;
        }
    }
    Ok(hi)
}

/// CPython's `powerloop`: the power of the boundary between the run of
/// `n1` items at `s1` and the next one of `n2`, in a list of `n`.
fn powerloop(s1: usize, n1: usize, n2: usize, n: usize) -> u32 {
    let mut result = 0;
    let mut a = 2 * s1 + n1;
    let mut b = a + n1 + n2;
    loop {
        result += 1;
        if a >= n {
            a -= n;
            b -= n;
        } else if b >= n {
            break;
        }
        a <<= 1;
        b <<= 1;
    }
    result
}

/// A pending run: where it starts, its length, its power.
struct Run {
    base: usize,
    len: usize,
    power: u32,
}

/// CPython 3.13's `MergeState`, over the list being sorted.
struct Merger<'a> {
    a: &'a mut [Item],
    runs: Vec<Run>,
    min_gallop: usize,
}

impl Merger<'_> {
    /// `found_new_run`: merge what the new run of `n2` items closes off.
    fn found_new_run(&mut self, n2: usize) -> Result<(), Fail> {
        if let Some(last) = self.runs.last() {
            let power = powerloop(last.base, last.len, n2, self.a.len());
            while self.runs.len() > 1 && self.runs[self.runs.len() - 2].power > power {
                self.merge_at(self.runs.len() - 2)?;
            }
            let last = self.runs.len() - 1;
            self.runs[last].power = power;
        }
        Ok(())
    }

    /// `merge_force_collapse`.
    fn force_collapse(&mut self) -> Result<(), Fail> {
        while self.runs.len() > 1 {
            let mut n = self.runs.len() - 2;
            if n > 0 && self.runs[n - 1].len < self.runs[n + 1].len {
                n -= 1;
            }
            self.merge_at(n)?;
        }
        Ok(())
    }

    /// `merge_at`: runs `i` and `i + 1` as one.
    fn merge_at(&mut self, i: usize) -> Result<(), Fail> {
        let (mut pa, mut na) = (self.runs[i].base, self.runs[i].len);
        let (pb, nb) = (self.runs[i + 1].base, self.runs[i + 1].len);
        self.runs[i].len = na + nb;
        self.runs.remove(i + 1);
        let k = gallop_right(&self.a[pb], &self.a[pa..pa + na], 0)?;
        pa += k;
        na -= k;
        if na == 0 {
            return Ok(());
        }
        let nb = gallop_left(&self.a[pa + na - 1], &self.a[pb..pb + nb], nb - 1)?;
        if nb == 0 {
            return Ok(());
        }
        if na <= nb {
            self.merge_lo(pa, na, pb, nb)
        } else {
            self.merge_hi(pa, na, pb, nb)
        }
    }

    /// `merge_lo`: the left run (the shorter) copied aside and merged
    /// from the left. On a refusal every item is still in the list.
    fn merge_lo(&mut self, pa: usize, na: usize, pb: usize, nb: usize) -> Result<(), Fail> {
        let tmp: Vec<Item> = self.a[pa..pa + na].to_vec();
        let (dest, ia, ib) = (pa, 0, pb);
        let mut c = Cursor {
            dest,
            ia,
            ib,
            na,
            nb,
        };
        let result = merge_lo_body(self.a, &tmp, &mut c, &mut self.min_gallop);
        let Cursor {
            dest,
            ia,
            ib,
            na,
            nb,
        } = c;
        let a = &mut *self.a;
        match result {
            // CopyB: the rest of the right run, then the left's last item.
            Ok(true) => {
                for j in 0..nb {
                    a[dest + j] = a[ib + j].clone();
                }
                a[dest + nb] = tmp[ia].clone();
                Ok(())
            }
            done => {
                a[dest..dest + na].clone_from_slice(&tmp[ia..ia + na]);
                done.map(|_| ())
            }
        }
    }

    /// `merge_hi`: the right run (the shorter) copied aside and merged
    /// from the right. Positions are one past CPython's pointers, which
    /// may point before the list.
    fn merge_hi(&mut self, pa: usize, na: usize, pb: usize, nb: usize) -> Result<(), Fail> {
        let tmp: Vec<Item> = self.a[pb..pb + nb].to_vec();
        // `dest` and `ia` index the list, `ib` the copy, each one past
        // the item CPython's pointer names.
        let (dest, ia, ib) = (pb + nb, pa + na, nb);
        let mut c = Cursor {
            dest,
            ia,
            ib,
            na,
            nb,
        };
        let result = merge_hi_body(self.a, &tmp, &mut c, &mut self.min_gallop);
        let Cursor {
            mut dest,
            ia,
            ib,
            na,
            nb,
        } = c;
        let a = &mut *self.a;
        match result {
            // CopyA: the rest of the left run, then the right's first item.
            Ok(true) => {
                for j in (0..na).rev() {
                    a[dest - na + j] = a[ia - na + j].clone();
                }
                dest -= na;
                a[dest - 1] = tmp[ib - 1].clone();
                Ok(())
            }
            done => {
                a[dest - nb..dest].clone_from_slice(&tmp[..nb]);
                done.map(|_| ())
            }
        }
    }
}

/// Where a merge stands: the next places to write and read, and how
/// many items each run has left.
struct Cursor {
    dest: usize,
    ia: usize,
    ib: usize,
    na: usize,
    nb: usize,
}

/// The merge loop of `merge_lo`; `Ok(true)` is its `CopyB` exit.
fn merge_lo_body(
    a: &mut [Item],
    tmp: &[Item],
    c: &mut Cursor,
    min_gallop_out: &mut usize,
) -> Result<bool, Fail> {
    a[c.dest] = a[c.ib].clone();
    c.dest += 1;
    c.ib += 1;
    c.nb -= 1;
    if c.nb == 0 {
        return Ok(false);
    }
    if c.na == 1 {
        return Ok(true);
    }
    let mut min_gallop = *min_gallop_out;
    loop {
        let (mut acount, mut bcount) = (0, 0);
        loop {
            if lt(&a[c.ib], &tmp[c.ia])? {
                a[c.dest] = a[c.ib].clone();
                c.dest += 1;
                c.ib += 1;
                bcount += 1;
                acount = 0;
                c.nb -= 1;
                if c.nb == 0 {
                    return Ok(false);
                }
                if bcount >= min_gallop {
                    break;
                }
            } else {
                a[c.dest] = tmp[c.ia].clone();
                c.dest += 1;
                c.ia += 1;
                acount += 1;
                bcount = 0;
                c.na -= 1;
                if c.na == 1 {
                    return Ok(true);
                }
                if acount >= min_gallop {
                    break;
                }
            }
        }
        min_gallop += 1;
        loop {
            min_gallop -= usize::from(min_gallop > 1);
            *min_gallop_out = min_gallop;
            let k = gallop_right(&a[c.ib], &tmp[c.ia..c.ia + c.na], 0)?;
            acount = k;
            if k > 0 {
                a[c.dest..c.dest + k].clone_from_slice(&tmp[c.ia..c.ia + k]);
                c.dest += k;
                c.ia += k;
                c.na -= k;
                if c.na == 1 {
                    return Ok(true);
                }
                if c.na == 0 {
                    return Ok(false);
                }
            }
            a[c.dest] = a[c.ib].clone();
            c.dest += 1;
            c.ib += 1;
            c.nb -= 1;
            if c.nb == 0 {
                return Ok(false);
            }
            let k = gallop_left(&tmp[c.ia], &a[c.ib..c.ib + c.nb], 0)?;
            bcount = k;
            if k > 0 {
                for j in 0..k {
                    a[c.dest + j] = a[c.ib + j].clone();
                }
                c.dest += k;
                c.ib += k;
                c.nb -= k;
                if c.nb == 0 {
                    return Ok(false);
                }
            }
            a[c.dest] = tmp[c.ia].clone();
            c.dest += 1;
            c.ia += 1;
            c.na -= 1;
            if c.na == 1 {
                return Ok(true);
            }
            if acount < MIN_GALLOP && bcount < MIN_GALLOP {
                break;
            }
        }
        min_gallop += 1;
        *min_gallop_out = min_gallop;
    }
}

/// The merge loop of `merge_hi`; `Ok(true)` is its `CopyA` exit.
fn merge_hi_body(
    a: &mut [Item],
    tmp: &[Item],
    c: &mut Cursor,
    min_gallop_out: &mut usize,
) -> Result<bool, Fail> {
    a[c.dest - 1] = a[c.ia - 1].clone();
    c.dest -= 1;
    c.ia -= 1;
    c.na -= 1;
    if c.na == 0 {
        return Ok(false);
    }
    if c.nb == 1 {
        return Ok(true);
    }
    let mut min_gallop = *min_gallop_out;
    loop {
        let (mut acount, mut bcount) = (0, 0);
        loop {
            if lt(&tmp[c.ib - 1], &a[c.ia - 1])? {
                a[c.dest - 1] = a[c.ia - 1].clone();
                c.dest -= 1;
                c.ia -= 1;
                acount += 1;
                bcount = 0;
                c.na -= 1;
                if c.na == 0 {
                    return Ok(false);
                }
                if acount >= min_gallop {
                    break;
                }
            } else {
                a[c.dest - 1] = tmp[c.ib - 1].clone();
                c.dest -= 1;
                c.ib -= 1;
                bcount += 1;
                acount = 0;
                c.nb -= 1;
                if c.nb == 1 {
                    return Ok(true);
                }
                if bcount >= min_gallop {
                    break;
                }
            }
        }
        min_gallop += 1;
        loop {
            min_gallop -= usize::from(min_gallop > 1);
            *min_gallop_out = min_gallop;
            let k = gallop_right(&tmp[c.ib - 1], &a[c.ia - c.na..c.ia], c.na - 1)?;
            let k = c.na - k;
            acount = k;
            if k > 0 {
                for j in (0..k).rev() {
                    a[c.dest - k + j] = a[c.ia - k + j].clone();
                }
                c.dest -= k;
                c.ia -= k;
                c.na -= k;
                if c.na == 0 {
                    return Ok(false);
                }
            }
            a[c.dest - 1] = tmp[c.ib - 1].clone();
            c.dest -= 1;
            c.ib -= 1;
            c.nb -= 1;
            if c.nb == 1 {
                return Ok(true);
            }
            let k = gallop_left(&a[c.ia - 1], &tmp[..c.nb], c.nb - 1)?;
            let k = c.nb - k;
            bcount = k;
            if k > 0 {
                a[c.dest - k..c.dest].clone_from_slice(&tmp[c.ib - k..c.ib]);
                c.dest -= k;
                c.ib -= k;
                c.nb -= k;
                if c.nb == 1 {
                    return Ok(true);
                }
                if c.nb == 0 {
                    return Ok(false);
                }
            }
            a[c.dest - 1] = a[c.ia - 1].clone();
            c.dest -= 1;
            c.ia -= 1;
            c.na -= 1;
            if c.na == 0 {
                return Ok(false);
            }
            if acount < MIN_GALLOP && bcount < MIN_GALLOP {
                break;
            }
        }
        min_gallop += 1;
        *min_gallop_out = min_gallop;
    }
}

/// `list.sort` over the keyed rows: ascending, stable, by CPython 3.13's
/// own sequence of comparisons (`count_run`, binary insertion up to the
/// minimum run, the powersort merge order, galloping merges), so a
/// TypeError names the pair CPython's first failing comparison names and
/// a NaN leaves the order CPython leaves.
fn list_sort(a: &mut [Item]) -> Result<(), Fail> {
    let n = a.len();
    if n < 2 {
        return Ok(());
    }
    let min_run = {
        let (mut n, mut r) = (n, 0);
        while n >= 64 {
            r |= n & 1;
            n >>= 1;
        }
        n + r
    };
    let mut merger = Merger {
        a,
        runs: Vec::new(),
        min_gallop: MIN_GALLOP,
    };
    let mut lo = 0;
    while lo < n {
        let mut k = count_run(&mut merger.a[lo..])?;
        if k < min_run {
            let force = (n - lo).min(min_run);
            binary_sort(&mut merger.a[lo..lo + force], k)?;
            k = force;
        }
        merger.found_new_run(k)?;
        merger.runs.push(Run {
            base: lo,
            len: k,
            power: 0,
        });
        lo += k;
    }
    merger.force_collapse()
}

/// `_sort_rows`: one stable sort per name, the last name first, by
/// `(value is not None, value or 0)`, descending for a name led by `-`
/// (all its leading `-` stripped). Descending is `reverse=True`: the
/// list reversed, sorted ascending and reversed back, so equal rows keep
/// their order. A row without the column is KeyError, raised while the
/// keys are made, before any comparison; values Python cannot order
/// against each other are its TypeError. On a refusal every row is still
/// there, in the order the names before the refusing one left them (the
/// reference leaves its list part sorted: not compared).
pub fn sort_rows(rows: &mut Vec<Row>, order_by: &[&str]) -> Result<(), Fail> {
    for name in order_by.iter().rev() {
        let column = name.trim_start_matches('-');
        let mut keyed = Vec::with_capacity(rows.len());
        for (i, row) in rows.iter().enumerate() {
            let value = row.get(column).ok_or_else(|| key_error(column))?;
            let key = match value {
                SqlValue::Null => (false, SqlValue::Integer(0)),
                v => (true, v.clone()),
            };
            keyed.push((key, i));
        }
        let reverse = name.starts_with('-');
        if reverse {
            keyed.reverse();
        }
        // On a refusal the rows stay as this name's sort found them.
        list_sort(&mut keyed)?;
        if reverse {
            keyed.reverse();
        }
        let mut old: Vec<Option<Row>> = std::mem::take(rows).into_iter().map(Some).collect();
        *rows = keyed
            .into_iter()
            .map(|(_, i)| old[i].take().expect("each row once"))
            .collect();
    }
    Ok(())
}

/// `_known_columns`: every table of an in-memory database built from
/// `SCHEMA` and then `MEMBERSHIP_SCHEMA` (the first two of the pinned
/// schema texts), in `sqlite_master`'s order, with its columns in the
/// schema's order.
pub fn known_columns() -> &'static [(String, Vec<String>)] {
    static KNOWN: OnceLock<Vec<(String, Vec<String>)>> = OnceLock::new();
    KNOWN.get_or_init(|| read_known().expect("the pinned schema builds"))
}

fn read_known() -> Result<Vec<(String, Vec<String>)>, FoldError> {
    let conn = Connection::open_in_memory().map_err(StoreError::from)?;
    for text in super::admission::schema_texts().iter().take(2) {
        conn.execute_batch(text).map_err(StoreError::from)?;
    }
    let tables: Vec<String> = {
        let mut statement = conn
            .prepare("SELECT name FROM sqlite_master WHERE type = 'table'")
            .map_err(StoreError::from)?;
        let names = statement
            .query_map([], |r| r.get::<_, String>(0))
            .map_err(StoreError::from)?
            .collect::<Result<Vec<_>, _>>()
            .map_err(StoreError::from)?;
        names
    };
    tables
        .into_iter()
        .map(|t| columns_of(&conn, &t).map(|c| (t, c)))
        .collect()
}

/// A value `_verification_row` puts in its dict: a column as sqlite3
/// gives it, or a value it decoded.
#[derive(Clone, Debug, PartialEq)]
pub enum Field {
    Column(SqlValue),
    Py(PyValue),
}

/// `_verification_row`: the four key columns as read, the four lists
/// (`_load_json` of each `*_json` column, `[]` for null), then the path
/// targets the row's attestation carries. A column the row lacks is
/// IndexError.
pub fn verification_row(row: &Row) -> Result<Vec<(&'static str, Field)>, RecordError> {
    let at = |name: &str| row.item(name).ok_or(RecordError::Raised("IndexError"));
    let mut out = Vec::new();
    for name in ["memory_id", "machine_id", "verified_at", "verified_head"] {
        out.push((name, Field::Column(at(name)?.clone())));
    }
    for name in VERIFICATION_LISTS {
        let value = at(&format!("{name}_json"))?;
        out.push((
            name,
            Field::Py(load_json(value, PyValue::List(Vec::new()))?),
        ));
    }
    for (name, value) in path_targets(row)? {
        out.push((name, Field::Py(value)));
    }
    Ok(out)
}

/// `store_rows._path_targets`, whole: the target fields of the record the
/// row's `attestation_payload` carries, as parsed. The id check is made
/// only when the row names an `id`. `records::rows`' copy serves the
/// record readers, whose rows always name one, and keeps only the first
/// level of each map (all the model reads), so it is not reused here.
fn path_targets(row: &Row) -> Result<Vec<(&'static str, PyValue)>, RecordError> {
    // `in row.keys()` is exact; `row[name]` is sqlite3.Row's lookup.
    let payload = match row.item("attestation_payload") {
        _ if !row.has_key("attestation_payload") => return Ok(Vec::new()),
        None | Some(SqlValue::Null) => return Ok(Vec::new()),
        Some(v) => match load_json(v, PyValue::None) {
            Ok(p) => p,
            Err(RecordError::Raised(
                "JSONDecodeError" | "UnicodeDecodeError" | "ValueError" | "RecursionError",
            )) => return Ok(Vec::new()),
            Err(e) => return Err(e),
        },
    };
    let field = |v: &PyValue, name: &str| -> Option<PyValue> {
        let PyValue::Dict(items) = v else { return None };
        let name = unicode::cps(name);
        items
            .iter()
            .find(|(k, _)| matches!(k, PyValue::Str(s) if *s == name))
            .map(|(_, v)| v.clone())
    };
    if !matches!(payload, PyValue::Dict(_)) {
        return Ok(Vec::new());
    }
    let record = field(&payload, "memory")
        .or_else(|| field(&payload, "tombstone"))
        .unwrap_or_else(|| payload.clone());
    if !matches!(record, PyValue::Dict(_)) {
        return Ok(Vec::new());
    }
    if let Some(id) = row.item("id").filter(|_| row.has_key("id")) {
        if !json_eq_column(&field(&record, "id").unwrap_or(PyValue::None), id) {
            return Ok(Vec::new());
        }
    }
    Ok(["verified_path_targets", "verified_absent_path_targets"]
        .into_iter()
        .filter_map(|name| field(&record, name).map(|v| (name, v)))
        .collect())
}

/// Python's `==` between a parsed JSON value and a column value.
fn json_eq_column(a: &PyValue, b: &SqlValue) -> bool {
    let number = match a {
        PyValue::Bool(x) => Some(SqlValue::Integer(i64::from(*x))),
        PyValue::Int(digits) => match (digits.parse::<i64>(), b) {
            (Ok(i), _) => Some(SqlValue::Integer(i)),
            // Past i64: equal only to a float of exactly that value.
            (Err(_), SqlValue::Real(f)) => {
                return f.is_finite() && f.fract() == 0.0 && format!("{f:.0}") == *digits
            }
            (Err(_), _) => return false,
        },
        PyValue::Float(f) => Some(SqlValue::Real(*f)),
        _ => None,
    };
    match (a, b) {
        (PyValue::None, SqlValue::Null) => true,
        (PyValue::Str(s), SqlValue::Text(t)) => *s == unicode::cps(t),
        _ => match (number, b) {
            (Some(x), SqlValue::Integer(_) | SqlValue::Real(_)) => {
                matches!(compare(&x, b), Ok(Some(Ordering::Equal)))
            }
            _ => false,
        },
    }
}

/// `store_schema.MUTATION_KINDS`: `<table>_put` and `<table>_delete` of
/// the record tables and the membership tables.
pub const MUTATION_TABLES: [&str; 21] = [
    "memory",
    "tombstone",
    "episode",
    "verification",
    "conflict",
    "import",
    "quarantine",
    "machine",
    "project",
    "project_witness",
    "project_blob",
    "project_remote",
    "project_file",
    "declared_remote",
    "checkout",
    "checkout_view",
    "home",
    "proposal",
    "standing",
    "returned",
    "native_member",
];

/// Whether `kind` is in `MUTATION_KINDS`.
pub fn is_mutation(kind: &str) -> bool {
    kind.strip_suffix("_put")
        .or_else(|| kind.strip_suffix("_delete"))
        .is_some_and(|table| MUTATION_TABLES.contains(&table))
}

/// Whether `kind` is in `MEMBERSHIP_MUTATION_KINDS` (the membership
/// tables' `_put` and `_delete`).
pub fn is_membership_mutation(kind: &str) -> bool {
    kind.strip_suffix("_put")
        .or_else(|| kind.strip_suffix("_delete"))
        .is_some_and(|table| MUTATION_TABLES[7..].contains(&table))
}

/// `log.CONTROL_KINDS`, all four of its assignments together.
pub const CONTROL_KINDS: [&str; 13] = [
    "store_created",
    "rekey",
    "migrate_v8",
    "capture_open",
    "capture_member",
    "capture_close",
    "capture_undo",
    "capture_relations",
    "index_rebuilt",
    "store_restored",
    "history_accepted",
    "key_imported",
    "key_adopted",
];

/// `store_schema.MEMBERSHIP_EVENT_KINDS`.
pub const MEMBERSHIP_EVENT_KINDS: [&str; 30] = [
    "resolution_indeterminate",
    "resolution_deferred",
    "resolution_session",
    "resolution_joined",
    "resolution_new",
    "proposal_opened",
    "proposal_answered",
    "checkout_registered",
    "checkout_moved",
    "checkout_view_added",
    "checkout_rekeyed",
    "checkout_object_refreshed",
    "checkout_retired",
    "token_superseded",
    "token_reminted",
    "remote_declared",
    "project_minted",
    "project_merged",
    "project_absorbed",
    "project_unmerged",
    "project_file_bound",
    "project_reparented",
    "machine_cloned",
    "machine_facts_updated",
    "witness_put",
    "witness_released",
    "import_homed",
    "recall_suppressed",
    "membership_migrated",
    "store_forked",
];

/// Whether `kind` is in `store_schema._NOT_TELEMETRY`
/// (`MUTATION_KINDS | CONTROL_KINDS | MEMBERSHIP_EVENT_KINDS`).
pub fn not_telemetry(kind: &str) -> bool {
    is_mutation(kind) || CONTROL_KINDS.contains(&kind) || MEMBERSHIP_EVENT_KINDS.contains(&kind)
}

/// `_event_from_columns`: a log row's columns as the v8 event (`ts`,
/// `session` when not None, `kind`, then the payload's other keys in its
/// order), or None for a kind that is not telemetry and for a payload
/// that is not a JSON object. `json.loads` refusing the payload with a
/// ValueError (a JSONDecodeError, a UnicodeDecodeError of bytes, too
/// many digits) is None; TypeError (null, a number) and RecursionError
/// propagate.
pub fn event_from_columns(
    ts: &[u32],
    session: Option<&[u32]>,
    kind: &[u32],
    payload: &SqlValue,
) -> Result<Option<PyValue>, RecordError> {
    if unicode::to_string(kind).is_some_and(|k| not_telemetry(&k)) {
        return Ok(None);
    }
    if matches!(payload, SqlValue::Null) {
        return Err(RecordError::Raised("TypeError"));
    }
    let fields = match load_json(payload, PyValue::None) {
        Ok(v) => v,
        Err(RecordError::Raised("JSONDecodeError" | "UnicodeDecodeError" | "ValueError")) => {
            return Ok(None)
        }
        Err(e) => return Err(e),
    };
    let PyValue::Dict(items) = &fields else {
        return Ok(None);
    };
    let mut event = vec![(PyValue::str("ts"), PyValue::Str(ts.to_vec()))];
    if let Some(session) = session {
        event.push((PyValue::str("session"), PyValue::Str(session.to_vec())));
    }
    event.push((PyValue::str("kind"), PyValue::Str(kind.to_vec())));
    for (key, value) in items {
        let reserved = matches!(key, PyValue::Str(k)
            if ["ts", "session", "kind"].iter().any(|r| unicode::cps(r) == *k));
        if !reserved {
            event.push((key.clone(), value.clone()));
        }
    }
    Ok(Some(PyValue::Dict(event)))
}

/// `store_schema.IMPORTED_FROM_V8`.
pub const IMPORTED_FROM_V8: &str = "v8";

/// `store_rows._ROW_COLUMNS`: the fields an event keeps in the row's own
/// columns.
pub const ROW_COLUMNS: [&str; 3] = ["ts", "session", "kind"];

/// What `event_import_payload` answers: `(ts, session, kind, payload)`.
pub type ImportedEvent = (Option<PyStr>, Option<PyStr>, PyStr, Vec<(PyValue, PyValue)>);

/// `event_import_payload(event, imported_from=...)`, with
/// `credentials.secret_spans` passed as `spans`: the row a v8 event
/// becomes. `Err` is the `ValueError`'s text: a kind that is not a
/// non-empty `str`, or one in `_NOT_TELEMETRY` (by its `repr`). A `ts` or
/// `session` that is not a non-empty `str` is None; every field but the
/// three columns goes through `_redact_event_fields`, then
/// `imported_from` is set (in place when the event held that key).
pub fn event_import_payload(
    event: &[(PyValue, PyValue)],
    imported_from: &[u32],
    spans: crate::event_redact::Spans,
) -> Result<ImportedEvent, PyStr> {
    let get = |name: &str| {
        let name = unicode::cps(name);
        event.iter().find_map(|(k, v)| match k {
            PyValue::Str(s) if *s == name => Some(v),
            _ => None,
        })
    };
    let kind = match get("kind") {
        Some(PyValue::Str(k)) if !k.is_empty() => k.clone(),
        _ => return Err(unicode::cps("an event needs a string kind")),
    };
    if unicode::to_string(&kind).is_some_and(|k| not_telemetry(&k)) {
        let mut text = repr_str(&kind);
        text.extend(unicode::cps(" is a mutation or control kind, not an event"));
        return Err(text);
    }
    let text_of = |v: Option<&PyValue>| match v {
        Some(PyValue::Str(s)) if !s.is_empty() => Some(s.clone()),
        _ => None,
    };
    let rest: Vec<(PyValue, PyValue)> = event
        .iter()
        .filter(|(k, _)| !matches!(k, PyValue::Str(s) if ROW_COLUMNS.iter().any(|c| unicode::cps(c) == *s)))
        .cloned()
        .collect();
    let mut payload = crate::event_redact::redact_event_fields(&rest, spans);
    let from = PyValue::Str(imported_from.to_vec());
    match payload
        .iter_mut()
        .find(|(k, _)| matches!(k, PyValue::Str(s) if *s == unicode::cps("imported_from")))
    {
        Some(slot) => slot.1 = from,
        None => payload.push((PyValue::str("imported_from"), from)),
    }
    Ok((text_of(get("ts")), text_of(get("session")), kind, payload))
}

/// `Store._event_from_raw`: `str()` of the row's `ts` and `kind`, of
/// `session` when not null, and the payload as read.
pub fn event_from_raw(raw: &Row) -> Result<Option<PyValue>, RecordError> {
    let at = |name: &str| raw.item(name).ok_or(RecordError::Raised("IndexError"));
    let ts = unicode::cps(&at("ts")?.py_str());
    let session = match at("session")? {
        SqlValue::Null => None,
        v => Some(unicode::cps(&v.py_str())),
    };
    let kind = unicode::cps(&at("kind")?.py_str());
    let payload = at("payload")?;
    event_from_columns(&ts, session.as_deref(), &kind, payload)
}
