//! Hostile inputs to the document frequency memo: scopes nested deep, a
//! row number at each end of `i64`, a position far past the width, masks
//! of many rows, lengths whose sum passes `i64`, many threads on one
//! memo, and the time of a query over ten times the rows against the
//! time over the rows (`timing::grows_within`).

mod timing;

use std::sync::Arc;

use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::store::docfreq::{
    load_scopes, row_mask, AdmissionRow, DocActor, DocFreqError, DocFreqInputs, DocFreqMemo,
    DocFreqSource, Head, Mask, PyInt, Ranked,
};

struct Rows {
    rows: Vec<AdmissionRow>,
    hits: Vec<i64>,
}

impl DocFreqSource for Rows {
    fn memory_head(&self) -> Result<Head, DocFreqError> {
        Ok((1, "m".into()))
    }
    fn store_key(&self) -> (String, String) {
        (format!("/s{}", self.rows.len()), "id".into())
    }
    fn in_transaction(&self) -> bool {
        false
    }
    fn admission_rows(&self) -> Result<(Vec<AdmissionRow>, bool), DocFreqError> {
        Ok((self.rows.clone(), true))
    }
    fn term_masks(&self, _: &str, inputs: &DocFreqInputs) -> Result<(Mask, Mask), DocFreqError> {
        let p: Vec<usize> = self
            .hits
            .iter()
            .filter_map(|r| inputs.index.get(&PyInt::Small(*r)).copied())
            .collect();
        let m = row_mask(&p, inputs.index.len())?;
        Ok((m.clone(), m))
    }
    fn fts_rowids(&self, _: &str, _: i64, _: usize) -> Result<Vec<i64>, DocFreqError> {
        Ok(vec![])
    }
}

fn rows(n: usize, length: i64) -> Rows {
    Rows {
        rows: (0..n)
            .map(|i| AdmissionRow {
                rid: PyInt::Small(i as i64),
                scopes_text: format!("[\"s{}\"]", i % 7),
                home_project: None,
                client: None,
                model: None,
                length,
            })
            .collect(),
        hits: (0..n as i64).step_by(3).collect(),
    }
}

fn all(_: &[PyValue], _: Option<&str>, _: Option<&DocActor>) -> bool {
    true
}

#[test]
fn deep_scopes_raise_and_do_not_overflow_the_stack() {
    let deep = "[".repeat(100_000) + &"]".repeat(100_000);
    assert!(matches!(load_scopes(&deep), Err(DocFreqError::Py(_))));
}

#[test]
fn row_numbers_at_the_ends_of_i64() {
    let mut r = rows(0, 1);
    for rid in [i64::MIN, -1, 0, i64::MAX] {
        r.rows.push(AdmissionRow {
            rid: rid.into(),
            scopes_text: "[]".into(),
            home_project: None,
            client: None,
            model: None,
            length: 1,
        });
    }
    r.hits = vec![i64::MIN, i64::MAX];
    let memo = DocFreqMemo::new();
    let got = memo
        .ranking_statistics(&r, &["t".into()], &mut all)
        .unwrap();
    assert_eq!(
        got,
        Ranked::Found((4, vec![("t".into(), 2)], vec![("t".into(), 2)], Some(1.0)))
    );
}

#[test]
fn a_position_far_past_the_width_is_an_error() {
    assert_eq!(row_mask(&[usize::MAX], 8), Err(DocFreqError::IndexError));
    let mut m = Mask::default();
    m.clear(usize::MAX);
    assert!(m.is_zero());
}

#[test]
fn lengths_past_i64_sum_without_overflow() {
    let r = rows(4, i64::MAX);
    let memo = DocFreqMemo::new();
    let Ranked::Found((size, _, _, Some(average))) = memo
        .ranking_statistics(&r, &["t".into()], &mut all)
        .unwrap()
    else {
        panic!("an answer")
    };
    assert_eq!(size, 4);
    assert_eq!(average, i64::MAX as f64);
}

#[test]
fn many_threads_share_one_memo() {
    let memo = Arc::new(DocFreqMemo::new());
    let handles: Vec<_> = (0..8)
        .map(|t| {
            let memo = memo.clone();
            std::thread::spawn(move || {
                let r = rows(50 + t, 1);
                for i in 0..200 {
                    let term = format!("t{}", i % 37);
                    let got = memo.ranking_statistics(&r, &[term], &mut all).unwrap();
                    assert!(matches!(got, Ranked::Found((n, _, _, _)) if n == 50 + t));
                }
            })
        })
        .collect();
    for h in handles {
        h.join().unwrap();
    }
    assert!(memo.keys().len() <= 8);
}

#[test]
fn a_query_grows_with_the_rows() {
    let small = rows(2_000, 3);
    let large = rows(20_000, 3);
    let terms: Vec<String> = (0..20).map(|i| format!("t{i}")).collect();
    let run = |r: &Rows| {
        let memo = DocFreqMemo::new();
        timing::once(|| memo.ranking_statistics(r, &terms, &mut all).unwrap())
    };
    timing::grows_within(
        "store_docfreq query",
        timing::TENFOLD,
        &mut || run(&small),
        &mut || run(&large),
    );
}
