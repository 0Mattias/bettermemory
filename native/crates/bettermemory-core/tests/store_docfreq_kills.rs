//! Cases a plausible wrong document frequency memo fails, each read off
//! `store_index.py`: the bounds of `_row_mask`, `_PATCH_ROWS` and
//! `_PATCH_WINDOW`, the body query asked only when a changed row matched,
//! the second head read before keeping, an entry replaced meanwhile, the
//! admit predicate run for an empty group, the average's absence without
//! lengths, and the terms' and stores' least recently used order.

use std::cell::{Cell, RefCell};
use std::collections::HashMap;
use std::rc::Rc;

use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::store::docfreq::{
    admitted_mask, int_div, patched_masks, row_mask, AdmissionRow, DocActor, DocFreqError,
    DocFreqInputs, DocFreqMemo, DocFreqSource, Group, Head, Mask, PyInt, Ranked, PATCH_ROWS,
    PATCH_WINDOW,
};

/// A scripted store: its rows, each term's body and scopes hits by row
/// number, its head, and the calls made to it.
#[derive(Default)]
struct Script {
    rows: Vec<AdmissionRow>,
    digest: bool,
    body: HashMap<String, Vec<i64>>,
    scopes: HashMap<String, Vec<i64>>,
    head: Cell<i64>,
    /// The head the n-th read answers, past the first.
    later_head: Cell<Option<i64>>,
    heads: Cell<usize>,
    calls: RefCell<Vec<String>>,
    /// What `advanced_inputs` answers next, in order; None once empty.
    advanced: RefCell<Vec<DocFreqInputs>>,
    /// Run once inside the next `term_masks` call.
    hook: RefCell<Option<Box<dyn FnOnce()>>>,
}

impl Script {
    fn row(&mut self, rid: i64, scopes: &str) {
        self.rows.push(AdmissionRow {
            rid: rid.into(),
            scopes_text: scopes.into(),
            home_project: None,
            client: None,
            model: None,
            length: 2,
        });
    }

    fn calls(&self) -> Vec<String> {
        self.calls.borrow_mut().drain(..).collect()
    }
}

impl DocFreqSource for Script {
    fn memory_head(&self) -> Result<Head, DocFreqError> {
        self.heads.set(self.heads.get() + 1);
        if self.heads.get() > 1 {
            if let Some(h) = self.later_head.get() {
                return Ok((h, "m".into()));
            }
        }
        Ok((self.head.get(), "m".into()))
    }
    fn store_key(&self) -> (String, String) {
        ("/s".into(), "id".into())
    }
    fn in_transaction(&self) -> bool {
        false
    }
    fn admission_rows(&self) -> Result<(Vec<AdmissionRow>, bool), DocFreqError> {
        self.calls.borrow_mut().push("read".into());
        Ok((self.rows.clone(), self.digest))
    }
    fn term_masks(&self, term: &str, inputs: &DocFreqInputs) -> Result<(Mask, Mask), DocFreqError> {
        self.calls.borrow_mut().push(format!("tm:{term}"));
        if let Some(hook) = self.hook.borrow_mut().take() {
            hook();
        }
        let pos = |rids: Option<&Vec<i64>>| -> Vec<usize> {
            rids.into_iter()
                .flatten()
                .filter_map(|r| inputs.index.get(&PyInt::Small(*r)).copied())
                .collect()
        };
        let body = row_mask(&pos(self.body.get(term)), inputs.index.len())?;
        let mut either = body.clone();
        either.or_with(&row_mask(&pos(self.scopes.get(term)), inputs.index.len())?);
        Ok((body, either))
    }
    fn advanced_inputs(
        &self,
        _entry: &DocFreqInputs,
        _head: &Head,
    ) -> Result<Option<DocFreqInputs>, DocFreqError> {
        let mut queue = self.advanced.borrow_mut();
        Ok((!queue.is_empty()).then(|| queue.remove(0)))
    }
    fn fts_rowids(&self, expr: &str, low: i64, limit: usize) -> Result<Vec<i64>, DocFreqError> {
        self.calls.borrow_mut().push(format!("fts:{expr}"));
        let term = expr.trim_start_matches("body_fts : ").trim_matches('"');
        let mut hits: Vec<i64> = self.body.get(term).cloned().unwrap_or_default();
        if !expr.starts_with("body_fts") {
            hits.extend(self.scopes.get(term).cloned().unwrap_or_default());
        }
        hits.sort_unstable_by(|a, b| b.cmp(a));
        hits.dedup();
        hits.retain(|r| *r >= low);
        hits.truncate(limit);
        Ok(hits)
    }
}

