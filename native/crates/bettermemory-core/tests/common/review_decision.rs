use bettermemory_core::pyvalue::PyStr;
use bettermemory_core::review::{RelatedEntry, ReviewAnswers};

pub fn answers(relation: Vec<(PyStr, PyStr)>, unsafe_score: f64, secret: f64) -> ReviewAnswers {
    ReviewAnswers {
        relation,
        r#unsafe: unsafe_score,
        secret,
        review_id: None,
        standing: None,
        said: None,
        named: None,
    }
}

pub fn entry(id: PyStr, claim_sized: bool) -> RelatedEntry {
    RelatedEntry {
        id,
        scopes: vec![],
        updated: String::new(),
        claim_sized,
        text: vec![],
    }
}
