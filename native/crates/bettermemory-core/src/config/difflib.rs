//! `difflib.get_close_matches` with `n=1`, as `config check`'s
//! did-you-mean (`config_cmd._close`) calls it: `SequenceMatcher` with no
//! junk function and `autojunk=True`, over code points.

use std::collections::HashMap;

/// `SequenceMatcher(None, a, b).ratio()`.
fn ratio(a: &[u32], b: &[u32], b2j: &HashMap<u32, Vec<usize>>) -> f64 {
    let mut rows = Rows::new(b.len());
    let mut matches = 0usize;
    let mut queue = vec![(0usize, a.len(), 0usize, b.len())];
    while let Some((alo, ahi, blo, bhi)) = queue.pop() {
        let (i, j, k) = find_longest_match(a, b, b2j, &mut rows, (alo, ahi, blo, bhi));
        if k > 0 {
            matches += k;
            if alo < i && blo < j {
                queue.push((alo, i, blo, j));
            }
            if i + k < ahi && j + k < bhi {
                queue.push((i + k, ahi, j + k, bhi));
            }
        }
    }
    let length = a.len() + b.len();
    if length == 0 {
        1.0
    } else {
        2.0 * matches as f64 / length as f64
    }
}

/// `find_longest_match`'s `j2len` and `newj2len` dicts as two dense rows
/// indexed by `j + 1`, with the entries each row set listed, so a row is
/// cleared in the time it took to fill.
struct Rows {
    old: Vec<usize>,
    new: Vec<usize>,
    old_set: Vec<usize>,
    new_set: Vec<usize>,
}

impl Rows {
    fn new(len: usize) -> Rows {
        Rows {
            old: vec![0; len + 1],
            new: vec![0; len + 1],
            old_set: Vec::new(),
            new_set: Vec::new(),
        }
    }

    fn clear_old(&mut self) {
        for &j in &self.old_set {
            self.old[j] = 0;
        }
        self.old_set.clear();
    }

    /// `j2len = newj2len`, and a fresh `newj2len`.
    fn advance(&mut self) {
        self.clear_old();
        std::mem::swap(&mut self.old, &mut self.new);
        std::mem::swap(&mut self.old_set, &mut self.new_set);
    }
}

/// `SequenceMatcher.find_longest_match`, with no junk: popular elements
/// are out of `b2j` but still extend a match at its ends.
fn find_longest_match(
    a: &[u32],
    b: &[u32],
    b2j: &HashMap<u32, Vec<usize>>,
    rows: &mut Rows,
    (alo, ahi, blo, bhi): (usize, usize, usize, usize),
) -> (usize, usize, usize) {
    let (mut besti, mut bestj, mut bestsize) = (alo, blo, 0usize);
    rows.clear_old();
    for (i, elt) in a.iter().enumerate().take(ahi).skip(alo) {
        if let Some(js) = b2j.get(elt) {
            for &j in js {
                if j < blo {
                    continue;
                }
                if j >= bhi {
                    break;
                }
                // rows.old[j] is j2len[j - 1].
                let k = rows.old[j] + 1;
                rows.new[j + 1] = k;
                rows.new_set.push(j + 1);
                if k > bestsize {
                    besti = i + 1 - k;
                    bestj = j + 1 - k;
                    bestsize = k;
                }
            }
        }
        rows.advance();
    }
    rows.clear_old();
    while besti > alo && bestj > blo && a[besti - 1] == b[bestj - 1] {
        besti -= 1;
        bestj -= 1;
        bestsize += 1;
    }
    while besti + bestsize < ahi
        && bestj + bestsize < bhi
        && a[besti + bestsize] == b[bestj + bestsize]
    {
        bestsize += 1;
    }
    (besti, bestj, bestsize)
}

/// `difflib.get_close_matches(word, possibilities, n=1, cutoff=cutoff)`:
/// the possibility with the highest ratio at or above `cutoff` (the later
/// string, compared as Python compares, on a tie), or None.
pub fn get_close_match(word: &[u32], possibilities: &[Vec<u32>], cutoff: f64) -> Option<usize> {
    let mut b2j: HashMap<u32, Vec<usize>> = HashMap::new();
    for (i, &elt) in word.iter().enumerate() {
        b2j.entry(elt).or_default().push(i);
    }
    let n = word.len();
    if n >= 200 {
        let ntest = n / 100 + 1;
        b2j.retain(|_, idxs| idxs.len() <= ntest);
    }
    let mut best: Option<(f64, usize)> = None;
    for (index, x) in possibilities.iter().enumerate() {
        let score = ratio(x, word, &b2j);
        if score < cutoff {
            continue;
        }
        let better = match best {
            None => true,
            Some((s, bi)) => {
                score > s || (score == s && x.as_slice() > possibilities[bi].as_slice())
            }
        };
        if better {
            best = Some((score, index));
        }
    }
    best.map(|(_, i)| i)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::unicode::cps;

    #[test]
    fn close_matches_as_difflib_finds_them() {
        let options: Vec<Vec<u32>> = ["storage", "behavior", "scopes"]
            .iter()
            .map(|s| cps(s))
            .collect();
        assert_eq!(get_close_match(&cps("behaviour"), &options, 0.6), Some(1));
        assert_eq!(get_close_match(&cps("zzz"), &options, 0.6), None);
    }
}