fn all(_: &[PyValue], _: Option<&str>, _: Option<&DocActor>) -> bool {
    true
}

fn terms(t: &[&str]) -> Vec<String> {
    t.iter().map(|s| s.to_string()).collect()
}

#[test]
fn row_mask_fails_past_the_bytes_not_past_the_width() {
    assert_eq!(row_mask(&[7], 1).unwrap().count(), 1);
    assert_eq!(row_mask(&[8], 1), Err(DocFreqError::IndexError));
    assert_eq!(row_mask(&[0], 0), Err(DocFreqError::IndexError));
    assert_eq!(row_mask(&[15], 9).unwrap().count(), 1);
    // The bound is taken without overflow, so debug and release agree.
    assert_eq!(row_mask(&[0], usize::MAX).unwrap().count(), 1);
}

#[test]
fn a_duplicate_row_number_reuses_a_position() {
    // `index[rid] = position` overwrites: the third row takes position 1,
    // the second row's, so the mask holds two bits for three rows.
    let rows: Vec<AdmissionRow> = [(5, "[]"), (5, "[]"), (6, "[]")]
        .iter()
        .map(|&(rid, s)| AdmissionRow {
            rid: PyInt::Small(rid),
            scopes_text: s.into(),
            home_project: None,
            client: None,
            model: None,
            length: 1,
        })
        .collect();
    let inputs = DocFreqInputs::from_rows((1, "m".into()), &rows, true).unwrap();
    assert_eq!(inputs.index.len(), 2);
    assert_eq!(inputs.groups[0].mask.count(), 2);
    assert_eq!(inputs.lengths, Some(vec![3]));
}

#[test]
fn scopes_that_are_not_json_raise_and_not_a_list_skip() {
    let mut s = Script::default();
    s.row(1, "{}");
    s.row(2, "None");
    let rows = s.rows.clone();
    assert!(matches!(
        DocFreqInputs::from_rows((0, String::new()), &rows, true),
        Err(DocFreqError::Py(_))
    ));
    let inputs = DocFreqInputs::from_rows((0, String::new()), &rows[..1], true).unwrap();
    assert!(inputs.index.is_empty() && inputs.groups.is_empty());
}

#[test]
fn admit_runs_for_an_empty_group() {
    let inputs = DocFreqInputs {
        head: (1, "m".into()),
        groups: vec![Group {
            scopes: vec![],
            home_project: None,
            actor: None,
            mask: Mask::default(),
        }],
        index: HashMap::new(),
        lengths: Some(vec![7]),
        keys: vec![],
        group_of: None,
        length_of: None,
        version: 0,
        changed: vec![],
    };
    let mut n = 0;
    let (mask, length) = admitted_mask(&inputs, &mut |_, _, _| {
        n += 1;
        true
    });
    assert_eq!((n, mask.is_zero(), length), (1, true, 7));
}

fn patch_inputs(changed: usize) -> DocFreqInputs {
    let mut index = HashMap::new();
    for rid in 0..200 {
        index.insert(PyInt::Small(rid), rid as usize);
    }
    DocFreqInputs {
        head: (1, "m".into()),
        groups: vec![],
        index,
        lengths: None,
        keys: vec![],
        group_of: None,
        length_of: None,
        version: 1,
        changed: (0..changed)
            .map(|i| (1, PyInt::Small(100 + i as i64), 100 + i))
            .collect(),
    }
}

#[test]
fn eight_changed_rows_patch_and_nine_read_whole() {
    let s = Script::default();
    let old = (Mask::default(), Mask::default(), 0);
    patched_masks(&s, "w", &old, &patch_inputs(PATCH_ROWS)).unwrap();
    assert_eq!(s.calls(), ["fts:\"w\""]);
    patched_masks(&s, "w", &old, &patch_inputs(PATCH_ROWS + 1)).unwrap();
    assert_eq!(s.calls(), ["tm:w"]);
}

#[test]
fn the_window_holds_sixty_four_rows_and_not_sixty_five() {
    let mut s = Script::default();
    s.body
        .insert("w".into(), (100..100 + PATCH_WINDOW as i64).collect());
    let old = (Mask::default(), Mask::default(), 0);
    let got = patched_masks(&s, "w", &old, &patch_inputs(1)).unwrap();
    assert_eq!(s.calls(), ["fts:\"w\"", "fts:body_fts : \"w\""]);
    assert!(got.0.get(100) && got.1.get(100));
    s.body.get_mut("w").unwrap().push(199);
    patched_masks(&s, "w", &old, &patch_inputs(1)).unwrap();
    assert_eq!(s.calls(), ["fts:\"w\"", "tm:w"]);
}

#[test]
fn the_body_is_asked_only_when_a_changed_row_matched() {
    let mut s = Script::default();
    s.scopes.insert("w".into(), vec![150]);
    let mut stale = (Mask::default(), Mask::default(), 0);
    stale.0.set(100);
    stale.1.set(100);
    let got = patched_masks(&s, "w", &stale, &patch_inputs(1)).unwrap();
    assert_eq!(s.calls(), ["fts:\"w\""]);
    // The changed row's bits are cleared, as the reference's `& ~bit`.
    assert!(!got.0.get(100) && !got.1.get(100));
    assert_eq!(got.2, 1);
}

#[test]
fn masks_read_at_this_version_need_no_patch() {
    let s = Script::default();
    let now = (Mask::default(), Mask::default(), 1);
    let got = patched_masks(&s, "w", &now, &patch_inputs(3)).unwrap();
    assert!(s.calls().is_empty());
    assert_eq!(got.2, 1);
}

#[test]
fn a_head_that_moved_meanwhile_keeps_nothing() {
    let mut s = Script {
        digest: true,
        ..Script::default()
    };
    s.row(1, "[\"a\"]");
    s.body.insert("w".into(), vec![1]);
    s.later_head.set(Some(9));
    let memo = DocFreqMemo::new();
    let got = memo
        .ranking_statistics(&s, &terms(&["w"]), &mut all)
        .unwrap();
    assert!(matches!(got, Ranked::Found((1, _, _, Some(a))) if a == 2.0));
    assert!(memo.keys().is_empty());
}

#[test]
fn no_lengths_no_average_and_no_admitted_row_no_answer() {
    let mut s = Script::default();
    s.row(1, "[]");
    let memo = DocFreqMemo::new();
    let got = memo
        .ranking_statistics(&s, &terms(&["w"]), &mut all)
        .unwrap();
    assert_eq!(got, Ranked::Found((1, vec![], vec![], None)));
    // Nothing admitted: no term read, the fresh entry kept all the same.
    let s2 = Script::default();
    let memo2 = DocFreqMemo::new();
    let got = memo2
        .ranking_statistics(&s2, &terms(&["w"]), &mut all)
        .unwrap();
    assert_eq!(got, Ranked::Nothing);
    assert_eq!(s2.calls(), ["read"]);
    assert_eq!(memo2.keys().len(), 1);
}

#[test]
fn a_repeated_term_is_read_twice_when_called_directly() {
    let mut s = Script::default();
    s.row(1, "[]");
    let memo = DocFreqMemo::new();
    memo.document_frequencies(&s, &terms(&["w", "w"]), &mut all)
        .unwrap();
    assert_eq!(s.calls(), ["read", "tm:w", "tm:w"]);
    // Through `ranking_statistics` the terms are sorted and unique.
    memo.clear();
    memo.ranking_statistics(&s, &terms(&["w", "", "w"]), &mut all)
        .unwrap();
    assert_eq!(s.calls(), ["read", "tm:w"]);
}

#[test]
fn a_hit_moves_the_term_to_the_end() {
    let mut s = Script::default();
    s.row(1, "[]");
    let memo = DocFreqMemo::with_bounds(8, 2);
    memo.ranking_statistics(&s, &terms(&["a", "b"]), &mut all)
        .unwrap();
    memo.ranking_statistics(&s, &terms(&["a"]), &mut all)
        .unwrap();
    memo.ranking_statistics(&s, &terms(&["c"]), &mut all)
        .unwrap();
    let entry = memo.entry(&s.store_key()).unwrap();
    assert_eq!(entry.terms(), ["a", "c"]);
}

fn at_version(
    s: &Script,
    head: i64,
    version: u64,
    changed: Vec<(u64, i64, usize)>,
) -> DocFreqInputs {
    let mut inputs = DocFreqInputs::from_rows((head, "m".into()), &s.rows, s.digest).unwrap();
    inputs.version = version;
    inputs.changed = changed
        .into_iter()
        .map(|(v, rid, p)| (v, PyInt::Small(rid), p))
        .collect();
    inputs
}

#[test]
fn only_masks_at_exactly_this_version_are_known() {
    // A history that moved back leaves a term kept at a later version
    // than the entry's: it is behind (`masks[2] == version` fails), so it
    // is patched and kept at the entry's version.
    let mut s = Script {
        digest: true,
        ..Script::default()
    };
    s.row(1, "[\"a\"]");
    s.body.insert("w".into(), vec![1]);
    let memo = DocFreqMemo::new();
    let w = terms(&["w"]);
    memo.ranking_statistics(&s, &w, &mut all).unwrap();
    for (head, version, changed) in [(2, 5, vec![]), (3, 3, vec![]), (4, 4, vec![(4, 1, 0)])] {
        s.calls();
        let next = at_version(&s, head, version, changed);
        s.advanced.borrow_mut().push(next);
        s.head.set(head);
        memo.ranking_statistics(&s, &w, &mut all).unwrap();
    }
    assert_eq!(s.calls(), ["fts:\"w\"", "fts:body_fts : \"w\""]);
}

#[test]
fn a_stale_query_keeps_nothing_on_an_entry_replaced_meanwhile() {
    let mut s = Script {
        digest: true,
        ..Script::default()
    };
    s.row(1, "[\"a\"]");
    let mut later = Script {
        digest: true,
        ..Script::default()
    };
    later.row(1, "[\"a\"]");
    later.head.set(2);
    let later = Rc::new(later);
    let memo = Rc::new(DocFreqMemo::new());
    memo.ranking_statistics(&s, &terms(&["w"]), &mut all)
        .unwrap();
    // While the stale query reads "x", a query at head 2 replaces the
    // store's entry.
    let (m, l) = (memo.clone(), later.clone());
    *s.hook.borrow_mut() = Some(Box::new(move || {
        m.ranking_statistics(&*l, &terms(&["y"]), &mut all).unwrap();
    }));
    memo.ranking_statistics(&s, &terms(&["x"]), &mut all)
        .unwrap();
    later.calls();
    // The entry at head 2 stays, with "y" known and nothing read again.
    memo.ranking_statistics(&*later, &terms(&["y"]), &mut all)
        .unwrap();
    assert!(later.calls().is_empty());
}

fn double(exponent: u64, fraction: u64) -> f64 {
    f64::from_bits(((exponent + 1023) << 52) | fraction)
}

#[test]
fn the_average_length_is_rounded_once_past_two_to_the_fifty_third() {
    // Python: (2**53 + 1) / 3 == 0x1.5555555555556p+51; rounding the sum
    // to a double first gives 0x1.5555555555555p+51.
    let mut s = Script {
        digest: true,
        ..Script::default()
    };
    for rid in 1..=3 {
        s.row(rid, "[\"a\"]");
    }
    s.rows[0].length = (1 << 53) + 1;
    s.rows[1].length = 0;
    s.rows[2].length = 0;
    s.body.insert("w".into(), vec![1]);
    let got = DocFreqMemo::new()
        .ranking_statistics(&s, &terms(&["w"]), &mut all)
        .unwrap();
    let Ranked::Found((3, _, _, Some(average))) = got else {
        panic!("{got:?}");
    };
    assert_eq!(average.to_bits(), double(51, 0x5555555555556).to_bits());
    // Python: (3 * 2**62 + 5) / 7, -(2**100 + 1) / (2**63 - 1), 7 / 2.
    assert_eq!(int_div(3 * (1 << 62) + 5, 7), double(60, 0xb6db6db6db6db));
    assert_eq!(int_div(-((1 << 100) + 1), (1 << 63) - 1), -double(37, 0));
    assert_eq!(int_div(7, 2), 3.5);
    assert_eq!(int_div(0, 5), 0.0);
}
